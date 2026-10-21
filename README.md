<div align="center">

# NiuFan · 飞牛 NAS 风扇控制

**按 CPU / 显卡 / 硬盘温度自动调节机箱与 CPU 风扇转速**

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-x86-lightgrey.svg)](#兼容性)
[![fnOS](https://img.shields.io/badge/fnOS-%E2%89%A51.1.3100-green.svg)](https://www.fnnas.com/)
[![Version](https://img.shields.io/badge/version-2.2.7-orange.svg)](CHANGELOG.md)

[简体中文](README.md) · [English](README.en.md)

</div>

---

> **本仓库是 [LiuFudi/NiuFan](https://github.com/LiuFudi/NiuFan) 的 Fork。**
> 在上游全部功能的基础上，新增**威联通（QNAP）NAS 风扇控制支持**（基于开源驱动
> [0xGiddi/qnap8528](https://github.com/0xGiddi/qnap8528)），详见下方
> 「威联通 QNAP 机型支持（本 Fork 新增）」一节。上游本体版权归原作者所有，
> 本 Fork 遵循同一 GPL-3.0 许可证；驱动部分版权归 0xGiddi 所有（GPL）。
> 本项目与威联通公司（QNAP Systems, Inc.）无任何关联。

## 为什么做这个

成品 NAS 的风扇策略通常只有「静音 / 均衡 / 全速」三档，而且**只看 CPU 温度**。

但 NAS 里最怕热的恰恰不是 CPU，是硬盘：WD 官方建议硬盘长期工作温度低于 50 °C，
而很多机器在 CPU 闲逛的时候，硬盘已经悄悄爬到 55 °C 了 —— 因为机箱风扇压根没考虑硬盘。

这个应用让你**用硬盘温度驱动机箱风扇**，同时 CPU 风扇照常跟 CPU 走，
两条曲线互不干扰。设计思路参考了 [FanControl](https://github.com/Rem0o/FanControl.Releases)，
但刻意只保留最核心的部分：**温度源 + 控温曲线 + 配置持久化**，不做过度设计。

## 功能特性

| | |
|---|---|
| 🌡️ **温度源覆盖整机** | CPU（coretemp / k10temp / PECI）、显卡（amdgpu / i915 / xe / nouveau / nvidia-smi）、硬盘（drivetemp / nvme / smartctl 兜底），外加主板（SYSTIN / CPUTIN / T_SENSOR）、内存（jc42 / spd5118）与 ACPI 热区，**每路一个独立开关** |
| 🔍 **硬件自检向导** | 首次启动自动列出控制器暴露的**全部** PWM 通道、实际转速与 BIOS 绑定的温度源，可逐个通道全速试转识别停转的风扇 |
| 🧬 **主动硬件识别** | 不只读取内核已有的 `hwmon`：同时采集 DMI / PCI / USB / ACPI / I2C / 设备树 ID，并只读探测 x86 Super-I/O 配置空间；报告厂商、设备 ID、型号、I/O 基址和可用控制后端 |
| 🛠️ **无 hwmon 回退** | 对寄存器表明确的 Nuvoton NCT6779/679x 与 ITE IT87xx，可在飞牛内核没有绑定驱动时使用受控用户态寄存器后端；带进程锁、原始状态快照和停止/卸载恢复 |
| 🧾 **诊断日志导出** | 「硬件检测」页一键导出本机硬件快照与运行日志：主板 / BIOS、CPU、显卡、风扇控制器（hwmon 名称、驱动模块、PWM 通道）、内核模块、传感器读数、`info.log` / 安装 / 升级日志与**脱敏后**的配置摘要，文件名包含设备标识，单个 txt 直接下载；缺少 `info.log` 时拒绝导出不完整包 |
| ⏬ **标定含 0 %** | 标定会把风扇降到 0 %，因此能直接查出风扇是否支持停转（0 % 读到 0 RPM），标定结果会保存并在下次打开时回显 |
| 💽 **按盘选择，不按位置** | 硬盘用型号 + 序列号标识（`/dev/disk/by-id`），不是 `sda`/`sdb`：换盘位、换线或改启动顺序后，选择依然对应同一块盘 |
| 🖥️ **每块显卡一个温度源** | 单卡时是一张「显卡」卡片，多卡时自动拆成「显卡1 / 显卡2 …」，各自独立开关且卡内显示型号，不会再出现两个勾选框管同一件事 |
| 🎴 **显卡按型号识别** | GPU 用 PCI 型号名标识（如 `Intel DG1 [Iris Xe Graphics]`），不是笼统的 `i915`；多张显卡可分别勾选 |
| 👁️ **关掉也照样显示** | 未参与调速的温度源仍会读取并显示读数，不会把「机器没这个传感器」和「你没打开」混为一谈 |
| 🧭 **假读数不会混进来** | 没接传感器的输入（NCT67xx 空置的 AUXTIN 会稳定读在 111–115 °C）以及 PECI 这类与 CPU 重复的读数都不会列出来 —— 一个假读数足以把所有风扇拉满 |
| 🎚️ **两点转速标定** | 硬件检测分别以约 30 % 与 100 % 转速测量转速，给出每路的**最低 / 最高转速**和「调速是否有效」，用来确认 PWM 真的生效 |
| 📍 **工作点可视化** | 曲线上实时标出当前温度与转速对应的点，一眼看出风扇此刻被要求跑在哪里 |
| 📈 **可视化曲线** | 每个风扇一条独立曲线，网页上直接拖动节点调整，支持 2–8 个节点 |
| 🎛️ **多种模式** | 温度曲线 / 固定转速 / BIOS 自动，逐风扇独立设置 |
| 🔀 **多源取最高** | 一个风扇可绑定多个温度源，取其中的最高值（例如机箱风扇同时看硬盘和 CPU） |
| 🐢 **温度迟滞** | 降温超过设定幅度才允许降速，避免风扇反复变速的噪音 |
| 💾 **硬盘友好** | 硬盘温度单独设置较长轮询间隔，减少唤醒休眠硬盘；smartctl 路径使用 `-n standby` 绝不唤醒 |
| 🛡️ **失效保护** | 温度源全部失效时风扇升到设定的保护转速，界面同时告警 |
| ↩️ **安全交还** | 停止 / 卸载应用时自动把风扇交还主板 BIOS；从配置里移除某个风扇时，该通道精确恢复成接管前的状态 |
| 🪶 **零依赖** | 后端纯 Python 3 标准库，前端原生 JS 无构建步骤，不需要 nodejs / python312 运行时应用 |

## 界面

> 📷 *截图待补充 —— 如果你愿意贡献截图，欢迎提 PR。*

<!--
把截图放到 docs/screenshots/ 后替换成：
![总览](docs/screenshots/overview.png)
![曲线编辑](docs/screenshots/curve.png)
-->

主界面分三块：

- **温度源** —— CPU / 显卡 / 硬盘三张大卡片，各自独立开关；主板、内存、ACPI 热区收在「其它温度源」折叠区；硬盘逐块温度与勾选列在最下面
- **风扇** —— 每个通道一张卡片：转速、转速、依据温度、模式、温度源、最低/最高转速、可拖动的曲线图
- **全局设置** —— 控制周期、硬盘轮询间隔、温度迟滞、失效保护转速

## 安装

### 前置条件

- 飞牛 fnOS（`os_min_version ≥ 1.1.3100`，实测于 `1.2.0604`）
- x86 主板，风扇挂在本项目支持的控制器上（见 [兼容性](#兼容性)）
- 需要**管理员**账号安装（第三方应用安装本身要求管理员）

### 从 Release 安装

1. 到 [Releases](../../releases) 下载 `niufan-<版本>-<架构>-<系统>.fpk`（飞牛包即
   `niufan-2.2.5-x86-fn.fpk`）
2. 飞牛 **应用中心 → 手动安装**，选择该 `.fpk`，选一个存储空间
3. 安装完成后打开 **风扇控制**

也可以走命令行：

```bash
sudo appcenter-cli install-fpk niufan-2.2.5-x86-fn.fpk -v 3   # -v 是存储空间序号
sudo appcenter-cli start niufan
```

> ⚠️ 实测 `appcenter-cli install-fpk` 对**已安装的同名应用不会执行升级**，
> 更新版本需要先 `uninstall` 再装（或用应用中心界面的升级入口）。
> 卸载不会删除 `config.json`，重装后曲线配置照旧。

## 使用

**首次启动必须先做一次转速标定。** 这不是可以跳过的引导：一路风扇到底听不听话，
恰恰是装完之后最难自己判断的事 —— 界面看着在调速、风扇却纹丝不动，用户只会以为
软件坏了。所以在点过「主动检测」并「应用并保存」之前，向导不会关闭，也进不了主界面。

标定会把每路风扇先拉满、再降到 0 % 各测一次，得出转速范围，并判断这一路是否真的受
PWM 控制、是否支持停转。**只有一种情况不阻塞**：机器上根本没有可调速的 PWM 通道
（主板由 EC 管理、驱动没加载等）—— 那种情况下没有风扇会被调错，向导改为把诊断信息讲清楚。

进入主界面后：

1. **确认温度源** —— CPU / 显卡 / 硬盘是三张大卡片；主板、内存、ACPI 热区收在
   「其它温度源」折叠区里，需要哪一路就展开勾选。硬盘逐块温度列在最下面，
   取消勾选不想参与调速的盘
2. **确认接头归属** —— 首次安装会按主板 BIOS 的配置自动推断（跟 CPU 核心温度的判为 CPU 风扇，
   其余判为机箱风扇），但**建议核对一下**哪张卡片对应哪个物理接头：给 CPU 加压或用 `stress-ng`，
   看哪个风扇转速跟着动
3. **调曲线** —— 拖动圆点，双击空白处加节点，右键节点删除
4. **点击保存** —— 配置立即生效并落盘

> 🔍 任何时候点右上角「**硬件检测**」都能看到完整清单：所有温度传感器（包括没读数的，
> 折叠起来并注明原因 —— 读数为 0、超出合理范围按未接传感器处理、与 CPU 温度重复等）
> 和所有 PWM 通道（无转速的同样折叠，可能是没接风扇，也可能是风扇停转）。

> 🧾 **识别不出风扇控制器时，先导出诊断日志。** 此时「硬件检测」页顶部会出现警告，
> 点旁边的「导出诊断日志」即可下载一份硬件快照和运行日志（主板 / BIOS、CPU、显卡、内核模块、
> 每个 hwmon 节点的名称与 `pwm*` 状态、当前传感器读数、`info.log`、脱敏后的配置摘要；不含口令、
> 密钥、用户文件与完整序列号），把它发给开发者就能直接看出是权限不足、驱动没加载，
> 还是芯片型号没被支持。日志是单个 txt，放在 `var/diagnostics/`，只保留最近 20 份。

> 💡 **推荐起步配置**：机箱风扇绑 `硬盘`，CPU 风扇绑 `CPU`。
> 硬盘曲线可以设成 `40°C → 30%`、`48°C → 60%`、`55°C → 100%`。

## 兼容性

能不能用，优先取决于**风扇挂在哪颗控制器上，以及它的内核驱动有没有被适配**。
2.2.5 起，NiuFan 也会主动探测设备 ID；NCT6779/679x 与 IT87xx 在满足
root、`CAP_SYS_RAWIO`/`/dev/port` 且寄存器资料完整时，可绕过未绑定的 `hwmon`
使用直接后端。IT8613E 也按 Linux hwmon 上游已接受的 `0x8613` / GPIO 复用规则
接入，即使飞牛自带内核的 `it87` 还不认识该 ID 也可以主动识别并控制。其它型号
仍优先依赖已有内核驱动，不会盲写未知寄存器。
各驱动家族的 `pwm_enable` 语义**并不相同**（写错值可能把风扇控制整个关掉），
所以每个家族都有独立配置，数值全部取自内核文档 `Documentation/hwmon/*.rst`：

| 控制器家族 | 内核驱动 | 手动 | 自动 | 状态 |
| --- | --- | :---: | :---: | --- |
| Nuvoton NCT6106 / 6116 / 6775 / 6776 / 6779 / 679x | `nct6775` | `1` | `5` Smart Fan IV | ✅ 已实测 |
| ITE IT87xx（含 IT8613E / IT8689E） | `it87` | `1` | `2` ※ | ✅ 已适配 |
| Fintek F718xx / F8000 / F81865F / F81968 | `f71882fg` | `1` | `2` | ✅ 已适配 |
| Fintek F71805F / F71872F | `f71805f` | `1` | `2` | ✅ 已适配 |
| Winbond W83627EHF / DHG / UHG / W83667HG | `w83627ehf` | `1` | `2` | ✅ 已适配 |
| Winbond W83627HF / THF / 37HF / 87THF / 97HF | `w83627hf` | `1` | `2` | ✅ 已适配 |
| SMSC SCH5627 / SCH5636 | `sch5627` / `sch5636` | `1` | `2` | ✅ 已适配 |
| **Nuvoton NCT6683 / 6686 / 6687** | `nct6683` | — | — | ⚠️ **仅监控** |
| ITE IT8613E | `it87` | `1` | `2` | ✅ 主动识别 / 用户态回退 |
| 其它带 `pwmN` 的 hwmon 节点 | 任意 | `1` | `2` | ⚠️ 通用兜底，未验证 |
| ACPI `PNP0C0B` 风扇对象 | — | — | — | ❌ DSDT 空壳 |
| 风扇由 EC 管理（笔记本 / 部分迷你主机） | — | — | — | ❌ 没有 pwm 节点 |

> 除 NCT6775 家族外，其余家族是按内核文档适配的，**尚未在真实硬件上验证**。
> 如果你有 ITE / Fintek / Winbond 的机器，非常欢迎反馈结果（见[贡献](#贡献)）。

本项目的开发与实测环境：

```
主板   ASUSTeK TUF B365M-PLUS GAMING (B365, LGA1151)
CPU    Intel CC150  8C/16T
芯片   Nuvoton NCT6796D @ 0x2e:0x290
控制器 6 路 PWM，其中 2 路接有风扇
系统   飞牛 fnOS 1.2.0604 / 内核 6.18.18.c1032-trim
```

### 两个值得知道的坑

**① ITE 的自动模式只对老芯片有效。** `it87` 驱动的 “Smart Guardian” 仅实现了
IT8705F ≤ rev F、IT8712F ≤ rev G，新芯片写 `2` 会被驱动拒绝。应用对此的处理是
逐级回退：先恢复接管前记录的状态 → 再试驱动自动模式 → 都不行就**直接给全速**。
最后这一条很关键：没有它，一块不支持自动模式的 ITE 主板上「停止应用」会把风扇
永久留在最后一次写入的低转速上。

**② ITE 主板常需要额外一步。** `it87` 驱动可能因 ACPI 已占用 SuperIO 的 I/O 端口
而拒绝接管，`dmesg` 里会看到 `ACPI: resource conflict`：

```bash
sudo modprobe it87 ignore_resource_conflict=1   # 只影响该驱动，风险相对小
# 或在内核启动参数加 acpi_enforce_resources=lax（影响面更大）
```

NiuFan 现在会主动读取最近的内核日志并检测这类冲突。若已有 `raw-superio` 直接
后端，冲突不会阻止用户态控制；软件会在控制循环中周期性重新写入手动模式，
并在检测到固件/BIOS 把模式改回时写入应用日志。诊断日志第 6 节也会列出检测到的
ACPI / SuperIO 冲突记录。

首次安装打开「硬件检测」后，可通过顶部按钮切换飞牛驱动 / 软件驱动。飞牛驱动
标定无响应时，软件会自动尝试软件驱动并重新标定；软件驱动仍失败时只在检测页
给出可关闭的排查提示，不会强制反馈或强制导出日志。

**NCT6683 / 6686 / 6687 为什么只读**：内核 `nct6683` 驱动的文档明确写着，Intel EC
固件的寄存器布局与 Nuvoton 数据手册不符，**从操作系统写入任何值都被视为风险过高
而禁用**（驱动根本不导出可写的 `pwmN`）。这类主板（ASRock B650/X670E、MSI B550/X670
等）应用会识别出控制器并如实告诉你只能监控。

## 威联通 QNAP 机型支持（本 Fork 新增）

部分威联通 NAS 换装飞牛 OS 后没有风扇控制：它们的风扇由主板上的 **ITE IT8528 EC**
芯片管理，飞牛内核没有与之对话的驱动，风扇只能按 EC 固件的出厂策略转。

本 Fork 的做法（不修改飞牛系统文件，一切收在应用包内）：

- 应用包自带开源内核驱动 **qnap8528**（作者 [0xGiddi](https://github.com/0xGiddi/qnap8528)，
  源码在本仓库 `qnap8528/` 目录，文件头含其版权与 GPL 声明）编译出的模块
  `app/driver/qnap8528-<内核版本>.ko`；
- 应用启动时加载该模块，EC 的风扇转速 / PWM / 温度随即以标准 hwmon 接口出现，
  由 NiuFan 按温度曲线统一调速；应用停止时卸载模块，风扇交还 EC 固件——
  任何异常情况下都不会停转失控。

### 支持口径（请务必读完）

qnap8528 驱动的配置表覆盖**一百余款**威联通机型（主板码 + 背板码齐全，
机器可被驱动自动识别）。据此我们如实声明：

- **大概率可用，但不保证。** 驱动配置表所列机型理论上都能被识别并控制风扇；
- **除 TS-564 外，均未经本项目测试。** 本项目仅在 **TS-564（飞牛 OS）** 上完成
  完整实测：驱动加载、转速标定、温度曲线闭环调速、停启交还 EC 均验证通过；
- 其他机型如需使用，请自行按下方说明编译与内核匹配的模块；可用与否欢迎回报，
  机型配置层面的问题请同时反馈给上游驱动项目。

### 机型一览（驱动配置表覆盖，含同型号不同主板版本）

完整表格（含主板码 / 背板码）见本仓库 `qnap8528/README.md` 的
*Supported Models* 一节（驱动项目原文）。型号名单如下：

- **TS 桌面型**：TS-251A、TS-251D、TS-253A、TS-253B、TS-253BE、TS-253D、
  TS-262、TS-262C、TS-264、TS-264C、TS-364、TS-451A、TS-451D、TS-451D2、
  TS-453A、TS-453B、TS-453BE、TS-453D、TS-453DMINI、TS-453BMINI、TS-453MINI II、
  TS-462、TS-462C、TS-464、TS-464C、TS-464C2、TS-464T4、TS-466C、
  TS-473、TS-473A、TS-551、TS-553AS、**TS-564（本项目实测）**、TS-653A、TS-653B、
  TS-653D、TS-664、TS-673、TS-673A、TS-686、TS-853A、TS-873、TS-873A、
  TS-886、TS-973AX、TS-ALTO4、TBS-453A、TBS-453DX、TBS-464、HS-453DX、Qu605
- **TVS 桌面型**：TVS-472X、TVS-472XT、TVS-474、TVS-473e、TVS-673e、TVS-873e、
  TVS-672N、TVS-672X、TVS-672XT、TVS-674、TVS-675、TVS-872N、TVS-872X、
  TVS-872XT、TVS-874、TVS-H674T、TVS-H874T、TVS-H674X、TVS-H874X、
  TVS-951N、TVS-951X、TVS-1288X、TVS-1688X
- **机架型**：TS-464U、TS-464EU、TS-864EU、TS-1264U、TS-655X、TS-855X、
  TS-855EU、TS-873AEU、TS-873AEU-RP、TS-873AU、TS-873AU-RP、TS-1273AU、
  TS-1273AU-RP、TS-1673AU-RP、TS-977XU、TS-983XU、TS-883XU、TS-1283XU、
  TS-1683XU、TS-877XU、TS-1277XU、TS-1677XU、TS-2477XU、TS-2483XU、
  TS-1090FU、TS-1290FX、TS-2490FU、TS-1655、TS-1685、TES-1685-SAS、
  TS-1886XU、TS-1886XU R2、TS-1887XU、TS-2287XU、TS-2888X、TS-3087XU、
  TS-3088XU、TES-1885U、TES-3085U、TVS-875U、TVS-1275U、TVS-1675U、
  TNS-1083X、TNS-C1083X、TDS-2489FU、TDS-2489FU R2、BA-400、BA-600、BA-800

注：部分机型（如 TS-464 系、TS-253D）需要以 `skip_hw_check=true` 加载模块，
本 Fork 加载时恒带该参数，已覆盖。个别机型的部分硬盘 LED 由其他硬件管理
（与风扇无关），细节见驱动原文。

### 内核版本与模块重编

`.ko` 与内核版本严格绑定。安装包自带的模块对应飞牛内核
**6.18.18.c1126-trim**；飞牛升级内核后，风扇会自动回落 EC 出厂策略（安全），
按仓库 `REBUILD.md` 用新内核头文件重编模块、放入 `app/driver/` 即可恢复，
全过程约十分钟，不动宿主机环境。

## 从源码构建

需要飞牛官方的打包工具 `fnpack`（随 fnOS 提供，位于 `/usr/local/bin/fnpack`）。

```bash
git clone https://github.com/LiuFudi/NiuFan.git
cd NiuFan
./build-fpk.sh                     # 产出 dist/niufan-<版本>-<架构>-<系统>.fpk
```

构建脚本跑的是 `tools/release_checks.py`，任何一条不通过都直接以非零码退出：

1. **版本号交叉校验**：`package/manifest` 的 `version`、`app/server/fancontrold.py` 的
   `VERSION`、`CHANGELOG.md` 顶层条目、README 顶部徽章必须一致
2. 从 `package/manifest` 读取 `appname`、`version` 与 `platform`（架构）
3. 清理 `__pycache__` / `*.pyc` / 遗留 `.fpk`；外部收款码目录存在时重建内嵌模块，
   不存在时使用仓库内已提交的 `donate-qr.js`
4. **打包树检查**：必需文件齐全；`appname` / `desktop_applaunchname` / `display_name` /
   网关路径等标识未被改动；无软链接、无脚手架占位符、无主机专属路径或口令；
   manifest 的值里不能出现 `;`（fnpack 会把它当行内注释，desc 会被截断）
5. 调用 `fnpack build`，并检查产物文件真的存在（`fnpack` 失败也返回 0）
6. 归一包内权限（`cmd/*` 755、其余 644）并重算 manifest 的 `checksum`
7. **成品检查**：版本、`checksum == md5(app.tgz)`、权限、软链接，以及包内 UI 文案
   （`<h1>NiuFan</h1>`、Github 链接与 QQ 群号）都对，才重命名为
   **`<appname>-<version>-<arch>-<system>.fpk`**（如 `niufan-2.2.5-x86-fn.fpk`）放进 `dist/`
8. 生成**源码包**（`release_checks.py sourcepkg`）：按白名单收集源码 → 写成以 `<appname>/`
   为根的 `niufan-<版本>-source.tar.gz` 与 `.zip` → 回读包内 `manifest.version` 校验
9. 删掉无版本号的遗留产物与**其它版本**的产物 —— 工作区只留当前版本
   （《通用开发规范》3.1；历史版本归档在 `/vol1/1000/软件备份/NiuFan/`）

图标可以重新生成（纯 Python，无第三方依赖）：

```bash
python3 tools/make_icons.py
```

改动前后的数据兼容性可以单独验：

```bash
python3 tools/check_compat.py      # 旧配置读取、改名迁移、卸载保留数据、旧 donate.json
```

## 工作原理

```
浏览器 iframe
   └─ 飞牛统一网关 /app/niufan   ← 校验登录态 + 转发 X-Trim-Isadmin
        └─ Unix socket  package/target/app.sock   ← 不监听任何 TCP 端口
             └─ fancontrold.py (以 root 运行)
                  ├─ 控制循环线程：读温度 → 曲线插值 → 写 /sys/class/hwmon/*/pwmN
                  └─ HTTP 线程：静态页面 + /api/*
```

**控制循环**每 `interval` 秒执行一次：读取启用的温度源 → 对每个风扇取其绑定源中的最高温 →
代入曲线做线性插值得到百分比 → 按 `min_duty` / `max_duty` 夹取为 0–255 的转速 →
仅在数值变化时才写 `pwmN`。降速前还要通过温度迟滞检查。

**为什么必须是 root**：写 `/sys/class/hwmon/*/pwm*`（`0644 root:root`）、
`modprobe nct6775`、以及读硬盘 SMART 都需要 root。这段权限属于**应用**而不是用户 ——
装一次之后守护进程常驻 root，日常调节只看网页，不需要 sudo、不需要命令行。

## 配置

配置以 JSON 保存在应用配置目录（`/volN/@appconf/niufan/config.json`），
界面保存时原子写入。手工编辑也会在下次保存时被服务端规范化。

```jsonc
{
  "version": 1,
  "enabled": true,          // 总开关；false 时全部交还 BIOS
  "interval": 3,            // 控制周期（秒）
  "hdd_interval": 60,       // 硬盘温度读取间隔（秒），调大可减少唤醒休眠硬盘
  "hysteresis": 3,          // 温度迟滞（°C）
  "fail_safe_duty": 255,    // 所有温度源失效时的转速
  "sources": {
    "cpu": true,
    "gpu": false,
    "hdd": true,
    "hdd_devices": []       // 空数组 = 使用全部硬盘中的最高温度
  },
  "fans": [
    {
      "channel": 2,         // SuperIO 通道号
      "name": "CPU_FAN",
      "mode": "curve",      // curve | manual | auto
      "source": ["cpu"],    // 可多选，取最高温
      "points": [[30, 20], [45, 35], [60, 60], [75, 100]],   // [温度°C, 转速%]
      "min_duty": 60,
      "max_duty": 255,
      "manual_duty": 128
    }
  ]
}
```

## HTTP 接口

服务通过 Unix socket 暴露一个小 JSON API，由飞牛网关鉴权后转发：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ping` | 存活探测 |
| GET | `/api/status` | 温度源、风扇实时状态、当前配置 |
| GET | `/api/config` | 仅配置 |
| GET | `/api/hardware` | 探测到的 SuperIO 通道与硬盘清单 |
| GET | `/api/diagnostics/status` | 可控制控制器数量、检测到的 hwmon 名称、警告文案、已存日志清单 |
| POST | `/api/diagnostics/export` | 采集硬件信息并生成诊断日志，返回文件名与下载地址 |
| GET | `/api/diagnostics/download?file=<名称>` | 下载诊断日志（`Content-Disposition: attachment`） |
| POST | `/api/config` | 校验 → 原子落盘 → 立即生效 |
| POST | `/api/action` | `{"action":"restore-auto"}` / `{"action":"refresh-hardware"}` |

守护进程另有独立子命令，便于排错（均需 root）：

```bash
python3 package/app/server/fancontrold.py status      --appdest <dir> --etc <dir>
python3 package/app/server/fancontrold.py init-config --appdest <dir> --etc <dir> [--force]
python3 package/app/server/fancontrold.py restore     --appdest <dir> --etc <dir>
python3 package/app/server/fancontrold.py run --host 127.0.0.1 --port 8099   # 本地调试
```

## 安全设计

| 场景 | 行为 |
|---|---|
| 停止 / 卸载应用 | 守护进程退出前把它**实际驱动过**的通道写回 `pwmN_enable=5`（BIOS 自动）；从未管理过的通道保持原样不动 |
| 守护进程被强杀 | `cmd/main` 检测到需要 `kill -9` 时兜底执行全量 `restore` |
| 从配置移除风扇 | 该通道立即恢复成**接管前记录的状态**（enable + duty 一并还原） |
| 温度源全部失效 | 升到 `fail_safe_duty`（默认全速），界面告警 |
| 非管理员访问 | 服务端校验网关注入的 `X-Trim-Isadmin`，为假或缺失一律 `403`（fail-closed） |
| 本机其它账号 | 应用 socket 为 `0600 root:root`，无法绕过网关直连；访问控制交还飞牛登录态 |
| 路径穿越 | 静态文件服务对目标路径做 `realpath` 边界校验 |
| 诊断日志下载 | 只接受本应用生成的 `*.txt` 文件名（`..`、绝对路径、子目录、软链接逃逸一律拒绝）；导出与下载都要登录态，没有网关登录态的网络请求一律 `403` |

## 常见问题

<details>
<summary><b>界面提示「未找到风扇控制器」</b></summary>

先确认驱动是否加载成功：

```bash
sudo modprobe nct6775
dmesg | grep -iE "nct|it87|f718|w836|sch56"   # 芯片有没有被识别
cat /sys/class/hwmon/*/name                    # 驱动注册了哪些节点
ls /sys/class/hwmon/*/name | xargs cat | grep -i nct
```

如果 `dmesg` 里出现 `ACPI: resource conflict`，说明 ACPI 占用了 SuperIO 的
I/O 端口，需要在启动参数里加 `acpi_enforce_resources=lax` 后重启。

如果界面**点名列出了某个芯片**，说明它已被识别但没有可用的写入通道，见 [兼容性](#兼容性)。
</details>

<details>
<summary><b>打开应用显示 Bad Gateway（502）</b></summary>

网关连不上应用，通常是应用没起来或 socket 没建立：

```bash
sudo appcenter-cli status niufan
ls -la /var/apps/niufan/target/app.sock    # 关键
sudo /var/apps/niufan/cmd/main status; echo "exit=$?"
```

若状态显示 running 但 socket 不存在，是 1.7.1 修掉的缺陷：PID 文件跨重启保留，
而内核会复用 PID —— 重启后旧 PID 被别的服务占用时，只靠 `kill -0` 会误判应用仍在运行，
应用中心便跳过启动。恢复：

```bash
sudo appcenter-cli stop niufan
sudo rm -f /var/apps/niufan/var/app.pid
sudo appcenter-cli start niufan
```
</details>

<details>
<summary><b>能读到转速，但改 PWM 没反应</b></summary>

点**硬件检测 → 主动检测**：应用会以约 30% 与 100% 转速各测一次，并在「有转速但
调不动」时直接列出原因。按提示对照即可：

| 检测结果 | 处理 |
| --- | --- |
| 写入转速 X 但读回 Y | 寄存器没接受写入，驱动或芯片忽略了它 |
| 写 `pwmN_enable` 被拒绝 | 芯片进不了手动模式，仍由 BIOS 自动曲线控制 |
| 模式显示 **DC** | DC 电压调速模式，部分主板不响应转速写入。试 `echo 1 > .../pwmN_mode` 切到 PWM 后重测 |
| 寄存器读写正常、转速却不变 | 问题在风扇或接线：3 针风扇插在 PWM 接头上时第 4 根线不起作用，风扇恒速运转 |

手工最小排查（`hwmonX` 换成实际节点）：

```bash
H=/sys/class/hwmon/hwmonX
cat $H/pwm1_mode; cat $H/pwm1_enable
echo 1 > $H/pwm1_enable
echo 255 > $H/pwm1; sleep 5; cat $H/fan1_input
echo 76  > $H/pwm1; sleep 5; cat $H/fan1_input
```

两次转速相同 → 写入没到达风扇；不同 → 调速其实是好的，问题在曲线或配置上。

> 判断转速是否稳定请给足等待时间：大风扇降速要好几秒，读太早会拿到**上一个转速**。
</details>

<details>
<summary><b><code>pwmN_enable</code> 回读值和写入值不一致</b></summary>

已知的驱动行为差异。在 NCT6796D 上写入 `1`（手动）会读回 `0`。
本应用因此**不依赖回读值判断模式**，而是自行记录已下发的模式，
界面上显示的是应用自己的有效模式。
</details>

<details>
<summary><b>有 4 针风扇但转速为 0</b></summary>

该接头没插风扇，或者风扇没有测速线。默认配置只包含有转速读数的通道，
如果某个风扇当前停转导致被漏判，可以在界面底部用「添加该通道」手动加回，
不想要时点卡片上的「移除」即可。
</details>

<details>
<summary><b>担心硬盘被频繁唤醒</b></summary>

把「全局设置 → 硬盘温度读取间隔」调大（例如 300 秒）。
另外若硬盘能走 `drivetemp` / `nvme` 内核驱动，走的是纯 sysfs 读取；
只有没有 hwmon 节点的盘才会退回 `smartctl -n standby`，该参数保证不会唤醒休眠盘。
</details>

## 目录结构

```
NiuFan/
├── LICENSE                   GPL-3.0
├── README.md / README.en.md
├── CHANGELOG.md
├── build-fpk.sh              构建（fnOS / Linux）+ 版本化命名
├── package/                  fnpack 源码树（会被打包进 fpk）
│   ├── manifest
│   ├── ICON.PNG / ICON_256.PNG
│   ├── app/
│   │   ├── server/           守护进程（纯标准库）
│   │   │   ├── fancontrold.py    控制循环 + HTTP API + 静态页面
│   │   │   ├── fandiag.py        诊断日志采集（硬件快照 + 脱敏 + 存储）
│   │   │   ├── hardwareid.py     主动设备 ID / SuperIO 探测与直接后端
│   │   │   ├── fanhardware.py    SuperIO / hwmon / 温度源
│   │   │   └── fanconfig.py      配置校验 + 曲线插值
│   │   └── ui/               前端（原生 JS）
│   ├── cmd/                  生命周期脚本
│   └── config/               privilege / resource
├── tools/make_donate_qr.py   从源码树外读取收款码并生成内嵌模块
├── tools/make_icons.py       纯 Python 图标生成
├── tools/release_checks.py   版本校验 / 打包树检查 / 权限归一 / 成品检查
├── tools/check_compat.py     新旧配置与升级回退的兼容性检查
├── tools/test_diagnostics.py 诊断日志接口的端到端与安全测试
└── dist/                     本地构建产物（被 .gitignore 排除，不入仓库和源码包）
```

运行时目录（安装后，`etc/` 与 `var/` 都是平台分配的持久目录）：

```
etc/config.json               用户配置（曲线、标定、温度源选择）
var/info.log                  运行日志
var/diagnostics/*.txt         导出的诊断日志（最近 20 份 / 最多 7 天）
```

## 贡献

欢迎 Issue 和 PR，尤其是：

- **硬件适配** —— 如果你有 ITE / Fintek / 其它 SuperIO 的机器，欢迎提供
  `dmesg`、`ls /sys/class/hwmon/*/name`、`sensors` 输出，一起评估适配
- **截图** —— 界面截图会让 README 完整很多
- **英文文档校对** —— `README.en.md` 欢迎润色

## 免责声明

本软件直接读写主板 SuperIO 的 PWM 寄存器。作者已尽力保证安全设计
（失效保护、停止即交还 BIOS、最低转速限制），但**因使用本软件导致的任何
硬件损坏或数据损失，作者不承担责任**。请自行评估风险，尤其是首次在非验证过的
主板上使用时。

## 打赏支持

应用界面右上角有一个 ❤ 打赏入口。它是**纯本地**的：

- 不联网、不上报、不统计点击；
- 打赏完全自愿，**不影响任何功能**，也不会改变软件的任何行为。<br />如果遇到BUG，欢迎前往<a href="https://github.com/LiuFudi/NiuFan" style="color:red;">Github</a>反馈或者添加<span style="color:red;">QQ群：818299505</span>

## 许可证

[GNU General Public License v3.0 或更新版本](LICENSE) © 2026 [LiuFudi](https://github.com/LiuFudi)

NiuFan 是自由软件：你可以依照自由软件基金会发布的 GNU 通用公共许可证
（第 3 版，或你选择的任何更新版本）的条款重新发布和/或修改它。

NiuFan 分发时希望它有用，但不提供**任何担保**，甚至不带对适销性或特定用途
适用性的默示担保。详见 GNU 通用公共许可证。

你应当已随本程序收到 GNU 通用公共许可证的副本；如果没有，
请见 <https://www.gnu.org/licenses/>。

> **关于许可证变更**：1.9.0 及更早的版本以 **MIT** 许可证发布。许可证变更仅对
> 新版本生效 —— 已经获得那些版本的人依然保留 MIT 授予的权利，这一点无法撤销。
