"""Élio : un agent personnel dont l'identité, les règles et la mémoire sont des fichiers.

Le modèle qui les lit est une pièce qu'on change : `moteur` route chaque demande vers un niveau
(local, petit, moyen, grand), `savoir` rend les passages utiles de la mémoire, `garde` relit
les réponses avant qu'elles partent.
"""

__version__ = "0.1.0"
