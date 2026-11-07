"""Modèle de données du réseau d'enquête et son stockage.

Image simple : un mur d'enquête dont on ne peut rien décrocher. Chaque fiche
(nœud) et chaque ficelle (lien) y est épinglée avec sa preuve ; si l'on se
trompe, on épingle une nouvelle version à côté, l'ancienne reste visible.

Trois règles tiennent tout le module :
  1. un lien sans preuve n'existe pas (refus à la construction) ;
  2. le stockage est en ajout seul : SQLite refuse tout UPDATE et tout DELETE
     sur le journal, une correction ajoute une version ;
  3. la solidité d'une pièce se dit en deux notes séparées (fiabilité de la
     source, crédibilité de l'information), toujours traduites en mots.

Références : docs/methode.md §4 (liens, rareté, échelle de lecture), §8 (échelle de conclusion),
§12 (le réseau, neuf familles de liens) ; §2 et §6 (rôles dans l'affaire, hiérarchie des sources).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Vocabulaire : types de nœuds, rôles, familles de liens, statuts, échelle
# ---------------------------------------------------------------------------

class TypeNoeud(str, Enum):
    """Ce qu'est une pièce du dossier."""

    VIDEO = "video"
    COMPTE = "compte"
    PERSONNE = "personne"
    SOCIETE = "societe"
    SITE = "site"
    DOCUMENT = "document"
    # X, Reddit, Telegram, blog, presse : la plateforme va dans les attributs.
    PUBLICATION_TEXTE = "publication_texte"
    SON = "son"
    PHRASE = "phrase"
    TEXTE_ECRAN = "texte_ecran"
    IMAGE_CLE = "image_cle"
    HASHTAG = "hashtag"
    LIEU = "lieu"
    AFFIRMATION = "affirmation"
    QUESTION_OUVERTE = "question_ouverte"
    GRAPPE = "grappe"
    # Pivots de l'argent : l'adresse d'un portefeuille, un code promo ou d'affiliation, un
    # identifiant de paiement (cagnotte, lien de don). Le domaine, la page de liens et la boutique
    # sont des SITE ; le SIREN et le SIRET, une SOCIETE ; un canal Telegram, un COMPTE.
    PORTEFEUILLE = "portefeuille"
    CODE_PROMO = "code_promo"
    PAIEMENT = "paiement"


class Role(str, Enum):
    """Le rôle d'une pièce dans l'affaire : c'est lui qui range les dossiers."""

    ORIGINE = "origine"
    AMPLIFICATEUR = "amplificateur"
    BENEFICIAIRE = "beneficiaire"
    INFRASTRUCTURE = "infrastructure"
    VICTIME = "victime"
    TEMOIN = "temoin"
    EXPERT = "expert"
    CONTRADICTEUR = "contradicteur"
    MIS_EN_CAUSE = "mis_en_cause"
    INCONNU = "inconnu"


# Intitulés affichés pour chaque dossier : les dossiers sont des rôles (docs/methode.md §2 et §6).
LIBELLES_ROLES: dict[Role, str] = {
    Role.ORIGINE: "À l'origine",
    Role.AMPLIFICATEUR: "Amplificateurs",
    Role.BENEFICIAIRE: "Bénéficiaires",
    Role.INFRASTRUCTURE: "Infrastructure",
    Role.VICTIME: "Victimes et personnes concernées",
    Role.TEMOIN: "Témoins directs",
    Role.EXPERT: "Experts",
    Role.CONTRADICTEUR: "Contradicteurs",
    Role.MIS_EN_CAUSE: "Mis en cause",
    Role.INCONNU: "Inconnus",
}


class FamilleLien(str, Enum):
    """Les 9 familles de liens du catalogue (docs/methode.md §12)."""

    IDENTITE = "identite"
    CONTENU_DIT = "contenu_dit"
    CONTENU_MONTRE = "contenu_montre"
    TEMPS = "temps"
    LIEU = "lieu"
    INFRASTRUCTURE = "infrastructure"
    ARGENT = "argent"
    RELATIONS_SOCIALES = "relations_sociales"
    RECIT = "recit"


class StatutLien(str, Enum):
    """Ce qu'on peut dire d'un lien, en quatre statuts : prouvé (le code a retrouvé l'extrait dans la source),
    indice (un point commun qui ne suffit pas à conclure), contredit (un contre-indice pèse plus que l'appui),
    proposé (une piste pas encore contrôlée). Les contrôles qui font passer de l'un à l'autre : docs/methode.md §7."""

    PROUVE = "prouve"
    INDICE = "indice"
    CONTREDIT = "contredit"
    PROPOSE = "propose"


