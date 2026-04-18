from abc import ABC, abstractmethod
from typing import Any


class Plugin(ABC):
    """Base class for all plugins. Inherit this to create a new skill."""

    name: str = ""
    description: str = ""
    # JSON Schema des paramètres attendus par run()
    parameters: dict = {}

    @abstractmethod
    def run(self, **kwargs) -> str:
        """Execute the plugin and return a string result."""

    def validate(self, kwargs: dict) -> tuple[bool, str]:
        required = [k for k, v in self.parameters.items() if v.get("required", True)]
        missing = [k for k in required if k not in kwargs]
        if missing:
            return False, f"Paramètres manquants: {missing}"
        return True, ""

    def safe_run(self, **kwargs) -> str:
        ok, err = self.validate(kwargs)
        if not ok:
            return f"[Plugin {self.name}] Erreur: {err}"
        try:
            return self.run(**kwargs)
        except Exception as e:
            return f"[Plugin {self.name}] Erreur d'exécution: {e}"

    def __repr__(self) -> str:
        return f"Plugin(name={self.name!r})"
