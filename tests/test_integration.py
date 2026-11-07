"""Intégration de la chaîne : journal progressif, archivage complet, pivots, argent, images, serveur.

Premier bloc, « contrat » : ce que le raisonnement du tour 1 (`raisonnement.py`, `boucle.py`) attend
de `chaine.py`. Il échoue dès qu'un nom, un ordre d'arguments ou un format relu par le tour 1 change.
Le tour 1 n'est pas dans ce dépôt : le test fige donc ses attentes, il n'importe aucun de ses fichiers.

Aucun test ne touche le réseau : Oriane, Infra, les chaînes et l'archivage sont simulés.
"""

import inspect
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_chaine import CLE, FauxOriane, videos_de_test  # noqa: E402
from test_chaines import FauxReseau  # noqa: E402
from test_infra import MAINTENANT as MAINTENANT_INFRA  # noqa: E402
from test_infra import Faux as FauxInfra  # noqa: E402
from test_serveur import flux, get_json, post  # noqa: E402

from enquete import chaine  # noqa: E402
from enquete import pivots as piv  # noqa: E402
from enquete import serveur  # noqa: E402
from enquete.archive import PieceArchivee  # noqa: E402
from enquete.reseau import Noeud, ReseauEnquete, Role, TypeNoeud  # noqa: E402
from enquete.serveur import Serveur, duree_reelle, lire_evenements  # noqa: E402
from enquete.sources.chaines import ClientChaines  # noqa: E402
from enquete.sources.infra import Infra  # noqa: E402
from enquete.sources.oriane import OrianeClient  # noqa: E402

RACINE = Path(__file__).parent.parent


def client_oriane(tmp_path, faux) -> OrianeClient:
    return OrianeClient(cle=CLE, dossier_cache=tmp_path / "cache", transport=httpx.MockTransport(faux))


# --------------------------------------------------------------------------- contrat du tour 1


def test_contrat_les_noms_importes_par_le_tour_1_existent():
    for nom in ("CHAMPS_RECHERCHE", "Budget", "Compteur", "PlafondAtteint", "Video", "_filtres", "_resultats",
                "chercher_candidats", "normaliser_phrase", "regrouper", "relire_en_full",
                "video_depuis_resultat", "construire_reseau"):
        assert hasattr(chaine, nom), nom


def test_contrat_ordre_des_arguments():
    def noms(f):
        return list(inspect.signature(f).parameters)

    assert noms(chaine.construire_reseau)[:6] == ["question", "videos", "grappes", "archives", "collecte", "chemin"]
    assert noms(chaine.chercher_candidats) == ["budget", "mots", "depuis", "limite"]
    assert noms(chaine.relire_en_full) == ["budget", "identifiants"]
    assert chaine.Compteur().appels == 0  # se construit sans argument


class FauxBudget:
    """Ce qu'utilise le tour 1 : seulement `chercher()` et `compteur`."""

    def __init__(self, bruts):
        self.bruts = bruts
        self.compteur = chaine.Compteur()
        self.appels = []

    def chercher(self, projection, **kw):
        self.appels.append(projection)
        if projection == "full":
            ids = kw["filtres"]["id"]["includes"]
            return {"data": {"results": [self.bruts[i] for i in ids if i in self.bruts]}}
        return {"data": {"results": list(self.bruts.values())}}


def test_contrat_relire_en_full_et_chercher_candidats_n_utilisent_que_chercher():
    bruts = {v["id"]: v for v in videos_de_test()}
    budget = FauxBudget(bruts)
    candidats = chaine.chercher_candidats(budget, ["minceur"], None, 50)
    assert set(candidats) == set(bruts)
    completes = chaine.relire_en_full(budget, list(candidats))
    assert set(completes) == set(bruts)
    assert budget.appels == ["basic", "basic", "basic", "full"]


def test_contrat_construire_reseau_rend_des_comptes_libelles_et_des_videos_avec_url():
    videos = [chaine.video_depuis_resultat(v) for v in videos_de_test()]
    grappes = chaine.regrouper(videos)
    reseau = chaine.construire_reseau("Qui diffuse ?", videos, grappes, {}, datetime.now(UTC))
    noeuds = reseau.noeuds()
    libelles = {n.libelle for n in noeuds if n.type.value == "compte"}
    assert "@compte_a (tiktok)" in libelles
    urls = {n.attributs.get("url") for n in noeuds if n.type.value == "video"}
    assert "https://www.tiktok.com/@compte_a/video/7698316626466311456" in urls
    reseau.fermer()


# --------------------------------------------------------------------------- faux Oriane, faux Infra, faux explorateurs


class FauxOrianeComplet(FauxOriane):
    """Oriane simulé : contenus (basic/full), profils, et un ralenti par appel pour que le temps réel se voie."""

    def __init__(self, videos, profils=(), delai=0.0, refuse_profils=False):
        super().__init__(videos)
        self.profils = list(profils)
        self.delai = delai
        self.refuse_profils = refuse_profils
        self.requetes_profils = []

    def __call__(self, requete):
        if self.delai:
            time.sleep(self.delai)
        if requete.url.path.endswith("/profiles/search"):
            corps = json.loads(requete.content)
            self.requetes_profils.append((requete.url.params["projection"], corps))
            if self.refuse_profils:
                return httpx.Response(400, json={"error": {"message": "filtre inconnu"}})
            filtres = corps["filters"]
            plateforme = filtres["platform"]["includes"][0]
            pseudos = filtres["handle"]["exactMatch"]["values"]
            trouves = [p for p in self.profils if p["platform"] == plateforme and p["handle"] in pseudos]
            return httpx.Response(200, json={
                "metadata": {"pagination": {"offset": 0, "limit": 100, "totalCount": len(trouves)}},
                "data": {"results": trouves},
            })
        return super().__call__(requete)


