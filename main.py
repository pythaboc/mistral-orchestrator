"""
Point d'entrée de l'orchestrateur d'équipe d'agents Mistral.

Modes d'exécution :
    python main.py                    # mode interactif (REPL avec mémoire)
    python main.py "ta tâche"         # une seule tâche puis exit
    python main.py --one-coder "..."  # une tâche, un seul codeur

En mode interactif, l'utilisateur arrive sur une fenêtre de discussion avec :
  - un message d'accueil
  - un résumé de la dernière session (lu depuis journal.md par le scribe)
Puis il peut enchaîner les tâches. À la sortie, le scribe enregistre
un récapitulatif de session.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from agents.scribe import record_entry, summarize_previous_session
from agents.watcher import BudgetExceeded
from orchestrator import Orchestrator

# Tâche d'exemple pour le mode non-interactif.
DEFAULT_TASK = "Écris une fonction Python qui valide un numéro IBAN selon le format international."


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def print_banner() -> None:
    print()
    print("=" * 60)
    print("   🤖 Orchestrateur d'équipe d'agents Mistral")
    print("   Large 3 (orchestrateur) · Medium 3.5 (codeurs/vérificateur/chercheur)")
    print("   Small (veilleur · scribe)")
    print("=" * 60)


def print_help() -> None:
    print()
    print("Commandes disponibles :")
    print("  <ta tâche>     — décrit ce que tu veux faire, l'orchestrateur s'occupe du reste")
    print("  /help          — affiche cette aide")
    print("  /usage         — affiche la consommation de tokens (veilleur)")
    print("  /journal       — affiche le journal complet du projet")
    print("  /exit  (ou /quit) — quitte (le scribe enregistre un récapitulatif)")
    print()


def print_result(result) -> None:
    """Affiche le résultat d'une tâche orchestrée de façon lisible."""
    print()
    print("─" * 60)

    if result.research:
        print("📚 Chercheur :")
        answer = result.research.get("answer", "")
        print(f"   {answer[:400]}{'...' if len(answer) > 400 else ''}")
        sources = result.research.get("sources", [])
        if sources:
            print("   Sources :")
            for s in sources[:3]:
                print(f"     - {s.get('title', '')[:60]} ({s.get('url', '')})")
        print()

    print(f"🧑\u200d💻 Codeurs : {len(result.code_candidates)} candidat(s) · "
          f"🔁 itérations code→vérif : {result.iterations} · "
          f"✅ verdict final : {result.verification.get('verdict', '?')}")

    print()
    print("--- Code final ---")
    print(result.final_code)

    issues = result.verification.get("issues", []) or []
    if issues:
        print()
        print("--- Problèmes relevés par le vérificateur ---")
        for issue in issues:
            sev = issue.get("severity", "?")
            cat = issue.get("category", "?")
            desc = issue.get("description", "")
            print(f"  [{sev}/{cat}] {desc}")

    usage = result.watcher
    print()
    print("--- Veilleur ---")
    print(f"   Tokens : {usage.get('total_tokens', 0)}/{usage.get('max_tokens', '?')} "
          f"({usage.get('pct_used', 0)}%)")
    by_agent = usage.get("by_agent", {}) or {}
    if by_agent:
        print("   Par agent : " + ", ".join(f"{k}={v}" for k, v in by_agent.items()))
    analysis = usage.get("analysis", "")
    if analysis:
        print(f"   Analyse : {analysis[:200]}")

    print(f"\n📝 {len(result.journal_entries)} entrée(s) ajoutée(s) au journal par le scribe.")
    print("─" * 60)


def run_one_task(orch: Orchestrator, task: str, *, one_coder: bool, max_iter: int) -> None:
    """Exécute une tâche via l'orchestrateur et affiche le résultat."""
    print(f"\n▶ Tâche : {task}\n")
    try:
        result = orch.run(
            task,
            use_two_coders=not one_coder,
            max_iterations=max_iter,
        )
    except BudgetExceeded as exc:
        print(f"\n🛑 {exc}", file=sys.stderr)
        return
    except Exception as exc:
        print(f"\n❌ Erreur pendant l'orchestration : {exc}", file=sys.stderr)
        return
    print_result(result)


def interactive_loop(orch: Orchestrator, *, max_iter: int) -> int:
    """
    Mode interactif : message d'accueil + résumé de la dernière session +
    boucle de conversation.
    """
    print_banner()

    # Résumé de la dernière session lu depuis le journal par le scribe.
    print()
    print("📝 Mémoire de la dernière session :")
    try:
        summary = summarize_previous_session()
    except Exception as exc:
        # Pas de clé API : on lit quand même le journal brut si possible.
        from agents.scribe import read_journal
        journal = read_journal()
        if journal:
            summary = journal[-500:]
        else:
            summary = f"(impossible de générer le résumé : {exc})"
    print(f"   {summary}")

    print()
    print("Bonjour ! Décris une tâche et l'équipe s'en charge. Tape /help pour l'aide.")

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            user_input = "/exit"

        if not user_input:
            continue

        if user_input in ("/exit", "/quit"):
            _on_exit(orch)
            print("À bientôt ! 👋")
            return 0

        if user_input == "/help":
            print_help()
            continue

        if user_input == "/usage":
            usage = orch.watcher.get_usage()
            print(json.dumps(usage, ensure_ascii=False, indent=2))
            continue

        if user_input == "/journal":
            from agents.scribe import read_journal
            journal = read_journal()
            print()
            print(journal if journal else "(journal vide)")
            continue

        if user_input.startswith("/"):
            print(f"Commande inconnue : {user_input} (essaie /help)")
            continue

        # Une tâche : on la lance.
        run_one_task(orch, user_input, one_coder=False, max_iter=max_iter)


def _on_exit(orch: Orchestrator) -> None:
    """À la sortie, le scribe enregistre un récapitulatif de session."""
    usage = orch.watcher.get_usage()
    recap = (
        f"Fin de session. Tokens consommés : {usage.get('total_tokens', 0)}/"
        f"{usage.get('max_tokens', '?')} ({usage.get('pct_used', 0)}%). "
        f"Alertes : {len(usage.get('alerts', []))}. "
        f"Analyse du veilleur : {usage.get('analysis', 'n/a')[:150]}"
    )
    try:
        record_entry("session", recap, author="veilleur")
        print("\n📝 Récapitulatif de session enregistré dans le journal.")
    except Exception as exc:
        print(f"\n(Impossible d'enregistrer le récapitulatif : {exc})", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Orchestrateur d'équipe d'agents Mistral"
    )
    parser.add_argument(
        "task", nargs="?", default=None, help="Tâche à accomplir (si omis : mode interactif)"
    )
    parser.add_argument(
        "--one-coder", action="store_true", help="Un seul codeur (au lieu de 2)"
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=2,
        help="Max de cycles code->vérification",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Logs détaillés"
    )
    args = parser.parse_args()

    setup_logging(args.verbose)
    orch = Orchestrator()

    # Mode non-interactif : une tâche puis exit.
    if args.task:
        run_one_task(orch, args.task, one_coder=args.one_coder, max_iter=args.max_iterations)
        _on_exit(orch)
        return 0

    # Mode interactif (REPL).
    return interactive_loop(orch, max_iter=args.max_iterations)


if __name__ == "__main__":
    sys.exit(main())
