"""Une image pour chaque pièce et chaque dossier de l'export du réseau.

Règle de fond (décision d'Adem) : la pièce montre d'abord sa vraie capture. Une illustration ne
sert qu'à un dossier qui n'a aucune capture ; elle est toujours marquée « illustration » avec son
auteur, sa licence et son lien, et elle vient d'une banque gratuite (Wikimedia Commons, Openverse,
NASA, puis Unsplash, Pexels ou Pixabay si leur clé est déjà dans l'environnement). Jamais d'image
générée, jamais de service payant. Tout le reste serait de la fabrication.

D'où vient l'image d'une pièce :
- vidéo : l'image clé d'Oriane la plus proche de la seconde de la preuve (`frames[].url`),
  téléchargée et gardée avec son SHA-256 (les URL d'Oriane peuvent expirer) ; à défaut la
  vignette du catalogue Oriane, puis la vignette oEmbed officielle (TikTok, YouTube) ;
- site, document, registre : la capture PNG fournie dans `captures` (URL -> chemin) ;
- dossier : l'image de sa pièce la plus solide, avec `image_piece_id` ; sinon une illustration
  libre (activée par défaut, `illustrations=False` la coupe) ; sinon `image: null` et
  `silhouette: true` (zone d'ombre, l'interface dessine une silhouette pointillée).

Les QR codes lus sur les images clés téléchargées sortent en `qr_trouves` (pivots possibles :
liens de paiement, portefeuilles crypto). Les images téléchargées montrent de vraies personnes :
elles restent sous `archives/` et ne sont jamais commitées.
"""

from __future__ import annotations

import copy
import hashlib
import html
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

AGENT_UTILISATEUR = "enquete-images/0.1 (outil d'enquete pour redaction)"
DELAI_HTTP_S = 20.0
TAILLE_MAX = 15 * 1024 * 1024

PROVENANCES = ("capture", "illustration")
TYPES_CAPTURE = ("image_cle_video", "vignette_officielle", "capture_page", "capture_registre")
TYPES = (*TYPES_CAPTURE, "illustration")

# Marqueurs que laissent les générateurs d'images (norme IPTC, C2PA, outils courants).
_MARQUEURS_IA = (
    b"c2pa", b"trainedalgorithmicmedia", b"compositewithtrainedalgorithmicmedia",
    b"dall-e", b"dalle", b"midjourney", b"stable diffusion", b"stablediffusion",
    b"gpt-image", b"firefly", b"imagen",
)
_HOTES_IA = ("openai.com", "midjourney.com", "oaidalleapiprodscus", "stability.ai", "leonardo.ai")
_MOTS_REGISTRE = (
    "registre", "pappers", "annuaire-entreprises", "infogreffe", "opencorporates",
    "data.gouv", "sirene", "bodacc", "icij", "companieshouse", "company-information",
)
_RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LICENCES_LIBRES = re.compile(r"^(public domain|pd|cc0|cc[ -]by|cc[ -]by[ -]sa|attribution|free art)", re.I)


class ImageInvalide(Exception):
    """Le champ `image` enfreint une règle : il ne doit jamais atteindre l'interface."""


# --------------------------------------------------------------------------- règles


def _vide(valeur: Any) -> bool:
    return valeur is None or (isinstance(valeur, str) and not valeur.strip())


