"""État d'une source : en ligne, retirée, derrière un mur de connexion, ou privée.

Un journaliste qui ouvre une pièce doit savoir, avant de cliquer, si la page existe encore.
On interroge la plateforme comme le ferait un visiteur anonyme, sans compte et sans
contournement :

- TikTok : la page embarque un code d'état (`statusCode`) ; 10204 = introuvable ou visible de
  son seul auteur, 10222 = compte privé ;
- YouTube et X : l'oEmbed public répond 200 (en ligne), 404 (retirée), 401 (privée ou
  restreinte) ;
- Instagram : un visiteur anonyme reçoit la page de connexion pour une publication existante
  comme pour une publication inexistante (mesuré le 7 nov. 2026) : on rend `mur_connexion` et
  on dit qu'on ne peut pas trancher ;
- tout autre site : le code HTTP (200, 404 ou 410, 401 ou 403).

Un réseau qui échoue ou un code inattendu donne `indetermine`, jamais `retiree` : on n'affirme
pas la disparition d'une page qu'on n'a pas pu lire.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

import httpx

EN_LIGNE = "en_ligne"
RETIREE = "retiree"
MUR_CONNEXION = "mur_connexion"
PRIVEE = "privee"
INDETERMINE = "indetermine"
ETATS = (EN_LIGNE, RETIREE, MUR_CONNEXION, PRIVEE, INDETERMINE)

DELAI_S = 25.0
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

_STATUT_TIKTOK = re.compile(r'"webapp\.video-detail":\{"statusCode":(\d+)(?:,"statusMsg":"([^"]*)")?')
_STATUT_TIKTOK_LARGE = re.compile(r'"statusCode":(\d+)(?:,"statusMsg":"([^"]*)")?')
_MARQUES_CAPTCHA = ("captcha", "verify you are human", "are you a robot", "unusual traffic")
_MARQUES_CONNEXION_INSTAGRAM = ("/accounts/login", "loginform", "login_required")


def plateforme_de(url: str) -> str:
    hote = (urlparse(url).hostname or "").lower()
    if hote == "youtu.be" or hote.endswith("youtube.com"):
        return "youtube"
    if hote.endswith("tiktok.com"):
        return "tiktok"
    if hote.endswith("instagram.com"):
        return "instagram"
    if hote in ("x.com", "twitter.com") or hote.endswith((".x.com", ".twitter.com")):
        return "x"
    return "web"


def _resultat(url: str, plateforme: str, etat: str, raison: str, methode: str, http: int | None) -> dict:
    return {
        "url": url,
        "plateforme": plateforme,
        "etat": etat,
        "raison": raison,
        "methode": methode,
        "http": http,
        "verifie_le": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _tiktok(url: str, client: httpx.Client) -> dict:
    rep = client.get(url, headers={"User-Agent": AGENT})
    texte = rep.text
    if rep.status_code in (403, 429) or any(m in texte[:6000].lower() for m in _MARQUES_CAPTCHA):
        return _resultat(url, "tiktok", MUR_CONNEXION, "captcha ou blocage anti-robot", "page", rep.status_code)
    if rep.status_code == 404:
        return _resultat(url, "tiktok", RETIREE, "HTTP 404", "page", 404)
    trouve = _STATUT_TIKTOK.search(texte) or _STATUT_TIKTOK_LARGE.search(texte)
    if not trouve:
        return _resultat(url, "tiktok", INDETERMINE, "code d'état TikTok absent de la page", "page", rep.status_code)
    code, message = int(trouve.group(1)), (trouve.group(2) or "")
    if code in (0, 10000):
        return _resultat(url, "tiktok", EN_LIGNE, "statusCode 10000", "page", rep.status_code)
    if code == 10222 or message == "status_self_see":
        return _resultat(url, "tiktok", PRIVEE,
                         f"statusCode {code} ({message or 'compte privé'}) : visible de son seul auteur",
                         "page", rep.status_code)
    if code == 10204:
        return _resultat(url, "tiktok", RETIREE, f"statusCode 10204 ({message})", "page", rep.status_code)
    return _resultat(url, "tiktok", INDETERMINE, f"statusCode {code} ({message}) non classé", "page", rep.status_code)


def _oembed(url: str, plateforme: str, point: str, client: httpx.Client) -> dict:
    rep = client.get(f"{point}?url={quote(url, safe='')}&format=json", headers={"User-Agent": AGENT})
    code = rep.status_code
    if code == 200:
        return _resultat(url, plateforme, EN_LIGNE, "oEmbed 200", "oembed", code)
    if code in (404, 410):
        return _resultat(url, plateforme, RETIREE, f"oEmbed {code}", "oembed", code)
    if code == 400 and plateforme == "youtube":
        return _resultat(url, plateforme, RETIREE, "oEmbed 400 : identifiant que YouTube ne connaît pas", "oembed", code)
    if code == 401:
        return _resultat(url, plateforme, PRIVEE, "oEmbed 401 : vidéo privée ou à accès restreint", "oembed", code)
    if code == 403:
        # YouTube répond 403 quand l'intégration est désactivée : la vidéo existe.
        if plateforme == "youtube":
            return _resultat(url, plateforme, EN_LIGNE, "oEmbed 403 : intégration désactivée, la vidéo existe", "oembed", code)
        return _resultat(url, plateforme, PRIVEE, "oEmbed 403 : contenu protégé", "oembed", code)
    return _resultat(url, plateforme, INDETERMINE, f"oEmbed {code} inattendu", "oembed", code)


def _instagram(url: str, client: httpx.Client) -> dict:
    rep = client.get(url, headers={"User-Agent": AGENT})
    final = str(rep.url).lower()
    texte = rep.text
    if rep.status_code == 404:
        return _resultat(url, "instagram", RETIREE, "HTTP 404", "page", 404)
    if re.search(r'property="og:title" content="[^"]+', texte):
        return _resultat(url, "instagram", EN_LIGNE, "méta-données publiques lisibles", "page", rep.status_code)
    coque = re.search(r"<title[^>]*>\s*Instagram\s*</title>", texte) is not None
    if coque or "/accounts/login" in final or any(m in texte.lower() for m in _MARQUES_CONNEXION_INSTAGRAM):
        return _resultat(
            url, "instagram", MUR_CONNEXION,
            "page de connexion servie à un visiteur anonyme ; on ne peut pas dire si la publication existe encore",
            "page", rep.status_code,
        )
    return _resultat(url, "instagram", INDETERMINE, "page sans méta-données ni mur identifiable", "page", rep.status_code)


def _web(url: str, client: httpx.Client) -> dict:
    rep = client.get(url, headers={"User-Agent": AGENT})
    code = rep.status_code
    final = str(rep.url).lower()
    if any(f in final for f in ("/login", "/signin", "/connexion", "accounts.google.com")) and final != url.lower():
        return _resultat(url, "web", MUR_CONNEXION, "redirigé vers une page de connexion", "page", code)
    if code in (404, 410):
        return _resultat(url, "web", RETIREE, f"HTTP {code}", "page", code)
    if code in (401, 402, 451):
        return _resultat(url, "web", MUR_CONNEXION, f"HTTP {code}", "page", code)
    if code == 403:
        return _resultat(url, "web", MUR_CONNEXION, "HTTP 403 : accès refusé à un visiteur anonyme", "page", code)
    if 200 <= code < 300:
        return _resultat(url, "web", EN_LIGNE, f"HTTP {code}", "page", code)
    return _resultat(url, "web", INDETERMINE, f"HTTP {code} inattendu", "page", code)


def etat_source(url: str, client: httpx.Client | None = None) -> dict:
    """Dit si la source est en ligne, retirée, derrière un mur de connexion ou privée.

    Renvoie `{url, plateforme, etat, raison, methode, http, verifie_le}`. `etat` vaut
    `en_ligne`, `retiree`, `mur_connexion`, `privee` ou `indetermine`. Une panne réseau donne
    `indetermine` : on n'affirme jamais qu'une page a disparu sans l'avoir constaté.
    """
    if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
        raise ValueError(f"URL http(s) attendue, reçu {url!r}.")
    plateforme = plateforme_de(url)
    propre = client is None
    client = client or httpx.Client(timeout=DELAI_S, follow_redirects=True)
    try:
        if plateforme == "tiktok":
            return _tiktok(url, client)
        if plateforme == "youtube":
            return _oembed(url, "youtube", "https://www.youtube.com/oembed", client)
        if plateforme == "x":
            return _oembed(url, "x", "https://publish.x.com/oembed", client)
        if plateforme == "instagram":
            return _instagram(url, client)
        return _web(url, client)
    except httpx.HTTPError as erreur:
        return _resultat(url, plateforme, INDETERMINE, f"réseau : {type(erreur).__name__}", "aucune", None)
    finally:
        if propre:
            client.close()


if __name__ == "__main__":  # python -m enquete.etat_source URL...
    import sys

    for lien in sys.argv[1:]:
        print(json.dumps(etat_source(lien), ensure_ascii=False))
