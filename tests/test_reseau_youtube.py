"""Tests de l'adaptateur YouTube : yt-dlp simulé, aucun appel réseau, aucun processus réel."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enquete.reseau import Preuve
from enquete.sources import reseau_youtube
from enquete.sources.reseau_youtube import (
    ANGLE_MORT,
    CAPACITES,
    PLATEFORME,
    ReseauYoutube,
    YoutubeBloqueError,
    YoutubeError,
    YoutubeIntrouvableError,
    YoutubeLimiteError,
    id_video,
    vers_preuve,
)

REPONSES = Path(__file__).parent / "reponses"
MAINTENANT = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
CLES = {"plateforme", "id", "url", "auteur", "date", "texte", "langue", "medias", "metriques", "route",
        "recupere_le", "preuve"}
LE_MONDE, AFP, EUROPE1 = "pg306PxE9O0", "ZajRuZQ6AMQ", "bwgJlWzksFo"


def charger(nom: str) -> dict:
    return json.loads((REPONSES / nom).read_text("utf-8"))


class FauxYtdlp:
    """Simule yt-dlp : répond selon la commande et garde toutes les commandes reçues."""

    def __init__(self, videos=None, erreur=None):
        self.commandes: list[list[str]] = []
        self.videos = videos if videos is not None else {
            i: charger(f"reseau_youtube_video_{i}.json") for i in (LE_MONDE, AFP, EUROPE1)}
        self.erreur = erreur  # (code, stderr) renvoyé à la place de tout

    def __call__(self, args):
        self.commandes.append(list(args))
        if self.erreur:
            return self.erreur[0], "", self.erreur[1]
        if any(a.startswith("ytsearch") for a in args):
            return 0, json.dumps(charger("reseau_youtube_recherche.json")), ""
        if "--write-comments" in args:
            return 0, json.dumps(charger("reseau_youtube_commentaires.json")), ""
        vid = args[-1].split("v=")[-1]
        if vid not in self.videos:
            return 1, "", f"ERROR: [youtube] {vid}: Video unavailable"
        return 0, json.dumps(self.videos[vid]), ""


def yt(tmp_path, faux=None, **options):
    faux = faux or FauxYtdlp()
    pauses: list[float] = []
    options.setdefault("maintenant", lambda: MAINTENANT)
    options.setdefault("dormir", pauses.append)
    r = ReseauYoutube(dossier_cache=tmp_path / "yt", executer=faux, **options)
    return r, faux, pauses


def test_constantes_du_contrat():
    assert PLATEFORME == "youtube"
    assert CAPACITES == {"chercher": True, "lire": True}
    assert "sans la parole" in ANGLE_MORT


def test_chercher_cles_exactes_ordre_et_preuve(tmp_path):
    r, _, _ = yt(tmp_path)
    fiches = r.chercher("élection présidentielle 2027", limite=10)
    assert [f["id"] for f in fiches] == [AFP, LE_MONDE, EUROPE1]  # de la plus ancienne à la plus récente
    for f in fiches:
        assert set(f) == CLES
        assert f["plateforme"] == "youtube"
        assert f["route"] == "yt-dlp"
        assert f["date"] and f["metriques"]["vues"] > 0
        assert f["preuve"]["extrait"] and len(f["preuve"]["extrait"]) <= 500
        p = vers_preuve(f)
        assert isinstance(p, Preuve)
        assert p.source_donnee == "youtube (yt-dlp)"
    monde = next(f for f in fiches if f["id"] == LE_MONDE)
    assert monde["auteur"] == "Le Monde"
    assert monde["date"].startswith("2026-10-30")
    assert monde["url"] == f"https://www.youtube.com/watch?v={LE_MONDE}"
    assert monde["metriques"] == {"vues": 49675, "likes": 415, "commentaires": 76}


def test_commandes_sures_et_pauses(tmp_path):
    r, faux, pauses = yt(tmp_path)
    r.chercher("élection présidentielle 2027", limite=3)
    recherche, *videos = faux.commandes
    assert recherche[0] == "yt-dlp"
    assert "--flat-playlist" in recherche and "-J" in recherche
    assert recherche[-1] == "ytsearch6:élection présidentielle 2027"  # 2 x limite
    assert len(videos) == 3
    for cmd in faux.commandes:
        assert "--ignore-config" in cmd
        assert "--write-subs" not in cmd and "--write-auto-subs" not in cmd
        assert not any("cookies" in a for a in cmd)
        assert "--write-thumbnail" not in cmd
    for cmd in videos:
        assert cmd[-2] == "--"  # le lien vient après les deux tirets
        assert "--skip-download" in cmd and "-J" in cmd
    assert pauses and all(1.0 < p <= 2.0 for p in pauses)


def test_recherche_plafonnee_a_100(tmp_path):
    r, faux, _ = yt(tmp_path)
    r.chercher("x", limite=80)
    assert faux.commandes[0][-1] == "ytsearch100:x"


def test_limite_coupe_la_lecture(tmp_path):
    r, faux, _ = yt(tmp_path)
    fiches = r.chercher("élection", limite=2)
    assert len(fiches) == 2
    assert len(faux.commandes) == 1 + 2  # la troisième vidéo n'est pas lue


def test_gel_ecarte_les_fiches_posterieures_et_vide_les_metriques(tmp_path):
    r, _, _ = yt(tmp_path, date_gel="2026-10-29")
    fiches = r.chercher("élection", limite=10)
    assert [f["id"] for f in fiches] == [AFP]
    assert fiches[0]["metriques"] == {}
    raisons = {(j["url"].split("v=")[-1], j["raison"]) for j in r.journal}
    assert raisons == {(LE_MONDE, "apres_gel"), (EUROPE1, "apres_gel")}
    assert all(set(j) == {"url", "raison", "date"} for j in r.journal)


def test_gel_seul_vaut_fin_de_journee(tmp_path):
    r, _, _ = yt(tmp_path, date_gel="2026-10-30")
    assert LE_MONDE in [f["id"] for f in r.chercher("élection")]


def test_gel_ecarte_la_fiche_sans_date(tmp_path):
    video = charger(f"reseau_youtube_video_{LE_MONDE}.json")
    video.update(timestamp=None, upload_date=None)
    r, _, _ = yt(tmp_path, FauxYtdlp(videos={LE_MONDE: video}), date_gel="2026-12-31")
    assert r.chercher("élection") == []
    assert ("sans_date_sous_gel") in [j["raison"] for j in r.journal]
    # sans gel, la même fiche passe avec date None
    r2, _, _ = yt(tmp_path / "autre", FauxYtdlp(videos={LE_MONDE: video}))
    f = r2.chercher("élection")[0]
    assert f["date"] is None and f["metriques"]["vues"] > 0


def test_date_au_jour_seulement(tmp_path):
    video = charger(f"reseau_youtube_video_{LE_MONDE}.json")
    video["timestamp"] = None
    r, _, _ = yt(tmp_path, FauxYtdlp(videos={LE_MONDE: video}))
    assert r.lire(f"https://www.youtube.com/watch?v={LE_MONDE}")["date"] == "2026-10-30"


def test_fenetre_depuis_jusqua(tmp_path):
    r, _, _ = yt(tmp_path)
    fiches = r.chercher("élection", depuis="2026-10-29", jusqua="2026-11-04")
    assert [f["id"] for f in fiches] == [LE_MONDE]
    assert {j["raison"] for j in r.journal} == {"hors_fenetre"}
    assert len(r.journal) == 2


def test_filtre_url_refuse_sans_appel_ni_cache(tmp_path):
    vus: list[str] = []
    r, faux, _ = yt(tmp_path, filtre_url=lambda u: vus.append(u) or False)
    assert r.chercher("élection présidentielle") == []
    assert vus == ["https://www.youtube.com/results?search_query=%C3%A9lection%20pr%C3%A9sidentielle"]
    assert faux.commandes == [] and r.appels_reseau == 0
    assert not (tmp_path / "yt").exists()
    assert r.journal[0]["raison"] == "filtre_url"
    with pytest.raises(YoutubeError):
        r.lire(f"https://www.youtube.com/watch?v={LE_MONDE}")
    assert faux.commandes == []


def test_filtre_url_sur_chaque_video(tmp_path):
    r, faux, _ = yt(tmp_path, filtre_url=lambda u: AFP not in u)
    ids = [f["id"] for f in r.chercher("élection")]
    assert AFP not in ids and len(ids) == 2
    assert not any(AFP in " ".join(c) for c in faux.commandes)
    assert any(j["raison"] == "filtre_url" and AFP in j["url"] for j in r.journal)


def test_cache_second_appel_sans_requete(tmp_path):
    r, faux, _ = yt(tmp_path)
    premiere = r.chercher("élection")
    n = len(faux.commandes)
    assert n == 4 and r.appels_reseau == 4
    seconde = r.chercher("élection")
    assert len(faux.commandes) == n and r.appels_reseau == 4
    assert seconde == premiere
    fichiers = list((tmp_path / "yt").glob("*.json"))
    assert len(fichiers) == 4 and not list((tmp_path / "yt").glob("*.tmp"))
    assert all(len(f.stem) == 64 for f in fichiers)  # SHA-256


def test_cache_expire(tmp_path):
    heure = [MAINTENANT]
    r, faux, _ = yt(tmp_path, maintenant=lambda: heure[0], duree_cache=60)
    r.lire(f"https://www.youtube.com/watch?v={LE_MONDE}")
    heure[0] = datetime(2026, 11, 7, 12, 5, tzinfo=UTC)
    r.lire(f"https://www.youtube.com/watch?v={LE_MONDE}")
    assert len(faux.commandes) == 2


def test_429_lance_limite_apres_un_reessai(tmp_path):
    faux = FauxYtdlp(erreur=(1, "ERROR: unable to download: HTTP Error 429: Too Many Requests"))
    r, _, pauses = yt(tmp_path, faux)
    with pytest.raises(YoutubeLimiteError) as e:
        r.lire(f"https://www.youtube.com/watch?v={LE_MONDE}")
    assert e.value.statut == 429
    assert len(faux.commandes) == 2 and 30.0 in pauses
    with pytest.raises(YoutubeLimiteError):  # première requête de chercher
        r.chercher("élection")


def test_mur_anti_robot_lance_bloque(tmp_path):
    faux = FauxYtdlp(erreur=(1, "ERROR: [youtube] x: Sign in to confirm you're not a bot"))
    r, _, _ = yt(tmp_path, faux)
    with pytest.raises(YoutubeBloqueError):
        r.lire(f"https://www.youtube.com/watch?v={LE_MONDE}")
    assert len(faux.commandes) == 1  # constaté, jamais contourné : pas de réessai


def test_video_supprimee_lance_introuvable(tmp_path):
    r, _, _ = yt(tmp_path)
    with pytest.raises(YoutubeIntrouvableError):
        r.lire("https://www.youtube.com/watch?v=inconnu12345")


def test_chercher_continue_si_une_video_manque(tmp_path):
    videos = {i: charger(f"reseau_youtube_video_{i}.json") for i in (LE_MONDE, EUROPE1)}  # AFP supprimée
    r, _, _ = yt(tmp_path, FauxYtdlp(videos=videos))
    assert {f["id"] for f in r.chercher("élection")} == {LE_MONDE, EUROPE1}
    assert [j["raison"] for j in r.journal] == ["introuvable"]


def test_chercher_s_arrete_sur_429_en_cours_de_route(tmp_path):
    base = FauxYtdlp()

    def executer(args):
        if "--skip-download" in args and EUROPE1 in args[-1]:
            return 1, "", "HTTP Error 429"
        return base(args)

    r, _, _ = yt(tmp_path, executer)
    fiches = r.chercher("élection")
    assert EUROPE1 not in [f["id"] for f in fiches]
    assert "limite" in [j["raison"] for j in r.journal]


def test_commentaires_a_la_demande(tmp_path):
    r, faux, _ = yt(tmp_path)
    assert faux.commandes == []  # chercher et lire n'en demandent jamais
    com = r.commentaires(f"https://youtu.be/{LE_MONDE}", limite=3)
    cmd = faux.commandes[0]
    assert "--write-comments" in cmd and "youtube:max_comments=3,all,0,0" in cmd and cmd[-2] == "--"
    assert len(com) == 3
    for c in com:
        assert set(c) == CLES
        assert c["route"] == "yt-dlp/commentaire"
        assert c["url"] == f"https://www.youtube.com/watch?v={LE_MONDE}&lc={c['id']}"
        assert not c["auteur"].startswith("@")
        assert vers_preuve(c).source_donnee == "youtube (yt-dlp/commentaire)"
    assert com[0]["date"] <= com[-1]["date"] and len(com[0]["date"]) == 10


def test_commentaires_sous_gel(tmp_path):
    r, _, _ = yt(tmp_path, date_gel="2026-10-04")
    com = r.commentaires(f"https://www.youtube.com/watch?v={LE_MONDE}")
    assert com == []  # tous postérieurs au gel
    assert {j["raison"] for j in r.journal} == {"apres_gel"}


def test_id_video():
    assert id_video(f"https://www.youtube.com/watch?v={LE_MONDE}&t=5s") == LE_MONDE
    assert id_video(f"https://youtu.be/{LE_MONDE}") == LE_MONDE
    assert id_video("https://m.youtube.com/shorts/abcdefghijk") == "abcdefghijk"
    assert id_video("https://example.org/watch?v=abc") is None


def test_lien_non_youtube_refuse(tmp_path):
    r, faux, _ = yt(tmp_path)
    with pytest.raises(YoutubeError):
        r.lire("https://example.org/video")
    assert faux.commandes == []


def test_ligne_de_commande(tmp_path, monkeypatch, capsys):
    faux = FauxYtdlp()
    monkeypatch.setattr(reseau_youtube, "lanceur_ytdlp", faux)
    code = reseau_youtube.main(["--cache", str(tmp_path / "c"), "lire", f"https://www.youtube.com/watch?v={LE_MONDE}"])
    sortie = capsys.readouterr()
    assert code == 0
    fiche = json.loads(sortie.out.strip().splitlines()[0])
    assert set(fiche) == CLES and fiche["auteur"] == "Le Monde"
    assert "appels_reseau=1" in sortie.err
