#!/usr/bin/env python3
"""Savoir : la mémoire d'Élio, interrogeable sans modèle et sans quota.

Un index local (SQLite FTS5, hors du coffre) de tout ce qu'Élio peut savoir : les notes
du coffre Obsidian, les journaux de session, le carnet, et, si on l'active
(ELIO_ECHANGES=1), les échanges réels entre l'utilisateur et ses agents (transcriptions
locales de Claude Code). Une question rend quelques passages courts, datés, avec leur
fichier et leur ligne, au lieu de faire relire des fichiers entiers à un modèle : dans
la version privée, 83 % des tokens consommés venaient du contexte relu à chaque appel,
pas des réponses (mesure du 16/11/2026).

Rien ici ne dépend d'un modèle ni d'un fournisseur : Python et SQLite seulement. N'importe
quel agent (Claude, Codex, Gemini, modèle local) pose la même question par la même commande.

    savoir.py chercher <question> [--n 6] [--budget 2500] [--source coffre,session] [--depuis AAAA-MM-JJ] [--long] [--json]
    savoir.py repondre <question>   réponse rédigée par un modèle local à partir des passages (aucun quota)
    savoir.py lire <repère> [--autour 1]   le passage désigné par un repère, avec ses voisins
    savoir.py indexer [--tout]      mise à jour (seuls les fichiers changés), ou reconstruction
    savoir.py etat                  ce que l'index contient
    savoir.py evaluer <questions.jsonl>   part des questions dont la réponse attendue ressort
"""

import argparse
from array import array
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata
import urllib.request

DEPOT = Path(__file__).resolve().parents[1]
# Le coffre lu par Élio : ELIO_COFFRE, sinon le coffre d'exemple livré avec le dépôt.
COFFRE = Path(os.environ.get("ELIO_COFFRE") or DEPOT / "exemple" / "coffre").expanduser().resolve()
BASE = Path(os.environ.get("ELIO_SAVOIR_DB") or Path.home() / ".local" / "share" / "elio" / "savoir.db")
TRANSCRIPTIONS = Path(os.environ.get("ELIO_TRANSCRIPTIONS") or Path.home() / ".claude" / "projects")
ECHANGES = os.environ.get("ELIO_ECHANGES", "non").lower() in ("1", "oui", "yes", "true")
MEMOIRES_NATIVES = TRANSCRIPTIONS / ("-" + str(COFFRE).strip("/").replace("/", "-").replace(" ", "-").replace("~", "-")) / "memory"
VERSION = "8"  # changer ce numéro reconstruit l'index au passage suivant

TAILLE_PASSAGE = 600
# Classement par le sens : un petit modèle d'embedding local servi par Ollama. Facultatif : sans lui (Ollama
# éteint, modèle absent, ELIO_SENS=non), la recherche reste celle des mots. Changer de modèle : changer
# ce nom, les vecteurs se recalculent seuls.
SENS_MODELE = os.environ.get("ELIO_SENS", "embeddinggemma")
SENS_URL = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434") + "/api/embed"
SENS_DIM = 256  # le modèle range l'essentiel dans les premières dimensions : 256 suffisent et se parcourent vite
# Ce qui fait foi pèse plus que ce qui raconte : une note ou un contexte avant un journal, un journal avant un échange.
POIDS = {"coffre": 1.0, "memoire": 1.0, "regle": 1.1, "carnet": 0.85, "session": 0.8, "discord": 0.7, "echange": 0.6, "archive": 0.5}
VOISINS = 60  # passages les plus proches par le sens, ajoutés aux candidats trouvés par les mots
DEMI_VIE = 120  # jours : un passage de cet âge garde la moitié de son bonus de fraîcheur
EXCLUS = ("derniere_sauvegarde", "node_modules", "Excalidraw")
# Un projet réservé (ELIO_RESERVE=Nom_Du_Dossier) n'entre jamais dans l'index : ni son dossier hors sa fiche de suivi
# (<Nom>_Context.md), ni les sessions de son dépôt, ni un passage qui le nomme. Dans la version privée, c'est un jeu
# que l'utilisateur veut découvrir en joueur : Élio y travaille sans jamais lui en révéler l'histoire.
RESERVE_DOSSIER = os.environ.get("ELIO_RESERVE", "").strip()
RESERVE_SUIVI = RESERVE_DOSSIER + "_Context.md"
RESERVE_SESSIONS = RESERVE_DOSSIER.lower().replace("_", "-")
RESERVE_MOTS = re.compile(RESERVE_DOSSIER.replace("_", ".") if RESERVE_DOSSIER else r"(?!x)x", re.I)
AUTOMATES = ("Maintenant :", "Tu es ", "Tu joues ", "Voici le paquet", "[SYSTEM", "Task ID", "# Tâche", "Caveat:", "<",
             "You have ", "Tu évalues", "Contexte :", "Demande de l'utilisateur :")
SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
                    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|AIza[A-Za-z0-9_-]{30,})")
LONGUE_CHAINE = re.compile(r"(?<![A-Za-z0-9/._=-])[A-Za-z0-9+=_-]{40,}")


def masquer(texte):
    """Retire ce qui ressemble à une clé : préfixes connus, et longues chaînes mêlant chiffres et deux casses."""
    def juger(trouve):
        chaine = trouve.group(0)
        mele = any(c.isdigit() for c in chaine) and any(c.islower() for c in chaine) and any(c.isupper() for c in chaine)
        return "[secret masqué]" if mele else chaine
    return LONGUE_CHAINE.sub(juger, SECRET.sub("[secret masqué]", texte))


VIDES = set("""a ai as au aux avec ce ces cet cette c ca dans de des du d elle en es est et eu il ils j je l la le les leur lui
m ma mais me mes moi mon n ne nos notre nous on ou où par pas pour qu que quel quelle quels quelles qui quoi s sa se ses
si son sur t ta te tes toi ton tu un une vos votre vous y à été être avoir fait faire dit dire quand comment pourquoi
est-ce the of to and is in it for on what how combien lequel laquelle deja truc chose sont était ont avait plus très tout tous toute toutes cela ça ceci dont
the""".split())
DATE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")


def plier(texte):
    """Minuscules sans accents : la forme sous laquelle on compare les mots."""
    return "".join(c for c in unicodedata.normalize("NFD", texte.lower()) if unicodedata.category(c) != "Mn")


def racine(mot):
    """Racine grossière d'un mot plié : assez pour rapprocher « rendus », « rendre » et « rendu »."""
    if len(mot) >= 8:
        return mot[:-3]
    if len(mot) >= 6:
        return mot[:-2]
    if len(mot) == 5:
        return mot[:-1]
    return mot


def mots(question):
    """Les mots utiles d'une question, pliés, sans les mots vides, dans l'ordre et sans doublon."""
    vus, liste = set(), []
    for mot in re.findall(r"[a-z0-9]+", plier(question)):
        if mot in VIDES or (len(mot) < 2 and not mot.isdigit()) or mot in vus:
            continue
        vus.add(mot)
        liste.append(mot)
    return liste


def requete(question):
    """La requête FTS5 : chaque mot par sa racine en préfixe, reliés par OU (le classement fait le tri)."""
    termes = []
    for mot in mots(question):
        r = racine(mot)
        termes.append('"{}"*'.format(r) if len(r) >= 3 and not r.isdigit() else '"{}"'.format(r))
    return " OR ".join(termes)


# ---------------------------------------------------------------- lecture des sources

def source_de(relatif):
    r = relatif.replace(os.sep, "/")
    if r.startswith("08_Archive/sessions/"):
        return "session"
    if r.startswith("08_Archive/"):
        return "archive"
    if "/Carnet/" in r or "/Reves/" in r:
        return "carnet"
    if "/Discord/" in r:
        return "discord"
    if r.startswith("Conventions/") or r in ("AGENTS.md", "ELIO.md"):
        return "regle"
    return "coffre"


def date_de(nom, texte, mtime):
    """La date d'un fichier : celle de son nom, sinon la plus récente de son en-tête, sinon sa dernière modification."""
    trouve = DATE.search(nom)
    if trouve:
        return trouve.group(0)
    if texte.startswith("---") and "\n---" in texte[3:]:
        entete = texte[:texte.find("\n---", 3)]
        dates = [m.group(0) for ligne in re.findall(r"^(?:date|revise|created|updated|modified)\s*:\s*(.+)$", entete, re.M)
                 for m in [DATE.search(ligne)] if m]
        if dates:
            return max(dates)
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def phrases(ligne, taille=None):
    """Recoupe une longue ligne en morceaux d'au plus ~taille caractères, en fin de phrase quand c'est possible."""
    taille = taille or TAILLE_PASSAGE
    morceaux, courant = [], ""
    for phrase in re.split(r"(?<=[.;!?»)])\s+(?=[A-ZÉÈÀÔÎ`\[«*(\d-])", ligne):
        while len(phrase) > taille * 1.5:
            coupe = phrase.rfind(" ", 0, taille)
            coupe = coupe if coupe > 0 else taille
            morceaux.append((courant + " " + phrase[:coupe]).strip())
            courant, phrase = "", phrase[coupe:]
        if courant and len(courant) + len(phrase) > taille:
            morceaux.append(courant.strip())
            courant = ""
        courant += (" " if courant else "") + phrase
    if courant.strip():
        morceaux.append(courant.strip())
    return morceaux


