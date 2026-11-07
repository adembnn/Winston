"""Client de l'API Oriane (recherche de vidéos et de profils TikTok / Instagram / YouTube).

Points de conception :
- chaque réponse réussie est gardée sur disque, la requête identique suivante ne coûte
  donc aucun crédit ;
- la clé d'API ne sort jamais du client : ni affichée, ni journalisée, ni écrite dans le cache ;
- les erreurs HTTP deviennent des exceptions en français, avec un message exploitable.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx

URL_PAR_DEFAUT = "https://connect.oriane.xyz"
VARIABLE_CLE = "ORIANE_API_KEY"

ROUTE_CONTENUS = "/rest/contents/search"
ROUTE_PROFILS = "/rest/profiles/search"
ROUTE_ASSETS = "/rest/assets"

# Tarifs officiels, en crédits par appel.
CREDITS_CONTENUS = {"basic": 10, "default": 15, "full": 20}
CREDITS_PROFILS = {"basic": 5, "default": 10, "full": 15}
CREDITS_ASSET = 5

LIMITE_MAX = 100
ESSAIS_MAX = 3


class OrianeError(Exception):
    """Erreur générique de l'API Oriane. `statut` vaut None hors réponse HTTP."""

    def __init__(self, message: str, statut: int | None = None):
        super().__init__(message)
        self.statut = statut


class OrianeAuthError(OrianeError):
    """Clé absente ou refusée (401)."""


class OrianeCreditsError(OrianeError):
    """Crédits épuisés ou paiement requis (402)."""


class OrianeHorsPlageError(OrianeError):
    """Le décalage demandé dépasse le nombre de résultats (416)."""


def _lire_cle_dans_env(chemin: Path) -> str | None:
    """Lit la ligne `ORIANE_API_KEY=...` d'un fichier .env, sans rien exécuter."""
    try:
        lignes = chemin.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for ligne in lignes:
        ligne = ligne.strip()
        if ligne.startswith("export "):
            ligne = ligne[len("export "):].lstrip()
        nom, separateur, valeur = ligne.partition("=")
        if separateur and nom.strip() == VARIABLE_CLE:
            valeur = valeur.strip().strip("\"'")
            return valeur or None
    return None


def _trouver_cle(cle: str | None, fichier_env: str | Path | None = None) -> str | None:
    """Ordre de priorité : argument, variable d'environnement, fichier .env indiqué,
    puis fichier .env à la racine du dépôt."""
    if cle:
        return cle
    if os.environ.get(VARIABLE_CLE):
        return os.environ[VARIABLE_CLE]
    if fichier_env:
        return _lire_cle_dans_env(Path(fichier_env))
    racine = Path(__file__).resolve().parents[2]
    return _lire_cle_dans_env(racine / ".env")


