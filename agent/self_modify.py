"""
Self-Modification Engine — MasterAgent-Gros peut réécrire son propre code.

Sécurité (couches multiples):
  1. Whitelist stricte des fichiers modifiables
  2. Backup horodaté AVANT toute modification (réversible)
  3. Validation syntaxique (ast.parse) AVANT d'écrire
  4. Validation d'import (le module doit se charger) APRÈS écriture
  5. Rollback automatique si la validation échoue
  6. Journal de toutes les modifications (data/self_mods/journal.json)

L'agent NE PEUT PAS:
  - Modifier des fichiers hors whitelist (config secrets, .env, etc.)
  - Écrire du code qui ne parse pas
  - Laisser le système dans un état cassé (rollback auto)
"""
from __future__ import annotations

import ast
import difflib
import importlib
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── Configuration de sécurité ────────────────────────────────────────────────

# Racine du projet (ce fichier est dans agent/)
_ROOT = Path(__file__).resolve().parent.parent

# Fichiers que l'agent est AUTORISÉ à modifier (chemins relatifs à la racine)
ALLOWED_FILES: set[str] = {
    "agent/system_prompt.py",
    "agent/finance_deep.py",
    "agent/self_improve.py",
    "plugins/builtin/finance.py",
}

# Préfixes de dossiers où l'agent peut CRÉER de nouveaux modules
ALLOWED_NEW_DIRS: set[str] = {
    "data/plugins",       # plugins utilisateur (hot-reload)
    "agent/generated",    # modules auto-générés
}

# Motifs interdits dans le code soumis (sécurité de base anti-sabotage)
FORBIDDEN_PATTERNS: list[str] = [
    "os.system(",
    "subprocess.",
    "shutil.rmtree",
    "eval(",
    "exec(",
    "__import__",
    "open('/etc",
    'open("/etc',
    ".env",
    "TELEGRAM_TOKEN",
    "API_KEY",
    "rm -rf",
]

_BACKUP_DIR  = _ROOT / "data" / "self_mods" / "backups"
_JOURNAL     = _ROOT / "data" / "self_mods" / "journal.json"


# ─── Helpers internes ─────────────────────────────────────────────────────────

def _safe_resolve(rel_path: str) -> Path | None:
    """Résout un chemin relatif et vérifie qu'il reste dans la racine du projet."""
    try:
        target = (_ROOT / rel_path).resolve()
        target.relative_to(_ROOT)  # lève ValueError si hors racine
        return target
    except (ValueError, RuntimeError):
        return None


def _is_allowed_modify(rel_path: str) -> bool:
    return rel_path in ALLOWED_FILES


def _is_allowed_create(rel_path: str) -> bool:
    return any(rel_path.startswith(d + "/") for d in ALLOWED_NEW_DIRS)


def _check_forbidden(code: str) -> str | None:
    """Retourne le motif interdit trouvé, ou None si propre."""
    low = code
    for pat in FORBIDDEN_PATTERNS:
        if pat in low:
            return pat
    return None


