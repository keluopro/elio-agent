"""Le savoir d'Élio : index local, recherche par les mots et par le sens, sans modèle distant."""

from array import array
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from elio import savoir  # noqa: E402


def ecrire(chemin, texte):
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(texte, encoding="utf-8")


def tour(role, texte, jour="2026-11-10", **plus):
    return json.dumps(dict({"type": role, "timestamp": jour + "T10:00:00Z", "message": {"role": role, "content": texte}}, **plus),
                      ensure_ascii=False, separators=(",", ":"))


class Coffre(unittest.TestCase):
    """Un petit coffre jetable et sa base, sans modèle local."""

    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        racine = Path(self.dossier.name)
        self.coffre, self.transcriptions, self.memoires = racine / "Coffre", racine / "projects", racine / "memory"
        self.transcriptions.mkdir()
        self.memoires.mkdir()
        self.connexion = savoir.ouvrir(racine / "savoir.db")

    def tearDown(self):
        self.connexion.close()
        self.dossier.cleanup()

    def indexer(self, **options):
        options.setdefault("echanges", True)
        return savoir.indexer(self.connexion, coffre=self.coffre, transcriptions=self.transcriptions, memoires=self.memoires,
                              **options)

    def chercher(self, question, **options):
        options.setdefault("sens", False)
        return savoir.chercher(self.connexion, question, **options)


class MotsTests(unittest.TestCase):
    def test_les_mots_vides_et_les_accents_tombent(self):
        self.assertEqual(savoir.mots("Où est rangée la clé du serveur ?"), ["rangee", "cle", "serveur"])

    def test_la_racine_rapproche_les_formes_d_un_mot(self):
        self.assertEqual(savoir.racine("rendus"), savoir.racine("rendre"))
        self.assertEqual(savoir.racine("pel"), "pel")

    def test_une_question_sans_mot_utile_ne_cherche_rien(self):
        self.assertEqual(savoir.requete("où est-ce que ?"), "")


class DecoupageTests(unittest.TestCase):
    def test_un_passage_par_section_avec_son_fil_de_titres(self):
        texte = "---\ndate: 2026-11-01\n---\n# Projet\n\n## État\nLe serveur tourne depuis hier soir sans erreur.\n\n## Suite\nAcheter un nom de domaine avant vendredi."
        passages = savoir.decouper(texte, "Note")
        self.assertEqual([p[0] for p in passages], ["Note › Projet › État", "Note › Projet › Suite"])
        self.assertEqual(passages[0][1], 7)

    def test_une_ligne_fleuve_est_recoupee_en_phrases(self):
        ligne = "- " + " ".join("Phrase numéro {} sur l'état du chantier en cours.".format(i) for i in range(60))
        passages = savoir.decouper("# T\n" + ligne, "Note")
        self.assertGreater(len(passages), 3)
        self.assertTrue(all(len(p[2]) <= savoir.TAILLE_PASSAGE * 1.6 for p in passages))

    def test_un_titre_dans_un_bloc_de_code_n_ouvre_pas_de_section(self):
        passages = savoir.decouper("# T\nAvant le bloc de code, une phrase.\n```\n# pas un titre\n```\nAprès.", "Note")
        self.assertEqual(len(passages), 1)

    def test_la_date_vient_du_nom_puis_de_l_entete(self):
        self.assertEqual(savoir.date_de("session_2026-11-09_1431.md", "", 0), "2026-11-09")
        self.assertEqual(savoir.date_de("Note.md", "---\ndate: 2026-10-02\n---\nx", 0), "2026-10-02")
        self.assertEqual(savoir.date_de("Note.md", "---\ndate: 2026-10-02\nrevise: 2026-11-12\n---\nx", 0), "2026-11-12")


