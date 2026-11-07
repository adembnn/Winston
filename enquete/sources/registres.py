"""Suivre l'argent dans les registres français gratuits, comme le ferait un enquêteur.

Quatre lectures, sans clé :
- BODACC (annonces commerciales, Opendatasoft v2.1) : la vie datée d'une société, avis par avis ;
- Recherche d'entreprises (data.gouv.fr) : fiche d'une société, chiffres publiés, autres sociétés
  d'un dirigeant (`nom_personne`), sociétés domiciliées à la même adresse ;
- chaîne de direction : les dirigeants d'une société, puis ceux de chaque dirigeant personne
  morale, jusqu'à une personne physique ou une zone d'ombre ;
- HATVP : déclarations d'intérêts et de patrimoine publiées des responsables publics (`liste.csv`).

Mêmes garde-fous qu'`Infra` :
- cache disque (`cache/registres/`) ; la même requête ne repart pas (le fichier HATVP est relu au
  plus une fois par jour) ;
- `filtre_url` et `date_gel` s'appliquent à CHAQUE appel ; un appel rejeté est journalisé et ne
  touche ni le réseau ni le cache ;
- réessais sur 429 et 5xx ; au moins 0,15 s entre deux appels à Recherche d'entreprises
  (7 requêtes par seconde au plus) ;
- les personnes physiques sont masquées en initiales par défaut.

Jamais : l'adresse d'une personne physique (le siège d'un entrepreneur individuel est réduit à sa
commune), une date de naissance (l'année ne sert qu'à départager deux homonymes), la colonne
`url_photo` de la HATVP (retirée avant même d'écrire le cache), ni l'affirmation d'un « contrôle »
ou d'un « prête-nom » : ce sont des signaux, donnés avec leur explication innocente.

Limite à garder en tête : l'API ne donne que la composition ACTUELLE des organes de direction.
Avec une date de gel, les avis, les créations et les déclarations postérieurs sont écartés, mais la
liste des dirigeants reste celle d'aujourd'hui ; les sorties le disent.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from enquete.sources.infra import (
    AGENT,
    DateGel,
    FiltreUrl,
    Indice,
    _en_datetime,
    _lire_date,
    initiales,
)

journal_logging = logging.getLogger("enquete.registres")

URL_BODACC = "https://bodacc-datadila.opendatasoft.com/api/explore/v2.1"
URL_BODACC_AVIS = URL_BODACC + "/catalog/datasets/annonces-commerciales/records"
URL_ENTREPRISES = "https://recherche-entreprises.api.gouv.fr/search"
URL_HATVP_LISTE = "https://www.hatvp.fr/livraison/opendata/liste.csv"
URL_HATVP_SITE = "https://www.hatvp.fr"
URL_ANNUAIRE = "https://annuaire-entreprises.data.gouv.fr/entreprise/"

ESSAIS_MAX = 3
PAUSE_ENTREPRISES = 0.15  # secondes entre deux appels à Recherche d'entreprises
LIMITE_EXTRAIT = 1500
LIMITE_RESUME = 300
AVIS_PAR_PAGE = 100
AVIS_PAGES_MAX = 5
MANDATS_PAR_PAGE = 25
MANDATS_PAGES_MAX = 4
SEUIL_BRUIT_DOMICILIATION = 20
DUREE_CACHE_HATVP = timedelta(days=1)
MOIS_COMPTES = 24

FIN_PERSONNE = "personne physique ({})"
FIN_OMBRE = "s'arrête ici : zone d'ombre"
LIBELLE_NOMBREUX_MANDATS = "nombreux mandats : prête-nom possible, ou dirigeant de groupe"

LECTURE_INNOCENTE = {
    "changement_dirigeant": "renouvellement de mandat, départ à la retraite ou réorganisation ordinaire",
    "transfert_siege": "déménagement ordinaire ; un siège qui bouge souvent mérite pourtant un second regard",
    "cession": "vente courante d'un fonds ou de parts, y compris entre proches ou au sein d'un groupe",
    "procedure_collective": "difficultés financières, pas une faute ; une conciliation est une procédure amiable",
    "radiation": "fusion, dissolution volontaire ou clôture de liquidation ordinaires",
    "comptes_non_deposes": ("comptes confidentiels ou dispense de publication (petite entreprise), dépôt "
                            "récent pas encore annoncé, ou avis hors de la plage lue"),
    "nombreux_mandats": ("dirigeant professionnel d'un groupe, d'un réseau de filiales ou administrateur "
                         "indépendant ; seul un prête-nom serait un problème, et rien ne le prouve ici"),
}

LIBELLES_HATVP = {
    "di": "déclaration d'intérêts",
    "dim": "modification de déclaration d'intérêts",
    "dia": "déclaration d'intérêts et d'activités",
    "diam": "modification de déclaration d'intérêts et d'activités",
    "dsp": "déclaration de situation patrimoniale",
    "dspm": "modification de déclaration de situation patrimoniale",
    "dspfm": "déclaration de situation patrimoniale de fin de mandat",
}

FAMILLES_BODACC = {
    "creation": "creation",
    "immatriculation": "creation",
    "modification": "modification",
    "vente": "vente",
    "radiation": "radiation",
    "collective": "procedure_collective",
    "retablissement_professionnel": "procedure_collective",
    "dpc": "depot_comptes",
}


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def _norm(texte: Any) -> str:
    """Minuscules, sans accents ni ponctuation : pour comparer des noms."""
    nfkd = unicodedata.normalize("NFKD", str(texte or ""))
    sans_accent = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", sans_accent.lower()).strip()


def _siren(valeur: str) -> str:
    chiffres = re.sub(r"\D", "", str(valeur or ""))
    if len(chiffres) < 9:
        raise ValueError(f"SIREN attendu (9 chiffres), reçu {valeur!r}")
    return chiffres[:9]


def _tronquer(texte: str, limite: int = LIMITE_EXTRAIT) -> str:
    texte = re.sub(r"\s+", " ", texte or "").strip()
    return texte if len(texte) <= limite else texte[: limite - 1] + "…"


def _json_texte(valeur: Any) -> dict[str, Any]:
    """BODACC range des objets JSON dans des chaînes ; renvoie {} si absent ou illisible."""
    if isinstance(valeur, dict):
        return valeur
    if isinstance(valeur, str) and valeur.strip():
        try:
            lu = json.loads(valeur)
        except ValueError:
            return {}
        return lu if isinstance(lu, dict) else {}
    return {}


def _est_entrepreneur_individuel(e: dict[str, Any]) -> bool:
    """Catégorie juridique 1xxx : une personne physique exerce en son nom."""
    return str(e.get("nature_juridique") or "").startswith("1")


def _commune(siege: dict[str, Any]) -> dict[str, Any]:
    return {k: siege.get(k) for k in ("code_postal", "libelle_commune") if siege.get(k)}


def _siege_public(e: dict[str, Any]) -> dict[str, Any]:
    """Le siège d'une société ; réduit à la commune pour un entrepreneur individuel."""
    siege = e.get("siege") or {}
    if _est_entrepreneur_individuel(e):
        return {k: siege.get(k) for k in ("libelle_commune",) if siege.get(k)}
    return {k: siege.get(k) for k in ("adresse", "code_postal", "libelle_commune") if siege.get(k)}


