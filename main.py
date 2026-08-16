"""
Point d'entrée de l'orchestrateur d'équipe d'agents Mistral.

Modes d'exécution :
    python main.py                    # mode interactif (REPL avec mémoire)
    python main.py "ta tâche"         # une seule tâche puis exit
    python main.py --web              # lance l'interface web (http://localhost:5000)
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
from agents.watcher import BudgetExceeded, Watcher
from budget import BudgetManager
from conversation import ConversationManager
from dotenv import load_dotenv
from orchestrator import Orchestrator

load_dotenv()


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


def print_budget_status(budget: BudgetManager) -> None:
    """Affiche un résumé du budget mensuel au démarrage."""
    s = budget.status()
    print(f"\n💰 Budget mensuel : {s.used_30d:,}/{s.monthly_budget:,} tokens ({s.pct_used}%)")
    print(f"   Reste utilisable : {s.usable_remaining:,} tokens (réserve {s.safety_reserve:,})")
    daily = budget.daily_budget()
    print(f"   Budget du jour : {daily:,} tokens")


def print_help() -> None:
    print()
    print("Commandes disponibles :")
    print("  <ta tâche>     — décrit ce que tu veux faire, l'orchestrateur s'occupe du reste")
    print("  /help          — affiche cette aide")
    print("  /usage         — affiche la consommation de tokens (session + mois)")
    print("  /budget        — affiche l'état du budget mensuel")
    print("  /journal       — affiche le journal complet du projet")
    print("  /reset         — vide l'historique de la conversation (garde le journal)")
    print("  /exit  (ou /quit) — quitte (le scribe enregistre un récapitulatif)")
    print()


def print_result(result) -> None:
    """Affiche le résultat d'une tâche orchestrée de façon lisible."""
    print()
    print("─" * 60)

    if result.plan:
        print()
        print("📋 Plan de l'orchestrateur :")
        print(f"   {result.plan[:500]}")

    if result.research:
        print()
        print("📚 Chercheur :")
        answer = result.research.get("answer", "")
        print(f"   {answer[:500]}{'...' if len(answer) > 500 else ''}")
        sources = result.research.get("sources", [])
        if sources:
            print("   Sources :")
            for s in sources[:3]:
                print(f"     - {s.get('title', '')[:60]} ({s.get('url', '')})")

    print()
    print(f"🧑\u200d💻 Codeurs : {len(result.code_candidates)} candidat(s) · "
          f"candidat #{result.chosen_candidate + 1} retenu · "
          f"🔁 itérations : {result.iterations} · "
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
        print(f"   Analyse : {analysis[:300]}")

    print(f"\n📝 {len(result.journal_entries)} entrée(s) ajoutée(s) au journal par le scribe.")
    print("─" * 60)


def run_one_task(orch: Orchestrator, task: str, budget: BudgetManager,
                 conv: ConversationManager, *, one_coder: bool, max_iter: int) -> None:
    """Exécute une tâche via l'orchestrateur et affiche le résultat."""
    # Vérifie le budget mensuel avant de lancer
    if not budget.can_spend(10000):
        print("\n🛑 Budget mensuel insuffisant (réserve de sécurité menacée)", file=sys.stderr)
        print_budget_status(budget)
        return
    conv.add_user(task)
    try:
        result = orch.run(task, use_two_coders=not one_coder, max_iterations=max_iter)
    except BudgetExceeded as exc:
        print(f"\n🛑 {exc}", file=sys.stderr)
        return
    except Exception as exc:
        print(f"\n❌ Erreur pendant l'orchestration : {exc}", file=sys.stderr)
        return

    # Enregistre la consommation dans le budget mensuel
    by_agent = result.watcher.get("by_agent", {}) or {}
    for agent, tokens in by_agent.items():
        budget.record(agent, "mixed", tokens)

    print_result(result)
    response = (
        f"Plan : {result.plan[:200]}\n"
        f"Verdict : {result.verification.get('verdict', '?')} ({result.iterations} itération(s))\n"
        f"Code :\n{result.final_code}"
    )
    conv.add_assistant(response)


def interactive_loop(orch: Orchestrator, budget: BudgetManager,
                     conv: ConversationManager, *, max_iter: int) -> int:
    """Mode interactif : message d'accueil + résumé + boucle de conversation."""
    print_banner()
    print_budget_status(budget)

    print()
    print("📝 Mémoire de la dernière session :")
    try:
        summary = summarize_previous_session()
    except Exception as exc:
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
            _on_exit(orch, budget, conv)
            print("À bientôt ! 👋")
            return 0

        if user_input == "/help":
            print_help()
            continue

        if user_input == "/usage":
            print("\n--- Session ---")
            print(json.dumps(orch.watcher.get_usage(), ensure_ascii=False, indent=2))
            print("\n--- Mensuel ---")
            print(json.dumps(budget.to_dict(), ensure_ascii=False, indent=2))
            continue

        if user_input == "/budget":
            print_budget_status(budget)
            continue

        if user_input == "/journal":
            from agents.scribe import read_journal
            journal = read_journal()
            print()
            print(journal if journal else "(journal vide)")
            continue

        if user_input == "/reset":
            conv.reset()
            print("🔄 Historique de conversation réinitialisé (le journal est conservé).")
            continue

        if user_input.startswith("/"):
            print(f"Commande inconnue : {user_input} (essaie /help)")
            continue

        run_one_task(orch, user_input, budget, conv, one_coder=False, max_iter=max_iter)


def run_web(budget: BudgetManager, conv: ConversationManager, port: int) -> int:
    """Lance l'interface web Flask."""
    try:
        from web.app import init, run
    except ImportError as exc:
        print(f"❌ Flask non installé. Installe-le : pip install flask\n   ({exc})",
              file=sys.stderr)
        return 1

    orch = Orchestrator()
    init(budget_manager=budget, watcher=orch.watcher, conversation=conv, orchestrator=orch)
    print_banner()
    print_budget_status(budget)
    print(f"\n🌐 Interface web disponible sur http://localhost:{port}")
    run(port=port)
    return 0


def _on_exit(orch: Orchestrator, budget: BudgetManager, conv: ConversationManager) -> None:
    """À la sortie, le scribe enregistre un récapitulatif de session."""
    usage = orch.watcher.get_usage()
    bstatus = budget.status()
    recap = (
        f"Fin de session. Tokens session: {usage.get('total_tokens', 0)}. "
        f"Tokens mois: {bstatus.used_30d}/{bstatus.monthly_budget} ({bstatus.pct_used}%). "
        f"Reste: {bstatus.usable_remaining}. "
        f"Analyse veilleur: {usage.get('analysis', 'n/a')[:150]}"
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
        "--max-iterations", type=int, default=3,
        help="Max de cycles code->vérification",
    )
    parser.add_argument(
        "--web", action="store_true", help="Lance l'interface web au lieu du REPL"
    )
    parser.add_argument(
        "--port", type=int, default=5000, help="Port pour l'interface web"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Logs détaillés"
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    budget = BudgetManager()
    conv = ConversationManager()
    orch = Orchestrator()

    if args.web:
        return run_web(budget, conv, args.port)

    if args.task:
        run_one_task(orch, args.task, budget, conv,
                     one_coder=args.one_coder, max_iter=args.max_iterations)
        _on_exit(orch, budget, conv)
        return 0

    return interactive_loop(orch, budget, conv, max_iter=args.max_iterations)


if __name__ == "__main__":
    sys.exit(main())
