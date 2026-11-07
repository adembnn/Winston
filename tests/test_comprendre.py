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


# --- résumé de l'enquête ------------------------------------------------------

def _petit_export(tmp_path):
    """Un petit réseau construit ici : 3 vidéos en 1 série, une société solide et un site, un
    témoin, un expert, un mis en cause, une zone d'ombre."""
    from datetime import datetime, timezone

    from enquete.reseau import (FamilleLien, Lien, Noeud, Piste, Preuve, ReseauEnquete, Solidite,
                                TypeNoeud)
    t = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
    prv = lambda url, extrait, src="registre": Preuve(url, extrait, src, t)
    with ReseauEnquete("Qui vend ces gélules ?", str(tmp_path / "reseau.sqlite")) as r:
        for i in (1, 2, 3):
            r.ajouter_noeud(Noeud(TypeNoeud.VIDEO, f"Vidéo TikTok de @compte{i}", Role.AMPLIFICATEUR, id=f"v{i}",
                                  solidite=Solidite("E", 2)))
        r.ajouter_noeud(Noeud(TypeNoeud.GRAPPE, "Série 1", Role.INCONNU, id="g1", attributs={"videos": 3},
                              solidite=Solidite("C", 3)))
        r.ajouter_noeud(Noeud(TypeNoeud.SITE, "gelules-direct.shop", Role.INFRASTRUCTURE, id="site",
                              solidite=Solidite("B", 2)))
        r.ajouter_noeud(Noeud(TypeNoeud.SOCIETE, "Dorvane SARL", Role.BENEFICIAIRE, id="soc",
                              attributs={"creee_le": "2026-10-04"}, solidite=Solidite("A", 1)))
        r.ajouter_noeud(Noeud(TypeNoeud.PERSONNE, "Camille Fontaine", Role.MIS_EN_CAUSE, id="pers",
                              attributs={"fonction": "gérant déclaré"}, solidite=Solidite("A", 2)))
        r.ajouter_noeud(Noeud(TypeNoeud.PERSONNE, "Hélène Marchetti", Role.TEMOIN, id="tem",
                              solidite=Solidite("F", 6)))
        r.ajouter_noeud(Noeud(TypeNoeud.VIDEO, "Dr Ruben Tessier, YouTube, à 2:10", Role.EXPERT, id="exp",
                              attributs={"plateforme": "youtube"}, solidite=Solidite("E", 2)))
        for i in (1, 2, 3):
            r.ajouter_lien(Lien(type_lien="dit_phrase", famille=FamilleLien.CONTENU_DIT, source_id=f"v{i}",
                                cible_id="g1", preuves=(prv(f"https://t.exemple/{i}", "Un clic", "Oriane"),),
                                explication="Répète la phrase.", methode="phrase", agent="test"))
        r.ajouter_lien(Lien(type_lien="renvoie_vers", famille=FamilleLien.INFRASTRUCTURE, source_id="v1",
                            cible_id="site", preuves=(prv("https://t.exemple/1", "lien en bio", "Oriane"),),
                            explication="Le lien en bio mène à la boutique.", methode="url", agent="test"))
        r.ajouter_lien(Lien(type_lien="encaisse_pour", famille=FamilleLien.ARGENT, source_id="site",
                            cible_id="soc", preuves=(prv("https://registre.exemple/d", "Mentions : Dorvane"),),
                            explication="Les mentions légales citent Dorvane SARL.", methode="mentions",
                            agent="test"))
        r.poser_zone("Qui a écrit le premier message ?", "hors de portée", ["Recherche sur 3 plateformes"],
                     [Piste("Interroger l'archive du web", "archive.org", "Chercher la boutique", "facile")],
                     role_manquant=Role.ORIGINE)
        export = r.exporter_pour_interface()
    export["videos_lues"] = 90
    return export


def test_chiffres_sont_des_mesures_et_sources_independantes_est_null(tmp_path):
    ch = c.mesurer_chiffres(_petit_export(tmp_path))
    assert ch == {"videos": 90, "repetent": 3, "series": 1, "sources_independantes": None}


def test_resume_par_code_sur_un_petit_reseau(tmp_path):
    export = _petit_export(tmp_path)
    r = c.resume_enquete(export, profond=False)
    assert r["source"] == "code"
    assert r["ombre"] == [z["id"] for z in export["zones_d_ombre"]]
    ids = {p["id"] for d in export["dossiers"] for p in d["pieces"]}
    cites = ([e["piece_id"] for e in r["etabli"]] + [e["piece_id"] for e in r["chronologie"]]
             + [q["piece_id"] for q in r["qui_appeler"]])
    assert cites and set(cites) <= ids
    assert {e["piece_id"] for e in r["etabli"]} == {"site", "soc", "pers"}
    assert r["certitude"] == "probable"
    assert r["prochaines_etapes"] == ["Interroger l'archive du web", "Envoyer la sollicitation au mis en cause"]
    # témoins d'abord, mis en cause en dernier ; personne nommée nulle part
    assert [q["piece_id"] for q in r["qui_appeler"]] == ["tem", "exp", "pers"]
    texte = json.dumps(r, ensure_ascii=False)
    for nom in ("Fontaine", "Marchetti", "Tessier", "Camille", "Hélène"):
        assert nom not in texte
    assert {"role": "mis_en_cause", "nom_ou_type": "gérant déclaré"} in r["acteurs"]
    assert {"role": "beneficiaire", "nom_ou_type": "Dorvane SARL"} in r["acteurs"]
    assert [l["date"] for l in r["chronologie"]] == ["2026-10-04"]