def verifier_image(image: dict[str, Any] | None, *, dossier_avec_capture: bool = False) -> None:
    """Fait respecter les règles ; lève `ImageInvalide`. `None` est permis (pas d'image)."""
    if image is None:
        return
    provenance, type_image = image.get("provenance"), image.get("type")
    if provenance not in PROVENANCES:
        raise ImageInvalide(f"provenance inconnue : {provenance!r}")
    if type_image not in TYPES:
        raise ImageInvalide(f"type d'image inconnu : {type_image!r}")
    if _vide(image.get("src")):
        raise ImageInvalide("une image sans `src` n'est pas une image")
    if image.get("genere_par_ia"):
        raise ImageInvalide("une image générée par IA n'est pas une capture")
    hote = (urlparse(str(image.get("source_url") or "")).hostname or "").lower()
    if any(hote == h or hote.endswith("." + h) for h in _HOTES_IA):
        raise ImageInvalide(f"source de générateur d'images refusée : {hote}")

    if provenance == "capture":
        if type_image not in TYPES_CAPTURE:
            raise ImageInvalide(f"une capture ne peut pas avoir le type {type_image!r}")
        if _vide(image.get("source_url")):
            raise ImageInvalide("une capture porte toujours son `source_url`")
        empreinte = image.get("sha256")
        if not isinstance(empreinte, str) or not _RE_SHA256.match(empreinte):
            raise ImageInvalide("une capture porte toujours un `sha256` valide")
    else:
        if type_image != "illustration":
            raise ImageInvalide("une illustration a le type « illustration »")
        if _vide(image.get("credit")) or _vide(image.get("licence")):
            raise ImageInvalide("une illustration porte toujours son `credit` et sa `licence`")
        if _vide(image.get("source_url")):
            raise ImageInvalide("une illustration porte toujours le lien de sa source")
        if dossier_avec_capture:
            raise ImageInvalide("un dossier qui a une capture ne reçoit jamais d'illustration")
    if _vide(image.get("legende")):
        raise ImageInvalide("une image porte toujours une légende")


def fichier_genere_par_ia(chemin: str | Path) -> bool:
    """Vrai si le fichier porte un marqueur connu de générateur d'images (C2PA, IPTC, outils)."""
    try:
        with open(chemin, "rb") as f:
            debut = f.read(TAILLE_MAX).lower()
    except OSError:
        return False
    return any(m in debut for m in _MARQUEURS_IA)


# --------------------------------------------------------------------------- téléchargement


class _EchecTelechargement(Exception):
    pass


def _type_image(octets: bytes) -> str | None:
    """Extension d'après les premiers octets ; None si ce n'est pas une image que l'on garde."""
    if octets.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if octets.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if octets[:4] == b"RIFF" and octets[8:12] == b"WEBP":
        return ".webp"
    if octets[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def _verifier_http(url: str) -> None:
    analyse = urlparse(url)
    if analyse.scheme not in ("http", "https") or not analyse.netloc:
        raise _EchecTelechargement(f"URL non valide : {url!r}")


def _telecharger(client: httpx.Client, url: str, dossier: Path) -> tuple[Path, str]:
    """Télécharge une image, la range sous `<sha256>.<ext>` et rend (chemin, sha256)."""
    _verifier_http(url)
    try:
        reponse = client.get(url, headers={"User-Agent": AGENT_UTILISATEUR}, follow_redirects=True)
        reponse.raise_for_status()
    except httpx.HTTPError as erreur:
        raise _EchecTelechargement(f"{url} : {erreur}") from erreur
    contenu = reponse.content
    if not contenu or len(contenu) > TAILLE_MAX:
        raise _EchecTelechargement(f"{url} : taille inacceptable ({len(contenu)} octets)")
    extension = _type_image(contenu)
    if extension is None:
        raise _EchecTelechargement(f"{url} : le contenu n'est pas une image reconnue")
    empreinte = hashlib.sha256(contenu).hexdigest()
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / f"{empreinte}{extension}"
    if not chemin.exists():
        chemin.write_bytes(contenu)
    if fichier_genere_par_ia(chemin):
        raise _EchecTelechargement(f"{url} : l'image porte un marqueur de génération par IA")
    return chemin, empreinte


def empreinte_fichier(chemin: str | Path) -> str:
    h = hashlib.sha256()
    with open(chemin, "rb") as f:
        for bloc in iter(lambda: f.read(1 << 20), b""):
            h.update(bloc)
    return h.hexdigest()


def _minutes(secondes: float) -> str:
    total = int(secondes)
    return f"{total // 60}:{total % 60:02d}"


# --------------------------------------------------------------------------- QR codes


def decoder_qr(chemin: str | Path) -> list[str]:
    """Contenus des QR codes lisibles sur une image (OpenCV `QRCodeDetector`). Liste vide sinon."""
    try:
        import cv2
        import numpy as np
    except ImportError as erreur:  # pragma: no cover - dépend de l'installation
        raise RuntimeError(
            "OpenCV manque : pip install opencv-python-headless (lecture des QR codes)"
        ) from erreur
    try:
        tampon = np.frombuffer(Path(chemin).read_bytes(), dtype=np.uint8)
    except OSError:
        return []
    image = cv2.imdecode(tampon, cv2.IMREAD_COLOR)
    if image is None:
        return []
    detecteur = cv2.QRCodeDetector()
    trouves: list[str] = []

    def _noter(textes: Iterable[str]) -> None:
        for t in textes:
            if t and t not in trouves:
                trouves.append(t)

    for variante in (image, cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)):
        try:
            ok, textes, _points, _ = detecteur.detectAndDecodeMulti(variante)
            if ok:
                _noter(textes)
        except cv2.error:
            pass
        if not trouves:
            try:
                texte, _points, _ = detecteur.detectAndDecode(variante)
                _noter([texte])
            except cv2.error:
                pass
        if trouves:
            break
    return trouves


