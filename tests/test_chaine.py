"""Tests de la chaîne d'enquête : Oriane simulé, archivage simulé, aucun appel réseau.

Les réponses viennent d'un vrai appel Oriane (projection full, recherche « minceur » dans la
parole), anonymisé : `tests/reponses/oriane_full_anonymise.json`. Les autres vidéos des tests en
sont dérivées (même structure, autre identifiant, autre pseudo).
"""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from enquete import chaine
from enquete.archive import PieceArchivee
from enquete.banc import evaluer, rendu_depuis_dict, corrige_depuis_dict
from enquete.chaine import (
    Budget,
    enqueter,
    main,
    normaliser_phrase,
    regrouper,
    video_depuis_resultat,
)
from enquete.sources.oriane import OrianeClient

CLE = "cle-secrete-de-test-123"
REEL = json.loads(
    (Path(__file__).parent / "reponses" / "oriane_full_anonymise.json").read_text(encoding="utf-8")
)["data"]["results"][0]
PHRASE = "Plutôt entrée, entrée plus plat en général, ça dépend."
UNE_HEURE = 3600  # une heure plus tard, en secondes


def variante(
    suffixe: str, decalage_s: int = UNE_HEURE, phrase: str | None = None,
    son: str | None = None, pseudo: str | None = None,
) -> dict:
    """Copie du vrai résultat, mise en ligne `decalage_s` secondes plus tard par un autre compte."""
    r = copy.deepcopy(REEL)
    ancien = int(r["platformId"])
    r["platformId"] = str(((ancien >> 32) + decalage_s) << 32 | (ancien & 0xFFFFFFFF))
    r["id"] = f"cnt_{suffixe}"
    r["profileHandle"] = pseudo or f"compte_{suffixe}"
    r["caption"] = f"Légende propre à {suffixe}"
    r["audioPlatformId"] = son or f"son_{suffixe}"
    r["publishedAt"] = datetime.fromtimestamp((ancien >> 32) + decalage_s, UTC).isoformat()
    chunks = r["media"][0]["transcriptChunks"]
    if phrase is not None:
        chunks[2]["text"] = phrase
    else:
        for c in chunks:
            c["text"] = f"phrase sans rapport numéro {suffixe} {c['startSeconds']}"
    return r


def en_basic(r: dict) -> dict:
    """Ce que renvoie la projection basic : ni parole horodatée, ni son."""
    gardes = ("id", "platform", "platformId", "platformType", "profileHandle", "caption",
              "publishedAt", "viewsCount", "hashtags", "profileFollowersCount")
    basic = {k: r[k] for k in gardes if k in r}
    basic["media"] = [{"type": "video", "duration": 34, "caption": None}]
    return basic


class FauxOriane:
    """Rejoue des vidéos : `basic` pour toute recherche, `full` quand le filtre `id` est présent."""

    def __init__(self, videos: list[dict]):
        self.videos = videos
        self.projections: list[str] = []

    def __call__(self, requete: httpx.Request) -> httpx.Response:
        projection = requete.url.params["projection"]
        self.projections.append(projection)
        corps = json.loads(requete.content)
        if projection == "full":
            ids = set(corps["filters"]["id"]["includes"])
            resultats = [v for v in self.videos if v["id"] in ids]
        else:
            resultats = [en_basic(v) for v in self.videos]
        return httpx.Response(200, json={
            "metadata": {"pagination": {"offset": 0, "limit": 50, "totalCount": len(resultats)}},
            "data": {"results": resultats},
        })


def client(tmp_path, faux: FauxOriane) -> OrianeClient:
    return OrianeClient(cle=CLE, dossier_cache=tmp_path / "cache", transport=httpx.MockTransport(faux))


# -------------------------------------------------------------------- briques


def test_normaliser_phrase_ignore_accents_casse_et_ponctuation():
    assert normaliser_phrase("  Plutôt ENTRÉE, entrée !! ") == "plutot entree entree"


def test_une_video_reelle_est_datee_et_son_lien_reconstruit():
    video = video_depuis_resultat(REEL)
    assert video.url == "https://www.tiktok.com/@compte_a/video/7698316626466311456"
    # La date vient de l'identifiant ; elle s'écarte de quelques secondes de la date publiée.
    assert video.date.isoformat() == "2026-10-19T10:00:15+00:00"
    assert video.publiee_le == "2026-10-19T10:00:03.000Z"
    assert video.marge_s is not None and "TikTok" in video.methode_date
    assert video.id_son == REEL["audioPlatformId"]
    assert any(extrait == PHRASE for _, extrait, _, _ in video.phrases)


def test_les_phrases_trop_courtes_ne_comptent_pas():
    video = video_depuis_resultat(REEL)
    assert all(len(norme.split()) >= chaine.MIN_MOTS_PHRASE for norme, *_ in video.phrases)
    assert not any(extrait == "J'adore !" for _, extrait, _, _ in video.phrases)


def test_un_resultat_sans_identifiant_de_plateforme_est_ecarte():
    incomplet = copy.deepcopy(REEL)
    incomplet["platformId"] = None
    assert video_depuis_resultat(incomplet) is None


