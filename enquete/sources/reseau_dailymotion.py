"""Dailymotion : recherche et lecture par l'API publique, sans clé ni compte.

Voie : api.dailymotion.com. La recherche (`search=`) est large et bruitée : on la lit par pages de
100, triée du plus récent au plus ancien, puis on filtre en local (tous les mots de la requête,
sans casse ni accents, dans le titre ou la description) et par fenêtre de dates.
Titres et descriptions seulement, sans la parole.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from enquete.reseau import Preuve

PLATEFORME = "dailymotion"
CAPACITES = {"chercher": True, "lire": True}
ANGLE_MORT = "Dailymotion : titres et descriptions seulement, sans la parole."
AGENT = "enquete-osint/0.1 (recherche journalistique, lectures publiques)"

URL_API = "https://api.dailymotion.com"
CHAMPS = "id,title,url,owner.screenname,created_time,views_total,language,description"
INTERVALLE = 1.0
ESSAIS_MAX = 3
ATTENTE_429_DEFAUT = 30.0
ATTENTE_429_MAX = 120.0
LIMITE_EXTRAIT = 500

DateArg = date | datetime | str | None
FiltreUrl = Callable[[str], bool]


class DailymotionError(Exception):
    def __init__(self, message: str, statut: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.statut = statut


class DailymotionBloqueError(DailymotionError):
    """403 ou page anti-robot : constaté, jamais contourné."""


class DailymotionLimiteError(DailymotionError):
    """429 après réessais."""


class DailymotionIntrouvableError(DailymotionError):
    """404 : vidéo supprimée, ou hors de la fenêtre de gel."""


def _borne(valeur: DateArg, haute: bool) -> datetime | None:
    if valeur is None or valeur == "":
        return None
    if isinstance(valeur, datetime):
        return valeur if valeur.tzinfo else valeur.replace(tzinfo=UTC)
    if isinstance(valeur, date):
        return datetime(valeur.year, valeur.month, valeur.day, 23 if haute else 0, 59 if haute else 0,
                        59 if haute else 0, tzinfo=UTC)
    texte = str(valeur).strip().replace("Z", "+00:00")
    try:
        if "T" in texte or " " in texte:
            return _borne(datetime.fromisoformat(texte), haute)
        return _borne(date.fromisoformat(texte), haute)
    except ValueError:
        raise ValueError(f"Date illisible : {valeur!r}") from None


def _intervalle_fiche(texte: str | None) -> tuple[datetime, datetime] | None:
    if not texte:
        return None
    try:
        if len(texte) == 10:
            jour = date.fromisoformat(texte)
            debut = datetime(jour.year, jour.month, jour.day, tzinfo=UTC)
            return debut, debut + timedelta(hours=23, minutes=59, seconds=59)
        dt = datetime.fromisoformat(texte.replace("Z", "+00:00"))
        dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        return dt, dt
    except ValueError:
        return None


def _sans_accents(texte: str) -> str:
    return unicodedata.normalize("NFKD", texte.casefold()).encode("ascii", "ignore").decode("ascii")


def id_video(url: str) -> str | None:
    """Identifiant d'une vidéo Dailymotion d'après son lien (/video/<id>, /embed/video/<id>, dai.ly/<id>)."""
    p = urlsplit(url.strip())
    hote = (p.hostname or "").lower().removeprefix("www.")
    if hote == "dai.ly":
        m = re.match(r"/([A-Za-z0-9]+)", p.path)
        return m.group(1) if m else None
    if hote in ("dailymotion.com", "geo.dailymotion.com"):
        m = re.search(r"/video/([A-Za-z0-9]+)", p.path)
        return m.group(1) if m else None
    return None


class ReseauDailymotion:
    def __init__(
        self,
        dossier_cache: str | Path = "cache/reseaux/dailymotion",
        transport: httpx.BaseTransport | None = None,
        filtre_url: FiltreUrl | None = None,
        date_gel: DateArg = None,
        maintenant: Callable[[], datetime] = lambda: datetime.now(UTC),
        dormir: Callable[[float], None] = time.sleep,
        duree_cache: float = 86400,
        executer: Any = None,
    ) -> None:
        self.dossier_cache = Path(dossier_cache)
        self.filtre_url = filtre_url
        self.date_gel = _borne(date_gel, True)
        self.maintenant = maintenant
        self.dormir = dormir
        self.duree_cache = duree_cache
        self.executer = executer  # inutilisé : pas de yt-dlp ici
        self.journal: list[dict[str, str]] = []
        self.appels_reseau = 0
        self._dernier_appel: float | None = None
        self._client = httpx.Client(transport=transport, headers={"User-Agent": AGENT}, timeout=30,
                                    follow_redirects=True)

    # -- outils ----------------------------------------------------------

    def _noter(self, url: str, raison: str) -> None:
        self.journal.append({"url": url, "raison": raison, "date": self.maintenant().isoformat()})

    def _autorise(self, url: str) -> bool:
        if self.filtre_url is not None and not self.filtre_url(url):
            self._noter(url, "filtre_url")
            return False
        return True

    def _chemin(self, cle: str) -> Path:
        return self.dossier_cache / f"{cle}.json"

    def _lire_cache(self, cle: str) -> tuple[str, str] | None:
        try:
            brut = json.loads(self._chemin(cle).read_text("utf-8"))
            lu = datetime.fromisoformat(brut["recupere_le"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if (self.maintenant() - lu).total_seconds() > self.duree_cache:
            return None
        return brut["recupere_le"], brut["reponse"]

    def _ecrire_cache(self, cle: str, reponse: str) -> str:
        quand = self.maintenant().isoformat()
        self.dossier_cache.mkdir(parents=True, exist_ok=True)
        chemin = self._chemin(cle)
        tmp = chemin.with_suffix(".tmp")
        tmp.write_text(json.dumps({"recupere_le": quand, "reponse": reponse}, ensure_ascii=False), "utf-8")
        tmp.replace(chemin)
        return quand

    def _get(self, chemin: str, params: dict[str, Any]) -> tuple[str, str, str]:
        """GET sur l'API : (URL exacte, heure de lecture, corps). Lève une `DailymotionError`."""
        requete = self._client.build_request("GET", URL_API + chemin, params=params)
        url = str(requete.url)
        cle = hashlib.sha256(json.dumps(["GET", URL_API + chemin, sorted(params.items())],
                                        ensure_ascii=False).encode("utf-8")).hexdigest()
        en_cache = self._lire_cache(cle)
        if en_cache:
            return url, en_cache[0], en_cache[1]
        for essai in range(ESSAIS_MAX):
            if self._dernier_appel is not None:
                reste = INTERVALLE - (time.monotonic() - self._dernier_appel)
                if reste > 0:
                    self.dormir(reste)
            self._dernier_appel = time.monotonic()
            self.appels_reseau += 1
            try:
                rep = self._client.send(requete)
            except httpx.HTTPError as e:
                raise DailymotionError(f"réseau : {e}", None) from e
            if rep.status_code == 200:
                return url, self._ecrire_cache(cle, rep.text), rep.text
            if rep.status_code == 429:
                if essai + 1 < ESSAIS_MAX:
                    try:
                        attente = float(rep.headers.get("Retry-After", ATTENTE_429_DEFAUT))
                    except ValueError:
                        attente = ATTENTE_429_DEFAUT
                    self.dormir(min(attente, ATTENTE_429_MAX))
                    continue
                raise DailymotionLimiteError("HTTP 429 après réessais", 429)
            if rep.status_code == 404:
                raise DailymotionIntrouvableError("introuvable (404)", 404)
            if rep.status_code == 403:
                raise DailymotionBloqueError("accès refusé (403)", 403)
            raise DailymotionError(f"HTTP {rep.status_code}", rep.status_code)
        raise DailymotionLimiteError("HTTP 429 après réessais", 429)  # pragma: no cover

    def _verdict(self, texte_date: str | None, depuis: datetime | None, jusqua: datetime | None) -> str | None:
        iv = _intervalle_fiche(texte_date)
        if iv is None:
            return "sans_date_sous_gel" if self.date_gel is not None else None
        debut, fin = iv
        if self.date_gel is not None and debut > self.date_gel:
            return "apres_gel"
        if (jusqua is not None and debut > jusqua) or (depuis is not None and fin < depuis):
            return "hors_fenetre"
        return None

    def _fiche(self, v: dict[str, Any], url_lue: str, lu_le: str) -> dict[str, Any] | None:
        if not isinstance(v, dict) or not v.get("id"):
            return None
        titre = (v.get("title") or "").strip()
        description = (v.get("description") or "").strip()
        texte = f"{titre}\n\n{description}" if description else titre
        ct = v.get("created_time")
        date_iso = (datetime.fromtimestamp(ct, UTC).isoformat()
                    if isinstance(ct, int) and not isinstance(ct, bool) and ct > 0 else None)
        vues = v.get("views_total")
        metriques = {}
        if self.date_gel is None and isinstance(vues, int) and not isinstance(vues, bool):
            metriques["vues"] = vues
        url = v.get("url") or f"https://www.dailymotion.com/video/{v['id']}"
        return {
            "plateforme": PLATEFORME,
            "id": str(v["id"]),
            "url": url,
            "auteur": v.get("owner.screenname") or None,
            "date": date_iso,
            "texte": texte,
            "langue": v.get("language") or None,
            "medias": [{"type": "video", "url": url}],
            "metriques": metriques,
            "route": "api",
            "recupere_le": lu_le,
            "preuve": {"url": url_lue, "extrait": texte[:LIMITE_EXTRAIT]},
        }

    # -- interface -------------------------------------------------------

    def lire(self, url: str) -> dict[str, Any]:
        vid = id_video(url)
        if vid is None:
            raise DailymotionError(f"lien Dailymotion non reconnu : {url}", None)
        if not self._autorise(f"https://www.dailymotion.com/video/{vid}"):
            raise DailymotionError("lien refusé par le filtre", None)
        url_lue, lu_le, corps = self._get(f"/video/{vid}", {"fields": CHAMPS})
        try:
            fiche = self._fiche(json.loads(corps), url_lue, lu_le)
        except ValueError:
            fiche = None
        if fiche is None:
            raise DailymotionError("réponse illisible", None)
        raison = self._verdict(fiche["date"], None, None)
        if raison:
            self._noter(fiche["url"], raison)
            raise DailymotionIntrouvableError(f"vidéo écartée ({raison})", None)
        if not self._autorise(fiche["url"]):
            raise DailymotionError("lien refusé par le filtre", None)
        return fiche

    def chercher(self, requete: str, depuis: DateArg = None, jusqua: DateArg = None, limite: int = 50,
                 pages: int = 5, **options: Any) -> list[dict[str, Any]]:
        depuis_dt, jusqua_dt = _borne(depuis, False), _borne(jusqua, True)
        mots = [_sans_accents(m) for m in requete.split() if m]
        trouvees: dict[str, dict[str, Any]] = {}
        for page in range(1, pages + 1):
            params = {"search": requete, "sort": "recent", "limit": 100, "page": page, "fields": CHAMPS}
            adresse = f"{URL_API}/videos?search={requete}"
            if not self._autorise(adresse):
                break
            try:
                url_lue, lu_le, corps = self._get("/videos", params)
                liste = json.loads(corps).get("list") or []
                a_suite = bool(json.loads(corps).get("has_more"))
            except DailymotionError as e:
                if page == 1:
                    raise
                self._noter(adresse, "limite" if isinstance(e, DailymotionLimiteError)
                            else "mur" if isinstance(e, DailymotionBloqueError) else "introuvable")
                break
            except (ValueError, AttributeError):
                if page == 1:
                    raise DailymotionError("réponse de recherche illisible", None) from None
                break
            plus_ancienne = None
            for v in liste:
                fiche = self._fiche(v, url_lue, lu_le)
                if fiche is None:
                    continue
                iv = _intervalle_fiche(fiche["date"])
                if iv is not None and (plus_ancienne is None or iv[0] < plus_ancienne):
                    plus_ancienne = iv[0]
                haystack = _sans_accents(f"{v.get('title') or ''} {v.get('description') or ''}")
                if not all(m in haystack for m in mots):
                    continue
                if fiche["id"] in trouvees:
                    continue
                raison = self._verdict(fiche["date"], depuis_dt, jusqua_dt)
                if raison:
                    self._noter(fiche["url"], raison)
                    continue
                if not self._autorise(fiche["url"]):
                    continue
                trouvees[fiche["id"]] = fiche
            # triée du plus récent au plus ancien : inutile d'aller plus loin sous la borne basse
            if not a_suite or (depuis_dt is not None and plus_ancienne is not None and plus_ancienne < depuis_dt):
                break
            if len(trouvees) >= limite and depuis_dt is None:
                break
        fiches = sorted(trouvees.values(), key=lambda f: f["date"] or "9999", reverse=True)[:limite]
        fiches.sort(key=lambda f: f["date"] or "")
        return fiches


