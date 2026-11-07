"""Banc d'essai : mesure la fiabilité de l'outil contre le corrigé d'une affaire déjà résolue.

Principe : une affaire jugée par la presse fournit un *corrigé* (les faits que l'outil
devait retrouver, ce qui reste « non établi », les pièges à éviter). L'outil produit un
*rendu* (ses faits, chacun avec sa preuve). `evaluer` compare les deux et chiffre :

- le rappel pondéré : part (pondérée) des faits attendus retrouvés ;
- la précision : part des faits produits qui correspondent à un fait attendu ;
- les faits inventés : sans preuve, ou contraires au corrigé (piège, « non établi » affirmé) ;
- les pièges tombés et le respect du « non établi ».

Durcissement (angles morts G03, G04, G05) : un identifiant `fait_vise` ne vaut que si le texte
du fait le confirme ; chaque preuve est rapprochée du journal de collecte (ce que l'outil a
vraiment lu) ; une phrase canari et la liste des URL de presse du corrigé détectent une fuite ;
chaque fait porte une portée (vidéo, web, hors portée) ; la réserve est scellée hors du dépôt.

Le format des fichiers JSON est décrit dans `banc/README.md`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

# Types de faits reconnus dans un corrigé.
TYPES_FAIT = frozenset(
    {"origine", "reprise", "acteur", "role", "lien", "chronologie", "interlocuteur", "zone_ombre"}
)

# Seuil de rapprochement de texte par défaut : part des mots du critère retrouvés.
SEUIL_DEFAUT = 0.6

# Un fait attendu n'est reconnu qu'avec un seuil d'au moins 0,6 : en dessous, le critère est trop lâche.
SEUIL_MIN_FAIT = 0.6

# Portée d'un fait : ce que l'outil peut aller chercher. « Visible » = vidéo ou web.
PORTEES = ("video", "web", "hors_portee")
PORTEES_VISIBLES = frozenset({"video", "web"})


def _journal_reserve_par_defaut() -> Path:
    """Journal de réserve HORS du dépôt (le dossier de travail est lisible par les agents)."""
    env = os.environ.get("HACK_ORIANE_RESERVE")
    return Path(env) if env else Path.home() / ".hack-oriane" / "banc-reserve.jsonl"


JOURNAL_RESERVE = _journal_reserve_par_defaut()
# Ancien emplacement (dans le dépôt) : encore lu pour refuser une réserve déjà jouée.
JOURNAL_RESERVE_ANCIEN = Path("donnees") / "banc-reserve.jsonl"

# Mots trop courants pour compter dans un rapprochement de texte.
_MOTS_VIDES = frozenset(
    "le la les un une des de du d l et ou a au aux en dans par pour sur sous avec sans "
    "est sont etait ete ce cet cette ces se sa son ses leur leurs qui que quoi dont "
    "il elle ils elles on ne pas plus ni y".split()
)


class ErreurBanc(Exception):
    """Fichier de corrigé ou de rendu mal formé."""


class ReserveDejaJouee(ErreurBanc):
    """Une affaire de réserve ne peut être évaluée qu'une seule fois."""


class JournalAltere(ErreurBanc):
    """Le journal de réserve ne se laisse pas lire dans l'ordre : on refuse plutôt que de deviner."""


class FuiteCorrige(ErreurBanc):
    """Le rendu contient la phrase canari du corrigé : l'outil a eu accès à la réponse."""


# --------------------------------------------------------------------------- modèles


@dataclass(frozen=True)
class Critere:
    """Façon de reconnaître une affirmation : mots à retrouver et seuil de couverture."""

    mots: frozenset[str]
    seuil: float = SEUIL_DEFAUT


@dataclass(frozen=True)
class FaitAttendu:
    id: str
    phrase: str
    type: str
    poids: float
    critere: Critere
    portee: str | None = None  # video | web | hors_portee
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Interdit:
    """Affirmation à ne pas produire (piège) ou à laisser « non établie »."""

    id: str
    phrase: str
    critere: Critere


@dataclass(frozen=True)
class Corrige:
    id: str
    question: str
    gel_donnees: str
    reserve: bool
    faits: tuple[FaitAttendu, ...]
    non_etabli: tuple[Interdit, ...]
    pieges: tuple[Interdit, ...]
    canari: str = ""  # phrase absurde qui ne doit apparaître dans aucun rendu
    urls_presse: frozenset[str] = frozenset()  # URL (normalisées) citées par le corrigé : liste noire
    empreinte: str = ""  # sha256 du corrigé tel que lu


@dataclass(frozen=True)
class FaitRendu:
    id: str
    texte: str
    preuves: tuple[dict, ...]
    fait_vise: str | None
    non_etabli: bool  # l'outil déclare lui-même ce point comme « non établi »

    @property
    def a_preuve(self) -> bool:
        """Une preuve ne vaut que avec une URL et un extrait."""
        return any(
            str(p.get("url", "")).strip() and str(p.get("extrait", "")).strip()
            for p in self.preuves
        )


