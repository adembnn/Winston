"""Tests du banc d'essai, sur le corrigé fictif `banc/affaires/exemple-fictif.json`."""

import copy
import json
from pathlib import Path

import pytest

from enquete.banc import (
    ErreurBanc,
    ReserveDejaJouee,
    charger_corrige,
    corrige_depuis_dict,
    evaluer,
    normaliser,
    rapport_texte,
    rendu_depuis_dict,
)

CHEMIN_CORRIGE = Path(__file__).parent.parent / "banc" / "affaires" / "exemple-fictif.json"
PREUVE = [{"url": "https://exemple.invalid/p", "extrait": "extrait de la source"}]


@pytest.fixture
def brut_corrige():
    return json.loads(CHEMIN_CORRIGE.read_text(encoding="utf-8"))


@pytest.fixture
def corrige():
    return charger_corrige(CHEMIN_CORRIGE)


def rendu_parfait(corrige):
    """Un rendu qui vise explicitement chaque fait attendu, avec preuve."""
    return {
        "affaire_id": corrige.id,
        "faits": [
            {"id": f"p-{f.id}", "texte": f.phrase, "preuves": PREUVE, "fait_vise": f.id}
            for f in corrige.faits
        ],
    }


def test_score_parfait(corrige):
    rapport = evaluer(corrige, rendu_depuis_dict(rendu_parfait(corrige)))
    assert rapport.rappel_pondere == 1.0
    assert rapport.precision == 1.0
    assert rapport.inventes == []
    assert rapport.pieges_tombes == []
    assert rapport.non_etabli_respecte
    assert rapport.manquants == []
    assert rapport.a_relire == []  # identifiants explicites : rien à relire


def test_faits_manquants_et_poids(corrige):
    brut = rendu_parfait(corrige)
    # Poids totaux : 3+2+1+2+2+1+1+1 = 13. On retire F1 (3) et F6 (1) : rappel = 9/13.
    brut["faits"] = [f for f in brut["faits"] if f["fait_vise"] not in ("F1", "F6")]
    rapport = evaluer(corrige, rendu_depuis_dict(brut))
    assert rapport.manquants == ["F1", "F6"]
    assert rapport.nb_retrouves == 6
    assert rapport.rappel_pondere == pytest.approx(9 / 13, abs=1e-3)
    assert rapport.precision == 1.0
    assert rapport.par_type["origine"]["retrouves"] == 0
    assert rapport.par_type["origine"]["rappel_pondere"] == 0.0
    assert rapport.par_type["lien"]["rappel_pondere"] == 1.0


def test_fait_sans_preuve_est_invente(corrige):
    brut = rendu_parfait(corrige)
    brut["faits"].append({"id": "x1", "texte": "La piscine fermera en juin pour travaux.", "preuves": []})
    brut["faits"][0]["preuves"] = [{"url": "https://exemple.invalid/p", "extrait": ""}]  # extrait vide
    rapport = evaluer(corrige, rendu_depuis_dict(brut))
    ids = {x["id"] for x in rapport.inventes}
    assert ids == {"x1", "p-F1"}
    assert "F1" in rapport.manquants  # un fait sans preuve ne compte pas dans le rappel
    assert rapport.precision < 1.0
    assert "sans preuve" in rapport_texte(rapport)


def test_piege_tombe(corrige):
    brut = rendu_parfait(corrige)
    brut["faits"].append(
        {"id": "x2", "texte": "Le petit compte local est coupable d'une manipulation.", "preuves": PREUVE}
    )
    rapport = evaluer(corrige, rendu_depuis_dict(brut))
    assert rapport.pieges_tombes == ["P1"]
    assert [x["id"] for x in rapport.inventes] == ["x2"]
    assert "1 piège évité sur 2" in rapport_texte(rapport)


def test_non_etabli_viole(corrige):
    brut = rendu_parfait(corrige)
    brut["faits"].append(
        {"id": "x3", "texte": "Les commerçants ont été payés pour relayer la vidéo : rémunération prouvée.", "preuves": PREUVE}
    )
    rapport = evaluer(corrige, rendu_depuis_dict(brut))
    assert not rapport.non_etabli_respecte
    assert rapport.non_etabli_violes == ["N2"]
    assert "VIOLÉ" in rapport_texte(rapport)


