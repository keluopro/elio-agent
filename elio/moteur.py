#!/usr/bin/env python3
"""Moteur : Élio demande un niveau, pas un modèle.

Élio, ce sont des fichiers et des règles ; le modèle qui les lit est une pièce qu'on change.
Ce module est le seul endroit qui sait quels modèles existent : le reste du code demande un
niveau (`local`, `petit`, `moyen`, `grand`) et `moteurs.json` dit quels moteurs le servent,
par ordre de préférence. Si le premier échoue (quota épuisé, connexion expirée, panne,
délai), le suivant prend le relais, et c'est noté. Un moteur qui vient d'échouer est laissé
au repos quelques minutes pour ne pas faire attendre chaque appel.

Trois sortes de moteurs :
- `claude:<modèle>`   la commande `claude -p`, sans outils ni hooks, hors du coffre ;
- `ollama:<modèle>`   un modèle local servi par Ollama : ni quota, ni réseau ;
- `commande:<nom>`    n'importe quelle commande décrite dans `moteurs.json` (question sur
                      l'entrée standard, réponse sur la sortie) : un autre fournisseur se
                      branche sans toucher au code.

Il sert aux demandes de texte (juger, résumer, répondre à partir de passages). Les jobs qui
ont besoin d'outils (veille, rêve) gardent leur propre lancement.

    python -m elio moteur demander --niveau petit [--systeme consigne.txt] < question.txt
    python -m elio moteur etat        quels moteurs répondent, niveau par niveau
    python -m elio moteur journal     les derniers appels : moteur, durée, relais
"""

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

ICI = Path(__file__).resolve().parents[1]
REGLAGES = Path(os.environ.get("ELIO_MOTEURS") or ICI / "moteurs.json")
DONNEES = Path(os.environ.get("ELIO_DONNEES") or Path.home() / ".local" / "share" / "elio")
JOURNAL = DONNEES / "moteur.jsonl"
REPOS = DONNEES / "moteur_repos.json"
# La commande Claude Code. Dans la version privée, un petit script l'enveloppe pour retomber sur un jeton
# longue durée quand la connexion du compte expire ; ici, la commande nue.
CLAUDE = os.environ.get("ELIO_CLAUDE", "claude")
OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434")


class Echec(Exception):
    """Un moteur n'a pas répondu ; le message dit pourquoi en quelques mots."""


