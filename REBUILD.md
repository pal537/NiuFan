# 飞牛风扇控制（NiuFan 2.2.7 + qnap8528）备份说明

适用机器：威联通 QNAP TS-564（ITE IT8528 EC）+ 飞牛 fnOS。
成品 fpk：`niufan-2.2.7-x86-fn.fpk`（已装机运行，风扇由 NiuFan 按 CPU+硬盘温度曲线调速）。
源码包：`niufan-qnap-sources.tar.gz`（含改过的 NiuFan 全部源码 + qnap8528 驱动源码 + 已编好的 .ko 一份）。

## 原理一句话
NiuFan 应用自带 qnap8528 内核模块（app/driver/qnap8528-<内核版本>.ko），
启动时 insmod 加载（参数 skip_hw_check=true），停止时 rmmod 卸载交还 EC。
不写系统模块目录，不动飞牛系统文件。

## 飞牛升级内核后怎么恢复（约 10 分钟）
模块与内核版本绑定。升级后风扇回落 EC 默认策略（安全，不会停转），按下面重编：

1. 确认新内核版本：NAS 上 `uname -r`，头文件应在 `/usr/src/linux-headers-<版本>`（飞牛自带）。
2. 在 NAS 上用临时容器编译（宿主机不装任何东西）：
   - 把源码包里的 `qnap8528/src/` 放到 NAS 的 /tmp/qnapbuild/src
   - `docker run --rm -v /tmp/qnapbuild/src:/src -v /usr/src/linux-headers-<版本>:/headers:ro debian:12 bash -c 'apt-get update -qq && apt-get install -y -qq gcc make libelf-dev libssl-dev bc flex bison kmod; make -C /headers M=/src modules'`
   - 产物 /tmp/qnapbuild/src/qnap8528.ko，用 `modinfo -F vermagic` 核对与新内核一致。
3. 把新 .ko 改名为 `qnap8528-<新内核版本>.ko`，放进已安装应用目录
   `/vol1/@appcenter/niufan/driver/`（旧 .ko 可留着回滚），重启 NiuFan 应用即可。
   （想长期保存就顺手重打 fpk：源码树 package/ 下 `fnpack build`。）

## 安装注意事项（血泪）
- `appcenter-cli install-fpk` 对已安装的同名应用是**假升级**（不换文件）。
  换版本必须先 `appcenter-cli stop niufan && appcenter-cli uninstall niufan` 再装，
  配置保存在 /vol1/@appconf/niufan，卸载不丢。
- NiuFan 里存的驱动模式偏好如果是「软件驱动」也不影响：qnap8528 在代码里
  绕过该偏好（EC 只能走 hwmon）。

## 关键参数
- 风扇范围：约 900–4380 RPM；0% 不停转（最低约 1137–1246 RPM，EC 硬件特性）。
- 当前曲线：30°C→20%、45°C→30%、67°C→50%、80°C→85%、100°C→100%，
  温度源 = CPU + 硬盘取最高，最低转速限制 25%。
