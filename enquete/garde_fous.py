"""Garde-fous de publication : mineurs, particuliers, mis en cause, mots interdits.

Quatre règles, appliquées avant d'afficher un dossier ou d'envoyer quoi que ce
soit hors de la machine :

1. Mineurs. Un compte ou une pièce qui laisse penser à un mineur (âge de moins
   de 18 ans, collège, lycée, « teen »...) est retenu : pseudonyme masqué et
   texte de la pièce retiré. Le repérage est un indice, pas une preuve : il
   préfère retenir trop que pas assez.
2. Particuliers. Un compte qui n'est ni vérifié, ni gros, ni une organisation
   est un particulier : son pseudo est remplacé par un pseudonyme stable
   (« compte privé a3f9 »), dans les libellés, les adresses et les citations.
   Une @mention inconnue dans une citation est traitée comme un particulier.
3. Mis en cause. Un dossier n'y entre qu'avec une source notée A ou B et un
   contradictoire prévu (lexique, §4 : statut, et date et canal dès que la
   sollicitation est envoyée).
4. Mots interdits. Les formules du lexique de rédaction (§3), plus les
   anciens mots de l'échelle de certitude, sont refusées dans les textes que
   l'outil écrit lui-même (pas dans les citations de tiers).

Les seuils ne sont pas des mesures : `SEUIL_ABONNES` est un réglage de prudence
(au-dessus, le compte est traité comme une personne publique).

Ligne de commande :
    python -m enquete.garde_fous <reseau.json> [--cache <dossier cache oriane>]
imprime des comptes (jamais un pseudo) avant et après masquage ; code de sortie 1
si un pseudo de particulier ou de mineur reste lisible après masquage.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

SEUIL_ABONNES = 100_000
MARQUE_RETENUE = "[contenu retenu : mineur probable]"
STATUTS_SOLLICITATION = (
    "à envoyer", "envoyée", "relancée", "réponse reçue", "sans réponse", "refus de répondre",
)


class ErreurGardeFou(ValueError):
    """Un export ou une requête a franchi une règle de publication."""


@dataclass(frozen=True)
class Verdict:
    accepte: bool
    raisons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.accepte


@dataclass(frozen=True)
class Profil:
    pseudo: str
    abonnes: int | None = None
    verifie: bool = False
    bio: str = ""
    nom_affiche: str = ""


# ---------------------------------------------------------------------------
# Texte
# ---------------------------------------------------------------------------

def _brut(texte: str) -> str:
    """Minuscules, apostrophes droites, « œ » éclaté ; longueur et accents conservés
    pour que les positions restent celles du texte d'origine, sauf « œ »."""
    return texte.lower().replace("’", "'").replace("‘", "'")


_ACCENTS = {"a": "[aàâä]", "e": "[eéèêë]", "i": "[iîï]", "o": "[oôö]", "u": "[uùûü]", "c": "[cç]"}


def _souple(mot: str) -> str:
    """Motif d'un mot ou d'une expression, tolérant aux accents et à « œ »."""
    mot = mot.lower().replace("œ", "oe")
    sortie = []
    i = 0
    while i < len(mot):
        c = mot[i]
        if mot[i : i + 2] == "oe":
            sortie.append("(?:oe|œ)")
            i += 2
            continue
        if c == " ":
            sortie.append(r"\s+")
        elif c == "'":
            sortie.append("['’]")
        elif c in _ACCENTS:
            sortie.append(_ACCENTS[c])
        else:
            sortie.append(re.escape(c))
        i += 1
    return "".join(sortie)


def _mot(motif: str) -> str:
    return rf"(?<![\w]){motif}(?![\w])"


# ---------------------------------------------------------------------------
# Mineurs
# ---------------------------------------------------------------------------

