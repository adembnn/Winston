"""Tests de l'archivage : aucun réseau, aucun téléchargement réel."""

import hashlib
import json
import subprocess
import sys

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


# =========================================================================== archivage sans compte
#
# Tout ce qui suit tourne hors réseau : clients httpx simulés, yt-dlp et openssl doublés,
# Playwright jamais lancé.


@pytest.fixture(autouse=True)
def cles_ia(monkeypatch):
    """Par défaut les clés archive.org sont présentes (voie SPN2 des tests d'origine)."""
    monkeypatch.setenv("IA_ACCESS", "acces")
    monkeypatch.setenv("IA_SECRET", "secret")


def faux_jeton(statut=0):
    """TimeStampResp minimal : suffit à tester le statut, pas la signature."""
    return archive._der(0x30, archive._der(0x30, archive._der(0x02, bytes([statut]))))


# --- requête et jeton RFC 3161 ---------------------------------------------


def test_requete_horodatage_est_un_der_valide_sur_l_empreinte():
    empreinte = hashlib.sha256(b"preuve").hexdigest()
    requete = archive.construire_requete_horodatage(empreinte, nonce=42)
    assert requete[0] == 0x30
    _, debut, fin = archive._lire_tlv(requete)
    assert fin == len(requete)  # la longueur annoncée couvre tout
    assert bytes.fromhex(empreinte) in requete
    assert bytes.fromhex("0609608648016503040201") in requete  # OID SHA-256
    assert requete.endswith(b"\x01\x01\xff")  # certReq : on demande le certificat


def test_requete_horodatage_refuse_une_empreinte_de_mauvaise_taille():
    with pytest.raises(ValueError):
        archive.construire_requete_horodatage("abcd")


def test_statut_reponse_horodatage():
    assert archive.statut_reponse_horodatage(faux_jeton(0)) == 0
    assert archive.statut_reponse_horodatage(faux_jeton(2)) == 2


def faux_tsa(comportements):
    """Client dont chaque autorité répond selon son hôte : 'ok', 'refus' ou un code HTTP."""
    appels = []

    def gestionnaire(requete: httpx.Request) -> httpx.Response:
        appels.append(requete.url.host)
        comportement = comportements[requete.url.host]
        assert requete.headers["content-type"] == "application/timestamp-query"
        if comportement == "ok":
            return httpx.Response(200, content=faux_jeton(0))
        if comportement == "refus":
            return httpx.Response(200, content=faux_jeton(2))
        return httpx.Response(comportement)

    return httpx.Client(transport=httpx.MockTransport(gestionnaire)), appels


def test_horodater_s_arrete_a_deux_jetons_et_ecrit_tsq_tsr(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "_heure_attestee", lambda tsr: None)
    fichier = tmp_path / "preuve.bin"
    fichier.write_bytes(b"contenu")
    client, appels = faux_tsa({"freetsa.org": "ok", "timestamp.digicert.com": "ok", "timestamp.sectigo.com": "ok"})

    jetons = archive.horodater(fichier, client=client)

    assert appels == ["freetsa.org", "timestamp.digicert.com"]  # Sectigo reste en secours
    assert [j["statut"] for j in jetons] == [OK, OK]
    assert (tmp_path / "preuve.bin.freetsa.tsq").is_file()
    assert (tmp_path / "preuve.bin.freetsa.tsr").read_bytes() == faux_jeton(0)
    assert jetons[0]["empreinte_sha256"] == hashlib.sha256(b"contenu").hexdigest()
    assert jetons[0]["autorite"] == "freetsa"


def test_horodater_bascule_sur_sectigo_quand_une_autorite_echoue(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "_heure_attestee", lambda tsr: None)
    fichier = tmp_path / "preuve.bin"
    fichier.write_bytes(b"contenu")
    client, appels = faux_tsa({"freetsa.org": 503, "timestamp.digicert.com": "refus", "timestamp.sectigo.com": "ok"})

    jetons = archive.horodater(fichier, client=client)

    assert appels == ["freetsa.org", "timestamp.digicert.com", "timestamp.sectigo.com"]
    assert [j["statut"] for j in jetons] == [ECHEC, ECHEC, OK]
    assert jetons[0]["erreur"] == "HTTP 503"
    assert "refusé" in jetons[1]["erreur"]
    assert not (tmp_path / "preuve.bin.freetsa.tsr").exists()