@dataclass(frozen=True)
class Rendu:
    affaire_id: str
    faits: tuple[FaitRendu, ...]


@dataclass
class Rapport:
    affaire_id: str
    nb_attendus: int
    nb_retrouves: int
    nb_produits: int
    rappel_pondere: float
    precision: float
    inventes: list[dict] = field(default_factory=list)  # {"id", "motif"}
    pieges_tombes: list[str] = field(default_factory=list)
    nb_pieges: int = 0
    non_etabli_respecte: bool = True
    non_etabli_violes: list[str] = field(default_factory=list)
    manquants: list[str] = field(default_factory=list)
    hors_corrige: list[str] = field(default_factory=list)
    a_relire: list[dict] = field(default_factory=list)  # {"rendu", "attendu", "score"}
    par_type: dict[str, dict] = field(default_factory=dict)
    rappel_visible: float | None = None  # rappel sur les faits de portée vidéo ou web
    par_portee: dict[str, dict] = field(default_factory=dict)
    vise_refuses: list[dict] = field(default_factory=list)  # fait_vise démenti par le texte
    preuves_refusees: list[dict] = field(default_factory=list)  # {"fait", "motifs"}
    preuves_rapprochees: bool = False  # preuves comparées à un journal de collecte
    reserve: bool = False

    def en_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------- normalisation


def normaliser(texte: str) -> frozenset[str]:
    """Mots d'un texte : minuscules, sans accents, sans mots vides ni mots d'une lettre.

    Les nombres sont conservés (« 17 », « 800 »), ils portent souvent le fait.
    """
    decompose = unicodedata.normalize("NFKD", texte.lower())
    sans_accent = "".join(c for c in decompose if not unicodedata.combining(c))
    mots = re.findall(r"[a-z0-9]+", sans_accent)
    return frozenset(m for m in mots if m not in _MOTS_VIDES and (len(m) > 1 or m.isdigit()))


def couverture(critere: Critere, texte: str) -> float:
    """Part des mots du critère présents dans le texte (0 à 1)."""
    if not critere.mots:
        return 0.0
    return len(critere.mots & normaliser(texte)) / len(critere.mots)


def normaliser_url(url: str) -> str:
    """Forme comparable d'une URL : sans schéma, sans « www. », sans fragment ni barre finale."""
    brut = str(url).strip()
    u = urlsplit(brut if "://" in brut else "//" + brut)
    hote = (u.hostname or "").lower().removeprefix("www.")
    requete = u.query if "watch" in u.path and "v=" in u.query else ""
    return hote + u.path.rstrip("/") + ("?" + requete if requete else "")


def _suite_mots(texte: str) -> tuple[str, ...]:
    """Tous les mots d'un texte dans l'ordre (minuscules, sans accents, mots vides conservés)."""
    decompose = unicodedata.normalize("NFKD", str(texte).lower())
    return tuple(re.findall(r"[a-z0-9]+", "".join(c for c in decompose if not unicodedata.combining(c))))


def contient_canari(canari: str, texte: str) -> bool:
    """Vrai si la phrase canari figure dans le texte, mot pour mot ou à 80 % de ses mots."""
    mots = _suite_mots(canari)
    if not mots:
        return False
    if " ".join(mots) in " ".join(_suite_mots(texte)):
        return True
    presents = set(_suite_mots(texte))
    return sum(m in presents for m in set(mots)) / len(set(mots)) >= 0.8


class JournalCollecte:
    """Ce que l'outil a réellement lu pendant le passage : des textes rangés par URL.

    Une preuve n'est acceptée que si son URL figure ici ET si son extrait se retrouve, mot pour
    mot (hors accents et casse), dans un texte lu à cette URL.
    """

    def __init__(self, entrees: Iterable[tuple[str, str]] = ()) -> None:
        self._textes: dict[str, str] = {}
        for url, texte in entrees:
            self.ajouter(url, texte)

    def ajouter(self, url: str, texte: str) -> None:
        cle = normaliser_url(url)
        self._textes[cle] = self._textes.get(cle, "") + " # " + " ".join(_suite_mots(texte)) + " "

    def __len__(self) -> int:
        return len(self._textes)

    def verifier(self, url: str, extrait: str) -> str | None:
        """None si la preuve est retrouvée dans la collecte, sinon le motif du refus."""
        texte = self._textes.get(normaliser_url(url))
        if texte is None:
            return f"URL absente du journal de collecte ({url})"
        mots = " ".join(_suite_mots(extrait))
        if not mots or f" {mots} " not in texte:
            return f"extrait absent du texte collecté à {url}"
        return None


