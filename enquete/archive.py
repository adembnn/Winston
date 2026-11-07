"""Archivage d'une preuve au moment où elle entre dans le dossier.

Une vidéo ou un post peut disparaître. À l'entrée dans le dossier, on garde donc :
une copie locale, son empreinte SHA-256 (elle prouve que le fichier n'a pas bougé
depuis la collecte), la date de collecte en UTC et un instantané indépendant sur la
Wayback Machine d'Internet Archive.

Chaque étape est isolée : si le téléchargement échoue, l'instantané Wayback est quand
même tenté, et le statut de chaque étape dit ce qui a réussi.

Les fichiers téléchargés sont traités comme des données : on les lit pour calculer
l'empreinte, on ne les exécute jamais.

`archiver_piece` enchaîne toutes les voies indépendantes de tout compte (capture de page
dans un vrai navigateur, fiche oEmbed, vidéo yt-dlp, Wayback anonyme, horodatage RFC 3161
auprès de plusieurs autorités). archive.today n'est volontairement pas automatisé (captcha) :
c'est un geste manuel du journaliste, rappelé dans le manifeste.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

# Statuts possibles d'une étape.
OK = "ok"
ECHEC = "echec"
IGNORE = "ignore"

# Étapes suivies dans `PieceArchivee.statuts`.
ETAPE_TELECHARGEMENT = "telechargement"
ETAPE_EMPREINTE = "empreinte"
ETAPE_WAYBACK = "wayback"
ETAPE_CAPTURE_PAGE = "capture_page"
ETAPE_OEMBED = "oembed"
ETAPE_HORODATAGE = "horodatage"

# Format léger : un seul fichier (pas de fusion audio/vidéo, donc pas besoin de ffmpeg),
# 480 p au plus, sinon la plus petite qualité disponible.
FORMAT_BASSE_DEFINITION = "b[height<=480]/worst"

# YouTube ne propose plus de fichier unique : image et son sont séparés. Ce format les fusionne
# (ffmpeg requis), 480 p au plus.
FORMAT_AVEC_FUSION = "bv*[height<=480]+ba/b[height<=480]/b"

DELAI_YTDLP_S = 300
DELAI_HTTP_S = 30
DELAI_WAYBACK_S = 180
PAUSE_SONDAGE_S = 5
TAILLE_BLOC = 1024 * 1024

# Save Page Now 2 : https://web.archive.org/save (doc « SPN2 Public API »).
# Sans clé, la capture marche mais avec des limites plus strictes ; avec les clés
# S3 d'archive.org (variables d'environnement), l'en-tête `LOW accès:secret` s'applique.
URL_WAYBACK_SAUVER = "https://web.archive.org/save"
URL_WAYBACK_STATUT = "https://web.archive.org/save/status/"
VAR_CLE_ACCES = "IA_ACCESS"
VAR_CLE_SECRET = "IA_SECRET"

URL_WAYBACK_DISPONIBLE = "https://archive.org/wayback/available"
ESSAIS_WAYBACK = 3
ATTENTE_429_DEFAUT_S = 20
ATTENTE_429_MAX_S = 120

# Autorités d'horodatage RFC 3161 : indépendantes les unes des autres, sans compte.
AUTORITES_HORODATAGE = (
    ("freetsa", "https://freetsa.org/tsr"),
    ("digicert", "http://timestamp.digicert.com"),
    ("sectigo", "http://timestamp.sectigo.com"),
)
URL_CA_FREETSA = "https://freetsa.org/files/cacert.pem"

# Capture de page : attente maximale du vrai chargement.
ATTENTE_PAGE_MAX_S = 15
CALME_RESEAU_S = 1.5

GESTE_MANUEL_ARCHIVE_TODAY = (
    "archive.today (archive.ph) n'est pas automatisé : il exige un captcha. "
    "Geste manuel du journaliste : ouvrir https://archive.ph/, coller l'URL, "
    "résoudre le captcha, puis noter l'adresse de l'instantané dans le dossier."
)

# Les en-têtes HTTP doivent rester en ASCII.
AGENT_UTILISATEUR = "enquete-archivage/0.1 (preuve numerique pour redaction)"


@dataclass
class PieceArchivee:
    """Ce qu'on sait d'une preuve archivée. Sérialisable en JSON."""

    url: str
    type: str  # "video" ou "page"
    date_collecte: str  # ISO 8601, UTC
    chemin_fichier: str | None = None
    chemin_metadonnees: str | None = None
    empreinte_sha256: str | None = None
    url_wayback: str | None = None
    journal: list[str] = field(default_factory=list)
    statuts: dict[str, str] = field(default_factory=dict)
    # Renseignés par `archiver_piece` (la chaîne historique les laisse vides).
    voies: dict[str, dict] = field(default_factory=dict)  # détail et heure de chaque voie
    fichiers: list[dict] = field(default_factory=list)  # nom, rôle, octets, SHA-256
    horodatages: list[dict] = field(default_factory=list)  # jetons RFC 3161
    mur_connexion: bool | None = None
    video_non_telechargee: bool = False
    geste_manuel: str | None = None

    def noter(self, message: str) -> None:
        """Ajoute une ligne horodatée au journal des étapes."""
        self.journal.append(f"{_maintenant()} {message}")

    @property
    def complete(self) -> bool:
        """Vrai si aucune étape demandée n'a échoué."""
        return ECHEC not in self.statuts.values()

    def vers_dict(self) -> dict:
        return asdict(self)

    def voie(self, nom: str, statut: str, **details) -> None:
        """Enregistre le statut d'une voie avec l'heure et ses détails."""
        self.statuts[nom] = statut
        self.voies[nom] = {"statut": statut, "heure": _maintenant(), **details}


def _maintenant() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def empreinte_fichier(chemin: str | Path) -> str:
    """SHA-256 du fichier, lu par blocs pour ne pas charger une vidéo en mémoire."""
    sha = hashlib.sha256()
    with open(chemin, "rb") as f:
        while bloc := f.read(TAILLE_BLOC):
            sha.update(bloc)
    return sha.hexdigest()


