import json

from enquete.classifier import categoriser

EXPORT = {
    "question": "Que s'est-il passé ?",
    "dossiers": [
        {"role": "origine", "nom": "À l'origine", "pieces": [{"id": "a", "libelle": "A"}, {"id": "b", "libelle": "B"}]},
        {"role": "experts", "nom": "Experts", "pieces": [{"id": "c", "libelle": "C"}]},
    ],
}


def rep(cats, autres=()):
    return lambda prompt: json.dumps({"categories": cats, "autres": list(autres)})


def cat(nom, pieces):
    return {"id": nom[:3].lower(), "nom": nom, "definition": "Une phrase.", "pieces": pieces}


def test_nominal():
    r = categoriser(EXPORT, rep([cat("Sources primaires", ["a", "b"]), cat("Voix expertes", ["c"])]))
    assert r["repli"] is False
    assert [c["nombre"] for c in r["categories"]] == [2, 1]
    assert r["non_classees"] == []


def test_id_invente_retire():
    r = categoriser(EXPORT, rep([cat("Sources primaires", ["a", "zzz"])]))
    assert r["categories"][0]["pieces"] == ["a"]
    assert r["non_classees"] == ["b", "c"]


def test_mot_interdit_et_nom_propre_replient():
    for nom in ("Les arnaques", "Jean Dupont"):
        r = categoriser(EXPORT, rep([cat(nom, ["a"])]))
        assert r["repli"] is True


def test_repli_sur_erreur_et_sur_reponse_illisible():
    def boom(_):
        raise RuntimeError("claude absent")
    for f in (boom, lambda p: "pas du json"):
        r = categoriser(EXPORT, f)
        assert r["repli"] is True
        assert [c["nom"] for c in r["categories"]] == ["À l'origine", "Experts"]


def test_prompt_plafonne_a_300_pieces():
    grand = {"question": "q", "dossiers": [{"role": "x", "nom": "X", "pieces": [{"id": str(i), "libelle": "l"} for i in range(400)]}]}
    vus = []
    categoriser(grand, lambda p: vus.append(p) or "{}")
    assert '"id": "299"' in vus[0] and '"id": "300"' not in vus[0]