def test_youtube_garde_la_date_donnee_par_oriane():
    youtube = copy.deepcopy(REEL)
    youtube.update(platform="youtube", platformId="dQw4w9WgXcQ", platformType="video")
    video = video_depuis_resultat(youtube)
    assert video.marge_s is None
    assert video.date.isoformat() == "2026-10-19T10:00:03+00:00"
    assert video.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_regroupement_par_phrase_par_son_et_isolement():
    a = video_depuis_resultat(REEL)
    b = video_depuis_resultat(variante("b", 3600, phrase="plutôt ENTRÉE entrée plus plat, en général ça dépend"))
    c = video_depuis_resultat(variante("c", 7200, son=REEL["audioPlatformId"]))
    d = video_depuis_resultat(variante("d", 9000))
    grappes = regrouper([d, c, b, a])
    assert len(grappes) == 1  # d, sans phrase ni son commun, reste seule
    grappe = grappes[0]
    assert [v.pseudo for v in grappe.videos] == ["compte_a", "compte_b", "compte_c"]  # par date
    assert {e.nature for e in grappe.elements[a.id_oriane]} == {"phrase", "son"}
    assert {e.nature for e in grappe.elements[b.id_oriane]} == {"phrase"}
    assert {e.nature for e in grappe.elements[c.id_oriane]} == {"son"}


def test_un_son_de_tendance_trop_partage_ne_fait_pas_de_grappe():
    videos = [video_depuis_resultat(variante(f"t{i}", 60 * (i + 1), son="tendance"))
              for i in range(chaine.MAX_VIDEOS_PAR_SON + 1)]
    assert regrouper(videos) == []


# -------------------------------------------------------------------- budget


def test_le_plafond_arrete_les_recherches_avant_le_depassement(tmp_path):
    faux = FauxOriane([REEL])
    with client(tmp_path, faux) as c:
        budget = Budget(c, plafond=25)
        budget.chercher("basic", filtres={"platform": {"includes": ["tiktok"]}})
        budget.chercher("basic", filtres={"platform": {"includes": ["tiktok"]}, "x": 1})
        with pytest.raises(chaine.PlafondAtteint):
            budget.chercher("basic", filtres={"platform": {"includes": ["tiktok"]}, "x": 2})
    assert budget.compteur.credits == 20
    assert budget.compteur.appels == 2
    assert budget.compteur.plafond_atteint
    assert len(faux.projections) == 2  # le troisième appel n'est jamais parti


# -------------------------------------------------------------------- chaîne complète


def archives_simulees(monkeypatch):
    """Remplace l'archivage réseau : l'empreinte est fausse mais bien formée."""
    appels = []

    def faux_archiver(url, dossier):
        appels.append(url)
        piece = PieceArchivee(url=url, type="video", date_collecte="2026-11-07T10:00:00+00:00")
        piece.empreinte_sha256 = "ab" * 32
        piece.statuts.update(telechargement="ok", empreinte="ok", wayback="echec")
        return piece

    monkeypatch.setattr(chaine, "archiver_video", faux_archiver)
    return appels


def videos_de_test() -> list[dict]:
    return [
        REEL,
        variante("b", 3600, phrase="Plutôt entrée, entrée plus plat en général, ça dépend !"),
        variante("c", 7200, son=REEL["audioPlatformId"]),
        variante("d", 9000),
    ]


def passage(tmp_path, monkeypatch, mots=("minceur",), plafond=600, nb_archives=2):
    faux = FauxOriane(videos_de_test())
    appels = archives_simulees(monkeypatch)
    with client(tmp_path, faux) as c:
        rapport = enqueter(
            "Qui diffuse ces vidéos minceur ?", list(mots), c, tmp_path / "sortie", "affaire-test",
            plafond_credits=plafond, nb_archives=nb_archives,
        )
    return rapport, faux, appels


def test_la_chaine_de_bout_en_bout(tmp_path, monkeypatch):
    rapport, faux, appels = passage(tmp_path, monkeypatch)

    # 1 mot-clé x 3 champs en basic (10 crédits) + 1 relecture full (20 crédits).
    assert faux.projections == ["basic", "basic", "basic", "full"]
    assert rapport["appels_payants"] == 4
    assert rapport["credits_depenses"] == 50
    assert not rapport["plafond_atteint"]
    assert rapport["videos_trouvees"] == 4
    assert rapport["videos_analysees"] == 4
    assert rapport["grappes"] == 1 and rapport["tailles_des_grappes"] == [3]
    assert rapport["questions_ouvertes"] == 3
    assert len(appels) == 2 and len(rapport["archives"]) == 2  # deux vidéos au plus

    export = json.loads((tmp_path / "sortie" / "reseau.json").read_text(encoding="utf-8"))
    assert export["compteurs"]["liens"] == rapport["liens"] > 0
    types = {p["type"] for d in export["dossiers"] for p in d["pieces"]}
    assert {"video", "compte", "grappe"} <= types


