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

# 画法整理：网标外移 + 引线（只动画法，网表逐 pin 不一致就回滚）
python3 d2s.py leadout --sch /tmp/d2s_out/my_board.kicad_sch \
    --title "我的板子" --comment "一句话说明"

# 人类画法总装：就近摆放 + 就近连线 + 电源符号（门禁同上，不通就回滚）
python3 d2s.py rewire --sch /tmp/d2s_out/my_board.kicad_sch
python3 d2s.py rewire --sch <板>.kicad_sch --no-place   # 只连线/换电源符号，不动器件

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
scripts/leadout_labels.py  # 网标外移 + 引线（几何借 il/tools 的 extract+geom；leadout 子命令的后端）
scripts/rewire_sch.py      # 人类画法总装：就近摆放 + 就近连线 + 电源符号/电源轨（rewire 子命令的后端）
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

## 画法规则（`rewire` 里写死的原理图基本规则）

- **先分功能块，再谈摆放**（`block_groups` / `block_layout`）：不是把所有器件往纸中间一挤。
  - 分块照人的习惯：**枢纽**=信号网邻居最多的多脚器件（MCU）自成一类；把枢纽从信号网图里摘掉
    后剩下的连通分量里，**带连接器**的就是一个接口块（RS485 通道 = MAX3485 + 终端电阻 +
    去耦电容 + 端子）；跟枢纽直接相连的两三只零件（复位 RC、晶振对、指示灯）归主控最小系统；
    只挂电源网的去耦电容跟"离它最近的那只 IC 电源脚"走（C9 跟 U3、C10 跟 U4）。
  - 排布**从左到右、从上到下**：以枢纽块为中列，源图里在它左边的块进左列、右边的进右列，
    列内按源图 y 排序；一列竖着放不下就顺次往右再开一列。
  - 块与块之间留空档（`BLOCK_GAP_X/Y`），每块画虚线框 + 加粗块标题；
    块框按**落盘后**的实际位置量，并把就近的电源符号（GND/+3V3 图形和那行字）圈进去。
  - **块内可动范围 = 块框本身**：`place_near` 挪件不许出块，否则块框会被撑大、压到隔壁块；
    有去耦电容的块头顶额外留 15.24mm（那排电容要摆到芯片电源脚上方 `ROW_DROP` 处）。
  - 图纸自动选 A4→A3→A2→A1（内容区 = 纸面减 16mm 边距，右下角给标题栏留 34mm，只压最右一列）。
    A1 都放不下时报 `needs_split`，**不硬塞**——多页（层次图纸）还没实现。
- **GND 一律朝下**（符号 rot 0），其余电源符号图形朝外；电源**不再贴标签**，
  ...
  改成"引线 + 电源符号"，并列同电位的脚合并成**一根电源轨**（轨端一个符号 + 中间落点补 junction）。
  符号摆不下才退回标签，且这类残留标签会被 `drop_power_labels` 清掉——
  同网上已经有电源符号的标签是多余的一行字（判法见下：网表里没有 `#PWR` 节点，只能自己按导线分组）。
- **就近摆放**：两脚器件贴到主伙伴脚的外侧；有 GND 脚的器件转角必须让 GND 朝下；
  同一伙伴脚多个器件沿垂直方向排开。禁区里除引脚外还包括**别人的电源引线端点**
  （`pin + 朝外*5.08`）——器件正好落在那个点上就是把自己的脚焊到轨道上。
- **块内是二维排布，不是"沿伙伴脚排一列"**（`place_near`，2026-10-26 第四轮）：
  每只器件同时占三块矩形——**本体**（含 `Reference`/`Value` 那行字的 pad）、
  **每只电源脚一条走廊**（`pwr_reserve` 算出的"电源符号 + 它那行 Value 字"的地，
  和后面真去放符号时用的是同一把尺子）、以及引脚禁区。候选位置按"离伙伴脚的偏移 ×
  沿边滑动"扫一遍，三块矩形跟别人的任一矩形相交就否掉；四轮定点迭代（改到没人再动为止），
  所以后面的挪动不会侵占前面已预订的空地。只在 `(at, rot)` 真的变了时才写回。
  这一步治的是"器件框明明没压，画面却挤成一团"——挤的从来不是本体，是**电源符号和字**。
- **`rewire` 可重复跑**：开跑前先清掉上一轮**我们自己画的**东西——导线/标签/结点（`drop_blocks`）、
  虚线块框和加粗标题（`drop_block_frames`）、`#PWR1xx` 电源符号（`drop_own_power`）。
  三类各有自己的指纹，d2s 生成的图原本不带这些记号，不会误删别人的东西。**清除必须在第一次
  `extract` 之前**：清理放在后面的话，上一轮的电源符号会当成"器件"进 `boxes` 和引脚禁区，
  越跑越挤、永不收敛（实测 34→39→42 个符号，标签跟着涨）。
- **就近连线**：同网最近优先长线，逐边 L 形，线段不许穿别人的脚、不许压器件图形、
  不许与已有线成 T 或共线重叠；超过 `MAXWIRE`(30mm) 或绕不开才退回标签。
  上拉/下拉/负载电容这类短连接因此全是实线，不再用网标代替。
- **信号网留标签**：网名（`USART1_TX`/`LED_PWR`…）是往下游传的信号名，
  连线只负责通、不负责起名；半接半标的网在接通的那一簇上补一个同名标签把两头牵起来。
- **标签落点**（`leadout_labels.plan`）：按引脚朝外挪 1~2 格（`LEAD=5.08`），
  逐个打分挑最不难看的，权重从重到轻：电气并网（一票否决）> 字压字(50) >
  字写进本体(40) > 线从字里穿过(12) > 引线蹭到别人的字(6) > 引线长度(每格 2) >
  能直不拐(+1) > 贴脚不牵线(+15，最后一招)。几条硬规矩：
  - **方向只朝外或侧向，绝不朝本体里牵线**（反方向就是往芯片肚子里画线）。
  - **侧向不许一步到位**：必须先出 1 格再横着走，否则字会顺着本体边缘往下排、
    压着隔壁脚的引脚号和桩。
  - **电气安全的三种并网都要查**：落点/拐点落在别人的线上（T 接）、别人的线头落在
    我这段线的中段（反向 T 接）、两段共线重叠。两段引线的**拐角**也算接点——
    漏掉它曾把 GND 网劈成 8 块。
  - 实在无处可去：贴着引脚不牵线、字朝外写（`_style(v, 0)`），
    这才是"挤"，比把网名塞进器件本体里强。
- **标签文字的真实尺寸**：`text_rect` 里的常数是从 `kicad-cli sch export svg` 的
  笔画包络反推的（竖排 x∈[锚−1.06, 锚+0.40]；横排 y∈[锚−1.08, 锚+0.38]）。
  自己按"居中 ±半高"估出来的框比真值窄 0.2~0.4mm，看着不压其实压着。
- **器件自己的 Reference/Value 摆到本体外**（`place_fields`）：原来写在符号中心，
  "U2"/"AMS1117-3.3" 叠在符号里就是用户说的"器件重叠"。
- **体检**：`python3 scripts/check_text_layout.py <sch>`——字压字用 SVG 真值，
  字压线/压器件用几何模型。字压字必须是 0；相邻（差 <0.1mm）不算压。
