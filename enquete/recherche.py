"""Moteur de recherche dans tout le dossier d'une enquête.

Image simple : un index de livre. On relit l'export du réseau, on range chaque
morceau de texte (phrase dite, légende, compte, document...) dans un index
plein texte SQLite (FTS5), et chaque résultat garde de quoi rouvrir la pièce :
son identifiant, la seconde de la phrase, l'adresse du post d'origine.

Un résultat : `{type, texte, extrait_surligne, piece_id, seconde, url_origine}`.
Le surlignage est du HTML déjà échappé : seules les balises `<mark>` sont posées.
La recherche ignore la casse et les accents (« seisme » trouve « séisme »).
"""

from __future__ import annotations

import html
import re
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any

from enquete.liens import url_origine

# Clés d'attributs qui ne portent pas de texte à chercher (adresses, dates, méthodes).
_ATTRIBUTS_IGNORES = ("url", "date", "methode", "empreinte", "id_plateforme", "marge")

# Type de nœud -> type de résultat. Ce qui n'est pas listé garde son type de nœud.
_TYPE_DE_NOEUD = {
    "compte": "compte",
    "personne": "compte",
    "video": "video",
    "texte_ecran": "texte_ecran",
    "phrase": "phrase",
    "document": "document",
    "publication_texte": "publication",
    "question_ouverte": "question",
}

_DEBUT, _FIN = "\x02", "\x03"
_RE_TIKTOK = re.compile(r"tiktok\.com/@([A-Za-z0-9._]{1,24})/(video|photo)/(\d+)")
_RE_INSTAGRAM = re.compile(r"instagram\.com/(p|reel|reels)/([A-Za-z0-9_-]+)")
_RE_YOUTUBE = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")


@dataclass(frozen=True)
class Document:
    """Un morceau de texte du dossier, avec de quoi rouvrir sa pièce."""

    type: str
    texte: str
    piece_id: str | None
    seconde: int | None = None
    url: str | None = None


def adresse_d_origine(url: str | None, seconde: int | None = None) -> str | None:
    """Passe par `enquete.liens.url_origine` : la seconde n'est portée que par YouTube.

    Une adresse qu'on ne sait pas décomposer est rendue telle quelle.
    """
    if not url:
        return None
    try:
        if m := _RE_TIKTOK.search(url):
            return url_origine("tiktok", m.group(3), m.group(1), m.group(2))
        if m := _RE_INSTAGRAM.search(url):
            genre = "post" if m.group(1) == "p" else "reel"
            return url_origine("instagram", m.group(2), None, genre)
        if m := _RE_YOUTUBE.search(url):
            genre = "short" if "/shorts/" in url else "video"
            return url_origine("youtube", m.group(1), None, genre, seconde)
    except ValueError:
        pass
    return url


def _textes_d_attributs(attributs: dict[str, Any]) -> list[str]:
    morceaux: list[str] = []
    for cle, valeur in attributs.items():
        if any(mot in cle for mot in _ATTRIBUTS_IGNORES):
            continue
        if isinstance(valeur, str):
            morceaux.append(valeur)
        elif isinstance(valeur, list):
            morceaux.extend(v for v in valeur if isinstance(v, str))
    return morceaux


