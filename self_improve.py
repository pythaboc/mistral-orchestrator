"""
Module d'auto-amélioration de l'équipe d'agents.

Trois niveaux :
1. Prompts auto : l'orchestrateur réécrit les prompts des agents en s'appuyant
   sur les retours enregistrés par le scribe dans journal.md.
2. Préférences : l'orchestrateur apprend les préférences de l'utilisateur
   (style de code, habitudes) et les injecte dans les briefs.
3. Méta-réflexion : post-mortem après chaque tâche (auto-critique de
   l'orchestrateur sur son plan, ses choix, sa stratégie).

Les prompts sont stockés dans prompts/ (versionnés git) et lus par les agents
au lieu d'être hardcodés. Les préférences sont dans preferences.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime

import live
from tools.mistral_client import chat_complete

logger = logging.getLogger("orchestrator.self_improve")

_ORCHESTRATOR_MODEL = os.getenv("ORCHESTRATOR_MODEL", "mistral-large-latest")

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
_PREFERENCES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preferences.md")
_FEEDBACK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback.json")


def load_prompt(name: str, fallback: str = "") -> str:
    """Charge un prompt depuis prompts/<name>.txt, avec fallback si absent."""
    path = os.path.join(_PROMPTS_DIR, f"{name}.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.debug("Prompt %s non trouvé, utilisation du fallback", name)
        return fallback


def load_preferences() -> str:
    """Retourne le contenu de preferences.md (ou vide si inexistant)."""
    try:
        with open(_PREFERENCES_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def learn_preferences(user_input: str, task_result: dict | None = None) -> str | None:
    """
    L'orchestrateur analyse le message de l'utilisateur et le résultat de la
    tâche pour détecter de nouvelles préférences (style de code, habitudes).
    """
    current_prefs = load_preferences()
    code_produced = ""
    if task_result and "final_code" in task_result:
        code_produced = task_result["final_code"][:2000]

    prompt = f"""Analyse ce message d'utilisateur et (optionnellement) le code produit,
pour détecter des préférences de style ou des habitudes de développement.

MESSAGE UTILISATEUR :
{user_input[:1000]}

CODE PRODUIT (extrait) :
{code_produced[:1500]}

PRÉFÉRENCES DÉJÀ CONNUES :
{current_prefs or "(aucune pour l'instant)"}

Détecte de NOUVELLES préférences (pas celles déjà connues). Exemples :
- "Préfère les type hints"
- "Veut des docstrings sur chaque fonction"
- "N'aime pas les commentaires évidents"
- "Utilise toujours des dataclasses"

Si tu ne détectes rien de nouveau, réponds exactement : RIEN
Sinon, liste les nouvelles préférences (une par ligne, sans numéro)."""

    result = chat_complete(
        _ORCHESTRATOR_MODEL,
        [{"role": "user", "content": prompt}],
        temperature=0.1,
    )

    if "RIEN" in result.content.upper().strip()[:10]:
        return None

    new_prefs = result.content.strip()
    if not new_prefs:
        return None

    header = ""
    if not os.path.exists(_PREFERENCES_PATH):
        header = "# Préférences de l'utilisateur\n\n_Apprises automatiquement par l'orchestrateur._\n\n"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(_PREFERENCES_PATH, "a", encoding="utf-8") as f:
        f.write(f"{header}## [{timestamp}] Nouvelles préférences détectées\n\n{new_prefs}\n\n")

    live.agent_info("orchestrateur", f"Plusieurs préférences apprises")
    return new_prefs


def get_preferences_for_brief() -> str:
    """Retourne les préférences formatées pour injection dans un brief de codeur."""
    prefs = load_preferences()
    if not prefs:
        return ""
    return f"\n\nPRÉFÉRENCES DE L'UTILISATEUR (respecte-les) :\n{prefs}"


_IMPROVABLE_PROMPTS = {
    "orchestrator": "orchestrator",
    "coder": "coder",
    "verifier": "verifier",
    "scribe": "scribe",
}


def improve_prompts(journal_content: str) -> dict:
    """
    L'orchestrateur analyse le journal pour trouver des points d'amélioration
    et réécrit les prompts concernés (niveau 1).
    """
    live.agent_start("orchestrateur", "Auto-amélioration des prompts (niveau 1)", model=_ORCHESTRATOR_MODEL)

    current_prompts = {}
    for name in _IMPROVABLE_PROMPTS:
        current_prompts[name] = load_prompt(name)

    feedbacks = load_feedback()
    feedback_str = json.dumps(feedbacks[-10:], ensure_ascii=False, indent=2) if feedbacks else "(aucun)"

    prompt = f"""Tu es l'orchestrateur. Tu vas améliorer les prompts de ton équipe
en t'appuyant sur le journal des sessions précédentes et les feedbacks.

JOURNAL DU PROJET (extrait) :
{journal_content[:3000]}

FEEDBACKS RÉCENTS :
{feedback_str}

PROMPTS ACTUELS :
{json.dumps(current_prompts, ensure_ascii=False, indent=2)}

Analyse pour identifier :
1. Des problèmes récurrents (ex: vérificateur rate des bugs, codeur trop verbeux)
2. Des retours négatifs de l'utilisateur
3. Des inefficacités (ex: recherche inutile, plan mauvais)

Pour chaque prompt que tu penses devoir améliorer, réécris-le.
Ne réécris un prompt QUE si tu as une amélioration concrète à apporter.

