"""Tests de l'adaptateur Telegram : transport simulé, aucun appel réseau."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from enquete.reseau import Preuve
from enquete.sources import reseau_telegram as tg
from enquete.sources.reseau_telegram import (
    ReseauTelegram,
    TelegramBloqueError,
    TelegramError,
    TelegramIntrouvableError,
    TelegramLimiteError,
)

REPONSES = Path(__file__).parent / "reponses"
MAINTENANT = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
CLES = {"plateforme", "id", "url", "auteur", "date", "texte", "langue", "medias", "metriques", "route",
        "recupere_le", "preuve"}


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
        hote, chemin, q = req.url.host, req.url.path, dict(req.url.params)
        if hote == "lyzem.com":
            return httpx.Response(200, text=lire_fixture("reseau_telegram_lyzem.html"))
        if hote == "t.me" and chemin.startswith("/s/"):
            canal = chemin[3:]
            if canal == "BFMTV":
                return httpx.Response(302, headers={"location": "https://t.me/BFMTV"})
            if q.get("before") == "10148":
                return httpx.Response(200, text=lire_fixture("reseau_telegram_lire.html"))
            if q.get("before") == "9289":
                page = lire_fixture("reseau_telegram_recherche.html").replace("9318", "9270")
                return httpx.Response(200, text=page.replace("rt_france/9289", "rt_france/9280")
                                      .replace("rt_france/9352", "rt_france/9275"))
            if "before" in q:
                return httpx.Response(200, text="<html><body><section></section></body></html>")
            page = lire_fixture("reseau_telegram_recherche.html")
            return httpx.Response(200, text=page.replace("rt_france", canal) if canal != "rt_france" else page)
        raise AssertionError(f"hôte inattendu : {req.url}")

    @property
    def hotes(self) -> list[str]:
        return [r.url.host for r in self.requetes]


def client(tmp_path, faux=None, **kw):
    faux = faux or Faux()
    dormi: list[float] = []
    r = ReseauTelegram(dossier_cache=tmp_path / "cache", transport=httpx.MockTransport(faux),
                       maintenant=lambda: MAINTENANT, dormir=dormi.append, **kw)
    r.dormi = dormi
    return r, faux


# --------------------------------------------------------------------------- contrat

def test_module_expose_le_contrat():
    assert tg.PLATEFORME == "telegram"
    assert tg.CAPACITES == {"chercher": True, "lire": True}
    assert "Telegram" in tg.ANGLE_MORT and "aperçu web" in tg.ANGLE_MORT


def test_chercher_fiches_conformes_et_preuve(tmp_path):
    r, faux = client(tmp_path)
    fiches = r.chercher("Macron", canaux=["rt_france"], pages=1)
    assert len(fiches) == 3
    assert [f["id"] for f in fiches] == ["rt_france/9289", "rt_france/9318", "rt_france/9352"]
    assert [f["date"] for f in fiches] == sorted(f["date"] for f in fiches)
    f = fiches[0]
    assert set(f) == CLES
    assert f["plateforme"] == "telegram"
    assert f["url"] == "https://t.me/rt_france/9289"
    assert f["auteur"] == "rt_france"
    assert f["date"] == "2023-07-12T15:15:04+00:00"
    assert f["metriques"] == {"vues": 638}
    assert f["route"] == "t.me/s"
    assert f["langue"] is None
    assert f["medias"][0]["type"] == "image" and f["medias"][0]["url"].startswith("https://")
    assert f["texte"].startswith("Livraison de missiles longue portée")
    assert "\n" in f["texte"]  # les <br> deviennent des retours à la ligne
    assert "macron" in f["texte"].lower()
    assert f["recupere_le"] == MAINTENANT.isoformat()
    assert f["preuve"]["url"] == "https://t.me/s/rt_france?q=Macron"
    assert f["preuve"]["extrait"] == f["texte"][:500]
    assert fiches[2]["medias"][0] == {"type": "video", "url": "https://t.me/rt_france/9352"}
    p = Preuve(url=f["preuve"]["url"], extrait=f["preuve"]["extrait"],
               source_donnee=f"{f['plateforme']} ({f['route']})",
               date_collecte=datetime.fromisoformat(f["recupere_le"]))
    assert p.source_donnee == "telegram (t.me/s)"
    assert faux.hotes == ["t.me"]


def test_lire_nombre_vues():
    assert tg.lire_nombre("638") == 638
    assert tg.lire_nombre("5.93K") == 5930
    assert tg.lire_nombre("1.2M") == 1_200_000
    assert tg.lire_nombre("") is None and tg.lire_nombre("abc") is None


def test_remonte_avec_before_jusqu_au_plus_petit_id(tmp_path):
    r, faux = client(tmp_path)
    fiches = r.chercher("Macron", canaux=["rt_france"], pages=2)
    urls = [str(q.url) for q in faux.requetes]
    assert urls == ["https://t.me/s/rt_france?q=Macron", "https://t.me/s/rt_france?q=Macron&before=9289"]
    assert {f["id"] for f in fiches} == {"rt_france/9289", "rt_france/9318", "rt_france/9352",
                                         "rt_france/9270", "rt_france/9280", "rt_france/9275"}
    assert [f["date"] for f in fiches] == sorted(f["date"] for f in fiches)


def test_limite_garde_les_plus_recents(tmp_path):
    r, _ = client(tmp_path)
    fiches = r.chercher("Macron", canaux=["rt_france"], pages=1, limite=2)
    assert [f["id"] for f in fiches] == ["rt_france/9318", "rt_france/9352"]
    assert any(j["raison"] == "limite" for j in r.journal)


# --------------------------------------------------------------------------- fenêtre et gel

def test_gel_ecarte_les_fiches_posterieures_et_les_journalise(tmp_path):
    r, _ = client(tmp_path, date_gel="2023-07-19")
    fiches = r.chercher("Macron", canaux=["rt_france"], pages=1)
    assert [f["id"] for f in fiches] == ["rt_france/9289", "rt_france/9318"]
    assert all(f["date"] < "2023-07-20" for f in fiches)
    assert all(f["metriques"] == {} for f in fiches)
    ecartees = [j for j in r.journal if j["raison"] == "apres_gel"]
    assert [j["url"] for j in ecartees] == ["https://t.me/rt_france/9352"]


def test_gel_ecarte_une_fiche_sans_date(tmp_path):
    sans_date = lire_fixture("reseau_telegram_recherche.html").replace(
        'datetime="2023-07-12T15:15:04+00:00"', "")

    def surcharge(req):
        return httpx.Response(200, text=sans_date) if req.url.host == "t.me" else None

    r, _ = client(tmp_path, Faux(surcharge), date_gel="2023-12-31")
    fiches = r.chercher("Macron", canaux=["rt_france"], pages=1)
    assert "rt_france/9289" not in {f["id"] for f in fiches}
    assert [j["url"] for j in r.journal if j["raison"] == "sans_date_sous_gel"] == ["https://t.me/rt_france/9289"]


def test_depuis_ecarte_hors_fenetre_et_arrete_la_remontee(tmp_path):
    r, faux = client(tmp_path)
    fiches = r.chercher("Macron", depuis="2023-07-20", canaux=["rt_france"], pages=3)
    assert [f["id"] for f in fiches] == ["rt_france/9352"]
    assert len(faux.requetes) == 1  # la page contient déjà un message antérieur à `depuis`
    assert sum(j["raison"] == "hors_fenetre" for j in r.journal) == 2


def test_fenetre_non_atteinte_une_ligne_de_journal(tmp_path):
    r, _ = client(tmp_path)
    r.chercher("Macron", depuis="2023-07-01", canaux=["rt_france"], pages=1)
    assert [j["raison"] for j in r.journal] == ["limite"]


def test_jusqua_ecarte_les_fiches_apres_la_borne(tmp_path):
    r, _ = client(tmp_path)
    fiches = r.chercher("Macron", jusqua="2023-07-18", canaux=["rt_france"], pages=1)
    assert [f["id"] for f in fiches] == ["rt_france/9289", "rt_france/9318"]
    assert [j["raison"] for j in r.journal] == ["hors_fenetre"]


# --------------------------------------------------------------------------- découverte

def test_decouverte_lyzem_sans_bots_ni_groupes(tmp_path):
    r, faux = client(tmp_path, date_gel="2023-12-31")
    fiches = r.chercher("élection présidentielle 2027", pages=1)
    decouverts = [j["url"] for j in r.journal if j["raison"] == "canal_decouvert"]
    assert decouverts == ["https://t.me/FranceXTelegram", "https://t.me/OVALmedia_Francais",
                          "https://t.me/enmarchefr"]
    assert faux.hotes[0] == "lyzem.com"
    assert {f["auteur"] for f in fiches} == {"FranceXTelegram", "OVALmedia_Francais", "enmarchefr"}
    assert all(f["date"] <= "2023-12-31T23:59:59+00:00" for f in fiches)


def test_decouverte_limitee_a_cinq(tmp_path):
    liens = "<ul>" + "".join(
        f'<li class="search-result"><div class="search-result-type-wrapper" title="channel"></div>'
        f'<a href="https://t.me/canal{i}">x</a></li>' for i in range(9)) + "</ul>"

    def surcharge(req):
        return httpx.Response(200, text=liens) if req.url.host == "lyzem.com" else None

    r, _ = client(tmp_path, Faux(surcharge))
    assert len(r._decouvrir("x")) == 5
    assert sum(j["raison"] == "canal_decouvert" for j in r.journal) == 5


# --------------------------------------------------------------------------- lire

def test_lire_un_message_par_son_lien(tmp_path):
    r, faux = client(tmp_path)
    f = r.lire("https://t.me/rt_france/10147")
    assert set(f) == CLES
    assert f["id"] == "rt_france/10147"
    assert f["preuve"]["url"] == "https://t.me/s/rt_france?before=10148"
    assert str(faux.requetes[0].url) == "https://t.me/s/rt_france?before=10148"
    assert f["date"].startswith("20")
    assert f["texte"]


def test_lire_message_absent_de_la_page(tmp_path):
    r, _ = client(tmp_path)
    with pytest.raises(TelegramIntrouvableError):
        r.lire("https://t.me/rt_france/10100")  # la page rendue s'arrête à d'autres identifiants


def test_lire_redirection_vers_le_canal_est_un_mur(tmp_path):
    r, _ = client(tmp_path)
    with pytest.raises(TelegramBloqueError, match="aperçu web fermé"):
        r.lire("https://t.me/BFMTV/1")


def test_lire_lien_illisible(tmp_path):
    r, faux = client(tmp_path)
    with pytest.raises(TelegramError):
        r.lire("https://example.org/x")
    assert faux.requetes == []


def test_lire_apres_le_gel_est_refuse(tmp_path):
    r, _ = client(tmp_path, date_gel="2000-01-01")
    with pytest.raises(TelegramIntrouvableError):
        r.lire("https://t.me/rt_france/10147")
    assert [j["raison"] for j in r.journal] == ["apres_gel"]


# --------------------------------------------------------------------------- filtre, cache, erreurs

def test_filtre_url_passe_avant_tout_appel(tmp_path):
    r, faux = client(tmp_path, filtre_url=lambda url: False)
    assert r.chercher("Macron", canaux=["rt_france"]) == []
    with pytest.raises(TelegramError):
        r.lire("https://t.me/rt_france/10147")
    assert faux.requetes == [] and r.appels_reseau == 0
    assert {j["raison"] for j in r.journal} == {"filtre_url"}
    assert not (tmp_path / "cache").exists()


def test_filtre_url_sur_chaque_fiche_rendue(tmp_path):
    r, _ = client(tmp_path, filtre_url=lambda url: not url.endswith("/9318"))
    fiches = r.chercher("Macron", canaux=["rt_france"], pages=1)
    assert [f["id"] for f in fiches] == ["rt_france/9289", "rt_france/9352"]
    assert [j["url"] for j in r.journal] == ["https://t.me/rt_france/9318"]


def test_cache_le_second_appel_ne_touche_pas_le_reseau(tmp_path):
    r1, _ = client(tmp_path)
    premier = r1.chercher("Macron", canaux=["rt_france"], pages=1)
    assert r1.appels_reseau == 1
    r2, faux2 = client(tmp_path)
    second = r2.chercher("Macron", canaux=["rt_france"], pages=1)
    assert r2.appels_reseau == 0 and faux2.requetes == []
    assert second == premier
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1
    assert not list((tmp_path / "cache").glob("*.tmp"))


def test_cache_expire_apres_la_duree(tmp_path):
    r1, _ = client(tmp_path)
    r1.chercher("Macron", canaux=["rt_france"], pages=1)
    r2 = ReseauTelegram(dossier_cache=tmp_path / "cache", transport=httpx.MockTransport(Faux()),
                        maintenant=lambda: datetime(2026, 11, 9, tzinfo=UTC), dormir=lambda s: None)
    r2.chercher("Macron", canaux=["rt_france"], pages=1)
    assert r2.appels_reseau == 1


def test_403_est_un_mur_constate(tmp_path):
    r, _ = client(tmp_path, Faux(lambda req: httpx.Response(403, text="Forbidden")))
    with pytest.raises(TelegramBloqueError) as e:
        r.chercher("Macron", canaux=["rt_france"])
    assert e.value.statut == 403


def test_429_attend_retry_after_puis_reussit(tmp_path):
    appels = []

    def surcharge(req):
        appels.append(1)
        if len(appels) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return None

    r, _ = client(tmp_path, Faux(surcharge))
    assert len(r.chercher("Macron", canaux=["rt_france"], pages=1)) == 3
    assert 7.0 in r.dormi and r.appels_reseau == 2


def test_429_deux_fois_leve_limite(tmp_path):
    r, _ = client(tmp_path, Faux(lambda req: httpx.Response(429)))
    with pytest.raises(TelegramLimiteError):
        r.chercher("Macron", canaux=["rt_france"])
    assert 20.0 in r.dormi


def test_404_est_introuvable(tmp_path):
    r, _ = client(tmp_path, Faux(lambda req: httpx.Response(404)))
    with pytest.raises(TelegramIntrouvableError):
        r.chercher("Macron", canaux=["inconnu"])


def test_apres_la_premiere_requete_une_erreur_est_journalisee(tmp_path):
    def surcharge(req):
        if req.url.path == "/s/ferme":
            return httpx.Response(302, headers={"location": "https://t.me/ferme"})
        return None

    r, _ = client(tmp_path, Faux(surcharge))
    fiches = r.chercher("Macron", canaux=["rt_france", "ferme"], pages=1)
    assert len(fiches) == 3
    assert [(j["url"], j["raison"]) for j in r.journal] == [("https://t.me/s/ferme", "mur")]


def test_requete_reseau_en_erreur_est_une_telegram_error(tmp_path):
    def surcharge(req):
        raise httpx.ConnectError("hors ligne")

    r, _ = client(tmp_path, Faux(surcharge))
    with pytest.raises(TelegramError, match="injoignable"):
        r.lire("https://t.me/rt_france/10147")


def test_ligne_de_commande_ecrit_une_fiche_json_par_ligne(tmp_path, monkeypatch, capsys):
    faux = Faux()
    vrai_init = ReseauTelegram.__init__

    def init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(faux)
        kw["dormir"] = lambda s: None
        vrai_init(self, *a, **kw)

    monkeypatch.setattr(ReseauTelegram, "__init__", init)
    code = tg.principal(["chercher", "Macron", "--canaux", "rt_france", "--pages", "1",
                         "--cache", str(tmp_path / "c")])
    sortie = capsys.readouterr()
    assert code == 0
    lignes = [json.loads(ligne) for ligne in sortie.out.splitlines()]
    assert len(lignes) == 3 and all(set(f) == CLES for f in lignes)
    assert "appels_reseau=1" in sortie.err
    assert tg.principal(["lire", "https://t.me/BFMTV/1", "--cache", str(tmp_path / "c")]) == 1
