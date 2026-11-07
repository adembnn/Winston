"""Tests du moteur de recherche : phrase retrouvée avec sa seconde, adresse d'origine, accents, surlignage."""

from datetime import datetime, timezone

from enquete.recherche import Document, Index, adresse_d_origine, chercher, documents_depuis_export
from enquete.reseau import FamilleLien, Lien, Noeud, Preuve, ReseauEnquete, Role, TypeNoeud

COLLECTE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
TIKTOK = "https://www.tiktok.com/@the911onabc/video/7691766912862162206"
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def un_export():
    with ReseauEnquete("Ces vidéos de séismes sont-elles réelles ?") as r:
        compte = r.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@the911onabc (tiktok)", Role.ORIGINE, id="c1"))
        video = r.ajouter_noeud(Noeud(
            TypeNoeud.VIDEO, "Vidéo tiktok de @the911onabc", Role.ORIGINE, id="v1",
            attributs={"url": TIKTOK, "hashtags": ["#séisme"], "methode_date": "secret interne"}))
        yt = r.ajouter_noeud(Noeud(TypeNoeud.VIDEO, "Vidéo youtube", Role.AMPLIFICATEUR, id="v2",
                                   attributs={"url": YOUTUBE}))
        for cible, url, extrait, debut in ((video, TIKTOK, "911, what's your emergency?", 17),
                                           (yt, YOUTUBE, "le tremblement de terre a frappé", 95)):
            r.ajouter_lien(Lien(
                type_lien="publie", famille=FamilleLien.IDENTITE, source_id=compte.id, cible_id=cible.id,
                preuves=(Preuve(url, extrait, "Oriane", COLLECTE, debut_s=debut),),
                explication="Le compte publie la vidéo.", methode="adresse", agent="test"))
        r.poser_question("Qui a publié en premier ?")
        return r.exporter_pour_interface()


def test_phrase_retrouvee_avec_sa_seconde():
    resultats = chercher(un_export(), "emergency")
    phrase = next(x for x in resultats if x["type"] == "phrase")
    assert phrase["seconde"] == 17
    assert phrase["piece_id"] == "v1"  # la vidéo, pas le compte qui la cite
    assert phrase["url_origine"] == TIKTOK  # TikTok : jamais de seconde dans l'adresse
    assert "<mark>emergency</mark>" in phrase["extrait_surligne"]


def test_youtube_porte_la_seconde():
    phrase = next(x for x in chercher(un_export(), "tremblement") if x["type"] == "phrase")
    assert phrase["seconde"] == 95
    assert phrase["url_origine"] == YOUTUBE + "&t=95s"


def test_ignore_accents_et_casse_et_cherche_en_debut_de_mot():
    assert chercher(un_export(), "SEISME")  # « séisme » dans le hashtag
    assert any(x["type"] == "phrase" for x in chercher(un_export(), "emerg"))


def test_tous_les_mots_sont_exiges():
    assert chercher(un_export(), "emergency tremblement") == []


def test_methode_interne_non_indexee_et_requete_vide():
    assert chercher(un_export(), "secret") == []
    assert chercher(un_export(), "   ") == []
    assert chercher(un_export(), '"; DROP -- *') == []


def test_apostrophe_lisible():
    [x] = [x for x in chercher(un_export(), "emergency") if x["type"] == "phrase"]
    assert "what's your <mark>emergency</mark>?" in x["extrait_surligne"]


def test_surlignage_echappe_le_html():
    export = un_export()
    export["questions_ouvertes"].append({"id": "q", "texte": "<script>alert(1)</script> zone", "statut": "ouverte"})
    resultat = next(x for x in chercher(export, "zone") if "<script>" in x["texte"])
    assert "<script>" not in resultat["extrait_surligne"]
    assert "&lt;script&gt;" in resultat["extrait_surligne"]
    assert "<mark>zone</mark>" in resultat["extrait_surligne"]


def test_types_de_resultats():
    types = {d.type for d in documents_depuis_export(un_export())}
    assert {"compte", "video", "phrase", "lien", "question"} <= types


def test_adresse_d_origine():
    assert adresse_d_origine(YOUTUBE, 5) == YOUTUBE + "&t=5s"
    assert adresse_d_origine("https://exemple.test/page", 5) == "https://exemple.test/page"
    assert adresse_d_origine(None) is None
    assert adresse_d_origine("https://www.instagram.com/reel/Ddo1WTnR_Dn/", 9) == "https://www.instagram.com/reel/Ddo1WTnR_Dn/"


def test_extrait_court_sur_texte_long():
    long = "mot " * 200 + "aiguille " + "mot " * 200
    [x] = Index([Document("legende", long, "p")]).chercher("aiguille")
    assert "<mark>aiguille</mark>" in x["extrait_surligne"] and len(x["extrait_surligne"]) < 400
