"""Contrôle des preuves : 10 citations vraies acceptées, 10 fausses refusées.

Les données viennent du vrai cache du premier passage (réponses Oriane de
l'affaire 10). Le dossier se règle par la variable ENQUETE_CACHE_ORIANE ; s'il
est absent, les tests sur le vrai cache sont ignorés et ceux sur données
construites tournent seuls.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from enquete.controle_preuve import (
    REFUS,
    cle_publication,
    normaliser,
    sources_depuis_cache,
    verifier_preuve,
)

CACHE = Path(
    os.environ.get(
        "ENQUETE_CACHE_ORIANE",
        "cache/oriane",
    )
)

URL = {
    "tiktok": "https://www.tiktok.com/@{pseudo}/video/{id}",
    "instagram": "https://www.instagram.com/reel/{id}/",
    "youtube": "https://www.youtube.com/watch?v={id}",
}


def _url(r):
    return URL[r["platform"]].format(pseudo=r["profileHandle"], id=r["platformId"])


def _morceaux_parole(r):
    return [
        c
        for m in r.get("media") or []
        for c in m.get("transcriptChunks") or []
        if len(normaliser(c.get("text") or "").split()) >= 4
    ]


@pytest.fixture(scope="module")
def sources():
    if not CACHE.exists():
        pytest.skip("cache Oriane du premier passage absent")
    s = sources_depuis_cache(CACHE)
    assert len(s) > 100
    return s


@pytest.fixture(scope="module")
def avec_parole(sources):
    """Vidéos à parole horodatée, ordre stable, une seule par pseudo, phrase unique."""
    vues, pseudos, sortie = {}, set(), []
    for r in sorted(sources, key=lambda x: x["id"]):
        morceaux = _morceaux_parole(r)
        if not morceaux or r["profileHandle"] in pseudos:
            continue
        # Une phrase doit être unique dans la vidéo pour que la seconde soit sans ambiguïté.
        textes = [normaliser(c["text"]) for m in r["media"] for c in m.get("transcriptChunks") or []]
        uniques = [c for c in morceaux if textes.count(normaliser(c["text"])) == 1]
        if uniques:
            pseudos.add(r["profileHandle"])
            sortie.append((r, uniques[0]))
    assert len(sortie) >= 12
    return sortie


def _preuve(r, texte, debut=None, fin=None):
    return {"url": _url(r), "extrait": texte, "debut_s": debut, "fin_s": fin}


def test_dix_citations_vraies_acceptees(sources, avec_parole):
    vraies = []
    # 6 phrases dites, avec leur seconde.
    for r, c in avec_parole[:6]:
        vraies.append(_preuve(r, c["text"], c["startSeconds"], c["endSeconds"]))
    # 1 phrase dite, sans seconde, citée en minuscules et sans ponctuation.
    r, c = avec_parole[6]
    vraies.append(_preuve(r, normaliser(c["text"])))
    # 1 phrase dite, avec une seconde décalée de 2 s (tolérance).
    r, c = avec_parole[7]
    vraies.append(_preuve(r, c["text"], c["startSeconds"] + 2, c["endSeconds"] + 2))
    # 1 citation en deux morceaux séparés par « … » dans la même vidéo.
    r, c = avec_parole[8]
    mots = c["text"].split()
    vraies.append(_preuve(r, " ".join(mots[:2]) + " … " + " ".join(mots[-2:])))
    # 1 légende.
    legendes = [x for x in sources if len(normaliser(x.get("caption") or "").split()) >= 6]
    assert legendes
    x = sorted(legendes, key=lambda y: y["id"])[0]
    vraies.append(_preuve(x, x["caption"]))
    assert len(vraies) == 10

    journal: list = []
    refuses = [(p, verifier_preuve(p, sources, journal=journal)) for p in vraies]
    assert [(p["extrait"][:40], v.raison) for p, v in refuses if not v.accepte] == []
    assert journal == []


def test_dix_citations_fausses_refusees(sources, avec_parole):
    fausses = []
    (ra, ca), (rb, cb), (rc, cc), (rd, cd), (re_, ce) = avec_parole[:5]
    # 1. un mot de la phrase est changé
    mots = ca["text"].split()
    mots[len(mots) // 2] = "trahison"
    fausses.append(_preuve(ra, " ".join(mots), ca["startSeconds"], ca["endSeconds"]))
    # 2. phrase d'une autre vidéo attribuée à celle-ci
    fausses.append(_preuve(ra, cb["text"], cb["startSeconds"], cb["endSeconds"]))
    # 3. phrase vraie, seconde fausse de 30 s
    fausses.append(_preuve(rb, cb["text"], cb["startSeconds"] + 30, cb["endSeconds"] + 30))
    # 4. phrase vraie, avec des mots inventés ajoutés à la fin
    fausses.append(_preuve(rc, cc["text"] + " et le ministre a reconnu les faits"))
    # 5. adresse qui n'est dans aucune source
    fausses.append({"url": "https://www.tiktok.com/@personne/video/1234567890123456789", "extrait": cd["text"]})
    # 6. phrase entièrement inventée
    fausses.append(_preuve(rd, "le directeur a avoué avoir falsifié les mesures"))
    # 7. extrait trop court
    fausses.append(_preuve(re_, "Get down"))
    # 8. emojis seuls
    fausses.append(_preuve(re_, "😭🙏🔥"))
    # 9. deux morceaux dans l'ordre inverse
    mots = cc["text"].split()
    fausses.append(_preuve(rc, " ".join(mots[-2:]) + " … " + " ".join(mots[:2])))
    # 10. légende vraie mais seconde revendiquée : la légende n'a pas de seconde
    legendes = [x for x in sources if len(normaliser(x.get("caption") or "").split()) >= 6]
    x = sorted(legendes, key=lambda y: y["id"])[0]
    fausses.append(_preuve(x, x["caption"], 12, 15))
    assert len(fausses) == 10

    journal: list = []
    avant = len(REFUS)
    verdicts = [verifier_preuve(p, sources, journal=journal) for p in fausses]
    acceptees = [(p["extrait"][:40], v.raison) for p, v in zip(fausses, verdicts) if v.accepte]
    assert acceptees == []
    # Chaque refus est journalisé avec sa raison.
    assert len(journal) == 10 and len(REFUS) - avant == 10
    assert all(ligne["raison"] and ligne["url"] for ligne in journal)


def test_journal_dans_un_fichier(tmp_path):
    fichier = tmp_path / "refus.jsonl"
    src = [{"url": "https://exemple.org/a", "texte": "le conseil a voté le budget mardi soir"}]
    v = verifier_preuve({"url": "https://exemple.org/a", "extrait": "le conseil a rejeté le budget"}, src, journal=fichier)
    assert not v.accepte
    assert "introuvable" in fichier.read_text(encoding="utf-8")


# --- Données construites : tournent partout -------------------------------

OR = {
    "platform": "tiktok",
    "platformId": "7600000000000000001",
    "profileHandle": "compte.test",
    "caption": "Le maire annonce la réouverture du pont lundi #pont",
    "media": [
        {
            "type": "video",
            "onScreenText": ["PONT FERMÉ", "réouverture lundi 8 h"],
            "transcriptChunks": [
                {"startSeconds": 0, "endSeconds": 4, "text": "Le maire vient de confirmer"},
                {"startSeconds": 4, "endSeconds": 9, "text": "que le pont rouvrira lundi matin."},
                {"startSeconds": 30, "endSeconds": 34, "text": "Les travaux ont coûté deux millions."},
            ],
        }
    ],
}
U = "https://www.tiktok.com/@compte.test/video/7600000000000000001"


def test_phrase_a_cheval_sur_deux_morceaux():
    v = verifier_preuve({"url": U, "extrait": "confirmer que le pont rouvrira", "debut_s": 3, "fin_s": 6}, [OR])
    assert v.accepte and v.methode == "parole"


def test_seconde_hors_tolerance():
    v = verifier_preuve({"url": U, "extrait": "Les travaux ont coûté deux millions", "debut_s": 5, "fin_s": 8}, [OR])
    assert not v.accepte and "absent de la parole" in v.raison


def test_texte_a_l_ecran_accepte_sans_seconde_refuse_avec():
    sans = verifier_preuve({"url": U, "extrait": "réouverture lundi 8 h"}, [OR])
    avec = verifier_preuve({"url": U, "extrait": "réouverture lundi 8 h", "debut_s": 2}, [OR])
    assert sans.accepte and sans.methode == "texte à l'écran"
    assert not avec.accepte and "invérifiable" in avec.raison


def test_page_archivee_html():
    page = {"url": "https://exemple.org/article?utm=1", "html": "<html><script>var a='x y z w'</script><p>Le préfet a signé l&apos;arrêté le <b>3 novembre</b> à Lyon.</p></html>"}
    ok = verifier_preuve({"url": "https://www.exemple.org/article/", "extrait": "a signé l'arrêté le 3 novembre"}, [page])
    faux = verifier_preuve({"url": "https://exemple.org/article", "extrait": "a signé l'arrêté le 4 novembre"}, [page])
    script = verifier_preuve({"url": "https://exemple.org/article", "extrait": "var a x y z w"}, [page])
    assert ok.accepte and ok.methode == "page archivée"
    assert not faux.accepte and not script.accepte


def test_dictionnaire_adresse_vers_texte_et_objet_preuve():
    from datetime import datetime, timezone

    from enquete.reseau import Preuve

    p = Preuve("https://exemple.org/x", "le budget a été voté mardi", "presse", datetime.now(timezone.utc))
    assert verifier_preuve(p, {"https://exemple.org/x": "Hier, le budget a été voté mardi soir."}).accepte
    assert not verifier_preuve(p, {"https://exemple.org/x": "Hier, le budget a été rejeté."}).accepte


def test_cle_publication():
    assert cle_publication(U) == ("tiktok", "7600000000000000001")
    assert cle_publication("https://www.instagram.com/reel/AbC-d_1/") == ("instagram", "AbC-d_1")
    assert cle_publication("https://youtu.be/abcdefghijk?t=3") == ("youtube", "abcdefghijk")
    assert cle_publication("https://exemple.org/") is None
