"""La note de rédaction : le dossier sous la forme que la rédaction lit en conférence.

Module pur : ni réseau, ni modèle. Aucun fait n'est écrit ici ; tout vient de
l'export du réseau (`ReseauEnquete.exporter_pour_interface()` ou `/api/export`).
Ce module range, classe et formule selon des règles écrites dans le code.

Trois sorties :
- `note_enquete` : ce qu'on sait (chaque phrase avec sa note vers la pièce), ce
  qu'on ne sait pas, les prochaines actions classées, qui contacter et dans
  quel ordre, les risques, la méthode et ses limites ;
- `lettre_contradictoire` : le brouillon que le journaliste envoie lui-même ;
- `demandes_cada` : une demande de document administratif par question ouverte
  qui en nomme un.

Lit aussi, s'ils sont là, les types et attributs de l'intégration (portefeuille,
code_promo, paiement, page de liens `a_deplier`, `indices`, `contradictoire`),
sans jamais les exiger.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date, datetime, UTC
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from enquete.reseau import LIBELLES_ROLES, Role, echelle_verbale

PARIS = ZoneInfo("Europe/Paris")
STATUT_LETTRE = "brouillon : le journaliste l'envoie lui-même"
MENTION_PROFIL = "profil public, non vérifié"
PAS_AVANT_MIS_EN_CAUSE = "après la lettre du contradictoire et au moins 2 liens prouvés le visant"


class AffirmationSansPiece(ValueError):
    """Une phrase affirmée sans note vers une pièce : la note ne sort pas."""


# ---------------------------------------------------------------------------
# Petits outils
# ---------------------------------------------------------------------------

def _vrai(valeur: Any) -> bool:
    if isinstance(valeur, str):
        return valeur.strip().lower() not in ("", "0", "non", "false", "none", "aucun")
    return bool(valeur)


def _sans_accents(texte: str) -> str:
    """Minuscules sans accents, un caractère pour un caractère (les indices restent alignés)."""
    return "".join(unicodedata.normalize("NFD", c)[0].lower() for c in texte)


_MOTS_VIDES = {
    "tiktok", "instagram", "youtube", "video", "videos", "dans", "avec", "pour", "quel", "quelle", "quels", "quelles", "sont", "cette", "ces", "leur",
    "leurs", "entre", "depuis", "aussi", "plus", "comme", "alors", "etait", "avant", "apres",
}


def _mots(texte: str) -> set[str]:
    return {m for m in re.findall(r"[a-z0-9]{5,}", _sans_accents(texte)) if m not in _MOTS_VIDES}


def _fin_de_phrase(texte: str) -> str:
    texte = texte.strip()
    return texte if texte[-1:] in ".?!…" else texte + "."


def _vers_paris(valeur: datetime) -> datetime:
    if valeur.tzinfo is None:
        return valeur.replace(tzinfo=PARIS)
    return valeur.astimezone(PARIS)


def _maintenant(valeur: datetime | None) -> datetime:
    return _vers_paris(valeur) if valeur else datetime.now(UTC).astimezone(PARIS)


def _ajouter_mois(jour: date, mois: int) -> date:
    index = jour.month - 1 + mois
    annee, m = jour.year + index // 12, index % 12 + 1
    return date(annee, m, min(jour.day, calendar.monthrange(annee, m)[1]))


def _date_fr(jour: date) -> str:
    return f"{jour:%d/%m/%Y}"


def _heure_fr(valeur: datetime) -> str:
    return f"{_date_fr(valeur.date())} à {valeur:%H} h {valeur:%M}, heure de Paris"


def _hote(url: str) -> str:
    return urlparse(url).netloc or url


# ---------------------------------------------------------------------------
# Lecture de l'export
# ---------------------------------------------------------------------------

_ROLES_RISQUE_FORT = {Role.ORIGINE.value, Role.BENEFICIAIRE.value, Role.MIS_EN_CAUSE.value}

_VERBES = {
    "reprend": "reprend le contenu de",
    "publie": "publie",
    "dit_la_meme_chose": "dit la même chose que",
    "co_agit_avec": "agit de concert avec",
    "meme_operateur": "est tenu par le même opérateur que",
    "meme_infrastructure": "partage la même infrastructure que",
    "meme_argent": "reçoit de l'argent par le même canal que",
    "profite_a": "profite à",
    "contredit": "contredit",
    "recycle": "recycle un contenu de",
    "genere_par_ia": "est généré par IA, comme",
}

_FORCE = {
    "lien très fort": 5, "lien fort": 4, "appui net": 3, "appui faible": 2,
    "coïncidence probable": 1, "indices contraires": 0,
}


class _Export:
    """Vue de lecture de l'export : pièces par identifiant, liens, questions."""

    def __init__(self, export: dict[str, Any]) -> None:
        self.brut = export
        self.question = str(export.get("question") or "")
        self.pieces: dict[str, dict[str, Any]] = {}
        for dossier in export.get("dossiers", []):
            for piece in dossier.get("pieces", []):
                self.pieces[piece["id"]] = {**piece, "role": piece.get("role") or dossier.get("role")}
        self.liens: list[dict[str, Any]] = list(export.get("liens", []))
        self.questions: list[dict[str, Any]] = [
            q for q in export.get("questions_ouvertes", []) if q.get("statut", "ouverte") == "ouverte"
        ]

    def piece(self, piece_id: str) -> dict[str, Any] | None:
        return self.pieces.get(piece_id)

    @staticmethod
    def attribut(piece: dict[str, Any] | None, nom: str) -> Any:
        return ((piece or {}).get("attributs") or {}).get(nom)

    def protegee(self, piece_id: str) -> bool:
        piece = self.piece(piece_id)
        return _vrai(self.attribut(piece, "particulier")) or _vrai(self.attribut(piece, "mineur_possible"))

    def confidentielle(self, piece_id: str) -> bool:
        return _vrai(self.attribut(self.piece(piece_id), "confidentiel"))

    def libelle(self, piece_id: str, repli: str = "") -> str:
        piece = self.piece(piece_id)
        return (piece or {}).get("libelle") or repli or piece_id

    def touche_protegee(self, lien: dict[str, Any], sauf: str | None = None) -> bool:
        return any(
            self.protegee(i) for i in (lien.get("source_id"), lien.get("cible_id")) if i and i != sauf
        )

    def echelle(self, lien: dict[str, Any]) -> str:
        if lien.get("echelle"):
            return lien["echelle"]
        return echelle_verbale(
            float(lien.get("force_points") or 0), int(lien.get("familles_independantes") or 1),
            int(lien.get("methodes_independantes") or 1), bool(lien.get("identifiant_officiel")),
        ).value

    def pieces_de_la_preuve(self) -> dict[tuple, list[str]]:
        """Pour chaque preuve (url, extrait, seconde), les pièces qui la portent."""
        index: dict[tuple, list[str]] = {}
        for piece in self.pieces.values():
            for p in piece.get("preuves", []):
                index.setdefault(_cle_preuve(p), []).append(piece["id"])
        return index