def reglages(chemin=None):
    return json.loads(Path(chemin or REGLAGES).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- les moteurs

class Reponse(str):
    """Le texte rendu par un moteur, avec ce qu'il a coûté quand le moteur le dit (`usage`, `modele`)."""
    usage = None
    modele = None


def _lire_claude(sortie):
    """La sortie JSON de `claude -p` : le texte, les tokens réels et le modèle. Une sortie en texte brut passe telle quelle."""
    try:
        rendu = json.loads(sortie)
    except ValueError:
        return Reponse(sortie.strip())
    if not isinstance(rendu, dict) or not isinstance(rendu.get("result"), str):
        return Reponse(sortie.strip())
    if rendu.get("is_error"):
        raise Echec(rendu["result"].strip()[-200:] or "erreur sans message")
    reponse = Reponse(rendu["result"].strip())
    usage = rendu.get("usage")
    if isinstance(usage, dict):
        reponse.usage = {cle: usage[cle] for cle in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens", "cache_creation") if cle in usage}
    modeles = rendu.get("modelUsage")
    if isinstance(modeles, dict) and modeles:
        reponse.modele = max(modeles, key=lambda m: (modeles[m] or {}).get("outputTokens", 0)).split("[")[0]
    return reponse


def par_claude(modele, texte, systeme, delai, lancer=subprocess.run):
    commande = [str(CLAUDE), "-p", "--model", modele, "--strict-mcp-config", "--tools", "", "--no-session-persistence",
                "--settings", '{"disableAllHooks": true}', "--output-format", "json"]
    if systeme:
        commande += ["--system-prompt", systeme]
    with tempfile.TemporaryDirectory(prefix="elio-moteur-") as ailleurs:
        try:
            resultat = lancer(commande, input=texte, text=True, capture_output=True, timeout=delai,
                              env=dict(os.environ, ELIO_GARDE="1"), cwd=ailleurs)
        except subprocess.TimeoutExpired:
            raise Echec("délai dépassé")
        except OSError as erreur:
            raise Echec("commande introuvable : {}".format(erreur))
    if resultat.returncode or not resultat.stdout.strip():
        raise Echec((resultat.stderr.strip() or resultat.stdout.strip() or "réponse vide")[-200:])
    reponse = _lire_claude(resultat.stdout)
    if not reponse:
        raise Echec("réponse vide")
    return reponse


def par_ollama(modele, texte, systeme, delai, ouvrir=urllib.request.urlopen):
    messages = ([{"role": "system", "content": systeme}] if systeme else []) + [{"role": "user", "content": texte}]
    charge = json.dumps({"model": modele, "messages": messages, "stream": False, "think": False, "keep_alive": "10m",
                         "options": {"temperature": 0.2, "num_ctx": 8192}}).encode()
    try:
        with ouvrir(urllib.request.Request(OLLAMA + "/api/chat", charge, {"Content-Type": "application/json"}),
                    timeout=delai) as r:
            reponse = (json.load(r).get("message") or {}).get("content", "").strip()
    except Exception as erreur:
        raise Echec("ollama : {}".format(str(erreur)[:160]))
    if not reponse:
        raise Echec("ollama : réponse vide")
    return reponse


def par_commande(argv, texte, systeme, delai, lancer=subprocess.run):
    entree = (systeme + "\n\n---\n\n" + texte) if systeme else texte
    with tempfile.TemporaryDirectory(prefix="elio-moteur-") as ailleurs:
        try:
            resultat = lancer(list(argv), input=entree, text=True, capture_output=True, timeout=delai, cwd=ailleurs)
        except subprocess.TimeoutExpired:
            raise Echec("délai dépassé")
        except OSError as erreur:
            raise Echec("commande introuvable : {}".format(erreur))
    if resultat.returncode or not resultat.stdout.strip():
        raise Echec((resultat.stderr.strip() or "réponse vide")[-200:])
    return resultat.stdout.strip()


def appeler(nom, texte, systeme, delai, config, appels=None):
    """Un appel à un moteur nommé « sorte:modèle ». `appels` remplace les trois sortes dans les tests."""
    sorte, _, modele = nom.partition(":")
    appels = appels or {"claude": par_claude, "ollama": par_ollama, "commande": par_commande}
    if sorte not in appels:
        raise Echec("sorte de moteur inconnue : " + sorte)
    if sorte == "commande":
        argv = (config.get("commandes") or {}).get(modele)
        if not isinstance(argv, list):
            raise Echec("commande non décrite dans moteurs.json : " + modele)
        return appels[sorte](argv, texte, systeme, delai)
    return appels[sorte](modele, texte, systeme, delai)


# ---------------------------------------------------------------- repos et journal

def _lire_repos():
    try:
        return json.loads(REPOS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def au_repos(nom, maintenant=None):
    jusqu_a = _lire_repos().get(nom)
    return bool(jusqu_a) and jusqu_a > (maintenant or datetime.now()).isoformat()


def _noter_repos(nom, minutes, maintenant=None):
    etat = _lire_repos()
    if minutes:
        etat[nom] = ((maintenant or datetime.now()) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    else:
        etat.pop(nom, None)
    REPOS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    REPOS.write_text(json.dumps(etat), encoding="utf-8")


def _noter(ligne):
    JOURNAL.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as flux:
        flux.write(json.dumps(ligne, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- demander

def demander(texte, niveau="petit", systeme=None, delai=120, pour="", config=None, appels=None, maintenant=None):
    """Rend (réponse, moteur). Essaie les moteurs du niveau dans l'ordre ; lève Echec si aucun ne répond.

    `pour` nomme l'usage dans le journal (« garde », « demande »…), pour savoir plus tard ce qui consomme quoi.
    """
    config = config or reglages()
    moteurs = (config.get("niveaux") or {}).get(niveau)
    if not moteurs:
        raise Echec("niveau inconnu dans moteurs.json : " + niveau)
    disponibles = [m for m in moteurs if not au_repos(m, maintenant)] or list(moteurs)
    raisons = []
    for rang, nom in enumerate(disponibles):
        debut = time.time()
        try:
            reponse = appeler(nom, texte, systeme, delai, config, appels)
        except Echec as echec:
            raisons.append("{} : {}".format(nom, echec))
            _noter_repos(nom, config.get("repos_apres_echec_min", 15), maintenant)
            _noter({"quand": datetime.now().isoformat(timespec="seconds"), "niveau": niveau, "pour": pour, "moteur": nom,
                    "ok": False, "raison": str(echec)[:200], "duree": round(time.time() - debut, 1)})
            continue
        if au_repos(nom, maintenant):
            _noter_repos(nom, 0)
        ligne = {"quand": datetime.now().isoformat(timespec="seconds"), "niveau": niveau, "pour": pour, "moteur": nom,
                 "ok": True, "relais": rang > 0 or nom != moteurs[0], "duree": round(time.time() - debut, 1),
                 "entree": len(texte) + len(systeme or ""), "sortie": len(reponse)}
        if getattr(reponse, "usage", None):  # tokens réels, pour que quota.py compte aussi ces appels sans transcription
            ligne.update(usage=reponse.usage, modele=reponse.modele)
        _noter(ligne)
        return reponse, nom
    raise Echec("aucun moteur n'a répondu pour le niveau {} ({})".format(niveau, " ; ".join(raisons)))


def etat(config=None, appels=None):
    """Un petit appel réel à chaque moteur cité dans un niveau : qui répond, en combien de temps."""
    config = config or reglages()
    lignes, vus = [], {}
    for niveau, moteurs in (config.get("niveaux") or {}).items():
        lignes.append("{} :".format(niveau))
        for nom in moteurs:
            if nom not in vus:
                debut = time.time()
                try:
                    appeler(nom, "Réponds par le seul mot : ok", None, 60, config, appels)
                    vus[nom] = "répond ({:.1f} s)".format(time.time() - debut)
                except Echec as echec:
                    vus[nom] = "EN ÉCHEC : {}".format(str(echec)[:120])
            lignes.append("  {:<24} {}{}".format(nom, vus[nom], " — au repos" if au_repos(nom) else ""))
    return "\n".join(lignes)


def journal(n=15):
    try:
        lignes = [json.loads(l) for l in JOURNAL.read_text(encoding="utf-8").splitlines()[-n:]]
    except (OSError, ValueError):
        return "Aucun appel noté."
    return "\n".join("{} {:<6} {:<10} {:<22} {} {}".format(
        l["quand"], l["niveau"], l.get("pour", "") or "-", l["moteur"], "{:>5.1f} s".format(l["duree"]),
        ("relais" if l.get("relais") else "") if l["ok"] else "ÉCHEC : " + l.get("raison", "")[:80]) for l in lignes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sous = parser.add_subparsers(dest="action", required=True)
    p = sous.add_parser("demander", help="une question sur l'entrée standard, la réponse sur la sortie")
    p.add_argument("--niveau", default="petit")
    p.add_argument("--systeme", help="fichier de consigne")
    p.add_argument("--delai", type=int, default=120)
    p.add_argument("--pour", default="", help="nom de l'usage, pour le journal")
    sous.add_parser("etat", help="quels moteurs répondent")
    p = sous.add_parser("journal", help="derniers appels")
    p.add_argument("--n", type=int, default=15)
    args = parser.parse_args(argv)
    if args.action == "etat":
        print(etat())
        return 0
    if args.action == "journal":
        print(journal(args.n))
        return 0
    systeme = Path(args.systeme).read_text(encoding="utf-8") if args.systeme else None
    try:
        reponse, nom = demander(sys.stdin.read(), niveau=args.niveau, systeme=systeme, delai=args.delai, pour=args.pour)
    except Echec as echec:
        print(str(echec), file=sys.stderr)
        return 1
    print(reponse)
    print("[moteur : {}]".format(nom), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
