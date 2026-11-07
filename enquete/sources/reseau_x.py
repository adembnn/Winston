"""X (ex-Twitter) sans compte : recherche par Brave, lecture par la syndication.

Voies, testées en réel le 7 nov. 2026, sans compte, cookie ni jeton :
- recherche : la page de résultats `search.brave.com` pour `site:x.com <requête>`, qui donne
  des liens `x.com/<compte>/status/<id>` (15 à 20 par page, sans tri ni date) ;
- date : tirée de l'identifiant du post (« snowflake »), `(id >> 22) + 1288834974657` ms. Elle est
  connue AVANT toute lecture : un post hors fenêtre ou postérieur au gel n'est jamais lu ;
- lecture : `cdn.syndication.twimg.com/tweet-result` (texte, date, auteur, likes, réponses,
  médias) ; secours `publish.x.com/oembed` (texte et jour seulement).

Limites connues :
- Brave n'indexe qu'une partie de X et peut répondre 429 (limite ou contrôle anti-robot) : le
  module attend (`Retry-After`, sinon 30 s) puis réessaie une fois, puis s'arrête et le
  journalise. Il ne résout jamais de captcha et ne contourne aucun blocage ;
- la syndication ne donne ni les réponses ni les vues ; le nombre de likes y est parfois à 0 ;
- sous gel, `metriques` vaut `{}` : elles sont lues aujourd'hui, pas au jour du gel ;
- la vidéo ne se télécharge pas ici : elle s'archive par `archive.py`.

Une voie qui exigerait un compte ou une clé n'est pas utilisée : l'API officielle de X est payante
à l'usage (0,005 $ par post lu), non branchée.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

PLATEFORME = "x"
CAPACITES = {"chercher": True, "lire": True}
ANGLE_MORT = (
    "X : la recherche passe par un moteur web qui n'en voit qu'une partie ; "
    "un post se lit par son lien, sans les réponses ni le nombre de vues."
)

BRAVE = "https://search.brave.com/search"
SYNDICATION = "https://cdn.syndication.twimg.com/tweet-result"
OEMBED = "https://publish.x.com/oembed"

AGENT = "enquete-osint/0.1 (recherche journalistique, lectures publiques)"
AGENT_NAVIGATEUR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

EPOCH_X_MS = 1288834974657  # 4 nov. 2010 : début des identifiants datés
PREMIER_ID_DATE = 29700859247  # les identifiants plus petits n'étaient pas datés
INTERVALLE_BRAVE = 4.0
INTERVALLE_SYNDICATION = 1.0
ATTENTE_429 = 30.0
ATTENTE_429_MAX = 120.0

_RE_LIEN = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{6,25})", re.I
)
_RE_URL_POST = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{6,25})(?:[/?#].*)?$", re.I
)
_MOIS = {
    m: i + 1
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"]
    )
}


class XError(Exception):
    def __init__(self, message: str, statut: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.statut = statut


class XBloqueError(XError):
    """403, page anti-robot, mur de connexion : constaté, jamais contourné."""


class XLimiteError(XError):
    """429 après réessai."""


class XIntrouvableError(XError):
    """404, post supprimé, protégé ou sans contenu."""


class _Ecarte(XIntrouvableError):
    """Post écarté (fenêtre, gel) : déjà journalisé avec sa raison."""


# --- dates -----------------------------------------------------------------------------


def date_depuis_id(identifiant: str | int) -> datetime | None:
    """Date de création tirée de l'identifiant (à la milliseconde), None si l'identifiant est trop ancien."""
    try:
        n = int(identifiant)
    except (TypeError, ValueError):
        return None
    if n < PREMIER_ID_DATE:
        return None
    return datetime.fromtimestamp(((n >> 22) + EPOCH_X_MS) / 1000, UTC)


def _en_datetime(valeur: Any, fin_de_jour: bool = False) -> datetime | None:
    """Date (texte, date ou datetime) vers un datetime UTC. Un jour seul vaut 00:00, ou 23:59:59 si `fin_de_jour`."""
    if valeur is None or valeur == "":
        return None
    if isinstance(valeur, datetime):
        return valeur.replace(tzinfo=UTC) if valeur.tzinfo is None else valeur.astimezone(UTC)
    if isinstance(valeur, date):
        jour = datetime(valeur.year, valeur.month, valeur.day, tzinfo=UTC)
        return jour + timedelta(hours=23, minutes=59, seconds=59) if fin_de_jour else jour
    texte = str(valeur).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", texte):
        return _en_datetime(date.fromisoformat(texte), fin_de_jour)
    return _en_datetime(datetime.fromisoformat(texte.replace("Z", "+00:00")))


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).replace(microsecond=0).isoformat()


def jeton_depuis_id(identifiant: str | int) -> str:
    """Jeton que calcule le widget officiel : (id / 1e15 * pi) en base 36, sans zéros ni point."""
    x = (int(identifiant) / 1e15) * math.pi
    chiffres = "0123456789abcdefghijklmnopqrstuvwxyz"
    entier = int(x)
    frac = x - entier
    s = ""
    while entier:
        s = chiffres[entier % 36] + s
        entier //= 36
    s = s or "0"
    f = ""
    for _ in range(11):
        frac *= 36
        d = int(frac)
        f += chiffres[d]
        frac -= d
    return (s + f).replace("0", "")


def identifiant_depuis_url(url: str) -> tuple[str, str] | None:
    m = _RE_URL_POST.match(url.strip())
    return (m.group(1), m.group(2)) if m else None


# --- analyse de l'oEmbed -----------------------------------------------------------------


class _Blockquote(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.dans_p = False
        self.texte: list[str] = []
        self.langue: str | None = None
        self.liens: list[str] = []
        self._dans_a = False
        self._a_courant: list[str] = []
        self.derniere_ancre = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "p" and not self.texte and not self.dans_p:
            self.dans_p = True
            self.langue = a.get("lang")
        elif tag == "br" and self.dans_p:
            self.texte.append("\n")
        elif tag == "a":
            self._dans_a = True
            self._a_courant = []
            if self.dans_p and a.get("href"):
                self.liens.append(a["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "p":
            self.dans_p = False
        elif tag == "a":
            self._dans_a = False
            self.derniere_ancre = "".join(self._a_courant).strip()

    def handle_data(self, data: str) -> None:
        if self.dans_p:
            self.texte.append(data)
        if self._dans_a:
            self._a_courant.append(data)


def _jour_oembed(ancre: str) -> str | None:
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", ancre.strip())
    if not m or m.group(1).lower() not in _MOIS:
        return None
    try:
        return date(int(m.group(3)), _MOIS[m.group(1).lower()], int(m.group(2))).isoformat()
    except ValueError:
        return None


# --- adaptateur --------------------------------------------------------------------------


class ReseauX:
    def __init__(
        self,
        dossier_cache: str | Path = "cache/reseaux/x",
        transport: httpx.BaseTransport | None = None,
        filtre_url: Callable[[str], bool] | None = None,
        date_gel: Any = None,
        maintenant: Callable[[], datetime] = lambda: datetime.now(UTC),
        dormir: Callable[[float], None] = time.sleep,
        duree_cache: float = 86400,
    ) -> None:
        self.dossier_cache = Path(dossier_cache)
        self.filtre_url = filtre_url
        self.date_gel = _en_datetime(date_gel, fin_de_jour=True)
        self._maintenant = maintenant
        self._dormir = dormir
        self.duree_cache = duree_cache
        self.journal: list[dict[str, str]] = []
        self.appels_reseau = 0
        self._dernier_appel: dict[str, float] = {}
        self._client = httpx.Client(transport=transport, timeout=30.0, follow_redirects=True)

    # -- outils ----------------------------------------------------------------------------

    def _noter(self, url: str, raison: str) -> None:
        self.journal.append({"url": url, "raison": raison, "date": self._maintenant().isoformat()})

    def _autorise(self, url: str) -> bool:
        if self.filtre_url is not None and not self.filtre_url(url):
            self._noter(url, "filtre_url")
            return False
        return True

    def _chemin_cache(self, methode: str, url: str, params: Any) -> Path:
        cle = json.dumps([methode, url, params], sort_keys=True, ensure_ascii=False)
        return self.dossier_cache / (hashlib.sha256(cle.encode("utf-8")).hexdigest() + ".json")

    def _lire_cache(self, chemin: Path) -> dict[str, Any] | None:
        try:
            entree = json.loads(chemin.read_text("utf-8"))
            lu = datetime.fromisoformat(entree["date_lecture"])
        except (OSError, ValueError, KeyError):
            return None
        if (self._maintenant() - lu).total_seconds() > self.duree_cache:
            return None
        return entree

    def _ecrire_cache(self, chemin: Path, entree: dict[str, Any]) -> None:
        try:
            chemin.parent.mkdir(parents=True, exist_ok=True)
            tmp = chemin.with_suffix(".tmp")
            tmp.write_text(json.dumps(entree, ensure_ascii=False), "utf-8")
            tmp.replace(chemin)
        except OSError:
            pass

    def _patienter(self, hote: str, intervalle: float) -> None:
        dernier = self._dernier_appel.get(hote)
        if dernier is not None:
            reste = intervalle - (time.monotonic() - dernier)
            if reste > 0:
                self._dormir(reste)
        self._dernier_appel[hote] = time.monotonic()

    def _get(
        self, url: str, params: dict[str, str] | None, intervalle: float, navigateur: bool = False
    ) -> tuple[int, str, str, str]:
        """Rend (statut, corps, date_lecture, url_demandee). Met en cache les 200 et les 404."""
        complete = url + ("?" + urlencode(params, quote_via=quote) if params else "")
        chemin = self._chemin_cache("GET", url, params)
        entree = self._lire_cache(chemin)
        if entree is not None:
            return entree["statut"], entree["corps"], entree["date_lecture"], complete
        entetes = {"User-Agent": AGENT_NAVIGATEUR if navigateur else AGENT, "Accept-Language": "fr-FR,fr;q=0.9"}
        hote = urlparse(url).netloc
        attente_faite = False
        while True:
            self._patienter(hote, intervalle)
            self.appels_reseau += 1
            try:
                rep = self._client.get(url, params=params, headers=entetes)
            except httpx.HTTPError as exc:
                raise XError(f"réseau : {exc.__class__.__name__}") from exc
            if rep.status_code == 429:
                if attente_faite:
                    self._noter(complete, "limite")
                    raise XLimiteError("trop de requêtes (429) après réessai", 429)
                try:
                    attente = float(rep.headers.get("Retry-After", ""))
                except ValueError:
                    attente = ATTENTE_429
                self._dormir(min(max(attente, 0.0), ATTENTE_429_MAX))
                attente_faite = True
                continue
            break
        if rep.status_code == 403:
            self._noter(complete, "mur")
            raise XBloqueError("accès refusé (403) : blocage constaté, non contourné", 403)
        if rep.status_code not in (200, 404):
            raise XError(f"réponse inattendue (HTTP {rep.status_code})", rep.status_code)
        lecture = self._maintenant().isoformat()
        self._ecrire_cache(chemin, {"statut": rep.status_code, "corps": rep.text, "date_lecture": lecture})
        return rep.status_code, rep.text, lecture, complete

    def _fenetre(self, depuis: Any, jusqua: Any) -> tuple[datetime | None, datetime | None]:
        bas = _en_datetime(depuis)
        haut = _en_datetime(jusqua, fin_de_jour=True)
        if self.date_gel is not None and (haut is None or self.date_gel < haut):
            haut = self.date_gel
        return bas, haut

    def _hors_fenetre(self, d: datetime | None, bas: datetime | None, haut: datetime | None) -> str | None:
        """Raison du rejet d'une date, ou None si elle passe."""
        if d is None:
            return "sans_date_sous_gel" if self.date_gel is not None else None
        if self.date_gel is not None and d > self.date_gel:
            return "apres_gel"
        if (bas is not None and d < bas) or (haut is not None and d > haut):
            return "hors_fenetre"
        return None

    # -- chercher --------------------------------------------------------------------------

    def chercher(
        self,
        requete: str,
        depuis: Any = None,
        jusqua: Any = None,
        limite: int = 50,
        compte: str | None = None,
        pages: int = 3,
    ) -> list[dict[str, Any]]:
        bas, haut = self._fenetre(depuis, jusqua)
        compte = compte.lstrip("@") if compte else None
        site = f"site:x.com/{compte}" if compte else "site:x.com"
        q = f"{site} {requete}".strip()
        recent = (
            self.date_gel is None
            and bas is not None
            and (self._maintenant() - bas) < timedelta(days=7)
        )
        candidats: dict[str, tuple[str, str]] = {}
        for k in range(max(pages, 1)):
            params = {"q": q, "source": "web", "offset": str(k)}
            if recent:
                params["tf"] = "pw"
            url_page = BRAVE + "?" + urlencode(params, quote_via=quote)
            if not self._autorise(url_page):
                break
            try:
                statut, corps, _, _ = self._get(BRAVE, params, INTERVALLE_BRAVE, navigateur=True)
            except XError:
                if k == 0:
                    raise
                break
            if statut != 200:
                if k == 0:
                    raise XError(f"recherche : HTTP {statut}", statut)
                break
            nouveaux = 0
            for m in _RE_LIEN.finditer(unescape(corps)):
                pseudo, ident = m.group(1), m.group(2)
                if ident in candidats or (compte and pseudo.lower() != compte.lower()):
                    continue
                candidats[ident] = (pseudo, ident)
                nouveaux += 1
            if nouveaux == 0:
                break
            retenus = self._candidats_valides(candidats, bas, haut, journaliser=False)
            if len(retenus) >= limite:
                break
        retenus = self._candidats_valides(candidats, bas, haut, journaliser=True)
        fiches: dict[str, dict[str, Any]] = {}
        for pseudo, ident in retenus:
            if len(fiches) >= limite:
                break
            url = f"https://x.com/{pseudo}/status/{ident}"
            try:
                fiche = self.lire(url, _fenetre=(bas, haut))
            except _Ecarte:
                continue
            except XIntrouvableError:
                continue
            except XLimiteError:
                break
            except XError:
                continue
            fiches[fiche["id"]] = fiche
        return sorted(fiches.values(), key=lambda f: (f["date"] or "", int(f["id"])))

    def _candidats_valides(
        self,
        candidats: dict[str, tuple[str, str]],
        bas: datetime | None,
        haut: datetime | None,
        journaliser: bool,
    ) -> list[tuple[str, str]]:
        """Écarte, avant toute lecture, les posts dont la date de l'identifiant sort de la fenêtre."""
        retenus = []
        for pseudo, ident in candidats.values():
            url = f"https://x.com/{pseudo}/status/{ident}"
            raison = self._hors_fenetre(date_depuis_id(ident), bas, haut)
            if raison:
                if journaliser:
                    self._noter(url, raison)
                continue
            if self.filtre_url is not None and not self.filtre_url(url):
                if journaliser:
                    self._noter(url, "filtre_url")
                continue
            retenus.append((pseudo, ident))
        # le plus récent d'abord : si la limite coupe, ce sont les plus anciens qui partent
        retenus.sort(key=lambda c: int(c[1]), reverse=True)
        return retenus

    # -- lire ------------------------------------------------------------------------------

    def lire(self, url: str, _fenetre: tuple[datetime | None, datetime | None] | None = None) -> dict[str, Any]:
        trouve = identifiant_depuis_url(url)
        if not trouve:
            raise XIntrouvableError(f"lien de post X non reconnu : {url}", None)
        pseudo, ident = trouve
        url_post = f"https://x.com/{pseudo}/status/{ident}"
        if not self._autorise(url_post):
            raise XBloqueError("lien refusé par filtre_url", None)
        bas, haut = _fenetre if _fenetre is not None else self._fenetre(None, None)
        raison = self._hors_fenetre(date_depuis_id(ident), bas, haut)
        if raison:
            self._noter(url_post, raison)
            raise _Ecarte(f"post écarté avant lecture ({raison})", None)

        fiche: dict[str, Any] | None = None
        try:
            fiche = self._lire_syndication(ident)
        except XIntrouvableError:
            self._noter(url_post, "introuvable")
            raise
        except XError:
            fiche = None
        if fiche is None:
            try:
                fiche = self._lire_oembed(url_post)
            except XIntrouvableError:
                self._noter(url_post, "introuvable")
                raise
        if not self._autorise(fiche["url"]):
            raise XBloqueError("lien refusé par filtre_url", None)
        d = self._date_fiche(fiche)
        raison = self._hors_fenetre(d, bas, haut) if fiche["date"] and len(fiche["date"]) > 10 else None
        if fiche["date"] and len(fiche["date"]) == 10:
            # jour seul (oEmbed) : on compare au jour
            jour = date.fromisoformat(fiche["date"])
            if self.date_gel is not None and jour > self.date_gel.date():
                raison = "apres_gel"
            elif (bas is not None and jour < bas.date()) or (haut is not None and jour > haut.date()):
                raison = "hors_fenetre"
        elif fiche["date"] is None and self.date_gel is not None:
            raison = "sans_date_sous_gel"
        if raison:
            self._noter(fiche["url"], raison)
            raise _Ecarte(f"post écarté ({raison})", None)
        if self.date_gel is not None:
            fiche["metriques"] = {}
        return fiche

    @staticmethod
    def _date_fiche(fiche: dict[str, Any]) -> datetime | None:
        if not fiche["date"] or len(fiche["date"]) <= 10:
            return None
        return datetime.fromisoformat(fiche["date"])

    def _lire_syndication(self, ident: str) -> dict[str, Any]:
        corps = ""
        lecture = ""
        complete = ""
        derniere: XError | None = None
        for jeton in ("a", jeton_depuis_id(ident)):
            params = {"id": ident, "lang": "fr", "token": jeton}
            url_complete = SYNDICATION + "?" + urlencode(params, quote_via=quote)
            if not self._autorise(url_complete):
                raise XBloqueError("lecture refusée par filtre_url", None)
            try:
                statut, corps, lecture, complete = self._get(SYNDICATION, params, INTERVALLE_SYNDICATION)
            except XError as exc:
                derniere = exc
                continue
            if statut == 404:
                raise XIntrouvableError("post introuvable (404)", 404)
            try:
                data = json.loads(corps)
            except ValueError:
                derniere = XError("syndication : réponse illisible", 200)
                continue
            if not isinstance(data, dict) or not data or data.get("__typename") == "TweetTombstone":
                raise XIntrouvableError("post supprimé, protégé ou sans contenu", 200)
            return self._fiche_syndication(data, ident, lecture, complete)
        raise derniere or XError("syndication : aucune réponse")

    def _fiche_syndication(self, data: dict[str, Any], ident: str, lecture: str, complete: str) -> dict[str, Any]:
        user = data.get("user") or {}
        auteur = user.get("screen_name") or None
        ident = str(data.get("id_str") or ident)
        texte = data.get("text") or ""
        cree = data.get("created_at")
        d: datetime | None = None
        if cree:
            try:
                d = _en_datetime(cree)
            except ValueError:
                d = None
        langue = data.get("lang")
        metriques: dict[str, int] = {}
        if isinstance(data.get("favorite_count"), int):
            metriques["likes"] = data["favorite_count"]
        if isinstance(data.get("conversation_count"), int):
            metriques["reponses"] = data["conversation_count"]
        return {
            "plateforme": PLATEFORME,
            "id": ident,
            "url": f"https://x.com/{auteur or 'i'}/status/{ident}",
            "auteur": auteur,
            "date": _iso(d) if d else None,
            "texte": texte,
            "langue": None if langue in (None, "", "und", "zxx", "qme", "qam", "qht", "qst") else langue,
            "medias": self._medias(data),
            "metriques": metriques,
            "route": "syndication",
            "recupere_le": lecture,
            "preuve": {"url": complete, "extrait": texte[:500]},
        }

    @staticmethod
    def _medias(data: dict[str, Any]) -> list[dict[str, str]]:
        medias: list[dict[str, str]] = []
        vus: set[str] = set()

        def ajouter(type_: str, url: Any) -> None:
            if isinstance(url, str) and url and url not in vus:
                vus.add(url)
                medias.append({"type": type_, "url": url})

        for m in data.get("mediaDetails") or []:
            if not isinstance(m, dict):
                continue
            if m.get("type") == "photo":
                ajouter("image", m.get("media_url_https"))
            else:
                variantes = [
                    v for v in (m.get("video_info") or {}).get("variants", []) if v.get("content_type") == "video/mp4"
                ]
                if variantes:
                    meilleure = max(variantes, key=lambda v: v.get("bitrate") or 0)
                    ajouter("video", meilleure.get("url"))
                else:
                    ajouter("image", m.get("media_url_https"))
        for p in data.get("photos") or []:
            if isinstance(p, dict):
                ajouter("image", p.get("url"))
        for u in (data.get("entities") or {}).get("urls", []) or []:
            if isinstance(u, dict):
                ajouter("lien", u.get("expanded_url"))
        return medias

    def _lire_oembed(self, url_post: str) -> dict[str, Any]:
        params = {"url": url_post}
        url_complete = OEMBED + "?url=" + quote(url_post, safe="")
        if not self._autorise(url_complete):
            raise XBloqueError("lecture refusée par filtre_url", None)
        statut, corps, lecture, complete = self._get(OEMBED, params, INTERVALLE_SYNDICATION)
        if statut == 404:
            raise XIntrouvableError("post introuvable (404)", 404)
        try:
            data = json.loads(corps)
            html = data["html"]
        except (ValueError, KeyError, TypeError) as exc:
            raise XError("oEmbed : réponse illisible", 200) from exc
        analyse = _Blockquote()
        analyse.feed(html)
        texte = "".join(analyse.texte).strip()
        jour = _jour_oembed(analyse.derniere_ancre)
        trouve = identifiant_depuis_url(str(data.get("url") or url_post)) or identifiant_depuis_url(url_post)
        pseudo, ident = trouve  # type: ignore[misc]
        auteur_url = str(data.get("author_url") or "")
        auteur = auteur_url.rstrip("/").rsplit("/", 1)[-1] or pseudo
        medias = []
        for href in analyse.liens:
            if href not in {m["url"] for m in medias}:
                medias.append({"type": "lien", "url": href})
        return {
            "plateforme": PLATEFORME,
            "id": ident,
            "url": f"https://x.com/{auteur}/status/{ident}",
            "auteur": auteur,
            "date": jour,
            "texte": texte,
            "langue": analyse.langue or None,
            "medias": medias,
            "metriques": {},
            "route": "oembed",
            "recupere_le": lecture,
            "preuve": {"url": complete, "extrait": texte[:500]},
        }


