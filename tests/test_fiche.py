"""Fiche d'une page : réponses simulées, aucun réseau, aucun crédit."""

import httpx
import pytest

from enquete.fiche import LienNonReconnu, chercher_dans_oriane, fiche, fiche_texte, lire_lien

TIKTOK = "https://www.tiktok.com/@compte/video/7675835142736186641?lang=fr"


def faux_reseau(requete: httpx.Request) -> httpx.Response:
    if requete.url.path == "/oembed":
        return httpx.Response(200, json={"title": "Un titre", "author_name": "Un compte"})
    return httpx.Response(200, text='{"webapp.video-detail":{"statusCode":10000}}')


class FauxOriane:
    def __init__(self, resultats):
        self.resultats, self.appels = resultats, []

    def chercher_contenus(self, filtres, **kw):
        self.appels.append((filtres, kw))
        return {"data": {"results": self.resultats}}


def test_lire_lien_quatre_plateformes():
    assert lire_lien(TIKTOK)["identifiant"] == "7675835142736186641"
    assert lire_lien("https://www.instagram.com/compte/reel/DeUjG-DtqDC/")["type"] == "reel"
    assert lire_lien("https://youtu.be/dQw4w9WgXcQ?t=3")["identifiant"] == "dQw4w9WgXcQ"
    assert lire_lien("https://www.youtube.com/shorts/dQw4w9WgXcQ")["type"] == "short"
    assert lire_lien("https://twitter.com/compte/status/20")["plateforme"] == "x"


@pytest.mark.parametrize("lien", ["https://exemple.org/a", "pas un lien", "https://www.tiktok.com/@compte"])
def test_lire_lien_refuse_ce_qui_nest_pas_une_publication(lien):
    with pytest.raises(LienNonReconnu):
        lire_lien(lien)


def test_fiche_tiktok_complete():
    oriane = FauxOriane([{"id": "cnt_1", "platformId": "7675835142736186641", "publishedAt": "2026-08-19T20:01:04.000Z",
                          "profileHandle": "compte", "viewsCount": 10, "caption": "légende"}])
    with httpx.Client(transport=httpx.MockTransport(faux_reseau), follow_redirects=True) as c:
        f = fiche(TIKTOK, oriane=oriane, client=c)
    assert f["lien_origine"] == "https://www.tiktok.com/@compte/video/7675835142736186641"
    assert f["date"]["iso"].startswith("2026-08-19T20:00")
    assert f["etat_source"]["etat"] == "en_ligne"
    assert f["oembed"]["titre"] == "Un titre"
    assert f["oriane"]["statut"] == "trouvee"
    assert oriane.appels[0][0] == {"platformId": {"includes": [{"platform": "tiktok", "platformId": "7675835142736186641"}]}}
    texte = fiche_texte(f)
    assert "Lien d'origine" in texte and "en ligne" in texte and "cnt_1" in texte


def test_date_x_tiree_du_flocon():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)), follow_redirects=True) as c:
        f = fiche("https://x.com/compte/status/1939773777308430752", client=c)
    assert f["date"]["iso"].startswith("2025-06-30")
    assert f["etat_source"]["etat"] == "retiree"
    assert f["oriane"]["statut"] == "non_demandee"


def test_youtube_sans_date():
    with httpx.Client(transport=httpx.MockTransport(faux_reseau), follow_redirects=True) as c:
        f = fiche("https://youtu.be/dQw4w9WgXcQ", client=c)
    assert f["date"] is None
    assert "non lisible" in fiche_texte(f)


def test_panne_oriane_ne_casse_pas_la_fiche():
    class Panne:
        def chercher_contenus(self, *a, **k):
            raise RuntimeError("hors service")

    assert chercher_dans_oriane("tiktok", "1", Panne())["statut"] == "echec"


def test_oriane_ne_couvre_pas_x():
    assert chercher_dans_oriane("x", "1", FauxOriane([]))["statut"] == "non_couverte"


def test_oriane_ignore_un_resultat_dun_autre_identifiant():
    o = FauxOriane([{"id": "cnt_2", "platformId": "999"}])
    assert chercher_dans_oriane("tiktok", "1", o)["statut"] == "absente"
