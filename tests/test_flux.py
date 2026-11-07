"""Tests du flux : le journal s'écrit pendant la chaîne et se relit comme un fil d'événements.

Aucun réseau : on réutilise le faux Oriane et l'archivage simulé de `test_chaine.py`.
"""

import json
from pathlib import Path

import pytest

from enquete import chaine
from enquete.archive import PieceArchivee
from enquete.chaine import enqueter
from enquete.flux import exporter_jsonl, lire_depuis
from enquete.reseau import ReseauEnquete
from export_normalise import normaliser
from test_chaine import FauxOriane, client, passage, videos_de_test

REPONSES = Path(__file__).parent / "reponses"
GENRES_RESEAU = {"affaire", "noeud", "lien", "question"}


def journal_de(tmp_path) -> Path:
    return tmp_path / "sortie" / "reseau.sqlite"


def sans_etapes(lignes):
    return [l for l in lignes if l["genre"] != "etape"]


def test_l_ordre_du_flux_suit_le_travail(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    lignes = lire_depuis(journal_de(tmp_path))
    genres = [l["genre"] for l in sans_etapes(lignes)]

    def premier(genre, test=lambda l: True):
        return next(i for i, l in enumerate(sans_etapes(lignes)) if l["genre"] == genre and test(l))

    assert genres[0] == "affaire" and genres[-1] == "fin"
    recherches = [i for i, g in enumerate(genres) if g == "recherche"]
    assert len(recherches) == 4  # trois champs en basic, une relecture full
    premiere_relecture = next(i for i, l in enumerate(sans_etapes(lignes))
                              if l["genre"] == "recherche" and l["donnees"]["palier"] == "full")
    premier_noeud = premier("noeud")
    # recherches, puis relecture, puis les pièces à classer
    assert max(recherches) == premiere_relecture < premier_noeud
    # les pièces naissent en rôle « inconnu » ; les rôles sont corrigés ensuite (version 2)
    nouveaux = [l for l in lignes if l["genre"] == "noeud" and l["version"] == 1 and l["donnees"]["type"] != "grappe"]
    assert nouveaux and all(l["donnees"]["role"] == "inconnu" for l in nouveaux)
    corriges = [i for i, l in enumerate(sans_etapes(lignes)) if l["genre"] == "noeud" and l["version"] == 2]
    assert corriges and {sans_etapes(lignes)[i]["donnees"]["role"] for i in corriges} == {"origine", "amplificateur"}
    dernier_a_classer = max(i for i, l in enumerate(sans_etapes(lignes))
                            if l["genre"] == "noeud" and l["version"] == 1 and l["donnees"]["type"] != "grappe")
    premier_lien = premier("lien")
    assert dernier_a_classer < min(corriges) < premier_lien
    premiere_question = premier("question")
    dernier_lien_v1 = max(i for i, l in enumerate(sans_etapes(lignes)) if l["genre"] == "lien" and l["version"] == 1)
    assert dernier_lien_v1 < premiere_question
    # la preuve d'archive arrive après les questions, en nouvelle version des liens
    versions2 = [i for i, l in enumerate(sans_etapes(lignes)) if l["genre"] == "lien" and l["version"] == 2]
    assert versions2 and min(versions2) > premiere_question
    fin = lignes[-1]
    assert fin["donnees"]["cas"] == "reel" and fin["donnees"]["duree_reelle_s"] >= 0


def test_les_recherches_portent_leurs_champs(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    recherches = [l for l in lire_depuis(journal_de(tmp_path)) if l["genre"] == "recherche"]
    basic = [r["donnees"] for r in recherches if r["donnees"]["palier"] == "basic"]
    assert sorted(d["champ"] for d in basic) == ["caption", "onScreenText", "transcript"]
    assert {d["mot"] for d in basic} == {"minceur"}
    for d in basic:
        assert d["source"] == "oriane" and d["plateforme"] == "tiktok"
        assert d["total"] == d["recus"] == len(d["ids"]) == 4
        assert d["credits"] == 10 and d["cache"] is False
    full = next(r["donnees"] for r in recherches if r["donnees"]["palier"] == "full")
    assert full["credits"] == 20 and full["recus"] == 4


def test_les_etapes_ont_un_debut_et_une_fin(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    etapes = [(l["donnees"]["nom"], l["donnees"]["etat"]) for l in lire_depuis(journal_de(tmp_path))
              if l["genre"] == "etape"]
    attendu = []
    for nom in ("recherche", "relecture", "regroupement", "reseau", "archivage"):
        attendu += [(nom, "debut"), (nom, "fin")]
    assert etapes == attendu


def test_t_ne_recule_jamais_et_seq_est_continu(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    lignes = lire_depuis(journal_de(tmp_path))
    ts = [l["t"] for l in lignes]
    assert ts[0] == 0.0 and ts == sorted(ts)
    assert [l["seq"] for l in lignes] == list(range(1, len(lignes) + 1))
    assert set(lignes[0]) == {"seq", "t", "genre", "objet_id", "version", "motif", "donnees"}


def test_lire_depuis_lit_pendant_l_ecriture(tmp_path, monkeypatch):
    """Quand l'archivage démarre, le réseau est déjà lisible par un autre lecteur ; la fin, pas encore."""
    vu = {}

    def archiver_espion(url, dossier):
        lignes = lire_depuis(journal_de(tmp_path))
        vu["genres"] = {l["genre"] for l in lignes}
        vu["dernier_seq"] = lignes[-1]["seq"]
        vu["liens"] = sum(1 for l in lignes if l["genre"] == "lien")
        piece = PieceArchivee(url=url, type="video", date_collecte="2026-11-07T10:00:00+00:00")
        piece.empreinte_sha256 = "ab" * 32
        piece.statuts.update(telechargement="ok", empreinte="ok", wayback="echec")
        return piece

    faux = FauxOriane(videos_de_test())
    monkeypatch.setattr(chaine, "archiver_video", archiver_espion)
    with client(tmp_path, faux) as c:
        enqueter("Qui diffuse ces vidéos minceur ?", ["minceur"], c, tmp_path / "sortie", "affaire-test")

    assert {"affaire", "recherche", "noeud", "lien", "question"} <= vu["genres"]
    assert "fin" not in vu["genres"]
    assert vu["liens"] > 0
    # reprise du curseur : seules les nouvelles lignes reviennent
    toutes = lire_depuis(journal_de(tmp_path))
    suite = lire_depuis(journal_de(tmp_path), apres_seq=vu["dernier_seq"])
    assert suite and suite[0]["seq"] == vu["dernier_seq"] + 1 and suite[-1] == toutes[-1]
    assert lire_depuis(journal_de(tmp_path), apres_seq=toutes[-1]["seq"]) == []


def test_lire_depuis_un_fichier_absent_donne_une_liste_vide(tmp_path):
    assert lire_depuis(tmp_path / "rien.sqlite") == []


def test_l_export_final_est_celui_d_avant_le_flux(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    export = json.loads((tmp_path / "sortie" / "reseau.json").read_text(encoding="utf-8"))
    avant = json.loads((REPONSES / "export_avant_flux.json").read_text(encoding="utf-8"))
    assert normaliser(export) == avant


def test_exporter_jsonl_ecrit_le_meme_flux(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    sortie = tmp_path / "flux.jsonl"
    n = exporter_jsonl(journal_de(tmp_path), sortie)
    ecrites = [json.loads(l) for l in sortie.read_text(encoding="utf-8").splitlines()]
    assert n == len(ecrites) == len(lire_depuis(journal_de(tmp_path)))
    assert ecrites == lire_depuis(journal_de(tmp_path))


def test_un_journal_choisi_recoit_le_flux(tmp_path, monkeypatch):
    chemin = tmp_path / "ailleurs" / "mon-journal.sqlite"
    faux = FauxOriane(videos_de_test())
    monkeypatch.setattr(chaine, "archiver_video", lambda url, dossier: PieceArchivee(
        url=url, type="video", date_collecte="2026-11-07T10:00:00+00:00"))
    with client(tmp_path, faux) as c:
        enqueter("Qui diffuse ces vidéos minceur ?", ["minceur"], c, tmp_path / "sortie", "a",
                 chemin_journal=chemin)
    assert lire_depuis(chemin)[-1]["genre"] == "fin"
    assert not journal_de(tmp_path).exists()


def test_la_question_deja_ecrite_par_le_serveur_n_est_pas_dupliquee(tmp_path, monkeypatch):
    question = "Qui diffuse ces vidéos minceur ?"
    chemin = journal_de(tmp_path)
    chemin.parent.mkdir(parents=True)
    ReseauEnquete(question, str(chemin)).fermer()  # ce que fait le serveur avant d'appeler la chaîne
    passage(tmp_path, monkeypatch)
    lignes = lire_depuis(chemin)
    affaires = [l for l in lignes if l["genre"] == "affaire"]
    assert len(affaires) == 1 and affaires[0]["seq"] == 1 and affaires[0]["version"] == 1
    assert not list(chemin.parent.glob("reseau-*.sqlite"))  # rouvert, pas mis de côté


def test_un_ancien_passage_est_mis_de_cote_pas_melange(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    passage(tmp_path, monkeypatch)
    lignes = lire_depuis(journal_de(tmp_path))
    assert sum(1 for l in lignes if l["genre"] == "affaire") == 1
    assert sum(1 for l in lignes if l["genre"] == "fin") == 1
    anciens = list(journal_de(tmp_path).parent.glob("reseau-*.sqlite"))
    assert len(anciens) == 1 and lire_depuis(anciens[0])[-1]["genre"] == "fin"


def test_noter_refuse_les_genres_du_reseau():
    with ReseauEnquete("Une question ?") as reseau:
        for genre in GENRES_RESEAU:
            with pytest.raises(ValueError):
                reseau.noter(genre, {})
        seq = reseau.noter("recherche", {"mot": "a"})
        assert seq == 2
        reseau.noter("etape", {"nom": "x", "etat": "debut"})
        reseau.noter("etape", {"nom": "x", "etat": "fin"})
        historique = reseau.historique()
        assert [(e.genre, e.objet_id, e.version) for e in historique] == [
            ("affaire", "question_centrale", 1), ("recherche", "recherche-1", 1),
            ("etape", "x", 1), ("etape", "x", 2),
        ]
        # les événements de travail ne changent pas l'export
        assert reseau.exporter_pour_interface()["compteurs"]["pieces"] == 0


def test_une_erreur_laisse_un_evenement_fin_avec_l_erreur(tmp_path, monkeypatch):
    def casse(url, dossier):
        raise RuntimeError("yt-dlp a planté")

    faux = FauxOriane(videos_de_test())
    monkeypatch.setattr(chaine, "archiver_video", casse)
    with client(tmp_path, faux) as c:
        with pytest.raises(RuntimeError):
            monkeypatch.setattr(chaine, "archiver", casse)
            enqueter("Qui diffuse ces vidéos minceur ?", ["minceur"], c, tmp_path / "sortie", "a")
    fin = lire_depuis(journal_de(tmp_path))[-1]
    assert fin["genre"] == "fin" and "planté" in fin["donnees"]["erreur"]
