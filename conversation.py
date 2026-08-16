"""
Gestionnaire de conversation avec résumé automatique du contexte.

Problème : à chaque message, tout l'historique est renvoyé à l'API, donc
la consommation de tokens augmente de plus en plus.

Solution :
- On garde l'historique en mémoire.
- Quand l'historique dépasse un seuil (en tokens estimés), le scribe résume
  automatiquement les messages les plus anciens, et on repart d'un contexte
  court : [résumé + messages récents].
- Commande /reset : vide l'historique (le journal.md reste intact).
"""

from __future__ import annotations

import logging
import os

from agents.scribe import summarize_conversation

logger = logging.getLogger("orchestrator.conversation")

# Seuil en tokens estimés au-delà duquel on résume (~4 caractères/token)
_CONTEXT_THRESHOLD = int(os.getenv("CONTEXT_THRESHOLD_TOKENS", "8000"))
# Nombre de messages récents à garder tels quels après résumé
_KEEP_RECENT = int(os.getenv("CONTEXT_KEEP_RECENT", "4"))


def estimate_tokens(messages: list[dict]) -> int:
    """Estimation grossière du nombre de tokens d'une liste de messages."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content) // 4
        else:
            total += len(str(content)) // 4
    return total


class ConversationManager:
    """
    Gère l'historique de conversation avec résumé automatique.

    Usage :
        conv = ConversationManager()
        conv.add_user("Fais une fonction X")
        conv.add_assistant("Voici le code...")
        messages = conv.get_messages()  # prêt pour l'API
        conv.reset()  # vide l'historique
    """

    def __init__(self) -> None:
        self._history: list[dict] = []
        self._summary: str = ""
        self._summarized_count: int = 0

    def add_user(self, content: str) -> None:
        """Ajoute un message utilisateur."""
        self._history.append({"role": "user", "content": content})

    def add_assistant(self, content: str) -> None:
        """Ajoute un message de l'orchestrateur/assistant."""
        self._history.append({"role": "assistant", "content": content})

    def get_messages(self) -> list[dict]:
        """
        Retourne les messages prêts pour l'API, en résumant si nécessaire.

        Si l'historique dépasse le seuil, on résume les anciens messages et
        on garde les récents. Le résumé est mis en cache (on ne le refait
        qu'une fois par seuil franchi).
        """
        # Vérifie si on doit résumer
        if estimate_tokens(self._history) > _CONTEXT_THRESHOLD:
            self._maybe_summarize()

        messages: list[dict] = []
        if self._summary:
            messages.append({
                "role": "system",
                "content": f"Contexte précédent (résumé par le scribe) :\n{self._summary}",
            })
        messages.extend(self._history)
        return messages

    def _maybe_summarize(self) -> None:
        """Résume les messages anciens si le seuil est dépassé."""
        if len(self._history) <= _KEEP_RECENT:
            return  # Pas assez de messages à résumer

        # Messages à résumer (tout sauf les _KEEP_RECENT récents)
        to_summarize = self._history[:-_KEEP_RECENT]
        recent = self._history[-_KEEP_RECENT:]

        # Si rien de nouveau à résumer depuis la dernière fois, on skip
        if len(to_summarize) <= self._summarized_count:
            return

        logger.info(
            "Résumé automatique du contexte (%d messages -> résumé)",
            len(to_summarize),
        )
        try:
            # On combine l'ancien résumé + les nouveaux messages à résumer
            full_to_summarize = []
            if self._summary:
                full_to_summarize.append({"role": "system", "content": self._summary})
            full_to_summarize.extend(to_summarize)

            new_summary = summarize_conversation(full_to_summarize)
            self._summary = new_summary
            self._history = recent
            self._summarized_count = 0  # réinitialisé car on a vidé l'ancien
        except Exception as exc:
            logger.warning("Échec du résumé du contexte : %s", exc)

    def reset(self) -> None:
        """Vide l'historique de conversation (le journal.md reste intact)."""
        self._history = []
        self._summary = ""
        self._summarized_count = 0
        logger.info("Historique de conversation réinitialisé (/reset)")

    def get_stats(self) -> dict:
        """Retourne des stats sur la conversation (pour l'interface web)."""
        return {
            "message_count": len(self._history),
            "estimated_tokens": estimate_tokens(self._history),
            "has_summary": bool(self._summary),
            "summary_length": len(self._summary),
            "context_threshold": _CONTEXT_THRESHOLD,
        }
