"""Tests de la note de rédaction : tout hors réseau, sur un export fictif bâti avec ReseauEnquete."""

import re
from datetime import datetime, timezone

import pytest

from enquete import note_redaction as nr
from enquete.reseau import (
    FamilleLien, Lien, Noeud, Preuve, ReseauEnquete, Role, StatutLien, TypeNoeud,
)

COLLECTE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
MAINTENANT = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
SHA = "ab12cd34" + "0" * 56


def preuve(url, extrait, source="Oriane", debut=None, sha=None):
    return Preuve(url=url, extrait=extrait, source_donnee=source, date_collecte=COLLECTE,
                  debut_s=debut, empreinte_sha256=sha)


def lien(a, b, points, familles=1, type_lien="reprend", famille=FamilleLien.RECIT,
         preuves=None, statut=StatutLien.PROUVE, contre=0.0, explication="Même phrase, trois heures après."):
    return Lien(type_lien=type_lien, famille=famille, source_id=a.id, cible_id=b.id,
                preuves=tuple(preuves or [preuve(f"https://t.test/{a.id}", "la phrase exacte", debut=12.0, sha=SHA)]),
                explication=explication, methode="test", agent="test", statut=statut,
                force_points=points, contre_points=contre, familles_independantes=familles,
                methodes_independantes=2 if points >= 4 else 1)


@pytest.fixture
def monde():
    r = ReseauEnquete("Revente de billets : qui est derrière ?")
    n = {}

    def ajouter(cle, type_, libelle, role, **attr):
        n[cle] = r.ajouter_noeud(Noeud(type=type_, libelle=libelle, role=role, attributs=attr))

    ajouter("vendeur", TypeNoeud.COMPTE, "@vendeur", Role.MIS_EN_CAUSE, plateforme="TikTok", email="x@y.fr")
    ajouter("origine", TypeNoeud.COMPTE, "@premier", Role.ORIGINE, plateforme="TikTok")
    ajouter("relais", TypeNoeud.COMPTE, "@relais", Role.AMPLIFICATEUR, plateforme="Instagram")
    ajouter("temoin", TypeNoeud.COMPTE, "@temoin", Role.TEMOIN, plateforme="TikTok")
    ajouter("victime", TypeNoeud.COMPTE, "@victime", Role.VICTIME)
    ajouter("mineur", TypeNoeud.COMPTE, "@ado", Role.TEMOIN, mineur_possible=True)
    ajouter("prive", TypeNoeud.COMPTE, "@madame_dupont", Role.VICTIME, particulier=True)
    ajouter("secret", TypeNoeud.DOCUMENT, "Facture reçue d'une source", Role.INFRASTRUCTURE, confidentiel=True)
    ajouter("site", TypeNoeud.SITE, "billets-pas-chers.example", Role.INFRASTRUCTURE)
    ajouter("page", TypeNoeud.SITE, "lien.example/vendeur", Role.INFRASTRUCTURE, a_deplier=True, indices=["x"])
    ajouter("portefeuille", TypeNoeud.DOCUMENT, "bc1qexemple", Role.BENEFICIAIRE)
    ajouter("societe", TypeNoeud.SOCIETE, "Billets Express SAS", Role.BENEFICIAIRE, siren="123456789")
    ajouter("video", TypeNoeud.VIDEO, "Vidéo du vendeur", Role.AMPLIFICATEUR)

    r.ajouter_lien(lien(n["relais"], n["origine"], 4.5, familles=2))           # lien fort
    r.ajouter_lien(lien(n["vendeur"], n["site"], 2.5, type_lien="publie", famille=FamilleLien.INFRASTRUCTURE,
                        preuves=[preuve("https://t.test/boutique", "billets-pas-chers.example", sha=SHA)]))  # appui net
    r.ajouter_lien(lien(n["vendeur"], n["societe"], 1.2, type_lien="profite_a", famille=FamilleLien.ARGENT,
                        preuves=[preuve("https://t.test/mentions", "Billets Express SAS")]))  # appui faible
    r.ajouter_lien(lien(n["vendeur"], n["origine"], 4.2, familles=2, contre=1.5,
                        preuves=[preuve("https://t.test/conteste", "même voix", debut=3.0, sha=SHA)]))  # fort, contesté
    r.ajouter_lien(lien(n["vendeur"], n["video"], 3.5, familles=2, type_lien="diffuse",
                        preuves=[preuve("https://t.test/journal", "ce que m'a dit ma source", source="journaliste")]))
    r.ajouter_lien(lien(n["vendeur"], n["secret"], 3.5, familles=2, type_lien="paye_par",
                        preuves=[preuve("https://t.test/secret", "facture confidentielle", source="Oriane")]))
    r.ajouter_lien(lien(n["vendeur"], n["prive"], 4.5, familles=2, type_lien="vend_a"))
    r.ajouter_lien(lien(n["temoin"], n["vendeur"], 0.4, preuves=[preuve("https://t.test/hasard", "même mot")]))
    r.ajouter_lien(lien(n["victime"], n["vendeur"], 1.0, statut=StatutLien.PROPOSE))
    r.poser_question("Quel marché public de billetterie la mairie a-t-elle signé avec Billets Express ?")
    r.poser_question("Qui encaisse l'argent des billets ?")
    r.poser_question("Que dit la plateforme ?")
    export = r.exporter_pour_interface()
    r.fermer()
    for d in export["dossiers"]:  # le type « portefeuille » est celui de l'intégration, pas encore de l'enum
        for p in d["pieces"]:
            if p["libelle"] == "bc1qexemple":
                p["type"] = "portefeuille"
    return export, n


