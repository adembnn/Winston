"""Voie presse : retrouver le post d'origine (X, Facebook, Threads) dans les vérifications publiées.

Oriane ne voit ni X ni Facebook ni Threads, alors qu'une rumeur y naît souvent. Les
vérifications publiées (AFP Factuel, Les Décodeurs, Lead Stories, etc.) citent presque toujours
le post d'origine, en lien ou en encart. Cette voie :

1. trouve des articles de vérification (GDELT, ou des URL données à la main) ;
2. lit chaque article ; si le site refuse le lecteur anonyme (401, 402, 403), elle essaie la copie
   publique de la Wayback Machine et le dit (`via: "wayback"`) ; sinon l'article est écarté ;
3. en extrait les liens vers des posts, sans jamais suivre un lien de partage ;
4. rend des pièces marquées « citée par la presse » : le post n'est PAS prouvé par la presse,
   il est seulement désigné par elle ; la solidité reste à l'appréciation du journaliste.

Ce module ne contourne aucun mur : un article inaccessible reste signalé comme tel.
"""

from __future__ import annotations

import html as html_lib
import re
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

MARQUE = "citée par la presse"
URL_GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
DOMAINES_VERIF = ("factuel.afp.com", "lemonde.fr", "leadstories.com", "fullfact.org", "snopes.com", "politifact.com")
# Comptes des médias eux-mêmes : leurs encarts ne sont pas l'origine d'une rumeur.
COMPTES_MEDIAS = frozenset({
    "afpfr", "afpfactuel", "afp", "afpfactcheck", "lemondefr", "decodeurs", "leadstories", "fullfact",
    "snopes", "politifact", "reuters", "reutersfacts", "apfactcheck", "ap", "x", "twitter",
})

_X = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/(?P<pseudo>[A-Za-z0-9_]{1,15})/status(?:es)?/(?P<id>\d{5,25})")
_FB = re.compile(
    r"https?://(?:www\.|m\.|web\.)?facebook\.com/(?P<chemin>"
    r"[A-Za-z0-9_.\-]+/(?:posts|videos)/[A-Za-z0-9_\-]+|share/[pvr]/[A-Za-z0-9_\-]+|reel/\d+|watch/\?v=\d+|permalink\.php\?story_fbid=[A-Za-z0-9_\-]+)"
)
_THREADS = re.compile(r"https?://(?:www\.)?threads\.(?:net|com)/@(?P<pseudo>[A-Za-z0-9_.]+)/post/(?P<id>[A-Za-z0-9_\-]+)")
_EMBED = re.compile(r"<blockquote[^>]*(?:twitter-tweet|fb-xfbml|fb-post|instagram-media)[^>]*>.*?</blockquote>", re.S | re.I)
_TITRE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"|<title[^>]*>(.*?)</title>', re.S | re.I)
_DATE_ARTICLE = re.compile(
    r'(?:property="article:published_time"|name="date"|itemprop="datePublished")[^>]*content="([^"]+)"'
    r'|content="([^"]+)"[^>]*(?:property="article:published_time"|itemprop="datePublished")', re.I)


def _normaliser(m: re.Match[str], plateforme: str) -> dict | None:
    if plateforme == "x":
        pseudo = m["pseudo"]
        return {"plateforme": "x", "pseudo": pseudo, "identifiant": m["id"],
                "url": f"https://x.com/{pseudo}/status/{m['id']}"}
    if plateforme == "threads":
        return {"plateforme": "threads", "pseudo": m["pseudo"], "identifiant": m["id"],
                "url": f"https://www.threads.net/@{m['pseudo']}/post/{m['id']}"}
    chemin = m["chemin"]
    pseudo = chemin.split("/")[0] if "/posts/" in chemin or "/videos/" in chemin else None
    return {"plateforme": "facebook", "pseudo": pseudo, "identifiant": chemin.rsplit("/", 1)[-1],
            "url": f"https://www.facebook.com/{chemin}"}


def _texte_autour(page: str, debut: int, fin: int, marge: int = 220) -> str:
    morceau = page[max(0, debut - marge): fin + marge]
    morceau = re.sub(r"<(script|style).*?</\1>", " ", morceau, flags=re.S | re.I)
    morceau = html_lib.unescape(re.sub(r"<[^>]+>", " ", morceau))
    return re.sub(r"\s+", " ", morceau).strip()[: 2 * marge]


def extraire_posts(page: str, comptes_exclus: Iterable[str] = COMPTES_MEDIAS) -> list[dict]:
    """Liens vers des posts X, Facebook ou Threads présents dans l'article, sans doublon.

    `role` : « encart » si le post est embarqué (blockquote), « lien » sinon. Les posts des
    comptes de médias (`comptes_exclus`) sont écartés : ce sont des relais, pas l'origine.
    """
    exclus = {c.lower() for c in comptes_exclus}
    encarts = [(m.start(), m.end()) for m in _EMBED.finditer(page)]
    vus: dict[str, dict] = {}
    for motif, pf in ((_X, "x"), (_FB, "facebook"), (_THREADS, "threads")):
        for m in motif.finditer(page):
            post = _normaliser(m, pf)
            if post is None or (post["pseudo"] or "").lower() in exclus:
                continue
            en_encart = any(a <= m.start() < b for a, b in encarts)
            post["role"] = "encart" if en_encart else "lien"
            post["extrait"] = _texte_autour(page, m.start(), m.end())
            ancien = vus.get(post["url"])
            if ancien is None or (ancien["role"] == "lien" and en_encart):
                vus[post["url"]] = post
    return list(vus.values())


