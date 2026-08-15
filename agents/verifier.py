"""
Vérificateur : relit et challenge le code produit par les codeurs.

Rôle clé : il ne cherche PAS à confirmer que le code est bon.
Il cherche activement les bugs, failles de sécurité, edge cases,
et erreurs de logique. C'est le "deuxième avis qui essaie de te donner tort"
(le principe de @powl_d).

Retourne un verdict structuré (JSON) : OK / WARN / FAIL + liste des problèmes.
"""

from __future__ import annotations

import json
import logging
import os
import re

from monitoring.metrics import estimate_tokens, record_tokens
from tools.mistral_client import simple_prompt

logger = logging.getLogger("orchestrator.verifier")

_MODEL = os.getenv("VERIFIER_MODEL", "mistral-medium-latest")

_SYSTEM = """Tu es un vérificateur de code rigoureux. Ton but N'EST PAS de
confirmer que le code est correct, mais de chercher activement les bugs,
failles de sécurité, edge cases non gérés, erreurs de logique et mauvaises
pratiques.

Pour chaque vérification, retourne UNIQUEMENT un objet JSON valide (sans
texte autour, sans markdown) avec ce schéma :

{
  "verdict": "OK" | "WARN" | "FAIL",
  "issues": [
    {
      "severity": "critical" | "high" | "medium" | "low",
      "category": "security" | "bug" | "logic" | "quality",
      "description": "problème trouvé",
      "suggestion": "correction proposée"
    }
  ],
  "summary": "résumé en 1 phrase"
}

Si le code est réellement correct, retourne {"verdict":"OK","issues":[],"summary":"..."}.
Ne sois JAMAIS complaisant : si tu vois un risque potentiel, signale-le."""


def verify_code(code: str, *, task: str = "", language: str = "python") -> dict:
    """
    Vérifie un bloc de code en cherchant activement les problèmes.

    Args:
        code: le code à vérifier.
        task: la tâche que le code était censé résoudre (pour contexte).
        language: langage du code.

    Returns:
        Dictionnaire parsé : {verdict, issues, summary}.
    """
    prompt = f"""Vérifie ce code {language} de manière critique."""
    if task:
        prompt += f"\nTâche que le code devait résoudre : {task}"
    prompt += f"\n\n```{language}\n{code}\n```"

    raw = simple_prompt(prompt, model=_MODEL, system=_SYSTEM, temperature=0.1)
    record_tokens(_MODEL, estimate_tokens(prompt), estimate_tokens(raw))

    return _parse_json(raw)


def _parse_json(text: str) -> dict:
    """Extrait et parse le JSON depuis la réponse (tolérant au texte autour)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.warning("Vérificateur : réponse non-JSON. Extrait: %s", text[:200])
        return {
            "verdict": "WARN",
            "issues": [],
            "summary": f"Réponse non-JSON : {text[:300]}",
            "raw": text,
        }
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        logger.warning("Vérificateur : JSON invalide : %s", exc)
        return {
            "verdict": "WARN",
            "issues": [],
            "summary": f"JSON invalide : {exc}",
            "raw": text,
        }
