"""
Débuggeur de projet — analyse un dossier de code entier, trouve les bugs et
peut les corriger automatiquement (avec sauvegarde .bak avant toute modification).

Utilisable :
  - en Mode Agent : "agent: analyse le projet C:/mon-projet et corrige les bugs"
  - via l'onglet Code & Projets → Débugger un projet
"""
from pathlib import Path
from plugins.base import Plugin

# Extensions de code analysées
_CODE_EXT = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss",
    ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb", ".php",
    ".sql", ".sh", ".vue", ".svelte", ".kt", ".swift",
}
# Dossiers ignorés (dépendances, builds, caches)
_SKIP_DIRS = {
    "node_modules", ".git", "venv", ".venv", "env", "__pycache__", "dist",
    "build", ".next", ".nuxt", ".idea", ".vscode", "target", "vendor",
    ".mypy_cache", ".pytest_cache", "coverage", ".cache", "bin", "obj",
}
_MAX_FILES = 30      # plafond de fichiers analysés (évite de brûler le quota LLM)
_MAX_CHARS = 6000    # taille max envoyée au LLM par fichier


def _coerce_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "oui", "o", "y")


def _collect_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in _CODE_EXT else []
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _CODE_EXT:
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            if p.stat().st_size > 200_000:   # ignore les fichiers énormes
                continue
        except Exception:
            continue
        files.append(p)
    return files


class ProjectDebuggerPlugin(Plugin):
    name = "analyze_project"
    description = (
        "Analyse un projet de code ENTIER (un dossier), trouve les bugs réels et "
        "peut les corriger automatiquement. Paramètres: path (chemin du dossier), "
        "fix (true pour corriger, false pour juste lister)."
    )
    parameters = {
        "path": {"type": "string", "description": "Chemin du dossier du projet à analyser", "required": True},
        "fix":  {"type": "boolean", "description": "true = corrige et sauvegarde (.bak), false = liste seulement", "required": False},
    }

    def run(self, path: str = "", fix=False, **_) -> str:
        from llm.client import chat

        fix = _coerce_bool(fix)
        root = Path(str(path).strip().strip('"').strip("'")).expanduser()
        if not root.exists():
            return f"❌ Dossier/fichier introuvable : {path}"

        files = _collect_files(root)
        if not files:
            return f"❌ Aucun fichier de code trouvé dans : {path}"

        truncated = len(files) > _MAX_FILES
        files = files[:_MAX_FILES]

        report: list[str] = [f"# 🔍 Analyse — {root.name} ({len(files)} fichiers)\n"]
        files_with_bugs = 0
        fixed = 0

        for f in files:
            try:
                code = f.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                report.append(f"## ⚠️ {f.name} — illisible ({e})\n")
                continue
            if not code.strip():
                continue

            rel = f.relative_to(root) if root.is_dir() else f.name
            snippet = code[:_MAX_CHARS]

            find_prompt = (
                f"Fichier `{f.name}` :\n```\n{snippet}\n```\n\n"
                "Tu es un ingénieur senior. Liste UNIQUEMENT les VRAIS bugs (logique fausse, "
                "erreur de syntaxe, exception non gérée, faille de sécurité, variable non définie). "
                "Pour chacun : ligne approx. + problème + correction proposée. Sois concis. "
                "S'il n'y a aucun bug réel, réponds EXACTEMENT : RAS"
            )
            try:
                analysis = chat([{"role": "user", "content": find_prompt}], temperature=0.1)
            except Exception as e:
                report.append(f"## ⚠️ {rel} — analyse impossible ({str(e)[:80]})\n")
                continue

            a_low = analysis.strip().lower()
            if a_low.startswith("ras") or a_low in ("", "aucun bug", "aucun bug réel"):
                continue

            files_with_bugs += 1
            report.append(f"## 🐞 {rel}\n{analysis.strip()}\n")

            if fix and len(code) <= _MAX_CHARS:   # on ne corrige que si on a vu le fichier ENTIER
                fix_prompt = (
                    f"Fichier `{f.name}` avec bugs :\n```\n{snippet}\n```\n\n"
                    f"Bugs identifiés :\n{analysis}\n\n"
                    "Renvoie le fichier COMPLET corrigé dans UN SEUL bloc ``` … ```. "
                    "Garde tout le code qui fonctionne, corrige seulement les bugs. Aucun texte hors du bloc."
                )
                try:
                    import re
                    raw = chat([{"role": "user", "content": fix_prompt}], temperature=0.1)
                    m = re.search(r"```[a-zA-Z0-9]*\n(.*?)```", raw, re.DOTALL)
                    new_code = (m.group(1) if m else raw).strip() + "\n"
                    if len(new_code) >= 30:
                        bak = f.with_suffix(f.suffix + ".bak")
                        bak.write_text(code, encoding="utf-8")
                        f.write_text(new_code, encoding="utf-8")
                        fixed += 1
                        report.append(f"   ✅ Corrigé — sauvegarde de l'original : `{bak.name}`\n")
                    else:
                        report.append("   ⚠️ Correction non appliquée (résultat trop court).\n")
                except Exception as e:
                    report.append(f"   ⚠️ Correction échouée ({str(e)[:80]}).\n")
            elif fix:
                report.append("   ⚠️ Fichier trop long pour une correction auto sûre — à corriger à la main.\n")

        if files_with_bugs == 0:
            return f"✅ Aucun bug évident trouvé dans **{root.name}** ({len(files)} fichiers analysés)."

        footer = f"\n---\n**Bilan : {files_with_bugs} fichier(s) avec bugs"
        if fix:
            footer += f", {fixed} corrigé(s) automatiquement (originaux en .bak)"
        footer += f" sur {len(files)} analysés.**"
        if truncated:
            footer += f"\n⚠️ Projet volumineux : seuls les {_MAX_FILES} premiers fichiers ont été analysés."
        return "\n".join(report) + footer
