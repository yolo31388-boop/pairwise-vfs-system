import pytest
from vfs.system import VFSSystem


class TestVFSSystem:

    def test_mount_pak(self):
        """挂载pak不做索引，每次读文件都扫描整个包"""
        obj = VFSSystem()
        result = obj.mount_pak()
        self.assertIsNotNone(result)

    def test_read_file(self):
        """读取文件不做路径规范化，../可以跳出沙箱"""
        obj = VFSSystem()
        result = obj.read_file()
        self.assertIsNotNone(result)

    def test_build_index(self):
        """资源索引不做哈希表，线性查找O(n)"""
        obj = VFSSystem()
        result = obj.build_index()
        self.assertIsNotNone(result)

    def test_write_file(self):
        """写入不做原子性，写一半崩溃文件损坏"""
        obj = VFSSystem()
        result = obj.write_file()
        self.assertIsNotNone(result)

    def test_resolve_path(self):
        """路径解析不做大小写处理，Windows和Linux行为不一致"""
        obj = VFSSystem()
        result = obj.resolve_path()
        self.assertIsNotNone(result)

