"""Tests de enquete.comprendre : aucun appel réel à Claude."""

import json
import time

import pytest

from enquete import comprendre as c
from enquete.reseau import Role


def _reponse_valide() -> dict:
    return {
        "reformulation": "Le journaliste cherche qui vend et encaisse.",
        "gabarits": ["arnaque commerciale", "désinformation santé"],
        "mots": [
            {"mot": f"mot{i}", "variantes": ["a", "b"], "champs": ["transcript", "caption"]}
            for i in range(5)
        ],
        "questions": [
            {"texte": f"Question {i} ?", "role": r.value, "pistes": ["piste 1", "piste 2"]}
            for i, r in enumerate(list(Role)[:9])
        ],
        "hypotheses": [
            {"texte": "H1", "contre_le_journaliste": False, "test_qui_prouve": "x", "test_qui_refute": "y"},
            {"texte": "H2", "contre_le_journaliste": False, "test_qui_prouve": "x", "test_qui_refute": "y"},
            {"texte": "H3", "contre_le_journaliste": True, "test_qui_prouve": "x", "test_qui_refute": "y"},
        ],
        "angle_mort": "Les petits comptes.",
    }


# --- variantes locales -------------------------------------------------------

def test_variantes_locales_mots_cles_et_variantes():
    r = c.variantes_locales("Stylos minceur : qui est derrière ?")
    mots = {m["mot"]: m for m in r["mots"]}
    assert set(mots) == {"stylos", "minceur"}  # « qui est derrière » = mots vides
    assert "stylo" in mots["stylos"]["variantes"]
    assert "#stylos" in mots["stylos"]["variantes"]
    assert mots["stylos"]["champs"] == ["transcript", "onScreenText", "caption"]
    expr = r["expressions"][0]
    assert expr["expression"] == "stylos minceur"
    assert "stylo minceur" in expr["variantes"]
    assert "#stylominceur" in expr["variantes"]
    assert r["source"] == "local"


def test_variantes_sans_accents_et_nombres_en_lettres():
    r = c.variantes_locales("Les manifestants du 17 octobre à Évry")
    mots = {m["mot"]: m["variantes"] for m in r["mots"]}
    assert "evry" in mots["évry"]
    assert "dix-sept" in mots["17"]
    assert any(e["expression"] == "17 octobre" and "dix-sept octobre" in e["variantes"] for e in r["expressions"])


@pytest.mark.parametrize("n,attendu", [(17, "dix-sept"), (21, "vingt-et-un"), (71, "soixante-et-onze"),
                                       (80, "quatre-vingts"), (96, "quatre-vingt-seize"), (100, None)])
def test_nombre_en_lettres(n, attendu):
    assert c.nombre_en_lettres(n) == attendu


def test_variantes_locales_question_vide():
    r = c.variantes_locales("")
    assert r["mots"] == [] and r["expressions"] == []


# --- graine ------------------------------------------------------------------

def test_graine_tiktok():
    g = c.lire_graine("regarde ça https://www.tiktok.com/@nora.fit/video/7696581397153024512?lang=fr")
    assert g["plateforme"] == "tiktok"
    assert g["pseudo"] == "nora.fit"
    assert g["identifiant"] == "7696581397153024512"
    assert g["date"].startswith("2026-10-14T17:46")
    assert g["marge_secondes"] == 18
    assert g["date_lisible"] == "14 oct. 2026 vers 17 h 46 UTC"
    assert g["url_origine"] == "https://www.tiktok.com/@nora.fit/video/7696581397153024512"


