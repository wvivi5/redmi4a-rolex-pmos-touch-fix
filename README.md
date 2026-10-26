# 红米 4A (rolex) postmarketOS 触摸修复

> Redmi 4A / rolex（msm8917）· FT5336 触摸（I2C 0x38）· postmarketOS / Phosh
> 只改设备树 **11 个字节**，不动内核、不动驱动、不动 lk2nd。

## 现象

刷 postmarketOS 后屏幕能亮、系统能跑，但**触摸完全没反应**：

```sh
$ cat /sys/bus/i2c/devices/1-0038/name
cat: ...: No such file or directory        # 连设备都没有
$ sudo dmesg | grep -i ft5
（什么都没有）
```

## 根因

主线设备树（`msm8917-xiaomi-rolex.dtb`）里触摸有两条，默认都 `disabled`：

| 节点 | compatible | 对应硬件 |
| --- | --- | --- |
| `touchscreen@38` | `edt,edt-ft5306` | **这台 4A：FT5336**（驱动 `edt_ft5x06`） |
| `touchscreen@5d` | `goodix,gt911` | 另外两种面板用的 GT911 |

lk2nd（pmaports 23.1，以及 GitHub 最新版都一样）的 rolex 面板表把**三个面板**的
`touchscreen-compatible` 全写成 `goodix,gt911`，启动时它只把 GT911 那条的 `status`
删掉（= 启用 GT911），`edt` 那条完全不管 → `edt_ft5x06` 永远不 probe。

**还有个坑：这两条节点共用同一组 GPIO**——`reset-gpios` 都是 `<&tlmm 64 …>`，
中断都是 `<&tlmm 65 …>`。所以**不能**"两条都开"：GT911 驱动一旦 probe，会去拉同一个
reset 脚，把已经初始化好的 FT5336 打回复位，触摸就会时好时坏（取决于模块加载顺序）。

## 修法

只改 boot 分区里的 `msm8917-xiaomi-rolex.dtb`（两份副本），**长度 48,500 字节不变**：

| DTB 偏移 | 属性 | 原值 → 新值 |
| --- | --- | --- |
| `0x7F64`–`0x7F6B` | `touchscreen@38` 的 `status` | `disabled\0` → `okay\0\0\0\0\0` |
| `0x7F9D`–`0x7F9F` | `touchscreen@5d` 的 `compatible` | `goodix,gt911` → `goodix,gt000` |

- `okay` 后面多出来的 `\0` 只是填充（等长改写）。内核按第一个 `\0` 截断，读到的是 `okay`。
- `goodix,gt000` 是**故意改成等长的 12 字节占位名**，让 lk2nd 按 compatible 查节点时
  找不到它，从而不会去启用 GT911、不会碰 GPIO 64/65。该节点仍是 `disabled`。
- `edt,edt-ft5306` 是主线驱动 `edt_ft5x06` 正式支持的 compatible
  （支持 `ft5206/ft5306/ft5406/ft5506`），不用换驱动。

技术细节、取证过程见 **[docs/ROOT-CAUSE.md](docs/ROOT-CAUSE.md)**。

## 用法

### 方式一：给整机镜像打补丁（推荐）

适用于 `qcom-msm89x7.img` 这种带 msdos 分区表的整机镜像（p1 = boot ext2，p2 = root ext4）。
脚本只用"**等长原地覆写**"改 DTB 数据块，**不动 ext2 任何元数据**，不会破坏文件系统。

```bash
python tools/patch_pmos_touch.py qcom-msm89x7.img --check    # 只看状态，不写
python tools/patch_pmos_touch.py qcom-msm89x7.img            # 打补丁（幂等）
python tools/patch_pmos_touch.py qcom-msm89x7.img --revert   # 还原
```

- 需要 Python 3.8+，无第三方依赖。
- 自动从 MBR 找第一个 Linux 分区（0x83）作为 boot 分区；也可 `--boot-offset 1048576` 手动指定。
- 默认处理 `/msm8917-xiaomi-rolex.dtb` 和 `/dtbs/qcom/msm8917-xiaomi-rolex.dtb`，
  不存在会在 boot 分区里自动搜索 `*rolex*.dtb`。