def decouper(texte, nom):
    """[(titre, ligne, passage)] : un passage par section, recoupé en fin de ligne au-delà de TAILLE_PASSAGE."""
    lignes = texte.splitlines()
    debut = 0
    if lignes and lignes[0].strip() == "---":
        for i in range(1, min(len(lignes), 60)):
            if lignes[i].strip() == "---":
                debut = i + 1
                break
    passages, titres, tampon, taille, depart, dans_code = [], [], [], 0, debut + 1, False

    def vider():
        corps = "\n".join(tampon).strip()
        if len(corps) >= 25:
            passages.append((" › ".join([nom] + [t for t in titres if t]), depart, corps))

    for numero in range(debut, len(lignes)):
        ligne = lignes[numero]
        if ligne.lstrip().startswith("```"):
            dans_code = not dans_code
        entete = None if dans_code else re.match(r"^(#{1,6})\s+(.*)$", ligne)
        if entete:
            vider()
            niveau = len(entete.group(1))
            titres = titres[:niveau - 1] + [""] * (niveau - 1 - len(titres)) + [entete.group(2).strip(" #*")]
            tampon, taille, depart = [], 0, numero + 2
            continue
        if taille >= TAILLE_PASSAGE and ligne.strip() and not dans_code:
            vider()
            tampon, taille, depart = [], 0, numero + 1
        if not tampon and not ligne.strip():
            depart = numero + 2
            continue
        if len(ligne) > TAILLE_PASSAGE * 1.5 and not dans_code:
            # une ligne-fleuve (puce d'état, paragraphe d'un seul tenant) : un passage par groupe de phrases
            vider()
            tampon, taille, depart = [], 0, numero + 1
            for morceau in phrases(ligne[:20000]):
                tampon = [morceau]
                vider()
            tampon, taille, depart = [], 0, numero + 2
            continue
        tampon.append(ligne)
        taille += len(ligne) + 1
    vider()
    return passages


def fichiers_coffre(coffre):
    for chemin in sorted(coffre.rglob("*.md")):
        relatif = chemin.relative_to(coffre)
        if any(p.startswith(".") or p in EXCLUS for p in relatif.parts):
            continue
        if RESERVE_DOSSIER and RESERVE_DOSSIER in relatif.parts and relatif.name != RESERVE_SUIVI:
            continue
        yield chemin, str(relatif)


def texte_du_message(contenu):
    if isinstance(contenu, str):
        return contenu
    if isinstance(contenu, list):
        return "\n".join(b.get("text", "") for b in contenu if isinstance(b, dict) and b.get("type") == "text")
    return ""


def lire_echanges(chemin):
    """[(titre, ligne, date, passage)] d'une transcription : ce que l'utilisateur a écrit et ce que l'agent lui a répondu.

    Les appels d'outils, leurs résultats, la réflexion et les sous-agents sont laissés de côté ; une session
    lancée par un automate (veille, rêve, juge, worker) n'est pas indexée : son produit est déjà dans le coffre.
    """
    passages, premier = [], True
    try:
        flux = open(chemin, encoding="utf-8", errors="ignore")
    except OSError:
        return passages
    with flux:
        for numero, ligne in enumerate(flux, 1):
            if '"type":"user"' not in ligne and '"type":"assistant"' not in ligne:
                continue
            try:
                entree = json.loads(ligne)
            except ValueError:
                continue
            role = entree.get("type")
            if role not in ("user", "assistant") or entree.get("isSidechain") or entree.get("isMeta"):
                continue
            texte = texte_du_message((entree.get("message") or {}).get("content")).strip()
            if not texte:
                continue
            if role == "user":
                if premier and texte.startswith(AUTOMATES):
                    return []
                premier = False
                if texte.startswith(("<", "[SYSTEM", "Caveat:")) or "<system-reminder>" in texte[:200]:
                    continue
            elif len(texte) < 80:
                continue
            if RESERVE_MOTS.search(texte):
                continue
            texte = masquer(texte)
            jour = (entree.get("timestamp") or "")[:10]
            qui = "Utilisateur" if role == "user" else "Élio"
            for i in range(0, min(len(texte), 6 * TAILLE_PASSAGE), TAILLE_PASSAGE * 2):
                passages.append(("Échange › " + qui, numero, jour, texte[i:i + TAILLE_PASSAGE * 2]))
    return passages


def fichiers_echanges(racine_transcriptions=None):
    racine_transcriptions = Path(racine_transcriptions or TRANSCRIPTIONS)
    if racine_transcriptions.is_dir():
        yield from (c for c in sorted(racine_transcriptions.glob("*/*.jsonl"))
                    if not RESERVE_SESSIONS or RESERVE_SESSIONS not in c.parent.name.lower())


# ---------------------------------------------------------------- index

