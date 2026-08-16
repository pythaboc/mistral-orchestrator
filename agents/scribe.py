"""
Scribe : mémoire du projet.

Enregistre les décisions importantes et points clés dans `journal.md`
(à la racine du projet), versionné avec git. Chaque entrée est horodatée
et catégorisée (décision / observation / alerte / recherche / code).

Le scribe est aussi un agent IA Small : il reçoit un texte brut et le
résume/formate en une entrée de journal concise avant de l'écrire.

Les prompts sont lus depuis prompts/ (modifiables par auto-amélioration).
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime

import live
from self_improve import load_prompt
from tools.mistral_client import chat_complete

logger = logging.getLogger("orchestrator.scribe")

_JOURNAL_PATH = os.getenv("SCRIBE_JOURNAL", "journal.md")
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FALLBACK_SYSTEM = """Tu es le scribe d'une équipe d'agents IA. Ton rôle : synthétiser
une information brute en une entrée de journal concise et utile pour la
mémoire du projet.

Règles :
- Sois factuel et bref (2 à 5 lignes max).
- Conserve les éléments techniques importants (fichiers, fonctions, décisions).
- N'invente rien : si l'info est floue, note-le.
- Retourne UNIQUEMENT le texte de l'entrée de journal, sans préambule,
  sans titre, sans markdown superflu. Une entrée doit tenir en un paragraphe."""

_FALLBACK_SUMMARY_SYSTEM = """Tu es le scribe d'une équipe d'agents IA. Tu relis le
journal du projet pour préparer un résumé de la dernière session à destination
de l'utilisateur qui revient.

Règles :
- Résume en 3 à 6 lignes MAX ce qui a été fait la dernière fois.
- Mentionne les décisions clés, le code produit, les recherches faites.
- Si le journal est vide ou inexistant, dis simplement qu'il s'agit de la
  première session.
- Sois chaleureux mais factuel. Pas de markdown, juste du texte."""

_FALLBACK_CONV_SYSTEM = """Tu es le scribe d'une équipe d'agents IA.
On te donne l'historique d'une conversation entre l'utilisateur et l'orchestrateur.
Résume-le en un contexte concis (max 500 mots) qui permettra à l'orchestrateur
de reprendre la conversation sans perdre le fil. Conserve les décisions clés,
les tâches en cours, les préférences de l'utilisateur. Pas de markdown."""


def record_entry(category: str, content: str, *, author: str = "orchestrateur", commit: bool = True) -> str:
    """Enregistre une entrée dans le journal."""
    model = os.getenv("SCRIBE_MODEL", "mistral-small-latest")
    live.agent_start("scribe", f"Enregistrement : {category}", model=model)

    system_prompt = load_prompt("scribe", _FALLBACK_SYSTEM)
    result = chat_complete(
        model,
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}],
        temperature=0.1,
    )
    summarized = result.content

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"## [{ts}] {category.upper()} — par {author}\n\n{summarized}\n"

    _append_to_journal(entry)
    logger.info("Entrée de journal enregistrée (%s)", category)
    live.agent_done("scribe", "Entrée ajoutée au journal", result=result)

    if commit and _git_repo_has_remote():
        _git_commit_journal(entry_summary=f"{category} par {author}")
    return entry


def read_journal() -> str:
    """Retourne le contenu brut du journal, ou une chaîne vide s'il n'existe pas."""
    if not os.path.exists(_JOURNAL_PATH):
        return ""
    try:
        with open(_JOURNAL_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        logger.warning("Lecture du journal impossible : %s", exc)
        return ""


def summarize_previous_session() -> str:
    """Génère un résumé de la dernière session à partir du journal."""
    model = os.getenv("SCRIBE_MODEL", "mistral-small-latest")
    journal = read_journal()

    if not journal.strip():
        return (
            "Bienvenue ! Il s'agit de votre première session. "
            "Le journal du projet est encore vide — les décisions et points clés "
            "seront enregistrés automatiquement au fur et à mesure par le scribe."
        )

    prompt = (
        "Voici le journal du projet (entrées chronologiques) :\n\n"
        f"{journal}\n\n"
        "Résume ce qui a été fait lors de la dernière session "
        "(les entrées les plus récentes) en 3 à 6 lignes."
    )
    live.agent_start("scribe", "Lecture du journal pour le résumé de session", model=model)
    system_prompt = load_prompt("scribe_summary", _FALLBACK_SUMMARY_SYSTEM)
    result = chat_complete(
        model,
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
        temperature=0.2,
    )
    live.agent_done("scribe", "Résumé de la dernière session généré", result=result)
    return result.content


def summarize_conversation(history: list[dict]) -> str:
    """Résume l'historique d'une conversation pour éviter que le contexte ne grossisse."""
    if not history:
        return ""

    model = os.getenv("SCRIBE_MODEL", "mistral-small-latest")
    conv_text = "\n\n".join(f"[{m['role'].upper()}] {m['content']}" for m in history)
    prompt = f"Voici l'historique de la conversation à résumer :\n\n{conv_text}\n\nRésume ce contexte de manière concise."
    system_prompt = load_prompt("scribe_conv", _FALLBACK_CONV_SYSTEM)
    result = chat_complete(
        model,
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return result.content


def _append_to_journal(entry: str) -> None:
    """Ajoute l'entrée au fichier journal.md (le crée s'il n'existe pas)."""
    header = ""
    if not os.path.exists(_JOURNAL_PATH):
        header = "# Journal du projet\n\n_Carnet de bord tenu par le scribe (agent Mistral Small)._\n\n---\n\n"
    with open(_JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(header + entry + "\n")


def _git_repo_has_remote() -> bool:
    """Vérifie si le repo git a au moins un remote configuré."""
    try:
        result = subprocess.run(["git", "remote"], cwd=_PROJECT_DIR, capture_output=True, text=True, timeout=5)
        return result.returncode == 0 and result.stdout.strip() != ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _git_commit_journal(*, entry_summary: str) -> bool:
    """Commit le journal avec git. Échoue silencieusement sinon."""
    try:
        subprocess.run(["git", "add", _JOURNAL_PATH], cwd=_PROJECT_DIR, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"scribe: {entry_summary}"], cwd=_PROJECT_DIR, check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning("Commit git du journal échoué : %s", exc)
        return False