def _cle_preuve(p: dict[str, Any]) -> tuple:
    return (p.get("url"), p.get("extrait"), p.get("debut_s"))


# ---------------------------------------------------------------------------
# Notes et pièces numérotées
# ---------------------------------------------------------------------------

class _Notes:
    """Numérote les preuves citées ([1], [2]…) et les pièces de base (P1, P2…)."""

    def __init__(self, export: _Export) -> None:
        self._export = export
        self._par_cle: dict[tuple, int] = {}
        self._pieces_par_url: dict[str, str] = {}
        self.notes: list[dict[str, Any]] = []
        self.pieces: list[dict[str, Any]] = []
        self._titres: dict[str, str] = {}
        for piece in export.pieces.values():
            if export.protegee(piece["id"]):
                continue
            for p in piece.get("preuves", []):
                self._titres.setdefault(p.get("url"), piece.get("libelle") or "")

    def noter(self, preuve: dict[str, Any]) -> int | None:
        url, extrait = preuve.get("url"), preuve.get("extrait")
        if not url or not extrait or not preuve.get("date_collecte"):
            return None  # pas d'adresse, pas d'extrait ou pas de date : pas de note
        cle = _cle_preuve(preuve)
        if cle in self._par_cle:
            return self._par_cle[cle]
        numero_piece = self._piece(preuve)
        empreinte = preuve.get("empreinte_sha256")
        n = len(self.notes) + 1
        self.notes.append({
            "n": n,
            "piece_id": numero_piece,
            "url": url,
            "extrait": extrait,
            "seconde": preuve.get("debut_s"),
            "empreinte8": empreinte[:8] if empreinte else None,
            "date_collecte": preuve["date_collecte"],
            "source": preuve.get("source_donnee") or "",
        })
        self._par_cle[cle] = n
        return n

    def _piece(self, preuve: dict[str, Any]) -> str:
        url = preuve["url"]
        if url not in self._pieces_par_url:
            numero = f"P{len(self.pieces) + 1}"
            self._pieces_par_url[url] = numero
            empreinte = preuve.get("empreinte_sha256")
            self.pieces.append({
                "numero": numero,
                "titre": self._titres.get(url) or _hote(url),
                "url": url,
                "source": preuve.get("source_donnee") or "",
                "date_collecte": preuve["date_collecte"],
                "empreinte8": empreinte[:8] if empreinte else None,
            })
        else:  # la pièce garde l'empreinte la plus utile si une autre preuve en a une
            piece = next(p for p in self.pieces if p["numero"] == self._pieces_par_url[url])
            if not piece["empreinte8"] and preuve.get("empreinte_sha256"):
                piece["empreinte8"] = preuve["empreinte_sha256"][:8]
        return self._pieces_par_url[url]


# ---------------------------------------------------------------------------
# Formulation selon l'échelle
# ---------------------------------------------------------------------------

def _niveau(echelle: str) -> str | None:
    """affirme (lien fort), indique (appui net), piste (appui faible ou coïncidence) ; rien sinon."""
    if echelle in ("lien fort", "lien très fort"):
        return "affirme"
    if echelle == "appui net":
        return "indique"
    if echelle in ("appui faible", "coïncidence probable"):
        return "piste"
    return None


def _composants(export: _Export, lien: dict[str, Any]) -> tuple[str, str]:
    """(« A verbe B », explication) ; les libellés viennent de l'export."""
    source = lien.get("source_libelle") or export.libelle(lien.get("source_id", ""))
    cible = lien.get("cible_libelle") or export.libelle(lien.get("cible_id", ""))
    type_lien = lien.get("type_lien") or "lien"
    verbe = _VERBES.get(type_lien, type_lien.replace("_", " "))
    return f"{source} {verbe} {cible}", _fin_de_phrase(lien.get("explication") or "")


_SUFFIXE_CONTESTE = "Ce point est contesté : des éléments vont en sens contraire."


def _formuler(export: _Export, lien: dict[str, Any], niveau: str) -> str:
    base, explication = _composants(export, lien)
    if niveau == "affirme":
        phrase = f"{base}."
    elif niveau == "indique":
        phrase = f"Des éléments indiquent que {base}."
    else:
        phrase = f"Piste, à vérifier : {base}."
    texte = f"{phrase} {explication}".strip()
    if lien.get("conteste"):
        texte += f" {_SUFFIXE_CONTESTE}"
    return texte


MAX_NOTES_PAR_PHRASE = 3


def _extraits_partages(export: _Export) -> dict[str, int]:
    """Combien d'adresses différentes portent chaque extrait : la phrase commune est la vraie preuve."""
    adresses: dict[str, set[str]] = {}
    for lien in export.liens:
        for p in lien.get("preuves", []):
            adresses.setdefault(_sans_accents(" ".join(str(p.get("extrait", "")).split())), set()).add(p.get("url", ""))
    return {extrait: len(urls) for extrait, urls in adresses.items()}


