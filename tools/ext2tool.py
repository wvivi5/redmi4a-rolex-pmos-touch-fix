# -*- coding: utf-8 -*-
"""极简 ext2 读写工具：列目录、读文件、同长度原地覆写。

只用于修改整机镜像里 boot 分区（ext2）中的文件，比如 postmarketOS 的
qcom-msm89x7.img。设计原则：

  * 只支持"覆写长度不变的文件"（overwrite_same_size）；
  * 不分配块、不释放块、不修改 inode / 位图 / 组描述符；
  * 因此不会破坏文件系统，最坏情况也只是把某个文件的内容写坏，用镜像备份恢复即可。

支持 ext2 及"没有 extent"的 ext3/ext4（有 extents 时会直接报错）。
"""
import struct


class Ext2Error(Exception):
    pass


class Ext2:
    def __init__(self, path, offset=0):
        self.f = open(path, "r+b")
        self.off = offset
        sb = self._read(1024, 1024)
        self.magic = struct.unpack_from("<H", sb, 56)[0]
        if self.magic != 0xEF53:
            raise Ext2Error("偏移 %d 处不是 ext2 超级块（magic=0x%04x）" % (offset, self.magic))

        self.inodes_count = struct.unpack_from("<I", sb, 0)[0]
        self.blocks_count = struct.unpack_from("<I", sb, 4)[0]
        self.first_data_block = struct.unpack_from("<I", sb, 20)[0]
        self.log_block_size = struct.unpack_from("<I", sb, 24)[0]
        self.blocks_per_group = struct.unpack_from("<I", sb, 32)[0]
        self.inodes_per_group = struct.unpack_from("<I", sb, 40)[0]
        rev_level = struct.unpack_from("<I", sb, 76)[0]
        self.inode_size = struct.unpack_from("<H", sb, 88)[0] if rev_level >= 1 else 128
        incompat = struct.unpack_from("<I", sb, 96)[0]
        if incompat & 0x40:
            raise Ext2Error("这个 ext4 用了 extents，本工具不支持（会拒绝写入，避免损坏）")

        self.block_size = 1024 << self.log_block_size
        self.gdt_off = (self.first_data_block + 1) * self.block_size
        self.n_groups = ((self.blocks_count - self.first_data_block
                          + self.blocks_per_group - 1) // self.blocks_per_group)

    # ---------- 底层 ----------
    def _read(self, pos, n):
        self.f.seek(self.off + pos)
        d = self.f.read(n)
        if len(d) != n:
            raise Ext2Error("读取越界 @%d" % pos)
        return d

    def _write(self, pos, data):
        self.f.seek(self.off + pos)
        self.f.write(data)

    def group_desc(self, g):
        d = self._read(self.gdt_off + g * 32, 32)
        _bb, _ib, inode_table = struct.unpack_from("<III", d, 0)
        return inode_table

    def inode(self, ino):
        g = (ino - 1) // self.inodes_per_group
        idx = (ino - 1) % self.inodes_per_group
        pos = self.group_desc(g) * self.block_size + idx * self.inode_size
        d = self._read(pos, self.inode_size)
        size = struct.unpack_from("<I", d, 4)[0]
        if self.inode_size > 128:
            size |= struct.unpack_from("<I", d, 108)[0] << 32
        blocks = list(struct.unpack_from("<15I", d, 40))
        return {"ino": ino, "size": size, "blocks": blocks}

    def _block_iter(self, inode):
        bs = self.block_size
        ptrs = bs // 4
        for b in inode["blocks"][:12]:
            if b:
                yield b
        b = inode["blocks"][12]
        if b:
            for x in struct.unpack("<%dI" % ptrs, self._read(b * bs, bs)):
                if x:
                    yield x
        b = inode["blocks"][13]
        if b:
            for x in struct.unpack("<%dI" % ptrs, self._read(b * bs, bs)):
                if not x:
                    continue
                for y in struct.unpack("<%dI" % ptrs, self._read(x * bs, bs)):
                    if y:
                        yield y

    # ---------- 文件 ----------
    def read_file(self, ino):
        inode = self.inode(ino)
        remain = inode["size"]
        out = []
        for b in self._block_iter(inode):
            if remain <= 0:
                break
            n = min(remain, self.block_size)
            out.append(self._read(b * self.block_size, n))
            remain -= n
        if remain != 0:
            raise Ext2Error("文件 %d 数据块不完整（缺 %d 字节）" % (ino, remain))
        return b"".join(out)

    def listdir(self, ino):
        data = self.read_file(ino)
        pos = 0
        while pos < len(data):
            ino2, rec_len, name_len, ftype = struct.unpack_from("<IHBB", data, pos)
            if ino2 and rec_len:
                name = data[pos + 8: pos + 8 + name_len].decode("utf-8", "replace")
                if name not in (".", ".."):
                    yield {"ino": ino2, "name": name, "type": ftype}
            if rec_len <= 0:
                break
            pos += rec_len

    def lookup(self, path):
        ino = 2
        for part in [p for p in path.split("/") if p]:
            found = next((e for e in self.listdir(ino) if e["name"] == part), None)
            if not found:
                return None
            ino = found["ino"]
        return ino

    def walk(self, ino=2, prefix=""):
        for e in self.listdir(ino):
            path = prefix + "/" + e["name"]
            yield path, e
            if e["type"] == 2:
                yield from self.walk(e["ino"], path)

    def overwrite_same_size(self, ino, data):
        inode = self.inode(ino)
        if len(data) != inode["size"]:
            raise Ext2Error("拒绝写入：新内容 %d 字节，原文件 %d 字节（只支持等长覆写）"
                            % (len(data), inode["size"]))
        pos = 0
        for b in self._block_iter(inode):
            if pos >= len(data):
                break
            n = min(self.block_size, len(data) - pos)
            self._write(b * self.block_size, data[pos:pos + n])
            pos += n
        if pos != len(data):
            raise Ext2Error("写入不完整：%d/%d" % (pos, len(data)))
        self.f.flush()

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
