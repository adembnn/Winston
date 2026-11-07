"""Tests du client Oriane : transport simulé, aucun appel réseau."""

import json

import httpx
import pytest

from enquete.sources.oriane import (
    OrianeAuthError,
    OrianeClient,
    OrianeCreditsError,
    OrianeError,
)

CLE = "cle-secrete-de-test-123"


def reponse_page(resultats: int, total: int, ancre: str | None = None) -> dict:
    return {
        "metadata": {"pagination": {"offset": 0, "limit": 100, "totalCount": total, "aiSearchAnchor": ancre}},
        "data": {"aggregations": {}, "results": [{"id": str(i)} for i in range(resultats)]},
    }


class Serveur:
    """Faux serveur : enregistre les requêtes et rejoue des réponses dans l'ordre."""

    def __init__(self, *reponses: httpx.Response):
        self.reponses = list(reponses)
        self.requetes: list[httpx.Request] = []

    def __call__(self, requete: httpx.Request) -> httpx.Response:
        self.requetes.append(requete)
        return self.reponses.pop(0) if len(self.reponses) > 1 else self.reponses[0]


def client(tmp_path, serveur: Serveur, **options) -> OrianeClient:
    return OrianeClient(
        cle=CLE,
        dossier_cache=tmp_path / "cache",
        transport=httpx.MockTransport(serveur),
        dormir=lambda _s: None,
        **options,
    )


def ok(corps: dict, statut: int = 200) -> httpx.Response:
    return httpx.Response(statut, json=corps)


def test_entete_bearer_et_route(tmp_path):
    serveur = Serveur(ok(reponse_page(1, 1)))
    client(tmp_path, serveur).chercher_contenus({"platform": {"includes": ["tiktok"]}}, tri="viewsCount:desc")
    requete = serveur.requetes[0]
    assert requete.headers["Authorization"] == f"Bearer {CLE}"
    assert requete.url.path == "/rest/contents/search"
    assert requete.url.params["sort"] == "viewsCount:desc"
    assert requete.url.params["projection"] == "basic"
    assert json.loads(requete.content)["filters"] == {"platform": {"includes": ["tiktok"]}}


def test_cle_lue_dans_lenvironnement(tmp_path, monkeypatch):
    monkeypatch.setenv("ORIANE_API_KEY", "cle-env")
    serveur = Serveur(ok(reponse_page(1, 1)))
    c = OrianeClient(dossier_cache=tmp_path, transport=httpx.MockTransport(serveur))
    c.chercher_contenus({})
    assert serveur.requetes[0].headers["Authorization"] == "Bearer cle-env"


def test_cle_absente_des_fichiers_du_cache_et_du_repr(tmp_path):
    c = client(tmp_path, Serveur(ok(reponse_page(1, 1))))
    c.chercher_contenus({"platform": {"includes": ["tiktok"]}})
    c.creer_asset(texte="une marque")
    fichiers = list((tmp_path / "cache").iterdir())
    assert fichiers
    for fichier in fichiers:
        assert CLE not in fichier.read_text(encoding="utf-8")
    assert CLE not in repr(c)


def test_le_cache_evite_le_second_appel(tmp_path):
    serveur = Serveur(ok(reponse_page(2, 2)))
    c = client(tmp_path, serveur)
    premiere = c.chercher_contenus({"platform": {"includes": ["tiktok"]}})
    seconde = c.chercher_contenus({"platform": {"includes": ["tiktok"]}})
    assert premiere == seconde
    assert len(serveur.requetes) == 1
    assert c.credits_estimes == 10  # le second appel n'a rien coûté
    # Un nouveau client sur le même dossier relit le disque.
    autre = client(tmp_path, serveur)
    autre.chercher_contenus({"platform": {"includes": ["tiktok"]}})
    assert len(serveur.requetes) == 1


def test_forcer_ignore_le_cache(tmp_path):
    serveur = Serveur(ok(reponse_page(1, 1)))
    c = client(tmp_path, serveur)
    c.chercher_contenus({})
    c.chercher_contenus({}, forcer=True)
    assert len(serveur.requetes) == 2
    assert c.credits_estimes == 20


