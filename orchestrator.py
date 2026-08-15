"""
Orchestrateur : coordinateur de l'équipe d'agents.

Coeur du système. C'est le "chef d'orchestre" (Mistral Large) qui :
1. Analyse une tâche et décide quels agents mobiliser.
2. Si besoin d'infos externes -> délègue au chercheur.
3. Lance 1 ou 2 codeurs en parallèle sur la tâche (avec le contexte de recherche).
4. Fait vérifier le code par le vérificateur.
5. Si le vérificateur trouve des problèmes critiques, relance les codeurs
   avec les retours (boucle contrôlée par le veilleur).
6. Le scribe enregistre les décisions clés à chaque étape.
7. Le veilleur surveille la consommation de tokens à chaque appel.

L'orchestrateur ne code pas lui-même : il coordonne.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import live
from agents.coder import write_code
from agents.researcher import search as research_search
from agents.scribe import record_entry
from agents.verifier import verify_code
from agents.watcher import BudgetExceeded, Watcher
from dotenv import load_dotenv
from tools.mistral_client import chat_complete

load_dotenv()
logger = logging.getLogger("orchestrator")

_ORCHESTRATOR_MODEL = os.getenv("ORCHESTRATOR_MODEL", "mistral-large-latest")


@dataclass
class TaskResult:
    """Résultat final d'une tâche orchestrée."""

    task: str
    research: dict | None
    code_candidates: list[dict]
    final_code: str
    verification: dict
    iterations: int
    watcher: dict
    journal_entries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "research": self.research,
            "code_candidates": self.code_candidates,
            "final_code": self.final_code,
            "verification": self.verification,
            "iterations": self.iterations,
            "watcher": self.watcher,
            "journal_entries": self.journal_entries,
        }


