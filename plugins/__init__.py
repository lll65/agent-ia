from plugins.loader import PluginLoader

_loader = PluginLoader()

def get_loader() -> PluginLoader:
    return _loader