def ouvrir(base=None):
    base = Path(base or BASE)
    base.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connexion = sqlite3.connect(str(base))
    try:
        os.chmod(str(base), 0o600)
    except OSError:
        pass
    connexion.execute("CREATE TABLE IF NOT EXISTS meta (cle TEXT PRIMARY KEY, valeur TEXT)")
    version = connexion.execute("SELECT valeur FROM meta WHERE cle='version'").fetchone()
    if not version or version[0] != VERSION:
        connexion.executescript("DROP TABLE IF EXISTS idx; DROP TABLE IF EXISTS fichiers;")
        connexion.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)", (VERSION,))
    connexion.execute("CREATE TABLE IF NOT EXISTS fichiers (chemin TEXT PRIMARY KEY, mtime REAL, taille INTEGER,"
                      " premier INTEGER, dernier INTEGER)")
    connexion.execute("CREATE VIRTUAL TABLE IF NOT EXISTS idx USING fts5(texte, titre, chemin UNINDEXED,"
                      " source UNINDEXED, ligne UNINDEXED, jour UNINDEXED, empreinte UNINDEXED, tokenize='unicode61 remove_diacritics 2')")
    connexion.execute("CREATE TABLE IF NOT EXISTS vecteurs (empreinte TEXT PRIMARY KEY, modele TEXT, v BLOB)")
    connexion.commit()
    return connexion


def _oublier(connexion, cle):
    ancien = connexion.execute("SELECT premier, dernier FROM fichiers WHERE chemin=?", (cle,)).fetchone()
    if ancien and ancien[0] is not None:
        connexion.execute("DELETE FROM idx WHERE rowid BETWEEN ? AND ?", ancien)


def empreinte(titre, texte):
    # une empreinte pour reconnaître un passage inchangé, pas une protection : SHA-1 suffit
    return hashlib.sha1((titre + "\n" + texte).encode("utf-8"), usedforsecurity=False).hexdigest()[:20]


def _remplacer(connexion, cle, stat, lignes):
    _oublier(connexion, cle)
    premier = dernier = None
    suivi = cle.endswith(RESERVE_SUIVI)
    for ligne in lignes:
        if not suivi and RESERVE_MOTS.search(ligne[0] + ligne[1]):
            continue
        curseur = connexion.execute("INSERT INTO idx (texte, titre, chemin, source, ligne, jour, empreinte) VALUES (?,?,?,?,?,?,?)",
                                    ligne + (empreinte(ligne[1], ligne[0]),))
        dernier = curseur.lastrowid
        premier = premier if premier is not None else dernier
    connexion.execute("INSERT OR REPLACE INTO fichiers VALUES (?,?,?,?,?)",
                      (cle, stat.st_mtime, stat.st_size, premier, dernier))


def indexer(connexion, coffre=None, transcriptions=None, memoires=None, tout=False, echanges=None):
    """Met l'index à jour : seuls les fichiers dont la date ou la taille a changé sont relus. Rend (relus, retirés)."""
    coffre = Path(coffre or COFFRE)
    echanges = ECHANGES if echanges is None else echanges
    if tout:
        connexion.executescript("DELETE FROM idx; DELETE FROM fichiers;")
    connus = {c: (m, t) for c, m, t in connexion.execute("SELECT chemin, mtime, taille FROM fichiers")}
    vus, relus = set(), 0

    def a_relire(cle, chemin):
        vus.add(cle)
        try:
            stat = chemin.stat()
        except OSError:
            return None
        return None if connus.get(cle) == (stat.st_mtime, stat.st_size) else stat

    for chemin, relatif in fichiers_coffre(coffre):
        stat = a_relire(relatif, chemin)
        if not stat:
            continue
        try:
            texte = chemin.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        source, jour = source_de(relatif), date_de(chemin.name, texte, stat.st_mtime)
        _remplacer(connexion, relatif, stat, [(masquer(corps), titre, relatif, source, ligne, jour)
                                               for titre, ligne, corps in decouper(texte, chemin.stem)])
        relus += 1
    # mémoires natives de Claude Code pour ce coffre : elles suivent le même choix que les échanges
    natives = Path(memoires) if memoires else (MEMOIRES_NATIVES if echanges else None)
    for chemin in (sorted(natives.glob("*.md")) if natives and natives.is_dir() else []):
        cle = str(chemin)
        stat = a_relire(cle, chemin)
        if not stat or chemin.name == "MEMORY.md":
            continue
        texte = chemin.read_text(encoding="utf-8", errors="ignore")
        jour = date_de(chemin.name, "", stat.st_mtime)
        _remplacer(connexion, cle, stat, [(masquer(corps), titre, cle, "memoire", ligne, jour)
                                           for titre, ligne, corps in decouper(texte, chemin.stem)])
        relus += 1
    if echanges:
        for chemin in fichiers_echanges(transcriptions):
            cle = str(chemin)
            stat = a_relire(cle, chemin)
            if not stat:
                continue
            _remplacer(connexion, cle, stat, [(corps, titre, cle, "echange", ligne, jour)
                                               for titre, ligne, jour, corps in lire_echanges(chemin)])
            relus += 1
    else:
        vus.update(c for c in connus if c.endswith(".jsonl"))
    retires = [c for c in connus if c not in vus]
    for cle in retires:
        _oublier(connexion, cle)
        connexion.execute("DELETE FROM fichiers WHERE chemin=?", (cle,))
    connexion.execute("INSERT OR REPLACE INTO meta VALUES ('indexe', ?)", (datetime.now().isoformat(timespec="seconds"),))
    connexion.commit()
    return relus, len(retires)


