"""
Codeur : écrit du code en réponse à une tâche.

Utilise Mistral Medium (optimisé code/agentic, dialogue natif).
Deux instances peuvent travailler en parallèle sur la même tâche
pour diverger puis laisser le vérificateur arbitrer.
"""

from __future__ import annotations

import logging
import os
import re

import live
from tools.mistral_client import CallResult, chat_complete

logger = logging.getLogger("orchestrator.coder")

_MODEL = os.getenv("CODER_MODEL", "mistral-medium-latest")

_SYSTEM = """Tu es un codeur expert. Tu écris du code propre, idiomatique et
correct pour répondre à une tâche.

Règles :
- Retourne le code dans un bloc ```python (ou le langage demandé).
- Ajoute des commentaires uniquement pour expliquer un choix non évident.
- Inclue les imports nécessaires.
- Si la tâche est ambiguë, fais un choix raisonnable et note-le en commentaire.
- Ne retourne QUE le code (avec ses commentaires), pas d'explication hors bloc."""


def write_code(task: str, *, language: str = "python", context: str = "", label: str = "codeur") -> dict:
    """
    Écrit du code pour une tâche.

    Args:
        task: description de ce qu'il faut coder.
        language: langage de programmation attendu.
        context: contexte additionnel (recherche, décisions précédentes, ...).
        label: nom affiché (ex: "codeur_1", "codeur_2").

    Returns:
        {"code": str, "language": str, "raw": str, "tokens": int}
    """
    prompt = f"Tâche : {task}\n\nLangage : {language}"
    if context:
        prompt += f"\n\nContexte :\n{context}"

    live.agent_start(label, f"Écriture de code {language}", model=_MODEL)

    result = chat_complete(
        _MODEL,
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0.3,
    )

    code = _extract_code_block(result.content)
    lines = code.count("\n") + 1
    live.agent_done(
        label,
        f"{lines} lignes de code générées",
        result=result,
    )
    return {
        "code": code,
        "language": language,
        "raw": result.content,
        "tokens": result.total_tokens,
    }


def _extract_code_block(text: str) -> str:
    """Extrait le code d'un bloc markdown ```lang ... ```."""
    match = re.search(r"```(?:\w+)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Pas de bloc : on retourne tout (le modèle a peut-être oublié les backticks).
    return text.strip()
