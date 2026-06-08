"""
Plugins d'auto-modification — exposent le moteur self_modify comme outils ReAct.
L'agent peut: lire son code, proposer un diff, appliquer une modif (sécurisée), rollback.
"""
from plugins.base import Plugin


class ReadOwnCodePlugin(Plugin):
    name = "read_own_code"
    description = (
        "Lit le code source d'un des fichiers modifiables de l'agent "
        "(agent/system_prompt.py, agent/finance_deep.py, agent/self_improve.py, "
        "plugins/builtin/finance.py). Utilise-le AVANT de proposer une modification."
    )
    parameters = {
        "file": {"type": "string", "description": "Chemin relatif (ex: agent/finance_deep.py)", "required": True},
    }

    def run(self, file: str) -> str:
        from agent.self_modify import read_source
        code = read_source(file)
        if code.startswith("❌"):
            return code
        # Tronque si trop long pour le contexte LLM
        lines = code.splitlines()
        if len(lines) > 400:
            return (
                f"📄 {file} ({len(lines)} lignes) — affichage des 400 premières:\n\n"
                + "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines[:400]))
                + f"\n\n... ({len(lines)-400} lignes supplémentaires)"
            )
        return f"📄 {file} ({len(lines)} lignes):\n\n" + "\n".join(
            f"{i+1}: {l}" for i, l in enumerate(lines)
        )


class ProposeDiffPlugin(Plugin):
    name = "propose_code_diff"
    description = (
        "Génère un diff unifié (preview) entre le code actuel et un nouveau code, "
        "SANS rien appliquer. Permet de visualiser une modification avant de l'appliquer."
    )
    parameters = {
        "file":     {"type": "string", "description": "Chemin relatif du fichier", "required": True},
        "new_code": {"type": "string", "description": "Le nouveau code COMPLET du fichier", "required": True},
    }

    def run(self, file: str, new_code: str) -> str:
        from agent.self_modify import propose_diff
        diff = propose_diff(file, new_code)
        return f"📝 Diff proposé pour `{file}`:\n```diff\n{diff[:3000]}\n```"


class ApplyModificationPlugin(Plugin):
    name = "apply_self_modification"
    description = (
        "Applique une modification de code de façon SÉCURISÉE (backup + validation syntaxe "
        "+ test d'import + rollback auto si échec). Fournis le code COMPLET du fichier, "
        "pas seulement un fragment. Réservé aux fichiers de la whitelist."
    )
    parameters = {
        "file":     {"type": "string", "description": "Chemin relatif du fichier", "required": True},
        "new_code": {"type": "string", "description": "Le nouveau contenu COMPLET du fichier", "required": True},
        "reason":   {"type": "string", "description": "Raison de la modification (1 phrase)", "required": False},
    }

    def run(self, file: str, new_code: str, reason: str = "") -> str:
        from agent.self_modify import apply_modification
        result = apply_modification(file, new_code, reason)
        return result["message"]


class RollbackModificationPlugin(Plugin):
    name = "rollback_last_modification"
    description = "Annule la dernière auto-modification en restaurant le backup. Sécurité."
    parameters = {}

    def run(self) -> str:
        from agent.self_modify import rollback_last
        return rollback_last()["message"]


class SelfModStatusPlugin(Plugin):
    name = "self_modification_status"
    description = (
        "Affiche les fichiers que l'agent peut modifier et l'historique des "
        "dernières auto-modifications (journal)."
    )
    parameters = {}

    def run(self) -> str:
        from agent.self_modify import list_modifiable_files, get_journal
        files   = list_modifiable_files()
        journal = get_journal(10)

        lines = ["## 🔧 État de l'auto-modification\n", "### Fichiers modifiables"]
        for rel, info in files.items():
            status = f"{info['lines']} lignes" if info["exists"] else "introuvable"
            lines.append(f"- `{rel}` ({status})")

        lines.append("\n### Historique récent")
        if not journal:
            lines.append("*Aucune modification enregistrée.*")
        else:
            for e in reversed(journal):
                ts   = e.get("timestamp", "")[:19].replace("T", " ")
                act  = e.get("action", "?")
                icon = {"modify": "✏️", "create": "🆕", "rollback": "↩️"}.get(act, "•")
                lines.append(
                    f"{icon} `{e.get('file','?')}` — {act} "
                    f"(+{e.get('lines_added',0)}/-{e.get('lines_removed',0)}) "
                    f"— {ts}"
                )
                if e.get("reason"):
                    lines.append(f"   ↳ *{e['reason'][:80]}*")
        return "\n".join(lines)
