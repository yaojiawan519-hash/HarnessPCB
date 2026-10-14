# demo：LED 闪烁演示板（demo_blinker）

包内自带的一块**合成**演示板（非任何真实用户项目），用来在一台新机器上
把全链跑通、验证环境安装正确。

## 这块板是什么

一块最小的「能跑起来」的 MCU 板，麻雀虽小五脏俱全：

- **主控** U1：STM32F103C8T6（LQFP-48，0.5mm 脚距——特意选它，细脚距
  是布线引擎最难的场景，能过 demo 说明真板也有戏）
- **电源** U2：AMS1117-3.3（+5V 进，+3V3 出）+ 输入/输出各一颗 10µF 钽电容
- **接口** J1 电源输入（2 脚）、J2 SWD 调试口（4 脚）——两颗都锚在板边，
  开口朝外（见意图 spec）
- **时钟** X1 8MHz 晶振 + 两颗 20pF 负载电容
- **复现核心要素** BOOT0 下拉、NRST 上拉 + 复位电容、4 颗 100nF 去耦
- **负载** 两颗 LED（STATUS / USER）各串 1k 限流

板框 40×30mm 双层，四角 M3 安装孔。原理图无 ERC 错误、无悬浮引脚。

## 文件

| 文件 | 内容 |
|---|---|
| `demo_blinker.design.json` | 设计表（九键 schema，见 `docs/输入格式说明.md` §一） |
| `demo_blinker_intent.json` | 放置意图 spec：J1 贴左缘、J2 贴右缘、电源簇以 J1 为锚 |

## 期望产物链

按 README quickstart 跑完后，`demo/out/` 里应有（版本号 v1 起，重跑 +1）：

1. `DEMO_BLINKER_v1/DEMO_BLINKER.kicad_sch` — 原理图（ERC 0 错误）
2. 同名 `.kicad_pcb` — 板框 + 网表落板
3. `place_v1/` — 放置结果（接口件贴边、电源簇成形）
4. `route_245um/`（或链上更优档）— 布线结果，期望全部网布通
5. `pour/` — 铺铜后版本
6. `drc_*.rpt` — DRC 报告：**铜级 9 类 0 违规、未连接 0**（丝印类容忍项
   可能有小数字，不挡验收，判读口径见 `skills/pcb-drc-read`）

之后按 `skills/pcb-fab-export` 导出 Gerber 即为可送厂产物。
