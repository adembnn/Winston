"""Tests de l'adaptateur Facebook : oEmbed et yt-dlp simulés, aucun appel réseau, aucun processus réel."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from enquete.reseau import Preuve
from enquete.sources import reseau_facebook, reseau_youtube
from enquete.sources.reseau_facebook import (
    ANGLE_MORT,
    CAPACITES,
    PLATEFORME,
    FacebookBloqueError,
    FacebookError,
    FacebookIntrouvableError,
    FacebookLimiteError,
    ReseauFacebook,
    lire_oembed_html,
    vers_preuve,
)

REPONSES = Path(__file__).parent / "reponses"
MAINTENANT = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
CLES = {"plateforme", "id", "url", "auteur", "date", "texte", "langue", "medias", "metriques", "route",
        "recupere_le", "preuve"}
VIDEO = "https://www.facebook.com/lefigaro/videos/2181940979381942/"
REEL = "https://www.facebook.com/reel/2181940979381942/"
POST = "https://www.facebook.com/lefigaro/posts/pfbid02abcdef"


def charger(nom: str) -> dict:
    return json.loads((REPONSES / nom).read_text("utf-8"))


def oembed_post_avec_texte() -> dict:
    return {"type": "rich", "html": (
        '<div class="fb-post" data-href="x"><blockquote cite="https://www.facebook.com/lefigaro/posts/pfbid02abcdef" '
        'class="fb-xfbml-parse-ignore"><p>Texte du post &laquo;&nbsp;exemple&nbsp;&raquo;<br />ligne deux</p>'
        'Posted by <a href="https://www.facebook.com/lefigaro">Le Figaro</a> on Friday, September 4, 2026</blockquote></div>')}


class Faux:
    """Faux Graph API : garde les requêtes, lève sur tout autre hôte."""

    def __init__(self, video=None, post=None, statut=200, corps=None, entetes=None):
        self.requetes: list[httpx.Request] = []
        self.video = video if video is not None else charger("reseau_facebook_oembed_video.json")
        self.post = post if post is not None else oembed_post_avec_texte()
        self.statut, self.corps, self.entetes = statut, corps, entetes or {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.url.host == "graph.facebook.com", f"hôte inattendu : {req.url.host}"
        self.requetes.append(req)
        if self.statut != 200:
            return httpx.Response(self.statut, headers=self.entetes, json=self.corps or {})
        return httpx.Response(200, json=self.video if req.url.path.endswith("oembed_video") else self.post)


class FauxYtdlp:
    def __init__(self, info=None, erreur=None):
        self.commandes: list[list[str]] = []
        self.info = info if info is not None else charger("reseau_facebook_ytdlp.json")
        self.erreur = erreur

    def __call__(self, args):
        self.commandes.append(list(args))
        if self.erreur:
            return 1, "", self.erreur
        return 0, json.dumps(self.info), ""


def fb(tmp_path, faux=None, ytdlp=None, **options):
    faux, ytdlp = faux or Faux(), ytdlp or FauxYtdlp()
    pauses: list[float] = []
    options.setdefault("maintenant", lambda: MAINTENANT)
    options.setdefault("dormir", pauses.append)
    r = ReseauFacebook(dossier_cache=tmp_path / "fb", transport=httpx.MockTransport(faux), executer=ytdlp, **options)
    return r, faux, ytdlp, pauses


def test_constantes_du_contrat():
    assert PLATEFORME == "facebook"
    assert CAPACITES == {"chercher": False, "lire": True}
    assert "connexion" in ANGLE_MORT


def test_le_lanceur_est_celui_de_youtube():
    assert reseau_facebook.ReseauYoutube is reseau_youtube.ReseauYoutube
    assert reseau_facebook.OPTIONS_SURES is reseau_youtube.OPTIONS_SURES


def test_lire_video_texte_auteur_date_vues(tmp_path):
    r, faux, ytdlp, _ = fb(tmp_path)
    f = r.lire(VIDEO)
    assert set(f) == CLES
    assert f["plateforme"] == "facebook" and f["id"] == "2181940979381942"
    assert f["auteur"] == "lefigaro"  # nom de page tiré du lien
    assert f["date"].startswith("2026-10-21T") and f["date"].endswith("+00:00")
    assert "Bruno Retailleau ne perd pas de points" in f["texte"] and "&#039;" not in f["texte"]
    assert f["metriques"] == {"vues": 13537}
    assert f["route"] == "oembed+yt-dlp"
    assert f["medias"] == [{"type": "video", "url": VIDEO}]
    assert f["preuve"]["url"].startswith("https://graph.facebook.com/v21.0/oembed_video?url=")
    assert f["preuve"]["extrait"] in f["texte"] and len(f["preuve"]["extrait"]) <= 500
    p = vers_preuve(f)
    assert isinstance(p, Preuve) and p.source_donnee == "facebook (oembed+yt-dlp)"
    # aucun jeton, aucun compte
    assert "access_token" not in str(faux.requetes[0].url) and "authorization" not in faux.requetes[0].headers
    cmd = ytdlp.commandes[0]
    assert cmd[-2:] == ["--", VIDEO] and "--skip-download" in cmd and "--ignore-config" in cmd
    assert "--write-subs" not in cmd and not any("cookies" in a for a in cmd)


def test_reel_prend_le_nom_de_page_d_oembed(tmp_path):
    r, faux, _, _ = fb(tmp_path)
    f = r.lire(REEL)
    assert faux.requetes[0].url.path.endswith("oembed_video")
    assert f["auteur"] == "Le Figaro"  # le lien /reel/ ne porte pas de page
    assert f["id"] == "2181940979381942"


def test_post_simple_sans_yt_dlp_date_au_jour(tmp_path):
    r, faux, ytdlp, _ = fb(tmp_path)
    f = r.lire(POST)
    assert faux.requetes[0].url.path.endswith("oembed_post") and ytdlp.commandes == []
    assert f["date"] == "2026-09-04" and f["route"] == "oembed" and f["metriques"] == {} and f["medias"] == []
    assert f["texte"] == "Texte du post « exemple »\nligne deux"
    assert f["auteur"] == "lefigaro" and f["id"] == "pfbid02abcdef"
    assert isinstance(vers_preuve(f), Preuve)


def test_yt_dlp_en_echec_garde_la_lecture_oembed(tmp_path):
    r, _, _, _ = fb(tmp_path, ytdlp=FauxYtdlp(erreur="ERROR: [facebook] x: Sign in to confirm you're not a bot"))
    f = r.lire(VIDEO)
    assert f["route"] == "oembed" and f["date"] == "2026-10-21" and f["metriques"] == {}
    assert [j["raison"] for j in r.journal] == ["mur"]


def test_gel_posterieur_est_ecarte_et_journalise(tmp_path):
    r, _, _, _ = fb(tmp_path, date_gel="2026-10-20")
    with pytest.raises(FacebookIntrouvableError):
        r.lire(VIDEO)
    assert [j["raison"] for j in r.journal] == ["apres_gel"]
    assert set(r.journal[0]) == {"url", "raison", "date"}


def test_gel_le_jour_meme_passe_sans_metriques(tmp_path):
    r, _, _, _ = fb(tmp_path, date_gel="2026-10-21")
    f = r.lire(VIDEO)
    assert f["metriques"] == {} and f["date"].startswith("2026-10-21")


def test_gel_ecarte_la_fiche_sans_date(tmp_path):
    sans_pied = {"type": "video", "html": '<blockquote cite="x"><p>Un texte sans pied de page.</p></blockquote>'}
    r, _, _, _ = fb(tmp_path, Faux(post=sans_pied), date_gel="2026-12-31")
    with pytest.raises(FacebookIntrouvableError):
        r.lire(POST)
    assert [j["raison"] for j in r.journal] == ["sans_date_sous_gel"]
    r2, _, _, _ = fb(tmp_path / "b", Faux(post=sans_pied))
    assert r2.lire(POST)["date"] is None  # sans gel, la fiche reste lisible


def test_ni_texte_ni_date_lance_bloque(tmp_path):
    r, _, _, _ = fb(tmp_path, Faux(post={"type": "rich", "html": '<div class="fb-post"></div>'}))
    with pytest.raises(FacebookBloqueError):
        r.lire(POST)


def test_filtre_url_refuse_sans_appel_ni_cache(tmp_path):
    r, faux, ytdlp, _ = fb(tmp_path, filtre_url=lambda u: False)
    with pytest.raises(FacebookError):
        r.lire(VIDEO)
    assert faux.requetes == [] and ytdlp.commandes == [] and r.appels_reseau == 0
    assert not (tmp_path / "fb").exists()
    assert r.journal[0]["raison"] == "filtre_url"


def test_cache_second_appel_sans_requete(tmp_path):
    r, faux, ytdlp, _ = fb(tmp_path)
    a = r.lire(VIDEO)
    assert r.appels_reseau == 2
    b = r.lire(VIDEO)
    assert r.appels_reseau == 2 and len(faux.requetes) == 1 and len(ytdlp.commandes) == 1
    assert a == b


def test_400_lance_introuvable(tmp_path):
    corps = charger("reseau_facebook_erreur_400.json")
    r, _, _, _ = fb(tmp_path, Faux(statut=400, corps=corps))
    with pytest.raises(FacebookIntrouvableError) as e:
        r.lire(POST)
    assert e.value.statut == 400


def test_erreur_oauth_lance_bloque(tmp_path):
    corps = {"error": {"message": "An access token is required to request this resource.", "type": "OAuthException",
                       "code": 190}}
    r, faux, _, _ = fb(tmp_path, Faux(statut=400, corps=corps))
    with pytest.raises(FacebookBloqueError) as e:
        r.lire(POST)
    assert "jeton" in e.value.message and len(faux.requetes) == 1


def test_oauth_dans_une_reponse_200(tmp_path):
    corps = {"error": {"message": "Application request limit reached", "type": "OAuthException", "code": 4}}
    r, _, _, _ = fb(tmp_path, Faux(post=corps))
    with pytest.raises(FacebookError):
        r.lire(POST)


def test_403_lance_bloque(tmp_path):
    r, _, _, _ = fb(tmp_path, Faux(statut=403))
    with pytest.raises(FacebookBloqueError):
        r.lire(POST)


def test_429_respecte_retry_after_puis_lance_limite(tmp_path):
    r, faux, _, pauses = fb(tmp_path, Faux(statut=429, entetes={"Retry-After": "9"}))
    with pytest.raises(FacebookLimiteError):
        r.lire(POST)
    assert len(faux.requetes) == 3 and pauses.count(9.0) == 2


def test_chercher_n_existe_pas(tmp_path):
    r, faux, _, _ = fb(tmp_path)
    with pytest.raises(FacebookError):
        r.chercher("présidentielle")
    assert faux.requetes == []


def test_lien_non_facebook_refuse(tmp_path):
    r, faux, _, _ = fb(tmp_path)
    with pytest.raises(FacebookError):
        r.lire("https://example.org/lefigaro/videos/1/")
    assert faux.requetes == []


def test_lire_oembed_html():
    r = lire_oembed_html(charger("reseau_facebook_oembed_video.json")["html"])
    assert r["auteur"] == "Le Figaro" and r["jour"] == "2026-10-21" and r["texte"].startswith("Présidentielle 2027")
    assert lire_oembed_html("") == {"texte": None, "auteur": None, "jour": None}
    assert lire_oembed_html('<div class="fb-post" data-href="x"></div>')["texte"] is None


def test_ligne_de_commande(tmp_path, monkeypatch, capsys):
    vrai_client = httpx.Client
    monkeypatch.setattr(reseau_facebook.httpx, "Client", lambda **kw: vrai_client(
        transport=httpx.MockTransport(Faux()), headers=kw.get("headers")))
    monkeypatch.setattr(reseau_youtube, "lanceur_ytdlp", FauxYtdlp())
    code = reseau_facebook.main(["--cache", str(tmp_path / "c"), "lire", VIDEO])
    sortie = capsys.readouterr()
    assert code == 0 and json.loads(sortie.out)["auteur"] == "lefigaro"
    assert "appels_reseau=2" in sortie.err
