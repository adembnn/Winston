"""Tests de l'enrichissement d'infrastructure : transport simulé, aucun appel réseau."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from enquete.pivots import Pivot
from enquete.sources.infra import Indice, Infra, initiales

REPONSES = Path(__file__).parent / "reponses"
WAYBACK = json.loads((REPONSES / "wayback_exemple.json").read_text("utf-8"))
MAINTENANT = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def lire(nom: str) -> str:
    return (REPONSES / nom).read_text("utf-8")


class Faux:
    """Faux serveur : répond selon l'hôte et garde les requêtes."""

    def __init__(self) -> None:
        self.requetes: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requetes.append(req)
        hote, url = req.url.host, str(req.url)
        if hote == "rdap.org":
            return httpx.Response(200, text=lire("rdap_exemple.json"))
        if hote == "crt.sh":
            return httpx.Response(200, text=lire("crtsh_exemple.json"))
        if hote == "recherche-entreprises.api.gouv.fr":
            return httpx.Response(200, text=lire("entreprises_exemple.json"))
        if hote == "web.archive.org":
            if "limit=-1" in url:
                cle = "derniere_avant_gel" if "to=" in url else "derniere"
            else:
                cle = "premiere"
            return httpx.Response(200, text=json.dumps(WAYBACK[cle]))
        if hote == "linktr.ee":
            return httpx.Response(200, text=lire("lien_bio_exemple.html"))
        return httpx.Response(404, text="")


def infra(tmp_path, faux=None, **options) -> tuple[Infra, Faux]:
    faux = faux or Faux()
    options.setdefault("maintenant", lambda: MAINTENANT)
    options.setdefault("dormir", lambda _s: None)
    return Infra(dossier_cache=tmp_path / "infra", transport=httpx.MockTransport(faux), **options), faux


def test_rdap_creation_registrar_dns_sans_titulaire(tmp_path):
    inf, _ = infra(tmp_path)
    ind = inf.rdap("boutique-exemple.fr")
    assert isinstance(ind, Indice)
    assert ind.donnees["creation"] == "2025-09-12T08:00:00Z"
    assert ind.donnees["registrar"] == "Registrar Exemple SAS"
    assert ind.donnees["dns"] == ["ns1.hebergeur-exemple.net", "ns2.hebergeur-exemple.net"]
    assert ind.url == "https://rdap.org/domain/boutique-exemple.fr"
    assert ind.date_lecture == MAINTENANT
    assert "TITULAIRE" not in ind.extrait and "TITULAIRE" not in json.dumps(ind.donnees)
    assert ind.vers_preuve().url == ind.url


def test_crtsh_domaines_freres(tmp_path):
    inf, _ = infra(tmp_path)
    ind = inf.crtsh("boutique-exemple.fr")
    assert ind.donnees["freres"] == ["autre-boutique-exemple.com", "boutique-sosie.shop"]
    assert "www.boutique-exemple.fr" in ind.donnees["sous_domaines"]
    assert ind.donnees["certificats"] == 2
    assert ind.donnees["premier_certificat"] == "2025-09-13T08:10:14"


def test_entreprise_masque_les_personnes_physiques(tmp_path):
    inf, _ = infra(tmp_path)
    (ind,) = inf.entreprise(siren="552 100 554")
    assert ind.donnees["siren"] == "552100554"
    assert ind.donnees["date_creation"] == "2025-09-01"
    assert ind.donnees["siege"]["libelle_commune"] == "PARIS"
    physique, morale = ind.donnees["dirigeants"]
    assert physique == {"type_dirigeant": "personne physique", "initiales": "J.-M. D.-M.", "qualite": "Président"}
    assert morale["denomination"] == "HOLDING EXEMPLE"
    for texte in (ind.extrait, json.dumps(ind.donnees, ensure_ascii=False)):
        assert "DUPONT" not in texte and "Jeanne" not in texte and "1980" not in texte


def test_entreprise_personnes_visibles_sur_demande(tmp_path):
    inf, _ = infra(tmp_path, masquer_personnes=False)
    (ind,) = inf.entreprise(siren="552100554")
    assert ind.donnees["dirigeants"][0]["nom"] == "DUPONT-MARTIN"
    inf2, _ = infra(tmp_path / "autre")
    (ind2,) = inf2.entreprise(siren="552100554", masquer_personnes=False)
    assert "DUPONT-MARTIN" in ind2.extrait


def test_initiales():
    assert initiales("LAURE CLAUDE MIREILLE", "HAUSEUX (CALDAIROU)") == "L. H."
    assert initiales(None, None) == "?"


def test_wayback_premiere_et_derniere(tmp_path):
    inf, _ = infra(tmp_path)
    ind = inf.wayback("boutique-exemple.fr")
    assert ind.donnees["premiere_capture"] == "20250915101112"
    assert ind.donnees["derniere_capture"] == "20261020101112"


