# 根因与取证：pmOS 下 FT5336 为什么没触摸

设备：Xiaomi Redmi 4A（`rolex`，msm8917）· 触摸 IC：FT5336，I2C-1 地址 `0x38`
系统：postmarketOS（pmbootstrap 3.12.1，pmaports `main` / systemd-edge）
内核：`linux-postmarketos-qcom-msm89x7` 7.1.3-r1
引导：lk2nd（pmaports 23.1 / 上游最新版行为一致）

---

## 1. 设备树里的两条触摸节点

反编译 boot 分区里的 `msm8917-xiaomi-rolex.dtb`（48,500 B）：

```
[soc@0/i2c@78b7000/touchscreen@38]
  compatible           = "edt,edt-ft5306"
  reg                  = <0x38>
  interrupts-extended  = <&tlmm 65 0x8>
  reset-gpios          = <&tlmm 64 0x1>
  vcc-supply           = <&pm8916_l…>
  iovcc-supply         = <&pm8916_l…>
  touchscreen-size-x   = 720
  touchscreen-size-y   = 1280
  status               = "disabled"

[soc@0/i2c@78b7000/touchscreen@5d]
  compatible           = "goodix,gt911"
  reg                  = <0x5d>
  interrupts-extended  = <&tlmm 65 0x8>
  reset-gpios          = <&tlmm 64 0x1>
  AVDD28-supply        = <&pm8916_l…>
  VDDIO-supply         = <&pm8916_l…>
  status               = "disabled"
```

两条节点**共用同一组引脚**：

- `reset-gpios` 都是 `<&tlmm 64 GPIO_ACTIVE_LOW>`
- `interrupts-extended` 都是 `<&tlmm 65 IRQ_TYPE_LEVEL_LOW>`
- 供电也是同一组 PM8916 LDO

物理上触摸排线只有一组 reset/INT，主线的写法是"同一块板子不同面板，二选一"。

## 2. lk2nd 只启用 GT911

lk2nd 的 rolex 面板表（`lk2nd/device/dts/.../msm8917-xiaomi-rolex.dts`）三个面板：

```
panel/qcom,mdss_dsi_hx8394f_boe_720p_video     { touchscreen-compatible = "goodix,gt911"; }
panel/qcom,mdss_dsi_ili9881c_ebbg_720p_video   { touchscreen-compatible = "goodix,gt911"; }
panel/qcom,mdss_dsi_nt35521s_ebbg_720p_video   { touchscreen-compatible = "goodix,gt911"; }
```

启动时 `lk2nd/device/panel.c` 的逻辑（`lk2nd_panel_dt_update()`）：

```c
/* 把 panel 节点的 compatible 换成实际检测到的面板 */
node = fdt_node_offset_by_compatible(dtb, -1, panel->old_compatible);
ret = fdt_setprop(dtb, node, "compatible", panel->compatible, ...);

/* 只启用 touchscreen-compatible 对应的那一条 */
if (panel->ts_compatible) {
    node = fdt_node_offset_by_compatible(dtb, -1, panel->ts_compatible);
    if (node < 0)
        return 0;                       /* 找不到就算了，继续启动 */

    ret = fdt_nop_property(dtb, node, "status");   /* 删掉 status = 启用 */
}
```

也就是说：**它只会把 `goodix,gt911` 那条的 `status` 删掉**，`edt,edt-ft5306`
那条保持 `disabled`。结果：

- GT911 节点被启用，`goodix` 驱动去 probe `0x5d` —— 那里没有芯片，失败；
- FT5336 节点仍是 `disabled`，`edt_ft5x06` 驱动**根本不会 probe**；
- 触摸供电（L10/2.8V）也就没人去开，`i2c-1` 上 `0x38` 全部 NAK。

所以"换个内核"或"装个驱动"都没用，问题在 DTB 的 `status`。

## 3. 两个坑

### 3.1 不能"两条都开"

两条节点共用 GPIO 64（reset）和 GPIO 65（INT）。GT911 的 `goodix` 驱动 probe 时会
拉 reset 做复位；如果它比 `edt_ft5x06` 晚加载，就会把已经初始化好的 FT5336 打回复位，
触摸表现为"有时能用、有时不能用"，或者开机后好、过一会儿失灵。

因此修复必须**同时让 GT911 保持禁用**。

### 3.2 GT911 节点不能直接删

`status` 是 lk2nd 唯一会动的属性。就算把 GT911 的 `compatible` 保留、把 `status`
改成别的值，lk2nd 也会把 `status` 整个删掉（删除 = 启用），拦不住。

## 4. 改动

对 boot 分区里的 `msm8917-xiaomi-rolex.dtb` 做两处**等长**改写：

| DTB 内偏移 | 节点 | 属性 | 改前 | 改后 |
| --- | --- | --- | --- | --- |
| `0x7F64`–`0x7F6B`（8 B） | `touchscreen@38` | `status` | `disabled\0` | `okay\0\0\0\0\0` |
| `0x7F9D`–`0x7F9F`（3 B） | `touchscreen@5d` | `compatible` | `goodix,gt911` | `goodix,gt000` |

为什么是这两个值：

1. **`okay` 补 `\0`**：DTB 里属性值必须定长存放，`"okay"` 只有 5 字节，补足 8 + NUL
   后长度和 `"disabled\0"` 完全一样，字符串表的偏移都不用动。
   内核 `of_device_is_available()` 用 `strcmp(status, "okay")`，读到第一个 `\0` 就停，
   后面的填充不影响判断。
2. **`goodix,gt000`**：必须也是 12 字节（`goodix,gt911` 的长度），否则 DTB 结构块要整体
   平移。改成 12 字节的占位名后：
   - lk2nd 按 `goodix,gt911` 查找 → 找不到 → `return 0`，不启用、不碰 GPIO；
   - 内核里没有任何驱动 match `goodix,gt000`，节点又还是 `disabled`，完全无害。

改完 DTB 仍然是 48,500 字节，树结构不变（434 个节点），两份副本（`/` 和
`/dtbs/qcom/`）内容一致，整个 3 GB 镜像只差 22 字节。

## 5. 为什么不用 dtc 重编

`dtc` 反编译再编译会把属性按真实长度排布：`"okay"` 只有 5 字节、GT911 的节点如果删掉
更会让整个结构块缩短。文件长度一变，ext2 里的 inode 大小、块指针就和文件内容对不上了
（要么手动改元数据，要么在 Linux 下重新挂载写入）。

本项目选择**不碰文件系统元数据**：读出 DTB → 在内存里等长改写 → 按原块表写回。
`tools/ext2tool.py` 只实现了"读文件 / 等长覆盖"两个操作，不分配、不释放块，
误操作也不会破坏 ext2。

## 6. 验证记录

DTB 自检（打补丁脚本会打印）：

```
touchscreen@38/status      = b'okay\x00\x00\x00\x00\x00'
touchscreen@5d/compatible  = b'goodix,gt000\x00'
节点数 434，文件 48,500 B，两份副本一致
```

同一颗 FT5336、同一个 `edt,edt-ft5306` compatible，在 Ubuntu 24.04 上实测：

```
# ls -l /sys/bus/i2c/devices/1-0038/driver
... -> ../../../bus/i2c/drivers/edt_ft5x06
# cat /sys/bus/i2c/devices/1-0038/name
generic ft5x06 (53)
```

pmOS 侧刷入后用 `tools/check-touch.sh` 复核即可。