def test_pagination_avec_ancre(tmp_path):
    serveur = Serveur(
        ok(reponse_page(100, 250, ancre="ANCRE-1")),
        ok(reponse_page(100, 250, ancre="ANCRE-1")),
        ok(reponse_page(50, 250, ancre="ANCRE-1")),
    )
    pages = list(client(tmp_path, serveur).parcourir_contenus({"id": {"includes": ["x"]}}))
    assert [len(p["data"]["results"]) for p in pages] == [100, 100, 50]
    assert [r.url.params["offset"] for r in serveur.requetes] == ["0", "100", "200"]
    assert "aiSearchAnchor" not in serveur.requetes[0].url.params
    assert serveur.requetes[1].url.params["aiSearchAnchor"] == "ANCRE-1"
    assert serveur.requetes[2].url.params["aiSearchAnchor"] == "ANCRE-1"


def test_pagination_plafond_de_pages_et_206(tmp_path):
    serveur = Serveur(ok(reponse_page(100, 10_000), statut=206))
    pages = list(client(tmp_path, serveur).parcourir_contenus({}, max_pages=2))
    assert len(pages) == 2  # le 206 est une page valide, le plafond arrête la boucle


def test_pagination_sarrete_sur_416(tmp_path):
    serveur = Serveur(ok(reponse_page(100, 1000)), httpx.Response(416, json={"error": {"message": "hors plage"}}))
    pages = list(client(tmp_path, serveur).parcourir_contenus({}))
    assert len(pages) == 1


def test_401_message_explicite(tmp_path):
    c = client(tmp_path, Serveur(httpx.Response(401, json={"error": {"code": "UNAUTHORIZED"}})))
    with pytest.raises(OrianeAuthError, match="[Cc]lé refusée par Oriane, vérifier qu'elle est active"):
        c.chercher_contenus({})


def test_402_credits(tmp_path):
    c = client(tmp_path, Serveur(httpx.Response(402, json={"error": {"message": "plus de crédits"}})))
    with pytest.raises(OrianeCreditsError, match="plus de crédits"):
        c.chercher_profils({})
    assert c.credits_estimes == 0  # un appel refusé ne coûte rien


def test_reessai_sur_429_avec_attente_croissante(tmp_path):
    serveur = Serveur(httpx.Response(429), httpx.Response(503), ok(reponse_page(1, 1)))
    attentes: list[float] = []
    c = OrianeClient(
        cle=CLE, dossier_cache=tmp_path, transport=httpx.MockTransport(serveur), dormir=attentes.append
    )
    assert c.chercher_contenus({})["data"]["results"]
    assert len(serveur.requetes) == 3
    assert attentes == [1.0, 2.0]


def test_abandon_apres_trois_essais(tmp_path):
    serveur = Serveur(httpx.Response(429))
    with pytest.raises(OrianeError, match="réessais épuisés"):
        client(tmp_path, serveur).chercher_contenus({})
    assert len(serveur.requetes) == 3


def test_comptage_des_credits(tmp_path):
    serveur = Serveur(ok({"data": {"id": "a1"}}, statut=201), ok(reponse_page(1, 1)))
    c = client(tmp_path, serveur)
    c.creer_asset(texte="serum")                              # 5
    c.chercher_contenus({}, projection="full")                # 20
    c.chercher_contenus({}, projection="default")             # 15
    c.chercher_contenus({}, projection="basic")               # 10
    c.chercher_profils({}, projection="basic")                # 5
    c.chercher_profils({}, projection="full")                 # 15
    assert c.credits_estimes == 5 + 20 + 15 + 10 + 5 + 15


def test_creer_asset_corps(tmp_path):
    serveur = Serveur(ok({"data": {"id": "a1"}}, statut=201))
    c = client(tmp_path, serveur)
    assert c.creer_asset(image="https://exemple.fr/logo.png")["data"]["id"] == "a1"
    assert json.loads(serveur.requetes[0].content) == {
        "type": "image", "image": {"type": "url", "url": "https://exemple.fr/logo.png"}
    }
    with pytest.raises(ValueError):
        c.creer_asset()


def test_limite_invalide(tmp_path):
    with pytest.raises(ValueError):
        client(tmp_path, Serveur(ok(reponse_page(1, 1)))).chercher_contenus({}, limite=101)
