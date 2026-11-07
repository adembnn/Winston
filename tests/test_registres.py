"""Tests des registres (BODACC, Recherche d'entreprises, HATVP) : transport simulé, aucun appel réseau.

Les réponses enregistrées sont fictives mais au format réel vérifié le 7 nov. 2026.
"""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from enquete.sources.infra import Indice
from enquete.sources.registres import FIN_OMBRE, Registres, evenements_frise

REPONSES = Path(__file__).parent / "reponses"
BODACC = json.loads((REPONSES / "bodacc_exemple.json").read_text("utf-8"))
ENTREPRISES = json.loads((REPONSES / "entreprises_mandats_exemple.json").read_text("utf-8"))
HATVP = (REPONSES / "hatvp_liste_exemple.csv").read_text("utf-8")
MAINTENANT = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
GEL = "2026-10-01"
SIREN = "552100554"


def _enveloppe(*resultats):
    return {"results": list(resultats), "total_results": len(resultats), "page": 1, "per_page": 25, "total_pages": 1}


def _dirigeant(nom, prenoms, annee, qualite):
    return {"nom": nom, "prenoms": prenoms, "annee_de_naissance": annee, "qualite": qualite,
            "type_dirigeant": "personne physique"}


def _societe(siren, nom, adresse="1 RUE DE L EXEMPLE 75001 PARIS", nj="5710", dirigeants=()):
    return {"siren": siren, "nom_complet": nom, "nom_raison_sociale": nom, "date_creation": "2016-01-01",
            "date_fermeture": None, "nature_juridique": nj, "etat_administratif": "A",
            "siege": {"adresse": adresse, "code_postal": "75001", "libelle_commune": "PARIS"},
            "dirigeants": list(dirigeants), "finances": {}}


class Faux:
    """Faux serveurs : répondent selon l'hôte et gardent les requêtes."""

    def __init__(self, sans_dpc=False, hatvp=HATVP, **_):
        self.requetes: list[httpx.Request] = []
        self.fiches = copy.deepcopy(ENTREPRISES["fiches"])
        self.bodacc = copy.deepcopy(BODACC)
        if sans_dpc:
            self.bodacc["results"] = [r for r in self.bodacc["results"] if r["familleavis"] != "dpc"]
        self.hatvp = hatvp
        self.statuts: list[int] = []  # statuts forcés, consommés dans l'ordre
        self.martin = [_societe(f"41000000{i}", f"GROUPE EXEMPLE {i}", dirigeants=[
            _dirigeant("MARTIN", "Paul", "1970", "Gérant")]) for i in range(1, 13)]
        self.martin.append(_societe("419999999", "HOMONYME MARTIN", dirigeants=[
            _dirigeant("MARTIN", "Paul", "1990", "Gérant")]))
        self.adresse: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requetes.append(req)
        if self.statuts:
            statut = self.statuts.pop(0)
            if statut != 200:
                return httpx.Response(statut, text="", headers={"Retry-After": "1"} if statut == 429 else {})
        hote, p = req.url.host, req.url.params
        if hote == "bodacc-datadila.opendatasoft.com":
            return httpx.Response(200, json=self.bodacc)
        if hote == "www.hatvp.fr":
            return httpx.Response(200, text=self.hatvp)
        if hote == "recherche-entreprises.api.gouv.fr":
            if "nom_personne" in p:
                if p["nom_personne"] == "DUPONT-MARTIN":
                    return httpx.Response(200, json=ENTREPRISES["mandats_dupont_martin"])
                if p["nom_personne"] == "MARTIN":
                    return httpx.Response(200, json=_enveloppe(*self.martin))
                return httpx.Response(200, json=_enveloppe())
            q = p.get("q", "")
            if q.isdigit():
                return httpx.Response(200, json=self.fiches.get(q, _enveloppe()))
            return httpx.Response(200, json=_enveloppe(*self.adresse))
        return httpx.Response(404, text="")

    def appels(self, hote_ou_morceau: str) -> list[httpx.Request]:
        return [r for r in self.requetes if hote_ou_morceau in str(r.url)]


def registres(tmp_path, faux=None, **options) -> tuple[Registres, Faux, list[float]]:
    faux = faux or Faux()
    pauses: list[float] = []
    options.setdefault("maintenant", lambda: MAINTENANT)
    options.setdefault("dormir", pauses.append)
    options.setdefault("horloge", lambda: 0.0)
    reg = Registres(dossier_cache=tmp_path / "registres", transport=httpx.MockTransport(faux), **options)
    return reg, faux, pauses


