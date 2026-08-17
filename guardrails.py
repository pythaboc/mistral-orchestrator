"""
Human-in-the-loop et guardrails.

Deux mécanismes :
1. Guardrails : scanne le code produit pour détecter des patterns dangereux
   (secrets, os.system, eval, requêtes réseau non autorisées, etc.).
2. Human-in-the-loop : avant toute action irréversible (merge PR, post Slack,
   suppression de fichier), demande confirmation à l'utilisateur.

Pensé pour mobile : les confirmations sont simples (o/n) et affichées clairement.
En mode web, les actions dangereuses passent par un endpoint d'approbation.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field

logger = logging.getLogger("orchestrator.guardrails")

# Patterns dangereux à détecter dans le code produit
_DANGEROUS_PATTERNS = [
    (r"\bos\.system\s*\(", "os.system() — exécution de commande arbitraire", "critical"),
    (r"\bsubprocess\.(?:Popen|call|run)\s*\(", "subprocess — exécution externe", "high"),
    (r"\beval\s*\(", "eval() — exécution de code arbitraire", "critical"),
    (r"\bexec\s*\(", "exec() — exécution de code arbitraire", "critical"),
    (r"\b__import__\s*\(", "__import__() — import dynamique", "high"),
    (r"(?:api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"]{8,}['\"]", "Secret hardcoded", "critical"),
    (r"\brequests\.(?:get|post|put|delete)\s*\(\s*['\"]http://", "Requête HTTP non sécurisée (pas HTTPS)", "medium"),
    (r"\bopen\s*\([^)]*['\"]w", "Écriture de fichier (vérifier le chemin)", "low"),
    (r"\bshutil\.rmtree\s*\(", "Suppression récursive de fichiers", "high"),
    (r"\bos\.remove\s*\(", "Suppression de fichier", "medium"),
]


@dataclass
class GuardrailResult:
    """Résultat du scan guardrails."""
    passed: bool
    issues: list[dict] = field(default_factory=list)


def scan_code(code: str) -> GuardrailResult:
    """
    Scanne du code pour détecter des patterns dangereux.
    Retourne un GuardrailResult avec les issues trouvées.
    """
    issues = []
    for pattern, description, severity in _DANGEROUS_PATTERNS:
        matches = list(re.finditer(pattern, code, re.IGNORECASE))
        for match in matches:
            # Trouve le numéro de ligne approximatif
            line_num = code[:match.start()].count("\n") + 1
            issues.append({
                "severity": severity,
                "description": description,
                "line": line_num,
                "match": match.group(0)[:50],
            })

    has_critical = any(i["severity"] == "critical" for i in issues)
    return GuardrailResult(
        passed=not has_critical,
        issues=issues,
    )


# --------------------------------------------------------------------------- #
#  Human-in-the-loop : confirmation avant actions irréversibles
# --------------------------------------------------------------------------- #

# Actions considérées comme irréversibles (nécessitent confirmation)
_IRREVERSIBLE_ACTIONS = {
    "merge_pr": "Merger une Pull Request sur GitHub",
    "post_slack": "Poster un message sur Slack",
    "delete_file": "Supprimer un fichier",
    "git_push": "Pousser du code sur git (push)",
    "git_force_push": "Force-push sur git (DANGEREUX)",
    "run_subprocess": "Exécuter une commande externe (subprocess)",
    "write_file": "Écrire/écraser un fichier",
}


@dataclass
class PendingAction:
    """Action en attente de confirmation."""
    action_type: str
    description: str
    details: dict
    approved: bool | None = None  # None = en attente, True/False = décidé


# Mode de confirmation : "interactive" (terminal) ou "web" (endpoint API)
_CONFIRMATION_MODE = os.getenv("CONFIRMATION_MODE", "interactive")

# En mode web, les actions en attente sont stockées ici
_pending_actions: dict[str, PendingAction] = {}


def request_approval(
    action_type: str,
    description: str,
    details: dict | None = None,
) -> bool:
    """
    Demande l'approbation de l'utilisateur avant une action irréversible.

    En mode interactif (terminal) : demande o/n.
    En mode web : stocke l'action en attente (l'utilisateur valide via l'API).

    Args:
        action_type: type d'action (ex: "merge_pr", "git_push").
        description: description lisible de l'action.
        details: détails supplémentaires (ex: repo, PR number).

    Returns:
        True si approuvé, False sinon.
    """
    if action_type not in _IRREVERSIBLE_ACTIONS:
        # Action non répertoriée : on ne bloque pas
        return True

    if _CONFIRMATION_MODE == "web":
        return _request_approval_web(action_type, description, details or {})
    else:
        return _request_approval_interactive(action_type, description, details or {})


def _request_approval_interactive(
    action_type: str, description: str, details: dict,
) -> bool:
    """Demande o/n dans le terminal."""
    print(f"\n{'!' * 50}", file=sys.stderr)
    print(f"⚠️  ACTION IRRÉVERSIBLE : {description}", file=sys.stderr)
    if details:
        for k, v in details.items():
            print(f"   {k}: {v}", file=sys.stderr)
    print(f"{'!' * 50}", file=sys.stderr)
    try:
        response = input("Approuver ? (o/n) : ").strip().lower()
        return response in ("o", "oui", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _request_approval_web(
    action_type: str, description: str, details: dict,
) -> bool:
    """
    En mode web : stocke l'action en attente.
    Retourne False immédiatement (l'action devra être re-tentée après approbation).
    """
    action_id = f"{action_type}_{int(__import__('time').time())}"
    _pending_actions[action_id] = PendingAction(
        action_type=action_type,
        description=description,
        details=details,
    )
    logger.info("Action en attente d'approbation : %s (%s)", action_type, action_id)
    return False  # L'action devra être re-tentée après approbation


def get_pending_actions() -> list[dict]:
    """Retourne les actions en attente d'approbation (mode web)."""
    return [
        {
            "id": aid,
            "action_type": a.action_type,
            "description": a.description,
            "details": a.details,
        }
        for aid, a in _pending_actions.items()
        if a.approved is None
    ]


def approve_action(action_id: str, approved: bool) -> bool:
    """Approuve ou refuse une action en attente (mode web). Retourne True si trouvée."""
    if action_id in _pending_actions:
        _pending_actions[action_id].approved = approved
        return True
    return False


def is_auto_approvable(action_type: str) -> bool:
    """
    Certaines actions de faible risque peuvent être auto-approuvées
    si configuré via AUTO_APPROVE (ex: "write_file,post_slack").
    """
    auto = os.getenv("AUTO_APPROVE", "").split(",")
    auto = [a.strip() for a in auto if a.strip()]
    return action_type in auto
