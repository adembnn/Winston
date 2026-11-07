"""Enrichissement des pivots par des sources ouvertes, sans clé et gratuites.

Quatre sources, chacune renvoie des `Indice` (URL source, date de lecture, extrait brut) :
- RDAP (via rdap.org) : date de création du domaine, registrar, serveurs DNS ;
- crt.sh : domaines « frères » qui partagent un certificat avec le domaine ;
- API Recherche d'entreprises (data.gouv.fr) : société, SIREN, création, activité, siège, dirigeants ;
- Wayback CDX : première et dernière capture d'un domaine.

Garde-fous :
- chaque réponse est gardée sur disque (`cache/infra/`) : la même requête ne repart pas ;
- `filtre_url` (fonction URL -> vrai/faux) et `date_gel` (date limite du banc) s'appliquent à
  TOUTES les fonctions réseau ; un appel rejeté est journalisé et ne touche ni le réseau ni le
  cache. Wayback ne renvoie rien de postérieur à la date de gel, et les autres sources écartent
  ce qui date d'après ;
- les dirigeants personnes physiques sont masqués (initiales) par défaut ;
- on ne lit jamais le titulaire (« registrant ») d'un domaine : seulement le registrar.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from enquete.reseau import Preuve

journal_logging = logging.getLogger("enquete.infra")

URL_RDAP = "https://rdap.org/domain/"
URL_CRTSH = "https://crt.sh/"
URL_ENTREPRISES = "https://recherche-entreprises.api.gouv.fr/search"
URL_WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"

AGENT = "enquete-osint/0.1 (recherche journalistique, lectures publiques)"
ESSAIS_MAX = 2
LIMITE_EXTRAIT = 1500
LIMITE_SOEURS = 200

FiltreUrl = Callable[[str], bool]
DateGel = date | datetime | str | None


# ---------------------------------------------------------------------------
# Indice
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Indice:
    """Ce qu'une source ouverte a répondu : de quoi rouvrir la lecture, pas une conclusion."""

    source: str  # rdap, crt.sh, recherche-entreprises, wayback, lien-bio
    sujet: str  # domaine, SIREN... ce sur quoi on a interrogé
    url: str  # l'URL interrogée
    date_lecture: datetime
    extrait: str  # extrait brut de la réponse (personnes physiques masquées)
    resume: str  # une phrase lisible
    donnees: dict[str, Any] = field(default_factory=dict)

    def vers_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "sujet": self.sujet,
            "url": self.url,
            "date_lecture": self.date_lecture.isoformat(),
            "extrait": self.extrait,
            "resume": self.resume,
            "donnees": self.donnees,
        }

    def vers_preuve(self) -> Preuve:
        """Le même indice comme `Preuve` du réseau (extrait brut, URL source)."""
        return Preuve(
            url=self.url,
            extrait=self.extrait or self.resume,
            source_donnee=self.source,
            date_collecte=self.date_lecture,
        )


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def _en_datetime(valeur: DateGel) -> datetime | None:
    if valeur is None or valeur == "":
        return None
    if isinstance(valeur, datetime):
        return valeur if valeur.tzinfo else valeur.replace(tzinfo=UTC)
    if isinstance(valeur, date):
        return datetime(valeur.year, valeur.month, valeur.day, 23, 59, 59, tzinfo=UTC)
    texte = str(valeur).strip().replace("Z", "+00:00")
    try:
        return _en_datetime(datetime.fromisoformat(texte)) if "T" in texte or " " in texte else _en_datetime(date.fromisoformat(texte))
    except ValueError:
        raise ValueError(f"Date de gel illisible : {valeur!r}") from None


def _lire_date(texte: Any) -> datetime | None:
    """Date ISO renvoyée par une source ; None si absente ou illisible."""
    if not isinstance(texte, str) or not texte:
        return None
    try:
        return _en_datetime(texte)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Masquage des personnes physiques
# ---------------------------------------------------------------------------

def initiales(prenoms: str | None, nom: str | None) -> str:
    """« Jeanne-Marie Claire DUPONT-MARTIN » -> « J.-M. D.-M. » : le premier prénom, puis le nom."""
    def _init(mot: str) -> str:
        parties = [p for p in re.split(r"[-\s]+", mot.strip()) if p and p[0].isalpha()]
        return "-".join(f"{p[0].upper()}." for p in parties)

    premier = (prenoms or "").replace(",", " ").split()
    nom_sans_parentheses = re.sub(r"\(.*?\)", " ", nom or "")
    morceaux = [_init(premier[0]) if premier else "", _init(nom_sans_parentheses)]
    return " ".join(m for m in morceaux if m) or "?"