class StatutQuestion(str, Enum):
    OUVERTE = "ouverte"
    COMBLEE = "comblee"


class EchelleVerbale(str, Enum):
    """Échelle de force d'appui, une marche par facteur 10 (docs/methode.md §4).

    Elle parle de force des indices, jamais de probabilité que le lien soit vrai.
    """

    INDICES_CONTRAIRES = "indices contraires"
    COINCIDENCE_PROBABLE = "coïncidence probable"
    APPUI_FAIBLE = "appui faible"
    APPUI_NET = "appui net"
    LIEN_FORT = "lien fort"
    LIEN_TRES_FORT = "lien très fort"


def echelle_verbale(
    points: float,
    familles_independantes: int = 1,
    methodes_independantes: int = 1,
    identifiant_officiel: bool = False,
) -> EchelleVerbale:
    """Traduit un score en points (log10 du rapport de vraisemblance) en mots.

    Plafonds de la méthode : une seule famille d'indices plafonne à « appui net »
    (sauf identifiant officiel identique) ; « lien très fort » exige en plus deux
    méthodes indépendantes. Le contrôle du juge de preuve se fait en amont.
    """
    if points < 0:
        return EchelleVerbale.INDICES_CONTRAIRES
    if points < 1:
        return EchelleVerbale.COINCIDENCE_PROBABLE
    if points < 2:
        return EchelleVerbale.APPUI_FAIBLE
    if points < 3:
        return EchelleVerbale.APPUI_NET
    deux_familles = familles_independantes >= 2 or identifiant_officiel
    if not deux_familles:
        return EchelleVerbale.APPUI_NET
    if points < 4 or methodes_independantes < 2:
        return EchelleVerbale.LIEN_FORT
    return EchelleVerbale.LIEN_TRES_FORT


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def maintenant() -> datetime:
    return datetime.now(timezone.utc)


def empreinte_sha256(contenu: bytes) -> str:
    """Empreinte SHA-256 d'une archive : change si un seul octet change."""
    return hashlib.sha256(contenu).hexdigest()


def _nouvel_id() -> str:
    return uuid.uuid4().hex[:12]


def _texte_requis(valeur: str, nom: str) -> None:
    if not isinstance(valeur, str) or not valeur.strip():
        raise ValueError(f"{nom} est obligatoire et ne peut pas être vide")


_RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Solidité : deux notes séparées, traduites en mots
# ---------------------------------------------------------------------------

_MOTS_FIABILITE = {
    "A": "source de référence (document officiel, registre)",
    "B": "source habituellement fiable",
    "C": "source assez fiable",
    "D": "source pas toujours fiable",
    "E": "source peu fiable",
    "F": "source dont on ne peut pas juger la fiabilité",
}

_MOTS_CREDIBILITE = {
    1: "information recoupée par d'autres sources indépendantes",
    2: "information probablement vraie",
    3: "information possiblement vraie",
    4: "information douteuse",
    5: "information improbable",
    6: "information impossible à juger",
}


@dataclass(frozen=True)
class Solidite:
    """Qui parle (A à F) et ce qu'il dit (1 à 6), notés séparément.

    Une source très fiable peut dire une chose non recoupée, et une source
    douteuse peut dire une chose recoupée : les deux notes ne se fondent jamais.
    """

    fiabilite_source: str
    credibilite_information: int

    def __post_init__(self) -> None:
        if self.fiabilite_source not in _MOTS_FIABILITE:
            raise ValueError("la fiabilité de la source va de A à F")
        if self.credibilite_information not in _MOTS_CREDIBILITE:
            raise ValueError("la crédibilité de l'information va de 1 à 6")

    def en_mots(self) -> str:
        """La phrase affichée au survol ; jamais une note chiffrée seule."""
        source = _MOTS_FIABILITE[self.fiabilite_source]
        info = _MOTS_CREDIBILITE[self.credibilite_information]
        return f"{source} ; {info}"

    @property
    def matiere(self) -> str:
        """Comment l'interface rend la pièce : « solide » (cadre plein),
        « indice » (voilée) ou « inverifiable » (contour seul)."""
        if self.fiabilite_source == "F" or self.credibilite_information == 6:
            return "inverifiable"
        if self.fiabilite_source in "AB" and self.credibilite_information <= 2:
            return "solide"
        return "indice"

    def vers_dict(self) -> dict[str, Any]:
        return {
            "fiabilite_source": self.fiabilite_source,
            "credibilite_information": self.credibilite_information,
            "en_mots": self.en_mots(),
            "matiere": self.matiere,
        }

    @classmethod
    def depuis_dict(cls, d: dict[str, Any]) -> "Solidite":
        return cls(d["fiabilite_source"], d["credibilite_information"])


