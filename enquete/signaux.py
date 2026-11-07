"""Signaux faibles : quinze détecteurs et le faisceau d'indices.

Un signal faible est une PISTE À VÉRIFIER : jamais une preuve, jamais une accusation. Chaque
signal porte l'hypothèse (au conditionnel), l'explication innocente aussi précise que
l'hypothèse, la question ouverte, 2 ou 3 pistes, et les pièces que le juge de preuve peut
rouvrir. Les garde-fous sont tenus par le code (`SignalFaible.__post_init__`) : titre de
12 mots au plus, mots refusés, plafond de force par type, une preuve par pièce et par compte.

Spécification : kickoff/signaux-faibles.md. Tous les seuils marqués « notre choix » dans la
spécification sont des hypothèses à calibrer sur le banc ; tant qu'un type n'est pas dans
`CALIBRES`, les détecteurs de comportement (D5 à D9) et D12 plafonnent à « moyenne ».

Règles du module :
- fonctions pures : aucun appel réseau dans un détecteur, aucune lecture de l'horloge ; les
  appels du lot 2 sont faits avant, par `collecter_pour_signaux` (via `Budget` et le cache) ;
- unité de comptage = le COMPTE (`plateforme:pseudo`), pas la vidéo ;
- « première apparition trouvée », jamais « première » : l'index d'Oriane couvre 3 mois, ignore
  les comptes de moins de 5 000 abonnés et a jusqu'à une semaine de retard ;
- on décrit des gestes observés, jamais une intention ni une identité.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import random
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from enquete import pivots as _pv
from enquete.reseau import FamilleLien, Preuve, Solidite
from enquete.temps import date_depuis_identifiant

URL_ORIANE = "https://connect.oriane.xyz"
AGENT = "signaux.py"

# ---------------------------------------------------------------------------
# Contrat commun
# ---------------------------------------------------------------------------

FORCES = ("faible", "moyenne", "forte")
_RANG = {f: i for i, f in enumerate(FORCES)}

#: Type de signal -> famille de lien du réseau.
FAMILLE: dict[str, FamilleLien] = {
    "formulation_partagee": FamilleLien.CONTENU_DIT,
    "gabarit_commun": FamilleLien.CONTENU_MONTRE,
    "son_lance_par_peu": FamilleLien.CONTENU_DIT,
    "sequence_deja_vue": FamilleLien.CONTENU_MONTRE,
    "comptes_sans_passe": FamilleLien.IDENTITE,
    "commentateurs_recents": FamilleLien.IDENTITE,
    "cadence_hors_norme": FamilleLien.IDENTITE,
    "compte_reveille": FamilleLien.IDENTITE,
    "profils_jumeaux": FamilleLien.IDENTITE,
    "publications_rapprochees": FamilleLien.TEMPS,
    "sujet_qui_monte": FamilleLien.TEMPS,
    "domaine_recent": FamilleLien.INFRASTRUCTURE,
    "identifiant_de_suivi_partage": FamilleLien.INFRASTRUCTURE,
    "societe_recente": FamilleLien.ARGENT,
    "code_promo_en_serie": FamilleLien.ARGENT,
    "portefeuille_neuf": FamilleLien.ARGENT,
    "faisceau": FamilleLien.RECIT,
}

#: Type de signal -> famille d'indices du faisceau (deux détecteurs d'une même famille comptent une fois).
GROUPE_INDICES: dict[str, str] = {
    "formulation_partagee": "contenu", "gabarit_commun": "contenu", "son_lance_par_peu": "contenu",
    "sequence_deja_vue": "contenu",
    "comptes_sans_passe": "compte", "commentateurs_recents": "compte", "cadence_hors_norme": "compte",
    "compte_reveille": "compte", "profils_jumeaux": "compte",
    "publications_rapprochees": "temps", "sujet_qui_monte": "temps",
    "domaine_recent": "infrastructure", "identifiant_de_suivi_partage": "infrastructure",
    "societe_recente": "argent", "code_promo_en_serie": "argent", "portefeuille_neuf": "argent",
}

#: Types dont les seuils ont été calibrés sur le banc (9 signaux sur 10 au moins confirmés). Vide aujourd'hui.
CALIBRES: frozenset[str] = frozenset()

_TYPES_PLAFONNES = frozenset({
    "comptes_sans_passe", "cadence_hors_norme", "compte_reveille", "profils_jumeaux",
    "publications_rapprochees", "identifiant_de_suivi_partage",
})


def plafond(type_: str) -> str:
    """Force maximale d'un type : « moyenne » tant qu'il n'est pas calibré, « faible » pour les commentateurs."""
    if type_ == "commentateurs_recents":
        return "faible"
    if type_ in _TYPES_PLAFONNES and type_ not in CALIBRES:
        return "moyenne"
    return "forte"