def test_horodater_ne_leve_rien_si_le_reseau_tombe(tmp_path):
    fichier = tmp_path / "preuve.bin"
    fichier.write_bytes(b"contenu")

    def panne(requete):
        raise httpx.ConnectError("réseau coupé")

    client = httpx.Client(transport=httpx.MockTransport(panne))
    jetons = archive.horodater(fichier, client=client)
    assert jetons and all(j["statut"] == ECHEC for j in jetons)


def test_verifier_horodatage_sans_openssl_donne_un_message_clair(tmp_path, monkeypatch):
    monkeypatch.setattr(archive.shutil, "which", lambda nom: None)
    verdict = archive.verifier_horodatage(tmp_path / "f", tmp_path / "f.tsq", tmp_path / "f.tsr")
    assert verdict["verifie"] is None
    assert "openssl" in verdict["message"] and "openssl ts -verify" in verdict["message"]


# --- mur de connexion ------------------------------------------------------


@pytest.mark.parametrize(
    "url, titre, texte, legende, attendu",
    [
        ("https://www.instagram.com/accounts/login/?next=/reel/x", "Instagram", "", False, "connexion"),
        ("https://www.tiktok.com/@a/video/1", "TikTok", "Verify to continue: drag the slider", True, "captcha"),
        ("https://exemple.org/a", "Titre", "Please log in to continue", False, "connexion"),
        ("https://exemple.org/a", "Titre", "Please log in to continue", True, None),  # légende lisible
        ("https://exemple.org/a", "Article", "Un texte ordinaire", True, None),
    ],
)
def test_detecter_mur(url, titre, texte, legende, attendu):
    raison = archive.detecter_mur(url, titre, texte, legende)
    assert (raison is None) if attendu is None else (attendu in raison)