def passage(tmp_path, faux=None, profils=(), dossier="sortie", **options):
    """Un passage complet de `enqueter` sur Oriane simulé. Aucune archive réelle par défaut."""
    faux = faux or FauxOrianeComplet(videos_de_test(), profils)
    options.setdefault("nb_archives", 0)
    with client_oriane(tmp_path, faux) as c:
        rapport = chaine.enqueter(
            "Qui diffuse ces vidéos minceur ?", ["minceur"], c, tmp_path / dossier, "affaire-test", **options
        )
    return rapport, faux


def lire_json(chemin):
    return json.loads(Path(chemin).read_text(encoding="utf-8"))


def pieces_de(export):
    return [p for d in export["dossiers"] for p in d["pieces"]]


def lignes_du_journal(chemin):
    bd = sqlite3.connect(chemin)
    try:
        return bd.execute("SELECT seq, date, genre, objet_id, version, motif, donnees FROM evenements ORDER BY seq").fetchall()
    finally:
        bd.close()


# --------------------------------------------------------------------------- journal progressif


def test_le_journal_s_ecrit_au_fil_de_l_enquete_a_l_heure_reelle(tmp_path):
    journal = tmp_path / "sortie" / "reseau.sqlite"
    faux = FauxOrianeComplet(videos_de_test(), delai=0.05)
    rapport, _ = passage(tmp_path, faux, chemin_journal=journal)
    assert rapport["journal"] == str(journal)

    evenements = lire_evenements(journal)
    assert evenements[0]["type"] == "question" and evenements[0]["donnees"]["centrale"] is True
    genres = [e["type"] for e in evenements if e["type"] != "compteur"]
    assert genres.index("noeud") < genres.index("lien") < len(genres) - 1
    assert genres.count("question") == 1 + 3  # la question centrale, puis les trois questions de base
    horodatages = [e["horodatage"] for e in evenements]
    assert horodatages == sorted(horodatages)
    # Quatre appels Oriane de 50 ms : la question centrale est écrite avant, les pièces après.
    assert duree_reelle(evenements) >= 0.15
    premier_noeud = next(e for e in evenements if e["type"] == "noeud")
    ecart = (datetime.fromisoformat(premier_noeud["horodatage"])
             - datetime.fromisoformat(evenements[0]["horodatage"])).total_seconds()
    assert ecart >= 0.15


def test_journal_deja_ouvert_par_le_serveur_une_seule_ligne_affaire(tmp_path):
    journal = tmp_path / "sortie" / "reseau.sqlite"
    journal.parent.mkdir(parents=True)
    question = "Qui diffuse ces vidéos minceur ?"
    ReseauEnquete(question, str(journal)).fermer()  # ce que fait `Serveur.lancer` avant la collecte
    passage(tmp_path, chemin_journal=journal)
    affaires = [ligne for ligne in lignes_du_journal(journal) if ligne[2] == "affaire"]
    assert len(affaires) == 1


def test_memes_compteurs_et_libelles_pour_le_journal_le_json_et_la_memoire(tmp_path):
    journal = tmp_path / "a" / "reseau.sqlite"
    avec, _ = passage(tmp_path / "a", chemin_journal=journal)
    sans, _ = passage(tmp_path / "b")
    with ReseauEnquete(avec["question"], str(journal)) as r:
        depuis_le_journal = r.exporter_pour_interface()
    en_json = lire_json(tmp_path / "a" / "sortie" / "reseau.json")
    en_memoire = lire_json(tmp_path / "b" / "sortie" / "reseau.json")
    assert depuis_le_journal["compteurs"] == en_json["compteurs"] == en_memoire["compteurs"]
    libelles = [sorted(p["libelle"] for p in pieces_de(e)) for e in (depuis_le_journal, en_json, en_memoire)]
    assert libelles[0] == libelles[1] == libelles[2]
    assert avec["liens"] == sans["liens"] and avec["preuves"] == sans["preuves"]


def test_sans_journal_le_reseau_reste_en_memoire(tmp_path):
    passage(tmp_path)
    assert not list((tmp_path / "sortie").glob("*.sqlite"))


def test_le_serveur_voit_arriver_les_noeuds_puis_rejoue_avec_la_duree_reelle(tmp_path):
    racine = tmp_path / "enquetes"

    def enqueteur(question, mots, dossier, journal, identifiant):
        faux = FauxOrianeComplet(videos_de_test(), delai=0.05)
        with client_oriane(tmp_path, faux) as c:
            chaine.enqueter(question, mots, c, dossier, identifiant, nb_archives=0, chemin_journal=journal)

    with Serveur(racine, port=0, pas_sondage=0.02, enqueteur=enqueteur) as s:
        statut, corps = post(s, "/api/enquetes", {"question": "Qui diffuse ces vidéos minceur ?", "graine": "minceur"})
        assert statut == 201
        sorties = flux(s, f"/api/evenements?enquete={corps['id']}")
        instants_question = [t for t, e in sorties if e["type"] == "question"]
        instants_noeuds = [t for t, e in sorties if e["type"] == "noeud"]
        assert instants_noeuds and instants_noeuds[0] - instants_question[0] >= 0.1
        assert sorties[-1][1]["type"] == "fin" and sorties[-1][1]["donnees"]["ok"] is True

        rejeu = flux(s, f"/api/rejouer?enquete={corps['id']}&duree_cible=1")
        fin = rejeu[-1][1]
        assert fin["type"] == "fin" and fin["donnees"]["duree_reelle_s"] >= 0.15
        assert all(e["accelere"] in (True, False) and "facteur" in e for _, e in rejeu)


# --------------------------------------------------------------------------- archivage complet


def piece_factice(url, avec_video):
    piece = PieceArchivee(url=url, type="video", date_collecte="2026-11-07T10:00:00+00:00")
    piece.voie("oembed", "ok")
    piece.voie("capture_page", "ok", mur_connexion=True)
    piece.mur_connexion = True
    piece.voie("wayback", "ok")
    piece.url_wayback = "https://web.archive.org/web/20261107100000/" + url
    piece.fichiers = [{"nom": "x.empreintes.txt", "role": "liste_des_empreintes", "octets": 10, "sha256": "cd" * 32}]
    piece.horodatages = [{"autorite": "freetsa.org", "statut": "ok", "heure_attestee": "2026-11-07T10:00:05+00:00"},
                         {"autorite": "autre.example", "statut": "ok", "heure_attestee": "2026-11-07T10:00:06+00:00"}]
    piece.geste_manuel = "archive.today à la main"
    if avec_video:
        piece.empreinte_sha256 = "ab" * 32
        piece.statuts["telechargement"] = "ok"
    else:
        piece.video_non_telechargee = True
        piece.statuts["telechargement"] = "echec"
    return piece


