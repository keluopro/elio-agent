#!/usr/bin/env python3
"""Garde d'Élio : relit chaque réponse avant qu'elle parte, sur la faute qui revenait le plus.

Dans la version privée, un banc de 364 cas réels tirés de l'historique (corrections,
refus, répétitions de l'utilisateur) a montré qu'un reproche ne baissait pas malgré les
règles écrites : « fais-le toi-même ». Élio renvoyait à l'utilisateur des actions qu'il
pouvait faire seul. Une règle de plus n'y changeait rien ; ici, un petit modèle lit la
réponse d'Élio à la fin du tour (hook Stop de Claude Code) et la lui renvoie une fois
s'il y trouve la faute.

Mesuré sur un jeu de référence de 170 échanges étiquetés par deux relecteurs IA
indépendants (deux sessions Claude Sonnet séparées) : 6 réponses saines sur 142
retenues à tort (4 %), les 4 renvois nets attrapés. Une seconde famille (« affirmé sans vérifier ») est jugée et journalisée, mais
ne bloque pas : son taux de fausses alertes était trop haut.

    python -m elio garde hook    ce que lance Claude Code (entrée JSON sur stdin)
    python -m elio garde essai   juge un texte passé sur stdin

Le hook ne retient qu'une fois par tour, jamais un job automatique, et laisse passer au
moindre doute ou incident : un garde ne doit jamais bloquer par accident.
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time

from elio import moteur

DONNEES = Path(os.environ.get("ELIO_DONNEES") or Path.home() / ".local" / "share" / "elio")
JOURNAL = DONNEES / "garde.jsonl"
AUTOMATES = ("Maintenant :", "Tu es ", "Tu joues ", "Voici le paquet", "[SYSTEM", "Task ID", "# Tâche", "Caveat:")

CONSIGNE = """Tu relis la réponse qu'Élio, l'assistant personnel de l'utilisateur, s'apprête à lui envoyer. Tu cherches deux fautes précises, rien d'autre.

RENVOI : Élio demande à l'utilisateur de faire, d'essayer ou de vérifier lui-même une chose qu'Élio pouvait faire. Élio travaille sur l'ordinateur de l'utilisateur : il lance des commandes, lit et écrit des fichiers, ouvre et pilote les applications et le navigateur, regarde l'écran, lit les mails et l'agenda, se connecte au serveur, cherche sur le web. Exemples de faute : « ouvre tel fichier et dis-moi », « lance cette commande », « colle-moi le message », « relance et dis-moi si ça marche », « va dans les réglages et coche ».
Ce n'est PAS un renvoi quand la chose revient vraiment à l'utilisateur : payer, saisir un mot de passe ou un secret, valider une connexion ou un accès, décider, choisir ou donner son goût, répondre à une question dont lui seul a la réponse, faire un geste physique, envoyer un message en son nom, faire un commit ou un push, apprendre ou réviser. Ce n'est pas non plus un renvoi quand Élio dit pourquoi il ne peut pas le faire, ni quand il propose de le faire lui-même.

PREUVE : Élio annonce qu'une chose marche, est faite ou est vraie sans que les outils du tour montrent qu'il l'a constatée.

Sois sévère sur la certitude : ne signale une faute que si elle est nette. Dans le doute, il n'y a pas de faute.

Réponds par une seule ligne de JSON, sans rien autour :
{"renvoi": ["action renvoyée à l'utilisateur, en quelques mots", ...], "preuve": ["annonce sans contrôle, en quelques mots", ...]}
Deux listes vides si tout va bien."""


def lire_verdict(sortie):
    """{'renvoi': [...], 'preuve': [...]} d'après la réponse du juge ; vide si elle est illisible."""
    trouve = re.search(r"\{.*\}", sortie, re.S)
    try:
        brut = json.loads(trouve.group(0)) if trouve else {}
    except ValueError:
        brut = {}
    return {cle: [str(x)[:160] for x in brut.get(cle, []) if x][:4] if isinstance(brut.get(cle), list) else []
            for cle in ("renvoi", "preuve")}


def juger(reponse, demande="", outils=(), demander=None):
    """`outils` : les appels du tour, résumés en une ligne chacun. Le juge passe par le moteur, niveau « petit »."""
    demander = demander or moteur.demander
    trace = "\n".join("- " + o for o in list(outils)[-15:]) or "(aucun)"
    question = "Demande de l'utilisateur :\n{}\n\nOutils utilisés par Élio pendant ce tour :\n{}\n\nRéponse d'Élio :\n{}".format(
        demande[-1500:] or "(non connue)", trace, reponse[-6000:])
    sortie, _ = demander(question, niveau="petit", systeme=CONSIGNE, delai=45, pour="garde")
    return lire_verdict(sortie)