def tout_le_texte(indices) -> str:
    return json.dumps([i.vers_dict() if isinstance(i, Indice) else i for i in indices], ensure_ascii=False, default=str)


# ----------------------------------------------------------------------------- BODACC

def test_bodacc_un_indice_par_avis_avec_url_officielle(tmp_path):
    reg, faux, _ = registres(tmp_path)
    avis = reg.bodacc(SIREN)
    assert len(avis) == 6  # l'avis d'une autre société (registre 999999999) est écarté
    assert all(isinstance(a, Indice) and a.source == "bodacc" for a in avis)
    assert all(a.url.startswith("https://www.bodacc.fr/pages/annonces-commerciales-detail/") for a in avis)
    assert [a.donnees["date_parution"] for a in avis] == sorted((a.donnees["date_parution"] for a in avis), reverse=True)
    assert {a.donnees["famille"] for a in avis} == {"radiation", "vente", "depot_comptes", "modification",
                                                    "procedure_collective", "creation"}
    creation = next(a for a in avis if a.donnees["famille"] == "creation")
    assert set(creation.donnees) >= {"siren", "date_parution", "famille", "type_avis", "tribunal", "resume", "url_avis"}
    assert creation.vers_preuve().url == creation.url
    requete = faux.appels("opendatasoft")[0].url.params
    assert requete["where"] == f'registre like "{SIREN}"'


def test_bodacc_avis_posterieur_au_gel_ignore(tmp_path):
    reg, _, _ = registres(tmp_path, date_gel=GEL)
    avis = reg.bodacc(SIREN)
    assert len(avis) == 5
    assert all(a.donnees["date_parution"] <= "2026-10-01" for a in avis)
    assert "radiation" not in {a.donnees["famille"] for a in avis}
    assert any("postérieur" in j["raison"] for j in reg.journal)


def test_bodacc_ne_sort_ni_nom_ni_adresse_de_personne(tmp_path):
    reg, _, _ = registres(tmp_path)
    texte = tout_le_texte(reg.bodacc(SIREN))
    assert "SENTINELLE" not in texte and "Secrete" not in texte
    assert "Désigne Me" not in texte  # le complément libre d'un jugement n'est pas recopié


def test_bodacc_siren_invalide(tmp_path):
    reg, _, _ = registres(tmp_path)
    with pytest.raises(ValueError):
        reg.bodacc("12")


# ----------------------------------------------------------------------- vie de société

def test_vie_de_societe_evenements_signaux_finances(tmp_path):
    reg, _, _ = registres(tmp_path, date_gel=GEL)
    vie = reg.vie_de_societe(SIREN)
    dates = [e["date"] for e in vie["evenements"]]
    assert dates == sorted(dates)
    assert vie["evenements"][0]["type"] == "creation" and vie["evenements"][0]["date"] == "2015-04-01"
    assert all({"date", "type", "libelle", "source_url", "extrait"} <= set(e) for e in vie["evenements"])
    types = {s["type"] for s in vie["signaux"]}
    assert types == {"changement_dirigeant", "transfert_siege", "cession", "procedure_collective"}
    assert all(s["preuve"]["url"].startswith("https://www.bodacc.fr/") and s["lecture"] for s in vie["signaux"])
    assert vie["finances"]["2024"] == {"chiffre_affaires": 1200000, "resultat_net": 85000}
    assert "avertissement_gel" in vie


def test_achat_de_fonds_n_est_pas_une_cession_de_la_societe(tmp_path):
    faux = Faux()
    for r in faux.bodacc["results"]:
        if r["familleavis"] == "vente":
            r["acte"] = json.dumps({"vente": {"categorieVente": "Achat d'un fonds par une personne morale (insertion provisoire)"}})
    reg, _, _ = registres(tmp_path, faux, date_gel=GEL)
    vie = reg.vie_de_societe(SIREN)
    assert "cession" not in {s["type"] for s in vie["signaux"]}
    assert "vente" in {e["type"] for e in vie["evenements"]}  # l'événement reste sur la frise


def test_vie_de_societe_radiation_sans_gel_et_finances_filtrees_par_le_gel(tmp_path):
    reg, _, _ = registres(tmp_path)
    assert "radiation" in {s["type"] for s in reg.vie_de_societe(SIREN)["signaux"]}
    reg2, _, _ = registres(tmp_path / "autre", date_gel="2024-06-30")
    vie = reg2.vie_de_societe(SIREN)
    assert list(vie["finances"]) == ["2023"]  # 2024 n'est pas « vu » à la date de gel