@pytest.mark.parametrize("avec_video, empreinte", [(True, "ab" * 32), (False, "cd" * 32)])
def test_archiver_piece_remplace_l_archivage_simple(tmp_path, monkeypatch, avec_video, empreinte):
    appels = []

    def faux_archiver_piece(url, dossier="archives", **options):
        appels.append((url, Path(dossier)))
        return piece_factice(url, avec_video)

    monkeypatch.setattr(chaine, "archiver_piece", faux_archiver_piece)
    rapport, _ = passage(tmp_path, nb_archives=2)
    assert len(appels) == 2 and all(dossier == tmp_path / "sortie" / "archives" for _, dossier in appels)

    export = lire_json(tmp_path / "sortie" / "reseau.json")
    videos = {p["attributs"]["url"]: p for p in pieces_de(export) if p["type"] == "video"}
    archive = videos[appels[0][0]]["attributs"]["archive"]
    assert archive["url_wayback"].startswith("https://web.archive.org/")
    assert [j["autorite"] for j in archive["jetons_horodatage"]] == ["freetsa.org", "autre.example"]
    assert archive["mur_de_connexion"] is True and archive["video_non_telechargee"] is (not avec_video)
    assert archive["geste_manuel"] and archive["empreinte_sha256"] == empreinte
    assert archive["statuts"]["wayback"] == "ok"
    empreintes = {p["empreinte_sha256"] for lien in export["liens"] for p in lien["preuves"]}
    assert empreinte in empreintes
    # Le rapport garde ses clés et en ajoute.
    assert {"url", "reussie", "statuts", "empreinte_sha256", "url_wayback", "voies", "horodatages"} <= set(
        rapport["archives"][0])
    assert rapport["archives"][0]["horodatages"] == 2


def test_une_exception_d_archivage_est_notee_et_la_chaine_continue(tmp_path, monkeypatch):
    def en_panne(url, dossier="archives", **options):
        raise RuntimeError("Playwright absent")

    monkeypatch.setattr(chaine, "archiver_piece", en_panne)
    rapport, _ = passage(tmp_path, nb_archives=2)
    assert len(rapport["archives"]) == 2 and not any(a["reussie"] for a in rapport["archives"])
    assert rapport["liens"] > 0


# --------------------------------------------------------------------------- pivots

EMAIL = "zoe@mail.com"
TELEPHONE = "06 12 34 56 78"
PROFILS = [
    {"platform": "tiktok", "handle": "compte_a", "publicEmail": "secret@mail.com",
     "bio": "Boutique https://boutique-exemple.fr — page https://linktr.ee/zoe_fit", "bioLink": None},
    {"platform": "tiktok", "handle": "compte_b", "bioLink": "linktr.ee/zoe_fit",
     "bio": "https://boutique-exemple.fr/promo et https://linktr.ee/zoe_fit et https://boutique-sosie.shop"},
    {"platform": "tiktok", "handle": "compte_c", "bioLink": None,
     "bio": f"https://boutique-sosie.shop code ZOE10 mail {EMAIL} tel {TELEPHONE} SIREN 552 100 554"},
]


def faux_infra(tmp_path):
    faux = FauxInfra()
    infra = Infra(dossier_cache=tmp_path / "infra", transport=httpx.MockTransport(faux),
                  maintenant=lambda: MAINTENANT_INFRA, dormir=lambda _s: None)
    return infra, faux


def passage_pivots(tmp_path, profils=PROFILS, faux=None, **options):
    infra, faux_i = faux_infra(tmp_path)
    options.setdefault("chemin_journal", tmp_path / "sortie" / "reseau.sqlite")
    rapport, faux_o = passage(tmp_path, faux, profils, pivots=True, infra=infra, **options)
    return rapport, faux_o, faux_i


def noeuds_par_libelle(export):
    return {p["libelle"]: p for p in pieces_de(export)}


