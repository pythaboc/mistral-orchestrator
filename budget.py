"""
Gestionnaire de budget mensuel adaptatif.

Suit la consommation de tokens sur 30 jours glissants (stocké en SQLite).
Chaque jour, recalcule le budget restant en tenant compte :
  - du budget mensuel total (ex: 17M tokens pour 25,50 € d'API)
  - de la réserve de sécurité (500k, non touchable)
  - du roulement : si peu consommé un jour, le reste s'ajoute aux jours suivants

Le système refuse de démarrer une tâche si le budget restant < réserve.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("orchestrator.budget")

# Budget mensuel en tokens (25,50 € d'API ≈ 17M tokens à prix moyen mixte)
_MONTHLY_BUDGET = int(os.getenv("MONTHLY_TOKEN_BUDGET", "17000000"))
# Réserve de sécurité : on ne touche jamais à ça
_SAFETY_RESERVE = int(os.getenv("SAFETY_RESERVE_TOKENS", "500000"))
# Fenêtre glissante en jours
_WINDOW_DAYS = int(os.getenv("BUDGET_WINDOW_DAYS", "30"))

_DB_PATH = os.getenv("BUDGET_DB", "budget.sqlite")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS token_usage (
            ts REAL NOT NULL,
            agent TEXT NOT NULL,
            model TEXT NOT NULL,
            tokens INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage(ts)"
    )
    conn.commit()
    return conn


@dataclass
class BudgetStatus:
    """Instantané de l'état du budget."""

    monthly_budget: int
    used_30d: int
    remaining: int
    safety_reserve: int
    usable_remaining: int  # remaining - safety_reserve
    pct_used: float
    daily_avg: int
    days_left_in_budget: int  # estimé basé sur la moyenne
    by_agent: dict[str, int] = field(default_factory=dict)
    by_model: dict[str, int] = field(default_factory=dict)


class BudgetManager:
    """
    Gestionnaire de budget mensuel adaptatif.

    Usage :
        bm = BudgetManager()
        bm.record(agent="codeur_1", model="mistral-medium-latest", tokens=1500)
        status = bm.status()
        if not bm.can_spend(2000):
            raise BudgetExceeded("Budget dépassé")
    """

    def __init__(self) -> None:
        self.monthly_budget = _MONTHLY_BUDGET
        self.safety_reserve = _SAFETY_RESERVE
        self.window_days = _WINDOW_DAYS
        logger.info(
            "BudgetManager initialisé : budget mensuel=%d tokens, réserve=%d",
            self.monthly_budget,
            self.safety_reserve,
        )

    def record(self, agent: str, model: str, tokens: int) -> None:
        """Enregistre une consommation de tokens (timestampée)."""
        if tokens <= 0:
            return
        conn = _db()
        conn.execute(
            "INSERT INTO token_usage (ts, agent, model, tokens) VALUES (?, ?, ?, ?)",
            (time.time(), agent, model, tokens),
        )
        conn.commit()
        conn.close()

    def _usage_since(self, since_ts: float) -> tuple[list[tuple], int]:
        """Retourne les enregistrements depuis since_ts et le total."""
        conn = _db()
        rows = conn.execute(
            "SELECT agent, model, tokens FROM token_usage WHERE ts >= ?",
            (since_ts,),
        ).fetchall()
        conn.close()
        total = sum(r[2] for r in rows)
        return rows, total

    def status(self) -> BudgetStatus:
        """Calcule l'état actuel du budget sur la fenêtre glissante."""
        now = time.time()
        window_start = now - (self.window_days * 86400)
        rows, used = self._usage_since(window_start)

        by_agent: dict[str, int] = defaultdict(int)
        by_model: dict[str, int] = defaultdict(int)
        for agent, model, tokens in rows:
            by_agent[agent] += tokens
            by_model[model] += tokens

        remaining = self.monthly_budget - used
        usable_remaining = remaining - self.safety_reserve
        pct_used = round(100 * used / self.monthly_budget, 1) if self.monthly_budget else 0
        daily_avg = used // self.window_days if self.window_days else 0

        # Estimation : combien de jours avant épuisement (basé sur la moyenne)
        if daily_avg > 0 and usable_remaining > 0:
            days_left = usable_remaining // daily_avg
        else:
            days_left = -1  # illimité / indéterminé

        return BudgetStatus(
            monthly_budget=self.monthly_budget,
            used_30d=used,
            remaining=remaining,
            safety_reserve=self.safety_reserve,
            usable_remaining=usable_remaining,
            pct_used=pct_used,
            daily_avg=daily_avg,
            days_left_in_budget=days_left,
            by_agent=dict(by_agent),
            by_model=dict(by_model),
        )

    def can_spend(self, tokens: int) -> bool:
        """Vérifie si on peut dépenser `tokens` sans franchir la réserve."""
        status = self.status()
        return status.usable_remaining - tokens >= 0

    def daily_budget(self) -> int:
        """
        Budget adaptatif pour AUJOURD'HUI.

        Calcul : (reste mensuel - réserve) / jours restants dans la fenêtre.
        Si peu consommé les jours précédents, le budget du jour augmente
        (roulement). On répartit le restant sur les jours qu'il reste.
        """
        status = self.status()
        # Jours restants = fenêtre 30j - jours déjà écoulés avec conso
        # En pratique, on répartit sur le reste de la fenêtre.
        days_elapsed = self.window_days  # approximation conservatrice
        # Mais si on a déjà consommé, on compte les jours "actifs"
        days_left = self.window_days
        if status.daily_avg > 0:
            # Estime les jours déjà écoulés par la consommation
            days_elapsed = max(1, status.used_30d // max(1, status.daily_avg))
            days_left = max(1, self.window_days - days_elapsed)

        if status.usable_remaining <= 0:
            return 0
        return status.usable_remaining // days_left

    def to_dict(self) -> dict:
        """Pour sérialisation JSON (interface web)."""
        s = self.status()
        return {
            "monthly_budget": s.monthly_budget,
            "used_30d": s.used_30d,
            "remaining": s.remaining,
            "safety_reserve": s.safety_reserve,
            "usable_remaining": s.usable_remaining,
            "pct_used": s.pct_used,
            "daily_avg": s.daily_avg,
            "days_left_in_budget": s.days_left_in_budget,
            "daily_budget": self.daily_budget(),
            "by_agent": s.by_agent,
            "by_model": s.by_model,
        }


class BudgetExceeded(Exception):
    """Levé quand le budget mensuel est dépassé ou la réserve menacée."""