def _nom_societe(e: dict[str, Any], masquer: bool) -> str:
    """Le nom d'un entrepreneur individuel est celui d'une personne : initiales par défaut."""
    nom = e.get("nom_complet") or e.get("nom_raison_sociale") or ""
    if _est_entrepreneur_individuel(e) and masquer:
        dirigeants = [d for d in e.get("dirigeants") or [] if d.get("type_dirigeant") == "personne physique"]
        if dirigeants:
            return f"entrepreneur individuel {initiales(dirigeants[0].get('prenoms'), dirigeants[0].get('nom'))}"
        return "entrepreneur individuel"
    return nom


def _qualite_cac(qualite: Any) -> bool:
    return "commissaire aux comptes" in _norm(qualite)


# ---------------------------------------------------------------------------
# Frise
# ---------------------------------------------------------------------------

def _url_fiche(siren: str) -> str:
    return URL_ANNUAIRE + siren


def evenements_frise(indices: list[Indice]) -> list[dict[str, Any]]:
    """Les indices datés (BODACC, HATVP, sociétés) au format de la frise :
    {date, type, entite, libelle, source_url, extrait}, triés par date."""
    sortie: list[dict[str, Any]] = []
    for i in indices:
        d = i.donnees
        if i.source == "bodacc":
            quand, type_, entite, libelle = d.get("date_parution"), d.get("famille", "autre"), d.get("siren"), d.get("resume")
        elif i.source == "hatvp":
            quand, type_, entite = d.get("date_publication"), "declaration_hatvp", d.get("personne")
            libelle = f"{d.get('type_declaration')} publiée : {d.get('mandat') or d.get('type_mandat')}"
        elif i.source == "recherche-entreprises" and d.get("date_creation"):
            quand, type_, entite = d["date_creation"], "creation", d.get("siren")
            libelle = f"Création de {d.get('nom')}"
        else:
            continue
        if not quand:
            continue
        sortie.append({"date": quand, "type": type_, "entite": entite, "libelle": libelle,
                       "source_url": i.url, "extrait": i.extrait})
    sortie.sort(key=lambda x: (x["date"], x["type"]))
    return sortie


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class Registres:
    """BODACC, Recherche d'entreprises et HATVP, avec cache disque et garde-fous du banc."""

    evenements_frise = staticmethod(evenements_frise)

    def __init__(
        self,
        dossier_cache: str | Path = "cache/registres",
        transport: httpx.BaseTransport | None = None,
        filtre_url: FiltreUrl | None = None,
        date_gel: DateGel = None,
        masquer_personnes: bool = True,
        maintenant: Callable[[], datetime] = lambda: datetime.now(UTC),
        dormir: Callable[[float], None] = time.sleep,
        attente_base: float = 2.0,
        horloge: Callable[[], float] = time.monotonic,
    ):
        self._dossier = Path(dossier_cache)
        self._http = httpx.Client(
            transport=transport, timeout=60.0, follow_redirects=True, headers={"User-Agent": AGENT}
        )
        self.filtre_url = filtre_url
        self.date_gel = _en_datetime(date_gel)
        self.masquer_personnes = masquer_personnes
        self._maintenant = maintenant
        self._dormir = dormir
        self._attente_base = attente_base
        self._horloge = horloge
        self._dernier_appel_entreprises: float | None = None
        self.journal: list[dict[str, str]] = []
        self.appels_reseau = 0
        self._memo_mandats: dict[tuple, dict[str, Any]] = {}
        self._memo_hatvp: tuple[str, list[dict[str, str]]] | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Registres:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- garde-fous

    def _rejeter(self, url: str, raison: str) -> None:
        entree = {"url": url, "raison": raison, "date": self._maintenant().isoformat()}
        self.journal.append(entree)
        journal_logging.warning("appel rejeté (%s) : %s", raison, url)

    def _autorise(self, url: str, filtre_url: FiltreUrl | None) -> bool:
        filtre = filtre_url or self.filtre_url
        if filtre is not None and not filtre(url):
            self._rejeter(url, "refusé par filtre_url")
            return False
        return True

    def _gel(self, date_gel: DateGel) -> datetime | None:
        return _en_datetime(date_gel) if date_gel is not None else self.date_gel

    def _masquer(self, masquer_personnes: bool | None) -> bool:
        return self.masquer_personnes if masquer_personnes is None else masquer_personnes

    def _pause_entreprises(self) -> None:
        """Recherche d'entreprises tolère 7 requêtes par seconde : au moins 0,15 s entre deux."""
        if self._dernier_appel_entreprises is not None:
            reste = PAUSE_ENTREPRISES - (self._horloge() - self._dernier_appel_entreprises)
            if reste > 0:
                self._dormir(reste)
        self._dernier_appel_entreprises = self._horloge()

    # ------------------------------------------------------------------ cache

    def _chemin(self, url: str) -> Path:
        return self._dossier / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.json"

    def _lire(self, url: str) -> dict[str, Any] | None:
        try:
            contenu = json.loads(self._chemin(url).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return contenu if isinstance(contenu, dict) else None

    def _ecrire(self, url: str, statut: int, texte: str) -> dict[str, Any]:
        entree = {"url": url, "statut": statut, "date_lecture": self._maintenant().isoformat(), "corps": texte}
        self._dossier.mkdir(parents=True, exist_ok=True)
        cible = self._chemin(url)
        temporaire = cible.with_suffix(".tmp")
        temporaire.write_text(json.dumps(entree, ensure_ascii=False), encoding="utf-8")
        temporaire.replace(cible)
        return entree

    def _lire_url(
        self,
        url: str,
        filtre_url: FiltreUrl | None,
        *,
        duree_cache: timedelta | None = None,
        nettoyer: Callable[[str], str] | None = None,
    ) -> dict[str, Any] | None:
        """Filtre, puis cache, puis réseau. None si rejeté ou injoignable."""
        if not self._autorise(url, filtre_url):
            return None
        en_cache = self._lire(url)
        if en_cache is not None:
            if duree_cache is None:
                return en_cache
            lu = _lire_date(en_cache.get("date_lecture"))
            if lu is not None and self._maintenant() - lu < duree_cache:
                return en_cache
        perime = en_cache  # une copie trop vieille vaut mieux que rien si le réseau tombe
        entreprises = url.startswith(URL_ENTREPRISES)
        for essai in range(1, ESSAIS_MAX + 1):
            if entreprises:
                self._pause_entreprises()
            self.appels_reseau += 1
            try:
                rep = self._http.get(url)
            except httpx.HTTPError as exc:
                self._rejeter(url, f"injoignable ({type(exc).__name__})")
                return perime
            if rep.status_code in (429, 500, 502, 503, 504):
                if essai < ESSAIS_MAX:
                    attente = self._attente_base * essai
                    retard = rep.headers.get("Retry-After", "")
                    if retard.isdigit():
                        attente = max(attente, min(float(retard), 30.0))
                    self._dormir(attente)
                    continue
                self._rejeter(url, f"erreur HTTP {rep.status_code}")
                return perime
            if rep.status_code in (200, 404):
                texte = rep.text
                if rep.status_code == 200 and nettoyer is not None:
                    texte = nettoyer(texte)
                return self._ecrire(url, rep.status_code, texte)
            self._rejeter(url, f"HTTP {rep.status_code}")
            return perime
        return perime  # pragma: no cover

    @staticmethod
    def _json(entree: dict[str, Any] | None) -> Any:
        if not entree or entree.get("statut") != 200:
            return None
        try:
            return json.loads(entree["corps"])
        except (ValueError, TypeError):
            return None

    def _indice(self, source: str, sujet: str, url: str, entree: dict[str, Any], extrait: str, resume: str,
                donnees: dict[str, Any]) -> Indice:
        date_lecture = _lire_date(entree.get("date_lecture")) or self._maintenant()
        return Indice(source, sujet, url, date_lecture, _tronquer(extrait), resume, donnees)

    # ----------------------------------------------- Recherche d'entreprises

    @staticmethod
    def _url_recherche(**params: Any) -> str:
        propres = {k: v for k, v in params.items() if v not in (None, "")}
        return f"{URL_ENTREPRISES}?{urlencode(propres, quote_via=quote)}"

    def _fiche(self, siren: str, filtre_url: FiltreUrl | None = None) -> tuple[dict[str, Any], dict[str, Any], str] | None:
        """(société, entrée de cache, URL) pour un SIREN, ou None (introuvable, rejeté, injoignable)."""
        url = self._url_recherche(q=siren, per_page=1)
        entree = self._lire_url(url, filtre_url)
        d = self._json(entree)
        if not isinstance(d, dict):
            return None
        for e in d.get("results") or []:
            if e.get("siren") == siren:
                return e, entree, url  # type: ignore[return-value]
        return None

    def _indice_societe(self, e: dict[str, Any], entree: dict[str, Any], url: str, masquer: bool,
                        qualite: str | None = None) -> Indice:
        siege = _siege_public(e)
        nom = _nom_societe(e, masquer)
        donnees = {
            "siren": e.get("siren"), "nom": nom, "date_creation": e.get("date_creation"),
            "etat": e.get("etat_administratif"), "nature_juridique": e.get("nature_juridique"),
            "activite": e.get("activite_principale"), "siege": siege,
        }
        if qualite:
            donnees["qualite"] = qualite
        brut = dict(donnees)
        resume = (f"{nom} (SIREN {e.get('siren')}), créée le {e.get('date_creation') or 'date inconnue'}, "
                  f"siège à {siege.get('libelle_commune') or 'inconnu'}")
        return self._indice("recherche-entreprises", str(e.get("siren")), _url_fiche(str(e.get("siren"))), entree,
                            json.dumps(brut, ensure_ascii=False), resume, donnees)

    # ----------------------------------------------------------------- BODACC

    def _url_bodacc(self, siren: str, decalage: int) -> str:
        params = {
            "where": f'registre like "{siren}"',
            "order_by": "dateparution desc",
            "select": ("id,dateparution,familleavis,familleavis_lib,typeavis_lib,tribunal,ville,registre,"
                       "url_complete,modificationsgenerales,jugement,acte,depot,radiationaurcs"),
            "limit": AVIS_PAR_PAGE,
            "offset": decalage,
        }
        return f"{URL_BODACC_AVIS}?{urlencode(params, quote_via=quote)}"

    @staticmethod
    def _resume_avis(r: dict[str, Any]) -> str:
        """Texte officiel abrégé de l'avis, sans noms de personnes ni détail libre."""
        famille = r.get("familleavis")
        morceaux: list[str] = []
        modif = _json_texte(r.get("modificationsgenerales"))
        jugement = _json_texte(r.get("jugement"))
        acte = _json_texte(r.get("acte"))
        depot = _json_texte(r.get("depot"))
        radiation = _json_texte(r.get("radiationaurcs"))
        if famille in ("creation", "immatriculation"):
            morceaux.append((acte.get("creation") or {}).get("categorieCreation") or "Création")
            if acte.get("dateImmatriculation"):
                morceaux.append(f"immatriculation le {acte['dateImmatriculation']}")
        elif famille == "vente":
            vente = acte.get("vente") or {}
            morceaux.append(vente.get("categorieVente") or "Vente ou cession")
            if vente.get("dateEffet"):
                morceaux.append(f"effet : {vente['dateEffet']}")
        elif famille in ("collective", "conciliation", "retablissement_professionnel"):
            morceaux.extend(x for x in (jugement.get("famille"), jugement.get("nature"), jugement.get("date")) if x)
        elif famille == "modification":
            if modif.get("descriptif"):
                morceaux.append(modif["descriptif"])
            if modif.get("dateEffet"):
                morceaux.append(f"effet : {modif['dateEffet']}")
        elif famille == "dpc":
            morceaux.append("Dépôt des comptes")
            if depot.get("dateCloture"):
                morceaux.append(f"exercice clos le {depot['dateCloture']}")
            if depot.get("typeDepot"):
                morceaux.append(depot["typeDepot"])
        elif famille == "radiation" or radiation:
            morceaux.append("Radiation du registre du commerce et des sociétés")
        if not morceaux:
            morceaux.append(r.get("familleavis_lib") or "Avis")
        texte = " · ".join(str(m) for m in morceaux)
        if r.get("typeavis_lib") and r.get("typeavis_lib") != "Avis initial":
            texte = f"{r['typeavis_lib']} : {texte}"
        return _tronquer(texte, LIMITE_RESUME)

    def bodacc(self, siren: str, filtre_url: FiltreUrl | None = None, date_gel: DateGel = None) -> list[Indice]:
        """Un `Indice` par avis du BODACC (du plus récent au plus ancien), jamais après la date de gel."""
        siren = _siren(siren)
        gel = self._gel(date_gel)
        indices: list[Indice] = []
        ecartes = 0
        total = 0
        for page in range(AVIS_PAGES_MAX):
            url = self._url_bodacc(siren, page * AVIS_PAR_PAGE)
            entree = self._lire_url(url, filtre_url)
            d = self._json(entree)
            if not isinstance(d, dict):
                break
            total = int(d.get("total_count") or 0)
            resultats = d.get("results") or []
            for r in resultats:
                registres = {re.sub(r"\D", "", str(x)) for x in r.get("registre") or []}
                if siren not in registres:
                    continue  # « like » est un filtre de mots : on revérifie le numéro
                parution = _lire_date(r.get("dateparution"))
                if gel is not None and parution is not None and parution > gel:
                    ecartes += 1
                    continue
                indices.append(self._indice_avis(siren, r, entree, total))
            if len(resultats) < AVIS_PAR_PAGE or (page + 1) * AVIS_PAR_PAGE >= total:
                break
        if ecartes:
            self._rejeter(self._url_bodacc(siren, 0), f"{ecartes} avis postérieur(s) à la date de gel ignoré(s)")
        return indices

    def _indice_avis(self, siren: str, r: dict[str, Any], entree: dict[str, Any], total: int) -> Indice:
        famille_brute = r.get("familleavis") or ""
        famille = FAMILLES_BODACC.get(famille_brute, "autre")
        resume_officiel = self._resume_avis(r)
        url_avis = r.get("url_complete") or (
            f"https://www.bodacc.fr/pages/annonces-commerciales-detail/?q.id=id:{r.get('id')}")
        donnees = {
            "siren": siren, "date_parution": r.get("dateparution"), "famille": famille,
            "famille_bodacc": famille_brute, "type_avis": r.get("typeavis_lib"),
            "tribunal": r.get("tribunal"), "resume": resume_officiel, "url_avis": url_avis,
            "id_avis": r.get("id"), "avis_total_source": total,
        }
        extrait = json.dumps({k: donnees[k] for k in ("id_avis", "date_parution", "famille_bodacc", "type_avis",
                                                      "tribunal", "resume")}, ensure_ascii=False)
        resume = f"{r.get('dateparution')} · {r.get('familleavis_lib') or famille} · {resume_officiel}"
        return self._indice("bodacc", siren, url_avis, entree, extrait, resume, donnees)

    # ------------------------------------------------------- vie de société

    @staticmethod
    def _signaux_depuis_avis(a: Indice) -> list[dict[str, Any]]:
        d = a.donnees
        texte = _norm(d.get("resume"))
        types: list[str] = []
        achat = re.match(r"(achat|acquisition)\b", texte)  # la société est l'acheteuse : pas une cession de sa part
        if (d["famille"] == "vente" and not achat) or (d["famille"] == "modification" and re.search(r"\bcession\b", texte)):
            types.append("cession")
        if d["famille"] == "procedure_collective" or d.get("famille_bodacc") == "conciliation":
            types.append("procedure_collective")
        if d["famille"] == "radiation":
            types.append("radiation")
        if d["famille"] == "modification":
            if re.search(r"\b(dirigeant|gerant|gerance|president|administrat\w*|directeur general|directoire|"
                         r"conseil de surveillance|representant permanent)\b", texte):
                types.append("changement_dirigeant")
            if re.search(r"\b(siege|etablissement principal|transfert)\b", texte):
                types.append("transfert_siege")
        return [{
            "type": t, "date": d["date_parution"],
            "preuve": {"url": d["url_avis"], "extrait": d["resume"]},
            "lecture": LECTURE_INNOCENTE[t],
        } for t in types]

    def vie_de_societe(self, siren: str, filtre_url: FiltreUrl | None = None, date_gel: DateGel = None) -> dict[str, Any]:
        """La vie datée d'une société : événements triés, signaux à vérifier, chiffres publiés."""
        siren = _siren(siren)
        gel = self._gel(date_gel)
        sortie: dict[str, Any] = {"siren": siren, "evenements": [], "signaux": [], "finances": {}, "indices": []}
        fiche = self._fiche(siren, filtre_url)
        e: dict[str, Any] = {}
        if fiche is not None:
            e, entree_fiche, url_fiche = fiche
            creation = _lire_date(e.get("date_creation"))
            if gel is not None and creation is not None and creation > gel:
                self._rejeter(url_fiche, f"société {siren} créée après la date de gel")
                sortie["ignore"] = "société créée après la date de gel"
                return sortie
            sortie["nom"] = _nom_societe(e, self.masquer_personnes)
            sortie["indices"].append(self._indice_societe(e, entree_fiche, url_fiche, self.masquer_personnes))
        avis = self.bodacc(siren, filtre_url, date_gel)
        sortie["indices"].extend(avis)
        sortie["avis_total_source"] = avis[0].donnees["avis_total_source"] if avis else 0
        evenements: list[dict[str, Any]] = []
        if e.get("date_creation"):
            evenements.append({"date": e["date_creation"], "type": "creation", "entite": siren,
                               "libelle": "Création de l'unité légale (répertoire Sirene)",
                               "source_url": _url_fiche(siren), "extrait": f"date_creation : {e['date_creation']}"})
        fermeture = _lire_date(e.get("date_fermeture"))
        if fermeture is not None and (gel is None or fermeture <= gel):
            evenements.append({"date": e["date_fermeture"], "type": "radiation", "entite": siren,
                               "libelle": "Fermeture de l'unité légale (répertoire Sirene)",
                               "source_url": _url_fiche(siren), "extrait": f"date_fermeture : {e['date_fermeture']}"})
        for a in avis:
            evenements.append({"date": a.donnees["date_parution"], "type": a.donnees["famille"], "entite": siren,
                               "libelle": a.donnees["resume"], "source_url": a.donnees["url_avis"],
                               "extrait": a.extrait})
            sortie["signaux"].extend(self._signaux_depuis_avis(a))
        evenements.sort(key=lambda x: (x["date"] or "", x["type"]))
        sortie["evenements"] = evenements
        sortie["signaux"].sort(key=lambda s: s["date"] or "")
        signal_comptes = self._signal_comptes(siren, e, avis, gel)
        if signal_comptes:
            sortie["signaux"].append(signal_comptes)
        # chiffres publiés : une année n'est « vue » que si elle est antérieure à l'année du gel
        finances = e.get("finances") or {}
        retenues = {}
        for annee, f in sorted(finances.items()):
            if gel is not None and str(annee).isdigit() and int(annee) >= gel.year:
                continue
            retenues[str(annee)] = {"chiffre_affaires": (f or {}).get("ca"), "resultat_net": (f or {}).get("resultat_net")}
        sortie["finances"] = retenues
        if retenues:
            sortie["finances_source_url"] = _url_fiche(siren)
        if gel is not None:
            sortie["avertissement_gel"] = ("Les avis et chiffres postérieurs à la date de gel sont écartés ; "
                                           "l'état actuel de la fiche (dirigeants, fermeture) n'est pas daté.")
        return sortie

    def _signal_comptes(self, siren: str, e: dict[str, Any], avis: list[Indice], gel: datetime | None) -> dict[str, Any] | None:
        """Aucun avis de dépôt de comptes depuis 24 mois pour une société commerciale active."""
        if not e or e.get("etat_administratif") != "A" or not str(e.get("nature_juridique") or "").startswith(("5", "6")):
            return None
        reference = gel or self._maintenant()
        creation = _lire_date(e.get("date_creation"))
        limite = reference - timedelta(days=30 * MOIS_COMPTES)
        if creation is None or creation > limite:
            return None
        depots = [a for a in avis if a.donnees["famille"] == "depot_comptes"]
        recents = [a for a in depots if (_lire_date(a.donnees["date_parution"]) or limite) >= limite]
        if recents:
            return None
        dernier = depots[0].donnees["date_parution"] if depots else None
        return {
            "type": "comptes_non_deposes", "date": reference.date().isoformat(),
            "preuve": {"url": depots[0].donnees["url_avis"] if depots else _url_fiche(siren),
                       "extrait": (f"dernier avis de dépôt des comptes : {dernier}" if dernier
                                   else f"aucun avis de dépôt des comptes au BODACC ({len(avis)} avis lus)")},
            "lecture": LECTURE_INNOCENTE["comptes_non_deposes"],
        }

    # --------------------------------------------------------------- mandats

    def mandats_avec_comptes(self, nom: str, prenoms: str | None = None, *, annee_naissance: int | str | None = None,
                             filtre_url: FiltreUrl | None = None, date_gel: DateGel = None,
                             masquer_personnes: bool | None = None) -> dict[str, Any]:
        """Les sociétés où une personne est dirigeante, avec le décompte de ce qu'on a écarté."""
        masquer = self._masquer(masquer_personnes)
        gel = self._gel(date_gel)
        annee = str(annee_naissance) if annee_naissance not in (None, "") else None
        cle = (_norm(nom), _norm(prenoms), annee, masquer, gel)
        if filtre_url is None and cle in self._memo_mandats:
            return self._memo_mandats[cle]
        nom_n = _norm(nom)
        prenoms_n = _norm(prenoms).split()
        sujet = initiales(prenoms, nom) if masquer else f"{prenoms or ''} {nom}".strip()
        indices: list[Indice] = []
        compte = {"homonymes_ecartes": 0, "annee_inconnue": 0, "entreprises_individuelles_ecartees": 0,
                  "apres_gel_ignorees": 0, "resultats_api": 0}
        vues: set[str] = set()
        incomplet = False
        for page in range(1, MANDATS_PAGES_MAX + 1):
            url = self._url_recherche(nom_personne=nom, prenoms_personne=prenoms, type_personne="dirigeant",
                                      per_page=MANDATS_PAR_PAGE, page=page)
            entree = self._lire_url(url, filtre_url)
            d = self._json(entree)
            if not isinstance(d, dict):
                break
            resultats = d.get("results") or []
            compte["resultats_api"] += len(resultats)
            for e in resultats:
                siren = str(e.get("siren"))
                if siren in vues:
                    continue
                correspondants = [x for x in e.get("dirigeants") or [] if x.get("type_dirigeant") == "personne physique"
                                  and _norm(x.get("nom")) == nom_n and self._prenom_compatible(x.get("prenoms"), prenoms_n)]
                if not correspondants:
                    continue
                vues.add(siren)
                dirigeant = correspondants[0]
                annee_dirigeant = str(dirigeant.get("annee_de_naissance") or "") or None
                if annee and annee_dirigeant and annee_dirigeant != annee:
                    compte["homonymes_ecartes"] += 1
                    continue
                if _est_entrepreneur_individuel(e):
                    compte["entreprises_individuelles_ecartees"] += 1
                    continue
                creation = _lire_date(e.get("date_creation"))
                if gel is not None and creation is not None and creation > gel:
                    compte["apres_gel_ignorees"] += 1
                    self._rejeter(url, f"société {siren} créée après la date de gel")
                    continue
                if annee and not annee_dirigeant:
                    compte["annee_inconnue"] += 1
                ind = self._indice_societe(e, entree, url, masquer, qualite=dirigeant.get("qualite"))
                ind.donnees["personne"] = sujet
                ind.donnees["annee_verifiee"] = bool(annee and annee_dirigeant)
                indices.append(ind)
            if len(resultats) < MANDATS_PAR_PAGE or page >= int(d.get("total_pages") or 1):
                break
        else:
            incomplet = True
        for ind in indices:
            ind.donnees.update(homonymes_ecartes=compte["homonymes_ecartes"], lecture_incomplete=incomplet)
        resultat = {"personne": sujet, "mandats": indices, "nb_mandats": len(indices), "au_moins": incomplet,
                    **compte}
        if filtre_url is None:
            self._memo_mandats[cle] = resultat
        return resultat

    @staticmethod
    def _prenom_compatible(prenoms_dirigeant: Any, prenoms_requete: list[str]) -> bool:
        if not prenoms_requete:
            return True
        return bool(set(_norm(prenoms_dirigeant).split()) & set(prenoms_requete))

    def mandats(self, nom: str, prenoms: str | None = None, *, annee_naissance: int | str | None = None,
                filtre_url: FiltreUrl | None = None, date_gel: DateGel = None,
                masquer_personnes: bool | None = None) -> list[Indice]:
        """Un `Indice` par société où la personne est dirigeante. Un homonyme dont l'année de
        naissance diffère est écarté ; le décompte est dans `donnees["homonymes_ecartes"]` de chaque
        indice et dans `mandats_avec_comptes()`."""
        return self.mandats_avec_comptes(nom, prenoms, annee_naissance=annee_naissance, filtre_url=filtre_url,
                                         date_gel=date_gel, masquer_personnes=masquer_personnes)["mandats"]

    # ---------------------------------------------------- chaîne de direction

    def chaine_de_controle(self, siren: str, profondeur_max: int = 3, seuil_mandats: int = 10,
                           filtre_url: FiltreUrl | None = None, date_gel: DateGel = None,
                           masquer_personnes: bool | None = None) -> dict[str, Any]:
        """Dirigeants de la société, puis ceux de chaque dirigeant personne morale, sur `profondeur_max`
        niveaux (la société de départ est le niveau 1). Les commissaires aux comptes ne sont pas
        remontés. Chaque chemin se termine par « personne physique (initiales) » ou « s'arrête ici :
        zone d'ombre ». Ce sont les dirigeants déclarés au registre, pas les détenteurs du capital : la
        chaîne ne prouve aucun contrôle."""
        siren = _siren(siren)
        masquer = self._masquer(masquer_personnes)
        parametres = {"profondeur_max": profondeur_max, "seuil_mandats": seuil_mandats, "filtre_url": filtre_url,
                      "date_gel": date_gel, "masquer": masquer}
        racine = self._noeud(siren, 1, {siren}, parametres)
        fins: list[dict[str, Any]] = []
        signaux: list[dict[str, Any]] = []
        self._aplatir(racine, [racine.get("nom") or siren], fins, signaux)
        sortie = {
            "siren": siren, "profondeur_max": profondeur_max, "seuil_mandats": seuil_mandats,
            "racine": racine, "fins": fins, "signaux": signaux,
            "lecture": ("Dirigeants déclarés au registre du commerce, pas détenteurs du capital : cette chaîne "
                        "ne prouve aucun contrôle."),
        }
        if self._gel(date_gel) is not None:
            sortie["avertissement_gel"] = "La composition des organes de direction est celle d'aujourd'hui, non datée."
        return sortie

    def _noeud(self, siren: str, niveau: int, vus: set[str], p: dict[str, Any]) -> dict[str, Any]:
        fiche = self._fiche(siren, p["filtre_url"])
        if fiche is None:
            return {"siren": siren, "nom": None, "niveau": niveau, "dirigeants": [], "fin": FIN_OMBRE,
                    "raison": "fiche introuvable, refusée par le filtre ou injoignable"}
        e, entree, url = fiche
        gel = self._gel(p["date_gel"])
        creation = _lire_date(e.get("date_creation"))
        if gel is not None and creation is not None and creation > gel:
            self._rejeter(url, f"société {siren} créée après la date de gel")
            return {"siren": siren, "nom": None, "niveau": niveau, "dirigeants": [], "fin": FIN_OMBRE,
                    "raison": "société créée après la date de gel"}
        indice = self._indice_societe(e, entree, url, p["masquer"])
        noeud: dict[str, Any] = {"siren": siren, "nom": indice.donnees["nom"], "niveau": niveau, "indice": indice,
                                 "dirigeants": []}
        for d in e.get("dirigeants") or []:
            noeud["dirigeants"].append(self._maillon(d, niveau, vus, p, indice))
        return noeud

    def _maillon(self, d: dict[str, Any], niveau: int, vus: set[str], p: dict[str, Any], indice_societe: Indice) -> dict[str, Any]:
        qualite = d.get("qualite")
        if d.get("type_dirigeant") == "personne morale":
            base = {"type": "personne morale", "siren": d.get("siren"), "denomination": d.get("denomination"),
                    "qualite": qualite, "indice": indice_societe}
            if _qualite_cac(qualite):
                return {**base, "type": "commissaire_aux_comptes", "remonte": False}
            sir = re.sub(r"\D", "", str(d.get("siren") or ""))
            if len(sir) != 9:
                return {**base, "fin": FIN_OMBRE, "raison": "personne morale sans SIREN (État, organisme public ou société étrangère)"}
            if sir in vus:
                return {**base, "fin": FIN_OMBRE, "raison": "boucle : cette société figure déjà plus haut dans la chaîne"}
            if niveau >= p["profondeur_max"]:
                return {**base, "fin": FIN_OMBRE, "raison": f"profondeur maximale ({p['profondeur_max']}) atteinte"}
            enfant = self._noeud(sir, niveau + 1, vus | {sir}, p)
            return {**base, "enfant": enfant}
        # personne physique
        masquer = p["masquer"]
        nom_public = initiales(d.get("prenoms"), d.get("nom")) if masquer else \
            f"{(d.get('prenoms') or '').title()} {d.get('nom') or ''}".strip()
        if _qualite_cac(qualite):
            return {"type": "commissaire_aux_comptes", "personne": nom_public, "qualite": qualite, "remonte": False,
                    "indice": indice_societe}
        comptes = self.mandats_avec_comptes(d.get("nom") or "", d.get("prenoms"),
                                            annee_naissance=d.get("annee_de_naissance"),
                                            filtre_url=p["filtre_url"], date_gel=p["date_gel"],
                                            masquer_personnes=masquer)
        maillon: dict[str, Any] = {
            "type": "personne physique", "personne": nom_public, "qualite": qualite,
            "nb_mandats": comptes["nb_mandats"], "mandats_au_moins": comptes["au_moins"],
            "homonymes_ecartes": comptes["homonymes_ecartes"],
            "fin": FIN_PERSONNE.format(nom_public), "indice": indice_societe, "signaux": [],
        }
        if comptes["nb_mandats"] > p["seuil_mandats"]:
            maillon["signaux"].append({
                "type": "nombreux_mandats", "libelle": LIBELLE_NOMBREUX_MANDATS,
                "nb_mandats": comptes["nb_mandats"], "seuil": p["seuil_mandats"],
                "lecture": LECTURE_INNOCENTE["nombreux_mandats"],
                "preuve": [i.url for i in comptes["mandats"][:5]],
            })
        return maillon

    def _aplatir(self, noeud: dict[str, Any], chemin: list[str], fins: list[dict[str, Any]],
                 signaux: list[dict[str, Any]]) -> None:
        if "fin" in noeud:  # nœud sans fiche : fin de chemin
            fins.append({"chemin": chemin, "fin": noeud["fin"], "raison": noeud.get("raison")})
            return
        avant = len(fins)
        for m in noeud["dirigeants"]:
            if m["type"] == "commissaire_aux_comptes":
                continue
            suite = chemin + [m.get("personne") or m.get("denomination") or "?"]
            if "enfant" in m:
                self._aplatir(m["enfant"], chemin + [m["enfant"].get("nom") or m.get("denomination") or "?"], fins, signaux)
                continue
            fins.append({"chemin": suite, "fin": m["fin"], "raison": m.get("raison")})
            for s in m.get("signaux", []):
                signaux.append({**s, "chemin": suite})
        if len(fins) == avant:  # aucun dirigeant à suivre : le chemin s'arrête sans personne au bout
            fins.append({"chemin": chemin, "fin": FIN_OMBRE, "raison": "aucun dirigeant (hors commissaires aux comptes) déclaré"})

    # ------------------------------------------------------- codomiciliation

    def codomiciliation(self, siren: str, limite: int = 50, filtre_url: FiltreUrl | None = None,
                        date_gel: DateGel = None, masquer_personnes: bool | None = None) -> dict[str, Any]:
        """Les autres entités immatriculées à la même adresse de siège. Au-delà de 20, c'est
        probablement une domiciliation commerciale : les liens sont faibles."""
        siren = _siren(siren)
        masquer = self._masquer(masquer_personnes)
        gel = self._gel(date_gel)
        fiche = self._fiche(siren, filtre_url)
        if fiche is None:
            return {"siren": siren, "entites": [], "ignore": "fiche introuvable, refusée ou injoignable"}
        e, _entree, _url = fiche
        if _est_entrepreneur_individuel(e):
            return {"siren": siren, "entites": [], "ignore": "entrepreneur individuel : adresse privée, non recherchée"}
        adresse = (e.get("siege") or {}).get("adresse")
        if not adresse:
            return {"siren": siren, "entites": [], "ignore": "adresse du siège inconnue"}
        cible = _norm(adresse)
        entites: list[dict[str, Any]] = []
        nb_au_meme_siege = 0
        pages = max(1, -(-limite // MANDATS_PAR_PAGE))
        for page in range(1, pages + 1):
            url = self._url_recherche(q=adresse, per_page=MANDATS_PAR_PAGE, page=page)
            entree = self._lire_url(url, filtre_url)
            d = self._json(entree)
            if not isinstance(d, dict):
                break
            resultats = d.get("results") or []
            trouves_page = 0
            for r in resultats:
                if _norm((r.get("siege") or {}).get("adresse")) != cible or r.get("siren") == siren:
                    continue
                if _est_entrepreneur_individuel(r):
                    continue  # l'adresse d'un entrepreneur individuel est celle d'une personne
                creation = _lire_date(r.get("date_creation"))
                if gel is not None and creation is not None and creation > gel:
                    continue
                trouves_page += 1
                nb_au_meme_siege += 1
                if len(entites) < limite:
                    entites.append({"siren": r.get("siren"), "nom": _nom_societe(r, masquer),
                                    "date_creation": r.get("date_creation"), "etat": r.get("etat_administratif"),
                                    "url": _url_fiche(str(r.get("siren")))})
            if len(resultats) < MANDATS_PAR_PAGE or trouves_page < len(resultats) // 2:
                break  # le classement met les adresses identiques en tête : le reste est du bruit
        bruyant = nb_au_meme_siege > SEUIL_BRUIT_DOMICILIATION
        sortie: dict[str, Any] = {
            "siren": siren, "adresse_commune": (e.get("siege") or {}).get("libelle_commune"),
            "nb_entites": nb_au_meme_siege, "entites": entites, "bruit": bruyant,
        }
        if bruyant:
            sortie["lecture"] = "domiciliation commerciale probable, liens faibles"
        return sortie

    # ------------------------------------------------------------------ HATVP

    @staticmethod
    def _nettoyer_hatvp(texte: str) -> str:
        """Retire la colonne `url_photo` avant tout stockage : on ne la lit ni ne la garde."""
        lignes = list(csv.reader(io.StringIO(texte), delimiter=";"))
        if not lignes:
            return texte
        sortie = io.StringIO()
        ecrivain = csv.writer(sortie, delimiter=";", lineterminator="\n")
        garder = [i for i, c in enumerate(lignes[0]) if c.strip().lstrip("﻿") != "url_photo"]
        for ligne in lignes:
            ecrivain.writerow([ligne[i] for i in garder if i < len(ligne)])
        return sortie.getvalue()

    def _lignes_hatvp(self, filtre_url: FiltreUrl | None) -> tuple[dict[str, Any], list[dict[str, str]]] | None:
        entree = self._lire_url(URL_HATVP_LISTE, filtre_url, duree_cache=DUREE_CACHE_HATVP, nettoyer=self._nettoyer_hatvp)
        if not entree or entree.get("statut") != 200:
            return None
        cle = entree["date_lecture"]
        if self._memo_hatvp and self._memo_hatvp[0] == cle:
            return entree, self._memo_hatvp[1]
        texte = entree["corps"].lstrip("﻿")
        lecteur = csv.DictReader(io.StringIO(texte), delimiter=";")
        garder = ("civilite", "prenom", "nom", "type_mandat", "qualite", "type_document", "departement",
                  "date_publication", "date_depot", "nom_fichier", "url_dossier", "statut_publication")
        lignes = [{k: (ligne.get(k) or "").strip() for k in garder} for ligne in lecteur]
        self._memo_hatvp = (cle, lignes)
        return entree, lignes

    def hatvp(self, nom: str, prenom: str | None = None, filtre_url: FiltreUrl | None = None,
              date_gel: DateGel = None) -> list[Indice]:
        """Déclarations publiées d'un responsable public (liste ouverte de la HATVP). Un `Indice` par
        déclaration, la plus récente d'abord. Le nom d'un responsable public n'est pas masqué : c'est
        l'objet de la recherche."""
        lecture = self._lignes_hatvp(filtre_url)
        if lecture is None:
            return []
        entree, lignes = lecture
        gel = self._gel(date_gel)
        jetons_nom = set(_norm(nom).split())
        jetons_prenom = set(_norm(prenom).split()) if prenom else set()
        if not jetons_nom:
            raise ValueError("hatvp() attend un nom.")
        trouvees: list[Indice] = []
        non_publiees = 0
        apres_gel = 0
        for ligne in lignes:
            if not jetons_nom <= set(_norm(ligne["nom"]).split()):
                continue
            if jetons_prenom and not jetons_prenom <= set(_norm(ligne["prenom"]).split()):
                continue
            publication = _lire_date(ligne["date_publication"])
            if publication is None:
                non_publiees += 1  # déposée mais pas (encore) publiée
                continue
            if gel is not None and publication > gel:
                apres_gel += 1
                continue
            trouvees.append(self._indice_hatvp(ligne, entree))
        trouvees.sort(key=lambda i: i.donnees["date_publication"], reverse=True)
        for i in trouvees:
            i.donnees["non_publiees"] = non_publiees
        if apres_gel:
            self._rejeter(URL_HATVP_LISTE, f"{apres_gel} déclaration(s) publiée(s) après la date de gel ignorée(s)")
        return trouvees

    def _indice_hatvp(self, ligne: dict[str, str], entree: dict[str, Any]) -> Indice:
        code = ligne["type_document"]
        libelle = LIBELLES_HATVP.get(code, code or "déclaration")
        personne = f"{ligne['prenom']} {ligne['nom']}".strip()
        url = URL_HATVP_SITE + ligne["url_dossier"] if ligne["url_dossier"].startswith("/") else ligne["url_dossier"]
        donnees = {
            "personne": personne, "type_document": code, "type_declaration": libelle,
            "type_mandat": ligne["type_mandat"], "mandat": ligne["qualite"],
            "departement": ligne["departement"] or None, "date_publication": ligne["date_publication"],
            "date_depot": ligne["date_depot"] or None, "url_dossier": url,
        }
        extrait = json.dumps({**donnees, "source": "liste.csv de la HATVP"}, ensure_ascii=False)
        resume = (f"{personne} : {libelle} publiée le {ligne['date_publication']}"
                  f"{' · ' + ligne['qualite'] if ligne['qualite'] else ''}")
        return self._indice("hatvp", _norm(personne), url, entree, extrait, resume, donnees)