class MasquageTests(unittest.TestCase):
    def test_une_cle_est_masquee_pas_une_adresse_ni_un_chemin(self):
        cle = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2"  # fausse clé, pour le test  # pragma: allowlist secret
        self.assertNotIn(cle, savoir.masquer("jeton " + cle))
        self.assertNotIn("sk-abcdefghijklmnopqrstu", savoir.masquer("clé sk-abcdefghijklmnopqrstu"))
        adresse = "https://www.exemple.fr/produits/27-gigabyte-go27q24a-oled-abcdefghijklmnopqrstuvwxyzABCDEFG"
        chemin = "-Users-alex-Documents-Coffre"
        self.assertEqual(savoir.masquer(adresse + " " + chemin), adresse + " " + chemin)


class IndexTests(Coffre):
    def test_seul_un_fichier_change_est_relu_et_un_fichier_supprime_disparait(self):
        ecrire(self.coffre / "a.md", "# A\nLe conteneur elio a deux gigas de mémoire vive.")
        ecrire(self.coffre / "b.md", "# B\nLa sauvegarde du coffre tourne toutes les heures.")
        self.assertEqual(self.indexer(), (2, 0))
        self.assertEqual(self.indexer(), (0, 0))
        ecrire(self.coffre / "a.md", "# A\nLe conteneur elio a quatre gigas de mémoire vive, depuis ce matin.")
        self.assertEqual(self.indexer(), (1, 0))
        self.assertIn("quatre", self.chercher("conteneur elio")[0]["texte"])
        (self.coffre / "b.md").unlink()
        self.assertEqual(self.indexer(), (0, 1))
        self.assertEqual(self.chercher("sauvegarde du coffre"), [])

    def test_dossiers_caches_et_sauvegardes_restent_dehors(self):
        ecrire(self.coffre / ".obsidian" / "x.md", "# X\nRéglage interne d'Obsidian à ne pas indexer.")
        ecrire(self.coffre / "08_Archive" / "sessions" / "derniere_sauvegarde" / "y.md", "# Y\nCopie de travail à ne pas indexer.")
        self.indexer()
        self.assertEqual(self.chercher("indexer"), [])

    def test_le_projet_reserve_n_entre_jamais_dans_l_index(self):
        anciens = (savoir.RESERVE_DOSSIER, savoir.RESERVE_SUIVI, savoir.RESERVE_SESSIONS, savoir.RESERVE_MOTS)

        def remettre():
            savoir.RESERVE_DOSSIER, savoir.RESERVE_SUIVI, savoir.RESERVE_SESSIONS, savoir.RESERVE_MOTS = anciens
        self.addCleanup(remettre)
        savoir.RESERVE_DOSSIER, savoir.RESERVE_SUIVI = "Jeu_Secret", "Jeu_Secret_Context.md"
        savoir.RESERVE_SESSIONS, savoir.RESERVE_MOTS = "jeu-secret", savoir.re.compile("jeu.secret", savoir.re.I)
        ecrire(self.coffre / "04_Projets" / "Jeu_Secret" / "Histoire.md", "# Histoire\nLe twist final se passe dans la chapelle.")
        ecrire(self.coffre / "04_Projets" / "Jeu_Secret" / "Jeu_Secret_Context.md",
               "# Suivi\nJeu Secret : phase 3 terminée, serveur ouvert.")
        ecrire(self.coffre / "carnet.md", "# Jour\nNote sur le jeu secret : la chapelle cache la fin.")
        os.makedirs(self.transcriptions / "-Users-alex-Dev-jeu-secret")
        ecrire(self.transcriptions / "-Users-alex-Dev-jeu-secret" / "s.jsonl", tour("user", "écris la scène de la chapelle"))
        ecrire(self.transcriptions / "coffre" / "s.jsonl",
               tour("user", "où en est le jeu ?") + "\n" + tour("assistant", "Dans Jeu Secret, la chapelle cache la fin du jeu, " * 3))
        self.indexer()
        self.assertEqual(self.chercher("chapelle"), [])
        self.assertEqual(len(self.chercher("phase terminée serveur ouvert")), 1)

    def test_les_secrets_d_une_note_sont_masques(self):
        ecrire(self.coffre / "cles.md", "# Clés\nLa clé de test est sk-abcdefghijklmnopqrstuvwx, à révoquer.")
        self.indexer()
        self.assertIn("[secret masqué]", self.chercher("clé de test")[0]["texte"])

    def test_une_memoire_native_est_indexee_mais_pas_son_index(self):
        ecrire(self.memoires / "reference-modem.md", "---\nname: reference-modem\n---\nLe modem 4G en USB sert de connexion de secours.")
        ecrire(self.memoires / "MEMORY.md", "- [Modem](reference-modem.md) — modem 4G de secours")
        self.indexer()
        self.assertEqual([r["source"] for r in self.chercher("modem 4G")], ["memoire"])


