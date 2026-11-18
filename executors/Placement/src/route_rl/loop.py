"""六步循环（埋点版）：①编译 DSN → ②取状态 → ③FreeRouting → ④SES 导入+清理
→ ⑤引擎 DRC → ⑥记三元组。

执行器可换（阶段一两阶段 MDP 迁移）：engine="freerouting"（缺省，DSN⇄SES
字节等价路径不动）或 "negotiated"（kicad_access.route.engine 协商引擎，①③④
换一条子进程直出板文件，②⑤⑥ 与记录格式不动——考官口径天然与基线一致）。
spec 变换映射见 run_round engine 分支注释。

Controller 是 S1 的动作源接口预留：v0 = LadderController（静态 clearance 阶梯），
R1 换离线 CQL 控制器时只替换该对象，循环与记录格式不动。
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

from . import env, metrics, records
from .dsn_edit import (del_nets, drop_pins, fix_net_wiring, fix_wiring,
                       force_clearance, keep_only_nets, lock_layer,
                       protect_wiring, shrink_npth_keepout,
                       skeleton_keepouts, split_power_class)
from .mdp import CLEARANCE_GRID, obs_vector, unflatten_action
from .stackup import PassSpec, Stackup


def _say_unrouted(r) -> None:
    """把布线器自己写的「哪根网、从哪个脚到哪个脚没布通」打进日志。

    FreeRouting 每轮都会写这种行：
        Net 'MCU_VCAP' (1 unrouted connection): C10-1 -> U2-22

    此前这里只留 stdout 的**最后一行**（`log_tail`），前面这些行全丢——
    2026-10-29 mcu_stm32f411 卡在「未连通 1」，只能靠五组对照实验反推是哪根网。
    答案一直在日志里，只是没被读出来。
    """
    txt = (getattr(r, "stdout", "") or "") + "\n" + (getattr(r, "stderr", "") or "")
    lines = [l.strip() for l in txt.splitlines() if "unrouted connection" in l]
    if not lines:
        return
    print(f"  未布通明细（布线器原话，{len(lines)} 条）：", flush=True)
    for l in lines[:20]:
        print("    " + l, flush=True)
    if len(lines) > 20:
        print(f"    …另有 {len(lines) - 20} 条", flush=True)


# --------------------------------------------------------------------------
# 动作源（S1 §10 接口预留）
# --------------------------------------------------------------------------
class Controller:
    """动作源接口：reset 每 pass 一次；act(状态)→动作；observe 收回合记录。"""

    def reset(self, board_id: str, pass_name: str,
              signal_layers: list[str] | None = None) -> None:
        pass

    def act(self, state: dict) -> dict:
        raise NotImplementedError

    def observe(self, record: dict) -> None:
        pass

    def done(self) -> bool:
        return False


class LadderController(Controller):
    """v0 静态阶梯：依序吐 clearance，跑完即止。等价于 legacy 阶梯扫描，
    差别只在每轮都落三元组。"""

    def __init__(self, clearances: list[int]):
        self.clearances = list(clearances)
        self._i = 0

    def reset(self, board_id: str, pass_name: str,
              signal_layers: list[str] | None = None) -> None:
        self._i = 0

    def act(self, state: dict) -> dict:
        a = {"clearance_um": self.clearances[self._i]}
        self._i += 1
        return a

    def done(self) -> bool:
        return self._i >= len(self.clearances)


class RandomController(Controller):
    """R1 采集动作源：按 mdp 网格随机采样，K 轮封顶。

    - clearance：CLEARANCE_GRID 均匀 13 档
    - layer_act：信号层激活子集（位掩码，bit i ↔ signal_layers[i]）。加权
      采样——全集 50%（等价缺省，不写槽）/ 真子集均分 50%；大面积锁层会
      把多数轮变成纯失败，压着采才能既覆盖又不淹没数据
    - via_ladder / edge_inset：未接线，槽恒 0（诚实记录，不留假维度）
    seed 决定整个 episode 的动作序列（random.Random 接受 str 种子，
    逐 episode 派生保证可复现）。
    """

    def __init__(self, seed, k_min: int = 3, k_max: int = 8):
        self._seed = seed
        self._k_min, self._k_max = k_min, k_max
        self._rng = random.Random(seed)
        self._layers: list[str] = []
        self._i = 0
        self._k = 0

    def reset(self, board_id: str, pass_name: str,
              signal_layers: list[str] | None = None) -> None:
        self._rng = random.Random(self._seed)     # 同 seed 严格复现
        self._layers = list(signal_layers or [])
        self._k = self._rng.randint(self._k_min, self._k_max)
        self._i = 0

    def act(self, state: dict) -> dict:
        a: dict = {"clearance_um": self._rng.choice(CLEARANCE_GRID)}
        n = len(self._layers)
        if n > 1:
            full = (1 << n) - 1
            mask = full if self._rng.random() < 0.5 else \
                self._rng.randrange(1, full)
            if mask != full:                       # 全集=缺省，不占动作槽
                a["layer_act"] = mask
        self._i += 1
        return a

    def done(self) -> bool:
        return self._i >= self._k


class FixedActionController(Controller):
    """Q6 普查动作源：动作整轮钉死——clearance 必给；layer_act 可选真子集
    位掩码（None=不写槽，全集缺省）。K 轮封顶（普查 K=1，每 run 单点
    (板, clearance, 旋钮) 质量样本）。与 RandomController 采样语义互斥，
    collect 侧按 job["pin_action"] 选用。reset 重置计数，同实例可复用。"""

    def __init__(self, clearance_um: int, layer_act: int | None = None,
                 k: int = 1):
        self.clearance_um = int(clearance_um)
        self.layer_act = layer_act          # None=全集；否则真子集位掩码
        self._k = k
        self._i = 0

    def reset(self, board_id: str, pass_name: str,
              signal_layers: list[str] | None = None) -> None:
        self._i = 0

    def act(self, state: dict) -> dict:
        a = {"clearance_um": self.clearance_um}
        if self.layer_act is not None:
            a["layer_act"] = self.layer_act
        self._i += 1
        return a

    def done(self) -> bool:
        return self._i >= self._k


class PolicyController(Controller):
    """S1 策略层回放动作源：d3rlpy 贪心（predict=argmax Q）。

    obs 与训练侧 mdp.obs_vector 同构——state_before/pass/last_action 由
    run_pass 递入，iter 内部计数（与 mdp.obs_from_record 的编码一致）。
    clearance 槽 0（未设）回退网格中点 230：训练数据里从未出现槽 0，
    纯防御。done() K 轮封顶，评测预算可控（与 RandomController 同语义）。
    """

    def __init__(self, policy, k: int = 6):
        self._policy = policy          # d3rlpy learnable（或测试注入桩）
        self._k = k
        self._i = 0

    def reset(self, board_id: str, pass_name: str,
              signal_layers: list[str] | None = None) -> None:
        self._i = 0

    def act(self, state: dict) -> dict:
        obs = obs_vector(state.get("state_before"), state.get("pass", ""),
                         self._i, state.get("last_action"))
        idx = int(self._policy.predict(obs[None]).reshape(-1)[0])
        s = unflatten_action(idx)
        a = {"clearance_um": CLEARANCE_GRID[s[0] - 1] if s[0] else 230}
        if s[2]:                           # layer_act 真子集位掩码
            a["layer_act"] = s[2]
        self._i += 1
        return a

    def done(self) -> bool:
        return self._i >= self._k


def load_policy(path: str):
    """d3rlpy 延迟加载（依赖重；非回放命令不加载）。"""
    import d3rlpy
    return d3rlpy.load_learnable(path)


# --------------------------------------------------------------------------
# ① DSN 编译（变换顺序对齐 legacy 脚本，等价性由 l0_equiv_check.py 对拍保证）
# --------------------------------------------------------------------------
def compile_pass_dsn(board: Path, dst: Path, spec: PassSpec, stackup: Stackup,
                     clearance_um: int,
                     active_layers: list[str] | None = None) -> None:
    """active_layers = layer_act 激活信号层子集：未列出的信号层锁死（type
    power，FreeRouting 不在其上布新线）。None = 缺省全集——与 legacy 字节
    等价（l0_equiv_check 前提不破）。"""
    env.pcbnew("export_dsn", str(board), str(dst), str(clearance_um))
    t = dst.read_text()
    if stackup.aux_power_nets:                       # 0 序：电源轨拆 PWR 类走粗线
        t, n_moved = split_power_class(t, list(stackup.aux_power_nets))
        if n_moved:
            print(f"  [dsn] PWR 类拆出 {n_moved} 根电源轨（粗线 0.2mm；"
                  f"KiCad10 netclass 在工程文件、独立导 DSN 丢失，文本层补）")
    if spec.delete_plane_nets:                       # A 序：删网 → 锁层 → 抬 clearance
        t = del_nets(t, sorted(stackup.plane_nets))
    if spec.only_nets:                               # 关键网先布：其余网摘 pins
        t = keep_only_nets(t, list(spec.only_nets))
    _no_route = os.environ.get("RRL_DSN_NO_ROUTE", "")   # 手布网禁布：摘 pins
    if _no_route:                                    # （wiring 顶层段不伤，铜仍是障碍）
        _nr = [s.strip() for s in _no_route.split(",") if s.strip()]
        t = drop_pins(t, _nr)
        t, _ = fix_net_wiring(t, _nr)                # 手布铜钉 fixed（route 型会被撕）
        t, _nk = skeleton_keepouts(t, _nr)           # 骨架铜→keepout（FR 对无脚网
        if _nk:                                      #   的 fixed 铜也盲，run4 实证）
            print(f"  [dsn] 骨架 keepout {_nk} 条")
    _fix_only = os.environ.get("RRL_DSN_FIX", "")    # 半手布网（大电源轨）：pins 全留
    if _fix_only:                                    #   给 FR 布余段，手布铜只钉 fixed
        _fx = [s.strip() for s in _fix_only.split(",") if s.strip()]
        t, _nf = fix_net_wiring(t, _fx)
        if _nf:                                      # 不摘 pins/不加 keepout：有脚网
            print(f"  [dsn] fix-only 钉 fixed {_nf} 条")
        # 的 fixed 铜 FR 看得见（盲只发生在无脚网，run4）；已连通 fixed 子图
        # 从 ratsnest 消失 → FR 不会重布手放的球逃逸段，只补余下脚
    if spec.shrink_npth:                             # C 序：缩径 → protect → 锁层
        t = shrink_npth_keepout(t, clearance_um,
                                stackup.npth_hole_radius_um,
                                stackup.npth_exported_radius_um)
    if spec.fix_wiring:
        t, _ = fix_wiring(t)
    elif spec.protect_wiring:
        t, _ = protect_wiring(t)
    if spec.lock_non_signal:
        for L in stackup.layers:
            if not L.is_signal:
                t = lock_layer(t, L.name)
    if active_layers is not None:                    # layer_act：锁未激活信号层
        for L in stackup.signal_layers:
            if L not in active_layers:
                t = lock_layer(t, L)
    for _L in filter(None, os.environ.get("RRL_DSN_LOCK_LAYERS", "").split(",")):
        t = lock_layer(t, _L.strip())                # 全 pass 硬锁层（骨架专属层）
    if spec.force_clearance_txt:
        t = force_clearance(t, clearance_um)
    dst.write_text(t)


# --------------------------------------------------------------------------
# 执行器 B：协商引擎（kicad_access.route.engine，子进程直出板文件）
# --------------------------------------------------------------------------
def _engine_src() -> Path:
    """权威引擎源码树（KiCAD_Access/src）。RRL_KICAD_ACCESS_SRC 可覆盖；
    缺省工作区相对推导（本文件 → 上三级 = 工作区根；工作台与发行包
    executors/ 布局同构）。不能用 venv 里的 kicad_access——工作台 venv
    是 9/17 冻结副本，没有 route/engine。
    """
    p = os.environ.get("RRL_KICAD_ACCESS_SRC")
    src = (Path(p) if p else Path(__file__).resolve().parents[3]
           / "KiCAD_Access" / "src")
    if not (src / "kicad_access" / "route" / "engine.py").is_file():
        raise FileNotFoundError(
            f"协商引擎不在 {src}（RRL_KICAD_ACCESS_SRC 可覆盖）")
    return src


def _route_quality(board: Path) -> dict | None:
    """Q4：对布完的板算 quality() 九格刻度（in-process，无 pcbnew 依赖——
    引擎子进程同 venv + PYTHONPATH 已证该 import 可行）。失败置 None 不炸轮。
    必须在轮内算：--prune 删轮产物后板不在盘，无法事后补算。"""
    try:
        src = str(_engine_src())
        if src not in sys.path:
            sys.path.insert(0, src)
        from kicad_access.route.model import RouteBoard
        from kicad_access.route.quality import quality
        rb = RouteBoard(board)
        rb.inherit_nets()
        return quality(rb)
    except Exception:
        return None


#: RRL_ENGINE_FLAGS 保留参数：路径/动作/预算由本函数管，质量旗标不得
#: 覆盖（argparse 后者胜——混入会把 RL 动作维悄悄钉死）。
_ENGINE_RESERVED_FLAGS = {"--board", "--out", "--clearance-um", "--layers",
                          "--budget-s", "--report"}


def _engine_extra_flags() -> list[str]:
    """质量旗标转发（FLAG_FREEZE 缺口修复）：env RRL_ENGINE_FLAGS = JSON
    字符串数组（如 ["--dir-prior", "--space", …]），逐字拼进引擎命令行。
    未设 = 空列表（裸缺省引擎，v1 行为）。采集侧 collect.py 按板配置注入；
    训练侧回放/评估同变量统一切换。
    """
    raw = (os.environ.get("RRL_ENGINE_FLAGS") or "").strip()
    if not raw:
        return []
    flags = json.loads(raw)
    if not isinstance(flags, list) or \
            not all(isinstance(f, str) for f in flags):
        raise ValueError(f"RRL_ENGINE_FLAGS 须为 JSON 字符串数组，got: {raw!r}")
    bad = [f for f in flags if f in _ENGINE_RESERVED_FLAGS]
    if bad:
        raise ValueError(
            f"RRL_ENGINE_FLAGS 含保留旗标 {bad}（clearance 是 RL 动作维、"
            "路径/预算由 _engine_round 管）")
    return flags


def _engine_round(base: Path, out: Path, stackup: Stackup,
                  clearance_um: int, active: list[str] | None,
                  report: Path) -> tuple[bool, str]:
    """跑一轮协商引擎（python -m kicad_access.route.engine）。

    返回 (ok, 日志尾行)。预算缺省 RRL_ENGINE_BUDGET_S=900s（ecc 冒烟实测
    902s 全预算用满）；子进程超时 = 预算 + 300s 收尾余量（同 FreeRouting
    的 no-SES 语义：诚实负样本，不炸 episode）。质量旗标经
    RRL_ENGINE_FLAGS 注入（见 _engine_extra_flags）。
    """
    budget = float(os.environ.get("RRL_ENGINE_BUDGET_S", "900"))
    cmd = [sys.executable, "-m", "kicad_access.route.engine",
           "--board", str(base), "--out", str(out),
           "--clearance-um", str(clearance_um),
           "--layers", ",".join(active or stackup.signal_layers),
           "--budget-s", str(budget), "--report", str(report),
           *_engine_extra_flags()]
    prior = os.environ.get("PYTHONPATH")
    sub_env = {**os.environ,
               "PYTHONPATH": str(_engine_src())
               + (os.pathsep + prior if prior else "")}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=budget + 300, env=sub_env)
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    tail = (r.stdout.splitlines() or [""])[-1][:200]
    if r.returncode != 0 or not out.is_file():
        return False, ((r.stderr.strip().splitlines() or [""])[-1][:200]
                       or tail)
    return True, tail


# --------------------------------------------------------------------------
# 单轮六步
# --------------------------------------------------------------------------
def run_round(base: Path, out_dir: Path, spec: PassSpec, stackup: Stackup,
              action: dict, run_id: str, idx: int, mp: int = 60,
              state_before: dict | None = None,
              last_action: dict | None = None,
              engine: str = "freerouting") -> dict:
    clr = action["clearance_um"]
    # layer_act 位掩码 → 激活信号层子集；全集/未设 → None（legacy 行为，
    # 字节等价不破）。空集防御性回退全集——DSN 全锁会成纯失败轮，无信息。
    active = None
    mask = action.get("layer_act")
    if mask:
        sig = stackup.signal_layers
        names = [L for i, L in enumerate(sig) if mask >> i & 1]
        if 0 < len(names) < len(sig):
            active = names
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    dsn = out_dir / f"{spec.name}_{idx:03d}.dsn"
    ses = out_dir / f"{spec.name}_{idx:03d}.ses"
    raw = out_dir / f"{spec.name}_{idx:03d}_raw.kicad_pcb"
    tmp = out_dir / f"{spec.name}_{idx:03d}.kicad_pcb"

    rec = {
        "run_id": run_id, "board_id": stackup.board_id, "pass": spec.name,
        "iter": idx, "action": dict(action), "board_in": str(base),
        "dsn": str(dsn), "ses": str(ses), "board_out": str(tmp),
        "state_before": state_before, "mp": mp, "engine": engine,
    }
    if engine == "negotiated":
        # ①③④ 一条子进程直出板文件。spec 变换映射（缺项=引擎内建等价）：
        # delete_plane_nets→zone 网自动跳过；protect_wiring→既有铜即障碍；
        # lock_non_signal/layer_act→--layers 只给激活信号层（平面层天然
        # 不参与）；force_clearance→--clearance-um 即路由约束。无对应项：
        # shrink_npth（孔距缺口，已知限制）；mp（引擎无 pass 概念，
        # max_iter/budget_s 由 _engine_round 管）。
        report = out_dir / f"{spec.name}_{idx:03d}_engine.json"
        rec["engine_report"] = str(report)
        extra = _engine_extra_flags()
        if extra:                       # 溯源：三元组自记所用质量旗标
            rec["engine_flags"] = extra
        ok, log_tail = _engine_round(base, tmp, stackup, clr, active, report)
        if not ok:
            rec.update({"ses_ok": False, "fr_log_tail": log_tail,
                        "wall_s": round(time.time() - t0, 1)})
            return rec
        # 引擎直出板无 SES 导入/pcbnew Save——无伴生 .kicad_pro/.kicad_prl
    else:
        compile_pass_dsn(base, dsn, spec, stackup, clr, active)  # ①
        try:
            r = env.freerouting(dsn, ses, max_passes=mp)             # ③
            _say_unrouted(r)                                         # ③' 病因点名
            log_tail = (r.stdout.splitlines() or [""])[-1][:200]
        except subprocess.TimeoutExpired:
            # 超时=动作把板变成时限内不可布——记 no-SES 失败轮（诚实负样本），
            # episode 继续往下走，不炸整段；fr_log_tail="TIMEOUT" 供数据侧识别
            log_tail = "TIMEOUT"
        if not (ses.is_file() and ses.stat().st_size > 0):
            rec.update({"ses_ok": False, "fr_log_tail": log_tail,
                        "wall_s": round(time.time() - t0, 1)})
            return rec

        env.pcbnew("import_ses", str(ses), str(base), str(raw))  # ④
        env.pcbnew("cleanup", str(raw), str(tmp))
        _skel = os.environ.get("RRL_SKELETON")
        if _skel:
            # ④' 骨架守卫：cleanup 把只挂单层走线的骨架过孔（窗孔只带 F 狗骨；
            # 落孔接通前是单层岛）判悬空剪掉——幂等补回。必须放 cleanup 之后：
            # 落盘 tmp 永远带骨架 → 下轮 DSN 导出看得见窗孔位，FR 才不会把
            # 别的网布进窗位（放 cleanup 之前 = DSN 从无骨架板出，窗位被占后
            # 补回的孔直接撞线）。已接通两层的骨架件 cleanup 不剪，幂等跳过。
            env.pcbnew("skeleton_apply", _skel, str(tmp), str(tmp))
        # pcbnew Save 会在 raw/tmp 旁顺手写 .kicad_pro/.kicad_prl——留着会让
        # 事后人工 kicad-cli 复判读到 demo netclass 判距（0.25/0.28 ≠ 板规 0.2，
        # 见 env.kicad_drc 注）。kicad_drc 已隔离判决，这里删掉保副产物单板可复判。
        for f in (raw, tmp):
            for ext in (".kicad_pro", ".kicad_prl"):
                f.with_suffix(ext).unlink(missing_ok=True)
    # ⑤ DRC 前重填 zone（两执行器同口径）：SES 导入后填充是陈旧多边形
    # （基板带入），DRC 判 track↔zone 全按旧几何 → clearance 假爆（demo 板
    # 实测 154/332 → refill 后 3/0）。refill+save 只覆写本轮产物 tmp（非
    # 基板/legacy），board_out 顺带带上新填充出厂。
    outcome = metrics.summarize_drc(env.kicad_drc(tmp, refill=True), stackup)
    try:
        outcome["layer_hist"] = env.pcbnew_json("layer_hist", str(tmp))
    except Exception:
        outcome["layer_hist"] = None
    rec.update({
        "ses_ok": True, "outcome": outcome,
        "score": [outcome[k] for k in spec.score_keys],
        "fr_log_tail": log_tail,
        "wall_s": round(time.time() - t0, 1),
        "last_action": last_action,
    })
    # ⑤' Q4 质量刻度（tmp 此刻是 refill 后的最终板；prune 前唯一时机）
    rec["quality"] = _route_quality(tmp)
    return rec


# --------------------------------------------------------------------------
# 单 pass 循环（best-of 跟踪 + 早停）
# --------------------------------------------------------------------------
def run_pass(base: Path, out_dir: Path, spec: PassSpec, stackup: Stackup,
             controller: Controller, run_id: str, mp: int = 60,
             attempts: int = 1, engine: str = "freerouting") -> tuple | None:
    """返回 (score_list, board_path, action, n_rounds)；三元组逐轮落盘。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    # ② 状态基线：base 板的引擎判定（每 pass 一次）
    state_before = metrics.summarize_drc(env.kicad_drc(base), stackup)
    records.append_round(out_dir, {
        "run_id": run_id, "board_id": stackup.board_id, "pass": spec.name,
        "iter": -1, "action": None, "board_in": str(base),
        "board_out": str(base), "ses_ok": None,
        "state_before": None, "outcome": state_before, "score": None,
        "note": "pass_base_state",
    })

    controller.reset(stackup.board_id, spec.name,
                     signal_layers=stackup.signal_layers)
    best = None
    idx = 0
    last_action = None
    while not controller.done():
        action = controller.act({"pass": spec.name, "base": str(base),
                                 "state_before": state_before,
                                 "last_action": last_action})
        for _ in range(max(1, attempts)):
            rec = run_round(base, out_dir, spec, stackup, action, run_id,
                            idx, mp, state_before, last_action, engine)
            idx += 1
            if rec.get("ses_ok"):
                sc = rec["score"]
                rec["is_best"] = best is None or sc < best[0]
                if rec["is_best"]:
                    best = (sc, Path(rec["board_out"]), dict(action), idx)
            controller.observe(rec)
            records.append_round(out_dir, rec)               # ⑥
            la = action.get("layer_act")
            a_str = f"clr={action['clearance_um']}" + \
                (f" la={la:04b}" if la else "")
            print(f"  [{spec.name} #{idx}] {a_str} "
                  f"score={rec.get('score')} "
                  f"({'BEST' if rec.get('is_best') else '...' if rec.get('ses_ok') else 'no SES'})")
        last_action = action
        # 早停：完成度与铜级双 0（前两评分键）
        if best is not None and best[0][0] == 0 and best[0][1] == 0:
            print(f"  [{spec.name}] early stop: 双 0 达成")
            break
    return best


