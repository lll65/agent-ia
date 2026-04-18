import subprocess
import resource
from plugins.base import Plugin
from config import config


class CodeExecPlugin(Plugin):
    name = "exec_python"
    description = "Exécute du code Python de façon sécurisée (sandbox subprocess, timeout 15s)."
    parameters = {
        "code": {"type": "string", "description": "Code Python à exécuter", "required": True},
        "timeout": {"type": "integer", "description": "Timeout en secondes (max 30)", "required": False},
    }

    def run(self, code: str, timeout: int = 15) -> str:
        timeout = min(timeout, 30)

        def limit_resources():
            # RAM max 256MB, CPU max 10s
            try:
                resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
                resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
            except Exception:
                pass

        try:
            result = subprocess.run(
                ["python3", "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=config.OUTPUT_DIR,
                preexec_fn=limit_resources,
            )
            out = result.stdout.strip()
            err = result.stderr.strip()
            if err and not out:
                return f"Erreur:\n{err}"
            if err:
                return f"Sortie:\n{out}\n\nAvertissements:\n{err}"
            return out or "(aucune sortie)"
        except subprocess.TimeoutExpired:
            return f"Timeout: le code a dépassé {timeout}s."
        except Exception as e:
            return f"Erreur d'exécution: {e}"