# ---------------------------------------------------------------------------
# Preuve
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Preuve:
    """Ce qui permet de rouvrir un fait : l'adresse, le mot pour mot, la copie.

    `debut_s` et `fin_s` ne servent que pour une vidéo (seconde de début et de
    fin de l'extrait). `empreinte_sha256` est celle de l'archive conservée.
    """

    url: str
    extrait: str
    source_donnee: str  # d'où vient la donnée : Oriane, registre, presse...
    date_collecte: datetime
    empreinte_sha256: str | None = None
    debut_s: float | None = None
    fin_s: float | None = None

    def __post_init__(self) -> None:
        _texte_requis(self.url, "l'URL d'origine de la preuve")
        _texte_requis(self.extrait, "l'extrait exact de la preuve")
        _texte_requis(self.source_donnee, "la source de la donnée")
        if not isinstance(self.date_collecte, datetime):
            raise ValueError("la date de collecte doit être une date")
        if self.empreinte_sha256 is not None:
            empreinte = self.empreinte_sha256.lower()
            if not _RE_SHA256.match(empreinte):
                raise ValueError("l'empreinte doit être un SHA-256 (64 caractères hexadécimaux)")
            object.__setattr__(self, "empreinte_sha256", empreinte)
        if self.fin_s is not None and self.debut_s is None:
            raise ValueError("une seconde de fin exige une seconde de début")
        if self.debut_s is not None:
            if self.debut_s < 0:
                raise ValueError("la seconde de début ne peut pas être négative")
            if self.fin_s is not None and self.fin_s < self.debut_s:
                raise ValueError("la seconde de fin précède la seconde de début")

    def vers_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "extrait": self.extrait,
            "source_donnee": self.source_donnee,
            "date_collecte": self.date_collecte.isoformat(),
            "empreinte_sha256": self.empreinte_sha256,
            "debut_s": self.debut_s,
            "fin_s": self.fin_s,
        }

    @classmethod
    def depuis_dict(cls, d: dict[str, Any]) -> "Preuve":
        return cls(
            url=d["url"],
            extrait=d["extrait"],
            source_donnee=d["source_donnee"],
            date_collecte=datetime.fromisoformat(d["date_collecte"]),
            empreinte_sha256=d.get("empreinte_sha256"),
            debut_s=d.get("debut_s"),
            fin_s=d.get("fin_s"),
        )


# ---------------------------------------------------------------------------
# Nœud
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Noeud:
    """Une pièce du dossier (compte, vidéo, société, phrase...) et son rôle."""

    type: TypeNoeud
    libelle: str
    role: Role = Role.INCONNU
    attributs: dict[str, Any] = field(default_factory=dict)
    solidite: Solidite | None = None
    id: str = field(default_factory=_nouvel_id)
    date_ajout: datetime = field(default_factory=maintenant)
    version: int = 1

    def __post_init__(self) -> None:
        _texte_requis(self.libelle, "le libellé du nœud")
        _texte_requis(self.id, "l'identifiant du nœud")
        object.__setattr__(self, "type", TypeNoeud(self.type))
        object.__setattr__(self, "role", Role(self.role))

    def vers_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "libelle": self.libelle,
            "role": self.role.value,
            "attributs": self.attributs,
            "solidite": self.solidite.vers_dict() if self.solidite else None,
            "date_ajout": self.date_ajout.isoformat(),
            "version": self.version,
        }

    @classmethod
    def depuis_dict(cls, d: dict[str, Any]) -> "Noeud":
        return cls(
            id=d["id"],
            type=TypeNoeud(d["type"]),
            libelle=d["libelle"],
            role=Role(d["role"]),
            attributs=d["attributs"],
            solidite=Solidite.depuis_dict(d["solidite"]) if d["solidite"] else None,
            date_ajout=datetime.fromisoformat(d["date_ajout"]),
            version=d["version"],
        )


