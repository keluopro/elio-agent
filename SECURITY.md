# Sécurité

Élio lit des notes personnelles et les donne à des modèles. Voici ce que le code garantit, ce qu'il ne garantit pas, et comment signaler un problème.

## Ce que le code garantit

- **Aucun secret dans le dépôt.** Les identifiants Claude restent ceux de votre propre installation de Claude Code ; le dépôt n'en lit ni n'en stocke aucun.
- **Les modèles n'ont aucun outil.** `moteur` lance Claude avec `--tools ""`, `--strict-mcp-config`, les hooks désactivés et un dossier de travail temporaire, hors du coffre ; Ollama ne reçoit que du texte. Une note piégée peut fausser une réponse, pas lancer une commande ni écrire un fichier.
- **Pas de shell.** Tous les sous-processus sont lancés avec une liste d'arguments, jamais `shell=True`.
- **SQL paramétré.** Aucune requête n'est construite en concaténant une entrée.
- **Secrets masqués avant l'index.** Ce qui ressemble à une clé d'API ou à un jeton est remplacé avant d'entrer dans `savoir.db` ; un dossier déclaré dans `ELIO_RESERVE` n'y entre jamais.
- **Données privées au disque.** L'index, les journaux et les états vivent hors du coffre, dans `~/.local/share/elio` (dossier `0700`, base `0600`).
- **Transcriptions désactivées par défaut.** Les échanges Claude Code ne sont indexés que si `ELIO_ECHANGES=1`.

## Ce qu'il ne garantit pas

- **La garde n'est pas une barrière de sécurité.** Elle laisse passer au moindre incident, par conception : son rôle est de relire la qualité d'une réponse, pas de bloquer une attaque. Une injection dans une note peut au pire la faire taire.
- **Un moteur distant voit le texte qu'on lui envoie.** Les niveaux servis par Claude envoient la question et les passages trouvés à l'API d'Anthropic ; pour qu'un sujet reste sur la machine, servez son niveau uniquement par Ollama dans `moteurs.json`.
- **`OLLAMA_HOST_URL` en `http://` vers une autre machine transmet en clair.** Gardez Ollama en local, ou passez par un tunnel chiffré.
- **Le masquage des secrets repose sur des motifs.** Il attrape les formats courants, pas tout ; ne gardez pas de secrets en clair dans un coffre indexé.

## Signaler une faille

Ouvrez une [alerte de sécurité privée](../../security/advisories/new) sur ce dépôt plutôt qu'une issue publique.
