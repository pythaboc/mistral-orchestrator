"""
Point d'entrée de l'orchestrateur d'équipe d'agents Mistral.

L'orchestrateur coordonne une équipe :
  - 1 ou 2 codeurs (Mistral Medium)
  - 1 vérificateur (Mistral Medium)
  - 1 chercheur (Mistral Medium + websearch natif ou fallback DuckDuckGo)
  - 1 veilleur (Mistral Small) : surveille les tokens
  - 1 scribe (Mistral Small) : tient le journal (journal.md)

Exemples :
    python main.py                                # tâche d'exemple
    python main.py "Écris une fonction qui valide un IBAN"
    python main.py --one-coder "ta tâche"          # un seul codeur
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from agents.watcher import BudgetExceeded
from orchestrator import Orchestrator

# Tâche d'exemple : nécessite une recherche (format IBAN à jour).
DEFAULT_TASK = "Écris une fonction Python qui valide un numéro IBAN selon le format international."


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Orchestrateur d'équipe d'agents Mistral"
    )
    parser.add_argument(
        "task", nargs="?", default=DEFAULT_TASK, help="La tâche à accomplir"
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
    print("=" * 60)
    print("Équipe d'agents Mistral")
    print("=" * 60)
    print(f"\nTâche : {args.task}\n")

    try:
        result = orch.run(
            args.task,
            use_two_coders=not args.one_coder,
            max_iterations=args.max_iterations,
        )
    except BudgetExceeded as exc:
        print(f"\n🛑 {exc}", file=sys.stderr)
        return 2

    # Affichage du résultat
    print("\n" + "=" * 60)
    print("RÉSULTAT")
    print("=" * 60)

    if result.research:
        print("\n📚 Recherche du chercheur :")
        print(f"   {result.research.get('answer', '')[:300]}")
        if result.research.get("sources"):
            print("   Sources :")
            for s in result.research["sources"][:3]:
                print(f"     - {s.get('title', '')[:60]} ({s.get('url', '')})")

    print(f"\n🧑\u200d💻 Codeurs : {len(result.code_candidates)} candidat(s) produit(s)")
    print(f"🔁 Itérations code->vérif : {result.iterations}")
    print(f"✅ Vérification finale : {result.verification.get('verdict', '?')}")

    print("\n--- Code final ---")
    print(result.final_code)

    print("\n--- Vérification ---")
    print(json.dumps(result.verification, ensure_ascii=False, indent=2))

    print("\n--- Veilleur (consommation) ---")
    print(json.dumps(result.watcher, ensure_ascii=False, indent=2))

    print(f"\n📝 {len(result.journal_entries)} entrée(s) de journal écrites dans journal.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