# ---------------------------------------------------------------------------
# Lien
# ---------------------------------------------------------------------------

class LienSansPreuve(ValueError):
    """Un lien sans preuve n'existe pas."""


@dataclass(frozen=True)
class ExplicationEcartee:
    """Une explication concurrente envisagée, et ce qui l'écarte."""

    explication: str  # ex. « tendance virale »
    raison: str  # la preuve ou l'observation qui l'écarte

    def __post_init__(self) -> None:
        _texte_requis(self.explication, "l'explication écartée")
        _texte_requis(self.raison, "la raison de l'écart")


@dataclass(frozen=True)
class Lien:
    """Une ficelle entre deux nœuds, qui porte sa preuve et son explication."""

    type_lien: str  # ex. « publie », « reprend », « dit_phrase », « co_agit_avec »
    famille: FamilleLien
    source_id: str
    cible_id: str
    preuves: tuple[Preuve, ...]
    explication: str  # une phrase lisible par un journaliste
    methode: str
    agent: str
    statut: StatutLien = StatutLien.PROPOSE
    oriente: bool = False  # vrai si le lien a un sens (qui vient avant, qui paie)
    force_points: float = 0.0  # somme des poids, un point = indice 10 fois plus attendu
    contre_points: float = 0.0  # poids des indices contraires (≥ 0)
    familles_independantes: int = 1
    methodes_independantes: int = 1
    identifiant_officiel: bool = False
    explications_ecartees: tuple[ExplicationEcartee, ...] = ()
    solidite: Solidite | None = None
    id: str = field(default_factory=_nouvel_id)
    date: datetime = field(default_factory=maintenant)
    version: int = 1

    def __post_init__(self) -> None:
        preuves = tuple(self.preuves)
        if not preuves:
            raise LienSansPreuve(
                "un lien sans preuve n'existe pas : au moins une preuve est exigée"
            )
        if not all(isinstance(p, Preuve) for p in preuves):
            raise ValueError("les preuves doivent être des objets Preuve")
        object.__setattr__(self, "preuves", preuves)
        object.__setattr__(self, "explications_ecartees", tuple(self.explications_ecartees))
        object.__setattr__(self, "famille", FamilleLien(self.famille))
        object.__setattr__(self, "statut", StatutLien(self.statut))
        _texte_requis(self.type_lien, "le type du lien")
        _texte_requis(self.source_id, "le nœud source")
        _texte_requis(self.cible_id, "le nœud cible")
        _texte_requis(self.explication, "l'explication du lien")
        _texte_requis(self.methode, "la méthode")
        _texte_requis(self.agent, "l'agent auteur")
        if self.source_id == self.cible_id:
            raise ValueError("un lien relie deux nœuds différents")
        if self.contre_points < 0:
            raise ValueError("contre_points est un poids positif (0 si aucun contre-indice)")
        if self.familles_independantes < 1 or self.methodes_independantes < 1:
            raise ValueError("au moins une famille et une méthode")

    @property
    def echelle(self) -> EchelleVerbale:
        return echelle_verbale(
            self.force_points,
            self.familles_independantes,
            self.methodes_independantes,
            self.identifiant_officiel,
        )

    @property
    def conteste(self) -> bool:
        """Un contre-indice d'au moins 1 point affiche « contesté », quel que soit le total."""
        return self.contre_points >= 1.0

    def vers_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type_lien": self.type_lien,
            "famille": self.famille.value,
            "source_id": self.source_id,
            "cible_id": self.cible_id,
            "oriente": self.oriente,
            "preuves": [p.vers_dict() for p in self.preuves],
            "explication": self.explication,
            "explications_ecartees": [
                {"explication": e.explication, "raison": e.raison}
                for e in self.explications_ecartees
            ],
            "methode": self.methode,
            "agent": self.agent,
            "statut": self.statut.value,
            "force_points": self.force_points,
            "contre_points": self.contre_points,
            "familles_independantes": self.familles_independantes,
            "methodes_independantes": self.methodes_independantes,
            "identifiant_officiel": self.identifiant_officiel,
            "echelle": self.echelle.value,
            "conteste": self.conteste,
            "solidite": self.solidite.vers_dict() if self.solidite else None,
            "date": self.date.isoformat(),
            "version": self.version,
        }

    @classmethod
    def depuis_dict(cls, d: dict[str, Any]) -> "Lien":
        return cls(
            id=d["id"],
            type_lien=d["type_lien"],
            famille=FamilleLien(d["famille"]),
            source_id=d["source_id"],
            cible_id=d["cible_id"],
            oriente=d["oriente"],
            preuves=tuple(Preuve.depuis_dict(p) for p in d["preuves"]),
            explication=d["explication"],
            explications_ecartees=tuple(
                ExplicationEcartee(e["explication"], e["raison"])
                for e in d["explications_ecartees"]
            ),
            methode=d["methode"],
            agent=d["agent"],
            statut=StatutLien(d["statut"]),
            force_points=d["force_points"],
            contre_points=d["contre_points"],
            familles_independantes=d["familles_independantes"],
            methodes_independantes=d["methodes_independantes"],
            identifiant_officiel=d["identifiant_officiel"],
            solidite=Solidite.depuis_dict(d["solidite"]) if d["solidite"] else None,
            date=datetime.fromisoformat(d["date"]),
            version=d["version"],
        )