def test_les_pivots_entrent_au_reseau_avec_leurs_preuves(tmp_path):
    rapport, faux_o, _ = passage_pivots(tmp_path)
    export = lire_json(tmp_path / "sortie" / "reseau.json")
    noeuds = noeuds_par_libelle(export)
    roles = {d["role"]: d["nom"] for d in export["dossiers"]}
    assert roles["infrastructure"] == "Infrastructure"

    def role_de(libelle):
        return next(d["role"] for d in export["dossiers"] for p in d["pieces"] if p["libelle"] == libelle)

    for libelle in ("Site boutique-exemple.fr", "Site boutique-sosie.shop", "Page de liens linktr.ee/zoe_fit",
                    "Page boutique-exemple.fr/promo"):
        assert role_de(libelle) == "infrastructure", libelle
    assert role_de("Code promo ZOE10") == "beneficiaire"
    assert role_de("SIREN 552100554") == "beneficiaire"
    assert noeuds["SIREN 552100554"]["type"] == "societe"
    assert noeuds["Site boutique-exemple.fr"]["type"] == "site"

    # Chaque lien de pivot porte une URL d'origine et l'extrait exact.
    types = {"renvoie_vers", "cite_le_code", "cite_la_societe"}
    liens_pivots = [l for l in export["liens"] if l["type_lien"] in types]
    assert len(liens_pivots) == 2 + 2 + 2 + 1 + 1 + 1  # domaine A, domaine B, page de liens, URL, code promo, SIREN
    for lien in liens_pivots:
        assert lien["statut"] == "prouve"
        for preuve in lien["preuves"]:
            assert preuve["url"].startswith("https://www.tiktok.com/@") and preuve["extrait"].strip()
    lien_code = next(l for l in liens_pivots if l["type_lien"] == "cite_le_code")
    assert "ZOE10" in lien_code["preuves"][0]["extrait"]

    # Une question « même propriétaire ? » par valeur partagée, rien de plus pour les valeurs d'un seul compte.
    questions = [q["texte"] for q in export["questions_ouvertes"] if "affichent le même" in q["texte"]]
    assert len(questions) == 3
    assert all("À écarter d'abord" in q for q in questions)
    assert any("boutique-sosie.shop" in q and "@compte_b (tiktok)" in q and "@compte_c (tiktok)" in q
               for q in questions)

    pivots = rapport["pivots"]
    assert pivots["profils_lus"] == 3 and pivots["pivots_extraits"] >= 6
    assert pivots["pivots_au_reseau"] == 6 and pivots["pivots_partages"] == 3
    assert pivots["pivots_enrichis"] == 3 and pivots["appels_infra"] > 0 and pivots["rejets_infra"] == 0
    # Un seul appel de profils par plateforme, 15 crédits (projection full), filtre plateforme + pseudos triés.
    assert len(faux_o.requetes_profils) == 1
    projection, corps = faux_o.requetes_profils[0]
    assert projection == "full"
    assert corps["filters"]["platform"] == {"includes": ["tiktok"]}
    pseudos = corps["filters"]["handle"]["exactMatch"]["values"]
    assert pseudos == sorted(pseudos) == ["compte_a", "compte_b", "compte_c", "compte_d"]
    assert rapport["credits_depenses"] == 3 * 10 + 20 + 15


def test_les_indices_des_sources_ouvertes_deviennent_des_documents(tmp_path):
    _, _, faux_i = passage_pivots(tmp_path)
    export = lire_json(tmp_path / "sortie" / "reseau.json")
    documents = [p for p in pieces_de(export) if p["type"] == "document"]
    sources = {p["attributs"]["source"] for p in documents}
    assert {"rdap", "crt.sh", "wayback", "recherche-entreprises"} <= sources
    par_source = {p["attributs"]["source"]: p["solidite"]["fiabilite_source"] for p in documents}
    assert par_source["rdap"] == "A" and par_source["recherche-entreprises"] == "A"
    assert par_source["wayback"] == "B"
    liens = {l["id"]: l for l in export["liens"]}
    documentes = [l for l in liens.values() if l["type_lien"] == "documente"]
    assert len(documentes) == len(documents)
    assert all(l["preuves"][0]["url"].startswith("https://") for l in documentes)
    # Un domaine frère par certificat qui est aussi un domaine du dossier : lien « meme_certificat » + question.
    certificats = [l for l in liens.values() if l["type_lien"] == "meme_certificat"]
    assert len(certificats) == 1 and certificats[0]["statut"] == "indice"
    assert {certificats[0]["source_libelle"], certificats[0]["cible_libelle"]} == {
        "Site boutique-exemple.fr", "Site boutique-sosie.shop"}
    assert any("même certificat TLS" in q["texte"] for q in export["questions_ouvertes"])
    # Rien n'a été demandé hors des quatre sources ouvertes simulées.
    assert {r.url.host for r in faux_i.requetes} <= {
        "rdap.org", "crt.sh", "recherche-entreprises.api.gouv.fr", "web.archive.org"}


def test_aucun_email_ni_numero_dans_le_journal_ni_les_sorties(tmp_path):
    passage_pivots(tmp_path)
    textes = [Path(tmp_path / "sortie" / n).read_text(encoding="utf-8") for n in ("reseau.json", "passage.json")]
    textes.append(json.dumps(lignes_du_journal(tmp_path / "sortie" / "reseau.sqlite"), ensure_ascii=False))
    for texte in textes:
        for interdit in (EMAIL, "secret@mail.com", TELEPHONE, "0612345678", "612345678"):
            assert interdit not in texte
    assert "[masqué]" in textes[0]  # l'extrait garde la place du masque


def test_le_serveur_retrouve_le_domaine_dans_la_recherche(tmp_path):
    passage_pivots(tmp_path)
    with Serveur(tmp_path, port=0, pas_sondage=0.02) as s:
        resultats = get_json(s, "/api/recherche?enquete=sortie&q=boutique-exemple")["resultats"]
    assert resultats and any("boutique-exemple" in json.dumps(r, ensure_ascii=False) for r in resultats)


def test_pivots_inactifs_par_defaut_la_chaine_de_base_ne_bouge_pas(tmp_path):
    rapport, faux = passage(tmp_path, FauxOrianeComplet(videos_de_test(), PROFILS))
    assert faux.projections == ["basic", "basic", "basic", "full"] and not faux.requetes_profils
    assert rapport["credits_depenses"] == 50 and rapport["questions_ouvertes"] == 3
    assert "pivots" not in rapport


def test_si_oriane_refuse_le_filtre_des_profils_l_extraction_se_fait_sur_les_contenus(tmp_path):
    videos = videos_de_test()
    videos[1]["profileBio"] = "Ma page https://linktr.ee/zoe_fit et mon site https://boutique-exemple.fr"
    videos[2]["profileBio"] = "Retrouve https://linktr.ee/zoe_fit"
    faux = FauxOrianeComplet(videos, PROFILS, refuse_profils=True)
    rapport, _, _ = passage_pivots(tmp_path, faux=faux)
    assert any("profils" in e for e in rapport["erreurs"])
    assert rapport["pivots"]["profils_lus"] == 0
    export = lire_json(tmp_path / "sortie" / "reseau.json")
    assert "Page de liens linktr.ee/zoe_fit" in noeuds_par_libelle(export)
    assert rapport["liens"] > 0


# --- unités : poser_pivots et enrichir_pivots


def comptes_du_test(reseau, *pseudos):
    return {p: reseau.ajouter_noeud(Noeud(
        TypeNoeud.COMPTE, f"@{p} (tiktok)", Role.INCONNU, {"plateforme": "tiktok", "pseudo": p})) for p in pseudos}