def _norm(texte: str) -> str:
    """Texte sans accents, majuscules ni ponctuation (même règle que `chaine.normaliser_phrase`)."""
    decompose = unicodedata.normalize("NFKD", str(texte).lower())
    sans_accent = "".join(c for c in decompose if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", sans_accent))


_REFUS = re.compile(
    r"\b(bots?|faux comptes?|coordonn(?:e|ee|es|ees)|coordination|operations?|propagande|desinformation|"
    r"manipulations?|ingerences?|complots?|coupables?|fraudes?|fraudeurs?|arnaques?|escroqueries?|escrocs?|"
    r"trolls?|prouve que)\b"
)


def mots_refuses(texte: str) -> list[str]:
    """Mots interdits dans un texte de signal (accusation, intention, étiquette non établie)."""
    return sorted({m.group(0) for m in _REFUS.finditer(_norm(texte))})


def _nb_mots(titre: str) -> int:
    return len(re.findall(r"\w+(?:['’\-]\w+)*", titre))


@dataclass(frozen=True)
class PreuveSignal:
    """Une pièce citée, que le juge de preuve rouvre."""

    piece: str
    url: str
    extrait: str
    seconde: float | None = None
    compte: str | None = None
    source: str = "Oriane"
    lu_le: datetime | None = None

    def vers_dict(self) -> dict[str, Any]:
        return {"piece": self.piece, "url": self.url, "extrait": self.extrait, "seconde": self.seconde,
                "compte": self.compte, "source": self.source,
                "lu_le": self.lu_le.isoformat() if self.lu_le else None}


@dataclass(frozen=True)
class SignalFaible:
    id: str
    type: str
    titre: str
    ce_que_ca_peut_vouloir_dire: str
    explication_innocente: str
    force: str
    premiere_apparition: datetime | None
    pieces: tuple[str, ...]
    comptes: tuple[str, ...]
    preuves: tuple[PreuveSignal, ...]
    question_ouverte: str
    pistes: tuple[str, ...]
    mesures: tuple[tuple[str, Any], ...] = ()  # chiffres recomptés, pour l'écran et le moment fort

    def __post_init__(self) -> None:
        if self.type not in FAMILLE:
            raise ValueError(f"type de signal inconnu : {self.type!r}")
        if not self.titre.strip() or _nb_mots(self.titre) > 12:
            raise ValueError(f"le titre tient en 12 mots au plus ({_nb_mots(self.titre)} : {self.titre!r})")
        for nom, texte in (("titre", self.titre), ("hypothèse", self.ce_que_ca_peut_vouloir_dire),
                           ("explication innocente", self.explication_innocente),
                           ("question", self.question_ouverte), ("pistes", " ".join(self.pistes))):
            trouves = mots_refuses(texte)
            if trouves:
                raise ValueError(f"mot refusé dans {nom} : {', '.join(trouves)}")
        if not self.ce_que_ca_peut_vouloir_dire.strip():
            raise ValueError("l'hypothèse est obligatoire")
        if not self.explication_innocente.strip():
            raise ValueError("l'explication innocente est obligatoire")
        if self.force not in _RANG:
            raise ValueError(f"force inconnue : {self.force!r}")
        if _RANG[self.force] > _RANG[plafond(self.type)]:
            raise ValueError(f"le type {self.type} plafonne à « {plafond(self.type)} » (reçu « {self.force} »)")
        if not 2 <= len(self.pistes) <= 3:
            raise ValueError("un signal propose 2 ou 3 pistes")
        if not self.question_ouverte.strip().endswith("?"):
            raise ValueError("la question ouverte finit par « ? »")
        if not self.pieces and not self.comptes:
            raise ValueError("un signal cite au moins une pièce ou un compte")
        cites_p = {p.piece for p in self.preuves}
        cites_c = {p.compte for p in self.preuves if p.compte}
        for piece in self.pieces:
            if piece not in cites_p:
                raise ValueError(f"pièce sans preuve : {piece}")
        for compte in self.comptes:
            if compte not in cites_c:
                raise ValueError(f"compte sans preuve : {compte}")

    def mesure(self, nom: str, defaut: Any = None) -> Any:
        return dict(self.mesures).get(nom, defaut)

    @property
    def famille_indices(self) -> str | None:
        return GROUPE_INDICES.get(self.type)


def _id_signal(type_: str, pieces: Iterable[str]) -> str:
    return "sf-" + hashlib.sha1((type_ + "|" + "|".join(sorted(set(pieces)))).encode()).hexdigest()[:10]


def _cle_tri(s: SignalFaible) -> tuple:
    return (-_RANG[s.force], s.premiere_apparition or datetime.max.replace(tzinfo=UTC), s.id)


def _trier(signaux: Iterable[SignalFaible]) -> list[SignalFaible]:
    return sorted(signaux, key=_cle_tri)


def _signal(type_: str, titre: str, hypothese: str, innocent: str, force: str, premiere: datetime | None,
            preuves: Sequence[PreuveSignal], question: str, pistes: Sequence[str],
            mesures: Sequence[tuple[str, Any]] = (), pieces: Sequence[str] | None = None,
            comptes: Sequence[str] | None = None) -> SignalFaible:
    """Fabrique un signal : pièces et comptes sont lus dans les preuves (donc toujours couverts)."""
    ps = tuple(dict.fromkeys(pieces if pieces is not None else (p.piece for p in preuves)))
    cs = tuple(dict.fromkeys(comptes if comptes is not None else (p.compte for p in preuves if p.compte)))
    return SignalFaible(
        id=_id_signal(type_, ps or cs), type=type_, titre=titre, ce_que_ca_peut_vouloir_dire=hypothese,
        explication_innocente=innocent, force=force, premiere_apparition=premiere, pieces=ps, comptes=cs,
        preuves=tuple(preuves), question_ouverte=question, pistes=tuple(pistes), mesures=tuple(mesures))


# ---------------------------------------------------------------------------
# Lecture des résultats Oriane
# ---------------------------------------------------------------------------

def _lire_iso(valeur: Any) -> datetime | None:
    if isinstance(valeur, datetime):
        return valeur if valeur.tzinfo else valeur.replace(tzinfo=UTC)
    if not isinstance(valeur, str) or not valeur.strip():
        return None
    try:
        d = datetime.fromisoformat(valeur.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)


def _vid(v: dict) -> str:
    return str(v.get("id") or f"{v.get('platform')}:{v.get('platformId')}")


def _compte(v: dict) -> str | None:
    return _pv._cle_compte(str(v.get("platform") or "").lower() or None, v.get("profileHandle"))


def _url(v: dict, seconde: float | None = None) -> str:
    return _pv._url_contenu(v, seconde) or URL_ORIANE


@lru_cache(maxsize=None)
def _date_cachee(pf: str, pid: str, typ: str, pub: str) -> tuple[datetime | None, float, str]:
    publie = _lire_iso(pub) if pub else None
    if pid and pf in ("tiktok", "instagram"):
        try:
            d = date_depuis_identifiant(pf, pid, typ or None)
        except ValueError:
            d = None
        if d is not None and (publie is None or abs((d.date - publie).total_seconds()) <= d.marge_secondes + 600):
            etiquette = ("identifiant TikTok décodé (± 18 s)" if pf == "tiktok"
                         else f"identifiant Instagram décodé (± {int(d.marge_secondes)} s)")
            return d.date, d.marge_secondes, etiquette
    if publie is not None:
        return publie, 0.0, "publishedAt donné par Oriane"
    return None, 0.0, "date inconnue"


def _date_info(v: dict) -> tuple[datetime | None, float, str]:
    return _date_cachee(str(v.get("platform") or "").lower(), str(v.get("platformId") or ""),
                        str(v.get("platformType") or v.get("format") or ""), str(v.get("publishedAt") or ""))


def _date(v: dict) -> datetime | None:
    return _date_info(v)[0]


def _date_sure(v: dict) -> datetime | None:
    """Date sûre à ± 2 min 30 : identifiant TikTok, photo Instagram. Reels, YouTube : écartés."""
    d, marge, _ = _date_info(v)
    if d is None or marge <= 0 or marge > 150:
        return None
    return d


def _creation_compte(v: dict, avant: datetime | None = None) -> datetime | None:
    """Date de création estimée d'un compte TikTok, lue dans `profilePlatformId` (un plancher)."""
    if str(v.get("platform") or "").lower() != "tiktok" or not v.get("profilePlatformId"):
        return None
    try:
        d = date_depuis_identifiant("tiktok", str(v["profilePlatformId"])).date
    except ValueError:
        return None
    ref = avant or _date(v)
    if ref is not None and d > ref:
        return None
    return d


def _reference(videos: Sequence[dict]) -> datetime | None:
    """Date de collecte la plus récente lue dans les résultats (pas d'horloge)."""
    dates = [d for v in videos for d in (_lire_iso(v.get("updatedAt")), _lire_iso(v.get("createdAt")), _date(v)) if d]
    return max(dates) if dates else None


def _tr(texte: str, n: int = 200) -> str:
    texte = " ".join(str(texte).split())
    return texte if len(texte) <= n else texte[: n - 1] + "…"


def _preuve(v: dict, extrait: str, seconde: float | None = None, piece: str | None = None,
            horloge: bool = False) -> PreuveSignal:
    if horloge:
        d, _, etiquette = _date_info(v)
        extrait = f"{_tr(extrait, 170)} [date : {d:%Y-%m-%d %H:%M} UTC, {etiquette}]" if d else extrait
    return PreuveSignal(piece=piece or _vid(v), url=_url(v, seconde), extrait=_tr(extrait, 260),
                        seconde=seconde, compte=_compte(v))


def _par_id(videos: Iterable[dict]) -> dict[str, dict]:
    sortie: dict[str, dict] = {}
    for v in videos:
        for cle in (v.get("id"), v.get("platformId"), _vid(v)):
            if cle:
                sortie.setdefault(str(cle), v)
    return sortie


def _dedoublonner(videos: Iterable[dict]) -> list[dict]:
    vus: set[str] = set()
    sortie = []
    for v in videos:
        cle = _vid(v)
        if cle not in vus:
            vus.add(cle)
            sortie.append(v)
    return sortie


def _premiere_par_compte(videos: Iterable[dict]) -> dict[str, datetime]:
    sortie: dict[str, datetime] = {}
    for v in videos:
        c, d = _compte(v), _date(v)
        if c and d and (c not in sortie or d < sortie[c]):
            sortie[c] = d
    return sortie


def _fenetre_max(dates: Sequence[datetime], heures: float) -> int:
    """Plus grand nombre de dates dans une fenêtre glissante de `heures` heures."""
    ds = sorted(dates)
    meilleur, j = 0, 0
    largeur = timedelta(hours=heures)
    for i, d in enumerate(ds):
        while ds[j] < d - largeur:
            j += 1
        meilleur = max(meilleur, i - j + 1)
    return meilleur


def _fr(valeur: float, decimales: int = 1) -> str:
    return f"{valeur:.{decimales}f}".replace(".", ",")


class _Union:
    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def trouver(self, x: Any) -> Any:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def unir(self, a: Any, b: Any) -> None:
        ra, rb = self.trouver(a), self.trouver(b)
        if ra != rb:
            self.parent[rb] = ra

    def composantes(self) -> list[set]:
        groupes: dict[Any, set] = defaultdict(set)
        for x in list(self.parent):
            groupes[self.trouver(x)].add(x)
        return list(groupes.values())


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _queue_binomiale(k: int, n: int, p: float) -> float:
    """P(X ≥ k) pour X ~ Binomiale(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return min(1.0, sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def _queue_poisson(x: int, lam: float) -> float:
    """P(X ≥ x) pour X ~ Poisson(lam)."""
    if x <= 0:
        return 1.0
    cumul, terme = 0.0, math.exp(-lam)
    for i in range(x):
        cumul += terme
        terme *= lam / (i + 1)
    return max(0.0, 1.0 - cumul)


# ---------------------------------------------------------------------------
# Témoin : taux de base d'un sujet sans enjeu
# ---------------------------------------------------------------------------

#: Mesures du témoin « football » (100 vidéos TikTok en projection full, spécification §1) : repli quand
#: aucun témoin n'est fourni. Le détail de `fragments_7` n'est pas conservé dans la spécification.
TEMOIN_FOOTBALL: dict[str, Any] = {
    "nom": "football (mesures de la spécification)", "comptes_datables": 89, "comptes_moins_90j": 0,
    "comptes_sans_passe": 0, "comptes_3_videos_ou_moins": 0, "rythme_median": 0.9, "rythme_max": 8.3,
    "rythme_p99": 8.3, "part_sons_licence": 0.14, "part_5_hashtags": 0.30, "fragments_7": frozenset(),
}


def _fragments(mots: Sequence[str], n: int) -> list[tuple[str, ...]]:
    return [tuple(mots[i:i + n]) for i in range(len(mots) - n + 1)]


def _mots_parole(v: dict) -> list[tuple[str, float | None, str]]:
    """(mot normalisé, seconde du morceau, texte brut du morceau) pour toute la parole d'une vidéo."""
    mots: list[tuple[str, float | None, str]] = []
    for media in v.get("media") or []:
        morceaux = media.get("transcriptChunks") or []
        if morceaux:
            for ch in morceaux:
                brut = ch.get("text")
                if isinstance(brut, str):
                    for m in _norm(brut).split():
                        mots.append((m, ch.get("startSeconds"), brut))
        elif isinstance(media.get("transcript"), str):
            for m in _norm(media["transcript"]).split():
                mots.append((m, None, media["transcript"]))
    return mots


def _profil_neuf(info: dict, ref: datetime | None) -> dict[str, bool | None]:
    """Traits A à E d'un compte (None quand le champ manque : on ne devine pas)."""
    anc = info.get("anciennete_j")
    posts, suit, bio, handle = info.get("posts"), info.get("suit"), info.get("bio"), info.get("pseudo") or ""
    return {
        "A": None if anc is None else anc < 90,
        "B": None if posts is None else posts <= 3,
        "C": None if suit is None else suit == 0,
        "D": None if bio is None else not str(bio).strip(),
        "E": bool(re.fullmatch(r"user\d{10,14}", handle.lower())),
    }


def _sans_passe(traits: dict[str, bool | None]) -> bool:
    points = sum(1 for t in traits.values() if t)
    return points >= 2 and bool(traits["A"] or traits["B"])


def _profils(videos: Iterable[dict], ref: datetime | None = None) -> dict[str, dict]:
    """Un dossier par compte TikTok : champs de profil, ancienneté estimée, vidéos."""
    comptes: dict[str, dict] = {}
    for v in videos:
        c = _compte(v)
        if c is None:
            continue
        info = comptes.setdefault(c, {"compte": c, "plateforme": c.split(":", 1)[0], "pseudo": c.split(":", 1)[1],
                                      "videos": [], "posts": None, "suit": None, "bio": None, "nom": None,
                                      "photo": None, "abonnes": None, "creation": None})
        info["videos"].append(v)
        if info["posts"] is None and isinstance(v.get("profilePostsCount"), (int, float)):
            info["posts"] = int(v["profilePostsCount"])
        if info["suit"] is None and isinstance(v.get("profileFollowingCount"), (int, float)):
            info["suit"] = int(v["profileFollowingCount"])
        if info["bio"] is None and "profileBio" in v:
            info["bio"] = v.get("profileBio") or ""
        if info["nom"] is None and v.get("profileDisplayName"):
            info["nom"] = v["profileDisplayName"]
        if info["photo"] is None and v.get("profilePictureUrl"):
            info["photo"] = v["profilePictureUrl"]
        if isinstance(v.get("profileFollowersCount"), (int, float)):
            info["abonnes"] = max(info["abonnes"] or 0, int(v["profileFollowersCount"]))
    for info in comptes.values():
        premiere = min((d for d in map(_date, info["videos"]) if d), default=None)
        creation = _creation_compte(info["videos"][0], premiere)
        info["creation"] = creation
        info["anciennete_j"] = (premiere - creation).total_seconds() / 86400 if creation and premiere else None
        info["premiere"] = premiere
    return comptes


def mesurer_temoin(videos: Sequence[dict], reference: datetime | None = None) -> dict[str, Any]:
    """Taux de base d'un sujet sans enjeu, mesurés sur ses vidéos TikTok en projection full.

    À recalculer depuis le cache, jamais commité. Rend les chiffres que les détecteurs de compte
    (D5, D6) comparent à leur groupe, et les fragments de parole courants que D1 écarte.
    """
    videos = _dedoublonner(videos)
    ref = reference or _reference(videos)
    profils = {c: i for c, i in _profils(videos).items() if i["plateforme"] == "tiktok"}
    datables = [i for i in profils.values() if i["anciennete_j"] is not None]
    rythmes = []
    for i in datables:
        if i["posts"] is not None and ref and i["creation"]:
            jours = (ref - i["creation"]).total_seconds() / 86400
            if jours >= 7:
                rythmes.append(i["posts"] / jours)
    rythmes.sort()
    licencies = [v for v in videos if v.get("audioPlatformId")]
    fragments: set[str] = set()
    for v in videos:
        fragments.update(" ".join(f) for f in _fragments([m for m, _, _ in _mots_parole(v)], 7))
    avec_tags = [v for v in videos if v.get("hashtags")]
    return {
        "nom": "témoin mesuré", "videos": len(videos), "comptes_datables": len(datables),
        "comptes_moins_90j": sum(1 for i in datables if i["anciennete_j"] < 90),
        "comptes_sans_passe": sum(1 for i in profils.values() if _sans_passe(_profil_neuf(i, ref))),
        "comptes_3_videos_ou_moins": sum(1 for i in profils.values() if i["posts"] is not None and i["posts"] <= 3),
        "rythme_median": statistics.median(rythmes) if rythmes else None,
        "rythme_max": rythmes[-1] if rythmes else None,
        "rythme_p99": rythmes[min(len(rythmes) - 1, int(0.99 * len(rythmes)))] if rythmes else None,
        "part_sons_licence": (sum(1 for v in licencies if v.get("audioCopyrighted") or v.get("audioType") == "licensed")
                              / len(licencies)) if licencies else None,
        "part_5_hashtags": (sum(1 for v in avec_tags if len(v["hashtags"]) >= 5) / len(avec_tags)) if avec_tags else None,
        "fragments_7": frozenset(fragments),
    }


def _temoin(temoin: dict | None) -> dict:
    return temoin if temoin is not None else TEMOIN_FOOTBALL


# ---------------------------------------------------------------------------
# D1 · Même phrase, comptes différents
# ---------------------------------------------------------------------------

_EXPL_D1 = ("Une personnalité, un journal télévisé ou une dépêche citée par tout le monde, un slogan ou un format de "
            "tendance donnent la même phrase sans aucun contact entre les comptes. Le vrai premier diffuseur peut "
            "aussi être hors de l'index (X, Telegram, compte de moins de 5 000 abonnés) : la première apparition "
            "trouvée n'est pas la première.")


def formulation_partagee(videos: Sequence[dict], rarete: dict[str, int] | None = None,
                         temoin: dict | None = None) -> list[SignalFaible]:
    """D1 : une phrase d'au moins 8 mots, mot pour mot, dite par au moins 3 comptes.

    `rarete[phrase normalisée]` = `totalCount` d'une sonde `transcript.includesExactly` sans filtre
    de sujet. Sans sonde, la force reste « faible ».
    """
    videos = _dedoublonner(v for v in videos if _compte(v))
    ecart = _temoin(temoin).get("fragments_7") or frozenset()
    textes = [_mots_parole(v) for v in videos]
    mots = [[m for m, _, _ in t] for t in textes]
    comptes = [_compte(v) for v in videos]

    frag_videos: dict[tuple[str, ...], set[int]] = defaultdict(set)
    for i, ms in enumerate(mots):
        for f in _fragments(ms, 7):
            if len(set(f)) > 2 and " ".join(f) not in ecart:
                frag_videos[f].add(i)
    candidats = {f: vs for f, vs in frag_videos.items() if len({comptes[i] for i in vs}) >= 3}
    if not candidats:
        return []

    phrases: set[tuple[str, ...]] = set()
    for i, ms in enumerate(mots):
        frs = _fragments(ms, 7)
        j = 0
        while j < len(frs):
            if frs[j] not in candidats:
                j += 1
                continue
            soutien = set(candidats[frs[j]])
            fin = j
            while fin + 1 < len(frs) and frs[fin + 1] in candidats:
                commun = soutien & candidats[frs[fin + 1]]
                if len({comptes[x] for x in commun}) < 3:
                    break
                soutien, fin = commun, fin + 1
            if fin >= j + 1:  # au moins deux fragments recollés : 8 mots ou plus
                phrases.add(tuple(ms[j:fin + 7]))
            j = fin + 1

    paddes = [" " + " ".join(ms) + " " for ms in mots]
    trouvees: list[tuple[str, set[int]]] = []
    for ph in phrases:
        texte = " ".join(ph)
        soutien = {i for i, p in enumerate(paddes) if f" {texte} " in p}
        if len({comptes[i] for i in soutien}) >= 3:
            trouvees.append((texte, soutien))
    # Une phrase contenue dans une plus longue n'est gardée que si elle réunit plus de comptes.
    trouvees.sort(key=lambda t: -len(t[0]))
    gardees: list[tuple[str, set[int]]] = []
    for texte, soutien in trouvees:
        nc = len({comptes[i] for i in soutien})
        if any(texte in autre and nc <= len({comptes[i] for i in s2}) for autre, s2 in gardees):
            continue
        gardees.append((texte, soutien))

    sons_libres = {i: str(videos[i].get("audioPlatformId")) for i in range(len(videos))
                   if videos[i].get("audioPlatformId") and not videos[i].get("audioCopyrighted")
                   and videos[i].get("audioType") != "licensed"}
    signaux = []
    for texte, soutien in gardees:
        idx = sorted(soutien)
        n_comptes = {comptes[i] for i in idx}
        union = _Union()
        for i in idx:
            union.trouver(i)
        par_compte: dict[str, int] = {}
        par_son: dict[str, int] = {}
        for i in idx:
            if comptes[i] in par_compte:
                union.unir(i, par_compte[comptes[i]])
            par_compte[comptes[i]] = i
            if i in sons_libres:
                if sons_libres[i] in par_son:
                    union.unir(i, par_son[sons_libres[i]])
                par_son[sons_libres[i]] = i
        frs = {i: {" ".join(f) for f in _fragments(mots[i], 7)} for i in idx}
        for a, b in itertools.combinations(idx, 2):
            if _jaccard(frs[a], frs[b]) >= 0.8:
                union.unir(a, b)
        grappes = len(union.composantes())

        premieres = _premiere_par_compte(videos[i] for i in idx)
        ordre = sorted(premieres.items(), key=lambda kv: kv[1])
        primo = [c for c, _ in ordre[:3]]
        abonnes = {}
        for i in idx:
            f = videos[i].get("profileFollowersCount")
            if isinstance(f, (int, float)):
                abonnes[comptes[i]] = max(abonnes.get(comptes[i], 0), f)
        avance = None
        if ordre:
            gros = [d for c, d in ordre if abonnes.get(c, 0) >= 100_000]
            avance = (gros[0] - ordre[0][1]) if gros else None
        fenetre = _fenetre_max(list(premieres.values()), 72)
        total = (rarete or {}).get(texte)

        force = "faible"
        if total is not None and fenetre >= 5 and total <= 200:
            force = "moyenne"
            if grappes >= 3 and avance is not None and avance >= timedelta(hours=6):
                force = "forte"

        # Les vidéos citées, la plus ancienne d'abord (l'horloge est nommée dans la première preuve).
        idx.sort(key=lambda i: (_date(videos[i]) or datetime.max.replace(tzinfo=UTC), _vid(videos[i])))
        preuves = []
        for rang, i in enumerate(idx):
            mots_i = textes[i]
            pos = paddes[i].find(f" {texte} ")
            avant = paddes[i][:pos].count(" ") if pos >= 0 else 0
            seconde = mots_i[avant][1] if avant < len(mots_i) else None
            brut = mots_i[avant][2] if avant < len(mots_i) else texte
            preuves.append(_preuve(videos[i], brut, seconde, horloge=(rang == 0)))
        premiere = _date(videos[idx[0]])
        signaux.append(_signal(
            "formulation_partagee",
            f"{len(idx)} vidéos, {len(n_comptes)} comptes : la même phrase, mot pour mot",
            "Un texte commun circule (consigne, script fourni, reprise d'une même source) : à vérifier.",
            _EXPL_D1, force, premiere, preuves,
            "D'où vient cette phrase, et qui l'a dite en premier ?",
            ("Écrire aux 3 primo-diffuseurs · comptes publics · gratuit",
             "Chercher la phrase dans la presse et sur le web avant leur date · GDELT, recherche web · gratuit",
             "Vérifier qu'elle ne sort pas d'un JT ou d'un discours · replays, archives · gratuit"),
            mesures=(("videos", len(idx)), ("comptes", len(n_comptes)), ("grappes", grappes),
                     ("phrase", texte), ("fenetre_72h", fenetre), ("rarete_totalCount", total),
                     ("primo_diffuseurs", tuple(primo)),
                     ("avance_s", avance.total_seconds() if avance is not None else None))))
    return _trier(signaux)


def chiffre_moment_fort(signal: SignalFaible) -> str:
    """Le chiffre mesuré du moment fort de la démo : « N vidéos · M comptes · K grappes indépendantes »."""
    return (f"{signal.mesure('videos')} vidéos · {signal.mesure('comptes')} comptes · "
            f"{signal.mesure('grappes')} grappes indépendantes")


# ---------------------------------------------------------------------------
# D2 · Gabarit commun
# ---------------------------------------------------------------------------

_EXPL_D2 = ("Un défi ou une tendance repris de bon gré, le modèle d'une application de montage, le brief déclaré "
            "d'une marque ou d'une agence, ou des fans qui copient un créateur produisent les mêmes textes et "
            "les mêmes hashtags sans qu'une consigne secrète existe.")


def _empreintes_gabarit(v: dict, banals: set[str]) -> dict[str, Any]:
    tags = [str(t).strip().lstrip("#").casefold() for t in (v.get("hashtags") or []) if isinstance(t, str) and t.strip("# ")]
    utiles = tuple(t for t in tags if t not in banals)
    sortie: dict[str, Any] = {}
    if len(utiles) >= 3:
        sortie["a"] = utiles
    ecran = _norm(" ".join(t for m in v.get("media") or [] for t in (m.get("onScreenText") or []) if isinstance(t, str)))
    if len(ecran.split()) >= 4:
        sortie["b"] = frozenset(_fragments(ecran.split(), 3)) or None
    legende = v.get("caption") or ""
    legende = re.sub(r"#\w+|@[\w.]+", " ", legende)
    mots = _norm(legende).split()
    if len(mots) >= 8:
        sortie["c"] = frozenset(_fragments(mots, 5)) or None
    return {k: x for k, x in sortie.items() if x}


def gabarit_commun(videos: Sequence[dict]) -> list[SignalFaible]:
    """D2 : plusieurs comptes publient la même suite de hashtags, le même texte à l'écran ou la même légende."""
    videos = _dedoublonner(v for v in videos if _compte(v))
    comptes = [_compte(v) for v in videos]
    par_tag: dict[str, set[str]] = defaultdict(set)
    for v, c in zip(videos, comptes):
        for t in v.get("hashtags") or []:
            if isinstance(t, str) and t.strip("# "):
                par_tag[t.strip().lstrip("#").casefold()].add(c)
    total = len(set(comptes))
    banals = {t for t, cs in par_tag.items() if _pv.est_banal(_pv.HASHTAG, t, len(cs), total)}
    emp = [_empreintes_gabarit(v, banals) for v in videos]

    # composantes de comptes par type d'empreinte : kind -> liste de (comptes, vidéos)
    comps: list[tuple[str, set[str], set[int]]] = []
    for kind in ("a", "b", "c"):
        union = _Union()
        porteurs = [i for i, e in enumerate(emp) if kind in e]
        if kind == "a":
            groupe: dict[Any, list[int]] = defaultdict(list)
            for i in porteurs:
                groupe[emp[i]["a"]].append(i)
            paires = [(a, b) for lst in groupe.values() for a, b in itertools.combinations(lst, 2)]
        else:
            inv: dict[tuple, list[int]] = defaultdict(list)
            for i in porteurs:
                for s in emp[i][kind]:
                    inv[s].append(i)
            vues: set[tuple[int, int]] = set()
            paires = []
            for lst in inv.values():
                if len(lst) > 400:
                    continue
                for a, b in itertools.combinations(sorted(lst), 2):
                    if (a, b) not in vues:
                        vues.add((a, b))
                        if _jaccard(emp[a][kind], emp[b][kind]) >= 0.8:
                            paires.append((a, b))
        implique: set[int] = set()
        for a, b in paires:
            if comptes[a] != comptes[b]:
                union.unir(comptes[a], comptes[b])
                implique.update((a, b))
        for compo in union.composantes():
            vs = {i for i in implique if comptes[i] in compo}
            if len(compo) >= 5:
                comps.append((kind, compo, vs))
    if not comps:
        return []

    cluster = _Union()
    for n, (_, cs, _) in enumerate(comps):
        cluster.trouver(n)
    for (n1, (_, c1, _)), (n2, (_, c2, _)) in itertools.combinations(enumerate(comps), 2):
        if len(c1 & c2) >= 5:
            cluster.unir(n1, n2)
    signaux = []
    for groupe in cluster.composantes():
        par_kind: dict[str, tuple[set[str], set[int]]] = {}
        for n in groupe:
            kind, cs, vs = comps[n]
            c0, v0 = par_kind.get(kind, (set(), set()))
            par_kind[kind] = (c0 | cs, v0 | vs)
        choix = None
        for taille in (3, 2, 1):
            for combo in itertools.combinations(sorted(par_kind), taille):
                inter = set.intersection(*(par_kind[k][0] for k in combo))
                if len(inter) >= 5 and (choix is None or len(combo) > len(choix[0])
                                        or (len(combo) == len(choix[0]) and len(inter) > len(choix[1]))):
                    choix = (combo, inter)
            if choix:
                break
        if choix is None:
            continue
        kinds, coeur = choix
        vs = sorted({i for k in kinds for i in par_kind[k][1] if comptes[i] in coeur},
                    key=lambda i: (_date(videos[i]) or datetime.max.replace(tzinfo=UTC), _vid(videos[i])))
        premieres = _premiere_par_compte(videos[i] for i in vs)
        fenetre = _fenetre_max([d for c, d in premieres.items() if c in coeur], 72)
        if len(kinds) == 3 and fenetre >= 5:
            force = "forte"
        elif len(kinds) >= 2 or len(coeur) >= 10:
            force = "moyenne"
        else:
            force = "faible"
        morceaux = {"b": "même texte à l'écran", "a": "mêmes hashtags dans le même ordre", "c": "même légende"}
        if set(kinds) == {"a", "b", "c"} or set(kinds) == {"a", "b"}:
            partie = f"{morceaux['b']}, {morceaux['a']}"
        else:
            partie = ", ".join(morceaux[k] for k in ("b", "a", "c") if k in kinds)
        preuves = []
        for rang, i in enumerate(vs):
            e = emp[i]
            extrait = (" ".join("#" + t for t in e["a"]) if "a" in e and "a" in kinds else
                       " ".join(t for m in videos[i].get("media") or [] for t in (m.get("onScreenText") or [])[:6])
                       if "b" in e and "b" in kinds else videos[i].get("caption") or "")
            preuves.append(_preuve(videos[i], extrait, horloge=(rang == 0)))
        signaux.append(_signal(
            "gabarit_commun", f"{len(coeur)} comptes, {partie}",
            "Un modèle fourni (brief, consigne de groupe, kit de campagne) pourrait être à l'origine de ces vidéos.",
            _EXPL_D2, force, _date(videos[vs[0]]), preuves, "Qui a fourni ce modèle de vidéo ?",
            ("Retrouver la plus ancienne vidéo du gabarit · fiche du signal · gratuit",
             "Chercher le texte incrusté ailleurs · onScreenText.includesExactly · 10 crédits",
             "Demander aux comptes s'ils ont reçu un brief · comptes publics · gratuit"),
            mesures=(("comptes", len(coeur)), ("gabarits", len(kinds)), ("fenetre_72h", fenetre),
                     ("types", tuple(sorted(kinds))))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# D3 · Son lancé par peu de comptes puis repris
# ---------------------------------------------------------------------------

_EXPL_D3 = ("Un son qui plaît est poussé par la plateforme et tout le monde le reprend : c'est le fonctionnement "
            "normal de TikTok (dans le témoin, un son sur trois est plus ancien que la vidéo qui l'utilise). Un "
            "studio peut aussi réutiliser son propre son pour ses clients.")


def _date_son(identifiant: Any) -> datetime | None:
    try:
        return date_depuis_identifiant("tiktok", str(identifiant)).date
    except ValueError:
        return None


def son_lance_par_peu(videos: Sequence[dict], usages: dict[str, Any] | None = None) -> list[SignalFaible]:
    """D3 : un son non licencié créé par un compte puis repris par plusieurs comptes en peu de temps.

    `usages[son]` : liste de vidéos d'une sonde `audioPlatformId`, ou `{"videos": [...], "totalCount": n}`.
    """
    videos = _dedoublonner(v for v in videos if _compte(v))
    par_son: dict[str, list[dict]] = defaultdict(list)
    infos: dict[str, dict] = {}
    for v in videos:
        if str(v.get("platform") or "").lower() != "tiktok" or not v.get("audioPlatformId"):
            continue
        if v.get("audioCopyrighted") or v.get("audioType") == "licensed":
            continue
        son = str(v["audioPlatformId"])
        par_son[son].append(v)
        infos.setdefault(son, v)
    signaux = []
    for son, vs in par_son.items():
        extra = (usages or {}).get(son)
        total = None
        if isinstance(extra, dict):
            total = extra.get("totalCount")
            extra = extra.get("videos") or []
        pool = _dedoublonner([*vs, *[x for x in (extra or []) if _compte(x)]])
        d_son = _date_son(son)
        premiere_par_compte = _premiere_par_compte(pool)
        if d_son is None or len(premiere_par_compte) < 5:
            continue
        # Son d'origine : sa date tombe moins de 2 min après celle d'une vidéo (marge d'identifiant : 18 s).
        origine = None
        for v in sorted(pool, key=lambda x: _date(x) or datetime.max.replace(tzinfo=UTC)):
            d = _date(v)
            if d is not None and -18 <= (d_son - d).total_seconds() <= 120:
                origine = v
                break
        createur = _compte(origine) if origine else None
        debut = d_son if origine else min(premiere_par_compte.values())
        age_son = (min(premiere_par_compte.values()) - d_son).days
        repris = {c: d for c, d in premiere_par_compte.items() if c != createur and d >= debut - timedelta(minutes=3)}
        ds = sorted(repris.values())

        def dans(heures: float) -> int:
            return sum(1 for d in ds if d - debut <= timedelta(hours=heures))

        n6, n24, n72 = dans(6), dans(24), dans(72)
        delai5 = (ds[4] - ds[0]) if len(ds) >= 5 else None
        force = None
        if n72 >= 5:
            force = "faible"
        if n6 >= 5 or (age_son > 365 and n24 >= 8):
            force = "moyenne"
        if n6 >= 10:
            force = "forte"
        if force is None or (origine is None and age_son <= 365):
            continue  # sans origine trouvée, on ne dit pas « créé par un compte »
        if origine is not None:
            heures = 6 if n6 >= 5 else (24 if n24 >= 5 else 72)
            n = {6: n6, 24: n24, 72: n72}[heures]
            titre = f"Son créé par un compte, repris par {n} comptes en {heures} heures"
            hyp = "Une trame (voix, texte lu) lancée par un petit cercle puis relayée : à vérifier."
        else:
            heures, n = 24, n24
            titre = f"Son de plus d'un an, repris par {n} comptes en 24 heures"
            hyp = "Une trame ancienne remise en circulation par un petit cercle : à vérifier."
        cites = sorted((c for c in repris if _date_par(pool, c) is not None), key=lambda c: repris[c])
        preuves = []
        if origine is not None:
            preuves.append(_preuve(origine, f"audioPlatformId={son} ; son créé le {d_son:%Y-%m-%d %H:%M} UTC "
                                            f"(date lue dans l'identifiant du son)", horloge=True))
        for c in cites:
            v = _date_par(pool, c)
            if all(p.piece != _vid(v) for p in preuves):
                preuves.append(_preuve(v, f"audioPlatformId={son} ({infos[son].get('audioTitle') or 'son sans titre'})"))
        if not preuves:
            continue
        signaux.append(_signal(
            "son_lance_par_peu", titre, hyp, _EXPL_D3, force, d_son if origine else debut, preuves,
            "Qui a créé ce son, et d'où vient ce qu'il dit ?",
            ("Écrire au créateur du son · compte public · gratuit",
             "Transcrire le son et chercher sa phrase (D1) · transcription locale · gratuit",
             "Regarder les comptes des 6 premières heures (D5, D9) · fiche du signal · gratuit"),
            mesures=(("comptes_6h", n6), ("comptes_24h", n24), ("comptes_72h", n72),
                     ("delai_1er_au_5e_s", delai5.total_seconds() if delai5 else None), ("totalCount", total),
                     ("createur_trouve", origine is not None), ("son", son))))
    return _trier(signaux)


def _date_par(pool: Sequence[dict], compte: str) -> dict | None:
    """La plus ancienne vidéo d'un compte dans un ensemble."""
    candidats = [v for v in pool if _compte(v) == compte and _date(v)]
    return min(candidats, key=_date) if candidats else None


# ---------------------------------------------------------------------------
# D4 · Séquence déjà vue
# ---------------------------------------------------------------------------

MIN_SCORE_IMAGE = 0.9  # conseil de l'aide Oriane ; échelle à vérifier, à calibrer sur 10 paires connues
_EXPL_D4 = ("Une archive annoncée comme telle, une image d'illustration, des plans repris dans un montage "
            "d'actualité ou un décor semblable donnent des images proches. L'original peut aussi venir de la "
            "télévision ou d'un réseau non indexé, et ne rien trouver ne prouve rien (l'index couvre 3 mois).")


def sequence_deja_vue(videos: Sequence[dict], correspondances: dict[str, list[list[dict]]],
                      dates_revendiquees: dict[str, Any] | None = None,
                      min_score: float = MIN_SCORE_IMAGE) -> list[SignalFaible]:
    """D4 : des images clés déjà publiées plus tôt par un autre compte.

    `correspondances[clé de la vidéo]` : une liste par image clé (3 espacées), chacune une liste de
    vidéos retrouvées `{id, platform, platformId, profileHandle, publishedAt, score}`.
    `dates_revendiquees` : date dite dans la vidéo ou par le journaliste ; à défaut, la date de publication.
    """
    par_id = _par_id(videos)
    signaux = []
    for cle, images in (correspondances or {}).items():
        v = par_id.get(str(cle))
        if v is None or not images:
            continue
        propre = _compte(v)
        trouvees: dict[str, dict] = {}
        for k, matches in enumerate(images):
            for m in matches or []:
                if float(m.get("score") or 0) < min_score or _compte(m) in (None, propre):
                    continue
                t = trouvees.setdefault(_vid(m), {"video": m, "images": set(), "score": 0.0})
                t["images"].add(k)
                t["score"] = max(t["score"], float(m["score"]))
        revendiquee = _lire_iso((dates_revendiquees or {}).get(cle)) or _date(v)
        meilleur = None
        for t in trouvees.values():
            if len(t["images"]) < 2:
                continue
            d = _date(t["video"])
            if d is not None and (meilleur is None or d < _date(meilleur["video"])):
                meilleur = t
        if meilleur is None or revendiquee is None:
            continue
        ecart = revendiquee - _date(meilleur["video"])
        if ecart < timedelta(hours=24):
            continue
        sur = len(images)
        force = "faible"
        if ecart >= timedelta(days=7):
            force = "moyenne"
        if ecart >= timedelta(days=30) and len(meilleur["images"]) >= 3 and sur >= 3:
            force = "forte"
        m = meilleur["video"]
        jours = ecart.days
        preuves = [
            _preuve(v, f"{_tr(v.get('caption') or 'vidéo analysée')} (date revendiquée : {revendiquee:%Y-%m-%d})",
                    horloge=True),
            PreuveSignal(piece=_vid(m), url=_url(m), compte=_compte(m), source="Oriane",
                         extrait=_tr(f"première correspondance trouvée : {len(meilleur['images'])} images sur {sur}, "
                                     f"score {meilleur['score']:.2f}, publiée le {_date(m):%Y-%m-%d}")),
        ]
        signaux.append(_signal(
            "sequence_deja_vue", f"Images déjà publiées {jours} jours plus tôt par un autre compte",
            "Des images anciennes pourraient être présentées comme neuves : à vérifier.", _EXPL_D4, force,
            _date(m), preuves, "Où et quand ces images ont-elles été filmées ?",
            ("Écrire au compte de la première apparition trouvée · compte public · gratuit",
             "Recherche d'image inversée des 3 images · Google Lens, Yandex, TinEye, à la main · gratuit",
             "Chercher une vérification publiée et la presse du lieu supposé · AFP Factuel · gratuit"),
            mesures=(("ecart_jours", jours), ("images_concordantes", len(meilleur["images"])), ("images", sur),
                     ("min_score", min_score))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# D5 · Comptes sans passé
# ---------------------------------------------------------------------------

_EXPL_D5 = ("Un sujet neuf attire de nouveaux inscrits ; un témoin ouvre un compte pour raconter ce qu'il a vu ; un "
            "pseudo par défaut signale un profil jamais réglé. L'ancienneté est une estimation tirée de "
            "l'identifiant du compte (méthode non documentée par TikTok).")


def _groupes_par_defaut(videos: Sequence[dict], groupes: dict[str, Sequence[dict]] | None) -> dict[str, Sequence[dict]]:
    return groupes if groupes else {"dossier": videos}


def comptes_sans_passe(videos: Sequence[dict], groupes: dict[str, Sequence[dict]] | None = None,
                       temoin: dict | None = None) -> list[SignalFaible]:
    """D5 : part de comptes TikTok sans passé (ancienneté estimée faible, profil vide) dans un groupe."""
    tem = _temoin(temoin)
    p_tem = (tem.get("comptes_sans_passe", tem.get("comptes_moins_90j", 0)) + 1) / (tem.get("comptes_datables", 0) + 2)
    signaux = []
    for nom, groupe in _groupes_par_defaut(videos, groupes).items():
        profils = {c: i for c, i in _profils(_dedoublonner(groupe)).items() if i["plateforme"] == "tiktok"}
        n = len(profils)
        if n < 3:
            continue
        neufs = [(i, _profil_neuf(i, None)) for i in profils.values()]
        sans = [(i, t) for i, t in neufs if _sans_passe(t)]
        k = len(sans)
        if k < 3:
            continue
        part = k / n
        p = _queue_binomiale(k, n, p_tem)
        force = "faible"
        if k >= 5 and part >= 0.30 and part >= 5 * p_tem and p < 0.01:
            force = "moyenne"
        tous_a = all(t["A"] for _, t in sans)
        titre = (f"{k} des {n} comptes ont moins de 90 jours (ancienneté estimée)" if tous_a
                 else f"{k} des {n} comptes montrent un profil sans passé")
        preuves = []
        for rang, (i, t) in enumerate(sorted(sans, key=lambda x: x[0]["premiere"] or datetime.max.replace(tzinfo=UTC))):
            v = i["videos"][0]
            traits = "".join(sorted(k_ for k_, ok in t.items() if ok))
            anc = f"ancienneté estimée {i['anciennete_j']:.0f} j" if i["anciennete_j"] is not None else "ancienneté non datable"
            preuves.append(_preuve(v, f"traits {traits} · {anc} · {i['posts'] if i['posts'] is not None else '?'} vidéos "
                                      f"publiées · bio {'vide' if i['bio'] == '' else 'renseignée' if i['bio'] else 'non lue'}",
                                   horloge=(rang == 0)))
        signaux.append(_signal(
            "comptes_sans_passe", titre,
            "Des comptes ouverts pour l'occasion, ou à usage unique : à vérifier.", _EXPL_D5, force,
            min((i["premiere"] for i, _ in sans if i["premiere"]), default=None), preuves,
            "Qui a ouvert ces comptes, et pourquoi maintenant ?",
            ("Comparer leurs profils (D8) · fiche du signal · gratuit",
             "Vérifier s'ils sont encore en ligne · pages publiques · gratuit",
             "Leur écrire · comptes publics · gratuit"),
            mesures=(("comptes_sans_passe", k), ("comptes", n), ("part", round(part, 3)),
                     ("part_temoin_lissee", round(p_tem, 4)), ("p", p), ("groupe", nom))))
    return _trier(signaux)


def commentateurs_recents(videos: Sequence[dict], temoin: dict | None = None) -> list[SignalFaible]:
    """D5, variante : part des auteurs de commentaires populaires de moins de 30 jours.

    Faible au plus, et sans nœud dans le réseau : un commentateur ne devient jamais un nœud.
    """
    auteurs: dict[str, dict] = {}
    for v in videos:
        for c in v.get("popularComments") or []:
            ident, pub = c.get("profilePlatformId"), _lire_iso(c.get("publishedAt"))
            if not ident or pub is None:
                continue
            try:
                creation = date_depuis_identifiant("tiktok", str(ident)).date
            except ValueError:
                continue
            if creation > pub:
                continue
            auteurs.setdefault(str(ident), {"jours": (pub - creation).total_seconds() / 86400, "video": v})
    neufs = [a for a in auteurs.values() if a["jours"] < 30]
    if len(auteurs) < 20 or len(neufs) < 5 or len(neufs) / len(auteurs) < 0.30:
        return []
    cibles = _dedoublonner(a["video"] for a in neufs)
    preuves = [_preuve(v, "commentaires populaires : auteurs de moins de 30 jours (identité non conservée)",
                       horloge=(r == 0)) for r, v in enumerate(cibles)]
    return [_signal(
        "commentateurs_recents",
        f"{len(neufs)} des {len(auteurs)} commentateurs ont moins de 30 jours",
        "Des comptes ouverts pour commenter, ou un sujet qui attire de nouveaux inscrits : à vérifier.",
        "Un sujet neuf attire de nouveaux inscrits, et les commentaires populaires sont une petite part de ce que "
        "les gens écrivent. Aucun commentateur n'est nommé ni suivi.", "faible", None, preuves,
        "Ces nouveaux comptes commentent-ils au-delà de ces vidéos ?",
        ("Relire les commentaires sous les vidéos citées · plateforme · gratuit",
         "Comparer avec un sujet sans enjeu (témoin) · cache · gratuit"),
        mesures=(("commentateurs", len(auteurs)), ("moins_de_30_jours", len(neufs))), comptes=())]


# ---------------------------------------------------------------------------
# D6 · Cadence hors norme
# ---------------------------------------------------------------------------

_EXPL_D6 = ("Un média, une marque ou un compte de reprises assumé publie beaucoup ; une équipe à plusieurs mains ou "
            "un outil de programmation donnent la même cadence sans aucune automatisation cachée.")


def cadence_hors_norme(videos: Sequence[dict], groupes: dict[str, Sequence[dict]] | None = None,
                       temoin: dict | None = None, reference: datetime | None = None) -> list[SignalFaible]:
    """D6 : un compte TikTok qui publie 10 vidéos par jour ou plus depuis sa création estimée."""
    tem = _temoin(temoin)
    seuil = max(10.0, float(tem.get("rythme_p99") or 0))
    signaux = []
    for nom, groupe in _groupes_par_defaut(videos, groupes).items():
        groupe = _dedoublonner(groupe)
        ref = reference or _reference(groupe)
        hauts = []
        for c, i in _profils(groupe).items():
            if i["plateforme"] != "tiktok" or i["posts"] is None or not i["creation"] or ref is None:
                continue
            jours = (ref - i["creation"]).total_seconds() / 86400
            if jours >= 7 and i["posts"] / jours >= seuil:
                hauts.append((i, i["posts"] / jours, jours))
        if not hauts:
            continue

        def preuves_de(lot: list) -> list[PreuveSignal]:
            lot = sorted(lot, key=lambda x: x[0]["premiere"] or datetime.max.replace(tzinfo=UTC))
            return [_preuve(i["videos"][0], f"{i['posts']} vidéos publiées en {jours:.0f} jours estimés, "
                                            f"soit {_fr(taux)} par jour (témoin : 99e centile {_fr(seuil)})",
                            horloge=(r == 0)) for r, (i, taux, jours) in enumerate(lot)]

        if len(hauts) >= 3:
            signaux.append(_signal(
                "cadence_hors_norme", f"{len(hauts)} comptes publient plus de 10 vidéos par jour",
                "Une publication automatisée, ou une équipe derrière chaque profil : à vérifier.", _EXPL_D6,
                "moyenne", min((i["premiere"] for i, _, _ in hauts if i["premiere"]), default=None), preuves_de(hauts),
                "Qui publie réellement sur ces comptes ?",
                ("Lire les bios et les mentions légales (média, agence ?) · profils publics · gratuit",
                 "Comparer leurs vidéos à celles d'autres comptes (D1, D4) · fiche du signal · gratuit",
                 "Leur écrire · comptes publics · gratuit"),
                mesures=(("comptes", len(hauts)), ("seuil_par_jour", seuil), ("groupe", nom))))
        else:
            for h in hauts:
                signaux.append(_signal(
                    "cadence_hors_norme", f"Un compte publie {_fr(h[1])} vidéos par jour depuis sa création",
                    "Une publication automatisée, ou une équipe derrière un seul profil : à vérifier.", _EXPL_D6,
                    "faible", h[0]["premiere"], preuves_de([h]), "Qui publie réellement sur ce compte ?",
                    ("Lire la bio et les mentions légales (média, agence ?) · profil public · gratuit",
                     "Comparer ses vidéos à celles d'autres comptes (D1, D4) · fiche du signal · gratuit",
                     "Lui écrire · compte public · gratuit"),
                    mesures=(("par_jour", round(h[1], 2)), ("seuil_par_jour", seuil), ("groupe", nom))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# D7 · Compte réveillé
# ---------------------------------------------------------------------------

_EXPL_D7 = ("Un retour après une pause, un nouveau projet, un compte transmis ou cédé en règle, ou d'anciennes "
            "vidéos supprimées ou masquées donnent la même courbe. Le nombre de vidéos récentes est un plancher "
            "(l'index a jusqu'à une semaine de retard).")


def compte_reveille(videos: Sequence[dict], recents: dict[str, int] | None,
                    reference: datetime | None = None) -> list[SignalFaible]:
    """D7 : un compte ancien, quasi muet, soudain actif. `recents[compte]` = vidéos des 90 jours avant la référence."""
    videos = _dedoublonner(v for v in videos if _compte(v))
    ref = reference or _reference(videos)
    if not recents or ref is None:
        return []
    profils = _profils(videos)
    signaux = []
    for compte, n_recents in recents.items():
        i = profils.get(compte)
        if i is None or i["plateforme"] != "tiktok" or i["posts"] is None or not i["creation"]:
            continue
        anciennete = (ref - i["creation"]).total_seconds() / 86400
        if anciennete < 365 or n_recents < 5:
            continue
        passe = max(i["posts"] - n_recents, 0) / max(anciennete - 90, 1)
        recent = n_recents / 90
        ratio = recent / max(passe, 1 / 365)
        if ratio < 10:
            continue
        sur_sujet = sum(1 for v in i["videos"] if (d := _date(v)) and ref - timedelta(days=90) <= d <= ref)
        force = "moyenne" if sur_sujet >= 0.5 * n_recents else "faible"
        ans = int(anciennete // 365)
        titre = (f"Compte de {ans} ans, quasi muet, soudain {sur_sujet} vidéos sur ce sujet" if force == "moyenne"
                 else f"Compte de {ans} ans, quasi muet, soudain {n_recents} vidéos en 90 jours")
        preuves = [_preuve(v, f"compte créé il y a {anciennete / 365:.1f} an(s) (estimation), {i['posts']} vidéos "
                              f"publiées au total, {n_recents} dans les 90 jours avant le {ref:%Y-%m-%d} "
                              f"(plancher), rythme multiplié par {ratio:.0f}", horloge=(r == 0))
                   for r, v in enumerate(sorted(i["videos"], key=lambda x: _date(x) or ref))]
        signaux.append(_signal(
            "compte_reveille", titre, "Un compte racheté, repris ou réaffecté : à vérifier.", _EXPL_D7, force,
            i["premiere"], preuves, "Qui tient ce compte aujourd'hui ?",
            ("Captures Wayback du profil · web.archive.org · gratuit",
             "Écrire au titulaire · compte public · gratuit",
             "Retrouver d'anciens abonnés · plateforme · gratuit"),
            mesures=(("anciennete_ans", round(anciennete / 365, 1)), ("recents_90j", n_recents),
                     ("rythme_passe_par_jour", round(passe, 4)), ("ratio", round(ratio, 1)),
                     ("videos_du_dossier", sur_sujet))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# D8 · Profils jumeaux
# ---------------------------------------------------------------------------

_EXPL_D8 = ("Des fan-clubs, les antennes d'une marque, des franchises, des associations, une agence qui gère ses "
            "clients sur un gabarit, les comptes secondaires d'une même personne ou un avatar générique donnent des "
            "profils semblables. Seule une empreinte d'image est comparée : aucune comparaison de visages.")


def empreinte_photo(octets: bytes) -> int:
    """Empreinte dHash de 64 bits d'une image (Pillow). À calculer à la collecte : l'adresse de l'image expire."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(octets)) as im:
        gris = im.convert("L").resize((9, 8))
        px = list(gris.get_flattened_data() if hasattr(gris, "get_flattened_data") else gris.getdata())
    bits = 0
    for ligne in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[ligne * 9 + col] > px[ligne * 9 + col + 1] else 0)
    return bits


def distance_hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _moule_pseudo(pseudo: str) -> str | None:
    p = pseudo.lower()
    if re.fullmatch(r"user\d{10,14}", p):
        return "user#"
    m = re.fullmatch(r"([a-z]{5})[a-z]*\d+", p)
    return m.group(1) if m else None


def profils_jumeaux(videos: Sequence[dict], empreintes_photos: dict[str, int] | None = None) -> list[SignalFaible]:
    """D8 : des profils qui partagent au moins deux traits (bio, nom affiché, photo, moule de pseudo)."""
    profils = _profils(_dedoublonner(v for v in videos if _compte(v)))
    cles = list(profils)
    traits_de: dict[tuple[str, str], set[str]] = {}
    bios = {c: _norm(i["bio"] or "") for c, i in profils.items()}
    noms = {c: _norm(i["nom"] or "") for c, i in profils.items()}
    moules = {c: _moule_pseudo(i["pseudo"]) for c, i in profils.items()}
    for a, b in itertools.combinations(cles, 2):
        t = set()
        if len(bios[a].split()) >= 5 and bios[a] == bios[b]:
            t.add("bio")
        if len(noms[a]) >= 3 and noms[a] == noms[b]:
            t.add("nom")
        pa, pb = (empreintes_photos or {}).get(a), (empreintes_photos or {}).get(b)
        if pa is not None and pb is not None and distance_hamming(pa, pb) <= 8:
            t.add("photo")
        if moules[a] and moules[a] == moules[b]:
            t.add("pseudo")
        if len(t) >= 2:
            traits_de[(a, b)] = t
    union = _Union()
    for (a, b) in traits_de:
        union.unir(a, b)
    signaux = []
    libelles = {"photo": "la même photo", "bio": "la même biographie", "nom": "le même nom affiché",
                "pseudo": "des pseudos sur un même moule"}
    for compo in union.composantes():
        if len(compo) < 3:
            continue
        aretes = {k: t for k, t in traits_de.items() if k[0] in compo}
        vus = set().union(*aretes.values())
        communs = set.intersection(*aretes.values()) if aretes else set()
        force = "moyenne" if (len(compo) >= 5 or len(communs) >= 3) else "faible"
        liste = [t for t in ("photo", "bio", "nom", "pseudo") if t in (communs or vus)]
        if len(liste) <= 2:
            titre = f"{len(compo)} profils partagent " + " et ".join(libelles[t] for t in liste)
        else:
            titre = f"{len(compo)} profils partagent {len(liste)} traits en commun"
        ordre = sorted(compo, key=lambda c: profils[c]["premiere"] or datetime.max.replace(tzinfo=UTC))
        preuves = []
        for r, c in enumerate(ordre):
            i = profils[c]
            vus_c = sorted(set().union(*[t for k, t in aretes.items() if c in k]))
            preuves.append(_preuve(i["videos"][0], f"traits partagés : {', '.join(vus_c)} · nom affiché "
                                                   f"« {i['nom'] or '?'} » · bio « {_tr(i['bio'] or '', 80)} »",
                                   horloge=(r == 0)))
        signaux.append(_signal(
            "profils_jumeaux", titre, "Des comptes sortis d'un même atelier ou d'un même modèle : à vérifier.",
            _EXPL_D8, force, profils[ordre[0]]["premiere"], preuves,
            "Qui a créé ces profils sur le même modèle ?",
            ("Chercher la provenance de la photo · recherche inversée · gratuit",
             "Comparer leurs liens en bio · pivots · gratuit",
             "Comparer leurs anciennetés estimées (D5) · fiche du signal · gratuit"),
            mesures=(("profils", len(compo)), ("traits", tuple(sorted(vus))))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# D9 · Publications rapprochées et répétées
# ---------------------------------------------------------------------------

_EXPL_D9 = ("Des réactions au même direct, à un journal télévisé, à un match ou à une annonce tombent à la même "
            "minute ; une rédaction ou une marque qui programme ses posts, ou un embargo commun, donnent le même "
            "calendrier sans aucune consigne partagée.")


def _coactions(par_compte_groupe: dict[tuple[str, str], list[datetime]], a: str, b: str, groupes: Iterable[str]) -> int:
    """Co-actions distinctes d'une paire : à 5 min d'écart au plus, groupes différents ou 1 h d'écart au moins."""
    evenements = []
    for g in groupes:
        for da in par_compte_groupe.get((a, g), ()):
            for db in par_compte_groupe.get((b, g), ()):
                if abs((da - db).total_seconds()) <= 300:
                    evenements.append((min(da, db), g))
    evenements.sort()
    comptes: list[tuple[datetime, str]] = []
    for d, g in evenements:
        if all(not (g2 == g and abs((d - d2).total_seconds()) < 3600) for d2, g2 in comptes):
            comptes.append((d, g))
    return len(comptes)


def publications_rapprochees(videos: Sequence[dict], groupes: dict[str, Sequence[dict]] | None = None,
                             tirages: int = 1000, graine: int = 2027) -> list[SignalFaible]:
    """D9 : des paires de comptes qui publient à moins de 5 minutes l'une de l'autre, plusieurs fois.

    Modèle nul : les heures de chaque compte sont rebattues dans ses propres journées (`tirages` fois).
    Les groupes par défaut sont les hashtags non banals. Plafond « moyenne », et le mot de coordination
    reste interdit tant que le type n'est pas calibré.
    """
    videos = _dedoublonner(v for v in videos if _compte(v) and _date_sure(v))
    if groupes is None:
        par_tag: dict[str, list[dict]] = defaultdict(list)
        for v in videos:
            for t in {str(t).strip().lstrip("#").casefold() for t in (v.get("hashtags") or []) if isinstance(t, str)}:
                if t:
                    par_tag[t].append(v)
        total = len({_compte(v) for v in videos})
        groupes = {f"hashtag:{t}": vs for t, vs in par_tag.items()
                   if len({_compte(v) for v in vs}) >= 2 and not _pv.est_banal(
                       _pv.HASHTAG, t, len({_compte(v) for v in vs}), total)}
    par_cg: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    video_de: dict[tuple[str, str, datetime], dict] = {}
    for g, vs in groupes.items():
        for v in vs:
            c, d = _compte(v), _date_sure(v)
            if c and d:
                par_cg[(c, g)].append(d)
                video_de[(c, g, d)] = v
    membres: dict[str, set[str]] = defaultdict(set)
    for (c, g) in par_cg:
        membres[g].add(c)
    paires: set[tuple[str, str]] = set()
    groupes_de: dict[str, set[str]] = defaultdict(set)
    for g, cs in membres.items():
        paires.update(itertools.combinations(sorted(cs), 2))
        for c in cs:
            groupes_de[c].add(g)
    candidats = []
    for (a, b) in paires:
        gs = sorted(groupes_de[a] & groupes_de[b])
        r = _coactions(par_cg, a, b, gs)
        if r >= 3:
            candidats.append((a, b, gs, r))
    if not candidats:
        return []
    jours_de: dict[str, list[datetime]] = defaultdict(list)
    for v in videos:
        jours_de[_compte(v)].append(_date_sure(v))
    rng = random.Random(graine)

    def r_tirage(a: str, b: str, gs: list[str]) -> int:
        melange: dict[str, dict[datetime, datetime]] = {}
        for c in (a, b):
            ds = jours_de[c]
            heures = [d - d.replace(hour=0, minute=0, second=0, microsecond=0) for d in ds]
            rng.shuffle(heures)
            melange[c] = {d: d.replace(hour=0, minute=0, second=0, microsecond=0) + h for d, h in zip(ds, heures)}
        nouveau: dict[tuple[str, str], list[datetime]] = {}
        for c in (a, b):
            for g in gs:
                nouveau[(c, g)] = [melange[c].get(d, d) for d in par_cg.get((c, g), ())]
        return _coactions(nouveau, a, b, gs)

    significatives = []
    for a, b, gs, r in candidats:
        meilleurs = sum(1 for _ in range(tirages) if r_tirage(a, b, gs) >= r)
        p = (meilleurs + 1) / (tirages + 1)
        if p < 0.01:
            significatives.append((a, b, gs, r, p))
    if not significatives:
        return []
    union = _Union()
    for a, b, *_ in significatives:
        union.unir(a, b)
    signaux = []
    for compo in union.composantes():
        sel = [s for s in significatives if s[0] in compo]
        aretes = {(s[0], s[1]) for s in sel} | {(s[1], s[0]) for s in sel}
        triangle = len(compo) >= 3 and all((x, y) in aretes for x, y in itertools.combinations(sorted(compo), 2))
        if len(compo) >= 3 and not triangle:
            continue  # chaîne de paires sans triangle : pas assez pour un seul signal
        force = "moyenne" if triangle else "faible"
        r_min = min(s[3] for s in sel)
        p_max = max(s[4] for s in sel)
        gs = sorted({g for s in sel for g in s[2]})
        evenements = []
        for (c, g), ds in par_cg.items():
            if c in compo and g in gs:
                for d in ds:
                    evenements.append((d, c, g))
        evenements.sort()
        preuves, vus = [], set()
        for d, c, g in evenements:
            v = video_de[(c, g, d)]
            if (_vid(v), c) in vus:
                continue
            vus.add((_vid(v), c))
            extrait = f"publiée le {d:%Y-%m-%d %H:%M:%S} UTC (groupe {g})"
            if not preuves:
                extrait += " [horloge : identifiant décodé, ± 18 s TikTok ou ± 2 s photo Instagram]"
            preuves.append(_preuve(v, extrait))
        signaux.append(_signal(
            "publications_rapprochees", f"{len(compo)} comptes publient à moins de 5 minutes d'écart, {r_min} fois",
            "Un calendrier commun (consigne, outil de programmation, même équipe) : à vérifier.", _EXPL_D9, force,
            evenements[0][0], preuves,
            "Ces publications suivent-elles un calendrier commun, fixé par qui ?",
            ("Caler les heures sur les événements publics · presse, GDELT · gratuit",
             "Chercher une agence ou un partenariat commun · bio, mentions légales · gratuit",
             "Interroger les comptes · comptes publics · gratuit"),
            mesures=(("comptes", len(compo)), ("coactions_min_par_paire", r_min), ("p_max", round(p_max, 4)),
                     ("tirages", tirages))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# D10 · Sujet qui monte sous les radars
# ---------------------------------------------------------------------------

_EXPL_D10 = ("Une actualité, une émission, un défi, une mise en avant par l'algorithme ou un effet de calendrier "
             "font monter un sujet sans que personne ne l'ait poussé. L'index ne remonte qu'à 3 mois : la "
             "nouveauté ne se juge que sur cette période.")


def sujet_qui_monte(series: dict[str, dict], presse: dict[str, dict[str, int]] | None = None) -> list[SignalFaible]:
    """D10 : une rafale d'auteurs distincts, nouvelle ou qui change de plateforme, avec peu d'écho dans la presse.

    `series[nom]` : `{"auteurs": {plateforme: {"AAAA-MM-JJ": n}}, "langues": {"AAAA-MM-JJ": [langues]},
    "nouveaute": bool | None, "type": "phrase" | "hashtag" | "son", "comptes": [...], "url": str,
    "debut": "AAAA-MM-JJ"}` ; `debut` est le premier jour couvert par l'index (les jours sans auteur ne sont
    pas listés) : une rafale exige 28 jours observés avant elle.
    `presse[nom]` : `{"AAAA-MM-JJ": nombre d'articles}` (GDELT), ou absent quand l'écho n'est pas mesuré.
    """
    signaux = []
    for nom, s in (series or {}).items():
        par_jour: dict[str, int] = defaultdict(int)
        plateformes_jour: dict[str, set[str]] = defaultdict(set)
        for pf, jours_pf in (s.get("auteurs") or {}).items():
            for j, n in jours_pf.items():
                par_jour[j] += n
                if n >= 1:
                    plateformes_jour[j].add(pf)
        if not par_jour:
            continue
        # Une rafale se juge sur 28 jours d'avant observés : sans `debut`, la série commence à son premier jour.
        debut = datetime.fromisoformat(s.get("debut") or min(par_jour))
        rafale = None
        for j in sorted(par_jour):
            d = datetime.fromisoformat(j)
            if d - timedelta(days=28) < debut:
                continue  # historique trop court pour fixer le rythme habituel

            avant = [par_jour.get((d - timedelta(days=k)).date().isoformat(), 0) for k in range(1, 29)]
            lam = max(sum(avant) / 28, 0.5)
            x = par_jour[j]
            if x >= 8 and _queue_poisson(x, lam) < 0.001:
                rafale = (j, x, lam, avant)
                break
        if rafale is None:
            continue
        j0, x, lam, avant = rafale
        d0 = datetime.fromisoformat(j0)
        suite = [par_jour.get((d0 + timedelta(days=k)).date().isoformat(), 0) for k in range(0, 14)]
        duree = 0
        for n in suite:
            if n >= max(8, 2 * lam):
                duree += 1
            else:
                break
        duree = max(duree, 1)
        facteur = (sum(suite[:duree]) / duree) / lam
        pf_avant = {pf for k in range(1, 29)
                    for pf in plateformes_jour.get((d0 - timedelta(days=k)).date().isoformat(), ())}
        pf_jour = {pf for pf, jj in (s.get("auteurs") or {}).items() if jj.get(j0, 0) >= 3}
        langues = s.get("langues") or {}
        lg_avant = {lg for k in range(1, 29) for lg in langues.get((d0 - timedelta(days=k)).date().isoformat(), ())}
        lg_jour = set(langues.get(j0, ()))
        franchissement = (len(pf_jour) >= 2 and len(pf_avant) <= 1) or (len(lg_jour) >= 2 and len(lg_avant) <= 1)
        nouveaute = bool(s.get("nouveaute")) if s.get("nouveaute") is not None else sum(avant) == 0
        articles = None
        if presse is not None and presse.get(nom) is not None:
            articles = sum(n for j, n in presse[nom].items()
                           if d0 <= datetime.fromisoformat(j) <= d0 + timedelta(days=duree))
            if articles > 3:
                continue  # déjà visible dans la presse : pas de signal
        force = "faible"
        if nouveaute or franchissement:
            force = "moyenne"
        if franchissement and articles is not None:
            force = "forte"
        if articles is None:
            titre = f"Rythme multiplié par {_fr(facteur, 0)} en {duree} jours"
        else:
            titre = (f"Rythme multiplié par {_fr(facteur, 0)} en {duree} jours ; la presse "
                     + ("n'en parle pas" if articles == 0 else "en parle peu"))
        piece = f"{s.get('type', 'phrase')}:{nom}"
        url = s.get("url") or URL_ORIANE
        echo = f" ; {articles} article(s) de presse sur la période" if articles is not None else " ; écho de presse non mesuré"
        preuves = [PreuveSignal(piece=piece, url=url, compte=None, extrait=_tr(
            f"{x} auteurs distincts le {j0} contre {lam:.1f} par jour en moyenne sur les 28 jours précédents "
            f"(loi de Poisson, P < 0,001){echo}"))]
        for c in s.get("comptes") or []:
            preuves.append(PreuveSignal(piece=piece, url=url, compte=c, extrait="parmi les premiers auteurs de la rafale"))
        signaux.append(_signal(
            "sujet_qui_monte", titre,
            "Un sujet qui sort de sa niche avant d'être couvert (angle mort, ou poussée organisée) : à vérifier.",
            _EXPL_D10, force, d0.replace(tzinfo=UTC), preuves,
            f"Qu'est-ce qui a déclenché la montée du {j0} ?",
            ("Établir la chronologie de la veille · événements publics, GDELT · gratuit",
             "Regarder les premiers auteurs de la rafale (D1) · fiche du signal · gratuit",
             "Mesurer la part de comptes neufs dans la rafale (D5) · fiche du signal · gratuit"),
            mesures=(("jour", j0), ("auteurs", x), ("lambda", round(lam, 2)), ("facteur", round(facteur, 1)),
                     ("duree_j", duree), ("nouveaute", nouveaute), ("franchissement", franchissement),
                     ("articles", articles))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# Pivots : lecture commune à D11 à D15
# ---------------------------------------------------------------------------

def _indice(p: Any, source: str) -> Any | None:
    return next((i for i in getattr(p, "indices", []) or [] if i.source == source), None)


def _premiere_citation(p: Any, par_id: dict[str, dict]) -> tuple[datetime, Any, dict] | None:
    """La plus ancienne vidéo qui cite le pivot : (date, preuve du pivot, vidéo)."""
    meilleur = None
    for pp in p.preuves:
        v = par_id.get(str(pp.contenu_id)) if pp.contenu_id else None
        d = _date(v) if v else None
        if d is not None and (meilleur is None or d < meilleur[0]):
            meilleur = (d, pp, v)
    return meilleur


def _preuves_pivot(p: Any, par_id: dict[str, dict], premiere: tuple[datetime, Any, dict] | None,
                   piece: str) -> list[PreuveSignal]:
    """Vidéo de la première citation (horloge nommée), puis une preuve du pivot par compte qui le cite."""
    preuves: list[PreuveSignal] = []
    if premiere is not None:
        d, pp, v = premiere
        preuves.append(_preuve(v, f"{p.type} « {p.valeur} » cité : {pp.extrait}", pp.debut_s, horloge=True))
    vus: set[str] = set()
    for pp in sorted(p.preuves, key=lambda x: (_date(par_id[str(x.contenu_id)]) if x.contenu_id and str(x.contenu_id) in par_id
                                               else datetime.max.replace(tzinfo=UTC))):
        if pp.compte in vus:
            continue
        vus.add(pp.compte)
        preuves.append(PreuveSignal(piece=piece, url=pp.url or URL_ORIANE, extrait=_tr(pp.extrait), seconde=pp.debut_s,
                                    compte=pp.compte))
    return preuves


def _preuve_indice(i: Any, piece: str) -> PreuveSignal:
    return PreuveSignal(piece=piece, url=i.url, extrait=_tr(i.extrait or i.resume, 260), source=i.source,
                        lu_le=i.date_lecture)


# ---------------------------------------------------------------------------
# D11 · Domaine récent
# ---------------------------------------------------------------------------

_EXPL_D11 = ("Le lancement d'une marque, d'une boutique ou d'un média, le nouveau nom d'une structure ancienne, un "
             "domaine pris pour un jeu concours, un transfert pour une question de prix ou une marque qui dépose ses "
             "variantes d'un coup donnent des domaines récents. Un domaine peut aussi être réservé des mois à l'avance.")


def _domaine_heberge(valeur: str) -> bool:
    return any(valeur == h or valeur.endswith("." + h) for h in _pv._HEBERGES)


def domaine_recent(pivots: Sequence[Any], videos: Sequence[dict]) -> list[SignalFaible]:
    """D11 : un site enregistré peu avant la première vidéo qui le cite, et les domaines enregistrés en lot."""
    par_id = _par_id(videos)
    infos = []
    for p in pivots:
        if p.type != _pv.DOMAINE:
            continue
        premiere = _premiere_citation(p, par_id)
        rdap, crt = _indice(p, "rdap"), _indice(p, "crt.sh")
        if premiere is None:
            continue
        if _domaine_heberge(p.valeur):
            creation, source = (_lire_iso(crt.donnees.get("premier_certificat")) if crt else None), crt
        else:
            creation, source = (_lire_iso(rdap.donnees.get("creation")) if rdap else None), rdap
        if creation is None:
            continue
        evenements = (rdap.donnees.get("evenements") or {}) if rdap else {}
        transferts = [t for k in ("transfer", "reregistration") if (t := _lire_iso(evenements.get(k)))]
        infos.append({"p": p, "premiere": premiere, "creation": creation, "source": source,
                      "age": (premiere[0] - creation).days if premiere[0] >= creation else None,
                      "registrar": (rdap.donnees.get("registrar") if rdap else None),
                      "transfert": max(transferts) if transferts else None,
                      "freres": set((crt.donnees.get("freres") or [])) if crt else set()})
    recents = [x for x in infos if x["age"] is not None and x["age"] <= 180]

    # Variante lot : créés à 48 h d'écart chez le même registrar, ou portés par un même certificat.
    lots: list[tuple[list[dict], str]] = []
    par_registrar: dict[str, list[dict]] = defaultdict(list)
    for x in recents:
        if x["registrar"]:
            par_registrar[x["registrar"]].append(x)
    for xs in par_registrar.values():
        xs.sort(key=lambda x: x["creation"])
        for i, depart in enumerate(xs):
            lot = [x for x in xs[i:] if x["creation"] - depart["creation"] <= timedelta(hours=48)]
            if len(lot) >= 2 and not any({id(y) for y in lot} <= {id(y) for y in l} for l, _ in lots):
                lots.append((lot, "créés à moins de 48 heures d'écart"))
    union = _Union()
    valeurs = {x["p"].valeur: x for x in recents}
    for x in recents:
        union.trouver(x["p"].valeur)
        if 0 < len(x["freres"]) <= 20:  # au-delà : certificat mutualisé d'un CDN, pas un lot
            for frere in x["freres"]:
                if frere in valeurs:
                    union.unir(x["p"].valeur, frere)
    for compo in union.composantes():
        if len(compo) >= 2:
            lots.append(([valeurs[v] for v in sorted(compo)], "portés par un même certificat"))
    dans_lot = {x["p"].valeur: len(lot) for lot, _ in lots for x in lot}

    signaux = []
    tranche_31_180 = [x for x in recents if x["age"] >= 31]
    for x in infos:
        p, (d, pp, v), age = x["p"], x["premiere"], x["age"]
        piece = f"domaine:{p.valeur}"
        force, titre = None, None
        if age is not None and age <= 30:
            force, titre = "forte", f"Site enregistré {age} jours avant la première vidéo qui le cite"
        elif age is not None and age <= 180:
            if len(tranche_31_180) >= 3 or dans_lot.get(p.valeur, 0) >= 3:
                force = "moyenne"
            else:
                force = "faible"
            titre = f"Site enregistré {age} jours avant la première vidéo qui le cite"
        if x["transfert"] is not None and 0 <= (d - x["transfert"]).days < 90 and force is None:
            force = "faible"
            titre = f"Site transféré {(d - x['transfert']).days} jours avant la première vidéo qui le cite"
        if force is None:
            continue
        preuves = _preuves_pivot(p, par_id, x["premiere"], piece)
        if x["source"] is not None:
            preuves.append(_preuve_indice(x["source"], piece))
        signaux.append(_signal(
            "domaine_recent", titre,
            "Une structure montée pour l'occasion, ou un site neuf qui se dit établi : à vérifier.", _EXPL_D11, force,
            d, preuves, "Qui a enregistré ce site, et pour quoi faire ?",
            ("Lire les mentions légales et les CGV jusqu'au SIREN (D13) · Wayback · gratuit",
             "Regarder les domaines frères · crt.sh · gratuit",
             "Chercher les identifiants de suivi (D12) · code des pages · gratuit"),
            mesures=(("age_jours", age), ("creation", x["creation"].isoformat()), ("registrar", x["registrar"]))))
    for lot, motif in lots:
        if len({x["p"].valeur for x in lot}) < 2:
            continue
        force = "moyenne" if len(lot) >= 3 else "faible"
        preuves = []
        for x in sorted(lot, key=lambda y: y["creation"]):
            piece = f"domaine:{x['p'].valeur}"
            preuves.extend(_preuves_pivot(x["p"], par_id, x["premiere"], piece))
            if x["source"] is not None:
                preuves.append(_preuve_indice(x["source"], piece))
        premiere = min(x["premiere"][0] for x in lot)
        signaux.append(_signal(
            "domaine_recent", f"{len(lot)} domaines du dossier {motif}",
            "Un lot de sites enregistré d'un coup par une même main : à vérifier.", _EXPL_D11, force, premiere,
            preuves, "Qui a enregistré ces sites, et pour quoi faire ?",
            ("Lire les mentions légales et les CGV · Wayback · gratuit",
             "Regarder les domaines frères · crt.sh · gratuit",
             "Chercher les identifiants de suivi (D12) · code des pages · gratuit"),
            mesures=(("domaines", len(lot)), ("motif", motif))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# D12 · Même identifiant de suivi
# ---------------------------------------------------------------------------

_EXPL_D12 = ("Une même agence ou un même développeur, un modèle de site copié avec son identifiant, une régie "
             "commune ou des sites d'un même groupe déclaré partagent un identifiant. Un identifiant commun suggère "
             "un lien, il ne prouve pas un contrôle commun.")

_ID_SUIVI = (
    ("AdSense", re.compile(r"\bca-pub-\d{16}\b")),
    ("Google Analytics", re.compile(r"\bUA-(\d{4,10})-\d+\b")),
    ("GA4", re.compile(r"\bG-(?=[A-Z0-9]*\d)[A-Z0-9]{6,12}\b")),
    ("GTM", re.compile(r"\bGTM-[A-Z0-9]{4,9}\b")),
    ("pixel Meta", re.compile(r"""fbq\(\s*['"]init['"]\s*,\s*['"](\d{8,18})['"]""")),
    ("pixel TikTok", re.compile(r"""ttq\.load\(\s*['"]([A-Z0-9]{10,24})['"]""")),
)


def identifiants_de_suivi(html: str) -> set[tuple[str, str]]:
    """(type, valeur) des identifiants de suivi lus dans le code d'une page (Analytics : numéro de base)."""
    trouves: set[tuple[str, str]] = set()
    for type_, motif in _ID_SUIVI:
        for m in motif.finditer(html or ""):
            if type_ == "Google Analytics":
                trouves.add((type_, "UA-" + m.group(1)))
            elif type_ in ("pixel Meta", "pixel TikTok"):
                trouves.add((type_, m.group(1)))
            else:
                trouves.add((type_, m.group(0)))
    return trouves


def _lectures_pages(entree: Any) -> list[tuple[datetime | None, str]]:
    if isinstance(entree, str):
        return [(None, entree)]
    sortie = []
    for e in entree or []:
        if isinstance(e, str):
            sortie.append((None, e))
        elif isinstance(e, dict):
            sortie.append((_lire_iso(e.get("date")), str(e.get("html") or "")))
        else:
            sortie.append((_lire_iso(e[0]), str(e[1])))
    return sortie


def identifiant_de_suivi_partage(pages: dict[str, Any], pivots: Sequence[Any]) -> list[SignalFaible]:
    """D12 : des sites présentés comme distincts qui portent le même identifiant de suivi.

    `pages[domaine]` : le code HTML de la page actuelle et d'au plus 3 captures Wayback antérieures à la
    date de gel, `[(date | None, html), ...]`.
    """
    pivot_de = {p.valeur: p for p in pivots if p.type == _pv.DOMAINE}
    vus: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for domaine, entree in (pages or {}).items():
        for date, html in _lectures_pages(entree):
            for ident in identifiants_de_suivi(html):
                s = vus[ident].setdefault(domaine, {"premiere": date, "derniere": date})
                if date is not None:
                    s["premiere"] = date if s["premiere"] is None else min(s["premiere"], date)
                    s["derniere"] = date if s["derniere"] is None else max(s["derniere"], date)
    partages = {i: sites for i, sites in vus.items() if len(sites) >= 2}
    if not partages:
        return []
    union = _Union()
    for ident, sites in partages.items():
        sites_l = sorted(sites)
        for s in sites_l[1:]:
            union.unir(sites_l[0], s)
    signaux = []
    for compo in union.composantes():
        idents = {i: s for i, s in partages.items() if set(s) <= compo}
        types = sorted({t for t, _ in idents})
        rd = {d: _indice(pivot_de[d], "rdap") for d in compo if d in pivot_de}
        registrars = {r.donnees.get("registrar") for r in rd.values() if r}
        creations = [c for r in rd.values() if r and (c := _lire_iso(r.donnees.get("creation")))]
        meme_registre = (len(registrars) == 1 and None not in registrars and len(creations) == len(compo)
                         and (max(creations) - min(creations)) <= timedelta(hours=48))
        force = "moyenne" if (len(types) >= 2 or meme_registre) else "faible"
        titre = (f"{len(compo)} sites présentés comme distincts partagent le même identifiant {types[0]}"
                 if len(types) == 1 else f"{len(compo)} sites présentés comme distincts partagent {len(types)} types d'identifiants")
        preuves: list[PreuveSignal] = []
        dates = []
        for domaine in sorted(compo):
            piece = f"domaine:{domaine}"
            liste = sorted(f"{t} {v}" for (t, v), s in idents.items() if domaine in s)
            for (t, v), s in idents.items():
                if domaine in s and s[domaine]["premiere"]:
                    dates.append(s[domaine]["premiere"])
            preuves.append(PreuveSignal(piece=piece, url=f"https://{domaine}", source="code de la page",
                                        extrait=_tr("identifiants communs lus dans la page : " + " ; ".join(liste))))
            p = pivot_de.get(domaine)
            if p is not None:
                vus_c: set[str] = set()
                for pp in p.preuves:
                    if pp.compte not in vus_c:
                        vus_c.add(pp.compte)
                        preuves.append(PreuveSignal(piece=piece, url=pp.url or URL_ORIANE, extrait=_tr(pp.extrait),
                                                    seconde=pp.debut_s, compte=pp.compte))
        signaux.append(_signal(
            "identifiant_de_suivi_partage", titre,
            "Une même main technique, au moins une même agence : à vérifier.", _EXPL_D12, force,
            min(dates) if dates else None, preuves, "Qui gère l'identifiant commun à ces sites ?",
            ("Lire les mentions légales de chaque site · pages · gratuit",
             "Dater l'apparition de l'identifiant · Wayback · gratuit",
             "Écrire à l'agence créditée en pied de page · contact public · gratuit"),
            mesures=(("sites", len(compo)), ("types", tuple(types)), ("identifiants", len(idents)),
                     ("meme_registrar_et_creations_48h", meme_registre))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# D13 · Société immatriculée juste avant
# ---------------------------------------------------------------------------

_EXPL_D13 = ("Une jeune entreprise, un nouveau projet, une restructuration avec un nouveau SIREN ou un "
             "auto-entrepreneur donnent une immatriculation récente. Une adresse de domiciliation ne dit rien de "
             "l'activité réelle.")

_RE_DOMICILIATION = re.compile(r"(?i)domiciliation|centre d['’ ]affaires|business cent")


def _empreinte_dirigeant(d: dict) -> str | None:
    if d.get("siren"):
        return "morale:" + str(d["siren"])
    if d.get("nom") and d.get("prenoms"):  # personne physique lue en interne seulement
        brut = f"{_norm(d['nom'])}|{_norm(d['prenoms'])}|{d.get('annee_de_naissance') or ''}"
        return "physique:" + hashlib.sha1(brut.encode()).hexdigest()[:10]
    return None


def societe_recente(pivots: Sequence[Any], videos: Sequence[dict]) -> list[SignalFaible]:
    """D13 : une société immatriculée peu avant la première vidéo qui la cite, et le registre partagé."""
    par_id = _par_id(videos)
    socs: dict[str, dict] = {}
    for p in pivots:
        if p.type not in (_pv.SIREN, _pv.SIRET):
            continue
        ind = _indice(p, "recherche-entreprises")
        if ind is None:
            continue
        don = ind.donnees
        creation = _lire_iso(don.get("date_creation"))
        siren = str(don.get("siren") or p.valeur[:9])
        if creation is None:
            continue
        premiere = _premiere_citation(p, par_id)
        s = socs.setdefault(siren, {"siren": siren, "creation": creation, "indice": ind, "pivots": [],
                                    "premiere": None, "don": don})
        s["pivots"].append(p)
        if premiere is not None and (s["premiere"] is None or premiere[0] < s["premiere"][0]):
            s["premiere"] = premiere
    for s in socs.values():
        siege = s["don"].get("siege") or {}
        adresse = _norm(f"{siege.get('adresse') or ''} {siege.get('code_postal') or ''}")
        s["adresse"] = adresse or None
        s["domiciliation"] = bool(_RE_DOMICILIATION.search(str(siege.get("adresse") or "")))
        s["dirigeants"] = {e for d in s["don"].get("dirigeants") or [] if (e := _empreinte_dirigeant(d))}

    # Registre partagé : même siège ou même dirigeant, créations à 30 jours d'écart au plus.
    union = _Union()
    traits: dict[tuple[str, str], set[str]] = {}
    for a, b in itertools.combinations(sorted(socs), 2):
        sa, sb = socs[a], socs[b]
        if abs((sa["creation"] - sb["creation"]).days) > 30:
            continue
        t = set()
        if sa["adresse"] and sa["adresse"] == sb["adresse"]:
            t.add("siège domicilié" if sa["domiciliation"] else "siège")
        if sa["dirigeants"] & sb["dirigeants"]:
            t.add("dirigeant")
        if t:
            traits[(a, b)] = t
            union.unir(a, b)
    groupes = []
    for compo in union.composantes():
        ts = set().union(*[t for k, t in traits.items() if k[0] in compo])
        seulement_domicilie = ts == {"siège domicilié"}
        if len(compo) >= 3:
            force = "faible" if seulement_domicilie else "moyenne"
        elif seulement_domicilie:
            continue
        else:
            force = "faible"
        groupes.append((sorted(compo), force, ts))
    en_registre = {s for g, _, _ in groupes for s in g}

    signaux = []
    for s in socs.values():
        if s["premiere"] is None:
            continue
        d, pp, v = s["premiere"]
        ecart = (d - s["creation"]).days
        if ecart < 0:
            continue
        force = None
        if ecart <= 90:
            force = "forte"
        elif ecart <= 365 and s["siren"] in en_registre:
            force = "moyenne"
        if force is None:
            continue
        if s["domiciliation"]:
            force = {"forte": "moyenne", "moyenne": "faible"}[force]
        piece = f"siren:{s['siren']}"
        preuves = _preuves_pivot(s["pivots"][0], par_id, s["premiere"], piece) + [_preuve_indice(s["indice"], piece)]
        signaux.append(_signal(
            "societe_recente", f"Société immatriculée {ecart} jours avant la première vidéo qui la cite",
            "Une structure créée pour cette activité ; reste à savoir qui dirige et qui encaisse : à vérifier.",
            _EXPL_D13, force, d, preuves, "Qui a créé cette société, et pourquoi à ce moment ?",
            ("Lire les annonces légales · BODACC · gratuit",
             "Retrouver les autres sociétés du même dirigeant · registre (personne morale seulement) · gratuit",
             "Droit de réponse au dirigeant · courrier · gratuit"),
            mesures=(("ecart_jours", ecart), ("creation", s["creation"].date().isoformat()),
                     ("domiciliation", s["domiciliation"]))))
    for membres, force, ts in groupes:
        preuves = []
        for siren in membres:
            s = socs[siren]
            piece = f"siren:{siren}"
            if s["premiere"] is not None:
                preuves.extend(_preuves_pivot(s["pivots"][0], par_id, s["premiere"], piece))
            else:
                preuves.extend(_preuves_pivot(s["pivots"][0], par_id, None, piece))
            preuves.append(_preuve_indice(s["indice"], piece))
        premiere = min((socs[m]["premiere"][0] for m in membres if socs[m]["premiere"]), default=None)
        signaux.append(_signal(
            "societe_recente", f"{len(membres)} sociétés du dossier partagent un siège ou un dirigeant",
            "Des structures créées ensemble par une même main : à vérifier.", _EXPL_D13, force, premiere, preuves,
            "Qui a créé ces sociétés, et pourquoi à ce moment ?",
            ("Lire les annonces légales · BODACC · gratuit",
             "Retrouver les autres sociétés du même dirigeant · registre (personne morale seulement) · gratuit",
             "Droit de réponse aux dirigeants · courrier · gratuit"),
            mesures=(("societes", len(membres)), ("traits", tuple(sorted(ts))))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# D14 · Même code promo chez plusieurs créateurs
# ---------------------------------------------------------------------------

_EXPL_D14 = ("Un programme d'affiliation ouvert, le code public d'une marque établie ou des fans qui relaient un "
             "code donnent le même code chez plusieurs créateurs. Un code propre à chaque créateur est la façon "
             "normale de suivre ses ventes. La part de vidéos sans mention lisible est un constat, jamais une "
             "infraction : l'étiquette de la plateforme n'est pas lue.")

_CODES_GENERIQUES = re.compile(
    r"(?i)^(BIENVENUE|WELCOME|NOEL|SOLDES|BLACKFRIDAY|BLACKFRIDAY\d*|BF\d*|PROMO\d*|CODE\d*|HELLO|MERCI|NEW|FIRST|"
    r"NEWSLETTER|PRINTEMPS|ETE|HIVER|RENTREE|FRIDAY|CYBERMONDAY|FETES|CADEAU)\d{0,3}$")
_MENTIONS = re.compile(r"publicite|collaboration commerciale|partenariat|sponsoris|\bsponsored\b|\bpaid partnership\b")
_MENTION_TAG = re.compile(r"(?i)#(ad|pub|sponso\w*|collab\w*)\b")


def _a_mention(v: dict) -> bool:
    textes = [v.get("title") or "", v.get("caption") or ""]
    for m in v.get("media") or []:
        textes.append(m.get("caption") or "")
        textes.append(m.get("transcript") or "")
        textes.extend(t for t in m.get("onScreenText") or [] if isinstance(t, str))
    brut = " ".join(str(t) for t in textes)
    return bool(_MENTION_TAG.search(brut) or _MENTIONS.search(_norm(brut)))


def code_promo_en_serie(pivots: Sequence[Any], videos: Sequence[dict]) -> list[SignalFaible]:
    """D14 : le même code promo chez plusieurs créateurs dans la semaine qui suit sa première apparition trouvée."""
    par_id = _par_id(videos)
    signaux = []
    for p in pivots:
        if p.type != _pv.CODE_PROMO or _CODES_GENERIQUES.match(p.valeur):
            continue
        citations = []
        for pp in p.preuves:
            v = par_id.get(str(pp.contenu_id)) if pp.contenu_id else None
            d = _date(v) if v else None
            if d is not None:
                citations.append((d, pp, v))
        if not citations:
            continue
        citations.sort(key=lambda x: x[0])
        debut = citations[0][0]
        fenetre = [c for c in citations if c[0] - debut <= timedelta(days=7)]
        par_compte: dict[str, tuple] = {}
        for c in fenetre:
            par_compte.setdefault(c[1].compte, c)
        n = len(par_compte)
        if n < 3:
            continue
        force = "forte" if n >= 10 else "moyenne" if n >= 5 else "faible"
        premiers = sorted(par_compte.values(), key=lambda x: x[0])
        jours = max(1, math.ceil((premiers[-1][0] - debut).total_seconds() / 86400))
        videos_fenetre = _dedoublonner(c[2] for c in fenetre)
        sans = sum(1 for v in videos_fenetre if not _a_mention(v))
        piece = f"code_promo:{p.valeur}"
        preuves = []
        for r, (d, pp, v) in enumerate(premiers):
            preuves.append(_preuve(v, f"code « {p.valeur} » : {pp.extrait}", pp.debut_s, horloge=(r == 0)))
            preuves.append(PreuveSignal(piece=piece, url=pp.url or _url(v), extrait=_tr(pp.extrait), seconde=pp.debut_s,
                                        compte=pp.compte))
        signaux.append(_signal(
            "code_promo_en_serie", f"Le même code promo chez {n} créateurs en {jours} jours",
            "Une campagne commerciale commune, payée par un marchand : à vérifier.", _EXPL_D14, force, debut, preuves,
            "Quel marchand est derrière ce code, et que paie-t-il aux créateurs ?",
            ("Suivre le code jusqu'au site puis au SIREN (D11, D13) · pages publiques · gratuit",
             "Demander aux créateurs leur contrat · comptes publics · gratuit",
             "Vérifier l'étiquette « contenu commercial » · plateforme · gratuit"),
            mesures=(("comptes", n), ("jours", jours), ("videos", len(videos_fenetre)),
                     ("videos_sans_mention_lisible", sans), ("code", p.valeur))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# D15 · Portefeuille crypto neuf au moment de l'appel aux dons
# ---------------------------------------------------------------------------

_EXPL_D15 = ("Ouvrir une adresse neuve pour chaque collecte est une bonne pratique. Les plateformes d'échange et les "
             "mélangeurs coupent la chaîne, et deux dépôts sur une même plateforme ne font pas un même titulaire. "
             "Une adresse peut aussi figurer sur une liste de sanctions sans que la personne qui l'affiche le sache.")


def _donnees_releve(r: dict) -> dict:
    return r.get("donnees", r) if isinstance(r, dict) else {}


def portefeuille_neuf(pivots: Sequence[Any], videos: Sequence[dict], releves: dict[str, Any] | None) -> list[SignalFaible]:
    """D15 : une adresse de dons dont la première activité précède de peu le premier appel en vidéo.

    `releves` : `{"profils": {adresse: résultat de chaines.profil_portefeuille}, "groupes": [[adresse, ...]],
    "flux": [[a, b], ...]}` (ou directement `{adresse: profil}`).
    """
    par_id = _par_id(videos)
    releves = releves or {}
    profils = releves.get("profils", {k: v for k, v in releves.items() if k not in ("groupes", "flux")})
    en_lien: set[str] = set()
    liens: list[tuple[str, ...]] = []
    for g in releves.get("groupes") or []:
        g = g.get("adresses", g) if isinstance(g, dict) else g
        liens.append(tuple(g))
    for f in releves.get("flux") or []:
        liens.append((f.get("de"), f.get("vers")) if isinstance(f, dict) else tuple(f[:2]))
    signaux = []
    pivot_de = {p.valeur: p for p in pivots if p.type == _pv.PORTEFEUILLE and not p.banal}
    for lien in liens:
        membres = [a for a in lien if a in pivot_de]
        if len(set(membres)) >= 2:
            en_lien.update(membres)
    for adresse, p in pivot_de.items():
        r = profils.get(adresse)
        if r is None:
            continue
        don = _donnees_releve(r)
        premiere_activite = _lire_iso(don.get("premiere_activite"))
        ofac = (don.get("sanction_ofac") or {}).get("statut") == "oui"
        premiere = _premiere_citation(p, par_id)
        age = (premiere[0] - premiere_activite).days if premiere and premiere_activite and premiere[0] >= premiere_activite else None
        if ofac:
            force, titre = "forte", "Adresse de dons présente sur la liste OFAC"
        elif adresse in en_lien:
            force, titre = "moyenne", "Adresse de dons reliée à une autre adresse du dossier"
        elif age is not None and age <= 7:
            force, titre = "faible", f"Adresse de dons créée {age} jours avant le premier appel en vidéo"
        else:
            continue
        piece = f"portefeuille:{adresse}"
        resume = r.get("extrait") if isinstance(r, dict) and r.get("extrait") else (
            f"première activité {premiere_activite:%Y-%m-%d}" if premiere_activite else "relevé de l'adresse")
        preuves = _preuves_pivot(p, par_id, premiere, piece)
        preuves.append(PreuveSignal(
            piece=piece, url=(r.get("url_source") if isinstance(r, dict) else None) or URL_ORIANE, source="explorateur de chaîne",
            extrait=_tr(str(resume), 260), lu_le=_lire_iso(r.get("date_lecture")) if isinstance(r, dict) else None))
        signaux.append(_signal(
            "portefeuille_neuf", titre,
            "Une collecte montée pour l'occasion, ou liée à d'autres adresses du dossier : à vérifier.", _EXPL_D15,
            force, premiere[0] if premiere else None, preuves, "Qui contrôle cette adresse, et où partent les fonds ?",
            ("Suivre les premiers fonds, un saut · explorateur public · gratuit",
             "Interroger la plateforme d'échange destinataire (elle doit répondre sur réquisition) · gratuit",
             "Demander au compte qui gère la collecte · compte public · gratuit"),
            mesures=(("age_jours", age), ("ofac", ofac), ("reliee", adresse in en_lien),
                     ("chaine", p.sous_type))))
    return _trier(signaux)


# ---------------------------------------------------------------------------
# Faisceau d'indices
# ---------------------------------------------------------------------------

STATUT_PISTE = "piste à vérifier"
STATUT_FAISCEAU = {"moyenne": "faisceau d'indices à recouper", "forte": "faisceau d'indices prioritaire"}

_EXPL_FAISCEAU = ("Une agence, un collectif militant ou une rédaction partagent en règle des outils, des horaires et "
                  "des textes. Un lien ne dit rien de l'intention.")


def faisceau(signaux: Sequence[SignalFaible]) -> list[SignalFaible]:
    """Réunit les signaux qui se rejoignent sur un même compte ou une même pièce touchés par 2 familles ou plus.

    Les familles : contenu (D1 à D4), compte (D5 à D8), temps (D9, D10), infrastructure (D11, D12), argent
    (D13 à D15). Deux détecteurs d'une même famille comptent une fois. 2 familles : « faisceau d'indices à
    recouper » (moyenne) ; 3 ou plus : « faisceau d'indices prioritaire » (forte). Jamais « établi » :
    il faut une pièce primaire et une validation humaine.
    """
    actifs = [s for s in signaux if s.type in GROUPE_INDICES and s.type != "commentateurs_recents"]
    par_objet: dict[tuple[str, str], list[SignalFaible]] = defaultdict(list)
    for s in actifs:
        for p in s.pieces:
            par_objet[("piece", p)].append(s)
        for c in s.comptes:
            par_objet[("compte", c)].append(s)
    union = _Union()
    for objet, ss in par_objet.items():
        if len({GROUPE_INDICES[s.type] for s in ss}) >= 2:
            for s in ss[1:]:
                union.unir(ss[0].id, s.id)
    par_id = {s.id: s for s in actifs}
    resultat = []
    for compo in union.composantes():
        membres = [par_id[i] for i in sorted(compo)]
        if len(membres) < 2:
            continue
        familles = sorted({GROUPE_INDICES[s.type] for s in membres})
        convergents = {o for o, ss in par_objet.items()
                       if len({GROUPE_INDICES[s.type] for s in ss if s.id in compo}) >= 2}
        comptes = sorted(o[1] for o in convergents if o[0] == "compte")
        pieces = sorted(o[1] for o in convergents if o[0] == "piece")
        f = len(familles)
        if comptes:
            k = len(comptes)
            titre = (f"{f} familles d'indices convergent sur les mêmes {k} comptes" if k > 1
                     else f"{f} familles d'indices convergent sur le même compte")
        else:
            k = len(pieces)
            titre = (f"{f} familles d'indices convergent sur les mêmes {k} pièces" if k > 1
                     else f"{f} familles d'indices convergent sur la même pièce")
        force = "forte" if f >= 3 else "moyenne"
        preuves, vues = [], set()
        for s in membres:
            for pr in s.preuves:
                if (pr.piece in pieces or (pr.compte in comptes)) and (pr.piece, pr.compte, pr.extrait) not in vues:
                    vues.add((pr.piece, pr.compte, pr.extrait))
                    preuves.append(pr)
        dates = [s.premiere_apparition for s in membres if s.premiere_apparition]
        signaux_ids = tuple(s.id for s in membres)
        resultat.append(SignalFaible(
            id=_id_signal("faisceau", signaux_ids), type="faisceau", titre=titre,
            ce_que_ca_peut_vouloir_dire="Une même structure pourrait animer ces comptes : à établir.",
            explication_innocente=_EXPL_FAISCEAU, force=force, premiere_apparition=min(dates) if dates else None,
            pieces=tuple(pieces), comptes=tuple(comptes), preuves=tuple(preuves),
            question_ouverte="Une même structure anime-t-elle ces comptes ?",
            pistes=("Trouver la pièce primaire qui trancherait · registre, document, réponse de l'intéressé · variable",
                    "Exercer le droit de réponse auprès des comptes · courrier · gratuit",
                    "Relire chaque indice sur sa pièce · fiche du signal · gratuit"),
            mesures=(("familles", tuple(familles)), ("signaux", signaux_ids), ("comptes", len(comptes)),
                     ("pieces", len(pieces)), ("statut", STATUT_FAISCEAU[force]))))
    return _trier(resultat)


# ---------------------------------------------------------------------------
# Export vers le réseau
# ---------------------------------------------------------------------------

_POINTS = {"faible": 0.5, "moyenne": 1.0, "forte": 1.5}


def _statut_signal(s: SignalFaible) -> str:
    return STATUT_FAISCEAU[s.force] if s.type == "faisceau" else STATUT_PISTE


def _vers_preuve(p: PreuveSignal, lu_le: datetime) -> Preuve:
    return Preuve(url=p.url or URL_ORIANE, extrait=p.extrait or "(extrait vide)", source_donnee=p.source,
                  date_collecte=p.lu_le or lu_le, debut_s=p.seconde)


def pour_export(signaux: Sequence[SignalFaible], lu_le: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """`{"noeuds", "liens", "questions"}` pour le réseau, sans toucher au réseau.

    Chaque signal devient un nœud `signal_faible` (rôle inconnu, solidité C3) relié à ses pièces
    (`signale`) et à ses comptes (`concerne`) ; un faisceau est relié à ses signaux (`recoupe`). Les
    clés de pièce et de compte sont celles des signaux : `chaine.py` les traduit en identifiants de
    nœuds et n'écrit jamais un lien vers une clé inconnue. Statut du lien : `indice`. Jamais de nœud
    pour un commentateur. `lu_le` date les preuves qui n'ont pas de date de lecture.
    """
    lu_le = lu_le or datetime.now(UTC)
    exportables = [s for s in signaux if s.type != "commentateurs_recents"]
    noeuds, liens, questions = [], [], []
    par_id = {s.id: s for s in exportables}
    for s in exportables:
        famille = FAMILLE[s.type]
        genres = []
        if s.comptes:
            genres.append("comptes")
        if s.pieces:
            genres.append("pièces")
        noeuds.append({
            "id": s.id, "type": "signal_faible", "libelle": s.titre, "role": "inconnu",
            "solidite": Solidite("C", 3),
            "attributs": {
                "type": s.type, "famille": famille.value, "famille_indices": s.famille_indices, "force": s.force,
                "statut": _statut_signal(s), "calibre": s.type in CALIBRES, "hypothese": s.ce_que_ca_peut_vouloir_dire,
                "explication_innocente": s.explication_innocente,
                "premiere_apparition": s.premiere_apparition.isoformat() if s.premiere_apparition else None,
                "question_ouverte": s.question_ouverte, "pistes": list(s.pistes),
                "dossiers_touches": sorted({g for g in (s.famille_indices,) if g} | set(genres)),
            }})
        par_cle: dict[tuple[str, str], list[PreuveSignal]] = defaultdict(list)
        for p in s.preuves:
            par_cle[("piece", p.piece)].append(p)
            if p.compte:
                par_cle[("compte", p.compte)].append(p)
        cibles = [("piece", x, "signale") for x in s.pieces] + [("compte", x, "concerne") for x in s.comptes]
        for genre, cle, type_lien in cibles:
            ps = par_cle.get((genre, cle))
            if not ps:
                continue  # une pièce sans preuve n'est pas reliée
            liens.append({
                "type_lien": type_lien, "famille": famille.value, "source": s.id, "cible": cle, "genre_cible": genre,
                "statut": "indice", "preuves": [_vers_preuve(p, lu_le) for p in ps],
                "force_points": _POINTS[s.force], "oriente": False, "agent": AGENT,
                "explication": f"{s.titre} (piste à vérifier, non établie)",
                "methode": f"détecteur {s.type}"})
        if s.type == "faisceau":
            for sid in s.mesure("signaux", ()):
                m = par_id.get(sid)
                if m is None or not m.preuves:
                    continue
                liens.append({
                    "type_lien": "recoupe", "famille": famille.value, "source": s.id, "cible": sid, "genre_cible": "signal",
                    "statut": "indice", "preuves": [_vers_preuve(m.preuves[0], lu_le)], "force_points": _POINTS[s.force],
                    "oriente": False, "agent": AGENT, "explication": f"{m.titre} recoupe {s.titre}",
                    "methode": "faisceau d'indices (familles indépendantes)"})
    for s in sorted(exportables, key=lambda x: (x.type != "faisceau", _cle_tri(x))):
        if s.type == "faisceau" or s.force in ("moyenne", "forte"):
            questions.append({"texte": s.question_ouverte, "origine": "signal faible", "role": "inconnu",
                              "pistes": list(s.pistes), "noeud": s.id, "force": s.force})
    return {"noeuds": noeuds, "liens": liens, "questions": questions}


# ---------------------------------------------------------------------------
# Lot 2 : les appels, faits avant les détecteurs
# ---------------------------------------------------------------------------

def _total(reponse: dict) -> int | None:
    return ((reponse.get("metadata") or {}).get("pagination") or {}).get("totalCount")


def _resultats(reponse: dict) -> list[dict]:
    return (reponse.get("data") or {}).get("results") or []


def collecter_pour_signaux(budget: Any, infra: Any, videos: Sequence[dict], pivots: Sequence[Any], *,
                           reference: datetime | None = None, gdelt: Callable[[str], dict[str, int]] | None = None,
                           max_phrases: int = 20, max_sons: int = 15, max_comptes: int = 20,
                           max_series: int = 5) -> dict[str, Any]:
    """Les appels du lot 2 qui nourrissent les détecteurs : sondes de D1, D3, D7, D10 ; pages de D12 ; relevés de D15.

    `budget` est le `Budget` de chaine.py (plafond de crédits, cache). Chaque sonde `basic` coûte
    10 crédits ; elle passe par le cache. Une sonde qui échoue est notée dans `erreurs` et le reste
    continue ; le plafond atteint arrête la collecte. D4 (images clés) n'est pas collecté ici :
    il exige des assets (5 crédits) et le format des requêtes visuelles est à vérifier.
    Non éprouvé sur le service réel : les formes de filtres suivent la documentation d'Oriane.
    `gdelt(expression)` rend `{"AAAA-MM-JJ": articles}` ; sans lui, D10 ne mesure pas l'écho de presse.
    """
    videos = _dedoublonner(videos)
    erreurs: list[str] = []
    sortie: dict[str, Any] = {"rarete": {}, "usages": {}, "recents": {}, "series": {}, "presse": {}, "pages": {},
                              "releves": {}, "erreurs": erreurs}
    ref = reference or _reference(videos)

    def sonde(etiquette: str, **parametres: Any) -> dict | None:
        try:
            return budget.chercher("basic", **parametres)
        except Exception as e:  # noqa: BLE001 : une sonde qui échoue ne casse pas la collecte
            erreurs.append(f"{etiquette} : {type(e).__name__} {e}")
            if type(e).__name__ == "PlafondAtteint":
                raise
            return None

    try:
        # D1 : rareté des phrases candidates, sans filtre de sujet.
        for s in formulation_partagee(videos)[:max_phrases]:
            phrase = s.mesure("phrase")
            rep = sonde("rareté", filtres={"transcript": {"includesExactly": {"values": [phrase]}}}, limite=1)
            if rep is not None and _total(rep) is not None:
                sortie["rarete"][phrase] = _total(rep)
        # D3 : usages des sons non licenciés les plus repris du dossier.
        sons = Counter(str(v["audioPlatformId"]) for v in videos if v.get("audioPlatformId")
                       and not v.get("audioCopyrighted") and v.get("audioType") != "licensed")
        for son, _ in sons.most_common(max_sons):
            rep = sonde("usages du son", filtres={"audioPlatformId": {"includes": [son]}}, limite=100, tri="publishedAt")
            if rep is not None:
                sortie["usages"][son] = {"videos": _resultats(rep), "totalCount": _total(rep)}
        # D7 : vidéos récentes des comptes déjà touchés par un signal.
        if ref is not None:
            touches = []
            for s in (*formulation_partagee(videos), *gabarit_commun(videos), *comptes_sans_passe(videos)):
                touches.extend(s.comptes)
            profils = _profils(videos)
            for compte in list(dict.fromkeys(touches))[:max_comptes]:
                pid = next((v.get("profileId") for v in profils.get(compte, {}).get("videos", []) if v.get("profileId")), None)
                if not pid:
                    continue
                rep = sonde("vidéos récentes", filtres={"profileId": {"includes": [pid]}, "publishedAt": {
                    "after": (ref - timedelta(days=90)).date().isoformat(), "before": ref.date().isoformat()}}, limite=1)
                if rep is not None and _total(rep) is not None:
                    sortie["recents"][compte] = _total(rep)
        # D10 : auteurs par jour des hashtags non banals les plus portés.
        tags = [p for p in pivots if p.type == _pv.HASHTAG and not p.banal and p.n_comptes >= 3]
        for p in sorted(tags, key=lambda p: -p.n_comptes)[:max_series]:
            par_pf: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
            langues: dict[str, set[str]] = defaultdict(set)
            for page in range(3):
                rep = sonde("série", filtres={"caption": {"includesExactly": {"values": ["#" + p.valeur]}}},
                            limite=100, decalage=100 * page, tri="publishedAt:desc")
                res = _resultats(rep) if rep else []
                for v in res:
                    d, c = _date(v), _compte(v)
                    if d and c:
                        par_pf[str(v.get("platform"))][d.date().isoformat()].add(c)
                        if v.get("captionLanguage"):
                            langues[d.date().isoformat()].add(v["captionLanguage"])
                if len(res) < 100:
                    break
            if par_pf:
                sortie["series"][p.valeur] = {
                    "type": "hashtag", "auteurs": {pf: {j: len(cs) for j, cs in jours.items()} for pf, jours in par_pf.items()},
                    "langues": {j: sorted(ls) for j, ls in langues.items()}, "nouveaute": None,
                    "debut": (ref - timedelta(days=90)).date().isoformat() if ref else None,
                    "comptes": sorted(p.comptes)[:5]}
                if gdelt is not None:
                    try:
                        sortie["presse"][p.valeur] = gdelt(p.valeur)
                    except Exception as e:  # noqa: BLE001
                        erreurs.append(f"presse : {type(e).__name__} {e}")
    except Exception as e:  # noqa: BLE001 : plafond de crédits atteint
        if type(e).__name__ != "PlafondAtteint":
            erreurs.append(f"collecte interrompue : {type(e).__name__} {e}")

    # D12 : pages des sites du dossier (gratuit, en cache) si le client de pages existe.
    lire_page = getattr(infra, "page", None)
    if callable(lire_page):
        for p in pivots:
            if p.type == _pv.DOMAINE and not p.banal:
                try:
                    sortie["pages"][p.valeur] = lire_page(p.valeur)
                except Exception as e:  # noqa: BLE001
                    erreurs.append(f"page {p.valeur} : {type(e).__name__} {e}")
    # D15 : relevés des portefeuilles du dossier (gratuit).
    portefeuilles = [p for p in pivots if p.type == _pv.PORTEFEUILLE and not p.banal and p.sous_type]
    if portefeuilles:
        from enquete.sources import chaines as _chaines

        profils_p: dict[str, Any] = {}
        for p in portefeuilles:
            try:
                profils_p[p.valeur] = _chaines.profil_portefeuille(p.sous_type, p.valeur, ref)
            except Exception as e:  # noqa: BLE001
                erreurs.append(f"portefeuille {p.valeur} : {type(e).__name__} {e}")
        btc = [p.valeur for p in portefeuilles if p.sous_type == "btc" and p.valeur in profils_p]
        groupes = []
        if len(btc) >= 2:
            try:
                groupes = [g.get("adresses", g) if isinstance(g, dict) else g
                           for g in _chaines.regrouper_entrees_communes(btc, date_gel=ref)]
            except Exception as e:  # noqa: BLE001
                erreurs.append(f"entrées communes : {type(e).__name__} {e}")
        sortie["releves"] = {"profils": profils_p, "groupes": groupes, "flux": []}
    return sortie


# ---------------------------------------------------------------------------
# Les quinze détecteurs, puis le faisceau
# ---------------------------------------------------------------------------

def _videos_des_pieces(signal: SignalFaible, par_id: dict[str, dict]) -> list[dict]:
    return [par_id[p] for p in signal.pieces if p in par_id]


def detecter(videos: Sequence[dict], pivots: Sequence[Any] | None = None, *, temoin: dict | None = None,
             reference: datetime | None = None, rarete: dict[str, int] | None = None,
             usages: dict[str, Any] | None = None, correspondances: dict[str, list[list[dict]]] | None = None,
             dates_revendiquees: dict[str, Any] | None = None, recents: dict[str, int] | None = None,
             empreintes_photos: dict[str, int] | None = None, series: dict[str, dict] | None = None,
             presse: dict[str, dict[str, int]] | None = None, pages: dict[str, Any] | None = None,
             releves: dict[str, Any] | None = None, groupes: dict[str, Sequence[dict]] | None = None,
             tirages: int = 1000) -> list[SignalFaible]:
    """Lance les quinze détecteurs, puis `faisceau`. Les entrées du lot 2 sont facultatives : sans elles,
    le détecteur concerné ne lève rien (ou reste faible). Aucun appel réseau, aucune lecture de l'horloge."""
    videos = _dedoublonner(videos)
    pivots = list(pivots or [])
    par_id = _par_id(videos)
    d1 = formulation_partagee(videos, rarete, temoin)
    d2 = gabarit_commun(videos)
    d3 = son_lance_par_peu(videos, usages)
    d4 = sequence_deja_vue(videos, correspondances or {}, dates_revendiquees)
    if groupes is None:
        groupes = {s.id: g for s in (*d1, *d2, *d3) if len(g := _videos_des_pieces(s, par_id)) >= 3}
        groupes["dossier"] = videos
    d5 = comptes_sans_passe(videos, groupes, temoin)
    d6 = cadence_hors_norme(videos, groupes, temoin, reference)
    d7 = compte_reveille(videos, recents, reference)
    d8 = profils_jumeaux(videos, empreintes_photos)
    d9 = publications_rapprochees(videos, None, tirages)
    d10 = sujet_qui_monte(series or {}, presse)
    d11 = domaine_recent(pivots, videos)
    d12 = identifiant_de_suivi_partage(pages or {}, pivots)
    d13 = societe_recente(pivots, videos)
    d14 = code_promo_en_serie(pivots, videos)
    d15 = portefeuille_neuf(pivots, videos, releves)
    d5b = commentateurs_recents(videos, temoin)
    tous = [*d1, *d2, *d3, *d4, *d5, *d5b, *d6, *d7, *d8, *d9, *d10, *d11, *d12, *d13, *d14, *d15]
    uniques = list({s.id: s for s in tous}.values())
    return _trier([*uniques, *faisceau(uniques)])
