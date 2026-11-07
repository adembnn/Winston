import pytest

from enquete.liens import url_origine


def test_tiktok_video():
    # Forme d'URL citée par https://hindsig.ht/blog/tinkering-with-tiktok-timestamps/
    assert (
        url_origine("tiktok", "6854717870488702213", pseudo="billnye")
        == "https://www.tiktok.com/@billnye/video/6854717870488702213"
    )


def test_tiktok_photo():
    assert (
        url_origine("tiktok", "6854717870488702213", pseudo="bill.nye_1", type_publication="photo")
        == "https://www.tiktok.com/@bill.nye_1/photo/6854717870488702213"
    )


def test_tiktok_ignore_la_seconde():
    # TikTok ne prend pas de position de départ dans l'URL d'une vidéo.
    assert "t=" not in url_origine("tiktok", "6854717870488702213", pseudo="billnye", seconde=12)


@pytest.mark.parametrize(
    ("pseudo", "identifiant"),
    [
        (None, "6854717870488702213"),
        ("@billnye", "6854717870488702213"),
        ("bill nye", "6854717870488702213"),
        ("", "6854717870488702213"),
        ("a" * 25, "6854717870488702213"),
        ("billnye", "abc"),
        ("billnye", ""),
        ("billnye", "123/../x"),
    ],
)
def test_tiktok_entrees_invalides(pseudo, identifiant):
    with pytest.raises(ValueError):
        url_origine("tiktok", identifiant, pseudo=pseudo)


def test_instagram_post_par_defaut():
    # Shortcode CGGOHDXhhK- : https://ws-dl.blogspot.com/2021/02/2021-02-20-creation-time-and-published.html
    assert url_origine("instagram", "CGGOHDXhhK-") == "https://www.instagram.com/p/CGGOHDXhhK-/"


def test_instagram_reel():
    assert (
        url_origine("instagram", "DH3tWudxIKB", type_publication="reel")
        == "https://www.instagram.com/reel/DH3tWudxIKB/"
    )


def test_instagram_identifiant_numerique_donne_le_shortcode():
    # Paire de https://sociavault.com/blog/instagram-shortcode-to-media-id
    assert (
        url_origine("instagram", "3600545900919030401")
        == "https://www.instagram.com/p/DH3tWudxIKB/"
    )


def test_instagram_ignore_la_seconde_et_accepte_un_pseudo():
    url = url_origine("instagram", "DH3tWudxIKB", pseudo="nasa", seconde=30)
    assert url == "https://www.instagram.com/p/DH3tWudxIKB/"


def test_instagram_code_prive_refuse():
    with pytest.raises(ValueError):
        url_origine("instagram", "A" * 28)


def test_youtube_video():
    # Identifiant public de test : https://www.youtube.com/watch?v=dQw4w9WgXcQ
    assert (
        url_origine("youtube", "dQw4w9WgXcQ")
        == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )


def test_youtube_video_avec_seconde():
    assert (
        url_origine("youtube", "dQw4w9WgXcQ", seconde=43)
        == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=43s"
    )


def test_youtube_seconde_zero_est_ajoutee():
    assert url_origine("youtube", "dQw4w9WgXcQ", seconde=0).endswith("&t=0s")


def test_youtube_short():
    assert (
        url_origine("youtube", "dQw4w9WgXcQ", type_publication="short")
        == "https://www.youtube.com/shorts/dQw4w9WgXcQ"
    )


def test_youtube_short_avec_seconde_passe_par_le_lecteur_classique():
    assert (
        url_origine("youtube", "dQw4w9WgXcQ", type_publication="short", seconde=5)
        == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5s"
    )


@pytest.mark.parametrize("seconde", [-1, 1.5, "10", True])
def test_seconde_invalide(seconde):
    with pytest.raises(ValueError, match="seconde"):
        url_origine("youtube", "dQw4w9WgXcQ", seconde=seconde)


@pytest.mark.parametrize("identifiant", ["court", "dQw4w9WgXcQx", "dQw4w9WgX?Q", ""])
def test_youtube_identifiant_invalide(identifiant):
    with pytest.raises(ValueError):
        url_origine("youtube", identifiant)


def test_type_inconnu():
    with pytest.raises(ValueError, match="inconnu"):
        url_origine("youtube", "dQw4w9WgXcQ", type_publication="live")


def test_plateforme_inconnue_et_casse():
    with pytest.raises(ValueError, match="Plateforme inconnue"):
        url_origine("facebook", "1")
    assert url_origine("YouTube", "dQw4w9WgXcQ").startswith("https://www.youtube.com/")
