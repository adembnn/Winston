"""Tests de l'adaptateur Reddit : transport simulé, aucun appel réseau."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from enquete.reseau import Preuve
from enquete.sources import reseau_reddit as rd
from enquete.sources.reseau_reddit import (
    RedditBloqueError,
    RedditError,
    RedditIntrouvableError,
    RedditLimiteError,
    ReseauReddit,
)

REPONSES = Path(__file__).parent / "reponses"
MAINTENANT = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
CLES = {"plateforme", "id", "url", "auteur", "date", "texte", "langue", "medias", "metriques", "route",
        "recupere_le", "preuve"}
VIDE = json.dumps({"data": []})


def lire_fixture(nom: str) -> str:
    return (REPONSES / nom).read_text("utf-8")


class Faux:
    """Faux serveur : répond selon l'hôte et le chemin, lève sur un hôte inconnu."""

    def __init__(self, surcharge=None) -> None:
        self.requetes: list[httpx.Request] = []
        self.surcharge = surcharge

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requetes.append(req)
        if self.surcharge is not None:
            rep = self.surcharge(req)
            if rep is not None:
                return rep
        hote, chemin = req.url.host, req.url.path
        if hote == "arctic-shift.photon-reddit.com":
            if chemin == "/api/posts/search":
                return httpx.Response(200, text=lire_fixture("reseau_reddit_posts.json"))
            if chemin == "/api/comments/search":
                return httpx.Response(200, text=lire_fixture("reseau_reddit_commentaires.json"))
            if chemin == "/api/posts/ids":
                return httpx.Response(200, text=lire_fixture("reseau_reddit_post_ids.json"))
            if chemin == "/api/comments/ids":
                donnees = json.loads(lire_fixture("reseau_reddit_commentaires.json"))["data"][:1]
                return httpx.Response(200, text=json.dumps({"data": donnees}))
        if hote == "www.reddit.com" and chemin.endswith("/search.rss"):
            return httpx.Response(200, text=lire_fixture("reseau_reddit_flux.xml"))
        raise AssertionError(f"requête inattendue : {req.url}")

    def hotes(self) -> list[str]:
        return [r.url.host for r in self.requetes]

    def chemins(self) -> list[str]:
        return [r.url.path for r in self.requetes]


def client(tmp_path, faux=None, **kw):
    faux = faux or Faux()
    dormi: list[float] = []
    r = ReseauReddit(dossier_cache=tmp_path / "cache", transport=httpx.MockTransport(faux),
                     maintenant=lambda: MAINTENANT, dormir=dormi.append, **kw)
    r.dormi = dormi
    return r, faux


def sans_archive(req):
    """L'archive ne répond rien : seul le flux RSS donne des fiches."""
    if req.url.host == "arctic-shift.photon-reddit.com":
        return httpx.Response(200, text=VIDE)
    return None


# --------------------------------------------------------------------------- contrat

def test_module_expose_le_contrat():
    assert rd.PLATEFORME == "reddit"
    assert rd.CAPACITES == {"chercher": True, "lire": True}
    assert "Reddit" in rd.ANGLE_MORT and "supprimé" in rd.ANGLE_MORT


def test_chercher_fiches_conformes_sans_doublon(tmp_path):
    r, faux = client(tmp_path)
    fiches = r.chercher("Bardella")
    ids = [f["id"] for f in fiches]
    assert len(ids) == len(set(ids)) == 5  # 3 posts + 2 commentaires ; le flux RSS ne double rien
    assert [f["date"] for f in fiches] == sorted(f["date"] for f in fiches)
    assert all(set(f) == CLES for f in fiches)
    assert {f["route"] for f in fiches} == {"arctic-shift"}
    post = next(f for f in fiches if f["id"] == "t3_1xnq8q0")
    assert post["url"].startswith("https://www.reddit.com/r/france/comments/1xnq8q0/")
    assert post["auteur"] == "utilisateur_1"
    assert post["date"] == "2026-11-04T14:54:45+00:00"
    assert post["metriques"] == {"score": 486, "commentaires": 65}
    assert post["medias"] == [{"type": "video", "url": "https://v.redd.it/cpwzqoeh0mzh1"}]
    assert post["recupere_le"] == MAINTENANT.isoformat()
    com = next(f for f in fiches if f["id"] == "t1_19s8u8lfne2hv")
    assert com["texte"] == "Le père de Bardella ?"
    assert com["metriques"] == {"score": 2} and com["medias"] == []
    p = Preuve(url=post["preuve"]["url"], extrait=post["preuve"]["extrait"],
               source_donnee=f"{post['plateforme']} ({post['route']})",
               date_collecte=datetime.fromisoformat(post["recupere_le"]))
    assert p.source_donnee == "reddit (arctic-shift)"
    assert post["preuve"]["url"].startswith("https://arctic-shift.photon-reddit.com/api/posts/search?")
    assert "title=Bardella" in post["preuve"]["url"] and "subreddit=france" in post["preuve"]["url"]
    assert faux.chemins().count("/api/posts/search") == 1
    assert "/r/france/search.rss" in faux.chemins()


