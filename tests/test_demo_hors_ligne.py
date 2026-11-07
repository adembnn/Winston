"""Tests de la démo de secours : rejeu d'un export, réseau coupé, signalé « accéléré »."""

import importlib.util
import json
import os
import socket
import urllib.request
from pathlib import Path

import pytest

from enquete.reseau import FamilleLien, Lien, Noeud, Preuve, ReseauEnquete, Role, TypeNoeud
from enquete.serveur import Serveur

_spec = importlib.util.spec_from_file_location(
    "demo_hors_ligne", Path(__file__).resolve().parent.parent / "scripts" / "demo_hors_ligne.py")
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)

URL = "https://www.tiktok.com/@exemple/video/1"


@pytest.fixture
def source(tmp_path):
    """Une affaire factice avec son reseau.json, comme la sortie de `enqueter`."""
    dossier = tmp_path / "source" / "affaire-test"
    dossier.mkdir(parents=True)
    from datetime import datetime, timezone
    with ReseauEnquete("Question de test ?", str(tmp_path / "tmp.sqlite")) as r:
        a = r.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@a", Role.ORIGINE, id="a"))
        b = r.ajouter_noeud(Noeud(TypeNoeud.COMPTE, "@b", Role.AMPLIFICATEUR, id="b"))
        r.ajouter_lien(Lien(type_lien="reprend", famille=FamilleLien.CONTENU_DIT, source_id=a.id, cible_id=b.id,
                            preuves=(Preuve(URL, "même phrase", "Oriane", datetime(2026, 11, 7, tzinfo=timezone.utc)),),
                            explication="b reprend a", methode="m", agent="t"))
        r.poser_question("Qui a publié en premier ?")
        (dossier / "reseau.json").write_text(json.dumps(r.exporter_pour_interface(), ensure_ascii=False), encoding="utf-8")
    return dossier


def test_preparer_copie_sans_toucher_la_source(source, tmp_path):
    avant = (source / "reseau.json").read_bytes()
    racine = tmp_path / "secours"
    assert demo.preparer(source, racine) == "affaire-test"
    assert (racine / "affaire-test" / "reseau.json").read_bytes() == avant
    assert (source / "reseau.json").read_bytes() == avant
    manifeste = json.loads((racine / "affaire-test" / "manifeste.json").read_text(encoding="utf-8"))
    assert manifeste["rythme_reconstitue"] is True


def test_preparer_refuse_un_dossier_sans_export(tmp_path):
    with pytest.raises(SystemExit):
        demo.preparer(tmp_path, tmp_path / "secours")


def test_rejeu_hors_ligne_signale_accelere(source, tmp_path):
    racine = tmp_path / "secours"
    demo.preparer(source, racine)
    demo.construire_journal(racine, "affaire-test")
    with demo.ReseauCoupe() as coupe, Serveur(racine, port=0, enqueteur=demo._enqueteur_interdit) as s:
        evenements, _ = demo._lire_flux(f"{s.url}/api/rejouer?enquete=affaire-test&facteur=50")
    assert evenements[-1]["type"] == "fin" and evenements[-1]["donnees"]["ok"] is True
    corps = [e for e in evenements if e["type"] != "fin"]
    assert corps and all(e["accelere"] and e["facteur"] == 50 for e in corps)
    assert {"question", "noeud", "lien", "preuve", "compteur"} <= {e["type"] for e in corps}
    assert coupe.refus == []


def test_reseau_coupe_refuse_et_compte_les_sorties(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://valeur-d-origine")
    with demo.ReseauCoupe() as coupe:
        assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:9"
        with pytest.raises(OSError, match="réseau coupé"):
            socket.create_connection(("8.8.8.8", 53), timeout=1)
        with pytest.raises(OSError, match="réseau coupé"):
            socket.socket().connect(("1.1.1.1", 80))
    assert len(coupe.refus) == 2
    assert os.environ["HTTP_PROXY"] == "http://valeur-d-origine"  # remis en place


def test_verifier_passe_sur_un_export_valide(source, tmp_path, capsys):
    racine = tmp_path / "secours"
    demo.preparer(source, racine)
    assert demo.verifier(racine, 0.5) == 0
    assert "connexions externes refusées 0" in capsys.readouterr().out


def test_verifier_echoue_sans_rien_a_rejouer(tmp_path):
    assert demo.verifier(tmp_path / "vide", 2.0) == 1
