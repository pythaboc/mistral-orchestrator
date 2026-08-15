"""
Scribe : mémoire du projet.

Enregistre les décisions importantes et points clés dans `journal.md`
(à la racine du projet), versionné avec git. Chaque entrée est horodatée
et catégorisée (décision / observation / alerte / recherche / code).

Le scribe est aussi un agent IA Small : il reçoit un texte brut et le
résume/formate en une entrée de journal concise avant de l'écrire.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime

from monitoring.metrics import estimate_tokens, record_tokens
from tools.mistral_client import simple_prompt

logger = logging.getLogger("orchestrator.scribe")

_JOURNAL_PATH = os.getenv("SCRIBE_JOURNAL", "journal.md")
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SYSTEM = """Tu es le scribe d'une équipe d'agents IA. Ton rôle : synthétiser
une information brute en une entrée de journal concise et utile pour la
mémoire du projet.

Règles :
- Sois factuel et bref (2 à 5 lignes max).
- Conserve les éléments techniques importants (fichiers, fonctions, décisions).
- N'invente rien : si l'info est floue, note-le.
- Retourne UNIQUEMENT le texte de l'entrée de journal, sans préambule,
  sans titre, sans markdown superflu. Une entrée doit tenir en un paragraphe."""

_SUMMARY_SYSTEM = """Tu es le scribe d'une équipe d'agents IA. Tu relis le
journal du projet pour préparer un résumé de la dernière session à destination
de l'utilisateur qui revient.

Règles :
- Résume en 3 à 6 lignes MAX ce qui a été fait la dernière fois.
- Mentionne les décisions clés, le code produit, les recherches faites.
- Si le journal est vide ou inexistant, dis simplement qu'il s'agit de la
  première session.
- Sois chaleureux mais factuel. Pas de markdown, juste du texte."""


def record_entry(
    category: str,
    content: str,
    *,
    author: str = "orchestrateur",
    commit: bool = True,
) -> str:
    """
    Enregistre une entrée dans le journal.

    Args:
        category: "decision" | "observation" | "alerte" | "recherche" | "code" | "session".
        content: texte brut à mémoriser.
        author: qui prend la décision (nom de l'agent ou "humain").
        commit: si True, fait un git commit du journal après écriture.

    Returns:
        L'entrée formatée telle qu'écrite dans le journal.
    """
    model = os.getenv("SCRIBE_MODEL", "mistral-small-latest")

    # Le scribe synthétise l'entrée via le modèle Small (léger, peu coûteux).
    summarized = simple_prompt(content, model=model, system=_SYSTEM, temperature=0.1)
    record_tokens(model, estimate_tokens(content), estimate_tokens(summarized))

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"## [{ts}] {category.upper()} — par {author}\n\n{summarized}\n"

    _append_to_journal(entry)
    logger.info("Entrée de journal enregistrée (%s)", category)

    if commit:
        _git_commit_journal(entry_summary=f"{category} par {author}")
    return entry


def read_journal() -> str:
    """
    Retourne le contenu brut du journal, ou une chaîne vide s'il n'existe pas.

    C'est la base utilisée pour générer le résumé de la dernière session.
    """
    if not os.path.exists(_JOURNAL_PATH):
        return ""
    try:
        with open(_JOURNAL_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        logger.warning("Lecture du journal impossible : %s", exc)
        return ""


def summarize_previous_session() -> str:
    """
    Génère un résumé de la dernière session à partir du journal.

    Utilise le modèle Small pour synthétiser l'historique. Si le journal est
    vide, indique qu'il s'agit de la première session.

    Returns:
        Un résumé de 3 à 6 lignes de ce qui a été fait la dernière fois.
    """
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
    summary = simple_prompt(prompt, model=model, system=_SUMMARY_SYSTEM, temperature=0.2)
    record_tokens(model, estimate_tokens(prompt), estimate_tokens(summary))
    return summary


def _append_to_journal(entry: str) -> None:
    """Ajoute l'entrée au fichier journal.md (le crée s'il n'existe pas)."""
    header = ""
    if not os.path.exists(_JOURNAL_PATH):
        header = "# Journal du projet\n\n_Carnet de bord tenu par le scribe (agent Mistral Small)._\n\n---\n\n"
    with open(_JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(header + entry + "\n")


def _git_commit_journal(*, entry_summary: str) -> bool:
    """
    Commit le journal avec git. Échoue silencieusement si git n'est pas
    disponible ou si le répertoire n'est pas un dépôt git.
    """
    try:
        # On s'assure d'être dans le répertoire du projet.
        subprocess.run(
            ["git", "add", _JOURNAL_PATH],
            cwd=_PROJECT_DIR,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                f"scribe: {entry_summary}",
            ],
            cwd=_PROJECT_DIR,
            check=True,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning("Commit git du journal échoué : %s", exc)
        return False