_AGE = re.compile(
    r"(?<![\w/])(\d{1,2})\s*(?:ans?|y/?o|yo|yrs?|years?[\s-]*old|year[\s-]*old)(?![\w])"
)
_AVANT_DUREE = re.compile(
    r"(?:depuis|il y a|pendant|dans|pour|durant|since|for|every|chaque|après|apres|plus de|moins de|over|than)\s+(?:\w+\s+)?$"
)
_APRES_DUREE = re.compile(r"\s*(?:ago|de prison|de r[ée]clusion|d'exp[ée]rience|d'[âa]ge de diff|plus tard|later)")
_ENFANT_AGE = re.compile(
    r"(?:ma fille|mon fils|mon enfant|mes enfants|ma petite|mon petit|my (?:daughter|son|kid|child)|"
    r"(?:il|elle|he|she) (?:a|is|has)|j'ai|i'm|i am|im|je suis [ée]g[ée]e? de|age[:\s])\s*(?:de\s+)?(\d{1,2})\b",
)
_NAISSANCE = re.compile(
    r"(?:n[ée]e?\s+en|n[ée]e?\s+le\s+[\d/.\-]*|born(?:\s+in)?|b-?day|ne en)\s*(\d{4})\b|\b(20[01]\d)\s*(?:baby|kid|born)\b"
)
_MOTS_MINEUR = re.compile(
    "|".join(
        _mot(_souple(m))
        for m in (
            "mineur", "mineure", "collégien", "collégienne", "lycéen", "lycéenne", "écolier",
            "écolière", "ado", "adolescent", "adolescente", "préado", "teen", "teenager",
            "high school", "middle school", "elementary school", "primary school",
            "au collège", "au lycée", "en 6ème", "en 5ème", "en 4ème", "en 3ème",
            "en seconde", "en première", "en terminale", "grade school", "underage",
            "enfant de", "petit garçon", "petite fille", "bébé", "baby",
        )
    ),
    re.IGNORECASE,
)


def indices_mineur(texte: str, annee: int | None = None) -> list[str]:
    """Raisons pour lesquelles le texte laisse penser à un mineur (liste vide : aucune)."""
    annee = annee or date.today().year
    t = _brut(texte or "")
    raisons: list[str] = []
    for m in _AGE.finditer(t):
        age = int(m.group(1))
        if 3 <= age <= 17 and not _AVANT_DUREE.search(t[: m.start()]) and not _APRES_DUREE.match(t, m.end()):
            raisons.append(f"âge cité : {age} ans")
    for m in _ENFANT_AGE.finditer(t):
        age = int(m.group(1))
        if 3 <= age <= 17:
            raisons.append(f"âge cité : {age} ans")
    for m in _NAISSANCE.finditer(t):
        an = int(m.group(1) or m.group(2))
        if annee - 18 < an <= annee:
            raisons.append(f"année de naissance citée : {an}")
    for m in _MOTS_MINEUR.finditer(t):
        raisons.append(f"mot : {m.group(0).strip()}")
    # Sans doublon, ordre conservé.
    return list(dict.fromkeys(raisons))


def mineur_probable(*textes: str, annee: int | None = None) -> Verdict:
    """`accepte` vaut ici « mineur probable » ; `raisons` dit pourquoi."""
    raisons: list[str] = []
    for t in textes:
        raisons.extend(indices_mineur(t, annee))
    raisons = list(dict.fromkeys(raisons))
    return Verdict(bool(raisons), tuple(raisons))


# ---------------------------------------------------------------------------
# Particuliers
# ---------------------------------------------------------------------------

_ORGANISATION = re.compile(
    "|".join(
        _mot(_souple(m))
        for m in (
            "official", "officiel", "officielle", "news", "actualités", "actualité", "journal",
            "chaîne", "média", "media", "presse", "radio", "magazine", "agence", "association",
            "ministère", "mairie", "gouvernement", "fondation", "ong",
        )
    ),
    re.IGNORECASE,
)


def est_particulier(profil: Profil, seuil_abonnes: int = SEUIL_ABONNES) -> bool:
    """Vrai si le compte est traité comme un particulier : ni vérifié, ni gros,
    ni organisation reconnue à sa description. Sans nombre d'abonnés connu, vrai."""
    if profil.verifie:
        return False
    if profil.abonnes is not None and profil.abonnes >= seuil_abonnes:
        return False
    if _ORGANISATION.search(f"{profil.bio} {profil.nom_affiche}"):
        return False
    return True