def charger_journal_collecte(chemin: Path) -> JournalCollecte:
    """Lit un journal de collecte : liste JSON (ou lignes JSON) d'objets {"url", "texte"}."""
    try:
        brut = Path(chemin).read_text(encoding="utf-8")
    except OSError as e:
        raise ErreurBanc(f"{chemin} : lecture impossible ({e})") from e
    try:
        donnees = json.loads(brut)
    except json.JSONDecodeError:
        try:
            donnees = [json.loads(l) for l in brut.splitlines() if l.strip()]
        except json.JSONDecodeError as e:
            raise ErreurBanc(f"{chemin} : journal de collecte illisible ({e})") from e
    if isinstance(donnees, dict):
        donnees = donnees.get("entrees", [])
    journal = JournalCollecte()
    for i, e in enumerate(donnees):
        _exiger(isinstance(e, dict) and bool(e.get("url")) and "texte" in e,
                f"journal de collecte, entrée n°{i + 1} : url et texte requis")
        journal.ajouter(e["url"], e["texte"] if isinstance(e["texte"], str) else " ".join(map(str, e["texte"])))
    return journal


def detecter_fuite(corrige: Corrige, rendu: Rendu) -> list[str]:
    """Identifiants des faits du rendu qui contiennent la phrase canari du corrigé."""
    if not corrige.canari:
        return []
    return [
        f.id for f in rendu.faits
        if contient_canari(corrige.canari, f.texte + " " + " ".join(str(p.get("extrait", "")) for p in f.preuves))
    ]


# -------------------------------------------------------------------------- chargement


def _exiger(condition: bool, message: str) -> None:
    if not condition:
        raise ErreurBanc(message)


def _critere(brut: dict, phrase: str, contexte: str) -> Critere:
    """Lit le critère de correspondance ; à défaut, les mots de la phrase servent de critère."""
    bloc = brut.get("correspondance") or {}
    mots = normaliser(" ".join(bloc["mots_cles"])) if bloc.get("mots_cles") else normaliser(phrase)
    seuil = float(bloc.get("seuil", SEUIL_DEFAUT))
    _exiger(bool(mots), f"{contexte} : aucun mot exploitable pour la correspondance")
    _exiger(0 < seuil <= 1, f"{contexte} : le seuil doit être entre 0 (exclu) et 1")
    return Critere(mots, seuil)


def _interdits(brut: list, nature: str) -> tuple[Interdit, ...]:
    sortie = []
    for i, el in enumerate(brut):
        contexte = f"{nature} n°{i + 1}"
        _exiger(isinstance(el, dict) and bool(el.get("id")) and bool(el.get("phrase")),
                f"{contexte} : id et phrase requis")
        sortie.append(Interdit(str(el["id"]), str(el["phrase"]), _critere(el, el["phrase"], contexte)))
    return tuple(sortie)


def _urls_du_corrige(brut) -> set[str]:
    """Toutes les URL citées n'importe où dans le corrigé (sources, notes, recherche)."""
    if isinstance(brut, str):
        return set(re.findall(r"https?://[^\s\"'<>)\]]+", brut))
    if isinstance(brut, dict):
        return set().union(*[_urls_du_corrige(v) for v in brut.values()]) if brut else set()
    if isinstance(brut, list):
        return set().union(*[_urls_du_corrige(v) for v in brut]) if brut else set()
    return set()