def lire_article(url: str, client: httpx.Client) -> dict:
    """Lit un article. Renvoie `{url, html, via, statut}` ; `html` vaut None si illisible."""
    try:
        rep = client.get(url, headers={"User-Agent": AGENT})
    except httpx.HTTPError as erreur:
        return {"url": url, "html": None, "via": None, "statut": f"réseau : {type(erreur).__name__}"}
    if rep.status_code == 200:
        return {"url": url, "html": rep.text, "via": "direct", "statut": "ok"}
    if rep.status_code in (401, 402, 403):
        try:
            copie = client.get(f"https://web.archive.org/web/2/{url}", headers={"User-Agent": AGENT})
        except httpx.HTTPError:
            copie = None
        if copie is not None and copie.status_code == 200 and "web.archive.org/web/" in str(copie.url).lower():
            return {"url": url, "html": copie.text, "via": "wayback", "statut": "ok"}
        return {"url": url, "html": None, "via": None, "statut": f"HTTP {rep.status_code}, pas de copie Wayback"}
    return {"url": url, "html": None, "via": None, "statut": f"HTTP {rep.status_code}"}


def _titre(page: str) -> str:
    m = _TITRE.search(page)
    brut = (m.group(1) or m.group(2)) if m else ""
    return re.sub(r"\s+", " ", html_lib.unescape(brut)).strip()


def _date_article(page: str) -> str | None:
    m = _DATE_ARTICLE.search(page)
    return (m.group(1) or m.group(2)) if m else None


def verifications_gdelt(
    requete: str,
    domaines: Iterable[str] = DOMAINES_VERIF,
    client: httpx.Client | None = None,
    essais: int = 5,
    pause_s: float = 10.0,
    dormir: Callable[[float], None] = time.sleep,
    periode: str = "6months",
    maximum: int = 15,
) -> list[dict]:
    """Articles de vérification trouvés par GDELT (`{url, titre, domaine, date}`).

    GDELT limite à une requête toutes les 5 s et répond par une phrase au lieu d'un JSON ;
    on attend et on réessaie. Au bout de `essais`, on rend une liste vide plutôt que d'inventer.
    """
    filtre_domaines = " OR ".join(f"domain:{d}" for d in domaines)
    propre = client is None
    client = client or httpx.Client(timeout=40.0)
    try:
        for tentative in range(essais):
            rep = client.get(URL_GDELT, params={
                "query": f"{requete} ({filtre_domaines})", "mode": "artlist", "format": "json",
                "maxrecords": maximum, "timespan": periode, "sort": "datedesc"})
            if rep.text.startswith("Please limit"):
                if tentative < essais - 1:
                    dormir(pause_s)
                continue
            try:
                articles = rep.json().get("articles", [])
            except ValueError:
                return []
            return [{"url": a["url"], "titre": a.get("title", ""), "domaine": a.get("domain", ""),
                     "date": a.get("seendate")} for a in articles if a.get("url")]
        return []
    finally:
        if propre:
            client.close()


def pieces_presse(
    sujet: str | None = None,
    urls_articles: Iterable[str] = (),
    client: httpx.Client | None = None,
    domaines: Iterable[str] = DOMAINES_VERIF,
    dormir: Callable[[float], None] = time.sleep,
    comptes_exclus: Iterable[str] = COMPTES_MEDIAS,
) -> dict:
    """Pièces « citée par la presse » pour un sujet et/ou des articles donnés.

    Renvoie `{pieces, articles, journal}`. Chaque pièce : `url`, `plateforme`, `pseudo`,
    `identifiant`, `marque`, `citations` (article, titre, date, rôle, extrait, mode de lecture).
    Un post cité par plusieurs articles reste une seule pièce, avec toutes ses citations.
    """
    journal: list[str] = []
    candidats: list[dict] = [{"url": u, "titre": "", "domaine": urlparse(u).hostname or "", "date": None}
                             for u in urls_articles]
    propre = client is None
    client = client or httpx.Client(timeout=40.0, follow_redirects=True)
    try:
        if sujet:
            trouves = verifications_gdelt(sujet, domaines, client=client, dormir=dormir)
            journal.append(f"GDELT : {len(trouves)} article(s) pour « {sujet} »")
            deja = {c["url"] for c in candidats}
            candidats += [t for t in trouves if t["url"] not in deja]
        pieces: dict[str, dict] = {}
        articles: list[dict] = []
        for cand in candidats:
            lu = lire_article(cand["url"], client)
            fiche_article = {"url": cand["url"], "titre": cand["titre"], "domaine": cand["domaine"],
                             "via": lu["via"], "statut": lu["statut"], "nb_posts": 0}
            articles.append(fiche_article)
            if lu["html"] is None:
                journal.append(f"illisible : {cand['url']} ({lu['statut']})")
                continue
            titre = _titre(lu["html"]) or cand["titre"]
            fiche_article["titre"] = titre
            date_art = _date_article(lu["html"]) or cand["date"]
            posts = extraire_posts(lu["html"], comptes_exclus)
            fiche_article["nb_posts"] = len(posts)
            for p in posts:
                piece = pieces.setdefault(p["url"], {
                    "url": p["url"], "plateforme": p["plateforme"], "pseudo": p["pseudo"],
                    "identifiant": p["identifiant"], "marque": MARQUE, "citee_par_la_presse": True,
                    "citations": []})
                piece["citations"].append({
                    "article": cand["url"], "titre": titre, "domaine": cand["domaine"],
                    "date_article": date_art, "role": p["role"], "extrait": p["extrait"], "via": lu["via"]})
        return {"pieces": list(pieces.values()), "articles": articles, "journal": journal,
                "etabli_le": datetime.now(UTC).isoformat(timespec="seconds")}
    finally:
        if propre:
            client.close()
