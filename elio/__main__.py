"""python -m elio <module> … : un seul point d'entrée pour les trois briques.

    python -m elio savoir chercher <question>    passages de la mémoire, sans modèle
    python -m elio savoir repondre <question>    réponse rédigée par un modèle local
    python -m elio moteur etat                   quels moteurs répondent
    python -m elio garde essai < reponse.txt     juger une réponse
"""

import sys

from elio import garde, moteur, savoir

MODULES = {"savoir": savoir.main, "moteur": moteur.main, "garde": garde.main}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in MODULES:
        print(__doc__.strip())
        return 0 if not argv or argv[0] in ("-h", "--help") else 2
    return MODULES[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
