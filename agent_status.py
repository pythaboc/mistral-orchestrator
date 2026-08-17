"""
Statut temps réel des agents.

Permet de savoir quel agent travaille MAINTENANT (pas juste les tokens consommés).
Les agents appellent set_busy() au début d'un travail et set_idle() à la fin.
L'interface web interroge /api/agents pour afficher le statut en direct.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

_lock = threading.Lock()

# Statut de chaque agent : {nom: {"busy": bool, "task": str, "since": float}}
_status: dict[str, dict] = {}

# Définition des agents (nom, modèle, rôle)
AGENTS_DEF = [
    {"name": "orchestrateur", "model": "mistral-large-latest", "role": "Coordinateur"},
    {"name": "chercheur", "model": "mistral-medium-latest", "role": "Recherche web"},
    {"name": "codeur_1", "model": "mistral-medium-latest", "role": "Codeur"},
    {"name": "codeur_2", "model": "mistral-medium-latest", "role": "Codeur"},
    {"name": "verificateur", "model": "mistral-medium-latest", "role": "Vérificateur"},
    {"name": "veilleur", "model": "mistral-small-latest", "role": "Surveillance tokens"},
    {"name": "scribe", "model": "mistral-small-latest", "role": "Mémoire"},
]


def set_busy(agent: str, task: str = "") -> None:
    """Marque un agent comme occupé (en train de travailler)."""
    with _lock:
        _status[agent] = {"busy": True, "task": task, "since": time.time()}


def set_idle(agent: str) -> None:
    """Marque un agent comme inactif."""
    with _lock:
        if agent in _status:
            _status[agent]["busy"] = False


def get_all_status() -> list[dict]:
    """Retourne le statut de tous les agents (pour l'API web)."""
    with _lock:
        result = []
        for a in AGENTS_DEF:
            name = a["name"]
            st = _status.get(name, {"busy": False, "task": "", "since": 0})
            result.append({
                "name": name,
                "model": a["model"],
                "role": a["role"],
                "busy": st["busy"],
                "task": st.get("task", ""),
                "since": st.get("since", 0),
                "duration": round(time.time() - st.get("since", time.time()), 1) if st["busy"] else 0,
            })
        return result


def get_busy_agents() -> list[str]:
    """Retourne la liste des agents actuellement occupés."""
    with _lock:
        return [name for name, st in _status.items() if st.get("busy")]
