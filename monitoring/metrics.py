"""
Métriques Prometheus pour l'orchestrateur.

Expose sur un port HTTP les compteurs : tokens estimés, tâches exécutées,
contradictions détectées, coût estimé. À visualiser dans Prometheus + Superset
(comme @powl_d avec Prometheus + Superset).
"""

from __future__ import annotations

import logging
import os
import threading
from functools import partial

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("orchestrator.metrics")

_PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", "8000"))

# Import paresseux : prometheus_client est optionnel.
try:
    from prometheus_client import Counter, Gauge, start_http_server

    _TOKENS_USED = Counter(
        "orchestrator_tokens_used", "Tokens estimés consommés", ["model"]
    )
    _TASKS_EXECUTED = Counter(
        "orchestrator_tasks_executed", "Tâches exécutées", ["task_type"]
    )
    _CONTRADICTIONS = Counter(
        "orchestrator_contradictions", "Contradictions détectées entre agents"
    )
    _COST_EUR = Gauge(
        "orchestrator_cost_eur_estimated", "Coût estimé en EUR (approximatif)"
    )
    _AVAILABLE = True
except ImportError:  # pragma: no cover
    logger.warning(
        "prometheus-client non installé : métriques désactivées. "
        "Installe-le avec : pip install prometheus-client"
    )
    _AVAILABLE = False

    def _noop(*args, **kwargs):  # type: ignore
        pass

    _TOKENS_USED = _noop  # type: ignore
    _TASKS_EXECUTED = _noop  # type: ignore
    _CONTRADICTIONS = _noop  # type: ignore
    _COST_EUR = _noop  # type: ignore


# Prix approximatifs par 1M tokens (à vérifier sur https://docs.mistral.ai/getting-started/pricing)
_PRICE_PER_M_TOKENS_EUR = {
    "mistral-large-latest": {"input": 2.0, "output": 6.0},
    "mistral-medium-latest": {"input": 0.4, "output": 4.0},
    "mistral-small-latest": {"input": 0.1, "output": 0.1},
    "default": {"input": 1.0, "output": 3.0},
}

_running = False


def start_server(port: int | None = None) -> None:
    """Démarre le serveur Prometheus en arrière-plan (idempotent)."""
    global _running
    if not _AVAILABLE:
        return
    if _running:
        return
    start_http_server(port or _PROMETHEUS_PORT)
    _running = True
    logger.info(
        "Métriques Prometheus disponibles sur http://localhost:%s/metrics",
        port or _PROMETHEUS_PORT,
    )


def record_task(task_type: str) -> None:
    if _AVAILABLE:
        _TASKS_EXECUTED.labels(task_type=task_type).inc()


def record_contradiction() -> None:
    if _AVAILABLE:
        _CONTRADICTIONS.inc()


def record_tokens(model: str, input_tokens: int, output_tokens: int) -> None:
    """Enregistre les tokens et met à jour l'estimation de coût."""
    if not _AVAILABLE:
        return
    _TOKENS_USED.labels(model=model).inc(input_tokens + output_tokens)
    prices = _PRICE_PER_M_TOKENS_EUR.get(model, _PRICE_PER_M_TOKENS_EUR["default"])
    cost = (
        (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
    )
    _COST_EUR.set(cost)


def estimate_tokens(text: str) -> int:
    """Estimation grossière : ~4 caractères par token."""
    return max(1, len(text) // 4)
