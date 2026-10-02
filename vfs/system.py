"""
pairwise-vfs-system - VFSSystem

This module contains a deliberately broken implementation.
Fix all bugs so that tests pass.
"""


class VFSSystem:
    def __init__(self):
        self.state = {}

    def mount_pak(self, *args, **kwargs):
        """BUG: 挂载pak不做索引，每次读文件都扫描整个包"""
        return None

    def read_file(self, *args, **kwargs):
        """BUG: 读取文件不做路径规范化，../可以跳出沙箱"""
        return None

    def build_index(self, *args, **kwargs):
        """BUG: 资源索引不做哈希表，线性查找O(n)"""
        return None

    def write_file(self, *args, **kwargs):
        """BUG: 写入不做原子性，写一半崩溃文件损坏"""
        return None

    def resolve_path(self, *args, **kwargs):
        """BUG: 路径解析不做大小写处理，Windows和Linux行为不一致"""
        return None

