"""Tests des images de l'export : tout est simulé, aucun appel réseau.

Les résultats Oriane viennent du vrai résultat full anonymisé (`oriane_full_anonymise.json`), dont
on remplace les URL d'images par des adresses de test servies par un faux serveur. Les images sont
synthétiques (un aplat, un QR code encodé par OpenCV).
"""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import cv2
import httpx
import numpy as np
import pytest

from enquete.chaine import construire_reseau, regrouper, video_depuis_resultat
from enquete.images import (
    API_COMMONS,
    API_NASA,
    API_OPENVERSE,
    ImageInvalide,
    chercher_illustration,
    decoder_qr,
    empreinte_fichier,
    fichier_genere_par_ia,
    illustrer_export,
    verifier_image,
)

REEL = json.loads(
    (Path(__file__).parent / "reponses" / "oriane_full_anonymise.json").read_text(encoding="utf-8")
)["data"]["results"][0]
SHA = "a" * 64
HOTE = "https://images.test"


def png(fond: int = 120, qr: str | None = None) -> bytes:
    image = np.full((90, 160, 3), fond, dtype=np.uint8)
    if qr:
        code = cv2.QRCodeEncoder.create().encode(qr)
        code = cv2.resize(code, None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
        code = cv2.copyMakeBorder(code, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
        image = cv2.cvtColor(code, cv2.COLOR_GRAY2BGR)
    ok, octets = cv2.imencode(".png", image)
    assert ok
    return octets.tobytes()


def video(suffixe: str, secondes_frames=(0, 2, 10), thumb: str | None = None) -> dict:
    r = copy.deepcopy(REEL)
    r["id"] = f"cnt_{suffixe}"
    r["platformId"] = str(int(r["platformId"]) + sum(map(ord, suffixe)) * 2 ** 32)
    r["profileHandle"] = f"compte_{suffixe}"
    r["thumbnailMediaUrl"] = thumb
    r["media"][0]["frames"] = [
        {"id": f"med_{suffixe}_{i}", "position": i + 1, "timestampSeconds": s,
         "onScreenTextRegions": [], "url": f"{HOTE}/{suffixe}/{s}.webp"}
        for i, s in enumerate(secondes_frames)
    ]
    return r


def export_de(resultats: list[dict]) -> dict:
    videos = [video_depuis_resultat(r) for r in resultats]
    reseau = construire_reseau(
        "minceur", videos, regrouper(videos), {}, datetime(2026, 11, 7, tzinfo=UTC)
    )
    export = reseau.exporter_pour_interface()
    reseau.fermer()
    return export


def pieces(export: dict) -> list[dict]:
    return [p for d in export["dossiers"] for p in d["pieces"]]


def une_video(export: dict) -> dict:
    return next(p for p in pieces(export) if p["type"] == "video")


def client(routes: dict, demandes: list[str] | None = None) -> httpx.Client:
    """Faux serveur : une URL complète (ou son préfixe sans paramètres) -> octets ou JSON."""
    def repondre(requete: httpx.Request) -> httpx.Response:
        complete = str(requete.url)
        sans_parametres = complete.split("?")[0]
        if demandes is not None:
            demandes.append(complete)
        corps = routes.get(complete, routes.get(sans_parametres))
        if corps is None:
            return httpx.Response(404)
        return httpx.Response(200, json=corps) if isinstance(corps, dict) else httpx.Response(200, content=corps)

    return httpx.Client(transport=httpx.MockTransport(repondre))


# --------------------------------------------------------------------------- vidéos


def test_video_prend_l_image_cle_la_plus_proche_de_la_seconde_de_la_preuve(tmp_path):
    r = video("a")
    export = export_de([r])
    une_video(export)["preuves"][0]["debut_s"] = 9.0
    routes = {f"{HOTE}/a/{s}.webp": png(fond=s * 10) for s in (0, 2, 10)}
    sortie = illustrer_export(export, {r["id"]: r}, dossier=tmp_path, client=client(routes), illustrations=False)
    image = une_video(sortie)["image"]
    assert image["type"] == "image_cle_video" and image["provenance"] == "capture"
    assert image["seconde"] == 10 and image["legende"] == "Image de la vidéo à 0:10"
    assert image["source_url"] == f"{HOTE}/a/10.webp"
    assert image["credit"] is None and image["licence"] is None
    fichier = Path(image["src"])
    assert fichier.is_file() and fichier.parent == tmp_path
    assert image["sha256"] == empreinte_fichier(fichier)
    verifier_image(image)


def test_sans_seconde_prend_la_premiere_image_et_ne_change_rien_d_autre(tmp_path):
    r = video("b")
    export = export_de([r])
    avant = copy.deepcopy(export)
    routes = {f"{HOTE}/b/{s}.webp": png() for s in (0, 2, 10)}
    sortie = illustrer_export(export, [r], dossier=tmp_path, client=client(routes), illustrations=False)
    assert export == avant  # l'entrée n'est pas modifiée
    assert une_video(sortie)["image"]["seconde"] == 0
    for d in sortie["dossiers"]:
        for cle in ("image", "image_piece_id", "silhouette"):
            d.pop(cle)
        for p in d["pieces"]:
            p.pop("image")
    sortie.pop("qr_trouves")
    sortie.pop("images_echecs")
    assert sortie == avant


def test_image_cle_expiree_passe_a_la_suivante_puis_a_la_vignette_oembed(tmp_path):
    r = video("c", secondes_frames=(0, 2))
    export = export_de([r])
    url = une_video(export)["attributs"]["url"]
    sortie = illustrer_export(export, [r], dossier=tmp_path, illustrations=False,
                              client=client({f"{HOTE}/c/2.webp": png(60)}))
    assert une_video(sortie)["image"]["seconde"] == 2

    routes = {"https://www.tiktok.com/oembed?url=" + quote(url, safe=""): {"thumbnail_url": f"{HOTE}/vignette.jpg"},
              f"{HOTE}/vignette.jpg": png(30)}
    sortie = illustrer_export(export, [r], dossier=tmp_path, client=client(routes), illustrations=False)
    image = une_video(sortie)["image"]
    assert image["type"] == "vignette_officielle" and image["seconde"] is None
    assert image["source_url"] == f"{HOTE}/vignette.jpg"


def test_rien_a_montrer_donne_image_nulle_et_silhouette(tmp_path):
    r = video("d")
    export = export_de([r])
    sortie = illustrer_export(export, [r], dossier=tmp_path, client=client({}), illustrations=False)
    assert all(p["image"] is None for p in pieces(sortie))
    for d in sortie["dossiers"]:
        assert d["image"] is None and d["silhouette"] is True and d["image_piece_id"] is None
    assert len(sortie["images_echecs"]) >= 3


def test_dossier_reprend_l_image_de_sa_piece_la_plus_solide(tmp_path):
    a, b = video("e"), video("f")
    export = export_de([a, b])
    dossier = next(d for d in export["dossiers"] if any(p["type"] == "video" for p in d["pieces"]))
    videos = [p for p in dossier["pieces"] if p["type"] == "video"]
    videos[0]["solidite"] = {"fiabilite_source": "D", "credibilite_information": 4}
    if len(videos) > 1:
        videos[1]["solidite"] = {"fiabilite_source": "A", "credibilite_information": 1}
    routes = {f"{HOTE}/{s}/{t}.webp": png(40) for s in "ef" for t in (0, 2, 10)}
    sortie = illustrer_export(export, [a, b], dossier=tmp_path, client=client(routes), illustrations=False)
    d = next(d for d in sortie["dossiers"] if d["role"] == dossier["role"])
    attendu = videos[-1]["id"]
    assert d["image_piece_id"] == attendu and d["silhouette"] is False
    assert d["image"] == next(p for p in d["pieces"] if p["id"] == attendu)["image"]
    assert any(x["silhouette"] for x in sortie["dossiers"])  # les rôles vides restent des silhouettes


# --------------------------------------------------------------------------- captures de pages


def test_capture_de_page_et_de_registre(tmp_path):
    export = export_de([video("g")])
    site = {"id": "p-site", "type": "site", "libelle": "Un blog", "role": "inconnu",
            "attributs": {"url": "https://blog.exemple.test/post/"}, "solidite": None, "preuves": []}
    societe = {"id": "p-soc", "type": "societe", "libelle": "ACME SAS", "role": "beneficiaire",
               "attributs": {}, "solidite": None,
               "preuves": [{"url": "https://annuaire-entreprises.data.gouv.fr/x", "extrait": "e"}]}
    sans = {"id": "p-vide", "type": "document", "libelle": "Doc", "role": "inconnu",
            "attributs": {"url": "https://nulle.part.test/"}, "solidite": None, "preuves": []}
    export["dossiers"][0]["pieces"] += [site, societe, sans]
    (tmp_path / "blog.png").write_bytes(png(10))
    (tmp_path / "reg.png").write_bytes(png(20))
    captures = {"https://blog.exemple.test/post": tmp_path / "blog.png",
                "https://annuaire-entreprises.data.gouv.fr/x": str(tmp_path / "reg.png")}
    sortie = illustrer_export(export, [], captures=captures, dossier=tmp_path / "img",
                              client=client({}), illustrations=False)
    par_id = {p["id"]: p for p in pieces(sortie)}
    assert par_id["p-site"]["image"]["type"] == "capture_page"
    assert par_id["p-site"]["image"]["source_url"] == "https://blog.exemple.test/post/"
    assert par_id["p-soc"]["image"]["type"] == "capture_registre"
    assert par_id["p-vide"]["image"] is None
    assert len(par_id["p-site"]["image"]["sha256"]) == 64


# --------------------------------------------------------------------------- règles


def test_regles_capture_et_illustration():
    base = {"src": "x.png", "provenance": "capture", "type": "capture_page",
            "source_url": "https://a.test/", "seconde": None, "sha256": SHA,
            "legende": "Capture", "credit": None, "licence": None}
    verifier_image(base)
    verifier_image(None)
    for cle in ("source_url", "sha256"):
        with pytest.raises(ImageInvalide):
            verifier_image({**base, cle: None})
    for mauvais in ({"sha256": "pas-un-hash"}, {"type": "illustration"}, {"genere_par_ia": True},
                    {"source_url": "https://cdn.midjourney.com/x.png"}):
        with pytest.raises(ImageInvalide):
            verifier_image({**base, **mauvais})
    illustration = {**base, "provenance": "illustration", "type": "illustration",
                    "credit": "Jeanne", "licence": "CC BY-SA 4.0", "sha256": None}
    verifier_image(illustration)
    for cle in ("credit", "licence"):
        with pytest.raises(ImageInvalide):
            verifier_image({**illustration, cle: " "})
    with pytest.raises(ImageInvalide):
        verifier_image(illustration, dossier_avec_capture=True)


def test_fichier_avec_marqueur_ia_refuse(tmp_path):
    faux = tmp_path / "ia.png"
    faux.write_bytes(png() + b"...trainedAlgorithmicMedia...")
    assert fichier_genere_par_ia(faux)
    export = export_de([video("h")])
    export["dossiers"][0]["pieces"].append({
        "id": "p-ia", "type": "site", "libelle": "x", "role": "inconnu",
        "attributs": {"url": "https://ia.test/"}, "solidite": None, "preuves": []})
    with pytest.raises(ImageInvalide):
        illustrer_export(export, [], captures={"https://ia.test/": faux}, dossier=tmp_path / "i",
                         client=client({}), illustrations=False)
    # venue d'Oriane, une image marquée IA n'est pas gardée du tout
    r = video("h", secondes_frames=(0,))
    routes = {f"{HOTE}/h/0.webp": png() + b"c2pa"}
    sortie = illustrer_export(export_de([r]), [r], dossier=tmp_path / "j", client=client(routes),
                              illustrations=False)
    assert all(p["image"] is None for p in pieces(sortie))


# --------------------------------------------------------------------------- banques libres


def commons(index=1, licence="CC BY-SA 4.0", artiste='<a href="x">Jeanne Dupont</a>',
            mime="image/jpeg", titre="File:Studio.jpg"):
    page = {"index": index, "title": titre, "imageinfo": [{
        "thumburl": f"{HOTE}/thumb{index}.jpg", "url": f"{HOTE}/full{index}.jpg",
        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:Studio{index}.jpg", "mime": mime,
        "extmetadata": {"LicenseShortName": {"value": licence}, "Artist": {"value": artiste}}}]}
    return str(index), page


def reponse_commons(*pages) -> dict:
    return {"query": {"pages": dict(pages)}}


def openverse(**champs) -> dict:
    return {"title": "Pot de crème", "creator": "Marc", "license": "by", "license_version": "2.0",
            "url": f"{HOTE}/ov.jpg", "thumbnail": f"{HOTE}/ov-thumb.jpg",
            "foreign_landing_url": "https://flickr.test/p/1", "tags": [{"name": "cosmetique"}], **champs}


def test_commons_garde_une_image_libre_avec_auteur_et_sans_personne():
    routes = {API_COMMONS: reponse_commons(
        commons(1, licence="All rights reserved"), commons(2, artiste=""),
        commons(3, mime="image/svg+xml"), commons(4, titre="File:Portrait de femme.jpg"),
        commons(5, licence="CC BY-NC 4.0"), commons(6))}
    trouvee = chercher_illustration(["studio", "minceur"], client(routes), sources=["commons"])
    assert trouvee["credit"] == "Jeanne Dupont" and trouvee["licence"] == "CC BY-SA 4.0"
    assert trouvee["source_url"].endswith("Studio6.jpg") and trouvee["banque"] == "commons"
    assert chercher_illustration("studio", client({}), sources=["commons"]) is None


def test_ordre_des_banques_openverse_puis_nasa():
    routes = {API_COMMONS: reponse_commons(), API_OPENVERSE: {"results": [
        openverse(creator=None), openverse(license="by-nc"), openverse(title="Selfie au soleil"), openverse()]}}
    trouvee = chercher_illustration("creme", client(routes))
    assert trouvee["banque"] == "openverse" and trouvee["licence"] == "CC BY 2.0"
    assert trouvee["credit"] == "Marc" and trouvee["src"].endswith("ov-thumb.jpg")

    nasa = {"collection": {"items": [{
        "data": [{"nasa_id": "PIA1", "title": "Mars crater", "center": "JPL"}],
        "links": [{"href": f"{HOTE}/nasa.jpg", "render": "image"}]}]}}
    trouvee = chercher_illustration("mars", client({API_COMMONS: reponse_commons(), API_OPENVERSE: {"results": []},
                                                   API_NASA: nasa}))
    assert trouvee["banque"] == "nasa" and trouvee["licence"] == "Domaine public (NASA)"
    assert trouvee["credit"] == "NASA (JPL)" and trouvee["source_url"] == "https://images.nasa.gov/details/PIA1"


def test_banques_a_cle_seulement_si_la_cle_est_deja_la(monkeypatch):
    from enquete.images import API_PEXELS
    pexels = {"photos": [{"src": {"large": f"{HOTE}/px.jpg"}, "url": "https://pexels.test/1",
                          "photographer": "Lou", "alt": "Une plante"}]}
    demandes: list[str] = []
    vide = {API_COMMONS: reponse_commons(), API_OPENVERSE: {"results": []}, API_NASA: {}}
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    assert chercher_illustration("plante", client({**vide, API_PEXELS: pexels}, demandes)) is None
    assert not any("pexels" in d for d in demandes)
    monkeypatch.setenv("PEXELS_API_KEY", "cle-de-test")
    trouvee = chercher_illustration("plante", client({**vide, API_PEXELS: pexels}, demandes))
    assert trouvee["banque"] == "pexels" and trouvee["credit"] == "Lou" and trouvee["licence"] == "Licence Pexels"


# --------------------------------------------------------------------------- illustration d'un dossier


def serveur_banques(demandes: list[str], extra: dict | None = None) -> httpx.Client:
    routes = {API_COMMONS: reponse_commons(commons(1)), f"{HOTE}/thumb1.jpg": png(200), **(extra or {})}
    return client(routes, demandes)


def test_illustration_active_par_defaut_marquee_et_jamais_sur_un_dossier_avec_capture(tmp_path):
    r = video("i")
    export = export_de([r])
    demandes: list[str] = []
    sortie = illustrer_export(export, [r], dossier=tmp_path, client=serveur_banques(demandes))
    for d in sortie["dossiers"]:
        image = d["image"]
        assert image["provenance"] == "illustration" and image["type"] == "illustration"
        assert image["credit"] == "Jeanne Dupont" and image["licence"] == "CC BY-SA 4.0"
        assert image["legende"].startswith("Illustration (commons)")
        assert d["image_piece_id"] is None and d["silhouette"] is False
        assert Path(image["src"]).is_file() and len(image["sha256"]) == 64
        assert all(p["image"] is None for p in d["pieces"])

    demandes.clear()
    extra = {f"{HOTE}/i/{s}.webp": png() for s in (0, 2, 10)}
    sortie = illustrer_export(export, [r], dossier=tmp_path, client=serveur_banques(demandes, extra))
    avec = [d for d in sortie["dossiers"] if any(p["image"] for p in d["pieces"])]
    assert avec
    for d in avec:
        assert d["image"]["provenance"] == "capture" and d["image_piece_id"]
    for d in sortie["dossiers"]:
        for p in d["pieces"]:
            assert p["image"] is None or p["image"]["provenance"] == "capture"  # une pièce n'a jamais d'illustration


def test_illustrations_coupees(tmp_path):
    r = video("k")
    demandes: list[str] = []
    sortie = illustrer_export(export_de([r]), [r], dossier=tmp_path, illustrations=False,
                              client=serveur_banques(demandes))
    assert not any("commons" in d for d in demandes)
    assert all(d["image"] is None and d["silhouette"] for d in sortie["dossiers"])


# --------------------------------------------------------------------------- QR codes


def test_qr_codes_des_images_cles(tmp_path):
    r = video("j", secondes_frames=(0, 5))
    export = export_de([r])
    routes = {f"{HOTE}/j/0.webp": png(), f"{HOTE}/j/5.webp": png(qr="https://paiement.exemple.test/abc")}
    # image la plus proche = 0 s : le QR de 5 s ne se voit qu'en scannant toutes les images
    sortie = illustrer_export(export, [r], dossier=tmp_path, client=client(routes), illustrations=False)
    assert sortie["qr_trouves"] == []
    sortie = illustrer_export(export, [r], dossier=tmp_path, client=client(routes), illustrations=False,
                              scanner_toutes_les_images=True)
    assert sortie["qr_trouves"] == [
        {"video_id": "cnt_j", "seconde": 5.0, "contenu": "https://paiement.exemple.test/abc"}]
    assert une_video(sortie)["image"]["seconde"] == 0
    assert decoder_qr(tmp_path / "absent.png") == []
