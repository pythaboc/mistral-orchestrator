"""
Serveur web Flask pour l'interface de l'orchestrateur.

Expose :
- GET  /            -> page HTML (chat + tableau de tokens + budget)
- GET  /api/usage   -> JSON : consommation par agent + budget
- GET  /api/status  -> JSON : statut des agents
- POST /api/chat    -> JSON : envoie un message, retourne la réponse
- POST /api/reset   -> JSON : reset la conversation

Lance avec : python -m web.app  (ou  python web/app.py)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()

logger = logging.getLogger("orchestrator.web")

app = Flask(__name__, static_folder="static", template_folder="templates")

# Variables globales (partagées entre les routes)
_budget_manager = None
_watcher = None
_conversation = None
_orchestrator = None

# Verrou pour éviter les tâches parallèles (un seul orchestrateur à la fois)
_task_lock = threading.Lock()
_current_task_status: dict[str, Any] = {"running": False, "task": "", "result": None}


def init(budget_manager, watcher, conversation, orchestrator):
    """Initialise les références partagées. À appeler au démarrage."""
    global _budget_manager, _watcher, _conversation, _orchestrator
    _budget_manager = budget_manager
    _watcher = watcher
    _conversation = conversation
    _orchestrator = orchestrator


@app.route("/")
def index():
    """Page HTML principale."""
    return send_from_directory(app.template_folder, "index.html")


@app.route("/api/usage")
def api_usage():
    """Retourne la consommation de tokens par agent + budget."""
    data = {"budget": {}, "session": {}, "conversation": {}}
    if _budget_manager:
        data["budget"] = _budget_manager.to_dict()
    if _watcher:
        data["session"] = _watcher.get_usage()
    if _conversation:
        data["conversation"] = _conversation.get_stats()
    return jsonify(data)


@app.route("/api/status")
def api_status():
    """Retourne le statut courant (tâche en cours ou non)."""
    return jsonify(_current_task_status)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Envoie un message à l'orchestrateur et retourne la réponse."""
    data = request.get_json(force=True)
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "message vide"}), 400

    if _current_task_status["running"]:
        return jsonify({"error": "une tâche est déjà en cours, patiente"}), 409

    # Lance la tâche en arrière-plan
    def run_task():
        _current_task_status["running"] = True
        _current_task_status["task"] = message
        _current_task_status["result"] = None
        try:
            # Ajoute le message à la conversation
            _conversation.add_user(message)
            # Exécute via l'orchestrateur
            result = _orchestrator.run(message, use_two_coders=True, max_iterations=3)
            _current_task_status["result"] = {
                "final_code": result.final_code,
                "verification": result.verification,
                "iterations": result.iterations,
                "plan": result.plan,
                "watcher": result.watcher,
            }
            # L'orchestrateur répond avec le code produit
            response = (
                f"📋 Plan : {result.plan[:200]}\n\n"
                f"✅ Verdict : {result.verification.get('verdict', '?')} "
                f"({result.iterations} itération(s))\n\n"
                f"```\n{result.final_code}\n```"
            )
            _conversation.add_assistant(response)
        except Exception as exc:
            logger.error("Erreur tâche : %s", exc, exc_info=True)
            _current_task_status["result"] = {"error": str(exc)}
            _conversation.add_assistant(f"Erreur : {exc}")
        finally:
            _current_task_status["running"] = False

    thread = threading.Thread(target=run_task, daemon=True)
    thread.start()

    return jsonify({"status": "tâche lancée", "task": message})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Reset la conversation (garde le journal.md)."""
    if _conversation:
        _conversation.reset()
        return jsonify({"status": "conversation réinitialisée"})
    return jsonify({"error": "conversation non initialisée"}), 500


@app.route("/api/journal")
def api_journal():
    """Retourne le contenu du journal."""
    from agents.scribe import read_journal
    return jsonify({"journal": read_journal()})


def run(host: str = "0.0.0.0", port: int = 5000) -> None:
    """Lance le serveur Flask."""
    logger.info("Interface web sur http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
