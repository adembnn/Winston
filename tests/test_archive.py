"""Tests de l'archivage : aucun réseau, aucun téléchargement réel."""

import hashlib
import json
import subprocess

import httpx
import pytest

from enquete import archive
from enquete.archive import (
    ECHEC,
    IGNORE,
    OK,
    archiver_page,
    archiver_video,
    empreinte_fichier,
    verifier_empreinte,
)

URL_VIDEO = "https://www.tiktok.com/@compte/video/7300000000000000000"
URL_PAGE = "https://exemple.org/article"


# --- empreinte -------------------------------------------------------------


def test_empreinte_correspond_a_sha256(tmp_path):
    fichier = tmp_path / "preuve.bin"
    fichier.write_bytes(b"contenu de la preuve")
    assert empreinte_fichier(fichier) == hashlib.sha256(b"contenu de la preuve").hexdigest()


def test_empreinte_par_blocs_sur_fichier_plus_grand_qu_un_bloc(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "TAILLE_BLOC", 4)
    fichier = tmp_path / "gros.bin"
    contenu = b"0123456789abcdef-reste"
    fichier.write_bytes(contenu)
    assert empreinte_fichier(fichier) == hashlib.sha256(contenu).hexdigest()


def test_verifier_empreinte_detecte_une_modification(tmp_path):
    fichier = tmp_path / "preuve.bin"
    fichier.write_bytes(b"original")
    attendue = empreinte_fichier(fichier)
    assert verifier_empreinte(fichier, attendue)
    assert verifier_empreinte(fichier, attendue.upper())
    fichier.write_bytes(b"original modifie")
    assert not verifier_empreinte(fichier, attendue)


# --- doublures de yt-dlp et de Wayback -------------------------------------


def faux_ytdlp(appels, code=0):
    """Remplace subprocess.run : crée la vidéo et ses métadonnées comme le ferait yt-dlp."""

    def run(commande, **kwargs):
        appels.append((commande, kwargs))
        if code != 0:
            return subprocess.CompletedProcess(commande, code, "", "ERROR: Video unavailable\n")
        modele = commande[commande.index("-o") + 1]
        video = modele.replace("%(extractor)s", "TikTok").replace("%(id)s", "73").replace("%(ext)s", "mp4")
        with open(video, "wb") as f:
            f.write(b"faux flux video")
        with open(video[:-4] + ".info.json", "w", encoding="utf-8") as f:
            f.write('{"uploader": "compte"}')
        return subprocess.CompletedProcess(commande, 0, video + "\n", "")

    return run


def faux_wayback(reponses_statut, refus=None):
    """Client httpx simulé : POST /save puis sondage de /save/status/<job>."""
    requetes = []
    statuts = iter(reponses_statut)

    def gestionnaire(requete: httpx.Request) -> httpx.Response:
        requetes.append(requete)
        if requete.method == "POST":
            return httpx.Response(200, json=refus or {"url": URL_VIDEO, "job_id": "job-1"})
        return httpx.Response(200, json=next(statuts))

    return httpx.Client(transport=httpx.MockTransport(gestionnaire)), requetes


SUCCES = {"status": "success", "timestamp": "20261107120000", "original_url": URL_VIDEO}


# --- vidéo -----------------------------------------------------------------


def test_video_chaine_complete(tmp_path, monkeypatch):
    appels = []
    monkeypatch.setattr(archive.subprocess, "run", faux_ytdlp(appels))
    client, _ = faux_wayback([{"status": "pending"}, SUCCES])

    piece = archiver_video(URL_VIDEO, tmp_path, client=client, pause=0)

    assert piece.statuts == {"telechargement": OK, "empreinte": OK, "wayback": OK}
    assert piece.complete
    assert piece.empreinte_sha256 == hashlib.sha256(b"faux flux video").hexdigest()
    assert piece.chemin_fichier.endswith("TikTok_73.mp4")
    assert piece.chemin_metadonnees.endswith("TikTok_73.info.json")
    assert piece.url_wayback == f"https://web.archive.org/web/20261107120000/{URL_VIDEO}"
    assert piece.date_collecte.endswith("+00:00")
    assert len(piece.journal) >= 4
    fiche = json.loads((tmp_path / "TikTok_73.preuve.json").read_text(encoding="utf-8"))
    assert fiche["empreinte_sha256"] == piece.empreinte_sha256


def test_ytdlp_appele_sans_shell_avec_delai_et_format_leger(tmp_path, monkeypatch):
    appels = []
    monkeypatch.setattr(archive.subprocess, "run", faux_ytdlp(appels))

    archiver_video(URL_VIDEO, tmp_path, wayback=False)

    commande, options = appels[0]
    assert isinstance(commande, list)
    assert not options.get("shell")
    assert options["timeout"] == archive.DELAI_YTDLP_S
    assert commande[0] == "yt-dlp"
    assert "--write-info-json" in commande
    assert commande[commande.index("-f") + 1] == archive.FORMAT_BASSE_DEFINITION
    assert commande[-2:] == ["--", URL_VIDEO]


