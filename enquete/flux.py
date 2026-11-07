"""Le flux de l'enquête : le journal SQLite lu comme un fil d'actualité, une ligne JSON par événement.

Image simple : la chaîne écrit dans un cahier pendant qu'un lecteur le suit par-dessus son épaule.
La lecture est en lecture seule et sûre pendant qu'un autre processus écrit.

Une ligne : {"seq", "t", "genre", "objet_id", "version", "motif", "donnees"}. `seq`, `genre`,
`objet_id`, `version`, `motif` et `donnees` sont ceux de `reseau.Evenement` ; `t` compte les secondes
écoulées depuis le premier événement du journal (horloge réelle), jamais en arrière.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from enquete.reseau import Evenement

_REQUETE = "SELECT seq, date, genre, objet_id, version, motif, donnees FROM evenements"


def ligne(evenement: Evenement, t0: datetime) -> dict[str, Any]:
    """L'enveloppe du contrat pour un événement ; `t0` est la date du tout premier événement."""
    t = round((evenement.date - t0).total_seconds(), 3)
    return {
        "seq": evenement.seq,
        "t": max(0.0, t),
        "genre": evenement.genre,
        "objet_id": evenement.objet_id,
        "version": evenement.version,
        "motif": evenement.motif,
        "donnees": evenement.donnees,
    }


def _ouvrir_en_lecture(chemin: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{chemin.resolve().as_uri()}?mode=ro", uri=True, timeout=10)


def lire_depuis(chemin_sqlite: str | Path, apres_seq: int = 0) -> list[dict[str, Any]]:
    """Les lignes dont le `seq` dépasse `apres_seq`, dans l'ordre. Liste vide si rien de nouveau.

    Ouvre le fichier en lecture seule, le referme aussitôt : on peut l'appeler en boucle pendant
    que la chaîne écrit. Un fichier absent, ou pas encore initialisé, donne une liste vide.
    """
    chemin = Path(chemin_sqlite)
    if not chemin.exists():
        return []
    try:
        bd = _ouvrir_en_lecture(chemin)
    except sqlite3.Error:
        return []
    try:
        premier = bd.execute("SELECT date FROM evenements ORDER BY seq LIMIT 1").fetchone()
        if premier is None:
            return []
        lignes = bd.execute(_REQUETE + " WHERE seq > ? ORDER BY seq", (apres_seq,)).fetchall()
    except sqlite3.OperationalError:
        return []  # la table n'existe pas encore
    finally:
        bd.close()

    t0 = datetime.fromisoformat(premier[0])
    sortie: list[dict[str, Any]] = []
    plus_tard = 0.0
    for seq, date, genre, objet_id, version, motif, donnees in lignes:
        evenement = Evenement(seq, datetime.fromisoformat(date), genre, objet_id, version, motif,
                              json.loads(donnees))
        enveloppe = ligne(evenement, t0)
        plus_tard = enveloppe["t"] = max(plus_tard, enveloppe["t"])
        sortie.append(enveloppe)
    return sortie


def exporter_jsonl(chemin_sqlite: str | Path, sortie: str | Path) -> int:
    """Écrit tout le journal en .jsonl au contrat ; renvoie le nombre de lignes."""
    lignes = lire_depuis(chemin_sqlite)
    with open(sortie, "w", encoding="utf-8", newline="\n") as f:
        for une in lignes:
            f.write(json.dumps(une, ensure_ascii=False) + "\n")
    return len(lignes)
