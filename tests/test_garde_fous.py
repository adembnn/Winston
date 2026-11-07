"""Garde-fous de publication : mineurs, particuliers, mis en cause, mots interdits, export."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from enquete.garde_fous import (
    MARQUE_RETENUE,
    ErreurGardeFou,
    Masqueur,
    Profil,
    est_particulier,
    exiger_export_propre,
    indices_mineur,
    mineur_probable,
    mots_interdits,
    profils_depuis_cache,
    valider_mis_en_cause,
    verifier_export,
    verifier_requete_externe,
)

RACINE_PASSAGE = Path(
    os.environ.get(
        "ENQUETE_PASSAGE",
        ".",
    )
)


def _export():
    """Petit export construit : un compte public, un particulier, un mineur probable."""
    def preuve(url, extrait):
        return {"url": url, "extrait": extrait, "source_donnee": "Oriane"}

    return {
        "question": "Que s'est-il passé ?",
        "dossiers": [
            {
                "role": "amplificateur",
                "nom": "Relais",
                "pieces": [
                    {
                        "id": "a", "type": "compte", "libelle": "@grandmedia (tiktok)",
                        "attributs": {"abonnes": 2_400_000},
                        "preuves": [preuve("https://www.tiktok.com/@grandmedia/video/1", "séisme en direct avec @voisin_lambda")],
                    },
                    {
                        "id": "b", "type": "compte", "libelle": "@monique.dupont (tiktok)",
                        "attributs": {"abonnes": 312},
                        "preuves": [preuve("https://www.tiktok.com/@monique.dupont/video/2", "ça tremble chez moi")],
                    },
                    {
                        "id": "c", "type": "compte", "libelle": "@lilou_2010 (instagram)",
                        "attributs": {"abonnes": 90},
                        "preuves": [preuve("https://www.instagram.com/reel/AbC/", "j'ai 14 ans et j'ai eu peur")],
                    },
                ],
            }
        ],
        "liens": [
            {"id": "l", "explication": "Le compte @monique.dupont relaie @grandmedia.", "source_libelle": "@monique.dupont (tiktok)"}
        ],
    }


# 1 -- mineurs ---------------------------------------------------------------

def test_mineurs_reperes_et_faux_positifs_evites():
    assert mineur_probable("j'ai 14 ans et j'adore ça").accepte
    assert mineur_probable("lycéenne en terminale").accepte
    assert mineur_probable("my 12 year old daughter filmed this").accepte
    assert mineur_probable("né en 2011", annee=2026).accepte
    assert mineur_probable("high school freshman").accepte
    # Des durées et des adultes ne sont pas des mineurs.
    assert not mineur_probable("depuis 5 ans le pont est fermé").accepte
    assert not mineur_probable("il a été condamné à 10 ans de prison").accepte
    assert not mineur_probable("j'ai 34 ans").accepte
    assert not mineur_probable("né en 1990", annee=2026).accepte
    assert indices_mineur("") == []


# 2 -- particuliers -----------------------------------------------------------

def test_particulier_ou_personne_publique():
    assert est_particulier(Profil("a", abonnes=312))
    assert est_particulier(Profil("a"))  # inconnu : prudence
    assert not est_particulier(Profil("a", abonnes=2_400_000))
    assert not est_particulier(Profil("a", abonnes=50, verifie=True))
    assert not est_particulier(Profil("a", abonnes=800, bio="Chaîne officielle du journal local"))


# 3 -- masquage ---------------------------------------------------------------

def test_masquage_pseudos_adresses_et_mentions():
    ex = _export()
    m = Masqueur(sel="s").decouvrir(ex)
    assert m.particuliers == {"monique.dupont", "lilou_2010", "voisin_lambda"} | set()
    assert m.publics == {"grandmedia"}
    sortie = json.dumps(m.masquer(ex), ensure_ascii=False)
    for secret in ("monique.dupont", "lilou_2010", "voisin_lambda"):
        assert secret not in sortie
    assert "@grandmedia" in sortie  # personne publique conservée
    assert "compte privé" in sortie
    # L'original n'est pas modifié.
    assert "monique.dupont" in json.dumps(ex)


def test_pseudonyme_stable_et_lie_au_sel():
    a, b = Masqueur(sel="un"), Masqueur(sel="un")
    assert a.pseudonyme("Monique.Dupont") == b.pseudonyme("monique.dupont")
    assert a.pseudonyme("x") != Masqueur(sel="deux").pseudonyme("x")


def test_mineur_probable_retient_le_contenu():
    ex = _export()
    m = Masqueur(sel="s", annee=2026).decouvrir(ex)
    assert "lilou_2010" in m.mineurs
    sortie = m.masquer(ex)
    piece = sortie["dossiers"][0]["pieces"][2]
    assert piece["preuves"][0]["extrait"] == MARQUE_RETENUE
    assert "14 ans" not in json.dumps(sortie, ensure_ascii=False)
    # Un adulte garde sa citation.
    assert sortie["dossiers"][0]["pieces"][0]["preuves"][0]["extrait"] != MARQUE_RETENUE


# 4 -- mis en cause -------------------------------------------------------------

def test_mis_en_cause_exige_source_a_ou_b_et_contradictoire():
    ok = {"statut": "envoyée", "date": "2026-11-12", "canal": "courriel"}
    assert valider_mis_en_cause(["B", "D"], ok)
    assert valider_mis_en_cause([{"fiabilite_source": "A"}], {"statut": "à envoyer"})
    assert not valider_mis_en_cause(["C", "D"], ok)  # aucune source A ou B
    assert not valider_mis_en_cause(["A"], None)  # pas de contradictoire
    assert not valider_mis_en_cause(["A"], {"statut": "envoyée"})  # ni date ni canal
    assert not valider_mis_en_cause(["A"], {"statut": "contacté"})  # statut hors lexique
    v = valider_mis_en_cause(["F"], None)
    assert len(v.raisons) == 2


# 5 -- mots interdits ---------------------------------------------------------

def test_mots_interdits_du_lexique():
    cas = {
        "Il est coupable.": "culpabilité",
        "Un mis en examen accusé de fraude": "culpabilité",
        "C'est une arnaque": "verdict",
        "Il se mure dans le silence": "insinuation",
        "confirmé par 47 vidéos": "fausse source",
        "lien prouvé": "fausse garantie",
        "87 % de correspondance": "fausse garantie",
        "texte validé juridiquement": "garantie juridique",
        "notre informateur": "source humaine",
        "chaque nœud et chaque grappe": "mot de logiciel",
        "Ta question": "tutoiement",
        "un lien fort": "ancien mot",
    }
    for texte, categorie in cas.items():
        assert categorie in {t.categorie for t in mots_interdits(texte)}, texte


def test_mots_permis():
    # Le verbe « accuse » est permis ; le participe « accusé » ne l'est pas.
    assert not mots_interdits("Le maire accuse la préfecture d'un retard.")
    assert [t.mot for t in mots_interdits("un homme accusé de fraude")] == ["accusé"]
    # Un verdict cité entre guillemets avec sa source est toléré.
    assert not mots_interdits("Selon le parquet, l'opération est une « arnaque ».")
    assert mots_interdits("Selon le parquet, l'opération est une arnaque.")
    assert not mots_interdits("Le conseil a voté le budget mardi.")
    assert not mots_interdits("Elle est responsable de la rédaction.")


# 6 -- export -----------------------------------------------------------------

def test_export_brut_refuse_export_masque_accepte():
    ex = _export()
    assert not verifier_export(ex)  # pseudos lisibles
    propre = exiger_export_propre(ex, Masqueur(sel="s", annee=2026))
    assert verifier_export(propre, Masqueur(sel="s", annee=2026).decouvrir(ex))


def test_export_avec_mot_interdit_et_mis_en_cause_faible():
    ex = _export()
    ex["liens"][0]["explication"] = "C'est une arnaque."
    ex["dossiers"].append(
        {"role": "mis_en_cause", "nom": "Mis en cause", "pieces": [
            {"id": "z", "type": "compte", "libelle": "@grandmedia (tiktok)", "solidite": {"fiabilite_source": "D"}}
        ]}
    )
    with pytest.raises(ErreurGardeFou) as e:
        exiger_export_propre(ex, Masqueur(sel="s", annee=2026))
    assert "mot interdit" in str(e.value) and "mis en cause" in str(e.value)


def test_requete_externe_refusee_si_pseudo_de_particulier():
    ex = _export()
    m = Masqueur(sel="s").decouvrir(ex)
    with pytest.raises(ErreurGardeFou):
        verifier_requete_externe("qui est @monique.dupont ?", m)
    verifier_requete_externe("séisme Colombie août 2026", m)


# 7 -- le vrai export du premier passage -------------------------------------

def test_export_reel_sans_pseudo_de_particulier_lisible():
    export = RACINE_PASSAGE / "donnees/affaire-10-seismes/reseau.json"
    cache = RACINE_PASSAGE / "cache/oriane"
    if not export.exists() or not cache.exists():
        pytest.skip("données du premier passage absentes")
    ex = json.loads(export.read_text(encoding="utf-8"))
    m = Masqueur(profils_depuis_cache(cache), sel="essai").decouvrir(ex)
    assert m.particuliers  # le contrôle ne serait pas probant sans particulier
    assert m.pseudos_lisibles(ex)  # lisibles avant masquage
    assert m.pseudos_lisibles(m.masquer(ex)) == []  # et plus aucun après
