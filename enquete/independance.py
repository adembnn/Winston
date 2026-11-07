"""Indépendance des sources : « N pièces · M comptes · K origines sans lien visible ».

Image simple : dix journaux qui impriment la même dépêche ne font pas dix
témoins, ils font un témoin entendu dix fois. Ce module remonte « qui reprend
qui » entre les pièces d'un dossier, fusionne celles qui remontent à la même
origine, puis compte les familles qui restent.

Module pur : aucun réseau, aucun modèle. Il lit l'export de
`ReseauEnquete.exporter_pour_interface()` (ou `/api/export`), en option les
liens candidats de `pivots.relier_par_pivots` (`LienCandidat.vers_dict()`) et des
pages déjà lues (`{id, url, texte, html?, publication?}`).

Règle de prudence : seules les dépendances ÉTABLIES fusionnent deux pièces
(K). Les dépendances PROBABLES ne servent qu'au compte prudent (k_prudent, toujours
inférieur ou égal à K). Le module compte ce qu'il voit : une origine commune
hors de portée (message privé, abonnement, plateforme non indexée) reste
invisible, d'où la liste `limites` rendue à chaque fois.
"""

from __future__ import annotations

import html as _html
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

from enquete.chaine import normaliser_phrase

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

MOTS_ETABLIE = 8        # phrase commune d'au moins 8 mots : reprise établie
MOTS_PROBABLE = 5       # de 5 à 7 mots : reprise probable
MOTS_COPIE_MIN = 40     # un texte plus court n'entre pas dans la comparaison par copie
TAILLE_SUITE = 5        # copie de texte : suites de 5 mots
POIDS_PROBABLE = 1.5    # pivot non banal dont le poids de rareté atteint ce seuil : probable
GROUPE_MAX_PAIRES = 12  # au-delà, chaque pièce n'est comparée qu'à la plus ancienne du groupe
DEPENDANCES_MAX = 25    # par pièce et par règle

ETABLIE = "etablie"
PROBABLE = "probable"

STATUT_PLUSIEURS = "plusieurs origines sans lien visible"
STATUT_UNIQUE = "origine unique : ne pas présenter comme recoupé"
STATUT_CONTESTEE = "contestée"
STATUT_VIDE = "aucune pièce"

QUESTION_RECOUPEMENT = "Qui d'autre est au courant, hors de cette origine ?"

LIMITES = (
    "Les abonnements payants, les messageries privées et les plateformes hors index sont invisibles.",
    "Deux origines sans lien visible peuvent partager une source hors de portée.",
)

_NATURES_RESUME = ("video", "page", "document")
_TYPES_PAGE = {"publication_texte": "page", "document": "document", "video": "video", "compte": "compte"}

# Plateformes et services : citer l'un d'eux ne dit rien sur l'origine d'une information.
_DOMAINES_BANALS = {
    "tiktok.com", "instagram.com", "youtube.com", "youtu.be", "facebook.com", "fb.com", "x.com", "twitter.com",
    "t.co", "linkedin.com", "google.com", "google.fr", "apple.com", "bing.com", "wikipedia.org", "whatsapp.com",
    "wa.me", "t.me", "telegram.org", "snapchat.com", "pinterest.com", "reddit.com", "bit.ly", "goo.gl",
    "amazon.com", "amazon.fr", "paypal.com", "gstatic.com", "googleapis.com", "cloudflare.com", "w3.org",
    "schema.org", "bsky.app", "threads.net", "vimeo.com", "dailymotion.com", "spotify.com", "apple.news",
}

# Agences de presse : nom affiché, motif.
_AGENCES: dict[str, str] = {
    "AFP": r"AFP|Agence France[- ]Presse",
    "Reuters": r"Reuters",
    "AP": r"AP|Associated Press",
    "dpa": r"dpa",
    "ANSA": r"ANSA",
    "EFE": r"EFE",
    "Belga": r"Belga",
    "PA Media": r"PA Media|Press Association",
    "Bloomberg": r"Bloomberg",
    # Médias cités comme source d'une information : même traitement qu'une agence.
    "Financial Times": r"Financial Times|FT",
    "CNBC": r"CNBC",
    "The Verge": r"The Verge",
    "Wall Street Journal": r"Wall Street Journal|WSJ",
    "New York Times": r"New York Times|NYT",
    "Washington Post": r"Washington Post",
    "Le Monde": r"Le Monde",
    "BBC": r"BBC",
    "The Guardian": r"The Guardian",
    "CNN": r"CNN",
}
# Domaines de l'agence elle-même : une page publiée par l'agence est son origine.
_DOMAINES_AGENCES = {
    "afp.com": "AFP", "reuters.com": "Reuters", "apnews.com": "AP", "dpa.com": "dpa", "ansa.it": "ANSA",
    "efe.com": "EFE", "belga.be": "Belga", "pa.media": "PA Media", "bloomberg.com": "Bloomberg",
}
_ALTERNATIVE_AGENCES = "|".join(f"(?P<a{i}>{motif})" for i, motif in enumerate(_AGENCES.values()))
_NOMS_AGENCES = list(_AGENCES)

