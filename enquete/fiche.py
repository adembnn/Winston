"""Fiche d'une page : le journaliste colle un lien, l'outil rend ce qu'il faut emporter.

Entrée : un lien TikTok, Instagram, YouTube ou X (twitter.com). Sortie : un dictionnaire
(`fiche`) et une page de texte (`fiche_texte`) avec

- l'identité de la publication (plateforme, identifiant, compte, type) ;
- le lien d'origine reconstruit proprement ;
- la date, tirée de l'identifiant quand la plateforme en porte une (TikTok, Instagram, X ;
  YouTube : non, son identifiant est aléatoire), avec sa marge d'erreur ;
- la fiche oEmbed publique (titre, auteur) quand elle existe, sans clé ;
- l'état de la source (en ligne, retirée, mur de connexion, privée) ;
- la recherche Oriane de la même vidéo par son identifiant de plateforme, si un client est
  fourni (une recherche = 10 crédits au tarif `basic`, gardée en cache ensuite).

Chaque bloc échoue seul : un oEmbed muet ou une panne Oriane laisse le reste de la fiche.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from enquete.etat_source import AGENT, DELAI_S, etat_source, plateforme_de
from enquete.liens import url_origine
from enquete.temps import date_depuis_identifiant

_X_EPOCH_MS = 1288834974657
_RE_TIKTOK = re.compile(r"^/@(?P<pseudo>[^/]+)/(?P<genre>video|photo)/(?P<id>\d+)")
_RE_INSTAGRAM = re.compile(r"^(?:/[^/]+)?/(?P<genre>p|reel|reels|tv)/(?P<id>[A-Za-z0-9_-]+)")
_RE_YOUTUBE_CHEMIN = re.compile(r"^/(?P<genre>shorts|live|embed|v)/(?P<id>[A-Za-z0-9_-]{11})")
_RE_X = re.compile(r"^/(?P<pseudo>[^/]+)/status(?:es)?/(?P<id>\d+)")
_RACCOURCIS_TIKTOK = ("vm.tiktok.com", "vt.tiktok.com")


class LienNonReconnu(ValueError):
    """Le lien n'est pas une publication TikTok, Instagram, YouTube ou X identifiable."""


def _suivre_raccourci(url: str, client: httpx.Client) -> str:
    """Résout un lien court TikTok (vm./vt./tiktok.com/t/) vers l'adresse longue."""
    hote = (urlparse(url).hostname or "").lower()
    if hote in _RACCOURCIS_TIKTOK or (hote.endswith("tiktok.com") and urlparse(url).path.startswith("/t/")):
        rep = client.get(url, headers={"User-Agent": AGENT}, follow_redirects=True)
        return str(rep.url)
    return url


