---
name: pcb-fab-export
description: 制造文件导出（gerbers + drill + 贴片坐标 + 打包 zip）：kicad-cli 三命令、双层板层集、路径经 ~/.harnesspcb.json 解析。要投板生产时用。
---

# 制造文件导出（fab export）

板子过了 DRC 铜级门（pcb-chain 完成 / pcb-drc-read 复核）后，
导出制造文件投板。

## 三条命令

kicad-cli 位置经 `~/.harnesspcb.json` 的 `HWS_KICAD_CLI` 解析
（下文记 `$KC`；见 README 安装节）。板用最终产物
（chain 的 `<stem>_routed.kicad_pcb`，铺铜后那份）。

```bash
F=<导出目录>; mkdir -p $F/gerbers

# 1. Gerbers（2 层板层集：铜×2 + 阻焊/锡膏/丝印×2 + 板边）
$KC pcb export gerbers --output $F/gerbers \
  --layers F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts \
  <板.kicad_pcb>

# 2. 钻孔（与 gerbers 同目录， Excellon 格式）
$KC pcb export drill --output $F/gerbers <板.kicad_pcb>

# 3. 贴片坐标（SMT 贴片用；正面）
$KC pcb export pos --output $F/pos.csv --side front --format csv <板.kicad_pcb>
```

产出：9 个 gerber（两层各 Cu/Mask/Paste/SilkS + Edge_Cuts.gm1）+
`.drl` 钻孔 + `.gbrjob`（KiCad 自动生成的作业文件，一并交厂）+
`pos.csv`。

## 打包投厂

```bash
cd $F/gerbers && zip -j ../fab_gerbers.zip * && cd ..
# fab_gerbers.zip 交 PCB 厂；pos.csv 交 SMT（或同包）
```

- 板厂下单时选「2 层 + 1.6mm 板厚 + HASL/沉金」常规档即可
  （本包工艺量：最小线宽/线距按布线 clearance 首过档报给厂里）。
- `.gbrjob` 有些厂不收——收不收都无妨，9 个 gerber + drl 是完整集。

## 验证

- 导出日志每层一条「繪製到/Plotting」无报错；`ls` 数齐 9 gerber + drl。
- pos.csv 行数 = 正面贴片器件数（DRC 后板上的 SMD 计数）。
- 投厂前最后再跑一次 DRC（pcb-drc-read 口径，铜级 0）确认导的是
  通过版。

## 铁律

- 只对**通过 DRC 的最终板**导出；中间档（route 目录里的试验板）不导。
- zip 用 `-j` 去目录结构（厂里解包即平铺）。
- 导出目录是生成物，可随时删掉重导——别手改里面的文件。