_RE_MENTION = re.compile(r"(?<![\w.])@([A-Za-z0-9_](?:[A-Za-z0-9._]{0,28}[A-Za-z0-9_])?)")


def _motif_pseudo(pseudo: str) -> re.Pattern[str]:
    """Le pseudo, avec « @ » toujours, sans « @ » s'il compte 5 caractères ou plus."""
    p = re.escape(pseudo)
    fin = r"(?![A-Za-z0-9_]|\.[A-Za-z0-9_])"
    if len(pseudo) >= 5:
        return re.compile(rf"(?<![A-Za-z0-9._])@?{p}{fin}", re.IGNORECASE)
    return re.compile(rf"(?<![A-Za-z0-9._])@{p}{fin}", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Masquage
# ---------------------------------------------------------------------------

_CHAMPS_CONTENU = ("extrait", "caption", "legende", "texte_citation")


def _toutes_les_chaines(obj: Any, chemin: str = "") -> Iterable[tuple[str, str, str]]:
    """(chemin, clé, texte) pour chaque chaîne d'un objet JSON."""
    if isinstance(obj, str):
        yield chemin, chemin.rsplit(".", 1)[-1].split("[")[0], obj
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            yield from _toutes_les_chaines(v, f"{chemin}.{k}" if chemin else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _toutes_les_chaines(v, f"{chemin}[{i}]")


_RE_COMPTE_LIBELLE = re.compile(r"^@([A-Za-z0-9._]{1,30}) \(([^)]+)\)")


def profils_depuis_cache(dossier_cache: str | Path) -> dict[str, Profil]:
    """Pseudo (minuscules) -> profil, d'après les résultats Oriane du cache."""
    profils: dict[str, Profil] = {}
    for fichier in sorted(Path(dossier_cache).glob("*.json")):
        try:
            resultats = json.loads(fichier.read_text(encoding="utf-8"))["reponse"]["data"]["results"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for r in resultats:
            pseudo = (r.get("profileHandle") or "").strip()
            if not pseudo:
                continue
            ancien = profils.get(pseudo.lower())
            nouveau = Profil(
                pseudo=pseudo,
                abonnes=r.get("profileFollowersCount"),
                verifie=bool(r.get("profileVerified")),
                bio=r.get("profileBio") or "",
                nom_affiche=r.get("profileDisplayName") or "",
            )
            if ancien is None or (ancien.abonnes is None and nouveau.abonnes is not None):
                profils[pseudo.lower()] = nouveau
    return profils


class Masqueur:
    """Décide qui est masqué dans un export et le masque, de façon stable.

    `profils` : pseudo -> `Profil` (le cache Oriane en donne verification, bio
    et abonnés). Un pseudo de l'export absent de `profils` est jugé sur le
    nombre d'abonnés de sa pièce ; sans nombre, il est traité comme particulier.
    """

    def __init__(
        self,
        profils: Mapping[str, Profil] | None = None,
        *,
        seuil_abonnes: int = SEUIL_ABONNES,
        sel: str | None = None,
        masquer_mentions: bool = True,
        annee: int | None = None,
    ) -> None:
        self.profils = {k.lower(): v for k, v in (profils or {}).items()}
        self.seuil = seuil_abonnes
        self._sel = (sel or secrets.token_hex(8)).encode()
        self.masquer_mentions = masquer_mentions
        self.annee = annee
        self.particuliers: set[str] = set()
        self.mineurs: dict[str, list[str]] = {}
        self.publics: set[str] = set()
        self._motifs: dict[str, re.Pattern[str]] = {}

    # -- pseudonymes --------------------------------------------------------
    def pseudonyme(self, pseudo: str) -> str:
        h = hmac.new(self._sel, pseudo.lower().encode(), hashlib.sha256).hexdigest()[:4]
        return f"compte privé {h}"

    # -- découverte ---------------------------------------------------------
    def decouvrir(self, export: Any) -> "Masqueur":
        """Repère les comptes de l'export et les range : public, particulier, mineur."""
        abonnes: dict[str, int | None] = {}
        textes: dict[str, list[str]] = {}
        for chemin, _cle, t in _toutes_les_chaines(export):
            m = _RE_COMPTE_LIBELLE.match(t)
            if m:
                abonnes.setdefault(m.group(1).lower(), None)
            for mention in _RE_MENTION.finditer(t):
                abonnes.setdefault(mention.group(1).lower(), None)
        # Abonnés lus sur les pièces de type compte.
        for piece in _pieces(export):
            m = _RE_COMPTE_LIBELLE.match(piece.get("libelle") or "")
            if m and isinstance(piece.get("attributs"), Mapping):
                n = piece["attributs"].get("abonnes")
                abonnes[m.group(1).lower()] = n if isinstance(n, int) else None
        for cle, n in abonnes.items():
            profil = self.profils.get(cle)
            if profil is None:
                profil = Profil(pseudo=cle, abonnes=n)
            elif profil.abonnes is None and n is not None:
                profil = Profil(profil.pseudo, n, profil.verifie, profil.bio, profil.nom_affiche)
            if est_particulier(profil, self.seuil):
                self.particuliers.add(cle)
                self.profils.setdefault(cle, profil)
            else:
                self.publics.add(cle)
        # Mineurs : bio et nom du compte, puis citations des pièces du compte.
        for piece in _pieces(export):
            m = _RE_COMPTE_LIBELLE.match(piece.get("libelle") or "")
            if not m:
                continue
            cle = m.group(1).lower()
            profil = self.profils.get(cle)
            sources = [profil.bio, profil.nom_affiche] if profil else []
            sources += [p.get("extrait") or "" for p in piece.get("preuves") or [] if isinstance(p, Mapping)]
            raisons = mineur_probable(*sources, annee=self.annee).raisons
            if raisons:
                self.mineurs[cle] = list(raisons)
                self.particuliers.add(cle)
                self.publics.discard(cle)
        return self

    # -- masquage -----------------------------------------------------------
    def _motif(self, pseudo: str) -> re.Pattern[str]:
        if pseudo not in self._motifs:
            self._motifs[pseudo] = _motif_pseudo(pseudo)
        return self._motifs[pseudo]

    def masquer_texte(self, texte: str) -> str:
        for pseudo in sorted(self.particuliers, key=len, reverse=True):
            texte = self._motif(pseudo).sub(lambda _m, p=pseudo: f"[{self.pseudonyme(p)}]", texte)
        if self.masquer_mentions:
            def inconnue(m: re.Match[str]) -> str:
                cle = m.group(1).lower()
                return m.group(0) if cle in self.publics else "[compte non identifié]"
            texte = _RE_MENTION.sub(inconnue, texte)
        return texte

    def _mentionne_mineur(self, texte: str) -> bool:
        return any(self._motif(p).search(texte) for p in self.mineurs)

    def masquer(self, export: Any) -> Any:
        """Copie de l'export, sans pseudo de particulier ni contenu de mineur."""
        if not self.particuliers and not self.publics:
            self.decouvrir(export)
        return self._masquer(copy.deepcopy(export))

    def _masquer(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.masquer_texte(obj)
        if isinstance(obj, list):
            return [self._masquer(v) for v in obj]
        if isinstance(obj, dict):
            if self.mineurs and any(
                isinstance(v, str) and self._mentionne_mineur(v) for v in obj.values()
            ):
                _retenir(obj)
            return {k: self._masquer(v) for k, v in obj.items()}
        return obj

    # -- contrôle -----------------------------------------------------------
    def pseudos_lisibles(self, export: Any) -> list[tuple[str, str]]:
        """(chemin, genre) pour chaque pseudo de particulier ou de mineur encore lisible,
        et chaque @mention non classée. Ne renvoie jamais le pseudo lui-même."""
        trouves = []
        for chemin, _cle, t in _toutes_les_chaines(export):
            for pseudo in self.particuliers:
                if self._motif(pseudo).search(t):
                    trouves.append((chemin, "mineur" if pseudo in self.mineurs else "particulier"))
            if self.masquer_mentions:
                for m in _RE_MENTION.finditer(t):
                    if m.group(1).lower() not in self.publics:
                        trouves.append((chemin, "mention non classée"))
        return trouves


def _retenir(obj: Any) -> None:
    """Remplace le contenu cité de l'objet et de tout ce qu'il contient."""
    if isinstance(obj, dict):
        for champ in _CHAMPS_CONTENU:
            if isinstance(obj.get(champ), str):
                obj[champ] = MARQUE_RETENUE
        for v in obj.values():
            _retenir(v)
    elif isinstance(obj, list):
        for v in obj:
            _retenir(v)


def _pieces(export: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(export, Mapping):
        for d in export.get("dossiers") or []:
            if isinstance(d, Mapping):
                for p in d.get("pieces") or []:
                    if isinstance(p, Mapping):
                        yield p


# ---------------------------------------------------------------------------
# Mis en cause
# ---------------------------------------------------------------------------

def valider_mis_en_cause(
    fiabilites: Iterable[Any],
    contradictoire: Mapping[str, Any] | None,
) -> Verdict:
    """Un « mis en cause » exige une source A ou B et un contradictoire prévu.

    `fiabilites` : lettres A à F, objets `Solidite` ou dictionnaires `solidite`
    des sources qui portent l'imputation. `contradictoire` : `{"statut": ...,
    "date": ..., "canal": ...}` ; le statut est l'un de `STATUTS_SOLLICITATION` ;
    dès qu'il n'est plus « à envoyer », la date et le canal sont obligatoires
    (lexique §4 : « contacté » ne s'écrit qu'avec date et canal).
    """
    raisons = []
    lettres = []
    for f in fiabilites or []:
        if isinstance(f, str):
            lettres.append(f.upper())
        elif isinstance(f, Mapping):
            lettres.append(str(f.get("fiabilite_source", "")).upper())
        else:
            lettres.append(str(getattr(f, "fiabilite_source", "")).upper())
    if not any(x in ("A", "B") for x in lettres):
        raisons.append("aucune source notée A ou B : pas d'entrée dans « mis en cause »")
    if not contradictoire:
        raisons.append("contradictoire non prévu : aucune sollicitation consignée")
    else:
        statut = str(contradictoire.get("statut", "")).lower()
        if statut not in STATUTS_SOLLICITATION:
            raisons.append(f"statut de sollicitation inconnu : {statut or 'absent'}")
        elif statut != "à envoyer" and not (contradictoire.get("date") and contradictoire.get("canal")):
            raisons.append("sollicitation sans date ou sans canal : « contacté » ne peut pas s'écrire")
    return Verdict(not raisons, tuple(raisons))


# ---------------------------------------------------------------------------
# Mots interdits
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Trouvaille:
    mot: str
    categorie: str
    position: int


def _formules() -> list[tuple[re.Pattern[str], str]]:
    S = _souple
    brut = [
        # Culpabilité avant condamnation (lexique §3)
        (_mot(S("coupable")), "culpabilité"),
        (_mot(S("auteur de")), "culpabilité"),
        (_mot(S("l'auteur présumé")), "culpabilité"),
        (_mot(S("le fraudeur")), "culpabilité"),
        (_mot(S("inculpé") + "s?"), "culpabilité"),
        (_mot(S("responsable de") + r"(?!\s+la\s+(?:publication|rédaction|redaction))"), "culpabilité"),
        (_mot(r"accus[éè]s?"), "culpabilité"),  # « accusé » ; le verbe « accuse » reste permis
        # Verdicts
        *[(_mot(S(m)), "verdict") for m in (
            "arnaque", "escroquerie", "bot", "faux compte", "troll", "ingérence", "diffamatoire",
            "société écran",
        )],
        # Insinuations
        *[(_mot(S(m)), "insinuation") for m in (
            "refuse de s'expliquer", "se mure dans le silence", "fuit", "enfin démasqué",
            "l'homme de l'ombre", "qui dirige vraiment", "opaque", "douteux",
        )],
        # Fausses sources
        *[(_mot(S(m)), "fausse source") for m in (
            "circule sur les réseaux", "selon des internautes", "selon une vidéo virale", "on dit que",
        )],
        (_mot(r"confirm[ée]e?s?\s+par\s+\d+\s+vid[ée]os?"), "fausse source"),
        # Fausses garanties
        (_mot(r"prouv[ée]e?s?"), "fausse garantie"),
        *[(_mot(S(m)), "fausse garantie") for m in (
            "vérifié par l'IA", "authentifié", "score de confiance", "lien intelligent",
        )],
        (r"\d+\s?%\s+de\s+correspondance", "fausse garantie"),
        # Fausses garanties juridiques
        *[(_mot(S(m)), "garantie juridique") for m in (
            "validé juridiquement", "sans risque", "bonne foi acquise", "conforme RGPD",
            "exempté du RGPD", "vous ne risquez rien",
        )],
        (_mot(S("conforme") + r"\s+(?:à\s+la\s+loi|juridiquement|au\s+droit)"), "garantie juridique"),
        # Sources humaines et pièces
        *[(_mot(S(m)), "source humaine") for m in (
            "informateur", "indic", "mouchard", "taupe", "fuiteur", "document volé", "données piratées",
        )],
        # Mots de logiciel
        *[(_mot(S(m) + "s?"), "mot de logiciel") for m in (
            "agent", "nœud", "grappe", "gabarit", "graine", "index", "corpus", "pivot", "cache", "banal",
        )],
        (_mot(r"poids"), "mot de logiciel"),
        *[(_mot(S(m)), "mot de logiciel") for m in ("premier vu", "hors de ma portée")],
        (_mot(r"(?:ta|tes|tu|toi)"), "tutoiement"),
        # Anciens mots de l'échelle de certitude
        *[(_mot(S(m)), "ancien mot") for m in (
            "coïncidence probable", "appui faible", "appui net", "lien très fort", "lien fort",
        )],
    ]
    return [(re.compile(p, re.IGNORECASE), cat) for p, cat in brut]


_FORMULES = _formules()
_GUILLEMETS = re.compile(r"«[^»]*»|“[^”]*”|\"[^\"]*\"")


def mots_interdits(texte: str) -> list[Trouvaille]:
    """Formules interdites du lexique trouvées dans un texte que l'outil écrit.

    Un verdict (« arnaque »...) entre guillemets est toléré : le lexique n'admet
    une qualification que citée, avec sa source.
    """
    t = _brut(texte or "")
    citations = [(m.start(), m.end()) for m in _GUILLEMETS.finditer(t)]
    trouves = []
    for motif, cat in _FORMULES:
        for m in motif.finditer(t):
            if cat == "verdict" and any(a <= m.start() < b for a, b in citations):
                continue
            trouves.append(Trouvaille(m.group(0), cat, m.start()))
    return sorted(trouves, key=lambda x: x.position)


def verifier_texte(texte: str) -> None:
    trouves = mots_interdits(texte)
    if trouves:
        raise ErreurGardeFou(
            "mots interdits : " + ", ".join(f"« {t.mot} » ({t.categorie})" for t in trouves)
        )


# Champs que l'outil écrit lui-même ; les citations de tiers (extrait) en sont exclues.
CHAMPS_ECRITS = frozenset(
    {"nom", "libelle", "texte", "explication", "methode", "en_mots", "echelle", "source_libelle",
     "cible_libelle", "resume", "titre", "comblee_par_libelle"}
)


# ---------------------------------------------------------------------------
# Contrôle d'un export
# ---------------------------------------------------------------------------

@dataclass
class Anomalie:
    genre: str  # pseudo lisible, mot interdit, mis en cause
    chemin: str
    detail: str


@dataclass
class Rapport:
    anomalies: list[Anomalie] = field(default_factory=list)
    comptes: dict[str, int] = field(default_factory=dict)

    def __bool__(self) -> bool:  # vrai : l'export est propre
        return not self.anomalies

    def par_genre(self) -> dict[str, int]:
        sortie: dict[str, int] = {}
        for a in self.anomalies:
            sortie[a.genre] = sortie.get(a.genre, 0) + 1
        return sortie


def verifier_export(export: Any, masqueur: Masqueur | None = None) -> Rapport:
    """Vérifie un export avant affichage ou requête externe : pseudos lisibles de
    particuliers ou de mineurs, mots interdits, « mis en cause » mal fondés."""
    masqueur = masqueur or Masqueur()
    if not masqueur.particuliers and not masqueur.publics:
        masqueur.decouvrir(export)
    rapport = Rapport(
        comptes={
            "particuliers": len(masqueur.particuliers),
            "mineurs_probables": len(masqueur.mineurs),
            "personnes_publiques": len(masqueur.publics),
        }
    )
    for chemin, genre in masqueur.pseudos_lisibles(export):
        rapport.anomalies.append(Anomalie(f"{genre} lisible", chemin, "pseudo non masqué"))
    for chemin, cle, t in _toutes_les_chaines(export):
        if cle in CHAMPS_ECRITS:
            for trouve in mots_interdits(t):
                rapport.anomalies.append(Anomalie("mot interdit", chemin, f"« {trouve.mot} » ({trouve.categorie})"))
    if isinstance(export, Mapping):
        for d in export.get("dossiers") or []:
            if not (isinstance(d, Mapping) and d.get("role") == "mis_en_cause"):
                continue
            for i, piece in enumerate(d.get("pieces") or []):
                v = valider_mis_en_cause(
                    [piece.get("solidite") or {}], piece.get("contradictoire")
                )
                if not v:
                    rapport.anomalies.append(
                        Anomalie("mis en cause", f"dossiers[{d.get('role')}].pieces[{i}]", " ; ".join(v.raisons))
                    )
    return rapport


def exiger_export_propre(export: Any, masqueur: Masqueur | None = None) -> Any:
    """Masque puis vérifie ; renvoie l'export masqué ou lève `ErreurGardeFou`."""
    masqueur = masqueur or Masqueur()
    masqueur.decouvrir(export)
    propre = masqueur.masquer(export)
    rapport = verifier_export(propre, masqueur)
    if rapport:
        return propre
    raise ErreurGardeFou(
        "export refusé : " + ", ".join(f"{n} {g}" for g, n in rapport.par_genre().items())
    )


def verifier_requete_externe(requete: str, masqueur: Masqueur) -> None:
    """Refuse d'envoyer hors de la machine un texte qui contient un pseudo de
    particulier ou de mineur connu du masqueur."""
    for pseudo in masqueur.particuliers:
        if masqueur._motif(pseudo).search(requete):
            raise ErreurGardeFou("requête externe refusée : elle contient le pseudo d'un particulier")


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cache = None
    if "--cache" in args:
        i = args.index("--cache")
        cache = args[i + 1]
        del args[i : i + 2]
    if len(args) != 1:
        print("usage : python -m enquete.garde_fous <reseau.json> [--cache <dossier>]")
        return 2
    export = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    profils = profils_depuis_cache(cache) if cache else {}
    masqueur = Masqueur(profils, sel="essai").decouvrir(export)
    avant = masqueur.pseudos_lisibles(export)
    masque = masqueur.masquer(export)
    apres = masqueur.pseudos_lisibles(masque)
    rapport = verifier_export(masque, masqueur)
    print(f"comptes : {rapport.comptes}")
    print(f"pseudos lisibles avant masquage : {len(avant)}")
    print(f"pseudos lisibles après masquage : {len(apres)}")
    print(f"autres anomalies après masquage : { {g: n for g, n in rapport.par_genre().items() if 'lisible' not in g} }")
    return 1 if apres else 0


if __name__ == "__main__":
    raise SystemExit(main())