def test_capturer_page_sans_playwright_echoue_proprement(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    resultat = archive.capturer_page(URL_VIDEO, tmp_path)
    assert resultat["statut"] == ECHEC
    assert "Playwright absent" in resultat["detail"]
    assert resultat["mur_connexion"] is None


# --- oEmbed ----------------------------------------------------------------


def client_json(corps=None, code=200):
    appels = []

    def gestionnaire(requete):
        appels.append(requete)
        return httpx.Response(code, json=corps or {})

    return httpx.Client(transport=httpx.MockTransport(gestionnaire)), appels


def test_oembed_tiktok_et_youtube_sans_cle():
    client, appels = client_json({"title": "t", "author_name": "a", "thumbnail_url": "https://i.example/v.jpg"})
    fiche = archive.fiche_oembed(URL_VIDEO, client)
    assert fiche["statut"] == OK and fiche["vignette_url"] == "https://i.example/v.jpg"
    assert appels[0].url.host == "www.tiktok.com" and appels[0].url.path == "/oembed"
    assert "authorization" not in appels[0].headers

    fiche = archive.fiche_oembed("https://www.youtube.com/shorts/abc", client)
    assert fiche["plateforme"] == "youtube" and appels[1].url.host == "www.youtube.com"


def test_oembed_instagram_non_pris_en_charge_sans_appel_reseau():
    client, appels = client_json()
    fiche = archive.fiche_oembed("https://www.instagram.com/reel/abc/", client)
    assert fiche["statut"] == IGNORE and "Meta" in fiche["detail"]
    assert appels == []


def test_oembed_en_erreur_ne_leve_pas():
    client, _ = client_json(code=404)
    assert archive.fiche_oembed(URL_VIDEO, client)["statut"] == ECHEC


# --- Wayback anonyme -------------------------------------------------------

INSTANTANE = "/web/20261107120000/https://www.tiktok.com/@compte/video/7300000000000000000"


def client_save(reponses):
    """Chaque appel GET renvoie la réponse suivante (code, en-têtes)."""
    suivantes = iter(reponses)
    requetes = []

    def gestionnaire(requete):
        requetes.append(requete)
        code, en_tetes = next(suivantes)
        return httpx.Response(code, headers=en_tetes)

    return httpx.Client(transport=httpx.MockTransport(gestionnaire)), requetes


def test_wayback_anonyme_succes_lit_content_location():
    client, requetes = client_save([(200, {"content-location": INSTANTANE})])
    adresse = archive.sauver_wayback_anonyme(URL_VIDEO, client, dormir=lambda s: None)
    assert adresse == "https://web.archive.org" + INSTANTANE
    assert str(requetes[0].url) == f"https://web.archive.org/save/{URL_VIDEO}"
    assert "authorization" not in requetes[0].headers


def test_wayback_anonyme_respecte_retry_after_sur_429():
    client, requetes = client_save([(429, {"retry-after": "7"}), (429, {}), (200, {"content-location": INSTANTANE})])
    pauses = []
    adresse = archive.sauver_wayback_anonyme(URL_VIDEO, client, attente_defaut=10, dormir=pauses.append)
    assert adresse.endswith(INSTANTANE)
    assert pauses == [7, 20]  # Retry-After d'abord, puis défaut croissant (10 x 2)
    assert len(requetes) == 3


def test_wayback_anonyme_abandonne_apres_trois_429():
    client, requetes = client_save([(429, {})] * 3)
    pauses = []
    with pytest.raises(RuntimeError, match="3 essais"):
        archive.sauver_wayback_anonyme(URL_VIDEO, client, dormir=pauses.append)
    assert len(requetes) == 3 and len(pauses) == 2  # pas d'attente inutile après le dernier essai


def test_wayback_anonyme_plafonne_une_attente_demesuree():
    client, _ = client_save([(429, {"retry-after": "99999"}), (200, {"content-location": INSTANTANE})])
    pauses = []
    archive.sauver_wayback_anonyme(URL_VIDEO, client, dormir=pauses.append)
    assert pauses == [archive.ATTENTE_429_MAX_S]


def test_chercher_capture_existante():
    corps = {"archived_snapshots": {"closest": {"available": True, "url": "http://web.archive.org/web/2026/x"}}}
    client, _ = client_json(corps)
    assert archive.chercher_capture_wayback(URL_VIDEO, client) == "https://web.archive.org/web/2026/x"
    client, _ = client_json({"archived_snapshots": {}})
    assert archive.chercher_capture_wayback(URL_VIDEO, client) is None


def etape_wayback(monkeypatch, sauvegarde, existante):
    monkeypatch.delenv("IA_ACCESS")
    monkeypatch.delenv("IA_SECRET")
    monkeypatch.setattr(archive, "sauver_wayback_anonyme", sauvegarde)
    monkeypatch.setattr(archive, "chercher_capture_wayback", existante)
    piece = archive.PieceArchivee(url=URL_VIDEO, type="video", date_collecte="2026-11-07")
    archive._etape_wayback(piece, None, 1, 0)
    return piece


def test_wayback_sans_cles_utilise_la_sauvegarde_anonyme(monkeypatch):
    piece = etape_wayback(monkeypatch, lambda *a, **k: "https://web.archive.org/web/1/x", lambda *a: None)
    assert piece.statuts["wayback"] == OK and piece.voies["wayback"]["mode"] == "sauvegarde_anonyme"


def test_wayback_retombe_sur_la_capture_existante(monkeypatch):
    def refus(*a, **k):
        raise RuntimeError("429 trop de demandes")

    piece = etape_wayback(monkeypatch, refus, lambda *a: "https://web.archive.org/web/0/x")
    assert piece.statuts["wayback"] == OK
    assert piece.voies["wayback"]["mode"] == "capture_existante"
    assert piece.url_wayback.endswith("/web/0/x")


def test_wayback_tout_echoue_sans_rien_lever(monkeypatch):
    def refus(*a, **k):
        raise RuntimeError("panne")

    piece = etape_wayback(monkeypatch, refus, refus)
    assert piece.statuts["wayback"] == ECHEC
    assert piece.url_wayback is None
    assert any("panne" in ligne for ligne in piece.journal)


# --- yt-dlp : réessai en Chrome --------------------------------------------


def faux_ytdlp_bloque_puis_ok(appels, message="ERROR: [TikTok] Unable to extract: HTTP Error 403"):
    ok = faux_ytdlp(appels)

    def run(commande, **kwargs):
        if "--impersonate" in commande:
            return ok(commande, **kwargs)
        appels.append((commande, kwargs))
        return subprocess.CompletedProcess(commande, 1, "", message + "\n")

    return run


def test_ytdlp_bloque_reessaie_en_se_faisant_passer_pour_chrome(tmp_path, monkeypatch):
    appels = []
    monkeypatch.setattr(archive.subprocess, "run", faux_ytdlp_bloque_puis_ok(appels))

    piece = archiver_video(URL_VIDEO, tmp_path, wayback=False)

    assert piece.statuts["telechargement"] == OK
    assert len(appels) == 2
    premiere, seconde = appels[0][0], appels[1][0]
    assert "--impersonate" not in premiere and premiere[0] == "yt-dlp"
    assert seconde[seconde.index("--impersonate") + 1] == "chrome"
    assert seconde[-2:] == ["--", URL_VIDEO]
    assert any("Chrome" in ligne for ligne in piece.journal)


def test_ytdlp_video_disparue_ne_declenche_pas_de_reessai(tmp_path, monkeypatch):
    appels = []
    monkeypatch.setattr(archive.subprocess, "run", faux_ytdlp(appels, code=1))  # « Video unavailable »
    piece = archiver_video(URL_VIDEO, tmp_path, wayback=False)
    assert piece.statuts["telechargement"] == ECHEC
    assert len(appels) == 1


def test_ytdlp_toujours_bloque_apres_usurpation_est_un_echec(tmp_path, monkeypatch):
    def toujours_bloque(commande, **kwargs):
        return subprocess.CompletedProcess(commande, 1, "", "ERROR: HTTP Error 403\n")

    monkeypatch.setattr(archive.subprocess, "run", toujours_bloque)
    piece = archiver_video(URL_VIDEO, tmp_path, wayback=False)
    assert piece.statuts["telechargement"] == ECHEC


# --- archiver_piece : manifeste unique -------------------------------------


def client_complet():
    """Simule oEmbed, vignette, Wayback anonyme et les autorités d'horodatage."""

    def gestionnaire(requete: httpx.Request) -> httpx.Response:
        hote = requete.url.host
        if requete.url.path == "/oembed":
            return httpx.Response(200, json={"title": "t", "thumbnail_url": "https://i.example/v.jpg"})
        if hote == "i.example":
            return httpx.Response(200, content=b"image", headers={"content-type": "image/jpeg"})
        if hote == "web.archive.org":
            return httpx.Response(200, headers={"content-location": INSTANTANE})
        if requete.method == "POST":
            return httpx.Response(200, content=faux_jeton(0))
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(gestionnaire))


