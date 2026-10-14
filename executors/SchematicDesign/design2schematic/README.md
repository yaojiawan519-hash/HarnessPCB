# design2Schematic（d2s）

设计意图 → 原理图 → 放置 → 布线 → 铺铜 → DRC 0 的完整工具链编排。

```
设计表 JSON ──d2s sch──▶ .kicad_sch + .net（ERC 0 错误硬门禁 + 逐 pin 回读对账门禁）
                          │
              ──d2s board─▶ base_unrouted.kicad_pcb（板框/安装孔/锚点预放）+ stackup.json
                          │
              Placement ▶ placed.kicad_pcb（子进程原样调用，内部零改动）
                          │
              Routing  ▶ route/（阶梯 clearances；result.json 的 final 即选中板）
                          │
              铺铜脚本    ▶ routed.kicad_pcb（双面 GND THERMAL + full-pads 实连）
                          │
              kicad-cli  ▶ DRC 终判（隔离副本 refill；铜级 0 + 未布通 0 才算通过）

全程两次落盘：每步报告（成功/失败都写）+ MCP 调用审计（~/.d2s/audit/<日期>.jsonl）
```

## 用法

```bash
cd SchematicDesign/design2schematic

# 只生成原理图（ERC 门禁：errors>0 直接失败）
python3 d2s.py sch --design <你的设计表>.design.json --out /tmp/d2s_out

# 只生成板骨架（需要先有 sch 产物）
python3 d2s.py board --sch /tmp/d2s_out/my_board.kicad_sch \
    --design <你的设计表>.design.json --out /tmp/d2s_out

# 全链一条命令（sch → board → place → route → pour → DRC）
# --out 缺省 = KiCAD_Projects/<项目名>/，版本自动取目录内最大 vN + 1
python3 d2s.py chain --design <你的设计表>.design.json

# 从某步重入（之前的产物必须已存在，缺即报错；重入自动复用最新版本）
python3 d2s.py chain --design <你的设计表>.design.json --step-from route

# 显式钉定版本号（如重跑 v4）
python3 d2s.py chain --design <你的设计表>.design.json --version 4 --step-from place
```

## 产物命名规范（KiCAD_Projects/<项目>/）

所有产物统一 `<项目>_v<N>` 前缀（用户 2026-09-22/23 指令；与历史工程 v1/v2/v3 同处）：

| 文件 | 生成步 | 说明 |
|---|---|---|
| `<P>_vN.kicad_sch` / `.net` / `.pdf` | sch | 原理图 + 网表 + PDF（ERC 0 错误门禁） |
| `<P>_vN.kicad_pro` / `.kicad_prl` | sch/board | 工程与 GUI 状态 |
| `<P>_vN_sch_report.json` | sch | ERC 计数 / 器件 / 网络数 / **回读对账 + TopologyHash**；失败也写（status=failed + steps_done） |
| `<P>_vN_base.kicad_pcb` | board | 板骨架：板框/四角安装孔/锚点预放（**同步丢件硬门禁**） |
| `<P>_vN_stackup.json` | board | 两层叠层（via_ladder 0.3/0.6 = 规范孔径） |
| `<P>_vN_placed.kicad_pcb` | place | Placement 输出（锚点锁定） |
| `<P>_vN_route/` | route | Routing 阶梯布线原始档（result.json 的 final 即选中板） |
| `<P>_vN_routed.kicad_pcb` | pour+DRC | **交付物**：布线终板 + 双面 GND 铺铜，DRC 通过后含 zone 填充 |
| `<P>_vN_chain_report.json` | chain | 计划步（planned）+ 每步 status（applied/reused/failed）+ 命令 / DRC 计数；**异常中断也落盘**（含 traceback 尾部） |

版本规则：`chain` 跑 sch 步（fresh）→ `max(v)+1`；重入（`--step-from ≠ sch`）→ 复用
最新版；`--version N` 显式钉定（sch 步遇已有同名 sch 会 raise，不覆盖）。
`board` 子命令的版本从 `--sch` 文件名推导（sch 文件名即版本载体）。

## 设计表 schema（输入契约）

字段一览（完整示例见发行包 `demo/demo_blinker.design.json` 与
`docs/输入格式说明.md`）：

| 键 | 说明 |
|---|---|
| `project{name,title,spec}` | 工程名（也作文件名）/ 标题 / 一句话规格 |
| `board{width,height,layers, mounting_holes,hole_d,hole_edge_offset, full_pads?}` | 板框尺寸、四角安装孔数/孔径/边距；`full_pads` = 铺铜实连焊盘 ref（starved_thermal 修复） |
| `parts[{ref,lib,value,footprint,x,y}]` | 器件表；`lib` 用 `Library:Symbol` 全名；x/y 是原理图坐标 |
| `conns[{ref,pin,net,…}]` | 连接表；`pin` 用引脚**名**（内部归一后按名/别名/引脚号匹配）。`net:null` = 显式 no-connect 声明（如 J1 的 ID）。可选：`pin_aliases`（别名数组，D-/DM 这类）、`multi:true`（同名多脚全连，VDD×3/OSC 对）、`exclude_substring`（OSC_IN 排除 OSC32） |
| `power{rails, pwr_flags:[{net,ref,x,y}]}` | 电源轨名；PWR_FLAG 放置点。**经验：只有无源驱动的网（GND）需要 PWR_FLAG**，+5V/+3V3 有驱动源再加反而 ERC 冲突 |
| `no_connect[{ref,pin_prefix}]` | 按前缀声明 NC（信息性：未连引脚本来就会走自动 NC） |
| `keep_connected[名字]` | 未连接但**不要**自动 NC 的引脚名（交给工程师） |
| `anchors[{ref,x,y,rotation}]` | 板上预放锚点（连接器/接口），Placement 视为固定锚 |