def test_chaque_lien_porte_url_extrait_exact_et_seconde(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    export = json.loads((tmp_path / "sortie" / "reseau.json").read_text(encoding="utf-8"))
    liens_dits = [l for l in export["liens"] if l["type_lien"] == "dit_la_meme_chose"]
    assert len(liens_dits) == 3
    for lien in export["liens"]:
        assert lien["preuves"]
        for preuve in lien["preuves"]:
            assert preuve["url"].startswith("https://www.tiktok.com/@")
            assert preuve["extrait"].strip()
    # La parole citée est l'extrait exact du morceau de transcription, avec sa seconde.
    parole = [p for l in liens_dits for p in l["preuves"] if p["extrait"].startswith("Plutôt entrée")]
    assert parole and all(p["debut_s"] == 2.3 and p["fin_s"] == 7.9 for p in parole)


def test_les_roles_distinguent_origine_et_amplificateurs(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    export = json.loads((tmp_path / "sortie" / "reseau.json").read_text(encoding="utf-8"))
    comptes = {p["libelle"]: d["role"] for d in export["dossiers"] for p in d["pieces"] if p["type"] == "compte"}
    assert comptes["@compte_a (tiktok)"] == "origine"
    assert comptes["@compte_b (tiktok)"] == "amplificateur"
    assert comptes["@compte_c (tiktok)"] == "amplificateur"
    assert comptes["@compte_d (tiktok)"] == "inconnu"
    reprises = [l for l in export["liens"] if l["type_lien"] == "reprend"]
    assert len(reprises) == 2 and all(l["oriente"] for l in reprises)


def test_les_questions_ouvertes_de_base(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    export = json.loads((tmp_path / "sortie" / "reseau.json").read_text(encoding="utf-8"))
    textes = [q["texte"] for q in export["questions_ouvertes"]]
    assert len(textes) == 3 and all(q["statut"] == "ouverte" for q in export["questions_ouvertes"])
    assert "19/10/2026" in textes[0]  # la plus ancienne vidéo repérée, mais pas « l'origine »
    assert "profite" in textes[1] and "démenti" in textes[2]


def test_l_empreinte_de_l_archive_entre_dans_les_preuves(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    export = json.loads((tmp_path / "sortie" / "reseau.json").read_text(encoding="utf-8"))
    empreintes = {p["empreinte_sha256"] for l in export["liens"] for p in l["preuves"]}
    assert "ab" * 32 in empreintes


def test_sans_archives_demandees_rien_n_est_telecharge(tmp_path, monkeypatch):
    rapport, _, appels = passage(tmp_path, monkeypatch, nb_archives=0)
    assert appels == [] and rapport["archives"] == []


def test_le_plafond_laisse_un_rapport_partiel(tmp_path, monkeypatch):
    rapport, faux, _ = passage(tmp_path, monkeypatch, plafond=25)
    assert faux.projections == ["basic", "basic"]  # 20 crédits ; la suite aurait dépassé 25
    assert rapport["plafond_atteint"] and rapport["credits_depenses"] == 20
    assert rapport["videos_analysees"] == 4  # la version basic suffit à dater et relier
    assert rapport["grappes"] == 0  # mais sans parole ni son, rien ne se regroupe


def test_un_second_passage_vient_du_cache(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    rapport, faux, _ = passage(tmp_path, monkeypatch)
    assert faux.projections == []
    assert rapport["appels_payants"] == 0 and rapport["appels_en_cache"] == 4
    assert rapport["credits_depenses"] == 0


def test_la_cle_n_apparait_dans_aucun_fichier(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    for chemin in tmp_path.rglob("*"):
        if chemin.is_file():
            assert CLE not in chemin.read_text(encoding="utf-8", errors="ignore"), chemin


# -------------------------------------------------------------------- rendu pour le banc


def test_le_rendu_du_banc_a_des_preuves_et_avoue_ses_questions(tmp_path, monkeypatch):
    passage(tmp_path, monkeypatch)
    brut = json.loads((tmp_path / "sortie" / "rendu-banc.json").read_text(encoding="utf-8"))
    assert brut["affaire_id"] == "affaire-test"
    rendu = rendu_depuis_dict(brut)
    affirmes = [f for f in rendu.faits if not f.non_etabli]
    avoues = [f for f in rendu.faits if f.non_etabli]
    assert affirmes and all(f.a_preuve for f in affirmes)
    assert len(avoues) == 3

    corrige = corrige_depuis_dict({
        "id": "affaire-test", "question": "?", "gel_donnees": "2026-11-07",
        "faits": [{"id": "F1", "phrase": "Trois vidéos reprennent la phrase plutôt entrée plat général dépend.",
                   "type": "reprise"}],
    })
    rapport = evaluer(corrige, rendu)
    assert rapport.inventes == [] and rapport.nb_retrouves == 1


# -------------------------------------------------------------------- commande


def test_la_commande_refuse_une_liste_de_mots_vide(capsys):
    with pytest.raises(SystemExit):
        main(["question", "--mots", " , "])
    assert "--mots" in capsys.readouterr().err
