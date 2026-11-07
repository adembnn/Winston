"""Tests de l'adaptateur Dailymotion : transport simulé qui lève sur un hôte inconnu, aucun appel réseau."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from enquete.reseau import Preuve
from enquete.sources import reseau_dailymotion
from enquete.sources.reseau_dailymotion import (
    ANGLE_MORT,
    CAPACITES,
    PLATEFORME,
    DailymotionBloqueError,
    DailymotionError,
    DailymotionIntrouvableError,
    DailymotionLimiteError,
    ReseauDailymotion,
    id_video,
    vers_preuve,
)

REPONSES = Path(__file__).parent / "reponses"
MAINTENANT = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
CLES = {"plateforme", "id", "url", "auteur", "date", "texte", "langue", "medias", "metriques", "route",
        "recupere_le", "preuve"}


def charger(nom: str) -> dict:
    return json.loads((REPONSES / nom).read_text("utf-8"))


class Faux:
    """Faux serveur Dailymotion : garde les requêtes, lève sur tout autre hôte."""

    def __init__(self, recherche=None, statut=200, entetes=None, video=None):
        self.requetes: list[httpx.Request] = []
        self.recherche = recherche if recherche is not None else charger("reseau_dailymotion_recherche.json")
        self.statut = statut
        self.entetes = entetes or {}
        self.video = video

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.dailymotion.com", f"hôte inattendu : {req.url.host}"
        self.requetes.append(req)
        if self.statut != 200:
            return httpx.Response(self.statut, headers=self.entetes, text="{}")
        if req.url.path == "/videos":
            return httpx.Response(200, json=self.recherche)
        if req.url.path == "/video/x89ucz5":
            return httpx.Response(200, json=self.video or charger("reseau_dailymotion_video.json"))
        return httpx.Response(404, json={"error": {"code": 404}})


def dm(tmp_path, faux=None, **options):
    faux = faux or Faux()
    pauses: list[float] = []
    options.setdefault("maintenant", lambda: MAINTENANT)
    options.setdefault("dormir", pauses.append)
    r = ReseauDailymotion(dossier_cache=tmp_path / "dm", transport=httpx.MockTransport(faux), **options)
    return r, faux, pauses


def test_constantes_du_contrat():
    assert PLATEFORME == "dailymotion"
    assert CAPACITES == {"chercher": True, "lire": True}
    assert "sans la parole" in ANGLE_MORT


def test_chercher_filtre_local_cles_et_preuve(tmp_path):
    r, faux, _ = dm(tmp_path)
    fiches = r.chercher("intelligence artificielle")
    assert len(fiches) == 2  # les deux autres résultats ne contiennent pas tous les mots
    assert [f["date"] for f in fiches] == sorted(f["date"] for f in fiches)
    for f in fiches:
        assert set(f) == CLES
        assert f["plateforme"] == "dailymotion" and f["route"] == "api"
        assert f["url"].startswith("https://www.dailymotion.com/video/")
        assert f["auteur"] and f["date"].endswith("+00:00")
        assert "vues" in f["metriques"]
        p = vers_preuve(f)
        assert isinstance(p, Preuve) and p.url.startswith("https://api.dailymotion.com/videos?")
    q = faux.requetes[0].url.params
    assert q["search"] == "intelligence artificielle" and q["sort"] == "recent" and q["limit"] == "100"
    assert q["fields"] == "id,title,url,owner.screenname,created_time,views_total,language,description"


def test_mots_sans_casse_ni_accents(tmp_path):
    r, _, _ = dm(tmp_path)
    assert len(r.chercher("INTELLIGENCE Artificielle")) == 2
    assert len(r.chercher("bande annonce ARTIFICIAL andrew")) == 1
    assert r.chercher("introuvable xyz") == []


def test_pagination_tant_que_has_more(tmp_path):
    page1 = charger("reseau_dailymotion_recherche.json")
    page1["has_more"] = True
    faux = Faux(recherche=page1)
    r, faux, _ = dm(tmp_path, faux)
    r.chercher("intelligence artificielle", pages=3)
    assert [q.url.params["page"] for q in faux.requetes] == ["1", "2", "3"]


def test_depuis_arrete_la_pagination(tmp_path):
    page = charger("reseau_dailymotion_recherche.json")
    page["has_more"] = True
    r, faux, _ = dm(tmp_path, Faux(recherche=page))
    fiches = r.chercher("intelligence artificielle", depuis="2026-12-01", pages=5)
    assert fiches == [] and len(faux.requetes) == 1
    assert {j["raison"] for j in r.journal} == {"hors_fenetre"}


def test_gel_ecarte_les_fiches_posterieures(tmp_path):
    r, _, _ = dm(tmp_path, date_gel="2026-09-16")
    assert r.chercher("intelligence artificielle") == []
    assert len(r.journal) == 2 and {j["raison"] for j in r.journal} == {"apres_gel"}
    assert all(set(j) == {"url", "raison", "date"} for j in r.journal)


def test_gel_garde_les_anciennes_sans_metriques(tmp_path):
    r, _, _ = dm(tmp_path, date_gel="2026-11-07")
    fiches = r.chercher("intelligence artificielle")
    assert len(fiches) == 2 and all(f["metriques"] == {} for f in fiches)


def test_gel_ecarte_la_fiche_sans_date(tmp_path):
    page = charger("reseau_dailymotion_recherche.json")
    for v in page["list"]:
        v.pop("created_time")
    r, _, _ = dm(tmp_path, Faux(recherche=page), date_gel="2026-12-31")
    assert r.chercher("intelligence artificielle") == []
    assert {j["raison"] for j in r.journal} == {"sans_date_sous_gel"}
    r2, _, _ = dm(tmp_path / "b", Faux(recherche=page))
    assert [f["date"] for f in r2.chercher("intelligence artificielle")] == [None, None]


def test_fenetre_jusqua(tmp_path):
    r, _, _ = dm(tmp_path)
    toutes = r.chercher("intelligence artificielle")
    premiere = toutes[0]["date"][:10]
    r2, _, _ = dm(tmp_path / "b")
    seule = r2.chercher("intelligence artificielle", jusqua=premiere)
    assert len(seule) == 1 and r2.journal[0]["raison"] == "hors_fenetre"


def test_limite(tmp_path):
    r, _, _ = dm(tmp_path)
    assert len(r.chercher("intelligence artificielle", limite=1)) == 1


def test_filtre_url_refuse_sans_appel_ni_cache(tmp_path):
    r, faux, _ = dm(tmp_path, filtre_url=lambda u: False)
    assert r.chercher("intelligence artificielle") == []
    assert faux.requetes == [] and r.appels_reseau == 0 and not (tmp_path / "dm").exists()
    assert r.journal[0]["raison"] == "filtre_url"
    with pytest.raises(DailymotionError):
        r.lire("https://www.dailymotion.com/video/x89ucz5")
    assert faux.requetes == []


def test_filtre_url_sur_chaque_fiche_rendue(tmp_path):
    cible = "xbuae1q"
    r, _, _ = dm(tmp_path, filtre_url=lambda u: cible not in u or u.startswith("https://api."))
    ids = [f["id"] for f in r.chercher("intelligence artificielle")]
    assert cible not in ids and len(ids) == 1
    assert any(j["raison"] == "filtre_url" and cible in j["url"] for j in r.journal)


def test_cache_second_appel_sans_requete(tmp_path):
    r, faux, _ = dm(tmp_path)
    a = r.chercher("intelligence artificielle")
    n = len(faux.requetes)
    b = r.chercher("intelligence artificielle")
    assert len(faux.requetes) == n == r.appels_reseau and a == b
    fichiers = list((tmp_path / "dm").glob("*.json"))
    assert fichiers and all(len(f.stem) == 64 for f in fichiers) and not list((tmp_path / "dm").glob("*.tmp"))


def test_lire_une_video(tmp_path):
    r, _, _ = dm(tmp_path)
    f = r.lire("https://www.dailymotion.com/video/x89ucz5")
    assert set(f) == CLES
    assert f["auteur"] == "Capital" and f["date"].startswith("2022-04-08")
    assert f["langue"] == "fr" and f["metriques"] == {"vues": 1150}
    assert f["texte"] == "L'intelligence artificielle"
    assert isinstance(vers_preuve(f), Preuve)


def test_lire_sous_gel_anterieur_refuse_la_video_recente(tmp_path):
    r, _, _ = dm(tmp_path, date_gel="2022-01-01")
    with pytest.raises(DailymotionIntrouvableError):
        r.lire("https://www.dailymotion.com/video/x89ucz5")
    assert r.journal[0]["raison"] == "apres_gel"


def test_id_video():
    assert id_video("https://www.dailymotion.com/video/x89ucz5") == "x89ucz5"
    assert id_video("https://www.dailymotion.com/video/x89ucz5_titre-long") == "x89ucz5"
    assert id_video("https://dai.ly/x89ucz5") == "x89ucz5"
    assert id_video("https://example.org/video/x89ucz5") is None


def test_404_lance_introuvable(tmp_path):
    r, _, _ = dm(tmp_path)
    with pytest.raises(DailymotionIntrouvableError) as e:
        r.lire("https://www.dailymotion.com/video/xzzzzzz")
    assert e.value.statut == 404


def test_403_lance_bloque_sans_reessai(tmp_path):
    r, faux, _ = dm(tmp_path, Faux(statut=403))
    with pytest.raises(DailymotionBloqueError):
        r.chercher("intelligence artificielle")
    assert len(faux.requetes) == 1


def test_429_respecte_retry_after_puis_lance_limite(tmp_path):
    r, faux, pauses = dm(tmp_path, Faux(statut=429, entetes={"Retry-After": "7"}))
    with pytest.raises(DailymotionLimiteError):
        r.chercher("intelligence artificielle")
    assert len(faux.requetes) == 3 and pauses.count(7.0) == 2


def test_seconde_page_en_echec_est_journalisee(tmp_path):
    page = charger("reseau_dailymotion_recherche.json")
    page["has_more"] = True

    def faux(req):
        if req.url.params.get("page") == "2":
            return httpx.Response(403, text="{}")
        return httpx.Response(200, json=page)

    r = ReseauDailymotion(dossier_cache=tmp_path / "dm", transport=httpx.MockTransport(faux),
                          maintenant=lambda: MAINTENANT, dormir=lambda _s: None)
    assert len(r.chercher("intelligence artificielle", pages=3)) == 2
    assert [j["raison"] for j in r.journal] == ["mur"]


def test_ligne_de_commande(tmp_path, monkeypatch, capsys):
    vrai_client = httpx.Client
    monkeypatch.setattr(reseau_dailymotion.httpx, "Client", lambda **kw: vrai_client(
        transport=httpx.MockTransport(Faux()), headers=kw.get("headers")))
    code = reseau_dailymotion.main(["--cache", str(tmp_path / "c"), "lire", "https://www.dailymotion.com/video/x89ucz5"])
    sortie = capsys.readouterr()
    assert code == 0 and json.loads(sortie.out)["auteur"] == "Capital"
    assert "appels_reseau=1" in sortie.err
