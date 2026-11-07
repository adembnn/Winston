"""État d'une source : réponses simulées, aucun réseau."""

import httpx
import pytest

from enquete.etat_source import etat_source, plateforme_de


def client(reponse):
    """Client dont toutes les requêtes reçoivent `reponse(requete) -> httpx.Response`."""
    return httpx.Client(transport=httpx.MockTransport(reponse), follow_redirects=True)


def page_tiktok(code, message=""):
    msg = f',"statusMsg":"{message}"' if message else ""
    return f'<html><script>{{"webapp.video-detail":{{"statusCode":{code}{msg}}}}}</script></html>'


@pytest.mark.parametrize("code,message,attendu", [
    (10000, "", "en_ligne"),
    (10204, "item doesn't exist", "retiree"),
    (10204, "status_self_see", "privee"),
    (10222, "", "privee"),
    (10999, "autre", "indetermine"),
])
def test_tiktok_lit_le_code_detat(code, message, attendu):
    c = client(lambda r: httpx.Response(200, text=page_tiktok(code, message)))
    assert etat_source("https://www.tiktok.com/@compte/video/7300000000000000000", c)["etat"] == attendu


def test_tiktok_captcha_est_un_mur():
    c = client(lambda r: httpx.Response(200, text="<html>Please solve the CAPTCHA</html>"))
    assert etat_source("https://www.tiktok.com/@compte/video/7300000000000000000", c)["etat"] == "mur_connexion"


@pytest.mark.parametrize("code,attendu", [
    (200, "en_ligne"), (404, "retiree"), (401, "privee"), (403, "en_ligne"), (400, "retiree"), (500, "indetermine"),
])
def test_youtube_oembed(code, attendu):
    c = client(lambda r: httpx.Response(code, json={}))
    assert etat_source("https://www.youtube.com/watch?v=dQw4w9WgXcQ", c)["etat"] == attendu


def test_x_oembed_404_est_retiree():
    c = client(lambda r: httpx.Response(404, text="nope"))
    assert etat_source("https://x.com/compte/status/20", c)["etat"] == "retiree"


def test_instagram_coque_de_connexion_est_un_mur_et_le_dit():
    c = client(lambda r: httpx.Response(200, text="<html><title>Instagram</title></html>"))
    r = etat_source("https://www.instagram.com/reel/AAAAAAAAAAA/", c)
    assert r["etat"] == "mur_connexion"
    assert "pas dire" in r["raison"]


def test_instagram_avec_meta_publiques_est_en_ligne():
    c = client(lambda r: httpx.Response(200, text='<meta property="og:title" content="Un reel">'))
    assert etat_source("https://www.instagram.com/reel/AAAAAAAAAAA/", c)["etat"] == "en_ligne"


@pytest.mark.parametrize("code,attendu", [(200, "en_ligne"), (404, "retiree"), (410, "retiree"), (403, "mur_connexion"), (503, "indetermine")])
def test_site_web_par_code_http(code, attendu):
    c = client(lambda r: httpx.Response(code, text="x"))
    assert etat_source("https://exemple.org/article", c)["etat"] == attendu


def test_panne_reseau_donne_indetermine_jamais_retiree():
    def panne(requete):
        raise httpx.ConnectError("coupé")

    r = etat_source("https://exemple.org/article", client(panne))
    assert r["etat"] == "indetermine"
    assert r["http"] is None


def test_url_invalide_refusee():
    with pytest.raises(ValueError):
        etat_source("ftp://exemple.org/x")


def test_plateforme_de():
    assert plateforme_de("https://twitter.com/a/status/1") == "x"
    assert plateforme_de("https://youtu.be/dQw4w9WgXcQ") == "youtube"
    assert plateforme_de("https://exemple.org") == "web"