_RE_CREDIT_PARENTHESE = re.compile(
    rf"\((?:\s*(?:avec|source\s*:|selon)\s+)?(?:l['’]\s?)?(?:{_ALTERNATIVE_AGENCES})\b[^)]{{0,40}}\)"
)
_RE_CREDIT_MOT = re.compile(
    r"(?i:\b(?:avec|selon|d['’]après|sources?\s*:|via|according to|(?:first )?reported by|"
    r"(?:a |the )?reports? (?:from|by)|seen by|consulted by|consultée? par|vue? par|citing|citant|with))\s+"
    rf"(?:l['’]\s?|le |la |les |the )?(?:(?i:agence)\s+)?(?:{_ALTERNATIVE_AGENCES})\b"
)
_RE_CREDIT_SUJET = re.compile(
    rf"\b(?:{_ALTERNATIVE_AGENCES})\s+(?:has |have )?(?:first )?(?:reported|reports|revealed|wrote|writes|said|says|rapporte|révèle|a révélé|a rapporté)\b"
)
_RE_SUITE_CREDIT = re.compile(
    rf"\s*(?:,|et|and|&|ou|or)\s+(?:l['’]\s?|le |la |les |the )?(?:{_ALTERNATIVE_AGENCES})\b"
)
_RE_AVEC_AGENCES = re.compile(r"(?i)\bavec\s+(?:les\s+)?agences\b")
_RE_SOURCE_NOMMEE = re.compile(r"(?i)\bsource\s*:\s*([A-ZÉÈÀ][\w'’.&\- ]{1,38}?)(?=\s*(?:[.;,\n()|/]|$))")
_NON_SOURCES = {
    "tiktok", "instagram", "youtube", "facebook", "x", "twitter", "capture d'ecran", "photo", "image", "dr",
    "getty images", "getty", "shutterstock", "wikipedia", "google", "internet", "web", "reseaux sociaux",
    "inconnue", "anonyme", "capture", "video", "archives", "archive",
}
_RE_CREDIT_IMAGE = re.compile(r"(?i)(photo|crédit|credit|©|image|illustration|cliché)[^()]{0,25}$")

_RE_URL = re.compile(r"""https?://[^\s<>"'\])}]+""")
_RE_HREF = re.compile(r"""(?is)<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["']""")
_RE_BLOC_BRUIT = re.compile(r"(?is)<(nav|header|footer|aside|script|style|form|noscript|svg)\b.*?</\1\s*>")
_RE_BLOC_CORPS = re.compile(r"(?is)<(article|main)\b.*?</\1\s*>")
_RE_BALISE = re.compile(r"(?s)<[^>]+>")
_RE_REPETE_BRUIT = re.compile(
    r"(?i)cookies?|abonnez-vous|abonne-toi|all rights reserved|tous droits réservés|newsletter|"
    r"politique de confidentialité|mentions légales|privacy policy|terms of use|se connecter|"
    r"suivez-nous|follow us|partager sur|share on|lire aussi|read more|publicité|advertisement"
)
_PARAMS_SUIVI = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid", "si", "feature", "t", "s",
                 "lang", "is_from_webapp", "sender_device", "web_id", "_r", "_t"}
_RE_AUTEUR_LIBELLE = re.compile(r"^@(?P<pseudo>.+?) \((?P<plateforme>[^()]+)\)$")
_RE_AUTEUR_VIDEO = re.compile(r"^Vidéo (?P<plateforme>\S+) de @(?P<pseudo>[^,\s]+)")
_TYPES_PIVOT_SANS_OPERATEUR = {"hashtag", "pseudo_cite", "son"}
_SEUILS_SECONDES_MAX = datetime.max.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PieceSource:
    """Une pièce vue sous l'angle de l'origine : qui la publie, quand, ce qu'elle cite."""

    id: str
    nature: str  # video | page | compte | document
    auteur: str | None  # « plateforme:pseudo » ; domaine enregistrable pour une page
    date: datetime | None  # UTC
    url: str | None
    texte: str
    liens_sortants: tuple[str, ...] = ()
    credits: tuple[str, ...] = ()

    def vers_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "nature": self.nature, "auteur": self.auteur,
            "date": self.date.isoformat() if self.date else None, "url": self.url,
            "liens_sortants": list(self.liens_sortants), "credits": list(self.credits),
        }


@dataclass(frozen=True)
class Dependance:
    """`de` (la plus récente) dépend de `vers` (la plus ancienne), ou d'un nœud virtuel `agence:<nom>`."""

    de: str
    vers: str
    regle: str
    niveau: str  # « etablie » | « probable »
    preuve: str

    def vers_dict(self) -> dict[str, str]:
        return {"de": self.de, "vers": self.vers, "regle": self.regle, "niveau": self.niveau, "preuve": self.preuve}


# ---------------------------------------------------------------------------
# Outils : dates, adresses, auteurs
# ---------------------------------------------------------------------------