# ---------------------------------------------------------------- sens

def plonger(textes, delai=120, ouvrir_url=urllib.request.urlopen):
    """Les vecteurs (normés, SENS_DIM dimensions) d'une liste de textes, ou None si le modèle local ne répond pas."""
    if SENS_MODELE in ("", "non"):
        return None
    charge = json.dumps({"model": SENS_MODELE, "input": textes, "keep_alive": "30m", "truncate": True}).encode()
    try:
        with ouvrir_url(urllib.request.Request(SENS_URL, charge, {"Content-Type": "application/json"}), timeout=delai) as r:
            bruts = json.load(r)["embeddings"]
    except Exception:
        return None
    vecteurs = []
    for brut in bruts:
        court = brut[:SENS_DIM]
        norme = math.sqrt(sum(x * x for x in court)) or 1.0
        vecteurs.append(array("f", [x / norme for x in court]))
    return vecteurs


def vectoriser(connexion, limite=None, lot=48, plonger=None, suivi=None):
    """Calcule les vecteurs des passages qui n'en ont pas (ou d'un ancien modèle). Rend le nombre de passages traités."""
    plonger = plonger or globals()["plonger"]
    connexion.execute("DELETE FROM vecteurs WHERE modele != ?", (SENS_MODELE,))
    manquants = connexion.execute("SELECT empreinte, titre, texte FROM idx WHERE empreinte NOT IN (SELECT empreinte FROM vecteurs)"
                                  " GROUP BY empreinte LIMIT ?", (int(limite) if limite else -1,)).fetchall()
    if not limite:  # passage complet : on en profite pour oublier les vecteurs de passages disparus
        connexion.execute("DELETE FROM vecteurs WHERE empreinte NOT IN (SELECT empreinte FROM idx)")
    faits = 0
    for i in range(0, len(manquants), lot):
        groupe = manquants[i:i + lot]
        vecteurs = plonger(["title: {} | text: {}".format(titre or "none", texte[:1800]) for _, titre, texte in groupe])
        if not vecteurs or len(vecteurs) != len(groupe):
            break
        connexion.executemany("INSERT OR REPLACE INTO vecteurs VALUES (?,?,?)",
                              [(cle, SENS_MODELE, v.tobytes()) for (cle, _, _), v in zip(groupe, vecteurs)])
        connexion.commit()
        faits += len(groupe)
        if suivi:
            suivi(faits, len(manquants))
    return faits


def proximites(connexion, question, plonger=None):
    """{rowid: proximité de sens avec la question} pour tous les passages vectorisés ; {} sans modèle local."""
    plonger = plonger or globals()["plonger"]
    if not connexion.execute("SELECT 1 FROM vecteurs LIMIT 1").fetchone():
        return {}
    vecteurs = plonger(["task: search result | query: " + question], delai=8)
    if not vecteurs:
        return {}
    q, notes = vecteurs[0], {}
    for rowid, blob in connexion.execute("SELECT i.rowid, v.v FROM idx i JOIN vecteurs v ON v.empreinte = i.empreinte"
                                         " WHERE v.modele = ?", (SENS_MODELE,)):
        v = array("f")
        v.frombytes(blob)
        notes[rowid] = sum(map(float.__mul__, q, v))
    return notes


# ---------------------------------------------------------------- recherche