def test_dire_non_etabli_est_la_bonne_reponse(corrige):
    brut = rendu_parfait(corrige)
    brut["faits"].append(
        {"id": "x4", "texte": "L'identité de la personne qui tient le compte reste inconnue.",
         "statut": "non_etabli", "preuves": []}
    )
    rapport = evaluer(corrige, rendu_depuis_dict(brut))
    assert rapport.non_etabli_respecte
    assert rapport.inventes == []
    assert rapport.precision == 1.0  # un aveu d'ignorance n'entre pas dans le dénominateur


def test_rapprochement_de_texte_marque_a_relire(corrige):
    brut = {
        "affaire_id": corrige.id,
        "faits": [
            {"id": "t1", "preuves": PREUVE,
             "texte": "Le 3 mars, première vidéo sur la fermeture de la piscine, postée par un compte de sport."},
            {"id": "t2", "preuves": PREUVE, "texte": "Le ciel était bleu ce jour-là."},
        ],
    }
    rapport = evaluer(corrige, rendu_depuis_dict(brut))
    assert rapport.nb_retrouves == 1
    assert rapport.a_relire[0]["rendu"] == "t1" and rapport.a_relire[0]["attendu"] == "F1"
    assert rapport.hors_corrige == ["t2"]
    assert rapport.precision == 0.5
    assert "À relire par un humain" in rapport_texte(rapport)


def test_un_identifiant_explicite_prime_sur_le_texte(corrige):
    brut = {
        "affaire_id": corrige.id,
        "faits": [{"id": "e1", "texte": corrige.faits[0].phrase, "preuves": PREUVE, "fait_vise": "F2"}],
    }
    rapport = evaluer(corrige, rendu_depuis_dict(brut))
    assert rapport.manquants[0] == "F1" and "F2" not in rapport.manquants


def test_rapport_texte_dix_lignes_et_phrase_cle(corrige):
    texte = rapport_texte(evaluer(corrige, rendu_depuis_dict(rendu_parfait(corrige))))
    assert len(texte.splitlines()) == 10
    assert "Sur cette affaire : 8 faits sur 8 retrouvés, aucun fait inventé, 2 pièges évités sur 2." in texte


def test_rendu_d_une_autre_affaire_refuse(corrige):
    with pytest.raises(ErreurBanc):
        evaluer(corrige, rendu_depuis_dict({"affaire_id": "autre", "faits": []}))


def test_corrige_mal_forme_refuse(brut_corrige):
    mauvais = copy.deepcopy(brut_corrige)
    mauvais["faits"][0]["type"] = "n-importe-quoi"
    with pytest.raises(ErreurBanc, match="type inconnu"):
        corrige_depuis_dict(mauvais)


def test_normaliser_ignore_accents_casse_et_mots_vides():
    assert normaliser("La Première VIDÉO du 3 mars") == {"premiere", "video", "3", "mars"}


def test_reserve_jouee_une_seule_fois(brut_corrige, tmp_path):
    brut_corrige["reserve"] = True
    corrige_reserve = corrige_depuis_dict(brut_corrige)
    rendu = rendu_depuis_dict(rendu_parfait(corrige_reserve))
    journal = tmp_path / "donnees" / "journal.jsonl"

    rapport = evaluer(corrige_reserve, rendu, journal_reserve=journal)
    assert rapport.reserve
    trace = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert trace["affaire_id"] == corrige_reserve.id

    with pytest.raises(ReserveDejaJouee):
        evaluer(corrige_reserve, rendu, journal_reserve=journal)
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1  # le refus n'écrit rien


def test_affaire_ordinaire_peut_etre_rejouee(corrige, tmp_path):
    journal = tmp_path / "journal.jsonl"
    rendu = rendu_depuis_dict(rendu_parfait(corrige))
    evaluer(corrige, rendu, journal_reserve=journal)
    evaluer(corrige, rendu, journal_reserve=journal)
    assert not journal.exists()
