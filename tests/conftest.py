"""Garde-fous communs aux tests."""

import pytest

from enquete import chaine


def _module_absent():
    raise ImportError("enquete.images n'est jamais chargé par les tests de la chaîne")


@pytest.fixture(autouse=True)
def pas_d_images_reelles(monkeypatch):
    """`chaine.enqueter` illustre l'export avec `enquete.images` s'il est importable. En test, ce module irait
    télécharger des images et interroger des banques d'images : on le déclare absent. Un test qui veut le
    tester injecte un faux module (voir test_integration.py)."""
    monkeypatch.setattr(chaine, "_importer_images", _module_absent)
