"""Tests du banc d'essai, sur le corrigé fictif `banc/affaires/exemple-fictif.json`."""

import copy
import json
from pathlib import Path

import pytest

from enquete import banc
from enquete.banc import (
    MODES_TRUQUES,
    ErreurBanc,
    FuiteCorrige,
    JournalAltere,
    JournalCollecte,
    ReserveDejaJouee,
    charger_corrige,
    charger_corriges,
    charger_journal_collecte,
    contient_canari,
    controler_corrige,
    corrige_depuis_dict,
    detecter_fuite,
    ecrire_garde_fou,
    evaluer,
    mesurer_truquages,
    normaliser,
    normaliser_url,
    rapport_texte,
    rendu_depuis_dict,
    rendu_truque,
    sceller_reserve,
)

DOSSIER_AFFAIRES = Path(__file__).parent.parent / "banc" / "affaires"
CHEMIN_CORRIGE = DOSSIER_AFFAIRES / "exemple-fictif.json"
PREUVE = [{"url": "https://exemple.invalid/p", "extrait": "extrait de la source"}]
# Les cinq affaires d'entraînement du tour 1 (la 9 est la réserve).
ENTRAINEMENT = ["affaire-01-17-octobre", "affaire-02-videos-recyclees-lyceens", "affaire-04-faux-propos-prefet",
                "affaire-05-faux-comptes-actu", "affaire-10-seismes"]


def journal_de_la_preuve() -> JournalCollecte:
    """Ce que l'outil a lu : la page de PREUVE, avec son extrait au milieu d'un texte plus long."""
    return JournalCollecte([("https://www.exemple.invalid/p/", "Début. Voici l'EXTRAIT de la source, en entier. Fin.")])


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
    # Le texte dit F1 et F2 à égalité : l'identifiant tranche, sans passer par « à relire ».
    brut = {
        "affaire_id": corrige.id,
        "faits": [{"id": "e1", "texte": corrige.faits[0].phrase + " " + corrige.faits[1].phrase,
                   "preuves": PREUVE, "fait_vise": "F2"}],
    }
    rapport = evaluer(corrige, rendu_depuis_dict(brut))
    assert "F2" not in rapport.manquants and "F1" in rapport.manquants
    assert rapport.a_relire == [] and rapport.vise_refuses == []


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

    rapport = evaluer(corrige_reserve, rendu, journal_reserve=journal, journal_collecte=journal_de_la_preuve())
    assert rapport.reserve
    trace = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert trace["affaire_id"] == corrige_reserve.id
    assert trace["empreinte_corrige"] == corrige_reserve.empreinte

    with pytest.raises(ReserveDejaJouee):
        evaluer(corrige_reserve, rendu, journal_reserve=journal, journal_collecte=journal_de_la_preuve())
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1  # le refus n'écrit rien


def test_affaire_ordinaire_peut_etre_rejouee(corrige, tmp_path):
    journal = tmp_path / "journal.jsonl"
    rendu = rendu_depuis_dict(rendu_parfait(corrige))
    evaluer(corrige, rendu, journal_reserve=journal)
    evaluer(corrige, rendu, journal_reserve=journal)
    assert not journal.exists()


# ====================================================================== banc durci (G03, G04, G05)


def test_fait_vise_sans_le_texte_est_refuse(corrige):
    """Un identifiant seul ne suffit plus : le texte creux ne retrouve rien."""
    brut = {"affaire_id": corrige.id,
            "faits": [{"id": "v1", "texte": "Tout est établi.", "preuves": PREUVE, "fait_vise": "F1"}]}
    rapport = evaluer(corrige, rendu_depuis_dict(brut))
    assert rapport.nb_retrouves == 0 and rapport.rappel_pondere == 0.0
    assert rapport.vise_refuses == [{"rendu": "v1", "attendu": "F1", "score": 0.0}]
    assert rapport.hors_corrige == ["v1"]
    assert "refusé" in rapport_texte(rapport)


@pytest.mark.parametrize("affaire", ENTRAINEMENT)
@pytest.mark.parametrize("mode", MODES_TRUQUES)
def test_un_rendu_truque_tombe_a_zero_sur_les_cinq_affaires(affaire, mode):
    """Truqué par identifiants, par recopie de phrases, par articles de presse ou par canari."""
    c = charger_corrige(DOSSIER_AFFAIRES / f"{affaire}.json")
    try:
        rapport = evaluer(c, rendu_truque(c, mode), journal_collecte=JournalCollecte())
    except FuiteCorrige:
        assert mode == "canari"
        return
    assert rapport.rappel_pondere == 0.0
    assert rapport.nb_retrouves == 0