# --- ligne de commande -------------------------------------------------------------------


def _principal(argv: list[str] | None = None) -> int:
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="python -m enquete.sources.reseau_x", description=ANGLE_MORT)
    sous = parser.add_subparsers(dest="commande", required=True)
    for nom in ("chercher", "lire"):
        p = sous.add_parser(nom)
        p.add_argument("cible", help="la requête (chercher) ou le lien du post (lire)")
        p.add_argument("--gel", default=None)
        p.add_argument("--cache", default="cache/reseaux/x")
        if nom == "chercher":
            p.add_argument("--depuis", default=None)
            p.add_argument("--jusqua", default=None)
            p.add_argument("--limite", type=int, default=50)
            p.add_argument("--compte", default=None)
            p.add_argument("--pages", type=int, default=3)
    args = parser.parse_args(argv)
    x = ReseauX(dossier_cache=args.cache, date_gel=args.gel)
    code = 0
    try:
        if args.commande == "chercher":
            fiches = x.chercher(
                args.cible, depuis=args.depuis, jusqua=args.jusqua, limite=args.limite,
                compte=args.compte, pages=args.pages,
            )
        else:
            fiches = [x.lire(args.cible)]
        for f in fiches:
            print(json.dumps(f, ensure_ascii=False))
    except XError as exc:
        print(f"{exc.__class__.__name__} : {exc.message}", file=sys.stderr)
        code = 1
    for ligne in x.journal:
        print(json.dumps(ligne, ensure_ascii=False), file=sys.stderr)
    print(f"appels réseau : {x.appels_reseau}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(_principal())