def pivot_partage(valeur, type_="domaine", banal=False, poids=1.0, comptes=("a", "b"), champ="bio", url=True):
    return piv.Pivot(
        type=type_, valeur=valeur, comptes=tuple(f"tiktok:{c}" for c in comptes), poids_rarete=poids, banal=banal,
        preuves=tuple(piv.PreuvePivot(f"tiktok:{c}", champ, f"voir https://{valeur}",
                                      url=f"https://www.tiktok.com/@{c}" if url else None) for c in comptes))


def test_un_pivot_banal_n_entre_pas_au_reseau():
    with ReseauEnquete("Q ?") as reseau:
        comptes_du_test(reseau, "a", "b")
        posees = chaine.poser_pivots(reseau, [pivot_partage("banal.fr", banal=True), pivot_partage("rare.fr")],
                                     datetime.now(UTC))
        assert [n.libelle for n in reseau.noeuds() if n.type is TypeNoeud.SITE] == ["Site rare.fr"]
        assert posees.partages == 1


def test_un_domaine_cite_par_un_seul_compte_n_entre_que_s_il_est_dans_sa_bio():
    with ReseauEnquete("Q ?") as reseau:
        comptes_du_test(reseau, "a")
        chaine.poser_pivots(reseau, [
            pivot_partage("cite.fr", comptes=("a",), champ="transcript"),
            pivot_partage("possede.fr", comptes=("a",), champ="bio"),
            pivot_partage("lien.fr", comptes=("a",), champ="bioLink"),
        ], datetime.now(UTC))
        assert sorted(n.libelle for n in reseau.noeuds() if n.type is TypeNoeud.SITE) == [
            "Site lien.fr", "Site possede.fr"]
        assert reseau.questions() == []  # un seul compte : pas de « même propriétaire ? »


def test_une_preuve_sans_url_d_origine_est_ecartee_et_sans_preuve_pas_de_piece():
    with ReseauEnquete("Q ?") as reseau:
        comptes_du_test(reseau, "a", "b")
        chaine.poser_pivots(reseau, [pivot_partage("sans-url.fr", url=False)], datetime.now(UTC))
        assert [n for n in reseau.noeuds() if n.type is TypeNoeud.SITE] == []


def test_l_enrichissement_s_arrete_a_quinze_pivots_sur_un_dossier_de_vingt_domaines(tmp_path):
    infra, faux = faux_infra(tmp_path)
    with ReseauEnquete("Q ?") as reseau:
        comptes_du_test(reseau, "a", "b")
        pivots = [pivot_partage(f"site{i:02d}.fr", poids=1.0 + i / 100) for i in range(20)]
        posees = chaine.poser_pivots(reseau, pivots, datetime.now(UTC))
        assert len(posees.noeuds) == 20
        stats = chaine.enrichir_pivots(reseau, infra, posees)
    assert stats["enrichis"] == 15
    domaines_lus = {r.url.path.rsplit("/", 1)[-1] for r in faux.requetes if r.url.host == "rdap.org"}
    assert len(domaines_lus) == 15
    # Les plus rares d'abord : site19 ... site05 ; les cinq moins rares ne sont pas lus.
    assert domaines_lus == {f"site{i:02d}.fr" for i in range(5, 20)}


def test_pivots_depuis_resultats_tient_en_5000_caracteres_et_vide_donne_vide():
    assert piv.pivots_depuis_resultats([]) == []
    lot = []
    for i in range(80):
        r = dict(videos_de_test()[0])
        r.update(id=f"cnt_{i}", profileHandle=f"compte_{i}", profileBio=f"https://site-{i}.fr/ et https://autre.fr/",
                 platformId=str(7698316626466311456 + i))
        lot.append(r)
        r2 = dict(r)
        r2.update(id=f"cnt_bis_{i}", profileHandle=f"compte_bis_{i}")
        lot.append(r2)
    resultat = piv.pivots_depuis_resultats(lot)
    assert resultat and len(resultat) <= 40
    assert len(json.dumps([dict(x) for x in resultat])) < 5000
    assert all(not x.get("banal") for x in resultat)
    assert resultat[0]["n_comptes"] >= 2  # les partagés d'abord


def test_masquer_est_public_et_cache_les_pistes_desactivees():
    assert piv.masquer(f"écris à {EMAIL} ou au {TELEPHONE}") == "écris à [masqué] ou au [masqué]"


# --------------------------------------------------------------------------- argent qui bouge

PORTEFEUILLE_A = "0x1111111111111111111111111111111111111111"
PORTEFEUILLE_B = "0x4444444444444444444444444444444444444444"
PROFILS_ARGENT = [
    {"platform": "tiktok", "handle": "compte_a", "bio": f"Soutiens-moi : {PORTEFEUILLE_A}", "bioLink": None},
    {"platform": "tiktok", "handle": "compte_b", "bio": f"Don en crypto {PORTEFEUILLE_B}", "bioLink": None},
]


def faux_chaines(tmp_path):
    faux = FauxReseau()
    client = ClientChaines(dossier_cache=tmp_path / "chaines", transport=httpx.MockTransport(faux),
                           dormir=lambda _s: None, intervalle=0)
    return client, faux


def adresse_base58(version, charge):
    import hashlib

    brut = bytes([version]) + charge
    controle = hashlib.sha256(hashlib.sha256(brut).digest()).digest()[:4]
    n = int.from_bytes(brut + controle, "big")
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    sortie = ""
    while n:
        n, reste = divmod(n, 58)
        sortie = alphabet[reste] + sortie
    return sortie


