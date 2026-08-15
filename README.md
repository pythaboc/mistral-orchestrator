# Orchestrateur d'équipe d'agents Mistral

Un orchestrateur qui coordonne une **équipe d'agents spécialisés**, chacun avec le modèle Mistral le plus adapté à son rôle. Inspiré du workflow de [@powl_d](https://x.com/powl_d) (Emilien orchestrateur + agents spécialisés), mais adapté à l'**API Mistral réelle** (2025/2026).

## 🏗️ L'équipe

| Rôle | Modèle | Mission |
|---|---|---|
| **Orchestrateur** | Mistral Large 3 | Coordonne l'équipe, décide qui mobiliser et quand. Ne code pas lui-même. |
| **Codeurs** (×2) | Mistral Medium 3.5 | Écrivent le code en parallèle (divergence pour le vérificateur). |
| **Vérificateur** | Mistral Medium 3.5 | Relit et **challenge** le code (cherche les bugs, pas les confirmations). |
| **Chercheur** | Mistral Medium 3.5 | Recherche sur internet (websearch natif Mistral + fallback DuckDuckGo). |
| **Veilleur** | Mistral Small | Surveille **drastiquement** la consommation de tokens, détecte les boucles, bloque si budget dépassé. |
| **Scribe** | Mistral Small | Enregistre les décisions clés dans `journal.md` (versionné git). |

### Pourquoi ces modèles ?

- **Large 3** pour l'orchestrateur : c'est le "chef", il doit comprendre la tâche globalement et décider de la stratégie. Le plus capable, le plus cher, mais appelé peu de fois.
- **Medium 3.5** pour les codeurs/vérificateur/chercheur : optimisé pour le code et l'agentic, dialogue natif, bon rapport coût/performance. Codestral est un modèle de complétion (FIM), pas idéal pour dialoguer en agent.
- **Small** pour le veilleur et le scribe : tâches simples (compter, résumer), appelés souvent, donc on minimise le coût.

## 🔄 Workflow

```
                    ┌──────────────────────┐
   tâche ──────────►│   Orchestrateur       │
                    │   (Mistral Large 3)    │
                    └──────────┬───────────┘
                               │
                  recherche nécessaire ?
                      /            \
                    OUI            NON
                     │              │
            ┌────────▼────────┐    │
            │   Chercheur      │    │
            │ (Medium + web)   │    │
            │  websearch natif │    │
            │  ou DuckDuckGo   │    │
            └────────┬────────┘    │
                     │             │
                     └──────┬──────┘
                            ▼
                ┌───────────────────────┐
                │  Codeurs (×2 parallèle)│
                │  (Mistral Medium 3.5) │
                └───────────┬───────────┘
                            ▼
                ┌───────────────────────┐
                │   Vérificateur         │
                │  (cherche les bugs)   │
                └───────────┬───────────┘
                            │
                  problèmes critiques ?
                      /            \
                    NON            OUI
                     │              │
                  FIN         relance codeurs
                              avec les retours
                              (max 2 itérations)
```

**À chaque étape :**
- Le **veilleur** enregistre la consommation et vérifie le budget (bloque si dépassé).
- Le **scribe** enregistre la décision dans `journal.md`.

## ⚠️ Corrections vs la conversation Vibe précédente

| Vibe disait | Réalité (vérifiée) |
|---|---|
| `MistralClient(api_key=...)` | Obsolète → `Mistral(api_key=...)` |
| `client.chat(model=...)` | `client.chat.complete(...)` |
| Limite 32k tokens | 128k+ (Large 3), jusqu'à 1M (GLM 5.2) |
| "Pas de vision native" | Large 3/Medium 3.5/Small 4 sont multimodaux |
| "Pas d'OCR natif" | `mistral-ocr-2505` existe |
| LangChain `initialize_agent(llm=client.chat(...))` | Ne fonctionne pas |
| API Agents non mentionnée | **Existe** : `client.beta.agents.create` + `client.beta.conversations.start` |
| Websearch via `chat.complete` | **Faux** : websearch fonctionne via l'API Conversations (`client.beta.conversations`), pas `chat.complete`. D'où le fallback DuckDuckGo. |

## 📁 Structure

