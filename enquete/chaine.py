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
import json
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from enquete.archive import PieceArchivee, archiver_video
from enquete.liens import url_origine
from enquete.reseau import (
    ExplicationEcartee,
    FamilleLien,
    Lien,
    Noeud,
    Preuve,
    ReseauEnquete,
    Role,
    Solidite,
    StatutLien,
    TypeNoeud,
)
from enquete.sources.oriane import (
    CREDITS_CONTENUS,
    OrianeAuthError,
    OrianeClient,
    OrianeCreditsError,
    OrianeError,
)
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
SOLIDITE_VIDEO = Solidite("B", 2)  # donnée d'une plateforme, relue telle quelle
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

    def chercher(self, projection: str, **parametres: Any) -> dict[str, Any]:
        tarif = CREDITS_CONTENUS[projection]
        if self.client.credits_estimes + tarif > self.plafond:
            self.compteur.plafond_atteint = True
            raise PlafondAtteint(f"plafond de {self.plafond} crédits atteint")
        avant = self.client.credits_estimes
        reponse = self.client.chercher_contenus(projection=projection, **parametres)
        depense = self.client.credits_estimes - avant
        if depense:
            self.compteur.appels += 1
            self.compteur.credits += depense
        else:
            self.compteur.appels_en_cache += 1
        return reponse


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


def archiver(videos: list[Video], dossier: Path) -> dict[str, PieceArchivee]:
    """Archive chaque vidéo (yt-dlp réel, basse définition, Wayback). Une erreur n'arrête pas le reste."""
    pieces: dict[str, PieceArchivee] = {}
    for video in videos:
        assert video.url  # garanti par `choisir_a_archiver`
        try:
            pieces[video.id_oriane] = archiver_video(video.url, dossier=dossier)
        except (ValueError, OSError) as erreur:
            piece = PieceArchivee(url=video.url, type="video", date_collecte=datetime.now(UTC).isoformat())
            piece.statuts["telechargement"] = "echec"
            piece.noter(f"archivage impossible : {erreur}")
            pieces[video.id_oriane] = piece
    return pieces


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
        extrait=extrait,
        source_donnee=SOURCE,
        date_collecte=collecte,
        empreinte_sha256=piece.empreinte_sha256 if piece else None,
        debut_s=debut,
        fin_s=fin,
    )


def construire_reseau(
    question: str, videos: list[Video], grappes: list[Grappe], archives: dict[str, PieceArchivee],
    collecte: datetime, chemin: str = ":memory:",
) -> ReseauEnquete:
    """Pose nœuds, liens prouvés et questions ouvertes. Une vidéo sans URL n'entre pas : pas d'URL, pas de preuve."""
    reseau = ReseauEnquete(question, chemin)
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
                {"plateforme": video.plateforme, "abonnes": video.abonnes}, SOLIDITE_VIDEO,
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

    _poser_questions_de_base(reseau, grappes, citables)
    return reseau


def _poser_questions_de_base(reseau: ReseauEnquete, grappes: list[Grappe], videos: list[Video]) -> None:
    """Les trois zones d'ombre que la chaîne ne sait pas combler seule."""
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
) -> dict[str, Any]:
    """Fait tourner toute la chaîne et écrit ses sorties dans `dossier`. Renvoie le rapport de passage."""
    debut = datetime.now(UTC)
    dossier.mkdir(parents=True, exist_ok=True)
    budget = Budget(client, plafond_credits)

    candidats = chercher_candidats(budget, mots, depuis, limite)
    ids = retenir(candidats, retenues)
    completes = relire_en_full(budget, ids)
    videos = []
    for identifiant in ids:
        # Sans relecture `full`, on garde la version `basic` : pas de phrases, mais légende et date.
        resultat = completes.get(identifiant) or candidats[identifiant]["resultat"]
        video = video_depuis_resultat(resultat)
        if video:
            videos.append(video)
    grappes = regrouper(videos)

    a_archiver = choisir_a_archiver(videos, grappes, nb_archives)
    archives = archiver(a_archiver, dossier / "archives") if a_archiver else {}

    reseau = construire_reseau(question, videos, grappes, archives, debut)
    export = reseau.exporter_pour_interface()
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
             "empreinte_sha256": p.empreinte_sha256, "url_wayback": p.url_wayback}
            for p in archives.values()
        ],
    }
    for nom, contenu in (("reseau.json", export), ("rendu-banc.json", rendu), ("passage.json", rapport)):
        (dossier / nom).write_text(json.dumps(contenu, ensure_ascii=False, indent=2), encoding="utf-8")
    return rapport


def _separer_mots(brut: str) -> list[str]:
    return [m.strip() for m in re.split(r"[,;]", brut) if m.strip()]


def main(argv: list[str] | None = None) -> int:
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
    args = analyseur.parse_args(argv)

    mots = _separer_mots(args.mots)
    if not mots:
        analyseur.error("--mots ne contient aucun mot")
    nom = args.nom or _nom_dossier(args.question)
    try:
        with OrianeClient(dossier_cache=args.cache, fichier_env=args.env) as client:
            rapport = enqueter(
                args.question, mots, client, Path(args.sortie) / nom, nom,
                args.plafond_credits, args.depuis, args.limite, args.retenues, args.archives,
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
