"""
Veilleur : surveille drastiquement la consommation de tokens.

Deux niveaux de surveillance :
1. Comptage dur en Python (compteurs, seuils) : fiable, zéro coût.
2. Agent IA Small qui analyse périodiquement les tendances et propose des
   optimisations (ex: un agent qui boucle, un prompt trop répétitif).

Le veilleur peut :
- alerter quand un seuil est franchi (ALERT_THRESHOLD_PCT du budget).
- bloquer (lever une exception) quand le budget MAX_TOKENS_PER_SESSION est dépassé.
- détecter les boucles (un même agent appelé > MAX_AGENT_CALLS fois).
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field

from monitoring.metrics import estimate_tokens, record_tokens
from tools.mistral_client import simple_prompt

logger = logging.getLogger("orchestrator.watcher")


class BudgetExceeded(Exception):
    """Levé quand le budget de tokens de la session est dépassé (bloquant)."""


@dataclass
class Watcher:
    """
    Veilleur de consommation.

    Usage :
        watcher = Watcher()
        watcher.track_call("codeur_1", input_tokens=500, output_tokens=200)
        watcher.check_budget()  # lève BudgetExceeded si dépassé
        report = watcher.analyze()  # analyse IA Small des tendances
    """

    max_tokens: int = field(default_factory=lambda: int(os.getenv("MAX_TOKENS_PER_SESSION", "200000")))
    alert_pct: int = field(default_factory=lambda: int(os.getenv("ALERT_THRESHOLD_PCT", "70")))
    max_agent_calls: int = field(default_factory=lambda: int(os.getenv("MAX_AGENT_CALLS", "15")))

    # Compteurs internes
    _tokens_by_agent: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _calls_by_agent: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _total_tokens: int = 0
    _alerts: list[str] = field(default_factory=list)

    def track_call(self, agent_name: str, input_tokens: int, output_tokens: int) -> None:
        """Enregistre un appel d'agent et sa consommation de tokens."""
        tokens = input_tokens + output_tokens
        self._tokens_by_agent[agent_name] += tokens
        self._calls_by_agent[agent_name] += 1
        self._total_tokens += tokens

        # Détection de boucle : même agent appelé trop de fois
        if self._calls_by_agent[agent_name] == self.max_agent_calls:
            msg = (
                f"⚠️ {agent_name} a été appelé {self.max_agent_calls} fois — "
                f"suspicion de boucle. Vérifie le workflow."
            )
            self._alerts.append(msg)
            logger.warning(msg)

    def check_budget(self) -> bool:
        """
        Vérifie le budget. Lève BudgetExceeded si dépassé.
        Retourne True si OK, False si en alerte (mais pas bloqué).
        """
        if self._total_tokens >= self.max_tokens:
            msg = (
                f"Budget dépassé : {self._total_tokens} >= {self.max_tokens} tokens. "
                f"Arrêt forcé par le veilleur."
            )
            logger.critical(msg)
            raise BudgetExceeded(msg)

        alert_threshold = int(self.max_tokens * self.alert_pct / 100)
        if self._total_tokens >= alert_threshold:
            msg = (
                f"⚠️ Alerte budget : {self._total_tokens}/{self.max_tokens} tokens "
                f"({100*self._total_tokens//self.max_tokens}%)."
            )
            if msg not in self._alerts:
                self._alerts.append(msg)
                logger.warning(msg)
            return False
        return True

    def get_usage(self) -> dict:
        """Retourne un résumé de la consommation actuelle."""
        return {
            "total_tokens": self._total_tokens,
            "max_tokens": self.max_tokens,
            "pct_used": round(100 * self._total_tokens / self.max_tokens, 1) if self.max_tokens else 0,
            "by_agent": dict(self._tokens_by_agent),
            "calls_by_agent": dict(self._calls_by_agent),
            "alerts": list(self._alerts),
        }

    def analyze(self) -> str:
        """
        Analyse IA (Small) des tendances de consommation.
        Propose des optimisations concrètes.
        """
        model = os.getenv("WATCHER_MODEL", "mistral-small-latest")
        usage = self.get_usage()

        prompt = f"""Voici l'état de consommation d'une session d'agents IA :

{usage}

Analyse :
1. Y a-t-il un agent qui consomme anormalement plus que les autres ?
2. Y a-t-il une suspicion de boucle (beaucoup d'appels pour peu de résultat) ?
3. Propose 1-2 optimisations concrètes pour réduire la consommation.

Sois bref (5 lignes max)."""
        analysis = simple_prompt(prompt, model=model, temperature=0.1)
        record_tokens(model, estimate_tokens(prompt), estimate_tokens(analysis))
        return analysis
