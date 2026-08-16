# Prompts des agents

Ce dossier contient les prompts (instructions système) de chaque agent.
Ils sont lus au démarrage par les agents et peuvent être **réécrits automatiquement**
par l'orchestrateur (auto-amélioration niveau 1) en s'appuyant sur les retours
enregistrés par le scribe dans `journal.md`.

## Fichiers

- `orchestrator.txt` : prompt de l'orchestrateur (coordinateur)
- `coder.txt` : prompt des codeurs
- `verifier.txt` : prompt du vérificateur
- `scribe.txt` : prompt du scribe (enregistrement des entrées)
- `scribe_summary.txt` : prompt du scribe (résumé de session)
- `scribe_conv.txt` : prompt du scribe (résumé de conversation)

## Auto-amélioration

Quand l'orchestrateur détecte (via `journal.md`) que quelque chose a mal fonctionné
(ex: vérificateur a raté un bug, codeur a produit du code verbeux), il peut
réécrire le prompt concerné pour éviter que ça se reproduise.

Les anciennes versions sont conservées via git (`git log prompts/verifier.txt`).
