"""Rend un export du réseau comparable d'un passage à l'autre.

Les identifiants sont tirés au hasard et les dates viennent de l'horloge : on remplace chaque
identifiant par son rang d'apparition et on retire dates et numéros de version. Tout le reste
(libellés, rôles, preuves, extraits, secondes, empreintes, ordre) doit être identique.
"""

from __future__ import annotations

from typing import Any

CLES_RETIREES = {"date_ajout", "date_collecte", "version"}
# Ajoutées après la référence « avant le flux » : les images des pièces ne changent pas la structure.
CLES_AJOUTEES_DEPUIS = {"image", "images_cles"}


def normaliser(export: dict[str, Any]) -> dict[str, Any]:
    ordre: list[str] = []
    for dossier in export["dossiers"]:
        ordre += [p["id"] for p in dossier["pieces"]]
    ordre += [l["id"] for l in export["liens"]]
    ordre += [q["id"] for q in export["questions_ouvertes"]]
    rang = {identifiant: f"id{i}" for i, identifiant in enumerate(ordre)}

    def aller(valeur: Any) -> Any:
        if isinstance(valeur, dict):
            retirees = CLES_RETIREES | CLES_AJOUTEES_DEPUIS | ({"date"} if "type_lien" in valeur else set())
            return {k: aller(v) for k, v in valeur.items() if k not in retirees}
        if isinstance(valeur, list):
            return [aller(v) for v in valeur]
        if isinstance(valeur, str):
            return rang.get(valeur, valeur)
        return valeur

    return aller(export)