def test_auteur_supprime_vaut_none(tmp_path):
    r, _ = client(tmp_path)
    f = next(f for f in r.chercher("Bardella") if f["id"] == "t3_1xofoi9")
    assert f["auteur"] is None


def test_fiches_du_flux_rss(tmp_path):
    r, faux = client(tmp_path, Faux(sans_archive))
    fiches = r.chercher("Bardella")
    assert len(fiches) == 2 and all(set(f) == CLES for f in fiches)
    f = fiches[-1]
    assert f["route"] == "rss" and f["id"] == "t3_1xofoi9"
    assert f["auteur"] == "utilisateur_r"
    assert f["date"] == "2026-11-05T10:52:29+00:00"
    assert f["metriques"] == {}
    assert f["texte"].startswith("Des journalistes quittent la soirée de lancement")
    assert f["preuve"]["extrait"] == f["texte"].split("\n\n")[0]
    assert f["preuve"]["url"] == "https://www.reddit.com/r/france/search.rss?q=Bardella&restrict_sr=on&sort=new"
    assert faux.requetes[-1].headers["user-agent"].startswith("enquete-osint/0.1")


def test_requete_arctic_porte_la_fenetre_et_les_subreddits(tmp_path):
    r, faux = client(tmp_path)
    r.chercher("Bardella", depuis="2026-08-01", jusqua="2026-09-17", subreddits=("france", "French"), limite=30)
    posts = [q for q in faux.requetes if q.url.path == "/api/posts/search"]
    assert [q.url.params["subreddit"] for q in posts] == ["france", "French"]
    assert posts[0].url.params["after"] == "2026-08-01T00:00:00"
    assert posts[0].url.params["before"] == "2026-09-17T23:59:59"
    assert posts[0].url.params["limit"] == "30"
    coms = [q for q in faux.requetes if q.url.path == "/api/comments/search"]
    assert coms[0].url.params["body"] == "Bardella"
    assert "www.reddit.com" not in faux.hotes()  # une borne haute : l'archive suffit


def test_auteur_filtre_sans_flux_rss(tmp_path):
    r, faux = client(tmp_path)
    r.chercher("Bardella", auteur="utilisateur_1", subreddits=())
    q = faux.requetes[0].url.params
    assert q["author"] == "utilisateur_1" and "subreddit" not in q
    assert "www.reddit.com" not in faux.hotes()


# --------------------------------------------------------------------------- gel

def test_gel_ecarte_les_fiches_posterieures_et_les_journalise(tmp_path):
    r, faux = client(tmp_path, date_gel="2026-11-04")
    fiches = r.chercher("Bardella")
    assert {f["id"] for f in fiches} == {"t3_1xnq8q0", "t3_1xjduik"}
    assert all(f["date"] <= "2026-11-04T23:59:59+00:00" for f in fiches)
    assert all(f["metriques"] == {} for f in fiches)
    ecartees = sorted(j["url"] for j in r.journal if j["raison"] == "apres_gel")
    assert len(ecartees) == 3  # un post et deux commentaires
    posts = [q for q in faux.requetes if q.url.path == "/api/posts/search"]
    assert posts[0].url.params["before"] == "2026-11-04T23:59:59"
    assert "www.reddit.com" not in faux.hotes()  # sous gel : pas de flux RSS, il ne vaut que pour aujourd'hui