def lire_lien(url: str) -> dict[str, Any]:
    """Découpe un lien en `plateforme`, `identifiant`, `pseudo`, `type`. Aucun réseau."""
    analyse = urlparse(url.strip())
    if analyse.scheme not in ("http", "https") or not analyse.hostname:
        raise LienNonReconnu(f"Lien http(s) attendu, reçu {url!r}.")
    plateforme = plateforme_de(url)
    chemin = analyse.path
    if plateforme == "tiktok":
        m = _RE_TIKTOK.match(chemin)
        if m:
            return {"plateforme": "tiktok", "identifiant": m["id"], "pseudo": m["pseudo"], "type": m["genre"]}
    elif plateforme == "instagram":
        m = _RE_INSTAGRAM.match(chemin)
        if m:
            genre = "reel" if m["genre"] in ("reel", "reels") else "post"
            return {"plateforme": "instagram", "identifiant": m["id"], "pseudo": None, "type": genre}
    elif plateforme == "youtube":
        hote = analyse.hostname.lower()
        if hote == "youtu.be":
            identifiant = chemin.strip("/").split("/")[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", identifiant):
                return {"plateforme": "youtube", "identifiant": identifiant, "pseudo": None, "type": "video"}
        else:
            v = parse_qs(analyse.query).get("v", [""])[0]
            if chemin == "/watch" and re.fullmatch(r"[A-Za-z0-9_-]{11}", v):
                return {"plateforme": "youtube", "identifiant": v, "pseudo": None, "type": "video"}
            m = _RE_YOUTUBE_CHEMIN.match(chemin)
            if m:
                genre = "short" if m["genre"] == "shorts" else "video"
                return {"plateforme": "youtube", "identifiant": m["id"], "pseudo": None, "type": genre}
    elif plateforme == "x":
        m = _RE_X.match(chemin)
        if m:
            return {"plateforme": "x", "identifiant": m["id"],
                    "pseudo": None if m["pseudo"] in ("i", "web") else m["pseudo"], "type": "post"}
    raise LienNonReconnu(f"Pas une publication reconnue (TikTok, Instagram, YouTube, X) : {url!r}.")


def _lien_origine(infos: dict[str, Any]) -> str | None:
    pf, ident = infos["plateforme"], infos["identifiant"]
    try:
        if pf == "x":
            return f"https://x.com/{infos['pseudo'] or 'i/web'}/status/{ident}"
        return url_origine(pf, ident, infos["pseudo"], infos["type"])
    except ValueError:
        return None


def _date(infos: dict[str, Any]) -> dict[str, Any] | None:
    pf, ident = infos["plateforme"], infos["identifiant"]
    if pf == "x":
        instant = datetime.fromtimestamp(((int(ident) >> 22) + _X_EPOCH_MS) / 1000, UTC)
        return {"iso": instant.isoformat(timespec="seconds"), "marge_s": 1,
                "methode": "identifiant X (flocon : millisecondes depuis le 4 nov. 2010 dans les bits de poids fort)"}
    try:
        d = date_depuis_identifiant(pf, ident, infos["type"])
    except ValueError:
        return None  # YouTube : l'identifiant ne porte aucune date
    return {"iso": d.date.isoformat(timespec="seconds"), "marge_s": d.marge_secondes, "methode": d.methode}


def _oembed(url: str, infos: dict[str, Any], client: httpx.Client) -> dict[str, Any]:
    pf = infos["plateforme"]
    points = {"tiktok": "https://www.tiktok.com/oembed", "youtube": "https://www.youtube.com/oembed",
              "x": "https://publish.x.com/oembed"}
    if pf not in points:
        return {"statut": "ignore", "detail": "Instagram exige un jeton Meta pour son oEmbed : non utilisé"}
    try:
        rep = client.get(points[pf], params={"url": url, "format": "json"}, headers={"User-Agent": AGENT})
        rep.raise_for_status()
        j = rep.json()
    except (httpx.HTTPError, ValueError) as erreur:
        return {"statut": "echec", "detail": f"{type(erreur).__name__}"}
    return {"statut": "ok", "titre": j.get("title"), "auteur": j.get("author_name"),
            "auteur_url": j.get("author_url"), "vignette": j.get("thumbnail_url")}


def chercher_dans_oriane(plateforme: str, identifiant: str, oriane: Any) -> dict[str, Any]:
    """Cherche la même publication dans Oriane par `platformId` (projection basic, 10 crédits)."""
    if plateforme not in ("tiktok", "instagram", "youtube"):
        return {"statut": "non_couverte", "detail": "Oriane ne couvre que TikTok, Instagram et YouTube"}
    try:
        # Le filtre `platformId` d'Oriane attend des paires {platform, platformId} (spec OpenAPI).
        rep = oriane.chercher_contenus(
            {"platformId": {"includes": [{"platform": plateforme, "platformId": identifiant}]}},
            limite=5, projection="basic",
        )
    except Exception as erreur:  # noqa: BLE001 - la fiche survit à toute panne Oriane
        return {"statut": "echec", "detail": f"{type(erreur).__name__} : {erreur}"}
    trouves = [
        {"id": r.get("id"), "publie_le": r.get("publishedAt"), "compte": r.get("profileHandle"),
         "vues": r.get("viewsCount"), "legende": (r.get("caption") or "")[:200]}
        for r in ((rep.get("data") or rep).get("results") or [])
        if str(r.get("platformId")) == identifiant
    ]
    return {"statut": "trouvee" if trouves else "absente", "resultats": trouves}


def fiche(url: str, oriane: Any = None, client: httpx.Client | None = None) -> dict[str, Any]:
    """Fiche d'une page à partir d'un lien collé. `oriane` : un `OrianeClient` (facultatif)."""
    propre = client is None
    client = client or httpx.Client(timeout=DELAI_S, follow_redirects=True)
    try:
        try:
            longue = _suivre_raccourci(url.strip(), client)
        except httpx.HTTPError:
            longue = url.strip()
        infos = lire_lien(longue)
        origine = _lien_origine(infos)
        cible = origine or longue
        return {
            "lien_colle": url.strip(),
            **infos,
            "lien_origine": origine,
            "date": _date(infos),
            "oembed": _oembed(cible, infos, client),
            "etat_source": etat_source(cible, client),
            "oriane": (chercher_dans_oriane(infos["plateforme"], infos["identifiant"], oriane)
                       if oriane is not None else {"statut": "non_demandee"}),
            "etablie_le": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    finally:
        if propre:
            client.close()


_ETATS = {"en_ligne": "en ligne", "retiree": "retirée", "mur_connexion": "derrière un mur de connexion",
          "privee": "privée", "indetermine": "indéterminé"}


def fiche_texte(f: dict[str, Any]) -> str:
    """La fiche en une page de texte à emporter (Markdown simple)."""
    lignes = [f"# Fiche : {f['plateforme']} {f['identifiant']}", ""]
    lignes.append(f"- Lien d'origine : {f['lien_origine'] or 'non reconstructible'}")
    if f.get("pseudo"):
        lignes.append(f"- Compte : @{f['pseudo']}")
    d = f.get("date")
    if d:
        marge = f" (± {d['marge_s']} s)" if d.get("marge_s") is not None else ""
        lignes.append(f"- Date de publication : {d['iso']}{marge}, {d['methode']}")
    else:
        lignes.append("- Date de publication : non lisible dans l'identifiant (YouTube : voir la page)")
    e = f["etat_source"]
    lignes.append(f"- État de la source : {_ETATS.get(e['etat'], e['etat'])} ({e['raison']}), vérifié le {e['verifie_le']}")
    o = f["oembed"]
    if o["statut"] == "ok" and not (o.get("titre") or o.get("auteur")):
        lignes.append("- oEmbed : répond, sans titre ni auteur (TikTok ne les fournit plus)")
    elif o["statut"] == "ok":
        lignes.append(f"- oEmbed : « {o.get('titre') or '(sans titre)'} », par {o.get('auteur')}")
    else:
        lignes.append(f"- oEmbed : {o['statut']} ({o.get('detail', '')})")
    r = f["oriane"]
    if r["statut"] == "trouvee":
        un = r["resultats"][0]
        lignes.append(f"- Oriane : trouvée (id {un['id']}, publiée le {un['publie_le']}, {un['vues']} vues)")
    else:
        lignes.append(f"- Oriane : {r['statut']}" + (f" ({r['detail']})" if r.get("detail") else ""))
    lignes += ["", f"Établie le {f['etablie_le']}. Les états de source décrivent ce que voit un visiteur anonyme."]
    return "\n".join(lignes)


if __name__ == "__main__":  # python -m enquete.fiche LIEN [--oriane]
    import sys

    from enquete.sources.oriane import OrianeClient

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cli = OrianeClient() if "--oriane" in sys.argv else None
    print(fiche_texte(fiche(args[0], oriane=cli)))
