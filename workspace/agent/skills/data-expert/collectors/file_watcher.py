class FileWatcher:
    """监听目录变化（可选模块，当前为桩实现）"""

    def __init__(self, watch_dir: str):
        self.watch_dir = watch_dir

    def start(self):
        raise NotImplementedError("FileWatcher 暂未实现，需要 watchdog 依赖")