class EchangesTests(Coffre):
    def test_on_garde_les_mots_de_l_utilisateur_et_les_reponses_pas_les_outils(self):
        lignes = [tour("user", "quel écran acheter au black friday ?"),
                  tour("assistant", [{"type": "tool_use", "name": "Bash", "input": {}}]),
                  tour("user", [{"type": "tool_result", "content": "résultat d'outil sur les écrans"}]),
                  tour("assistant", [{"type": "text", "text": "Prends le modèle OLED, c'est l'écran qui coche toutes tes cases pour ce prix, sans hésiter."}]),
                  tour("user", "<system-reminder>rappel automatique sur les écrans</system-reminder>"),
                  tour("assistant", "un écran en sous-agent, texte assez long pour passer le seuil de quatre-vingts caractères", isSidechain=True)]
        ecrire(self.transcriptions / "coffre" / "s.jsonl", "\n".join(lignes))
        self.indexer()
        resultats = self.chercher("écran", n=10)
        self.assertEqual(sorted(r["titre"] for r in resultats), ["Échange › Utilisateur", "Échange › Élio"])
        self.assertEqual(resultats[0]["jour"], "2026-11-10")

    def test_une_session_lancee_par_un_automate_n_est_pas_indexee(self):
        ecrire(self.transcriptions / "coffre" / "job.jsonl",
               tour("user", "Tu es Élio, fais la veille du matin.") + "\n" + tour("assistant", "Veille faite : rien à signaler ce matin sur les mails ni sur l'agenda, tout est calme."))
        self.indexer()
        self.assertEqual(self.chercher("veille mails agenda"), [])

    def test_sans_les_echanges_le_coffre_seul_est_indexe(self):
        ecrire(self.transcriptions / "coffre" / "s.jsonl", tour("user", "parle-moi du serveur de sauvegarde"))
        ecrire(self.coffre / "a.md", "# A\nLe serveur de sauvegarde est chez un ami.")
        self.indexer(echanges=False)
        self.assertEqual([r["source"] for r in self.chercher("serveur de sauvegarde")], ["coffre"])


