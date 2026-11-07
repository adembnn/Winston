"""Première chaîne d'enquête, de la question au réseau prouvé.

Une question et quelques mots-clés entrent, un réseau d'enquête et un rapport de passage sortent :

1. recherches Oriane complémentaires (parole, texte à l'écran, légende) en projection `basic` ;
2. relecture en projection `full` des seules vidéos retenues (transcription horodatée, son) ;
3. regroupement des vidéos qui disent la même chose (même phrase normalisée ou même son) ;
4. date de chaque vidéo lue dans son identifiant, lien d'origine reconstruit ;
5. archivage de deux vidéos au plus (copie basse définition, empreinte SHA-256, Wayback) ;
6. réseau `reseau.py` : chaque lien porte sa preuve (URL, extrait exact, seconde) ;
7. questions ouvertes de base et export JSON pour l'interface.

Les crédits Oriane sont comptés à chaque appel payant ; au plafond, la chaîne s'arrête
proprement avec ce qu'elle a déjà. La clé d'API ne passe jamais par ce module.

Usage : python -m enquete.chaine "question" --mots "mot un, mot deux" --plafond-credits 600
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from enquete import pivots as piv
from enquete.archive import PieceArchivee, archiver_piece
from enquete.liens import url_origine
from enquete.reseau import (
    ExplicationEcartee,
    FamilleLien,
    Lien,
    Noeud,
    Piste,
    Preuve,
    ReseauEnquete,
    Role,
    Solidite,
    StatutLien,
    TypeNoeud,
)
from enquete.sources.oriane import (
    CREDITS_CONTENUS,
    CREDITS_PROFILS,
    OrianeAuthError,
    OrianeClient,
    OrianeCreditsError,
    OrianeError,
)
from enquete.sources import chaines as sources_chaines
from enquete.sources.infra import Indice, Infra
from enquete.temps import date_depuis_identifiant

# Champs interrogés pour chaque mot-clé : ce qui est dit, ce qui est écrit à l'écran, la légende.
CHAMPS_RECHERCHE = ("transcript", "onScreenText", "caption")
LIBELLES_CHAMPS = {"transcript": "parole", "onScreenText": "texte à l'écran", "caption": "légende"}

# Une phrase plus courte (« J'adore ! ») est trop banale pour prouver une reprise.
MIN_MOTS_PHRASE = 5
# Un même son partagé par plus de vidéos que cela est une musique de tendance, pas une reprise.
MAX_VIDEOS_PAR_SON = 12

AGENT = "chaine.py"
SOURCE = "Oriane"
SOLIDITE_VIDEO = Solidite("F", 3)  # compte non vérifié : fiabilité impossible à juger, information non recoupée
SOLIDITE_GRAPPE = Solidite("C", 3)  # regroupement calculé : à relire par un humain


class PlafondAtteint(Exception):
    """Le prochain appel dépasserait le plafond de crédits fixé pour ce passage."""


# --------------------------------------------------------------------------- modèles


@dataclass
class Element:
    """Ce qui rapproche une vidéo d'une grappe : une phrase dite ou un son partagé."""

    nature: str  # « phrase » ou « son »
    texte: str  # extrait exact (phrase) ou titre du son
    debut_s: float | None
    fin_s: float | None
    cle: str  # phrase normalisée ou identifiant du son


@dataclass
class Video:
    """Une vidéo retenue, avec ce qu'il faut pour la dater, la relier et la citer."""

    id_oriane: str
    plateforme: str
    id_plateforme: str
    pseudo: str
    legende: str
    publiee_le: str  # `publishedAt` renvoyé par Oriane (ISO 8601)
    date: datetime  # date de mise en ligne retenue, UTC
    marge_s: float | None  # marge de la date lue dans l'identifiant ; None si date d'Oriane
    methode_date: str
    url: str | None
    vues: int
    abonnes: int
    hashtags: list[str]
    id_son: str | None
    titre_son: str | None
    phrases: list[tuple[str, str, float | None, float | None]] = field(default_factory=list)
    # phrases : (phrase normalisée, extrait exact, début, fin)


@dataclass
class Grappe:
    """Des vidéos qui disent la même chose, de la plus ancienne à la plus récente."""

    videos: list[Video]
    elements: dict[str, list[Element]]  # par id_oriane de vidéo

    @property
    def premiere(self) -> Video:
        return self.videos[0]

    def libelle(self) -> str:
        phrases = [e for els in self.elements.values() for e in els if e.nature == "phrase"]
        if phrases:
            plus_courante = max(
                {e.cle: e for e in phrases}.values(),
                key=lambda e: sum(1 for x in phrases if x.cle == e.cle),
            )
            return f"« {_abreger(plus_courante.texte)} » · {len(self.videos)} vidéos"
        sons = [e for els in self.elements.values() for e in els if e.nature == "son"]
        return f"Même son : {_abreger(sons[0].texte)} · {len(self.videos)} vidéos"


@dataclass
class Compteur:
    """Ce que le passage a coûté et trouvé, pour le rapport."""

    appels: int = 0
    appels_en_cache: int = 0
    credits: int = 0
    erreurs: list[str] = field(default_factory=list)
    plafond_atteint: bool = False


# --------------------------------------------------------------------------- outils de texte


def _abreger(texte: str, longueur: int = 80) -> str:
    texte = " ".join(texte.split())
    return texte if len(texte) <= longueur else texte[: longueur - 1].rstrip() + "…"


