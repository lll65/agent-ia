"""
Analyseur santé — lit un export de données santé/sport/habitudes (JSON ou CSV,
ex: depuis l'app Vita) et joue le rôle d'un coach : tendances, corrélations,
points forts/faibles et recommandations personnalisées.

Aucune clé API requise si Vita peut exporter ses données en fichier.
⚠️ L'analyse envoie les données au modèle cloud (Groq) — pour des données très
sensibles, retire ton nom du fichier avant.
"""
import csv
import io
import json
from pathlib import Path
from plugins.base import Plugin

_MAX_CHARS = 9000   # taille max de données envoyées au LLM


def _load_data(path: Path) -> tuple[str, str]:
    """Retourne (texte_compact, type) prêt pour le LLM. type = json/csv/text."""
    ext = path.suffix.lower()
    raw = path.read_text(encoding="utf-8", errors="ignore")

    if ext == ".json":
        try:
            obj = json.loads(raw)
        except Exception:
            return raw[:_MAX_CHARS], "json (brut)"
        # Si c'est une liste d'entrées, garde un échantillon + le total
        if isinstance(obj, list):
            total = len(obj)
            sample = obj[:60]
            txt = json.dumps(sample, ensure_ascii=False, indent=1)[:_MAX_CHARS]
            return f"({total} entrées, échantillon des {len(sample)} premières)\n{txt}", "json"
        return json.dumps(obj, ensure_ascii=False, indent=1)[:_MAX_CHARS], "json"

    if ext == ".csv":
        try:
            rows = list(csv.reader(io.StringIO(raw)))
        except Exception:
            return raw[:_MAX_CHARS], "csv (brut)"
        if not rows:
            return "", "csv"
        header = rows[0]
        body = rows[1:]
        total = len(body)
        sample = body[:80]
        lines = [",".join(header)] + [",".join(r) for r in sample]
        return f"({total} lignes, colonnes: {', '.join(header)})\n" + "\n".join(lines)[:_MAX_CHARS], "csv"

    return raw[:_MAX_CHARS], "texte"


class HealthAnalyzerPlugin(Plugin):
    name = "analyze_health"
    description = (
        "Analyse des données de santé/sport/habitudes (export JSON ou CSV, ex: app Vita) "
        "et donne tendances + recommandations comme un coach. "
        "Paramètres: path (fichier de données), question (optionnel: ce sur quoi te concentrer)."
    )
    parameters = {
        "path":     {"type": "string", "description": "Chemin du fichier exporté (JSON ou CSV)", "required": True},
        "question": {"type": "string", "description": "Focus (ex: 'mon sommeil', 'progression sport') — optionnel", "required": False},
    }

    def run(self, path: str = "", question: str = "", **_) -> str:
        from llm.client import chat

        src = str(path).strip().strip('"').strip("'")

        # ── Cas 1 : URL (endpoint Vita en direct) ──────────────────────────
        if src.lower().startswith(("http://", "https://")):
            import requests, json as _json
            try:
                r = requests.get(src, timeout=25,
                                  headers={"User-Agent": "MasterAgent/1.0", "Accept": "application/json"})
            except Exception as e:
                return f"❌ Impossible de joindre l'endpoint Vita : {str(e)[:120]}"
            if r.status_code == 401:
                return "❌ Secret refusé (401) — vérifie le `key=` dans ton URL."
            if r.status_code == 501:
                return "❌ Endpoint désactivé (501) — la variable AGENT_SECRET n'est pas posée sur Vercel."
            if r.status_code != 200:
                return f"❌ L'endpoint Vita a répondu {r.status_code} : {r.text[:200]}"
            try:
                obj = r.json()
                data_txt = _json.dumps(obj, ensure_ascii=False, indent=1)[:_MAX_CHARS]
            except Exception:
                data_txt = r.text[:_MAX_CHARS]
            kind = "json (Vita en direct)"
            return self._analyze(chat, data_txt, kind, question, "Vita (temps réel)")

        # ── Cas 2 : fichier local ──────────────────────────────────────────
        p = Path(src).expanduser()
        if not p.exists():
            return f"❌ Fichier introuvable : {path}"
        if p.is_dir():
            return "❌ Donne un FICHIER exporté (JSON/CSV) ou une URL Vita, pas un dossier."

        try:
            data_txt, kind = _load_data(p)
        except Exception as e:
            return f"❌ Lecture impossible : {e}"
        if not data_txt.strip():
            return "❌ Le fichier semble vide."

        return self._analyze(chat, data_txt, kind, question, p.name)

    def _analyze(self, chat, data_txt: str, kind: str, question: str, source: str) -> str:
        if not data_txt or not data_txt.strip():
            return "❌ Aucune donnée à analyser."
        focus = (question or "").strip()
        focus_line = f"\nL'utilisateur veut surtout : « {focus} »." if focus else ""

        system = (
            "Tu es un coach santé, sport et bien-être expérimenté et bienveillant. "
            "Tu analyses des données personnelles et donnes des constats CHIFFRÉS et "
            "des conseils CONCRETS et réalistes. Tu ne poses pas de diagnostic médical "
            "et tu rappelles de consulter un médecin pour tout symptôme sérieux. Français."
        )
        user = (
            f"Voici mes données de santé/sport/habitudes (source : {source}, format {kind}) :\n"
            f"\"\"\"\n{data_txt}\n\"\"\"\n{focus_line}\n\n"
            "Analyse et structure ta réponse ainsi :\n"
            "## 📊 Ce que disent tes données (constats chiffrés)\n"
            "## 📈 Tendances & corrélations (ex: sport ↔ sommeil, régularité)\n"
            "## ✅ Points forts\n"
            "## ⚠️ Points à améliorer\n"
            "## 🎯 3 recommandations concrètes pour cette semaine\n"
            "Base-toi UNIQUEMENT sur les données fournies, n'invente pas de chiffres."
        )
        try:
            return chat([{"role": "system", "content": system},
                         {"role": "user", "content": user}], temperature=0.3)
        except Exception as e:
            return f"❌ Erreur analyse : {e}"