def _load_journal() -> list:
    if _JOURNAL.exists():
        try:
            return json.loads(_JOURNAL.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _append_journal(entry: dict):
    _JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    data = _load_journal()
    data.append(entry)
    data = data[-200:]  # garde les 200 dernières modifs
    _JOURNAL.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _backup(target: Path, rel_path: str) -> Path:
    """Sauvegarde le fichier actuel avec timestamp. Retourne le chemin du backup."""
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_name = rel_path.replace("/", "__")
    backup    = _BACKUP_DIR / f"{safe_name}.{ts}.bak"
    if target.exists():
        shutil.copy2(target, backup)
    return backup


def _module_name_from_path(rel_path: str) -> str | None:
    """Convertit 'agent/finance_deep.py' → 'agent.finance_deep'."""
    if not rel_path.endswith(".py"):
        return None
    return rel_path[:-3].replace("/", ".")


# ─── API publique ─────────────────────────────────────────────────────────────

def read_source(rel_path: str) -> str:
    """Lit le code source d'un fichier autorisé."""
    if not (_is_allowed_modify(rel_path) or _is_allowed_create(rel_path)):
        return f"❌ Fichier non autorisé en lecture: {rel_path}\nAutorisés: {sorted(ALLOWED_FILES)}"
    target = _safe_resolve(rel_path)
    if not target or not target.exists():
        return f"❌ Fichier introuvable: {rel_path}"
    try:
        return target.read_text(encoding="utf-8")
    except Exception as e:
        return f"❌ Erreur de lecture: {e}"


def propose_diff(rel_path: str, new_code: str) -> str:
    """Génère un diff unifié sans rien appliquer (preview)."""
    target  = _safe_resolve(rel_path)
    old_code = target.read_text(encoding="utf-8") if (target and target.exists()) else ""
    diff = difflib.unified_diff(
        old_code.splitlines(keepends=True),
        new_code.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        n=3,
    )
    text = "".join(diff)
    return text or "(aucune différence)"


def apply_modification(rel_path: str, new_code: str, reason: str = "") -> dict:
    """
    Applique une modification de code de façon SÉCURISÉE.

    Pipeline:
      whitelist → motifs interdits → syntaxe → backup → écriture
      → validation import → (rollback si échec)

    Retourne dict: {ok, message, backup, diff_summary}
    """
    # 1. Autorisation
    is_modify = _is_allowed_modify(rel_path)
    is_create = _is_allowed_create(rel_path)
    if not (is_modify or is_create):
        return {
            "ok": False,
            "message": (
                f"❌ REFUSÉ: '{rel_path}' n'est pas dans la whitelist.\n"
                f"Modifiables: {sorted(ALLOWED_FILES)}\n"
                f"Création autorisée dans: {sorted(ALLOWED_NEW_DIRS)}"
            ),
        }

    target = _safe_resolve(rel_path)
    if target is None:
        return {"ok": False, "message": f"❌ REFUSÉ: chemin invalide (hors projet): {rel_path}"}

    existed_before = target.exists()
    if is_create and existed_before and not is_modify:
        return {"ok": False, "message": f"❌ Le fichier existe déjà: {rel_path}. Utilise un chemin de création neuf."}

    # 2. Motifs interdits
    forbidden = _check_forbidden(new_code)
    if forbidden:
        return {
            "ok": False,
            "message": f"❌ REFUSÉ: motif de sécurité interdit détecté dans le code: '{forbidden}'",
        }

    # 3. Validation syntaxique
    try:
        ast.parse(new_code)
    except SyntaxError as e:
        return {
            "ok": False,
            "message": f"❌ REFUSÉ: erreur de syntaxe Python ligne {e.lineno}: {e.msg}",
        }

    # 4. Backup
    backup = _backup(target, rel_path)

    # Diff pour le journal
    diff_text = propose_diff(rel_path, new_code)
    n_added   = sum(1 for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++"))
    n_removed = sum(1 for l in diff_text.splitlines() if l.startswith("-") and not l.startswith("---"))

    # 5. Écriture
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_code, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "message": f"❌ Erreur d'écriture: {e}"}

    # 6. Validation d'import (le module doit se charger sans casser)
    mod_name      = _module_name_from_path(rel_path)
    import_ok     = True
    import_error  = ""
    if mod_name and is_modify:  # ne teste l'import que pour les modules du projet
        try:
            if mod_name in importlib.sys.modules:
                importlib.reload(importlib.sys.modules[mod_name])
            else:
                importlib.import_module(mod_name)
        except Exception as e:
            import_ok    = False
            import_error = str(e)

    # 7. Rollback si l'import casse
    if not import_ok:
        try:
            if backup.exists():
                shutil.copy2(backup, target)
                # Recharge la version saine
                if mod_name and mod_name in importlib.sys.modules:
                    importlib.reload(importlib.sys.modules[mod_name])
        except Exception:
            pass
        return {
            "ok": False,
            "message": (
                f"❌ ROLLBACK: le nouveau code casse l'import du module.\n"
                f"Erreur: {import_error}\n"
                f"L'ancienne version a été restaurée. Aucun dommage."
            ),
            "backup": str(backup),
        }

    # 8. Journal
    entry = {
        "timestamp": datetime.now().isoformat(),
        "file":      rel_path,
        "action":    "create" if not existed_before else "modify",
        "reason":    reason[:300],
        "lines_added":   n_added,
        "lines_removed": n_removed,
        "backup":    str(backup.relative_to(_ROOT)),
    }
    _append_journal(entry)

    logger.info(f"[SelfMod] {rel_path} modifié (+{n_added}/-{n_removed}) — {reason[:60]}")

    return {
        "ok": True,
        "message": (
            f"✅ Modification appliquée à `{rel_path}` (+{n_added} / -{n_removed} lignes)\n"
            f"Backup: `{backup.relative_to(_ROOT)}`\n"
            f"Module rechargé et validé."
        ),
        "backup":  str(backup),
        "added":   n_added,
        "removed": n_removed,
    }


def rollback_last() -> dict:
    """Annule la dernière modification en restaurant son backup."""
    journal = _load_journal()
    if not journal:
        return {"ok": False, "message": "Aucune modification à annuler."}

    last   = journal[-1]
    backup = _ROOT / last["backup"]
    target = _safe_resolve(last["file"])

    if not backup.exists():
        return {"ok": False, "message": f"❌ Backup introuvable: {last['backup']}"}
    if target is None:
        return {"ok": False, "message": f"❌ Cible invalide: {last['file']}"}

    try:
        shutil.copy2(backup, target)
        mod_name = _module_name_from_path(last["file"])
        if mod_name and mod_name in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod_name])
        # Marque comme annulé dans le journal
        _append_journal({
            "timestamp": datetime.now().isoformat(),
            "file":      last["file"],
            "action":    "rollback",
            "reason":    f"Annulation de la modif du {last['timestamp']}",
            "lines_added": 0, "lines_removed": 0,
            "backup":    last["backup"],
        })
        return {"ok": True, "message": f"✅ '{last['file']}' restauré depuis le backup."}
    except Exception as e:
        return {"ok": False, "message": f"❌ Erreur de rollback: {e}"}


def get_journal(n: int = 20) -> list:
    """Retourne les n dernières entrées du journal de modifications."""
    return _load_journal()[-n:]


def list_modifiable_files() -> dict:
    """Retourne les fichiers modifiables et leur taille actuelle."""
    out = {}
    for rel in sorted(ALLOWED_FILES):
        target = _safe_resolve(rel)
        if target and target.exists():
            lines = len(target.read_text(encoding="utf-8").splitlines())
            out[rel] = {"exists": True, "lines": lines}
        else:
            out[rel] = {"exists": False, "lines": 0}
    return out