def test_comptes_non_deposes_quand_aucun_avis_de_depot(tmp_path):
    reg, _, _ = registres(tmp_path / "a", Faux(sans_dpc=True), date_gel=GEL)
    signaux = reg.vie_de_societe(SIREN)["signaux"]
    assert "comptes_non_deposes" in {s["type"] for s in signaux}
    reg2, _, _ = registres(tmp_path / "b", date_gel=GEL)
    assert "comptes_non_deposes" not in {s["type"] for s in reg2.vie_de_societe(SIREN)["signaux"]}


def test_societe_creee_apres_le_gel_est_ignoree(tmp_path):
    reg, faux, _ = registres(tmp_path, date_gel="2014-01-01")
    vie = reg.vie_de_societe(SIREN)
    assert vie["evenements"] == [] and "ignore" in vie
    assert not faux.appels("opendatasoft")


def test_entrepreneur_individuel_siege_reduit_a_la_commune_et_nom_masque(tmp_path):
    faux = Faux()
    ei = _societe("300000001", "JEAN SENTINELLE", adresse="3 RUE PRIVEE 38000 GRENOBLE", nj="1000",
                  dirigeants=[_dirigeant("SENTINELLE", "Jean", "1975", None)])
    faux.fiches["300000001"] = _enveloppe(ei)
    reg, _, _ = registres(tmp_path, faux)
    ind = reg.vie_de_societe("300000001")["indices"][0]
    assert ind.donnees["siege"] == {"libelle_commune": "PARIS"}
    assert "RUE PRIVEE" not in tout_le_texte([ind]) and "SENTINELLE" not in tout_le_texte([ind])
    assert ind.donnees["nom"] == "entrepreneur individuel J. S."


# ---------------------------------------------------------------------------- mandats

def test_mandats_homonyme_ecarte_par_l_annee_et_compte_a_part(tmp_path):
    reg, _, _ = registres(tmp_path)
    detail = reg.mandats_avec_comptes("DUPONT-MARTIN", "Jeanne-Marie", annee_naissance=1980)
    sirens = {i.donnees["siren"] for i in detail["mandats"]}
    assert sirens == {"552100554", "400000001", "400000004", "400000005"}
    assert "400000002" not in sirens  # né en 1955 : un homonyme
    assert detail["homonymes_ecartes"] == 1
    assert detail["entreprises_individuelles_ecartees"] == 1
    assert detail["annee_inconnue"] == 1
    assert all(i.donnees["homonymes_ecartes"] == 1 for i in detail["mandats"])
    sans_annee = reg.mandats("DUPONT-MARTIN", "Jeanne-Marie")
    assert "400000002" in {i.donnees["siren"] for i in sans_annee}  # sans année, rien ne tranche


def test_mandats_societe_creee_apres_le_gel_ignoree(tmp_path):
    reg, _, _ = registres(tmp_path, date_gel=GEL)
    detail = reg.mandats_avec_comptes("DUPONT-MARTIN", "Jeanne-Marie", annee_naissance=1980)
    assert "400000004" not in {i.donnees["siren"] for i in detail["mandats"]}
    assert detail["apres_gel_ignorees"] == 1
    assert any("créée après la date de gel" in j["raison"] for j in reg.journal)


def test_mandats_masques_en_initiales_par_defaut_et_option_pour_lever(tmp_path):
    reg, _, _ = registres(tmp_path)
    masque = reg.mandats("DUPONT-MARTIN", "Jeanne-Marie", annee_naissance=1980)
    texte = tout_le_texte(masque)
    assert "J.-M. D.-M." in texte and "Jeanne" not in texte and "DUPONT" not in texte.replace("DUPONT-MARTIN", "")
    assert "annee_de_naissance" not in texte and "1980" not in texte
    leve = reg.mandats("DUPONT-MARTIN", "Jeanne-Marie", annee_naissance=1980, masquer_personnes=False)
    assert "Jeanne-Marie DUPONT-MARTIN" in tout_le_texte(leve)
    assert "1980" not in tout_le_texte(leve)  # même levé, la naissance n'est jamais affichée
    reg2, _, _ = registres(tmp_path / "x", masquer_personnes=False)
    assert "Jeanne-Marie DUPONT-MARTIN" in tout_le_texte(reg2.mandats("DUPONT-MARTIN", "Jeanne-Marie", annee_naissance=1980))


