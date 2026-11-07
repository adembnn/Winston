"""YouTube hors Oriane : recherche et lecture par yt-dlp, sans compte, sans cookies, sans téléchargement.

Voie : yt-dlp en sous-processus (liste d'arguments, `--` avant l'URL, sortie lue en UTF-8).
- `chercher` : `yt-dlp --flat-playlist -J "ytsearch<N>:<requête>"` (N = 2 x limite, 100 au plus),
  puis `yt-dlp --skip-download -J -- <url>` par vidéo (2 s entre deux) pour la date, les vues,
  les likes et le nombre de commentaires. La recherche ne donne ni date ni tri : les dates sont
  filtrées après lecture.
- `lire` : une vidéo par son lien.
- `commentaires` : à la demande seulement (un commentaire vient souvent d'un particulier).

Jamais de compte, de cookie, de sous-titre ni de fichier vidéo : `--ignore-config` écarte toute
configuration locale de yt-dlp, `--skip-download` et `-J` n'écrivent rien sur le disque.
Le lanceur (`lanceur_ytdlp`) vit ici et `reseau_facebook.py` l'importe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx  # noqa: F401  (transport accepté par le contrat commun ; yt-dlp fait ses propres appels)

from enquete.reseau import Preuve

PLATEFORME = "youtube"
CAPACITES = {"chercher": True, "lire": True}
ANGLE_MORT = "YouTube hors Oriane : titres, descriptions et commentaires, sans la parole."
AGENT = "enquete-osint/0.1 (recherche journalistique, lectures publiques)"

INTERVALLE_YTDLP = 2.0
ATTENTE_429 = 30.0
LIMITE_EXTRAIT = 500
RECHERCHE_MAX = 100

DateArg = date | datetime | str | None
FiltreUrl = Callable[[str], bool]
Executeur = Callable[[list[str]], tuple[int, str, str]]

# Options communes : aucune configuration locale (cookies, compte), aucun sous-titre, aucun fichier.
OPTIONS_SURES = [
    "--ignore-config",
    "--no-write-subs",
    "--no-write-auto-subs",
    "--no-write-thumbnail",
    "--no-playlist",
]


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------

class YoutubeError(Exception):
    def __init__(self, message: str, statut: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.statut = statut


class YoutubeBloqueError(YoutubeError):
    """Mur de connexion ou page anti-robot : constaté, jamais contourné."""


class YoutubeLimiteError(YoutubeError):
    """HTTP 429 après réessai."""


class YoutubeIntrouvableError(YoutubeError):
    """Vidéo supprimée, privée, ou hors de la fenêtre de gel."""


# ---------------------------------------------------------------------------
# Lanceur yt-dlp (partagé avec reseau_facebook.py)
# ---------------------------------------------------------------------------

def lanceur_ytdlp(args: list[str]) -> tuple[int, str, str]:
    """Lance yt-dlp (en module, donc sans dépendre du PATH). `args[0]` vaut « yt-dlp »."""
    cmd = [sys.executable, "-m", "yt_dlp", *args[1:]] if args and args[0] == "yt-dlp" else list(args)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    p = subprocess.run(cmd, capture_output=True, timeout=300, env=env, check=False)  # noqa: S603
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def erreur_ytdlp(stderr: str, erreur_base: type[YoutubeError] = YoutubeError) -> YoutubeError:
    """Classe un échec de yt-dlp d'après son message d'erreur."""
    s = stderr or ""
    ligne = next((x for x in reversed(s.strip().splitlines()) if x.startswith("ERROR")), s.strip()[-300:])
    if "HTTP Error 429" in s:
        return YoutubeLimiteError(ligne, 429)
    if "Sign in to confirm" in s or "Sign in to view" in s or "login required" in s.lower():
        return YoutubeBloqueError(ligne, 403)
    marques = ("Video unavailable", "Private video", "has been removed", "is not available",
               "does not exist", "Unable to extract", "Cannot parse data", "404")
    if any(m in s for m in marques):
        return YoutubeIntrouvableError(ligne, 404)
    return erreur_base(ligne or "yt-dlp a échoué", None)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def _borne(valeur: DateArg, haute: bool) -> datetime | None:
    """Borne de fenêtre : une date seule vaut 00:00 UTC (depuis) ou 23:59:59 UTC (jusqua, gel)."""
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
    """(début, fin) de la date d'une fiche : un instant, ou la journée entière si seul le jour est connu."""
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