class RechercheTests(Coffre):
    def test_ce_qui_fait_foi_passe_avant_ce_qui_raconte(self):
        phrase = "Le rêve d'Élio tourne chaque nuit à 4 h 30."
        ecrire(self.coffre / "04_Projets" / "Elio" / "Elio_Context.md", "# Contexte\n" + phrase)
        ecrire(self.coffre / "08_Archive" / "vieux.md", "# Archive\n" + phrase + " Version archivée.")
        self.indexer()
        self.assertEqual([r["source"] for r in self.chercher("heure du rêve la nuit")], ["coffre", "archive"])

    def test_a_texte_egal_le_plus_recent_passe_devant(self):
        ecrire(self.coffre / "2026-01-05.md", "# Note\nLe prix de l'écran est de 400 euros cette semaine.")
        ecrire(self.coffre / "2026-11-05.md", "# Note\nLe prix de l'écran est de 350 euros cette semaine.")
        self.indexer()
        resultats = self.chercher("prix de l'écran", aujourd_hui=datetime(2026, 11, 16))
        self.assertEqual(resultats[0]["jour"], "2026-11-05")

    def test_les_doublons_ne_sortent_qu_une_fois(self):
        for nom in ("a.md", "b.md"):
            ecrire(self.coffre / nom, "# Résumé\nLa migration du serveur est prévue pour décembre, après les partiels.")
        self.indexer()
        self.assertEqual(len(self.chercher("migration du serveur")), 1)

    def test_filtres_par_source_et_par_date(self):
        ecrire(self.coffre / "2026-09-01.md", "# Note\nLe serveur a été installé en septembre.")
        ecrire(self.coffre / "Conventions" / "regle.md", "# Règle\nNe jamais modifier le serveur sans sauvegarde.")
        self.indexer()
        self.assertEqual([r["source"] for r in self.chercher("serveur", sources=["regle"])], ["regle"])
        self.assertEqual(self.chercher("serveur installé", depuis="2026-10-01", sources=["coffre"]), [])

    def test_avant_une_date_on_ne_voit_que_ce_qui_la_precede(self):
        ecrire(self.coffre / "2026-10-01.md", "# Note\nLe serveur prêté a 16 Go de mémoire.")
        ecrire(self.coffre / "2026-11-10.md", "# Note\nLe serveur prêté a en réalité 80 Go de mémoire.")
        self.indexer()
        self.assertEqual([r["jour"] for r in self.chercher("mémoire du serveur prêté", avant="2026-11-10")], ["2026-10-01"])

    def test_la_reponse_tient_dans_son_budget_et_cite_fichier_et_ligne(self):
        for i in range(8):
            ecrire(self.coffre / "note{}.md".format(i), "# Sujet {}\n".format(i) + "Le serveur de calcul numéro {} chauffe. ".format(i) * 12)
        self.indexer()
        rendu = savoir.rendre(self.chercher("serveur de calcul", n=8), "serveur de calcul", budget=1200)
        self.assertLessEqual(len(rendu), 1300)
        self.assertRegex(rendu, r"note\d\.md:2")

    def test_rien_trouve_se_dit(self):
        self.indexer()
        self.assertIn("Rien dans l'index", savoir.rendre(self.chercher("licorne"), "licorne"))


class LireTests(Coffre):
    def test_un_repere_rend_le_passage_et_ses_voisins(self):
        ecrire(self.coffre / "a.md", "# Un\nPremière section du document, sur le serveur.\n\n# Deux\nDeuxième section, sur la sauvegarde.\n\n# Trois\nTroisième section, sur la vigie.")
        ecrire(self.transcriptions / "coffre" / "abcdef0123.jsonl",
               tour("user", "où en est la sauvegarde du coffre ?") + "\n" + tour("assistant", "La sauvegarde du coffre tourne toutes les heures, et la remontée a été testée des deux côtés."))
        self.indexer()
        resultat = self.chercher("deuxième section sauvegarde", sources=["coffre"])[0]
        rendu = savoir.lire(self.connexion, savoir.lieu(resultat))
        self.assertIn("Première", rendu)
        self.assertIn("Troisième", rendu)
        echange = self.chercher("remontée testée", sources=["echange"])[0]
        self.assertEqual(savoir.lieu(echange), "echange:abcdef01:2")
        self.assertIn("où en est la sauvegarde", savoir.lire(self.connexion, savoir.lieu(echange)))
        self.assertIn("Repère inconnu", savoir.lire(self.connexion, "echange:zzzz:3"))