def test_gel_ecarte_une_fiche_sans_date(tmp_path):
    def surcharge(req):
        if req.url.path == "/api/posts/search":
            donnees = json.loads(lire_fixture("reseau_reddit_posts.json"))["data"][1:2]
            del donnees[0]["created_utc"]
            return httpx.Response(200, text=json.dumps({"data": donnees}))
        if req.url.path == "/api/comments/search":
            return httpx.Response(200, text=VIDE)
        return None

    r, _ = client(tmp_path, Faux(surcharge), date_gel="2026-12-31")
    assert r.chercher("Bardella") == []
    assert [j["raison"] for j in r.journal] == ["sans_date_sous_gel"]


def test_depuis_ecarte_hors_fenetre(tmp_path):
    r, _ = client(tmp_path)
    fiches = r.chercher("Bardella", depuis="2026-11-04", jusqua="2026-12-01")
    assert {f["id"] for f in fiches if f["id"].startswith("t3_")} == {"t3_1xofoi9", "t3_1xnq8q0"}
    assert [j["raison"] for j in r.journal] == ["hors_fenetre"]


def test_limite_garde_les_plus_recents(tmp_path):
    r, _ = client(tmp_path)
    fiches = r.chercher("Bardella", limite=2)
    assert len(fiches) == 2
    assert fiches[-1]["date"] == max(f["date"] for f in fiches)
    assert any(j["raison"] == "limite" for j in r.journal)


# --------------------------------------------------------------------------- 422, 429, 403, 404

def test_422_timeout_attend_20_s_et_reessaie(tmp_path):
    essais = []

    def surcharge(req):
        if req.url.path == "/api/posts/search":
            essais.append(1)
            if len(essais) <= 2:
                return httpx.Response(422, text='{"data":null,"error":"Timeout. Maybe slow down a bit"}')
        return None

    r, _ = client(tmp_path, Faux(surcharge))
    assert len(r.chercher("Bardella", jusqua="2026-12-01")) == 5
    assert r.dormi.count(20.0) == 2
    assert len(essais) == 3


def test_422_trois_fois_leve_une_erreur(tmp_path):
    r, _ = client(tmp_path, Faux(lambda req: httpx.Response(422, text='{"error":"Timeout. Maybe slow down"}')))
    with pytest.raises(RedditError, match="422") as e:
        r.chercher("Bardella")
    assert e.value.statut == 422
    assert r.dormi.count(20.0) == 2 and r.appels_reseau == 3


def test_403_est_un_mur_constate(tmp_path):
    r, _ = client(tmp_path, Faux(lambda req: httpx.Response(403, text="Blocked")))
    with pytest.raises(RedditBloqueError):
        r.chercher("Bardella")


def test_429_deux_fois_leve_limite(tmp_path):
    r, _ = client(tmp_path, Faux(lambda req: httpx.Response(429)))
    with pytest.raises(RedditLimiteError):
        r.chercher("Bardella")
    assert r.dormi.count(20.0) == 1


def test_429_du_flux_rss_est_journalise_apres_la_premiere_requete(tmp_path):
    def surcharge(req):
        return httpx.Response(429, headers={"Retry-After": "3"}) if req.url.host == "www.reddit.com" else None

    r, _ = client(tmp_path, Faux(surcharge))
    assert len(r.chercher("Bardella")) == 5
    assert 3.0 in r.dormi
    assert [j["raison"] for j in r.journal] == ["limite"]


def test_404_est_introuvable(tmp_path):
    r, _ = client(tmp_path, Faux(lambda req: httpx.Response(404)))
    with pytest.raises(RedditIntrouvableError):
        r.lire("t3_inconnu")


# --------------------------------------------------------------------------- filtre, cache

def test_filtre_url_passe_avant_tout_appel(tmp_path):
    r, faux = client(tmp_path, filtre_url=lambda url: False)
    assert r.chercher("Bardella") == []
    with pytest.raises(RedditError):
        r.lire("t3_1xnq8q0")
    assert faux.requetes == [] and r.appels_reseau == 0
    assert {j["raison"] for j in r.journal} == {"filtre_url"}
    assert not (tmp_path / "cache").exists()


def test_filtre_url_sur_chaque_fiche_rendue(tmp_path):
    r, _ = client(tmp_path, filtre_url=lambda url: "1xnq8q0" not in url)
    ids = {f["id"] for f in r.chercher("Bardella", jusqua="2026-12-01")}
    assert "t3_1xnq8q0" not in ids and "t3_1xofoi9" in ids
    assert [j["raison"] for j in r.journal] == ["filtre_url"]