def test_mandats_ne_donne_jamais_l_adresse_d_un_entrepreneur_individuel(tmp_path):
    reg, _, _ = registres(tmp_path)
    texte = tout_le_texte(reg.mandats("DUPONT-MARTIN", "Jeanne-Marie", annee_naissance=1980))
    assert "RUE PRIVEE" not in texte


# ----------------------------------------------------------------- chaîne de direction

def test_chaine_deux_niveaux_fins_et_commissaire_non_remonte(tmp_path):
    reg, faux, _ = registres(tmp_path)
    chaine = reg.chaine_de_controle(SIREN)
    racine = chaine["racine"]
    types = [m["type"] for m in racine["dirigeants"]]
    assert types == ["personne physique", "personne morale", "commissaire_aux_comptes"]
    assert racine["dirigeants"][2]["remonte"] is False
    assert not [r for r in faux.requetes if r.url.params.get("q") == "732829320"]  # le cabinet n'est pas ouvert
    holding = racine["dirigeants"][1]["enfant"]
    assert holding["siren"] == "123456782" and holding["niveau"] == 2
    fins = {tuple(f["chemin"][-1:]): f["fin"] for f in chaine["fins"]}
    assert fins[("J.-M. D.-M.",)] == "personne physique (J.-M. D.-M.)"
    assert fins[("P. M.",)] == "personne physique (P. M.)"
    assert any(f["fin"] == FIN_OMBRE and "sans SIREN" in f["raison"] for f in chaine["fins"])
    assert all(m.get("indice") is not None for m in racine["dirigeants"])
    assert "Jeanne" not in tout_le_texte([m["indice"] for m in racine["dirigeants"]])


def test_chaine_signal_nombreux_mandats_au_dela_du_seuil_seulement(tmp_path):
    reg, _, _ = registres(tmp_path)
    chaine = reg.chaine_de_controle(SIREN)
    martin = chaine["racine"]["dirigeants"][1]["enfant"]["dirigeants"][1]
    assert martin["nb_mandats"] == 12 and martin["homonymes_ecartes"] == 1  # le Martin né en 1990 est écarté
    assert [s["type"] for s in martin["signaux"]] == ["nombreux_mandats"]
    assert martin["signaux"][0]["libelle"] == "nombreux mandats : prête-nom possible, ou dirigeant de groupe"
    assert martin["signaux"][0]["lecture"]  # l'explication innocente accompagne le signal
    dupont = chaine["racine"]["dirigeants"][0]
    assert dupont["nb_mandats"] == 4 and dupont["signaux"] == []  # sous le seuil de 10
    assert len(chaine["signaux"]) == 1
    haut = reg.chaine_de_controle(SIREN, seuil_mandats=12)
    assert haut["signaux"] == []  # « au-delà » : 12 n'est pas > 12


def test_chaine_profondeur_max_arrete_en_zone_d_ombre(tmp_path):
    reg, _, _ = registres(tmp_path)
    chaine = reg.chaine_de_controle(SIREN, profondeur_max=1)
    holding = chaine["racine"]["dirigeants"][1]
    assert "enfant" not in holding and holding["fin"] == FIN_OMBRE and "profondeur" in holding["raison"]
    assert "avertissement_gel" not in chaine


def test_chaine_boucle_et_fiche_introuvable_sont_des_zones_d_ombre(tmp_path):
    faux = Faux()
    faux.fiches["123456782"]["results"][0]["dirigeants"].append(
        {"denomination": "SOCIETE EXEMPLE", "siren": SIREN, "qualite": "Associé", "type_dirigeant": "personne morale"})
    faux.fiches["123456782"]["results"][0]["dirigeants"].append(
        {"denomination": "FANTOME SAS", "siren": "888888888", "qualite": "Associé", "type_dirigeant": "personne morale"})
    reg, _, _ = registres(tmp_path, faux)
    chaine = reg.chaine_de_controle(SIREN)
    raisons = " | ".join(f["raison"] or "" for f in chaine["fins"] if f["fin"] == FIN_OMBRE)
    assert "boucle" in raisons and "introuvable" in raisons


def test_chaine_avec_gel_previent_que_les_organes_sont_actuels(tmp_path):
    reg, _, _ = registres(tmp_path, date_gel=GEL)
    assert "avertissement_gel" in reg.chaine_de_controle(SIREN)


