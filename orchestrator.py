"""
Orchestrateur : coordinateur actif de l'équipe d'agents.

Coeur du système. C'est le "chef d'orchestre" (Mistral Large) qui :
1. Analyse la tâche et élabore un PLAN (étapes, approche, complexité).
2. Décide s'il faut une recherche web (et quoi chercher exactement).
3. Lance 1 ou 2 codeurs en parallèle avec un brief précis issu du plan.
4. Analyse les candidats et CHOISIT le meilleur (au lieu de prendre le 1er).
5. Fait vérifier le code par le vérificateur.
6. Décide intelligemment des relances : quels problèmes adresser, si ça vaut
   le coup d'itérer encore, ou si on garde le code malgré des warnings mineurs.
7. Le scribe enregistre les décisions clés à chaque étape.
8. Le veilleur surveille la consommation de tokens à chaque appel.

L'orchestrateur ne code pas lui-même : il raisonne, décide et coordonne.
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
from self_improve import (
    get_preferences_for_brief,
    improve_prompts,
    learn_preferences,
    load_prompt,
    post_mortem,
    post_mortem_to_dict,
    record_feedback,
)
from tools.mistral_client import chat_complete

load_dotenv()
logger = logging.getLogger("orchestrator")

_ORCHESTRATOR_MODEL = os.getenv("ORCHESTRATOR_MODEL", "mistral-large-latest")

# System prompt de l'orchestrateur : il doit RÉFLÉCHIR et PLANIFIER.
_ORCHESTRATOR_SYSTEM = """Tu es l'orchestrateur d'une équipe d'agents IA spécialisés en développement logiciel. Tu ne codes pas toi-même : tu coordonnes ton équipe.

Ton équipe :
- CHERCHEUR : peut faire des recherches web (API récentes, librairies, formats à jour).
- CODEURS (×2) : écrivent du code Python en parallèle (divergence pour comparaison).
- VÉRIFICATEUR : relit et challenge le code (cherche les bugs, pas les confirmations).
- VEILLEUR : surveille la consommation de tokens.
- SCRIBE : enregistre les décisions dans le journal du projet.

Ton rôle :
1. Analyser la tâche et élaborer un plan.
2. Décider si une recherche web est nécessaire (et formuler la requête précise).
3. Rédiger un BRIEF technique clair pour les codeurs (pas juste recopier la tâche).
4. Après production, analyser les candidats et choisir le meilleur.
5. Après vérification, décider de la suite : relancer, itérer, ou valider.

Sois concis mais précis. Ne recopie pas la tâche : apporte ta valeur de chef d'orchestre."""


@dataclass
class TaskResult:
    """Résultat final d'une tâche orchestrée."""

    task: str
    plan: str
    research: dict | None
    code_candidates: list[dict]
    chosen_candidate: int
    final_code: str
    verification: dict
    iterations: int
    watcher: dict
    journal_entries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "plan": self.plan,
            "research": self.research,
            "code_candidates": self.code_candidates,
            "chosen_candidate": self.chosen_candidate,
            "final_code": self.final_code,
            "verification": self.verification,
            "iterations": self.iterations,
            "watcher": self.watcher,
            "journal_entries": self.journal_entries,
        }


