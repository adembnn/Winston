"""Facebook : lecture d'un post par son lien, sans compte ni jeton.

Pas de recherche (`CAPACITES["chercher"]` est faux) : les liens viennent du WebSearch de l'agent.
Voie : `graph.facebook.com/v21.0/oembed_post` ou `oembed_video`, appelé sans jeton ; le texte et la
ligne « Posted by ... on ... » sont tirés du HTML de la réponse. Pour une vidéo ou un reel,
yt-dlp (lanceur de `reseau_youtube.py`) ajoute l'horodatage, les vues et la durée.
Pages, groupes, commentaires et recherche interne restent derrière la connexion : constaté, jamais contourné.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from enquete.reseau import Preuve
from enquete.sources.reseau_youtube import (
    OPTIONS_SURES,
    ReseauYoutube,
    YoutubeBloqueError,
    YoutubeError,
    YoutubeIntrouvableError,
    YoutubeLimiteError,
    _borne,
    _entier,
    _intervalle_fiche,
    _iso_epoch,
    _iso_jour,
)

PLATEFORME = "facebook"
CAPACITES = {"chercher": False, "lire": True}
ANGLE_MORT = ("Facebook : un post se lit par son lien ; pages, groupes, commentaires et recherche "
              "interne restent derrière la connexion.")
AGENT = "enquete-osint/0.1 (recherche journalistique, lectures publiques)"

URL_OEMBED = "https://graph.facebook.com/v21.0"
INTERVALLE = 1.0
ESSAIS_MAX = 3
ATTENTE_429_DEFAUT = 30.0
ATTENTE_429_MAX = 120.0
LIMITE_EXTRAIT = 500

# Codes d'erreur Graph qui disent « il faut un jeton » ; le code 100 d'un lien inconnu est un 400 ordinaire.
CODES_JETON = {10, 102, 104, 190, 200, 299}
PAGES_RESERVEES = {"reel", "reels", "watch", "share", "permalink.php", "photo", "photo.php", "story.php",
                   "groups", "people", "profile.php", "video.php", "videos", "events", "hashtag", "pages",
                   "p", "v", "stories", "plugins"}
MOIS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}

DateArg = date | datetime | str | None
FiltreUrl = Callable[[str], bool]


class FacebookError(Exception):
    def __init__(self, message: str, statut: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.statut = statut


class FacebookBloqueError(FacebookError):
    """Meta exige un jeton ou une connexion : constaté, jamais contourné."""


class FacebookLimiteError(FacebookError):
    """429 après réessais."""


class FacebookIntrouvableError(FacebookError):
    """400/404 : post supprimé, privé, ou hors de la fenêtre de gel."""


# ---------------------------------------------------------------------------
# HTML de l'oEmbed
# ---------------------------------------------------------------------------

class _Citation(HTMLParser):
    """Lit le `<blockquote>` de l'oEmbed : paragraphes du post, et pied « Posted by <page> on <jour> »."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.dans_citation = False
        self.dans_p = False
        self.paragraphes: list[str] = []
        self.pied: list[str] = []
        self.cite: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "blockquote":
            self.dans_citation = True
            self.cite = dict(attrs).get("cite")
        elif self.dans_citation and tag == "p":
            self.dans_p = True
            self.paragraphes.append("")
        elif self.dans_citation and tag == "br" and self.dans_p:
            self.paragraphes[-1] += "\n"

    def handle_endtag(self, tag: str) -> None:
        if tag == "blockquote":
            self.dans_citation = False
        elif tag == "p":
            self.dans_p = False

    def handle_data(self, data: str) -> None:
        if not self.dans_citation:
            return
        if self.dans_p:
            self.paragraphes[-1] += data
        else:
            self.pied.append(data)


def lire_oembed_html(code_html: str) -> dict[str, Any]:
    """{texte, auteur, jour} tirés du HTML d'un oEmbed Facebook (valeurs absentes : None)."""
    p = _Citation()
    p.feed(code_html or "")
    texte = "\n".join(x.strip() for x in p.paragraphes if x.strip()).strip()
    pied = " ".join(x.strip() for x in p.pied if x.strip())
    m = re.search(r"Posted by\s+(?P<auteur>.*?)\s+on\s+\w+,\s+(?P<mois>[A-Za-z]+)\s+(?P<jour>\d{1,2}),\s+(?P<an>\d{4})", pied)
    auteur = jour = None
    if m:
        auteur = m.group("auteur").strip() or None
        mois = MOIS.get(m.group("mois").lower())
        if mois:
            jour = f"{int(m.group('an')):04d}-{mois:02d}-{int(m.group('jour')):02d}"
    else:
        m2 = re.search(r"Posted by\s+(?P<auteur>.+?)(?:\s+on\b|$)", pied)
        auteur = m2.group("auteur").strip() if m2 else None
    return {"texte": texte or None, "auteur": auteur, "jour": jour}