# ---------------------------------------------------------------------------
# Question ouverte
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QuestionOuverte:
    """Une zone d'ombre : ce qu'on ne sait pas encore, et la pièce qui l'a comblée."""

    texte: str
    statut: StatutQuestion = StatutQuestion.OUVERTE
    comblee_par: str | None = None  # identifiant du nœud qui répond à la question
    id: str = field(default_factory=_nouvel_id)
    version: int = 1

    def __post_init__(self) -> None:
        _texte_requis(self.texte, "le texte de la question")
        object.__setattr__(self, "statut", StatutQuestion(self.statut))
        if self.statut is StatutQuestion.COMBLEE and not self.comblee_par:
            raise ValueError("une question comblée doit nommer la pièce qui la comble")
        if self.statut is StatutQuestion.OUVERTE and self.comblee_par:
            raise ValueError("une question ouverte n'a pas de pièce qui la comble")

    def vers_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "texte": self.texte,
            "statut": self.statut.value,
            "comblee_par": self.comblee_par,
            "version": self.version,
        }

    @classmethod
    def depuis_dict(cls, d: dict[str, Any]) -> "QuestionOuverte":
        return cls(
            id=d["id"],
            texte=d["texte"],
            statut=StatutQuestion(d["statut"]),
            comblee_par=d["comblee_par"],
            version=d["version"],
        )


# ---------------------------------------------------------------------------
# Le réseau et son stockage en ajout seul
# ---------------------------------------------------------------------------

class NoeudInconnu(KeyError):
    """Le nœud demandé n'existe pas dans ce réseau."""


