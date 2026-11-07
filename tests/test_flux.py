"""Tests du flux : le journal se relit comme un fil d'événements, travail en cours compris.

Les tests du journal écrit au fil de la chaîne vivent avec la chaîne (lot de l'intégrateur).
"""

import pytest

from enquete.flux import exporter_jsonl, lire_depuis
from enquete.reseau import ReseauEnquete

GENRES_RESEAU = {"affaire", "noeud", "lien", "question"}


def test_lire_depuis_un_fichier_absent_donne_une_liste_vide(tmp_path):
    assert lire_depuis(tmp_path / "rien.sqlite") == []


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


def test_lire_depuis_et_exporter_jsonl_suivent_le_journal(tmp_path):
    chemin = tmp_path / "reseau.sqlite"
    with ReseauEnquete("Une question ?", str(chemin)) as reseau:
        reseau.noter("recherche", {"mot": "a", "ids": ["v1"]})
        reseau.poser_question("Qui a publié en premier ?")
    lignes = lire_depuis(chemin)
    assert [l["genre"] for l in lignes] == ["affaire", "recherche", "question"]
    assert all(a["t"] <= b["t"] for a, b in zip(lignes, lignes[1:]))
    assert [l["seq"] for l in lire_depuis(chemin, apres_seq=1)] == [2, 3]
    sortie = tmp_path / "flux.jsonl"
    exporter_jsonl(chemin, sortie)
    assert len(sortie.read_text(encoding="utf-8").splitlines()) == 3
