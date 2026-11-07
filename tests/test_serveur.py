"""Tests du serveur local : routes, flux SSE, rejeu au bon rythme, recherche, questions."""

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from enquete.reseau import FamilleLien, Lien, Noeud, Preuve, ReseauEnquete, Role, TypeNoeud
from enquete.serveur import Serveur, lire_evenements

COLLECTE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
TIKTOK = "https://www.tiktok.com/@the911onabc/video/7691766912862162206"
QUESTION = "Ces vidéos de séismes sont-elles réelles ?"


def remplir(reseau: ReseauEnquete) -> None:
    compte = reseau.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@the911onabc (tiktok)", Role.ORIGINE, id="c1"))
    video = reseau.ajouter_noeud(Noeud(TypeNoeud.VIDEO, "Vidéo tiktok", Role.ORIGINE, id="v1",
                                       attributs={"url": TIKTOK}))
    reseau.ajouter_lien(Lien(
        type_lien="publie", famille=FamilleLien.IDENTITE, source_id=compte.id, cible_id=video.id,
        preuves=(Preuve(TIKTOK, "911, what's your emergency?", "Oriane", COLLECTE, debut_s=17),
                 Preuve(TIKTOK, "une légende", "Oriane", COLLECTE)),
        explication="Le compte publie la vidéo.", methode="adresse", agent="test"))
    reseau.poser_question("Qui a publié en premier ?")


def etaler(chemin: Path, ecart_s: float) -> None:
    """Copie de test : les lignes du journal sont espacées de `ecart_s` secondes (on retire
    les gardes d'ajout seul, dans la copie seulement)."""
    bd = sqlite3.connect(chemin)
    for garde in ("evenements_pas_de_modification", "evenements_pas_d_effacement"):
        bd.execute(f"DROP TRIGGER {garde}")
    for (seq,) in bd.execute("SELECT seq FROM evenements").fetchall():
        bd.execute("UPDATE evenements SET date = ? WHERE seq = ?",
                   ((COLLECTE + timedelta(seconds=seq * ecart_s)).isoformat(), seq))
    bd.commit()
    bd.close()


@pytest.fixture
def racine(tmp_path):
    dossier = tmp_path / "affaire"
    dossier.mkdir()
    with ReseauEnquete(QUESTION, str(dossier / "reseau.sqlite")) as r:
        remplir(r)
    etaler(dossier / "reseau.sqlite", 1.0)  # 5 lignes d'1 s d'écart : 4 s de journal
    return tmp_path


@pytest.fixture
def serveur(racine):
    with Serveur(racine, port=0, pas_sondage=0.02) as s:
        yield s


def get(serveur, chemin, entetes=None):
    requete = urllib.request.Request(serveur.url + chemin, headers=entetes or {})
    with urllib.request.urlopen(requete, timeout=10) as r:
        return r.status, dict(r.headers), r.read()


def get_json(serveur, chemin):
    return json.loads(get(serveur, chemin)[2])


