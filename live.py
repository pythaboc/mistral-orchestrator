"""
Affichage en direct (live logging) du travail des agents.

Permet de voir, pendant l'exécution :
- quel agent travaille
- ce qu'il produit (extrait)
- combien de tokens il a consommé (vrais compteurs de l'API)

Utilisé par l'orchestrateur pour rendre le tout visible au lieu d'être
une "boîte noire".
"""

from __future__ import annotations

import textwrap
from typing import Any

from agent_status import set_busy, set_idle
from tools.mistral_client import CallResult

# Codes couleur ANSI (désactivables si le terminal ne supporte pas)
_COLORS = {
    "orchestrateur": "\033[95m",   # magenta
    "chercheur": "\033[94m",       # bleu
    "codeur": "\033[92m",          # vert
    "codeur_1": "\033[92m",         # vert
    "codeur_2": "\033[92m",         # vert
    "verificateur": "\033[93m",     # jaune
    "veilleur": "\033[91m",        # rouge
    "scribe": "\033[96m",          # cyan
    "reset": "\033[0m",
}

# Largeur utile du terminal (onWrapping à cette largeur)
_TERM_WIDTH = 120


def _color(agent: str) -> str:
    return _COLORS.get(agent, _COLORS["orchestrateur"])


def _wrap(text: str, indent: str = "    ", width: int = _TERM_WIDTH) -> str:
    """Découpe un texte long sur plusieurs lignes avec indentation."""
    if not text:
        return ""
    wrapped = textwrap.fill(
        text,
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return wrapped


def agent_start(agent: str, action: str, *, model: str = "") -> None:
    """Affiche qu'un agent commence une action + met à jour le statut temps réel."""
    set_busy(agent, action)
    c = _color(agent)
    model_str = f" [{model}]" if model else ""
    print(f"\n{c}▶ {agent.title()}{model_str}{_COLORS['reset']}")
    print(f"{c}  → {action}{_COLORS['reset']}")


def agent_done(agent: str, summary: str, result: CallResult | None = None) -> None:
    """Affiche qu'un agent a fini + met à jour le statut temps réel."""
    set_idle(agent)
    c = _color(agent)
    # On wrap le résumé s'il est long (au lieu de tronquer brutalement)
    wrapped = _wrap(summary, indent="  ✓ ")
    print(f"{c}{wrapped}{_COLORS['reset']}")
    if result is not None:
        tokens = result.total_tokens
        detail = f"{result.prompt_tokens} in + {result.completion_tokens} out"
        cache = " (cache)" if result.from_cache else ""
        print(f"{c}    🔢 {tokens} tokens ({detail}){cache}{_COLORS['reset']}")


def agent_info(agent: str, message: str) -> None:
    """Affiche une info intermédiaire d'un agent (avec wrapping)."""
    c = _color(agent)
    wrapped = _wrap(message, indent="  · ")
    print(f"{c}{wrapped}{_COLORS['reset']}")


def agent_thinking(agent: str, reasoning: str) -> None:
    """Affiche le raisonnement d'un agent (orchestrateur qui réfléchit)."""
    c = _color(agent)
    print(f"{c}  💭 {reasoning}{_COLORS['reset']}")


def agent_error(agent: str, error: str) -> None:
    """Affiche une erreur d'un agent + libère le statut."""
    set_idle(agent)
    c = _color(agent)
    print(f"{c}  ✗ ERREUR : {error}{_COLORS['reset']}")


def section(title: str) -> None:
    """Affiche un séparateur de section."""
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def task_summary(tokens_by_agent: dict[str, int], total_tokens: int) -> None:
    """Affiche un récapitulatif des tokens par agent à la fin d'une tâche."""
    section("📊 Récapitulatif des tokens")
    for agent, tokens in sorted(tokens_by_agent.items(), key=lambda x: -x[1]):
        c = _color(agent)
        pct = (tokens / total_tokens * 100) if total_tokens else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {c}{agent:<16}{_COLORS['reset']} {bar} {tokens:>6} ({pct:4.1f}%)")
    print(f"\n  {'Total':<16} {'':<22} {total_tokens:>6} tokens")