def vers_preuve(fiche: dict[str, Any]) -> Preuve:
    return Preuve(
        url=fiche["preuve"]["url"],
        extrait=fiche["preuve"]["extrait"],
        source_donnee=f"{fiche['plateforme']} ({fiche['route']})",
        date_collecte=datetime.fromisoformat(fiche["recupere_le"]),
    )


def main(argv: list[str] | None = None) -> int:
    for flux in (sys.stdout, sys.stderr):
        if hasattr(flux, "reconfigure"):
            flux.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m enquete.sources.reseau_dailymotion")
    ap.add_argument("--cache", default="cache/reseaux/dailymotion")
    sub = ap.add_subparsers(dest="commande", required=True)
    c = sub.add_parser("chercher")
    c.add_argument("requete")
    c.add_argument("--depuis")
    c.add_argument("--jusqua")
    c.add_argument("--gel")
    c.add_argument("--limite", type=int, default=50)
    li = sub.add_parser("lire")
    li.add_argument("url")
    li.add_argument("--gel")
    a = ap.parse_args(argv)
    r = ReseauDailymotion(dossier_cache=a.cache, date_gel=a.gel)
    try:
        fiches = (r.chercher(a.requete, a.depuis, a.jusqua, a.limite) if a.commande == "chercher"
                  else [r.lire(a.url)])
    except DailymotionError as e:
        print(f"{type(e).__name__} : {e.message}", file=sys.stderr)
        return 1
    finally:
        for ligne in r.journal:
            print(json.dumps(ligne, ensure_ascii=False), file=sys.stderr)
        print(f"appels_reseau={r.appels_reseau}", file=sys.stderr)
    for f in fiches:
        print(json.dumps(f, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