def noeud_id(export, libelle):
    return next(p["id"] for d in export["dossiers"] for p in d["pieces"] if p["libelle"] == libelle)


def test_structure_et_compteur_a_zero(monde):
    note = nr.note_enquete(monde[0], maintenant=MAINTENANT)
    assert set(note["sections"]) >= {"ce_qu_on_sait", "ce_qu_on_ne_sait_pas", "prochaines_actions",
                                     "qui_contacter", "risques", "methode_et_limites"}
    assert note["controle"]["affirmations_sans_piece"] == 0
    assert note["controle"]["affirmations"] == len(note["sections"]["ce_qu_on_sait"]) > 0
    assert note["date"].startswith("2026-11-07")
    numeros = {n["n"] for n in note["notes"]}
    for ligne in note["sections"]["ce_qu_on_sait"]:
        assert ligne["notes"] and set(ligne["notes"]) <= numeros
    for n in note["notes"]:
        assert set(n) == {"n", "piece_id", "url", "extrait", "seconde", "empreinte8", "date_collecte", "source"}
    assert {p["numero"] for p in note["pieces"]} >= {n["piece_id"] for n in note["notes"]}
    assert note["notes"][0]["empreinte8"] == "ab12cd34" or note["notes"][0]["empreinte8"] is None


def test_formulation_selon_l_echelle_et_conteste(monde):
    sait = nr.note_enquete(monde[0], maintenant=MAINTENANT)["sections"]["ce_qu_on_sait"]
    par_niveau = {}
    for ligne in sait:
        par_niveau.setdefault(ligne["niveau"], []).append(ligne["texte"])
    assert any(t.startswith("@relais reprend") for t in par_niveau["affirme"])
    assert all("Des éléments indiquent" not in t and "Piste" not in t for t in par_niveau["affirme"])
    assert any(t.startswith("Des éléments indiquent que @vendeur publie billets-pas-chers.example") for t in par_niveau["indique"])
    assert any(t.startswith("Piste, à vérifier") for t in par_niveau["piste"])
    conteste = [t for t in par_niveau["affirme"] if "contesté" in t]
    assert len(conteste) == 1 and "@vendeur" in conteste[0]


def test_phrase_sans_piece_jamais_dans_sait(monde):
    export, n = monde
    export["liens"].append({**export["liens"][0], "id": "sans-preuve", "preuves": [], "explication": "Un fait en l'air."})
    note = nr.note_enquete(export, maintenant=MAINTENANT)
    assert all("Un fait en l'air" not in l["texte"] for l in note["sections"]["ce_qu_on_sait"])
    assert any("Un fait en l'air" in q["texte"] for q in note["sections"]["ce_qu_on_ne_sait_pas"])
    assert note["controle"]["affirmations_sans_piece"] == 0