- 打完补丁会同步导出一份 `msm8917-xiaomi-rolex-touch-fixed.dtb` 到当前目录。

### 方式二：只换 DTB（已经装好系统的机器）

把 Release 里的 `msm8917-xiaomi-rolex-touch-fixed.dtb` 复制进 boot 分区覆盖同名文件：

```sh
lsblk                                  # 找到 boot 分区（通常是 mmcblk0p1 或 SD 卡 p1）
sudo mount /dev/mmcblk0p1 /mnt
sudo cp msm8917-xiaomi-rolex-touch-fixed.dtb /mnt/msm8917-xiaomi-rolex.dtb
sudo cp msm8917-xiaomi-rolex-touch-fixed.dtb /mnt/dtbs/qcom/msm8917-xiaomi-rolex.dtb
sync && sudo umount /mnt
```

`lk2nd.img` **不用改**：它按 compatible 找 GT911，找不到就直接跳过
（`lk2nd/device/panel.c`：`if (node < 0) return 0;`），不影响启动。

## 验证

开机后：

```sh
# 1) 驱动绑上了吗
cat /sys/bus/i2c/devices/1-0038/name
#    期望：generic ft5x06 (53)

# 2) 输入设备
cat /proc/bus/input/devices | grep -A4 -i ft5x06
sudo libinput list-devices | grep -A3 -i ft5x06

# 3) 内核日志
sudo dmesg | grep -iE 'ft5x06|edt|goodix'
#    应看到 edt_ft5x06 1-0038 绑定；不应有 goodix probe 成功

# 4) 手指按屏幕
sudo evtest          # 选 ft5x06 的 event，应有 ABS_MT_POSITION_X/Y
```

也可以直接跑仓库里的脚本（在设备上）：

```sh
sudo sh tools/check-touch.sh
```

## 状态

- DTB 改动已完成并自检（两份副本一致、树结构 434 个节点、长度不变）。
- `edt,edt-ft5306` 这个 compatible 在 **Ubuntu 24.04** 上已实机验证：
  `1-0038` 绑定 `edt_ft5x06`，`/dev/input/event4` = `generic ft5x06 (53)`，触摸正常。
- pmOS 侧属于同一颗芯片、同一套主线 DTS 属性，刷入后请按上面的命令确认一次。

## 固件 / 镜像

本仓库**不包含整机镜像**（`qcom-msm89x7.img` 3.06 GB，超过 GitHub Release
单文件 2 GiB 的限制），只发布改动本身：

| 文件 | 大小 | SHA256 |
| --- | --- | --- |
| `dtb/msm8917-xiaomi-rolex-touch-fixed.dtb` | 48,500 B | `ac8c21f9bb93cf3f03a93307a8bd1483dfa20a83b12a061ab5fd5c2bef300aee` |

参考用的原始镜像信息（pmbootstrap 3.12.1 自编译，pmaports `main` / systemd-edge，
内核 `linux-postmarketos-qcom-msm89x7` 7.1.3-r1，Phosh）：

```
qcom-msm89x7.img   3,061,841,920 B
修复前  f8e780f018e8b0ed4ab75e76f87ac31e44013cae99dea8f3007b98f5182a5de6
修复后  643e542eafea419d9fe3f5d2a462933fe6a8ca4972c1a28ac207970b36a0b260
```

两者只差 22 字节（boot 分区里两份 DTB 各 11 字节）。

## 还原

```bash
python tools/patch_pmos_touch.py qcom-msm89x7.img --revert
```

或者把 `status` 改回 `disabled\0`、`compatible` 改回 `goodix,gt911\0`。
这个修复是**针对 FT5336 屏**的：如果同一镜像要刷 GT911 面板的 4A，请先还原。

## 相关

- Ubuntu 24.04 上的同类修复（compatible 写错成 `edt,edt-ft5336`）：
  <https://github.com/wvivi5/redmi4a-rolex-touch-fix>
- 上游 lk2nd：<https://github.com/msm8916-mainline/lk2nd>

## 免责

刷机有风险，确认机型是 Redmi 4A（`rolex` / msm8917）再动手。
本仓库只改设备树里的两个属性，不涉及分区表、引导链和 rootfs。

MIT License.
