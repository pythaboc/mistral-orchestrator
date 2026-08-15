"""
Client Mistral unifié pour l'orchestrateur.

Wrappe le SDK officiel `mistralai` (API actuelle : client.chat.complete).
Toutes les autres parties du projet utilisent cette fonction pour parler aux
modèles, ce qui centralise la gestion des erreurs, du cache et du logging.

Note : la conversation Vibe précédente utilisait `MistralClient` + `client.chat(model=...)`,
qui sont OBSOLÈTES. Le SDK actuel expose `Mistral` + `client.chat.complete(...)`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv

# L'emplacement de `Mistral` dépend de la version du SDK :
#   - SDK < 1.x  : `from mistralai.client import Mistral`
#   - SDK >= 1.x : `from mistralai import Mistral`
try:
    from mistralai import Mistral
except ImportError:  # pragma: no cover
    from mistralai.client import Mistral

load_dotenv()

logger = logging.getLogger("orchestrator.mistral")

_API_KEY = os.getenv("MISTRAL_API_KEY", "")
_client: Mistral | None = None

# Cache SQLite optionnel (activé si MISTRAL_CACHE_DB est défini).
# Contrairement au `lru_cache` en mémoire, il survit aux redémarrages.
_CACHE_DB = os.getenv("MISTRAL_CACHE_DB", "cache.sqlite")


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
        # La table n'existe pas encore ou base verrouillée : on continue sans cache.
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


def chat_complete(
    model: str,
    messages: list[dict],
    *,
    temperature: float = 0.2,
    use_cache: bool = True,
    **kwargs: Any,
) -> str:
    """
    Appelle `client.chat.complete` et retourne le contenu textuel de la réponse.

    Args:
        model: nom du modèle Mistral (ex: "mistral-large-latest").
        messages: liste de messages au format [{"role": ..., "content": ...}].
        temperature: 0.2 par défaut (déterministe, recommandé pour de l'audit).
        use_cache: active le cache SQLite (évite de payer deux fois le même prompt).
        **kwargs: passés tels quels à l'API (max_tokens, tools, tool_choice, ...).

    Returns:
        Le texte de la réponse du modèle.
    """
    key = _cache_key(model, messages, temperature=temperature, **kwargs)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            logger.debug("Cache hit (model=%s)", model)
            return cached

    client = _get_client()
    response = client.chat.complete(
        model=model,
        messages=messages,
        temperature=temperature,
        **kwargs,
    )
    content = response.choices[0].message.content or ""

    if use_cache:
        _cache_set(key, content)
    return content


def simple_prompt(
    prompt: str,
    model: str,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    use_cache: bool = True,
) -> str:
    """Raccourci : un seul prompt utilisateur -> réponse texte."""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat_complete(model, messages, temperature=temperature, use_cache=use_cache)