class Orchestrator:
    """
    Orchestrateur actif de l'équipe d'agents.

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
        self._task_count = 0
        self._improve_every = 5  # améliore les prompts toutes les 5 tâches

    def run(
        self,
        task: str,
        *,
        language: str = "python",
        use_two_coders: bool = True,
        max_iterations: int = 2,
    ) -> TaskResult:
        """
        Orchestre une tâche complète : plan -> recherche -> code -> choix -> vérification -> boucle.
        """
        journal_entries: list[str] = []
        self.watcher.check_budget()

        live.section(f"🎯 Tâche : {task[:100]}")

        # 1. L'orchestrateur élabore un PLAN et décide de la recherche.
        plan, research_needed, research_query = self._plan_task(task, language)

        # 2. Recherche si nécessaire.
        research = None
        research_context = ""
        if research_needed and research_query:
            research = self._do_research(research_query, journal_entries)
            research_context = (
                f"Informations de recherche utiles :\n{research.get('answer', '')}\n"
                f"Sources : {[s.get('url') for s in research.get('sources', [])]}"
            )

        # 3. Rédiger un brief technique pour les codeurs (à partir du plan).
        brief = self._write_brief(task, plan, research_context, language)
        journal_entries.append(
            record_entry(
                "decision",
                f"Plan élaboré pour: {task[:100]}. Approche: {plan[:200]}",
                author="orchestrateur",
            )
        )

        # 4. Codeurs : 1 ou 2 en parallèle avec le brief.
        code_candidates = self._run_coders(
            brief, language, research_context, use_two_coders, journal_entries
        )

        # 5. Choisir le meilleur candidat.
        iterations = 0
        chosen_idx = 0
        if len(code_candidates) > 1:
            chosen_idx = self._choose_best_candidate(task, code_candidates)
            live.agent_info(
                "orchestrateur",
                f"Candidat #{chosen_idx + 1} retenu (sur {len(code_candidates)})",
            )

        current_code = code_candidates[chosen_idx]["code"]
        verification = {}

        # 6. Boucle de vérification / correction.
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

            # L'orchestrateur DÉCIDE de la suite : relancer ou garder ?
            issues = verification.get("issues", []) or []
            critical = [i for i in issues if i.get("severity") in ("critical", "high")]

            if not critical:
                live.agent_info(
                    "orchestrateur",
                    f"Verdict {verdict} mais aucun problème critique — on conserve le code",
                )
                break

            # L'orchestrateur analyse les problèmes et décide quoi adresser.
            should_continue, focus = self._decide_iteration(task, verification, iterations, max_iterations)
            if not should_continue:
                live.agent_info(
                    "orchestrateur",
                    f"Max d'itérations atteint ou problèmes non bloquants — on conserve le code",
                )
                break

            live.agent_info(
                "orchestrateur",
                f"{len(critical)} problème(s) critique(s) à corriger. Relance des codeurs.",
            )
            journal_entries.append(
                record_entry(
                    "observation",
                    f"Itération {iterations}: {len(critical)} problème(s) critique(s). Focus: {focus[:200]}",
                    author="orchestrateur",
                )
            )
            current_code = self._rerun_coder(
                task, language, current_code, focus, journal_entries
            )

        # 7. Analyse du veilleur + récapitulatif.
        analysis = self.watcher.analyze()
        watcher_report = self.watcher.get_usage()
        watcher_report["analysis"] = analysis
        live.task_summary(watcher_report["by_agent"], watcher_report["total_tokens"])

        journal_entries.append(
            record_entry(
                "observation",
                f"Tâche terminée. Tokens: {watcher_report['total_tokens']}/{watcher_report['max_tokens']}. "
                f"Itérations: {iterations}. Verdict: {verification.get('verdict', '?')}. "
                f"Analyse: {analysis[:150]}",
                author="veilleur",
            )
        )

        # --- Auto-amélioration (niveaux 1, 2, 3) ---
        post_mortem_result = None
        try:
            # Niveau 2 : apprentissage des préférences utilisateur
            self.watcher.check_budget()
            learn_preferences(task, {
                "final_code": current_code,
                "verification": verification,
                "iterations": iterations,
            })

            # Niveau 3 : post-mortem (méta-réflexion)
            self.watcher.check_budget()
            pm = post_mortem(task, plan, {
                "final_code": current_code,
                "verification": verification,
                "iterations": iterations,
            })
            post_mortem_result = post_mortem_to_dict(pm)
            self.watcher.track_call("orchestrateur", tokens=pm.tokens)

            # Enregistrer les leçons dans le journal
            journal_entries.append(
                record_entry(
                    "observation",
                    f"Post-mortem: {pm.lessons}",
                    author="orchestrateur",
                )
            )

            # Niveau 1 : amélioration des prompts toutes les N tâches
            self._task_count += 1
            if self._task_count % self._improve_every == 0:
                self.watcher.check_budget()
                from agents.scribe import read_journal
                improve_result = improve_prompts(read_journal())
                journal_entries.append(
                    record_entry(
                        "decision",
                        f"Auto-amélioration: {improve_result.get('summary', '')}",
                        author="orchestrateur",
                    )
                )
        except Exception as exc:
            logger.warning("Auto-amélioration échouée (non bloquant) : %s", exc)

        return TaskResult(
            task=task,
            plan=plan,
            research=research,
            code_candidates=code_candidates,
            chosen_candidate=chosen_idx,
            final_code=current_code,
            verification=verification,
            iterations=iterations,
            watcher=watcher_report,
            journal_entries=journal_entries,
        )

    # ------------------------------------------------------------------ #
    #  Étape 1 : Planification
    # ------------------------------------------------------------------ #

    def _plan_task(self, task: str, language: str) -> tuple[str, bool, str]:
        """
        L'orchestrateur analyse la tâche et produit :
        - un plan d'action
        - un booléen : recherche web nécessaire ?
        - la requête de recherche précise (si applicable)
        """
        prompt = f"""Analyse cette tâche de développement et réponds en JSON :

TÂCHE : {task}
LANGAGE : {language}

Réponds UNIQUEMENT avec un objet JSON valide :
{{
  "plan": "ton plan d'action en 3-5 étapes concises",
  "research_needed": true/false,
  "research_query": "la requête de recherche précise si research_needed=true, sinon chaîne vide"
}}

