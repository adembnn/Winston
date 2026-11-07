from datetime import UTC, datetime

import pytest

from enquete.temps import (
    MARGE_INSTAGRAM_PHOTO_S,
    MARGE_INSTAGRAM_VIDEO_S,
    MARGE_TIKTOK_S,
    DateNonDecodable,
    date_depuis_identifiant,
    identifiant_vers_shortcode,
    shortcode_vers_identifiant,
)


def test_tiktok_exemple_bill_nye():
    # Source : https://hindsig.ht/blog/tinkering-with-tiktok-timestamps/ (aussi
    # https://maxintel.org/id-decoder.html) : l'identifiant donne 02:06:13 UTC,
    # le champ createTime de la page donne 02:06:17 (4 s d'écart, sous la marge).
    resultat = date_depuis_identifiant("tiktok", "6854717870488702213")
    assert resultat.date == datetime(2020, 7, 29, 2, 6, 13, tzinfo=UTC)
    assert resultat.marge_secondes == MARGE_TIKTOK_S
    create_time_page = datetime.fromtimestamp(1595988377, tz=UTC)
    assert abs((create_time_page - resultat.date).total_seconds()) <= resultat.marge_secondes
    assert "32 bits" in resultat.methode


def test_tiktok_exemple_article_arxiv():
    # Source : https://arxiv.org/html/2504.13279v1 (figure 10) : les 32 premiers
    # bits valent 1709316007, soit le 1er mars 2024 à 18:00:07 UTC (l'article
    # affiche 19:00:07, heure d'Europe centrale).
    resultat = date_depuis_identifiant("TikTok", "7341456348594310401")
    assert resultat.date == datetime(2024, 3, 1, 18, 0, 7, tzinfo=UTC)
    assert resultat.date.utcoffset().total_seconds() == 0


def test_tiktok_intervalle():
    debut, fin = date_depuis_identifiant("tiktok", "6854717870488702213").intervalle
    assert (fin - debut).total_seconds() == 2 * MARGE_TIKTOK_S


@pytest.mark.parametrize("mauvais", ["", "abc", "12.5", "-6854717870488702213", str(2**64)])
def test_tiktok_identifiant_mal_forme(mauvais):
    with pytest.raises(ValueError):
        date_depuis_identifiant("tiktok", mauvais)


def test_tiktok_identifiant_trop_petit_donne_une_date_impossible():
    # 12345 >> 32 = 0 : 1970, avant la naissance de TikTok.
    with pytest.raises(DateNonDecodable, match="hors de la période"):
        date_depuis_identifiant("tiktok", "12345")


def test_tiktok_identifiant_du_futur():
    with pytest.raises(DateNonDecodable):
        date_depuis_identifiant("tiktok", str((4_000_000_000) << 32))


def test_instagram_exemple_etude_old_dominion():
    # Source : https://ws-dl.blogspot.com/2021/02/2021-02-20-creation-time-and-published.html
    # La publication CGGOHDXhhK- a pour identifiant 2415680307434230462 et pour
    # heure de publication 2020-10-08T21:12:34Z (taken_at_timestamp 1602191554).
    assert shortcode_vers_identifiant("CGGOHDXhhK-") == 2415680307434230462
    publiee = datetime.fromtimestamp(1602191554, tz=UTC)
    resultat = date_depuis_identifiant("instagram", "CGGOHDXhhK-", type_publication="post")
    assert resultat.date.replace(microsecond=0) == publiee
    assert resultat.marge_secondes == MARGE_INSTAGRAM_PHOTO_S
    assert abs((resultat.date - publiee).total_seconds()) <= resultat.marge_secondes


def test_instagram_identifiant_numerique_equivaut_au_shortcode():
    par_code = date_depuis_identifiant("instagram", "CGGOHDXhhK-")
    par_numero = date_depuis_identifiant("instagram", "2415680307434230462")
    avec_suffixe = date_depuis_identifiant("instagram", "2415680307434230462_123456")
    assert par_code.date == par_numero.date == avec_suffixe.date


def test_instagram_marge_prudente_sans_type_ou_pour_un_reel():
    assert date_depuis_identifiant("instagram", "CGGOHDXhhK-").marge_secondes == MARGE_INSTAGRAM_VIDEO_S
    reel = date_depuis_identifiant("instagram", "CGGOHDXhhK-", type_publication="reel")
    assert reel.marge_secondes == MARGE_INSTAGRAM_VIDEO_S


def test_instagram_code_prive_trop_long():
    with pytest.raises(DateNonDecodable, match="privée"):
        date_depuis_identifiant("instagram", "A" * 28)


def test_instagram_caractere_invalide():
    with pytest.raises(ValueError, match="invalide"):
        date_depuis_identifiant("instagram", "CGGOHD+hhK-")


def test_instagram_identifiant_du_futur():
    with pytest.raises(DateNonDecodable, match="futur"):
        date_depuis_identifiant("instagram", str((2**41 - 1) << 23))


# Source des paires (shortcode, identifiant) : https://sociavault.com/blog/instagram-shortcode-to-media-id
# (convertis dans les deux sens, sur quatre publications réelles).
PAIRES_SHORTCODE = [
    ("DH3tWudxIKB", 3600545900919030401),
    ("DEiyb48AeB9", 3540614075954356349),
    ("DF5s0duxDts", 3565077699422862188),
    ("Dd1gQBsCWE8", 3996242083173720380),
]


@pytest.mark.parametrize(("shortcode", "numero"), PAIRES_SHORTCODE)
def test_conversion_shortcode_dans_les_deux_sens(shortcode, numero):
    assert shortcode_vers_identifiant(shortcode) == numero
    assert identifiant_vers_shortcode(numero) == shortcode


@pytest.mark.parametrize("mauvais", [0, -5, True, "12"])
def test_identifiant_vers_shortcode_refuse_les_entrees_invalides(mauvais):
    with pytest.raises(ValueError):
        identifiant_vers_shortcode(mauvais)


def test_shortcode_vide():
    with pytest.raises(ValueError):
        shortcode_vers_identifiant("")


def test_youtube_aucune_date_dans_l_identifiant():
    # Source : https://maxintel.org/id-decoder.html : un identifiant YouTube ne
    # contient pas de date, elle vient de l'API (snippet.publishedAt).
    with pytest.raises(DateNonDecodable, match="Date non décodable pour cette plateforme"):
        date_depuis_identifiant("youtube", "dQw4w9WgXcQ")


def test_plateforme_inconnue():
    with pytest.raises(DateNonDecodable, match="Date non décodable pour cette plateforme"):
        date_depuis_identifiant("facebook", "123")