def test_cache_le_second_appel_ne_touche_pas_le_reseau(tmp_path):
    r1, _ = client(tmp_path)
    premier = r1.chercher("Bardella")
    assert r1.appels_reseau == 3  # posts, commentaires, flux
    r2, faux2 = client(tmp_path)
    second = r2.chercher("Bardella")
    assert r2.appels_reseau == 0 and faux2.requetes == []
    assert second == premier
    assert not list((tmp_path / "cache").glob("*.tmp"))


def test_une_erreur_422_n_entre_pas_dans_le_cache(tmp_path):
    r, _ = client(tmp_path, Faux(lambda req: httpx.Response(422, text="Timeout")))
    with pytest.raises(RedditError):
        r.chercher("Bardella")
    assert not (tmp_path / "cache").exists()


# --------------------------------------------------------------------------- lire

@pytest.mark.parametrize("lien, attendu", [
    ("https://www.reddit.com/r/france/comments/1xnq8q0/jordan_bardella_sur_rtl/", "t3_1xnq8q0"),
    ("https://old.reddit.com/r/france/comments/1xnq8q0/", "t3_1xnq8q0"),
    ("https://redd.it/1xnq8q0", "t3_1xnq8q0"),
    ("t3_1xnq8q0", "t3_1xnq8q0"),
    ("https://www.reddit.com/r/france/comments/1xpjv07/la_justice/19s8u8lfne2hv/", "t1_19s8u8lfne2hv"),
    ("t1_19s8u8lfne2hv", "t1_19s8u8lfne2hv"),
])
def test_identifiant_depuis_un_lien(lien, attendu):
    assert ReseauReddit._identifiant(lien) == attendu


def test_lire_un_post(tmp_path):
    r, faux = client(tmp_path)
    f = r.lire("https://www.reddit.com/r/france/comments/1xnq8q0/jordan_bardella_sur_rtl/")
    assert set(f) == CLES and f["id"] == "t3_1xnq8q0" and f["route"] == "arctic-shift"
    assert faux.requetes[0].url.path == "/api/posts/ids"
    assert faux.requetes[0].url.params["ids"] == "t3_1xnq8q0"
    assert f["preuve"]["url"] == "https://arctic-shift.photon-reddit.com/api/posts/ids?ids=t3_1xnq8q0"
    assert f["preuve"]["extrait"] in f["texte"]


def test_lire_un_commentaire(tmp_path):
    r, faux = client(tmp_path)
    f = r.lire("https://www.reddit.com/r/france/comments/1xpjv07/la_justice/19s8u8lfne2hv/")
    assert f["id"] == "t1_19s8u8lfne2hv" and f["texte"] == "Le père de Bardella ?"
    assert faux.requetes[0].url.path == "/api/comments/ids"


def test_lire_contenu_absent_de_l_archive(tmp_path):
    r, _ = client(tmp_path, Faux(lambda req: httpx.Response(200, text=VIDE)))
    with pytest.raises(RedditIntrouvableError):
        r.lire("t3_zzzzzz")


def test_lire_apres_le_gel_est_refuse(tmp_path):
    r, _ = client(tmp_path, date_gel="2026-01-01")
    with pytest.raises(RedditIntrouvableError):
        r.lire("t3_1xnq8q0")
    assert [j["raison"] for j in r.journal] == ["apres_gel"]


def test_lire_lien_illisible(tmp_path):
    r, faux = client(tmp_path)
    with pytest.raises(RedditError):
        r.lire("https://example.org/x")
    assert faux.requetes == []


def test_ligne_de_commande_ecrit_une_fiche_json_par_ligne(tmp_path, monkeypatch, capsys):
    faux = Faux()
    vrai_init = ReseauReddit.__init__

    def init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(faux)
        kw["dormir"] = lambda s: None
        vrai_init(self, *a, **kw)

    monkeypatch.setattr(ReseauReddit, "__init__", init)
    code = rd.principal(["chercher", "Bardella", "--limite", "20", "--cache", str(tmp_path / "c")])
    sortie = capsys.readouterr()
    assert code == 0
    lignes = [json.loads(ligne) for ligne in sortie.out.splitlines()]
    assert len(lignes) == 5 and all(set(f) == CLES for f in lignes)
    assert "appels_reseau=3" in sortie.err
    assert rd.principal(["lire", "https://example.org/x", "--cache", str(tmp_path / "c")]) == 1