def _preuves_a_citer(preuves: list[dict[str, Any]], partages: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Au plus trois preuves par phrase : d'abord une par adresse, l'extrait commun à plusieurs
    adresses en tête, puis celles avec seconde et copie conservée."""
    partages = partages or {}
    valides = [p for p in preuves if p.get("url") and p.get("extrait")]
    valides.sort(key=lambda p: (
        -partages.get(_sans_accents(" ".join(str(p["extrait"]).split())), 1),
        p.get("debut_s") is None, not p.get("empreinte_sha256"),
    ))
    choisies, urls = [], set()
    for p in valides:
        if p["url"] not in urls:
            choisies.append(p)
            urls.add(p["url"])
    choisies += [p for p in valides if p not in choisies]
    communs = [p for p in choisies if partages.get(_sans_accents(" ".join(str(p["extrait"]).split())), 1) >= 2]
    return (communs or choisies)[:MAX_NOTES_PAR_PHRASE]  # les extraits non partagés n'appuient pas la phrase


def _structurel(export: _Export, lien: dict[str, Any]) -> bool:
    """Un compte qui publie sa propre vidéo : un fait de structure, pas une information."""
    source, cible = export.piece(lien.get("source_id", "")), export.piece(lien.get("cible_id", ""))
    return (lien.get("type_lien") == "publie" and (source or {}).get("type") == "compte"
            and (cible or {}).get("type") == "video")


def _phrases_sait(export: _Export, notes: _Notes) -> tuple[list[dict], list[dict], int]:
    """(ce qu'on sait, phrases retombées dans « ce qu'on ne sait pas », liens écartés pour particulier)."""
    sait: list[dict] = []
    retombees: list[dict] = []
    ecartes = 0
    candidats = sorted(
        (l for l in export.liens if l.get("statut") in ("prouve", "indice")),
        key=lambda l: -_FORCE.get(export.echelle(l), 0),
    )
    vus: set[str] = set()
    partages = _extraits_partages(export)
    for lien in candidats:
        if lien.get("id") in vus:
            continue
        vus.add(lien.get("id"))
        if export.touche_protegee(lien):
            ecartes += 1
            continue
        if _structurel(export, lien):
            continue
        niveau = _niveau(export.echelle(lien))
        if niveau is None:
            retombees.append({"texte": f"Indices contraires : {_composants(export, lien)[0]}.", "pistes": []})
            continue
        numeros = list(dict.fromkeys(n for p in _preuves_a_citer(lien.get("preuves", []), partages) if (n := notes.noter(p))))
        texte = _formuler(export, lien, niveau)
        if not numeros:
            retombees.append({"texte": f"Non prouvé (aucune pièce) : {texte}", "pistes": ["Retrouver la pièce d'origine."]})
            continue
        sait.append({"texte": texte, "notes": numeros, "niveau": niveau, "conteste": bool(lien.get("conteste"))})
    return sait, retombees, ecartes


# ---------------------------------------------------------------------------
# Ce qu'on ne sait pas
# ---------------------------------------------------------------------------

_PISTES = [
    (r"contrat|march[ée]|convention|d[ée]lib[ée]ration|arr[êe]t[ée]|subvention|autorisation|rapport",
     "Demander le document à l'administration (voir les demandes CADA)."),
    (r"soci[ée]t[ée]|siren|g[ée]rant|dirigeant|entreprise|immatricul",
     "Registre des entreprises : dirigeants, adresse, date d'immatriculation, annonces légales."),
    (r"domaine|site|boutique|mentions l[ée]gales|h[ée]berg",
     "Domaine : date de création, hébergeur, domaines frères, archives du site (Wayback)."),
    (r"argent|paiement|portefeuille|crypto|virement|encaisse|rémun[ée]r|cagnotte",
     "Suivre le paiement : lien de paiement, explorateur de blockchain public, mentions légales du vendeur."),
    (r"premi[eè]re?|origine|d'o[uù]|qui a (lanc|post|cr[ée])|d[ée]but",
     "Chercher la première occurrence : phrase exacte, date lue dans l'identifiant de la vidéo, archives."),
    (r"sanction|autorit[ée]|dgccrf|amende|d[ée]cision|plainte|enqu[êe]te",
     "Bases publiques des autorités (décisions, listes noires, communiqués)."),
    (r"t[ée]moin|victime|v[ée]cu|plaignant",
     "Chercher des témoins directs parmi les comptes qui commentent (voir qui contacter)."),
]


def _pistes_pour(question: dict[str, Any]) -> list[str]:
    existantes = [p for p in (question.get("pistes") or []) if p]
    if existantes:
        return list(existantes)
    texte = _sans_accents(question.get("texte", ""))
    return [piste for motif, piste in _PISTES if re.search(_sans_accents(motif), texte)][:3]


def _ne_sait_pas(export: _Export, retombees: list[dict]) -> list[dict]:
    lignes: list[dict] = []
    for q in export.questions:
        lignes.append({"texte": q.get("texte", ""), "pistes": _pistes_pour(q), "_question": q.get("id")})
    for lien in export.liens:
        if lien.get("statut") in ("contredit", "propose") and not export.touche_protegee(lien):
            base, expl = _composants(export, lien)
            etat = "Contredit" if lien["statut"] == "contredit" else "Non établi"
            lignes.append({"texte": f"{etat} : {base}. {expl}".strip(), "pistes": ["Trouver une seconde famille d'indices."]})
    lignes.extend(retombees)
    for i, ligne in enumerate(lignes, 1):
        ligne["numero"] = i
    return [{"numero": l["numero"], "texte": l["texte"], "pistes": l["pistes"]} for l in lignes]


# ---------------------------------------------------------------------------
# Prochaines actions
# ---------------------------------------------------------------------------

_COUTS = {"0 crédit": 1, "recherches Oriane": 2, "humain": 3}
_FAMILLES_GRATUITES = {"identite", "infrastructure", "argent", "lieu", "temps"}
_TYPES_A_ARCHIVER = {"video", "site", "document", "publication_texte", "compte"}
_RE_SIREN = re.compile(r"\b\d{3}\s?\d{3}\s?\d{3}\b")


def _bloque_une_question(export: _Export, texte: str, genre: str = "") -> bool:
    mots = _mots(texte)
    for q in export.questions:
        if mots & _mots(q.get("texte", "")):
            return True
        if genre in ("societe", "domaine", "portefeuille") and re.search(
            r"qui|soci[ée]t[ée]|derri[eè]re|encaisse|argent|paiement|exploit|vend|domaine|site",
            _sans_accents(q.get("texte", "")),
        ):
            return True
    return False


def _priorite(debloque: bool, role: str | None, cout: str) -> float:
    return round((2 if debloque else 1) * (1.5 if role in _ROLES_RISQUE_FORT else 1) / _COUTS[cout], 2)


def _prochaines_actions(export: _Export, plafond: int = 20) -> list[dict]:
    actions: list[dict] = []
    a_archiver: list[tuple[str, str | None]] = []

    def ajouter(action: str, pourquoi: str, cout: str, qui: str, role: str | None, debloque: bool) -> None:
        actions.append({
            "action": action, "pourquoi": pourquoi, "cout": cout, "qui": qui,
            "priorite": _priorite(debloque, role, cout),
        })

    for piece in export.pieces.values():
        if export.protegee(piece["id"]):
            continue
        nom, type_, role = piece.get("libelle", ""), piece.get("type", ""), piece.get("role")
        indices = export.attribut(piece, "indices")
        # page de liens non dépliée
        if _vrai(export.attribut(piece, "a_deplier")):
            ajouter(f"Déplier la page de liens « {nom} » : lire chaque lien sortant et le suivre",
                    "Les liens sortants n'ont pas été lus ; une boutique ou un paiement s'y cache souvent.",
                    "0 crédit", "outil", role, _bloque_une_question(export, nom, "domaine"))
        # SIREN ou domaine sans indice
        siren = export.attribut(piece, "siren") or (_RE_SIREN.search(nom) if type_ == "societe" else None)
        domaine = export.attribut(piece, "domaine") or (nom if type_ == "site" else None)
        if siren and not _vrai(indices):
            ajouter(f"Interroger le registre des entreprises sur « {nom} » (dirigeants, adresse, date d'immatriculation)",
                    "Un SIREN sans indice n'est qu'un numéro : il ne relie encore personne à personne.",
                    "0 crédit", "outil", role, _bloque_une_question(export, nom, "societe"))
        if domaine and not _vrai(indices):
            ajouter(f"Profiler le domaine « {domaine} » (création, hébergeur, domaines frères, archives)",
                    "Un domaine sans indice ne dit ni qui l'a acheté ni quand.",
                    "0 crédit", "outil", role, _bloque_une_question(export, str(domaine), "domaine"))
        # portefeuille non profilé
        if type_ in ("portefeuille", "wallet") and not (_vrai(export.attribut(piece, "profil")) or _vrai(indices)):
            ajouter(f"Profiler le portefeuille « {nom} » (premiers mouvements, contreparties, plateformes d'échange)",
                    "Un portefeuille non profilé ne dit pas où va l'argent.",
                    "0 crédit", "outil", role, _bloque_une_question(export, nom, "portefeuille"))
        # pièce sans empreinte (regroupée plus bas)
        preuves = piece.get("preuves", [])
        if type_ in _TYPES_A_ARCHIVER and type_ != "compte" and preuves \
                and not any(p.get("empreinte_sha256") for p in preuves):
            a_archiver.append((nom, role))
        # mis en cause sans contradictoire
        if role == Role.MIS_EN_CAUSE.value and not _vrai(export.attribut(piece, "contradictoire")) \
                and type_ in ("compte", "personne", "societe"):
            ajouter(f"Envoyer la lettre du contradictoire à « {nom} »",
                    "Le mis en cause doit pouvoir répondre avant toute publication.",
                    "humain", "journaliste", role, _bloque_une_question(export, nom))
    # question ouverte sans piste
    for q in export.questions:
        if not _pistes_pour(q):
            ajouter(f"Trouver une première piste pour : {q.get('texte', '')}",
                    "Une zone d'ombre sans piste ne peut pas avancer.",
                    "humain", "journaliste", None, True)
    # lien d'une seule famille : une action par famille, avec le nombre de liens
    par_famille: dict[str, list[tuple[str, str | None, bool]]] = {}
    for lien in export.liens:
        if lien.get("statut") not in ("prouve", "indice") or export.touche_protegee(lien) or _structurel(export, lien):
            continue
        if int(lien.get("familles_independantes") or 1) >= 2 or lien.get("identifiant_officiel"):
            continue
        roles = [(export.piece(i) or {}).get("role") for i in (lien.get("source_id"), lien.get("cible_id"))]
        base, _ = _composants(export, lien)
        par_famille.setdefault(lien.get("famille", "?"), []).append(
            (base, next((r for r in roles if r in _ROLES_RISQUE_FORT), None), _bloque_une_question(export, base)))
    for famille, items in par_famille.items():
        cout = "0 crédit" if famille in _FAMILLES_GRATUITES else "recherches Oriane"
        meilleur = max(items, key=lambda i: _priorite(i[2], i[1], cout))
        reste = f" (et {len(items) - 1} autre(s))" if len(items) > 1 else ""
        ajouter(f"Chercher une seconde famille d'indices pour {len(items)} lien(s) fondé(s) sur « {famille} », d'abord : {meilleur[0]}{reste}",
                "Une seule famille d'indices plafonne le lien à l'appui net ; la seconde le rend affirmable.",
                cout, "outil" if cout == "0 crédit" else "outil (crédits Oriane)", meilleur[1], meilleur[2])
    if a_archiver:
        exemples = ", ".join(f"« {n} »" for n, _ in a_archiver[:3])
        role = next((r for _, r in a_archiver if r in _ROLES_RISQUE_FORT), None)
        ajouter(f"Archiver {len(a_archiver)} pièce(s) sans copie conservée (capture, copie horodatée, empreinte), d'abord : {exemples}",
                "Sans copie conservée, une pièce peut disparaître ou être modifiée avant parution.",
                "0 crédit", "outil", role, False)
    actions.sort(key=lambda a: -a["priorite"])
    return actions[:plafond]


# ---------------------------------------------------------------------------
# Qui contacter
# ---------------------------------------------------------------------------

_ORDRE_CONTACT = [
    Role.TEMOIN.value, Role.VICTIME.value, Role.EXPERT.value, Role.CONTRADICTEUR.value,
    Role.ORIGINE.value, Role.AMPLIFICATEUR.value, Role.MIS_EN_CAUSE.value,
]

MAX_CONTACTS_PAR_ROLE = 3

_QUESTIONS_CONTACT = {
    Role.TEMOIN.value: [
        "Qu'avez-vous vu ou vécu directement, et à quelle date ?",
        "Avez-vous gardé des preuves (captures, échanges, reçus) que vous accepteriez de partager ?",
        "Qui d'autre était au courant et pourrait le confirmer ?",
    ],
    Role.VICTIME.value: [
        "Que s'est-il passé, dans quel ordre et à quelles dates ?",
        "Combien avez-vous payé, par quel moyen, à qui ?",
        "Avez-vous porté plainte ou signalé les faits, et savez-vous si d'autres personnes sont concernées ?",
    ],
    Role.EXPERT.value: [
        "Ce schéma est-il connu, et sous quel nom ?",
        "Qu'est-ce qui, dans les éléments que nous avons, vous paraît solide ou fragile ?",
        "Qui d'autre devrions-nous interroger, et quelle donnée publique manque selon vous ?",
    ],
    Role.CONTRADICTEUR.value: [
        "Sur quels éléments contestez-vous la version qui circule ?",
        "Pouvez-vous nous donner la pièce qui l'établit ?",
        "Avez-vous déjà répondu publiquement, et où ?",
    ],
    Role.ORIGINE.value: [
        "Êtes-vous bien l'auteur du premier contenu, et d'où le tenez-vous ?",
        "Qui vous l'a transmis ou demandé, et quand ?",
        "Avez-vous été rémunéré ou fourni en produits pour le publier ?",
    ],
    Role.AMPLIFICATEUR.value: [
        "Pourquoi avez-vous repris ce contenu, et qui vous l'a transmis ?",
        "Avez-vous été rémunéré, fourni en produits ou en code promo ?",
        "Avez-vous vérifié l'information avant de la relayer ?",
    ],
    Role.MIS_EN_CAUSE.value: [
        "Confirmez-vous exploiter ce compte, ce site ou cette société ?",
        "Que répondez-vous aux éléments qui vous visent (voir la lettre du contradictoire) ?",
        "Quels documents (contrat, facture, immatriculation) pouvez-vous nous communiquer ?",
    ],
}


def _canal(piece: dict[str, Any]) -> str:
    plateforme = ((piece.get("attributs") or {}).get("plateforme") or "").strip()
    cible = f"de la plateforme {plateforme}" if plateforme else "de la plateforme"
    return f"messagerie publique {cible} ({MENTION_PROFIL})"


def _qui_contacter(export: _Export) -> list[dict]:
    liens_prouves: dict[str, int] = {}
    for lien in export.liens:
        if lien.get("statut") == "prouve":
            for i in (lien.get("source_id"), lien.get("cible_id")):
                liens_prouves[i] = liens_prouves.get(i, 0) + 1
    candidats = []
    for piece in export.pieces.values():
        if piece.get("role") not in _ORDRE_CONTACT or piece.get("type") not in ("compte", "personne", "societe"):
            continue
        if export.protegee(piece["id"]):
            continue  # un particulier ou un mineur possible n'est jamais proposé
        candidats.append(piece)
    candidats.sort(key=lambda p: (_ORDRE_CONTACT.index(p["role"]), -liens_prouves.get(p["id"], 0), p.get("libelle", "")))
    par_role: dict[str, int] = {}
    retenus = []
    for piece in candidats:  # trois par rôle au plus : une liste de quarante noms ne se lit pas en conférence
        par_role[piece["role"]] = par_role.get(piece["role"], 0) + 1
        if par_role[piece["role"]] <= MAX_CONTACTS_PAR_ROLE:
            retenus.append(piece)
    candidats = retenus
    resultat = []
    for ordre, piece in enumerate(candidats, 1):
        role = piece["role"]
        n_prouves = liens_prouves.get(piece["id"], 0)
        nombre = len(piece.get("preuves", []))
        resultat.append({
            "ordre": ordre,
            "piece_id": piece["id"],
            "libelle": piece.get("libelle", ""),
            "role": role,
            "raison": f"{n_prouves} lien(s) prouvé(s) et {nombre} preuve(s) dans le dossier.",
            "questions": list(_QUESTIONS_CONTACT[role]),
            "canal": _canal(piece),
            "pas_avant": PAS_AVANT_MIS_EN_CAUSE if role == Role.MIS_EN_CAUSE.value else None,
        })
    return resultat


# ---------------------------------------------------------------------------
# Risques, méthode et limites
# ---------------------------------------------------------------------------

def _risques(export: _Export, sait: list[dict], notes: _Notes, ecartes: int) -> list[dict]:
    risques: list[dict] = []
    sans_contradictoire = [
        p for p in export.pieces.values()
        if p.get("role") == Role.MIS_EN_CAUSE.value and not export.protegee(p["id"])
        and not _vrai(export.attribut(p, "contradictoire"))
    ]
    if sans_contradictoire:
        risques.append({
            "type": "contradictoire",
            "texte": "Mis en cause jamais interrogé : ne rien publier sans lui avoir laissé le temps de répondre.",
            "pieces": [p["id"] for p in sans_contradictoire],
        })
    attribution = [
        p["id"] for l in export.liens if l.get("statut") in ("prouve", "indice")
        and _niveau(export.echelle(l)) in ("indique", "piste")
        for p in (export.piece(l.get("source_id")), export.piece(l.get("cible_id")))
        if p and p.get("role") == Role.MIS_EN_CAUSE.value and not export.protegee(p["id"])
    ]
    if attribution:
        risques.append({
            "type": "attribution",
            "texte": "Un mis en cause n'est relié que par des éléments d'appui net ou plus faible : risque d'homonymie ou de mauvaise attribution, donc de diffamation.",
            "pieces": list(dict.fromkeys(attribution)),
        })
    contestes = [l["id"] for l in export.liens if l.get("conteste") and l.get("statut") in ("prouve", "indice")]
    if contestes:
        risques.append({"type": "conteste", "texte": f"{len(contestes)} lien(s) contesté(s) par des éléments contraires.", "pieces": contestes})
    pistes = [s for s in sait if s["niveau"] == "piste"]
    if pistes:
        risques.append({"type": "piste", "texte": f"{len(pistes)} phrase(s) restent au niveau de la piste : ne pas les publier telles quelles.", "pieces": []})
    sources = {n["source"] for n in notes.notes if n["source"]}
    if len(sources) == 1:
        risques.append({"type": "source_unique", "texte": f"Toutes les notes viennent d'une seule source de données ({next(iter(sources))}).", "pieces": []})
    non_archivees = [
        p["id"] for p in export.pieces.values()
        if p.get("type") in _TYPES_A_ARCHIVER and p.get("preuves")
        and not any(x.get("empreinte_sha256") for x in p["preuves"]) and not export.protegee(p["id"])
    ]
    if non_archivees:
        risques.append({"type": "disparition", "texte": f"{len(non_archivees)} pièce(s) sans copie conservée : elles peuvent disparaître avant parution.", "pieces": non_archivees})
    if ecartes or any(export.protegee(i) for i in export.pieces):
        risques.append({"type": "particulier", "texte": "Des particuliers ou des mineurs possibles figurent dans le dossier : ils ne sont ni nommés ni proposés au contact.", "pieces": []})
    confidentielles = [i for i in export.pieces if export.confidentielle(i)]
    journaliste = [
        l["id"] for l in export.liens for p in l.get("preuves", []) if p.get("source_donnee") == "journaliste"
    ]
    if confidentielles or journaliste:
        risques.append({
            "type": "confidentialite",
            "texte": "Des pièces confidentielles ou fournies par le journaliste sont dans le dossier : elles ne partent dans aucune lettre.",
            "pieces": confidentielles + list(dict.fromkeys(journaliste)),
        })
    return risques


def _methode_et_limites(export: _Export, notes: _Notes, sait: list[dict], ecartes: int) -> list[str]:
    niveaux = {n: sum(1 for s in sait if s["niveau"] == n) for n in ("affirme", "indique", "piste")}
    lignes = [
        "Aucun fait de cette note n'est écrit par un modèle : chaque phrase est tirée d'un lien du réseau, avec ses notes vers les pièces.",
        "Une phrase sans pièce ne figure jamais dans « ce qu'on sait » ; elle retombe dans « ce qu'on ne sait pas ».",
        f"Formulation selon la force des indices : {niveaux['affirme']} phrase(s) affirmative(s) (lien fort), "
        f"{niveaux['indique']} « des éléments indiquent » (appui net), {niveaux['piste']} piste(s) (appui faible).",
        "L'échelle parle de force des indices, jamais de probabilité que le lien soit vrai.",
        f"{len(notes.notes)} note(s) sur {len(notes.pieces)} pièce(s) de base ; sources de données : "
        f"{', '.join(sorted({n['source'] for n in notes.notes if n['source']})) or 'aucune'}.",
        "Les profils sont publics et non vérifiés ; aucun contact par e-mail ni téléphone n'est proposé.",
    ]
    if ecartes:
        lignes.append(f"{ecartes} lien(s) touchant un particulier ou un mineur possible sont écartés de la note.")
    sans_archive = sum(1 for n in notes.notes if not n["empreinte8"])
    if sans_archive:
        lignes.append(f"{sans_archive} note(s) renvoient à une pièce sans empreinte : le contenu peut avoir changé depuis la collecte.")
    lignes.append("Ce que la note ne voit pas : les plateformes fermées, les contenus supprimés, tout ce qui n'a pas été collecté.")
    return lignes


# ---------------------------------------------------------------------------
# La note
# ---------------------------------------------------------------------------

def _complements(extras: list[dict] | None, notes: _Notes) -> tuple[list[dict], list[dict]]:
    """Sections déjà prouvées venues d'autres modules ; une ligne sans preuve retombe en « ne sait pas »."""
    sections: list[dict] = []
    retombees: list[dict] = []
    for extra in extras or []:
        lignes = []
        for ligne in extra.get("lignes", []):
            numeros = list(dict.fromkeys(n for p in ligne.get("preuves", []) if (n := notes.noter(p))))
            if numeros:
                lignes.append({"texte": ligne["texte"], "notes": numeros, "niveau": "affirme"})
            else:
                retombees.append({"texte": f"Non prouvé (aucune pièce) : {ligne['texte']}", "pistes": ["Retrouver la pièce d'origine."]})
        sections.append({"titre": extra.get("titre", ""), "lignes": lignes})
    return sections, retombees


def verifier_note(note: dict[str, Any]) -> None:
    """Lève `AffirmationSansPiece` si une phrase affirmée n'a pas de note valide."""
    connues = {n["n"] for n in note["notes"]}
    lignes = list(note["sections"]["ce_qu_on_sait"])
    for complement in note["sections"].get("complements", []):
        lignes.extend(complement["lignes"])
    sans_piece = [l["texte"] for l in lignes if not l.get("notes") or not set(l["notes"]) <= connues]
    if sans_piece:
        raise AffirmationSansPiece(
            f"{len(sans_piece)} affirmation(s) sans pièce, la note ne sort pas : " + " | ".join(sans_piece[:3])
        )


def _controle(note: dict[str, Any]) -> dict[str, int]:
    connues = {n["n"] for n in note["notes"]}
    lignes = list(note["sections"]["ce_qu_on_sait"])
    for complement in note["sections"].get("complements", []):
        lignes.extend(complement["lignes"])
    return {
        "affirmations": len(lignes),
        "affirmations_sans_piece": sum(1 for l in lignes if not l.get("notes") or not set(l["notes"]) <= connues),
    }


def note_enquete(
    export: dict[str, Any], *, maintenant: datetime | None = None, extras: list[dict] | None = None,
) -> dict[str, Any]:
    """Construit la note de rédaction depuis l'export du réseau."""
    lu = _Export(export)
    notes = _Notes(lu)
    sait, retombees, ecartes = _phrases_sait(lu, notes)
    complements, retombees_extras = _complements(extras, notes)
    note: dict[str, Any] = {
        "question": lu.question,
        "date": _maintenant(maintenant).isoformat(timespec="seconds"),
        "sections": {
            "ce_qu_on_sait": sait,
            "complements": complements,
            "ce_qu_on_ne_sait_pas": _ne_sait_pas(lu, retombees + retombees_extras),
            "prochaines_actions": _prochaines_actions(lu),
            "qui_contacter": _qui_contacter(lu),
            "risques": _risques(lu, sait, notes, ecartes),
            "methode_et_limites": _methode_et_limites(lu, notes, sait, ecartes),
        },
        "notes": notes.notes,
        "pieces": notes.pieces,
    }
    note["controle"] = _controle(note)
    if note["controle"]["affirmations_sans_piece"]:
        verifier_note(note)
    return note


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _renvois(numeros: list[int]) -> str:
    return "".join(f"[{n}]" for n in numeros)


def _court(texte: str, longueur: int = 280) -> str:
    texte = " ".join(texte.split())
    return texte if len(texte) <= longueur else texte[: longueur - 1].rstrip() + "…"


def en_markdown(note: dict[str, Any]) -> str:
    s = note["sections"]
    sortie = [f"# Note d'enquête : {note['question']}", "", f"Date : {note['date']}", ""]
    sortie += ["## Ce qu'on sait", ""]
    sortie += [f"- {l['texte']} {_renvois(l['notes'])}" for l in s["ce_qu_on_sait"]] or ["- Rien d'établi à ce stade."]
    for complement in s.get("complements", []):
        if complement["lignes"]:
            sortie += ["", f"### {complement['titre']}", ""]
            sortie += [f"- {l['texte']} {_renvois(l['notes'])}" for l in complement["lignes"]]
    sortie += ["", "## Ce qu'on ne sait pas", ""]
    for q in s["ce_qu_on_ne_sait_pas"]:
        sortie.append(f"{q['numero']}. {q['texte']}")
        sortie += [f"   - Piste : {p}" for p in q["pistes"]]
    sortie += ["", "## Prochaines actions (par priorité)", ""]
    sortie += [
        f"{i}. {a['action']} (priorité {a['priorite']}, coût : {a['cout']}, par : {a['qui']}). {a['pourquoi']}"
        for i, a in enumerate(s["prochaines_actions"], 1)
    ] or ["Aucune action déduite."]
    sortie += ["", "## Qui contacter, dans cet ordre", ""]
    for c in s["qui_contacter"]:
        sortie.append(f"{c['ordre']}. {c['libelle']} ({LIBELLES_ROLES[Role(c['role'])]}). {c['raison']}")
        sortie.append(f"   - Canal : {c['canal']}")
        if c["pas_avant"]:
            sortie.append(f"   - Pas avant : {c['pas_avant']}")
        sortie += [f"   - Question : {q}" for q in c["questions"]]
    if not s["qui_contacter"]:
        sortie.append("Personne à proposer à ce stade.")
    sortie += ["", "## Risques", ""]
    sortie += [f"- {r['texte']}" for r in s["risques"]] or ["- Aucun risque déduit des données."]
    sortie += ["", "## Méthode et limites", ""] + [f"- {m}" for m in s["methode_et_limites"]]
    c = note["controle"]
    sortie += ["", f"Contrôle : {c['affirmations']} affirmation(s), {c['affirmations_sans_piece']} sans pièce.", "", "## Notes", ""]
    for n in note["notes"]:
        seconde = f", à {n['seconde']:g} s" if n["seconde"] is not None else ""
        empreinte = f", empreinte {n['empreinte8']}" if n["empreinte8"] else ", sans empreinte"
        sortie.append(
            f"[{n['n']}] {n['piece_id']} : {n['url']}{seconde}{empreinte}, collecté le {n['date_collecte'][:10]} "
            f"({n['source']}). « {_court(n['extrait'])} »"
        )
    sortie += ["", "## Pièces", ""]
    sortie += [
        f"{p['numero']} : {p['titre']}, {p['url']}, {p['source']}, collectée le {p['date_collecte'][:10]}"
        + (f", empreinte {p['empreinte8']}" if p["empreinte8"] else ", sans empreinte")
        for p in note["pieces"]
    ]
    return "\n".join(sortie) + "\n"


# ---------------------------------------------------------------------------
# Lettre du contradictoire
# ---------------------------------------------------------------------------

def _preuve_retiree(export: _Export, preuve: dict[str, Any], confidentielles: set[tuple]) -> str | None:
    if preuve.get("source_donnee") == "journaliste":
        return "preuve fournie par le journaliste : ne part pas dans une lettre"
    if _vrai(preuve.get("confidentiel")) or _cle_preuve(preuve) in confidentielles:
        return "pièce confidentielle : ne part pas dans une lettre"
    return None


def lettre_contradictoire(
    export: dict[str, Any], piece_id: str, *, echeance: datetime, parution: datetime | None = None,
) -> dict[str, Any]:
    """Brouillon de la lettre au mis en cause. Seules les affirmations contrôlées y entrent."""
    lu = _Export(export)
    piece = lu.piece(piece_id)
    if piece is None:
        raise KeyError(f"pièce inconnue : {piece_id}")
    if lu.protegee(piece_id):
        raise ValueError("pièce marquée particulier ou mineur possible : aucune lettre produite")
    limite = _vers_paris(echeance)
    sortie_prevue = _vers_paris(parution) if parution else None
    if sortie_prevue and limite >= sortie_prevue:
        raise ValueError("l'échéance doit précéder la parution")

    confidentielles = {
        _cle_preuve(p) for pid in lu.pieces if lu.confidentielle(pid) for p in lu.pieces[pid].get("preuves", [])
    }
    notes = _Notes(lu)
    allegations, pistes, retraits = [], [], []
    # la pièce et ses propres vidéos : ce que dit le compte, c'est aussi ce que disent ses vidéos
    proches = {piece_id} | {
        l["cible_id"] for l in lu.liens if l.get("source_id") == piece_id and _structurel(lu, l)
    }
    for lien in lu.liens:
        if not proches & {lien.get("source_id"), lien.get("cible_id")} or _structurel(lu, lien):
            continue
        if lien.get("statut") not in ("prouve", "indice") or lu.touche_protegee(lien, sauf=piece_id):
            continue
        niveau = _niveau(lu.echelle(lien))
        if niveau is None:
            continue
        gardees, retirees = [], []
        for p in lien.get("preuves", []):
            motif = _preuve_retiree(lu, p, confidentielles)
            if motif:
                retirees.append({"lien_id": lien.get("id"), "url": p.get("url"), "motif": motif})
            else:
                gardees.append(p)
        numeros = list(dict.fromkeys(n for p in gardees if (n := notes.noter(p))))
        retire_allegation = not numeros
        for r in retirees:
            r["allegation_retiree"] = retire_allegation
        retraits.extend(retirees)
        if retire_allegation:
            continue
        base, explication = _composants(lu, lien)
        suffixe = f" {_SUFFIXE_CONTESTE}" if lien.get("conteste") else ""
        if niveau == "piste":
            pistes.append(f"Pouvez-vous nous dire si {base} ?")
            continue
        intro = "Selon nos informations, " + ("des éléments indiquent que " if niveau == "indique" else "")
        allegations.append({"texte": f"{intro}{base}. {explication}{suffixe}".strip(), "notes": numeros})

    mots_pieces = _mots(" ".join(lu.libelle(i) for i in proches))
    questions = [q["texte"] for q in lu.questions if mots_pieces & _mots(q.get("texte", ""))] + pistes + [
        f"Confirmez-vous exploiter « {piece.get('libelle', '')} » ?",
        "Que répondez-vous à ces éléments ?",
        "Avez-vous des documents ou des éléments que vous souhaitez que nous prenions en compte ?",
    ]
    echeance_txt = _heure_fr(limite)
    corps = [
        "Madame, Monsieur,", "",
        f"Dans le cadre d'une enquête sur « {lu.question} », nous vous écrivons au sujet de « {piece.get('libelle', '')} ».",
    ]
    if allegations:
        corps += ["", "Voici ce que nous avons établi à ce jour et que nous pourrions publier :"]
        corps += [f"{i}. {a['texte']}" for i, a in enumerate(allegations, 1)]
    corps += ["", "Nous souhaitons recueillir votre réponse aux questions suivantes :"]
    corps += [f"- {q}" for q in questions]
    corps += ["", f"Nous vous remercions de répondre avant le {echeance_txt}. "
              "Sans réponse de votre part à cette date, nous indiquerons que vous n'avez pas répondu à nos sollicitations."]
    if sortie_prevue:
        corps.append(f"Parution envisagée : {_heure_fr(sortie_prevue)}.")
    corps += ["", "Cordialement,", "[Nom du journaliste, rédaction]"]
    return {
        "destinataire": {
            "piece_id": piece_id, "libelle": piece.get("libelle", ""), "role": piece.get("role"),
            "canal": _canal(piece),
        },
        "allegations": allegations,
        "questions_ouvertes": questions,
        "echeance": {
            "date": limite.date().isoformat(), "heure": f"{limite:%H:%M}", "fuseau": "Europe/Paris",
            "texte": echeance_txt, "iso": limite.isoformat(timespec="minutes"),
        },
        "corps": "\n".join(corps) + "\n",
        "retraits": retraits,
        "pret_a_envoyer": bool(allegations),
        "notes": notes.notes,
        "statut": STATUT_LETTRE,
        "tentatives": [],
    }


# ---------------------------------------------------------------------------
# Demandes CADA
# ---------------------------------------------------------------------------

_RE_DOCUMENT = re.compile(
    r"\b(contrats?|marches?|conventions?|deliberations?|arretes?|rapports?|subventions?|autorisations?)\b"
)
_AVANT_FAUX = {"par", "en", "au", "sans", "avec", "ete", "a", "ont", "est", "sont"}


def _document_nomme(texte: str) -> str | None:
    """Le document administratif nommé par la question, avec quelques mots de contexte."""
    nu = _sans_accents(texte)
    for m in _RE_DOCUMENT.finditer(nu):
        avant = nu[: m.start()].split()
        precedent = avant[-1] if avant else ""
        if m.group(1).startswith(("rapport", "arrete")) and precedent in _AVANT_FAUX:
            continue
        fin = re.search(r"[.?!;:]", texte[m.start():])
        morceau = texte[m.start(): m.start() + fin.start()] if fin else texte[m.start():]
        mots = morceau.split()[:9]
        return " ".join(mots).strip(" ,")
    return None


def demandes_cada(export: dict[str, Any], *, maintenant: datetime | None = None) -> list[dict]:
    """Une demande de communication par question ouverte qui nomme un document administratif."""
    lu = _Export(export)
    aujourdhui = _maintenant(maintenant).date()
    reponse, saisine = _ajouter_mois(aujourdhui, 1), _ajouter_mois(aujourdhui, 3)
    demandes = []
    for q in lu.questions:
        document = _document_nomme(q.get("texte", ""))
        if not document:
            continue
        corps = (
            "Madame, Monsieur,\n\n"
            "En application des articles L311-1 et suivants du code des relations entre le public et "
            "l'administration, je vous demande de me communiquer, de préférence par voie électronique "
            f"(article L311-9), le document suivant : {document}.\n\n"
            f"Contexte de la demande : {q.get('texte', '')}\n\n"
            f"Date de la demande : {_date_fr(aujourdhui)}. Le silence gardé pendant un mois, soit jusqu'au "
            f"{_date_fr(reponse)}, vaut refus (article R311-13) ; je saisirai alors la Commission d'accès aux "
            "documents administratifs, dans le délai de deux mois prévu à l'article R343-1.\n\n"
            "Veuillez agréer, Madame, Monsieur, l'expression de mes salutations distinguées.\n"
            "[Nom du journaliste, rédaction, adresse de réponse]\n"
        )
        demandes.append({
            "question": q.get("texte", ""),
            "document": document,
            "administration": "à préciser",
            "corps": corps,
            "date_limite_reponse": reponse.isoformat(),
            "saisine_cada_avant": saisine.isoformat(),
            "statut": "à envoyer",
        })
    return demandes