def test_le_flux_d_argent_entre_deux_portefeuilles_du_dossier_est_prouve_par_la_transaction(tmp_path):
    client, _ = faux_chaines(tmp_path)
    rapport, _, _ = passage_pivots(tmp_path, PROFILS_ARGENT, chaines=client)
    export = lire_json(tmp_path / "sortie" / "reseau.json")
    noeuds = noeuds_par_libelle(export)
    assert noeuds[f"Portefeuille evm {PORTEFEUILLE_A}"]["type"] == "portefeuille"

    flux_ = [l for l in export["liens"] if l["type_lien"] == "flux_d_argent"]
    entre_les_deux = [l for l in flux_ if {l["source_libelle"], l["cible_libelle"]} == {
        f"Portefeuille evm {PORTEFEUILLE_A}", f"Portefeuille evm {PORTEFEUILLE_B}"}]
    # Un lien par sens, orienté de l'envoyeur vers le receveur.
    assert 1 <= len(entre_les_deux) <= 2
    lien = next(l for l in entre_les_deux if l["source_libelle"].endswith(PORTEFEUILLE_A))
    assert lien["famille"] == "argent" and lien["statut"] == "prouve" and lien["oriente"] is True
    assert lien["identifiant_officiel"] is True
    assert lien["preuves"][0]["url"] == "https://eth.blockscout.com/tx/0xt3"
    assert "ETH" in lien["preuves"][0]["extrait"] and "2026-05-01" in lien["preuves"][0]["extrait"]
    assert len(lien["preuves"]) <= 3

    # Le profil du portefeuille est une version du nœud ; sa page d'adresse est une preuve de plus.
    profil = noeuds[f"Portefeuille evm {PORTEFEUILLE_A}"]["attributs"]["profil"]
    assert profil["chaine"] == "eth" and profil["nb_transactions"] and profil["totaux"]
    assert "jamais attribué à une personne" in profil["attribution"]
    liens_du_compte = [l for l in export["liens"] if l["type_lien"] == "affiche_le_portefeuille"
                       and l["cible_libelle"].endswith(PORTEFEUILLE_A)]
    assert any(p["url"] == f"https://eth.blockscout.com/address/{PORTEFEUILLE_A}"
               for l in liens_du_compte for p in l["preuves"])
    assert rapport["pivots"]["argent"]["flux"] >= 2


def test_une_plateforme_d_echange_doit_repondre_et_une_sanction_ouvre_une_question(tmp_path):
    client, _ = faux_chaines(tmp_path)
    passage_pivots(tmp_path, PROFILS_ARGENT, chaines=client)
    export = lire_json(tmp_path / "sortie" / "reseau.json")
    plateformes = [p for p in pieces_de(export) if p["attributs"].get("doit_repondre")]
    assert len(plateformes) == 1
    plateforme = plateformes[0]
    assert plateforme["libelle"] == "Plateforme d'échange : Binance 14"
    assert plateforme["role"] == "infrastructure"
    assert "Doit répondre" in plateforme["attributs"]["mention"]
    liens_plateforme = [l for l in export["liens"] if l["type_lien"] == "flux_d_argent"
                        and plateforme["libelle"] in (l["source_libelle"], l["cible_libelle"])]
    assert liens_plateforme and all(p["url"].startswith("https://eth.blockscout.com/tx/")
                                    for l in liens_plateforme for p in l["preuves"])
    textes = [q["texte"] for q in export["questions_ouvertes"]]
    assert any("Binance 14" in t and "réquisition" in t for t in textes)
    assert any(PORTEFEUILLE_B in t and "OFAC" in t for t in textes)


def test_aucun_portefeuille_n_est_attribue_a_une_personne(tmp_path):
    client, _ = faux_chaines(tmp_path)
    passage_pivots(tmp_path, PROFILS_ARGENT, chaines=client)
    export = lire_json(tmp_path / "sortie" / "reseau.json")
    for piece in pieces_de(export):
        if piece["type"] == "portefeuille":
            texte = json.dumps(piece["attributs"], ensure_ascii=False)
            assert "ENTITE DE TEST" not in texte and "AUTRE ENTITE" not in texte
    sanction = noeuds_par_libelle(export)[f"Portefeuille evm {PORTEFEUILLE_B}"]["attributs"]["profil"]["sanction_ofac"]
    assert sanction["statut"] == "oui" and sanction["programmes"] == ["CYBER2"]


def test_une_chaine_non_prise_en_charge_est_notee_sans_appel_reseau(tmp_path):
    litecoin = adresse_base58(0x30, bytes(range(1, 21)))
    assert litecoin[0] in "LM"
    client, faux = faux_chaines(tmp_path)
    profils = [{"platform": "tiktok", "handle": "compte_a", "bio": f"Litecoin {litecoin}", "bioLink": None}]
    rapport, _, _ = passage_pivots(tmp_path, profils, chaines=client)
    assert faux.requetes == []
    assert any("chaîne non prise en charge" in e for e in rapport["erreurs"])
    noeud = noeuds_par_libelle(lire_json(tmp_path / "sortie" / "reseau.json"))[f"Portefeuille litecoin {litecoin}"]
    assert noeud["attributs"]["profil"] == {"chaine_non_prise_en_charge": "litecoin"}
    assert sources_chaines_du_pivot("litecoin") is None and sources_chaines_du_pivot("dogecoin") is None
    assert [sources_chaines_du_pivot(s) for s in ("evm", "bitcoin", "tron", "solana")] == ["eth", "btc", "tron", "sol"]


def sources_chaines_du_pivot(sous_type):
    from enquete.sources.chaines import chaine_du_pivot

    return chaine_du_pivot(sous_type)


def test_une_instance_qui_repond_403_donne_une_erreur_notee_et_un_reseau_complet(tmp_path):
    client, faux = faux_chaines(tmp_path)
    faux.pannes["eth.blockscout.com"] = 403
    rapport, _, _ = passage_pivots(tmp_path, PROFILS_ARGENT, chaines=client)
    assert any("ChainesBloqueeError" in e for e in rapport["erreurs"])
    export = lire_json(tmp_path / "sortie" / "reseau.json")
    assert f"Portefeuille evm {PORTEFEUILLE_A}" in noeuds_par_libelle(export)
    assert not [l for l in export["liens"] if l["type_lien"] == "flux_d_argent"]
    assert export["compteurs"]["liens"] == rapport["liens"] > 0
    assert (tmp_path / "sortie" / "passage.json").is_file()