def doublures_archiver_piece(monkeypatch, ytdlp):
    monkeypatch.delenv("IA_ACCESS")  # Wayback passe par la sauvegarde anonyme
    monkeypatch.delenv("IA_SECRET")
    monkeypatch.setattr(archive.subprocess, "run", ytdlp)
    monkeypatch.setattr(archive, "_heure_attestee", lambda tsr: "2026-11-07T12:00:00+00:00")
    monkeypatch.setattr(archive, "verifier_horodatage", lambda *a, **k: {"verifie": True, "message": "ok"})

    def capture(url, dossier, nom=None, **kwargs):
        (dossier / f"{nom}_page.png").write_bytes(b"png")
        (dossier / f"{nom}_page.html").write_bytes(b"<html>")
        return {"statut": OK, "date": "2026-11-07", "mur_connexion": False, "titre": "t",
                "fichiers": {"png": f"{nom}_page.png", "html": f"{nom}_page.html"}}

    monkeypatch.setattr(archive, "capturer_page", capture)


def test_archiver_piece_ecrit_un_manifeste_complet(tmp_path, monkeypatch):
    doublures_archiver_piece(monkeypatch, faux_ytdlp([]))

    piece = archive.archiver_piece(URL_VIDEO, tmp_path, client=client_complet(), dormir=lambda s: None)

    manifestes = list(tmp_path.glob("*.preuve.json"))
    assert len(manifestes) == 1  # un seul manifeste
    fiche = json.loads(manifestes[0].read_text(encoding="utf-8"))
    for voie in ("oembed", "capture_page", "telechargement", "wayback", "horodatage"):
        assert fiche["statuts"][voie] == OK, voie
        assert fiche["voies"][voie]["heure"], voie
    assert fiche["video_non_telechargee"] is False
    assert "archive.today" in fiche["geste_manuel"] and "captcha" in fiche["geste_manuel"]
    assert {f["role"] for f in fiche["fichiers"]} >= {"video", "oembed", "vignette", "page_png", "liste_des_empreintes"}
    for f in fiche["fichiers"]:
        assert f["sha256"] == hashlib.sha256((tmp_path / f["nom"]).read_bytes()).hexdigest()
    assert [j["autorite"] for j in fiche["horodatages"]] == ["freetsa", "digicert"]
    assert (tmp_path / fiche["horodatages"][0]["tsr"]).is_file()
    # le fichier horodaté recense bien chaque empreinte
    liste = (tmp_path / fiche["voies"]["horodatage"]["fichier_horodate"]).read_text(encoding="utf-8")
    assert piece.empreinte_sha256 in liste
    # pas de chemin de la machine dans la fiche
    assert str(tmp_path) not in manifestes[0].read_text(encoding="utf-8")