Réponds en JSON :
{{
  "improvements": {{
    "coder": "nouveau prompt OU null",
    "verifier": "nouveau prompt OU null",
    "orchestrator": "nouveau prompt OU null",
    "scribe": "nouveau prompt OU null"
  }},
  "summary": "ce que tu as changé et pourquoi (2-3 lignes)"
}}"""

    result = chat_complete(
        _ORCHESTRATOR_MODEL,
        [{"role": "user", "content": prompt}],
        temperature=0.2,
    )

    parsed = _parse_json(result.content)
    improvements = parsed.get("improvements", {})
    summary = parsed.get("summary", "Pas d'amélioration")

    improved = {}
    for name, new_prompt in improvements.items():
        if new_prompt and new_prompt.lower() != "null" and name in _IMPROVABLE_PROMPTS:
            _save_prompt(name, new_prompt)
            improved[name] = True
            live.agent_info("orchestrateur", f"Prompt '{name}' amélioré")
        else:
            improved[name] = False

    if any(improved.values()):
        live.agent_done("orchestrateur", f"{sum(improved.values())} prompt(s) amélioré(s)", result=result)
    else:
        live.agent_done("orchestrateur", "Aucune amélioration nécessaire", result=result)

    # Récupère les anciens prompts pour le diff
    old_prompts = {}
    for name in _IMPROVABLE_PROMPTS:
        old_prompts[name] = current_prompts.get(name, "")

    return {
        "improved": improved,
        "summary": summary,
        "old_prompts": old_prompts,
        "changes": {name: improved.get(name, False) for name in _IMPROVABLE_PROMPTS},
    }


def _save_prompt(name: str, content: str) -> None:
    """Sauvegarde un prompt dans prompts/<name>.txt."""
    path = os.path.join(_PROMPTS_DIR, f"{name}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.strip() + "\n")
    logger.info("Prompt %s sauvegardé", name)


@dataclass
class PostMortem:
    """Résultat d'un post-mortem après une tâche."""
    plan_quality: str
    choice_quality: str
    verifier_quality: str
    strategy_feedback: str
    lessons: str
    tokens: int = 0


def post_mortem(task: str, plan: str, result: dict) -> PostMortem:
    """
    L'orchestrateur fait une auto-critique après une tâche (niveau 3).
    """
    live.agent_start("orchestrateur", "Méta-réflexion (post-mortem)", model=_ORCHESTRATOR_MODEL)

    verification = result.get("verification", {})
    verdict = verification.get("verdict", "?")
    issues = verification.get("issues", [])
    iterations = result.get("iterations", 0)
    final_code = result.get("final_code", "")[:1500]

    prompt = f"""Tu es l'orchestrateur. Fais un post-mortem honnête de cette tâche.

TÂCHE : {task[:500]}

PLAN ÉLABORÉ : {plan[:500]}

RÉSULTAT :
- Verdict final : {verdict}
- Itérations : {iterations}
- Problèmes trouvés : {len(issues)}
- Code produit (extrait) :
{final_code}

Auto-évalue-toi honnêtement. Réponds en JSON :
{{
  "plan_quality": "bon/mauvais/moyen + pourquoi",
  "choice_quality": "le bon candidat a-t-il été choisi ?",
  "verifier_quality": "le vérificateur a-t-il été efficace ? a-t-il raté quelque chose ?",
  "strategy_feedback": "faut-il changer de stratégie pour ce type de tâche ?",
  "lessons": "1-2 leçons à retenir pour les prochaines tâches"
}}

Sois critique et honnête. Si quelque chose a mal fonctionné, dis-le."""

    result_api = chat_complete(
        _ORCHESTRATOR_MODEL,
        [{"role": "user", "content": prompt}],
        temperature=0.2,
    )

    parsed = _parse_json(result_api.content)

    pm = PostMortem(
        plan_quality=parsed.get("plan_quality", "?"),
        choice_quality=parsed.get("choice_quality", "?"),
        verifier_quality=parsed.get("verifier_quality", "?"),
        strategy_feedback=parsed.get("strategy_feedback", "?"),
        lessons=parsed.get("lessons", "?"),
        tokens=result_api.total_tokens,
    )

    live.agent_done("orchestrateur", f"Post-mortem terminé", result=result_api)
    live.agent_thinking("orchestrateur", f"Leçons : {pm.lessons}")

    return pm


def post_mortem_to_dict(pm: PostMortem) -> dict:
    return {
        "plan_quality": pm.plan_quality,
        "choice_quality": pm.choice_quality,
        "verifier_quality": pm.verifier_quality,
        "strategy_feedback": pm.strategy_feedback,
        "lessons": pm.lessons,
        "tokens": pm.tokens,
    }


def record_feedback(task: str, feedback_type: str, detail: str) -> None:
    """
    Enregistre un feedback (positif ou négatif) sur une tâche.
    Sert de matière première pour improve_prompts().
    """
    feedbacks = []
    try:
        with open(_FEEDBACK_PATH, "r", encoding="utf-8") as f:
            feedbacks = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    feedbacks.append({
        "timestamp": datetime.now().isoformat(),
        "task": task[:200],
        "type": feedback_type,
        "detail": detail[:500],
    })

    with open(_FEEDBACK_PATH, "w", encoding="utf-8") as f:
        json.dump(feedbacks, f, ensure_ascii=False, indent=2)

    logger.info("Feedback enregistré : %s", feedback_type)


def load_feedback() -> list:
    """Retourne la liste des feedbacks enregistrés."""
    try:
        with open(_FEEDBACK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _parse_json(text: str) -> dict:
    """Extrait et parse un JSON (tolérant au texte autour)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.warning("JSON non trouvé. Extrait: %s", text[:200])
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        try:
            return json.loads(match.group(0).strip().strip("`"))
        except json.JSONDecodeError:
            logger.warning("JSON invalide. Extrait: %s", text[:200])
            return {}