class Orchestrator:
    """
    Orchestrateur de l'équipe d'agents.

    Usage :
        orch = Orchestrator()
        result = orch.run("Écris une fonction qui valide une adresse email")
        print(result.final_code)
    """

    def __init__(self) -> None:
        self.watcher = Watcher()
        logger.info(
            "Orchestrateur initialisé (model=%s, budget=%d tokens)",
            _ORCHESTRATOR_MODEL,
            self.watcher.max_tokens,
        )

    def run(
        self,
        task: str,
        *,
        language: str = "python",
        use_two_coders: bool = True,
        max_iterations: int = 2,
    ) -> TaskResult:
        """
        Orchestre une tâche complète : recherche -> code -> vérification -> boucle.
        """
        journal_entries: list[str] = []

        # 0. Le veilleur vérifie qu'on a encore du budget avant de démarrer.
        self.watcher.check_budget()

        live.section(f"🎯 Tâche : {task[:100]}")

        # 1. L'orchestrateur décide s'il faut rechercher des infos externes.
        research = self._maybe_research(task, journal_entries)
        research_context = ""
        if research:
            research_context = (
                f"Informations de recherche utiles :\n{research.get('answer', '')}\n"
                f"Sources : {[s.get('url') for s in research.get('sources', [])]}"
            )

        # 2. Codeurs : 1 ou 2 en parallèle.
        code_candidates = self._run_coders(
            task, language, research_context, use_two_coders, journal_entries
        )

        # 3. Sélection du meilleur candidat + vérification.
        iterations = 0
        current_code = code_candidates[0]["code"]
        verification = {}

        while iterations < max_iterations:
            iterations += 1
            self.watcher.check_budget()

            live.section(f"🔁 Vérification — itération {iterations}/{max_iterations}")
            verification = self._verify(current_code, task, language, journal_entries)
            verdict = verification.get("verdict", "WARN")

            if verdict == "OK":
                live.agent_info("orchestrateur", f"✅ Code validé après {iterations} itération(s)")
                journal_entries.append(
                    record_entry(
                        "code",
                        f"Code validé après {iterations} itération(s) pour: {task[:100]}",
                        author="verificateur",
                    )
                )
                break

            # Verdict WARN ou FAIL : on relance les codeurs avec les retours.
            issues = verification.get("issues", []) or []
            critical = [i for i in issues if i.get("severity") in ("critical", "high")]
            if not critical:
                # WARN sans problème critique : on garde le code.
                live.agent_info("orchestrateur", "Problèmes mineurs uniquement, on conserve le code")
                break

            live.agent_info(
                "orchestrateur",
                f"⚠️ {len(critical)} problème(s) critique(s) — relance des codeurs avec les retours"
            )
            feedback = json.dumps(critical, ensure_ascii=False)
            journal_entries.append(
                record_entry(
                    "observation",
                    f"Itération {iterations}: {len(critical)} problème(s) critique(s) trouvé(s). Relance des codeurs.",
                    author="verificateur",
                )
            )
            current_code = self._rerun_coder(
                task, language, current_code, feedback, journal_entries
            )

        # 4. Analyse du veilleur + récapitulatif.
        analysis = self.watcher.analyze()
        watcher_report = self.watcher.get_usage()
        watcher_report["analysis"] = analysis

        # Récapitulatif visuel des tokens par agent.
        live.task_summary(watcher_report["by_agent"], watcher_report["total_tokens"])

        journal_entries.append(
            record_entry(
                "observation",
                f"Session terminée. Tokens: {watcher_report['total_tokens']}/{watcher_report['max_tokens']}. "
                f"Analyse veilleur: {analysis[:150]}",
                author="veilleur",
            )
        )

        return TaskResult(
            task=task,
            research=research,
            code_candidates=code_candidates,
            final_code=current_code,
            verification=verification,
            iterations=iterations,
            watcher=watcher_report,
            journal_entries=journal_entries,
        )

    # ------------------------------------------------------------------ #
    #  Sous-étapes
    # ------------------------------------------------------------------ #

    def _maybe_research(self, task: str, journal: list[str]) -> dict | None:
        """
        L'orchestrateur (Large) décide si une recherche web est nécessaire.
        Si oui, délègue au chercheur.
        """
        prompt = (
            f"Voici une tâche de développement :\n{task}\n\n"
            f"Une recherche internet est-elle nécessaire pour accomplir cette "
            f"tâche (par exemple: API récente, librairie spécifique, format "
            f"à jour) ? Réponds UNIQUEMENT 'OUI' ou 'NON'."
        )
        live.agent_start("orchestrateur", "Décision : recherche web nécessaire ?", model=_ORCHESTRATOR_MODEL)
        result = chat_complete(
            _ORCHESTRATOR_MODEL,
            [{"role": "user", "content": prompt}],
            temperature=0.0,
        )
        self.watcher.track_call("orchestrateur", tokens=result.total_tokens)
        live.agent_done("orchestrateur", f"Décision : {result.content.strip()[:20]}", result=result)

        if "OUI" in result.content.upper():
            journal.append(
                record_entry(
                    "decision",
                    f"Recherche web nécessaire pour: {task[:100]}",
                    author="orchestrateur",
                )
            )
            result_research = research_search(task)
            self.watcher.track_call("chercheur", tokens=result_research.get("tokens", 0))
            journal.append(
                record_entry(
                    "recherche",
                    f"Recherche: {result_research.get('answer', '')[:200]}",
                    author="chercheur",
                )
            )
            return result_research
        return None

    def _run_coders(
        self,
        task: str,
        language: str,
        context: str,
        use_two: bool,
        journal: list[str],
    ) -> list[dict]:
        """Lance 1 ou 2 codeurs en parallèle."""
        live.section("🧑\u200d💻 Codeurs en action")

        if use_two:
            live.agent_info("orchestrateur", "Lancement de 2 codeurs en parallèle")
            with ThreadPoolExecutor(max_workers=2) as pool:
                # Deux codeurs avec un label différent (divergence via temperature).
                fut_a = pool.submit(write_code, task, language=language, context=context, label="codeur_1")
                fut_b = pool.submit(write_code, task, language=language, context=context, label="codeur_2")
                code_a = fut_a.result()
                code_b = fut_b.result()
            self.watcher.track_call("codeur_1", tokens=code_a.get("tokens", 0))
            self.watcher.track_call("codeur_2", tokens=code_b.get("tokens", 0))
            journal.append(
                record_entry(
                    "code",
                    f"2 codeurs ont produit du code en parallèle pour: {task[:80]}",
                    author="codeur",
                )
            )
            return [code_a, code_b]

        code = write_code(task, language=language, context=context, label="codeur_1")
        self.watcher.track_call("codeur_1", tokens=code.get("tokens", 0))
        journal.append(
            record_entry(
                "code",
                f"1 codeur a produit du code pour: {task[:80]}",
                author="codeur",
            )
        )
        return [code]

    def _verify(
        self, code: str, task: str, language: str, journal: list[str]
    ) -> dict:
        """Fait vérifier le code par le vérificateur."""
        result = verify_code(code, task=task, language=language)
        self.watcher.track_call("verificateur", tokens=result.get("tokens", 0))
        verdict = result.get("verdict", "WARN")
        journal.append(
            record_entry(
                "observation",
                f"Vérification: verdict={verdict}, {len(result.get('issues', []))} problème(s).",
                author="verificateur",
            )
        )
        return result

    def _rerun_coder(
        self,
        task: str,
        language: str,
        previous_code: str,
        feedback: str,
        journal: list[str],
    ) -> str:
        """Relance un codeur avec le feedback du vérificateur."""
        context = (
            f"Code précédent (avec des problèmes) :\n```{language}\n{previous_code}\n```\n\n"
            f"Problèmes signalés par le vérificateur :\n{feedback}\n\n"
            f"Corrige ces problèmes."
        )
        result = write_code(task, language=language, context=context, label="codeur_1")
        self.watcher.track_call("codeur_1", tokens=result.get("tokens", 0))
        journal.append(
            record_entry(
                "code",
                "Codeur a corrigé le code suite aux retours du vérificateur.",
                author="codeur",
            )
        )
        return result["code"]