def test_truque_par_identifiants_tombe_a_zero_meme_sans_journal():
    for c in charger_corriges(DOSSIER_AFFAIRES):
        rapport = evaluer(replace_reserve(c), rendu_truque(c, "identifiants"))
        assert rapport.rappel_pondere == 0.0, c.id


def replace_reserve(c):
    from dataclasses import replace
    return replace(c, reserve=False)


def test_recopie_sans_journal_ne_fait_pas_foi(corrige):
    """Limite assumée : sans journal de collecte, la recopie passe, et le rapport le dit."""
    rapport = evaluer(corrige, rendu_truque(corrige, "recopie"))
    assert rapport.rappel_pondere == 1.0 and not rapport.preuves_rapprochees
    assert "ne fait pas foi" in rapport_texte(rapport)
    avec = evaluer(corrige, rendu_truque(corrige, "recopie"), journal_collecte=JournalCollecte())
    assert avec.rappel_pondere == 0.0 and avec.preuves_rapprochees
    assert "ne fait pas foi" not in rapport_texte(avec)


def test_mesure_des_truquages_sur_tout_le_banc():
    mesures = mesurer_truquages(charger_corriges(DOSSIER_AFFAIRES), avec_journal=True)
    assert set(mesures) >= set(ENTRAINEMENT)
    assert all(v == 0.0 for modes in mesures.values() for v in modes.values())


def test_preuve_rapprochee_du_journal_de_collecte(corrige):
    rendu = rendu_depuis_dict(rendu_parfait(corrige))
    ok = evaluer(corrige, rendu, journal_collecte=journal_de_la_preuve())
    assert ok.rappel_pondere == 1.0 and ok.preuves_refusees == []

    autre_page = JournalCollecte([("https://exemple.invalid/autre", "Voici l'extrait de la source")])
    assert evaluer(corrige, rendu, journal_collecte=autre_page).rappel_pondere == 0.0

    extrait_absent = JournalCollecte([("https://exemple.invalid/p", "Un texte sans rapport avec la preuve.")])
    refuse = evaluer(corrige, rendu, journal_collecte=extrait_absent)
    assert refuse.rappel_pondere == 0.0
    assert "extrait absent du texte collecté" in refuse.preuves_refusees[0]["motifs"][0]
    assert "preuve refusée" in rapport_texte(refuse)


def test_une_preuve_vraie_suffit_parmi_des_fausses(corrige):
    brut = {"affaire_id": corrige.id, "faits": [{
        "id": "m1", "texte": corrige.faits[0].phrase, "fait_vise": "F1",
        "preuves": [{"url": "https://exemple.invalid/faux", "extrait": "jamais lu"}, *PREUVE]}]}
    rapport = evaluer(corrige, rendu_depuis_dict(brut), journal_collecte=journal_de_la_preuve())
    assert rapport.nb_retrouves == 1 and rapport.inventes == []
    assert rapport.preuves_refusees[0]["fait"] == "m1"  # le refus reste tracé


def test_charger_journal_de_collecte(tmp_path):
    chemin = tmp_path / "collecte.json"
    chemin.write_text(json.dumps([{"url": "https://exemple.invalid/p", "texte": "Voici l'extrait de la source."}]),
                      encoding="utf-8")
    journal = charger_journal_collecte(chemin)
    assert journal.verifier("http://www.exemple.invalid/p/", "l'extrait de la source") is None
    assert journal.verifier("https://exemple.invalid/p", "extrait de la sauce") is not None
    chemin.write_text(json.dumps([{"url": "https://exemple.invalid/p"}]), encoding="utf-8")
    with pytest.raises(ErreurBanc):
        charger_journal_collecte(chemin)


def test_normaliser_url():
    assert normaliser_url("HTTPS://www.Exemple.invalid/a/b/?utm=1#x") == "exemple.invalid/a/b"
    assert normaliser_url("https://www.youtube.com/watch?v=abc&t=3") == "youtube.com/watch?v=abc&t=3"


# ---- phrase canari et liste noire


def test_la_phrase_canari_est_detectee_dans_un_rendu_qui_la_contient(corrige):
    assert corrige.canari
    brut = rendu_parfait(corrige)
    brut["faits"].append({"id": "fuite", "texte": f"Selon nos sources : {corrige.canari}", "preuves": PREUVE})
    assert detecter_fuite(corrige, rendu_depuis_dict(brut)) == ["fuite"]
    with pytest.raises(FuiteCorrige):
        evaluer(corrige, rendu_depuis_dict(brut))