def test_erreur_si_on_force_une_affirmation_sans_piece(monde, monkeypatch):
    def triche(export, notes):
        return [{"texte": "Il est coupable.", "notes": [], "niveau": "affirme"}], [], 0

    monkeypatch.setattr(nr, "_phrases_sait", triche)
    with pytest.raises(nr.AffirmationSansPiece):
        nr.note_enquete(monde[0], maintenant=MAINTENANT)
    faux = {"notes": [{"n": 1}], "sections": {"ce_qu_on_sait": [{"texte": "x", "notes": [2]}]}}
    with pytest.raises(nr.AffirmationSansPiece):
        nr.verifier_note(faux)


def test_extras_avec_et_sans_preuve(monde):
    extras = [{"titre": "Argent", "lignes": [
        {"texte": "Le portefeuille a reçu 3 virements.", "preuves": [preuve("https://explorer.test/tx/1", "tx 1").vers_dict()]},
        {"texte": "Il a tout retiré.", "preuves": []},
    ]}]
    note = nr.note_enquete(monde[0], maintenant=MAINTENANT, extras=extras)
    lignes = note["sections"]["complements"][0]["lignes"]
    assert len(lignes) == 1 and lignes[0]["notes"]
    assert any("Il a tout retiré" in q["texte"] for q in note["sections"]["ce_qu_on_ne_sait_pas"])
    assert note["controle"]["affirmations_sans_piece"] == 0


def test_particulier_et_mineur_jamais_nommes_ni_proposes(monde):
    note = nr.note_enquete(monde[0], maintenant=MAINTENANT)
    texte = nr.en_markdown(note)
    assert "madame_dupont" not in texte and "@ado" not in texte
    libelles = [c["libelle"] for c in note["sections"]["qui_contacter"]]
    assert "@ado" not in libelles and "@madame_dupont" not in libelles
    assert any(r["type"] == "particulier" for r in note["sections"]["risques"])


def test_qui_contacter_ordre_mis_en_cause_dernier_sans_email(monde):
    contacts = nr.note_enquete(monde[0], maintenant=MAINTENANT)["sections"]["qui_contacter"]
    roles = [c["role"] for c in contacts]
    assert roles[-1] == "mis_en_cause" and roles.count("mis_en_cause") == 1
    assert roles.index("temoin") < roles.index("origine") < roles.index("amplificateur") < roles.index("mis_en_cause")
    assert [c["ordre"] for c in contacts] == list(range(1, len(contacts) + 1))
    assert contacts[-1]["pas_avant"] == "après la lettre du contradictoire et au moins 2 liens prouvés le visant"
    assert all(c["pas_avant"] is None for c in contacts[:-1])
    for c in contacts:
        assert len(c["questions"]) == 3
        assert "profil public, non vérifié" in c["canal"]
        assert "messagerie publique" in c["canal"]
        assert not re.search(r"@\w+\.\w+|\+?\d[\d .]{7,}|e-?mail|mail", c["canal"].lower())
    assert "x@y.fr" not in str(contacts)


def test_prochaines_actions_regles_et_priorite(monde):
    actions = nr.note_enquete(monde[0], maintenant=MAINTENANT)["sections"]["prochaines_actions"]
    textes = " | ".join(a["action"] for a in actions)
    assert "Archiver « @relais »" in textes or "Archiver" in textes
    assert "Profiler le domaine « billets-pas-chers.example »" in textes
    assert "registre des entreprises" in textes
    assert "Profiler le portefeuille « bc1qexemple »" in textes
    assert "Envoyer la lettre du contradictoire à « @vendeur »" in textes
    assert "seconde famille" in textes
    assert "Déplier la page de liens" in textes
    assert [a["priorite"] for a in actions] == sorted((a["priorite"] for a in actions), reverse=True)
    portefeuille = next(a for a in actions if "portefeuille" in a["action"])
    # bénéficiaire (1,5), débloque « Qui encaisse l'argent » (2), 0 crédit (1)
    assert portefeuille["priorite"] == 3.0
    lettre = next(a for a in actions if "contradictoire" in a["action"])
    assert lettre["cout"] == "humain" and lettre["priorite"] == 0.5  # 1 x 1,5 / 3


