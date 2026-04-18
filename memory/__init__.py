from memory.manager import MemoryManager

_manager: MemoryManager | None = None


def get_memory() -> MemoryManager:
    global _manager
    if _manager is None:
        _manager = MemoryManager()
    return _manager
