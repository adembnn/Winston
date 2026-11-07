"""Banc d'essai : mesure la fiabilité de l'outil contre le corrigé d'une affaire déjà résolue.

Principe : une affaire jugée par la presse fournit un *corrigé* (les faits que l'outil
devait retrouver, ce qui reste « non établi », les pièges à éviter). L'outil produit un
*rendu* (ses faits, chacun avec sa preuve). `evaluer` compare les deux et chiffre :

- le rappel pondéré : part (pondérée) des faits attendus retrouvés ;
- la précision : part des faits produits qui correspondent à un fait attendu ;
- les faits inventés : sans preuve, ou contraires au corrigé (piège, « non établi » affirmé) ;
- les pièges tombés et le respect du « non établi ».

Le format des fichiers JSON est décrit dans `banc/README.md`.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Types de faits reconnus dans un corrigé.
TYPES_FAIT = frozenset(
    {"origine", "reprise", "acteur", "role", "lien", "chronologie", "interlocuteur", "zone_ombre"}
)

# Seuil de rapprochement de texte par défaut : part des mots du critère retrouvés.
SEUIL_DEFAUT = 0.6

# Journal des affaires de réserve déjà jouées (dossier ignoré par git).
JOURNAL_RESERVE = Path("donnees") / "banc-reserve.jsonl"

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
        faits.append(FaitAttendu(str(f["id"]), f["phrase"], f["type"], poids,
                                 _critere(f, f["phrase"], contexte)))
    ids = [f.id for f in faits]
    _exiger(len(ids) == len(set(ids)), "corrigé : identifiants de faits en double")
    return Corrige(
        id=str(brut["id"]),
        question=brut["question"],
        gel_donnees=str(brut["gel_donnees"]),
        reserve=bool(brut.get("reserve", False)),
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


def _verifier_reserve(corrige: Corrige, journal: Path) -> None:
    """Refuse une affaire de réserve déjà jouée."""
    if not journal.exists():
        return
    for ligne in journal.read_text(encoding="utf-8").splitlines():
        if ligne.strip() and json.loads(ligne).get("affaire_id") == corrige.id:
            raise ReserveDejaJouee(
                f"L'affaire de réserve « {corrige.id} » a déjà été évaluée (voir {journal}) : "
                "elle ne se joue qu'une fois, sinon le chiffre ne prouve plus rien."
            )


def _noter_reserve(rapport: Rapport, journal: Path) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    trace = {
        "affaire_id": rapport.affaire_id,
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rappel_pondere": rapport.rappel_pondere,
        "precision": rapport.precision,
    }
    with journal.open("a", encoding="utf-8") as f:
        f.write(json.dumps(trace, ensure_ascii=False) + "\n")


def _ratio(num: float, den: float) -> float:
    """Rapport arrondi ; 1.0 quand il n'y avait rien à retrouver ou à produire."""
    return round(num / den, 4) if den else 1.0


def evaluer(corrige: Corrige, rendu: Rendu, journal_reserve: Path | None = None) -> Rapport:
    """Compare un rendu au corrigé et renvoie le rapport de fiabilité.

    Correspondance d'un fait produit avec un fait attendu :
    1. identifiant explicite (`fait_vise`) ;
    2. sinon rapprochement de texte : part des mots du critère retrouvés dans le fait
       produit (mots normalisés), au-dessus du seuil du fait attendu. Ces correspondances
       sont listées dans `a_relire` : un humain doit les confirmer.

    Un fait est « inventé » s'il n'a aucune preuve (URL et extrait), s'il affirme un piège,
    ou s'il établit ce que le corrigé laisse « non établi ». Un fait inventé ne compte
    jamais dans le rappel. Un fait que l'outil marque lui-même « non établi » n'est ni
    juste ni faux : c'est un aveu d'ignorance, il n'entre pas dans la précision.

    Une affaire de réserve n'est évaluée qu'une fois (trace dans `journal_reserve`).
    """
    journal = Path(journal_reserve) if journal_reserve else JOURNAL_RESERVE
    if rendu.affaire_id != corrige.id:
        raise ErreurBanc(f"Le rendu porte sur « {rendu.affaire_id} », le corrigé sur « {corrige.id} ».")
    if corrige.reserve:
        _verifier_reserve(corrige, journal)

    par_id = {f.id: f for f in corrige.faits}
    retrouves: dict[str, FaitRendu] = {}
    inventes: list[dict] = []
    hors_corrige: list[str] = []
    a_relire: list[dict] = []
    pieges_tombes: set[str] = set()
    non_etabli_violes: set[str] = set()
    nb_justes = 0
    nb_comptes = 0  # faits produits qui affirment quelque chose

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
        if not fait.a_preuve:
            motifs.append("sans preuve (URL et extrait requis)")
        nb_comptes += 1
        if motifs:
            inventes.append({"id": fait.id, "motif": " ; ".join(motifs)})
            continue

        explicite = fait.fait_vise in par_id
        if explicite:
            cible, score = par_id[fait.fait_vise], 1.0
        else:
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
        reserve=corrige.reserve,
    )
    if corrige.reserve:
        _noter_reserve(rapport, journal)
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
    lignes = [
        f"Affaire « {r.affaire_id} »" + (" (réserve : jouée une seule fois)." if r.reserve else "."),
        f"Sur cette affaire : {r.nb_retrouves} faits sur {r.nb_attendus} retrouvés, {inventes}, {pieges}.",
        f"Rappel pondéré : {r.rappel_pondere:.0%} des faits attendus, comptés selon leur importance.",
        f"Précision : {r.precision:.0%} des {r.nb_produits} faits affirmés correspondent à un fait attendu.",
        "Faits inventés : "
        + ("aucun." if not r.inventes else "; ".join(f"{x['id']} ({x['motif']})" for x in r.inventes) + "."),
        "Pièges : " + ("aucun n'est tombé." if not r.pieges_tombes else "tombé dans " + ", ".join(r.pieges_tombes) + "."),
        "Ce qui reste « non établi » : "
        + ("respecté." if r.non_etabli_respecte else "VIOLÉ pour " + ", ".join(r.non_etabli_violes) + "."),
        f"Par type de fait : {types}.",
        "Faits manquants : " + (", ".join(r.manquants) if r.manquants else "aucun") + ".",
        relire + " Chiffre valable pour cette affaire, pas une fiabilité générale.",
    ]
    return "\n".join(lignes)


def main(argv: list[str] | None = None) -> int:
    """Usage : python -m enquete.banc corrige.json rendu.json"""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(main.__doc__)
        return 2
    try:
        print(rapport_texte(evaluer(charger_corrige(Path(args[0])), charger_rendu(Path(args[1])))))
    except ErreurBanc as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
