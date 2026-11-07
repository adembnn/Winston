"""Suivi des flux d'argent sur les chaînes publiques (Bitcoin, EVM, TRON, Solana).

Sources, toutes gratuites et sans clé (vérifiées le 7 nov. 2026) :
- Bitcoin : mempool.space (Esplora), secours Blockstream (même schéma) ;
- Ethereum, Optimism, Celo (et Base, Polygon, Arbitrum, Gnosis, voir ci-dessous) : Blockscout v2 ;
- TRON : TronGrid pour les transactions, Tronscan (ancien domaine apilist.tronscan.org) pour
  l'étiquette publique ;
- Solana : RPC public mainnet-beta (getSignaturesForAddress, getTransaction) ;
- sanctions : liste SDN du Trésor américain (SDN.CSV, adresses « Digital Currency Address »).

Limites connues de ces sources :
- les instances Blockscout de Base, Polygon, Arbitrum et Gnosis sont derrière un contrôle
  anti-robot (Cloudflare) : elles répondent 403 depuis un script. Le module le détecte et lève
  `ChainesBloqueeError` au lieu d'inventer un résultat. BNB Chain n'a pas d'instance Blockscout
  publique : non prise en charge ;
- TronGrid et le RPC Solana limitent le débit sans clé : réessais avec attente, puis erreur typée ;
- Solana : `getTransaction` coûte un appel par transaction, on lit au plus `max_detail_sol`
  transactions (les plus récentes dans la fenêtre) ;
- les transferts internes EVM (contrats qui envoient de l'ETH) ne sont pas lus ;
- Bitcoin : le montant d'un flux est réparti au prorata des entrées quand plusieurs
  adresses financent la transaction (champ `approx`).

Règles de l'enquête, appliquées dans le code :
- un portefeuille n'est jamais attribué à une personne ; seules les étiquettes publiques de
  l'explorateur sont reprises, avec leur source ;
- une plateforme d'échange est marquée « doit répondre » : elle peut identifier le titulaire
  sur réquisition ;
- `date_gel` écarte toute transaction postérieure ; `filtre_url` écarte toute preuve dont
  l'URL est refusée ; chaque rejet est journalisé (logger `enquete.chaines`) et gardé dans
  `client.rejets`.

Chaque résultat public est un dict `{type, valeur, url_source, date_lecture, extrait, donnees}`.
Les montants sont des chaînes décimales (aucune perte de précision, JSON direct).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

journal = logging.getLogger("enquete.chaines")

MEMPOOL = "https://mempool.space"
BLOCKSTREAM = "https://blockstream.info"
TRONGRID = "https://api.trongrid.io"
TRONSCAN_API = "https://apilist.tronscan.org"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
OFAC_CSV = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV"
OFAC_DETAIL = "https://sanctionssearch.ofac.treas.gov/Details.aspx?id={}"

# Instances Blockscout (explorateur public) par chaîne EVM : base, monnaie native.
EVM = {
    "eth": ("https://eth.blockscout.com", "ETH"),
    "optimism": ("https://optimism.blockscout.com", "ETH"),
    "celo": ("https://celo.blockscout.com", "CELO"),
    "base": ("https://base.blockscout.com", "ETH"),
    "polygon": ("https://polygon.blockscout.com", "POL"),
    "arbitrum": ("https://arbitrum.blockscout.com", "ETH"),
    "gnosis": ("https://gnosis.blockscout.com", "xDAI"),
}
ALIAS = {
    "bitcoin": "btc", "xbt": "btc", "ethereum": "eth", "ether": "eth", "trx": "tron",
    "solana": "sol", "op": "optimism", "matic": "polygon", "arb": "arbitrum", "xdai": "gnosis",
}
CHAINES = ("btc", "tron", "sol", *EVM)

# Contrats officiels de l'USDT : un jeton homonyme sur un autre contrat est presque toujours un faux.
USDT_OFFICIEL = {
    "eth": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "tron": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
    "sol": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
JETONS_SOL = {
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
}

ESSAIS_MAX = 4
MENTION_ATTRIBUTION = (
    "Un portefeuille n'est jamais attribué à une personne : seul un tiers qui tient le registre "
    "du titulaire (plateforme d'échange, justice) peut l'identifier."
)
TEXTE_DOIT_REPONDRE = (
    "Doit répondre : plateforme d'échange, elle peut identifier le titulaire de ce compte "
    "sur réquisition judiciaire."
)

_NOMS_PLATEFORMES = re.compile(
    r"\b(exchange|cex|binance|coinbase|kraken|okx|okex|bybit|kucoin|bitfinex|huobi|htx|gate\.?io|"
    r"bitstamp|gemini|crypto\.com|bitget|mexc|revolut|bitpanda|bitvavo|bittrex|poloniex|upbit|"
    r"bithumb|coinone|bitmex|deribit|ftx|whitebit|bingx|lbank|robinhood|bitso|btse|coinex|"
    r"phemex|woo x|htx)\b",
    re.IGNORECASE,
)
_ADRESSE_OFAC = re.compile(r"Digital Currency Address - (\S+) ([^;\s]+)")
_RE_BTC = re.compile(r"^(bc1[a-z0-9]{11,87}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$")
_RE_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# ----------------------------------------------------------------------------- erreurs


class ChainesError(Exception):
    """Erreur générique. `statut` vaut None hors réponse HTTP."""

    def __init__(self, message: str, statut: int | None = None):
        super().__init__(message)
        self.statut = statut


class ChainesAdresseInvalideError(ChainesError, ValueError):
    """Adresse mal formée pour la chaîne demandée."""


class ChainesNonSupporteeError(ChainesError, ValueError):
    """Chaîne inconnue ou sans source gratuite."""


class ChainesIntrouvableError(ChainesError):
    """L'explorateur ne connaît pas cette ressource (404)."""


class ChainesLimiteError(ChainesError):
    """Débit limité par la source (429), réessais épuisés."""


class ChainesBloqueeError(ChainesError):
    """La source répond par un contrôle anti-robot au lieu de données."""


# ----------------------------------------------------------------------- utilitaires