def test_canari_retrouve_dans_un_extrait_ou_une_paraphrase(corrige):
    brut = rendu_parfait(corrige)
    brut["faits"][0]["preuves"] = [{"url": "https://exemple.invalid/p", "extrait": corrige.canari.upper()}]
    assert detecter_fuite(corrige, rendu_depuis_dict(brut)) == [brut["faits"][0]["id"]]
    mots = corrige.canari.split()
    assert contient_canari(corrige.canari, " ".join(reversed(mots)))  # mots mélangés : 100 % des mots
    assert not contient_canari(corrige.canari, "La première vidéo date du 3 mars.")
    assert detecter_fuite(corrige, rendu_depuis_dict(rendu_parfait(corrige))) == []


def test_chaque_corrige_a_son_propre_canari():
    corriges = charger_corriges(DOSSIER_AFFAIRES)
    canaris = [c.canari for c in corriges]
    assert all(canaris) and len(set(canaris)) == len(canaris)
    for c in corriges:
        # aucune phrase d'un corrigé ne contient le canari d'un autre
        assert not any(contient_canari(autre, f.phrase) for autre in canaris for f in c.faits)


def test_une_preuve_qui_cite_la_presse_du_corrigé_est_refusee():
    c = charger_corrige(DOSSIER_AFFAIRES / "affaire-10-seismes.json")
    assert normaliser_url("https://factcheck.afp.com/doc.afp.com.C64C7NQ") in c.urls_presse
    rendu = rendu_depuis_dict({"affaire_id": c.id, "faits": [{
        "id": "p1", "texte": c.faits[0].phrase, "fait_vise": "F1",
        "preuves": [{"url": "https://factcheck.afp.com/doc.afp.com.C64C7NQ/", "extrait": c.faits[0].phrase}]}]})
    rapport = evaluer(c, rendu)
    assert rapport.rappel_pondere == 0.0 and "article de presse du corrigé" in rapport.inventes[0]["motif"]


def test_export_garde_fou(tmp_path):
    corriges = charger_corriges(DOSSIER_AFFAIRES)
    sortie = ecrire_garde_fou(corriges, tmp_path / "sous" / "garde-fou.json")
    export = json.loads(sortie.read_text(encoding="utf-8"))
    affaire = export["affaires"]["affaire-10-seismes"]
    assert affaire["canari"] and "factcheck.afp.com/doc.afp.com.C64C7NQ" in affaire["urls_presse"]
    assert set(export["affaires"]) == {c.id for c in corriges}


# ---- portée, sources, seuils


def test_chaque_fait_a_une_portee_une_source_et_un_seuil_serieux():
    corriges = charger_corriges(DOSSIER_AFFAIRES)
    assert sum(len(c.faits) for c in corriges) == 88  # 80 faits réels + 8 faits fictifs
    assert [d for c in corriges for d in controler_corrige(c)] == []


def test_seuil_trop_lache_ou_portee_inconnue_refuses(brut_corrige):
    lache = copy.deepcopy(brut_corrige)
    lache["faits"][0]["correspondance"]["seuil"] = 0.5
    with pytest.raises(ErreurBanc, match="trop lâche"):
        corrige_depuis_dict(lache)
    inconnue = copy.deepcopy(brut_corrige)
    inconnue["faits"][0]["portee"] = "ailleurs"
    with pytest.raises(ErreurBanc, match="portée inconnue"):
        corrige_depuis_dict(inconnue)


def test_controle_signale_un_fait_sans_portee_ni_source(brut_corrige):
    del brut_corrige["faits"][0]["portee"]
    del brut_corrige["faits"][1]["source"]
    defauts = controler_corrige(corrige_depuis_dict(brut_corrige))
    assert any("F1 : sans portée" in d for d in defauts) and any("F2 : sans source" in d for d in defauts)


def test_rappel_sur_le_visible_et_sur_le_total(corrige):
    # F8 (zone d'ombre) est hors de portée : l'oublier coûte au total, pas au visible.
    brut = rendu_parfait(corrige)
    brut["faits"] = [f for f in brut["faits"] if f["fait_vise"] != "F8"]
    rapport = evaluer(corrige, rendu_depuis_dict(brut))
    assert rapport.rappel_visible == 1.0
    assert rapport.rappel_pondere == pytest.approx(12 / 13, abs=1e-3)
    assert rapport.par_portee["hors_portee"]["retrouves"] == 0
    assert rapport.par_portee["video"]["rappel_pondere"] == 1.0
    assert "Sur le visible (vidéo et web) : 100%" in rapport_texte(rapport)
    assert len(rapport_texte(rapport).splitlines()) == 10


def test_sans_portee_pas_de_rappel_visible():
    c = corrige_depuis_dict({"id": "x", "question": "?", "gel_donnees": "2026-11-07",
                             "faits": [{"id": "F1", "phrase": "Trois vidéos reprennent la phrase.", "type": "reprise"}]})
    assert evaluer(c, rendu_depuis_dict({"affaire_id": "x", "faits": []})).rappel_visible is None