class SensTests(Coffre):
    """Le classement par le sens, avec un faux modèle : un axe par thème."""

    THEMES = {"ram": (1, 0, 0), "mémoire vive": (1, 0, 0), "identité visuelle": (0, 1, 0), "charte": (0, 1, 0)}

    def plonger(self, textes, delai=0):
        vecteurs = []
        for texte in textes:
            v = [0.01, 0.01, 0.01]
            for mot, axe in self.THEMES.items():
                if mot in texte.lower():
                    v = [a + b for a, b in zip(v, axe)]
            norme = sum(x * x for x in v) ** 0.5
            vecteurs.append(array("f", [x / norme for x in v]))
        return vecteurs

    def test_le_sens_retrouve_ce_que_les_mots_ratent(self):
        ecrire(self.coffre / "serveur.md", "# Serveur\nLe conteneur elio n'a que 2 Go de RAM.")
        ecrire(self.coffre / "autre.md", "# Autre\nLe conteneur de stockage vive la république est plein.")
        self.indexer()
        self.assertEqual(savoir.vectoriser(self.connexion, plonger=self.plonger), 2)
        question = "combien de mémoire vive pour le conteneur"
        self.assertIn("stockage", self.chercher(question)[0]["texte"])
        self.assertIn("2 Go", self.chercher(question, sens=True, plonger=self.plonger)[0]["texte"])

    def test_un_passage_inchange_n_est_pas_recalcule(self):
        ecrire(self.coffre / "a.md", "# A\nLa charte de l'identité visuelle est rangée ici.\n\n# B\nAutre section, autre sujet, rien à voir.")
        self.indexer()
        self.assertEqual(savoir.vectoriser(self.connexion, plonger=self.plonger), 2)
        ecrire(self.coffre / "a.md", "# A\nLa charte de l'identité visuelle est rangée ici.\n\n# B\nSection réécrite ce matin, autre contenu.")
        self.indexer()
        self.assertEqual(savoir.vectoriser(self.connexion, plonger=self.plonger), 1)
        self.assertEqual(self.connexion.execute("SELECT COUNT(*) FROM vecteurs").fetchone()[0], 2)

    def test_sans_modele_local_la_recherche_par_les_mots_tient(self):
        ecrire(self.coffre / "a.md", "# A\nLa charte graphique est dans le dossier des ressources.")
        self.indexer()
        self.assertEqual(savoir.vectoriser(self.connexion, plonger=lambda textes: None), 0)
        resultats = self.chercher("charte graphique", sens=True, plonger=lambda textes, delai=0: None)
        self.assertEqual(len(resultats), 1)

    def test_changer_de_modele_recalcule_les_vecteurs(self):
        ecrire(self.coffre / "a.md", "# A\nLa charte graphique est dans le dossier des ressources.")
        self.indexer()
        savoir.vectoriser(self.connexion, plonger=self.plonger)
        ancien, savoir.SENS_MODELE = savoir.SENS_MODELE, "autre-modele"
        try:
            self.assertEqual(savoir.vectoriser(self.connexion, plonger=self.plonger), 1)
        finally:
            savoir.SENS_MODELE = ancien


class RepondreTests(Coffre):
    def test_le_modele_recoit_la_question_et_les_passages_numerotes(self):
        ecrire(self.coffre / "a.md", "# A\nLe VPS de Sam coûte 8,41 euros par mois.")
        self.indexer()
        recu = {}

        def demander(texte, niveau, systeme, delai, pour):
            recu.update(texte=texte, niveau=niveau, systeme=systeme, pour=pour)
            return "8,41 euros par mois. [1]", "ollama:essai"
        reponse, passages, nom = savoir.repondre(self.connexion, "prix du VPS de Sam", demander=demander)
        self.assertEqual((reponse, nom, len(passages)), ("8,41 euros par mois. [1]", "ollama:essai", 1))
        self.assertIn("[1] coffre", recu["texte"])
        self.assertIn("8,41", recu["texte"])
        self.assertIn("prix du VPS de Sam", recu["texte"])
        self.assertEqual((recu["niveau"], recu["pour"]), ("local", "demande"))

    def test_sans_passage_aucun_modele_n_est_appele(self):
        self.indexer()

        def demander(*a, **k):
            raise AssertionError("le modèle ne doit pas être appelé")
        self.assertEqual(savoir.repondre(self.connexion, "licorne", demander=demander),
                         ("Je ne trouve pas ça dans ma mémoire.", [], None))


class EvaluationTests(Coffre):
    def test_compte_les_questions_dont_la_reponse_ressort(self):
        ecrire(self.coffre / "a.md", "# A\nLe VPS de Sam coûte 8,41 euros par mois.")
        self.indexer()
        questions = Path(self.dossier.name) / "q.jsonl"
        questions.write_text('{"q": "prix du VPS de Sam", "attendu": ["8,41"]}\n{"q": "couleur du VPS", "attendu": ["rouge"]}\n',
                             encoding="utf-8")
        self.assertEqual(savoir.evaluer(self.connexion, questions, sens=False), (1, 2, ["couleur du VPS"]))


if __name__ == "__main__":
    unittest.main()