def _b58_decode(texte: str) -> bytes | None:
    nombre = 0
    for car in texte:
        position = _B58.find(car)
        if position < 0:
            return None
        nombre = nombre * 58 + position
    brut = nombre.to_bytes((nombre.bit_length() + 7) // 8, "big")
    zeros = len(texte) - len(texte.lstrip("1"))
    return b"\x00" * zeros + brut


def _b58_encode(brut: bytes) -> str:
    nombre = int.from_bytes(brut, "big")
    sortie = ""
    while nombre:
        nombre, reste = divmod(nombre, 58)
        sortie = _B58[reste] + sortie
    return "1" * (len(brut) - len(brut.lstrip(b"\x00"))) + sortie


def _somme_controle(charge: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(charge).digest()).digest()[:4]


def tron_hex_vers_base58(adresse_hex: str) -> str:
    """Convertit une adresse TRON hexadécimale (préfixe 41) en base58, forme des explorateurs."""
    charge = bytes.fromhex(adresse_hex)
    return _b58_encode(charge + _somme_controle(charge))


def _tron_valide(adresse: str) -> bool:
    brut = _b58_decode(adresse)
    return bool(brut) and len(brut) == 25 and brut[0] == 0x41 and brut[-4:] == _somme_controle(brut[:-4])


def normaliser_chaine(chaine: str) -> str:
    cle = chaine.strip().lower()
    cle = ALIAS.get(cle, cle)
    if cle not in CHAINES:
        raise ChainesNonSupporteeError(
            f"Chaîne non prise en charge : {chaine!r} (disponibles : {', '.join(CHAINES)})."
        )
    return cle


def verifier_adresse(chaine: str, adresse: str) -> str:
    """Rend l'adresse nettoyée, ou lève ChainesAdresseInvalideError."""
    chaine = normaliser_chaine(chaine)
    adresse = adresse.strip()
    if chaine == "btc":
        bon = bool(_RE_BTC.match(adresse))
    elif chaine == "tron":
        bon = _tron_valide(adresse)
    elif chaine == "sol":
        brut = _b58_decode(adresse)
        bon = brut is not None and len(brut) == 32
    else:
        bon = bool(_RE_EVM.match(adresse))
    if not bon:
        raise ChainesAdresseInvalideError(f"Adresse invalide pour la chaîne {chaine} : {adresse!r}.")
    return adresse


def _cle(chaine: str, adresse: str) -> str:
    """Forme de comparaison : les adresses EVM ne distinguent pas la casse."""
    return adresse.lower() if chaine in EVM else adresse


def url_adresse(chaine: str, adresse: str) -> str:
    chaine = normaliser_chaine(chaine)
    if chaine == "btc":
        return f"{MEMPOOL}/address/{adresse}"
    if chaine == "tron":
        return f"https://tronscan.org/#/address/{adresse}"
    if chaine == "sol":
        return f"https://explorer.solana.com/address/{adresse}"
    return f"{EVM[chaine][0]}/address/{adresse}"


def url_transaction(chaine: str, identifiant: str) -> str:
    chaine = normaliser_chaine(chaine)
    if chaine == "btc":
        return f"{MEMPOOL}/tx/{identifiant}"
    if chaine == "tron":
        return f"https://tronscan.org/#/transaction/{identifiant}"
    if chaine == "sol":
        return f"https://explorer.solana.com/tx/{identifiant}"
    return f"{EVM[chaine][0]}/tx/{identifiant}"


def _decimal(valeur: Any) -> Decimal:
    try:
        return Decimal(str(valeur))
    except Exception:  # noqa: BLE001 - valeur absente ou illisible : on la compte pour zéro
        return Decimal(0)


def _txt(montant: Decimal) -> str:
    """Décimal en texte positionnel (jamais de notation scientifique)."""
    if montant == 0:
        return "0"
    texte = format(montant.normalize(), "f")
    return texte


def _iso(instant: datetime | None) -> str | None:
    return instant.astimezone(timezone.utc).isoformat() if instant else None


def lire_date_gel(date_gel: date | datetime | str | None) -> datetime | None:
    """Une date seule vaut minuit UTC de ce jour : tout ce qui est strictement après est écarté."""
    if date_gel is None:
        return None
    if isinstance(date_gel, datetime):
        instant = date_gel
    elif isinstance(date_gel, date):
        instant = datetime(date_gel.year, date_gel.month, date_gel.day)
    else:
        try:
            instant = datetime.fromisoformat(str(date_gel).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"date_gel illisible : {date_gel!r} (attendu AAAA-MM-JJ ou ISO 8601).") from exc
    return instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant.astimezone(timezone.utc)


def _instant(secondes: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(secondes), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _instant_iso(texte: Any) -> datetime | None:
    if not texte:
        return None
    try:
        instant = datetime.fromisoformat(str(texte).replace("Z", "+00:00"))
    except ValueError:
        return None
    return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- modèle interne


@dataclass
class Mouvement:
    """Un transfert lu sur la chaîne : de, vers, montant, actif, avec sa transaction."""

    id: str
    date: datetime | None
    de: str
    vers: str
    montant: Decimal
    actif: str
    contrat: str | None
    url: str
    approx: bool = False


@dataclass
class Releve:
    """Tout ce que l'on sait d'une adresse à la date de gel, avant mise en forme."""

    chaine: str
    adresse: str
    url: str
    mouvements: list[Mouvement] = field(default_factory=list)
    totaux: dict[tuple[str, str | None], dict[str, Any]] = field(default_factory=dict)
    tx_ids: set[str] = field(default_factory=set)
    premiere: datetime | None = None
    derniere: datetime | None = None
    nb_declare: int | None = None
    partiel: bool = False
    totaux_exacts: bool = False
    etiquettes: list[dict[str, str]] = field(default_factory=list)
    etiquettes_tiers: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    est_contrat: bool | None = None
    avertissements: list[str] = field(default_factory=list)
    rejets_gel: int = 0
    rejets_filtre: int = 0
    lu_le: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def compter(self, actif: str, contrat: str | None, recu: Decimal = Decimal(0), envoye: Decimal = Decimal(0)) -> None:
        ligne = self.totaux.setdefault((actif, contrat), {"recu": Decimal(0), "envoye": Decimal(0), "nb": 0})
        ligne["recu"] += recu
        ligne["envoye"] += envoye
        ligne["nb"] += 1

    def horodater(self, instant: datetime | None) -> None:
        if instant is None:
            return
        if self.premiere is None or instant < self.premiere:
            self.premiere = instant
        if self.derniere is None or instant > self.derniere:
            self.derniere = instant


def _est_plateforme(etiquettes: list[dict[str, str]], est_contrat: bool | None) -> bool:
    for e in etiquettes:
        if e.get("categorie") in ("note", "scam"):
            continue
        if e.get("slug") in ("exchange", "cex"):
            return True
        if not est_contrat and _NOMS_PLATEFORMES.search(e.get("nom", "")):
            return True
    return False


# ---------------------------------------------------------------------- sanctions


class ListeOFAC:
    """Adresses de monnaies numériques de la liste SDN, indexées par adresse."""

    def __init__(self, entrees: dict[str, list[dict[str, str]]], liste_du: str | None, source: str):
        self._entrees = entrees
        self.liste_du = liste_du
        self.source = source

    @classmethod
    def depuis_csv(cls, texte: str, liste_du: str | None = None, source: str = OFAC_CSV) -> ListeOFAC:
        entrees: dict[str, list[dict[str, str]]] = defaultdict(list)
        for ligne in csv.reader(io.StringIO(texte)):
            if len(ligne) < 12 or "Digital Currency Address" not in ligne[-1]:
                continue
            for monnaie, adresse in _ADRESSE_OFAC.findall(ligne[-1]):
                entrees[cls.cle(adresse)].append({
                    "ent_num": ligne[0].strip(),
                    "nom": ligne[1].strip(),
                    "type": ligne[2].strip(),
                    "programme": ligne[3].strip(),
                    "monnaie": monnaie,
                    "url": OFAC_DETAIL.format(ligne[0].strip()),
                })
        return cls(dict(entrees), liste_du, source)

    @staticmethod
    def cle(adresse: str) -> str:
        return adresse.lower() if adresse.startswith("0x") else adresse

    def controle(self, adresse: str) -> dict[str, Any]:
        trouvees = self._entrees.get(self.cle(adresse), [])
        return {
            "statut": "oui" if trouvees else "non",
            "entrees": trouvees,
            "liste_du": self.liste_du,
            "source": self.source,
            "avertissement": "Liste lue à sa date de téléchargement : une désignation peut être "
                             "postérieure à la date de gel de l'enquête.",
        }

    def __len__(self) -> int:
        return len(self._entrees)


# -------------------------------------------------------------------------- client


class ClientChaines:
    """Client synchrone avec cache disque, réessais et rejet journalisé des preuves refusées."""

    def __init__(
        self,
        dossier_cache: str | Path = "cache/chaines",
        transport: httpx.BaseTransport | None = None,
        attente_base: float = 1.0,
        dormir: Callable[[float], None] = time.sleep,
        duree_cache: float | None = 86400.0,
        maintenant: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        filtre_url: Callable[[str], bool] | None = None,
        max_pages: int = 10,
        max_detail_sol: int = 30,
        duree_ofac: float = 86400.0,
        intervalle: float = 0.15,
    ):
        self._dossier = Path(dossier_cache)
        self._http = httpx.Client(
            transport=transport, timeout=60.0, follow_redirects=True,
            headers={"User-Agent": "enquete-osint/0.1 (journalisme)"},
        )
        self._attente_base = attente_base
        self._dormir = dormir
        self._duree_cache = duree_cache
        self._maintenant = maintenant
        self.filtre_url = filtre_url
        self.max_pages = max_pages
        self.max_detail_sol = max_detail_sol
        self._duree_ofac = duree_ofac
        self._intervalle = intervalle
        self._derniers_appels: dict[str, float] = {}
        self._ofac: ListeOFAC | None = None
        self._lectures: list[str] = []
        self.rejets: list[dict[str, str]] = []

    def __repr__(self) -> str:
        return f"ClientChaines(cache={str(self._dossier)!r}, rejets={len(self.rejets)})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ClientChaines:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ cache

    @staticmethod
    def _empreinte(methode: str, url: str, params: Any, corps: Any) -> str:
        brut = json.dumps([methode, url, params, corps], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(brut.encode("utf-8")).hexdigest()

    def _lire_cache(self, empreinte: str) -> tuple[Any, str] | None:
        try:
            contenu = json.loads((self._dossier / f"{empreinte}.json").read_text("utf-8"))
            lu_le = contenu["lu_le"]
            reponse = contenu["reponse"]
        except (OSError, ValueError, KeyError, TypeError):
            return None  # absent ou abîmé : on refait l'appel
        if self._duree_cache is not None:
            instant = _instant_iso(lu_le)
            if instant is None or (self._maintenant() - instant).total_seconds() > self._duree_cache:
                return None
        return reponse, lu_le

    def _ecrire_cache(self, empreinte: str, methode: str, url: str, params: Any, corps: Any, reponse: Any, lu_le: str) -> None:
        self._dossier.mkdir(parents=True, exist_ok=True)
        cible = self._dossier / f"{empreinte}.json"
        temporaire = cible.with_suffix(".tmp")
        temporaire.write_text(
            json.dumps({"methode": methode, "url": url, "params": params, "corps": corps,
                        "lu_le": lu_le, "reponse": reponse}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        temporaire.replace(cible)  # écriture atomique

    # -------------------------------------------------------------- transport

    def _envoyer(self, methode: str, url: str, params: dict | None, corps: Any) -> httpx.Response:
        hote = httpx.URL(url).host
        for essai in range(1, ESSAIS_MAX + 1):
            attente = self._intervalle - (time.monotonic() - self._derniers_appels.get(hote, -1e9))
            if attente > 0:
                self._dormir(attente)  # politesse : TronGrid et le RPC Solana coupent les rafales
            self._derniers_appels[hote] = time.monotonic()
            try:
                rep = self._http.request(methode, url, params=params, json=corps)
            except httpx.HTTPError as exc:
                raise ChainesError(f"Source injoignable ({httpx.URL(url).host}) : {type(exc).__name__}") from exc
            if rep.status_code in (429, 500, 502, 503, 504) and essai < ESSAIS_MAX:
                self._dormir(self._attente_base * 2 ** (essai - 1))
                continue
            return rep
        raise AssertionError("inatteignable")  # pragma: no cover

    def _requete(self, methode: str, url: str, params: dict | None = None, corps: Any = None) -> Any:
        """JSON d'une source, depuis le cache si possible. Les erreurs sont typées."""
        empreinte = self._empreinte(methode, url, params, corps)
        en_cache = self._lire_cache(empreinte)
        if en_cache is not None:
            self._lectures.append(en_cache[1])
            return en_cache[0]
        rep = self._envoyer(methode, url, params, corps)
        hote = httpx.URL(url).host
        statut = rep.status_code
        if statut == 404:
            raise ChainesIntrouvableError(f"{hote} ne connaît pas cette ressource (404).", statut)
        if statut == 403 or (statut == 200 and rep.text.lstrip()[:1] == "<"):
            raise ChainesBloqueeError(
                f"{hote} refuse l'accès automatique (contrôle anti-robot, HTTP {statut}).", statut
            )
        if statut == 429:
            raise ChainesLimiteError(f"{hote} limite le débit (429), réessais épuisés.", statut)
        if statut >= 400:
            raise ChainesError(f"{hote} répond HTTP {statut}.", statut)
        try:
            reponse = rep.json()
        except ValueError as exc:
            raise ChainesError(f"Réponse de {hote} illisible (JSON attendu).", statut) from exc
        if isinstance(reponse, dict) and isinstance(reponse.get("error"), dict):  # JSON-RPC
            raise ChainesError(f"{hote} : {reponse['error'].get('message', 'erreur RPC')}.")
        lu_le = self._maintenant().astimezone(timezone.utc).isoformat()
        self._ecrire_cache(empreinte, methode, url, params, corps, reponse, lu_le)
        self._lectures.append(lu_le)
        return reponse

    def _get(self, url: str, params: dict | None = None) -> Any:
        return self._requete("GET", url, params=params)

    def _get_avec_secours(self, bases: tuple[str, ...], chemin: str) -> Any:
        """Essaie chaque base à la suite ; une 404 ou une adresse refusée n'est pas rejouée."""
        derniere: ChainesError | None = None
        for base in bases:
            try:
                return self._get(base + chemin)
            except (ChainesIntrouvableError, ChainesAdresseInvalideError):
                raise
            except ChainesError as exc:
                if exc.statut is not None and 400 <= exc.statut < 429:
                    raise
                journal.warning("Source %s en échec (%s), passage au secours.", base, exc)
                derniere = exc
        assert derniere is not None
        raise derniere

    # --------------------------------------------------------------- garde-fous

    def _rejeter(self, url: str, motif: str) -> None:
        journal.info("Rejet (%s) : %s", motif, url)
        self.rejets.append({"url": url, "motif": motif})

    def _accepter(
        self, rel: Releve, url: str, date_tx: datetime | None, gel: datetime | None,
        filtre_url: Callable[[str], bool] | None,
    ) -> bool:
        """Applique le gel et le filtre d'URL à une transaction ; compte et journalise les rejets."""
        if gel is not None and (date_tx is None or date_tx > gel):
            rel.rejets_gel += 1
            self._rejeter(url, "postérieure à date_gel" if date_tx else "non confirmée, date inconnue avec date_gel")
            return False
        filtre = filtre_url or self.filtre_url
        if filtre is not None and not filtre(url):
            rel.rejets_filtre += 1
            self._rejeter(url, "refusée par filtre_url")
            return False
        return True

    # ----------------------------------------------------------------- sanctions

    def sanctions(self) -> ListeOFAC | None:
        """Liste OFAC en mémoire, téléchargée au plus une fois par `duree_ofac` ; None si injoignable."""
        if self._ofac is not None:
            return self._ofac
        fichier = self._dossier / "ofac-sdn.csv"
        recent = False
        try:
            age = self._maintenant().timestamp() - fichier.stat().st_mtime
            recent = age < self._duree_ofac
        except OSError:
            pass
        if not recent:
            try:
                rep = self._envoyer("GET", OFAC_CSV, None, None)
                if rep.status_code != 200 or not rep.content:
                    raise ChainesError(f"OFAC répond HTTP {rep.status_code}.", rep.status_code)
                self._dossier.mkdir(parents=True, exist_ok=True)
                fichier.write_bytes(rep.content)
            except ChainesError as exc:
                journal.warning("Liste OFAC non téléchargée : %s", exc)
        try:
            texte = fichier.read_text(encoding="utf-8", errors="replace")
            liste_du = datetime.fromtimestamp(fichier.stat().st_mtime, tz=timezone.utc).date().isoformat()
        except OSError:
            return None
        self._ofac = ListeOFAC.depuis_csv(texte, liste_du)
        return self._ofac

    def controle_sanction(self, adresse: str) -> dict[str, Any]:
        liste = self.sanctions()
        if liste is None:
            return {"statut": "inconnu", "entrees": [], "liste_du": None, "source": OFAC_CSV,
                    "avertissement": "Liste OFAC indisponible : le contrôle n'a pas pu être fait."}
        return liste.controle(adresse)

    # --------------------------------------------------------------- relevés

    def releve(
        self, chaine: str, adresse: str, date_gel: date | datetime | str | None = None,
        filtre_url: Callable[[str], bool] | None = None, max_pages: int | None = None,
    ) -> Releve:
        chaine = normaliser_chaine(chaine)
        adresse = verifier_adresse(chaine, adresse)
        gel = lire_date_gel(date_gel)
        pages = max_pages or self.max_pages
        debut = len(self._lectures)
        rel = Releve(chaine=chaine, adresse=adresse, url=url_adresse(chaine, adresse))
        if chaine == "btc":
            self._releve_btc(rel, gel, filtre_url, pages)
        elif chaine == "tron":
            self._releve_tron(rel, gel, filtre_url, pages)
        elif chaine == "sol":
            self._releve_sol(rel, gel, filtre_url, pages)
        else:
            self._releve_evm(rel, gel, filtre_url, pages)
        lectures = self._lectures[debut:]
        rel.lu_le = min(lectures) if lectures else self._maintenant().astimezone(timezone.utc).isoformat()
        if gel is not None and rel.partiel:
            rel.avertissements.append(
                "Historique lu en partie seulement (pages plafonnées) : le gel peut cacher des "
                "transactions plus anciennes que celles lues."
            )
        return rel

    # --- Bitcoin

    def _btc(self, chemin: str) -> Any:
        return self._get_avec_secours((MEMPOOL, BLOCKSTREAM), chemin)

    def _btc_transactions(self, adresse: str, pages: int):
        """Rend (déclaré, générateur de lots) : transactions de l'adresse, les plus récentes d'abord."""
        info = self._btc(f"/api/address/{adresse}")
        declare = info["chain_stats"]["tx_count"] + info["mempool_stats"]["tx_count"]

        def lots():
            dernier = None
            confirmees = 0
            for _ in range(pages):
                chemin = f"/api/address/{adresse}/txs" + (f"/chain/{dernier}" if dernier else "")
                lot = self._btc(chemin)
                if not lot:
                    return
                yield lot
                confirmees += sum(1 for t in lot if t.get("status", {}).get("confirmed"))
                dernier = lot[-1]["txid"]
                if confirmees >= info["chain_stats"]["tx_count"]:
                    return
            yield None  # signal : plafond de pages atteint avant la fin

        return info, declare, lots()

    def _releve_btc(self, rel: Releve, gel: datetime | None, filtre_url: Callable | None, pages: int) -> None:
        a = rel.adresse
        try:
            info, declare, lots = self._btc_transactions(a, pages)
        except ChainesIntrouvableError:
            return
        rel.nb_declare = declare
        rel.etiquettes_tiers = {}
        rel.extra["etiquettes_indisponibles"] = "Esplora n'expose aucune étiquette publique pour Bitcoin."
        for lot in lots:
            if lot is None:
                rel.partiel = True
                break
            for tx in lot:
                statut = tx.get("status", {})
                date_tx = _instant(statut.get("block_time")) if statut.get("confirmed") else None
                url = url_transaction("btc", tx["txid"])
                if not self._accepter(rel, url, date_tx, gel, filtre_url):
                    continue
                self._ajouter_tx_btc(rel, tx, date_tx, url)
        stats = info["chain_stats"]
        if gel is None and rel.rejets_filtre == 0:
            # Sans gel ni filtre, le registre de l'explorateur donne des totaux exacts même si
            # l'historique est plus long que la fenêtre lue (confirmé seulement, hors mempool).
            rel.totaux = {("BTC", None): {
                "recu": Decimal(stats["funded_txo_sum"]) / 10**8,
                "envoye": Decimal(stats["spent_txo_sum"]) / 10**8,
                "nb": stats["tx_count"],
            }}
            rel.totaux_exacts = True

    def _ajouter_tx_btc(self, rel: Releve, tx: dict[str, Any], date_tx: datetime | None, url: str) -> None:
        a = rel.adresse
        entrees: dict[str, int] = defaultdict(int)
        coinbase = False
        for vin in tx.get("vin", []):
            if vin.get("is_coinbase"):
                coinbase = True
                continue
            prev = vin.get("prevout") or {}
            if prev.get("scriptpubkey_address"):
                entrees[prev["scriptpubkey_address"]] += prev.get("value", 0)
        sorties: list[tuple[str, int]] = [
            (v["scriptpubkey_address"], v.get("value", 0)) for v in tx.get("vout", []) if v.get("scriptpubkey_address")
        ]
        total_entrees = sum(entrees.values())
        a_in = entrees.get(a, 0)
        a_out = sum(v for ad, v in sorties if ad == a)
        rel.tx_ids.add(tx["txid"])
        rel.horodater(date_tx)
        rel.compter("BTC", None, Decimal(a_out) / 10**8, Decimal(a_in) / 10**8)
        if a_in > 0:
            part = Decimal(a_in) / Decimal(total_entrees) if total_entrees else Decimal(1)
            for ad, valeur in sorties:
                if ad != a:
                    rel.mouvements.append(Mouvement(
                        tx["txid"], date_tx, a, ad, Decimal(valeur) * part / 10**8, "BTC", None, url,
                        approx=part < 1))
        elif a_out > 0:
            if coinbase and not entrees:
                rel.mouvements.append(Mouvement(tx["txid"], date_tx, "coinbase", a, Decimal(a_out) / 10**8, "BTC", None, url))
            for ad, valeur in entrees.items():
                part = Decimal(valeur) / Decimal(total_entrees) if total_entrees else Decimal(0)
                rel.mouvements.append(Mouvement(
                    tx["txid"], date_tx, ad, a, Decimal(a_out) * part / 10**8, "BTC", None, url,
                    approx=len(entrees) > 1))

    # --- EVM (Blockscout)

    @staticmethod
    def _etiquettes_blockscout(obj: dict[str, Any] | None, hote: str) -> list[dict[str, str]]:
        """Étiquettes publiques d'un objet adresse Blockscout, chacune avec sa source."""
        if not obj:
            return []
        source = f"Blockscout ({hote})"
        sortie: list[dict[str, str]] = []
        if obj.get("name"):
            sortie.append({"nom": obj["name"], "categorie": "contrat" if obj.get("is_contract") else "nom",
                           "source": source})
        if obj.get("ens_domain_name"):
            sortie.append({"nom": obj["ens_domain_name"], "categorie": "ens", "source": source + ", ENS (nom "
                           "choisi par le propriétaire, n'établit pas l'identité)"})
        for tag in obj.get("public_tags") or []:
            nom = tag.get("display_name") or tag.get("label")
            if nom:
                sortie.append({"nom": nom, "categorie": tag.get("tag_type") or "public", "source": source})
        for tag in (obj.get("metadata") or {}).get("tags") or []:
            if tag.get("tagType") == "note" or not tag.get("name"):
                continue
            meta = tag.get("meta") or {}
            attribution = meta.get("tooltipAttribution")
            sortie.append({
                "nom": tag["name"], "categorie": tag.get("tagType") or "tag", "slug": tag.get("slug") or "",
                "source": source + (f", tag fourni par {attribution}" if attribution else ""),
            })
        if obj.get("is_scam"):
            sortie.append({"nom": "signalée comme arnaque", "categorie": "scam", "source": source})
        return sortie

    def _releve_evm(self, rel: Releve, gel: datetime | None, filtre_url: Callable | None, pages: int) -> None:
        base, natif = EVM[rel.chaine]
        hote = httpx.URL(base).host
        a = rel.adresse
        cle_a = _cle(rel.chaine, a)
        try:
            info = self._get(f"{base}/api/v2/addresses/{a}")
        except ChainesIntrouvableError:
            return  # adresse jamais vue sur cette chaîne
        rel.etiquettes = self._etiquettes_blockscout(info, hote)
        rel.est_contrat = bool(info.get("is_contract"))
        rel.extra["solde_natif"] = _txt(_decimal(info.get("coin_balance")) / 10**18)
        rel.avertissements.append("Transferts internes (contrat vers adresse) non lus : seuls les transferts natifs et ERC-20 le sont.")
        try:
            compteurs = self._get(f"{base}/api/v2/addresses/{a}/counters")
            rel.nb_declare = int(compteurs.get("transactions_count") or 0)
            rel.extra["transferts_jetons_declares"] = int(compteurs.get("token_transfers_count") or 0)
        except (ChainesError, ValueError):
            rel.avertissements.append("Compteurs de l'explorateur indisponibles.")
        for chemin, params in ((f"{base}/api/v2/addresses/{a}/transactions", {}),
                               (f"{base}/api/v2/addresses/{a}/token-transfers", {"type": "ERC-20"})):
            suite: dict[str, Any] | None = dict(params)
            for _ in range(pages):
                if suite is None:
                    break
                page = self._get(chemin, suite)
                for it in page.get("items", []):
                    self._ajouter_item_evm(rel, it, natif, hote, cle_a, gel, filtre_url, base)
                suite = page.get("next_page_params")
                if suite is not None:
                    suite = {**params, **suite}
            if suite is not None:
                rel.partiel = True

    def _ajouter_item_evm(
        self, rel: Releve, it: dict[str, Any], natif: str, hote: str, cle_a: str,
        gel: datetime | None, filtre_url: Callable | None, base: str,
    ) -> None:
        identifiant = it.get("transaction_hash") or it.get("hash")
        if not identifiant:
            return
        date_tx = _instant_iso(it.get("timestamp"))
        url = url_transaction(rel.chaine, identifiant)
        if not self._accepter(rel, url, date_tx, gel, filtre_url):
            return
        de_obj, vers_obj = it.get("from") or {}, it.get("to") or {}
        de, vers = de_obj.get("hash", ""), (vers_obj.get("hash") or "")
        for obj in (de_obj, vers_obj):
            if obj.get("hash") and _cle(rel.chaine, obj["hash"]) != cle_a:
                etiquettes = self._etiquettes_blockscout(obj, hote)
                if etiquettes:
                    rel.etiquettes_tiers[_cle(rel.chaine, obj["hash"])] = etiquettes
        if "token" in it:  # transfert de jeton ERC-20
            jeton = it["token"]
            total = it.get("total") or {}
            decimales = int(total.get("decimals") or jeton.get("decimals") or 0)
            montant = _decimal(total.get("value")) / Decimal(10) ** decimales
            actif = jeton.get("symbol") or "jeton inconnu"
            contrat = jeton.get("address_hash") or jeton.get("address")
        else:  # transaction native
            if it.get("status") == "error" or it.get("result") not in (None, "success"):
                rel.tx_ids.add(identifiant)
                rel.horodater(date_tx)
                return  # échouée : rien n'a bougé
            montant = _decimal(it.get("value")) / 10**18
            actif, contrat = natif, None
        rel.tx_ids.add(identifiant)
        rel.horodater(date_tx)
        entrant = _cle(rel.chaine, vers) == cle_a
        sortant = _cle(rel.chaine, de) == cle_a
        rel.compter(actif, contrat, montant if entrant else Decimal(0), montant if sortant else Decimal(0))
        if montant > 0 and vers and de and entrant != sortant:
            rel.mouvements.append(Mouvement(identifiant, date_tx, de, vers, montant, actif, contrat, url))

    # --- TRON

    def _tronscan_etiquettes(self, adresse: str) -> tuple[list[dict[str, str]], bool | None]:
        """Étiquette publique de Tronscan (addressTag, nom de contrat) ; silencieux si indisponible."""
        try:
            donnees = self._get(f"{TRONSCAN_API}/api/account", {"address": adresse})
        except ChainesError as exc:
            journal.info("Tronscan sans étiquette pour %s : %s", adresse, exc)
            return [], None
        sortie: list[dict[str, str]] = []
        source = "Tronscan (apilist.tronscan.org)"
        if donnees.get("addressTag"):
            sortie.append({"nom": donnees["addressTag"], "categorie": "tag", "source": source})
        if donnees.get("name"):
            sortie.append({"nom": donnees["name"], "categorie": "contrat", "source": source})
        return sortie, donnees.get("accountType") == 2 or None

    def _releve_tron(self, rel: Releve, gel: datetime | None, filtre_url: Callable | None, pages: int) -> None:
        a = rel.adresse
        compte = self._get(f"{TRONGRID}/v1/accounts/{a}").get("data") or []
        if not compte:
            rel.extra["compte"] = "non activé : aucune activité sur la chaîne"
            return
        rel.extra["solde_natif"] = _txt(_decimal(compte[0].get("balance")) / 10**6)
        if compte[0].get("create_time"):
            rel.extra["creation"] = _iso(_instant(compte[0]["create_time"] / 1000))
        rel.est_contrat = compte[0].get("type") == "Contract" or None
        rel.etiquettes, est_contrat = self._tronscan_etiquettes(a)
        if est_contrat:
            rel.est_contrat = True
        rel.extra["etiquettes_source"] = "Tronscan ; TronGrid n'en fournit pas"
        base_params: dict[str, Any] = {"limit": 200, "only_confirmed": "true", "order_by": "block_timestamp,desc"}
        if gel is not None:
            base_params["max_timestamp"] = int(gel.timestamp() * 1000)
        for chemin, trc20 in ((f"{TRONGRID}/v1/accounts/{a}/transactions", False),
                              (f"{TRONGRID}/v1/accounts/{a}/transactions/trc20", True)):
            params = dict(base_params)
            for _ in range(pages):
                page = self._get(chemin, params)
                for it in page.get("data", []):
                    self._ajouter_item_tron(rel, it, trc20, gel, filtre_url)
                empreinte = (page.get("meta") or {}).get("fingerprint")
                if not empreinte:
                    params = None  # type: ignore[assignment]
                    break
                params = {**base_params, "fingerprint": empreinte}
            if params is not None:
                rel.partiel = True

    def _ajouter_item_tron(self, rel: Releve, it: dict[str, Any], trc20: bool, gel: datetime | None, filtre_url: Callable | None) -> None:
        identifiant = it.get("transaction_id") if trc20 else it.get("txID")
        if not identifiant:
            return
        date_tx = _instant((it.get("block_timestamp") or 0) / 1000) if it.get("block_timestamp") else None
        url = url_transaction("tron", identifiant)
        if trc20:
            info = it.get("token_info") or {}
            de, vers = it.get("from", ""), it.get("to", "")
            montant = _decimal(it.get("value")) / Decimal(10) ** int(info.get("decimals") or 0)
            actif, contrat = info.get("symbol") or "jeton inconnu", info.get("address")
        else:
            contrats = (it.get("raw_data") or {}).get("contract") or []
            if not contrats or contrats[0].get("type") != "TransferContract":
                return  # appel de contrat : les jetons passent par l'autre route
            if (it.get("ret") or [{}])[0].get("contractRet") not in (None, "SUCCESS"):
                return
            valeur = contrats[0]["parameter"]["value"]
            try:
                de, vers = tron_hex_vers_base58(valeur["owner_address"]), tron_hex_vers_base58(valeur["to_address"])
            except (KeyError, ValueError):
                return
            montant = _decimal(valeur.get("amount")) / 10**6
            actif, contrat = "TRX", None
        if not self._accepter(rel, url, date_tx, gel, filtre_url):
            return
        rel.tx_ids.add(identifiant)
        rel.horodater(date_tx)
        entrant, sortant = vers == rel.adresse, de == rel.adresse
        rel.compter(actif, contrat, montant if entrant else Decimal(0), montant if sortant else Decimal(0))
        if montant > 0 and entrant != sortant:
            rel.mouvements.append(Mouvement(identifiant, date_tx, de, vers, montant, actif, contrat, url))

    # --- Solana

    def _rpc(self, methode: str, params: list[Any]) -> Any:
        corps = {"jsonrpc": "2.0", "id": 1, "method": methode, "params": params}
        return self._requete("POST", SOLANA_RPC, corps=corps).get("result")

    def _releve_sol(self, rel: Releve, gel: datetime | None, filtre_url: Callable | None, pages: int) -> None:
        a = rel.adresse
        compte = (self._rpc("getAccountInfo", [a, {"encoding": "jsonParsed"}]) or {}).get("value")
        if compte:
            rel.est_contrat = bool(compte.get("executable"))
            rel.extra["solde_natif"] = _txt(_decimal(compte.get("lamports")) / 10**9)
        rel.extra["etiquettes_source"] = "Le RPC Solana ne fournit aucune étiquette publique."
        signatures: list[dict[str, Any]] = []
        avant = None
        for _ in range(pages):
            options: dict[str, Any] = {"limit": 1000}
            if avant:
                options["before"] = avant
            lot = self._rpc("getSignaturesForAddress", [a, options]) or []
            signatures += lot
            if len(lot) < 1000:
                break
            avant = lot[-1]["signature"]
        else:
            rel.partiel = True
        retenues: list[tuple[dict[str, Any], datetime | None]] = []
        for s in signatures:
            date_tx = _instant(s.get("blockTime"))
            if self._accepter(rel, url_transaction("sol", s["signature"]), date_tx, gel, filtre_url):
                retenues.append((s, date_tx))
                rel.tx_ids.add(s["signature"])
                rel.horodater(date_tx)
        rel.nb_declare = len(signatures) if not rel.partiel else None
        lues = [(s, d) for s, d in retenues if not s.get("err")][: self.max_detail_sol]
        if len(retenues) > len(lues):
            rel.partiel = True
            rel.avertissements.append(
                f"Seules {len(lues)} transactions lues en détail sur {len(retenues)} (un appel RPC chacune) : "
                "les totaux ne couvrent que celles-ci."
            )
        for s, date_tx in lues:
            tx = self._rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 2}])
            if tx:
                self._ajouter_tx_sol(rel, s["signature"], date_tx, tx)

    def _ajouter_tx_sol(self, rel: Releve, signature: str, date_tx: datetime | None, tx: dict[str, Any]) -> None:
        a = rel.adresse
        url = url_transaction("sol", signature)
        cles = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
        meta = tx.get("meta") or {}
        compte_jeton: dict[str, tuple[str, str, int]] = {}  # compte de jeton -> (propriétaire, mint, décimales)
        for bilan in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
            compte_jeton[cles[bilan["accountIndex"]]] = (
                bilan.get("owner", ""), bilan.get("mint", ""), (bilan.get("uiTokenAmount") or {}).get("decimals", 0))
        instructions = list(tx["transaction"]["message"].get("instructions", []))
        for interne in meta.get("innerInstructions") or []:
            instructions += interne.get("instructions", [])
        for ins in instructions:
            parse = ins.get("parsed")
            if not isinstance(parse, dict):
                continue
            info = parse.get("info") or {}
            if ins.get("program") == "system" and parse.get("type") == "transfer":
                de, vers = info.get("source", ""), info.get("destination", "")
                montant, actif, contrat = _decimal(info.get("lamports")) / 10**9, "SOL", None
            elif ins.get("program") in ("spl-token", "spl-token-2022") and parse.get("type") in ("transfer", "transferChecked"):
                source, destination = info.get("source", ""), info.get("destination", "")
                de = compte_jeton.get(source, (source, "", 0))[0]
                vers = compte_jeton.get(destination, (destination, "", 0))[0]
                mint = info.get("mint") or compte_jeton.get(source, ("", "", 0))[1] or compte_jeton.get(destination, ("", "", 0))[1]
                decimales = (info.get("tokenAmount") or {}).get("decimals", compte_jeton.get(source, ("", "", 0))[2])
                brut = (info.get("tokenAmount") or {}).get("amount", info.get("amount"))
                montant = _decimal(brut) / Decimal(10) ** int(decimales)
                actif, contrat = JETONS_SOL.get(mint, f"SPL:{mint[:6]}"), mint or None
            else:
                continue
            entrant, sortant = vers == a, de == a
            if not (entrant or sortant):
                continue
            rel.compter(actif, contrat, montant if entrant else Decimal(0), montant if sortant else Decimal(0))
            if montant > 0 and entrant != sortant:
                rel.mouvements.append(Mouvement(signature, date_tx, de, vers, montant, actif, contrat, url))


# ------------------------------------------------------------------ mise en forme


_DEFAUT: ClientChaines | None = None


def _client(client: ClientChaines | None) -> ClientChaines:
    global _DEFAUT
    if client is not None:
        return client
    if _DEFAUT is None:
        _DEFAUT = ClientChaines()
    return _DEFAUT


def _resultat(type_: str, valeur: Any, url: str, lu_le: str | None, extrait: str, donnees: dict[str, Any]) -> dict[str, Any]:
    return {"type": type_, "valeur": valeur, "url_source": url, "date_lecture": lu_le, "extrait": extrait, "donnees": donnees}


def _liste_totaux(rel: Releve) -> list[dict[str, Any]]:
    officiel = USDT_OFFICIEL.get(rel.chaine)
    lignes = [
        {"actif": actif, "contrat": contrat, "recu": _txt(v["recu"]), "envoye": _txt(v["envoye"]),
         "nb_mouvements": v["nb"],
         "usdt_officiel": actif == "USDT" and bool(contrat) and officiel is not None
         and _cle(rel.chaine, contrat) == _cle(rel.chaine, officiel),
         "jeton_suspect": bool(contrat) and not re.fullmatch(r"[A-Za-z0-9]{1,12}", actif)}
        for (actif, contrat), v in rel.totaux.items()
    ]
    # L'USDT est toujours montré en jetons, jamais converti : un montant en jetons est un montant lu.
    return sorted(lignes, key=lambda l: (not l["usdt_officiel"], l["contrat"] is not None, l["jeton_suspect"],
                                         l["actif"] != "USDT", -l["nb_mouvements"]))


def _donnees_gel(rel: Releve, gel: datetime | None) -> dict[str, Any]:
    return {
        "date_gel": _iso(gel),
        "transactions_ecartees_apres_gel": rel.rejets_gel,
        "transactions_ecartees_par_filtre_url": rel.rejets_filtre,
    }


def _bloc_plateforme(etiquettes: list[dict[str, str]], est_contrat: bool | None) -> dict[str, Any]:
    plateforme = _est_plateforme(etiquettes, est_contrat)
    return {"plateforme_echange": plateforme, "doit_repondre": plateforme,
            "mention": TEXTE_DOIT_REPONDRE if plateforme else None}


def profil_portefeuille(
    chaine: str, adresse: str, date_gel: date | datetime | str | None = None, *,
    client: ClientChaines | None = None, filtre_url: Callable[[str], bool] | None = None,
) -> dict[str, Any] | None:
    """Profil d'un portefeuille : activité, volumes, étiquettes publiques, sanction OFAC.

    Rend None si l'URL de la page d'adresse est refusée par `filtre_url`.
    """
    c = _client(client)
    gel = lire_date_gel(date_gel)
    rel = c.releve(chaine, adresse, gel, filtre_url)
    filtre = filtre_url or c.filtre_url
    if filtre is not None and not filtre(rel.url):
        c._rejeter(rel.url, "refusée par filtre_url")
        return None
    sanction = c.controle_sanction(rel.adresse)
    for ligne in _liste_totaux(rel):
        if max(Decimal(ligne["recu"]), Decimal(ligne["envoye"])) > Decimal(10) ** 15:
            rel.avertissements.append(
                f"Montant aberrant en {ligne['actif']} (plus de 10^15) : valeur maximale ou jeton piégé, "
                "à vérifier sur l'explorateur avant de le citer.")
        if ligne["jeton_suspect"]:
            rel.avertissements.append(
                f"Jeton au symbole douteux « {ligne['actif']} » (contrat {ligne['contrat']}) : imitation "
                "probable, ne pas le confondre avec le vrai actif.")
    declare = rel.nb_declare is not None and gel is None and rel.rejets_filtre == 0
    nb = rel.nb_declare if declare else len(rel.tx_ids)
    donnees = {
        "chaine": rel.chaine,
        "adresse": rel.adresse,
        "premiere_activite": _iso(rel.premiere),
        "derniere_activite": _iso(rel.derniere),
        "premiere_activite_exacte": not rel.partiel,
        "nb_transactions": nb,
        "nb_transactions_exact": declare or not rel.partiel,
        "totaux": _liste_totaux(rel),
        "totaux_exacts": rel.totaux_exacts or not rel.partiel,
        "etiquettes": rel.etiquettes,
        **_bloc_plateforme(rel.etiquettes, rel.est_contrat),
        "est_contrat": rel.est_contrat,
        "sanction_ofac": sanction,
        "partiel": rel.partiel,
        "avertissements": rel.avertissements,
        "attribution": MENTION_ATTRIBUTION,
        **_donnees_gel(rel, gel),
        **rel.extra,
    }
    morceaux = [f"{rel.chaine} {rel.adresse}", f"{donnees['nb_transactions']} transaction(s)"]
    if rel.premiere:
        morceaux.append(f"du {rel.premiere.date()} au {rel.derniere.date()}")
    if donnees["totaux"]:
        t = donnees["totaux"][0]
        morceaux.append(f"{t['actif']} reçu {t['recu']}, envoyé {t['envoye']}")
    if rel.etiquettes:
        morceaux.append("étiquettes : " + ", ".join(e["nom"] for e in rel.etiquettes))
    if sanction["statut"] == "oui":
        morceaux.append("SOUS SANCTION OFAC")
    if donnees["doit_repondre"]:
        morceaux.append("plateforme d'échange : doit répondre")
    return _resultat("portefeuille", rel.adresse, rel.url, rel.lu_le, " · ".join(morceaux), donnees)


def _principal(rel: Releve) -> tuple[str, str | None] | None:
    """Actif le plus fréquent dans les mouvements : sert à classer par montant sans mélanger les unités."""
    compte = Counter((m.actif, m.contrat) for m in rel.mouvements)
    return compte.most_common(1)[0][0] if compte else None


def contreparties(
    chaine: str, adresse: str, limite: int = 50, date_gel: date | datetime | str | None = None, *,
    tri: str = "nombre", client: ClientChaines | None = None, filtre_url: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Adresses qui envoient ou reçoivent le plus, avec montants par actif et transactions.

    `tri` : « nombre » de mouvements (défaut) ou « montant » dans l'actif principal du portefeuille.
    """
    if tri not in ("nombre", "montant"):
        raise ValueError("tri doit valoir « nombre » ou « montant ».")
    c = _client(client)
    rel = c.releve(chaine, adresse, date_gel, filtre_url)
    gel = lire_date_gel(date_gel)
    groupes: dict[str, list[Mouvement]] = defaultdict(list)
    for m in rel.mouvements:
        autre = m.vers if _cle(rel.chaine, m.de) == _cle(rel.chaine, rel.adresse) else m.de
        groupes[_cle(rel.chaine, autre)].append(m)
    principal = _principal(rel)

    def poids(item: tuple[str, list[Mouvement]]) -> tuple[Decimal | int, int]:
        mouvements = item[1]
        volume = sum((m.montant for m in mouvements if (m.actif, m.contrat) == principal), Decimal(0))
        return (volume, len(mouvements)) if tri == "montant" else (len(mouvements), int(volume))

    classes = sorted(groupes.items(), key=poids, reverse=True)[:limite]
    resultats = []
    for cle, mouvements in classes:
        reel = next(m.vers if _cle(rel.chaine, m.de) == _cle(rel.chaine, rel.adresse) else m.de for m in mouvements)
        etiquettes = rel.etiquettes_tiers.get(cle, [])
        est_contrat = None
        if rel.chaine == "tron" and reel not in ("coinbase",):
            etiquettes, est_contrat = c._tronscan_etiquettes(reel)
        montants: dict[tuple[str, str | None], dict[str, Decimal | int]] = {}
        for m in mouvements:
            ligne = montants.setdefault((m.actif, m.contrat), {"recu_de": Decimal(0), "envoye_a": Decimal(0)})
            ligne["recu_de" if _cle(rel.chaine, m.vers) == _cle(rel.chaine, rel.adresse) else "envoye_a"] += m.montant
        recentes = sorted(mouvements, key=lambda m: m.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        transactions = [{
            "id": m.id, "url": m.url, "date": _iso(m.date), "sens": "recu" if _cle(rel.chaine, m.vers) == _cle(rel.chaine, rel.adresse) else "envoye",
            "montant": _txt(m.montant), "actif": m.actif, "approx": m.approx,
        } for m in recentes]
        sanction = c.controle_sanction(reel) if reel != "coinbase" else {"statut": "non", "entrees": []}
        donnees = {
            "chaine": rel.chaine, "portefeuille": rel.adresse, "adresse": reel,
            "nb_mouvements": len(mouvements),
            "montants": [{"actif": a, "contrat": k, "recu_de": _txt(v["recu_de"]), "envoye_a": _txt(v["envoye_a"])}
                         for (a, k), v in montants.items()],
            "premiere": _iso(min((m.date for m in mouvements if m.date), default=None)),
            "derniere": _iso(max((m.date for m in mouvements if m.date), default=None)),
            "transactions": transactions,
            "etiquettes": etiquettes,
            **_bloc_plateforme(etiquettes, est_contrat),
            "sanction_ofac": sanction,
            "montants_approximatifs": any(m.approx for m in mouvements),
            "attribution": MENTION_ATTRIBUTION,
            **_donnees_gel(rel, gel),
        }
        total = ", ".join(f"{a} {_txt(v['recu_de'])} reçus / {_txt(v['envoye_a'])} envoyés" for (a, _k), v in montants.items())
        extrait = f"{reel} : {len(mouvements)} mouvement(s) avec {rel.adresse} ({total})"
        if donnees["doit_repondre"]:
            extrait += " · plateforme d'échange : doit répondre"
        if sanction["statut"] == "oui":
            extrait += " · SOUS SANCTION OFAC"
        resultats.append(_resultat("contrepartie", reel, transactions[0]["url"] if transactions else url_adresse(rel.chaine, reel),
                                   rel.lu_le, extrait, donnees))
    return resultats


def flux_entre(
    chaine: str, a: str, b: str, date_gel: date | datetime | str | None = None, *,
    intermediaire: bool = True, client: ClientChaines | None = None, filtre_url: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Transactions directes entre `a` et `b`, puis à un intermédiaire près (a→X→b ou b→X→a).

    Un passage par un intermédiaire reste une piste : jamais conclu sur un contrat ou une plateforme
    d'échange que tout le monde utilise.
    """
    c = _client(client)
    gel = lire_date_gel(date_gel)
    rel_a = c.releve(chaine, a, gel, filtre_url)
    rel_b = c.releve(chaine, b, gel, filtre_url)
    ch = rel_a.chaine
    ka, kb = _cle(ch, rel_a.adresse), _cle(ch, rel_b.adresse)
    lu_le = min(x for x in (rel_a.lu_le, rel_b.lu_le) if x)
    vus: set[tuple] = set()
    directs: list[Mouvement] = []
    for m in rel_a.mouvements + rel_b.mouvements:
        extremites = {_cle(ch, m.de), _cle(ch, m.vers)}
        cle = (m.id, _cle(ch, m.de), _cle(ch, m.vers), m.actif, m.montant)
        if extremites == {ka, kb} and cle not in vus:
            vus.add(cle)
            directs.append(m)
    resultats = []
    for m in sorted(directs, key=lambda m: m.date or datetime.min.replace(tzinfo=timezone.utc)):
        sens = "a_vers_b" if _cle(ch, m.de) == ka else "b_vers_a"
        resultats.append(_resultat(
            "flux", m.id, m.url, lu_le,
            f"{_txt(m.montant)} {m.actif} de {m.de} vers {m.vers} le {m.date.date() if m.date else 'date inconnue'}",
            {"chaine": ch, "a": rel_a.adresse, "b": rel_b.adresse, "sens": sens, "montant": _txt(m.montant),
             "actif": m.actif, "contrat": m.contrat, "date": _iso(m.date), "approx": m.approx,
             "lien_explorateur": m.url, "force": "direct", "attribution": MENTION_ATTRIBUTION, **_donnees_gel(rel_a, gel)}))
    if intermediaire:
        resultats += _flux_indirects(ch, rel_a, rel_b, lu_le, gel)
    return resultats


def _flux_indirects(ch: str, rel_a: Releve, rel_b: Releve, lu_le: str, gel: datetime | None) -> list[dict[str, Any]]:
    ka, kb = _cle(ch, rel_a.adresse), _cle(ch, rel_b.adresse)
    sortie = []
    for source, cible, rel_s, rel_c, ks, kc in ((rel_a.adresse, rel_b.adresse, rel_a, rel_b, ka, kb),
                                                (rel_b.adresse, rel_a.adresse, rel_b, rel_a, kb, ka)):
        envoyes: dict[str, Mouvement] = {}
        for m in rel_s.mouvements:
            if _cle(ch, m.de) == ks and _cle(ch, m.vers) != kc:
                actuel = envoyes.get(_cle(ch, m.vers))
                if actuel is None or (m.date and actuel.date and m.date < actuel.date):
                    envoyes[_cle(ch, m.vers)] = m  # la plus ancienne : la seule qui puisse précéder la seconde jambe
        for m in rel_c.mouvements:
            milieu = _cle(ch, m.de)
            if _cle(ch, m.vers) != kc or milieu not in envoyes or milieu == ks:
                continue
            jambe1 = envoyes[milieu]
            if jambe1.date and m.date and jambe1.date > m.date:
                continue  # l'argent ne peut pas repartir avant d'être arrivé
            etiquettes = rel_s.etiquettes_tiers.get(milieu, []) + rel_c.etiquettes_tiers.get(milieu, [])
            if _est_plateforme(etiquettes, None) or any(e.get("categorie") == "contrat" for e in etiquettes):
                continue  # point de passage banal : ne prouve aucun lien
            sortie.append(_resultat(
                "flux_indirect", m.id, jambe1.url, lu_le,
                f"{source} → {m.de} ({_txt(jambe1.montant)} {jambe1.actif}) puis {m.de} → {cible} ({_txt(m.montant)} {m.actif})",
                {"chaine": ch, "de": source, "vers": cible, "intermediaire": m.de, "force": "faible",
                 "jambe_1": {"id": jambe1.id, "url": jambe1.url, "date": _iso(jambe1.date), "montant": _txt(jambe1.montant), "actif": jambe1.actif},
                 "jambe_2": {"id": m.id, "url": m.url, "date": _iso(m.date), "montant": _txt(m.montant), "actif": m.actif},
                 "urls_sources": [jambe1.url, m.url], "approx": jambe1.approx or m.approx,
                 "avertissement": "Passage par un intermédiaire : une piste, pas une preuve de lien.",
                 "attribution": MENTION_ATTRIBUTION, **_donnees_gel(rel_s, gel)}))
    return sortie[:20]


# ------------------------------------------------------------ entrées communes


def _ressemble_coinjoin(tx: dict[str, Any]) -> bool:
    """CoinJoin probable : au moins trois sorties de même valeur et au moins autant d'entrées."""
    sorties = [v.get("value", 0) for v in tx.get("vout", [])]
    if len(tx.get("vin", [])) < 3 or len(sorties) < 3:
        return False
    return Counter(sorties).most_common(1)[0][1] >= 3 and len(tx["vin"]) >= Counter(sorties).most_common(1)[0][1]


class _UnionTrouve:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def trouver(self, x: str) -> str:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def unir(self, a: str, b: str) -> None:
        self._parent[self.trouver(a)] = self.trouver(b)


def regrouper_entrees_communes(
    adresses_btc: list[str], *, date_gel: date | datetime | str | None = None,
    client: ClientChaines | None = None, filtre_url: Callable[[str], bool] | None = None,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """Heuristique des entrées communes : des adresses dépensées ensemble dans une même transaction
    Bitcoin ont probablement le même propriétaire.

    Limites, à garder dans tout texte publié :
    - CoinJoin (Wasabi, Whirlpool, JoinMarket) : plusieurs propriétaires signent la même transaction.
      Les transactions qui y ressemblent sont écartées (≥ 3 sorties de même valeur, ≥ autant
      d'entrées) mais la détection est imparfaite : JoinMarket à deux participants, Wasabi 2.0 et
      PayJoin passent à travers ;
    - un portefeuille de plateforme d'échange ou de service regroupe des milliers de clients :
      un gros groupe désigne un service, pas une personne ;
    - seul l'historique des adresses fournies est lu, le groupe n'est pas étendu de proche en proche ;
    - c'est une probabilité, jamais une preuve d'identité : voir `MENTION_ATTRIBUTION`.
    """
    c = _client(client)
    gel = lire_date_gel(date_gel)
    pages = max_pages or c.max_pages
    uf = _UnionTrouve()
    preuves: list[tuple[str, str, datetime | None, list[str]]] = []
    ignorees: list[dict[str, str]] = []
    demandees = [verifier_adresse("btc", a) for a in adresses_btc]
    lectures_debut = len(c._lectures)
    garde = Releve(chaine="btc", adresse="", url="")
    for adresse in demandees:
        try:
            _info, _declare, lots = c._btc_transactions(adresse, pages)
        except ChainesIntrouvableError:
            continue
        for lot in lots:
            if lot is None:
                break
            for tx in lot:
                statut = tx.get("status", {})
                date_tx = _instant(statut.get("block_time")) if statut.get("confirmed") else None
                url = url_transaction("btc", tx["txid"])
                if not c._accepter(garde, url, date_tx, gel, filtre_url):
                    continue
                entrees = sorted({(v.get("prevout") or {}).get("scriptpubkey_address")
                                  for v in tx.get("vin", []) if (v.get("prevout") or {}).get("scriptpubkey_address")})
                if len(entrees) < 2:
                    continue
                if _ressemble_coinjoin(tx):
                    journal.info("CoinJoin probable écarté : %s", url)
                    ignorees.append({"id": tx["txid"], "url": url, "motif": "ressemble à un CoinJoin"})
                    continue
                for autre in entrees[1:]:
                    uf.unir(entrees[0], autre)
                if not any(p[0] == tx["txid"] for p in preuves):
                    preuves.append((tx["txid"], url, date_tx, entrees))
    lectures = c._lectures[lectures_debut:]
    lu_le = min(lectures) if lectures else c._maintenant().astimezone(timezone.utc).isoformat()
    groupes: dict[str, set[str]] = defaultdict(set)
    for _id, _url, _d, entrees in preuves:
        for ad in entrees:
            groupes[uf.trouver(ad)].add(ad)
    resultats = []
    for racine, membres in groupes.items():
        if not membres & set(demandees):
            continue
        txs = [p for p in preuves if uf.trouver(p[3][0]) == racine]
        txs.sort(key=lambda p: p[2] or datetime.min.replace(tzinfo=timezone.utc))
        adresses = sorted(membres)
        resultats.append(_resultat(
            "entrees_communes", adresses, txs[0][1], lu_le,
            f"{len(adresses)} adresses dépensées ensemble dans {len(txs)} transaction(s) : probablement le même propriétaire",
            {"chaine": "btc", "adresses": adresses, "adresses_demandees": sorted(membres & set(demandees)),
             "taille": len(adresses), "force": "probable",
             "preuves": [{"id": i, "url": u, "date": _iso(d), "entrees": e} for i, u, d, e in txs],
             "coinjoin_ecartes": ignorees,
             "avertissement": "Heuristique, pas une preuve : CoinJoin non détecté, ou service regroupant "
                              "de nombreux clients, donneraient un faux regroupement."
                              + (" Groupe de plus de 50 adresses : probablement un service, pas une personne." if len(adresses) > 50 else ""),
             "attribution": MENTION_ATTRIBUTION,
             "date_gel": _iso(gel)}))
    return resultats