def test_graine_instagram_et_youtube():
    ig = c.lire_graine("https://www.instagram.com/reel/CxYz1234567/")
    assert ig["plateforme"] == "instagram" and ig["type"] == "reel"
    assert ig["url_origine"].startswith("https://www.instagram.com/reel/")
    yt = c.lire_graine("https://youtu.be/dQw4w9WgXcQ")
    assert yt["plateforme"] == "youtube" and yt["date"] is None and "date_erreur" in yt
    assert yt["url_origine"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.mark.parametrize("texte", ["", "bonjour", "https://exemple.fr/video/1", "https://www.tiktok.com/@x/video/12"])
def test_graine_non_reconnue(texte):
    assert c.lire_graine(texte) is None


# --- comprendre --------------------------------------------------------------

def test_comprendre_json_valide_et_cache(tmp_path):
    appels = []

    def faux(prompt, delai):
        appels.append(prompt)
        return "```json\n" + json.dumps(_reponse_valide(), ensure_ascii=False) + "\n```"

    r = c.comprendre("Stylos minceur : qui est derrière ?", executeur=faux, cache=tmp_path)
    assert r["source"] == "claude"
    assert 4 <= len(r["mots"]) <= 8 and 8 <= len(r["questions"]) <= 12 and 3 <= len(r["hypotheses"]) <= 4
    assert all(q["role"] in c.ROLES and len(q["pistes"]) == 2 for q in r["questions"])
    assert any(h["contre_le_journaliste"] for h in r["hypotheses"])
    assert r["gabarits"] == ["arnaque commerciale", "désinformation santé"]
    json.dumps(r)  # sérialisable
    # Même question autrement écrite : réponse du cache, pas de second appel.
    r2 = c.comprendre("stylos MINCEUR qui est derriere", executeur=faux, cache=tmp_path)
    assert r2["source"] == "cache" and len(appels) == 1


def test_prompt_sans_fait_du_banc(tmp_path):
    vu = []
    c.comprendre("Une question", executeur=lambda p, d: vu.append(p) or "{}", cache=False)
    assert "Une question" in vu[0] and "BitKelt" not in vu[0]


def test_repli_sur_erreur(tmp_path):
    def casse(prompt, delai):
        raise RuntimeError("pas connecté")

    r = c.comprendre("Stylos minceur ?", executeur=casse, cache=tmp_path)
    assert r["source"] == "repli" and "pas connecté" in r["erreur"]
    assert {m["mot"] for m in r["mots"]} == {"stylos", "minceur"}
    assert r["gabarits"] == ["libre"]
    assert list(tmp_path.iterdir()) == []  # un repli n'est pas mis en cache


def test_repli_sur_json_incomplet(tmp_path):
    r = c.comprendre("Stylos minceur ?", executeur=lambda p, d: '{"reformulation": "x"}', cache=tmp_path)
    assert r["source"] == "repli"
    r = c.comprendre("Stylos minceur ?", executeur=lambda p, d: "pas du json", cache=tmp_path)
    assert r["source"] == "repli"


def test_repli_sans_hypothese_contraire(tmp_path):
    rep = _reponse_valide()
    for h in rep["hypotheses"]:
        h["contre_le_journaliste"] = False
    r = c.comprendre("Stylos minceur ?", executeur=lambda p, d: json.dumps(rep), cache=tmp_path)
    assert r["source"] == "repli" and "donne tort" in r["erreur"]


def test_repli_sur_delai_depasse(tmp_path):
    def lent(prompt, delai):
        time.sleep(2)
        return json.dumps(_reponse_valide())

    debut = time.monotonic()
    r = c.comprendre("Stylos minceur ?", executeur=lent, delai=0.1, cache=tmp_path)
    assert time.monotonic() - debut < 1.5
    assert r["source"] == "repli" and "délai" in r["erreur"]


# --- résumé de pièce ---------------------------------------------------------

PIECE = {
    "type": "video",
    "libelle": "Vidéo de @nora.fit",
    "role": "amplificateur",
    "attributs": {"pseudo": "nora.fit", "date": "2026-10-14T17:46:40+00:00"},
    "solidite": {"fiabilite_source": "B", "credibilite_information": 2},
}


def test_resume_accepte_si_pseudo_et_date_presents():
    phrase = "Vidéo de @nora.fit du 14 oct. 2026 qui vante le produit : amplificateur, source fiable, information probablement vraie."
    assert c.resumer_piece(PIECE, "q", executeur=lambda p, d: phrase) == phrase


def test_resume_trop_long_ou_sans_pseudo_donne_le_repli():
    long = "Vidéo de @nora.fit du 14 oct. " + "mot " * 30
    r = c.resumer_piece(PIECE, "q", executeur=lambda p, d: long)
    assert len(r.split()) <= 25 and "@nora.fit" in r and "14 oct. 2026" in r
    r = c.resumer_piece(PIECE, "q", executeur=lambda p, d: "Une vidéo vante un produit le 14 oct.")
    assert "@nora.fit" in r  # repli : le résumé inventé sans pseudo est écarté


def test_resume_repli_sur_erreur_et_contient_la_solidite():
    def casse(p, d):
        raise RuntimeError("x")

    r = c.resumer_piece(PIECE, "q", executeur=casse)
    assert len(r.split()) <= 25
    assert "@nora.fit" in r and "14 oct. 2026" in r


def test_verifier_resume_refuse_un_fait_absent_de_la_piece():
    piece = {"type": "site", "libelle": "Boutique en ligne", "attributs": {}}
    assert c.verifier_resume("Boutique en ligne vue en 2024.", piece) != []
    assert c.verifier_resume("Boutique en ligne par @inconnu.", piece) != []
    assert c.verifier_resume("Boutique en ligne du dossier.", piece) == []


def test_resume_repli_toujours_25_mots_au_plus():
    piece = dict(PIECE, libelle="mot " * 60)
    assert len(c.resumer_piece(piece, "q", executeur=lambda p, d: "") .split()) <= 25
