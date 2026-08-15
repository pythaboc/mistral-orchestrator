"""
Chercheur : recherche d'informations sur internet.

Deux stratégies, dans cet ordre :
1. Websearch natif Mistral via l'API Conversations (client.beta.agents.create +
   client.beta.conversations.start avec tools=[{"type": "web_search"}]).
   C'est la méthode officielle et la plus fiable, mais nécessite l'API beta.
2. Fallback Python : DuckDuckGo (package `ddgs`, anciennement `duckduckgo-search`)
   pour récupérer des résultats, puis synthèse via le modèle Mistral.

Le chercheur résume les résultats et fournit les sources (URLs).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from monitoring.metrics import estimate_tokens, record_tokens
from tools.mistral_client import _get_client

logger = logging.getLogger("orchestrator.researcher")

_MODEL = os.getenv("RESEARCHER_MODEL", "mistral-medium-latest")

# On garde un agent_id en cache pour ne pas recréer l'agent à chaque recherche.
_websearch_agent_id: str | None = None


def search(query: str, *, max_results: int = 5) -> dict:
    """
    Recherche sur internet et synthétise le résultat.

    Args:
        query: la question à rechercher.
        max_results: nombre max de résultats pour le fallback Python.

    Returns:
        {"answer": str, "sources": [{"title": ..., "url": ...}]}
    """
    logger.info("Recherche lancée : %s", query[:80])

    # 1. Tentative via le websearch natif Mistral
    try:
        result = _search_native(query)
        logger.info("Recherche web native Mistral réussie.")
        return result
    except Exception as exc:
        logger.warning(
            "Websearch natif Mistral indisponible (%s) -> fallback Python.", exc
        )

    # 2. Fallback Python (DuckDuckGo)
    try:
        result = _search_python_fallback(query, max_results=max_results)
        logger.info("Recherche via fallback Python (DuckDuckGo) réussie.")
        return result
    except Exception as exc:
        logger.error("Échec de la recherche (fallback inclus) : %s", exc)
        return {
            "answer": f"Recherche impossible : {exc}",
            "sources": [],
        }


def _search_native(query: str) -> dict:
    """
    Recherche via l'API Conversations native de Mistral avec web_search.

    Crée (une fois) un agent avec l'outil web_search, puis démarre une
    conversation. La réponse contient des chunks de texte et des références
    (tool_reference) avec les URLs des sources.
    """
    global _websearch_agent_id
    client = _get_client()

    if _websearch_agent_id is None:
        agent = client.beta.agents.create(
            model=_MODEL,
            name="Researcher Agent",
            description="Agent qui cherche des informations sur internet.",
            instructions=(
                "Tu es un chercheur. Utilise web_search pour trouver des "
                "informations à jour. Réponds de manière concise et cite tes sources."
            ),
            tools=[{"type": "web_search"}],
            completion_args={"temperature": 0.3, "top_p": 0.95},
        )
        _websearch_agent_id = agent.id

    response = client.beta.conversations.start(
        agent_id=_websearch_agent_id,
        inputs=query,
    )

    # On extrait le texte et les références depuis les entries.
    answer_parts: list[str] = []
    sources: list[dict] = []
    for entry in getattr(response, "outputs", []) or []:
        entry_type = getattr(entry, "type", "")
        if entry_type != "message.output":
            continue
        content = getattr(entry, "content", []) or []
        for chunk in content:
            chunk_type = getattr(chunk, "type", "")
            if chunk_type == "text" and getattr(chunk, "text", None):
                answer_parts.append(chunk.text)
            elif chunk_type == "tool_reference":
                url = getattr(chunk, "url", "") or ""
                title = getattr(chunk, "title", "") or url
                if url and url not in {s["url"] for s in sources}:
                    sources.append({"title": title, "url": url})

    answer = "\n".join(answer_parts).strip()
    if not answer:
        # Structure de réponse différente : on tente un fallback d'extraction.
        answer = _extract_any_text(response)

    # Estimation grossière des tokens pour le suivi.
    record_tokens(_MODEL, estimate_tokens(query), estimate_tokens(answer))
    return {"answer": answer, "sources": sources}


def _search_python_fallback(query: str, *, max_results: int) -> dict:
    """
    Fallback Python : DuckDuckGo pour les résultats, puis synthèse Mistral.
    Utilisé si l'API Conversations beta n'est pas disponible.

    Le package a été renommé : `duckduckgo-search` -> `ddgs`.
    On gère les deux noms pour rester robuste.
    """
    try:
        from ddgs import DDGS  # type: ignore  # nouveau nom du package
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore  # ancien nom (fallback)

    results_raw: list[dict] = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results_raw.append(
                {"title": r.get("title", ""), "url": r.get("href", ""), "body": r.get("body", "")}
            )

    if not results_raw:
        return {"answer": "Aucun résultat trouvé sur DuckDuckGo.", "sources": []}

    # Synthèse via le modèle Mistral.
    context = "\n\n".join(
        f"[{i+1}] {r['title']}\nURL: {r['url']}\n{r['body']}"
        for i, r in enumerate(results_raw)
    )
    prompt = f"""Question : {query}

Voici des résultats de recherche web :
{context}

Synthétise une réponse concise à la question, en t'appuyant sur ces résultats.
Cite les sources pertinentes par leur numéro entre crochets [1], [2], ..."""
    client = _get_client()
    response = client.chat.complete(
        model=_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    answer = response.choices[0].message.content or ""
    record_tokens(_MODEL, estimate_tokens(prompt), estimate_tokens(answer))

    sources = [{"title": r["title"], "url": r["url"]} for r in results_raw]
    return {"answer": answer, "sources": sources}


def _extract_any_text(response: Any) -> str:
    """Extraction de texte de secours si la structure n'est pas comme attendu."""
    raw = str(response)
    # Cherche les segments de texte entre guillemets après 'text='
    texts = re.findall(r"text='([^']+)'", raw)
    if texts:
        return "\n".join(texts)
    return raw[:500]
