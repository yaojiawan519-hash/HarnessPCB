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
from .dsn_edit import (del_nets, force_clearance, lock_layer,
                       protect_wiring, shrink_npth_keepout)
from .mdp import CLEARANCE_GRID, obs_vector, unflatten_action
from .stackup import PassSpec, Stackup


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
    if spec.delete_plane_nets:                       # A 序：删网 → 锁层 → 抬 clearance
        t = del_nets(t, sorted(stackup.plane_nets))
    if spec.shrink_npth:                             # C 序：缩径 → protect → 锁层
        t = shrink_npth_keepout(t, clearance_um,
                                stackup.npth_hole_radius_um,
                                stackup.npth_exported_radius_um)
    if spec.protect_wiring:
        t, _ = protect_wiring(t)
    if spec.lock_non_signal:
        for L in stackup.layers:
            if not L.is_signal:
                t = lock_layer(t, L.name)
    if active_layers is not None:                    # layer_act：锁未激活信号层
        for L in stackup.signal_layers:
            if L not in active_layers:
                t = lock_layer(t, L)
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
def run_pipeline(board: Path, stackup: Stackup, out_dir: Path,
                 passes: list[str] | None = None,
                 clearances: dict[str, list[int]] | None = None,
                 mp: int = 60, attempts: int = 1,
                 engine: str = "freerouting") -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = records.new_run_id()
    plan = [p for p in stackup.pass_plan() if not passes or p.name in passes]
    if not plan:
        raise ValueError(f"空 pass 计划：passes={passes}")

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
    for spec in plan:
        print(f"== pass {spec.name} (base={cur.name}) ==")
        if spec.name == "planes":
            from .planes import run_planes_pass
            rec = run_planes_pass(cur, out_dir, stackup, run_id)
            records.append_round(out_dir, rec)
            cur = Path(rec["board_out"])
            chain[spec.name] = {"score": rec.get("score"),
                                "board": str(cur)}
            continue
        cl = (clearances or {}).get(spec.name) or [spec.default_clearance_um]
        best = run_pass(cur, out_dir / spec.name, spec, stackup,
                        LadderController(cl), run_id, mp, attempts, engine)
        if best is None:
            raise RuntimeError(f"pass {spec.name}: 无有效 SES")
        cur = best[1]
        chain[spec.name] = {"score": best[0], "board": str(cur),
                            "n_rounds": best[3]}
    result = {"run_id": run_id, "board_id": stackup.board_id,
              "chain": chain, "final": str(cur)}
    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1))
    return result
