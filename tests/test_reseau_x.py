"""Tests de l'adaptateur X : transport simulé qui lève sur tout hôte inconnu, aucun appel réseau."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from enquete.reseau import Preuve
from enquete.sources import reseau_x
from enquete.sources.reseau_x import (
    ANGLE_MORT,
    CAPACITES,
    PLATEFORME,
    ReseauX,
    XBloqueError,
    XIntrouvableError,
    XLimiteError,
    date_depuis_id,
    jeton_depuis_id,
)

REPONSES = Path(__file__).parent / "reponses"
SYNDICATION = (REPONSES / "reseau_x_syndication.json").read_text("utf-8")
OEMBED = (REPONSES / "reseau_x_oembed.json").read_text("utf-8")
BRAVE = (REPONSES / "reseau_x_brave.html").read_text("utf-8")
MAINTENANT = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
LCP = "https://x.com/LCP/status/2092966029823447317"
CLES = {
    "plateforme", "id", "url", "auteur", "date", "texte", "langue",
    "medias", "metriques", "route", "recupere_le", "preuve",
}


def id_au(iso: str) -> str:
    """Identifiant X fabriqué pour une date donnée (inverse de la formule officielle)."""
    ms = int(datetime.fromisoformat(iso).timestamp() * 1000)
    return str((ms - 1288834974657) << 22 | 7)


ID_AVANT = id_au("2026-08-30T10:00:00+00:00")
ID_APRES = id_au("2026-10-17T10:00:00+00:00")


def tweet(ident: str, auteur: str, texte: str, avec_date: bool = True) -> str:
    d = date_depuis_id(ident)
    data = {
        "__typename": "Tweet", "id_str": ident, "lang": "fr", "text": texte,
        "favorite_count": 12, "conversation_count": 3,
        "user": {"screen_name": auteur},
        "mediaDetails": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/a.jpg"}],
    }
    if avec_date:
        data["created_at"] = d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps(data)


class Faux:
    """Faux serveur : répond selon l'hôte, garde les requêtes, lève sur un hôte inconnu."""

    def __init__(self, **surcharges):
        self.requetes: list[httpx.Request] = []
        self.surcharges = surcharges  # hote -> fonction(requete) -> Response
        self.brave = BRAVE.replace("{ID_AVANT}", ID_AVANT).replace("{ID_APRES}", ID_APRES)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requetes.append(req)
        hote = req.url.host
        if hote in self.surcharges:
            return self.surcharges[hote](req)
        if hote == "search.brave.com":
            return httpx.Response(200, text=self.brave)
        if hote == "cdn.syndication.twimg.com":
            ident = req.url.params["id"]
            if ident == "2092966029823447317":
                return httpx.Response(200, text=SYNDICATION)
            auteur = "franceinfo" if ident == ID_AVANT else "Elysee"
            return httpx.Response(200, text=tweet(ident, auteur, f"texte {ident}"))
        if hote == "publish.x.com":
            return httpx.Response(200, text=OEMBED)
        raise AssertionError(f"hôte inconnu : {hote}")

    def hotes(self) -> list[str]:
        return [r.url.host for r in self.requetes]


def fabrique(tmp_path, faux=None, **options):
    faux = faux or Faux()
    pauses: list[float] = []
    x = ReseauX(
        dossier_cache=tmp_path / "cache",
        transport=httpx.MockTransport(faux),
        maintenant=lambda: MAINTENANT,
        dormir=pauses.append,
        **options,
    )
    x.pauses = pauses
    return x, faux


# --- fiche -----------------------------------------------------------------------------------


def test_constantes_du_module():
    assert PLATEFORME == "x"
    assert CAPACITES == {"chercher": True, "lire": True}
    assert "recherche passe par un moteur web" in ANGLE_MORT


