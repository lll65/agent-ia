"""
Charge automatiquement tous les plugins depuis:
 - plugins/builtin/     (plugins intégrés)
 - data/plugins/        (plugins utilisateur)

Pour ajouter un plugin: crée un fichier .py dans data/plugins/
avec une classe héritant de Plugin.
"""
import importlib
import importlib.util
import inspect
import logging
import sys
from pathlib import Path

from plugins.base import Plugin

logger = logging.getLogger(__name__)


class PluginLoader:
    def __init__(self):
        self._plugins: dict[str, Plugin] = {}
        self._load_builtin()
        self._load_user_plugins()

    def _load_builtin(self):
        builtin_dir = Path(__file__).parent / "builtin"
        for py_file in builtin_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            module_name = f"plugins.builtin.{py_file.stem}"
            try:
                module = importlib.import_module(module_name)
                self._register_from_module(module)
            except Exception as e:
                logger.warning(f"Plugin builtin '{py_file.stem}' non chargé: {e}")

    def _load_user_plugins(self):
        from config import config
        user_dir = Path(config.PLUGINS_DIR)
        if not user_dir.exists():
            return
        for py_file in user_dir.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(f"user_plugin.{py_file.stem}", py_file)
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
                self._register_from_module(module)
                logger.info(f"Plugin utilisateur chargé: {py_file.stem}")
            except Exception as e:
                logger.warning(f"Plugin utilisateur '{py_file.stem}' non chargé: {e}")

    def _register_from_module(self, module):
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Plugin) and obj is not Plugin and obj.name:
                instance = obj()
                self._plugins[instance.name] = instance

    def get(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def run(self, name: str, params: dict) -> str:
        plugin = self.get(name)
        if not plugin:
            available = list(self._plugins.keys())
            return f"Plugin '{name}' inconnu. Disponibles: {available}"
        return plugin.safe_run(**params)

    def list_all(self) -> dict[str, str]:
        return {name: p.description for name, p in self._plugins.items()}

    def reload_user_plugins(self):
        """Recharge les plugins utilisateur sans redémarrer le serveur."""
        to_remove = [k for k in self._plugins if k not in self._get_builtin_names()]
        for k in to_remove:
            del self._plugins[k]
        self._load_user_plugins()

    def _get_builtin_names(self) -> set[str]:
        builtin_dir = Path(__file__).parent / "builtin"
        names = set()
        for py_file in builtin_dir.glob("*.py"):
            if not py_file.name.startswith("_"):
                try:
                    module = importlib.import_module(f"plugins.builtin.{py_file.stem}")
                    for _, obj in inspect.getmembers(module, inspect.isclass):
                        if issubclass(obj, Plugin) and obj is not Plugin and obj.name:
                            names.add(obj.name)
                except Exception:
                    pass
        return names
