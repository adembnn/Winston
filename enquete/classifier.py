"""Classificateur : des catégories propres à chaque enquête, en un seul appel Claude.

Usage : python -m enquete.classifier <reseau.json>
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

MODELE = "sonnet"
MAX_PIECES = 300
MAX_EXTRAIT = 200
DOSSIER_CACHE = Path(__file__).resolve().parent.parent / "cache" / "classifier"
MOTS_INTERDITS = ("arnaque", "coupable", "fraudeur", "escroc")
# prénom + nom : deux mots consécutifs à majuscule initiale
_NOM_PROPRE = re.compile(r"^[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’-]+\s+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’-]+$")  # « Intrusion Hugging Face » passe (marque)


def _pieces(export: dict) -> list[dict]:
    return [p for d in export.get("dossiers", []) for p in d.get("pieces", [])]


def _extrait(piece: dict) -> str:
    for preuve in piece.get("preuves") or []:
        if preuve.get("extrait"):
            return str(preuve["extrait"])[:MAX_EXTRAIT]
    note = (piece.get("attributs") or {}).get("note")
    return str(note)[:MAX_EXTRAIT] if note else ""


def _prompt(question: str, pieces: list[dict]) -> str:
    compact = [{"id": p["id"], "titre": p.get("libelle", ""), "extrait": _extrait(p)} for p in pieces]
    return (
        "Tu aides une rédaction d'investigation à ranger les pièces d'une enquête.\n"
        f"Question de l'enquête : {question}\n\n"
        "Crée de 5 à 8 catégories PROPRES À CETTE ENQUÊTE (pas des catégories génériques). "
        "Chaque catégorie : `id` court (minuscules, sans espace), `nom` de 3 mots au plus en "
        "vocabulaire de rédaction (jamais un nom de personne, jamais de mot de culpabilité), "
        "`definition` d'une phrase, `pieces` (liste d'ids). Chaque pièce va dans UNE seule catégorie ; "
        "ce qui ne rentre nulle part va dans `autres`. N'invente aucun id.\n"
        'Réponds en JSON strict, sans texte autour : {"categories":[{"id":"","nom":"","definition":"","pieces":[]}],"autres":[]}\n\n'
        f"Pièces :\n{json.dumps(compact, ensure_ascii=False)}"
    )


def appeler_claude(prompt: str) -> str:
    """Un appel `claude -p` ; la consigne passe par l'entrée standard (limite Windows)."""
    r = subprocess.run(
        ["claude", "-p", "--model", MODELE, "--output-format", "json"],
        input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=300,
        shell=sys.platform == "win32",
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"claude a rendu le code {r.returncode}")
    try:
        enveloppe = json.loads(r.stdout)
        return enveloppe["result"] if isinstance(enveloppe, dict) and "result" in enveloppe else r.stdout
    except json.JSONDecodeError:
        return r.stdout


def _extraire_json(texte: str) -> dict:
    texte = texte.strip()
    texte = re.sub(r"^```(?:json)?\s*|\s*```$", "", texte)
    try:
        return json.loads(texte)
    except json.JSONDecodeError:
        debut, fin = texte.find("{"), texte.rfind("}")
        if debut < 0 or fin <= debut:
            raise
        return json.loads(texte[debut:fin + 1])


def _nom_valide(nom: str) -> bool:
    bas = nom.lower()
    return (
        bool(nom.strip())
        and len(nom.split()) <= 5  # la consigne dit 3 ; on tolère un léger dépassement plutôt que de tout refuser
        and not any(m in bas for m in MOTS_INTERDITS)
        and not _NOM_PROPRE.match(nom.strip())
    )


def _valider(brut: dict, ids: list[str]) -> dict:
    """Contrôle par code. Lève ValueError si la réponse est inutilisable."""
    connus, vus = set(ids), set()
    categories = []
    liste = brut.get("categories")
    if not isinstance(liste, list):
        raise ValueError("pas de liste de catégories")
    for c in liste:
        nom = str(c.get("nom", "")).strip()
        if not _nom_valide(nom):
            raise ValueError(f"nom de catégorie refusé : {nom!r}")
        mes = []
        for i in c.get("pieces", []):
            if i in connus and i not in vus:
                vus.add(i)
                mes.append(i)
        categories.append({
            "id": str(c.get("id") or nom.lower().replace(" ", "-")),
            "nom": nom, "definition": str(c.get("definition", "")).strip(),
            "pieces": mes, "nombre": len(mes),
        })
    if not 1 <= len(categories) <= 12:
        raise ValueError("nombre de catégories incohérent")
    return {"categories": categories, "non_classees": [i for i in ids if i not in vus]}


def _repli(export: dict, ids: list[str]) -> dict:
    categories = []
    for d in export.get("dossiers", []):
        mes = [p["id"] for p in d.get("pieces", []) if p["id"] in ids]
        if mes:
            categories.append({"id": d.get("role", d.get("nom", "")), "nom": d.get("nom", ""),
                               "definition": "Rôle dans l'affaire.", "pieces": mes, "nombre": len(mes)})
    return {"categories": categories, "non_classees": [], "modele": None, "repli": True}


def categoriser(export: dict, appeler: Callable[[str], str] | None = None) -> dict:
    question = export.get("question", "")
    pieces = _pieces(export)[:MAX_PIECES]
    ids = [p["id"] for p in pieces]
    cle = hashlib.sha256(json.dumps([question, ids], ensure_ascii=False).encode()).hexdigest()[:24]
    fichier = DOSSIER_CACHE / f"{cle}.json"
    if appeler is None:
        try:
            return json.loads(fichier.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    try:
        brut = _extraire_json((appeler or appeler_claude)(_prompt(question, pieces)))
        res = _valider(brut, ids)
        resultat = {**res, "modele": MODELE, "repli": False}
    except Exception:
        return _repli(export, ids)
    if appeler is None:
        try:
            DOSSIER_CACHE.mkdir(parents=True, exist_ok=True)
            fichier.write_text(json.dumps(resultat, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return resultat


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage : python -m enquete.classifier <reseau.json>")
        return 2
    export = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    res = categoriser(export)
    print(f"modèle : {res['modele']} · repli : {res['repli']}")
    for c in res["categories"]:
        print(f"- {c['nom']} ({c['nombre']}) : {c['definition']}")
    print(f"non classées : {len(res['non_classees'])}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