# --------------------------------------------------------------------------
# 全管线：pass 计划依序执行，链式喂入
# --------------------------------------------------------------------------
def _non_plane_unconnected(drc: dict, plane_nets: set[str]) -> list[dict]:
    """未布通项里剔掉「纯平面网」的：平面网 SMD 焊盘已在前置扇出里落了过孔，
    连通由 planes pass 的整板 zone 负责，FreeRouting 再怎么跑也修不了它们。
    只看它们就重跑 finish（protect 回灌拆线）是净损失。"""
    import re as _re
    out: list[dict] = []
    for u in drc.get("unconnected_items") or []:
        nets = set()
        for it in u.get("items") or []:
            m = _re.search(r"\[([^\]]+)\]", it.get("description", ""))
            if m:
                nets.add(m.group(1))
        if not nets or not nets <= plane_nets:
            out.append(u)
    return out


def run_pipeline(board: Path, stackup: Stackup, out_dir: Path,
                 passes: list[str] | None = None,
                 clearances: dict[str, list[int]] | None = None,
                 mp: int = 60, attempts: int = 1,
                 engine: str = "freerouting",
                 priority_nets: list[str] | None = None,
                 fan_early: bool = False,
                 fan_early_only: list[str] | None = None,
                 signal_escapes: list[str] | None = None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = records.new_run_id()
    plan = [p for p in stackup.pass_plan() if not passes or p.name in passes]
    if not plan:
        raise ValueError(f"空 pass 计划：passes={passes}")
    if priority_nets:
        # **关键网先布**：先单独跑一轮"只有这几根网"的信号 pass，随后整条链
        # 把它的铜当障碍（protect_wiring）。理由：FreeRouting 按自己的网序布，
        # 时钟网常常排在中后段，等到它上场时通道已被别的线占满，只能绕远或
        # 打过孔（实测晶振两支引脚被绕 20mm+ / 2~3 孔）。先布就没人能挡它。
        for p in plan:
            if p.name == "priority":
                raise ValueError("优先级 pass 名与计划冲突")
        # signal_escapes 网也要进 priority 的 only_nets：否则 keep_only_nets 摘掉
        # 它们的 pins → esc 预埋铜变无脚网 → FR 对无脚网 fixed 铜盲（同源见
        # _fix_only 注释）→ esc 铜在 priority pass 丢失、signal pass 也看不到
        # （2026-11-16 HPM5E00 BGA 满栅格实弹）。esc 网有脚后 FR 从预埋孔连到
        # 对端，正是 signal-escapes 的本意。
        plan = [PassSpec("priority", delete_plane_nets=True,
                         lock_non_signal=True, force_clearance_txt=True,
                         fix_wiring=True,
                         only_nets=tuple(priority_nets) + tuple(signal_escapes or ()))] + plan
        # priority pass 自己也要 fix_wiring：pkbnew 导出把既有桩写成
        # `(type route)`，FreeRouting 会把它们整棵撕掉重放——桩的径向方向
        # 没了，出口面被 FreeRouting 自己的斜穿线封死（"一根线挡住所有线"）。
        # 而且桩多为单脚网，`(type protect)` 对单脚网无效（铜被当悬空丢弃），
        # 只有 `(type fixed)` 把桩留成永固障碍——单脚桩保住、别的网绕道内层/绕行。
        # 后续信号 pass 不许重布这几根网：fix = 既有铜变永固障碍（不是删掉）
        import dataclasses as _dc
        plan = [p if p.name != "signal" else _dc.replace(p, fix_wiring=True)
                for p in plan]
        print(f"  关键网先布：{'、'.join(priority_nets)}")

    records.write_manifest(out_dir, {
        "run_id": run_id, "board_id": stackup.board_id,
        "board_in": str(board), "engine": engine, "stackup": {
            "layers": [{"name": l.name, "role": l.role, "net": l.net}
                       for l in stackup.layers],
            "aux_power_nets": stackup.aux_power_nets,
            "via_ladder": stackup.via_ladder,
        },
        "plan": [p.name for p in plan], "mp": mp, "attempts": attempts,
    })

    cur = Path(board)
    chain: dict[str, dict] = {}

    # 平面网扇出占位：把每个平面网 SMD 焊盘的落孔位找好，用 keepout 规则区占住
    # （FR 布信号会绕开）。落铜推迟到所有 FR pass 之后、planes 之前——del_nets 会把
    # 平面网里的 via/wire 一起删掉，提前落铜等于没落。
    #
    # **时序：等 priority pass 跑完再规划**（2026-10-29 mcu_stm32f411 实测）。
    # 原先在光板上就规划，`_hits` 只看得见焊盘，落孔位会一头扎进"关键网唯一的
    # 出线走廊"：J2（TC2030，脚距 1.27mm）东侧走廊只有 1.17mm，两颗平面网过孔
    # 落在走廊正中、各自套一个 1.1mm keepout，走廊被挤到 0.61mm（0.2mm 线要
    # 0.60mm）→ 同封装第 3 脚 NRST 布不出去。对照实验：把这批 keepout 全删，
    # FreeRouting 的未布通从 6 掉到 1。关键网先布之后，那几根铜本身是 `_hits`
    # 的障碍，落孔位只能让开——先布的网赢，正是"关键网先布"该有的语义。
    #
    # **fan_early=True：细间距引脚场里的平面网脚改到"布线之前"占位**（见下）。
    fan_spots: list[dict] = []
    nospot: list[str] = []
    fan_done = not stackup.plane_layers
    if stackup.plane_layers:
        from .fanout import add_keepouts, plan_fanout, place_fanout

    def _plan_fanout(b: Path, tight: bool | None,
                     only: set[str] | None = None,
                     esc: tuple[str, ...] = ()) -> Path:
        """在**当前**板上找落孔位 + 落 keepout。`tight` 见 fanout.plan_fanout。

        `only`：只给这几只脚落 keepout（其余照常规划但不当障碍），位号-脚号
        形式，如 {'U1-9'}。用于"先布完看谁真没地方落孔，再只救那几只"。"""
        nonlocal fan_spots
        covered = {(s['ref'], s['num']) for s in fan_spots}
        # 已经订下、但铜还没落的落孔位（早占位那一批）：后一批规划必须让开
        reserved = [{'x': s['x'], 'y': s['y'], 'vd': s['vd'],
                     'px': (s['px'], s['py']),
                     'elbow': tuple(s['elbow']) if s.get('elbow') else None,
                     'net': s['net'], 'esc': bool(s.get('esc'))}
                    for s in fan_spots]
        try:
            spots = plan_fanout(b, stackup, tight=tight, skip=covered,
                                reserved=reserved,
                                signal_escapes=tuple(signal_escapes or ()))
        except Exception as e:      # 规划失败不挡链：退回布线后 stitch
            print(f"  [fanout] 规划失败，跳过（回退到 planes.stitch）：{e}")
            return b
        if only is not None:
            # esc spot（信号逃逸）不受 only 白名单约束——它伺候的是"网"不是
            # "这只脚要不要早占位"（2026-11-13 MCU_5e00_Core）
            spots = [s for s in spots if s.get('esc')
                     or f"{s['ref']}-{s['num']}" in only]
            if not spots:
                print(f"  [fanout] 指定要早占位的脚在这块板上找不到落孔位："
                      f"{sorted(only)}")
        if spots:
            b = add_keepouts(b, spots,
                             out_dir / "_fanout_keepout.kicad_pcb")
            fan_spots.extend(spots)
            print(f"  [fanout] +{len(spots)} 个平面网焊盘已占位（keepout，共 "
                  f"{len(fan_spots)}）")
        return b

    # 早占位：只有**细间距引脚场**里的平面网脚（0.5mm 脚距 QFP/QFN）需要在
    # priority/signal 之前就把落孔位订下来——它们的邻脚只隔 0.5mm，谁先落孔谁
    # 就把旁边那只挤死，而先布轮的线又会横切它们唯一的另一条走廊。
    # 10-29 mcu_stm32f103 实测：U1-9(+3.3V) 东侧被 U1-8(GND) 的落孔占死、西侧被
    # NRST 先布线横切，全流程没有一处能落孔 → 未连通 1（stitch 也救不了，见
    # planes.py）。普通间距焊盘仍等 priority 之后规划（J2 走廊教训）。
    if stackup.plane_layers and fan_early_only:
        cur = _plan_fanout(cur, None, only=set(fan_early_only))
    elif stackup.plane_layers and fan_early:
        cur = _plan_fanout(cur, True)

    for spec in plan:
        if not fan_done and spec.name != "priority":
            cur = _plan_fanout(cur, False if fan_early else None)
            fan_done = True
            nospot = [p for p in plan_fanout.last_nospot
                      if p not in {f"{s['ref']}-{s['num']}" for s in fan_spots}]
            if nospot:
                print(f"  [fanout] 这几只平面网脚本轮**没有落孔位**："
                      f"{'、'.join(nospot)}（布完会悬空）")
        print(f"== pass {spec.name} (base={cur.name}) ==")
        if spec.name == "planes":
            from .planes import run_planes_pass, strip_plane_copper
            # 先剥净 power pass 布的平面网铜（含过孔），再落扇出铜：power pass
            # 的 FreeRouting 落孔位不守 fanout 的 _lane_ok，可能压死口岸出口
            # （h730 的 +3.3V 过孔压死 RXD0）。剥完扇出才是唯一的平面网过孔来源。
            cur = strip_plane_copper(cur, out_dir, stackup)
            if fan_spots:
                cur = place_fanout(cur, fan_spots,
                                   out_dir / "_fanout_copper.kicad_pcb")
                print(f"  [fanout] 落铜 {place_fanout.last_landed}"
                      f"/{len(fan_spots)} 枚过孔 + 引线")
                nospot = sorted(set(nospot) | set(place_fanout.last_failed))
                fan_spots = None
            rec = run_planes_pass(cur, out_dir, stackup, run_id)
            records.append_round(out_dir, rec)
            cur = Path(rec["board_out"])
            chain[spec.name] = {"score": rec.get("score"),
                                "board": str(cur)}
            continue
        cl = (clearances or {}).get(spec.name) or [spec.default_clearance_um]
        # 收尾 pass 只在上一 pass 留下未布通时才有意义。protect_wiring 的 SES
        # 回灌会让 FreeRouting 重发既有走线（实测偶发丢段：v1 把 EN 竖直段
        # 弄断、+5V 断成两截）——无活可干时跑它只会伤已完成的布线。
        if spec.name == "finish":
            _d = env.kicad_drc(cur, refill=True)
            if not _non_plane_unconnected(_d, stackup.plane_nets):
                print("  [finish] 无未布通项，跳过（避免 protect 回灌拆线）")
                chain[spec.name] = {"score": None, "board": str(cur),
                                    "skipped": "nothing_left"}
                continue
        best = run_pass(cur, out_dir / spec.name, spec, stackup,
                        LadderController(cl), run_id, mp, attempts, engine)
        if best is None:
            raise RuntimeError(f"pass {spec.name}: 无有效 SES")
        cur = best[1]
        chain[spec.name] = {"score": best[0], "board": str(cur),
                            "n_rounds": best[3]}

    # 计划里没有 planes（如只跑 signal/power/finish）时，FR 阶段结束后补落铜
    if fan_spots:
        cur = place_fanout(cur, fan_spots,
                           out_dir / "_fanout_copper.kicad_pcb")
        print(f"  [fanout] 落铜 {place_fanout.last_landed}"
              f"/{len(fan_spots)} 枚过孔 + 引线")
        nospot = sorted(set(nospot) | set(place_fanout.last_failed))
        chain["fanout"] = {"board": str(cur), "spots": len(fan_spots),
                           "landed": place_fanout.last_landed,
                           "replan_failed": place_fanout.last_failed}

    result = {"run_id": run_id, "board_id": stackup.board_id,
              "chain": chain, "final": str(cur),
              # 布完仍悬空、且**根本找不到落孔位**的平面网脚：调用方拿它来决定
              # 要不要换"布线前占位"重来一次（blockpcb 的 ④' 那条路）。
              "fan_nospot": nospot}
    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1))
    return result