def documents_depuis_export(export: dict[str, Any]) -> list[Document]:
    """Tout ce qui se lit dans le dossier : pièces, preuves, liens, questions."""
    documents: list[Document] = []
    vues: set[tuple] = set()
    # Une preuve se range sur la pièce dont l'adresse est la sienne (la vidéo), pas sur
    # la première pièce qui la cite (une grappe, un compte).
    piece_par_url = {
        p["attributs"]["url"]: p["id"]
        for d in export.get("dossiers", []) for p in d.get("pieces", [])
        if isinstance((p.get("attributs") or {}).get("url"), str)
    }
    for dossier in export.get("dossiers", []):
        for piece in dossier.get("pieces", []):
            genre = _TYPE_DE_NOEUD.get(piece["type"], piece["type"])
            attributs = piece.get("attributs") or {}
            adresse = attributs.get("url") if isinstance(attributs.get("url"), str) else None
            texte = " ".join([piece["libelle"], *_textes_d_attributs(attributs)])
            documents.append(Document(genre, texte, piece["id"], None, adresse_d_origine(adresse)))
            for preuve in piece.get("preuves", []):
                cle = (preuve["url"], preuve["extrait"], preuve.get("debut_s"))
                if cle in vues:
                    continue
                vues.add(cle)
                seconde = preuve.get("debut_s")
                seconde = int(seconde) if seconde is not None else None
                if seconde is not None:
                    genre_preuve = "phrase"  # une phrase dite, avec sa seconde
                elif piece["type"] == "document":
                    genre_preuve = "document"
                else:
                    genre_preuve = "legende"
                documents.append(Document(
                    genre_preuve, preuve["extrait"], piece_par_url.get(preuve["url"], piece["id"]), seconde,
                    adresse_d_origine(preuve["url"], seconde),
                ))
    for lien in export.get("liens", []):
        documents.append(Document("lien", lien["explication"], lien["source_id"], None, None))
    for question in export.get("questions_ouvertes", []):
        documents.append(Document("question", question["texte"], None, None, None))
    return documents


def _requete_fts(q: str) -> str | None:
    """Tous les mots exigés ; le dernier est cherché comme début de mot (saisie au fil de la frappe)."""
    mots = re.findall(r"\w+", q, flags=re.UNICODE)
    if not mots:
        return None
    termes = [f'"{m}"' for m in mots]
    termes[-1] += "*"
    return " ".join(termes)


class Index:
    """Index plein texte en mémoire, sûr à partager entre threads."""

    def __init__(self, documents: list[Document]) -> None:
        self._documents = documents
        self._verrou = threading.Lock()
        self._bd = sqlite3.connect(":memory:", check_same_thread=False)
        self._bd.execute(
            "CREATE VIRTUAL TABLE t USING fts5(texte, tokenize='unicode61 remove_diacritics 2')"
        )
        self._bd.executemany(
            "INSERT INTO t (rowid, texte) VALUES (?, ?)",
            [(i, d.texte) for i, d in enumerate(documents)],
        )

    def __len__(self) -> int:
        return len(self._documents)

    def chercher(self, q: str, limite: int = 50) -> list[dict[str, Any]]:
        requete = _requete_fts(q)
        if requete is None:
            return []
        with self._verrou:
            lignes = self._bd.execute(
                "SELECT rowid, highlight(t, 0, ?, ?) FROM t WHERE t MATCH ? ORDER BY rank LIMIT ?",
                (_DEBUT, _FIN, requete, limite),
            ).fetchall()
        resultats = []
        for rowid, surligne in lignes:
            doc = self._documents[rowid]
            resultats.append({
                "type": doc.type,
                "texte": doc.texte,
                "extrait_surligne": _extrait(surligne),
                "piece_id": doc.piece_id,
                "seconde": doc.seconde,
                "url_origine": doc.url,
            })
        return resultats

    def fermer(self) -> None:
        with self._verrou:
            self._bd.close()


def _extrait(surligne: str, avant: int = 80, apres: int = 160) -> str:
    """Fenêtre autour du premier mot trouvé, échappée en HTML, mots trouvés dans <mark>."""
    i = surligne.find(_DEBUT)
    debut = max(0, i - avant) if i >= 0 else 0
    fin = min(len(surligne), (i if i >= 0 else 0) + apres)
    fenetre = surligne[debut:fin]
    # Une fenêtre qui coupe un mot trouvé en deux garde ses marqueurs équilibrés.
    if fenetre.count(_DEBUT) > fenetre.count(_FIN):
        fenetre += _FIN
    if fenetre.count(_FIN) > fenetre.count(_DEBUT):
        fenetre = _DEBUT + fenetre
    texte = html.escape(fenetre, quote=False).replace(_DEBUT, "<mark>").replace(_FIN, "</mark>")
    return ("… " if debut > 0 else "") + texte + (" …" if fin < len(surligne) else "")


def chercher(export: dict[str, Any], q: str, limite: int = 50) -> list[dict[str, Any]]:
    """Raccourci : indexe l'export puis cherche. Le serveur garde l'index en cache."""
    index = Index(documents_depuis_export(export))
    try:
        return index.chercher(q, limite)
    finally:
        index.fermer()
