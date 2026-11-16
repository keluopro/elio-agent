# Architecture d'Élio — les choix et leurs raisons

## Des fichiers d'abord, un modèle ensuite

Élio n'est pas un prompt système caché dans un service : c'est un coffre Obsidian que n'importe quel agent lit au démarrage. Trois fichiers suffisent à le faire exister :

| Fichier | Rôle |
|---|---|
| `AGENTS.md` | ce que tout agent doit savoir : interdictions, portée, où chercher |
| `ELIO.md` | l'identité et la voix |
| `Conventions/comportement.md` | les règles nées des corrections de l'utilisateur, datées |

Le démarrage est volontairement court : un agent lit ces fichiers et la mémoire courte, rien d'autre. Le reste se cherche à la demande (`savoir chercher`). Dans la version privée, ce choix a divisé le contexte de démarrage et fait du contexte relu, et non des réponses, le premier poste de consommation à attaquer (83 % des tokens mesurés le 16/11/2026).

## Savoir : pourquoi SQLite et pas une base vectorielle

- **Zéro dépendance** : la bibliothèque standard de Python embarque SQLite et FTS5. Rien à installer, rien à faire tourner.
- **Les mots d'abord** : dans un coffre personnel, la plupart des questions contiennent un nom propre, une date, un nom de fichier. BM25 les trouve mieux qu'un embedding.
- **Le sens ensuite** : un petit modèle local (`embeddinggemma`, 256 dimensions) rattrape les questions formulées autrement que la note (« mémoire vive » face à « RAM »). Il est facultatif : sans Ollama, la recherche par les mots fonctionne seule.
- **Fusion** : chaque note est ramenée entre 0 et 1, et la meilleure des deux compte double. Un passage premier par les mots *ou* par le sens doit pouvoir ressortir.
- **Poids des sources** : `regle` 1,1 · `coffre` et `memoire` 1,0 · `carnet` 0,85 · `session` 0,8 · `discord` 0,7 · `echange` 0,6 · `archive` 0,5. Ce qui fait foi passe avant ce qui raconte.
- **Fraîcheur** : un bonus qui décroît avec une demi-vie de 120 jours ; à texte égal, la note récente gagne.
- **Couverture** : un passage qui contient les mots *rares* de la question gagne plus qu'un passage qui répète les mots communs.
- **Budget** : la sortie tient dans un nombre de caractères fixé (2 500 par défaut), avec l'extrait le plus dense en mots de la question.

La source d'un passage se déduit de son chemin, selon la structure du coffre :

| Chemin | Source |
|---|---|
| `AGENTS.md`, `ELIO.md`, `Conventions/` | `regle` |
| `08_Archive/sessions/` | `session` |
| `08_Archive/` | `archive` |
| `…/Carnet/`, `…/Reves/` | `carnet` |
| `…/Discord/` | `discord` |
| transcriptions Claude Code (`ELIO_ECHANGES=1`) | `echange` |
| mémoires natives de Claude Code | `memoire` |
| tout le reste | `coffre` |

## Moteur : demander un niveau, jamais un modèle

Le reste du code ne connaît aucun nom de modèle. Les conséquences :

- changer de fournisseur, de modèle ou de prix se fait dans `moteurs.json` ;
- une panne ou un quota épuisé ne casse rien : le niveau passe au moteur suivant, et c'est journalisé (`relais`) ;
- un moteur en échec est mis au repos 15 minutes, pour que chaque appel ne paie pas le même délai ;
- si tous les moteurs sont au repos, on les réessaie quand même : mieux vaut une réponse lente qu'aucune.

Les appels à Claude passent par `claude -p` avec `--tools ""`, `--strict-mcp-config`, `disableAllHooks` et un dossier de travail temporaire : une demande de texte n'a accès ni aux fichiers, ni aux outils, ni aux hooks de l'utilisateur. Le juge de la garde ne peut donc pas se déclencher lui-même.

## Garde : mesurer avant de bloquer

La garde est un hook *Stop* : Claude Code lui passe la réponse et la transcription, elle rend `{"decision": "block", "reason": …}` ou rien.

Règles de sûreté, testées :

1. une seule retenue par tour (`stop_hook_active`) ;
2. jamais sur un job automatique (premier message reconnu) ni sur un appel du juge (`ELIO_GARDE`) ;
3. au moindre incident (quota, délai, JSON illisible), la réponse part.

Seule la famille « renvoi » bloque, parce qu'elle a été mesurée : 4 % de fausses alertes sur 142 réponses saines, 4 renvois nets attrapés sur 4, sur un jeu de 170 échanges étiquetés par deux relecteurs IA indépendants (deux sessions Claude Sonnet séparées). La famille « preuve » est journalisée pour être mesurée à son tour.

## Local et cloud

| Tâche | Où | Pourquoi |
|---|---|---|
| index, recherche, vecteurs | local | données personnelles, appel à chaque question |
| réponse à partir de la mémoire | local (`gemma4:12b`) | aucun quota, aucune donnée qui sort |
| juger, trier | `petit` (Haiku, repli local) | rapide et peu coûteux |
| travail avec outils, code, raisonnement long | Claude Code (Sonnet, Opus) | capacité de raisonnement et outils |

L'objectif est de déplacer cette frontière vers le local à mesure que le matériel le permet : des modèles plus grands en local, c'est plus de niveaux servis sans réseau.
