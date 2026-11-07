"""Démo de secours : rejoue un vrai passage enregistré, sans aucun appel réseau.

Image simple : le groupe électrogène de la salle. Si le wifi tombe, on rallume le
dernier passage réel (l'export `reseau.json` du premier vrai passage) et on le
rejoue plus vite, avec la mention « accéléré » dans chaque événement.

Gestes
------
  python scripts/demo_hors_ligne.py preparer --source <dossier-affaire> [--racine donnees/demo-secours]
      copie `reseau.json` (lecture seule à la source) dans la racine de secours.
  python scripts/demo_hors_ligne.py servir [--racine ...] [--port 8800]
      sert la racine avec enquete.serveur ; l'interface s'ouvre avec `?enquete=<id>`
      et lit `/api/rejouer?enquete=<id>&duree_cible=<s>`.
  python scripts/demo_hors_ligne.py verifier [--racine ...] [--duree-cible 25]
      répète la démo réseau coupé et affiche les mesures (durée, événements, appels
      sortants refusés) ; code de sortie 1 si l'un des contrôles échoue.

Aucun appel externe : les variables de proxy sont invalidées et toute connexion
vers une adresse autre que la boucle locale est refusée (et comptée) dans le processus.
Le rythme du rejeu est reconstitué : l'export ne garde pas l'heure de chaque pièce,
les lignes du journal de la COPIE sont espacées d'un écart fixe (noté au manifeste).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

RACINE_PAR_DEFAUT = Path("donnees") / "demo-secours"
ECART_RECONSTITUE_S = 0.5
DEBUT_RECONSTITUE = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
VARIABLES_PROXY = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


class ReseauCoupe:
    """Refuse (et compte) toute connexion hors boucle locale, tant que le contexte est ouvert."""

    def __init__(self) -> None:
        self.refus: list[str] = []
        self._connect = socket.socket.connect
        self._create = socket.create_connection
        self._env: dict[str, str | None] = {}

    @staticmethod
    def _local(adresse: object) -> bool:
        hote = adresse[0] if isinstance(adresse, tuple) else str(adresse)
        try:
            return ipaddress.ip_address(hote).is_loopback
        except ValueError:
            return hote == "localhost"

    def __enter__(self) -> "ReseauCoupe":
        for nom in VARIABLES_PROXY + ("NO_PROXY", "no_proxy"):
            self._env[nom] = os.environ.get(nom)
        for nom in VARIABLES_PROXY:
            os.environ[nom] = "http://127.0.0.1:9"  # invalide : rien n'écoute sur le port 9
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = ""
        garde = self

        def connect(sock: socket.socket, adresse: object) -> object:
            if not garde._local(adresse):
                garde.refus.append(str(adresse))
                raise OSError(f"réseau coupé : connexion refusée vers {adresse}")
            return garde._connect(sock, adresse)  # type: ignore[arg-type]

        def create(adresse: object, *a: object, **k: object) -> object:
            if not garde._local(adresse):
                garde.refus.append(str(adresse))
                raise OSError(f"réseau coupé : connexion refusée vers {adresse}")
            return garde._create(adresse, *a, **k)  # type: ignore[arg-type]

        socket.socket.connect = connect  # type: ignore[method-assign]
        socket.create_connection = create  # type: ignore[assignment]
        return self

    def __exit__(self, *_: object) -> None:
        socket.socket.connect = self._connect  # type: ignore[method-assign]
        socket.create_connection = self._create  # type: ignore[assignment]
        for nom, valeur in self._env.items():
            if valeur is None:
                os.environ.pop(nom, None)
            else:
                os.environ[nom] = valeur


def preparer(source: Path, racine: Path) -> str:
    """Copie l'export du passage réel dans `<racine>/<id>/reseau.json` ; rend l'id."""
    export = source / "reseau.json"
    if not export.is_file():
        raise SystemExit(f"pas de reseau.json dans {source}")
    json.loads(export.read_text(encoding="utf-8"))  # refuse un fichier abîmé avant de copier
    identifiant = source.name
    cible = racine / identifiant
    cible.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(export, cible / "reseau.json")
    (cible / "journal.sqlite").unlink(missing_ok=True)
    (cible / "reseau.sqlite").unlink(missing_ok=True)
    manifeste = {
        "origine": str(source),
        "copie_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rythme_reconstitue": True,
        "ecart_s": ECART_RECONSTITUE_S,
        "mention": "rejeu accéléré d'un vrai passage enregistré ; le rythme n'est pas celui de la collecte",
    }
    (cible / "manifeste.json").write_text(json.dumps(manifeste, ensure_ascii=False, indent=2), encoding="utf-8")
    return identifiant


def construire_journal(racine: Path, identifiant: str) -> Path:
    """Importe l'export, puis espace les lignes du journal de la copie (rythme reconstitué)."""
    from enquete.serveur import importer_export

    dossier = racine / identifiant
    journal = dossier / "reseau.sqlite"
    if journal.is_file():
        return journal
    importer_export(json.loads((dossier / "reseau.json").read_text(encoding="utf-8")), journal)
    bd = sqlite3.connect(journal)
    try:
        for garde in ("evenements_pas_de_modification", "evenements_pas_d_effacement"):
            bd.execute(f"DROP TRIGGER IF EXISTS {garde}")
        for (seq,) in bd.execute("SELECT seq FROM evenements").fetchall():
            date = (DEBUT_RECONSTITUE + timedelta(seconds=seq * ECART_RECONSTITUE_S)).isoformat()
            bd.execute("UPDATE evenements SET date = ? WHERE seq = ?", (date, seq))
        bd.commit()
    finally:
        bd.close()
    return journal