设计表即知识库的条目：新设计抄一份现成的改 parts/conns 即可
（发行包 demo 或既有设计表均可作底）。

## 环境变量

路径类变量一律**三级解析**（P0 环境解耦）：环境变量 > 机器配置文件
`~/.harnesspcb.json`（`HWS_CONFIG` 可改指；键名 = 环境变量名；每台机器
自己的环境声明，不进仓库）> 标准探测（PATH / 常见安装位）。

| 变量 | 无 env/config 时的探测 | 说明 |
|---|---|---|
| `D2S_MCP` | 无标准位——必须 env/config 指定 | KiCAD-MCP-Server 入口（dist/index.js） |
| `D2S_NODE` | `which node` / homebrew 常见位 | Node 解释器 |
| `D2S_KICAD_EXT` | 从解析到的 kicad-cli 反推 | KiCad.app 的 Contents 目录（KPY 从这里拼；如 `/Applications/KiCad/KiCad.app/Contents`） |
| `D2S_PLACEMENT_RL` | `../../Placement` | Placement 仓根（chain 用其 `.venv/bin/python` 跑 place/route，kicad-access 已由 `uv sync` 装入） |
| `D2S_PROJECTS` | `../../KiCAD_Projects` | 项目产物根目录（所有 KiCad 工程统一存放处） |
| `D2S_PRETTY_RAILS` | 未设 | 设 1 时启用电源轨道视觉增强（默认关；ERC 不依赖） |
| `D2S_AUDIT_DIR` | `~/.d2s/audit` | MCP 调用审计 JSONL 目录（按日期一文件，`<日期>.jsonl`） |
| `D2S_AUDIT_OFF` | 未设 | 设 1 关闭审计写入（审计失败本身永远静默，不破坏主流程） |

## 结构

```
design2schematic/
├── d2s.py              # CLI（sch / board / chain）
├── mcp_client.py       # KiCAD-MCP-Server 的 stdlib JSON-RPC 客户端（轮询等待后端）
├── lib/
│   ├── components.py   # parts → 放件
│   ├── nets.py         # conns → 网络标签（按引脚号贴，规避引脚名匹配坑）+ PWR_FLAG
│   ├── auto_nc.py      # 未连且不在 keep 集 → 自动 no-connect
│   ├── power_rails.py  # 可选电源轨道美化
│   ├── erc.py          # ERC 门禁（schematicRule.md：零错误是定稿硬条件）+ 网表导出
│   └── verify.py       # 逐 pin 网表回读对账（missing/extra/mismatch/aux + TopologyHash）
├── scripts/pour_2layer.py  # 双层 GND 铺铜（KiCad 自带 Python/pcbnew；会话双板验证参数）
├── tests/test_verify.py    # verify 单测（golden 真实网表锚定 + 扰动注入 + 审计层）
└── templates/<项目>.design.json  # 设计表模板（每项目一份；发行包示例见包内 demo/）
```

Python stdlib-only（铺铜脚本用 KiCad 自带 Python，同样零第三方依赖）。
KiCad 写入全部经 MCP 工具完成；Placement / Routing 子进程调用，内部零改动。

## 关键经验（写死在代码里的试错结论）

- **连接按引脚号贴标签**（`add_schematic_net_label(componentRef, pinNumber)`），
  不用引脚名匹配——ST/连接器库引脚名带 `~`、别名（D-/DM）时名字匹配静默失败。
- **PWR_FLAG 只给无源驱动的网**（GND）；有驱动的轨加了反而 ERC 输出冲突。
- 布线 clearance 阶梯默认 `205,215,225,235,245` µm——235 是双板验证的甜点档。
- 过孔 `via_ladder [[300,600]]`（0.3 孔 / 0.6 外径）= PCBRouter.md 规范值。
- 铺铜 zone 建立后**不在脚本里 fill**；refill + DRC 由 chain 在隔离副本上统一做
  （kicad-cli `--save-board` 会覆写板文件，不能直接跑在产物上）。
- **DRC 铜级门禁**（与 realboard 验收标准一致：0 未布通 + 0 铜级）：shorting /
  clearance / solder_mask_bridge / hole_clearance / hole_to_hole / track_width /
  via_dangling / unconnected 须 0；丝印（silk_*）、lib_footprint_mismatch、
  courtyard/text 属 footprint 原生或注释级——如实报数不挡链；**未知 kind 一律失败**
  （fail-closed，KiCad 新检查项须人工归类后加入容忍表）。
- **设计表必须给每个 `J*` 连接器 anchors**：place_rl 默认 `fix_connectors=True`
  锁所有 J* 器件于 sync 落点——漏给锚点的连接器会锁在板框外原点，DRC 报其焊盘
  未布通且 route 档位怎么调都一样（v4 的 J4 实例：补 (52.5,40) rot270 后 route
  首档 205µm 即 [0,0,0]）。