```
mistral-orchestrator/
├── orchestrator.py        # Coordinateur (Large 3) : dispatche aux agents
├── main.py                # CLI
├── agents/
│   ├── coder.py           # Codeur (Medium 3.5) : écrit le code
│   ├── verifier.py        # Vérificateur (Medium 3.5) : challenge le code
│   ├── researcher.py      # Chercheur (Medium + websearch natif ou DuckDuckGo)
│   ├── watcher.py         # Veilleur (Small) : budget tokens + détection boucles
│   └── scribe.py          # Scribe (Small) : journal.md + git
├── tools/
│   └── mistral_client.py  # Wrapper SDK + cache SQLite
├── monitoring/
│   └── metrics.py         # Prometheus (tokens, coût, contradictions)
├── journal.md             # Tenu par le scribe (auto-généré)
└── requirements.txt
```

## 🚀 Démarrage rapide

```bash
# 1. Dépendances
pip install -r requirements.txt

# 2. Configuration
cp .env.example .env
# Édite .env et mets ta MISTRAL_API_KEY

# 3. Lance avec la tâche d'exemple (validation d'IBAN)
python main.py

# 4. Tâche personnalisée
python main.py "Écris une fonction qui parse un fichier CSV et ignore les lignes malformées"

# 5. Un seul codeur (au lieu de 2 en parallèle)
python main.py --one-coder "ta tâche"
```

## 📊 Ce que tu vois à l'exécution

1. L'orchestrateur décide si une recherche web est nécessaire
2. Le chercheur va chercher (websearch natif ou DuckDuckGo) et fournit des **sources**
3. Les **2 codeurs** produisent du code en parallèle
4. Le vérificateur **challenge** le code (cherche les bugs)
5. Si problèmes critiques → relance des codeurs avec les retours (max 2 itérations)
6. Le **veilleur** affiche la consommation totale + son analyse IA
7. Le **scribe** a écrit chaque étape dans `journal.md`

## 🛡️ Le veilleur (surveillance drastique)

Configuré dans `.env` :
- `MAX_TOKENS_PER_SESSION=200000` — budget total par session
- `ALERT_THRESHOLD_PCT=70` — alerte à 70% du budget
- `MAX_AGENT_CALLS=15` — suspicion de boucle au-delà

Le veilleur :
- **Compte** chaque appel d'agent et ses tokens
- **Alerte** quand le seuil d'alerte est franchi
- **Bloque** (lève `BudgetExceeded`) quand le budget est dépassé
- **Détecte les boucles** (un agent appelé trop de fois)
- **Analyse** (via Small) les tendances et propose des optimisations

## 📝 Le scribe (mémoire du projet)

Chaque étape importante est enregistrée dans `journal.md` :
```markdown
## [2026-08-15 22:35:00] DECISION — par orchestrateur
Recherche web nécessaire pour: Écris une fonction Python qui valide un IBAN...

## [2026-08-15 22:35:02] RECHERCHE — par chercheur
Recherche: Un IBAN valide contient 15 à 34 caractères, vérifiable par modulo 97...
```

Le journal est commité avec git automatiquement.

## 🔍 Le chercheur (websearch + fallback)

1. **Websearch natif Mistral** : crée un agent avec `tools=[{"type": "web_search"}]` via `client.beta.agents.create` + `client.beta.conversations.start`. C'est la méthode officielle. Fournit les sources avec leurs URLs.
2. **Fallback DuckDuckGo** : si l'API beta échoue (clé manquante, quota, etc.), le chercheur utilise `ddgs` pour récupérer des résultats, puis les synthétise via Mistral.

Le chercheur essaie d'abord le natif, puis bascule sur le fallback automatiquement.

## 🧪 Vérifier que ça marche

Sans clé API, tu peux vérifier que tout se charge :

```bash
python -c "from orchestrator import Orchestrator; print('OK:', Orchestrator())"
```

Avec une clé API :

```bash
python main.py -v
```

## 🔗 Références

- [Docs Mistral](https://docs.mistral.ai/)
- [Agents & Conversations API](https://docs.mistral.ai/agents/agents) — `client.beta.agents` + `client.beta.conversations`
- [Websearch tool](https://docs.mistral.ai/studio-api/agents/agent-tools/websearch) — `web_search` via Conversations API
- [Models overview](https://docs.mistral.ai/getting-started/models/models_overview/)
