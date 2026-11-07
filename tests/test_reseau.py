"""Tests du réseau d'enquête : preuve obligatoire, ajout seul, export, questions, solidité."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from enquete.reseau import (
    EchelleVerbale,
    FamilleLien,
    Lien,
    LienSansPreuve,
    Noeud,
    NoeudInconnu,
    Preuve,
    ReseauEnquete,
    Role,
    Solidite,
    StatutLien,
    StatutQuestion,
    TypeNoeud,
    echelle_verbale,
    empreinte_sha256,
)

QUESTION = "Stylos minceur : qui est derrière ?"
COLLECTE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


def une_preuve(extrait="cent pour cent naturel", url="https://exemple.test/v/1", **kw):
    return Preuve(url=url, extrait=extrait, source_donnee="Oriane",
                  date_collecte=COLLECTE, **kw)


def un_lien(source, cible, **kw):
    valeurs = dict(
        type_lien="reprend", famille=FamilleLien.CONTENU_DIT,
        source_id=source, cible_id=cible, preuves=(une_preuve(),),
        explication="B reprend la phrase de A trois heures après.",
        methode="n-gramme rare", agent="agent-liens",
    )
    valeurs.update(kw)
    return Lien(**valeurs)


@pytest.fixture
def reseau():
    with ReseauEnquete(QUESTION) as r:
        yield r


@pytest.fixture
def reseau_garni(reseau):
    """Un petit dossier : deux comptes, une société, une vidéo, trois liens."""
    a = reseau.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@premier", Role.ORIGINE, id="a"))
    b = reseau.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@relais", Role.AMPLIFICATEUR, id="b"))
    s = reseau.ajouter_noeud(Noeud(
        TypeNoeud.SOCIETE, "Minceur SAS", Role.BENEFICIAIRE, id="s",
        solidite=Solidite("A", 1)))
    v = reseau.ajouter_noeud(Noeud(TypeNoeud.VIDEO, "vidéo du relais", Role.AMPLIFICATEUR, id="v"))
    partagee = une_preuve("minceur en dix jours", debut_s=12.0, fin_s=15.5)
    reseau.ajouter_lien(un_lien("a", "b", id="l1", preuves=(partagee,)))
    reseau.ajouter_lien(un_lien("b", "v", id="l2", type_lien="publie",
                                famille=FamilleLien.RELATIONS_SOCIALES, preuves=(partagee,)))
    reseau.ajouter_lien(un_lien(
        "s", "a", id="l3", type_lien="finance", famille=FamilleLien.ARGENT,
        preuves=(une_preuve("registre : Minceur SAS", url="https://registre.test/s"),
                 une_preuve("annonce sponsorisée", url="https://pub.test/9"))))
    return reseau


# -- 1. un lien sans preuve n'existe pas -------------------------------------

def test_lien_sans_preuve_refuse():
    with pytest.raises(LienSansPreuve):
        un_lien("a", "b", preuves=())


def test_lien_sans_preuve_refuse_aussi_par_correction(reseau_garni):
    with pytest.raises(LienSansPreuve):
        reseau_garni.corriger_lien("l1", "tentative", preuves=())
    assert len(reseau_garni.lien("l1").preuves) == 1  # rien n'a changé


def test_lien_vers_noeud_inconnu_refuse(reseau):
    reseau.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@seul", id="a"))
    with pytest.raises(NoeudInconnu):
        reseau.ajouter_lien(un_lien("a", "fantome"))


def test_preuve_exige_extrait_et_empreinte_valide():
    with pytest.raises(ValueError):
        une_preuve(extrait="  ")
    with pytest.raises(ValueError):
        une_preuve(empreinte_sha256="pas-un-sha")
    with pytest.raises(ValueError):
        une_preuve(debut_s=20.0, fin_s=10.0)
    ok = une_preuve(empreinte_sha256=empreinte_sha256(b"archive").upper())
    assert ok.empreinte_sha256 == empreinte_sha256(b"archive")


# -- 2. ajout seul -----------------------------------------------------------

def test_correction_garde_l_ancienne_version(reseau_garni):
    reseau_garni.corriger_lien("l1", "relu : indice, pas encore prouvé",
                               statut=StatutLien.INDICE, force_points=1.5)
    reseau_garni.corriger_noeud("a", "rôle revu après lecture de la bio", role=Role.AMPLIFICATEUR)

    assert reseau_garni.lien("l1").statut is StatutLien.INDICE
    assert reseau_garni.lien("l1").version == 2
    versions = reseau_garni.versions_lien("l1")
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].statut is StatutLien.PROPOSE  # l'ancienne reste intacte

    anciennes = reseau_garni.versions_noeud("a")
    assert [n.role for n in anciennes] == [Role.ORIGINE, Role.AMPLIFICATEUR]
    assert reseau_garni.noeud("a").role is Role.AMPLIFICATEUR


def test_journal_refuse_modification_et_effacement(reseau_garni):
    bd = reseau_garni._bd
    with pytest.raises(sqlite3.DatabaseError, match="ajout seul"):
        bd.execute("UPDATE evenements SET motif = 'truqué'")
    with pytest.raises(sqlite3.DatabaseError, match="ajout seul"):
        bd.execute("DELETE FROM evenements")


def test_historique_relit_tout_dans_l_ordre(reseau_garni):
    reseau_garni.corriger_lien("l2", "force précisée", force_points=2.0)
    evenements = reseau_garni.historique()
    assert [e.seq for e in evenements] == sorted(e.seq for e in evenements)
    assert evenements[0].genre == "affaire"
    seul = reseau_garni.historique("l2")
    assert [(e.version, e.motif) for e in seul] == [(1, "ajout"), (2, "force précisée")]


def test_ajouter_deux_fois_le_meme_noeud_refuse(reseau):
    reseau.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@x", id="x"))
    with pytest.raises(ValueError, match="corriger_noeud"):
        reseau.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@x bis", id="x"))


def test_question_reformulee_garde_l_ancienne(reseau):
    reseau.reformuler_question("Qui vend les stylos minceur ?", "plus précis")
    assert reseau.question_centrale == "Qui vend les stylos minceur ?"
    anciennes = [e.donnees["question"] for e in reseau.historique("question_centrale")]
    assert anciennes == [QUESTION, "Qui vend les stylos minceur ?"]


def test_reseau_survit_a_la_fermeture(tmp_path):
    fichier = str(tmp_path / "enquete.sqlite")
    with ReseauEnquete(QUESTION, fichier) as r:
        r.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@a", id="a"))
        r.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@b", id="b"))
        r.ajouter_lien(un_lien("a", "b", id="l"))
        q = r.poser_question("Qui dirige la société ?")
    with ReseauEnquete(QUESTION, fichier) as r:
        assert [n.id for n in r.noeuds()] == ["a", "b"]
        assert r.lien("l").preuves[0].extrait == "cent pour cent naturel"
        assert r.questions_ouvertes()[0].id == q.id
        assert r.question_centrale == QUESTION


# -- 3. export hiérarchique cohérent avec les compteurs ----------------------

def test_export_est_du_json_valide(reseau_garni):
    export = reseau_garni.exporter_pour_interface()
    assert json.loads(json.dumps(export, ensure_ascii=False)) == export


def test_export_hierarchie_et_compteurs(reseau_garni):
    export = reseau_garni.exporter_pour_interface()
    assert export["question"] == QUESTION
    assert [d["role"] for d in export["dossiers"]] == [r.value for r in Role]  # tous les rôles, même vides

    for dossier in export["dossiers"]:
        assert dossier["compteurs"]["pieces"] == len(dossier["pieces"])
        assert sum(dossier["compteurs"]["pieces_par_type"].values()) == len(dossier["pieces"])
        preuves_dossier = {p["url"] + p["extrait"] for pc in dossier["pieces"] for p in pc["preuves"]}
        assert dossier["compteurs"]["preuves"] == len(preuves_dossier)
        for piece in dossier["pieces"]:
            assert piece["role"] == dossier["role"]
            assert piece["compteurs"]["preuves"] == len(piece["preuves"])

    c = export["compteurs"]
    assert c["pieces"] == sum(d["compteurs"]["pieces"] for d in export["dossiers"]) == 4
    assert c["liens"] == len(export["liens"]) == 3

    par_role = {d["role"]: d["compteurs"]["pieces"] for d in export["dossiers"]}
    assert par_role["origine"] == 1 and par_role["amplificateur"] == 2
    assert par_role["beneficiaire"] == 1 and par_role["inconnu"] == 0

    # La preuve « minceur en dix jours » sert deux liens mais ne compte qu'une fois.
    assert c["preuves"] == 3
    relais = next(p for d in export["dossiers"] for p in d["pieces"] if p["id"] == "b")
    partagee = next(p for p in relais["preuves"] if p["extrait"] == "minceur en dix jours")
    assert sorted(partagee["liens"]) == ["l1", "l2"]
    assert (partagee["debut_s"], partagee["fin_s"]) == (12.0, 15.5)


def test_export_liens_portent_explication_et_preuves(reseau_garni):
    lien = next(l for l in reseau_garni.exporter_pour_interface()["liens"] if l["id"] == "l3")
    assert lien["explication"].startswith("B reprend")
    assert len(lien["preuves"]) == 2
    assert lien["source_libelle"] == "Minceur SAS" and lien["cible_libelle"] == "@premier"
    assert lien["statut"] == "propose"


def test_export_reseau_vide():
    with ReseauEnquete(QUESTION) as r:
        export = r.exporter_pour_interface()
    assert export["compteurs"]["pieces"] == 0
    assert all(d["pieces"] == [] for d in export["dossiers"])


# -- 4. questions ouvertes qui se comblent -----------------------------------

def test_question_ouverte_se_comble_par_une_piece(reseau_garni):
    q = reseau_garni.poser_question("Qui dirige la société ?")
    assert [x.id for x in reseau_garni.questions_ouvertes()] == [q.id]
    assert reseau_garni.exporter_pour_interface()["compteurs"]["zones_d_ombre"] == 1

    gerant = reseau_garni.ajouter_noeud(Noeud(TypeNoeud.PERSONNE, "M. Gérant", Role.MIS_EN_CAUSE))
    comblee = reseau_garni.combler_question(q.id, gerant.id)

    assert comblee.statut is StatutQuestion.COMBLEE and comblee.comblee_par == gerant.id
    assert reseau_garni.questions_ouvertes() == []
    export = reseau_garni.exporter_pour_interface()
    assert export["compteurs"]["zones_d_ombre"] == 0
    assert export["questions_ouvertes"][0]["comblee_par_libelle"] == "M. Gérant"
    # la question ouverte d'origine reste dans l'historique
    assert [e.donnees["statut"] for e in reseau_garni.historique(q.id)] == ["ouverte", "comblee"]


def test_combler_par_piece_inconnue_refuse(reseau):
    q = reseau.poser_question("Qui paie ?")
    with pytest.raises(NoeudInconnu):
        reseau.combler_question(q.id, "fantome")
    assert len(reseau.questions_ouvertes()) == 1


# -- 5. solidité en mots, échelle verbale ------------------------------------

def test_solidite_en_mots_deux_notes_separees():
    s = Solidite("A", 1)
    assert "document officiel" in s.en_mots() and "recoupée" in s.en_mots()
    assert s.matiere == "solide"
    # source très fiable, information non recoupée : les deux notes divergent
    assert Solidite("A", 4).en_mots() != Solidite("A", 1).en_mots()
    assert Solidite("D", 3).matiere == "indice"
    assert Solidite("F", 2).matiere == "inverifiable"
    assert Solidite("B", 6).matiere == "inverifiable"
    with pytest.raises(ValueError):
        Solidite("G", 1)
    with pytest.raises(ValueError):
        Solidite("A", 7)


def test_solidite_affichee_en_mots_dans_l_export(reseau_garni):
    societe = next(p for d in reseau_garni.exporter_pour_interface()["dossiers"]
                   for p in d["pieces"] if p["id"] == "s")
    assert societe["solidite"]["en_mots"].startswith("source de référence")
    assert societe["solidite"]["matiere"] == "solide"
    assert "fiabilite_source" in societe["solidite"]  # la note brute reste, jamais seule


@pytest.mark.parametrize("points, familles, attendu", [
    (-0.5, 2, EchelleVerbale.INDICES_CONTRAIRES),
    (0.4, 2, EchelleVerbale.COINCIDENCE_PROBABLE),
    (1.5, 1, EchelleVerbale.APPUI_FAIBLE),
    (2.5, 1, EchelleVerbale.APPUI_NET),
    (3.5, 1, EchelleVerbale.APPUI_NET),  # une seule famille plafonne à « appui net »
    (3.5, 2, EchelleVerbale.LIEN_FORT),
    (5.0, 2, EchelleVerbale.LIEN_FORT),  # très fort exige aussi 2 méthodes
])
def test_echelle_verbale(points, familles, attendu):
    assert echelle_verbale(points, familles) is attendu


def test_echelle_tres_fort_et_identifiant_officiel():
    assert echelle_verbale(4.2, 2, 2) is EchelleVerbale.LIEN_TRES_FORT
    assert echelle_verbale(3.5, 1, identifiant_officiel=True) is EchelleVerbale.LIEN_FORT


def test_lien_conteste_des_un_point_de_contre_indice():
    assert not un_lien("a", "b", force_points=3.5, contre_points=0.9).conteste
    assert un_lien("a", "b", force_points=3.5, contre_points=1.0).conteste
