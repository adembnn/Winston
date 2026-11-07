"""Reddit : lire les fils et les commentaires des subreddits, sans compte.

Voies (testées le 7 nov. 2026) :
- arctic-shift (`arctic-shift.photon-reddit.com/api/{posts,comments}/search`) : archive publique
  des posts (champ `title`) et des commentaires (champ `body`), avec `after` et `before` pour la
  fenêtre et le gel. Le service répond parfois 422 « Timeout » : on attend 20 s et on réessaie,
  deux fois ;
- flux RSS `reddit.com/r/<subreddit>/search.rss` : les dernières heures, que l'archive n'a pas
  encore. Seulement sans gel ni `jusqua` ;
- `/api/posts/ids` et `/api/comments/ids` : relire un contenu par son identifiant.

Aucun compte, aucun cookie, aucun jeton.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx

PLATEFORME = "reddit"
CAPACITES = {"chercher": True, "lire": True}
ANGLE_MORT = "Reddit : recherche dans les subreddits listés ; un contenu supprimé peut manquer."

AGENT = "enquete-osint/0.1 (recherche journalistique, lectures publiques)"
URL_ARCTIC = "https://arctic-shift.photon-reddit.com"
URL_REDDIT = "https://www.reddit.com"
PAUSE_HOTE_S = 1.0
PAUSE_ARCTIC_S = 4.0  # observé : au-delà de quelques appels rapprochés, 422 « Timeout » puis 429
ATTENTE_S = 20.0
ESSAIS_422 = 3  # un appel, puis deux réessais après 20 s
PAGE_MAX = 100
PAGES_MAX = 5
LIMITE_EXTRAIT = 500
SUPPRIME = {"[deleted]", "[removed]"}

FiltreUrl = Callable[[str], bool]
DateBorne = date | datetime | str | None

_ATOM = "{http://www.w3.org/2005/Atom}"
_RE_ID = re.compile(r"^t([13])_([a-z0-9]+)$", re.IGNORECASE)
_RE_COMMENTAIRE = re.compile(r"/comments/([a-z0-9]+)(?:/[^/?#]*)?(?:/([a-z0-9]+))?", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------

class RedditError(Exception):
    def __init__(self, message: str, statut: int | None = None):
        super().__init__(message)
        self.message = message
        self.statut = statut


class RedditBloqueError(RedditError):
    """403, page anti-robot : constaté, jamais contourné."""


class RedditLimiteError(RedditError):
    """429 après réessai."""


class RedditIntrouvableError(RedditError):
    """404, contenu absent de l'archive ou supprimé."""


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def _en_datetime(valeur: DateBorne, fin_de_jour: bool = False) -> datetime | None:
    """Une date AAAA-MM-JJ vaut 00:00 UTC, ou 23:59:59 UTC si `fin_de_jour`."""
    if valeur is None or valeur == "":
        return None
    if isinstance(valeur, datetime):
        return valeur.astimezone(UTC) if valeur.tzinfo else valeur.replace(tzinfo=UTC)
    if isinstance(valeur, date):
        heure = (23, 59, 59) if fin_de_jour else (0, 0, 0)
        return datetime(valeur.year, valeur.month, valeur.day, *heure, tzinfo=UTC)
    texte = str(valeur).strip().replace("Z", "+00:00")
    try:
        if "T" in texte or " " in texte:
            return _en_datetime(datetime.fromisoformat(texte), fin_de_jour)
        return _en_datetime(date.fromisoformat(texte), fin_de_jour)
    except ValueError:
        raise ValueError(f"Date illisible : {valeur!r}") from None


def _iso_arctic(quand: datetime) -> str:
    return quand.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


