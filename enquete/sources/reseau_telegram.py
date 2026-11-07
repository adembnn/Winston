"""Telegram : lire les canaux publics à aperçu web, sans compte.

Voies (testées le 7 nov. 2026) :
- `t.me/s/<canal>?q=<requête>` : les messages d'un canal qui contiennent la requête, 20 par page,
  du plus ancien au plus récent ; `?before=<id>` remonte plus loin dans le temps ;
- `t.me/s/<canal>?before=<id + 1>` : le message visé est le plus récent de la page ;
- `lyzem.com/search?q=` : moteur tiers, bruité, qui fournit des liens t.me quand on ne connaît
  aucun canal (5 canaux au plus, journalisés `canal_decouvert`).

Un canal sans aperçu web redirige vers `t.me/<canal>` : on le constate (`TelegramBloqueError`),
on ne le contourne jamais. Aucun compte, aucun cookie, aucun jeton.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx

PLATEFORME = "telegram"
CAPACITES = {"chercher": True, "lire": True}
ANGLE_MORT = (
    "Telegram : seuls les canaux publics à aperçu web se lisent ; "
    "les groupes et la recherche entre canaux restent hors de portée."
)

AGENT = "enquete-osint/0.1 (recherche journalistique, lectures publiques)"
URL_APERCU = "https://t.me/s/"
URL_LYZEM = "https://lyzem.com/search"
CANAUX_MAX = 5
PAUSE_HOTE_S = 1.0
ATTENTE_429_S = 20.0
LIMITE_EXTRAIT = 500

FiltreUrl = Callable[[str], bool]
DateBorne = date | datetime | str | None

_RE_LIEN = re.compile(r"^(?:https?://)?(?:www\.)?t\.me/(?:s/)?([A-Za-z0-9_]{3,})/(\d+)(?:[/?#].*)?$")
_RE_VUES = re.compile(r"^\s*([\d.,]+)\s*([KkMm]?)\s*$")


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------

class TelegramError(Exception):
    def __init__(self, message: str, statut: int | None = None):
        super().__init__(message)
        self.message = message
        self.statut = statut


class TelegramBloqueError(TelegramError):
    """Aperçu web fermé, 403, page anti-robot : constaté, jamais contourné."""


class TelegramLimiteError(TelegramError):
    """429 après réessai."""


class TelegramIntrouvableError(TelegramError):
    """Message absent, supprimé ou hors de la fenêtre du banc."""


# ---------------------------------------------------------------------------
# Dates et nombres
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


def lire_nombre(texte: str | None) -> int | None:
    """« 638 » -> 638 ; « 5.93K » -> 5930 ; « 1.2M » -> 1200000."""
    if not texte:
        return None
    m = _RE_VUES.match(texte)
    if not m:
        return None
    nombre, suffixe = m.group(1).replace(",", "."), m.group(2).lower()
    try:
        valeur = float(nombre)
    except ValueError:
        return None
    return round(valeur * {"": 1, "k": 1_000, "m": 1_000_000}[suffixe])


# ---------------------------------------------------------------------------
# Analyse de la page t.me/s/<canal> (html.parser)
# ---------------------------------------------------------------------------

class _PageCanal(HTMLParser):
    """Rassemble les blocs `data-post="<canal>/<id>"` d'une page d'aperçu web."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[dict[str, Any]] = []
        self._cur: dict[str, Any] | None = None
        self._profondeur = 0
        self._texte_prof: int | None = None  # profondeur du <div> du texte en cours
        self._texte: list[str] = []
        self._dans_vues = False

    @staticmethod
    def _classes(attrs: dict[str, str | None]) -> set[str]:
        return set((attrs.get("class") or "").split())

    def handle_starttag(self, tag: str, attrs_liste: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_liste)
        classes = self._classes(attrs)
        if self._cur is None:
            if tag == "div" and attrs.get("data-post"):
                self._cur = {"post": attrs["data-post"], "texte": None, "date": None, "vues": None, "medias": []}
                self._profondeur = 1
            return
        if tag == "div":
            self._profondeur += 1
            if ("js-message_text" in classes and "js-message_reply_text" not in classes
                    and self._cur["texte"] is None and self._texte_prof is None):
                self._texte_prof, self._texte = self._profondeur, []
        elif tag == "br" and self._texte_prof is not None:
            self._texte.append("\n")
        elif tag == "span" and "tgme_widget_message_views" in classes:
            self._dans_vues = True
        elif tag == "time" and attrs.get("datetime") and self._cur["date"] is None:
            self._cur["date"] = attrs["datetime"]
        elif tag == "a":
            href = attrs.get("href") or ""
            if "tgme_widget_message_photo_wrap" in classes:
                m = re.search(r"url\('([^']+)'\)", attrs.get("style") or "")
                if m:
                    self._cur["medias"].append({"type": "image", "url": m.group(1)})
            elif "tgme_widget_message_video_player" in classes and href:
                self._cur["medias"].append({"type": "video", "url": href})
            elif "tgme_widget_message_link_preview" in classes and href:
                self._cur["medias"].append({"type": "lien", "url": href})
        elif tag == "video" and attrs.get("src"):
            medias = self._cur["medias"]
            if medias and medias[-1]["type"] == "video":
                medias[-1]["url"] = attrs["src"]
            else:
                medias.append({"type": "video", "url": attrs["src"]})
        elif tag == "audio" and attrs.get("src"):
            self._cur["medias"].append({"type": "audio", "url": attrs["src"]})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and self._cur is not None and self._texte_prof is not None:
            self._texte.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._cur is None:
            return
        if tag == "span":
            self._dans_vues = False
        if tag != "div":
            return
        if self._texte_prof is not None and self._profondeur == self._texte_prof:
            self._cur["texte"] = "".join(self._texte).strip()
            self._texte_prof = None
        self._profondeur -= 1
        if self._profondeur == 0:
            self.messages.append(self._cur)
            self._cur = None

    def handle_data(self, data: str) -> None:
        if self._cur is None:
            return
        if self._texte_prof is not None:
            self._texte.append(data)
        elif self._dans_vues:
            self._cur["vues"] = lire_nombre(data)