# --------------------------------------------------------------------------- banques d'images libres

API_COMMONS = "https://commons.wikimedia.org/w/api.php"
API_OPENVERSE = "https://api.openverse.org/v1/images/"
API_NASA = "https://images-api.nasa.gov/search"
API_UNSPLASH = "https://api.unsplash.com/search/photos"
API_PEXELS = "https://api.pexels.com/v1/search"
API_PIXABAY = "https://pixabay.com/api/"

# Banques sans clé d'abord ; les trois autres ne servent que si leur clé est déjà dans l'environnement.
SOURCES_PAR_DEFAUT = ("commons", "openverse", "nasa", "unsplash", "pexels", "pixabay")
_VARIABLES_CLE = {"unsplash": "UNSPLASH_ACCESS_KEY", "pexels": "PEXELS_API_KEY", "pixabay": "PIXABAY_API_KEY"}

# Une illustration ne montre pas le visage d'une personne réelle identifiable : dans le doute
# (titre ou mots-clés qui parlent d'une personne), l'image est écartée.
_RE_VISAGE = re.compile(
    r"\b(portrait|selfie|face|faces|visage|headshot|people|person|persons|man|men|woman|women|"
    r"girl|boy|child|children|kid|baby|crowd|couple|student|students|model|politician|"
    r"president|minister|celebrity|actor|actress|singer|personne|personnes|homme|femme|enfant|"
    r"foule|portrait)\b", re.I)