def post(serveur, chemin, corps, type_contenu="application/json", entetes=None):
    requete = urllib.request.Request(
        serveur.url + chemin, data=json.dumps(corps).encode(), method="POST",
        headers={"Content-Type": type_contenu, **(entetes or {})})
    try:
        with urllib.request.urlopen(requete, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def flux(serveur, chemin, entetes=None):
    """Lit un flux SSE jusqu'à sa fin ; rend [(instant, événement)]."""
    requete = urllib.request.Request(serveur.url + chemin, headers=entetes or {})
    t0 = time.monotonic()
    sortie = []
    with urllib.request.urlopen(requete, timeout=30) as r:
        for ligne in r:
            ligne = ligne.decode("utf-8")
            if ligne.startswith("data: "):
                sortie.append((time.monotonic() - t0, json.loads(ligne[6:])))
    return sortie


def test_sante_et_liste(serveur):
    assert get_json(serveur, "/api/sante") == {"ok": True}
    assert get_json(serveur, "/api/enquetes")["enquetes"][0]["id"] == "affaire"


def test_export(serveur):
    export = get_json(serveur, "/api/export?enquete=affaire")
    assert export["question"] == QUESTION
    assert export["mode"] == "demo"
    assert export["compteurs"]["pieces"] == 2 and export["compteurs"]["liens"] == 1
    assert export["compteurs"]["zones_d_ombre"] == 1


def test_erreurs(serveur):
    for chemin, statut in (("/api/export", 400), ("/api/export?enquete=../x", 400),
                           ("/api/export?enquete=inconnue", 404), ("/api/nimporte", 404)):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(serveur, chemin)
        assert e.value.code == statut


def test_cors(serveur):
    _, entetes, _ = get(serveur, "/api/sante", {"Origin": "http://localhost:5173"})
    assert entetes["Access-Control-Allow-Origin"] == "http://localhost:5173"
    _, entetes, _ = get(serveur, "/api/sante", {"Origin": "http://127.0.0.1:3000"})
    assert entetes["Access-Control-Allow-Origin"] == "http://127.0.0.1:3000"
    with pytest.raises(urllib.error.HTTPError) as e:
        get(serveur, "/api/sante", {"Origin": "https://pirate.example"})
    assert e.value.code == 403
    requete = urllib.request.Request(serveur.url + "/api/questions", method="OPTIONS",
                                     headers={"Origin": "http://localhost:5173"})
    with urllib.request.urlopen(requete) as r:
        assert r.status == 204 and "POST" in r.headers["Access-Control-Allow-Methods"]


def test_evenements_traduits_et_ordonnes(serveur):
    evenements = [e for _, e in flux(serveur, "/api/evenements?enquete=affaire")]
    assert [e["n"] for e in evenements] == list(range(1, len(evenements) + 1))
    types = [e["type"] for e in evenements]
    assert types[0] == "question" and types[-1] == "fin"
    assert types.count("noeud") == 2 and types.count("lien") == 1
    assert types.count("preuve") == 2  # une par preuve distincte
    assert "compteur" in types
    for e in evenements:
        assert set(e) == {"n", "horodatage", "type", "donnees", "accelere", "facteur"}
        assert e["accelere"] is False and e["facteur"] is None
    assert evenements[0]["donnees"]["centrale"] is True
    lien = next(e for e in evenements if e["type"] == "lien")["donnees"]
    assert lien["source_libelle"] == "@the911onabc (tiktok)"
    dernier_compteur = [e for e in evenements if e["type"] == "compteur"][-1]["donnees"]
    assert dernier_compteur["pieces"] == 2 and dernier_compteur["preuves"] == 2
    assert dernier_compteur["zones_d_ombre"] == 1
    assert evenements[-1]["donnees"]["compteurs"] == dernier_compteur


def test_reprise_depuis(serveur):
    tous = [e for _, e in flux(serveur, "/api/evenements?enquete=affaire")]
    reprise = [e for _, e in flux(serveur, "/api/evenements?enquete=affaire&depuis=4")]
    assert [e["n"] for e in reprise] == [e["n"] for e in tous if e["n"] > 4]
    par_entete = [e for _, e in flux(serveur, "/api/evenements?enquete=affaire", {"Last-Event-ID": "4"})]
    assert [e["n"] for e in par_entete] == [e["n"] for e in reprise]


def test_rejeu_respecte_le_facteur(serveur):
    # Le journal dure 4 s ; à 4x, il doit durer 1 s (à 20 % près).
    sorties = flux(serveur, "/api/rejouer?enquete=affaire&facteur=4")
    evenements = [e for _, e in sorties]
    assert all(e["accelere"] is True and e["facteur"] == 4.0 for e in evenements)
    duree = sorties[-1][0] - sorties[0][0]
    assert 0.8 <= duree <= 1.2, duree
    assert evenements[-1]["type"] == "fin"
    assert evenements[-1]["donnees"]["duree_reelle_s"] == pytest.approx(4.0)


def test_rejeu_duree_cible(serveur):
    sorties = flux(serveur, "/api/rejouer?enquete=affaire&duree_cible=2")
    assert all(e["facteur"] == pytest.approx(2.0) for _, e in sorties)
    assert 1.6 <= sorties[-1][0] - sorties[0][0] <= 2.4


def test_rejeu_n_etire_jamais_le_journal(serveur):
    # Cible plus longue que le journal : facteur 1, donc pas d'accélération annoncée.
    sorties = flux(serveur, "/api/rejouer?enquete=affaire&duree_cible=60")
    assert all(e["accelere"] is False and e["facteur"] == 1.0 for _, e in sorties)


def test_rejeu_parametres_invalides(serveur):
    for chemin in ("/api/rejouer?enquete=affaire&facteur=0", "/api/rejouer?enquete=affaire&facteur=abc",
                   "/api/rejouer?enquete=affaire&duree_cible=-3"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(serveur, chemin)
        assert e.value.code == 400


def test_recherche_retrouve_phrase_et_seconde(serveur):
    resultats = get_json(serveur, "/api/recherche?enquete=affaire&q=emergency")["resultats"]
    phrase = next(x for x in resultats if x["type"] == "phrase")
    assert phrase["seconde"] == 17 and phrase["url_origine"] == TIKTOK
    assert set(phrase) == {"type", "texte", "extrait_surligne", "piece_id", "seconde", "url_origine"}
    assert get_json(serveur, "/api/recherche?enquete=affaire&q=")["resultats"] == []


def test_question_du_journaliste_dans_le_flux_et_la_recherche(serveur):
    statut, corps = post(serveur, "/api/questions", {"enquete": "affaire", "texte": "Qui finance le compte relais ?"})
    assert statut == 201 and corps["id"]
    evenements = [e for _, e in flux(serveur, "/api/evenements?enquete=affaire")]
    posee = [e for e in evenements if e["type"] == "question" and e["donnees"].get("id") == corps["id"]]
    assert len(posee) == 1 and posee[0]["donnees"]["origine"] == "journaliste"
    export = get_json(serveur, "/api/export?enquete=affaire")
    assert export["compteurs"]["zones_d_ombre"] == 2
    assert next(q for q in export["questions_ouvertes"] if q["id"] == corps["id"])["origine"] == "journaliste"
    # L'index est refait quand le journal change.
    assert get_json(serveur, "/api/recherche?enquete=affaire&q=finance")["resultats"]


def test_question_refusee(serveur):
    assert post(serveur, "/api/questions", {"enquete": "affaire", "texte": " "})[0] == 400
    assert post(serveur, "/api/questions", {"enquete": "inconnue", "texte": "x"})[0] == 404
    assert post(serveur, "/api/questions", {"enquete": "affaire", "texte": "x"}, "text/plain")[0] == 415
    assert post(serveur, "/api/questions", {"enquete": "affaire", "texte": "x"},
                entetes={"Origin": "https://pirate.example"})[0] == 403


def test_lancer_une_enquete_en_direct(racine):
    """Un enquêteur factice écrit au journal pendant que le flux est ouvert."""

    def enqueteur(question, mots, dossier, journal, identifiant):
        assert mots == ["séisme", "volcan"]
        with ReseauEnquete(question, str(journal)) as r:
            a = r.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@a", Role.ORIGINE, id="a"))
            time.sleep(0.3)
            b = r.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@b", Role.AMPLIFICATEUR, id="b"))
            time.sleep(0.3)
            r.ajouter_lien(Lien(type_lien="reprend", famille=FamilleLien.CONTENU_DIT, source_id=a.id,
                                cible_id=b.id, preuves=(Preuve(TIKTOK, "même phrase", "Oriane", COLLECTE),),
                                explication="b reprend a", methode="m", agent="t"))

    with Serveur(racine, port=0, pas_sondage=0.02, enqueteur=enqueteur) as s:
        statut, corps = post(s, "/api/enquetes", {"question": "Test en direct ?", "graine": "séisme, volcan"})
        assert statut == 201
        sorties = flux(s, f"/api/evenements?enquete={corps['id']}")
        types = [e["type"] for _, e in sorties]
        assert types[0] == "question" and types[-1] == "fin" and "lien" in types
        # Les nœuds sont arrivés au fil de l'eau, pas d'un bloc.
        instants = [t for t, e in sorties if e["type"] == "noeud"]
        assert instants[-1] - instants[0] >= 0.2
        assert sorties[-1][1]["donnees"]["ok"] is True
        assert get_json(s, f"/api/export?enquete={corps['id']}")["mode"] == "demo"


def test_enquete_en_direct_mode_et_echec(racine):
    libre = threading.Event()

    def enqueteur(*_):
        libre.wait(5)
        raise RuntimeError("clé Oriane refusée")

    with Serveur(racine, port=0, pas_sondage=0.02, enqueteur=enqueteur) as s:
        _, corps = post(s, "/api/enquetes", {"question": "Va échouer ?"})
        assert get_json(s, f"/api/export?enquete={corps['id']}")["mode"] == "direct"
        libre.set()
        fin = flux(s, f"/api/evenements?enquete={corps['id']}")[-1][1]
        assert fin["type"] == "fin" and fin["donnees"]["ok"] is False
        assert "clé Oriane refusée" in fin["donnees"]["erreur"]


def test_lancer_sans_question(serveur):
    assert post(serveur, "/api/enquetes", {"question": "  "})[0] == 400


def test_export_json_importe_en_journal(tmp_path):
    """Un dossier qui n'a que reseau.json (sortie de `enqueter`) devient un journal à la première demande."""
    with ReseauEnquete(QUESTION) as r:
        remplir(r)
        export = r.exporter_pour_interface()
    (tmp_path / "ancienne").mkdir()
    (tmp_path / "ancienne" / "reseau.json").write_text(json.dumps(export), encoding="utf-8")
    with Serveur(tmp_path, port=0, pas_sondage=0.02) as s:
        assert get_json(s, "/api/export?enquete=ancienne")["compteurs"] == export["compteurs"]
    assert lire_evenements(tmp_path / "ancienne" / "reseau.sqlite")[0]["type"] == "question"


def test_travail_en_cours_traduit_et_videos_lues(tmp_path):
    """Les lignes de travail de la chaîne (recherche, étape, fin) passent dans le flux,
    et le compteur dit combien de vidéos distinctes les recherches ont rapportées."""
    chemin = tmp_path / "reseau.sqlite"
    ReseauEnquete(QUESTION, str(chemin)).fermer()
    travail = [
        ("etape", "e1", {"nom": "recherche", "etat": "debut", "detail": "2 mots × 3 champs"}),
        ("recherche", "r1", {"source": "oriane", "champ": "transcript", "mot": "séisme",
                             "total": 165, "recus": 3, "ids": ["a", "b", "c"], "credits": 10}),
        ("recherche", "r2", {"source": "oriane", "champ": "caption", "mot": "séisme",
                             "total": 196, "recus": 2, "ids": ["c", "d"], "credits": 10}),
        ("fin", "f1", {"cas": "reel", "duree_reelle_s": 4.2}),
    ]
    bd = sqlite3.connect(chemin)
    for genre, objet_id, donnees in travail:
        bd.execute("INSERT INTO evenements (date, genre, objet_id, version, motif, donnees)"
                   " VALUES (?, ?, ?, 1, '', ?)",
                   (COLLECTE.isoformat(), genre, objet_id, json.dumps(donnees, ensure_ascii=False)))
    bd.commit()
    bd.close()

    evenements = lire_evenements(chemin)
    types = [e["type"] for e in evenements if e["type"] != "compteur"]
    assert types == ["question", "etape", "recherche", "recherche", "etape"]
    assert evenements[-1]["type"] == "compteur" and evenements[-1]["donnees"]["videos_lues"] == 4
    fin = [e for e in evenements if e["type"] == "etape"][-1]["donnees"]
    assert fin["nom"] == "enquete" and fin["etat"] == "fin" and fin["cas"] == "reel"


class _FauxComprendre:
    """Remplace `enquete.comprendre` le temps d'un test (aucun appel à Claude)."""

    @staticmethod
    def variantes_locales(question):
        return {"mots": [{"mot": "séisme", "variantes": ["seisme", "séismes"], "champs": ["transcript"]}]}

    @staticmethod
    def lire_graine(texte):
        return {"plateforme": "tiktok", "pseudo": "the911onabc"} if "tiktok" in texte else None

    @staticmethod
    def comprendre(question, graine=None):
        return {"reformulation": f"Lecture de : {question}", "mots": []}

    @staticmethod
    def resumer_piece(piece, question):
        return f"Résumé de {piece['libelle']}"


def test_comprendre_et_resume(serveur, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "enquete.comprendre", _FauxComprendre)
    rapide = get_json(serveur, "/api/comprendre?q=s%C3%A9isme&graine=" + TIKTOK)
    assert rapide["locales"]["mots"][0]["mot"] == "séisme"
    assert rapide["graine"]["plateforme"] == "tiktok" and "profond" not in rapide
    profond = get_json(serveur, "/api/comprendre?q=s%C3%A9isme&profond=1")
    assert profond["profond"]["reformulation"].startswith("Lecture de")
    resume = get_json(serveur, "/api/resume?enquete=affaire&piece=v1")
    assert resume == {"piece": "v1", "resume": "Résumé de Vidéo tiktok"}


def test_comprendre_absent_renvoie_503(serveur, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "enquete.comprendre", None)
    with pytest.raises(urllib.error.HTTPError) as erreur:
        get(serveur, "/api/comprendre?q=test")
    assert erreur.value.code == 503