def analyser_page(html: str) -> list[dict[str, Any]]:
    """Les messages d'une page, du plus ancien au plus récent, tels que Telegram les sert."""
    p = _PageCanal()
    p.feed(html)
    p.close()
    return p.messages


class _Lyzem(HTMLParser):
    """Liens t.me d'une page de résultats lyzem, avec le type annoncé (channel, group, bot...)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canaux: list[tuple[str, str]] = []
        self._type = ""

    def handle_starttag(self, tag: str, attrs_liste: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_liste)
        classes = (attrs.get("class") or "").split()
        if tag == "li" and "search-result" in classes:
            self._type = ""
        elif tag == "div" and "search-result-type-wrapper" in classes:
            self._type = (attrs.get("title") or "").strip().lower()
        elif tag == "a":
            m = re.match(r"^https?://t\.me/([A-Za-z0-9_]{3,})(?:/\d+)?/?$", attrs.get("href") or "")
            if m:
                self.canaux.append((m.group(1), self._type))


def _canaux_de_lyzem(html: str) -> list[str]:
    p = _Lyzem()
    p.feed(html)
    vus: set[str] = set()
    retenus: list[str] = []
    for nom, type_ in p.canaux:
        cle = nom.lower()
        if cle in vus or cle.endswith("bot") or cle.startswith("lyzem"):
            continue
        vus.add(cle)
        if type_ in ("group", "bot", "user"):
            continue
        retenus.append(nom)
    return retenus


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ReseauTelegram:
    """Lectures publiques de Telegram, avec cache disque et garde-fous du banc."""

    def __init__(
        self,
        dossier_cache: str | Path = "cache/reseaux/telegram",
        transport: httpx.BaseTransport | None = None,
        filtre_url: FiltreUrl | None = None,
        date_gel: DateBorne = None,
        maintenant: Callable[[], datetime] = lambda: datetime.now(UTC),
        dormir: Callable[[float], None] = time.sleep,
        duree_cache: float = 86400,
    ):
        self._dossier = Path(dossier_cache)
        self._http = httpx.Client(
            transport=transport, timeout=40.0, follow_redirects=False, headers={"User-Agent": AGENT}
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

    def __enter__(self) -> ReseauTelegram:
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

    def _chemin(self, url: str, params: dict[str, str] | None) -> Path:
        cle = json.dumps(["GET", url, sorted((params or {}).items())], ensure_ascii=False)
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
            reste = PAUSE_HOTE_S - (self._maintenant() - dernier).total_seconds()
            if reste > 0:
                self._dormir(reste)
        self._dernier_appel[hote] = self._maintenant()

    def _lire_url(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
        """Cache d'abord, réseau ensuite. None si le filtre refuse (ni réseau ni cache)."""
        complete = url + ("?" + urlencode(params, quote_via=quote) if params else "")
        if not self._autorise(complete):
            return None
        chemin = self._chemin(url, params)
        en_cache = self._depuis_cache(chemin)
        if en_cache is not None:
            return en_cache
        hote = urlsplit(url).hostname or ""
        for essai in (1, 2):
            self._attendre(hote)
            self.appels_reseau += 1
            try:
                rep = self._http.get(url, params=params)
            except httpx.HTTPError as exc:
                raise TelegramError(f"injoignable ({type(exc).__name__}) : {complete}") from exc
            if rep.status_code == 429:
                if essai == 1:
                    try:
                        attente = float(rep.headers.get("Retry-After", ""))
                    except ValueError:
                        attente = ATTENTE_429_S
                    self._dormir(attente)
                    continue
                raise TelegramLimiteError(f"429 après réessai : {complete}", 429)
            break
        entree = {
            "url": complete,
            "statut": rep.status_code,
            "location": rep.headers.get("location"),
            "corps": rep.text if rep.status_code == 200 else "",
            "date_lecture": self._maintenant().isoformat(),
        }
        if rep.status_code in (200, 301, 302, 303, 307, 308, 403, 404):
            self._ecrire(chemin, entree)
        return entree

    @staticmethod
    def _verifier(entree: dict[str, Any], canal: str) -> str:
        """Le corps d'une page d'aperçu, ou l'erreur qui dit pourquoi on ne le lit pas."""
        statut = entree["statut"]
        if statut in (301, 302, 303, 307, 308):
            raise TelegramBloqueError(f"aperçu web fermé pour t.me/{canal}", statut)
        if statut == 403:
            raise TelegramBloqueError(f"403 sur t.me/{canal}", statut)
        if statut == 404:
            raise TelegramIntrouvableError(f"canal introuvable : {canal}", statut)
        if statut != 200:
            raise TelegramError(f"HTTP {statut} sur t.me/{canal}", statut)
        return entree["corps"]

    # ------------------------------------------------------------------ fiches

    def _fiche(self, m: dict[str, Any], route: str, entree: dict[str, Any]) -> dict[str, Any]:
        canal, _, ident = m["post"].partition("/")
        url = f"https://t.me/{canal}/{ident}"
        quand = _en_datetime(m["date"]) if m["date"] else None
        texte = m["texte"] or ""
        metriques = {} if self.date_gel is not None else (
            {"vues": m["vues"]} if m["vues"] is not None else {}
        )
        return {
            "plateforme": PLATEFORME,
            "id": m["post"],
            "url": url,
            "auteur": canal,
            "date": quand.isoformat() if quand else None,
            "texte": texte,
            "langue": None,
            "medias": m["medias"],
            "metriques": metriques,
            "route": route,
            "recupere_le": entree["date_lecture"],
            "preuve": {"url": entree["url"], "extrait": texte[:LIMITE_EXTRAIT]},
        }

    def _fiches_de_page(self, entree: dict[str, Any], canal: str, route: str) -> list[dict[str, Any]]:
        corps = self._verifier(entree, canal)
        return [self._fiche(m, route, entree) for m in analyser_page(corps)]

    # ---------------------------------------------------------------- chercher

    def _decouvrir(self, requete: str) -> list[str]:
        entree = self._lire_url(URL_LYZEM, {"q": requete})
        if entree is None:
            return []
        statut = entree["statut"]
        if statut == 429:
            raise TelegramLimiteError("429 sur lyzem", 429)
        if statut != 200:
            raise TelegramBloqueError(f"lyzem a répondu HTTP {statut}", statut)
        canaux = _canaux_de_lyzem(entree["corps"])[:CANAUX_MAX]
        for c in canaux:
            self._noter(f"https://t.me/{c}", "canal_decouvert")
        return canaux

    def chercher(
        self,
        requete: str,
        depuis: DateBorne = None,
        jusqua: DateBorne = None,
        limite: int = 50,
        canaux: list[str] | tuple[str, ...] | None = None,
        pages: int = 5,
    ) -> list[dict[str, Any]]:
        """Fiches des messages qui contiennent la requête, du plus ancien au plus récent."""
        debut = _en_datetime(depuis)
        hautes = [d for d in (_en_datetime(jusqua, fin_de_jour=True), self.date_gel) if d is not None]
        borne = min(hautes) if hautes else None
        premiere = canaux is not None  # la découverte (lyzem) est alors la première requête
        if canaux is None:
            canaux = self._decouvrir(requete)
        retenues: dict[str, dict[str, Any]] = {}
        for canal in canaux:
            canal = canal.lstrip("@").strip()
            avant: int | None = None
            fenetre_atteinte = False
            lues = 0
            for _ in range(max(pages, 0)):
                params = {"q": requete} if requete else {}
                if avant is not None:
                    params["before"] = str(avant)
                try:
                    entree = self._lire_url(URL_APERCU + canal, params)
                    if entree is None:
                        break
                    fiches = self._fiches_de_page(entree, canal, "t.me/s")
                except TelegramError as exc:
                    if premiere:
                        raise
                    raison = ("mur" if isinstance(exc, TelegramBloqueError)
                              else "limite" if isinstance(exc, TelegramLimiteError) else "introuvable")
                    self._noter(f"{URL_APERCU}{canal}", raison)
                    break
                finally:
                    premiere = False
                lues += 1
                if not fiches:
                    fenetre_atteinte = True  # plus rien à remonter
                    break
                for f in fiches:
                    quand = _en_datetime(f["date"]) if f["date"] else None
                    if not f["texte"]:
                        continue
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
                plus_ancien = min(int(f["id"].rpartition("/")[2]) for f in fiches)
                datees = [_en_datetime(f["date"]) for f in fiches if f["date"]]
                if debut is not None and datees and min(d for d in datees if d) < debut:
                    fenetre_atteinte = True
                    break
                if len(retenues) >= limite:
                    break
                avant = plus_ancien
            else:
                if lues and not fenetre_atteinte and depuis is not None:
                    self._noter(f"{URL_APERCU}{canal}", "limite")
        resultat = sorted(retenues.values(), key=lambda f: (f["date"] or "", f["id"]))
        if len(resultat) > limite:
            self._noter(URL_APERCU, "limite")
            resultat = resultat[-limite:]
        return resultat

    # -------------------------------------------------------------------- lire

    def lire(self, url: str) -> dict[str, Any]:
        """Un message d'un canal public, par son lien t.me/<canal>/<id>."""
        m = _RE_LIEN.match(url.strip())
        if not m:
            raise TelegramError(f"lien Telegram illisible : {url}")
        canal, ident = m.group(1), int(m.group(2))
        entree = self._lire_url(URL_APERCU + canal, {"before": str(ident + 1)})
        if entree is None:
            raise TelegramError(f"refusé par filtre_url : {url}")
        fiches = self._fiches_de_page(entree, canal, "t.me/s")
        cible = f"{canal}/{ident}".lower()
        trouvee = next((f for f in fiches if f["id"].lower() == cible), None)
        if trouvee is None:
            raise TelegramIntrouvableError(f"message absent ou supprimé : {url}", 404)
        if not self._autorise(trouvee["url"]):
            raise TelegramError(f"refusé par filtre_url : {trouvee['url']}")
        quand = _en_datetime(trouvee["date"]) if trouvee["date"] else None
        if self.date_gel is not None:
            if quand is None:
                self._noter(trouvee["url"], "sans_date_sous_gel")
                raise TelegramIntrouvableError(f"message sans date sous gel : {url}")
            if quand > self.date_gel:
                self._noter(trouvee["url"], "apres_gel")
                raise TelegramIntrouvableError(f"message postérieur à la date de gel : {url}")
        if not trouvee["texte"]:
            trouvee["preuve"]["extrait"] = "(message sans texte : média seul)"
        return trouvee


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def principal(argv: list[str] | None = None) -> int:
    for flux in (sys.stdout, sys.stderr):
        if hasattr(flux, "reconfigure"):
            flux.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m enquete.sources.reseau_telegram", description=ANGLE_MORT)
    sous = ap.add_subparsers(dest="commande", required=True)
    c = sous.add_parser("chercher")
    c.add_argument("requete")
    c.add_argument("--depuis")
    c.add_argument("--jusqua")
    c.add_argument("--gel")
    c.add_argument("--limite", type=int, default=50)
    c.add_argument("--canaux", help="canaux séparés par des virgules")
    c.add_argument("--pages", type=int, default=5)
    c.add_argument("--cache", default="cache/reseaux/telegram")
    li = sous.add_parser("lire")
    li.add_argument("url")
    li.add_argument("--gel")
    li.add_argument("--cache", default="cache/reseaux/telegram")
    a = ap.parse_args(argv)
    with ReseauTelegram(dossier_cache=a.cache, date_gel=a.gel) as r:
        try:
            if a.commande == "chercher":
                canaux = [x for x in a.canaux.split(",") if x] if a.canaux else None
                fiches = r.chercher(a.requete, a.depuis, a.jusqua, a.limite, canaux=canaux, pages=a.pages)
            else:
                fiches = [r.lire(a.url)]
        except TelegramError as exc:
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