# --------------------------------------------------------------------------- images de l'export

IMPORTER_REEL = chaine._importer_images  # le conftest le remplace par un module absent, test par test


def faux_module_images(echec=None):
    import types

    module = types.ModuleType("enquete.images")
    module.appels = []

    class ImageInvalide(Exception):
        pass

    module.ImageInvalide = ImageInvalide

    def illustrer_export(export, resultats, captures=None, dossier="archives/images", **options):
        module.appels.append((export, resultats, captures, Path(dossier)))
        if echec:
            raise ImageInvalide(echec)
        sortie = json.loads(json.dumps(export))
        for d in sortie["dossiers"]:
            for p in d["pieces"]:
                p["image"] = {"type": "image_cle_video", "provenance": "capture", "url": "https://img.example/" + p["id"]}
            d["image"], d["image_piece_id"] = (d["pieces"][0]["image"], d["pieces"][0]["id"]) if d["pieces"] else (None, None)
            d["silhouette"] = not d["pieces"]
        sortie["qr_trouves"] = [{"video_id": "cnt_x", "seconde": 3.0, "contenu": "https://exemple.fr"}]
        sortie["images_echecs"] = []
        return sortie

    module.illustrer_export = illustrer_export
    return module


def injecter(monkeypatch, module):
    monkeypatch.setitem(sys.modules, "enquete.images", module)
    monkeypatch.setattr(chaine, "_importer_images", IMPORTER_REEL)


def test_les_images_sont_ajoutees_a_l_export_et_servies_par_l_api(tmp_path, monkeypatch):
    faux = faux_module_images()
    injecter(monkeypatch, faux)
    rapport, _ = passage(tmp_path, chemin_journal=tmp_path / "sortie" / "reseau.sqlite")
    assert len(faux.appels) == 1
    export_envoye, resultats, captures, dossier_images = faux.appels[0]
    assert set(resultats) == {v["id"] for v in videos_de_test()}  # les résultats full, par identifiant
    assert dossier_images == tmp_path / "sortie" / "archives" / "images" and captures == {}

    export = lire_json(tmp_path / "sortie" / "reseau.json")
    assert all(p.get("image") for p in pieces_de(export))
    images = lire_json(tmp_path / "sortie" / "images.json")
    assert set(images) == {"pieces", "dossiers", "qr_trouves", "images_echecs"}
    assert set(images["pieces"]) == {p["id"] for p in pieces_de(export)}
    assert images["qr_trouves"][0]["contenu"] == "https://exemple.fr"
    assert rapport["images"]["pieces_illustrees"] == len(pieces_de(export))

    # Le serveur réexporte depuis le journal : l'image arrive par images.json.
    with Serveur(tmp_path, port=0, pas_sondage=0.02) as s:
        servi = get_json(s, "/api/export?enquete=sortie")
    assert all(p.get("image") for p in pieces_de(servi))
    assert servi["qr_trouves"] and any(d.get("image_piece_id") for d in servi["dossiers"])


def test_une_image_invalide_garde_l_export_d_origine_et_s_inscrit_au_rapport(tmp_path, monkeypatch):
    injecter(monkeypatch, faux_module_images(echec="image générée par une IA"))
    journal = tmp_path / "sortie" / "reseau.sqlite"
    rapport, _ = passage(tmp_path, chemin_journal=journal)
    assert "ImageInvalide" in rapport["images"]["erreur"]
    export = lire_json(tmp_path / "sortie" / "reseau.json")
    assert not any("image" in p for p in pieces_de(export))
    assert not (tmp_path / "sortie" / "images.json").exists()
    with ReseauEnquete(rapport["question"], str(journal)) as r:
        assert r.exporter_pour_interface() == export  # l'export d'origine, tel quel


def test_sans_le_module_d_images_rien_ne_change(tmp_path):
    rapport, _ = passage(tmp_path)
    export = lire_json(tmp_path / "sortie" / "reseau.json")
    assert "images" not in rapport and not (tmp_path / "sortie" / "images.json").exists()
    assert not any("image" in p for p in pieces_de(export)) and "qr_trouves" not in export


def test_chaine_ne_charge_pas_le_module_d_images_a_l_import():
    code = "import sys, enquete.chaine; print('enquete.images' in sys.modules)"
    sortie = subprocess.run([sys.executable, "-c", code], cwd=RACINE, capture_output=True, text=True, timeout=60)
    assert sortie.stdout.strip() == "False"


# --------------------------------------------------------------------------- mots tirés de la question


@pytest.mark.parametrize("question, attendu", [
    ("Les « stylos minceur » sont-ils une arnaque ?", ["stylos minceur", "arnaque"]),
    ("Panama Papers : qui est derrière ?", ["Panama Papers"]),
    ("Comment Brut Scan repère-t-il les vidéos ?", ["Brut Scan", "repère"]),
    ("Le GLP-1 est-il vendu sur TikTok ?", ["GLP-1", "vendu"]),
    ('Que dit "October 17 march" sur Paris ?', ["October 17 march", "dit", "paris"]),
    ("Pourquoi ces séismes ?", ["séismes"]),
])
def test_mots_de_la_question(question, attendu):
    assert serveur.mots_de_la_question(question) == attendu


def test_mots_de_la_question_repli_doublons_et_plafond():
    assert serveur.mots_de_la_question("Qui ? Quoi ? Comment ?") == ["Qui ? Quoi ? Comment ?"]
    assert serveur.mots_de_la_question("") == []
    mots = serveur.mots_de_la_question("Séisme séisme séisme volcan Volcan éruption lave cendre nuage panache fumée gaz")
    assert len(mots) <= 8 and len({m.lower() for m in mots}) == len(mots)
    assert "séisme" in mots and "volcan" in mots
    assert serveur.fusionner_mots(["Séisme", "volcan"], ["seisme", "lave"]) == ["Séisme", "volcan", "lave"]