def test_lire_donne_les_12_cles_et_une_preuve_constructible(tmp_path):
    x, _ = fabrique(tmp_path)
    fiche = x.lire(LCP)
    assert set(fiche) == CLES
    assert fiche["auteur"] == "LCP"
    assert fiche["date"] == "2026-08-27T13:22:41+00:00"
    assert fiche["route"] == "syndication"
    assert fiche["langue"] == "fr"
    assert fiche["metriques"] == {"likes": 0, "reponses": 5}
    assert fiche["medias"] == [
        {"type": "lien", "url": "https://lcp.fr/actualites/presidentielle-2027-la-liste-des-candidats-deja-en-lice-et-des-pretendants-436373"}
    ]
    assert fiche["preuve"]["url"] == (
        "https://cdn.syndication.twimg.com/tweet-result?id=2092966029823447317&lang=fr&token=a"
    )
    assert fiche["preuve"]["extrait"] in fiche["texte"]
    assert len(fiche["preuve"]["extrait"]) <= 500
    preuve = Preuve(
        url=fiche["preuve"]["url"],
        extrait=fiche["preuve"]["extrait"],
        source_donnee=f"{fiche['plateforme']} ({fiche['route']})",
        date_collecte=datetime.fromisoformat(fiche["recupere_le"]),
    )
    assert preuve.source_donnee == "x (syndication)"


def test_date_de_l_identifiant_egale_created_at_a_la_seconde(tmp_path):
    x, _ = fabrique(tmp_path)
    fiche = x.lire(LCP)
    brut = json.loads(SYNDICATION)["created_at"]
    attendu = datetime.fromisoformat(brut.replace("Z", "+00:00")).replace(microsecond=0)
    assert date_depuis_id(fiche["id"]).replace(microsecond=0) == attendu
    assert date_depuis_id("123") is None  # avant l'ère des identifiants datés


def test_jeton_calcule_stable():
    assert jeton_depuis_id("2092966029823447317") == "52n8vq75mtod47"


def test_lien_non_reconnu(tmp_path):
    x, faux = fabrique(tmp_path)
    with pytest.raises(XIntrouvableError):
        x.lire("https://x.com/LCP")
    assert faux.requetes == []


# --- chercher --------------------------------------------------------------------------------


def test_chercher_dedoublonne_ordonne_et_ecarte_les_liens_sans_post(tmp_path):
    x, faux = fabrique(tmp_path)
    fiches = x.chercher("présidentielle 2027", pages=1)
    assert [f["id"] for f in fiches] == ["2092966029823447317", ID_AVANT, ID_APRES]
    dates = [f["date"] for f in fiches]
    assert dates == sorted(dates)
    assert len({f["id"] for f in fiches}) == len(fiches) == 3
    assert all(set(f) == CLES for f in fiches)
    requete = next(r for r in faux.requetes if r.url.host == "search.brave.com")
    assert requete.url.params["q"] == "site:x.com présidentielle 2027"
    assert requete.url.params["source"] == "web"
    assert requete.url.params["offset"] == "0"
    assert "tf" not in requete.url.params
    assert "enquete-osint" in requete.headers["user-agent"]
    synd = next(r for r in faux.requetes if r.url.host == "cdn.syndication.twimg.com")
    assert "Chrome" not in synd.headers["user-agent"]


def test_chercher_sous_gel_ecarte_avant_lecture_et_journalise(tmp_path):
    x, faux = fabrique(tmp_path, date_gel="2026-09-16")
    fiches = x.chercher("17 octobre", pages=1)
    assert {f["id"] for f in fiches} == {"2092966029823447317", ID_AVANT}
    assert all(f["date"] < "2026-09-17" for f in fiches)
    assert all(f["metriques"] == {} for f in fiches)
    lus = [r.url.params["id"] for r in faux.requetes if r.url.host == "cdn.syndication.twimg.com"]
    assert ID_APRES not in lus  # jamais lu
    rejets = [j for j in x.journal if j["raison"] == "apres_gel"]
    assert len(rejets) == 1 and ID_APRES in rejets[0]["url"]
    assert set(rejets[0]) == {"url", "raison", "date"}


def test_gel_seul_vaut_fin_de_journee(tmp_path):
    ident = id_au("2026-09-16T23:30:00+00:00")
    faux = Faux()
    faux.brave = f'<a href="https://x.com/Elysee/status/{ident}">x</a>'
    x, _ = fabrique(tmp_path, faux, date_gel="2026-09-16")
    assert [f["id"] for f in x.chercher("a", pages=1)] == [ident]


def test_fiche_sans_date_ecartee_sous_gel(tmp_path):
    def sans_date(req):
        return httpx.Response(200, text=tweet(req.url.params["id"], "Elysee", "t", avec_date=False))

    faux = Faux(**{"cdn.syndication.twimg.com": sans_date})
    x, _ = fabrique(tmp_path, faux, date_gel="2026-12-31")
    assert x.chercher("a", pages=1) == []
    assert {j["raison"] for j in x.journal} == {"sans_date_sous_gel"}