def test_chaine_en_clair_si_on_le_demande(tmp_path):
    reg, _, _ = registres(tmp_path)
    chaine = reg.chaine_de_controle(SIREN, masquer_personnes=False)
    assert chaine["racine"]["dirigeants"][0]["personne"] == "Jeanne-Marie Claire DUPONT-MARTIN"
    assert "1980" not in json.dumps(chaine["fins"], ensure_ascii=False)


# ----------------------------------------------------------------------------- HATVP

def test_hatvp_declarations_publiees_avec_lien_du_dossier(tmp_path):
    reg, faux, _ = registres(tmp_path)
    decl = reg.hatvp("Exemplaire", "Hélène")
    assert [d.donnees["date_publication"] for d in decl] == ["2026-09-30", "2026-04-27", "2026-04-20"]
    assert all(d.donnees["url_dossier"] == "https://www.hatvp.fr/pages_nominatives/exemplaire-helene-0001" for d in decl)
    assert decl[-1].donnees["type_declaration"] == "déclaration d'intérêts"
    assert decl[0].donnees["non_publiees"] == 1  # la déclaration « En cours » n'est pas publiée
    assert decl[0].vers_preuve().url == decl[0].url
    assert len(faux.appels("hatvp.fr")) == 1


def test_hatvp_declaration_publiee_apres_le_gel_ignoree(tmp_path):
    reg, _, _ = registres(tmp_path, date_gel="2026-06-01")
    assert [d.donnees["date_publication"] for d in reg.hatvp("EXEMPLAIRE")] == ["2026-04-27", "2026-04-20"]
    assert any("après la date de gel" in j["raison"] for j in reg.journal)


def test_hatvp_nom_compose_prenom_et_accents(tmp_path):
    reg, _, _ = registres(tmp_path)
    assert len(reg.hatvp("henanff", "louis")) == 1
    assert reg.hatvp("Temoin", "Hélène") == []
    assert len(reg.hatvp("TEMOIN")) == 2


def test_hatvp_url_photo_jamais_lue_ni_gardee(tmp_path):
    reg, _, _ = registres(tmp_path)
    decl = reg.hatvp("EXEMPLAIRE")
    assert decl and "SENTINELLE-PHOTO" not in tout_le_texte(decl) and "photo" not in tout_le_texte(decl).lower()
    cache = "".join(f.read_text("utf-8") for f in (tmp_path / "registres").glob("*.json"))
    assert "SENTINELLE-PHOTO" not in cache and "url_photo" not in cache
    assert "exemplaire-helene-0001" in cache  # le reste du fichier est bien gardé


def test_hatvp_relue_au_plus_une_fois_par_jour(tmp_path):
    horloge = {"t": MAINTENANT}
    reg, faux, _ = registres(tmp_path, maintenant=lambda: horloge["t"])
    reg.hatvp("EXEMPLAIRE")
    horloge["t"] = MAINTENANT + timedelta(hours=23)
    reg.hatvp("TEMOIN")
    assert len(faux.appels("hatvp.fr")) == 1
    horloge["t"] = MAINTENANT + timedelta(hours=25)
    reg.hatvp("TEMOIN")
    assert len(faux.appels("hatvp.fr")) == 2


# ------------------------------------------------------------ garde-fous communs

def test_filtre_url_rejette_et_journalise(tmp_path):
    reg, faux, _ = registres(tmp_path, filtre_url=lambda u: "opendatasoft" not in u)
    assert reg.bodacc(SIREN) == []
    assert faux.requetes == []
    assert reg.journal[0]["raison"] == "refusé par filtre_url" and "opendatasoft" in reg.journal[0]["url"]
    assert reg.vie_de_societe(SIREN)["evenements"][0]["type"] == "creation"  # la fiche, elle, passe
    # le filtre passé à l'appel prime sur celui du client
    assert reg.hatvp("EXEMPLAIRE", filtre_url=lambda u: False) == []
    assert any("hatvp" in j["url"] for j in reg.journal)
    assert not faux.appels("hatvp.fr")


def test_filtre_url_s_applique_aussi_au_cache(tmp_path):
    reg, faux, _ = registres(tmp_path)
    assert reg.bodacc(SIREN)
    n = len(faux.requetes)
    reg2, faux2, _ = registres(tmp_path, filtre_url=lambda u: False)
    assert reg2.bodacc(SIREN) == [] and faux2.requetes == [] and n == 1