def corrige_depuis_dict(brut: dict) -> Corrige:
    """Valide et convertit un corrigé lu depuis du JSON."""
    for cle in ("id", "question", "gel_donnees", "faits"):
        _exiger(bool(brut.get(cle)), f"corrigé : champ « {cle} » manquant")
    faits = []
    for i, f in enumerate(brut["faits"]):
        contexte = f"fait attendu n°{i + 1}"
        _exiger(isinstance(f, dict) and bool(f.get("id")) and bool(f.get("phrase")),
                f"{contexte} : id et phrase requis")
        _exiger(f.get("type") in TYPES_FAIT,
                f"{contexte} : type inconnu « {f.get('type')} » (attendus : {sorted(TYPES_FAIT)})")
        poids = float(f.get("poids", 1))
        _exiger(poids > 0, f"{contexte} : le poids doit être positif")
        critere = _critere(f, f["phrase"], contexte)
        _exiger(critere.seuil >= SEUIL_MIN_FAIT,
                f"{contexte} : seuil {critere.seuil} trop lâche (minimum {SEUIL_MIN_FAIT})")
        portee = f.get("portee")
        _exiger(portee is None or portee in PORTEES,
                f"{contexte} : portée inconnue « {portee} » (attendues : {list(PORTEES)})")
        sources = tuple(x for x in [f.get("source"), *f.get("autres_sources", [])] if x)
        faits.append(FaitAttendu(str(f["id"]), f["phrase"], f["type"], poids, critere, portee, sources))
    ids = [f.id for f in faits]
    _exiger(len(ids) == len(set(ids)), "corrigé : identifiants de faits en double")
    return Corrige(
        id=str(brut["id"]),
        question=brut["question"],
        gel_donnees=str(brut["gel_donnees"]),
        reserve=bool(brut.get("reserve", False)),
        canari=str(brut.get("canari", "")),
        urls_presse=frozenset(normaliser_url(u) for u in _urls_du_corrige(brut)),
        empreinte=hashlib.sha256(json.dumps({k: v for k, v in brut.items() if k != "id"},  # sans l'id : renommer ne change rien
                                               sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
        faits=tuple(faits),
        non_etabli=_interdits(brut.get("non_etabli", []), "non établi"),
        pieges=_interdits(brut.get("pieges", []), "piège"),
    )


def rendu_depuis_dict(brut: dict) -> Rendu:
    """Valide et convertit un rendu de l'outil lu depuis du JSON."""
    _exiger(bool(brut.get("affaire_id")), "rendu : champ « affaire_id » manquant")
    faits = []
    for i, f in enumerate(brut.get("faits", [])):
        _exiger(isinstance(f, dict) and bool(f.get("texte")), f"fait produit n°{i + 1} : texte requis")
        faits.append(
            FaitRendu(
                id=str(f.get("id") or f"p{i + 1}"),
                texte=f["texte"],
                preuves=tuple(p for p in f.get("preuves", []) if isinstance(p, dict)),
                fait_vise=f.get("fait_vise"),
                non_etabli=f.get("statut") == "non_etabli",
            )
        )
    return Rendu(str(brut["affaire_id"]), tuple(faits))


def _lire_json(chemin: Path) -> dict:
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ErreurBanc(f"{chemin} : lecture impossible ({e})") from e


def charger_corrige(chemin: Path) -> Corrige:
    return corrige_depuis_dict(_lire_json(chemin))


def charger_rendu(chemin: Path) -> Rendu:
    return rendu_depuis_dict(_lire_json(chemin))


# ------------------------------------------------------------------------- évaluation


def _meilleur_fait(fait: FaitRendu, candidats: tuple[FaitAttendu, ...]) -> tuple[FaitAttendu | None, float]:
    """Fait attendu le plus proche par le texte, à condition qu'il passe son seuil."""
    meilleur, score_max = None, 0.0
    for c in candidats:
        score = couverture(c.critere, fait.texte)
        if score >= c.critere.seuil and score > score_max:
            meilleur, score_max = c, score
    return meilleur, score_max


def _touche(interdits: tuple[Interdit, ...], fait: FaitRendu) -> list[Interdit]:
    return [x for x in interdits if couverture(x.critere, fait.texte) >= x.critere.seuil]


def _empreinte_ligne(ligne: str) -> str:
    return hashlib.sha256(ligne.encode("utf-8")).hexdigest()


def _lire_journal(journal: Path) -> list[dict]:
    """Lit le journal de réserve en vérifiant la chaîne d'empreintes : toute altération = refus."""
    if not journal.exists():
        return []
    evenements, precedent = [], "0" * 64
    for i, ligne in enumerate(l for l in journal.read_text(encoding="utf-8").splitlines() if l.strip()):
        try:
            e = json.loads(ligne)
        except json.JSONDecodeError as exc:
            raise JournalAltere(f"{journal} : ligne {i + 1} illisible ({exc})") from exc
        if e.get("precedent") != precedent:
            raise JournalAltere(f"{journal} : la chaîne d'empreintes est rompue à la ligne {i + 1}")
        precedent = _empreinte_ligne(ligne)
        evenements.append(e)
    return evenements


def _ajouter_au_journal(journal: Path, evenement: dict) -> None:
    lignes = [l for l in journal.read_text(encoding="utf-8").splitlines() if l.strip()] if journal.exists() else []
    _lire_journal(journal)  # refuse d'écrire à la suite d'un journal altéré
    evenement = {**evenement, "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "precedent": _empreinte_ligne(lignes[-1]) if lignes else "0" * 64}
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as f:
        f.write(json.dumps(evenement, ensure_ascii=False, sort_keys=True) + "\n")


def _ids_ancien_journal(ancien: Path) -> list[dict]:
    if not ancien.exists():
        return []
    try:
        return [json.loads(l) for l in ancien.read_text(encoding="utf-8").splitlines() if l.strip()]
    except json.JSONDecodeError as e:
        raise JournalAltere(f"{ancien} : illisible ({e})") from e


def sceller_reserve(corrige: Corrige, journal_reserve: Path | None = None) -> None:
    """Scelle l'empreinte du corrigé de réserve avant tout travail des agents.

    Si le corrigé change ensuite, l'évaluation est refusée : on ne règle pas la réserve.
    """
    journal = Path(journal_reserve) if journal_reserve else JOURNAL_RESERVE
    if not corrige.reserve:
        raise ErreurBanc(f"« {corrige.id} » n'est pas une affaire de réserve.")
    _verifier_reserve(corrige, [journal])
    if not any(e.get("evenement") == "scelle" and e.get("affaire_id") == corrige.id for e in _lire_journal(journal)):
        _ajouter_au_journal(journal, {"evenement": "scelle", "affaire_id": corrige.id,
                                      "empreinte_corrige": corrige.empreinte})


def _verifier_reserve(corrige: Corrige, journaux: list[Path]) -> None:
    """Refuse une affaire de réserve déjà jouée (même identifiant ou même empreinte) ou modifiée."""
    evenements = [e for j in journaux for e in (_lire_journal(j) if j != JOURNAL_RESERVE_ANCIEN
                                                  else _ids_ancien_journal(j))]
    for e in evenements:
        meme = e.get("affaire_id") == corrige.id or (corrige.empreinte and e.get("empreinte_corrige") == corrige.empreinte)
        if meme and e.get("evenement", "joue") in ("joue", "fuite"):
            raise ReserveDejaJouee(
                f"L'affaire de réserve « {corrige.id} » a déjà été évaluée (voir {journaux[0]}) : "
                "elle ne se joue qu'une fois, sinon le chiffre ne prouve plus rien."
            )
        if e.get("evenement") == "scelle" and e.get("affaire_id") == corrige.id \
                and e.get("empreinte_corrige") != corrige.empreinte:
            raise ErreurBanc(f"Le corrigé de réserve « {corrige.id} » a changé depuis son scellement : refus.")


def _noter_reserve(corrige: Corrige, rendu: Rendu, rapport: Rapport | None, journal: Path,
                   evenement: str = "joue") -> None:
    trace = {
        "evenement": evenement,
        "affaire_id": corrige.id,
        "empreinte_corrige": corrige.empreinte,
        "empreinte_rendu": hashlib.sha256(
            json.dumps(asdict(rendu), sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest(),
    }
    if rapport is not None:
        trace.update(rappel_pondere=rapport.rappel_pondere, precision=rapport.precision)
    _ajouter_au_journal(journal, trace)


def _juger_preuves(fait: FaitRendu, corrige: Corrige, collecte: JournalCollecte | None) -> tuple[bool, list[str]]:
    """Une preuve vaut si elle est complète, ne cite pas un article du corrigé et, quand on a un
    journal de collecte, si l'outil a bien lu cet extrait à cette URL."""
    refus, valides = [], 0
    for p in fait.preuves:
        url, extrait = str(p.get("url", "")).strip(), str(p.get("extrait", "")).strip()
        if not url or not extrait:
            refus.append("preuve incomplète (URL et extrait requis)")
        elif normaliser_url(url) in corrige.urls_presse:
            refus.append(f"{url} est un article de presse du corrigé")
        elif collecte is not None and (motif := collecte.verifier(url, extrait)):
            refus.append(motif)
        else:
            valides += 1
    return valides > 0, refus


def _ratio(num: float, den: float) -> float:
    """Rapport arrondi ; 1.0 quand il n'y avait rien à retrouver ou à produire."""
    return round(num / den, 4) if den else 1.0


def evaluer(corrige: Corrige, rendu: Rendu, journal_reserve: Path | None = None,
            journal_collecte: JournalCollecte | None = None) -> Rapport:
    """Compare un rendu au corrigé et renvoie le rapport de fiabilité.

    Correspondance d'un fait produit avec un fait attendu :
    1. identifiant explicite (`fait_vise`), à condition que le texte du fait passe le seuil du
       fait visé (sinon l'identifiant est refusé et listé dans `vise_refuses`) ;
    2. sinon rapprochement de texte : part des mots du critère retrouvés dans le fait
       produit (mots normalisés), au-dessus du seuil du fait attendu. Ces correspondances
       sont listées dans `a_relire` : un humain doit les confirmer.

    Un fait est « inventé » s'il n'a aucune preuve (URL et extrait), s'il affirme un piège,
    ou s'il établit ce que le corrigé laisse « non établi ». Un fait inventé ne compte
    jamais dans le rappel. Un fait que l'outil marque lui-même « non établi » n'est ni
    juste ni faux : c'est un aveu d'ignorance, il n'entre pas dans la précision.

    Preuves : si `journal_collecte` est donné, une preuve n'est retenue que si l'outil a lu cet
    extrait à cette URL ; sinon le rapport signale que les preuves ne sont pas rapprochées.
    Une preuve qui cite un article du corrigé (liste noire) est toujours refusée. Un rendu qui
    contient la phrase canari du corrigé est refusé (`FuiteCorrige`).

    Une affaire de réserve n'est évaluée qu'une fois (trace scellée dans `journal_reserve`, hors
    dépôt) et exige un journal de collecte.
    """
    journal = Path(journal_reserve) if journal_reserve else JOURNAL_RESERVE
    if rendu.affaire_id != corrige.id:
        raise ErreurBanc(f"Le rendu porte sur « {rendu.affaire_id} », le corrigé sur « {corrige.id} ».")
    if corrige.reserve:
        if journal_collecte is None:
            raise ErreurBanc(f"Réserve « {corrige.id} » : un journal de collecte est obligatoire "
                             "(sans lui les preuves ne se vérifient pas ; rien n'a été consommé).")
        _verifier_reserve(corrige, [journal] + ([JOURNAL_RESERVE_ANCIEN] if journal_reserve is None else []))
    fuites = detecter_fuite(corrige, rendu)
    if fuites:
        if corrige.reserve:
            _noter_reserve(corrige, rendu, None, journal, evenement="fuite")
        raise FuiteCorrige(f"La phrase canari de « {corrige.id} » figure dans le rendu (faits {', '.join(fuites)}) : "
                           "l'outil a eu accès au corrigé, le rendu est refusé.")

    par_id = {f.id: f for f in corrige.faits}
    retrouves: dict[str, FaitRendu] = {}
    inventes: list[dict] = []
    hors_corrige: list[str] = []
    a_relire: list[dict] = []
    pieges_tombes: set[str] = set()
    non_etabli_violes: set[str] = set()
    nb_justes = 0
    nb_comptes = 0  # faits produits qui affirment quelque chose
    vise_refuses: list[dict] = []
    preuves_refusees: list[dict] = []

    for fait in rendu.faits:
        if fait.non_etabli:
            continue  # l'outil avoue ne pas savoir : jamais une affirmation, donc ni piège ni invention
        motifs = []
        touches = _touche(corrige.pieges, fait)
        pieges_tombes.update(x.id for x in touches)
        if touches:
            motifs.append("contraire au corrigé (piège : " + ", ".join(x.id for x in touches) + ")")
        violes = _touche(corrige.non_etabli, fait)
        non_etabli_violes.update(x.id for x in violes)
        if violes:
            motifs.append("affirme un point non établi (" + ", ".join(x.id for x in violes) + ")")
        preuve_ok, refus = _juger_preuves(fait, corrige, journal_collecte)
        if refus:
            preuves_refusees.append({"fait": fait.id, "motifs": refus})
        if not preuve_ok:
            if all(r.startswith("preuve incomplète") for r in refus):
                motifs.append("sans preuve (URL et extrait requis)")
            else:
                motifs.append("preuve refusée (" + " ; ".join(refus) + ")")
        nb_comptes += 1
        if motifs:
            inventes.append({"id": fait.id, "motif": " ; ".join(motifs)})
            continue

        cible, score, explicite = None, 0.0, False
        if fait.fait_vise in par_id:
            vise = par_id[fait.fait_vise]
            couvert = couverture(vise.critere, fait.texte)
            if couvert >= vise.critere.seuil:
                cible, score, explicite = vise, 1.0, True
            else:  # l'identifiant seul ne suffit pas : le texte doit dire le fait
                vise_refuses.append({"rendu": fait.id, "attendu": vise.id, "score": round(couvert, 2)})
        if cible is None:
            cible, score = _meilleur_fait(fait, corrige.faits)
        if cible is None:
            hors_corrige.append(fait.id)
            continue
        nb_justes += 1
        retrouves.setdefault(cible.id, fait)
        if not explicite:
            a_relire.append({"rendu": fait.id, "attendu": cible.id, "score": round(score, 2)})

    par_type: dict[str, dict] = {}
    for f in corrige.faits:
        t = par_type.setdefault(f.type, {"attendus": 0, "retrouves": 0, "poids": 0.0, "poids_retrouve": 0.0})
        t["attendus"] += 1
        t["poids"] += f.poids
        if f.id in retrouves:
            t["retrouves"] += 1
            t["poids_retrouve"] += f.poids
    for t in par_type.values():
        t["rappel_pondere"] = _ratio(t.pop("poids_retrouve"), t.pop("poids"))

    par_portee: dict[str, dict] = {}
    for f in corrige.faits:
        t = par_portee.setdefault(f.portee or "non_classe",
                                  {"attendus": 0, "retrouves": 0, "poids": 0.0, "poids_retrouve": 0.0})
        t["attendus"] += 1
        t["poids"] += f.poids
        if f.id in retrouves:
            t["retrouves"] += 1
            t["poids_retrouve"] += f.poids
    for t in par_portee.values():
        t["rappel_pondere"] = _ratio(t.pop("poids_retrouve"), t.pop("poids"))
    visibles = [f for f in corrige.faits if f.portee in PORTEES_VISIBLES]
    rappel_visible = (_ratio(sum(f.poids for f in visibles if f.id in retrouves), sum(f.poids for f in visibles))
                      if visibles else None)

    poids_total = sum(f.poids for f in corrige.faits)
    poids_retrouve = sum(f.poids for f in corrige.faits if f.id in retrouves)
    rapport = Rapport(
        affaire_id=corrige.id,
        nb_attendus=len(corrige.faits),
        nb_retrouves=len(retrouves),
        nb_produits=nb_comptes,
        rappel_pondere=_ratio(poids_retrouve, poids_total),
        precision=_ratio(nb_justes, nb_comptes),
        inventes=inventes,
        pieges_tombes=sorted(pieges_tombes),
        nb_pieges=len(corrige.pieges),
        non_etabli_respecte=not non_etabli_violes,
        non_etabli_violes=sorted(non_etabli_violes),
        manquants=[f.id for f in corrige.faits if f.id not in retrouves],
        hors_corrige=hors_corrige,
        a_relire=a_relire,
        par_type=par_type,
        rappel_visible=rappel_visible,
        par_portee=par_portee,
        vise_refuses=vise_refuses,
        preuves_refusees=preuves_refusees,
        preuves_rapprochees=journal_collecte is not None,
        reserve=corrige.reserve,
    )
    if corrige.reserve:
        _noter_reserve(corrige, rendu, rapport, journal)
    return rapport


# ---------------------------------------------------------------------------- lecture


def _pluriel(n: int, mot: str) -> str:
    return f"{n} {mot}{'s' if n > 1 else ''}"


def rapport_texte(rapport: Rapport) -> str:
    """Dix lignes lisibles par un journaliste."""
    r = rapport
    if r.inventes:
        inventes = _pluriel(len(r.inventes), "fait") + " inventé" + ("s" if len(r.inventes) > 1 else "")
    else:
        inventes = "aucun fait inventé"
    evites = r.nb_pieges - len(r.pieges_tombes)
    if r.nb_pieges == 0:
        pieges = "pas de piège défini"
    else:
        pieges = f"{_pluriel(evites, 'piège')} évité{'s' if evites > 1 else ''} sur {r.nb_pieges}"
    types = ", ".join(f"{t} {v['retrouves']}/{v['attendus']}" for t, v in sorted(r.par_type.items()))
    if r.a_relire:
        relire = f"À relire par un humain : {_pluriel(len(r.a_relire), 'rapprochement')} de texte, sans identifiant explicite."
    else:
        relire = "À relire : aucun rapprochement de texte, tout repose sur des identifiants explicites."
    visible = ("" if r.rappel_visible is None else
               f" Sur le visible (vidéo et web) : {r.rappel_visible:.0%} ; sur le total, avec ce qui est hors de portée : {r.rappel_pondere:.0%}.")
    garantie = ("" if r.preuves_rapprochees else
                " Preuves non rapprochées d'un journal de collecte : ce chiffre ne fait pas foi.")
    if r.vise_refuses:
        relire += f" {_pluriel(len(r.vise_refuses), 'identifiant')} refusé{'s' if len(r.vise_refuses) > 1 else ''} faute de texte."
    lignes = [
        f"Affaire « {r.affaire_id} »" + (" (réserve : jouée une seule fois)." if r.reserve else "."),
        f"Sur cette affaire : {r.nb_retrouves} faits sur {r.nb_attendus} retrouvés, {inventes}, {pieges}.",
        f"Rappel pondéré : {r.rappel_pondere:.0%} des faits attendus, comptés selon leur importance." + visible,
        f"Précision : {r.precision:.0%} des {r.nb_produits} faits affirmés correspondent à un fait attendu.",
        "Faits inventés : "
        + ("aucun." if not r.inventes else "; ".join(f"{x['id']} ({x['motif']})" for x in r.inventes) + "."),
        "Pièges : " + ("aucun n'est tombé." if not r.pieges_tombes else "tombé dans " + ", ".join(r.pieges_tombes) + "."),
        "Ce qui reste « non établi » : "
        + ("respecté." if r.non_etabli_respecte else "VIOLÉ pour " + ", ".join(r.non_etabli_violes) + "."),
        f"Par type de fait : {types}.",
        "Faits manquants : " + (", ".join(r.manquants) if r.manquants else "aucun") + ".",
        relire + " Chiffre valable pour cette affaire, pas une fiabilité générale." + garantie,
    ]
    return "\n".join(lignes)


# ------------------------------------------------------------------ contrôle et garde-fou


def charger_corriges(dossier: Path) -> list[Corrige]:
    """Tous les corrigés d'un dossier (`banc/affaires`), triés par nom de fichier."""
    return [charger_corrige(p) for p in sorted(Path(dossier).glob("*.json"))]


def controler_corrige(corrige: Corrige) -> list[str]:
    """Défauts d'un corrigé : fait sans portée, sans source, seuil lâche, canari absent."""
    defauts = []
    for f in corrige.faits:
        if f.portee is None:
            defauts.append(f"{corrige.id} {f.id} : sans portée")
        if not f.sources:
            defauts.append(f"{corrige.id} {f.id} : sans source")
        if f.critere.seuil < SEUIL_MIN_FAIT:
            defauts.append(f"{corrige.id} {f.id} : seuil {f.critere.seuil} < {SEUIL_MIN_FAIT}")
    if not corrige.canari:
        defauts.append(f"{corrige.id} : sans phrase canari")
    return defauts


def export_garde_fou(corriges: Iterable[Corrige]) -> dict:
    """Ce que le garde-fou doit bloquer : phrases canari (à ne jamais voir dans une trace d'agent)
    et URL de presse du corrigé (à ne jamais ouvrir)."""
    return {
        "version": 1,
        "affaires": {
            c.id: {"canari": c.canari, "urls_presse": sorted(c.urls_presse), "empreinte_corrige": c.empreinte}
            for c in corriges
        },
    }


def ecrire_garde_fou(corriges: Iterable[Corrige], sortie: Path) -> Path:
    sortie = Path(sortie)
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(json.dumps(export_garde_fou(corriges), ensure_ascii=False, indent=2), encoding="utf-8")
    return sortie


# ------------------------------------------------------------------------ rendus truqués

MODES_TRUQUES = ("identifiants", "recopie", "presse", "canari")
_FAUSSE_PREUVE = {"url": "https://exemple.invalid/inventee", "extrait": "extrait inventé de toutes pièces"}


def rendu_truque(corrige: Corrige, mode: str) -> Rendu:
    """Rendu de triche pour tester le banc ; chaque mode est une façon de gonfler le score.

    - `identifiants` : un fait par fait attendu, `fait_vise` rempli, texte creux, fausse preuve ;
    - `recopie` : les phrases du corrigé recopiées telles quelles, avec de fausses preuves ;
    - `presse` : les phrases recopiées, preuves = articles de presse du corrigé ;
    - `canari` : un fait qui reprend la phrase canari.
    """
    if mode == "identifiants":
        faits = [FaitRendu(f"t-{f.id}", "Synthèse : ce point est établi.", (_FAUSSE_PREUVE,), f.id, False)
                 for f in corrige.faits]
    elif mode == "recopie":
        faits = [FaitRendu(f"t-{f.id}", f.phrase, (_FAUSSE_PREUVE,), None, False) for f in corrige.faits]
    elif mode == "presse":
        faits = [FaitRendu(f"t-{f.id}", f.phrase,
                           tuple({"url": u, "extrait": f.phrase} for u in (f.sources or ("https://exemple.invalid/",))),
                           None, False) for f in corrige.faits]
    elif mode == "canari":
        faits = [FaitRendu("t-canari", corrige.canari or "pas de canari", (_FAUSSE_PREUVE,), None, False)]
    else:
        raise ErreurBanc(f"mode de triche inconnu « {mode} » (attendus : {list(MODES_TRUQUES)})")
    return Rendu(corrige.id, tuple(faits))


def mesurer_truquages(corriges: Iterable[Corrige], avec_journal: bool = True) -> dict[str, dict[str, float]]:
    """Rappel pondéré obtenu par chaque rendu truqué, par affaire.

    `avec_journal` : le rendu est jugé contre le journal de collecte (vide : l'outil n'a rien lu de
    tel). Sans journal, la recopie avec fausses preuves passe : c'est pourquoi un score sans
    journal « ne fait pas foi ».
    """
    resultat: dict[str, dict[str, float]] = {}
    for c in corriges:
        # une affaire de réserve se mesure sur une copie non réservée : on ne touche pas au journal
        c_test = replace(c, reserve=False) if c.reserve else c
        ligne = {}
        for mode in MODES_TRUQUES:
            try:
                ligne[mode] = evaluer(c_test, rendu_truque(c_test, mode),
                                      journal_collecte=JournalCollecte() if avec_journal else None).rappel_pondere
            except FuiteCorrige:
                ligne[mode] = 0.0
        resultat[c.id] = ligne
    return resultat


def _tableau_truquages(corriges: list[Corrige]) -> str:
    lignes = []
    for titre, avec in (("avec journal de collecte", True), ("sans journal de collecte", False)):
        lignes.append(f"Rappel d'un rendu truqué, {titre} :")
        for affaire, modes in mesurer_truquages(corriges, avec).items():
            lignes.append(f"  {affaire} : " + ", ".join(f"{m} {v:.0%}" for m, v in modes.items()))
    return "\n".join(lignes)


def main(argv: list[str] | None = None) -> int:
    """Usage :
    python -m enquete.banc corrige.json rendu.json [journal-collecte.json]
    python -m enquete.banc --controle corrige.json...        (portée, source, seuil, canari)
    python -m enquete.banc --truquages corrige.json...       (rappel de quatre rendus de triche)
    python -m enquete.banc --garde-fou sortie.json corrige.json...
    """
    args = sys.argv[1:] if argv is None else argv
    try:
        if args and args[0] == "--controle" and len(args) > 1:
            corriges = [charger_corrige(Path(a)) for a in args[1:]]
            defauts = [d for c in corriges for d in controler_corrige(c)]
            nb_faits = sum(len(c.faits) for c in corriges)
            print(f"{nb_faits} faits dans {len(corriges)} corrigés ; {len(defauts)} défaut(s).")
            print("\n".join(defauts))
            return 1 if defauts else 0
        if args and args[0] == "--truquages" and len(args) > 1:
            print(_tableau_truquages([charger_corrige(Path(a)) for a in args[1:]]))
            return 0
        if args and args[0] == "--garde-fou" and len(args) > 2:
            sortie = ecrire_garde_fou([charger_corrige(Path(a)) for a in args[2:]], Path(args[1]))
            print(f"Garde-fou écrit : {sortie}")
            return 0
        if len(args) not in (2, 3) or args[0].startswith("--"):
            print(main.__doc__)
            return 2
        collecte = charger_journal_collecte(Path(args[2])) if len(args) == 3 else None
        print(rapport_texte(evaluer(charger_corrige(Path(args[0])), charger_rendu(Path(args[1])),
                                    journal_collecte=collecte)))
    except ErreurBanc as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