def test_archiver_piece_video_bloquee_garde_les_autres_preuves(tmp_path, monkeypatch):
    doublures_archiver_piece(monkeypatch, faux_ytdlp([], code=1))

    piece = archive.archiver_piece(URL_VIDEO, tmp_path, client=client_complet(), dormir=lambda s: None)

    assert piece.video_non_telechargee is True
    assert piece.statuts["telechargement"] == ECHEC
    assert piece.statuts["capture_page"] == OK and piece.statuts["oembed"] == OK
    assert piece.statuts["horodatage"] == OK
    roles = {f["role"] for f in piece.fichiers}
    assert "video" not in roles and {"page_png", "vignette"} <= roles
    fiche = json.loads(next(tmp_path.glob("*.preuve.json")).read_text(encoding="utf-8"))
    assert fiche["video_non_telechargee"] is True


def test_archiver_piece_une_voie_en_panne_ne_bloque_pas_les_autres(tmp_path, monkeypatch):
    doublures_archiver_piece(monkeypatch, faux_ytdlp([]))

    def panne(requete):
        if requete.url.host == "web.archive.org" or requete.url.path.startswith("/wayback"):
            return httpx.Response(500)
        if requete.url.path == "/oembed":
            return httpx.Response(404)
        return httpx.Response(200, content=faux_jeton(0))

    client = httpx.Client(transport=httpx.MockTransport(panne))
    piece = archive.archiver_piece(URL_VIDEO, tmp_path, client=client, dormir=lambda s: None)

    assert piece.statuts["wayback"] == ECHEC and piece.statuts["oembed"] == ECHEC
    assert piece.statuts["telechargement"] == OK and piece.statuts["horodatage"] == OK


def test_archiver_piece_refuse_une_url_invalide(tmp_path):
    with pytest.raises(ValueError):
        archive.archiver_piece("file:///etc/passwd", tmp_path)


def test_ytdlp_format_unique_indisponible_retombe_sur_la_fusion(tmp_path, monkeypatch):
    ok = faux_ytdlp([])
    formats = []

    def run(commande, **kwargs):
        formats.append(commande[commande.index("-f") + 1])
        if commande[commande.index("-f") + 1] == archive.FORMAT_BASSE_DEFINITION:
            return subprocess.CompletedProcess(
                commande, 1, "", "ERROR: [youtube] x: Requested format is not available\n"
            )
        return ok(commande, **kwargs)

    monkeypatch.setattr(archive.subprocess, "run", run)
    piece = archiver_video("https://www.youtube.com/shorts/abc", tmp_path, wayback=False)
    assert piece.statuts["telechargement"] == OK
    assert formats[-1] == archive.FORMAT_AVEC_FUSION