def _lire_date(valeur: Any) -> datetime | None:
    if isinstance(valeur, datetime):
        return valeur if valeur.tzinfo else valeur.replace(tzinfo=UTC)
    if not isinstance(valeur, str) or not valeur.strip():
        return None
    brut = valeur.strip().replace("Z", "+00:00")
    try:
        lue = datetime.fromisoformat(brut)
    except ValueError:
        try:
            lue = datetime.strptime(brut[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return (lue if lue.tzinfo else lue.replace(tzinfo=UTC)).astimezone(UTC)


def _domaine_enregistrable(url_ou_hote: str) -> str | None:
    """Domaine enregistrable approximatif (sans liste de suffixes publics) : « www.bbc.co.uk » donne « bbc.co.uk »."""
    brut = url_ou_hote if "//" in url_ou_hote else "//" + url_ou_hote
    hote = (urlsplit(brut).hostname or "").lower().strip(".")
    if not hote or "." not in hote:
        return None
    etiquettes = hote.split(".")
    if len(etiquettes) >= 3 and len(etiquettes[-1]) == 2 and etiquettes[-2] in {"co", "com", "org", "gov", "ac", "net", "gouv"}:
        return ".".join(etiquettes[-3:])
    return ".".join(etiquettes[-2:])


def _cle_url(url: str) -> str | None:
    """Clé de comparaison d'une adresse : sans schéma, sans « www », sans suivi, sans fragment."""
    try:
        morceaux = urlsplit(url.strip())
    except ValueError:
        return None
    hote = (morceaux.hostname or "").lower()
    if hote.startswith("www."):
        hote = hote[4:]
    if not hote:
        return None
    chemin = morceaux.path.rstrip("/")
    requete = [(k, v) for k, v in parse_qsl(morceaux.query) if k.lower() not in _PARAMS_SUIVI and not k.lower().startswith("utm_")]
    if hote == "youtu.be" and chemin:
        return f"youtube.com/watch?v={chemin.lstrip('/')}"
    if hote in {"m.youtube.com", "youtube.com", "music.youtube.com"}:
        valeurs = dict(requete)
        return f"youtube.com{chemin}" + (f"?v={valeurs['v']}" if "v" in valeurs else "")
    if hote in {"m.facebook.com", "mobile.twitter.com", "m.tiktok.com"}:
        hote = hote.split(".", 1)[1]
    if hote == "twitter.com":
        hote = "x.com"
    suite = "&".join(f"{k}={v}" for k, v in sorted(requete))
    return hote + chemin + (f"?{suite}" if suite else "")


def _urls_dans(texte: str) -> list[str]:
    return [u.rstrip(".,;:!?»»") for u in _RE_URL.findall(texte or "")]


def _liens_html(html: str) -> list[str]:
    """Liens du corps de l'article : on écarte menus, bas de page, scripts et formulaires."""
    corps = _RE_BLOC_CORPS.search(html)
    zone = corps.group(0) if corps else html
    zone = _RE_BLOC_BRUIT.sub(" ", zone)
    return [h.strip() for h in _RE_HREF.findall(zone) if h.strip().lower().startswith(("http://", "https://"))]


def _texte_depuis_html(html: str) -> str:
    corps = _RE_BLOC_CORPS.search(html)
    zone = _RE_BLOC_BRUIT.sub(" ", corps.group(0) if corps else html)
    zone = re.sub(r"(?i)</(p|div|li|h[1-6]|br|tr)\s*>|<br\s*/?>", "\n", zone)
    return re.sub(r"[ \t]+", " ", _html.unescape(_RE_BALISE.sub(" ", zone))).strip()


def _unique(valeurs: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v for v in valeurs if v))


def _slug(nom: str) -> str:
    return normaliser_phrase(nom).replace(" ", "-") or "inconnue"


def _noeud_agence(nom: str) -> str:
    return f"agence:{_slug(nom)}"


# ---------------------------------------------------------------------------
# Crédits d'agence
# ---------------------------------------------------------------------------

def repérer_credits(texte: str) -> list[tuple[str, str]]:
    """[(nom de l'agence ou de la source, extrait)] : « (AFP) », « avec AFP », « selon l'AFP », « source : … »."""
    trouves: list[tuple[str, str]] = []

    def nom_de(correspondance: re.Match[str]) -> str | None:
        for i, nom in enumerate(_NOMS_AGENCES):
            if correspondance.groupdict().get(f"a{i}"):
                return nom
        return None

    for motif in (_RE_CREDIT_PARENTHESE, _RE_CREDIT_MOT, _RE_CREDIT_SUJET):
        for m in motif.finditer(texte or ""):
            avant = texte[max(0, m.start() - 30): m.start()]
            if _RE_CREDIT_IMAGE.search(avant):
                continue  # « Photo AFP » : crédit d'image, pas une source du texte
            nom = nom_de(m)
            if nom:
                trouves.append((nom, m.group(0).strip()))
            if motif is _RE_CREDIT_MOT:  # « selon CNBC et le Financial Times »
                reste = texte[m.end(): m.end() + 80]
                while (suite := _RE_SUITE_CREDIT.match(reste)):
                    autre = nom_de(suite)
                    if autre:
                        trouves.append((autre, (m.group(0) + suite.group(0)).strip()))
                    reste = reste[suite.end():]
    for m in _RE_AVEC_AGENCES.finditer(texte or ""):
        trouves.append(("agences", m.group(0)))
    for m in _RE_SOURCE_NOMMEE.finditer(texte or ""):
        nom = m.group(1).strip()
        if _RE_CREDIT_IMAGE.search(texte[max(0, m.start() - 30): m.start()]):
            continue
        if normaliser_phrase(nom) in _NON_SOURCES or nom.lower().startswith("http") or len(nom.split()) > 4:
            continue
        if any(re.fullmatch(motif, nom) for motif in _AGENCES.values()):
            continue  # déjà vue sous son nom d'agence
        trouves.append((nom, m.group(0).strip()))
    vus: set[str] = set()
    uniques = []
    for nom, extrait in trouves:
        if _slug(nom) not in vus:
            vus.add(_slug(nom))
            uniques.append((nom, extrait))
    return uniques


# ---------------------------------------------------------------------------
# Lecture de l'export
# ---------------------------------------------------------------------------

def _auteur_video(noeud: dict[str, Any], liens: list[dict[str, Any]], noeuds: dict[str, dict[str, Any]]) -> str | None:
    for lien in liens:
        if lien.get("type_lien") == "publie" and lien.get("cible_id") == noeud["id"]:
            compte = noeuds.get(lien.get("source_id") or "")
            if compte:
                m = _RE_AUTEUR_LIBELLE.match(compte.get("libelle") or "")
                plateforme = (compte.get("attributs") or {}).get("plateforme") or (m.group("plateforme") if m else None)
                if m and plateforme:
                    return f"{plateforme}:{m.group('pseudo')}".lower()
    m = _RE_AUTEUR_VIDEO.match(noeud.get("libelle") or "")
    return f"{m.group('plateforme')}:{m.group('pseudo')}".lower() if m else None


def _auteur_compte(noeud: dict[str, Any]) -> str | None:
    m = _RE_AUTEUR_LIBELLE.match(noeud.get("libelle") or "")
    plateforme = (noeud.get("attributs") or {}).get("plateforme") or (m.group("plateforme") if m else None)
    return f"{plateforme}:{m.group('pseudo')}".lower() if m and plateforme else None


def _liens_son_seul(export: dict[str, Any]) -> set[str]:
    """Identifiants des liens « même chose » qui ne portent qu'un son (aucune phrase)."""
    return {
        lien["id"] for lien in export.get("liens", [])
        if lien.get("type_lien") == "dit_la_meme_chose" and float(lien.get("force_points") or 0) < 2.0
    }


def depuis_export(export: dict[str, Any], *, pages: Iterable[dict[str, Any]] | None = None) -> list[PieceSource]:
    """Les pièces d'un export (vidéos, comptes, documents, publications texte) et les pages données en plus."""
    liens = list(export.get("liens", []))
    noeuds = {p["id"]: p for d in export.get("dossiers", []) for p in d.get("pieces", [])}
    son_seul = _liens_son_seul(export)
    pieces: list[PieceSource] = []
    for id_, noeud in noeuds.items():
        nature = _TYPES_PAGE.get(noeud.get("type", ""))
        if nature is None:
            continue
        attributs = noeud.get("attributs") or {}
        url = attributs.get("url") or None
        if nature == "video":
            auteur = _auteur_video(noeud, liens, noeuds)
        elif nature == "compte":
            auteur = _auteur_compte(noeud)
        else:
            auteur = _domaine_enregistrable(url) if url else None
        cle_propre = _cle_url(url) if url else None
        extraits: list[str] = []
        for preuve in noeud.get("preuves", []):
            if preuve.get("liens") and all(i in son_seul for i in preuve["liens"]):
                continue  # titre de son : voir meme_son, ce n'est pas du texte repris
            extrait = (preuve.get("extrait") or "").strip()
            if not extrait or extrait.startswith("(vidéo sans légende)"):
                continue
            if cle_propre and preuve.get("url") and _cle_url(preuve["url"]) != cle_propre:
                continue  # extrait de l'autre pièce, rangé sur le lien
            extraits.append(extrait)
        extraits = list(dict.fromkeys(extraits))
        texte = "\n".join(extraits)
        date = _lire_date(attributs.get("date") or attributs.get("publication") or attributs.get("date_publication"))
        sortants = _unique(_urls_dans(texte))
        pieces.append(PieceSource(
            id=id_, nature=nature, auteur=auteur, date=date, url=url, texte=texte,
            liens_sortants=sortants, credits=_unique(nom for nom, _ in repérer_credits(texte)),
        ))
    pieces.extend(_pieces_depuis_pages(pages or ()))
    return pieces


def _pieces_depuis_pages(pages: Iterable[dict[str, Any]]) -> list[PieceSource]:
    sortie = []
    for page in pages:
        html = page.get("html") or ""
        texte = (page.get("texte") or "").strip() or (_texte_depuis_html(html) if html else "")
        url = page.get("url")
        sortants = _liens_html(html) + _urls_dans(texte)
        sortie.append(PieceSource(
            id=str(page.get("id") or url), nature="page", auteur=_domaine_enregistrable(url) if url else None,
            date=_lire_date(page.get("publication")), url=url, texte=texte,
            liens_sortants=_unique(sortants), credits=_unique(nom for nom, _ in repérer_credits(texte)),
        ))
    return sortie


# ---------------------------------------------------------------------------
# Comparaison de textes
# ---------------------------------------------------------------------------

def _segments(piece: PieceSource) -> list[list[str]]:
    """Le texte en lignes de mots normalisés ; menus et mentions légales des pages écartés."""
    lignes = []
    for ligne in piece.texte.split("\n"):
        if piece.nature != "video" and _RE_REPETE_BRUIT.search(ligne) and len(ligne) < 300:
            continue
        mots = normaliser_phrase(ligne).split()
        if mots:
            lignes.append(mots)
    return lignes


def _suites(segments: list[list[str]]) -> set[tuple[str, ...]]:
    return {
        tuple(seg[i:i + TAILLE_SUITE])
        for seg in segments for i in range(len(seg) - TAILLE_SUITE + 1)
    }


def _plus_longue_suite(a: list[list[str]], b: list[list[str]]) -> list[str]:
    """Plus longue suite de mots commune à deux textes, sans traverser une limite de ligne."""
    plat_a = [m for k, seg in enumerate(a) for m in (*seg, f"\x00a{k}")]
    plat_b = [m for k, seg in enumerate(b) for m in (*seg, f"\x00b{k}")]
    m = SequenceMatcher(None, plat_a, plat_b, autojunk=False).find_longest_match(0, len(plat_a), 0, len(plat_b))
    return plat_a[m.a: m.a + m.size]


def _niveau_suite(n_mots: int) -> str | None:
    if n_mots >= MOTS_ETABLIE:
        return ETABLIE
    if n_mots >= MOTS_PROBABLE:
        return PROBABLE
    return None


def _citer(mots: list[str], longueur: int = 100) -> str:
    texte = " ".join(mots)
    return texte if len(texte) <= longueur else texte[: longueur - 1].rstrip() + "…"


def _cle_ordre(piece: PieceSource) -> tuple[datetime, str]:
    return (piece.date or _SEUILS_SECONDES_MAX, piece.id)


def _ordonner(a: PieceSource, b: PieceSource) -> tuple[PieceSource, PieceSource]:
    """(la plus récente, la plus ancienne)."""
    return (b, a) if _cle_ordre(a) < _cle_ordre(b) else (a, b)


# ---------------------------------------------------------------------------
# Règles de dépendance
# ---------------------------------------------------------------------------

def _regle_meme_auteur(pieces: list[PieceSource]) -> list[Dependance]:
    par_auteur: dict[str, list[PieceSource]] = defaultdict(list)
    for p in pieces:
        if p.auteur:
            par_auteur[p.auteur].append(p)
    sortie = []
    for auteur, groupe in par_auteur.items():
        groupe.sort(key=_cle_ordre)
        plateforme = auteur.split(":", 1)[0] if ":" in auteur else auteur
        etiquette = f"même compte ({plateforme})" if ":" in auteur else f"même éditeur ({auteur})"
        for p in groupe[1:]:
            sortie.append(Dependance(p.id, groupe[0].id, "meme_auteur", ETABLIE, etiquette))
    return sortie


def _regle_textes(pieces: list[PieceSource], seuil_copie: float, seuil_probable: float) -> list[Dependance]:
    """reprise_phrase et copie_texte, sur les paires qui partagent au moins une suite de 5 mots."""
    segments = {p.id: _segments(p) for p in pieces}
    suites = {p.id: _suites(segments[p.id]) for p in pieces}
    index: dict[tuple[str, ...], list[PieceSource]] = defaultdict(list)
    for p in pieces:
        for s in suites[p.id]:
            index[s].append(p)
    candidates: set[tuple[str, str]] = set()
    for membres in index.values():
        if len(membres) < 2:
            continue
        membres = sorted(membres, key=_cle_ordre)
        if len(membres) <= GROUPE_MAX_PAIRES:
            paires: Iterable[tuple[PieceSource, PieceSource]] = combinations(membres, 2)
        else:
            paires = ((membres[0], m) for m in membres[1:])
        candidates.update((a.id, b.id) for a, b in paires)  # a est la plus ancienne

    par_id = {p.id: p for p in pieces}
    sortie: list[Dependance] = []
    for id_ancienne, id_recente in sorted(candidates):
        ancienne, recente = par_id[id_ancienne], par_id[id_recente]
        suite = _plus_longue_suite(segments[recente.id], segments[ancienne.id])
        niveau = _niveau_suite(len(suite))
        if niveau:
            sortie.append(Dependance(
                recente.id, ancienne.id, "reprise_phrase", niveau,
                f"phrase commune de {len(suite)} mots : « {_citer(suite)} »",
            ))
        mots_a = sum(len(s) for s in segments[recente.id])
        mots_b = sum(len(s) for s in segments[ancienne.id])
        if mots_a >= MOTS_COPIE_MIN and mots_b >= MOTS_COPIE_MIN:
            union = suites[recente.id] | suites[ancienne.id]
            jaccard = len(suites[recente.id] & suites[ancienne.id]) / len(union) if union else 0.0
            niveau_copie = ETABLIE if jaccard >= seuil_copie else PROBABLE if jaccard >= seuil_probable else None
            if niveau_copie:
                sortie.append(Dependance(
                    recente.id, ancienne.id, "copie_texte", niveau_copie,
                    f"{round(jaccard * 100)} % de suites de {TAILLE_SUITE} mots en commun",
                ))
    return sortie


def _regle_citation(pieces: list[PieceSource]) -> list[Dependance]:
    par_cle: dict[str, list[PieceSource]] = defaultdict(list)
    par_domaine: dict[str, list[PieceSource]] = defaultdict(list)
    for p in pieces:
        cle = _cle_url(p.url) if p.url else None
        if cle:
            par_cle[cle].append(p)
        if p.nature in ("page", "document") and p.auteur:
            par_domaine[p.auteur].append(p)
    sortie = []
    for p in pieces:
        exactes: set[str] = set()
        for lien in p.liens_sortants:
            cle = _cle_url(lien)
            for cible in par_cle.get(cle or "", ()):
                if cible.id != p.id and cible.id not in exactes:
                    exactes.add(cible.id)
                    sortie.append(Dependance(p.id, cible.id, "citation", ETABLIE, f"cite l'adresse {lien}"))
        domaines_vus: set[str] = set()
        for lien in p.liens_sortants:
            domaine = _domaine_enregistrable(lien)
            if not domaine or domaine in domaines_vus or domaine in _DOMAINES_BANALS or domaine == p.auteur:
                continue
            domaines_vus.add(domaine)
            cibles = [c for c in par_domaine.get(domaine, ()) if c.id != p.id and c.id not in exactes]
            if cibles:
                cible = min(cibles, key=_cle_ordre)
                sortie.append(Dependance(p.id, cible.id, "citation", PROBABLE, f"cite le site {domaine}"))
    return sortie


def _regle_credit_agence(pieces: list[PieceSource]) -> list[Dependance]:
    sortie = []
    for p in pieces:
        extraits = dict(repérer_credits(p.texte))
        for nom in p.credits:
            sortie.append(Dependance(
                p.id, _noeud_agence(nom), "credit_agence", ETABLIE,
                f"crédit « {extraits.get(nom, nom)[:60]} »",
            ))
        agence = _DOMAINES_AGENCES.get(p.auteur or "")
        if agence and agence not in p.credits:
            sortie.append(Dependance(p.id, _noeud_agence(agence), "credit_agence", ETABLIE, "publié par l'agence elle-même"))
    return sortie


def _pieces_par_compte(pieces: list[PieceSource]) -> dict[str, list[PieceSource]]:
    par_compte: dict[str, list[PieceSource]] = defaultdict(list)
    for p in pieces:
        if p.auteur:
            par_compte[p.auteur.lower()].append(p)
    for groupe in par_compte.values():
        groupe.sort(key=_cle_ordre)
    return par_compte


def _regle_operateur_et_son(
    pieces: list[PieceSource], export: dict[str, Any] | None, liens_pivots: list[dict[str, Any]],
) -> list[Dependance]:
    par_compte = _pieces_par_compte(pieces)
    par_cle_url = {_cle_url(p.url): p for p in pieces if p.url and _cle_url(p.url)}
    sortie: list[Dependance] = []

    for lien in liens_pivots:
        comptes = {c.lower() for c in lien.get("comptes", [])}
        if len(comptes) < 2 or lien.get("banal"):
            continue
        type_pivot = lien.get("pivot_type")
        if type_pivot == "son":
            groupe: list[PieceSource] = []
            for preuve in lien.get("preuves", []):
                cible = par_cle_url.get(_cle_url(preuve.get("url") or "") or "")
                if cible:
                    groupe.append(cible)
            if len({p.auteur for p in groupe}) < 2:  # pas d'adresse exacte : première pièce de chaque compte
                groupe = [par_compte[c][0] for c in comptes if c in par_compte]
            sortie.extend(_chaine(groupe, "meme_son", PROBABLE, "même son utilisé"))
        elif type_pivot not in _TYPES_PIVOT_SANS_OPERATEUR:
            niveau = ETABLIE if lien.get("fort") else PROBABLE if float(lien.get("poids") or 0) >= POIDS_PROBABLE else None
            if niveau is None:
                continue
            groupe = [par_compte[c][0] for c in comptes if c in par_compte]
            etiquette = f"même {type_pivot.replace('_', ' ')} sur plusieurs comptes"
            sortie.extend(_chaine(groupe, "meme_operateur", niveau, etiquette))

    if export:
        son_seul = _liens_son_seul(export)
        par_grappe: dict[str, list[PieceSource]] = defaultdict(list)
        par_id = {p.id: p for p in pieces}
        for lien in export.get("liens", []):
            if lien.get("id") in son_seul and lien.get("source_id") in par_id:
                par_grappe[lien["cible_id"]].append(par_id[lien["source_id"]])
        for groupe in par_grappe.values():
            sortie.extend(_chaine(groupe, "meme_son", PROBABLE, "même son (grappe du dossier)"))
    return sortie


def _chaine(groupe: list[PieceSource], regle: str, niveau: str, preuve: str) -> list[Dependance]:
    """Chaque pièce du groupe dépend de la plus ancienne (comptes différents seulement)."""
    uniques = sorted({p.id: p for p in groupe}.values(), key=_cle_ordre)
    if len(uniques) < 2:
        return []
    racine = uniques[0]
    return [Dependance(p.id, racine.id, regle, niveau, preuve) for p in uniques[1:]]


def _regle_liens_export(pieces: list[PieceSource], export: dict[str, Any]) -> list[Dependance]:
    """`reprend` et paires de `dit_la_meme_chose` vers la même grappe, jugés sur la phrase de leurs preuves."""
    par_id = {p.id: p for p in pieces}
    son_seul = _liens_son_seul(export)

    def extraits(lien: dict[str, Any], url: str | None) -> list[list[str]]:
        cle = _cle_url(url) if url else None
        segs = []
        for pr in lien.get("preuves", []):
            if cle and pr.get("url") and _cle_url(pr["url"]) != cle:
                continue
            mots = normaliser_phrase(pr.get("extrait") or "").split()
            if mots:
                segs.append(mots)
        return segs

    sortie: list[Dependance] = []

    def juger(recente: PieceSource, ancienne: PieceSource, segs_r: list[list[str]], segs_a: list[list[str]], via: str) -> None:
        suite = _plus_longue_suite(segs_r, segs_a)
        niveau = _niveau_suite(len(suite))
        if niveau:
            sortie.append(Dependance(
                recente.id, ancienne.id, "reprise_phrase", niveau,
                f"{via} : phrase commune de {len(suite)} mots : « {_citer(suite)} »",
            ))

    par_grappe: dict[str, list[tuple[PieceSource, dict[str, Any]]]] = defaultdict(list)
    for lien in export.get("liens", []):
        source, cible = par_id.get(lien.get("source_id", "")), par_id.get(lien.get("cible_id", ""))
        if lien.get("type_lien") == "reprend" and source and cible:
            if lien.get("statut") == "contredit":
                continue
            juger(source, cible, extraits(lien, source.url), extraits(lien, cible.url), "lien « reprend »")
        elif lien.get("type_lien") == "dit_la_meme_chose" and source and lien["id"] not in son_seul:
            par_grappe[lien["cible_id"]].append((source, lien))
    for membres in par_grappe.values():
        membres.sort(key=lambda m: _cle_ordre(m[0]))
        membres = membres[:40]
        for (ancienne, l_a), (recente, l_r) in combinations(membres, 2):
            juger(recente, ancienne, extraits(l_r, recente.url), extraits(l_a, ancienne.url), "même grappe")
    return sortie


def dependances(
    pieces: list[PieceSource],
    *,
    export: dict[str, Any] | None = None,
    liens_pivots: list[dict[str, Any]] | None = None,
    seuil_copie: float = 0.8,
    seuil_copie_probable: float = 0.5,
) -> list[Dependance]:
    """Toutes les dépendances trouvées. La plus récente dépend de la plus ancienne ; une seule par
    (de, vers, règle), au niveau le plus fort ; au plus 25 par pièce et par règle."""
    brutes: list[Dependance] = []
    brutes += _regle_meme_auteur(pieces)
    brutes += _regle_textes(pieces, seuil_copie, seuil_copie_probable)
    brutes += _regle_citation(pieces)
    brutes += _regle_credit_agence(pieces)
    brutes += _regle_operateur_et_son(pieces, export, liens_pivots or [])
    if export:
        brutes += _regle_liens_export(pieces, export)

    meilleures: dict[tuple[str, str, str], Dependance] = {}
    for d in brutes:
        if d.de == d.vers:
            continue
        cle = (d.de, d.vers, d.regle)
        if cle not in meilleures or (d.niveau == ETABLIE and meilleures[cle].niveau != ETABLIE):
            meilleures[cle] = d
    par_piece: dict[tuple[str, str], list[Dependance]] = defaultdict(list)
    for d in meilleures.values():
        par_piece[(d.de, d.regle)].append(d)
    sortie: list[Dependance] = []
    for groupe in par_piece.values():
        groupe.sort(key=lambda d: (d.niveau != ETABLIE, d.vers))
        sortie.extend(groupe[:DEPENDANCES_MAX])
    sortie.sort(key=lambda d: (d.de, d.vers, d.regle))
    return sortie


# ---------------------------------------------------------------------------
# Origines (union-find)
# ---------------------------------------------------------------------------

class _Union:
    def __init__(self, noeuds: Iterable[str]):
        self.parent = {n: n for n in noeuds}

    def racine(self, n: str) -> str:
        while self.parent[n] != n:
            self.parent[n] = self.parent[self.parent[n]]
            n = self.parent[n]
        return n

    def fusionner(self, a: str, b: str) -> None:
        ra, rb = self.racine(a), self.racine(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _pluriel(n: int, mot: str) -> str:
    return f"{n} {mot}" + ("s" if n > 1 else "")


def phrase_resume(n_pieces: int, n_comptes: int, k: int) -> str:
    return f"{_pluriel(n_pieces, 'pièce')} · {_pluriel(n_comptes, 'compte')} · {_pluriel(k, 'origine')} sans lien visible"


def _liens_contestes(export: dict[str, Any] | None, ids: set[str]) -> list[dict[str, Any]]:
    if not export:
        return []
    return [
        l for l in export.get("liens", [])
        if (l.get("statut") == "contredit" or l.get("conteste") or l.get("type_lien") in ("contredit", "conteste"))
        and (l.get("source_id") in ids or l.get("cible_id") in ids)
    ]


def origines(
    pieces: list[PieceSource],
    deps: list[Dependance],
    *,
    export: dict[str, Any] | None = None,
    parmi: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Familles de pièces qui remontent à la même origine.

    K ne fusionne que les dépendances établies ; k_prudent y ajoute les probables (donc k_prudent <= K).
    `parmi` restreint le compte à un sous-ensemble de pièces, en gardant les liens passant par les autres.
    """
    par_id = {p.id: p for p in pieces}
    retenues = list(pieces) if parmi is None else [par_id[i] for i in dict.fromkeys(parmi) if i in par_id]
    ids_retenus = {p.id for p in retenues}
    virtuels = {n for d in deps for n in (d.de, d.vers) if n.startswith("agence:") and n not in par_id}
    noeuds = list(par_id) + sorted(virtuels)
    etablie, prudente = _Union(noeuds), _Union(noeuds)
    for d in deps:
        if d.de not in etablie.parent or d.vers not in etablie.parent:
            continue
        if d.niveau == ETABLIE:
            etablie.fusionner(d.de, d.vers)
        prudente.fusionner(d.de, d.vers)

    familles: dict[str, list[PieceSource]] = defaultdict(list)
    for p in retenues:
        familles[etablie.racine(p.id)].append(p)
    k = len(familles)
    k_prudent = len({prudente.racine(p.id) for p in retenues})

    contestes = _liens_contestes(export, ids_retenus)
    ids_contestes = {i for l in contestes for i in (l.get("source_id"), l.get("cible_id"))}
    liste = []
    for racine_union, membres in familles.items():
        membres.sort(key=_cle_ordre)
        agences = sorted(v for v in virtuels if etablie.racine(v) == racine_union)
        datees = [m.date for m in membres if m.date]
        internes = [d for d in deps if etablie.parent.get(d.de) is not None and etablie.racine(d.de) == racine_union
                    and etablie.racine(d.vers) == racine_union]
        liste.append({
            "racine": agences[0] if len(agences) == 1 else membres[0].id,
            "membres": [m.id for m in membres],
            "agences": agences,
            "n_comptes": len({m.auteur for m in membres if m.auteur}),
            "premiere_date": min(datees).isoformat() if datees else None,
            "dependances": [d.vers_dict() for d in internes],
            "contestee": any(m.id in ids_contestes for m in membres),
        })
    liste.sort(key=lambda o: (o["premiere_date"] is None, o["premiere_date"] or "", o["racine"]))
    racine_de = {m: o["racine"] for o in liste for m in o["membres"]}
    probables = sorted({
        tuple(sorted((racine_de[d.de], racine_de[d.vers])))
        for d in deps
        if d.niveau == PROBABLE and d.de in racine_de and d.vers in racine_de and racine_de[d.de] != racine_de[d.vers]
    })

    if not retenues:
        statut = STATUT_VIDE
    elif contestes:
        statut = STATUT_CONTESTEE
    elif k >= 2:
        statut = STATUT_PLUSIEURS
    else:
        statut = STATUT_UNIQUE
    n_comptes = len({p.auteur for p in retenues if p.auteur})
    return {
        "n_pieces": len(retenues),
        "n_comptes": n_comptes,
        "k_origines": k,
        "k_prudent": k_prudent,
        "origines": liste,
        "liaisons_probables": [list(p) for p in probables],
        "statut": statut,
        "liens_contestes": sorted(l["id"] for l in contestes if l.get("id")),
        "question_recoupement": QUESTION_RECOUPEMENT if k == 1 else None,
        "phrase": phrase_resume(len(retenues), n_comptes, k),
        "limites": list(LIMITES),
    }


# ---------------------------------------------------------------------------
# Entrées pour le dossier et pour une affirmation
# ---------------------------------------------------------------------------

def _tout_calculer(
    export: dict[str, Any], pages: Iterable[dict[str, Any]] | None, liens_pivots: list[dict[str, Any]] | None,
    **options: Any,
) -> tuple[list[PieceSource], list[Dependance]]:
    pieces = [p for p in depuis_export(export, pages=pages) if p.nature in _NATURES_RESUME]
    return pieces, dependances(pieces, export=export, liens_pivots=liens_pivots, **options)


def resume_dossier(
    export: dict[str, Any], *, pages: Iterable[dict[str, Any]] | None = None,
    liens_pivots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Le chiffre du moment B : origines sur toutes les vidéos et pages du dossier."""
    pieces, deps = _tout_calculer(export, pages, liens_pivots)
    return origines(pieces, deps, export=export)


def pour_affirmation(
    export: dict[str, Any], pieces_ids: Iterable[str], *, pages: Iterable[dict[str, Any]] | None = None,
    liens_pivots: list[dict[str, Any]] | None = None, **options: Any,
) -> dict[str, Any]:
    """Origines d'un sous-ensemble de pièces. Les liens passant par des pièces extérieures comptent encore :
    deux pièces qui reprennent une même troisième ne sont pas indépendantes."""
    pieces, deps = _tout_calculer(export, pages, liens_pivots, **options)
    return origines(pieces, deps, export=export, parmi=pieces_ids)
