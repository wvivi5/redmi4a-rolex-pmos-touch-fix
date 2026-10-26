#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""postmarketOS (Redmi 4A / rolex) FT5336 触摸补丁工具。

做两处等长改写（详见 docs/ROOT-CAUSE.md）：

  1. touchscreen@38 的 status:      "disabled"  ->  "okay"（补 \\0 保持长度）
  2. touchscreen@5d 的 compatible:  "goodix,gt911" -> "goodix,gt000"
     （阻止 lk2nd 启用 GT911 —— 它和 FT5336 共用 reset GPIO 64 / INT GPIO 65）

用法：
  python tools/patch_pmos_touch.py qcom-msm89x7.img --check
  python tools/patch_pmos_touch.py qcom-msm89x7.img
  python tools/patch_pmos_touch.py qcom-msm89x7.img --revert

  # 手动指定 boot 分区偏移（默认从 MBR 里找第一个 0x83 Linux 分区）
  python tools/patch_pmos_touch.py disk.img --boot-offset 1048576

只改文件数据块，不动 ext2 元数据；文件长度不变。
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ext2tool import Ext2, Ext2Error  # noqa: E402

NODE_TOUCH = "touchscreen@38"
NODE_GOODIX = "touchscreen@5d"
PROP_STATUS = "status"
PROP_COMPAT = "compatible"

# (节点名, 属性名, 原值, 修复后值) —— 必须等长
EDITS = [
    (NODE_TOUCH, PROP_STATUS, b"disabled\x00", b"okay\x00\x00\x00\x00\x00"),
    (NODE_GOODIX, PROP_COMPAT, b"goodix,gt911\x00", b"goodix,gt000\x00"),
]

DEFAULT_DTBS = [
    "/msm8917-xiaomi-rolex.dtb",
    "/dtbs/qcom/msm8917-xiaomi-rolex.dtb",
]


# ---------------------------------------------------------------- FDT
def iter_props(blob):
    """遍历 DTB，产出 (节点路径, 属性名, 值在 blob 中的偏移, 值长度)。"""
    (_magic, _total, off_struct, off_strings, _rsv, _ver, _lc, _cpu,
     _szs, _szst) = struct.unpack_from(">10I", blob, 0)
    pos = off_struct
    stack = []
    while True:
        tok = struct.unpack_from(">I", blob, pos)[0]
        pos += 4
        if tok == 1:                       # FDT_BEGIN_NODE
            end = blob.index(b"\x00", pos)
            stack.append(blob[pos:end].decode("utf-8", "replace"))
            pos = (end + 4) & ~3
        elif tok == 2:                     # FDT_END_NODE
            stack.pop()
        elif tok == 3:                     # FDT_PROP
            plen, nameoff = struct.unpack_from(">II", blob, pos)
            pos += 8
            end = blob.index(b"\x00", off_strings + nameoff)
            pname = blob[off_strings + nameoff:end].decode()
            yield ("/".join(stack), pname, pos, plen)
            pos = (pos + plen + 3) & ~3
        elif tok == 4:                     # FDT_NOP
            continue
        elif tok == 9:                     # FDT_END
            break
        else:
            raise ValueError("DTB 结构异常：未知 token %d" % tok)


def apply_edits(blob, revert=False):
    """返回 (新 blob, [(节点, 属性, 偏移, 改前, 改后)])。已达标则改动为空。"""
    out = bytearray(blob)
    done = []
    for node, prop, old, new in EDITS:
        if revert:
            old, new = new, old
        hits = [(p, off, ln) for p, name, off, ln in iter_props(bytes(out))
                if p.endswith(node) and name == prop]
        if len(hits) != 1:
            raise ValueError("%s 的 %s 属性命中 %d 次（预期 1 次），DTB 结构不符合预期"
                             % (node, prop, len(hits)))
        _p, off, ln = hits[0]
        cur = bytes(out[off:off + ln])
        if cur == new:
            continue
        if cur != old:
            raise ValueError("%s/%s 当前值 %r，既不是 %r 也不是 %r（可能已被别的方式改过）"
                             % (node, prop, cur, old, new))
        out[off:off + ln] = new
        done.append((node, prop, off, cur, new))
    return bytes(out), done