@dataclass(frozen=True)
class Evenement:
    """Une ligne du journal : qui a été écrit, quand, dans quelle version."""

    seq: int
    date: datetime
    genre: str  # « affaire », « noeud », « lien » ou « question »
    objet_id: str
    version: int
    motif: str
    donnees: dict[str, Any]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evenements (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT    NOT NULL,
    genre    TEXT    NOT NULL,
    objet_id TEXT    NOT NULL,
    version  INTEGER NOT NULL,
    motif    TEXT    NOT NULL DEFAULT '',
    donnees  TEXT    NOT NULL,
    UNIQUE (genre, objet_id, version)
);
CREATE TRIGGER IF NOT EXISTS evenements_pas_de_modification
BEFORE UPDATE ON evenements
BEGIN SELECT RAISE(ABORT, 'journal en ajout seul : on ne modifie pas, on ajoute une version'); END;
CREATE TRIGGER IF NOT EXISTS evenements_pas_d_effacement
BEFORE DELETE ON evenements
BEGIN SELECT RAISE(ABORT, 'journal en ajout seul : on n''efface jamais'); END;
"""

_CHAMPS_CORRIGEABLES_NOEUD = {"type", "libelle", "role", "attributs", "solidite"}
_CHAMPS_CORRIGEABLES_LIEN = {
    "type_lien", "famille", "preuves", "explication", "explications_ecartees",
    "methode", "agent", "statut", "oriente", "force_points", "contre_points",
    "familles_independantes", "methodes_independantes", "identifiant_officiel",
    "solidite",
}

_GENRE_AFFAIRE = "affaire"
_ID_QUESTION_CENTRALE = "question_centrale"


class ReseauEnquete:
    """Le mur d'enquête : une question centrale, des pièces, des liens prouvés.

    Une seule table `evenements`, en ajout seul, fait foi. L'état courant d'un
    objet est sa dernière version ; les versions précédentes restent lisibles
    par `historique`, `versions_noeud` et `versions_lien`.
    """

    def __init__(self, question_centrale: str, chemin: str = ":memory:") -> None:
        _texte_requis(question_centrale, "la question centrale")
        self._bd = sqlite3.connect(chemin)
        self._bd.executescript(_SCHEMA)
        if self.question_centrale != question_centrale.strip():
            self._ajouter(_GENRE_AFFAIRE, _ID_QUESTION_CENTRALE,
                          {"question": question_centrale.strip()},
                          motif="question posée" if not self._existe(_GENRE_AFFAIRE, _ID_QUESTION_CENTRALE)
                          else "question reformulée")

    # -- cycle de vie -------------------------------------------------------

    def fermer(self) -> None:
        self._bd.close()

    def __enter__(self) -> "ReseauEnquete":
        return self

    def __exit__(self, *_: object) -> None:
        self.fermer()

    # -- journal (bas niveau) ----------------------------------------------

    def _existe(self, genre: str, objet_id: str) -> bool:
        ligne = self._bd.execute(
            "SELECT 1 FROM evenements WHERE genre = ? AND objet_id = ? LIMIT 1",
            (genre, objet_id),
        ).fetchone()
        return ligne is not None

    def _prochaine_version(self, genre: str, objet_id: str) -> int:
        (dernier,) = self._bd.execute(
            "SELECT COALESCE(MAX(version), 0) FROM evenements WHERE genre = ? AND objet_id = ?",
            (genre, objet_id),
        ).fetchone()
        return dernier + 1

    def _ajouter(self, genre: str, objet_id: str, donnees: dict[str, Any], motif: str = "") -> int:
        """Ajoute une ligne au journal (jamais de modification) ; renvoie sa version."""
        version = self._prochaine_version(genre, objet_id)
        donnees = {**donnees, "version": version} if "version" in donnees else donnees
        with self._bd:
            self._bd.execute(
                "INSERT INTO evenements (date, genre, objet_id, version, motif, donnees)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (maintenant().isoformat(), genre, objet_id, version, motif,
                 json.dumps(donnees, ensure_ascii=False)),
            )
        return version

    def _courants(self, genre: str) -> list[dict[str, Any]]:
        """Dernière version de chaque objet d'un genre, dans l'ordre de première apparition."""
        lignes = self._bd.execute(
            """
            SELECT e.donnees FROM evenements e
            WHERE e.genre = ?
              AND e.version = (SELECT MAX(version) FROM evenements
                               WHERE genre = e.genre AND objet_id = e.objet_id)
            ORDER BY (SELECT MIN(seq) FROM evenements
                      WHERE genre = e.genre AND objet_id = e.objet_id)
            """,
            (genre,),
        ).fetchall()
        return [json.loads(d) for (d,) in lignes]

    def _courant(self, genre: str, objet_id: str) -> dict[str, Any] | None:
        ligne = self._bd.execute(
            "SELECT donnees FROM evenements WHERE genre = ? AND objet_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (genre, objet_id),
        ).fetchone()
        return json.loads(ligne[0]) if ligne else None

    # -- historique ---------------------------------------------------------

    def historique(self, objet_id: str | None = None, genre: str | None = None) -> list[Evenement]:
        """Relit le journal dans l'ordre : tout, ou les versions d'un seul objet."""
        requete = "SELECT seq, date, genre, objet_id, version, motif, donnees FROM evenements"
        conditions, params = [], []
        if objet_id is not None:
            conditions.append("objet_id = ?")
            params.append(objet_id)
        if genre is not None:
            conditions.append("genre = ?")
            params.append(genre)
        if conditions:
            requete += " WHERE " + " AND ".join(conditions)
        requete += " ORDER BY seq"
        return [
            Evenement(seq, datetime.fromisoformat(date), g, oid, version, motif, json.loads(d))
            for seq, date, g, oid, version, motif, d in self._bd.execute(requete, params)
        ]

    def versions_noeud(self, noeud_id: str) -> list[Noeud]:
        return [Noeud.depuis_dict(e.donnees) for e in self.historique(noeud_id, "noeud")]

    def versions_lien(self, lien_id: str) -> list[Lien]:
        return [Lien.depuis_dict(e.donnees) for e in self.historique(lien_id, "lien")]

    # -- question centrale --------------------------------------------------

    @property
    def question_centrale(self) -> str:
        courant = self._courant(_GENRE_AFFAIRE, _ID_QUESTION_CENTRALE)
        return courant["question"] if courant else ""

    def reformuler_question(self, question: str, motif: str = "") -> None:
        """Change la question centrale ; l'ancienne formulation reste dans le journal."""
        _texte_requis(question, "la question centrale")
        self._ajouter(_GENRE_AFFAIRE, _ID_QUESTION_CENTRALE,
                      {"question": question.strip()}, motif or "question reformulée")

    # -- nœuds --------------------------------------------------------------

    def ajouter_noeud(self, noeud: Noeud) -> Noeud:
        if self._existe("noeud", noeud.id):
            raise ValueError(f"le nœud {noeud.id} existe déjà : utiliser corriger_noeud")
        noeud = replace(noeud, version=1)
        self._ajouter("noeud", noeud.id, noeud.vers_dict(), motif="ajout")
        return noeud

    def corriger_noeud(self, noeud_id: str, motif: str, **changements: Any) -> Noeud:
        """Ajoute une nouvelle version du nœud ; l'ancienne reste lisible."""
        inconnus = set(changements) - _CHAMPS_CORRIGEABLES_NOEUD
        if inconnus:
            raise ValueError(f"champs non corrigeables : {sorted(inconnus)}")
        _texte_requis(motif, "le motif de la correction")
        nouveau = replace(self.noeud(noeud_id), **changements)
        nouveau = replace(nouveau, version=self._prochaine_version("noeud", noeud_id))
        self._ajouter("noeud", noeud_id, nouveau.vers_dict(), motif=motif)
        return nouveau

    def noeud(self, noeud_id: str) -> Noeud:
        courant = self._courant("noeud", noeud_id)
        if courant is None:
            raise NoeudInconnu(noeud_id)
        return Noeud.depuis_dict(courant)

    def noeuds(self) -> list[Noeud]:
        return [Noeud.depuis_dict(d) for d in self._courants("noeud")]

    # -- liens --------------------------------------------------------------

    def ajouter_lien(self, lien: Lien) -> Lien:
        """Ajoute un lien. La preuve est exigée dès la construction du `Lien` ;
        ici on vérifie en plus que ses deux extrémités existent."""
        if self._existe("lien", lien.id):
            raise ValueError(f"le lien {lien.id} existe déjà : utiliser corriger_lien")
        for extremite in (lien.source_id, lien.cible_id):
            if not self._existe("noeud", extremite):
                raise NoeudInconnu(extremite)
        lien = replace(lien, version=1)
        self._ajouter("lien", lien.id, lien.vers_dict(), motif="ajout")
        return lien

    def corriger_lien(self, lien_id: str, motif: str, **changements: Any) -> Lien:
        """Ajoute une nouvelle version du lien (statut, force, explication...).
        Un lien ne peut jamais perdre toutes ses preuves."""
        inconnus = set(changements) - _CHAMPS_CORRIGEABLES_LIEN
        if inconnus:
            raise ValueError(f"champs non corrigeables : {sorted(inconnus)}")
        _texte_requis(motif, "le motif de la correction")
        nouveau = replace(self.lien(lien_id), **changements)
        nouveau = replace(nouveau, version=self._prochaine_version("lien", lien_id))
        self._ajouter("lien", lien_id, nouveau.vers_dict(), motif=motif)
        return nouveau

    def lien(self, lien_id: str) -> Lien:
        courant = self._courant("lien", lien_id)
        if courant is None:
            raise KeyError(lien_id)
        return Lien.depuis_dict(courant)

    def liens(self) -> list[Lien]:
        return [Lien.depuis_dict(d) for d in self._courants("lien")]

    # -- questions ouvertes -------------------------------------------------

    def poser_question(self, texte: str) -> QuestionOuverte:
        question = QuestionOuverte(texte=texte)
        self._ajouter("question", question.id, question.vers_dict(), motif="question posée")
        return question

    def combler_question(self, question_id: str, noeud_id: str) -> QuestionOuverte:
        """Marque la question comme comblée par une pièce du réseau."""
        courant = self._courant("question", question_id)
        if courant is None:
            raise KeyError(question_id)
        if not self._existe("noeud", noeud_id):
            raise NoeudInconnu(noeud_id)
        nouvelle = replace(
            QuestionOuverte.depuis_dict(courant),
            statut=StatutQuestion.COMBLEE,
            comblee_par=noeud_id,
            version=self._prochaine_version("question", question_id),
        )
        self._ajouter("question", question_id, nouvelle.vers_dict(),
                      motif=f"comblée par {noeud_id}")
        return nouvelle

    def questions(self, statut: StatutQuestion | None = None) -> list[QuestionOuverte]:
        toutes = [QuestionOuverte.depuis_dict(d) for d in self._courants("question")]
        return [q for q in toutes if statut is None or q.statut is statut]

    def questions_ouvertes(self) -> list[QuestionOuverte]:
        """Les zones d'ombre encore à combler."""
        return self.questions(StatutQuestion.OUVERTE)

    # -- export pour l'interface -------------------------------------------

    def exporter_pour_interface(self) -> dict[str, Any]:
        """JSON hiérarchique : question → dossiers (rôles) → pièces → preuves.

        Chaque niveau porte ses compteurs, calculés sur ce qu'il contient (ils ne
        peuvent donc pas se contredire). Les liens et les questions ouvertes
        suivent à la racine. Le résultat passe tel quel dans `json.dumps`.
        """
        noeuds = self.noeuds()
        liens = self.liens()
        liens_par_noeud: dict[str, list[Lien]] = {n.id: [] for n in noeuds}
        for lien in liens:
            for extremite in (lien.source_id, lien.cible_id):
                liens_par_noeud[extremite].append(lien)

        dossiers = []
        preuves_distinctes: set[Preuve] = set()
        for role in Role:
            pieces = []
            preuves_du_dossier: set[Preuve] = set()
            liens_du_dossier: set[str] = set()
            par_type: dict[str, int] = {}
            for noeud in (n for n in noeuds if n.role is role):
                piece, preuves_piece = self._piece(noeud, liens_par_noeud[noeud.id])
                pieces.append(piece)
                preuves_du_dossier |= preuves_piece
                liens_du_dossier |= {l.id for l in liens_par_noeud[noeud.id]}
                par_type[noeud.type.value] = par_type.get(noeud.type.value, 0) + 1
            preuves_distinctes |= preuves_du_dossier
            dossiers.append({
                "role": role.value,
                "nom": LIBELLES_ROLES[role],
                "compteurs": {
                    "pieces": len(pieces),
                    "preuves": len(preuves_du_dossier),
                    "liens": len(liens_du_dossier),
                    "pieces_par_type": par_type,
                },
                "pieces": pieces,
            })

        ouvertes = self.questions_ouvertes()
        etiquettes = {n.id: n.libelle for n in noeuds}
        return {
            "question": self.question_centrale,
            "compteurs": {
                "dossiers": len(dossiers),
                "pieces": len(noeuds),
                "liens": len(liens),
                "preuves": len(preuves_distinctes),
                "zones_d_ombre": len(ouvertes),
            },
            "dossiers": dossiers,
            "liens": [self._lien_pour_interface(l, etiquettes) for l in liens],
            "questions_ouvertes": [
                {**q.vers_dict(), "comblee_par_libelle": etiquettes.get(q.comblee_par or "")}
                for q in self.questions()
            ],
        }

    @staticmethod
    def _piece(noeud: Noeud, liens: Iterable[Lien]) -> tuple[dict[str, Any], set[Preuve]]:
        """Une pièce et les preuves des liens qui la touchent (chacune cite ses liens)."""
        citees: dict[Preuve, list[str]] = {}
        for lien in liens:
            for preuve in lien.preuves:
                citees.setdefault(preuve, []).append(lien.id)
        preuves = [{**p.vers_dict(), "liens": ids} for p, ids in citees.items()]
        piece = {
            "id": noeud.id,
            "type": noeud.type.value,
            "libelle": noeud.libelle,
            "role": noeud.role.value,
            "attributs": noeud.attributs,
            "solidite": noeud.solidite.vers_dict() if noeud.solidite else None,
            "version": noeud.version,
            "compteurs": {"preuves": len(preuves), "liens": len({i for ids in citees.values() for i in ids})},
            "preuves": preuves,
        }
        return piece, set(citees)

    @staticmethod
    def _lien_pour_interface(lien: Lien, etiquettes: dict[str, str]) -> dict[str, Any]:
        return {
            **lien.vers_dict(),
            "source_libelle": etiquettes[lien.source_id],
            "cible_libelle": etiquettes[lien.cible_id],
        }
