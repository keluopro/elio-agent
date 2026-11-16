# Ollama

Serveur local de modèles. Élio l'appelle sur `http://localhost:11434`.

- `/api/chat` pour les réponses (moteur, niveau `local`).
- `/api/embed` pour les vecteurs de la recherche par le sens (`embeddinggemma`, 256 premières dimensions).
- Un modèle chargé reste en mémoire 10 minutes après le dernier appel (`keep_alive`).