def extrait(texte, termes, taille):
    """Le morceau du passage le plus dense en mots de la question, coupé sur des mots entiers."""
    texte = re.sub(r"\s*\n\s*", " ⏎ ", texte.strip())
    if len(texte) <= taille:
        return texte
    plie = plier(texte)
    # plier garde la longueur sauf rares ligatures : on retombe sur le début si les positions divergent
    positions = [m.start() for t in termes for m in re.finditer(re.escape(racine(t)), plie)] if len(plie) == len(texte) else []
    if not positions:
        return texte[:taille].rsplit(" ", 1)[0] + " …"
    meilleur, score = 0, -1
    for p in positions:
        debut = max(0, p - taille // 4)
        compte = len({t for t in termes if racine(t) in plie[debut:debut + taille]}) * 100 + sum(
            debut <= q < debut + taille for q in positions)
        if compte > score:
            meilleur, score = debut, compte
    morceau = texte[meilleur:meilleur + taille]
    if meilleur:
        morceau = "… " + morceau.split(" ", 1)[-1]
    if meilleur + taille < len(texte):
        morceau = morceau.rsplit(" ", 1)[0] + " …"
    return morceau


def fusionner(resultats):
    """Note finale de chaque passage : ses deux notes ramenées entre 0 et 1, la meilleure comptant double.

    Un passage premier par les mots ou premier par le sens doit ressortir même s'il est quelconque dans l'autre
    classement ; un passage bon dans les deux passe devant.
    """
    if not resultats:
        return
    haut_mots = max(r["mots"] for r in resultats) or 1.0
    sens = [r["sens"] for r in resultats if r["sens"] > 0]
    bas_sens, haut_sens = (min(sens), max(sens)) if sens else (0.0, 0.0)
    for r in resultats:
        m = max(0.0, r["mots"] / haut_mots)
        s = (r["sens"] - bas_sens) / (haut_sens - bas_sens) * r["poids"] ** 0.5 if haut_sens > bas_sens and r["sens"] > 0 else 0.0
        r["score"] = (2 * max(m, s) + min(m, s)) / 3 if sens else m


def chercher(connexion, question, n=6, sources=None, depuis=None, aujourd_hui=None, sens=True, plonger=None, avant=None):
    """Les n meilleurs passages, par les mots et par le sens.

    Les mots : BM25 (titre compté triple) × poids de la source × fraîcheur × couverture des mots rares.
    Le sens : proximité avec la question selon le modèle local, quand il répond (voir `fusionner`).
    """
    fts = requete(question)
    if not fts:
        return []
    termes = mots(question)
    sql = ("SELECT texte, titre, chemin, source, ligne, jour, bm25(idx, 1.0, 3.0), rowid FROM idx WHERE idx MATCH ?"
           " ORDER BY bm25(idx, 1.0, 3.0) LIMIT 400")
    try:
        lignes = connexion.execute(sql, (fts,)).fetchall()
    except sqlite3.OperationalError:
        lignes = []
    voisins = proximites(connexion, question, plonger=plonger) if sens else {}
    connus = {l[7] for l in lignes}
    for rowid in sorted(voisins, key=voisins.get, reverse=True)[:VOISINS]:
        if rowid not in connus:
            ligne = connexion.execute("SELECT texte, titre, chemin, source, ligne, jour, 0.0, rowid FROM idx WHERE rowid=?",
                                      (rowid,)).fetchone()
            if ligne:
                lignes.append(ligne)
    aujourd_hui = aujourd_hui or datetime.now()
    plies = [plier(l[1] + " " + l[0]) for l in lignes]
    # un mot présent dans presque tous les candidats ne départage rien : il pèse peu dans la couverture
    rarete = {t: math.log(1 + len(lignes) / (1 + sum(racine(t) in p for p in plies))) for t in termes}
    total = sum(rarete.values()) or 1
    resultats = []
    for (texte, titre, chemin, source, ligne, jour, bm, rowid), plie in zip(lignes, plies):
        if sources and source not in sources:
            continue
        if depuis and (jour or "") < depuis:
            continue
        if avant and (jour or "9999") >= avant:
            continue  # ce qu'Élio savait avant ce jour-là : pour rejouer un cas passé sans connaître la suite
        try:
            age = max(0, (aujourd_hui - datetime.strptime(jour, "%Y-%m-%d")).days)
        except (TypeError, ValueError):
            age = 365
        couverture = (sum(rarete[t] for t in termes if racine(t) in plie) / total) ** 2
        contexte = POIDS.get(source, 0.6) * (1 + 0.5 * math.exp(-age / DEMI_VIE))
        resultats.append({"mots": -bm * contexte * (0.25 + couverture), "sens": voisins.get(rowid, 0.0), "poids": contexte,
                          "texte": texte, "titre": titre, "chemin": chemin, "source": source, "ligne": ligne, "jour": jour})
    fusionner(resultats)
    resultats.sort(key=lambda r: -r["score"])
    retenus, empreintes, par_fichier = [], set(), {}
    for r in resultats:
        empreinte = re.sub(r"\W+", "", plier(r["texte"]))[:160]
        if empreinte in empreintes or par_fichier.get(r["chemin"], 0) >= 2:
            continue
        empreintes.add(empreinte)
        par_fichier[r["chemin"]] = par_fichier.get(r["chemin"], 0) + 1
        retenus.append(r)
        if len(retenus) >= n:
            break
    return retenus


def lieu(r):
    """Où lire la suite : chemin du coffre, ou repère court pour ce qui vit hors du coffre (voir `lire`)."""
    if r["source"] == "echange":
        return "echange:{}:{}".format(Path(r["chemin"]).stem[:8], r["ligne"])
    if r["source"] == "memoire":
        return "memoire:{}:{}".format(Path(r["chemin"]).name, r["ligne"])
    return "{}:{}".format(r["chemin"], r["ligne"])


def lire(connexion, repere, autour=1):
    """Le passage désigné par un repère rendu par `chercher`, avec ses voisins dans le même fichier."""
    genre, _, reste = repere.partition(":")
    if genre == "echange":
        debut, _, ligne = reste.partition(":")
        motif = "%/" + debut + "%.jsonl"
    elif genre == "memoire":
        nom, _, ligne = reste.rpartition(":")
        motif = "%/memory/" + nom
    else:
        motif, _, ligne = repere.rpartition(":")
    lignes = connexion.execute("SELECT rowid, titre, texte, ligne, jour, chemin FROM idx WHERE chemin LIKE ? ORDER BY rowid",
                               (motif,)).fetchall()
    if not lignes or not ligne.isdigit():
        return "Repère inconnu : {}".format(repere)
    centre = min(range(len(lignes)), key=lambda i: (abs(int(lignes[i][3]) - int(ligne)), i))
    blocs = ["{} ({})".format(lignes[centre][5], lignes[centre][4])]
    for rowid, titre, texte, numero, jour, chemin in lignes[max(0, centre - autour):centre + autour + 1]:
        blocs.append("--- {} · ligne {}\n{}".format(titre, numero, texte))
    return "\n".join(blocs)


def rendre(resultats, question, budget=2500, long=False):
    if not resultats:
        return "Rien dans l'index pour « {} ».".format(question)
    termes, blocs, reste = mots(question), [], budget
    # les deux premiers passages ont droit à deux parts, les suivants à une
    part = max(200, (budget - 90 * len(resultats)) // (len(resultats) + min(2, len(resultats))))
    for rang, r in enumerate(resultats):
        corps = r["texte"] if long else extrait(r["texte"], termes, part * 2 if rang < 2 else part)
        bloc = "[{} · {}] {}\n  {}\n  {}".format(r["source"], r["jour"], r["titre"], lieu(r), corps)
        if not long and blocs and len(bloc) > reste:
            break
        blocs.append(bloc)
        reste -= len(bloc)
    return "\n".join(blocs)


def etat(connexion):
    lignes = ["Coffre : {}".format(COFFRE), "Index : {}".format(BASE)]
    indexe = connexion.execute("SELECT valeur FROM meta WHERE cle='indexe'").fetchone()
    lignes.append("Dernière mise à jour : {}".format(indexe[0] if indexe else "jamais"))
    for source, passages, caracteres in connexion.execute(
            "SELECT source, COUNT(*), SUM(LENGTH(texte)) FROM idx GROUP BY source ORDER BY 2 DESC"):
        lignes.append("  {:<8} {:>7} passages  {:>6.1f} Mo".format(source, passages, (caracteres or 0) / 1e6))
    lignes.append("Sens : {} passages vectorisés (modèle local {})".format(
        connexion.execute("SELECT COUNT(*) FROM vecteurs").fetchone()[0], SENS_MODELE))
    lignes.append("Fichiers suivis : {}".format(connexion.execute("SELECT COUNT(*) FROM fichiers").fetchone()[0]))
    return "\n".join(lignes)


CONSIGNE = """Tu es Élio, l'assistant personnel de l'utilisateur. Tu réponds à sa question à partir des seuls passages fournis, tirés de sa mémoire (notes, journaux, échanges passés). Chaque passage porte sa source et sa date.

Règles :
- Réponds en français, en tutoyant l'utilisateur, en trois phrases au plus, la réponse d'abord.
- N'affirme que ce que les passages disent. Si deux passages se contredisent, retiens le plus récent et dis-le.
- Si les passages ne contiennent pas la réponse, réponds exactement : « Je ne trouve pas ça dans ma mémoire. »
- Termine par les numéros des passages utilisés, entre crochets, par exemple [1][3]."""


def repondre(connexion, question, n=7, budget=5000, niveau="local", demander=None):
    """Une réponse rédigée à partir de la mémoire, par un modèle local : aucun quota. Rend (réponse, passages, moteur)."""
    if demander is None:
        from elio import moteur
        demander = moteur.demander
    passages = chercher(connexion, question, n=n)
    if not passages:
        return "Je ne trouve pas ça dans ma mémoire.", [], None
    termes, part = mots(question), max(300, budget // len(passages))
    blocs = ["[{}] {} · {} · {}\n{}".format(i, r["source"], r["jour"], r["titre"], extrait(r["texte"], termes, part))
             for i, r in enumerate(passages, 1)]
    texte = "Passages :\n\n{}\n\nQuestion : {}".format("\n\n".join(blocs), question)
    reponse, nom = demander(texte, niveau=niveau, systeme=CONSIGNE, delai=120, pour="demande")
    return reponse, passages, nom


def evaluer(connexion, fichier, n=6, budget=2500, sens=True):
    """Part des questions dont la réponse attendue figure dans ce qu'un agent lirait. Rend (trouvées, total, ratées)."""
    ratees, total = [], 0
    for ligne in Path(fichier).read_text(encoding="utf-8").splitlines():
        if not ligne.strip():
            continue
        cas = json.loads(ligne)
        total += 1
        rendu = plier(rendre(chercher(connexion, cas["q"], n=n, sens=sens), cas["q"], budget=budget))
        if not any(plier(a) in rendu for a in cas["attendu"]):
            ratees.append(cas["q"])
    return total - len(ratees), total, ratees


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sous = parser.add_subparsers(dest="action", required=True)
    p = sous.add_parser("chercher", help="passages qui répondent à une question")
    p.add_argument("question", nargs="+")
    p.add_argument("--n", type=int, default=6)
    p.add_argument("--budget", type=int, default=2500, help="taille maximale de la réponse, en caractères")
    p.add_argument("--source", help="coffre,memoire,regle,session,carnet,discord,echange,archive")
    p.add_argument("--depuis", help="AAAA-MM-JJ")
    p.add_argument("--long", action="store_true", help="passages entiers")
    p.add_argument("--json", action="store_true")
    p.add_argument("--sans-maj", action="store_true", help="ne pas rafraîchir l'index avant de chercher")
    p.add_argument("--sans-sens", action="store_true", help="les mots seuls, sans le modèle local")
    p = sous.add_parser("repondre", help="une réponse rédigée par un modèle local à partir de la mémoire (aucun quota)")
    p.add_argument("question", nargs="+")
    p.add_argument("--niveau", default="local", help="niveau de moteurs.json (local par défaut)")
    p = sous.add_parser("lire", help="un passage et ses voisins, à partir d'un repère rendu par chercher")
    p.add_argument("repere", help="chemin:ligne, echange:<id>:<ligne> ou memoire:<fichier>:<ligne>")
    p.add_argument("--autour", type=int, default=1, help="nombre de passages voisins de chaque côté")
    p = sous.add_parser("indexer", help="mettre l'index à jour")
    p.add_argument("--tout", action="store_true", help="tout reconstruire")
    sous.add_parser("etat", help="contenu de l'index")
    p = sous.add_parser("evaluer", help="mesurer la recherche sur un jeu de questions (JSONL : q, attendu[])")
    p.add_argument("fichier")
    p.add_argument("--n", type=int, default=6)
    p.add_argument("--budget", type=int, default=2500)
    p.add_argument("--sans-sens", action="store_true", help="les mots seuls, sans le modèle local")
    args = parser.parse_args(argv)
    connexion = ouvrir()
    if args.action == "indexer":
        relus, retires = indexer(connexion, tout=args.tout)
        print("{} fichiers relus, {} retirés.".format(relus, retires))
        faits = vectoriser(connexion, suivi=lambda f, t: print("  sens : {}/{}".format(f, t), end="\r", file=sys.stderr))
        print("{} passages vectorisés.".format(faits))
        print(etat(connexion))
        return 0
    if args.action == "etat":
        print(etat(connexion))
        return 0
    if args.action == "repondre":
        indexer(connexion)
        vectoriser(connexion, limite=300)
        question = " ".join(args.question)
        try:
            reponse, passages, nom = repondre(connexion, question, niveau=args.niveau)
        except Exception as erreur:  # aucun moteur : on rend au moins les passages
            print("Aucun modèle ne répond ({}). Voici ce que dit la mémoire :\n".format(str(erreur)[:120]))
            print(rendre(chercher(connexion, question), question))
            return 1
        print(reponse)
        for i, r in enumerate(passages, 1):
            if "[{}]".format(i) in reponse:
                print("  [{}] {} ({})".format(i, lieu(r), r["jour"]))
        if nom:
            print("  — répondu par {}, sans quota".format(nom) if nom.startswith("ollama") else "  — répondu par {}".format(nom))
        return 0
    if args.action == "lire":
        print(lire(connexion, args.repere, autour=args.autour))
        return 0
    if args.action == "evaluer":
        indexer(connexion)
        trouvees, total, ratees = evaluer(connexion, args.fichier, n=args.n, budget=args.budget, sens=not args.sans_sens)
        print("{}/{} questions trouvées (n={}, budget={}).".format(trouvees, total, args.n, args.budget))
        for q in ratees:
            print("  ratée : " + q)
        return 0
    if not args.sans_maj:
        indexer(connexion)
        if not args.sans_sens:
            vectoriser(connexion, limite=300)  # le reste attend le prochain `indexer`
    question = " ".join(args.question)
    resultats = chercher(connexion, question, n=args.n, sources=args.source.split(",") if args.source else None,
                         depuis=args.depuis, sens=not args.sans_sens)
    if args.json:
        print(json.dumps([dict(r, lieu=lieu(r)) for r in resultats], ensure_ascii=False, indent=1))
    else:
        print(rendre(resultats, question, budget=args.budget, long=args.long))
    return 0


if __name__ == "__main__":
    sys.exit(main())
