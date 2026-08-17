"""
Client Mistral unifié pour l'orchestrateur.

Wrappe le SDK officiel `mistralai` (API actuelle : client.chat.complete).
Inclut :
- Retry avec backoff exponentiel (robustesse réseau/rate limit)
- Tracing de chaque appel (traces.jsonl) pour audit et debug
- Cache SQLite pour éviter les appels redondants
- Compteurs de tokens réels (response.usage)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

try:
    from mistralai import Mistral
except ImportError:  # pragma: no cover
    from mistralai.client import Mistral

load_dotenv()

logger = logging.getLogger("orchestrator.mistral")

_API_KEY = os.getenv("MISTRAL_API_KEY", "")
_client: Mistral | None = None

_CACHE_DB = os.getenv("MISTRAL_CACHE_DB", "cache.sqlite")
_TRACES_PATH = os.getenv("TRACES_PATH", "traces.jsonl")

# Config retry
_MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
_RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))  # secondes


@dataclass
class CallResult:
    """Résultat d'un appel Mistral, avec les vrais compteurs de tokens."""

    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    from_cache: bool = False
    retries: int = 0
    error: str | None = None


def _get_client() -> Mistral:
    """Initialise (paresseusement) et retourne le client Mistral."""
    global _client
    if _client is None:
        if not _API_KEY:
            raise RuntimeError(
                "MISTRAL_API_KEY manquant. Copie .env.example en .env et renseigne ta clé."
            )
        _client = Mistral(api_key=_API_KEY)
    return _client


# --------------------------------------------------------------------------- #
#  Tracing (niveau 2)
# --------------------------------------------------------------------------- #

def _trace(
    agent: str,
    model: str,
    prompt_snippet: str,
    result: CallResult,
    error: str | None = None,
) -> None:
    """Enregistre une trace de l'appel dans traces.jsonl (JSON Lines)."""
    try:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "agent": agent,
            "model": model,
            "prompt_snippet": prompt_snippet[:200],
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "from_cache": result.from_cache,
            "retries": result.retries,
            "error": error,
        }
        with open(_TRACES_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Échec d'écriture de trace : %s", exc)


def read_traces(limit: int = 50) -> list[dict]:
    """Lit les N dernières traces (pour l'interface web / debug)."""
    traces = []
    try:
        with open(_TRACES_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-limit:]:
            try:
                traces.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except FileNotFoundError:
        pass
    return traces


# --------------------------------------------------------------------------- #
#  Cache SQLite
# --------------------------------------------------------------------------- #

def _cache_key(model: str, messages: list[dict], **kwargs: Any) -> str:
    """Construit une clé de cache déterministe pour un appel."""
    payload = json.dumps(
        {"model": model, "messages": messages, **kwargs}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> str | None:
    try:
        conn = sqlite3.connect(_CACHE_DB, timeout=2)
        cur = conn.execute("SELECT response FROM cache WHERE key = ?", (key,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def _cache_set(key: str, response: str) -> None:
    try:
        conn = sqlite3.connect(_CACHE_DB, timeout=2)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, response TEXT, ts REAL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, response, ts) VALUES (?, ?, ?)",
            (key, response, time.time()),
        )
        conn.commit()
        conn.close()
    except sqlite3.OperationalError as exc:
        logger.warning("Échec d'écriture dans le cache SQLite : %s", exc)


# --------------------------------------------------------------------------- #
#  Appel principal avec retry + tracing
# --------------------------------------------------------------------------- #

def _is_retryable_error(exc: Exception) -> bool:
    """Détermine si une erreur mérite un retry (transitoire)."""
    exc_str = str(exc).lower()
    retryable_signals = [
        "timeout", "timed out", "connection", "rate limit", "429",
        "500", "502", "503", "504", "overloaded", "temporary",
        "service unavailable", "gateway", "reset by peer",
    ]
    return any(sig in exc_str for sig in retryable_signals)


def chat_complete(
    model: str,
    messages: list[dict],
    *,
    temperature: float = 0.2,
    use_cache: bool = True,
    agent_name: str = "unknown",
    **kwargs: Any,
) -> CallResult:
    """
    Appelle `client.chat.complete` avec retry, tracing et cache.

    Args:
        model: nom du modèle Mistral.
        messages: liste de messages [{"role":..., "content":...}].
        temperature: 0.2 par défaut.
        use_cache: active le cache SQLite.
        agent_name: nom de l'agent qui appelle (pour le tracing).
        **kwargs: passés à l'API (max_tokens, tools, ...).

    Returns:
        CallResult avec contenu + tokens réels + métadonnées.
    """
    key = _cache_key(model, messages, temperature=temperature, **kwargs)
    prompt_snippet = str(messages[-1].get("content", "")) if messages else ""

    # Cache
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            logger.debug("Cache hit (model=%s, agent=%s)", model, agent_name)
            result = CallResult(content=cached, from_cache=True)
            _trace(agent_name, model, prompt_snippet, result)
            return result

    client = _get_client()
    last_error = None
    retries = 0

    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = client.chat.complete(
                model=model,
                messages=messages,
                temperature=temperature,
                **kwargs,
            )
            content = response.choices[0].message.content or ""

            usage = getattr(response, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            total_tokens = getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)

            if use_cache:
                _cache_set(key, content)

            result = CallResult(
                content=content,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                retries=retries,
            )
            _trace(agent_name, model, prompt_snippet, result)
            return result

        except Exception as exc:
            last_error = exc
            if attempt < _MAX_RETRIES and _is_retryable_error(exc):
                delay = _RETRY_BASE_DELAY * (2 ** attempt)  # backoff exponentiel
                retries = attempt + 1
                logger.warning(
                    "Appel Mistral échoué (tentative %d/%d), retry dans %.1fs : %s",
                    attempt + 1, _MAX_RETRIES, delay, str(exc)[:200],
                )
                time.sleep(delay)
                continue
            # Erreur non retryable ou max retries atteint
            break

    # Tous les retries ont échoué
    error_msg = f"Échec après {retries} retry(s) : {last_error}"
    logger.error(error_msg)
    result = CallResult(content="", error=error_msg, retries=retries)
    _trace(agent_name, model, prompt_snippet, result, error=error_msg)
    raise RuntimeError(error_msg) from last_error


def simple_prompt(
    prompt: str,
    model: str,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    use_cache: bool = True,
    agent_name: str = "unknown",
) -> str:
    """Raccourci : un seul prompt -> réponse texte."""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat_complete(
        model, messages,
        temperature=temperature, use_cache=use_cache, agent_name=agent_name,
    ).content


def simple_prompt_tracked(
    prompt: str,
    model: str,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    use_cache: bool = True,
    agent_name: str = "unknown",
) -> tuple[str, CallResult]:
    """Raccourci qui retourne aussi le CallResult (avec les vrais tokens)."""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    result = chat_complete(
        model, messages,
        temperature=temperature, use_cache=use_cache, agent_name=agent_name,
    )
    return result.content, result
