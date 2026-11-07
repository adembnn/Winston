"""Tests de l'indépendance des sources : données fictives, aucun réseau."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from enquete.independance import (
    Dependance,
    PieceSource,
    dependances,
    depuis_export,
    origines,
    pour_affirmation,
    repérer_credits,
    resume_dossier,
)

DEBUT = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
PHRASE_LONGUE = "la prefecture annonce la fermeture du pont pour trois mois a partir de lundi"  # 14 mots
PHRASE_MOYENNE = "le pont ferme pour trois mois des lundi"  # 8 mots
PHRASE_COURTE = "pont ferme pour trois mois"  # 5 mots


def piece(id_, auteur="tiktok:a", jour=0, texte="", url=None, nature="video", **kw) -> PieceSource:
    return PieceSource(
        id=id_, nature=nature, auteur=auteur, date=DEBUT + timedelta(days=jour), url=url or f"https://ex.test/{id_}",
        texte=texte, **kw,
    )


def regles(deps: list[Dependance]) -> set[tuple[str, str, str, str]]:
    return {(d.de, d.vers, d.regle, d.niveau) for d in deps}


def export_factice(videos: list[dict], liens: list[dict] | None = None) -> dict:
    """Export au format réel : comptes, vidéos, liens `publie` portant la légende en preuve."""
    pieces, tous_liens = [], []
    for i, v in enumerate(videos):
        compte_id, video_id = f"c{i}", v["id"]
        url = v.get("url", f"https://www.tiktok.com/@{v['pseudo']}/video/{7000 + i}")
        pieces.append({"id": compte_id, "type": "compte", "libelle": f"@{v['pseudo']} (tiktok)",
                       "attributs": {"plateforme": "tiktok"}, "preuves": []})
        preuves = [{"url": url, "extrait": v["legende"], "liens": [f"p{i}"]}] if v.get("legende") else []
        pieces.append({"id": video_id, "type": "video", "libelle": f"Vidéo tiktok de @{v['pseudo']}, 01/11/2026",
                       "attributs": {"url": url, "date": (DEBUT + timedelta(days=v.get("jour", 0))).isoformat()},
                       "preuves": preuves})
        tous_liens.append({"id": f"p{i}", "type_lien": "publie", "source_id": compte_id, "cible_id": video_id,
                           "statut": "prouve", "preuves": [], "force_points": 3.0})
    tous_liens += liens or []
    return {"question": "q", "dossiers": [{"role": "inconnu", "pieces": pieces}], "liens": tous_liens}


# --------------------------------------------------------------------------- une règle à la fois

def test_meme_auteur_est_etabli():
    a, b = piece("a", "tiktok:x", 0), piece("b", "tiktok:x", 2)
    assert regles(dependances([a, b])) == {("b", "a", "meme_auteur", "etablie")}


def test_reprise_phrase_8_mots_etablie_5_a_7_probable():
    longue = dependances([piece("a", "t:a", 0, PHRASE_LONGUE), piece("b", "t:b", 1, "Oui : " + PHRASE_LONGUE.upper() + " !")])
    assert ("b", "a", "reprise_phrase", "etablie") in regles(longue)
    moyenne = dependances([piece("a", "t:a", 0, "Info : " + PHRASE_COURTE + " ok"), piece("b", "t:b", 1, "bref " + PHRASE_COURTE)])
    assert ("b", "a", "reprise_phrase", "probable") in regles(moyenne)
    assert not any(d.niveau == "etablie" for d in moyenne)


def test_phrase_de_moins_de_5_mots_ne_compte_pas():
    deps = dependances([piece("a", "t:a", 0, "le pont ferme lundi"), piece("b", "t:b", 1, "le pont ferme lundi")])
    assert deps == []


def test_phrase_ne_traverse_pas_une_limite_de_ligne():
    a = piece("a", "t:a", 0, "le pont ferme pour\ntrois mois des lundi matin")
    b = piece("b", "t:b", 1, "le pont ferme pour trois mois des lundi matin")
    suite = [d for d in dependances([a, b]) if d.regle == "reprise_phrase"]
    assert suite and suite[0].niveau == "probable"  # 7 mots au mieux d'un seul côté de la coupure


def test_citation_url_exacte_etablie_et_domaine_seul_probable():
    cible = piece("p", "journal.test", 0, "article", url="https://www.journal.test/societe/pont-ferme?utm_source=x", nature="page")
    exacte = piece("c1", "autre.test", 1, "voir", url="https://autre.test/1", nature="page",
                   liens_sortants=("http://journal.test/societe/pont-ferme/",))
    domaine = piece("c2", "troisieme.test", 1, "voir", url="https://troisieme.test/1", nature="page",
                    liens_sortants=("https://journal.test/rubrique/autre-sujet",))
    pla = piece("c3", "quatrieme.test", 1, "voir", url="https://quatrieme.test/1", nature="page",
                liens_sortants=("https://www.tiktok.com/@x",))
    deps = regles(dependances([cible, exacte, domaine, pla]))
    assert ("c1", "p", "citation", "etablie") in deps
    assert ("c2", "p", "citation", "probable") in deps
    assert not any(d[0] == "c3" for d in deps)


def test_credit_agence_formes_reconnues():
    for texte in ["PARIS (AFP) - Le pont ferme", "Le pont ferme, avec AFP", "Le pont ferme, selon l'AFP.",
                  "Le pont ferme (Reuters)", "Le pont ferme. Source : AFP", "Le pont ferme (AP)", "avec agences"]:
        assert repérer_credits(texte), texte
    assert not repérer_credits("Photo : Dupont (AFP)")  # crédit d'image
    assert not repérer_credits("la maffia française")
    assert repérer_credits("Source : Le Quotidien Local")[0][0] == "Le Quotidien Local"
    assert not repérer_credits("Source : TikTok")


def test_credit_agence_dependance_vers_noeud_virtuel():
    a = piece("a", "x.test", 0, "Le pont ferme (AFP) pour trois mois", nature="page", credits=("AFP",))
    deps = dependances([a])
    assert ("a", "agence:afp", "credit_agence", "etablie") in regles(deps)


def test_meme_operateur_fort_etabli_poids_probable_banal_ignore():
    a, b, c, d = (piece(x, f"tiktok:{x}", i) for i, x in enumerate("abcd"))
    lien = lambda **kw: {"pivot_type": "portefeuille", "valeur": "w", "comptes": ["tiktok:a", "tiktok:b"],
                         "poids": 0.5, "banal": False, "fort": False, "preuves": [], **kw}
    assert ("b", "a", "meme_operateur", "etablie") in regles(dependances([a, b], liens_pivots=[lien(fort=True)]))
    assert ("b", "a", "meme_operateur", "probable") in regles(dependances([a, b], liens_pivots=[lien(poids=1.5, pivot_type="domaine")]))
    assert dependances([a, b], liens_pivots=[lien(poids=1.4, pivot_type="domaine")]) == []
    assert dependances([a, b], liens_pivots=[lien(fort=True, banal=True)]) == []
    assert dependances([a, b], liens_pivots=[lien(poids=3.0, pivot_type="hashtag")]) == []


def test_copie_texte_jaccard():
    mots = [f"mot{i}" for i in range(60)]
    original = " ".join(mots)
    copie = " ".join(mots[:55] + ["autre", "fin", "du", "texte", "ici"])
    a, b = piece("a", "a.test", 0, original, nature="page"), piece("b", "b.test", 1, copie, nature="page")
    deps = [d for d in dependances([a, b]) if d.regle == "copie_texte"]
    assert deps and deps[0].niveau == "etablie"
    moitie = " ".join(mots[:45] + [f"neuf{i}" for i in range(15)])
    c = piece("c", "c.test", 2, moitie, nature="page")
    deps = [d for d in dependances([a, c]) if d.regle == "copie_texte"]
    assert deps and deps[0].niveau == "probable"
    court = piece("d", "d.test", 3, " ".join(mots[:30]), nature="page")
    assert not [d for d in dependances([a, court]) if d.regle == "copie_texte"]


# --------------------------------------------------------------------------- origines

def test_dix_reprises_d_une_meme_video_donnent_une_origine_et_la_question():
    videos = [{"id": "v0", "pseudo": "origine", "legende": PHRASE_LONGUE, "jour": 0}]
    videos += [{"id": f"v{i}", "pseudo": f"relais{i}", "legende": "partage : " + PHRASE_LONGUE, "jour": i} for i in range(1, 11)]
    res = resume_dossier(export_factice(videos))
    assert (res["n_pieces"], res["n_comptes"], res["k_origines"]) == (11, 11, 1)
    assert res["statut"] == "origine unique : ne pas présenter comme recoupé"
    assert res["question_recoupement"] == "Qui d'autre est au courant, hors de cette origine ?"
    assert res["origines"][0]["racine"] == "v0"
    assert res["phrase"] == "11 pièces · 11 comptes · 1 origine sans lien visible"


def test_trois_comptes_sans_lien_donnent_trois_origines():
    videos = [
        {"id": "v0", "pseudo": "un", "legende": "il pleut beaucoup sur la ville aujourd hui"},
        {"id": "v1", "pseudo": "deux", "legende": "recette de gateau au chocolat tres simple"},
        {"id": "v2", "pseudo": "trois", "legende": "le match de samedi a ete reporte par le club"},
    ]
    res = resume_dossier(export_factice(videos))
    assert res["k_origines"] == res["k_prudent"] == 3
    assert res["statut"] == "plusieurs origines sans lien visible"
    assert res["question_recoupement"] is None
    assert res["phrase"] == "3 pièces · 3 comptes · 3 origines sans lien visible"
    assert len(res["limites"]) >= 2


def test_meme_son_seul_ne_fusionne_pas_mais_baisse_k_prudent():
    videos = [{"id": f"v{i}", "pseudo": f"c{i}", "legende": f"texte different numero {i} sans rapport avec les autres"} for i in range(3)]
    liens = [{"id": f"s{i}", "type_lien": "dit_la_meme_chose", "source_id": f"v{i}", "cible_id": "g", "statut": "prouve",
              "force_points": 1.0, "preuves": [{"url": "https://x.test/", "extrait": "original sound", "debut_s": 0.0}]}
             for i in range(3)]
    res = resume_dossier(export_factice(videos, liens))
    assert res["k_origines"] == 3
    assert res["k_prudent"] == 1
    assert res["liaisons_probables"]


def test_meme_son_par_pivot_est_probable_seulement():
    pieces = [piece("a", "tiktok:a", 0, "un texte"), piece("b", "tiktok:b", 1, "autre chose")]
    pivot = {"pivot_type": "son", "valeur": "tiktok:1", "comptes": ["tiktok:a", "tiktok:b"], "poids": 5.0,
             "banal": False, "fort": True, "preuves": []}
    deps = dependances(pieces, liens_pivots=[pivot])
    assert regles(deps) == {("b", "a", "meme_son", "probable")}
    res = origines(pieces, deps)
    assert (res["k_origines"], res["k_prudent"]) == (2, 1)


def test_six_pages_afp_donnent_une_origine():
    pages = [piece(f"p{i}", f"journal{i}.test", i, f"Le pont ferme (AFP) numero {i} avec un commentaire propre a chaque journal {i}",
                   nature="page", credits=("AFP",)) for i in range(6)]
    res = origines(pages, dependances(pages))
    assert (res["n_pieces"], res["k_origines"]) == (6, 1)
    assert res["origines"][0]["agences"] == ["agence:afp"]
    assert res["origines"][0]["racine"] == "agence:afp"


def test_agence_et_page_de_l_agence_se_rejoignent():
    agence = piece("ag", "afp.com", 0, "depeche sur le pont", nature="page")
    reprise = piece("p", "journal.test", 1, "depeche (AFP) sur le pont", nature="page", credits=("AFP",))
    assert origines([agence, reprise], dependances([agence, reprise]))["k_origines"] == 1


def test_lien_contredit_donne_statut_contestee():
    videos = [{"id": "v0", "pseudo": "un", "legende": "premiere version des faits tres precise"},
              {"id": "v1", "pseudo": "deux", "legende": "version totalement differente des evenements"}]
    liens = [{"id": "x", "type_lien": "contredit", "source_id": "v1", "cible_id": "v0", "statut": "contredit",
              "preuves": [], "force_points": 1.0}]
    res = resume_dossier(export_factice(videos, liens))
    assert res["statut"] == "contestée"
    assert res["liens_contestes"] == ["x"]
    assert res["k_origines"] == 2


def test_lien_reprend_avec_phrase_longue_fusionne_meme_sans_texte_propre():
    videos = [{"id": "v0", "pseudo": "un", "legende": ""}, {"id": "v1", "pseudo": "deux", "legende": ""}]
    liens = [{"id": "r", "type_lien": "reprend", "source_id": "v1", "cible_id": "v0", "statut": "indice", "force_points": 1.0,
              "preuves": [{"url": "https://www.tiktok.com/@deux/video/7001", "extrait": PHRASE_LONGUE},
                          {"url": "https://www.tiktok.com/@un/video/7000", "extrait": PHRASE_LONGUE}]}]
    res = resume_dossier(export_factice(videos, liens))
    assert res["k_origines"] == 1


def test_deux_liens_dit_la_meme_chose_vers_la_meme_grappe():
    videos = [{"id": "v0", "pseudo": "un", "legende": ""}, {"id": "v1", "pseudo": "deux", "legende": ""}]
    liens = [{"id": f"d{i}", "type_lien": "dit_la_meme_chose", "source_id": f"v{i}", "cible_id": "g", "statut": "prouve",
              "force_points": 2.0,
              "preuves": [{"url": f"https://www.tiktok.com/@{'un' if i == 0 else 'deux'}/video/{7000 + i}", "extrait": PHRASE_MOYENNE}]}
             for i in range(2)]
    res = resume_dossier(export_factice(videos, liens))
    assert (res["k_origines"], res["k_prudent"]) == (1, 1)  # 8 mots : établie


def test_k_prudent_jamais_superieur_a_k_origines():
    liens_pivots_possibles = [
        [], [{"pivot_type": "domaine", "valeur": "d", "comptes": ["t:a", "t:b"], "poids": 2.0, "banal": False, "fort": False, "preuves": []}],
        [{"pivot_type": "son", "valeur": "s", "comptes": ["t:a", "t:c", "t:d"], "poids": 1.0, "banal": False, "fort": False, "preuves": []}],
    ]
    textes = ["", "le pont ferme pour trois mois des lundi matin", "pont ferme pour trois mois ok", "autre sujet complet et long ici"]
    for sel in itertools.product(range(len(liens_pivots_possibles)), range(3)):
        pieces = [piece(x, f"t:{x}", i, textes[(i + sel[1]) % 4]) for i, x in enumerate("abcd")]
        res = origines(pieces, dependances(pieces, liens_pivots=liens_pivots_possibles[sel[0]]))
        assert res["k_prudent"] <= res["k_origines"] <= res["n_pieces"]


def test_entree_vide_et_pas_de_plantage():
    res = origines([], [])
    assert res["k_origines"] == 0 and res["statut"] == "aucune pièce"


# --------------------------------------------------------------------------- lecture de l'export, pages, sous-ensemble

def test_depuis_export_retrouve_auteur_date_et_texte():
    export = export_factice([{"id": "v0", "pseudo": "Un", "legende": "Bonjour https://journal.test/a (AFP)"}])
    pieces = {p.id: p for p in depuis_export(export)}
    v = pieces["v0"]
    assert (v.nature, v.auteur, v.date) == ("video", "tiktok:un", DEBUT)
    assert v.liens_sortants == ("https://journal.test/a",)
    assert v.credits == ("AFP",)
    assert pieces["c0"].nature == "compte"


def test_pages_html_sans_menu_ni_pied_de_page():
    pages = [{"id": "p1", "url": "https://www.journal.test/a", "texte": "", "publication": "2026-11-02T10:00:00Z",
              "html": '<nav><a href="https://faux.test/menu">m</a></nav><article><p>Texte (AFP) utile</p>'
                      '<a href="https://source.test/doc">doc</a></article><footer><a href="https://faux.test/pied">p</a></footer>'}]
    p = depuis_export({"dossiers": [], "liens": []}, pages=pages)[0]
    assert p.auteur == "journal.test" and p.liens_sortants == ("https://source.test/doc",)
    assert p.credits == ("AFP",) and p.date == datetime(2026, 11, 2, 10, tzinfo=UTC)


def test_pour_affirmation_garde_les_liens_passant_par_l_exterieur():
    videos = [{"id": "v0", "pseudo": "o", "legende": PHRASE_LONGUE, "jour": 0},
              {"id": "v1", "pseudo": "a", "legende": PHRASE_LONGUE, "jour": 1},
              {"id": "v2", "pseudo": "b", "legende": PHRASE_LONGUE, "jour": 2},
              {"id": "v3", "pseudo": "z", "legende": "sujet sans rapport du tout avec le reste", "jour": 3}]
    export = export_factice(videos)
    res = pour_affirmation(export, ["v1", "v2", "v3"])
    assert (res["n_pieces"], res["k_origines"]) == (3, 2)  # v1 et v2 remontent à v0, hors du sous-ensemble
    assert resume_dossier(export)["k_origines"] == 2


def test_medias_cites_comme_source_et_listes():
    noms = lambda t: {n for n, _ in repérer_credits(t)}
    assert noms("Le Financial Times first reported the memo") == {"Financial Times"}
    assert noms("une note consultée par CNBC et le Financial Times.") == {"CNBC", "Financial Times"}
    assert noms("according to a report from Reuters") == {"Reuters"}
    assert noms("Cover Image Source: Blue Origin") == set()  # crédit d'image
    assert noms("SNAP said hello") == set()


def test_deux_pages_qui_nomment_le_meme_media_ont_une_origine():
    pages = [
        {"id": "a", "url": "https://un.test/a", "texte": "Le pont ferme, d'après Bloomberg, hier soir."},
        {"id": "b", "url": "https://deux.test/b", "texte": "Pont fermé : selon Bloomberg, des travaux durent trois mois."},
        {"id": "c", "url": "https://trois.test/c", "texte": "Un sujet totalement différent sur la météo."},
    ]
    pieces = depuis_export({"dossiers": [], "liens": []}, pages=pages)
    res = origines(pieces, dependances(pieces))
    assert (res["n_pieces"], res["k_origines"]) == (3, 2)


def test_entites_html_decodees_dans_les_pages():
    pages = [{"id": "p", "url": "https://un.test/a", "texte": "", "html": "<p>consult&eacute;e par CNBC</p>"}]
    assert depuis_export({"dossiers": [], "liens": []}, pages=pages)[0].credits == ("CNBC",)