def test_question_sans_piste_devient_action():
    r = ReseauEnquete("Sujet")
    r.poser_question("Hmm ?")
    export = r.exporter_pour_interface()
    note = nr.note_enquete(export, maintenant=MAINTENANT)
    assert note["sections"]["ce_qu_on_ne_sait_pas"][0]["pistes"] == []
    action = note["sections"]["prochaines_actions"][0]
    assert action["priorite"] == round(2 / 3, 2) and action["cout"] == "humain"


def test_ne_sait_pas_numerote_avec_pistes(monde):
    lignes = nr.note_enquete(monde[0], maintenant=MAINTENANT)["sections"]["ce_qu_on_ne_sait_pas"]
    assert [l["numero"] for l in lignes] == list(range(1, len(lignes) + 1))
    marche = next(l for l in lignes if "marché" in l["texte"])
    assert any("CADA" in p for p in marche["pistes"])
    assert any(l["texte"].startswith("Non établi") for l in lignes)


def test_markdown_notes_en_fin(monde):
    md = nr.en_markdown(nr.note_enquete(monde[0], maintenant=MAINTENANT))
    assert md.index("## Ce qu'on sait") < md.index("## Notes")
    assert re.search(r"reprend le contenu de @premier.*\[\d+\]", md)
    assert "Contrôle : " in md and "0 sans pièce" in md
    assert "\n[1] P1 : https://" in md


def test_lettre_retire_journaliste_et_confidentiel(monde):
    export, _ = monde
    vendeur = noeud_id(export, "@vendeur")
    echeance = datetime(2026, 11, 12, 18, 0)  # sans fuseau : heure de Paris
    lettre = nr.lettre_contradictoire(export, vendeur, echeance=echeance,
                                      parution=datetime(2026, 11, 14, 8, 0))
    assert lettre["statut"] == "brouillon : le journaliste l'envoie lui-même"
    assert lettre["tentatives"] == []
    assert all(a["texte"].startswith("Selon nos informations, ") and a["notes"] for a in lettre["allegations"])
    urls_retirees = {r["url"] for r in lettre["retraits"]}
    assert urls_retirees == {"https://t.test/journal", "https://t.test/secret"}
    assert all(r["allegation_retiree"] for r in lettre["retraits"])
    assert "ce que m'a dit ma source" not in lettre["corps"] and "facture confidentielle" not in lettre["corps"]
    assert all("diffuse Vidéo du vendeur" not in a["texte"] for a in lettre["allegations"])
    assert all("vend_a" not in a["texte"] and "madame_dupont" not in a["texte"] for a in lettre["allegations"])
    assert "madame_dupont" not in lettre["corps"]
    assert any("contesté" in a["texte"] for a in lettre["allegations"])


def test_lettre_echeance_avec_fuseau(monde):
    export, _ = monde
    vendeur = noeud_id(export, "@vendeur")
    utc = datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc)
    lettre = nr.lettre_contradictoire(export, vendeur, echeance=utc)
    assert lettre["echeance"]["heure"] == "18:00" and lettre["echeance"]["fuseau"] == "Europe/Paris"
    assert lettre["echeance"]["texte"] == "12/11/2026 à 18 h 00, heure de Paris"
    assert lettre["echeance"]["iso"].endswith("+01:00")
    assert "heure de Paris" in lettre["corps"]
    ete = nr.lettre_contradictoire(export, vendeur, echeance=datetime(2026, 7, 1, 12, 0))
    assert ete["echeance"]["iso"].endswith("+02:00")