def test_certitude_non_etabli_possible_probable():
    def export(*pieces):
        return {"dossiers": [{"pieces": list(pieces)}]}

    piece = lambda role, matiere: {"id": role + matiere, "type": "site", "libelle": "s", "role": role,
                                   "solidite": {"matiere": matiere}}
    assert c._certitude(export()) == "non établi"
    assert c._certitude(export(piece("infrastructure", "indice"))) == "possible"
    assert c._certitude(export(piece("infrastructure", "solide"))) == "possible"
    assert c._certitude(export(piece("beneficiaire", "solide"))) == "probable"
    assert c._certitude(export(piece("mis_en_cause", "solide"))) == "probable"


def _phrases_valides():
    return {"en_bref": [
        {"texte": "90 vidéos lues, dont 3 répètent la même phrase en 1 séries distinctes.", "pieces": ["g1"]},
        {"texte": "Selon le registre, Dorvane SARL est liée à la boutique.", "pieces": ["soc", "site"]}],
        "mecanisme": [{"texte": "Des vidéos mènent à une boutique rattachée à une société.",
                       "pieces": ["v1", "site"]}]}


def test_claude_valide_est_garde_et_le_cache_evite_un_second_appel(tmp_path, monkeypatch):
    monkeypatch.setenv("ENQUETE_CACHE", str(tmp_path / "cache"))
    export = _petit_export(tmp_path)
    appels = []

    def faux(prompt, delai):
        appels.append(prompt)
        return json.dumps(_phrases_valides())

    r1 = c.rediger_resume(export, executeur=faux)
    assert r1["source"] == "claude" and r1["en_bref"].startswith("90 vidéos lues")
    assert "Fontaine" not in appels[0] and "Marchetti" not in appels[0] and "Tessier" not in appels[0]
    r2 = c.rediger_resume(export, executeur=faux)
    assert r2 == r1 and len(appels) == 1


@pytest.mark.parametrize("modif", [
    lambda p: p["en_bref"][0].update(pieces=["inconnu"]),            # piece_id inconnu
    lambda p: p["en_bref"][0].update(pieces=[]),                     # aucune citation
    lambda p: p["en_bref"][0].update(texte="214 vidéos lues, 3 répètent."),    # nombre inventé
    lambda p: p["en_bref"][0].update(texte="Quarante vidéos se répètent."),  # nombre en lettres inventé
    lambda p: p["mecanisme"][0].update(texte="Une arnaque bien rodée."),        # mot interdit
    lambda p: p["en_bref"][0].update(texte="Camille Fontaine dirige la société."),  # personne nommée
    lambda p: p["en_bref"][0].update(texte="5 sources indépendantes le disent."),
    lambda p: p.update(en_bref=p["en_bref"][:1]),                    # une seule phrase
])
def test_reponse_rejetee_repli_par_code(tmp_path, monkeypatch, modif):
    monkeypatch.setenv("ENQUETE_CACHE", str(tmp_path / "cache"))
    export = _petit_export(tmp_path)
    mauvais = _phrases_valides()
    modif(mauvais)
    r = c.rediger_resume(export, executeur=lambda p, d: json.dumps(mauvais))
    assert r["source"] == "code"
    assert not list((tmp_path / "cache").glob("resume-*.json")), "une réponse rejetée n'entre pas au cache"


def test_claude_muet_repli_par_code(tmp_path, monkeypatch):
    monkeypatch.setenv("ENQUETE_CACHE", str(tmp_path / "cache"))

    def casse(prompt, delai):
        raise c.ErreurClaude("pas de réponse")

    assert c.rediger_resume(_petit_export(tmp_path), executeur=casse)["source"] == "code"


def test_executeur_claude_passe_la_consigne_par_l_entree_standard(monkeypatch):
    """La consigne d'une grosse enquête dépasse la ligne de commande Windows (32 767 caractères) :
    elle passe par l'entrée standard, jamais dans les arguments."""
    from enquete import comprendre

    vus = {}

    class Fini:
        stdout = '{"result": "ok", "is_error": false}'
        stderr = ""

    def faux_run(cmd, **options):
        vus["cmd"], vus["input"] = cmd, options.get("input")
        return Fini()

    monkeypatch.setattr(comprendre.subprocess, "run", faux_run)
    longue = "x" * 40000
    assert comprendre.executeur_claude()(longue, 5) == "ok"
    assert vus["input"] == longue
    assert all(len(argument) < 200 for argument in vus["cmd"])
