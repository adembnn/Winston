"""Serveur local qui fait vivre l'interface : le réseau se construit sous les yeux du journaliste.

Image simple : une salle de rédaction avec un tableau en liège. Les agents
épinglent des fiches ; le serveur est la personne qui lit le journal du tableau
(la table `evenements` de SQLite, où l'on ne fait qu'ajouter) et crie chaque
nouveauté à l'interface, ou la rejoue plus vite pour la démo.

Écoute 127.0.0.1:8800. CORS ouvert aux origines localhost (et à `null`, l'origine
d'une page ouverte depuis un fichier, pour les maquettes HTML ; `autoriser_fichiers=False`
le retire). Bibliothèque standard seulement.

Routes
------
GET  /api/export?enquete=<id>                       réseau complet + `mode`
GET  /api/evenements?enquete=<id>&depuis=<n>        flux SSE (direct)
GET  /api/rejouer?enquete=<id>&facteur=20|duree_cible=25&depuis=<n>   flux SSE (démo)
GET  /api/recherche?enquete=<id>&q=<texte>[&limite=50]
POST /api/enquetes   {question, graine?}            lance l'enquête, rend {id}
POST /api/questions  {enquete, texte}               question du journaliste -> {id}
GET  /api/enquetes                                  liste des enquêtes (ajout hors contrat)
GET  /api/sante                                     {ok: true}

Mode : `export.mode` vaut « direct » si l'enquête tourne en ce moment, « demo »
sinon (journal enregistré, rejouable).

Correspondance journal -> événements du flux
--------------------------------------------
Le journal (`ReseauEnquete.historique`) a quatre genres de lignes. Chaque ligne
donne un ou plusieurs événements ; `n` compte les événements du flux (1, 2, 3...),
stable : relire le même journal redonne les mêmes `n`, donc `depuis=n` reprend
exactement où l'on s'est arrêté (l'en-tête `Last-Event-ID` aussi).

  genre « affaire »  -> `question`  donnees = {id: "question_centrale", texte, centrale: true,
                                              reformulee: bool}
  genre « noeud »    -> `noeud`     donnees = le nœud (`Noeud.vers_dict`) + motif
  genre « lien »     -> `lien`      donnees = le lien (`Lien.vers_dict`) + source_libelle,
                                    cible_libelle + motif ; puis un `preuve` par preuve
                                    pas encore vue : {lien_id, preuve, source_libelle,
                                    cible_libelle}. Une preuve déjà vue (même URL, même
                                    extrait, même seconde) n'est pas répétée.
  genre « question » -> `question`  donnees = la question (`QuestionOuverte.vers_dict`)
                                    + origine : « agent » ou « journaliste »
  après chaque ligne -> `compteur`  donnees = {pieces, liens, preuves, zones_d_ombre,
                                    par_role: {role: nombre de pièces}}
  fin du flux        -> `fin`       donnees = {ok, erreur?, evenements, compteurs, ...}
                                    (jamais écrit au journal ; en rejeu : duree_reelle_s,
                                    duree_rejeu_s, facteur)

Chaque événement : {n, horodatage, type, donnees, accelere, facteur}. `horodatage`
est la date du journal (jamais celle du rejeu). En direct : accelere=false,
facteur=null. En rejeu : facteur>1 -> accelere=true. Un facteur de 1 n'accélère
rien, donc accelere=false ; avec `duree_cible`, le facteur ne descend jamais sous 1
(on n'étire pas un journal pour le faire durer, ce serait un faux rythme).

Stockage : `<racine>/<id>/reseau.sqlite`. Un dossier qui ne contient que
`reseau.json` (sortie actuelle de `enqueter`) est importé à la première demande
(`importer_export`) : ses dates sont alors celles de l'import, d'un seul bloc.
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from enquete.recherche import Index, documents_depuis_export
from enquete.reseau import Lien, Noeud, ReseauEnquete, Role, Solidite

HOTE = "127.0.0.1"
PORT = 8800
_RE_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_RE_ORIGINE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
_NOMS_JOURNAL = ("reseau.sqlite", "journal.sqlite")
_TAILLE_MAX_CORPS = 64 * 1024
_BATTEMENT_S = 15.0  # un commentaire SSE tous les 15 s garde la connexion ouverte


class ErreurHttp(Exception):
    def __init__(self, statut: int, message: str):
        super().__init__(message)
        self.statut = statut
        self.message = message


# ---------------------------------------------------------------------------
# Du journal aux événements du flux
# ---------------------------------------------------------------------------

def _cle_preuve(p: dict[str, Any]) -> tuple:
    return (p["url"], p["extrait"], p.get("debut_s"), p.get("fin_s"))


def _ouvrir_en_lecture(chemin: Path) -> sqlite3.Connection:
    bd = sqlite3.connect(f"{chemin.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    return bd


def lire_evenements(chemin: Path) -> list[dict[str, Any]]:
    """Relit tout le journal et le traduit en événements du flux (voir le docstring du module)."""
    bd = _ouvrir_en_lecture(chemin)
    try:
        lignes = bd.execute(
            "SELECT seq, date, genre, objet_id, version, motif, donnees FROM evenements ORDER BY seq"
        ).fetchall()
        try:
            du_journaliste = {r[0] for r in bd.execute("SELECT question_id FROM questions_journaliste")}
        except sqlite3.OperationalError:
            du_journaliste = set()
    finally:
        bd.close()

    evenements: list[dict[str, Any]] = []
    libelles: dict[str, str] = {}
    roles: dict[str, str] = {}
    liens: set[str] = set()
    preuves: set[tuple] = set()
    questions_ouvertes: dict[str, bool] = {}

    def pousser(horodatage: str, type_: str, donnees: dict[str, Any]) -> None:
        evenements.append({
            "n": len(evenements) + 1, "horodatage": horodatage, "type": type_,
            "donnees": donnees, "accelere": False, "facteur": None,
        })

    for _seq, date, genre, objet_id, version, motif, brut in lignes:
        d = json.loads(brut)
        if genre == "affaire":
            pousser(date, "question", {
                "id": objet_id, "texte": d["question"], "centrale": True, "reformulee": version > 1,
            })
        elif genre == "noeud":
            libelles[d["id"]] = d["libelle"]
            roles[d["id"]] = d["role"]
            pousser(date, "noeud", {**d, "motif": motif})
        elif genre == "lien":
            liens.add(d["id"])
            noms = {"source_libelle": libelles.get(d["source_id"]), "cible_libelle": libelles.get(d["cible_id"])}
            pousser(date, "lien", {**d, **noms, "motif": motif})
            for preuve in d["preuves"]:
                cle = _cle_preuve(preuve)
                if cle not in preuves:
                    preuves.add(cle)
                    pousser(date, "preuve", {"lien_id": d["id"], "preuve": preuve, **noms})
        elif genre == "question":
            questions_ouvertes[objet_id] = d["statut"] == "ouverte"
            pousser(date, "question", {
                **d, "origine": "journaliste" if objet_id in du_journaliste else "agent", "motif": motif,
            })
        else:
            continue
        par_role: dict[str, int] = {}
        for role in roles.values():
            par_role[role] = par_role.get(role, 0) + 1
        pousser(date, "compteur", {
            "pieces": len(libelles), "liens": len(liens), "preuves": len(preuves),
            "zones_d_ombre": sum(questions_ouvertes.values()), "par_role": par_role,
        })
    return evenements


def _horodatage_s(evenement: dict[str, Any]) -> float:
    return datetime.fromisoformat(evenement["horodatage"]).timestamp()


def duree_reelle(evenements: list[dict[str, Any]]) -> float:
    """Temps écoulé entre le premier et le dernier événement du journal, en secondes."""
    if len(evenements) < 2:
        return 0.0
    return _horodatage_s(evenements[-1]) - _horodatage_s(evenements[0])


def facteur_pour_duree(evenements: list[dict[str, Any]], duree_cible: float) -> float:
    """Le facteur qui fait tenir le journal en `duree_cible` secondes, jamais sous 1."""
    return max(1.0, duree_reelle(evenements) / duree_cible)


def _compteurs_finaux(evenements: list[dict[str, Any]]) -> dict[str, Any]:
    for e in reversed(evenements):
        if e["type"] == "compteur":
            return e["donnees"]
    return {"pieces": 0, "liens": 0, "preuves": 0, "zones_d_ombre": 0, "par_role": {}}


# ---------------------------------------------------------------------------
# Importer un export JSON en journal SQLite
# ---------------------------------------------------------------------------

def importer_export(export: dict[str, Any], chemin: Path) -> None:
    """Rejoue un export (`reseau.json`) dans un journal neuf : nœuds, liens, questions.

    Les dates du journal sont celles de l'import : l'export ne garde pas l'heure
    à laquelle chaque pièce a été ajoutée.
    """
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_suffix(".sqlite.tmp")
    temporaire.unlink(missing_ok=True)
    with ReseauEnquete(export["question"], str(temporaire)) as reseau:
        for dossier in export["dossiers"]:
            for p in dossier["pieces"]:
                reseau.ajouter_noeud(Noeud(
                    type=p["type"], libelle=p["libelle"], role=Role(p["role"]),
                    attributs=p["attributs"], id=p["id"],
                    solidite=Solidite.depuis_dict(p["solidite"]) if p["solidite"] else None,
                ))
        for lien in export["liens"]:
            reseau.ajouter_lien(Lien.depuis_dict(lien))
        for q in export["questions_ouvertes"]:
            posee = reseau.poser_question(q["texte"])
            if q["statut"] == "comblee" and q["comblee_par"]:
                reseau.combler_question(posee.id, q["comblee_par"])
    temporaire.replace(chemin)


# ---------------------------------------------------------------------------
# Enquêtes en cours
# ---------------------------------------------------------------------------

Enqueteur = Callable[[str, list[str], Path, Path, str], None]


def enqueteur_par_defaut(question: str, mots: list[str], dossier: Path, journal: Path, affaire_id: str) -> None:
    """Lance `chaine.enqueter` avec un vrai client Oriane.

    Si `enqueter` accepte un paramètre `chemin_journal` (ou `chemin_reseau`,
    `chemin`), le journal s'écrit au fil de la collecte et le flux est vivant.
    Sinon la chaîne construit le réseau en mémoire : on importe `reseau.json`
    à la fin et les événements arrivent d'un bloc.
    """
    from enquete import chaine
    from enquete.sources.oriane import OrianeClient

    parametres = inspect.signature(chaine.enqueter).parameters
    supplement = {}
    for nom in ("chemin_journal", "chemin_reseau", "chemin"):
        if nom in parametres:
            supplement[nom] = str(journal)
            break
    with OrianeClient(dossier_cache="cache/oriane") as client:
        chaine.enqueter(question, mots, client, dossier, affaire_id, **supplement)
    if not supplement and (dossier / "reseau.json").exists():
        importer_export(json.loads((dossier / "reseau.json").read_text(encoding="utf-8")), journal)


@dataclass
class Travail:
    """Une enquête lancée par ce serveur."""

    id: str
    question: str
    etat: str = "en_cours"  # en_cours, terminee, echec
    erreur: str | None = None
    debut: float = field(default_factory=time.time)


def _slug(texte: str) -> str:
    sans_accents = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", sans_accents.lower()).strip("-")[:40] or "enquete"


# ---------------------------------------------------------------------------
# Le serveur
# ---------------------------------------------------------------------------

class Serveur:
    """Ce qui est partagé entre les requêtes : dossier des enquêtes, travaux en cours, index."""

    def __init__(
        self,
        racine: str | Path = "donnees",
        hote: str = HOTE,
        port: int = PORT,
        enqueteur: Enqueteur | None = None,
        sur_question: Callable[[str, str, str], None] | None = None,
        pas_sondage: float = 0.25,
        autoriser_fichiers: bool = True,
    ) -> None:
        self.racine = Path(racine)
        self.enqueteur = enqueteur or enqueteur_par_defaut
        self.sur_question = sur_question  # (enquete_id, question_id, texte) : les agents s'y branchent
        self.pas_sondage = pas_sondage
        self.autoriser_fichiers = autoriser_fichiers
        self.arret = threading.Event()
        self.travaux: dict[str, Travail] = {}
        self._verrou = threading.Lock()
        self._index: dict[str, tuple[int, Index]] = {}
        self._httpd = ThreadingHTTPServer((hote, port), _fabriquer_gestionnaire(self))
        self._httpd.daemon_threads = True
        self._fil: threading.Thread | None = None

    # -- cycle de vie -------------------------------------------------------

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self._httpd.server_address[0]}:{self.port}"

    def demarrer(self) -> "Serveur":
        self._fil = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._fil.start()
        return self

    def arreter(self) -> None:
        self.arret.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._fil:
            self._fil.join(timeout=5)

    def __enter__(self) -> "Serveur":
        return self.demarrer()

    def __exit__(self, *_: object) -> None:
        self.arreter()

    # -- enquêtes -----------------------------------------------------------

    def valider_id(self, identifiant: str | None) -> str:
        if not identifiant or not _RE_ID.match(identifiant):
            raise ErreurHttp(400, "paramètre « enquete » manquant ou invalide")
        return identifiant

    def chemin_journal(self, identifiant: str, creer: bool = True) -> Path:
        """Le journal SQLite de l'enquête ; importe `reseau.json` s'il n'y a que lui."""
        dossier = self.racine / self.valider_id(identifiant)
        for nom in _NOMS_JOURNAL:
            if (dossier / nom).is_file():
                return dossier / nom
        export = dossier / "reseau.json"
        if creer and export.is_file():
            with self._verrou:
                if not (dossier / _NOMS_JOURNAL[0]).is_file():
                    importer_export(json.loads(export.read_text(encoding="utf-8")), dossier / _NOMS_JOURNAL[0])
            return dossier / _NOMS_JOURNAL[0]
        raise ErreurHttp(404, f"enquête inconnue : {identifiant}")

    def en_cours(self, identifiant: str) -> bool:
        travail = self.travaux.get(identifiant)
        return travail is not None and travail.etat == "en_cours"

    def mode(self, identifiant: str) -> str:
        return "direct" if self.en_cours(identifiant) else "demo"

    def ouvrir_reseau(self, identifiant: str) -> ReseauEnquete:
        chemin = self.chemin_journal(identifiant)
        bd = _ouvrir_en_lecture(chemin)
        try:
            ligne = bd.execute(
                "SELECT donnees FROM evenements WHERE genre='affaire' ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        finally:
            bd.close()
        if not ligne:
            raise ErreurHttp(409, "journal sans question centrale")
        return ReseauEnquete(json.loads(ligne[0])["question"], str(chemin))

    def exporter(self, identifiant: str) -> dict[str, Any]:
        chemin = self.chemin_journal(identifiant)
        with self.ouvrir_reseau(identifiant) as reseau:
            export = reseau.exporter_pour_interface()
        origines = self._questions_du_journaliste(chemin)
        for q in export["questions_ouvertes"]:
            q["origine"] = "journaliste" if q["id"] in origines else "agent"
        return {**export, "mode": self.mode(identifiant), "enquete": identifiant}

    @staticmethod
    def _questions_du_journaliste(chemin: Path) -> set[str]:
        bd = _ouvrir_en_lecture(chemin)
        try:
            return {r[0] for r in bd.execute("SELECT question_id FROM questions_journaliste")}
        except sqlite3.OperationalError:
            return set()
        finally:
            bd.close()

    def lister(self) -> list[dict[str, Any]]:
        resultat = []
        if self.racine.is_dir():
            for dossier in sorted(p for p in self.racine.iterdir() if p.is_dir() and _RE_ID.match(p.name)):
                if any((dossier / n).is_file() for n in (*_NOMS_JOURNAL, "reseau.json")):
                    travail = self.travaux.get(dossier.name)
                    resultat.append({
                        "id": dossier.name, "mode": self.mode(dossier.name),
                        "etat": travail.etat if travail else "enregistree",
                    })
        return resultat

    def lancer(self, question: str, graine: Any = None) -> str:
        question = (question or "").strip()
        if not question:
            raise ErreurHttp(400, "« question » est obligatoire")
        if isinstance(graine, str):
            mots = [m.strip() for m in re.split(r"[,;]", graine) if m.strip()]
        elif isinstance(graine, list):
            mots = [str(m).strip() for m in graine if str(m).strip()]
        else:
            mots = []
        mots = mots or [question]
        identifiant = f"{_slug(question)}-{uuid.uuid4().hex[:6]}"
        dossier = self.racine / identifiant
        dossier.mkdir(parents=True, exist_ok=True)
        journal = dossier / _NOMS_JOURNAL[0]
        # La question centrale est écrite tout de suite : le flux a un premier événement avant la collecte.
        ReseauEnquete(question, str(journal)).fermer()
        travail = Travail(identifiant, question)
        self.travaux[identifiant] = travail

        def courir() -> None:
            try:
                self.enqueteur(question, mots, dossier, journal, identifiant)
                travail.etat = "terminee"
            except Exception as erreur:  # le flux doit pouvoir dire que ça s'est mal passé
                travail.erreur = f"{type(erreur).__name__} : {erreur}"
                travail.etat = "echec"

        threading.Thread(target=courir, name=f"enquete-{identifiant}", daemon=True).start()
        return identifiant

    def poser_question(self, identifiant: str, texte: str) -> str:
        texte = (texte or "").strip()
        if not texte:
            raise ErreurHttp(400, "« texte » est obligatoire")
        chemin = self.chemin_journal(identifiant)
        with self.ouvrir_reseau(identifiant) as reseau:
            question = reseau.poser_question(texte)
        bd = sqlite3.connect(str(chemin), timeout=10)
        try:
            with bd:
                bd.execute("CREATE TABLE IF NOT EXISTS questions_journaliste "
                           "(question_id TEXT PRIMARY KEY, date TEXT NOT NULL)")
                bd.execute("INSERT INTO questions_journaliste VALUES (?, ?)",
                           (question.id, datetime.now().astimezone().isoformat()))
        finally:
            bd.close()
        if self.sur_question:
            self.sur_question(identifiant, question.id, texte)
        return question.id

    # -- recherche ----------------------------------------------------------

    def chercher(self, identifiant: str, q: str, limite: int) -> list[dict[str, Any]]:
        chemin = self.chemin_journal(identifiant)
        bd = _ouvrir_en_lecture(chemin)
        try:
            (version,) = bd.execute("SELECT COALESCE(MAX(seq), 0) FROM evenements").fetchone()
        finally:
            bd.close()
        with self._verrou:
            en_cache = self._index.get(identifiant)
        if en_cache is None or en_cache[0] != version:
            with self.ouvrir_reseau(identifiant) as reseau:
                export = reseau.exporter_pour_interface()
            index = Index(documents_depuis_export(export))
            with self._verrou:
                self._index[identifiant] = (version, index)
        else:
            index = en_cache[1]
        return index.chercher(q, limite)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _fabriquer_gestionnaire(serveur: Serveur) -> type[BaseHTTPRequestHandler]:
    class Gestionnaire(BaseHTTPRequestHandler):
        server_version = "Enquete/0.1"

        # -- sorties --------------------------------------------------------

        def log_message(self, *_: object) -> None:  # silence : la console sert à la démo
            pass

        def _origine_permise(self) -> str | None:
            origine = self.headers.get("Origin")
            if origine is None:
                return None
            if _RE_ORIGINE.match(origine) or (serveur.autoriser_fichiers and origine == "null"):
                return origine
            return ""  # présente mais refusée

        def _entetes_cors(self) -> None:
            origine = self._origine_permise()
            if origine:
                self.send_header("Access-Control-Allow-Origin", origine)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Last-Event-ID")

        def _json(self, statut: int, objet: Any) -> None:
            corps = json.dumps(objet, ensure_ascii=False).encode("utf-8")
            self.send_response(statut)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Cache-Control", "no-store")
            self._entetes_cors()
            self.end_headers()
            self.wfile.write(corps)

        def _ouvrir_flux(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self._entetes_cors()
            self.end_headers()
            self.wfile.flush()

        def _envoyer(self, evenement: dict[str, Any]) -> None:
            donnees = json.dumps(evenement, ensure_ascii=False)
            self.wfile.write(f"id: {evenement['n']}\ndata: {donnees}\n\n".encode("utf-8"))
            self.wfile.flush()

        # -- entrées --------------------------------------------------------

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._entetes_cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            self._traiter("GET")

        def do_POST(self) -> None:
            self._traiter("POST")

        def _traiter(self, methode: str) -> None:
            try:
                if self._origine_permise() == "":
                    raise ErreurHttp(403, "origine non autorisée")
                url = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                route = (methode, url.path.rstrip("/"))
                if route == ("GET", "/api/sante"):
                    self._json(200, {"ok": True})
                elif route == ("GET", "/api/enquetes"):
                    self._json(200, {"enquetes": serveur.lister()})
                elif route == ("GET", "/api/export"):
                    self._json(200, serveur.exporter(serveur.valider_id(params.get("enquete"))))
                elif route == ("GET", "/api/recherche"):
                    identifiant = serveur.valider_id(params.get("enquete"))
                    limite = self._entier(params.get("limite"), 50, "limite", 1, 500)
                    self._json(200, {"resultats": serveur.chercher(identifiant, params.get("q", ""), limite)})
                elif route == ("GET", "/api/evenements"):
                    self._flux_direct(serveur.valider_id(params.get("enquete")), self._depuis(params))
                elif route == ("GET", "/api/rejouer"):
                    self._flux_rejeu(serveur.valider_id(params.get("enquete")), params)
                elif route == ("POST", "/api/enquetes"):
                    corps = self._corps()
                    self._json(201, {"id": serveur.lancer(corps.get("question"), corps.get("graine"))})
                elif route == ("POST", "/api/questions"):
                    corps = self._corps()
                    identifiant = serveur.valider_id(corps.get("enquete"))
                    self._json(201, {"id": serveur.poser_question(identifiant, corps.get("texte"))})
                else:
                    raise ErreurHttp(404, f"route inconnue : {methode} {url.path}")
            except ErreurHttp as e:
                self._repondre_erreur(e.statut, e.message)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as e:  # une erreur de route ne doit pas tuer le serveur
                self._repondre_erreur(500, f"{type(e).__name__} : {e}")

        def _repondre_erreur(self, statut: int, message: str) -> None:
            try:
                self._json(statut, {"erreur": message})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

        @staticmethod
        def _entier(brut: str | None, defaut: int, nom: str, mini: int, maxi: int) -> int:
            if brut is None:
                return defaut
            try:
                valeur = int(brut)
            except ValueError:
                raise ErreurHttp(400, f"« {nom} » doit être un entier") from None
            if not mini <= valeur <= maxi:
                raise ErreurHttp(400, f"« {nom} » doit être entre {mini} et {maxi}")
            return valeur

        def _depuis(self, params: dict[str, str]) -> int:
            brut = params.get("depuis") or self.headers.get("Last-Event-ID")
            return self._entier(brut, 0, "depuis", 0, 10**9)

        def _corps(self) -> dict[str, Any]:
            # Seul le JSON déclaré passe : il force la pré-vérification CORS, et bloque
            # les formulaires envoyés depuis une page étrangère.
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                raise ErreurHttp(415, "Content-Type: application/json attendu")
            taille = int(self.headers.get("Content-Length") or 0)
            if taille > _TAILLE_MAX_CORPS:
                raise ErreurHttp(413, "corps trop gros")
            try:
                corps = json.loads(self.rfile.read(taille) or b"{}")
            except json.JSONDecodeError:
                raise ErreurHttp(400, "JSON invalide") from None
            if not isinstance(corps, dict):
                raise ErreurHttp(400, "un objet JSON est attendu")
            return corps

        # -- flux -----------------------------------------------------------

        def _flux_direct(self, identifiant: str, depuis: int) -> None:
            chemin = serveur.chemin_journal(identifiant)
            self._ouvrir_flux()
            dernier = depuis
            dernier_envoi = time.monotonic()
            try:
                while not serveur.arret.is_set():
                    # On lit l'état avant le journal : si l'enquête vient de finir, rien ne manque.
                    tournait = serveur.en_cours(identifiant)
                    try:
                        evenements = lire_evenements(chemin)
                    except sqlite3.OperationalError:  # journal verrouillé par l'écriture en cours
                        serveur.arret.wait(serveur.pas_sondage)
                        continue
                    nouveaux = [e for e in evenements if e["n"] > dernier]
                    for e in nouveaux:
                        self._envoyer(e)
                        dernier = e["n"]
                        dernier_envoi = time.monotonic()
                    if not tournait:
                        travail = serveur.travaux.get(identifiant)
                        self._envoyer(self._fin(evenements, dernier + 1, travail))
                        return
                    if time.monotonic() - dernier_envoi > _BATTEMENT_S:
                        self.wfile.write(b": battement\n\n")
                        self.wfile.flush()
                        dernier_envoi = time.monotonic()
                    serveur.arret.wait(serveur.pas_sondage)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

        def _flux_rejeu(self, identifiant: str, params: dict[str, str]) -> None:
            chemin = serveur.chemin_journal(identifiant)
            evenements = lire_evenements(chemin)
            reelle = duree_reelle(evenements)
            if "facteur" in params:
                try:
                    facteur = float(params["facteur"])
                except ValueError:
                    raise ErreurHttp(400, "« facteur » doit être un nombre") from None
                if not facteur > 0:
                    raise ErreurHttp(400, "« facteur » doit être positif")
            elif "duree_cible" in params:
                try:
                    cible = float(params["duree_cible"])
                except ValueError:
                    raise ErreurHttp(400, "« duree_cible » doit être un nombre") from None
                if not cible > 0:
                    raise ErreurHttp(400, "« duree_cible » doit être positive")
                facteur = facteur_pour_duree(evenements, cible)
            else:
                facteur = 1.0
            depuis = self._depuis(params)
            restants = [e for e in evenements if e["n"] > depuis]
            self._ouvrir_flux()
            if restants:
                base = _horodatage_s(restants[0])
                depart = time.monotonic()
                try:
                    for e in restants:
                        # Échéance absolue : les retards de l'envoi ne s'additionnent pas.
                        attente = (_horodatage_s(e) - base) / facteur - (time.monotonic() - depart)
                        if attente > 0 and serveur.arret.wait(attente):
                            return
                        self._envoyer({**e, "accelere": facteur > 1, "facteur": facteur})
                    ecoule = time.monotonic() - depart
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return
            else:
                ecoule = 0.0
            fin = self._fin(evenements, (evenements[-1]["n"] if evenements else 0) + 1, None)
            fin.update(accelere=facteur > 1, facteur=facteur)
            fin["donnees"].update(duree_reelle_s=round(reelle, 3), duree_rejeu_s=round(ecoule, 3), facteur=facteur)
            try:
                self._envoyer(fin)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

        @staticmethod
        def _fin(evenements: list[dict[str, Any]], n: int, travail: Travail | None) -> dict[str, Any]:
            horodatage = evenements[-1]["horodatage"] if evenements else datetime.now().astimezone().isoformat()
            donnees = {
                "ok": travail is None or travail.etat != "echec",
                "evenements": len(evenements),
                "compteurs": _compteurs_finaux(evenements),
            }
            if travail is not None and travail.erreur:
                donnees["erreur"] = travail.erreur
            return {"n": n, "horodatage": horodatage, "type": "fin", "donnees": donnees,
                    "accelere": False, "facteur": None}

    return Gestionnaire


def main(argv: list[str] | None = None) -> int:
    import argparse

    analyseur = argparse.ArgumentParser(prog="python -m enquete.serveur", description=__doc__.split("\n")[0])
    analyseur.add_argument("--racine", default="donnees", help="dossier des enquêtes (un sous-dossier par enquête)")
    analyseur.add_argument("--port", type=int, default=PORT)
    args = analyseur.parse_args(argv)
    with Serveur(args.racine, port=args.port) as serveur:
        print(f"Serveur sur {serveur.url} · enquêtes dans {serveur.racine.resolve()}")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