def _iso_epoch(valeur: Any) -> str | None:
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)) or valeur <= 0:
        return None
    return datetime.fromtimestamp(valeur, UTC).isoformat()


def _iso_jour(valeur: Any) -> str | None:
    """« 20261030 » -> « 2026-10-30 »."""
    if isinstance(valeur, str) and re.fullmatch(r"\d{8}", valeur):
        return f"{valeur[:4]}-{valeur[4:6]}-{valeur[6:]}"
    return None


def _entier(valeur: Any) -> int | None:
    return valeur if isinstance(valeur, int) and not isinstance(valeur, bool) else None


# ---------------------------------------------------------------------------
# Adaptateur
# ---------------------------------------------------------------------------

def id_video(url: str) -> str | None:
    """Identifiant d'une vidéo YouTube d'après son lien (watch, youtu.be, shorts, embed, live)."""
    p = urlsplit(url.strip())
    hote = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if hote == "youtu.be":
        return p.path.strip("/").split("/")[0] or None
    if hote in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if p.path == "/watch":
            v = parse_qs(p.query).get("v")
            return v[0] if v else None
        m = re.match(r"/(?:shorts|embed|live|v)/([\w-]{6,})", p.path)
        return m.group(1) if m else None
    return None


class ReseauYoutube:
    def __init__(
        self,
        dossier_cache: str | Path = "cache/reseaux/youtube",
        transport: Any = None,
        filtre_url: FiltreUrl | None = None,
        date_gel: DateArg = None,
        maintenant: Callable[[], datetime] = lambda: datetime.now(UTC),
        dormir: Callable[[float], None] = time.sleep,
        duree_cache: float = 86400,
        executer: Executeur | None = None,
    ) -> None:
        self.dossier_cache = Path(dossier_cache)
        self.transport = transport  # inutilisé : yt-dlp fait ses propres appels
        self.filtre_url = filtre_url
        self.date_gel = _borne(date_gel, True)
        self.maintenant = maintenant
        self.dormir = dormir
        self.duree_cache = duree_cache
        self.executer: Executeur = executer or lanceur_ytdlp
        self.journal: list[dict[str, str]] = []
        self.appels_reseau = 0
        self._dernier_appel: float | None = None

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
        chemin = self._chemin(cle)
        try:
            brut = json.loads(chemin.read_text("utf-8"))
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

    def _ytdlp(self, args: list[str]) -> tuple[str, str]:
        """Lance yt-dlp (ou relit le cache) : (heure de lecture, sortie JSON). Lève si échec."""
        cle = hashlib.sha256(json.dumps(["yt-dlp", args], ensure_ascii=False).encode("utf-8")).hexdigest()
        en_cache = self._lire_cache(cle)
        if en_cache:
            return en_cache
        for essai in range(2):
            if self._dernier_appel is not None:
                reste = INTERVALLE_YTDLP - (time.monotonic() - self._dernier_appel)
                if reste > 0:
                    self.dormir(reste)
            self._dernier_appel = time.monotonic()
            self.appels_reseau += 1
            code, sortie, erreur = self.executer(args)
            if code == 0 and sortie.strip():
                return self._ecrire_cache(cle, sortie), sortie
            err = erreur_ytdlp(erreur)
            if isinstance(err, YoutubeLimiteError) and essai == 0:
                self.dormir(ATTENTE_429)
                continue
            raise err
        raise YoutubeLimiteError("HTTP Error 429", 429)  # pragma: no cover

    def _verdict(self, texte_date: str | None, depuis: datetime | None, jusqua: datetime | None) -> str | None:
        """Raison d'écarter une fiche d'après sa date ; None si elle passe."""
        iv = _intervalle_fiche(texte_date)
        if iv is None:
            return "sans_date_sous_gel" if self.date_gel is not None else None
        debut, fin = iv
        if self.date_gel is not None and debut > self.date_gel:
            return "apres_gel"
        if (jusqua is not None and debut > jusqua) or (depuis is not None and fin < depuis):
            return "hors_fenetre"
        return None

    # -- fiches ----------------------------------------------------------

    def _fiche_video(self, d: dict[str, Any], url: str, lu_le: str, commande: str) -> dict[str, Any]:
        titre = (d.get("title") or "").strip()
        description = (d.get("description") or "").strip()
        texte = f"{titre}\n\n{description}" if description else titre
        metriques = {}
        if self.date_gel is None:
            for cle, source in (("vues", "view_count"), ("likes", "like_count"), ("commentaires", "comment_count")):
                v = _entier(d.get(source))
                if v is not None:
                    metriques[cle] = v
        return {
            "plateforme": PLATEFORME,
            "id": str(d["id"]),
            "url": url,
            "auteur": (d.get("uploader") or d.get("channel") or None),
            "date": _iso_epoch(d.get("timestamp")) or _iso_jour(d.get("upload_date")),
            "texte": texte,
            "langue": d.get("language") or None,
            "medias": [{"type": "video", "url": url}],
            "metriques": metriques,
            "route": "yt-dlp",
            "recupere_le": lu_le,
            "preuve": {"url": url, "extrait": texte[:LIMITE_EXTRAIT]},
        }

    def _lire_video(self, url: str) -> dict[str, Any]:
        args = ["yt-dlp", *OPTIONS_SURES, "--skip-download", "-J", "--", url]
        lu_le, sortie = self._ytdlp(args)
        try:
            d = json.loads(sortie)
            if not isinstance(d, dict) or "id" not in d:
                raise ValueError
        except ValueError:
            raise YoutubeError("réponse de yt-dlp illisible", None) from None
        return self._fiche_video(d, url, lu_le, "video")

    @staticmethod
    def _url_canonique(identifiant: str) -> str:
        return f"https://www.youtube.com/watch?v={identifiant}"

    # -- interface -------------------------------------------------------

    def lire(self, url: str) -> dict[str, Any]:
        vid = id_video(url)
        if vid is None:
            raise YoutubeError(f"lien YouTube non reconnu : {url}", None)
        canon = self._url_canonique(vid)
        if not self._autorise(canon):
            raise YoutubeError("lien refusé par le filtre", None)
        fiche = self._lire_video(canon)
        raison = self._verdict(fiche["date"], None, None)
        if raison:
            self._noter(canon, raison)
            raise YoutubeIntrouvableError(f"vidéo écartée ({raison})", None)
        if not self._autorise(fiche["url"]):
            raise YoutubeError("lien refusé par le filtre", None)
        return fiche

    def chercher(self, requete: str, depuis: DateArg = None, jusqua: DateArg = None, limite: int = 50,
                 **options: Any) -> list[dict[str, Any]]:
        if not self._autorise(f"https://www.youtube.com/results?search_query={quote(requete)}"):
            return []
        depuis_dt, jusqua_dt = _borne(depuis, False), _borne(jusqua, True)
        n = max(1, min(RECHERCHE_MAX, 2 * limite))
        args = ["yt-dlp", *OPTIONS_SURES, "--flat-playlist", "-J", f"ytsearch{n}:{requete}"]
        _, sortie = self._ytdlp(args)  # si cette première requête échoue, l'erreur monte
        try:
            entrees = json.loads(sortie).get("entries") or []
        except (ValueError, AttributeError):
            raise YoutubeError("réponse de recherche illisible", None) from None
        vus: set[str] = set()
        fiches: list[dict[str, Any]] = []
        for e in entrees:
            if len(fiches) >= limite:
                break
            if not isinstance(e, dict) or not e.get("id") or e["id"] in vus:
                continue
            vus.add(e["id"])
            url = self._url_canonique(e["id"])
            if not self._autorise(url):
                continue
            try:
                fiche = self._lire_video(url)
            except YoutubeLimiteError:
                self._noter(url, "limite")
                break
            except YoutubeBloqueError:
                self._noter(url, "mur")
                continue
            except YoutubeError:
                self._noter(url, "introuvable")
                continue
            raison = self._verdict(fiche["date"], depuis_dt, jusqua_dt)
            if raison:
                self._noter(url, raison)
                continue
            if not self._autorise(fiche["url"]):
                continue
            fiches.append(fiche)
        fiches.sort(key=lambda f: (_intervalle_fiche(f["date"]) or (datetime.max.replace(tzinfo=UTC),))[0])
        return fiches

    def commentaires(self, url: str, limite: int = 50) -> list[dict[str, Any]]:
        """Commentaires d'une vidéo, à la demande seulement (auteurs souvent particuliers)."""
        vid = id_video(url)
        if vid is None:
            raise YoutubeError(f"lien YouTube non reconnu : {url}", None)
        canon = self._url_canonique(vid)
        if not self._autorise(canon):
            return []
        args = ["yt-dlp", *OPTIONS_SURES, "--skip-download", "--write-comments", "--extractor-args",
                f"youtube:max_comments={int(limite)},all,0,0", "-J", "--", canon]
        lu_le, sortie = self._ytdlp(args)
        try:
            liste = json.loads(sortie).get("comments") or []
        except (ValueError, AttributeError):
            raise YoutubeError("réponse de yt-dlp illisible", None) from None
        resultat: list[dict[str, Any]] = []
        vus: set[str] = set()
        for c in liste:
            if not isinstance(c, dict) or not c.get("id") or c["id"] in vus:
                continue
            vus.add(c["id"])
            quand = _iso_epoch(c.get("timestamp"))
            jour = quand[:10] if quand else None  # YouTube ne donne que le jour approximatif
            raison = self._verdict(jour, None, None)
            lien = f"{canon}&lc={c['id']}"
            if raison:
                self._noter(lien, raison)
                continue
            texte = c.get("text") or ""
            likes = _entier(c.get("like_count"))
            resultat.append({
                "plateforme": PLATEFORME,
                "id": str(c["id"]),
                "url": lien,
                "auteur": (c.get("author") or "").lstrip("@") or None,
                "date": jour,
                "texte": texte,
                "langue": None,
                "medias": [],
                "metriques": {"likes": likes} if likes is not None and self.date_gel is None else {},
                "route": "yt-dlp/commentaire",
                "recupere_le": lu_le,
                "preuve": {"url": canon, "extrait": texte[:LIMITE_EXTRAIT]},
            })
            if len(resultat) >= limite:
                break
        resultat.sort(key=lambda f: f["date"] or "9999")
        return resultat


