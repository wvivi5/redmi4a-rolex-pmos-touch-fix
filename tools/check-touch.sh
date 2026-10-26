#!/bin/sh
# 在 Redmi 4A (rolex) 的 postmarketOS 上自检 FT5336 触摸状态。
# 用法：sudo sh tools/check-touch.sh
set -u

NODE=/sys/bus/i2c/devices/1-0038
fail=0

say() { printf '%s\n' "$*"; }
ok()  { printf '  [OK]   %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; fail=1; }

say "=== 1. i2c 设备 1-0038 ==="
if [ -e "$NODE/name" ]; then
    ok "存在：$(cat "$NODE/name")"
else
    bad "$NODE 不存在（DTB 里 touchscreen@38 的 status 可能仍是 disabled，或者刷错了分区）"
fi

say "=== 2. 驱动绑定 ==="
if [ -e "$NODE/driver" ]; then
    drv=$(basename "$(readlink -f "$NODE/driver")")
    if [ "$drv" = "edt_ft5x06" ]; then
        ok "驱动 = $drv"
    else
        bad "驱动 = $drv（预期 edt_ft5x06）"
    fi
else
    bad "没有驱动绑定（试试 modprobe edt_ft5x06）"
fi

say "=== 3. 输入设备 ==="
if grep -qi 'ft5x06' /proc/bus/input/devices 2>/dev/null; then
    grep -i -A4 'ft5x06' /proc/bus/input/devices | sed 's/^/  /'
    ok "找到 ft5x06 输入设备"
else
    bad "/proc/bus/input/devices 里没有 ft5x06"
fi

say "=== 4. 内核日志 ==="
if command -v dmesg >/dev/null 2>&1; then
    dmesg | grep -iE 'ft5x06|edt_ft5x06|goodix' | tail -n 15 | sed 's/^/  /'
    if dmesg | grep -qi 'goodix.*probe'; then
        say "  [WARN] 看到 goodix 的 probe 记录（GT911 节点可能没挡住，注意共用 GPIO）"
    fi
fi

say "=== 5. GT911 节点（应该保持 disabled） ==="
if [ -e /sys/bus/i2c/devices/1-005d/name ]; then
    say "  [WARN] 1-005d 存在：GT911 节点被启用了（$(cat /sys/bus/i2c/devices/1-005d/name)）"
else
    ok "1-005d 不存在（GT911 未启用，符合预期）"
fi

say ""
if [ "$fail" -eq 0 ]; then
    say "结论：触摸链路正常。手指按屏幕试试，或用 evtest 看 ABS_MT 事件。"
else
    say "结论：有项目失败。先确认刷入的是修复过的 DTB："
    say "  grep -a -o 'edt,edt-ft5306' /boot/msm8917-xiaomi-rolex.dtb | head -1"
fi
exit "$fail"
