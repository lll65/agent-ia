"""
Débuggeur de projet — analyse un dossier de code, trouve les bugs, peut corriger.

RAPIDE : les fichiers sont regroupés en quelques lots (1 appel IA par lot au lieu
d'un par fichier) → réponse en ~30-60 s au lieu de 10 min. Le rapport est aussi
écrit dans output/rapport_debug.md (au cas où l'affichage web est lent).
"""
from pathlib import Path
from plugins.base import Plugin

_CODE_EXT = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss",
    ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb", ".php",
    ".sql", ".sh", ".vue", ".svelte", ".kt", ".swift",
}
_SKIP_DIRS = {
    "node_modules", ".git", "venv", ".venv", "env", "__pycache__", "dist",
    "build", ".next", ".nuxt", ".idea", ".vscode", "target", "vendor",
    ".mypy_cache", ".pytest_cache", "coverage", ".cache", "bin", "obj",
}
_MAX_FILES     = 20      # plafond de fichiers
_MAX_FILE_CHAR = 4000    # taille max lue par fichier
_BATCH_CHAR    = 8000    # taille max d'un lot envoyé au LLM
_MAX_BATCHES   = 4       # plafond d'appels IA pour l'analyse
_MAX_FIX       = 6       # plafond de fichiers corrigés (mode fix)


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
            if p.stat().st_size > 200_000:
                continue
        except Exception:
            continue
        files.append(p)
    return files


class ProjectDebuggerPlugin(Plugin):
    name = "analyze_project"
    description = (
        "Analyse un projet de code entier (un dossier), trouve les vrais bugs et peut "
        "les corriger. Paramètres: path (dossier), fix (true pour corriger, false pour lister)."
    )
    parameters = {
        "path": {"type": "string", "description": "Chemin du dossier du projet", "required": True},
        "fix":  {"type": "boolean", "description": "true = corrige (sauvegarde .bak), false = liste", "required": False},
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

        # Lecture + découpage en lots
        contents: dict[str, str] = {}
        for f in files:
            try:
                c = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if c.strip():
                rel = str(f.relative_to(root)) if root.is_dir() else f.name
                contents[rel] = c[:_MAX_FILE_CHAR]

        batches: list[list[str]] = []
        cur, cur_len = [], 0
        for rel, c in contents.items():
            block = len(c) + len(rel) + 20
            if cur and cur_len + block > _BATCH_CHAR:
                batches.append(cur); cur, cur_len = [], 0
            cur.append(rel); cur_len += block
        if cur:
            batches.append(cur)
        batches = batches[:_MAX_BATCHES]

        # Analyse : 1 appel IA par lot
        report = [f"# 🔍 Analyse — {root.name} ({len(contents)} fichiers, {len(batches)} lot(s))\n"]
        files_with_bugs = 0
        buggy_files: list[str] = []

        for batch in batches:
            joined = "\n\n".join(f"### FICHIER: {rel}\n```\n{contents[rel]}\n```" for rel in batch)
            prompt = (
                f"{joined}\n\n"
                "Tu es un ingénieur senior. Pour CHAQUE fichier ci-dessus, liste UNIQUEMENT les "
                "VRAIS bugs (logique fausse, syntaxe, exception non gérée, sécurité, variable non définie). "
                "Format par fichier :\n"
                "### <nom du fichier>\n- ligne ~N : problème → correction\n"
                "Si un fichier n'a aucun bug, écris '### <nom>\\nRAS'. Sois concis."
            )
            try:
                res = chat([{"role": "user", "content": prompt}], temperature=0.1)
            except Exception as e:
                report.append(f"⚠️ Lot non analysé ({str(e)[:70]})\n")
                continue
            report.append(res.strip() + "\n")
            # repère les fichiers avec bugs
            for rel in batch:
                seg = res.split(rel, 1)[-1][:400].lower() if rel in res else ""
                if seg and "ras" not in seg[:15]:
                    files_with_bugs += 1
                    buggy_files.append(rel)

        # Mode correction (per-fichier, seulement les fichiers signalés, plafonné)
        fixed = 0
        if fix and buggy_files:
            import re
            report.append("\n---\n## 🔧 Corrections\n")
            for rel in buggy_files[:_MAX_FIX]:
                f = (root / rel) if root.is_dir() else root
                try:
                    full = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                if len(full) > _MAX_FILE_CHAR:
                    report.append(f"- `{rel}` : trop long pour correction auto sûre — à la main.\n")
                    continue
                fix_prompt = (
                    f"Corrige les bugs de ce fichier `{f.name}` :\n```\n{full}\n```\n\n"
                    "Renvoie le fichier COMPLET corrigé dans UN SEUL bloc ``` … ```. "
                    "Garde tout ce qui marche. Aucun texte hors du bloc."
                )
                try:
                    raw = chat([{"role": "user", "content": fix_prompt}], temperature=0.1)
                    m = re.search(r"```[a-zA-Z0-9]*\n(.*?)```", raw, re.DOTALL)
                    new_code = (m.group(1) if m else raw).strip() + "\n"
                    if len(new_code) >= 30:
                        f.with_suffix(f.suffix + ".bak").write_text(full, encoding="utf-8")
                        f.write_text(new_code, encoding="utf-8")
                        fixed += 1
                        report.append(f"- ✅ `{rel}` corrigé (original en .bak)\n")
                except Exception as e:
                    report.append(f"- ⚠️ `{rel}` échec correction ({str(e)[:60]})\n")

        footer = f"\n---\n**Bilan : {files_with_bugs} fichier(s) avec bugs"
        if fix:
            footer += f", {fixed} corrigé(s)"
        footer += f" sur {len(contents)} analysés.**"
        if truncated:
            footer += f"\n⚠️ Projet volumineux : seuls {_MAX_FILES} fichiers analysés (pointe un sous-dossier pour le reste)."
        result = "\n".join(report) + footer

        # Sauvegarde du rapport (au cas où l'affichage web est lent/bloqué)
        try:
            from config import config
            out = Path(config.OUTPUT_DIR) / "rapport_debug.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(result, encoding="utf-8")
            result += f"\n\n*(Rapport aussi sauvegardé dans {out})*"
        except Exception:
            pass
        return result