class OrianeClient:
    """Client synchrone avec cache disque, réessais et compteur de crédits estimés."""

    def __init__(
        self,
        cle: str | None = None,
        base_url: str = URL_PAR_DEFAUT,
        dossier_cache: str | Path = "cache/oriane",
        transport: httpx.BaseTransport | None = None,
        attente_base: float = 1.0,
        dormir: Callable[[float], None] = time.sleep,
        fichier_env: str | Path | None = None,
    ):
        self._cle = _trouver_cle(cle, fichier_env)
        self._base_url = base_url.rstrip("/")
        self._dossier_cache = Path(dossier_cache)
        self._http = httpx.Client(base_url=self._base_url, transport=transport, timeout=60.0)
        self._attente_base = attente_base
        self._dormir = dormir
        self.credits_estimes = 0

    def __repr__(self) -> str:
        # Pas de clé ici : un repr finit facilement dans un journal ou une trace.
        return f"OrianeClient(base_url={self._base_url!r}, credits_estimes={self.credits_estimes})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OrianeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ cache

    @staticmethod
    def _empreinte(route: str, params: dict[str, Any], corps: Any) -> str:
        brut = json.dumps(
            {"route": route, "params": params, "corps": corps},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(brut.encode("utf-8")).hexdigest()

    def _lire_cache(self, empreinte: str) -> dict[str, Any] | None:
        try:
            contenu = json.loads((self._dossier_cache / f"{empreinte}.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None  # absent ou abîmé : on refait l'appel
        return contenu.get("reponse") if isinstance(contenu, dict) else None

    def _ecrire_cache(
        self, empreinte: str, route: str, params: dict[str, Any], corps: Any, reponse: dict[str, Any]
    ) -> None:
        self._dossier_cache.mkdir(parents=True, exist_ok=True)
        cible = self._dossier_cache / f"{empreinte}.json"
        temporaire = cible.with_suffix(".tmp")
        # On garde la requête lisible pour relire le cache à la main ; jamais d'en-têtes.
        temporaire.write_text(
            json.dumps({"route": route, "params": params, "corps": corps, "reponse": reponse},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        temporaire.replace(cible)  # écriture atomique : pas de fichier à moitié écrit

    # --------------------------------------------------------------- transport

    def _appeler(self, route: str, params: dict[str, Any], corps: Any, credits: int, forcer: bool) -> dict[str, Any]:
        """Cache d'abord, puis réseau. Les crédits ne sont comptés que pour un vrai appel."""
        empreinte = self._empreinte(route, params, corps)
        if not forcer:
            en_cache = self._lire_cache(empreinte)
            if en_cache is not None:
                return en_cache
        reponse = self._envoyer(route, params, corps)
        self.credits_estimes += credits
        self._ecrire_cache(empreinte, route, params, corps, reponse)
        return reponse

    def _envoyer(self, route: str, params: dict[str, Any], corps: Any) -> dict[str, Any]:
        if not self._cle:
            raise OrianeAuthError(
                f"Aucune clé Oriane : renseigner {VARIABLE_CLE} (variable d'environnement ou fichier .env)."
            )
        en_tetes = {"Authorization": f"Bearer {self._cle}"}
        for essai in range(1, ESSAIS_MAX + 1):
            try:
                rep = self._http.post(route, params=params, json=corps, headers=en_tetes)
            except httpx.HTTPError as exc:
                # On ne reprend que le type de l'erreur : jamais d'en-têtes dans le message.
                raise OrianeError(f"Oriane injoignable : {type(exc).__name__}") from exc
            if rep.status_code in (429, 500, 502, 503, 504) and essai < ESSAIS_MAX:
                self._dormir(self._attente_base * 2 ** (essai - 1))
                continue
            return self._traiter(rep)
        raise AssertionError("inatteignable")  # pragma: no cover

    @staticmethod
    def _detail(rep: httpx.Response) -> str:
        """Message d'erreur renvoyé par Oriane, s'il y en a un."""
        try:
            erreur = rep.json().get("error") or {}
            return str(erreur.get("message") or "")
        except (ValueError, AttributeError):
            return ""

    def _traiter(self, rep: httpx.Response) -> dict[str, Any]:
        statut = rep.status_code
        if statut in (200, 201, 206):  # 206 : page partielle mais exploitable
            try:
                return rep.json()
            except ValueError as exc:
                raise OrianeError("Réponse d'Oriane illisible (JSON attendu).", statut) from exc
        detail = self._detail(rep)
        suite = f" Détail : {detail}" if detail else ""
        if statut == 401:
            raise OrianeAuthError("Clé refusée par Oriane, vérifier qu'elle est active.", statut)
        if statut == 402:
            raise OrianeCreditsError("Crédits Oriane épuisés ou paiement requis." + suite, statut)
        if statut == 416:
            raise OrianeHorsPlageError("Décalage hors plage : il n'y a pas autant de résultats." + suite, statut)
        if statut == 429:
            raise OrianeError("Trop de requêtes : Oriane limite le débit, réessais épuisés." + suite, statut)
        if statut >= 500:
            raise OrianeError(f"Erreur serveur Oriane ({statut}), réessais épuisés." + suite, statut)
        raise OrianeError(f"Requête refusée par Oriane (HTTP {statut})." + suite, statut)

    # ------------------------------------------------------------- recherches

    @staticmethod
    def _corps_recherche(
        filtres: dict[str, Any] | None, requetes: list[dict[str, Any]] | None, operateur: str
    ) -> dict[str, Any]:
        corps: dict[str, Any] = {"operator": operateur}
        if filtres:
            corps["filters"] = filtres
        if requetes:
            corps["queries"] = requetes
        return corps

    @staticmethod
    def _verifier(limite: int, decalage: int, projection: str, tarifs: dict[str, int]) -> None:
        if not 1 <= limite <= LIMITE_MAX:
            raise ValueError(f"limite doit être entre 1 et {LIMITE_MAX} (reçu {limite}).")
        if decalage < 0:
            raise ValueError("decalage doit être positif ou nul.")
        if projection not in tarifs:
            raise ValueError(f"projection inconnue : {projection!r} (basic, default ou full).")

    def chercher_contenus(
        self,
        filtres: dict[str, Any] | None,
        requetes: list[dict[str, Any]] | None = None,
        operateur: str = "and",
        tri: str | None = None,
        limite: int = 100,
        decalage: int = 0,
        projection: str = "basic",
        ancre: str | None = None,
        forcer: bool = False,
    ) -> dict[str, Any]:
        """Une page de résultats de `POST /rest/contents/search`.

        `tri` suit la syntaxe d'Oriane (« viewsCount:desc,publishedAt »). `ancre` est la valeur
        `aiSearchAnchor` de la première page, à renvoyer pour paginer une recherche visuelle.
        """
        self._verifier(limite, decalage, projection, CREDITS_CONTENUS)
        params: dict[str, Any] = {"offset": decalage, "limit": limite, "projection": projection}
        if tri:
            params["sort"] = tri
        if ancre:
            params["aiSearchAnchor"] = ancre
        corps = self._corps_recherche(filtres, requetes, operateur)
        return self._appeler(ROUTE_CONTENUS, params, corps, CREDITS_CONTENUS[projection], forcer)

    def chercher_profils(
        self,
        filtres: dict[str, Any] | None,
        requetes: list[dict[str, Any]] | None = None,
        operateur: str = "and",
        tri: str | None = None,
        limite: int = 100,
        decalage: int = 0,
        projection: str = "basic",
        forcer: bool = False,
    ) -> dict[str, Any]:
        """Une page de résultats de `POST /rest/profiles/search`."""
        self._verifier(limite, decalage, projection, CREDITS_PROFILS)
        params: dict[str, Any] = {"offset": decalage, "limit": limite, "projection": projection}
        if tri:
            params["sort"] = tri
        corps = self._corps_recherche(filtres, requetes, operateur)
        return self._appeler(ROUTE_PROFILS, params, corps, CREDITS_PROFILS[projection], forcer)

    def creer_asset(
        self,
        texte: str | None = None,
        image: str | dict[str, Any] | None = None,
        forcer: bool = False,
    ) -> dict[str, Any]:
        """Crée un asset de recherche visuelle (réponse : `{"data": {"id": ...}}`).

        `image` est une URL HTTPS, ou le dictionnaire déjà au format Oriane
        (`{"type": "base64", "mediaType": ..., "base64": ...}`).
        """
        if (texte is None) == (image is None):
            raise ValueError("creer_asset attend soit `texte`, soit `image`, pas les deux.")
        if texte is not None:
            corps: dict[str, Any] = {"type": "text", "text": texte}
        else:
            source = {"type": "url", "url": image} if isinstance(image, str) else image
            corps = {"type": "image", "image": source}
        return self._appeler(ROUTE_ASSETS, {}, corps, CREDITS_ASSET, forcer)

    def parcourir_contenus(
        self,
        filtres: dict[str, Any] | None,
        requetes: list[dict[str, Any]] | None = None,
        operateur: str = "and",
        tri: str | None = None,
        limite: int = 100,
        projection: str = "basic",
        ancre: str | None = None,
        max_pages: int = 10,
        forcer: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Parcourt les pages une à une et renvoie chaque réponse brute.

        S'arrête dès que `totalCount` est atteint, qu'une page revient vide, qu'Oriane répond
        416 ou que `max_pages` est atteint (garde-fou : chaque page coûte des crédits).
        """
        decalage = 0
        for _ in range(max_pages):
            try:
                page = self.chercher_contenus(
                    filtres, requetes, operateur, tri, limite, decalage, projection, ancre, forcer
                )
            except OrianeHorsPlageError:
                return
            yield page
            pagination = (page.get("metadata") or {}).get("pagination") or {}
            resultats = (page.get("data") or {}).get("results") or []
            decalage += len(resultats)
            # L'ancre vient avec la première page d'une recherche visuelle : on la rejoue ensuite.
            ancre = pagination.get("aiSearchAnchor") or ancre
            total = pagination.get("totalCount")
            if not resultats or (total is not None and decalage >= total):
                return