def test_echec_ytdlp_n_empeche_pas_wayback(tmp_path, monkeypatch):
    monkeypatch.setattr(archive.subprocess, "run", faux_ytdlp([], code=1))
    client, _ = faux_wayback([SUCCES])

    piece = archiver_video(URL_VIDEO, tmp_path, client=client, pause=0)

    assert piece.statuts == {"telechargement": ECHEC, "empreinte": IGNORE, "wayback": OK}
    assert not piece.complete
    assert piece.chemin_fichier is None
    assert piece.empreinte_sha256 is None
    assert piece.url_wayback
    assert any("Video unavailable" in ligne for ligne in piece.journal)


def test_echec_wayback_n_efface_pas_le_telechargement(tmp_path, monkeypatch):
    monkeypatch.setattr(archive.subprocess, "run", faux_ytdlp([]))
    client, _ = faux_wayback([], refus={"status": "error", "message": "limite quotidienne"})

    piece = archiver_video(URL_VIDEO, tmp_path, client=client, pause=0)

    assert piece.statuts == {"telechargement": OK, "empreinte": OK, "wayback": ECHEC}
    assert piece.empreinte_sha256
    assert piece.url_wayback is None
    assert any("limite quotidienne" in ligne for ligne in piece.journal)


def test_ytdlp_absent_ou_trop_lent_est_un_echec_propre(tmp_path, monkeypatch):
    def absent(commande, **kwargs):
        raise FileNotFoundError

    def lent(commande, **kwargs):
        raise subprocess.TimeoutExpired(commande, kwargs["timeout"])

    for doublure in (absent, lent):
        monkeypatch.setattr(archive.subprocess, "run", doublure)
        piece = archiver_video(URL_VIDEO, tmp_path, wayback=False)
        assert piece.statuts["telechargement"] == ECHEC


def test_etapes_desactivees_sont_marquees_ignorees(tmp_path, monkeypatch):
    def interdit(*args, **kwargs):
        raise AssertionError("aucun appel attendu")

    monkeypatch.setattr(archive.subprocess, "run", interdit)
    piece = archiver_video(URL_VIDEO, tmp_path, telecharger=False, wayback=False)
    assert piece.statuts == {"telechargement": IGNORE, "empreinte": IGNORE, "wayback": IGNORE}
    assert piece.complete


@pytest.mark.parametrize("url", ["file:///etc/passwd", "--exec=calc", "javascript:alert(1)", ""])
def test_url_refusee_avant_tout_appel(tmp_path, url):
    with pytest.raises(ValueError):
        archiver_video(url, tmp_path)
    with pytest.raises(ValueError):
        archiver_page(url, tmp_path)


# --- Wayback ---------------------------------------------------------------


def test_wayback_attend_puis_abandonne_apres_le_delai(tmp_path):
    client, _ = faux_wayback([{"status": "pending"}] * 1000)
    with pytest.raises(RuntimeError, match="non terminée"):
        archive.demander_instantane_wayback(URL_VIDEO, client, delai=0, pause=0)


def test_wayback_envoie_les_cles_si_presentes(monkeypatch):
    monkeypatch.setenv("IA_ACCESS", "acces")
    monkeypatch.setenv("IA_SECRET", "secret")
    client, requetes = faux_wayback([SUCCES])
    archive.demander_instantane_wayback(URL_VIDEO, client, pause=0)
    assert requetes[0].headers["authorization"] == "LOW acces:secret"
    assert requetes[0].headers["accept"] == "application/json"


def test_wayback_sans_cles_n_envoie_pas_d_autorisation(monkeypatch):
    monkeypatch.delenv("IA_ACCESS", raising=False)
    monkeypatch.delenv("IA_SECRET", raising=False)
    client, requetes = faux_wayback([SUCCES])
    archive.demander_instantane_wayback(URL_VIDEO, client, pause=0)
    assert "authorization" not in requetes[0].headers


# --- page ------------------------------------------------------------------


def test_page_capture_html_empreinte_et_wayback(tmp_path):
    html = b"<html><body>post</body></html>"

    def gestionnaire(requete):
        if requete.method == "GET" and requete.url.host == "exemple.org":
            return httpx.Response(200, content=html)
        if requete.method == "POST":
            return httpx.Response(200, json={"job_id": "j"})
        return httpx.Response(200, json={**SUCCES, "original_url": URL_PAGE})

    client = httpx.Client(transport=httpx.MockTransport(gestionnaire))
    piece = archiver_page(URL_PAGE, tmp_path, client=client, pause=0)

    assert piece.type == "page"
    assert piece.statuts == {"telechargement": OK, "empreinte": OK, "wayback": OK}
    assert piece.empreinte_sha256 == hashlib.sha256(html).hexdigest()
    assert piece.chemin_fichier.endswith(".html")
    assert piece.url_wayback.endswith(URL_PAGE)


def test_page_introuvable_echoue_sans_bloquer_wayback(tmp_path):
    def gestionnaire(requete):
        if requete.method == "GET" and requete.url.host == "exemple.org":
            return httpx.Response(404)
        if requete.method == "POST":
            return httpx.Response(200, json={"job_id": "j"})
        return httpx.Response(200, json={**SUCCES, "original_url": URL_PAGE})

    client = httpx.Client(transport=httpx.MockTransport(gestionnaire))
    piece = archiver_page(URL_PAGE, tmp_path, client=client, pause=0)

    assert piece.statuts == {"telechargement": ECHEC, "empreinte": IGNORE, "wayback": OK}
    assert piece.url_wayback
