"""Contrôle d'une preuve : l'extrait cité se trouve-t-il vraiment dans la source ?

`verifier_preuve(preuve, sources)` répond oui ou non, avec la raison. Elle est
écrite pour être appelée par `ReseauEnquete.ajouter_lien` avant d'accepter une
`Preuve` (le branchement se fait ailleurs). Chaque refus est journalisé.

Ce qui est vérifié :

- la preuve doit désigner une source connue : la publication (plateforme +
  identifiant extraits de l'adresse) ou la page archivée de même adresse ;
- l'extrait doit s'y retrouver mot pour mot, à la casse, aux accents, à la
  ponctuation et aux emojis près ; un extrait en plusieurs morceaux séparés par
  « … » ou « [...] » doit se retrouver dans le même ordre ;
- pour une vidéo, si la preuve donne une seconde (`debut_s`, `fin_s`), l'extrait
  doit se trouver dans la transcription horodatée à 2 secondes près ; un extrait
  qui n'existe que dans la légende ou le texte à l'écran ne porte aucune seconde
  et est refusé s'il en revendique une.

Ce qui n'est pas vérifié : que la source dise vrai, ni que l'extrait soutienne
le lien qu'on lui accroche. Le contrôle prouve l'existence de la citation, pas
sa pertinence.

Sources acceptées (`sources` : liste de dictionnaires, ou dictionnaire adresse -> texte) :

- un résultat Oriane tel que l'API le renvoie (`platform`, `platformId`,
  `caption`, `media[*].transcriptChunks`, `media[*].onScreenText`) ;
- une page archivée : `{"url": ..., "texte": ...}`, `{"url": ..., "html": ...}`
  ou `{"url": ..., "chemin": ...}` (fichier texte ou HTML conservé).
"""

from __future__ import annotations

import bisect
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlparse

journal_logger = logging.getLogger("enquete.controle_preuve")

TOLERANCE_S = 2.0
MIN_MOTS = 3

# Refus de la session, du plus ancien au plus récent (en plus du logger et du fichier).
REFUS: list[dict[str, Any]] = []


@dataclass(frozen=True)
class Verdict:
    accepte: bool
    raison: str
    methode: str | None = None  # parole, texte à l'écran, légende, page archivée
    ecart_s: float | None = None  # écart de seconde constaté pour une parole

    def __bool__(self) -> bool:
        return self.accepte


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normaliser(texte: str) -> str:
    """Minuscules, sans accents ni ponctuation ni emojis, espaces simples."""
    texte = unicodedata.normalize("NFKD", str(texte).replace("œ", "oe").replace("Œ", "oe"))
    sortie = []
    for c in texte:
        if unicodedata.combining(c):
            continue
        sortie.append(c.lower() if c.isalnum() else " ")
    return " ".join("".join(sortie).split())


_RE_COUPURE = re.compile(r"\s*(?:…|\.{3}|\[\s*(?:…|\.{3})\s*\])\s*")


def _morceaux(extrait: str) -> list[str]:
    return [m for m in (normaliser(p) for p in _RE_COUPURE.split(extrait)) if m]