# ---- réserve scellée


@pytest.fixture
def reserve(brut_corrige):
    brut_corrige["reserve"] = True
    return corrige_depuis_dict(brut_corrige)


def test_le_journal_de_reserve_est_hors_du_depot():
    depot = Path(__file__).parent.parent.resolve()
    assert depot not in banc.JOURNAL_RESERVE.resolve().parents


def test_reserve_sans_journal_de_collecte_refusee_sans_etre_consommee(reserve, tmp_path):
    journal = tmp_path / "r.jsonl"
    rendu = rendu_depuis_dict(rendu_parfait(reserve))
    with pytest.raises(ErreurBanc, match="journal de collecte"):
        evaluer(reserve, rendu, journal_reserve=journal)
    assert not journal.exists()
    evaluer(reserve, rendu, journal_reserve=journal, journal_collecte=journal_de_la_preuve())  # toujours jouable


def test_second_passage_refuse_meme_sous_un_autre_nom(reserve, brut_corrige, tmp_path):
    journal = tmp_path / "r.jsonl"
    rendu = rendu_depuis_dict(rendu_parfait(reserve))
    evaluer(reserve, rendu, journal_reserve=journal, journal_collecte=journal_de_la_preuve())
    with pytest.raises(ReserveDejaJouee):
        evaluer(reserve, rendu, journal_reserve=journal, journal_collecte=journal_de_la_preuve())
    # même contenu sous un autre identifiant : l'empreinte ne tient pas compte de l'id
    renomme = copy.deepcopy(brut_corrige)
    renomme["id"] = "autre-nom"
    c2 = corrige_depuis_dict(renomme)
    assert c2.empreinte == reserve.empreinte
    with pytest.raises(ReserveDejaJouee):
        evaluer(c2, rendu_depuis_dict(rendu_parfait(c2)), journal_reserve=journal,
                journal_collecte=journal_de_la_preuve())
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1


def test_journal_altere_est_refuse(reserve, tmp_path):
    journal = tmp_path / "r.jsonl"
    rendu = rendu_depuis_dict(rendu_parfait(reserve))
    evaluer(reserve, rendu, journal_reserve=journal, journal_collecte=journal_de_la_preuve())
    journal.write_text(journal.read_text(encoding="utf-8").replace('"precedent"', '"pr"'), encoding="utf-8")
    with pytest.raises(JournalAltere):
        evaluer(reserve, rendu, journal_reserve=journal, journal_collecte=journal_de_la_preuve())
    journal.write_text("pas du json\n", encoding="utf-8")
    with pytest.raises(JournalAltere):
        evaluer(reserve, rendu, journal_reserve=journal, journal_collecte=journal_de_la_preuve())


def test_corrige_modifie_apres_scellement_refuse(reserve, brut_corrige, tmp_path):
    journal = tmp_path / "r.jsonl"
    sceller_reserve(reserve, journal)
    sceller_reserve(reserve, journal)  # idempotent
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1
    modifie = copy.deepcopy(brut_corrige)
    modifie["faits"][0]["phrase"] += " Et ceci, ajouté après coup pour que l'outil gagne."
    modifie["reserve"] = True
    c2 = corrige_depuis_dict(modifie)
    with pytest.raises(ErreurBanc, match="a changé depuis son scellement"):
        evaluer(c2, rendu_depuis_dict(rendu_parfait(c2)), journal_reserve=journal,
                journal_collecte=journal_de_la_preuve())
    # le corrigé scellé, lui, se joue normalement une fois
    evaluer(reserve, rendu_depuis_dict(rendu_parfait(reserve)), journal_reserve=journal,
            journal_collecte=journal_de_la_preuve())


def test_fuite_sur_la_reserve_la_consomme(reserve, tmp_path):
    journal = tmp_path / "r.jsonl"
    brut = rendu_parfait(reserve)
    brut["faits"].append({"id": "f", "texte": reserve.canari, "preuves": PREUVE})
    with pytest.raises(FuiteCorrige):
        evaluer(reserve, rendu_depuis_dict(brut), journal_reserve=journal, journal_collecte=journal_de_la_preuve())
    assert json.loads(journal.read_text(encoding="utf-8").splitlines()[0])["evenement"] == "fuite"
    with pytest.raises(ReserveDejaJouee):
        evaluer(reserve, rendu_depuis_dict(rendu_parfait(reserve)), journal_reserve=journal,
                journal_collecte=journal_de_la_preuve())


def test_la_reserve_reelle_est_marquee_et_non_jouee():
    c = charger_corrige(DOSSIER_AFFAIRES / "affaire-09-stylos-glp1.json")
    assert c.reserve and len(c.empreinte) == 64