def verifier_empreinte(chemin: str | Path, attendue: str) -> bool:
    """Compare l'empreinte actuelle du fichier à celle notée à la collecte."""
    actuelle = empreinte_fichier(chemin)
    return hmac.compare_digest(actuelle, attendue.strip().lower())


def _verifier_url(url: str) -> None:
    """N'accepte que du http(s) : pas de file://, pas d'option déguisée en URL."""
    analyse = urlparse(url)
    if analyse.scheme not in ("http", "https") or not analyse.netloc:
        raise ValueError(f"URL non valide (http ou https attendu) : {url!r}")


def _nom_sur(url: str) -> str:
    """Nom de fichier sûr dérivé de l'URL (hôte + chemin), borné en longueur."""
    analyse = urlparse(url)
    brut = f"{analyse.netloc}{analyse.path}".strip("/")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", brut)[:80] or "page"


# Messages de yt-dlp qui disent que la vidéo n'existe plus : réessayer ne servirait à rien.
_ABSENCE_DEFINITIVE = re.compile(
    r"unavailable|private video|has been removed|does not exist|not found|404|deleted", re.I
)


def _commande_ytdlp(piece: PieceArchivee, dossier: Path, format_video: str, usurper: bool) -> list[str]:
    """Liste d'arguments yt-dlp (jamais de shell). `usurper` : se fait passer pour Chrome.

    L'usurpation passe par le module Python `yt_dlp` (l'exécutable autonome n'embarque
    pas toujours curl_cffi, que le module trouve dans le même environnement).
    """
    modele = str(dossier / "%(extractor)s_%(id)s.%(ext)s")
    base = [sys.executable, "-m", "yt_dlp"] if usurper else ["yt-dlp"]
    return [
        *base,
        "--ignore-config",  # ne pas hériter d'un fichier de config local
        *(["--impersonate", "chrome"] if usurper else []),
        "--no-playlist",
        "--restrict-filenames",
        "--no-progress",
        "--write-info-json",  # métadonnées (auteur, date, description) à côté du fichier
        "-f", format_video,
        "-o", modele,
        "--print", "after_move:filepath",  # affiche le chemin final du fichier
        "--no-simulate",  # --print seul ne télécharge pas
        "--", piece.url,  # « -- » : l'URL ne peut pas être prise pour une option
    ]


def _lancer_ytdlp(piece: PieceArchivee, commande: list[str], delai: int) -> tuple[Path | None, str, bool]:
    """Exécute yt-dlp. Renvoie (fichier ou None, message d'erreur, réessai utile)."""
    try:
        resultat = subprocess.run(
            commande, capture_output=True, text=True, timeout=delai, check=False
        )
    except FileNotFoundError:
        return None, "yt-dlp introuvable sur cette machine", False
    except subprocess.TimeoutExpired:
        return None, f"yt-dlp interrompu après {delai} s", False

    lignes = [ligne.strip() for ligne in resultat.stdout.splitlines() if ligne.strip()]
    erreur = (resultat.stderr or "").strip().splitlines()
    derniere = erreur[-1] if erreur else "aucune sortie"
    if resultat.returncode != 0 or not lignes:
        return None, f"yt-dlp code {resultat.returncode} : {derniere}", not _ABSENCE_DEFINITIVE.search(derniere)
    fichier = Path(lignes[-1])
    if not fichier.is_file():
        return None, f"fichier annoncé par yt-dlp absent : {fichier}", False
    return fichier, "", False


def _telecharger_video(piece: PieceArchivee, dossier: Path, format_video: str, delai: int) -> None:
    """Télécharge la vidéo avec yt-dlp ; en cas de blocage, réessaie en usurpant Chrome."""
    piece.noter(f"yt-dlp lancé (format {format_video}, délai {delai} s)")
    fichier, erreur, reessayer = _lancer_ytdlp(
        piece, _commande_ytdlp(piece, dossier, format_video, usurper=False), delai
    )
    if fichier is None and reessayer:
        piece.noter(f"{erreur}")
        piece.noter("nouvel essai en se faisant passer pour Chrome (--impersonate chrome)")
        fichier, erreur, _ = _lancer_ytdlp(
            piece, _commande_ytdlp(piece, dossier, format_video, usurper=True), delai
        )
        if fichier is not None:
            piece.noter("téléchargement réussi avec l'usurpation Chrome")
    if fichier is None and "format is not available" in erreur:
        piece.noter(erreur)
        piece.noter("format unique indisponible : nouvel essai avec fusion image + son (ffmpeg)")
        fichier, erreur, _ = _lancer_ytdlp(
            piece, _commande_ytdlp(piece, dossier, FORMAT_AVEC_FUSION, usurper=False), delai
        )
    if fichier is None:
        piece.statuts[ETAPE_TELECHARGEMENT] = ECHEC
        piece.noter(erreur)
        return

    piece.chemin_fichier = str(fichier)
    infos = fichier.with_suffix(".info.json")
    if infos.is_file():
        piece.chemin_metadonnees = str(infos)
    else:
        piece.noter("métadonnées .info.json non trouvées")
    piece.statuts[ETAPE_TELECHARGEMENT] = OK
    piece.noter(f"vidéo enregistrée : {fichier.name}")


def _calculer_empreinte(piece: PieceArchivee) -> None:
    """Empreinte du fichier collecté (vidéo ou page), si on en a un."""
    if not piece.chemin_fichier:
        piece.statuts[ETAPE_EMPREINTE] = IGNORE
        piece.noter("empreinte non calculée : pas de fichier local")
        return
    try:
        piece.empreinte_sha256 = empreinte_fichier(piece.chemin_fichier)
    except OSError as erreur:
        piece.statuts[ETAPE_EMPREINTE] = ECHEC
        piece.noter(f"empreinte impossible : {erreur}")
        return
    piece.statuts[ETAPE_EMPREINTE] = OK
    piece.noter(f"SHA-256 {piece.empreinte_sha256}")


def _en_tetes_wayback() -> dict[str, str]:
    en_tetes = {"Accept": "application/json", "User-Agent": AGENT_UTILISATEUR}
    acces, secret = os.environ.get(VAR_CLE_ACCES), os.environ.get(VAR_CLE_SECRET)
    if acces and secret:
        en_tetes["Authorization"] = f"LOW {acces}:{secret}"
    return en_tetes


