"""Voie presse : pages simulées, aucun réseau. Comptes fictifs."""

import httpx

from enquete.sources.presse import MARQUE, extraire_posts, lire_article, pieces_presse, verifications_gdelt

ARTICLE = """
<html><head><title>Faux : cette photo n'est pas celle du barrage</title>
<meta property="article:published_time" content="2026-05-02T10:00:00Z"></head><body>
<p>La rumeur est née dans un message publié sur X
<a href="https://twitter.com/compte_fictif/status/1900000000000000001?s=20">ce post</a>.</p>
<blockquote class="twitter-tweet"><a href="https://x.com/autre_fictif/status/1900000000000000002">post</a></blockquote>
<a href="https://x.com/AFPFactuel/status/1900000000000000003">relais du média</a>
<a href="https://x.com/intent/tweet?text=partager">partager</a>
<a href="https://www.facebook.com/page.fictive/posts/123456789">Facebook</a>
<a href="https://www.facebook.com/sharer/sharer.php?u=x">partager</a>
</body></html>
"""


def test_extraire_posts_normalise_dedoublonne_et_ecarte_les_medias():
    posts = {p["url"]: p for p in extraire_posts(ARTICLE)}
    assert set(posts) == {
        "https://x.com/compte_fictif/status/1900000000000000001",
        "https://x.com/autre_fictif/status/1900000000000000002",
        "https://www.facebook.com/page.fictive/posts/123456789",
    }
    assert posts["https://x.com/autre_fictif/status/1900000000000000002"]["role"] == "encart"
    assert posts["https://x.com/compte_fictif/status/1900000000000000001"]["role"] == "lien"
    assert "rumeur" in posts["https://x.com/compte_fictif/status/1900000000000000001"]["extrait"]


def reseau(pages):
    def repondre(requete):
        return pages.get(str(requete.url), httpx.Response(404))

    return httpx.Client(transport=httpx.MockTransport(repondre), follow_redirects=True)


def test_pieces_presse_marque_et_cite_larticle():
    url = "https://verif.exemple/article"
    r = pieces_presse(urls_articles=[url], client=reseau({url: httpx.Response(200, text=ARTICLE)}))
    assert len(r["pieces"]) == 3
    p = next(p for p in r["pieces"] if p["plateforme"] == "facebook")
    assert p["marque"] == MARQUE and p["citee_par_la_presse"] is True
    c = p["citations"][0]
    assert c["article"] == url and c["via"] == "direct" and c["date_article"] == "2026-05-02T10:00:00Z"
    assert "barrage" in c["titre"]


def test_un_post_cite_par_deux_articles_reste_une_piece():
    a, b = "https://v1.exemple/a", "https://v2.exemple/b"
    pages = {a: httpx.Response(200, text=ARTICLE), b: httpx.Response(200, text=ARTICLE)}
    r = pieces_presse(urls_articles=[a, b], client=reseau(pages))
    assert len(r["pieces"]) == 3
    assert all(len(p["citations"]) == 2 for p in r["pieces"])


def test_article_ferme_bascule_sur_wayback_et_le_dit():
    url = "https://ferme.exemple/a"
    copie = f"https://web.archive.org/web/2/{url}"
    pages = {url: httpx.Response(403), copie: httpx.Response(200, text=ARTICLE)}
    lu = lire_article(url, reseau(pages))
    assert lu["via"] == "wayback" and lu["html"]


def test_article_ferme_sans_copie_est_signale_illisible():
    url = "https://ferme.exemple/a"
    r = pieces_presse(urls_articles=[url], client=reseau({url: httpx.Response(403)}))
    assert r["pieces"] == []
    assert r["articles"][0]["statut"].startswith("HTTP 403")
    assert r["journal"]


def test_gdelt_reessaie_apres_la_limite_de_debit():
    reponses = iter([
        httpx.Response(200, text="Please limit requests to one every 5 seconds"),
        httpx.Response(200, json={"articles": [{"url": "https://verif.exemple/a", "title": "T", "domain": "verif.exemple", "seendate": "20260502T100000Z"}]}),
    ])
    pauses = []
    c = httpx.Client(transport=httpx.MockTransport(lambda r: next(reponses)))
    out = verifications_gdelt("barrage", client=c, dormir=pauses.append, pause_s=7)
    assert [a["url"] for a in out] == ["https://verif.exemple/a"]
    assert pauses == [7]


def test_gdelt_toujours_limite_rend_vide():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="Please limit requests")))
    assert verifications_gdelt("x", client=c, essais=2, dormir=lambda s: None) == []


def test_gdelt_vide_ou_non_json():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="{}")))
    assert verifications_gdelt("x", client=c) == []