# ---------------------------------------------------------------------------
# Liens
# ---------------------------------------------------------------------------

def _hote_facebook(url: str) -> bool:
    h = (urlsplit(url.strip()).hostname or "").lower()
    return h in ("facebook.com", "fb.watch", "fb.com") or h.endswith(".facebook.com")


def _segments(url: str) -> list[str]:
    return [s for s in urlsplit(url).path.split("/") if s]


def est_video(url: str) -> bool:
    p = urlsplit(url)
    segs = _segments(url)
    return ("videos" in segs or "reel" in segs or "reels" in segs or "watch" in segs
            or p.path.rstrip("/").endswith("video.php") or (p.hostname or "").lower() == "fb.watch")


def page_de_lurl(url: str) -> str | None:
    """Nom de page lu dans le lien (`/lefigaro/videos/...` -> « lefigaro ») ; None si le lien n'en porte pas."""
    segs = _segments(url)
    if segs and segs[0].lower() not in PAGES_RESERVEES:
        return segs[0]
    return None


def id_du_lien(url: str) -> str:
    p = urlsplit(url)
    q = parse_qs(p.query)
    for cle in ("v", "story_fbid", "fbid", "id"):
        if q.get(cle):
            return q[cle][0]
    for s in reversed(_segments(url)):
        if re.fullmatch(r"\d{5,}|pfbid\w+", s):
            return s
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Adaptateur
# ---------------------------------------------------------------------------