def _texte_brut(valeur: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", str(valeur or "")))).strip()


def _json(client: httpx.Client, url: str, **options: Any) -> Any:
    try:
        reponse = client.get(url, **options)
        reponse.raise_for_status()
        return reponse.json()
    except (httpx.HTTPError, ValueError):
        return None


def _candidates_commons(client: httpx.Client, texte: str, maximum: int) -> list[dict[str, Any]]:
    donnees = _json(client, API_COMMONS, headers={"User-Agent": AGENT_UTILISATEUR}, params={
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
        "gsrsearch": f"filetype:bitmap {texte}", "gsrlimit": maximum,
        "prop": "imageinfo", "iiprop": "url|extmetadata|mime", "iiurlwidth": 800,
    })
    if not isinstance(donnees, dict):
        return []
    pages = sorted(((donnees.get("query") or {}).get("pages") or {}).values(), key=lambda p: p.get("index", 0))
    sortie = []
    for page in pages:
        for info in page.get("imageinfo") or []:
            meta = info.get("extmetadata") or {}
            if info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
                continue
            categories = _texte_brut((meta.get("Categories") or {}).get("value")).replace("|", " ")
            sortie.append({
                "src": info.get("thumburl") or info.get("url"), "source_url": info.get("descriptionurl"),
                "credit": _texte_brut((meta.get("Artist") or {}).get("value"))
                or _texte_brut((meta.get("Credit") or {}).get("value")),
                "licence": _texte_brut((meta.get("LicenseShortName") or {}).get("value")),
                "titre": _texte_brut(page.get("title", "")).removeprefix("File:"),
                "mots": categories,
            })
    return sortie


def _candidates_openverse(client: httpx.Client, texte: str, maximum: int) -> list[dict[str, Any]]:
    donnees = _json(client, API_OPENVERSE, headers={"User-Agent": AGENT_UTILISATEUR}, params={
        "q": texte, "page_size": maximum, "mature": "false", "category": "photograph",
    })
    sortie = []
    for r in (donnees or {}).get("results") or [] if isinstance(donnees, dict) else []:
        code = str(r.get("license") or "").lower()
        if code in ("cc0",):
            licence = "CC0 1.0"
        elif code == "pdm":
            licence = "Public Domain Mark 1.0"
        elif code:
            licence = f"CC {code.upper()} {r.get('license_version') or ''}".strip()
        else:
            licence = ""
        sortie.append({
            "src": r.get("thumbnail") or r.get("url"), "source_url": r.get("foreign_landing_url"),
            "credit": _texte_brut(r.get("creator")), "licence": licence,
            "titre": _texte_brut(r.get("title")),
            "mots": " ".join(str(t.get("name", "")) for t in r.get("tags") or []),
        })
    return sortie


def _candidates_nasa(client: httpx.Client, texte: str, maximum: int) -> list[dict[str, Any]]:
    donnees = _json(client, API_NASA, headers={"User-Agent": AGENT_UTILISATEUR},
                    params={"q": texte, "media_type": "image", "page_size": maximum})
    items = ((donnees or {}).get("collection") or {}).get("items") or [] if isinstance(donnees, dict) else []
    sortie = []
    for item in items:
        infos = (item.get("data") or [{}])[0]
        lien = next((l.get("href") for l in item.get("links") or [] if l.get("render") == "image"), None)
        if not (infos.get("nasa_id") and lien):
            continue
        sortie.append({
            "src": lien, "source_url": f"https://images.nasa.gov/details/{infos['nasa_id']}",
            "credit": _texte_brut(infos.get("photographer") or infos.get("secondary_creator")
                                  or infos.get("center") and f"NASA ({infos['center']})"),
            "licence": "Domaine public (NASA)", "titre": _texte_brut(infos.get("title")),
            "mots": " ".join(infos.get("keywords") or []),
        })
    return sortie


def _candidates_unsplash(client: httpx.Client, texte: str, maximum: int, cle: str) -> list[dict[str, Any]]:
    donnees = _json(client, API_UNSPLASH, headers={"Authorization": f"Client-ID {cle}"},
                    params={"query": texte, "per_page": maximum})
    return [{
        "src": (r.get("urls") or {}).get("regular"), "source_url": (r.get("links") or {}).get("html"),
        "credit": _texte_brut((r.get("user") or {}).get("name")), "licence": "Licence Unsplash",
        "titre": _texte_brut(r.get("description") or r.get("alt_description")), "mots": "",
    } for r in (donnees or {}).get("results") or []] if isinstance(donnees, dict) else []


def _candidates_pexels(client: httpx.Client, texte: str, maximum: int, cle: str) -> list[dict[str, Any]]:
    donnees = _json(client, API_PEXELS, headers={"Authorization": cle},
                    params={"query": texte, "per_page": maximum})
    return [{
        "src": (r.get("src") or {}).get("large"), "source_url": r.get("url"),
        "credit": _texte_brut(r.get("photographer")), "licence": "Licence Pexels",
        "titre": _texte_brut(r.get("alt")), "mots": "",
    } for r in (donnees or {}).get("photos") or []] if isinstance(donnees, dict) else []


def _candidates_pixabay(client: httpx.Client, texte: str, maximum: int, cle: str) -> list[dict[str, Any]]:
    donnees = _json(client, API_PIXABAY, params={
        "key": cle, "q": texte, "per_page": max(3, maximum), "image_type": "photo", "safesearch": "true"})
    return [{
        "src": r.get("webformatURL"), "source_url": r.get("pageURL"),
        "credit": _texte_brut(r.get("user")), "licence": "Licence Pixabay",
        "titre": _texte_brut(r.get("tags")), "mots": _texte_brut(r.get("tags")),
    } for r in (donnees or {}).get("hits") or []] if isinstance(donnees, dict) else []


def _libre(licence: str) -> bool:
    if re.search(r"NC|-NC|-ND", licence.upper()):
        return False  # pas d'usage commercial réservé, pas de « sans modification »
    return bool(_LICENCES_LIBRES.match(licence)) or licence.startswith(
        ("Domaine public", "Licence Unsplash", "Licence Pexels", "Licence Pixabay", "Public Domain"))


def chercher_illustration(
    mots: str | Iterable[str], client: httpx.Client | None = None, maximum: int = 8,
    sources: Iterable[str] = SOURCES_PAR_DEFAUT,
) -> dict[str, Any] | None:
    """Une image libre de droits et gratuite, ou None. Aucune génération d'image, aucun service payant.

    Interroge dans l'ordre Wikimedia Commons, Openverse, NASA (sans clé), puis Unsplash, Pexels et
    Pixabay seulement si leur clé est déjà dans l'environnement (UNSPLASH_ACCESS_KEY,
    PEXELS_API_KEY, PIXABAY_API_KEY). Rend `src`, `source_url`, `credit`, `licence`, `titre`,
    `banque`. Sont écartées : une image sans auteur lisible, sous licence non libre, ou dont le
    titre ou les mots-clés évoquent une personne (pas de visage de personne réelle).
    """
    texte = mots if isinstance(mots, str) else " ".join(m for m in mots if m)
    if not texte.strip():
        return None
    propre = client is None
    client = client or httpx.Client(timeout=DELAI_HTTP_S)
    try:
        for banque in sources:
            if banque == "commons":
                candidates = _candidates_commons(client, texte, maximum)
            elif banque == "openverse":
                candidates = _candidates_openverse(client, texte, maximum)
            elif banque == "nasa":
                candidates = _candidates_nasa(client, texte, maximum)
            elif banque in _VARIABLES_CLE:
                cle = os.environ.get(_VARIABLES_CLE[banque])
                if not cle:
                    continue
                fonction = {"unsplash": _candidates_unsplash, "pexels": _candidates_pexels,
                            "pixabay": _candidates_pixabay}[banque]
                candidates = fonction(client, texte, maximum, cle)
            else:
                continue
            for c in candidates:
                if not (c.get("src") and c.get("source_url") and c.get("credit") and c.get("licence")):
                    continue
                if not _libre(c["licence"]):
                    continue
                if _RE_VISAGE.search(f"{c.get('titre', '')} {c.get('mots', '')}"):
                    continue
                return {k: c[k] for k in ("src", "source_url", "credit", "licence", "titre")} | {"banque": banque}
    finally:
        if propre:
            client.close()
    return None


# --------------------------------------------------------------------------- vignettes officielles


def _url_oembed(url: str) -> str | None:
    hote = (urlparse(url).hostname or "").lower()
    if hote.endswith("tiktok.com"):
        return "https://www.tiktok.com/oembed?url=" + quote(url, safe="")
    if hote.endswith("youtube.com") or hote == "youtu.be":
        return "https://www.youtube.com/oembed?format=json&url=" + quote(url, safe="")
    return None


def vignette_oembed(client: httpx.Client, url: str) -> str | None:
    """URL de la vignette officielle d'une vidéo TikTok ou YouTube, ou None."""
    point = _url_oembed(url)
    if point is None:
        return None
    try:
        reponse = client.get(point, headers={"User-Agent": AGENT_UTILISATEUR})
        reponse.raise_for_status()
        miniature = reponse.json().get("thumbnail_url")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return miniature if isinstance(miniature, str) and miniature else None


# --------------------------------------------------------------------------- export


def _normaliser_resultats(resultats: Any) -> list[dict[str, Any]]:
    if resultats is None:
        return []
    if isinstance(resultats, Mapping):
        if "data" in resultats and isinstance(resultats["data"], Mapping):
            return list(resultats["data"].get("results") or [])
        return [r for r in resultats.values() if isinstance(r, Mapping)]
    return [r for r in resultats if isinstance(r, Mapping)]


def _url_clef(url: str) -> str:
    return url.strip().rstrip("/")


def _en_secondes(valeur: Any) -> float | None:
    return float(valeur) if isinstance(valeur, (int, float)) and not isinstance(valeur, bool) else None


class _Illustrateur:
    def __init__(self, resultats_full: Any, captures: Mapping[str, Any] | None, dossier: Path,
                 illustrations: bool, client: httpx.Client, mots_illustration: list[str] | None,
                 toutes_les_images: bool):
        self.dossier = dossier
        self.illustrations = illustrations
        self.client = client
        self.mots_illustration = mots_illustration
        self.toutes_les_images = toutes_les_images
        self.captures = {_url_clef(u): Path(c) for u, c in (captures or {}).items()}
        self.par_plateforme_id: dict[str, dict[str, Any]] = {}
        self.par_id: dict[str, dict[str, Any]] = {}
        for r in _normaliser_resultats(resultats_full):
            if r.get("id"):
                self.par_id[str(r["id"])] = dict(r)
            if r.get("platformId"):
                self.par_plateforme_id[str(r["platformId"])] = dict(r)
        self.qr: list[dict[str, Any]] = []
        self.echecs: list[str] = []

    # -- QR
    def _lire_qr(self, video_id: str, seconde: float | None, chemin: Path) -> None:
        for contenu in decoder_qr(chemin):
            if not any(q["video_id"] == video_id and q["contenu"] == contenu for q in self.qr):
                self.qr.append({"video_id": video_id, "seconde": seconde, "contenu": contenu})

    # -- vidéo
    def _resultat(self, piece: dict[str, Any]) -> dict[str, Any] | None:
        attributs = piece.get("attributs") or {}
        if attributs.get("id_oriane") in self.par_id:
            return self.par_id[attributs["id_oriane"]]
        return self.par_plateforme_id.get(str(attributs.get("id_plateforme") or ""))

    @staticmethod
    def _seconde_de_la_preuve(piece: dict[str, Any]) -> float | None:
        url = _url_clef((piece.get("attributs") or {}).get("url") or "")
        for p in piece.get("preuves") or []:
            if _en_secondes(p.get("debut_s")) is not None and (not url or _url_clef(p.get("url") or "") == url):
                return float(p["debut_s"])
        return None

    def _image(self, chemin: Path, sha: str, type_image: str, source_url: str,
               seconde: float | None, legende: str) -> dict[str, Any]:
        return {
            "src": str(chemin), "provenance": "capture", "type": type_image,
            "source_url": source_url, "seconde": seconde, "sha256": sha, "legende": legende,
            "credit": None, "licence": None,
        }

    def _pour_video(self, piece: dict[str, Any]) -> dict[str, Any] | None:
        resultat = self._resultat(piece)
        if resultat is None:
            return None
        video_id = str(resultat.get("id"))
        cible = self._seconde_de_la_preuve(piece)
        images = [f for m in resultat.get("media") or [] for f in m.get("frames") or [] if f.get("url")]
        images.sort(key=lambda f: (
            abs((_en_secondes(f.get("timestampSeconds")) or 0.0) - (cible or 0.0)),
            _en_secondes(f.get("timestampSeconds")) or 0.0,
        ))
        choisie: dict[str, Any] | None = None
        for f in images:
            try:
                chemin, sha = _telecharger(self.client, f["url"], self.dossier)
            except _EchecTelechargement as erreur:
                self.echecs.append(str(erreur))
                continue
            seconde = _en_secondes(f.get("timestampSeconds"))
            self._lire_qr(video_id, seconde, chemin)
            if choisie is None:
                legende = "Image de la vidéo" + (f" à {_minutes(seconde)}" if seconde is not None else "")
                choisie = self._image(chemin, sha, "image_cle_video", f["url"], seconde, legende)
            if not self.toutes_les_images:
                break
        if choisie:
            return choisie

        miniature = resultat.get("thumbnailMediaUrl")
        if miniature:
            try:
                chemin, sha = _telecharger(self.client, miniature, self.dossier)
                self._lire_qr(video_id, None, chemin)
                return self._image(chemin, sha, "vignette_officielle", miniature, None,
                                   "Vignette de la vidéo (catalogue Oriane)")
            except _EchecTelechargement as erreur:
                self.echecs.append(str(erreur))
        url_video = (piece.get("attributs") or {}).get("url") or ""
        officielle = vignette_oembed(self.client, url_video) if url_video else None
        if officielle:
            try:
                chemin, sha = _telecharger(self.client, officielle, self.dossier)
                self._lire_qr(video_id, None, chemin)
                return self._image(chemin, sha, "vignette_officielle", officielle, None,
                                   "Vignette officielle de la vidéo")
            except _EchecTelechargement as erreur:
                self.echecs.append(str(erreur))
        return None

    # -- page, document, registre
    def _pour_capture(self, piece: dict[str, Any]) -> dict[str, Any] | None:
        urls = [(piece.get("attributs") or {}).get("url")] + [p.get("url") for p in piece.get("preuves") or []]
        for url in filter(None, urls):
            chemin = self.captures.get(_url_clef(url))
            if chemin is None or not chemin.is_file():
                continue
            if fichier_genere_par_ia(chemin):
                raise ImageInvalide(f"{chemin} porte un marqueur de génération par IA")
            registre = piece.get("type") in ("societe", "document") or any(
                m in url.lower() for m in _MOTS_REGISTRE)
            return self._image(
                chemin, empreinte_fichier(chemin), "capture_registre" if registre else "capture_page",
                url, None, "Capture de la page" + (" du registre" if registre else ""),
            )
        return None

    def pour_piece(self, piece: dict[str, Any]) -> dict[str, Any] | None:
        image = self._pour_video(piece) if piece.get("type") == "video" else None
        return image or self._pour_capture(piece)

    # -- dossier
    def pour_dossier(self, dossier: dict[str, Any], question: str) -> tuple[dict[str, Any] | None, str | None]:
        candidates = [p for p in dossier["pieces"] if p.get("image")]

        def solidite(p: dict[str, Any]) -> tuple[int, int, int]:
            s = p.get("solidite") or {}
            return (ord(s.get("fiabilite_source") or "Z"), s.get("credibilite_information") or 9,
                    -len(p.get("preuves") or []))

        if candidates:
            meilleure = min(candidates, key=solidite)
            return dict(meilleure["image"]), meilleure["id"]
        if self.illustrations:
            mots = self.mots_illustration or [question]
            trouvee = chercher_illustration(mots, self.client)
            if trouvee:
                src = trouvee["src"]
                sha = None
                try:
                    chemin, sha = _telecharger(self.client, src, self.dossier)
                    src = str(chemin)
                except _EchecTelechargement as erreur:
                    self.echecs.append(str(erreur))
                return {
                    "src": src, "provenance": "illustration", "type": "illustration",
                    "source_url": trouvee["source_url"], "seconde": None, "sha256": sha,
                    "legende": "Illustration" + (f" ({trouvee['banque']})" if trouvee.get("banque") else "")
                    + (f" : {trouvee['titre']}" if trouvee.get("titre") else ""),
                    "credit": trouvee["credit"], "licence": trouvee["licence"],
                }, None
        return None, None


def illustrer_export(
    export: dict[str, Any],
    resultats_full: Any,
    captures: Mapping[str, Any] | None = None,
    dossier: str | Path = "archives/images",
    illustrations: bool = True,
    *,
    client: httpx.Client | None = None,
    mots_illustration: list[str] | None = None,
    scanner_toutes_les_images: bool = False,
) -> dict[str, Any]:
    """Copie de l'export où chaque pièce et chaque dossier porte un champ `image`.

    `resultats_full` : résultats Oriane en projection full (dict id -> résultat, liste, ou réponse
    brute). `captures` : URL -> chemin d'un PNG (produit par `archive.py`). Rien d'autre ne change,
    sauf l'ajout à la racine de `qr_trouves` (liste de {video_id, seconde, contenu}) et de
    `images_echecs` (téléchargements ratés, pour le journal).

    Une image qui enfreint une règle lève `ImageInvalide` : mieux vaut un export refusé qu'une
    image fabriquée affichée à un journaliste.
    """
    sortie = copy.deepcopy(export)
    propre = client is None
    client = client or httpx.Client(timeout=DELAI_HTTP_S)
    try:
        travail = _Illustrateur(resultats_full, captures, Path(dossier), illustrations, client,
                                mots_illustration, scanner_toutes_les_images)
        for d in sortie["dossiers"]:
            for piece in d["pieces"]:
                piece["image"] = travail.pour_piece(piece)
                verifier_image(piece["image"])
            image, piece_id = travail.pour_dossier(d, sortie.get("question") or "")
            avec_capture = any(p["image"] for p in d["pieces"])
            verifier_image(image, dossier_avec_capture=avec_capture)
            d["image"] = image
            d["image_piece_id"] = piece_id
            d["silhouette"] = image is None
        sortie["qr_trouves"] = travail.qr
        sortie["images_echecs"] = travail.echecs
    finally:
        if propre:
            client.close()
    return sortie