def _masquer_dirigeant(d: dict[str, Any], masquer: bool) -> dict[str, Any]:
    """Un dirigeant de l'API. Les personnes morales restent entières."""
    if d.get("type_dirigeant") != "personne physique":
        return {k: d.get(k) for k in ("type_dirigeant", "siren", "denomination", "qualite") if d.get(k)}
    qualite = d.get("qualite")
    if masquer:
        return {"type_dirigeant": "personne physique", "initiales": initiales(d.get("prenoms"), d.get("nom")),
                "qualite": qualite}
    return {"type_dirigeant": "personne physique", "prenoms": d.get("prenoms"), "nom": d.get("nom"),
            "qualite": qualite, "annee_de_naissance": d.get("annee_de_naissance")}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class Infra:
    """Quatre lectures publiques, avec cache disque et garde-fous du banc."""

    def __init__(
        self,
        dossier_cache: str | Path = "cache/infra",
        transport: httpx.BaseTransport | None = None,
        filtre_url: FiltreUrl | None = None,
        date_gel: DateGel = None,
        masquer_personnes: bool = True,
        maintenant: Callable[[], datetime] = lambda: datetime.now(UTC),
        dormir: Callable[[float], None] = time.sleep,
        attente_base: float = 2.0,
    ):
        self._dossier = Path(dossier_cache)
        self._http = httpx.Client(
            transport=transport, timeout=40.0, follow_redirects=True, headers={"User-Agent": AGENT}
        )
        self.filtre_url = filtre_url
        self.date_gel = _en_datetime(date_gel)
        self.masquer_personnes = masquer_personnes
        self._maintenant = maintenant
        self._dormir = dormir
        self._attente_base = attente_base
        self.journal: list[dict[str, str]] = []
        self.appels_reseau = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Infra:
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

    def _lire_url(self, url: str, filtre_url: FiltreUrl | None) -> dict[str, Any] | None:
        """Cache d'abord, réseau ensuite. None si rejeté ou injoignable."""
        if not self._autorise(url, filtre_url):
            return None
        en_cache = self._lire(url)
        if en_cache is not None:
            return en_cache
        for essai in range(1, ESSAIS_MAX + 1):
            self.appels_reseau += 1
            try:
                rep = self._http.get(url)
            except httpx.HTTPError as exc:
                self._rejeter(url, f"injoignable ({type(exc).__name__})")
                return None
            if rep.status_code in (429, 500, 502, 503, 504):
                if essai < ESSAIS_MAX:
                    self._dormir(self._attente_base * essai)
                    continue
                self._rejeter(url, f"erreur HTTP {rep.status_code}")
                return None
            if rep.status_code == 404 or rep.status_code == 200:
                return self._ecrire(url, rep.status_code, rep.text)
            self._rejeter(url, f"HTTP {rep.status_code}")
            return None
        return None  # pragma: no cover

    def _json(self, entree: dict[str, Any] | None) -> Any:
        if not entree or entree.get("statut") != 200:
            return None
        try:
            return json.loads(entree["corps"])
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _tronquer(texte: str, limite: int = LIMITE_EXTRAIT) -> str:
        return texte if len(texte) <= limite else texte[: limite - 1] + "…"

    def _indice(self, source: str, sujet: str, entree: dict[str, Any], extrait: str, resume: str,
                donnees: dict[str, Any]) -> Indice:
        date_lecture = _lire_date(entree.get("date_lecture")) or self._maintenant()
        return Indice(source, sujet, entree["url"], date_lecture, self._tronquer(extrait), resume, donnees)

    # ------------------------------------------------------------------- RDAP

    def rdap(self, domaine: str, filtre_url: FiltreUrl | None = None, date_gel: DateGel = None) -> Indice | None:
        """Création du domaine, registrar, serveurs DNS. Le titulaire n'est jamais lu."""
        domaine = domaine.strip().lower().rstrip(".")
        url = URL_RDAP + quote(domaine)
        entree = self._lire_url(url, filtre_url)
        d = self._json(entree)
        if not isinstance(d, dict):
            return None
        gel = self._gel(date_gel)
        evenements: dict[str, str] = {}
        for e in d.get("events") or []:
            action, quand = e.get("eventAction"), _lire_date(e.get("eventDate"))
            if not action or quand is None:
                continue
            if gel is not None and quand > gel:
                continue  # postérieur à la date de gel : on ne l'a pas « vu »
            evenements[action] = e["eventDate"]
        creation = evenements.get("registration")
        if gel is not None and creation is None:
            self._rejeter(url, "création absente ou postérieure à la date de gel")
            return None
        registrar = None
        for ent in d.get("entities") or []:
            if "registrar" in (ent.get("roles") or []):
                for champ in (ent.get("vcardArray") or [None, []])[1]:
                    if champ and champ[0] == "fn":
                        registrar = champ[3]
                        break
                if registrar:
                    break
        dns = sorted({n.get("ldhName", "").lower() for n in d.get("nameservers") or [] if n.get("ldhName")})
        donnees = {"domaine": domaine, "creation": creation, "registrar": registrar, "dns": dns,
                   "evenements": evenements}
        extrait = json.dumps({"ldhName": d.get("ldhName"), "events": [
            {"eventAction": a, "eventDate": q} for a, q in evenements.items()],
            "registrar": registrar, "nameservers": dns}, ensure_ascii=False)
        resume = (f"{domaine} créé le {creation[:10] if creation else 'date inconnue'}"
                  f", registrar {registrar or 'inconnu'}, DNS {', '.join(dns[:3]) or 'inconnus'}")
        return self._indice("rdap", domaine, entree, extrait, resume, donnees)

    # ----------------------------------------------------------------- crt.sh

    def crtsh(self, domaine: str, filtre_url: FiltreUrl | None = None, date_gel: DateGel = None) -> Indice | None:
        """Domaines frères : autres noms portés par les certificats du domaine."""
        domaine = domaine.strip().lower().rstrip(".")
        url = f"{URL_CRTSH}?q={quote(domaine)}&output=json"
        entree = self._lire_url(url, filtre_url)
        d = self._json(entree)
        if not isinstance(d, list):
            return None
        gel = self._gel(date_gel)
        noms: set[str] = set()
        premiere: str | None = None
        nb_certificats = 0
        for cert in d:
            debut = _lire_date(cert.get("not_before"))
            if gel is not None and debut is not None and debut > gel:
                continue
            nb_certificats += 1
            if cert.get("not_before") and (premiere is None or cert["not_before"] < premiere):
                premiere = cert["not_before"]
            for nom in str(cert.get("name_value") or "").splitlines():
                nom = nom.strip().lower().lstrip("*.")
                if nom and "@" not in nom:  # une adresse e-mail dans un certificat : jamais retenue
                    noms.add(nom)
        sous_domaines = sorted(n for n in noms if n == domaine or n.endswith("." + domaine))
        autres = sorted(n for n in noms if n not in sous_domaines)
        freres = sorted({_registrable(n) for n in autres} - {_registrable(domaine)})
        donnees = {"domaine": domaine, "certificats": nb_certificats, "premier_certificat": premiere,
                   "sous_domaines": sous_domaines[:LIMITE_SOEURS], "freres": freres[:LIMITE_SOEURS],
                   "noms_freres": autres[:LIMITE_SOEURS]}
        extrait = json.dumps({"q": domaine, "certificats": nb_certificats,
                              "noms_freres": autres[:30], "sous_domaines": sous_domaines[:30]}, ensure_ascii=False)
        resume = (f"{nb_certificats} certificats pour {domaine} ; {len(freres)} domaine(s) frère(s)"
                  + (f" : {', '.join(freres[:5])}" if freres else ""))
        return self._indice("crt.sh", domaine, entree, extrait, resume, donnees)

    # ------------------------------------------------- Recherche d'entreprises

    def entreprise(
        self,
        siren: str | None = None,
        nom: str | None = None,
        filtre_url: FiltreUrl | None = None,
        date_gel: DateGel = None,
        masquer_personnes: bool | None = None,
    ) -> list[Indice]:
        """Sociétés trouvées par SIREN (ou par nom). Un `Indice` par société."""
        requete = (siren or nom or "").strip()
        if not requete:
            raise ValueError("entreprise() attend un SIREN ou un nom.")
        masquer = self.masquer_personnes if masquer_personnes is None else masquer_personnes
        url = f"{URL_ENTREPRISES}?q={quote(requete)}&per_page=5"
        entree = self._lire_url(url, filtre_url)
        d = self._json(entree)
        if not isinstance(d, dict):
            return []
        gel = self._gel(date_gel)
        indices: list[Indice] = []
        for e in d.get("results") or []:
            if siren and re.sub(r"\D", "", siren) != e.get("siren"):
                continue
            creation = _lire_date(e.get("date_creation"))
            if gel is not None and creation is not None and creation > gel:
                self._rejeter(url, f"société {e.get('siren')} créée après la date de gel")
                continue
            siege = e.get("siege") or {}
            dirigeants = [_masquer_dirigeant(x, masquer) for x in e.get("dirigeants") or []]
            donnees = {
                "nom": e.get("nom_complet") or e.get("nom_raison_sociale"),
                "siren": e.get("siren"),
                "date_creation": e.get("date_creation"),
                "activite": e.get("activite_principale"),
                "nature_juridique": e.get("nature_juridique"),
                "etat": e.get("etat_administratif"),
                "siege": {k: siege.get(k) for k in ("adresse", "code_postal", "libelle_commune") if siege.get(k)},
                "dirigeants": dirigeants,
            }
            brut = {k: e.get(k) for k in ("siren", "nom_complet", "date_creation", "activite_principale",
                                          "nature_juridique", "etat_administratif")}
            brut["siege"] = donnees["siege"]
            brut["dirigeants"] = dirigeants
            resume = (f"{donnees['nom']} (SIREN {donnees['siren']}), créée le {donnees['date_creation']}, "
                      f"activité {donnees['activite']}, siège à {siege.get('libelle_commune') or 'inconnu'}, "
                      f"{len(dirigeants)} dirigeant(s)")
            indices.append(self._indice("recherche-entreprises", str(e.get("siren")), entree,
                                        json.dumps(brut, ensure_ascii=False), resume, donnees))
        return indices

    # ------------------------------------------------------------ Wayback CDX

    def wayback(self, domaine: str, filtre_url: FiltreUrl | None = None, date_gel: DateGel = None) -> Indice | None:
        """Première et dernière capture du domaine, jamais après la date de gel."""
        domaine = domaine.strip().lower().rstrip(".")
        gel = self._gel(date_gel)
        base = f"{URL_WAYBACK_CDX}?url={quote(domaine)}&output=json&fl=timestamp,original,statuscode"
        fin = f"&to={gel.strftime('%Y%m%d%H%M%S')}" if gel is not None else ""
        url_premiere = f"{base}{fin}&limit=1"
        url_derniere = f"{base}{fin}&limit=-1"
        premiere = self._cdx(url_premiere, filtre_url, gel)
        if premiere is None:
            return None
        derniere = self._cdx(url_derniere, filtre_url, gel) or premiere
        entree = self._lire(url_premiere) or {"url": url_premiere}
        if not premiere:
            return None
        donnees = {"domaine": domaine, "premiere_capture": premiere[0], "derniere_capture": derniere[0],
                   "premiere_url": premiere[1], "derniere_url": derniere[1]}
        extrait = json.dumps({"premiere": list(premiere), "derniere": list(derniere)}, ensure_ascii=False)
        resume = (f"{domaine} : première capture {_date_cdx(premiere[0])}, "
                  f"dernière {_date_cdx(derniere[0])}")
        return self._indice("wayback", domaine, entree, extrait, resume, donnees)

    def _cdx(self, url: str, filtre_url: FiltreUrl | None, gel: datetime | None) -> tuple[str, str, str] | None:
        """Une ligne CDX (horodatage, URL, statut), ou () s'il n'y a aucune capture, None si rejeté."""
        entree = self._lire_url(url, filtre_url)
        d = self._json(entree)
        if d is None:
            return None
        lignes = [l for l in d[1:] if isinstance(l, list) and len(l) >= 3] if isinstance(d, list) else []
        if gel is not None:  # ceinture et bretelles : le paramètre `to` a pu être ignoré
            lignes = [l for l in lignes if _lire_cdx(l[0]) is not None and _lire_cdx(l[0]) <= gel]
        return tuple(lignes[0][:3]) if lignes else ()  # type: ignore[return-value]

    # -------------------------------------------------- Dépliage des liens en bio

    def deplier_lien_bio(self, url: str, filtre_url: FiltreUrl | None = None, date_gel: DateGel = None) -> list[str]:
        """Les liens sortants d'une page de liens (Linktree, Beacons...). Meilleur effort : une page
        rendue par JavaScript peut ne rien livrer. `date_gel` n'a pas de prise ici : une page de
        liens n'a pas d'historique, on journalise la lecture."""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if self._gel(date_gel) is not None:
            journal_logging.info("deplier_lien_bio : page actuelle lue malgré la date de gel (%s)", url)
        entree = self._lire_url(url, filtre_url)
        if not entree or entree.get("statut") != 200:
            return []
        hote_page = urlsplit(url).hostname or ""
        trouves: list[str] = []
        texte = entree["corps"].replace("\\u002F", "/").replace("\\/", "/")
        for m in _RE_LIEN_HTML.finditer(texte):
            lien = m.group(1).rstrip(".,;)")
            hote = urlsplit(lien).hostname or ""
            if not hote or _meme_service(hote, hote_page) or hote.endswith(_HOTES_TECHNIQUES):
                continue
            if urlsplit(lien).path.lower().endswith(_EXTENSIONS_FICHIERS):
                continue
            if lien not in trouves:
                trouves.append(lien)
        return trouves[:100]

    def deplier_pages(
        self,
        pages: Iterable[str],
        filtre_url: FiltreUrl | None = None,
        date_gel: DateGel = None,
        seuil_gabarit: float = 0.3,
    ) -> dict[str, list[str]]:
        """Déplie plusieurs pages de liens et retire le gabarit du service : un lien présent sur au
        moins `seuil_gabarit` des pages d'un même service (et sur 3 pages au moins) est une
        publicité ou un bloc commun injecté par le service, pas un choix du titulaire."""
        pages = list(dict.fromkeys(pages))
        brut = {p: self.deplier_lien_bio(p, filtre_url, date_gel) for p in pages}
        par_service: dict[str, list[str]] = {}
        for page in pages:
            par_service.setdefault(page.split("/", 1)[0].lower(), []).append(page)
        propre: dict[str, list[str]] = {}
        for service, groupe in par_service.items():
            compte: dict[str, int] = {}
            for page in groupe:
                for lien in set(brut[page]):
                    compte[lien] = compte.get(lien, 0) + 1
            limite = max(3, seuil_gabarit * len(groupe))
            gabarit = {lien for lien, n in compte.items() if n >= limite}
            for page in groupe:
                propre[page] = [lien for lien in brut[page] if lien not in gabarit]
        return propre

    # --------------------------------------------------------------- enrichir

    def enrichir(self, pivots: Iterable[Any], filtre_url: FiltreUrl | None = None,
                 date_gel: DateGel = None) -> list[Any]:
        """Ajoute des `Indice` aux pivots utiles : domaines (RDAP, crt.sh, Wayback), SIREN et SIRET
        (Recherche d'entreprises). Renvoie de nouveaux pivots, les autres inchangés. Les pivots
        banals ne coûtent aucun appel."""
        sortie = []
        for p in pivots:
            if getattr(p, "banal", False):
                sortie.append(p)
                continue
            indices: list[Indice] = []
            if p.type == "domaine":
                for lecture in (self.rdap, self.crtsh, self.wayback):
                    ind = lecture(p.valeur, filtre_url=filtre_url, date_gel=date_gel)
                    if ind is not None:
                        indices.append(ind)
            elif p.type in ("siren", "siret"):
                indices.extend(self.entreprise(siren=p.valeur[:9], filtre_url=filtre_url, date_gel=date_gel))
            sortie.append(replace(p, indices=list(p.indices) + indices) if indices else p)
        return sortie


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