def demander_instantane_wayback(
    url: str,
    client: httpx.Client | None = None,
    delai: float = DELAI_WAYBACK_S,
    pause: float = PAUSE_SONDAGE_S,
) -> str:
    """Demande une capture à Save Page Now 2 et renvoie l'URL de l'instantané.

    Lève `RuntimeError` si la capture est refusée ou n'aboutit pas dans le délai.
    Note : SPN2 peut annoncer un succès pour une capture finalement absente ;
    l'URL renvoyée est à rouvrir à la main avant de citer l'instantané.
    """
    propre = client is None
    client = client or httpx.Client(timeout=DELAI_HTTP_S)
    en_tetes = _en_tetes_wayback()
    try:
        reponse = client.post(
            URL_WAYBACK_SAUVER,
            data={"url": url, "if_not_archived_within": "1d"},
            headers=en_tetes,
        )
        reponse.raise_for_status()
        demande = reponse.json()
        if "job_id" not in demande:
            # Refus dès la demande (limite quotidienne, URL invalide...).
            raise RuntimeError(demande.get("message") or demande.get("status_ext") or str(demande))

        echeance = time.monotonic() + delai
        while True:
            etat = client.get(URL_WAYBACK_STATUT + demande["job_id"], headers=en_tetes).json()
            if etat.get("status") == "success":
                original = etat.get("original_url", url)
                return f"https://web.archive.org/web/{etat['timestamp']}/{original}"
            if etat.get("status") == "error":
                raise RuntimeError(etat.get("message") or etat.get("status_ext") or "erreur Wayback")
            if time.monotonic() >= echeance:
                raise RuntimeError(f"capture Wayback non terminée après {delai:.0f} s")
            time.sleep(pause)
    finally:
        if propre:
            client.close()


def _cles_ia_presentes() -> bool:
    return bool(os.environ.get(VAR_CLE_ACCES) and os.environ.get(VAR_CLE_SECRET))


def sauver_wayback_anonyme(
    url: str,
    client: httpx.Client | None = None,
    essais: int = ESSAIS_WAYBACK,
    attente_defaut: float = ATTENTE_429_DEFAUT_S,
    dormir=time.sleep,
) -> str:
    """Capture via `https://web.archive.org/save/<url>`, sans compte, et renvoie l'URL de l'instantané.

    Sur une réponse 429, attend la durée de `Retry-After` (sinon `attente_defaut`, croissante)
    puis réessaie, `essais` fois au plus. Lève `RuntimeError` si rien n'aboutit.
    """
    propre = client is None
    client = client or httpx.Client(timeout=DELAI_WAYBACK_S)
    derniere = "aucune réponse"
    try:
        for numero in range(1, essais + 1):
            reponse = client.get(
                f"{URL_WAYBACK_SAUVER}/{url}",
                headers={"User-Agent": AGENT_UTILISATEUR},
                follow_redirects=True,
            )
            if reponse.status_code == 429:
                brut = reponse.headers.get("retry-after", "")
                attente = float(brut) if brut.replace(".", "", 1).isdigit() else attente_defaut * numero
                attente = min(attente, ATTENTE_429_MAX_S)
                derniere = f"429 (trop de demandes), attente de {attente:.0f} s"
                if numero < essais:
                    dormir(attente)
                continue
            if reponse.status_code >= 400:
                raise RuntimeError(f"Wayback a répondu {reponse.status_code}")
            for candidat in (
                reponse.headers.get("content-location", ""),
                str(reponse.url),
                reponse.headers.get("link", ""),
            ):
                trouve = re.search(r"(/web/\d{14}[a-z_]*/[^\s>;\"]+)", candidat)
                if trouve:
                    return "https://web.archive.org" + trouve.group(1)
            raise RuntimeError("Wayback n'a pas indiqué d'adresse d'instantané")
        raise RuntimeError(f"Wayback refuse après {essais} essais : {derniere}")
    finally:
        if propre:
            client.close()


def chercher_capture_wayback(url: str, client: httpx.Client | None = None) -> str | None:
    """Cherche une capture déjà présente (API de disponibilité). None s'il n'y en a pas."""
    propre = client is None
    client = client or httpx.Client(timeout=DELAI_HTTP_S)
    try:
        reponse = client.get(
            URL_WAYBACK_DISPONIBLE, params={"url": url}, headers={"User-Agent": AGENT_UTILISATEUR}
        )
        reponse.raise_for_status()
        proche = (reponse.json().get("archived_snapshots") or {}).get("closest") or {}
    finally:
        if propre:
            client.close()
    if proche.get("available") and proche.get("url"):
        return proche["url"].replace("http://", "https://", 1)
    return None


def _etape_wayback(
    piece: PieceArchivee, client: httpx.Client | None, delai: float, pause: float,
    dormir=time.sleep,
) -> None:
    """Wayback : SPN2 si les clés existent, sinon sauvegarde anonyme, sinon capture existante.

    Un échec ici ne bloque jamais la suite : toute erreur est notée, aucune n'est levée.
    """
    erreurs: list[str] = []
    if _cles_ia_presentes():
        try:
            piece.url_wayback = demander_instantane_wayback(piece.url, client, delai, pause)
            piece.voie(ETAPE_WAYBACK, OK, mode="spn2_avec_cles", url=piece.url_wayback)
            piece.noter(f"instantané Wayback (SPN2) : {piece.url_wayback}")
            return
        except Exception as erreur:  # noqa: BLE001 - la suite ne doit jamais être bloquée
            erreurs.append(f"SPN2 : {erreur}")
    try:
        piece.url_wayback = sauver_wayback_anonyme(piece.url, client, dormir=dormir)
        piece.voie(ETAPE_WAYBACK, OK, mode="sauvegarde_anonyme", url=piece.url_wayback)
        piece.noter(f"instantané Wayback (anonyme) : {piece.url_wayback}")
        return
    except Exception as erreur:  # noqa: BLE001
        erreurs.append(f"anonyme : {erreur}")
    try:
        existante = chercher_capture_wayback(piece.url, client)
    except Exception as erreur:  # noqa: BLE001
        existante = None
        erreurs.append(f"disponibilité : {erreur}")
    if existante:
        piece.url_wayback = existante
        piece.voie(ETAPE_WAYBACK, OK, mode="capture_existante", url=existante, essais_en_echec=erreurs)
        piece.noter(f"capture Wayback déjà existante (pas d'instantané frais) : {existante}")
        return
    piece.voie(ETAPE_WAYBACK, ECHEC, erreurs=erreurs)
    piece.noter(f"Wayback en échec : {' ; '.join(erreurs)}")