def test_fenetre_depuis_jusqua(tmp_path):
    x, _ = fabrique(tmp_path)
    fiches = x.chercher("a", depuis="2026-09-01", jusqua="2026-12-01", pages=1)
    assert {f["id"] for f in fiches} == {ID_APRES}
    assert {j["raison"] for j in x.journal} == {"hors_fenetre"}


def test_semaine_recente_ajoute_tf_pw_sauf_sous_gel(tmp_path):
    x, faux = fabrique(tmp_path)
    x.chercher("a", depuis="2026-11-05", pages=1)
    brave = [r for r in faux.requetes if r.url.host == "search.brave.com"][0]
    assert brave.url.params["tf"] == "pw"
    x2, faux2 = fabrique(tmp_path / "autre", date_gel="2026-11-06")
    x2.chercher("a", depuis="2026-11-05", pages=1)
    brave2 = [r for r in faux2.requetes if r.url.host == "search.brave.com"][0]
    assert "tf" not in brave2.url.params


def test_compte_restreint_la_recherche_et_les_liens(tmp_path):
    x, faux = fabrique(tmp_path)
    fiches = x.chercher("budget", compte="@LCP", pages=1)
    brave = [r for r in faux.requetes if r.url.host == "search.brave.com"][0]
    assert brave.url.params["q"] == "site:x.com/LCP budget"
    assert [f["auteur"] for f in fiches] == ["LCP"]


def test_pages_quatre_secondes_entre_deux_pages_et_arret_sans_nouveaux_liens(tmp_path):
    x, faux = fabrique(tmp_path)
    x.chercher("a", pages=3)
    pages = [r for r in faux.requetes if r.url.host == "search.brave.com"]
    assert [r.url.params["offset"] for r in pages] == ["0", "1"]  # la 2e page n'apporte rien : arrêt
    assert any(abs(p - 4.0) < 0.2 for p in x.pauses)


def test_limite_coupe_la_liste(tmp_path):
    x, _ = fabrique(tmp_path)
    assert len(x.chercher("a", limite=1, pages=1)) == 1


def test_filtre_url_avant_tout_appel(tmp_path):
    x, faux = fabrique(tmp_path, filtre_url=lambda u: False)
    assert x.chercher("a") == []
    assert x.appels_reseau == 0 and faux.requetes == []
    with pytest.raises(XBloqueError):
        x.lire(LCP)
    assert faux.requetes == []
    assert {j["raison"] for j in x.journal} == {"filtre_url"}
    assert not (tmp_path / "cache").exists()


def test_filtre_url_sur_une_fiche_seulement(tmp_path):
    x, faux = fabrique(tmp_path, filtre_url=lambda u: "Elysee" not in u)
    fiches = x.chercher("a", pages=1)
    assert all(f["auteur"] != "Elysee" for f in fiches)
    assert ID_APRES not in [r.url.params.get("id") for r in faux.requetes]


# --- cache -----------------------------------------------------------------------------------


def test_cache_second_appel_zero_requete(tmp_path):
    x, faux = fabrique(tmp_path)
    premiere = x.lire(LCP)
    assert x.appels_reseau == 1
    y, faux2 = fabrique(tmp_path)
    seconde = y.lire(LCP)
    assert y.appels_reseau == 0 and faux2.requetes == []
    assert seconde == premiere
    fichiers = list((tmp_path / "cache").glob("*.json"))
    assert len(fichiers) == 1 and len(fichiers[0].stem) == 64
    assert not list((tmp_path / "cache").glob("*.tmp"))


def test_cache_perime(tmp_path):
    x, _ = fabrique(tmp_path, duree_cache=10)
    x.lire(LCP)
    y, _ = fabrique(tmp_path, duree_cache=10)
    y._maintenant = lambda: datetime(2026, 11, 8, tzinfo=UTC)
    y.lire(LCP)
    assert y.appels_reseau == 1


# --- erreurs ---------------------------------------------------------------------------------


def test_404_introuvable(tmp_path):
    faux = Faux(**{"cdn.syndication.twimg.com": lambda r: httpx.Response(404, text="<html>")})
    x, _ = fabrique(tmp_path, faux)
    with pytest.raises(XIntrouvableError) as e:
        x.lire(LCP)
    assert e.value.statut == 404
    assert x.journal[-1]["raison"] == "introuvable"
    assert "publish.x.com" not in faux.hotes()


