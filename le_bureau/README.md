# le-bureau — agent managé Mistral

Agent « Le Bureau » déployé dans Mistral Studio (déploiement managé, worker `agents run-worker`).

## Architecture

- **Racine** `le-bureau` (mistral-large-4) : orchestrateur. Décompose la question, délègue en parallèle, croise, synthétise. Ne répond jamais de mémoire sur un point technique sensible sans vérification.
- **Sous-agents** (héritent du modèle de la racine) :
  - `chercheur` — recherche web sourcée via le connecteur `web_search` (≥ 2 sources indépendantes par chiffre, sources primaires vs secondaires, données introuvables signalées).
  - `analyste` — calculs, contrôle dimensionnel, tableaux + formules littérales + hypothèses.
  - `codeur` — code uniquement sur demande expresse, calculs trop volumineux/itératifs.
  - `verificateur` — contrôle qualité, marquage [✓]/[⚠]/[✖], incertitudes en tête.

## Règles de fond

Sources publiques uniquement (normes API/ASME/ISO, ouvrages, fabricants). Rien de classifié/confidentiel. Sécurité industrielle, PI et conformité export. Toute réponse finale porte l'avertissement : les chiffres doivent être revus par l'ingénieur (le Bureau informe, l'ingénieur décide).

## Développement local

```bash
cd le_bureau
uv sync
uv run agents info        # vérifie l'entrypoint sans clé API
uv run --env-file .env agents chat   # chat local (nécessite MISTRAL_API_KEY)
```

## Déploiement

Le déploiement managé se fait depuis la racine du dépôt `pythaboc/mistral-orchestrator`, sur le commit poussé, avec `build_directory = "le_bureau"` (voir `deploy_workflow_deployment`). Le connecteur `web_search` n'a pas besoin de sign-in ; le déploiement est `hardened` car un connecteur tourne en OBO lors des sessions utilisateur.