class _Texte(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parties: list[str] = []
        self._ignore = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript"):
            self._ignore += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._ignore:
            self._ignore -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignore:
            self.parties.append(data)


def _html_vers_texte(html: str) -> str:
    analyseur = _Texte()
    analyseur.feed(html)
    return " ".join(analyseur.parties)


# ---------------------------------------------------------------------------
# Désigner la publication : (plateforme, identifiant)
# ---------------------------------------------------------------------------

def cle_publication(url: str) -> tuple[str, str] | None:
    """(plateforme, identifiant) d'une adresse de publication, sinon None."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return None
    hote = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    chemin = [s for s in p.path.split("/") if s]
    if hote.endswith("tiktok.com"):
        for i, seg in enumerate(chemin):
            if seg in ("video", "photo") and i + 1 < len(chemin) and chemin[i + 1].isdigit():
                return ("tiktok", chemin[i + 1])
    elif hote.endswith("instagram.com"):
        for i, seg in enumerate(chemin):
            if seg in ("p", "reel", "reels", "tv") and i + 1 < len(chemin):
                return ("instagram", chemin[i + 1])
    elif hote.endswith("youtube.com"):
        if chemin[:1] == ["watch"]:
            v = parse_qs(p.query).get("v")
            if v:
                return ("youtube", v[0])
        if chemin[:1] in (["shorts"], ["live"]) and len(chemin) > 1:
            return ("youtube", chemin[1])
    elif hote == "youtu.be" and chemin:
        return ("youtube", chemin[0])
    return None


def _url_canonique(url: str) -> str:
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip().lower()
    hote = (p.hostname or "").lower().removeprefix("www.")
    return f"{hote}{p.path.rstrip('/')}".lower()


# ---------------------------------------------------------------------------
# Les textes d'une source
# ---------------------------------------------------------------------------

@dataclass
class _Parole:
    """Transcription horodatée : texte normalisé et position de chaque morceau."""

    texte: str
    debuts_car: list[int]
    morceaux: list[tuple[float, float]]

    def temps(self, debut: int, fin: int) -> tuple[float, float]:
        """Seconde de début et de fin des morceaux qui couvrent [debut, fin)."""
        i = max(bisect.bisect_right(self.debuts_car, debut) - 1, 0)
        j = max(bisect.bisect_right(self.debuts_car, max(fin - 1, debut)) - 1, 0)
        return self.morceaux[i][0], self.morceaux[j][1]


def _parole(resultat: Mapping[str, Any]) -> list[_Parole]:
    sorties = []
    for media in resultat.get("media") or []:
        morceaux = media.get("transcriptChunks") or []
        textes, bornes, debuts, position = [], [], [], 0
        for m in morceaux:
            t = normaliser(m.get("text") or "")
            if not t:
                continue
            debuts.append(position)
            textes.append(t)
            bornes.append((float(m.get("startSeconds") or 0), float(m.get("endSeconds") or 0)))
            position += len(t) + 1
        if textes:
            sorties.append(_Parole(" ".join(textes), debuts, bornes))
    return sorties


def _textes_fixes(resultat: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(méthode, texte normalisé) pour la légende, le titre et le texte à l'écran."""
    sorties = []
    for cle in ("caption", "title"):
        if resultat.get(cle):
            sorties.append(("légende", normaliser(resultat[cle])))
    for media in resultat.get("media") or []:
        if media.get("caption"):
            sorties.append(("légende", normaliser(media["caption"])))
        ecran = media.get("onScreenText")
        if ecran:
            brut = ecran if isinstance(ecran, str) else " ".join(map(str, ecran))
            sorties.append(("texte à l'écran", normaliser(brut)))
            # Chaque fragment seul aussi : l'extrait peut avoir été relevé sur une image.
            for fragment in ([] if isinstance(ecran, str) else ecran):
                sorties.append(("texte à l'écran", normaliser(fragment)))
        # Parole entière sans horodatage : sert quand les morceaux manquent.
        if media.get("transcript") and not media.get("transcriptChunks"):
            sorties.append(("parole sans seconde", normaliser(media["transcript"])))
    return sorties


def _occurrences(morceaux: list[str], texte: str) -> Iterable[tuple[int, int]]:
    """Position (début, fin) de chaque suite ordonnée de morceaux dans le texte."""
    haystack = f" {texte} "
    premier = f" {morceaux[0]} "
    depart = 0
    while True:
        i = haystack.find(premier, depart)
        if i < 0:
            return
        fin, ok = i + len(premier), True  # fin exclusive, espace de garde compris
        for suivant in morceaux[1:]:
            motif = f" {suivant} "
            j = haystack.find(motif, fin - 1)
            if j < 0:
                ok = False
                break
            fin = j + len(motif)
        if ok:
            # Coordonnées du texte sans espace de garde : [i, fin - 2).
            yield i, fin - 2
        depart = i + 1


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def _journaliser(preuve: Mapping[str, Any], raison: str, journal: list | str | Path | None) -> None:
    ligne = {
        "date": datetime.now(timezone.utc).isoformat(),
        "url": preuve.get("url"),
        "extrait": (preuve.get("extrait") or "")[:200],
        "debut_s": preuve.get("debut_s"),
        "fin_s": preuve.get("fin_s"),
        "raison": raison,
    }
    REFUS.append(ligne)
    journal_logger.warning("preuve refusée : %s (%s)", raison, ligne["url"])
    if isinstance(journal, list):
        journal.append(ligne)
    elif journal is not None:
        with open(journal, "a", encoding="utf-8") as f:
            f.write(json.dumps(ligne, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Le contrôle
# ---------------------------------------------------------------------------

def _en_dict(preuve: Any) -> dict[str, Any]:
    if isinstance(preuve, Mapping):
        return dict(preuve)
    return {
        "url": getattr(preuve, "url", None),
        "extrait": getattr(preuve, "extrait", None),
        "debut_s": getattr(preuve, "debut_s", None),
        "fin_s": getattr(preuve, "fin_s", None),
    }


def _sources_en_liste(sources: Any) -> list[Mapping[str, Any]]:
    if isinstance(sources, Mapping):
        return [{"url": u, "texte": t} for u, t in sources.items()]
    return list(sources or [])


def _texte_page(source: Mapping[str, Any]) -> str | None:
    if source.get("texte") is not None:
        return str(source["texte"])
    if source.get("html") is not None:
        return _html_vers_texte(str(source["html"]))
    if source.get("chemin"):
        chemin = Path(source["chemin"])
        if chemin.exists():
            brut = chemin.read_text(encoding="utf-8", errors="replace")
            return _html_vers_texte(brut) if "<" in brut and ">" in brut else brut
    return None


def verifier_preuve(
    preuve: Any,
    sources: Any,
    *,
    journal: list | str | Path | None = None,
    tolerance_s: float = TOLERANCE_S,
    min_mots: int = MIN_MOTS,
) -> Verdict:
    """Accepte la preuve seulement si son extrait se retrouve dans une source.

    `preuve` : une `Preuve` ou un dictionnaire avec `url`, `extrait`, `debut_s`,
    `fin_s`. `journal` : liste ou chemin d'un fichier JSON lines qui reçoit
    chaque refus (en plus de `REFUS` et du logger `enquete.controle_preuve`).
    """
    p = _en_dict(preuve)
    verdict = _decider(p, _sources_en_liste(sources), tolerance_s, min_mots)
    if not verdict.accepte:
        _journaliser(p, verdict.raison, journal)
    return verdict


def _decider(p: dict[str, Any], sources: list[Mapping[str, Any]], tolerance_s: float, min_mots: int) -> Verdict:
    url, extrait = p.get("url"), p.get("extrait")
    if not url or not isinstance(url, str):
        return Verdict(False, "adresse absente")
    if not extrait or not isinstance(extrait, str):
        return Verdict(False, "extrait absent")
    morceaux = _morceaux(extrait)
    if not morceaux:
        return Verdict(False, "extrait sans texte (ponctuation ou emojis seuls)")
    if sum(len(m.split()) for m in morceaux) < min_mots:
        return Verdict(False, f"extrait trop court : moins de {min_mots} mots, il ne prouve rien")

    debut, fin = p.get("debut_s"), p.get("fin_s")
    cle = cle_publication(url)
    canonique = _url_canonique(url)

    candidates_pub, candidates_page = [], []
    for s in sources:
        if "platformId" in s and s.get("platform"):
            if cle and (str(s["platform"]).lower(), str(s["platformId"])) == cle:
                candidates_pub.append(s)
        elif s.get("url") and (
            _url_canonique(str(s["url"])) == canonique
            or (cle and cle_publication(str(s["url"])) == cle)
        ):
            candidates_page.append(s)
    if not candidates_pub and not candidates_page:
        return Verdict(False, "aucune source connue à cette adresse : rien à comparer")

    seul_hors_temps = False
    for s in candidates_pub:
        for parole in _parole(s):
            for a, b in _occurrences(morceaux, parole.texte):
                t0, t1 = parole.temps(a, b)
                if debut is None:
                    return Verdict(True, "retrouvé dans la parole", "parole")
                haut = fin if fin is not None else debut
                ecart = max(t0 - haut, debut - t1, 0.0)
                if ecart <= tolerance_s:
                    return Verdict(True, "retrouvé dans la parole à la bonne seconde", "parole", ecart)
        for methode, texte in _textes_fixes(s):
            if next(iter(_occurrences(morceaux, texte)), None) is not None:
                if debut is not None:
                    seul_hors_temps = True
                    continue
                return Verdict(True, f"retrouvé dans {methode}", methode)
    for s in candidates_page:
        texte = _texte_page(s)
        if texte is None:
            continue
        if next(iter(_occurrences(morceaux, normaliser(texte))), None) is not None:
            if debut is not None:
                return Verdict(False, "une page n'a pas de seconde : la preuve en revendique une")
            return Verdict(True, "retrouvé dans la page archivée", "page archivée")
    if seul_hors_temps:
        return Verdict(False, "extrait retrouvé hors de la parole (légende ou écran) : la seconde revendiquée est invérifiable")
    if debut is not None and any(_parole(s) for s in candidates_pub):
        return Verdict(False, f"extrait absent de la parole à {debut} s (tolérance {tolerance_s} s)")
    return Verdict(False, "extrait introuvable dans la source")


# ---------------------------------------------------------------------------
# Sources tirées du cache Oriane (pour les tests et les rejeux)
# ---------------------------------------------------------------------------

def sources_depuis_cache(dossier_cache: str | Path) -> list[dict[str, Any]]:
    """Tous les résultats Oriane d'un dossier de cache, sans doublon d'identifiant
    quand deux versions portent la même parole."""
    vus: dict[str, dict[str, Any]] = {}
    for fichier in sorted(Path(dossier_cache).glob("*.json")):
        try:
            contenu = json.loads(fichier.read_text(encoding="utf-8"))
            resultats = contenu["reponse"]["data"]["results"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for r in resultats:
            rid = r.get("id")
            if rid is None:
                continue
            ancien = vus.get(rid)
            if ancien is None or (_parole(r) and not _parole(ancien)):
                vus[rid] = r
    return list(vus.values())