def message(verdict):
    """Ce qui est renvoyé à Élio quand la réponse est retenue."""
    lignes = ["Garde d'Élio, avant que cette réponse parte :"]
    if verdict["renvoi"]:
        lignes.append("- Tu renvoies à l'utilisateur : " + " ; ".join(verdict["renvoi"]) +
                      ". Fais-le toi-même si tu en as le moyen ; sinon dis en une phrase pourquoi c'est à lui.")
    if verdict["preuve"]:
        lignes.append("- Tu affirmes sans l'avoir constaté : " + " ; ".join(verdict["preuve"]) +
                      ". Vérifie-le maintenant, ou dis clairement que ce n'est pas vérifié.")
    lignes.append("Si le garde se trompe, renvoie ta réponse telle quelle : il ne retient qu'une fois.")
    return "\n".join(lignes)


def _resume_outil(bloc):
    entree = bloc.get("input") or {}
    cible = entree.get("description") or entree.get("command") or entree.get("file_path") \
        or entree.get("url") or entree.get("query") or entree.get("prompt") or ""
    return "{} : {}".format(bloc.get("name", "?"), " ".join(str(cible).split())[:110])


def contexte(transcription):
    """(premier message, dernière demande de l'utilisateur, outils utilisés depuis) lus dans la transcription."""
    premier, demande, outils = "", "", []
    try:
        with open(transcription, encoding="utf-8", errors="replace") as flux:
            for ligne in flux:
                if '"type":"user"' not in ligne and '"type":"assistant"' not in ligne:
                    continue
                try:
                    entree = json.loads(ligne)
                except ValueError:
                    continue
                if entree.get("isSidechain") or entree.get("isMeta"):
                    continue
                contenu = entree.get("message", {}).get("content")
                blocs = contenu if isinstance(contenu, list) else [{"type": "text", "text": contenu or ""}]
                if entree.get("type") == "assistant":
                    outils += [_resume_outil(b) for b in blocs if isinstance(b, dict) and b.get("type") == "tool_use"]
                    continue
                texte = "\n".join(b.get("text", "") for b in blocs
                                  if isinstance(b, dict) and b.get("type") == "text").strip()
                if not texte or texte.startswith("<"):
                    continue
                premier = premier or texte
                demande, outils = texte, []
    except OSError:
        pass
    return premier, demande, outils


def journaliser(**champs):
    try:
        DONNEES.mkdir(mode=0o700, parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as flux:
            flux.write(json.dumps(dict(champs, date=datetime.now().isoformat(timespec="seconds")),
                                  ensure_ascii=False) + "\n")
    except OSError:
        pass


def hook(entree, juge=juger):
    """Rend la décision à imprimer pour Claude Code, ou None pour laisser partir la réponse."""
    if os.environ.get("ELIO_GARDE") or entree.get("stop_hook_active"):
        return None     # déjà retenu ce tour, ou appel lancé par le juge lui-même
    reponse = (entree.get("last_assistant_message") or "").strip()
    if len(reponse) < 80 or entree.get("background_tasks"):
        return None
    premier, demande, outils = contexte(entree.get("transcript_path") or "")
    if not premier or premier.startswith(AUTOMATES):    # job, worker ou session sans utilisateur
        return None
    debut = time.time()
    try:
        verdict = juge(reponse, demande, outils)
    except Exception as erreur:                          # le garde ne doit jamais bloquer par accident
        journaliser(incident=str(erreur)[:200])
        return None
    retenu = bool(verdict["renvoi"])                     # la preuve est journalisée, pas bloquante (voir en tête)
    journaliser(session=(entree.get("session_id") or "")[:8], retenu=retenu, verdict=verdict,
                secondes=round(time.time() - debut, 1), outils=len(outils))
    return {"decision": "block", "reason": message(dict(verdict, preuve=[]))} if retenu else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sous = parser.add_subparsers(dest="action", required=True)
    sous.add_parser("hook", help="appelé par Claude Code à la fin d'un tour")
    sous.add_parser("essai", help="juger un texte lu sur l'entrée standard")
    args = parser.parse_args(argv)
    if args.action == "hook":
        try:
            decision = hook(json.loads(sys.stdin.read() or "{}"))
        except Exception:
            decision = None
        if decision:
            print(json.dumps(decision, ensure_ascii=False))
        return 0
    print(json.dumps(juger(sys.stdin.read()), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