@pytest.mark.parametrize("corps", ["{}", '{"__typename":"TweetTombstone","tombstone":{}}'])
def test_supprime_ou_vide_introuvable(tmp_path, corps):
    faux = Faux(**{"cdn.syndication.twimg.com": lambda r: httpx.Response(200, text=corps)})
    x, _ = fabrique(tmp_path, faux)
    with pytest.raises(XIntrouvableError):
        x.lire(LCP)


def test_403_bloque_constate_puis_secours_oembed(tmp_path):
    faux = Faux(**{"cdn.syndication.twimg.com": lambda r: httpx.Response(403, text="no")})
    x, _ = fabrique(tmp_path, faux)
    fiche = x.lire(LCP)
    assert fiche["route"] == "oembed"
    assert any(j["raison"] == "mur" for j in x.journal)


def test_403_sur_brave_leve_bloque(tmp_path):
    faux = Faux(**{"search.brave.com": lambda r: httpx.Response(403, text="captcha")})
    x, _ = fabrique(tmp_path, faux)
    with pytest.raises(XBloqueError):
        x.chercher("a")
    assert x.appels_reseau == 1  # pas de contournement : un seul essai


def test_429_retry_after_puis_succes(tmp_path):
    etat = {"n": 0}

    def une_fois(req):
        etat["n"] += 1
        if etat["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, text=SYNDICATION)

    x, _ = fabrique(tmp_path, Faux(**{"cdn.syndication.twimg.com": une_fois}))
    assert x.lire(LCP)["route"] == "syndication"
    assert 7.0 in x.pauses


def test_429_sans_retry_after_attend_30s_une_fois_puis_leve(tmp_path):
    faux = Faux(**{"search.brave.com": lambda r: httpx.Response(429, text="limite")})
    x, _ = fabrique(tmp_path, faux)
    with pytest.raises(XLimiteError):
        x.chercher("a")
    assert 30.0 in x.pauses
    assert x.appels_reseau == 2
    assert x.journal[-1]["raison"] == "limite"


def test_page_suivante_en_echec_journalise_et_continue(tmp_path):
    faux = Faux()
    base = faux.brave

    def brave(req):
        if req.url.params["offset"] == "0":
            return httpx.Response(200, text=base)
        return httpx.Response(429, text="")

    faux.surcharges["search.brave.com"] = brave
    x, _ = fabrique(tmp_path, faux)
    assert len(x.chercher("a", pages=3)) == 3  # la 1re page suffit, pas de levée
    assert any(j["raison"] == "limite" for j in x.journal)


def test_secours_oembed_donne_date_au_jour(tmp_path):
    faux = Faux(**{"cdn.syndication.twimg.com": lambda r: httpx.Response(500, text="oups")})
    x, _ = fabrique(tmp_path, faux)
    fiche = x.lire(LCP)
    assert fiche["route"] == "oembed"
    assert fiche["date"] == "2026-08-27"
    assert fiche["auteur"] == "LCP"
    assert fiche["texte"].startswith("Présidentielle 2027: la liste des candidats")
    assert fiche["metriques"] == {}
    assert set(fiche) == CLES
    assert fiche["preuve"]["url"].startswith("https://publish.x.com/oembed?url=https%3A%2F%2Fx.com%2FLCP")


def test_lire_sous_gel_apres_gel_refuse_sans_appel(tmp_path):
    x, faux = fabrique(tmp_path, date_gel="2026-09-16")
    url = f"https://x.com/Elysee/status/{ID_APRES}"
    with pytest.raises(XIntrouvableError):
        x.lire(url)
    assert faux.requetes == []
    assert x.journal[-1]["raison"] == "apres_gel"


def test_hote_inconnu_leve_dans_le_faux_serveur():
    with pytest.raises(AssertionError):
        Faux()(httpx.Request("GET", "https://exemple.org/"))


def test_ligne_de_commande(tmp_path, monkeypatch, capsys):
    x, _ = fabrique(tmp_path)
    monkeypatch.setattr(reseau_x, "ReseauX", lambda **kw: x)
    code = reseau_x._principal(["lire", LCP])
    sortie = capsys.readouterr()
    assert code == 0
    assert json.loads(sortie.out.splitlines()[0])["id"] == "2092966029823447317"
    assert "appels réseau" in sortie.err
