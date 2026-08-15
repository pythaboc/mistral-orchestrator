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

from typing import Any

from tools.mistral_client import CallResult

# Codes couleur ANSI (désactivables si le terminal ne supporte pas)
_COLORS = {
    "orchestrateur": "\033[95m",   # magenta
    "chercheur": "\033[94m",       # bleu
    "codeur": "\033[92m",          # vert
    "verificateur": "\033[93m",    # jaune
    "veilleur": "\033[91m",        # rouge
    "scribe": "\033[96m",          # cyan
    "reset": "\033[0m",
}


def _color(agent: str) -> str:
    return _COLORS.get(agent, _COLORS["orchestrateur"])


def agent_start(agent: str, action: str, *, model: str = "") -> None:
    """Affiche qu'un agent commence une action."""
    c = _color(agent)
    model_str = f" [{model}]" if model else ""
    print(f"\n{c}▶ {agent.title()}{model_str}{_COLORS['reset']}")
    print(f"{c}  → {action}{_COLORS['reset']}")


def agent_done(agent: str, summary: str, result: CallResult | None = None) -> None:
    """Affiche qu'un agent a fini, avec un résumé et les tokens consommés."""
    c = _color(agent)
    # Tronque le résumé pour ne pas spammer
    if len(summary) > 300:
        summary = summary[:297] + "..."
    print(f"{c}  ✓ {summary}{_COLORS['reset']}")
    if result is not None:
        tokens = result.total_tokens
        detail = f"{result.prompt_tokens} in + {result.completion_tokens} out"
        cache = " (cache)" if result.from_cache else ""
        print(f"{c}    🔢 {tokens} tokens ({detail}){cache}{_COLORS['reset']}")


def agent_info(agent: str, message: str) -> None:
    """Affiche une info intermédiaire d'un agent."""
    c = _color(agent)
    print(f"{c}  · {message}{_COLORS['reset']}")


def agent_error(agent: str, error: str) -> None:
    """Affiche une erreur d'un agent."""
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