class ReseauFacebook:
    def __init__(
        self,
        dossier_cache: str | Path = "cache/reseaux/facebook",
        transport: httpx.BaseTransport | None = None,
        filtre_url: FiltreUrl | None = None,
        date_gel: DateArg = None,
        maintenant: Callable[[], datetime] = lambda: datetime.now(UTC),
        dormir: Callable[[float], None] = time.sleep,
        duree_cache: float = 86400,
        executer: Callable[[list[str]], tuple[int, str, str]] | None = None,
    ) -> None:
        self.dossier_cache = Path(dossier_cache)
        self.filtre_url = filtre_url
        self.date_gel = _borne(date_gel, True)
        self.maintenant = maintenant
        self.dormir = dormir
        self.duree_cache = duree_cache
        self.journal: list[dict[str, str]] = []
        self._appels = 0
        self._dernier_appel: float | None = None
        self._client = httpx.Client(transport=transport, headers={"User-Agent": AGENT}, timeout=30,
                                    follow_redirects=True)
        # yt-dlp : le lanceur, son cache et son intervalle de 2 s sont ceux de reseau_youtube.py.
        self._yt = ReseauYoutube(dossier_cache=self.dossier_cache / "ytdlp", maintenant=maintenant, dormir=dormir,
                                 duree_cache=duree_cache, executer=executer)

    @property
    def appels_reseau(self) -> int:
        return self._appels + self._yt.appels_reseau

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

    @staticmethod
    def _erreur_graph(corps: str, statut: int) -> FacebookError:
        try:
            e = json.loads(corps).get("error") or {}
        except (ValueError, AttributeError):
            e = {}
        message = str(e.get("message") or f"HTTP {statut}")
        if e.get("code") in CODES_JETON or "access token" in message.lower():
            return FacebookBloqueError("Meta exige un jeton", statut)
        if statut in (400, 404) or e.get("code") == 100:
            return FacebookIntrouvableError(f"introuvable ou privé : {message}", statut)
        return FacebookError(message, statut)

    def _oembed(self, url: str) -> tuple[str, str, dict[str, Any]]:
        """(URL oEmbed exacte, heure de lecture, JSON) pour un lien Facebook."""
        point = "oembed_video" if est_video(url) else "oembed_post"
        requete = self._client.build_request("GET", f"{URL_OEMBED}/{point}", params={"url": url})
        adresse = str(requete.url)
        cle = hashlib.sha256(json.dumps(["GET", f"{URL_OEMBED}/{point}", [["url", url]]],
                                        ensure_ascii=False).encode("utf-8")).hexdigest()
        en_cache = self._lire_cache(cle)
        if en_cache:
            return adresse, en_cache[0], json.loads(en_cache[1])
        for essai in range(ESSAIS_MAX):
            if self._dernier_appel is not None:
                reste = INTERVALLE - (time.monotonic() - self._dernier_appel)
                if reste > 0:
                    self.dormir(reste)
            self._dernier_appel = time.monotonic()
            self._appels += 1
            try:
                rep = self._client.send(requete)
            except httpx.HTTPError as e:
                raise FacebookError(f"réseau : {e}", None) from e
            if rep.status_code == 200:
                try:
                    donnees = json.loads(rep.text)
                except ValueError:
                    raise FacebookError("réponse oEmbed illisible", 200) from None
                if isinstance(donnees, dict) and "error" in donnees:
                    raise self._erreur_graph(rep.text, 200)
                return adresse, self._ecrire_cache(cle, rep.text), donnees
            if rep.status_code == 429:
                if essai + 1 < ESSAIS_MAX:
                    try:
                        attente = float(rep.headers.get("Retry-After", ATTENTE_429_DEFAUT))
                    except ValueError:
                        attente = ATTENTE_429_DEFAUT
                    self.dormir(min(attente, ATTENTE_429_MAX))
                    continue
                raise FacebookLimiteError("HTTP 429 après réessais", 429)
            if rep.status_code == 403:
                raise FacebookBloqueError("accès refusé (403)", 403)
            raise self._erreur_graph(rep.text, rep.status_code)
        raise FacebookLimiteError("HTTP 429 après réessais", 429)  # pragma: no cover

    def _enrichir_ytdlp(self, url: str) -> dict[str, Any] | None:
        """Horodatage, vues, durée d'une vidéo ou d'un reel ; None si yt-dlp échoue (journalisé)."""
        if not self._autorise(url):
            return None
        args = ["yt-dlp", *OPTIONS_SURES, "--skip-download", "-J", "--", url]
        try:
            _, sortie = self._yt._ytdlp(args)
            d = json.loads(sortie)
            return d if isinstance(d, dict) else None
        except YoutubeBloqueError:
            self._noter(url, "mur")
        except YoutubeLimiteError:
            self._noter(url, "limite")
        except YoutubeIntrouvableError:
            self._noter(url, "introuvable")
        except (YoutubeError, ValueError):
            self._noter(url, "introuvable")
        return None

    # -- interface -------------------------------------------------------

    def chercher(self, requete: str, depuis: DateArg = None, jusqua: DateArg = None, limite: int = 50,
                 **options: Any) -> list[dict[str, Any]]:
        raise FacebookError("Facebook : pas de recherche par mot-clé ; les liens viennent du WebSearch de l'agent", None)

    def lire(self, url: str) -> dict[str, Any]:
        url = url.strip()
        if not _hote_facebook(url):
            raise FacebookError(f"lien Facebook non reconnu : {url}", None)
        if not self._autorise(url):
            raise FacebookError("lien refusé par le filtre", None)
        adresse, lu_le, donnees = self._oembed(url)
        lu = lire_oembed_html(donnees.get("html") or "")
        info = self._enrichir_ytdlp(url) if est_video(url) else None

        date_iso = None
        if info:
            date_iso = _iso_epoch(info.get("timestamp")) or _iso_jour(info.get("upload_date"))
        date_iso = date_iso or lu["jour"]
        texte = lu["texte"] or ((info or {}).get("description") or "").strip()
        if not texte and not date_iso:
            raise FacebookBloqueError("oEmbed ne donne ni texte ni date : lecture derrière la connexion", None)

        iv = _intervalle_fiche(date_iso)
        if self.date_gel is not None:
            if iv is None:
                self._noter(url, "sans_date_sous_gel")
                raise FacebookIntrouvableError("post sans date sous gel", None)
            if iv[0] > self.date_gel:
                self._noter(url, "apres_gel")
                raise FacebookIntrouvableError("post postérieur à la date de gel", None)

        auteur = page_de_lurl(url) or lu["auteur"]
        metriques: dict[str, int] = {}
        if info and self.date_gel is None:
            vues = _entier(info.get("view_count"))
            if vues is not None:
                metriques["vues"] = vues
        medias = [{"type": "video", "url": url}] if est_video(url) else []
        extrait = (texte or f"Posted by {lu['auteur'] or auteur} on {date_iso}")[:LIMITE_EXTRAIT]
        fiche = {
            "plateforme": PLATEFORME,
            "id": id_du_lien(url),
            "url": url,
            "auteur": auteur,
            "date": date_iso,
            "texte": texte,
            "langue": None,
            "medias": medias,
            "metriques": metriques,
            "route": "oembed+yt-dlp" if info else "oembed",
            "recupere_le": lu_le,
            "preuve": {"url": adresse, "extrait": extrait},
        }
        return fiche


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
    ap = argparse.ArgumentParser(prog="python -m enquete.sources.reseau_facebook")
    ap.add_argument("--cache", default="cache/reseaux/facebook")
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
    r = ReseauFacebook(dossier_cache=a.cache, date_gel=a.gel)
    try:
        fiches = (r.chercher(a.requete, a.depuis, a.jusqua, a.limite) if a.commande == "chercher"
                  else [r.lire(a.url)])
    except FacebookError as e:
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