def serveur_avec(tmp_path, cadreur=None):
    recus = []

    def enqueteur(question, mots, dossier, journal, identifiant):
        recus.append((question, mots))
        ReseauEnquete(question, str(journal)).fermer()

    return Serveur(tmp_path, port=0, pas_sondage=0.02, enqueteur=enqueteur, cadreur=cadreur), recus


def lancer_et_attendre(s, corps):
    statut, rep = post(s, "/api/enquetes", corps)
    assert statut == 201
    flux(s, f"/api/evenements?enquete={rep['id']}")
    return rep["id"]


def test_sans_graine_l_enqueteur_recoit_les_mots_de_la_question(tmp_path):
    s, recus = serveur_avec(tmp_path)
    with s:
        identifiant = lancer_et_attendre(s, {"question": "Stylos minceur : qui est derrière ?"})
    assert recus[0][0] == "Stylos minceur : qui est derrière ?"
    assert recus[0][1] == ["stylos", "minceur"]  # sans mots vides ni mot de question
    cadrage = lire_json(tmp_path / identifiant / "cadrage.json")
    assert {m["origine"] for m in cadrage["mots"]} == {"question"}
    assert [m["mot"] for m in cadrage["mots"]] == recus[0][1]


def test_avec_une_graine_rien_ne_change(tmp_path):
    s, recus = serveur_avec(tmp_path, cadreur=lambda q: ["ne doit pas servir"])
    with s:
        identifiant = lancer_et_attendre(s, {"question": "Test ?", "graine": "séisme, volcan"})
    assert recus[0][1] == ["séisme", "volcan"]
    assert {m["origine"] for m in lire_json(tmp_path / identifiant / "cadrage.json")["mots"]} == {"graine"}


def test_le_cadreur_passe_d_abord_puis_les_mots_de_la_question_sans_doublon(tmp_path):
    s, recus = serveur_avec(tmp_path, cadreur=lambda q: ["earthquake", "séisme", "sismo"])
    with s:
        identifiant = lancer_et_attendre(s, {"question": "Ce séisme à Paris est-il réel ?"})
    mots = recus[0][1]
    assert mots[:3] == ["earthquake", "séisme", "sismo"]
    assert len({m.lower() for m in mots}) == len(mots) and "paris" in mots
    origines = {m["mot"]: m["origine"] for m in lire_json(tmp_path / identifiant / "cadrage.json")["mots"]}
    assert origines["earthquake"] == "agent de cadrage" and origines["paris"] == "question"


def test_un_cadreur_qui_leve_laisse_les_mots_de_la_question(tmp_path):
    def en_panne(question):
        raise RuntimeError("claude -p indisponible")

    s, recus = serveur_avec(tmp_path, cadreur=en_panne)
    with s:
        lancer_et_attendre(s, {"question": "Ce séisme à Paris est-il réel ?"})
    assert recus[0][1] == serveur.mots_de_la_question("Ce séisme à Paris est-il réel ?")


def test_le_serveur_par_defaut_n_importe_ni_prompts_ni_appel_claude(tmp_path, monkeypatch):
    importes = []
    reel = serveur.importlib.import_module

    def espion(nom, *a, **k):
        importes.append(nom)
        return reel(nom, *a, **k)

    monkeypatch.setattr(serveur.importlib, "import_module", espion)
    s, _ = serveur_avec(tmp_path)  # cadreur=None, comme Serveur()
    with s:
        lancer_et_attendre(s, {"question": "Ce séisme à Paris est-il réel ?"})
    assert not [n for n in importes if n in ("enquete.prompts", "enquete.appel_claude")]
    assert Serveur.__init__.__defaults__ is not None and serveur.Serveur(tmp_path, port=0).cadreur is None


def test_cadrage_du_tour_1_rend_none_tant_que_les_modules_manquent(monkeypatch):
    def absent(nom, *a, **k):
        raise ModuleNotFoundError(nom)

    monkeypatch.setattr(serveur.importlib, "import_module", absent)
    assert serveur.cadrage_du_tour_1("Une question ?") is None


def test_cadrage_du_tour_1_utilise_les_modules_quand_ils_sont_la(monkeypatch):
    import types

    prompts = types.SimpleNamespace(cadrage=lambda question, gel: f"prompt:{question}:{gel}")

    class Claude:
        def __init__(self, dossier_cache, executeur):
            self.executeur = executeur

        def demander(self, role, prompt, modele):
            assert role == "cadrage" and prompt == "prompt:Une question ?:None" and modele == "sonnet"
            return {"mots": ["alpha", " beta ", 3, ""], "pourquoi": "test"}

    class ExecuteurCLI:
        def __init__(self, delai_s):
            assert delai_s == 90

    appel = types.SimpleNamespace(Claude=Claude, ExecuteurCLI=ExecuteurCLI, MODELE_AGENT="sonnet")
    monkeypatch.setattr(serveur.importlib, "import_module",
                        lambda nom, *a, **k: {"enquete.prompts": prompts, "enquete.appel_claude": appel}[nom])
    assert serveur.cadrage_du_tour_1("Une question ?") == ["alpha", "beta"]


# --------------------------------------------------------------------------- console et docstrings


def lancer_aide(module):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    sortie = subprocess.run([sys.executable, "-m", module, "--help"], cwd=RACINE, env=env,
                            capture_output=True, timeout=60)
    assert sortie.returncode == 0, sortie.stderr
    return sortie.stdout.decode("utf-8")  # lève si la console n'a pas écrit de l'UTF-8


@pytest.mark.parametrize("module", ["enquete.chaine", "enquete.serveur"])
def test_la_console_ecrit_en_utf8(module):
    texte = lancer_aide(module)
    assert "Enquête" in texte and "réseau" in texte


def test_les_docstrings_de_reseau_renvoient_a_la_methode_publique():
    source = (RACINE / "enquete" / "reseau.py").read_text(encoding="utf-8")
    for motif in ("kickoff", "3-methode", "da-bible", "§3."):
        assert motif not in source, motif
    assert "docs/methode.md" in source
    assert (RACINE / "docs" / "methode.md").is_file()