def test_date_de_gel_wayback_envoie_to_et_ne_renvoie_rien_de_posterieur(tmp_path):
    inf, faux = infra(tmp_path, date_gel="2025-12-31")
    ind = inf.wayback("boutique-exemple.fr")
    assert all("to=20251231235959" in str(r.url) for r in faux.requetes)
    assert ind.donnees["derniere_capture"] == "20251001101112"

    # Même si le serveur ignore `to`, le client écarte ce qui est postérieur au gel.
    def tetu(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(WAYBACK["derniere"]))

    inf2 = Infra(dossier_cache=tmp_path / "b", transport=httpx.MockTransport(tetu), date_gel="2025-12-31",
                 maintenant=lambda: MAINTENANT)
    assert inf2.wayback("boutique-exemple.fr") is None


def test_gel_ecarte_domaine_cree_apres(tmp_path):
    inf, _ = infra(tmp_path, date_gel="2025-01-01")
    assert inf.rdap("boutique-exemple.fr") is None
    assert inf.journal and "date de gel" in inf.journal[0]["raison"]
    assert inf.entreprise(siren="552100554") == []  # société créée le 2025-09-01


def test_filtre_url_rejette_et_journalise_sans_toucher_au_reseau(tmp_path):
    inf, faux = infra(tmp_path, filtre_url=lambda url: "crt.sh" not in url)
    assert inf.crtsh("boutique-exemple.fr") is None
    assert faux.requetes == []
    assert inf.journal[0]["url"].startswith("https://crt.sh/")
    assert inf.journal[0]["raison"] == "refusé par filtre_url"
    # le filtre par appel l'emporte sur celui de l'instance
    assert inf.crtsh("boutique-exemple.fr", filtre_url=lambda url: True) is not None


def test_filtre_url_s_applique_meme_au_cache(tmp_path):
    inf, _ = infra(tmp_path)
    assert inf.rdap("boutique-exemple.fr") is not None
    inf2, faux2 = infra(tmp_path, filtre_url=lambda url: False)
    assert inf2.rdap("boutique-exemple.fr") is None
    assert faux2.requetes == []


def test_cache_disque_evite_le_second_appel(tmp_path):
    inf, faux = infra(tmp_path)
    inf.rdap("boutique-exemple.fr")
    inf.rdap("boutique-exemple.fr")
    assert len(faux.requetes) == 1
    assert list((tmp_path / "infra").glob("*.json"))
    inf2, faux2 = infra(tmp_path)
    assert inf2.rdap("boutique-exemple.fr") is not None
    assert faux2.requetes == []


def test_erreur_serveur_reessayee_puis_journalisee_sans_cache(tmp_path):
    appels = []

    def panne(req: httpx.Request) -> httpx.Response:
        appels.append(1)
        return httpx.Response(502)

    inf = Infra(dossier_cache=tmp_path / "x", transport=httpx.MockTransport(panne), dormir=lambda _s: None,
                maintenant=lambda: MAINTENANT)
    assert inf.crtsh("boutique-exemple.fr") is None
    assert len(appels) == 2
    assert "502" in inf.journal[0]["raison"]
    assert not list((tmp_path / "x").glob("*.json"))


def test_deplier_lien_bio(tmp_path):
    inf, _ = infra(tmp_path)
    liens = inf.deplier_lien_bio("linktr.ee/exemple")
    assert "https://boutique-exemple.fr/collection?utm_source=bio" in liens
    assert "https://paypal.me/Exemple" in liens
    assert "https://t.me/CanalExemple" in liens
    assert not any("linktr.ee" in x or x.endswith(".png") for x in liens)


def test_deplier_pages_retire_le_gabarit_du_service(tmp_path):
    def serveur(req: httpx.Request) -> httpx.Response:
        nom = req.url.path.strip("/")
        return httpx.Response(200, text=f'<a href="https://pub-service.example/promo">p</a>'
                                        f'<a href="https://site-{nom}.example/">s</a>')

    inf = Infra(dossier_cache=tmp_path / "d", transport=httpx.MockTransport(serveur),
                maintenant=lambda: MAINTENANT)
    res = inf.deplier_pages(["linktr.ee/a", "linktr.ee/b", "linktr.ee/c"])
    assert res["linktr.ee/a"] == ["https://site-a.example/"]


def test_enrichir_ajoute_des_indices_aux_domaines_et_siren_sauf_banals(tmp_path):
    inf, faux = infra(tmp_path)

    def pivot(type_, valeur, banal=False):
        return Pivot(type=type_, valeur=valeur, comptes=("tiktok:a",), preuves=(), banal=banal)

    sortie = inf.enrichir([pivot("domaine", "boutique-exemple.fr"), pivot("siren", "552100554"),
                           pivot("domaine", "banal.example", banal=True), pivot("hashtag", "x")])
    assert [i.source for i in sortie[0].indices] == ["rdap", "crt.sh", "wayback"]
    assert [i.source for i in sortie[1].indices] == ["recherche-entreprises"]
    assert sortie[2].indices == [] and sortie[3].indices == []
    assert not any("banal.example" in str(r.url) for r in faux.requetes)
