"""
Moteur d'auto-amélioration — l'agent analyse ses propres outputs,
accumule des leçons et améliore ses prompts au fil du temps.
Stockage: data/self_improve.json
"""
import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_STORE = Path("data/self_improve.json")
_MAX_LESSONS = 40  # garde les 40 leçons les plus récentes


def _load() -> dict:
    if _STORE.exists():
        try:
            return json.loads(_STORE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"lessons": [], "stats": {"runs": 0, "avg_score": 0.0}}


def _save(data: dict):
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def evaluate_and_learn(task: str, result: str, domain: str = "general") -> dict:
    """
    Évalue la qualité d'un résultat et en tire une leçon.
    Appeler après chaque réponse importante de l'agent.
    """
    import asyncio
    import re
    from llm.client import chat

    EVAL_PROMPT = f"""Tu évalues la qualité d'une réponse d'agent IA.

Tâche originale: {task[:300]}

Résultat produit: {result[:600]}

Réponds en JSON:
{{
  "score": 7,
  "strengths": "Ce qui est bien dans cette réponse",
  "weaknesses": "Ce qui manque ou pourrait être amélioré",
  "lesson": "Une règle courte que l'agent doit retenir pour faire mieux la prochaine fois",
  "domain": "{domain}"
}}

Score: 1-10 (10 = parfait, 1 = inutile). Sois honnête et critique."""

    try:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, lambda: chat(
            [
                {"role": "system", "content": "Tu évalues objectivement des réponses IA. JSON uniquement."},
                {"role": "user", "content": EVAL_PROMPT},
            ],
            temperature=0.3,
        ))
        import re
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            logger.warning("[SelfImprove] pas de JSON dans la réponse LLM")
            return {}
        evaluation = json.loads(m.group())
    except Exception as e:
        logger.warning(f"[SelfImprove] évaluation échouée: {e}")
        return {}

    # Stocke la leçon
    data = _load()
    lesson = {
        "timestamp": datetime.now().isoformat(),
        "domain": domain,
        "score": evaluation.get("score", 5),
        "lesson": evaluation.get("lesson", ""),
        "weaknesses": evaluation.get("weaknesses", ""),
    }
    data["lessons"].append(lesson)
    data["lessons"] = data["lessons"][-_MAX_LESSONS:]  # garde les plus récentes

    runs = data["stats"]["runs"] + 1
    prev_avg = data["stats"]["avg_score"]
    data["stats"]["runs"] = runs
    data["stats"]["avg_score"] = round((prev_avg * (runs - 1) + lesson["score"]) / runs, 2)

    _save(data)
    logger.info(f"[SelfImprove] Score {lesson['score']}/10 — leçon: {lesson['lesson'][:60]}")
    return evaluation


def get_improvement_context(domain: str = "general", max_lessons: int = 5) -> str:
    """
    Retourne les leçons apprises pertinentes à injecter dans un prompt.
    """
    data = _load()
    lessons = data.get("lessons", [])

    # Filtre par domaine puis prend les plus récentes
    relevant = [l for l in lessons if l.get("domain") == domain][-max_lessons:]
    if not relevant:
        relevant = lessons[-max_lessons:]  # fallback: leçons générales

    if not relevant:
        return ""

    lines = ["[Leçons apprises de mes expériences précédentes]"]
    for l in relevant:
        if l.get("lesson"):
            lines.append(f"• {l['lesson']}")

    return "\n".join(lines)


def get_stats() -> dict:
    data = _load()
    return data.get("stats", {})


def get_recent_lessons(n: int = 10) -> list:
    data = _load()
    return data.get("lessons", [])[-n:]
