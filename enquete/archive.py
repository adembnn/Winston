"""Archivage d'une preuve au moment où elle entre dans le dossier.

Une vidéo ou un post peut disparaître. À l'entrée dans le dossier, on garde donc :
une copie locale, son empreinte SHA-256 (elle prouve que le fichier n'a pas bougé
depuis la collecte), la date de collecte en UTC et un instantané indépendant sur la
Wayback Machine d'Internet Archive.

Chaque étape est isolée : si le téléchargement échoue, l'instantané Wayback est quand
même tenté, et le statut de chaque étape dit ce qui a réussi.

Les fichiers téléchargés sont traités comme des données : on les lit pour calculer
l'empreinte, on ne les exécute jamais.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

# Statuts possibles d'une étape.
OK = "ok"
ECHEC = "echec"
IGNORE = "ignore"

# Étapes suivies dans `PieceArchivee.statuts`.
ETAPE_TELECHARGEMENT = "telechargement"
ETAPE_EMPREINTE = "empreinte"
ETAPE_WAYBACK = "wayback"

# Format léger : un seul fichier (pas de fusion audio/vidéo, donc pas besoin de ffmpeg),
# 480 p au plus, sinon la plus petite qualité disponible.
FORMAT_BASSE_DEFINITION = "b[height<=480]/worst"

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

    def noter(self, message: str) -> None:
        """Ajoute une ligne horodatée au journal des étapes."""
        self.journal.append(f"{_maintenant()} {message}")

    @property
    def complete(self) -> bool:
        """Vrai si aucune étape demandée n'a échoué."""
        return ECHEC not in self.statuts.values()

    def vers_dict(self) -> dict:
        return asdict(self)


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


def _telecharger_video(piece: PieceArchivee, dossier: Path, format_video: str, delai: int) -> None:
    """Appelle yt-dlp (liste d'arguments, jamais de shell) et renseigne la pièce."""
    modele = str(dossier / "%(extractor)s_%(id)s.%(ext)s")
    commande = [
        "yt-dlp",
        "--ignore-config",  # ne pas hériter d'un fichier de config local
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
    piece.noter(f"yt-dlp lancé (format {format_video}, délai {delai} s)")
    try:
        resultat = subprocess.run(
            commande, capture_output=True, text=True, timeout=delai, check=False
        )
    except FileNotFoundError:
        piece.statuts[ETAPE_TELECHARGEMENT] = ECHEC
        piece.noter("yt-dlp introuvable sur cette machine")
        return
    except subprocess.TimeoutExpired:
        piece.statuts[ETAPE_TELECHARGEMENT] = ECHEC
        piece.noter(f"yt-dlp interrompu après {delai} s")
        return

    lignes = [ligne.strip() for ligne in resultat.stdout.splitlines() if ligne.strip()]
    if resultat.returncode != 0 or not lignes:
        piece.statuts[ETAPE_TELECHARGEMENT] = ECHEC
        erreur = (resultat.stderr or "").strip().splitlines()
        piece.noter(f"yt-dlp code {resultat.returncode} : {erreur[-1] if erreur else 'aucune sortie'}")
        return

    fichier = Path(lignes[-1])
    if not fichier.is_file():
        piece.statuts[ETAPE_TELECHARGEMENT] = ECHEC
        piece.noter(f"fichier annoncé par yt-dlp absent : {fichier}")
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


def _etape_wayback(
    piece: PieceArchivee, client: httpx.Client | None, delai: float, pause: float
) -> None:
    try:
        piece.url_wayback = demander_instantane_wayback(piece.url, client, delai, pause)
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as erreur:
        piece.statuts[ETAPE_WAYBACK] = ECHEC
        piece.noter(f"Wayback en échec : {erreur}")
        return
    piece.statuts[ETAPE_WAYBACK] = OK
    piece.noter(f"instantané Wayback : {piece.url_wayback}")


def _ecrire_fiche(piece: PieceArchivee, dossier: Path) -> None:
    """Dépose à côté des fichiers une fiche JSON : empreinte et date, hors du fichier lui-même."""
    nom = Path(piece.chemin_fichier).stem if piece.chemin_fichier else _nom_sur(piece.url)
    fiche = dossier / f"{nom}.preuve.json"
    fiche.write_text(json.dumps(piece.vers_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


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
