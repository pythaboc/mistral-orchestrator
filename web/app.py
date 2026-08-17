"""
Serveur web Flask pour l'interface de l'orchestrateur.

Endpoints :
- GET  /                 -> page HTML (chat + tableau + budget, responsive mobile)
- GET  /api/usage        -> JSON : consommation par agent + budget
- GET  /api/status       -> JSON : statut des agents
- POST /api/chat         -> JSON : envoie un message, retourne la réponse
- POST /api/reset        -> JSON : reset la conversation
- GET  /api/journal      -> JSON : contenu du journal
- GET  /api/traces       -> JSON : dernières traces d'appels API
- GET  /api/pending      -> JSON : actions en attente d'approbation
- POST /api/approve      -> JSON : approuve/refuse une action

Lance avec : python -m web.app  (ou  python web/app.py)
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from dotenv import load_dotenv
import logging as _logging
from flask import Flask, jsonify, request, send_from_directory

# Filtre les logs HTTP de werkzeug (GET /api/*) pour éviter le spam dans la console
_werkzeug_logger = _logging.getLogger("werkzeug")
_werkzeug_logger.setLevel(_logging.ERROR)  # Only show real errors, not each request

load_dotenv()

logger = logging.getLogger("orchestrator.web")

app = Flask(__name__, static_folder="static", template_folder="templates")

_budget_manager = None
_watcher = None
_conversation = None
_orchestrator = None

_task_lock = threading.Lock()
_current_task_status: dict[str, Any] = {"running": False, "task": "", "result": None}


def init(budget_manager, watcher, conversation, orchestrator):
    global _budget_manager, _watcher, _conversation, _orchestrator
    _budget_manager = budget_manager
    _watcher = watcher
    _conversation = conversation
    _orchestrator = orchestrator


@app.route("/")
def index():
    return send_from_directory(app.template_folder, "index.html")


@app.route("/api/usage")
def api_usage():
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
    return jsonify(_current_task_status)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True)
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "message vide"}), 400

    if _current_task_status["running"]:
        return jsonify({"error": "une tâche est déjà en cours, patiente"}), 409

    def run_task():
        _current_task_status["running"] = True
        _current_task_status["task"] = message
        _current_task_status["result"] = None
        try:
            _conversation.add_user(message)
            result = _orchestrator.run(message, use_two_coders=True, max_iterations=3)

            # Enregistre la consommation dans le budget mensuel
            if _budget_manager:
                by_agent = result.watcher.get("by_agent", {}) or {}
                for agent, tokens in by_agent.items():
                    _budget_manager.record(agent, "mixed", tokens)

            # Le résultat est affiché UNE SEULE fois par le frontend (via /api/status)
            # On ne l'ajoute PAS à la conversation pour éviter la répétition
            _current_task_status["result"] = {
                "task_type": result.task_type,
                "final_code": result.final_code,
                "answer": result.answer,
                "verification": result.verification,
                "iterations": result.iterations,
                "plan": result.plan,
                "watcher": result.watcher,
                "journal_entries": len(result.journal_entries),
            }
        except Exception as exc:
            logger.error("Erreur tâche : %s", exc, exc_info=True)
            _current_task_status["result"] = {"error": str(exc)}
        finally:
            _current_task_status["running"] = False

    threading.Thread(target=run_task, daemon=True).start()
    return jsonify({"status": "tâche lancée", "task": message})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    if _conversation:
        _conversation.reset()
        return jsonify({"status": "conversation réinitialisée"})
    return jsonify({"error": "conversation non initialisée"}), 500


@app.route("/api/journal")
def api_journal():
    from agents.scribe import read_journal
    return jsonify({"journal": read_journal()})


@app.route("/api/agents")
def api_agents():
    """Statut temps réel de chaque agent (qui travaille MAINTENANT)."""
    from agent_status import get_all_status
    agents = get_all_status()
    # Enrichit avec les tokens consommés (session)
    usage = _watcher.get_usage() if _watcher else {}
    by_agent = usage.get("by_agent", {}) or {}
    for a in agents:
        a["tokens"] = by_agent.get(a["name"], 0)
    return jsonify({"agents": agents, "running": _current_task_status.get("running", False)})


@app.route("/api/traces")
def api_traces():
    from tools.mistral_client import read_traces
    limit = request.args.get("limit", 50, type=int)
    return jsonify({"traces": read_traces(limit=limit)})


@app.route("/api/pending")
def api_pending():
    from guardrails import get_pending_actions
    return jsonify({"pending": get_pending_actions()})


@app.route("/api/approve", methods=["POST"])
def api_approve():
    from guardrails import approve_action
    data = request.get_json(force=True)
    action_id = data.get("action_id", "")
    approved = bool(data.get("approved", False))
    found = approve_action(action_id, approved)
    if found:
        return jsonify({"status": "approved" if approved else "rejected", "action_id": action_id})
    return jsonify({"error": "action non trouvée"}), 404


def run(host: str = "0.0.0.0", port: int = 5000) -> None:
    logger.info("Interface web sur http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