def _chemin_relatif(chemin: str | None, dossier: Path) -> str | None:
    """Nom relatif au dossier d'archives : aucun chemin de la machine dans la fiche."""
    if not chemin:
        return chemin
    try:
        return Path(chemin).resolve().relative_to(dossier.resolve()).as_posix()
    except ValueError:
        return Path(chemin).name


def _ecrire_fiche(piece: PieceArchivee, dossier: Path, nom: str | None = None) -> Path:
    """Dépose à côté des fichiers une fiche JSON : empreinte et date, hors du fichier lui-même."""
    if nom is None:
        nom = Path(piece.chemin_fichier).stem if piece.chemin_fichier else _nom_sur(piece.url)
    fiche = dossier / f"{nom}.preuve.json"
    donnees = piece.vers_dict()
    for cle in ("chemin_fichier", "chemin_metadonnees"):
        donnees[cle] = _chemin_relatif(donnees[cle], dossier)
    fiche.write_text(json.dumps(donnees, ensure_ascii=False, indent=2), encoding="utf-8")
    return fiche


def archiver_video(
    url: str,
    dossier: str | Path = "archives",
    telecharger: bool = True,
    wayback: bool = True,
    format_video: str = FORMAT_BASSE_DEFINITION,
    delai_ytdlp: int = DELAI_YTDLP_S,
    client: httpx.Client | None = None,
    delai_wayback: float = DELAI_WAYBACK_S,
    pause: float = PAUSE_SONDAGE_S,
) -> PieceArchivee:
    """Archive une vidéo (TikTok, YouTube, X...) : fichier, métadonnées, empreinte, Wayback."""
    _verifier_url(url)
    dossier = Path(dossier)
    dossier.mkdir(parents=True, exist_ok=True)
    piece = PieceArchivee(url=url, type="video", date_collecte=_maintenant())
    piece.noter(f"collecte de {url}")

    if telecharger:
        _telecharger_video(piece, dossier, format_video, delai_ytdlp)
    else:
        piece.statuts[ETAPE_TELECHARGEMENT] = IGNORE
    _calculer_empreinte(piece)

    if wayback:
        _etape_wayback(piece, client, delai_wayback, pause)
    else:
        piece.statuts[ETAPE_WAYBACK] = IGNORE

    _ecrire_fiche(piece, dossier)
    return piece


def _capturer_html(piece: PieceArchivee, dossier: Path, client: httpx.Client | None) -> None:
    """Enregistre le HTML tel que reçu (ni interprété ni exécuté)."""
    propre = client is None
    client = client or httpx.Client(timeout=DELAI_HTTP_S)
    try:
        reponse = client.get(
            piece.url, headers={"User-Agent": AGENT_UTILISATEUR}, follow_redirects=True
        )
        reponse.raise_for_status()
        horodatage = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        fichier = dossier / f"{_nom_sur(piece.url)}_{horodatage}.html"
        fichier.write_bytes(reponse.content)
    except (httpx.HTTPError, OSError) as erreur:
        piece.statuts[ETAPE_TELECHARGEMENT] = ECHEC
        piece.noter(f"capture HTML en échec : {erreur}")
        return
    finally:
        if propre:
            client.close()
    piece.chemin_fichier = str(fichier)
    piece.statuts[ETAPE_TELECHARGEMENT] = OK
    piece.noter(f"HTML enregistré : {fichier.name} ({len(reponse.content)} octets)")


def archiver_page(
    url: str,
    dossier: str | Path = "archives",
    telecharger: bool = True,
    wayback: bool = True,
    client: httpx.Client | None = None,
    delai_wayback: float = DELAI_WAYBACK_S,
    pause: float = PAUSE_SONDAGE_S,
) -> PieceArchivee:
    """Archive une page web (post X, blog, article) : HTML brut, empreinte, Wayback."""
    _verifier_url(url)
    dossier = Path(dossier)
    dossier.mkdir(parents=True, exist_ok=True)
    piece = PieceArchivee(url=url, type="page", date_collecte=_maintenant())
    piece.noter(f"collecte de {url}")

    if telecharger:
        _capturer_html(piece, dossier, client)
    else:
        piece.statuts[ETAPE_TELECHARGEMENT] = IGNORE
    _calculer_empreinte(piece)

    if wayback:
        _etape_wayback(piece, client, delai_wayback, pause)
    else:
        piece.statuts[ETAPE_WAYBACK] = IGNORE

    _ecrire_fiche(piece, dossier)
    return piece


# --------------------------------------------------------------------------- horodatage RFC 3161
#
# Un jeton RFC 3161 est la signature, par une autorité tierce, de « cette empreinte existait
# à cette heure ». On le demande à plusieurs autorités indépendantes : aucune n'a besoin d'un
# compte, et aucune ne reçoit le fichier, seulement son empreinte SHA-256.

_OID_SHA256 = bytes.fromhex("0609608648016503040201")  # 2.16.840.1.101.3.4.2.1