def vers_preuve(fiche: dict[str, Any]) -> Preuve:
    """La fiche comme `reseau.Preuve`."""
    return Preuve(
        url=fiche["preuve"]["url"],
        extrait=fiche["preuve"]["extrait"],
        source_donnee=f"{fiche['plateforme']} ({fiche['route']})",
        date_collecte=datetime.fromisoformat(fiche["recupere_le"]),
    )


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def _sortie_utf8() -> None:
    for flux in (sys.stdout, sys.stderr):
        if hasattr(flux, "reconfigure"):
            flux.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _sortie_utf8()
    ap = argparse.ArgumentParser(prog="python -m enquete.sources.reseau_youtube")
    ap.add_argument("--cache", default="cache/reseaux/youtube")
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
    co = sub.add_parser("commentaires")
    co.add_argument("url")
    co.add_argument("--limite", type=int, default=50)
    a = ap.parse_args(argv)
    r = ReseauYoutube(dossier_cache=a.cache, date_gel=getattr(a, "gel", None))
    try:
        if a.commande == "chercher":
            fiches = r.chercher(a.requete, a.depuis, a.jusqua, a.limite)
        elif a.commande == "lire":
            fiches = [r.lire(a.url)]
        else:
            fiches = r.commentaires(a.url, a.limite)
    except YoutubeError as e:
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