Critères pour research_needed :
- true si la tâche nécessite des connaissances sur une API récente, un format précis (ex: IBAN, ISBN), une librairie spécifique, ou des specs techniques à jour.
- false si c'est de la logique pure, de l'algorithmique, ou du code standard."""
        live.agent_start("orchestrateur", "Analyse de la tâche et élaboration du plan", model=_ORCHESTRATOR_MODEL)
        result = chat_complete(
            _ORCHESTRATOR_MODEL,
            [{"role": "system", "content": _ORCHESTRATOR_SYSTEM}, {"role": "user", "content": prompt}],
            temperature=0.2,
        )
        self.watcher.track_call("orchestrateur", tokens=result.total_tokens)

        parsed = self._parse_json(result.content)
        plan = parsed.get("plan", task)
        research_needed = bool(parsed.get("research_needed", False))
        research_query = parsed.get("research_query", "")

        live.agent_done("orchestrateur", f"Plan élaboré", result=result)
        live.agent_thinking("orchestrateur", plan)
        if research_needed:
            live.agent_info("orchestrateur", f"🔍 Recherche web nécessaire : {research_query}")
        else:
            live.agent_info("orchestrateur", "Aucune recherche web nécessaire")

        return plan, research_needed, research_query

    def _write_brief(self, task: str, plan: str, research_context: str, language: str) -> str:
        """
        L'orchestrateur rédige un BRIEF technique précis pour les codeurs.
        Ce n'est pas juste la tâche recopiée : c'est un brief qui intègre le plan.
        """
        prompt = f"""Rédige un BRIEF technique concis (max 10 lignes) pour un codeur qui doit réaliser cette tâche.

TÂCHE : {task}
PLAN D'ACTION : {plan}
LANGAGE : {language}
"""
        if research_context:
            prompt += f"\nCONTEXTE DE RECHERCHE :\n{research_context}\n"
        prompt += "\nLe brief doit être direct, technique, et donner les consignes claires (pas de blabla)."

        result = chat_complete(
            _ORCHESTRATOR_MODEL,
            [{"role": "system", "content": _ORCHESTRATOR_SYSTEM}, {"role": "user", "content": prompt}],
            temperature=0.3,
        )
        self.watcher.track_call("orchestrateur", tokens=result.total_tokens)
        live.agent_done("orchestrateur", "Brief technique rédigé pour les codeurs", result=result)
        return result.content.strip()

    # ------------------------------------------------------------------ #
    #  Étape 2 : Recherche
    # ------------------------------------------------------------------ #

    def _do_research(self, query: str, journal: list[str]) -> dict:
        """Délègue la recherche au chercheur."""
        result_research = research_search(query)
        self.watcher.track_call("chercheur", tokens=result_research.get("tokens", 0))
        journal.append(
            record_entry(
                "recherche",
                f"Recherche '{query[:60]}': {result_research.get('answer', '')[:200]}",
                author="chercheur",
            )
        )
        return result_research

    # ------------------------------------------------------------------ #
    #  Étape 3 : Codeurs
    # ------------------------------------------------------------------ #

    def _run_coders(
        self,
        brief: str,
        language: str,
        context: str,
        use_two: bool,
        journal: list[str],
    ) -> list[dict]:
        """Lance 1 ou 2 codeurs en parallèle avec le brief."""
        live.section("🧑\u200d💻 Codeurs en action")

        full_context = context + f"\n\nBRIEF :\n{brief}" if context else f"BRIEF :\n{brief}"

        if use_two:
            live.agent_info("orchestrateur", "Lancement de 2 codeurs en parallèle (divergence)")
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = pool.submit(write_code, brief, language=language, context=full_context, label="codeur_1")
                fut_b = pool.submit(write_code, brief, language=language, context=full_context, label="codeur_2")
                code_a = fut_a.result()
                code_b = fut_b.result()
            self.watcher.track_call("codeur_1", tokens=code_a.get("tokens", 0))
            self.watcher.track_call("codeur_2", tokens=code_b.get("tokens", 0))
            journal.append(
                record_entry("code", f"2 codeurs ont produit du code en parallèle", author="codeur")
            )
            return [code_a, code_b]

        code = write_code(brief, language=language, context=full_context, label="codeur_1")
        self.watcher.track_call("codeur_1", tokens=code.get("tokens", 0))
        journal.append(record_entry("code", "1 codeur a produit du code", author="codeur"))
        return [code]

    # ------------------------------------------------------------------ #
    #  Étape 4 : Choix du meilleur candidat
    # ------------------------------------------------------------------ #

    def _choose_best_candidate(self, task: str, candidates: list[dict]) -> int:
        """
        L'orchestrateur compare les candidats et choisit le meilleur.
        Retourne l'index (0-based) du candidat retenu.
        """
        live.agent_start("orchestrateur", "Comparaison des candidats et choix du meilleur", model=_ORCHESTRATOR_MODEL)

        codes_desc = "\n\n".join(
            f"--- CANDIDAT {i+1} ({c['code'].count(chr(10))+1} lignes) ---\n{c['code'][:2000]}"
            for i, c in enumerate(candidates)
        )
        prompt = f"""Voici {len(candidates)} candidats de code pour cette tâche :

