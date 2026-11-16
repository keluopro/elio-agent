"""La garde : une réponse qui renvoie le travail à l'utilisateur est retenue une fois, jamais par accident."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from elio import garde  # noqa: E402

LONGUE = "J'ai préparé le script de sauvegarde. Lance-le toi-même avec la commande ci-dessous et dis-moi ce qu'il affiche."


def tour(role, contenu):
    return json.dumps({"type": role, "message": {"role": role, "content": contenu}}, ensure_ascii=False,
                      separators=(",", ":"))


class Base(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.ancien = (garde.DONNEES, garde.JOURNAL)
        garde.DONNEES = Path(self.dossier.name)
        garde.JOURNAL = garde.DONNEES / "garde.jsonl"
        self.transcription = Path(self.dossier.name) / "s.jsonl"
        self.transcription.write_text("\n".join([
            tour("user", "fais la sauvegarde du serveur"),
            tour("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "ls /srv"}}]),
        ]), encoding="utf-8")

    def tearDown(self):
        garde.DONNEES, garde.JOURNAL = self.ancien
        self.dossier.cleanup()

    def entree(self, **plus):
        return dict({"last_assistant_message": LONGUE, "transcript_path": str(self.transcription), "session_id": "abc"}, **plus)


class VerdictTests(unittest.TestCase):
    def test_un_verdict_entoure_de_texte_est_lu(self):
        verdict = garde.lire_verdict('Voici : {"renvoi": ["lancer le script"], "preuve": []} fin')
        self.assertEqual(verdict, {"renvoi": ["lancer le script"], "preuve": []})

    def test_un_verdict_illisible_ne_retient_rien(self):
        self.assertEqual(garde.lire_verdict("pas de JSON"), {"renvoi": [], "preuve": []})

    def test_le_juge_passe_par_le_niveau_petit_du_moteur(self):
        recu = {}

        def demander(texte, niveau, systeme, delai, pour):
            recu.update(texte=texte, niveau=niveau, pour=pour)
            return '{"renvoi": [], "preuve": []}', "ollama:essai"
        garde.juger(LONGUE, "fais la sauvegarde", ["Bash : ls /srv"], demander=demander)
        self.assertEqual((recu["niveau"], recu["pour"]), ("petit", "garde"))
        self.assertIn("Bash : ls /srv", recu["texte"])


class HookTests(Base):
    def test_un_renvoi_est_retenu_avec_la_raison(self):
        decision = garde.hook(self.entree(), juge=lambda r, d, o: {"renvoi": ["lancer le script"], "preuve": []})
        self.assertEqual(decision["decision"], "block")
        self.assertIn("lancer le script", decision["reason"])
        ligne = json.loads(garde.JOURNAL.read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(ligne["retenu"])

    def test_la_preuve_seule_est_notee_mais_ne_bloque_pas(self):
        self.assertIsNone(garde.hook(self.entree(), juge=lambda r, d, o: {"renvoi": [], "preuve": ["c'est fait"]}))

    def test_on_ne_retient_qu_une_fois_par_tour(self):
        juge = lambda r, d, o: {"renvoi": ["x"], "preuve": []}
        self.assertIsNone(garde.hook(self.entree(stop_hook_active=True), juge=juge))

    def test_un_job_automatique_n_est_jamais_juge(self):
        self.transcription.write_text(tour("user", "Tu es Élio, fais la veille du matin."), encoding="utf-8")

        def juge(*a):
            raise AssertionError("ne doit pas être appelé")
        self.assertIsNone(garde.hook(self.entree(), juge=juge))

    def test_un_juge_en_panne_laisse_passer(self):
        def juge(*a):
            raise RuntimeError("quota épuisé")
        self.assertIsNone(garde.hook(self.entree(), juge=juge))
        self.assertIn("quota épuisé", garde.JOURNAL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