def _der(etiquette: int, contenu: bytes) -> bytes:
    """Encode un élément DER (étiquette, longueur, contenu)."""
    n = len(contenu)
    if n < 0x80:
        longueur = bytes([n])
    else:
        octets = n.to_bytes((n.bit_length() + 7) // 8, "big")
        longueur = bytes([0x80 | len(octets)]) + octets
    return bytes([etiquette]) + longueur + contenu


def construire_requete_horodatage(empreinte_hex: str, nonce: int | None = None) -> bytes:
    """Requête RFC 3161 (TimeStampReq, DER) sur une empreinte SHA-256, certificat demandé.

    Écrite à la main pour ne pas dépendre d'openssl : le résultat est identique à
    `openssl ts -query -sha256 -cert`. Le nonce aléatoire empêche de rejouer une vieille réponse.
    """
    empreinte = bytes.fromhex(empreinte_hex)
    if len(empreinte) != 32:
        raise ValueError("empreinte SHA-256 de 32 octets attendue")
    if nonce is None:
        nonce = secrets.randbits(63)
    algo = _der(0x30, _OID_SHA256 + b"\x05\x00")
    imprint = _der(0x30, algo + _der(0x04, empreinte))
    entier_nonce = nonce.to_bytes(max(1, (nonce.bit_length() + 8) // 8), "big")  # signe positif
    return _der(0x30, b"\x02\x01\x01" + imprint + _der(0x02, entier_nonce) + b"\x01\x01\xff")


def _lire_tlv(donnees: bytes, debut: int = 0) -> tuple[int, int, int]:
    """Renvoie (étiquette, début du contenu, fin du contenu) de l'élément DER à `debut`."""
    etiquette = donnees[debut]
    n = donnees[debut + 1]
    pos = debut + 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(donnees[pos:pos + k], "big")
        pos += k
    return etiquette, pos, pos + n


def statut_reponse_horodatage(reponse: bytes) -> int:
    """PKIStatus d'une réponse RFC 3161 : 0 ou 1 = accordé, le reste = refusé."""
    _, debut, _ = _lire_tlv(reponse)  # TimeStampResp
    _, debut_statut, _ = _lire_tlv(reponse, debut)  # PKIStatusInfo
    _, debut_entier, fin_entier = _lire_tlv(reponse, debut_statut)  # status
    return int.from_bytes(reponse[debut_entier:fin_entier], "big")


def _heure_attestee(tsr: Path) -> str | None:
    """Heure signée dans le jeton (genTime), lue par openssl si présent, sinon dans les octets."""
    openssl = shutil.which("openssl")
    if openssl:
        sortie = subprocess.run(
            [openssl, "ts", "-reply", "-in", str(tsr), "-text"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        trouve = re.search(r"Time stamp:\s*(.+)", sortie.stdout)
        if trouve:
            brut = re.sub(r"\s+", " ", trouve.group(1).strip())
            try:
                return datetime.strptime(brut, "%b %d %H:%M:%S %Y GMT").replace(tzinfo=UTC).isoformat()
            except ValueError:
                return brut
    trouve = re.search(rb"\x18\x0f(\d{14})Z", tsr.read_bytes())
    if trouve:
        t = trouve.group(1).decode()
        return f"{t[:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:{t[10:12]}:{t[12:14]}Z"
    return None


def horodater(
    fichier: str | Path,
    autorites: tuple[tuple[str, str], ...] = AUTORITES_HORODATAGE,
    client: httpx.Client | None = None,
    minimum: int = 2,
) -> list[dict]:
    """Demande un jeton RFC 3161 sur le SHA-256 du fichier à des autorités indépendantes.

    Interroge les autorités dans l'ordre (FreeTSA, DigiCert, Sectigo) et s'arrête quand
    `minimum` jetons sont obtenus : les suivantes ne servent que de secours. Les fichiers
    `<fichier>.<autorité>.tsq` (requête) et `.tsr` (réponse signée) sont écrits à côté.
    Renvoie une entrée par autorité interrogée, réussie ou non ; ne lève rien sur un échec réseau.
    """
    fichier = Path(fichier)
    empreinte = empreinte_fichier(fichier)
    propre = client is None
    client = client or httpx.Client(timeout=DELAI_HTTP_S)
    resultats: list[dict] = []
    try:
        for nom, adresse in autorites:
            if sum(r["statut"] == OK for r in resultats) >= minimum:
                break
            entree = {
                "autorite": nom, "url_autorite": adresse, "fichier": fichier.name,
                "empreinte_sha256": empreinte, "date_demande": _maintenant(), "statut": ECHEC,
            }
            resultats.append(entree)
            try:
                requete = construire_requete_horodatage(empreinte)
                reponse = client.post(
                    adresse, content=requete, headers={
                        "Content-Type": "application/timestamp-query", "User-Agent": AGENT_UTILISATEUR,
                    },
                )
                if reponse.status_code != 200:
                    entree["erreur"] = f"HTTP {reponse.status_code}"
                    continue
                statut = statut_reponse_horodatage(reponse.content)
                if statut not in (0, 1):
                    entree["erreur"] = f"jeton refusé (PKIStatus {statut})"
                    continue
                tsq = fichier.with_name(f"{fichier.name}.{nom}.tsq")
                tsr = fichier.with_name(f"{fichier.name}.{nom}.tsr")
                tsq.write_bytes(requete)
                tsr.write_bytes(reponse.content)
                entree.update(
                    statut=OK, tsq=tsq.name, tsr=tsr.name,
                    sha256_tsr=hashlib.sha256(reponse.content).hexdigest(),
                    heure_attestee=_heure_attestee(tsr),
                )
            except (httpx.HTTPError, IndexError, ValueError, OSError, subprocess.SubprocessError) as erreur:
                entree["erreur"] = f"{type(erreur).__name__} : {erreur}"
    finally:
        if propre:
            client.close()
    return resultats


def _ca_pour(autorite: str, client: httpx.Client | None) -> Path | None:
    """Racine de confiance : le magasin certifi, plus l'autorité de FreeTSA (absente du magasin)."""
    import certifi  # dépendance de httpx : toujours présente

    morceaux = [Path(certifi.where()).read_text(encoding="utf-8")]
    if autorite == "freetsa":
        propre = client is None
        client = client or httpx.Client(timeout=DELAI_HTTP_S)
        try:
            reponse = client.get(URL_CA_FREETSA)
            reponse.raise_for_status()
            morceaux.append(reponse.text)
        except httpx.HTTPError:
            return None
        finally:
            if propre:
                client.close()
    sortie = Path(tempfile.mkdtemp(prefix="ca-")) / "ca.pem"
    sortie.write_text("\n".join(morceaux), encoding="utf-8")
    return sortie


def verifier_horodatage(
    fichier: str | Path, tsq: str | Path, tsr: str | Path,
    cafile: str | Path | None = None, autorite: str = "", client: httpx.Client | None = None,
) -> dict:
    """Vérifie un jeton avec openssl : le jeton porte-t-il bien l'empreinte de CE fichier ?

    Renvoie `{"verifie": True|False|None, "message": ...}`. `None` = vérification impossible
    ici (openssl absent) : le jeton reste valable, il faut le vérifier sur une machine qui a
    openssl (`openssl ts -verify -data <fichier> -in <tsr> -CAfile <ca> -untrusted <chaine>`).
    """
    openssl = shutil.which("openssl")
    if not openssl:
        return {
            "verifie": None,
            "message": (
                "openssl est introuvable sur cette machine : le jeton n'a pas pu être vérifié ici. "
                "Il reste valide ; installez openssl (inclus avec Git pour Windows) puis lancez : "
                f"openssl ts -verify -data {Path(fichier).name} "
                f"-in {Path(tsr).name} -CAfile <racine.pem> -untrusted <chaine.pem>"
            ),
        }
    ca = Path(cafile) if cafile else _ca_pour(autorite, client)
    if ca is None:
        return {"verifie": None, "message": "certificat racine de l'autorité indisponible (réseau)"}
    with tempfile.TemporaryDirectory(prefix="tsa-") as tmp:
        jeton, chaine = Path(tmp) / "jeton.der", Path(tmp) / "chaine.pem"
        try:
            subprocess.run([openssl, "ts", "-reply", "-in", str(tsr), "-token_out", "-out", str(jeton)],
                           capture_output=True, timeout=30, check=True)
            subprocess.run([openssl, "pkcs7", "-inform", "DER", "-in", str(jeton), "-print_certs",
                            "-out", str(chaine)], capture_output=True, timeout=30, check=True)
            resultat = subprocess.run(
                [openssl, "ts", "-verify", "-data", str(fichier),
                 "-in", str(tsr), "-CAfile", str(ca), "-untrusted", str(chaine)],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (subprocess.SubprocessError, OSError) as erreur:
            return {"verifie": False, "message": f"vérification impossible : {erreur}"}
    sortie = [ligne for ligne in (resultat.stdout + resultat.stderr).strip().splitlines() if ligne.strip()]
    verifie = resultat.returncode == 0 and "Verification: OK" in resultat.stdout
    ligne = next((x for x in sortie if x.startswith("Verification")), sortie[-1] if sortie else "aucune sortie")
    return {"verifie": verifie, "message": ligne}


# --------------------------------------------------------------------------- fiche oEmbed

_OEMBED = {
    "tiktok": ("tiktok.com", "https://www.tiktok.com/oembed"),
    "youtube": ("youtube.com", "https://www.youtube.com/oembed"),
}


def _plateforme(url: str) -> str:
    hote = (urlparse(url).hostname or "").lower()
    if hote == "youtu.be" or hote.endswith("youtube.com"):
        return "youtube"
    if hote.endswith("tiktok.com"):
        return "tiktok"
    if hote.endswith("instagram.com"):
        return "instagram"
    return "autre"


def fiche_oembed(url: str, client: httpx.Client | None = None) -> dict:
    """Fiche publique oEmbed (titre, auteur, vignette) pour TikTok et YouTube, sans clé.

    Instagram n'est pas pris en charge : son oEmbed exige un jeton d'application Meta, que
    nous ne créons pas. Pour Instagram, la capture de page et Wayback tiennent lieu de preuve.
    """
    _verifier_url(url)
    plateforme = _plateforme(url)
    base = {"plateforme": plateforme, "url": url, "date": _maintenant()}
    if plateforme == "instagram":
        return {**base, "statut": IGNORE,
                "detail": "non pris en charge : l'oEmbed Instagram exige un jeton Meta"}
    if plateforme not in _OEMBED:
        return {**base, "statut": IGNORE, "detail": "oEmbed non prévu pour cette plateforme"}
    propre = client is None
    client = client or httpx.Client(timeout=DELAI_HTTP_S)
    try:
        reponse = client.get(
            _OEMBED[plateforme][1], params={"url": url, "format": "json"},
            headers={"User-Agent": AGENT_UTILISATEUR},
        )
        reponse.raise_for_status()
        return {**base, "statut": OK, "donnees": reponse.json(),
                "vignette_url": reponse.json().get("thumbnail_url")}
    except (httpx.HTTPError, ValueError) as erreur:
        return {**base, "statut": ECHEC, "detail": f"{type(erreur).__name__} : {erreur}"}
    finally:
        if propre:
            client.close()


# --------------------------------------------------------------------------- capture de page

_SELECTEURS_LEGENDE = (
    '[data-e2e="browse-video-desc"]',  # TikTok
    '[data-e2e="video-desc"]',
    "ytd-watch-metadata #description",  # YouTube
    "h1.ytd-watch-metadata",
    "article h1",
    "main h1",
)
_MARQUES_CAPTCHA = (
    "captcha", "verify you are human", "verify to continue", "drag the slider",
    "unusual traffic", "are you a robot", "not a robot", "vérification de sécurité",
)
_MARQUES_CONNEXION = (
    "log in to continue", "sign in to continue", "log in to see", "se connecter pour",
    "connectez-vous pour", "login required", "sign in to confirm you",
)
# Fenêtre de connexion posée PAR-DESSUS une page lisible (TikTok) : on la signale, on ne la ferme pas.
_MARQUES_MODALE = ("connecte-toi à", "log in to tiktok", "se connecter |", "log in |", "log in to instagram")
# Bandeau de cookies posé par-dessus la page : signalé aussi (la capture le montre), jamais accepté.
_MARQUES_COOKIES = ("allow the use of cookies", "allow cookies from", "accepter les cookies", "avant d'accéder à")
_URL_CONNEXION = ("/login", "/accounts/login", "/i/flow/login", "consent.youtube.com", "/signin", "accounts.google.com")


def detecter_mur(url_finale: str, titre: str, texte: str, legende_trouvee: bool) -> str | None:
    """Dit si la page est un mur de connexion ou un captcha (on le constate, on ne le contourne pas).

    Renvoie la raison, ou None si la page a l'air lisible.
    """
    adresse = url_finale.lower()
    for fragment in _URL_CONNEXION:
        if fragment in adresse:
            return f"redirigé vers une page de connexion ({fragment})"
    haut = f"{titre}\n{texte[:4000]}".lower()
    for marque in _MARQUES_CAPTCHA:
        if marque in haut:
            return f"captcha détecté ({marque!r})"
    if not legende_trouvee:
        for marque in _MARQUES_CONNEXION:
            if marque in haut:
                return f"mur de connexion détecté ({marque!r})"
    return None


def capturer_page(
    url: str, dossier: str | Path = "archives", nom: str | None = None,
    attente_max: float = ATTENTE_PAGE_MAX_S, calme: float = CALME_RESEAU_S,
) -> dict:
    """Capture la page dans un vrai Chromium sans interface : PNG, MHTML et HTML rendu.

    Attend le vrai chargement : jusqu'à ce que le réseau se calme (rien en cours pendant
    `calme` s) ET qu'une légende soit apparue, au plus `attente_max` s. Si la page est un mur de
    connexion ou un captcha, le constate (`mur_connexion: True`) sans jamais le contourner.
    Playwright est une dépendance optionnelle : `pip install "enquete[archive]"` puis
    `playwright install chromium`.
    """
    _verifier_url(url)
    dossier = Path(dossier)
    dossier.mkdir(parents=True, exist_ok=True)
    nom = nom or _nom_sur(url)
    resultat: dict = {"statut": ECHEC, "date": _maintenant(), "mur_connexion": None, "fichiers": {}}
    try:
        from playwright.sync_api import Error as ErreurPlaywright
        from playwright.sync_api import sync_playwright
    except ImportError:
        resultat["detail"] = 'Playwright absent : pip install "enquete[archive]" puis playwright install chromium'
        return resultat

    try:
        with sync_playwright() as p:
            navigateur = p.chromium.launch(headless=True)
            try:
                page = navigateur.new_context(viewport={"width": 1280, "height": 900}).new_page()
                en_cours = {"n": 0, "dernier": time.monotonic()}

                def debut(_):
                    en_cours["n"] += 1

                def fin(_):
                    en_cours["n"] = max(0, en_cours["n"] - 1)
                    en_cours["dernier"] = time.monotonic()

                page.on("request", debut)
                page.on("requestfinished", fin)
                page.on("requestfailed", fin)
                reponse = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                resultat["statut_http"] = reponse.status if reponse else None

                t0 = time.monotonic()
                legende = ""
                while time.monotonic() - t0 < attente_max:
                    legende = _texte_legende(page)
                    reseau_calme = en_cours["n"] == 0 and time.monotonic() - en_cours["dernier"] >= calme
                    if legende and reseau_calme:
                        break
                    page.wait_for_timeout(300)
                resultat["attente_s"] = round(time.monotonic() - t0, 1)
                resultat["legende_trouvee"] = bool(legende)
                resultat["reseau_calme"] = en_cours["n"] == 0

                titre = page.title()
                texte = page.inner_text("body") if page.query_selector("body") else ""
                resultat["titre"] = titre[:200]
                resultat["url_finale"] = page.url
                raison = detecter_mur(page.url, titre, texte, bool(legende))
                resultat["mur_connexion"] = raison is not None
                visible = f"{titre} {texte[:3000]}".lower()
                resultat["fenetre_connexion_visible"] = any(m in visible for m in _MARQUES_MODALE)
                resultat["bandeau_cookies_visible"] = any(m in visible for m in _MARQUES_COOKIES)
                resultat["legende_extrait"] = legende[:300]
                if raison:
                    resultat["detail_mur"] = raison

                png, mhtml, html = (dossier / f"{nom}_page.{e}" for e in ("png", "mhtml", "html"))
                page.screenshot(path=str(png), full_page=False)
                cdp = page.context.new_cdp_session(page)
                mhtml.write_text(cdp.send("Page.captureSnapshot", {"format": "mhtml"})["data"], encoding="utf-8")
                html.write_text(page.content(), encoding="utf-8")
                resultat["fichiers"] = {"png": png.name, "mhtml": mhtml.name, "html": html.name}
                resultat["statut"] = OK
            finally:
                navigateur.close()
    except (ErreurPlaywright, OSError, TimeoutError) as erreur:
        resultat["detail"] = f"{type(erreur).__name__} : {str(erreur).splitlines()[0] if str(erreur) else ''}"
    return resultat


def _texte_legende(page) -> str:
    """Texte de la première légende visible, ou chaîne vide."""
    for selecteur in _SELECTEURS_LEGENDE:
        try:
            element = page.query_selector(selecteur)
            texte = element.inner_text().strip() if element else ""
        except Exception:  # noqa: BLE001 - la page peut changer pendant la lecture
            texte = ""
        if texte:
            return texte
    return ""


# --------------------------------------------------------------------------- tout enchaîner


def _telecharger_vignette(
    adresse: str, dossier: Path, nom: str, client: httpx.Client | None
) -> Path | None:
    propre = client is None
    client = client or httpx.Client(timeout=DELAI_HTTP_S)
    try:
        _verifier_url(adresse)
        reponse = client.get(adresse, headers={"User-Agent": AGENT_UTILISATEUR}, follow_redirects=True)
        reponse.raise_for_status()
        type_contenu = reponse.headers.get("content-type", "")
        extension = "png" if "png" in type_contenu else "webp" if "webp" in type_contenu else "jpg"
        chemin = dossier / f"{nom}_vignette.{extension}"
        chemin.write_bytes(reponse.content)
        return chemin
    except (httpx.HTTPError, ValueError, OSError):
        return None
    finally:
        if propre:
            client.close()


def _inventorier(piece: PieceArchivee, dossier: Path, nom: str, extras: list[tuple[Path, str]]) -> Path:
    """Empreinte de chaque fichier de preuve, puis fichier d'empreintes (ce que l'on horodate)."""
    for chemin, role in extras:
        if chemin and chemin.is_file():
            piece.fichiers.append({
                "nom": chemin.name, "role": role, "octets": chemin.stat().st_size,
                "sha256": empreinte_fichier(chemin),
            })
    liste = dossier / f"{nom}.empreintes.txt"
    liste.write_text(
        "".join(f"{f['sha256']}  {f['nom']}\n" for f in sorted(piece.fichiers, key=lambda f: f["nom"])),
        encoding="utf-8",
    )
    return liste


def archiver_piece(
    url: str,
    dossier: str | Path = "archives",
    client: httpx.Client | None = None,
    video: bool = True,
    page: bool = True,
    oembed: bool = True,
    wayback: bool = True,
    horodatage: bool = True,
    format_video: str = FORMAT_BASSE_DEFINITION,
    delai_ytdlp: int = DELAI_YTDLP_S,
    dormir=time.sleep,
) -> PieceArchivee:
    """Archive une pièce par toutes les voies sans compte et écrit un seul manifeste `.preuve.json`.

    Voies : fiche oEmbed + vignette, capture de page (PNG, MHTML, HTML), vidéo yt-dlp (avec
    réessai en Chrome), Wayback (anonyme ou SPN2), puis horodatage RFC 3161 du fichier
    d'empreintes qui recense tous les fichiers. Chaque voie est isolée : l'échec de l'une ne
    bloque pas les autres. Si la vidéo ne peut pas être téléchargée, la capture de page,
    l'oEmbed et la vignette servent de preuve (`video_non_telechargee: true`).
    """
    _verifier_url(url)
    dossier = Path(dossier)
    dossier.mkdir(parents=True, exist_ok=True)
    plateforme = _plateforme(url)
    nom = _nom_sur(url)
    piece = PieceArchivee(url=url, type="video" if video else "page", date_collecte=_maintenant())
    piece.geste_manuel = GESTE_MANUEL_ARCHIVE_TODAY
    piece.noter(f"collecte de {url} (plateforme : {plateforme})")
    extras: list[tuple[Path, str]] = []

    # 1. fiche oEmbed et vignette
    if oembed:
        fiche = fiche_oembed(url, client)
        piece.voie(ETAPE_OEMBED, fiche["statut"], plateforme=plateforme, detail=fiche.get("detail"))
        if fiche["statut"] == OK:
            chemin = dossier / f"{nom}_oembed.json"
            chemin.write_text(json.dumps(fiche, ensure_ascii=False, indent=2), encoding="utf-8")
            extras.append((chemin, "oembed"))
            if fiche.get("vignette_url"):
                vignette = _telecharger_vignette(fiche["vignette_url"], dossier, nom, client)
                if vignette:
                    extras.append((vignette, "vignette"))
        piece.noter(f"oEmbed : {fiche['statut']} {fiche.get('detail') or ''}".strip())
    else:
        piece.voie(ETAPE_OEMBED, IGNORE)

    # 2. capture de page
    if page:
        capture = capturer_page(url, dossier, nom)
        piece.voie(
            ETAPE_CAPTURE_PAGE, capture["statut"],
            **{k: v for k, v in capture.items() if k not in ("statut", "date", "fichiers")},
        )
        piece.mur_connexion = capture.get("mur_connexion")
        for role, fichier in capture["fichiers"].items():
            extras.append((dossier / fichier, f"page_{role}"))
        piece.noter(
            f"capture de page : {capture['statut']}"
            + (f", mur de connexion : {capture['detail_mur']}" if capture.get("mur_connexion") else "")
            + (f" ({capture['detail']})" if capture.get("detail") else "")
        )
    else:
        piece.voie(ETAPE_CAPTURE_PAGE, IGNORE)

    # 3. vidéo
    if video:
        _telecharger_video(piece, dossier, format_video, delai_ytdlp)
        piece.voies[ETAPE_TELECHARGEMENT] = {
            "statut": piece.statuts[ETAPE_TELECHARGEMENT], "heure": _maintenant()}
        if piece.statuts[ETAPE_TELECHARGEMENT] != OK:
            piece.video_non_telechargee = True
        elif piece.chemin_fichier:
            extras.append((Path(piece.chemin_fichier), "video"))
            if piece.chemin_metadonnees:
                extras.append((Path(piece.chemin_metadonnees), "metadonnees_ytdlp"))
    else:
        piece.statuts[ETAPE_TELECHARGEMENT] = IGNORE

    # 4. Wayback
    if wayback:
        _etape_wayback(piece, client, DELAI_WAYBACK_S, PAUSE_SONDAGE_S, dormir=dormir)
    else:
        piece.voie(ETAPE_WAYBACK, IGNORE)

    # 5. empreintes puis horodatage
    liste = _inventorier(piece, dossier, nom, extras)
    if piece.chemin_fichier:
        _calculer_empreinte(piece)
    else:
        piece.statuts[ETAPE_EMPREINTE] = OK if piece.fichiers else IGNORE
    if horodatage and piece.fichiers:
        piece.horodatages = horodater(liste, client=client)
        for jeton in piece.horodatages:
            if jeton["statut"] == OK:
                verdict = verifier_horodatage(
                    liste, dossier / jeton["tsq"], dossier / jeton["tsr"],
                    autorite=jeton["autorite"], client=client,
                )
                jeton["verification"] = verdict
        obtenus = sum(j["statut"] == OK for j in piece.horodatages)
        piece.voie(ETAPE_HORODATAGE, OK if obtenus >= 1 else ECHEC,
                   jetons_obtenus=obtenus, fichier_horodate=liste.name)
        piece.noter(f"horodatage RFC 3161 : {obtenus} jeton(s) obtenu(s)")
    else:
        piece.voie(ETAPE_HORODATAGE, IGNORE)

    # 6. manifeste unique
    piece.fichiers.append({"nom": liste.name, "role": "liste_des_empreintes",
                           "octets": liste.stat().st_size, "sha256": empreinte_fichier(liste)})
    _ecrire_fiche(piece, dossier, nom)
    return piece
