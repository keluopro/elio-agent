"""Le moteur : un niveau demandé, des moteurs essayés dans l'ordre, les échecs notés et mis au repos."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from elio import moteur  # noqa: E402

CONFIG = {"niveaux": {"petit": ["claude:haiku", "ollama:local"], "seul": ["commande:autre"]},
          "commandes": {"autre": ["autre-outil", "--lire"]}, "repos_apres_echec_min": 15}


class Base(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.anciens = (moteur.JOURNAL, moteur.REPOS)
        moteur.JOURNAL = Path(self.dossier.name) / "moteur.jsonl"
        moteur.REPOS = Path(self.dossier.name) / "repos.json"
        self.appels_faits = []

    def tearDown(self):
        moteur.JOURNAL, moteur.REPOS = self.anciens
        self.dossier.cleanup()

    def appels(self, claude="ok claude", ollama="ok ollama", commande="ok commande"):
        def faire(sorte, reponse):
            def appel(modele, texte, systeme, delai):
                self.appels_faits.append((sorte, modele))
                if isinstance(reponse, Exception):
                    raise reponse
                return reponse
            return appel
        return {"claude": faire("claude", claude), "ollama": faire("ollama", ollama), "commande": faire("commande", commande)}

    def journal(self):
        return [json.loads(l) for l in moteur.JOURNAL.read_text(encoding="utf-8").splitlines()]


class DemanderTests(Base):
    def test_le_premier_moteur_du_niveau_repond(self):
        reponse, nom = moteur.demander("question", niveau="petit", config=CONFIG, appels=self.appels(), pour="essai")
        self.assertEqual((reponse, nom), ("ok claude", "claude:haiku"))
        self.assertEqual(self.appels_faits, [("claude", "haiku")])
        ligne = self.journal()[0]
        self.assertEqual((ligne["ok"], ligne["relais"], ligne["pour"]), (True, False, "essai"))

    def test_le_suivant_prend_le_relais_et_c_est_note(self):
        appels = self.appels(claude=moteur.Echec("limite d'usage atteinte"))
        reponse, nom = moteur.demander("question", niveau="petit", config=CONFIG, appels=appels)
        self.assertEqual((reponse, nom), ("ok ollama", "ollama:local"))
        echec, reussite = self.journal()
        self.assertEqual((echec["ok"], echec["raison"]), (False, "limite d'usage atteinte"))
        self.assertTrue(reussite["relais"])

    def test_un_moteur_en_echec_est_laisse_au_repos_puis_reessaye(self):
        maintenant = datetime(2026, 11, 16, 10, 0)
        moteur.demander("q", niveau="petit", config=CONFIG, appels=self.appels(claude=moteur.Echec("panne")), maintenant=maintenant)
        self.appels_faits.clear()
        _, nom = moteur.demander("q", niveau="petit", config=CONFIG, appels=self.appels(), maintenant=maintenant + timedelta(minutes=5))
        self.assertEqual((nom, self.appels_faits), ("ollama:local", [("ollama", "local")]))
        self.assertTrue(self.journal()[-1]["relais"])
        self.appels_faits.clear()
        _, nom = moteur.demander("q", niveau="petit", config=CONFIG, appels=self.appels(), maintenant=maintenant + timedelta(minutes=20))
        self.assertEqual(nom, "claude:haiku")

    def test_tous_au_repos_on_les_reessaie_quand_meme(self):
        maintenant = datetime(2026, 11, 16, 10, 0)
        tous_en_panne = self.appels(claude=moteur.Echec("panne"), ollama=moteur.Echec("éteint"))
        with self.assertRaises(moteur.Echec) as contexte:
            moteur.demander("q", niveau="petit", config=CONFIG, appels=tous_en_panne, maintenant=maintenant)
        self.assertIn("claude:haiku : panne", str(contexte.exception))
        self.assertIn("ollama:local : éteint", str(contexte.exception))
        _, nom = moteur.demander("q", niveau="petit", config=CONFIG, appels=self.appels(), maintenant=maintenant + timedelta(minutes=1))
        self.assertEqual(nom, "claude:haiku")

    def test_niveau_inconnu(self):
        with self.assertRaises(moteur.Echec):
            moteur.demander("q", niveau="géant", config=CONFIG, appels=self.appels())

    def test_une_commande_decrite_dans_les_reglages_sert_de_moteur(self):
        recu = {}

        def commande(argv, texte, systeme, delai):
            recu["argv"] = argv
            return "ok autre"
        reponse, nom = moteur.demander("q", niveau="seul", config=CONFIG, appels=dict(self.appels(), commande=commande))
        self.assertEqual((reponse, nom, recu["argv"]), ("ok autre", "commande:autre", ["autre-outil", "--lire"]))

    def test_une_commande_non_decrite_est_un_echec_propre(self):
        config = dict(CONFIG, niveaux={"seul": ["commande:fantome"]})
        with self.assertRaises(moteur.Echec) as contexte:
            moteur.demander("q", niveau="seul", config=config, appels=self.appels())
        self.assertIn("non décrite", str(contexte.exception))


class SortesTests(unittest.TestCase):
    def test_claude_est_lance_sans_outils_ni_hooks_hors_du_coffre(self):
        vu = {}

        def lancer(commande, **options):
            vu.update(commande=commande, options=options)
            return subprocess.CompletedProcess(commande, 0, stdout=" réponse \n", stderr="")
        self.assertEqual(moteur.par_claude("haiku", "question", "consigne", 30, lancer=lancer), "réponse")
        commande = vu["commande"]
        self.assertEqual(commande[commande.index("--model") + 1], "haiku")
        self.assertEqual(commande[commande.index("--tools") + 1], "")
        self.assertIn("disableAllHooks", commande[commande.index("--settings") + 1])
        self.assertEqual(commande[commande.index("--system-prompt") + 1], "consigne")
        self.assertEqual(vu["options"]["input"], "question")
        self.assertFalse(vu["options"]["cwd"].startswith(str(moteur.ICI)))  # lancé hors du dépôt et du coffre

    def test_claude_rend_ses_tokens_reels(self):
        sortie = json.dumps({"is_error": False, "result": " réponse \n", "usage": {"input_tokens": 2, "output_tokens": 9,
                             "cache_creation_input_tokens": 3000, "service_tier": "standard"},
                             "modelUsage": {"claude-haiku-5-5": {"outputTokens": 9}}})
        lancer = lambda c, **o: subprocess.CompletedProcess(c, 0, stdout=sortie, stderr="")
        reponse = moteur.par_claude("haiku", "q", None, 30, lancer=lancer)
        self.assertEqual(reponse, "réponse")
        self.assertEqual(reponse.usage, {"input_tokens": 2, "output_tokens": 9, "cache_creation_input_tokens": 3000})
        self.assertEqual(reponse.modele, "claude-haiku-5-5")

    def test_claude_qui_rend_une_erreur_en_json_est_un_echec(self):
        sortie = json.dumps({"is_error": True, "result": "You've hit your limit"})
        lancer = lambda c, **o: subprocess.CompletedProcess(c, 0, stdout=sortie, stderr="")
        with self.assertRaises(moteur.Echec):
            moteur.par_claude("haiku", "q", None, 30, lancer=lancer)

    def test_un_code_d_erreur_ou_une_reponse_vide_est_un_echec(self):
        rate = lambda c, **o: subprocess.CompletedProcess(c, 1, stdout="", stderr="You've hit your limit")
        vide = lambda c, **o: subprocess.CompletedProcess(c, 0, stdout="  ", stderr="")
        for lancer in (rate, vide):
            with self.assertRaises(moteur.Echec):
                moteur.par_claude("haiku", "q", None, 30, lancer=lancer)

        def lent(c, **o):
            raise subprocess.TimeoutExpired(c, 30)
        with self.assertRaises(moteur.Echec):
            moteur.par_claude("haiku", "q", None, 30, lancer=lent)

    def test_ollama_eteint_est_un_echec_pas_une_exception_brute(self):
        def ouvrir(requete, timeout=0):
            raise OSError("Connection refused")
        with self.assertRaises(moteur.Echec):
            moteur.par_ollama("local", "q", None, 5, ouvrir=ouvrir)

    def test_une_commande_recoit_consigne_et_question_sur_son_entree(self):
        vu = {}

        def lancer(argv, **options):
            vu.update(argv=argv, entree=options["input"])
            return subprocess.CompletedProcess(argv, 0, stdout="réponse", stderr="")
        self.assertEqual(moteur.par_commande(["outil", "-"], "question", "consigne", 30, lancer=lancer), "réponse")
        self.assertEqual(vu["argv"], ["outil", "-"])
        self.assertTrue(vu["entree"].startswith("consigne") and vu["entree"].endswith("question"))


class ReglagesTests(unittest.TestCase):
    def test_le_fichier_livre_est_lisible_et_coherent(self):
        config = moteur.reglages(Path(moteur.ICI) / "moteurs.json")
        self.assertTrue({"local", "petit", "moyen", "grand"} <= set(config["niveaux"]))
        for moteurs in config["niveaux"].values():
            for nom in moteurs:
                sorte, _, modele = nom.partition(":")
                self.assertIn(sorte, ("claude", "ollama", "commande"))
                self.assertTrue(modele)
                if sorte == "commande":
                    self.assertIn(modele, config["commandes"])
        self.assertFalse(any("ollama" in m for m in config["niveaux"]["grand"]))


if __name__ == "__main__":
    unittest.main()