def test_lettre_refus(monde):
    export, _ = monde
    vendeur = noeud_id(export, "@vendeur")
    with pytest.raises(ValueError):
        nr.lettre_contradictoire(export, noeud_id(export, "@ado"), echeance=datetime(2026, 11, 12, 18))
    with pytest.raises(ValueError):
        nr.lettre_contradictoire(export, vendeur, echeance=datetime(2026, 11, 14, 9), parution=datetime(2026, 11, 14, 8))


def test_cada_dates_et_document(monde):
    demandes = nr.demandes_cada(monde[0], maintenant=MAINTENANT)
    assert len(demandes) == 1
    d = demandes[0]
    assert d["document"].startswith("marché public de billetterie")
    assert d["administration"] == "à préciser" and d["statut"] == "à envoyer"
    assert d["date_limite_reponse"] == "2026-12-07"
    assert d["saisine_cada_avant"] == "2027-02-07"
    assert "silence" in d["corps"] and "2026-12-07" not in d["corps"] and "07/12/2026" in d["corps"]


def test_cada_fin_de_mois_et_faux_amis():
    r = ReseauEnquete("Sujet")
    r.poser_question("Quelle subvention la région a-t-elle versée en 2025 ?")
    r.poser_question("Il a été arrêté en rapport avec une affaire ?")
    export = r.exporter_pour_interface()
    demandes = nr.demandes_cada(export, maintenant=datetime(2026, 11, 30, 9, 0, tzinfo=timezone.utc))
    assert [d["document"].split()[0] for d in demandes] == ["subvention"]
    assert demandes[0]["date_limite_reponse"] == "2026-12-30"
    assert demandes[0]["saisine_cada_avant"] == "2027-02-28"


def test_export_vide_et_sans_champs_optionnels():
    r = ReseauEnquete("Vide")
    note = nr.note_enquete(r.exporter_pour_interface(), maintenant=MAINTENANT)
    assert note["controle"] == {"affirmations": 0, "affirmations_sans_piece": 0}
    assert "Rien d'établi" in nr.en_markdown(note)


def test_structure_notes_plafonnees_et_actions_regroupees():
    r = ReseauEnquete("Sujet")
    compte = r.ajouter_noeud(Noeud(type=TypeNoeud.COMPTE, libelle="@a", role=Role.ORIGINE))
    autre = r.ajouter_noeud(Noeud(type=TypeNoeud.COMPTE, libelle="@b", role=Role.AMPLIFICATEUR))
    videos = [r.ajouter_noeud(Noeud(type=TypeNoeud.VIDEO, libelle=f"Vidéo {i}", role=Role.AMPLIFICATEUR)) for i in range(4)]
    for v in videos:
        r.ajouter_lien(lien(compte, v, 1.0, type_lien="publie", preuves=[preuve(f"https://t.test/v/{v.id}", "auteur")]))
    nombreuses = [preuve(f"https://t.test/{i}", f"phrase {i}", debut=float(i)) for i in range(6)]
    r.ajouter_lien(lien(autre, compte, 4.5, familles=2, preuves=nombreuses))
    note = nr.note_enquete(r.exporter_pour_interface(), maintenant=MAINTENANT)
    sait = note["sections"]["ce_qu_on_sait"]
    assert len(sait) == 1 and "publie" not in sait[0]["texte"]  # un compte qui publie sa vidéo n'est pas une information
    assert len(sait[0]["notes"]) == 3 and len(note["notes"]) == 3
    archives = [a for a in note["sections"]["prochaines_actions"] if a["action"].startswith("Archiver")]
    assert len(archives) == 1 and "4 pièce(s)" in archives[0]["action"]


def test_contacts_plafonnes_par_role():
    r = ReseauEnquete("Sujet")
    for i in range(7):
        r.ajouter_noeud(Noeud(type=TypeNoeud.COMPTE, libelle=f"@t{i}", role=Role.TEMOIN))
    r.ajouter_noeud(Noeud(type=TypeNoeud.COMPTE, libelle="@m", role=Role.MIS_EN_CAUSE))
    contacts = nr.note_enquete(r.exporter_pour_interface(), maintenant=MAINTENANT)["sections"]["qui_contacter"]
    assert [c["role"] for c in contacts] == ["temoin"] * 3 + ["mis_en_cause"]