def identifiants(racine: Path) -> list[str]:
    if not racine.is_dir():
        return []
    return sorted(d.name for d in racine.iterdir() if (d / "reseau.json").is_file())


def _enqueteur_interdit(*_: object) -> None:
    raise RuntimeError("démo hors ligne : aucune enquête ne se lance, on rejoue seulement")


def servir(racine: Path, port: int) -> int:
    from enquete.serveur import Serveur

    ids = identifiants(racine)
    if not ids:
        raise SystemExit(f"rien à servir dans {racine} : lancer d'abord « preparer »")
    for i in ids:
        construire_journal(racine, i)
    with ReseauCoupe() as coupe, Serveur(racine, port=port, enqueteur=_enqueteur_interdit) as s:
        print(f"Démo de secours sur {s.url} (réseau coupé, rejeu accéléré)")
        for i in ids:
            print(f"  {s.url}/api/rejouer?enquete={i}&duree_cible=25")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        print(f"connexions externes refusées : {len(coupe.refus)}")
    return 0


def _ouvreur_local() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))  # boucle locale : jamais par le proxy


def _lire_flux(url: str) -> tuple[list[dict], float]:
    t0 = time.monotonic()
    evenements: list[dict] = []
    with _ouvreur_local().open(url, timeout=120) as r:
        for ligne in r:
            ligne = ligne.decode("utf-8")
            if ligne.startswith("data: "):
                evenements.append(json.loads(ligne[6:]))
    return evenements, time.monotonic() - t0


def verifier(racine: Path, duree_cible: float) -> int:
    from enquete.serveur import Serveur

    ids = identifiants(racine)
    if not ids:
        print(f"ECHEC : rien dans {racine}")
        return 1
    echecs = 0
    for identifiant in ids:
        construire_journal(racine, identifiant)
        with ReseauCoupe() as coupe, Serveur(racine, port=0, enqueteur=_enqueteur_interdit) as s:
            export = json.loads(_ouvreur_local().open(f"{s.url}/api/export?enquete={identifiant}", timeout=30).read())
            evenements, duree = _lire_flux(f"{s.url}/api/rejouer?enquete={identifiant}&duree_cible={duree_cible}")
        types = [e["type"] for e in evenements]
        fin = evenements[-1] if evenements else {}
        corps = [e for e in evenements if e["type"] != "fin"]
        controles = {
            "mode demo": export.get("mode") == "demo",
            "flux terminé par fin": types[-1:] == ["fin"] and fin.get("donnees", {}).get("ok") is True,
            "événements signalés accélérés": bool(corps) and all(e["accelere"] for e in corps),
            "aucune connexion externe": not coupe.refus,
            "durée proche de la cible": abs(duree - duree_cible) <= max(3.0, 0.2 * duree_cible),
        }
        print(f"{identifiant} : {len(evenements)} événements, durée {duree:.1f} s (cible {duree_cible} s), "
              f"facteur {fin.get('facteur')}, pièces {export.get('compteurs', {}).get('pieces')}, "
              f"connexions externes refusées {len(coupe.refus)}")
        for nom, ok in controles.items():
            print(f"  {'OK' if ok else 'ECHEC'} {nom}")
            echecs += not ok
    return 1 if echecs else 0


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    a = argparse.ArgumentParser(prog="demo_hors_ligne", description=__doc__.split("\n")[0])
    sous = a.add_subparsers(dest="commande", required=True)
    p = sous.add_parser("preparer")
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--racine", type=Path, default=RACINE_PAR_DEFAUT)
    s = sous.add_parser("servir")
    s.add_argument("--racine", type=Path, default=RACINE_PAR_DEFAUT)
    s.add_argument("--port", type=int, default=8800)
    v = sous.add_parser("verifier")
    v.add_argument("--racine", type=Path, default=RACINE_PAR_DEFAUT)
    v.add_argument("--duree-cible", type=float, default=25.0)
    args = a.parse_args(argv)
    if args.commande == "preparer":
        print(f"copié : {preparer(args.source, args.racine)} -> {args.racine}")
        return 0
    if args.commande == "servir":
        return servir(args.racine, args.port)
    return verifier(args.racine, args.duree_cible)


if __name__ == "__main__":
    raise SystemExit(main())