# ---------------------------------------------------------------- 分区定位
def mbr_linux_offset(image):
    with open(image, "rb") as f:
        mbr = f.read(512)
    if len(mbr) != 512 or struct.unpack_from("<H", mbr, 510)[0] != 0xAA55:
        raise SystemExit("不是 MBR 镜像；请用 --boot-offset 手动指定 boot 分区偏移")
    candidates = []
    for i in range(4):
        e = mbr[446 + i * 16: 446 + (i + 1) * 16]
        ptype = e[4]
        lba, cnt = struct.unpack_from("<II", e, 8)
        if ptype == 0xEE:
            raise SystemExit("这是 GPT 保护分区，本工具只自动识别 MBR；请用 --boot-offset 指定")
        if ptype == 0x83 and lba and cnt:
            candidates.append((lba * 512, cnt * 512))
    if not candidates:
        raise SystemExit("MBR 里没找到 0x83 Linux 分区；请用 --boot-offset 指定")
    return candidates[0][0]


def find_dtbs(fs, explicit):
    if explicit:
        paths = explicit
    else:
        paths = []
        for p in DEFAULT_DTBS:
            if fs.lookup(p) is not None:
                paths.append(p)
        if not paths:
            # 退路：在 boot 分区里搜 *rolex*.dtb
            for path, e in fs.walk():
                if e["type"] == 1 and "rolex" in e["name"] and e["name"].endswith(".dtb"):
                    paths.append(path)
    if not paths:
        raise SystemExit("boot 分区里没找到 rolex 的 dtb，可用 --dtb /路径 指定")
    return paths


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="postmarketOS rolex FT5336 触摸补丁")
    ap.add_argument("image", help="整机镜像，例如 qcom-msm89x7.img")
    ap.add_argument("--boot-offset", type=lambda s: int(s, 0),
                    help="boot 分区在镜像中的字节偏移（默认从 MBR 自动找）")
    ap.add_argument("--dtb", action="append", default=[],
                    help="boot 分区内 dtb 路径，可重复（默认 /msm8917-xiaomi-rolex.dtb 等）")
    ap.add_argument("--revert", action="store_true", help="还原成原始状态")
    ap.add_argument("--check", action="store_true", help="只检查，不写入")
    ap.add_argument("--export", metavar="FILE",
                    help="把修复后的 dtb 另存一份（默认导出到当前目录）")
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        raise SystemExit("镜像不存在: %s" % args.image)

    offset = args.boot_offset if args.boot_offset is not None else mbr_linux_offset(args.image)
    print("镜像  : %s (%.2f GB)" % (args.image, os.path.getsize(args.image) / 1e9))
    print("boot  : 偏移 %d (0x%X)" % (offset, offset))

    try:
        fs = Ext2(args.image, offset)
    except Ext2Error as e:
        raise SystemExit("打不开 boot 分区: %s" % e)

    paths = find_dtbs(fs, args.dtb)
    action = "检查" if args.check else ("还原" if args.revert else "修复")
    exported = None

    for path in paths:
        ino = fs.lookup(path)
        data = fs.read_file(ino)
        fixed, done = apply_edits(data, args.revert)
        print("\n%s  (%d B) —— %s%s"
              % (path, len(data), action, "，已是目标状态" if not done else ""))
        for node, prop, off, old, new in done:
            print("   %-16s %-12s @0x%X  %r -> %r"
                  % (node, prop, off, old, new))
        if args.check:
            for node, prop, _old, _new in EDITS:
                for p, name, o, ln in iter_props(data):
                    if p.endswith(node) and name == prop:
                        print("   当前 %s/%s = %r" % (node, prop, data[o:o + ln]))
            continue
        if done:
            fs.overwrite_same_size(ino, fixed)
            print("   已写入（长度不变）")
        if exported is None:
            exported = fixed

    fs.close()

    if exported is not None and not args.check and not args.revert:
        out = args.export or "msm8917-xiaomi-rolex-touch-fixed.dtb"
        with open(out, "wb") as f:
            f.write(exported)
        print("\n已导出修复后的 dtb: %s" % out)

    if not args.check:
        print("\n完成。刷入后验证：")
        print("  cat /sys/bus/i2c/devices/1-0038/name     # 期望 generic ft5x06 (53)")
        print("  sudo dmesg | grep -iE 'ft5x06|edt'")


if __name__ == "__main__":
    main()