TÂCHE : {task}

{codes_desc}

Compare-les et choisis le MEILLEUR. Critères :
1. Correction (gère les edge cases)
2. Qualité (lisibilité, structure, idiomatique)
3. Complétude (répond à toute la tâche)

Réponds UNIQUEMENT avec un JSON : {{"choice": <numéro du candidat 1-N>, "reason": "pourquoi en 1 ligne"}}"""
        result = chat_complete(
            _ORCHESTRATOR_MODEL,
            [{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        self.watcher.track_call("orchestrateur", tokens=result.total_tokens)

        parsed = self._parse_json(result.content)
        choice = int(parsed.get("choice", 1)) - 1
        reason = parsed.get("reason", "")
        choice = max(0, min(choice, len(candidates) - 1))

        live.agent_done("orchestrateur", f"Candidat #{choice+1} retenu : {reason}", result=result)
        return choice

    # ------------------------------------------------------------------ #
    #  Étape 5 : Vérification
    # ------------------------------------------------------------------ #

    def _verify(self, code: str, task: str, language: str, journal: list[str]) -> dict:
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

    # ------------------------------------------------------------------ #
    #  Étape 6 : Décision d'itération
    # ------------------------------------------------------------------ #

    def _decide_iteration(self, task: str, verification: dict, iteration: int, max_iter: int) -> tuple[bool, str]:
        """
        L'orchestrateur décide s'il faut relancer les codeurs et sur quoi
        se concentrer. Retourne (should_continue, focus_for_codeurs).
        """
        issues = verification.get("issues", []) or []
        critical = [i for i in issues if i.get("severity") in ("critical", "high")]

        # Si on est à la dernière itération, on ne relance pas.
        if iteration >= max_iter:
            return False, ""

        # L'orchestrateur priorise les problèmes à adresser.
        issues_desc = "\n".join(
            f"- [{i.get('severity')}/{i.get('category')}] {i.get('description', '')}"
            for i in critical[:10]
        )
        prompt = f"""Tu es l'orchestrateur. Le vérificateur a relevé ces problèmes sur le code :

{issues_desc}

Itération actuelle : {iteration}/{max_iter}.

Décide :
1. Faut-il relancer les codeurs pour corriger ? (true/false)
2. Si oui, sur quels problèmes précisément se concentrer (max 5 lignes) ?

Réponds en JSON : {{"relaunch": true/false, "focus": "les problèmes à adresser en priorité"}}"""
        result = chat_complete(
            _ORCHESTRATOR_MODEL,
            [{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        self.watcher.track_call("orchestrateur", tokens=result.total_tokens)

        parsed = self._parse_json(result.content)
        relaunch = bool(parsed.get("relaunch", True))
        focus = parsed.get("focus", json.dumps(critical[:3], ensure_ascii=False))

        live.agent_done("orchestrateur", f"Décision: {'relancer' if relaunch else 'garder'} le code", result=result)
        return relaunch, focus

    def _rerun_coder(
        self,
        task: str,
        language: str,
        previous_code: str,
        focus: str,
        journal: list[str],
    ) -> str:
        """Relance un codeur avec le feedback priorisé du vérificateur."""
        context = (
            f"Code précédent (avec des problèmes) :\n```{language}\n{previous_code}\n```\n\n"
            f"Problèmes à corriger en priorité (vérificateur + orchestrateur) :\n{focus}\n\n"
            f"Corrige ces problèmes. Garde ce qui fonctionne déjà."
        )
        result = write_code(task, language=language, context=context, label="codeur_1")
        self.watcher.track_call("codeur_1", tokens=result.get("tokens", 0))
        journal.append(
            record_entry("code", "Codeur a corrigé le code suite aux retours", author="codeur")
        )
        return result["code"]

    # ------------------------------------------------------------------ #
    #  Utilitaires
    # ------------------------------------------------------------------ #

    def _parse_json(self, text: str) -> dict:
        """Extrait et parse un JSON depuis une réponse (tolérant au texte autour)."""
        import re
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            logger.warning("JSON non trouvé dans la réponse. Extrait: %s", text[:200])
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            # Tentative : retirer les commentaires / markdown autour
            try:
                clean = match.group(0).strip().strip("`")
                return json.loads(clean)
            except json.JSONDecodeError:
                logger.warning("JSON invalide. Extrait: %s", text[:200])
                return {}