def test_cache_evite_le_second_appel(tmp_path):
    reg, faux, _ = registres(tmp_path)
    premier = reg.mandats("DUPONT-MARTIN", "Jeanne-Marie", annee_naissance=1980)
    reg.bodacc(SIREN)
    n = len(faux.requetes)
    reg2, faux2, _ = registres(tmp_path)  # nouveau client, même dossier de cache
    assert reg2.mandats("DUPONT-MARTIN", "Jeanne-Marie", annee_naissance=1980)[0].donnees["siren"] == premier[0].donnees["siren"]
    assert reg2.bodacc(SIREN)
    assert faux2.requetes == [] and reg2.appels_reseau == 0 and n == 2
    assert list((tmp_path / "registres").glob("*.json"))


def test_reessais_sur_503_puis_succes(tmp_path):
    faux = Faux()
    faux.statuts = [503, 429, 200]
    reg, _, pauses = registres(tmp_path, faux)
    assert len(reg.bodacc(SIREN)) == 6
    assert reg.appels_reseau == 3
    assert pauses == [2.0, 4.0]  # 2 s puis 4 s (le Retry-After de 1 s est plus court)


def test_erreur_persistante_journalisee_sans_planter(tmp_path):
    faux = Faux()
    faux.statuts = [500, 500, 500]
    reg, _, _ = registres(tmp_path, faux)
    assert reg.bodacc(SIREN) == []
    assert reg.journal[-1]["raison"] == "erreur HTTP 500"
    assert not list((tmp_path / "registres").glob("*.json"))  # un échec n'est pas mis en cache


def test_pause_d_au_moins_0_15_s_entre_deux_appels_a_recherche_d_entreprises(tmp_path):
    reg, _, pauses = registres(tmp_path)
    reg.vie_de_societe(SIREN)            # 1 appel entreprises
    assert pauses == []
    reg.mandats("DUPONT-MARTIN", "Jeanne-Marie")  # 2e appel : pause obligatoire
    assert pauses and pauses[0] >= 0.15 - 1e-9
    avant = len(pauses)
    reg.bodacc(SIREN)                    # BODACC n'est pas concerné
    assert len(pauses) == avant


def test_pause_tient_compte_du_temps_deja_ecoule(tmp_path):
    temps = {"t": 0.0}
    reg, _, pauses = registres(tmp_path, horloge=lambda: temps["t"])
    reg.vie_de_societe(SIREN)
    temps["t"] = 0.1
    reg.mandats("DUPONT-MARTIN", "Jeanne-Marie")
    assert pauses == [pytest.approx(0.05)]


# -------------------------------------------------------------- co-domiciliation, frise

def test_codomiciliation_bruit_au_dela_de_20(tmp_path):
    faux = Faux()
    adresse = faux.fiches[SIREN]["results"][0]["siege"]["adresse"]
    faux.adresse = [_societe(f"50000{i:04d}", f"BOITE AUX LETTRES {i}", adresse=adresse) for i in range(22)]
    reg, _, _ = registres(tmp_path, faux)
    cod = reg.codomiciliation(SIREN, limite=5)
    assert cod["nb_entites"] == 22 and len(cod["entites"]) == 5 and cod["bruit"] is True
    assert cod["lecture"] == "domiciliation commerciale probable, liens faibles"
    faux2 = Faux()
    faux2.adresse = [_societe("600000001", "VOISINE EXEMPLE", adresse=adresse),
                     _societe("600000002", "AILLEURS", adresse="9 RUE LOINTAINE 75001 PARIS")]
    reg2, _, _ = registres(tmp_path / "b", faux2)
    cod2 = reg2.codomiciliation(SIREN)
    assert cod2["nb_entites"] == 1 and cod2["bruit"] is False and "lecture" not in cod2


def test_evenements_frise_au_bon_format(tmp_path):
    reg, _, _ = registres(tmp_path)
    indices = reg.bodacc(SIREN) + reg.hatvp("EXEMPLAIRE")
    frise = evenements_frise(indices)
    assert reg.evenements_frise(indices) == frise
    assert len(frise) == 9
    assert all(set(e) == {"date", "type", "entite", "libelle", "source_url", "extrait"} for e in frise)
    assert [e["date"] for e in frise] == sorted(e["date"] for e in frise)
    assert {e["type"] for e in frise} >= {"creation", "declaration_hatvp", "vente"}
