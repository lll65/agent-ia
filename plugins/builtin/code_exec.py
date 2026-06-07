import subprocess
import sys
import platform
from plugins.base import Plugin
from config import config


class CodeExecPlugin(Plugin):
    name = "exec_python"
    description = "Exécute du code Python de façon sécurisée (sandbox subprocess, timeout 30s). Retourne stdout + stderr + code de retour."
    parameters = {
        "code": {"type": "string", "description": "Code Python complet à exécuter (utilise print() pour afficher les résultats)", "required": True},
        "timeout": {"type": "integer", "description": "Timeout en secondes (max 60)", "required": False},
    }

    def run(self, code: str, timeout: int = 30) -> str:
        timeout = min(max(timeout, 5), 60)
        python = sys.executable

        # Wrapper: capture les exceptions non traitées proprement
        wrapped = (
            "import sys, traceback\n"
            "try:\n"
            + "\n".join("    " + line for line in code.splitlines())
            + "\nexcept Exception as _e:\n"
            "    print(f'EXCEPTION: {type(_e).__name__}: {_e}', file=sys.stderr)\n"
            "    traceback.print_exc(file=sys.stderr)\n"
        )

        preexec = None
        if platform.system() != "Windows":
            import resource
            def preexec():
                try:
                    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
                    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
                except Exception:
                    pass

        try:
            result = subprocess.run(
                [python, "-c", wrapped],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=config.OUTPUT_DIR,
                preexec_fn=preexec,
            )
            out = result.stdout.strip()
            err = result.stderr.strip()

            # Filtre les warnings Python non critiques
            err_lines = [l for l in err.splitlines() if l.strip()
                         and "DeprecationWarning" not in l
                         and "UserWarning" not in l
                         and "FutureWarning" not in l]
            err_clean = "\n".join(err_lines).strip()

            parts = []
            if out:
                parts.append(f"```\n{out[:3000]}\n```")
            if err_clean:
                label = "Erreur" if result.returncode != 0 else "Stderr"
                parts.append(f"**{label}:**\n```\n{err_clean[:1000]}\n```")
            if not parts:
                parts.append("✅ Code exécuté sans sortie.")
            if result.returncode != 0 and not err_clean:
                parts.append(f"Code de retour: {result.returncode}")

            return "\n\n".join(parts)

        except subprocess.TimeoutExpired:
            return f"⏱️ Timeout: le code a dépassé {timeout}s. Optimise ou augmente le timeout."
        except Exception as e:
            return f"❌ Erreur d'exécution: {e}"