def normaliser_phrase(texte: str) -> str:
    """Phrase sans accents, majuscules ni ponctuation : « Ça  ARRIVE ! » devient « ca arrive »."""
    decompose = unicodedata.normalize("NFKD", texte.lower())
    sans_accent = "".join(c for c in decompose if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", sans_accent))


def _lire_date_iso(valeur: str) -> datetime:
    return datetime.fromisoformat(valeur.replace("Z", "+00:00")).astimezone(UTC)


def _nom_dossier(texte: str) -> str:
    return normaliser_phrase(texte).replace(" ", "-")[:40] or "enquete"


# --------------------------------------------------------------------------- recherche Oriane


class Budget:
    """Enveloppe du client Oriane : compte les appels et refuse de dépasser le plafond."""

    def __init__(self, client: OrianeClient, plafond: int):
        self.client = client
        self.plafond = plafond
        self.compteur = Compteur()

    def _payer(self, tarif: int, appel: Any, parametres: dict[str, Any]) -> dict[str, Any]:
        if self.client.credits_estimes + tarif > self.plafond:
            self.compteur.plafond_atteint = True
            raise PlafondAtteint(f"plafond de {self.plafond} crédits atteint")
        avant = self.client.credits_estimes
        reponse = appel(**parametres)
        depense = self.client.credits_estimes - avant
        if depense:
            self.compteur.appels += 1
            self.compteur.credits += depense
        else:
            self.compteur.appels_en_cache += 1
        return reponse

    def chercher(self, projection: str, **parametres: Any) -> dict[str, Any]:
        return self._payer(
            CREDITS_CONTENUS[projection], self.client.chercher_contenus, {"projection": projection, **parametres}
        )

    def chercher_profils(self, projection: str, **parametres: Any) -> dict[str, Any]:
        """Même enveloppe pour `/rest/profiles/search` (tarif des profils, même plafond)."""
        return self._payer(
            CREDITS_PROFILS[projection], self.client.chercher_profils, {"projection": projection, **parametres}
        )


def _filtres(champ: str, mot: str, depuis: str | None) -> dict[str, Any]:
    filtres: dict[str, Any] = {champ: {"includesFuzzy": {"values": [mot]}}}
    if depuis:
        filtres["publishedAt"] = {"after": depuis}
    return filtres


def _resultats(reponse: dict[str, Any]) -> list[dict[str, Any]]:
    return (reponse.get("data") or {}).get("results") or []


def chercher_candidats(
    budget: Budget, mots: list[str], depuis: str | None, limite: int
) -> dict[str, dict[str, Any]]:
    """Une recherche `basic` par mot-clé et par champ ; une vidéo trouvée plusieurs fois remonte.

    Renvoie `{id_oriane: {"resultat": ..., "touches": {(mot, champ), ...}}}`.
    """
    candidats: dict[str, dict[str, Any]] = {}
    for mot in mots:
        for champ in CHAMPS_RECHERCHE:
            try:
                reponse = budget.chercher(
                    "basic", filtres=_filtres(champ, mot, depuis), tri="viewsCount:desc", limite=limite
                )
            except PlafondAtteint:
                return candidats
            except OrianeAuthError:
                raise
            except OrianeCreditsError as erreur:
                budget.compteur.erreurs.append(f"{mot!r} / {champ} : {erreur}")
                budget.compteur.plafond_atteint = True
                return candidats
            except OrianeError as erreur:
                budget.compteur.erreurs.append(f"{mot!r} / {champ} : {erreur}")
                continue
            for resultat in _resultats(reponse):
                entree = candidats.setdefault(resultat["id"], {"resultat": resultat, "touches": set()})
                entree["touches"].add((mot, champ))
    return candidats


def retenir(candidats: dict[str, dict[str, Any]], maximum: int) -> list[str]:
    """Identifiants à relire en `full` : d'abord les vidéos trouvées par le plus de recherches
    différentes, puis les plus vues."""
    classees = sorted(
        candidats.values(),
        key=lambda c: (-len(c["touches"]), -(c["resultat"].get("viewsCount") or 0)),
    )
    return [c["resultat"]["id"] for c in classees[:maximum]]


def relire_en_full(budget: Budget, identifiants: list[str]) -> dict[str, dict[str, Any]]:
    """Une seule requête `full` pour toutes les vidéos retenues (le tarif est par appel)."""
    if not identifiants:
        return {}
    try:
        reponse = budget.chercher(
            "full", filtres={"id": {"includes": identifiants}}, limite=min(len(identifiants), 100)
        )
    except PlafondAtteint:
        return {}
    except OrianeAuthError:
        raise
    except OrianeError as erreur:
        budget.compteur.erreurs.append(f"relecture full : {erreur}")
        return {}
    return {r["id"]: r for r in _resultats(reponse)}


# --------------------------------------------------------------------------- vidéos


def _type_publication(plateforme: str, type_oriane: str | None) -> str:
    """Traduit le `platformType` d'Oriane vers les types de `liens.url_origine`."""
    type_oriane = (type_oriane or "").lower()
    if plateforme == "tiktok":
        return "photo" if type_oriane == "photo" else "video"
    if plateforme == "instagram":
        return "reel" if type_oriane == "reel" else "post"
    return "short" if type_oriane == "short" else "video"


def _phrases(resultat: dict[str, Any]) -> list[tuple[str, str, float | None, float | None]]:
    """Phrases assez longues pour compter : morceaux de parole horodatés, puis légende."""
    vues: set[str] = set()
    phrases = []
    candidates = [
        (c.get("text") or "", c.get("startSeconds"), c.get("endSeconds"))
        for media in resultat.get("media") or []
        for c in media.get("transcriptChunks") or []
    ]
    candidates.append((resultat.get("caption") or "", None, None))
    for texte, debut, fin in candidates:
        norme = normaliser_phrase(texte)
        if len(norme.split()) >= MIN_MOTS_PHRASE and norme not in vues:
            vues.add(norme)
            phrases.append((norme, texte.strip(), debut, fin))
    return phrases


def video_depuis_resultat(resultat: dict[str, Any]) -> Video | None:
    """Convertit un résultat Oriane en `Video`. Renvoie None s'il manque l'essentiel."""
    plateforme = resultat.get("platform") or ""
    id_plateforme = str(resultat.get("platformId") or "")
    pseudo = resultat.get("profileHandle") or ""
    if not (resultat.get("id") and plateforme and id_plateforme and resultat.get("publishedAt")):
        return None
    type_pub = _type_publication(plateforme, resultat.get("platformType"))
    try:
        decodee = date_depuis_identifiant(plateforme, id_plateforme, type_pub)
        date, marge, methode = decodee.date, decodee.marge_secondes, decodee.methode
    except ValueError:
        # YouTube, ou identifiant sans date : on garde la date publiée par la plateforme.
        date = _lire_date_iso(resultat["publishedAt"])
        marge, methode = None, "date `publishedAt` donnée par Oriane (l'identifiant ne porte pas de date)"
    try:
        url = url_origine(plateforme, id_plateforme, pseudo or None, type_pub)
    except ValueError:
        url = None
    return Video(
        id_oriane=resultat["id"],
        plateforme=plateforme,
        id_plateforme=id_plateforme,
        pseudo=pseudo,
        legende=(resultat.get("caption") or "").strip(),
        publiee_le=resultat["publishedAt"],
        date=date,
        marge_s=marge,
        methode_date=methode,
        url=url,
        vues=resultat.get("viewsCount") or 0,
        abonnes=resultat.get("profileFollowersCount") or 0,
        hashtags=list(resultat.get("hashtags") or []),
        id_son=str(resultat["audioPlatformId"]) if resultat.get("audioPlatformId") else None,
        titre_son=resultat.get("audioTitle"),
        phrases=_phrases(resultat),
    )


# --------------------------------------------------------------------------- grappes


def regrouper(videos: list[Video]) -> list[Grappe]:
    """Réunit les vidéos qui partagent une phrase normalisée ou un son (union de proche en proche).

    Une vidéo seule ne forme pas de grappe. Les vidéos de chaque grappe sont rangées par date.
    """
    parent = list(range(len(videos)))

    def racine(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    par_phrase: dict[str, list[int]] = defaultdict(list)
    par_son: dict[str, list[int]] = defaultdict(list)
    for i, video in enumerate(videos):
        for norme, *_ in video.phrases:
            par_phrase[norme].append(i)
        if video.id_son:
            par_son[video.id_son].append(i)

    partages: list[tuple[str, str, list[int]]] = []  # (nature, clé, indices des vidéos)
    for norme, indices in par_phrase.items():
        if len(indices) >= 2:
            partages.append(("phrase", norme, indices))
    for id_son, indices in par_son.items():
        if 2 <= len(indices) <= MAX_VIDEOS_PAR_SON:
            partages.append(("son", id_son, indices))
    for _, _, indices in partages:
        for autre in indices[1:]:
            parent[racine(autre)] = racine(indices[0])

    elements: dict[int, list[Element]] = defaultdict(list)
    for nature, cle, indices in partages:
        for i in indices:
            if nature == "phrase":
                _, texte, debut, fin = next(p for p in videos[i].phrases if p[0] == cle)
                elements[i].append(Element("phrase", texte, debut, fin, cle))
            else:
                elements[i].append(Element("son", videos[i].titre_son or cle, 0.0, None, cle))

    membres: dict[int, list[int]] = defaultdict(list)
    for i in elements:
        membres[racine(i)].append(i)
    grappes = []
    for indices in membres.values():
        indices.sort(key=lambda i: videos[i].date)
        grappes.append(Grappe(
            videos=[videos[i] for i in indices],
            elements={videos[i].id_oriane: elements[i] for i in indices},
        ))
    grappes.sort(key=lambda g: (-len(g.videos), g.premiere.date))
    return grappes


# --------------------------------------------------------------------------- archivage


def choisir_a_archiver(videos: list[Video], grappes: list[Grappe], maximum: int) -> list[Video]:
    """La plus ancienne vidéo de la plus grosse grappe, puis la vidéo la plus vue."""
    choisies: list[Video] = []
    if grappes and grappes[0].premiere.url:
        choisies.append(grappes[0].premiere)
    for video in sorted(videos, key=lambda v: -v.vues):
        if video.url and video not in choisies:
            choisies.append(video)
    return choisies[:maximum]


def archiver_video(url: str, dossier: Path) -> PieceArchivee:
    """Archive une pièce par toutes les voies sans compte (`archive.archiver_piece`) : vidéo yt-dlp,
    capture de page, oEmbed et vignette, Wayback, horodatage RFC 3161. Le nom reste celui de
    l'ancienne fonction : les tests la remplacent par un faux, pour qu'aucun test ne télécharge."""
    return archiver_piece(url, dossier=dossier)


def archiver(videos: list[Video], dossier: Path) -> dict[str, PieceArchivee]:
    """Archive chaque vidéo. Une erreur d'une pièce, quelle qu'elle soit, n'arrête pas le reste."""
    pieces: dict[str, PieceArchivee] = {}
    for video in videos:
        assert video.url  # garanti par `choisir_a_archiver`
        try:
            pieces[video.id_oriane] = archiver_video(video.url, dossier=dossier)
        except Exception as erreur:  # noqa: BLE001 - l'archivage ne doit jamais arrêter l'enquête
            piece = PieceArchivee(url=video.url, type="video", date_collecte=datetime.now(UTC).isoformat())
            piece.statuts["telechargement"] = "echec"
            piece.noter(f"archivage impossible : {type(erreur).__name__} : {erreur}")
            pieces[video.id_oriane] = piece
    return pieces


def empreinte_de_la_piece(piece: PieceArchivee) -> str | None:
    """L'empreinte que portent les preuves : celle de la vidéo si elle est téléchargée, sinon celle de la
    liste d'empreintes (qui recense la capture de page, l'oEmbed et la vignette, et qui est horodatée)."""
    if piece.empreinte_sha256:
        return piece.empreinte_sha256
    for fichier in piece.fichiers:
        if fichier.get("role") == "liste_des_empreintes" and fichier.get("sha256"):
            return fichier["sha256"]
    return None


def resume_de_l_archive(piece: PieceArchivee) -> dict[str, Any]:
    """Ce que le nœud vidéo garde de l'archivage : statut de chaque voie, Wayback, jetons, gestes manuels."""
    return {
        "statuts": dict(piece.statuts),
        "url_wayback": piece.url_wayback,
        "jetons_horodatage": [
            {"autorite": j.get("autorite"), "heure_attestee": j.get("heure_attestee")}
            for j in piece.horodatages if j.get("statut") == "ok"
        ],
        "mur_de_connexion": piece.mur_connexion,
        "video_non_telechargee": piece.video_non_telechargee,
        "empreinte_sha256": empreinte_de_la_piece(piece),
        "geste_manuel": piece.geste_manuel,
    }


def poser_archives(reseau: ReseauEnquete, archives: dict[str, PieceArchivee]) -> None:
    """Ajoute au réseau déjà écrit ce que l'archivage a livré : une version du nœud vidéo, et une version
    de chaque lien dont une preuve cite la vidéo, avec l'empreinte. L'ancienne version reste lisible."""
    if not archives:
        return
    noeuds_video = {n.attributs.get("url"): n for n in reseau.noeuds() if n.type is TypeNoeud.VIDEO}
    for piece in archives.values():
        noeud = noeuds_video.get(piece.url)
        if noeud is not None:
            reseau.corriger_noeud(
                noeud.id, "archive : voies, Wayback et horodatage",
                attributs={**noeud.attributs, "archive": resume_de_l_archive(piece)},
            )
        empreinte = empreinte_de_la_piece(piece)
        if not empreinte:
            continue
        for lien in reseau.liens():
            if any(p.url == piece.url and p.empreinte_sha256 != empreinte for p in lien.preuves):
                reseau.corriger_lien(
                    lien.id, "archive : empreinte SHA-256",
                    preuves=tuple(
                        replace(p, empreinte_sha256=empreinte) if p.url == piece.url else p for p in lien.preuves
                    ),
                )


# --------------------------------------------------------------------------- réseau


def _extrait_video(video: Video) -> str:
    """Un extrait exact pour citer la vidéo : légende, sinon première phrase dite."""
    if video.legende:
        return video.legende
    if video.phrases:
        return video.phrases[0][1]
    return f"(vidéo sans légende) {video.url}"


def _preuve(video: Video, extrait: str, debut: float | None, fin: float | None,
            archives: dict[str, PieceArchivee], collecte: datetime) -> Preuve:
    piece = archives.get(video.id_oriane)
    return Preuve(
        url=video.url or "",
        extrait=piv.masquer(extrait) or extrait,
        source_donnee=SOURCE,
        date_collecte=collecte,
        empreinte_sha256=empreinte_de_la_piece(piece) if piece else None,
        debut_s=debut,
        fin_s=fin,
    )


def construire_reseau(
    question: str, videos: list[Video], grappes: list[Grappe], archives: dict[str, PieceArchivee],
    collecte: datetime, chemin: str = ":memory:", *, reseau: ReseauEnquete | None = None,
) -> ReseauEnquete:
    """Pose nœuds, liens prouvés et questions ouvertes. Une vidéo sans URL n'entre pas : pas d'URL, pas de preuve.

    `reseau` : un réseau déjà ouvert (le journal d'un passage en direct) où écrire ; sinon un réseau neuf
    est ouvert sur `chemin`. Dans les deux cas c'est à l'appelant de le fermer.
    """
    reseau = reseau if reseau is not None else ReseauEnquete(question, chemin)
    citables = [v for v in videos if v.url]

    # Seules les vidéos qui ont une adresse peuvent être citées ; la grappe se lit sur celles-là.
    citees = [[v for v in g.videos if v.url] for g in grappes]
    citees = [c for c in citees if len(c) >= 2]
    origines = {c[0].id_oriane for c in citees}
    reprises = {v.id_oriane for c in citees for v in c[1:]}
    comptes_origine = {(c[0].plateforme, c[0].pseudo) for c in citees}
    comptes_reprise = {(v.plateforme, v.pseudo) for c in citees for v in c[1:]}

    noeuds_compte: dict[tuple[str, str], Noeud] = {}
    noeuds_video: dict[str, Noeud] = {}
    for video in citables:
        cle = (video.plateforme, video.pseudo)
        if video.pseudo and cle not in noeuds_compte:
            role = (
                Role.ORIGINE if cle in comptes_origine
                else Role.AMPLIFICATEUR if cle in comptes_reprise
                else Role.INCONNU
            )
            noeuds_compte[cle] = reseau.ajouter_noeud(Noeud(
                TypeNoeud.COMPTE, f"@{video.pseudo} ({video.plateforme})", role,
                {"plateforme": video.plateforme, "pseudo": video.pseudo, "abonnes": video.abonnes}, SOLIDITE_VIDEO,
            ))
        role_video = (
            Role.ORIGINE if video.id_oriane in origines
            else Role.AMPLIFICATEUR if video.id_oriane in reprises
            else Role.INCONNU
        )
        noeuds_video[video.id_oriane] = reseau.ajouter_noeud(Noeud(
            TypeNoeud.VIDEO,
            f"Vidéo {video.plateforme} de @{video.pseudo or 'inconnu'}, {video.date:%d/%m/%Y}",
            role_video,
            {
                "url": video.url, "id_plateforme": video.id_plateforme,
                "date": video.date.isoformat(), "marge_secondes": video.marge_s,
                "methode_date": video.methode_date, "publiee_le_selon_oriane": video.publiee_le,
                "vues": video.vues, "hashtags": video.hashtags,
                # « Première du lot » n'est pas « première tout court » : voir la question d'origine.
                "premiere_de_sa_grappe": video.id_oriane in origines,
            },
            SOLIDITE_VIDEO,
        ))
        if video.pseudo:
            reseau.ajouter_lien(Lien(
                type_lien="publie", famille=FamilleLien.IDENTITE,
                source_id=noeuds_compte[cle].id, cible_id=noeuds_video[video.id_oriane].id,
                preuves=(_preuve(video, _extrait_video(video), None, None, archives, collecte),),
                explication=f"Le compte @{video.pseudo} figure comme auteur de la vidéo dans l'adresse "
                            "reconstruite depuis l'identifiant de la plateforme.",
                methode="adresse de la vidéo reconstruite depuis l'identifiant de la plateforme",
                agent=AGENT, statut=StatutLien.PROUVE, oriente=True,
                force_points=3.0, identifiant_officiel=True,
            ))

    for g, presentes in zip(grappes, [[v for v in g.videos if v.url] for g in grappes]):
        if len(presentes) < 2:
            continue
        origine = presentes[0]
        bases = sorted({e.nature for els in g.elements.values() for e in els})
        grappe = reseau.ajouter_noeud(Noeud(
            TypeNoeud.GRAPPE, g.libelle(), Role.INCONNU,
            {"bases": bases, "nb_videos": len(presentes),
             "plateformes": sorted({v.plateforme for v in presentes}),
             "premiere_date": origine.date.isoformat()},
            SOLIDITE_GRAPPE,
        ))
        for video in presentes:
            elements = g.elements[video.id_oriane]
            preuves = tuple(
                _preuve(video, e.texte, e.debut_s, e.fin_s, archives, collecte) for e in elements
            )
            reseau.ajouter_lien(Lien(
                type_lien="dit_la_meme_chose", famille=FamilleLien.CONTENU_DIT,
                source_id=noeuds_video[video.id_oriane].id, cible_id=grappe.id, preuves=preuves,
                explication="La vidéo contient la même phrase, ou le même son, que les autres vidéos de la grappe.",
                methode="phrase normalisée identique (au moins "
                        f"{MIN_MOTS_PHRASE} mots) ou même identifiant de son",
                agent=AGENT, statut=StatutLien.PROUVE,
                force_points=2.0 if any(e.nature == "phrase" for e in elements) else 1.0,
            ))
        for video in presentes[1:]:
            if video.date <= origine.date:
                continue
            communs = {e.cle for e in g.elements[video.id_oriane]} & {
                e.cle for e in g.elements[origine.id_oriane]
            }
            preuves = [
                _preuve(video, e.texte, e.debut_s, e.fin_s, archives, collecte)
                for e in g.elements[video.id_oriane] if e.cle in communs
            ] + [
                _preuve(origine, e.texte, e.debut_s, e.fin_s, archives, collecte)
                for e in g.elements[origine.id_oriane] if e.cle in communs
            ]
            if not preuves:
                continue  # reliée à la grappe par une autre vidéo, pas directement à la plus ancienne
            reseau.ajouter_lien(Lien(
                type_lien="reprend", famille=FamilleLien.RECIT,
                source_id=noeuds_video[video.id_oriane].id, cible_id=noeuds_video[origine.id_oriane].id,
                preuves=tuple(preuves),
                explication=f"Mise en ligne après la première vidéo repérée ({origine.date:%d/%m/%Y}), "
                            "avec la même phrase ou le même son.",
                methode="même contenu, date lue dans l'identifiant de chaque vidéo",
                agent=AGENT, statut=StatutLien.INDICE, oriente=True, force_points=float(len(communs)),
                explications_ecartees=(ExplicationEcartee(
                    "formule ou son banal repris par hasard",
                    f"phrase d'au moins {MIN_MOTS_PHRASE} mots, identique après normalisation"
                    if any(e.nature == "phrase" for e in g.elements[video.id_oriane]) else
                    f"son partagé par {len(presentes)} vidéos seulement (au plus {MAX_VIDEOS_PAR_SON} admis)",
                ),),
            ))

    _poser_questions_de_base(reseau, grappes, citables, noeuds_video)
    return reseau


def _poser_questions_de_base(
    reseau: ReseauEnquete, grappes: list[Grappe], videos: list[Video], noeuds_video: dict[str, Noeud],
) -> None:
    """Les trois trous que la chaîne ne sait pas combler seule : questions ouvertes ET zones d'ombre."""
    if videos:
        plus_ancienne = min(videos, key=lambda v: v.date)
        origine = (
            f"La plus ancienne vidéo repérée date du {plus_ancienne.date:%d/%m/%Y} "
            f"({plus_ancienne.plateforme}, @{plus_ancienne.pseudo or 'inconnu'}), mais Oriane ne couvre "
            "que TikTok, Instagram et YouTube : qui a publié le contenu en premier, et où ?"
        )
    else:
        origine = "Aucune vidéo repérée : où le contenu est-il apparu en premier, et qui l'a publié ?"
    reseau.poser_question(origine)
    reseau.poser_question("Qui profite de la diffusion de ces vidéos (audience, revenus, intérêt) ?")
    reseau.poser_question("Qui a démenti ou vérifié ces vidéos, et à quelle date ?")

    voisines = tuple(
        noeuds_video[v.id_oriane].id for v in sorted(videos, key=lambda v: v.date)[:5] if v.id_oriane in noeuds_video
    )
    plateformes = ", ".join(sorted({v.plateforme for v in videos})) or "aucune plateforme"
    vues = (
        f"{len(videos)} vidéos relues sur {plateformes}, dont {len(grappes)} grappe(s) de phrases ou sons identiques"
        if videos else "aucune vidéo exploitable relue"
    )
    reseau.poser_zone(
        "Qui a publié le contenu en premier, et où ?",
        "hors de portée",
        [f"Recherche Oriane dans la parole, le texte à l'écran et les légendes : {vues}.",
         "Oriane ne couvre que TikTok, Instagram et YouTube : X, Facebook et Telegram restent hors de portée."],
        [Piste("Chercher le même extrait ailleurs", "X, Facebook, Telegram",
               "recherche d'image inversée sur des captures de la vidéo, puis recherche de la phrase entre guillemets",
               "moyenne"),
         Piste("Écrire au compte le plus ancien", "le plus ancien compte repéré",
               "message privé ou e-mail public du profil : demander où il a trouvé le contenu", "facile")],
        autour=voisines, role_manquant=Role.ORIGINE,
        pourquoi_en_mots="La plus ancienne trace retrouvée est celle d'Oriane ; une trace antérieure sur une "
                         "autre plateforme reste possible.",
    )
    reseau.poser_zone(
        "Qui profite de la diffusion de ces vidéos ?",
        "introuvable",
        ["Lecture des bios et des liens en bio quand le profil a été relu.",
         "Aucun registre ni aucun revenu publicitaire n'est consultable depuis Oriane."],
        [Piste("Interroger le registre de la société", "registre du commerce (Infogreffe, Pappers)",
               "partir du lien en bio, identifier la société derrière le domaine, lire dirigeants et statuts",
               "moyenne"),
         Piste("Demander au compte ses liens commerciaux", "comptes les plus actifs de la diffusion",
               "demander s'il y a un partenariat, un code promo ou un lien d'affiliation", "demande du temps")],
        autour=voisines, role_manquant=Role.BENEFICIAIRE,
        pourquoi_en_mots="Les vidéos ne disent pas qui touche de l'argent ou de l'audience grâce à elles.",
    )
    reseau.poser_zone(
        "Qui a démenti ou vérifié ces vidéos, et à quelle date ?",
        "introuvable",
        ["Aucune recherche de vérification publiée n'est faite par cette chaîne : seules les vidéos sont lues."],
        [Piste("Consulter les vérifications publiées", "cellules de vérification des rédactions",
               "chercher la phrase et le sujet dans les rubriques de fact-checking", "facile"),
         Piste("Appeler le service de presse concerné", "service de presse de la personne ou marque citée",
               "demander s'il y a eu un démenti, et sa date", "moyenne")],
        autour=voisines, role_manquant=Role.CONTRADICTEUR,
        pourquoi_en_mots="Aucun démenti ni aucune vérification n'a été retrouvé dans les pièces lues.",
    )


# --------------------------------------------------------------------------- pivots, infrastructure, argent

PROJECTION_PROFILS = "full"  # bio et lien en bio ; l'e-mail public du profil n'est jamais recopié
LOT_PROFILS = 100
MAX_ENRICHIS = 15  # pivots enrichis au plus par les sources ouvertes (RDAP, crt.sh, entreprises, Wayback)
MAX_PORTEFEUILLES = 15  # portefeuilles lus au plus sur les explorateurs
MAX_PREUVES_PAR_LIEN = 3
AGENT_PIVOTS = "chaine.py (pivots)"
AGENT_INFRA = "chaine.py (infrastructure)"
AGENT_ARGENT = "chaine.py (argent)"
SOLIDITE_PIVOT = Solidite("B", 2)  # lu mot pour mot dans une bio ou une vidéo d'une plateforme
SOLIDITE_REGISTRE = Solidite("A", 2)  # registre officiel (RDAP, Recherche d'entreprises)
SOLIDITE_SOURCE_OUVERTE = Solidite("B", 2)  # Wayback, journaux de certificats
SOLIDITE_EXPLORATEUR = Solidite("B", 2)  # explorateur public d'une chaîne
SOLIDITE_PAR_SOURCE = {
    "rdap": SOLIDITE_REGISTRE, "recherche-entreprises": SOLIDITE_REGISTRE,
    "wayback": SOLIDITE_SOURCE_OUVERTE, "crt.sh": SOLIDITE_SOURCE_OUVERTE,
}
TYPES_ENRICHIS = (piv.DOMAINE, piv.SIREN, piv.SIRET)


def lire_profils(budget: Budget, comptes: Any) -> list[dict[str, Any]]:
    """Profils (bio, lien en bio) des comptes retenus : une requête par plateforme et par lot de 100 pseudos
    triés, donc une clé de cache stable. Un profil n'est gardé que si son pseudo est celui demandé.

    Si Oriane refuse le filtre, l'erreur est notée et l'extraction se fera sur les seules bios des contenus.
    """
    par_plateforme: dict[str, dict[str, str]] = defaultdict(dict)
    for plateforme, pseudo in comptes:
        if plateforme and pseudo:
            par_plateforme[plateforme.lower()].setdefault(pseudo.lower(), pseudo)
    profils: list[dict[str, Any]] = []
    for plateforme in sorted(par_plateforme):
        pseudos = sorted(par_plateforme[plateforme].values())
        for debut in range(0, len(pseudos), LOT_PROFILS):
            lot = pseudos[debut: debut + LOT_PROFILS]
            filtres = {
                "platform": {"includes": [plateforme]},
                "handle": {"exactMatch": {"values": lot, "operator": "or"}},
            }
            try:
                reponse = budget.chercher_profils(PROJECTION_PROFILS, filtres=filtres, limite=LOT_PROFILS)
            except PlafondAtteint:
                return profils
            except OrianeAuthError:
                raise
            except OrianeCreditsError as erreur:
                budget.compteur.erreurs.append(f"profils {plateforme} : {erreur}")
                budget.compteur.plafond_atteint = True
                return profils
            except OrianeError as erreur:
                budget.compteur.erreurs.append(f"profils {plateforme} : {erreur}")
                continue
            voulus = {p.lower() for p in lot}
            for profil in _resultats(reponse):
                if (str(profil.get("platform") or "").lower() == plateforme
                        and str(profil.get("handle") or "").lower() in voulus):
                    profils.append(profil)
    return profils


def _comptes_du_reseau(reseau: ReseauEnquete) -> dict[str, Noeud]:
    """Les nœuds compte du réseau, par clé « plateforme:pseudo » en minuscules (celle de `pivots.py`)."""
    return {
        f"{n.attributs['plateforme']}:{n.attributs['pseudo'].lower()}": n
        for n in reseau.noeuds()
        if n.type is TypeNoeud.COMPTE and n.attributs.get("pseudo") and n.attributs.get("plateforme")
    }


def _nom_du_compte(cle: str) -> str:
    plateforme, _, pseudo = cle.partition(":")
    return f"@{pseudo} ({plateforme})"


def _pivot_entre_au_reseau(p: piv.Pivot) -> bool:
    """Un pivot non banal d'infrastructure ou d'argent. Un domaine ou une URL d'un seul compte n'entre que
    s'il est dans sa bio ou son lien en bio : cité n'est pas possédé."""
    if p.banal or p.type not in (*piv.PIVOTS_INFRASTRUCTURE, *piv.PIVOTS_ARGENT):
        return False
    if p.type in (piv.DOMAINE, piv.URL) and p.n_comptes < 2:
        return any(pr.champ in piv.CHAMPS_DE_PROPRIETE for pr in p.preuves)
    return True


def _libelle_pivot(p: piv.Pivot) -> str:
    if p.type == piv.PORTEFEUILLE:
        return f"Portefeuille {p.sous_type or ''} {p.valeur}".replace("  ", " ")
    if p.type == piv.PAIEMENT:
        return f"Paiement {p.sous_type or ''} {p.valeur}".replace("  ", " ")[:120]
    if p.type == piv.TELEGRAM:
        return f"Canal Telegram {p.valeur}"
    return f"{piv.LIBELLE_PIVOT[p.type]} {p.valeur}"[:120]


def _preuves_du_compte(p: piv.Pivot, compte: str, collecte: datetime, filtre_url: Any) -> tuple[Preuve, ...]:
    """Les preuves d'un compte pour un pivot : URL d'origine, extrait exact (e-mails et numéros masqués),
    seconde. Une preuve sans URL d'origine est écartée, comme celle que `filtre_url` refuse."""
    sortie: list[Preuve] = []
    for pr in p.preuves:
        if pr.compte != compte or not pr.url or (filtre_url is not None and not filtre_url(pr.url)):
            continue
        try:
            sortie.append(replace(pr.vers_preuve(collecte), extrait=piv.masquer(pr.extrait)))
        except ValueError:
            continue
        if len(sortie) >= MAX_PREUVES_PAR_LIEN:
            break
    return tuple(sortie)


def _url_du_pivot(p: piv.Pivot) -> str | None:
    if p.type in (piv.DOMAINE, piv.URL, piv.PAGE_DE_LIENS, piv.TELEGRAM, piv.BOUTIQUE):
        return "https://" + p.valeur
    return None


@dataclass
class PivotsPoses:
    """Ce que `poser_pivots` a mis au réseau : le nœud de chaque pivot, et les pivots eux-mêmes."""

    noeuds: dict[tuple[str, str, str | None], Noeud] = field(default_factory=dict)
    pivots: dict[tuple[str, str, str | None], piv.Pivot] = field(default_factory=dict)
    partages: int = 0
    questions: int = 0
    extraits: int = 0


def _cle_pivot(p: piv.Pivot) -> tuple[str, str, str | None]:
    return (p.type, p.valeur, p.sous_type)


def poser_pivots(
    reseau: ReseauEnquete, pivots: list[piv.Pivot], collecte: datetime,
    date_gel: Any = None, filtre_url: Any = None,
) -> PivotsPoses:
    """Pose au réseau les pivots non banals : infrastructure au rôle Infrastructure, argent au rôle Bénéficiaires.

    Chaque compte porteur y est relié par un lien typé, prouvé (le texte du compte contient la valeur
    mot pour mot). Que deux comptes affichent le même point commun est un fait ; qu'ils aient le même
    propriétaire reste une question ouverte, avec l'explication innocente à écarter.

    `date_gel` : les pivots viennent de textes déjà bornés par la date de publication ; le gel ne sert
    qu'aux sources externes (`enrichir_pivots`, `suivre_l_argent`). `filtre_url` écarte les preuves refusées.
    """
    resultat = PivotsPoses(extraits=len(pivots))
    comptes = _comptes_du_reseau(reseau)
    entrants = [p for p in pivots if _pivot_entre_au_reseau(p)]
    entrants.sort(key=lambda p: (p.n_comptes < 2, -p.poids_rarete, piv._PRIORITE_TYPE.get(p.type, 9), p.valeur))
    for p in entrants:
        liens_prevus = []
        for compte in p.comptes:
            noeud_compte = comptes.get(compte)
            preuves = _preuves_du_compte(p, compte, collecte, filtre_url) if noeud_compte else ()
            if preuves:
                liens_prevus.append((compte, noeud_compte, preuves))
        if not liens_prevus:
            continue  # pas d'URL d'origine, pas de preuve, pas de pièce
        attributs = {
            "pivot": p.type, "sous_type": p.sous_type, "valeur": p.valeur,
            "comptes": [_nom_du_compte(c) for c, _, _ in liens_prevus], "nb_comptes": len(liens_prevus),
            "poids_rarete": p.poids_rarete, "partage": len(liens_prevus) >= 2,
            "explication_innocente": piv.EXPLICATION_INNOCENTE.get(p.type),
        }
        if _url_du_pivot(p):
            attributs["url"] = _url_du_pivot(p)
        if p.type == piv.PAGE_DE_LIENS:
            attributs["liens_sortants_lus"] = not p.a_deplier
        noeud = reseau.ajouter_noeud(Noeud(
            piv.TYPE_NOEUD_PIVOT[p.type], _libelle_pivot(p), piv.ROLE_PIVOT[p.type], attributs, SOLIDITE_PIVOT,
        ))
        resultat.noeuds[_cle_pivot(p)] = noeud
        resultat.pivots[_cle_pivot(p)] = p
        force = max(1.0, round(p.poids_rarete / math.log(10), 2))
        for compte, noeud_compte, preuves in liens_prevus:
            reseau.ajouter_lien(Lien(
                type_lien=piv.TYPE_LIEN_RESEAU[p.type], famille=piv.FAMILLE_PIVOT[p.type],
                source_id=noeud_compte.id, cible_id=noeud.id, preuves=preuves,
                explication=f"Le texte de {_nom_du_compte(compte)} contient {piv.LIBELLE_PIVOT[p.type].lower()} "
                            f"{p.valeur}, mot pour mot.",
                methode="pivot extrait du texte du compte par motif (bio, légende, parole, texte à l'écran)",
                agent=AGENT_PIVOTS, statut=StatutLien.PROUVE, oriente=True, force_points=force,
            ))
    resultat.partages = sum(1 for p in resultat.pivots.values() if p.n_comptes >= 2)
    # Les questions suivent les trois questions de base : portefeuilles et paiements d'abord (ordre de relier_par_pivots).
    for lien in piv.relier_par_pivots(resultat.pivots.values(), inclure_banals=False):
        noms = ", ".join(_nom_du_compte(c) for c in lien.comptes)
        reseau.poser_question(
            f"Même propriétaire ? {noms} affichent le même {piv.LIBELLE_PIVOT[lien.pivot_type].lower()} : "
            f"{lien.valeur}. Ce point commun est prouvé, pas l'identité de celui qui est derrière. "
            f"À écarter d'abord : {lien.explication_innocente}"
        )
        resultat.questions += 1
    return resultat


def _masquer_entrepreneur(indice: Indice) -> tuple[str, str]:
    """Extrait et résumé d'un indice du registre, sans le nom d'un entrepreneur individuel (une personne)."""
    extrait, resume = indice.extrait, indice.resume
    donnees = indice.donnees or {}
    if indice.source == "recherche-entreprises" and str(donnees.get("nature_juridique")) == "1000" and donnees.get("nom"):
        for texte in (str(donnees["nom"]),):
            extrait = extrait.replace(texte, "[entrepreneur individuel]")
            resume = resume.replace(texte, "[entrepreneur individuel]")
    return piv.masquer(extrait), resume


def _poser_indice(
    reseau: ReseauEnquete, noeud_pivot: Noeud, indice: Indice, type_pivot: str,
) -> Noeud:
    extrait, resume = _masquer_entrepreneur(indice)
    donnees = indice.donnees
    if indice.source == "recherche-entreprises" and str(donnees.get("nature_juridique")) == "1000":
        donnees = {**donnees, "nom": "[entrepreneur individuel]"}
    document = reseau.ajouter_noeud(Noeud(
        TypeNoeud.DOCUMENT, f"{indice.source} : {resume}"[:140], noeud_pivot.role,
        {"url": indice.url, "source": indice.source, "sujet": indice.sujet, "resume": resume,
         "date_lecture": indice.date_lecture.isoformat(), "donnees": donnees},
        SOLIDITE_PAR_SOURCE.get(indice.source, SOLIDITE_SOURCE_OUVERTE),
    ))
    preuve = replace(indice.vers_preuve(), extrait=extrait or resume)
    reseau.ajouter_lien(Lien(
        type_lien="documente", famille=piv.FAMILLE_PIVOT[type_pivot],
        source_id=document.id, cible_id=noeud_pivot.id, preuves=(preuve,),
        explication=f"{indice.source} répond sur {indice.sujet} : {resume}"[:300],
        methode=f"lecture publique de {indice.source} (sans clé)", agent=AGENT_INFRA,
        statut=StatutLien.PROUVE, oriente=True, force_points=1.0,
    ))
    return document


def enrichir_pivots(
    reseau: ReseauEnquete, infra: Infra, posees: PivotsPoses, date_gel: Any = None, filtre_url: Any = None,
    maximum: int = MAX_ENRICHIS,
) -> dict[str, int]:
    """Enrichit au plus `maximum` pivots du réseau (domaines, SIREN, SIRET) : partagés d'abord, puis par poids de
    rareté. Un pivot à la fois, pour que le direct les voie arriver. Chaque indice devient un nœud document relié
    au pivot ; un domaine frère par certificat qui est aussi un domaine du dossier donne un lien `meme_certificat`
    (indice) et une question « même propriétaire ? ».
    """
    candidats = [(c, p) for c, p in posees.pivots.items() if p.type in TYPES_ENRICHIS]
    candidats.sort(key=lambda cp: (cp[1].n_comptes < 2, -cp[1].poids_rarete, cp[1].valeur))
    domaines = {p.valeur.lower(): posees.noeuds[c] for c, p in posees.pivots.items() if p.type == piv.DOMAINE}
    par_registrable = {}
    for valeur, noeud in domaines.items():
        par_registrable.setdefault(piv._registrable(valeur), noeud)
    paires: set[frozenset[str]] = set()
    stats = {"enrichis": 0, "indices": 0, "documents": 0, "meme_certificat": 0, "erreurs": 0}
    for cle, p in candidats[:maximum]:
        try:
            enrichi = infra.enrichir([p], filtre_url=filtre_url, date_gel=date_gel)[0]
        except Exception:  # noqa: BLE001 - une source ouverte en panne n'arrête pas l'enquête
            stats["erreurs"] += 1
            continue
        stats["enrichis"] += 1
        noeud = posees.noeuds[cle]
        for indice in enrichi.indices:
            stats["indices"] += 1
            _poser_indice(reseau, noeud, indice, p.type)
            stats["documents"] += 1
            if indice.source != "crt.sh" or p.type != piv.DOMAINE:
                continue
            for frere in (indice.donnees or {}).get("freres") or []:
                autre = domaines.get(frere.lower()) or par_registrable.get(piv._registrable(frere.lower()))
                if autre is None or autre.id == noeud.id or frozenset((noeud.id, autre.id)) in paires:
                    continue
                paires.add(frozenset((noeud.id, autre.id)))
                reseau.ajouter_lien(Lien(
                    type_lien="meme_certificat", famille=FamilleLien.INFRASTRUCTURE,
                    source_id=noeud.id, cible_id=autre.id, preuves=(replace(
                        indice.vers_preuve(), extrait=piv.masquer(indice.extrait) or indice.resume),),
                    explication=f"{p.valeur} et {frere} figurent sur un même certificat TLS (journal crt.sh).",
                    methode="noms portés par les certificats du domaine (crt.sh)", agent=AGENT_INFRA,
                    statut=StatutLien.INDICE, force_points=1.0,
                ))
                reseau.poser_question(
                    f"Même propriétaire ? {p.valeur} et {frere} figurent sur un même certificat TLS (crt.sh). "
                    "À écarter d'abord : un hébergeur ou un service de protection qui regroupe des clients "
                    "sans lien sur un même certificat."
                )
                posees.questions += 1
                stats["meme_certificat"] += 1
    return stats


# --------------------------------------------------------------------------- argent qui bouge


def _hote(url: str) -> str:
    return urlsplit(url).hostname or url


def _lire_instant(texte: Any, defaut: datetime) -> datetime:
    try:
        return datetime.fromisoformat(str(texte).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return defaut


def _sanction_sans_noms(sanction: dict[str, Any]) -> dict[str, Any]:
    """Le contrôle OFAC sans le nom des désignés : le portefeuille reste sans nom de personne."""
    entrees = sanction.get("entrees") or []
    return {
        "statut": sanction.get("statut"), "liste_du": sanction.get("liste_du"), "source": sanction.get("source"),
        "programmes": sorted({e.get("programme") for e in entrees if e.get("programme")}),
        "references": [e["url"] for e in entrees if e.get("url")],
    }


def _profil_pour_le_noeud(profil: dict[str, Any]) -> dict[str, Any]:
    d = profil["donnees"]
    return {
        "chaine": d.get("chaine"), "premiere_activite": d.get("premiere_activite"),
        "derniere_activite": d.get("derniere_activite"), "nb_transactions": d.get("nb_transactions"),
        "totaux": d.get("totaux"),
        # Un nom ENS est choisi par le propriétaire : il n'établit pas l'identité, on ne le recopie pas.
        "etiquettes": [e for e in d.get("etiquettes") or [] if e.get("categorie") != "ens"],
        "plateforme_echange": d.get("plateforme_echange"), "doit_repondre": d.get("doit_repondre"),
        "sanction_ofac": _sanction_sans_noms(d.get("sanction_ofac") or {}),
        "avertissements": d.get("avertissements"), "partiel": d.get("partiel"),
        "attribution": d.get("attribution") or sources_chaines.MENTION_ATTRIBUTION,
    }


def _liens_de_flux(
    reseau: ReseauEnquete, chaine: str, adresse_a: str, noeud_a: Noeud, contrepartie: dict[str, Any],
    adresse_b: str, noeud_b: Noeud, collecte: datetime,
) -> int:
    """Un lien `flux_d_argent` par sens, orienté de l'envoyeur vers le receveur, une preuve par transaction (3 au plus)."""
    par_sens: dict[str, list[dict[str, Any]]] = {"envoye": [], "recu": []}
    for tx in contrepartie["donnees"].get("transactions") or []:
        if tx.get("sens") in par_sens and tx.get("url"):
            par_sens[tx["sens"]].append(tx)
    crees = 0
    for sens, transactions in par_sens.items():
        if not transactions:
            continue
        source, cible = (noeud_a, noeud_b) if sens == "envoye" else (noeud_b, noeud_a)
        de, vers = (adresse_a, adresse_b) if sens == "envoye" else (adresse_b, adresse_a)
        preuves = tuple(
            Preuve(
                url=tx["url"], source_donnee=f"explorateur public ({_hote(tx['url'])})",
                extrait=f"{tx['montant']} {tx['actif']}{' (montant approché)' if tx.get('approx') else ''}"
                        f" · {str(tx.get('date') or 'date inconnue')[:10]} · {de} vers {vers}",
                date_collecte=_lire_instant(contrepartie.get("date_lecture"), collecte),
            )
            for tx in transactions[:MAX_PREUVES_PAR_LIEN]
        )
        reseau.ajouter_lien(Lien(
            type_lien="flux_d_argent", famille=FamilleLien.ARGENT, source_id=source.id, cible_id=cible.id,
            preuves=preuves,
            explication=f"{len(transactions)} transaction(s) publique(s) de {de} vers {vers} sur {chaine}.",
            methode="lecture de l'explorateur public de la chaîne (identifiant de transaction officiel)",
            agent=AGENT_ARGENT, statut=StatutLien.PROUVE, oriente=True, force_points=2.0,
            identifiant_officiel=True,
        ))
        crees += 1
    return crees


def suivre_l_argent(
    reseau: ReseauEnquete, posees: PivotsPoses, collecte: datetime, client: Any = None,
    date_gel: Any = None, filtre_url: Any = None, maximum: int = MAX_PORTEFEUILLES,
) -> dict[str, Any]:
    """Pour chaque portefeuille non banal du réseau (15 au plus) : profil, contreparties, flux entre portefeuilles
    du dossier, plateformes d'échange (« doit répondre »), sanctions OFAC. Un portefeuille n'est jamais attribué
    à une personne. Une erreur de source est notée et la chaîne continue : un portefeuille que l'explorateur ne
    connaît pas (jamais utilisé) reste dans le dossier et peut recevoir un flux."""
    stats: dict[str, Any] = {"portefeuilles": 0, "lus": 0, "flux": 0, "plateformes": 0, "sanctions": 0, "erreurs": []}
    portefeuilles = [(c, p) for c, p in posees.pivots.items() if p.type == piv.PORTEFEUILLE and not p.banal]
    portefeuilles.sort(key=lambda cp: (cp[1].n_comptes < 2, -cp[1].poids_rarete, cp[1].valeur))
    stats["portefeuilles"] = len(portefeuilles[:maximum])
    # clé de comparaison de l'adresse -> (chaîne, adresse, nœud), pour tous les portefeuilles lisibles du dossier
    dossier_des_portefeuilles: dict[str, tuple[str, str, Noeud]] = {}
    lus: dict[str, tuple[Noeud, list[dict[str, Any]]]] = {}
    for cle, p in portefeuilles[:maximum]:
        noeud = posees.noeuds[cle]
        chaine = sources_chaines.chaine_du_pivot(p.sous_type)
        if chaine is None:
            stats["erreurs"].append(f"portefeuille {p.sous_type} {p.valeur} : chaîne non prise en charge")
            reseau.corriger_noeud(
                noeud.id, "chaîne non prise en charge : portefeuille non lu",
                attributs={**noeud.attributs, "profil": {"chaine_non_prise_en_charge": p.sous_type}},
            )
            continue
        cle_adresse = sources_chaines._cle(chaine, p.valeur)
        dossier_des_portefeuilles[cle_adresse] = (chaine, p.valeur, noeud)
        profil = None
        contreparties: list[dict[str, Any]] = []
        try:
            profil = sources_chaines.profil_portefeuille(chaine, p.valeur, date_gel, client=client, filtre_url=filtre_url)
            if profil:
                contreparties = sources_chaines.contreparties(
                    chaine, p.valeur, 50, date_gel, client=client, filtre_url=filtre_url)
        except Exception as erreur:  # noqa: BLE001 - ChainesError (bloquée, limitée, introuvable) et réseau
            stats["erreurs"].append(f"portefeuille {p.valeur} ({chaine}) : {type(erreur).__name__} : {erreur}")
            profil = None
        if profil is not None:
            stats["lus"] += 1
            resume = _profil_pour_le_noeud(profil)
            sanction = profil["donnees"].get("sanction_ofac") or {}
        else:
            # Pas de page d'adresse lisible : le contrôle des sanctions (autre source) se fait quand même.
            try:
                sanction = sources_chaines._client(client).controle_sanction(p.valeur)
            except Exception:  # noqa: BLE001
                sanction = {}
            resume = {"chaine": chaine, "lu_sur_l_explorateur": False,
                      "sanction_ofac": _sanction_sans_noms(sanction),
                      "attribution": sources_chaines.MENTION_ATTRIBUTION}
        if p.sous_type == "evm":
            resume["autres_chaines_evm"] = "non lues : seule Ethereum est interrogée"
        noeud = reseau.corriger_noeud(
            noeud.id, "profil public du portefeuille (explorateur)", attributs={**noeud.attributs, "profil": resume},
        )
        dossier_des_portefeuilles[cle_adresse] = (chaine, p.valeur, noeud)
        if profil is not None:
            # Une preuve de plus sur les liens qui montrent ce portefeuille : sa page d'adresse sur l'explorateur.
            page = Preuve(
                url=profil["url_source"], extrait=piv.masquer(profil["extrait"]),
                source_donnee=f"explorateur public ({_hote(profil['url_source'])})",
                date_collecte=_lire_instant(profil.get("date_lecture"), collecte),
            )
            for lien in reseau.liens():
                if lien.cible_id == noeud.id and page not in lien.preuves:
                    reseau.corriger_lien(lien.id, "page d'adresse sur l'explorateur", preuves=(*lien.preuves, page))
            lus[cle_adresse] = (noeud, contreparties)
        if sanction.get("statut") == "oui":
            stats["sanctions"] += 1
            reseau.poser_question(
                f"Le portefeuille {p.valeur} ({chaine}) figure sur la liste de sanctions OFAC (SDN), publique. "
                "Seul un tiers qui tient le registre du titulaire (plateforme d'échange, justice) peut dire à qui "
                "il appartient : qui le contrôle, et depuis quand ?"
            )
            posees.questions += 1

    plateformes: dict[str, Noeud] = {}
    paires: set[frozenset[str]] = set()
    for cle_a, (noeud_a, contreparties) in lus.items():
        chaine, adresse_a, _ = dossier_des_portefeuilles[cle_a]
        for c in contreparties:
            d = c["donnees"]
            adresse_b = d["adresse"]
            cle_b = sources_chaines._cle(chaine, adresse_b)
            if cle_b in dossier_des_portefeuilles and cle_b != cle_a:
                paire = frozenset((cle_a, cle_b))
                if paire in paires:
                    continue
                paires.add(paire)
                noeud_b = dossier_des_portefeuilles[cle_b][2]
                stats["flux"] += _liens_de_flux(reseau, chaine, adresse_a, noeud_a, c, adresse_b, noeud_b, collecte)
            elif d.get("doit_repondre"):
                noeud_b = plateformes.get(cle_b)
                etiquettes = [e for e in d.get("etiquettes") or [] if e.get("nom") and e.get("categorie") != "ens"]
                nom = etiquettes[0]["nom"] if etiquettes else adresse_b
                if noeud_b is None:
                    noeud_b = reseau.ajouter_noeud(Noeud(
                        TypeNoeud.PORTEFEUILLE, f"Plateforme d'échange : {nom}"[:120], Role.INFRASTRUCTURE,
                        {"chaine": chaine, "adresse": adresse_b, "doit_repondre": True,
                         "mention": sources_chaines.TEXTE_DOIT_REPONDRE, "etiquettes": etiquettes,
                         "attribution": sources_chaines.MENTION_ATTRIBUTION},
                        SOLIDITE_EXPLORATEUR,
                    ))
                    plateformes[cle_b] = noeud_b
                    stats["plateformes"] += 1
                    reseau.poser_question(
                        f"Qui détient le compte {nom} ({adresse_b}) qui échange de l'argent avec {adresse_a} ? "
                        "La plateforme d'échange peut l'identifier sur réquisition judiciaire : à elle de répondre."
                    )
                    posees.questions += 1
                stats["flux"] += _liens_de_flux(reseau, chaine, adresse_a, noeud_a, c, adresse_b, noeud_b, collecte)
    return stats


# --------------------------------------------------------------------------- rendu pour le banc


def rendu_pour_banc(export: dict[str, Any], affaire_id: str) -> dict[str, Any]:
    """Traduit le réseau exporté en faits du format de `banc.py`.

    Un fait par grappe et un fait pour la vidéo la plus ancienne, chacun avec les preuves de ses
    liens ; chaque question ouverte devient un aveu (« non établi »).
    """
    pieces = {p["id"]: p for d in export["dossiers"] for p in d["pieces"]}
    faits: list[dict[str, Any]] = []

    for piece in pieces.values():
        if piece["type"] != "grappe":
            continue
        liens = [l for l in export["liens"] if l["cible_id"] == piece["id"]]
        videos = [pieces[l["source_id"]] for l in liens]
        premiere = min(videos, key=lambda v: v["attributs"]["date"])
        date = premiere["attributs"]["date"][:10]
        faits.append({
            "id": f"g{len(faits) + 1}",
            "texte": f"{len(videos)} vidéos disent la même chose : {piece['libelle']}. "
                     f"La plus ancienne, du {date}, est : {premiere['libelle']}.",
            "preuves": [{"url": p["url"], "extrait": p["extrait"], "debut_s": p["debut_s"]}
                        for l in liens for p in l["preuves"]],
        })

    videos = [p for p in pieces.values() if p["type"] == "video"]
    if videos:
        premiere = min(videos, key=lambda v: v["attributs"]["date"])
        liens = [l for l in export["liens"] if premiere["id"] in (l["source_id"], l["cible_id"])]
        faits.append({
            "id": f"v{len(faits) + 1}",
            "texte": f"La plus ancienne vidéo repérée date du {premiere['attributs']['date'][:10]} : "
                     f"{premiere['libelle']}.",
            "preuves": [{"url": p["url"], "extrait": p["extrait"], "debut_s": p["debut_s"]}
                        for l in liens for p in l["preuves"]][:3],
        })

    for question in export["questions_ouvertes"]:
        if question["statut"] == "ouverte":
            faits.append({"id": f"q{len(faits) + 1}", "texte": question["texte"],
                          "statut": "non_etabli", "preuves": []})
    return {"affaire_id": affaire_id, "faits": faits}


# --------------------------------------------------------------------------- orchestration


def _captures_png(archives: dict[str, PieceArchivee], dossier: Path) -> dict[str, str]:
    """URL de la pièce -> chemin de sa capture de page PNG, quand l'archivage en a pris une."""
    captures: dict[str, str] = {}
    for piece in archives.values():
        for fichier in piece.fichiers:
            if fichier.get("role") == "page_png" and fichier.get("nom"):
                chemin = dossier / fichier["nom"]
                if chemin.is_file():
                    captures[piece.url] = str(chemin)
    return captures


def _importer_images() -> Any:
    """Le module d'images, importé à l'appel et jamais au chargement : s'il manque, la chaîne s'en passe."""
    return importlib.import_module("enquete.images")


def illustrer(
    export: dict[str, Any], resultats: dict[str, dict[str, Any]], archives: dict[str, PieceArchivee], dossier: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """Une image par pièce et par dossier, si `enquete.images` est importable (import à l'appel).

    Rend (export à écrire, contenu d'images.json ou None, bilan pour le rapport). Toute erreur, `ImageInvalide`
    comprise, garde l'export d'origine et s'inscrit au bilan : la chaîne ne s'arrête jamais pour une image.
    """
    try:
        module = _importer_images()
    except ImportError:
        return export, None, {}
    try:
        enrichi = module.illustrer_export(
            export, resultats, _captures_png(archives, dossier / "archives"), dossier / "archives" / "images",
        )
    except Exception as erreur:  # noqa: BLE001
        return export, None, {"erreur": f"{type(erreur).__name__} : {erreur}"}
    images = {
        "pieces": {p["id"]: p.get("image") for d in enrichi["dossiers"] for p in d["pieces"]},
        "dossiers": {
            d["role"]: {k: d.get(k) for k in ("image", "image_piece_id", "silhouette")} for d in enrichi["dossiers"]
        },
        "qr_trouves": enrichi.get("qr_trouves") or [],
        "images_echecs": enrichi.get("images_echecs") or [],
    }
    bilan = {
        "pieces_illustrees": sum(1 for image in images["pieces"].values() if image),
        "qr_trouves": len(images["qr_trouves"]), "echecs": len(images["images_echecs"]),
    }
    return enrichi, images, bilan


def enqueter(
    question: str,
    mots: list[str],
    client: OrianeClient,
    dossier: Path,
    affaire_id: str,
    plafond_credits: int = 600,
    depuis: str | None = None,
    limite: int = 50,
    retenues: int = 60,
    nb_archives: int = 2,
    *,
    chemin_journal: str | Path | None = None,
    pivots: bool = False,
    infra: Infra | None = None,
    chaines: Any = None,
    date_gel: Any = None,
    filtre_url: Any = None,
) -> dict[str, Any]:
    """Fait tourner toute la chaîne et écrit ses sorties dans `dossier`. Renvoie le rapport de passage.

    `chemin_journal` : journal SQLite écrit AU FIL de l'enquête, à l'heure réelle de chaque étape (question
    centrale tout de suite, pièces et liens dès la relecture `full`, pivots un à un, archives en dernier) ;
    le serveur le lit pour le direct et pour le rejeu. Sans lui, le réseau vit en mémoire comme avant.

    `pivots` : lit les profils des comptes, extrait les pivots (domaines, codes promo, portefeuilles...),
    les pose au réseau, enrichit 15 pivots au plus par des sources ouvertes (`infra`) et suit les portefeuilles
    sur les explorateurs (`chaines`). Désactivé par défaut : la chaîne de base ne change pas.
    """
    debut = datetime.now(UTC)
    dossier.mkdir(parents=True, exist_ok=True)
    budget = Budget(client, plafond_credits)
    reseau = ReseauEnquete(question, str(chemin_journal) if chemin_journal else ":memory:")
    infra_ouverte = infra is None and pivots
    stats_pivots: dict[str, Any] = {}
    bilan_images: dict[str, Any] = {}
    images_json: dict[str, Any] | None = None
    try:
        candidats = chercher_candidats(budget, mots, depuis, limite)
        ids = retenir(candidats, retenues)
        completes = relire_en_full(budget, ids)
        videos = []
        bruts: dict[str, dict[str, Any]] = {}
        for identifiant in ids:
            # Sans relecture `full`, on garde la version `basic` : pas de phrases, mais légende et date.
            resultat = completes.get(identifiant) or candidats[identifiant]["resultat"]
            video = video_depuis_resultat(resultat)
            if video:
                videos.append(video)
                bruts[identifiant] = resultat
        grappes = regrouper(videos)

        # Rôles et extraits exacts sont connus : tout le réseau de base entre au journal d'un coup, sans
        # nœud à corriger ensuite. L'archivage (long) passe après, pour ne pas bloquer le direct.
        profils = lire_profils(budget, ((v.plateforme, v.pseudo) for v in videos)) if pivots else []
        construire_reseau(question, videos, grappes, {}, debut, reseau=reseau)

        if pivots:
            extraits = piv.extraire_pivots(list(bruts.values()), profils)
            posees = poser_pivots(reseau, extraits, debut, date_gel, filtre_url)
            stats_pivots = {
                "profils_lus": len(profils), "pivots_extraits": len(extraits), "pivots_au_reseau": len(posees.noeuds),
                "pivots_partages": posees.partages, "questions_meme_proprietaire": posees.questions,
            }
            if posees.noeuds:
                infra = infra or Infra()
                try:
                    stats_pivots["infra"] = enrichir_pivots(reseau, infra, posees, date_gel, filtre_url)
                    stats_pivots["pivots_enrichis"] = stats_pivots["infra"]["enrichis"]
                    stats_pivots["appels_infra"] = infra.appels_reseau
                    stats_pivots["rejets_infra"] = len(infra.journal)
                finally:
                    if infra_ouverte:
                        infra.close()
                stats_pivots["argent"] = suivre_l_argent(reseau, posees, debut, chaines, date_gel, filtre_url)
                erreurs_argent = stats_pivots["argent"].pop("erreurs")
                stats_pivots["argent"]["erreurs"] = len(erreurs_argent)
                budget.compteur.erreurs.extend(erreurs_argent)

        a_archiver = choisir_a_archiver(videos, grappes, nb_archives)
        archives = archiver(a_archiver, dossier / "archives") if a_archiver else {}
        poser_archives(reseau, archives)

        export = reseau.exporter_pour_interface()
        export, images_json, bilan_images = illustrer(export, bruts, archives, dossier)
    finally:
        reseau.fermer()

    rendu = rendu_pour_banc(export, affaire_id)
    rapport = {
        "affaire_id": affaire_id,
        "question": question,
        "mots_cles": mots,
        "date": debut.isoformat(timespec="seconds"),
        "plafond_credits": plafond_credits,
        "appels_payants": budget.compteur.appels,
        "appels_en_cache": budget.compteur.appels_en_cache,
        "credits_depenses": budget.compteur.credits,
        "plafond_atteint": budget.compteur.plafond_atteint,
        "erreurs": budget.compteur.erreurs,
        "videos_trouvees": len(candidats),
        "videos_retenues": len(ids),
        "videos_analysees": len(videos),
        "videos_par_plateforme": {
            p: sum(1 for v in videos if v.plateforme == p) for p in sorted({v.plateforme for v in videos})
        },
        "grappes": len(grappes),
        "tailles_des_grappes": [len(g.videos) for g in grappes],
        "liens": export["compteurs"]["liens"],
        "preuves": export["compteurs"]["preuves"],
        "questions_ouvertes": export["compteurs"]["zones_d_ombre"],
        "archives": [
            {"url": p.url, "reussie": p.complete, "statuts": p.statuts,
             "empreinte_sha256": p.empreinte_sha256, "url_wayback": p.url_wayback,
             "voies": {nom: voie.get("statut") for nom, voie in p.voies.items()},
             "horodatages": sum(1 for j in p.horodatages if j.get("statut") == "ok")}
            for p in archives.values()
        ],
    }
    if chemin_journal:
        rapport["journal"] = str(chemin_journal)
    if pivots:
        rapport["pivots"] = stats_pivots
    if bilan_images:
        rapport["images"] = bilan_images
    sorties = [("reseau.json", export), ("rendu-banc.json", rendu), ("passage.json", rapport)]
    if images_json is not None:
        sorties.append(("images.json", images_json))
    for nom, contenu in sorties:
        (dossier / nom).write_text(json.dumps(contenu, ensure_ascii=False, indent=2), encoding="utf-8")
    return rapport


def _separer_mots(brut: str) -> list[str]:
    return [m.strip() for m in re.split(r"[,;]", brut) if m.strip()]


def _console_utf8() -> None:
    """Console en UTF-8 : sous Windows elle est en cp1252 et « Enquête » ou « réseau » s'afficheraient de travers.
    Sans effet si le flux ne se reconfigure pas (capture de pytest, flux remplacé)."""
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _ranger_l_ancien_journal(journal: Path) -> Path | None:
    """Un journal d'un passage précédent est renommé `reseau-AAAAMMJJTHHMMSS.sqlite`, jamais effacé."""
    if not journal.exists():
        return None
    date = datetime.fromtimestamp(journal.stat().st_mtime)
    range_ = journal.with_name(f"{journal.stem}-{date:%Y%m%dT%H%M%S}{journal.suffix}")
    journal.replace(range_)
    return range_


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    analyseur = argparse.ArgumentParser(
        prog="python -m enquete.chaine",
        description="Enquête de bout en bout : recherche Oriane, grappes, archives, réseau prouvé.",
    )
    analyseur.add_argument("question", help="la question posée à l'enquête")
    analyseur.add_argument("--mots", required=True, help="mots-clés séparés par des virgules")
    analyseur.add_argument("--plafond-credits", type=int, default=600, help="crédits Oriane au maximum")
    analyseur.add_argument("--depuis", help="ne garder que les vidéos publiées à partir de cette date (AAAA-MM-JJ)")
    analyseur.add_argument("--nom", help="nom court de l'affaire (dossier de sortie, identifiant du rendu)")
    analyseur.add_argument("--sortie", default="donnees", help="dossier des sorties (ignoré par git)")
    analyseur.add_argument("--cache", default="cache/oriane", help="dossier du cache des réponses Oriane")
    analyseur.add_argument("--env", help="fichier .env qui porte ORIANE_API_KEY")
    analyseur.add_argument("--limite", type=int, default=50, help="résultats demandés par recherche")
    analyseur.add_argument("--retenues", type=int, default=60, help="vidéos relues en projection full")
    analyseur.add_argument("--archives", type=int, default=2, help="vidéos archivées au plus (0 : aucune)")
    analyseur.add_argument("--sans-pivots", action="store_true",
                           help="ne lit ni les profils ni les pivots (domaines, codes promo, portefeuilles)")
    args = analyseur.parse_args(argv)

    mots = _separer_mots(args.mots)
    if not mots:
        analyseur.error("--mots ne contient aucun mot")
    nom = args.nom or _nom_dossier(args.question)
    dossier = Path(args.sortie) / nom
    dossier.mkdir(parents=True, exist_ok=True)
    # Le journal du passage : le serveur le rejoue avec ses vrais temps (`/api/rejouer`).
    journal = dossier / "reseau.sqlite"
    _ranger_l_ancien_journal(journal)
    try:
        with OrianeClient(dossier_cache=args.cache, fichier_env=args.env) as client:
            rapport = enqueter(
                args.question, mots, client, dossier, nom,
                args.plafond_credits, args.depuis, args.limite, args.retenues, args.archives,
                chemin_journal=journal, pivots=not args.sans_pivots,
            )
    except OrianeError as erreur:
        print(f"Erreur Oriane : {erreur}", file=sys.stderr)
        return 1
    resume = {k: v for k, v in rapport.items() if k not in ("archives", "erreurs", "question", "mots_cles")}
    print(json.dumps(resume, ensure_ascii=False, indent=2))
    print(f"Archives : {sum(a['reussie'] for a in rapport['archives'])}/{len(rapport['archives'])} réussies.")
    print(f"Sorties dans {Path(args.sortie) / nom}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