class _Texte(HTMLParser):
    """Texte brut du bloc `<div class="md">` d'une entrée RSS."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._prof = 0
        self.morceaux: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div" and (self._prof or "md" in (dict(attrs).get("class") or "").split()):
            self._prof += 1
        elif tag in ("br", "p") and self._prof:
            self.morceaux.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._prof:
            self._prof -= 1
        elif tag == "p" and self._prof:
            self.morceaux.append("\n")

    def handle_data(self, data: str) -> None:
        if self._prof:
            self.morceaux.append(data)


def _texte_du_flux(html: str) -> str:
    p = _Texte()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(p.morceaux)).strip()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ReseauReddit:
    """Lectures publiques de Reddit, avec cache disque et garde-fous du banc."""

    def __init__(
        self,
        dossier_cache: str | Path = "cache/reseaux/reddit",
        transport: httpx.BaseTransport | None = None,
        filtre_url: FiltreUrl | None = None,
        date_gel: DateBorne = None,
        maintenant: Callable[[], datetime] = lambda: datetime.now(UTC),
        dormir: Callable[[float], None] = time.sleep,
        duree_cache: float = 86400,
    ):
        self._dossier = Path(dossier_cache)
        self._http = httpx.Client(
            transport=transport, timeout=90.0, follow_redirects=True, headers={"User-Agent": AGENT}
        )
        self.filtre_url = filtre_url
        self.date_gel = _en_datetime(date_gel, fin_de_jour=True)
        self._maintenant = maintenant
        self._dormir = dormir
        self._duree_cache = duree_cache
        self._dernier_appel: dict[str, datetime] = {}
        self.journal: list[dict[str, str]] = []
        self.appels_reseau = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ReseauReddit:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ garde-fous

    def _noter(self, url: str, raison: str) -> None:
        self.journal.append({"url": url, "raison": raison, "date": self._maintenant().isoformat()})

    def _autorise(self, url: str) -> bool:
        if self.filtre_url is not None and not self.filtre_url(url):
            self._noter(url, "filtre_url")
            return False
        return True

    # ------------------------------------------------------------------ cache

    def _chemin(self, url: str, params: dict[str, str]) -> Path:
        cle = json.dumps(["GET", url, sorted(params.items())], ensure_ascii=False)
        return self._dossier / f"{hashlib.sha256(cle.encode('utf-8')).hexdigest()}.json"

    def _depuis_cache(self, chemin: Path) -> dict[str, Any] | None:
        try:
            entree = json.loads(chemin.read_text("utf-8"))
            lue = datetime.fromisoformat(entree["date_lecture"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if (self._maintenant() - lue).total_seconds() > self._duree_cache:
            return None
        return entree if isinstance(entree, dict) else None

    def _ecrire(self, chemin: Path, entree: dict[str, Any]) -> None:
        self._dossier.mkdir(parents=True, exist_ok=True)
        temporaire = chemin.with_suffix(".tmp")
        temporaire.write_text(json.dumps(entree, ensure_ascii=False), encoding="utf-8")
        temporaire.replace(chemin)

    # ---------------------------------------------------------------- réseau

    def _attendre(self, hote: str) -> None:
        dernier = self._dernier_appel.get(hote)
        if dernier is not None:
            pause = PAUSE_ARCTIC_S if hote.startswith("arctic-shift") else PAUSE_HOTE_S
            reste = pause - (self._maintenant() - dernier).total_seconds()
            if reste > 0:
                self._dormir(reste)
        self._dernier_appel[hote] = self._maintenant()

    def _lire_url(self, url: str, params: dict[str, str]) -> dict[str, Any] | None:
        """Cache d'abord, réseau ensuite. None si le filtre refuse (ni réseau ni cache)."""
        complete = url + ("?" + urlencode(params, quote_via=quote) if params else "")
        if not self._autorise(complete):
            return None
        chemin = self._chemin(url, params)
        en_cache = self._depuis_cache(chemin)
        if en_cache is not None:
            return en_cache
        hote = urlsplit(url).hostname or ""
        essais_422 = 0
        deja_429 = False
        while True:
            self._attendre(hote)
            self.appels_reseau += 1
            try:
                rep = self._http.get(url, params=params)
            except httpx.HTTPError as exc:
                raise RedditError(f"injoignable ({type(exc).__name__}) : {complete}") from exc
            if rep.status_code == 422 and "timeout" in rep.text.lower():
                essais_422 += 1
                if essais_422 < ESSAIS_422:
                    self._dormir(ATTENTE_S)
                    continue
                raise RedditError(f"422 Timeout après {ESSAIS_422} essais : {complete}", 422)
            if rep.status_code == 429:
                if not deja_429:
                    deja_429 = True
                    try:
                        attente = float(rep.headers.get("Retry-After", ""))
                    except ValueError:
                        attente = ATTENTE_S
                    self._dormir(attente)
                    continue
                raise RedditLimiteError(f"429 après réessai : {complete}", 429)
            break
        entree = {
            "url": complete,
            "statut": rep.status_code,
            "corps": rep.text if rep.status_code == 200 else "",
            "date_lecture": self._maintenant().isoformat(),
        }
        if rep.status_code in (200, 403, 404):
            self._ecrire(chemin, entree)
        return entree

    @staticmethod
    def _corps_json(entree: dict[str, Any]) -> list[dict[str, Any]]:
        statut = entree["statut"]
        if statut == 403:
            raise RedditBloqueError(f"403 : {entree['url']}", statut)
        if statut == 404:
            raise RedditIntrouvableError(f"introuvable : {entree['url']}", statut)
        if statut != 200:
            raise RedditError(f"HTTP {statut} : {entree['url']}", statut)
        try:
            donnees = json.loads(entree["corps"]).get("data")
        except (ValueError, AttributeError) as exc:
            raise RedditError(f"réponse illisible : {entree['url']}", statut) from exc
        return [d for d in donnees or [] if isinstance(d, dict)]

    # ------------------------------------------------------------------ fiches

    def _metriques(self, **valeurs: Any) -> dict[str, int]:
        if self.date_gel is not None:
            return {}  # lues aujourd'hui, pas au jour du gel
        return {k: int(v) for k, v in valeurs.items() if isinstance(v, int | float)}

    @staticmethod
    def _auteur(brut: Any) -> str | None:
        if not brut or brut in SUPPRIME:
            return None
        return str(brut).removeprefix("/u/")

    def _fiche_arctic(self, d: dict[str, Any], entree: dict[str, Any]) -> dict[str, Any] | None:
        nom = str(d.get("name") or "")
        if not _RE_ID.match(nom):
            ident = str(d.get("id") or "")
            if not ident:
                return None
            nom = ("t1_" if "body" in d else "t3_") + ident
        commentaire = nom.startswith("t1_")
        cree = d.get("created_utc")
        quand = datetime.fromtimestamp(float(cree), UTC) if isinstance(cree, int | float) else None
        permalink = d.get("permalink") or ""
        url = URL_REDDIT + permalink if permalink.startswith("/") else (
            f"{URL_REDDIT}/comments/{nom[3:]}/")
        medias: list[dict[str, str]] = []
        if commentaire:
            texte = str(d.get("body") or "")
            metriques = self._metriques(score=d.get("score"))
        else:
            titre, corps = str(d.get("title") or ""), str(d.get("selftext") or "")
            texte = titre if not corps or corps in SUPPRIME else f"{titre}\n\n{corps}"
            metriques = self._metriques(score=d.get("score"), commentaires=d.get("num_comments"))
            lien = d.get("url") or ""
            if lien and not d.get("is_self") and "/comments/" not in lien:
                kind = ("video" if d.get("is_video") or "v.redd.it" in lien
                        else "image" if re.search(r"\.(jpe?g|png|gif|webp)(\?|$)", lien) or "i.redd.it" in lien
                        else "lien")
                medias.append({"type": kind, "url": lien})
        return {
            "plateforme": PLATEFORME,
            "id": nom,
            "url": url,
            "auteur": self._auteur(d.get("author")),
            "date": quand.isoformat() if quand else None,
            "texte": texte,
            "langue": None,
            "medias": medias,
            "metriques": metriques,
            "route": "arctic-shift",
            "recupere_le": entree["date_lecture"],
            "preuve": {"url": entree["url"], "extrait": texte[:LIMITE_EXTRAIT]},
        }

    def _fiches_rss(self, entree: dict[str, Any]) -> list[dict[str, Any]]:
        statut = entree["statut"]
        if statut == 403:
            raise RedditBloqueError(f"403 : {entree['url']}", statut)
        if statut == 404:
            raise RedditIntrouvableError(f"introuvable : {entree['url']}", statut)
        if statut != 200:
            raise RedditError(f"HTTP {statut} : {entree['url']}", statut)
        try:
            racine = ET.fromstring(entree["corps"])
        except ET.ParseError as exc:
            raise RedditError(f"flux illisible : {entree['url']}", statut) from exc
        fiches = []
        for e in racine.iter(_ATOM + "entry"):
            ident = (e.findtext(_ATOM + "id") or "").strip()
            lien = e.find(_ATOM + "link")
            if not _RE_ID.match(ident) or lien is None:
                continue
            titre = (e.findtext(_ATOM + "title") or "").strip()
            corps = _texte_du_flux(e.findtext(_ATOM + "content") or "")
            texte = f"{titre}\n\n{corps}" if corps and corps != titre else titre
            quand = _en_datetime(e.findtext(_ATOM + "published") or e.findtext(_ATOM + "updated"))
            fiches.append({
                "plateforme": PLATEFORME,
                "id": ident,
                "url": lien.get("href") or "",
                "auteur": self._auteur(e.findtext(f"{_ATOM}author/{_ATOM}name")),
                "date": quand.isoformat() if quand else None,
                "texte": texte,
                "langue": None,
                "medias": [],
                "metriques": {},
                "route": "rss",
                "recupere_le": entree["date_lecture"],
                "preuve": {"url": entree["url"], "extrait": titre[:LIMITE_EXTRAIT]},
            })
        return fiches

    # ---------------------------------------------------------------- chercher

    def chercher(
        self,
        requete: str,
        depuis: DateBorne = None,
        jusqua: DateBorne = None,
        limite: int = 50,
        subreddits: tuple[str, ...] | list[str] | None = ("france",),
        auteur: str | None = None,
    ) -> list[dict[str, Any]]:
        """Posts et commentaires qui contiennent la requête, du plus ancien au plus récent.

        `subreddits` vide ou None : tous les subreddits (utile avec `auteur`).
        """
        debut = _en_datetime(depuis)
        fin = _en_datetime(jusqua, fin_de_jour=True)
        hautes = [d for d in (fin, self.date_gel) if d is not None]
        borne = min(hautes) if hautes else None
        retenues: dict[str, dict[str, Any]] = {}
        etat = {"premiere": True}

        def accepter(f: dict[str, Any]) -> None:
            quand = _en_datetime(f["date"]) if f["date"] else None
            if self.date_gel is not None and quand is None:
                self._noter(f["url"], "sans_date_sous_gel")
            elif not self._autorise(f["url"]):
                pass
            elif quand is not None and borne is not None and quand > borne:
                apres_gel = self.date_gel is not None and quand > self.date_gel
                self._noter(f["url"], "apres_gel" if apres_gel else "hors_fenetre")
            elif quand is not None and debut is not None and quand < debut:
                self._noter(f["url"], "hors_fenetre")
            else:
                retenues.setdefault(f["id"], f)

        def tenter(action: Callable[[], None], url: str) -> None:
            """Exécute une lecture ; seule la toute première requête peut lever."""
            premiere, etat["premiere"] = etat["premiere"], False
            try:
                action()
            except RedditError as exc:
                if premiere:
                    raise
                raison = ("mur" if isinstance(exc, RedditBloqueError)
                          else "limite" if isinstance(exc, RedditLimiteError) else "introuvable")
                self._noter(url, raison)

        cibles = list(subreddits) if subreddits else [None]
        for sub in cibles:
            for genre, champ in (("posts", "title"), ("comments", "body")):
                url = f"{URL_ARCTIC}/api/{genre}/search"

                def lire_genre(sub: str | None = sub, genre: str = genre, champ: str = champ,
                               url: str = url) -> None:
                    avant = borne
                    for _ in range(PAGES_MAX):
                        params = {"limit": str(min(PAGE_MAX, max(limite, 1))), "sort": "desc"}
                        if requete:
                            params[champ] = requete
                        if sub:
                            params["subreddit"] = sub
                        if auteur:
                            params["author"] = auteur
                        if debut is not None:
                            params["after"] = _iso_arctic(debut)
                        if avant is not None:
                            params["before"] = _iso_arctic(avant)
                        entree = self._lire_url(url, params)
                        if entree is None:
                            return
                        brutes = self._corps_json(entree)
                        fiches = [f for f in (self._fiche_arctic(d, entree) for d in brutes) if f]
                        for f in fiches:
                            accepter(f)
                        datees = [_en_datetime(f["date"]) for f in fiches if f["date"]]
                        if len(brutes) < int(params["limit"]) or not datees or len(retenues) >= limite:
                            return
                        plus_ancien = min(d for d in datees if d)
                        if avant is not None and plus_ancien >= avant:
                            return
                        avant = plus_ancien

                tenter(lire_genre, url)

        if requete and not auteur and borne is None:
            for sub in cibles:
                if not sub:
                    continue

                def lire_flux(sub: str = sub) -> None:
                    entree = self._lire_url(
                        f"{URL_REDDIT}/r/{sub}/search.rss",
                        {"q": requete, "restrict_sr": "on", "sort": "new"},
                    )
                    if entree is None:
                        return
                    for f in self._fiches_rss(entree):
                        accepter(f)

                tenter(lire_flux, f"{URL_REDDIT}/r/{sub}/search.rss")

        resultat = sorted(retenues.values(), key=lambda f: (f["date"] or "", f["id"]))
        if len(resultat) > limite:
            self._noter(URL_ARCTIC, "limite")
            resultat = resultat[-limite:]
        return resultat

    # -------------------------------------------------------------------- lire

    @staticmethod
    def _identifiant(url: str) -> str:
        """`t3_<id>` ou `t1_<id>` d'un lien Reddit, d'un lien court redd.it ou d'un identifiant."""
        brut = url.strip()
        m = _RE_ID.match(brut)
        if m:
            return f"t{m.group(1)}_{m.group(2).lower()}"
        hote = (urlsplit(brut).hostname or "").lower()
        chemin = urlsplit(brut).path
        if hote == "redd.it" and re.fullmatch(r"/[a-z0-9]+", chemin, re.IGNORECASE):
            return "t3_" + chemin[1:].lower()
        m = _RE_COMMENTAIRE.search(chemin)
        if hote.endswith("reddit.com") and m:
            return f"t1_{m.group(2).lower()}" if m.group(2) else f"t3_{m.group(1).lower()}"
        raise RedditError(f"lien Reddit illisible : {url}")

    def lire(self, url: str) -> dict[str, Any]:
        """Un post ou un commentaire, par son lien ou son identifiant."""
        nom = self._identifiant(url)
        genre = "posts" if nom.startswith("t3_") else "comments"
        entree = self._lire_url(f"{URL_ARCTIC}/api/{genre}/ids", {"ids": nom})
        if entree is None:
            raise RedditError(f"refusé par filtre_url : {url}")
        brutes = self._corps_json(entree)
        if not brutes:
            raise RedditIntrouvableError(f"absent de l'archive : {url}", 404)
        fiche = self._fiche_arctic(brutes[0], entree)
        if fiche is None:
            raise RedditIntrouvableError(f"réponse sans identifiant : {url}", 404)
        if not self._autorise(fiche["url"]):
            raise RedditError(f"refusé par filtre_url : {fiche['url']}")
        quand = _en_datetime(fiche["date"]) if fiche["date"] else None
        if self.date_gel is not None:
            if quand is None:
                self._noter(fiche["url"], "sans_date_sous_gel")
                raise RedditIntrouvableError(f"contenu sans date sous gel : {url}")
            if quand > self.date_gel:
                self._noter(fiche["url"], "apres_gel")
                raise RedditIntrouvableError(f"contenu postérieur à la date de gel : {url}")
        if not fiche["texte"]:
            fiche["preuve"]["extrait"] = "(contenu sans texte)"
        return fiche


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def principal(argv: list[str] | None = None) -> int:
    for flux in (sys.stdout, sys.stderr):
        if hasattr(flux, "reconfigure"):
            flux.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m enquete.sources.reseau_reddit", description=ANGLE_MORT)
    sous = ap.add_subparsers(dest="commande", required=True)
    c = sous.add_parser("chercher")
    c.add_argument("requete")
    c.add_argument("--depuis")
    c.add_argument("--jusqua")
    c.add_argument("--gel")
    c.add_argument("--limite", type=int, default=50)
    c.add_argument("--subreddits", default="france", help="séparés par des virgules ; vide = tous")
    c.add_argument("--auteur")
    c.add_argument("--cache", default="cache/reseaux/reddit")
    li = sous.add_parser("lire")
    li.add_argument("url")
    li.add_argument("--gel")
    li.add_argument("--cache", default="cache/reseaux/reddit")
    a = ap.parse_args(argv)
    with ReseauReddit(dossier_cache=a.cache, date_gel=a.gel) as r:
        try:
            if a.commande == "chercher":
                subs = tuple(x for x in a.subreddits.split(",") if x)
                fiches = r.chercher(a.requete, a.depuis, a.jusqua, a.limite, subreddits=subs, auteur=a.auteur)
            else:
                fiches = [r.lire(a.url)]
        except RedditError as exc:
            print(f"{type(exc).__name__}: {exc.message}", file=sys.stderr)
            return 1
        finally:
            for ligne in r.journal:
                print(json.dumps(ligne, ensure_ascii=False), file=sys.stderr)
            print(f"appels_reseau={r.appels_reseau}", file=sys.stderr)
        for f in fiches:
            print(json.dumps(f, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(principal())