_RE_LIEN_HTML = re.compile(r"""(?:href=["']|["']url["']\s*:\s*["'])(https?://[^"'\s<>\\]+)""", re.I)
_HOTES_TECHNIQUES = (
    "w3.org", "schema.org", "googleapis.com", "gstatic.com", "googletagmanager.com", "google-analytics.com",
    "cloudflare.com", "cloudfront.net", "sentry.io", "apple.com", "play.google.com", "jsdelivr.net",
    "unpkg.com", "fontawesome.com", "facebook.net", "doubleclick.net",
)

_EXTENSIONS_FICHIERS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".css", ".js", ".woff", ".woff2",
                        ".ttf", ".map", ".json", ".xml")

_SLD_COMPOSES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.br", "com.au", "co.jp", "co.nz", "com.tr", "com.mx",
    "co.za", "com.ar", "co.in", "com.cn", "com.pt", "com.es",
}


def _registrable(hote: str) -> str:
    """Domaine enregistrable approximatif (sans liste des suffixes publics complète)."""
    morceaux = [m for m in hote.lower().strip(".").split(".") if m]
    if len(morceaux) <= 2:
        return ".".join(morceaux)
    if ".".join(morceaux[-2:]) in _SLD_COMPOSES:
        return ".".join(morceaux[-3:])
    return ".".join(morceaux[-2:])


def _meme_service(hote: str, hote_page: str) -> bool:
    return _registrable(hote) == _registrable(hote_page)


def _lire_cdx(horodatage: str) -> datetime | None:
    try:
        return datetime.strptime(horodatage[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _date_cdx(horodatage: str) -> str:
    quand = _lire_cdx(horodatage)
    return quand.date().isoformat() if quand else horodatage
