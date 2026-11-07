"""Reconstruire l'URL d'une publication à partir de son identifiant.

Formes d'URL produites :

- TikTok : `https://www.tiktok.com/@<pseudo>/video/<id>` ou `/photo/<id>` pour
  un diaporama de photos.
- Instagram : `https://www.instagram.com/p/<shortcode>/` ou `/reel/<shortcode>/`.
- YouTube : `https://www.youtube.com/watch?v=<id>` ou `/shorts/<id>`.

La seconde de départ (`&t=<n>s`) n'est ajoutée que pour YouTube. TikTok et
Instagram ne la prennent pas en charge dans l'URL d'une publication : on ne
l'ajoute donc jamais, plutôt que de fabriquer un lien qui semble la porter.
Un Short n'a pas de paramètre de position : avec une seconde, on rend la forme
`watch?v=` de la même vidéo, qui accepte `t` (comportement courant de YouTube,
non confirmé par sa documentation officielle).
"""

from __future__ import annotations

import re

from enquete.temps import identifiant_instagram, identifiant_vers_shortcode

# Pseudo TikTok : lettres, chiffres, point et soulignement, 24 caractères au plus.
_PSEUDO_TIKTOK = re.compile(r"[A-Za-z0-9._]{1,24}")
# Instagram (30 caractères au plus) et YouTube : validation plus large, le pseudo
# n'entre pas dans l'URL produite.
_PSEUDO_AUTRES = re.compile(r"[A-Za-z0-9._]{1,30}")
# Identifiant YouTube : 11 caractères de l'alphabet base 64 URL-safe.
_ID_YOUTUBE = re.compile(r"[A-Za-z0-9_-]{11}")


def _verifier_pseudo(pseudo: str, motif: re.Pattern[str]) -> str:
    if pseudo.startswith("@"):
        raise ValueError(f"Pseudo sans « @ » attendu, reçu {pseudo!r}.")
    if not motif.fullmatch(pseudo):
        raise ValueError(f"Pseudo invalide : {pseudo!r}.")
    return pseudo


def _verifier_seconde(seconde: int | None) -> int | None:
    if seconde is None:
        return None
    if isinstance(seconde, bool) or not isinstance(seconde, int) or seconde < 0:
        raise ValueError(f"La seconde doit être un entier positif ou nul, reçu {seconde!r}.")
    return seconde


def _type(type_publication: str | None, autorises: tuple[str, ...], nom: str) -> str:
    """Valide le type ; le premier de `autorises` est le type par défaut."""
    genre = (type_publication or autorises[0]).lower()
    if genre not in autorises:
        raise ValueError(
            f"Type de publication {type_publication!r} inconnu pour {nom} "
            f"(attendu : {', '.join(autorises)})."
        )
    return genre


def url_origine(
    plateforme: str,
    identifiant: str,
    pseudo: str | None = None,
    type_publication: str | None = None,
    seconde: int | None = None,
) -> str:
    """URL du post d'origine.

    `type_publication` : TikTok « video » (défaut) ou « photo » ; Instagram
    « post » (défaut) ou « reel » ; YouTube « video » (défaut) ou « short ».
    `pseudo` : obligatoire pour TikTok, sans « @ » ; vérifié mais inutilisé
    pour Instagram et YouTube (leurs URL n'en ont pas besoin).
    `seconde` : voir le docstring du module ; seul YouTube l'utilise.
    Lève `ValueError` si une entrée est invalide ou la plateforme inconnue.
    """
    nom = plateforme.strip().lower()
    identifiant = str(identifiant).strip()
    seconde = _verifier_seconde(seconde)

    if nom == "tiktok":
        if pseudo is None:
            raise ValueError("Le pseudo est obligatoire pour fabriquer un lien TikTok.")
        _verifier_pseudo(pseudo, _PSEUDO_TIKTOK)
        if not (identifiant.isascii() and identifiant.isdigit()):
            raise ValueError(f"Identifiant TikTok invalide (chiffres attendus) : {identifiant!r}")
        genre = _type(type_publication, ("video", "photo"), "TikTok")
        return f"https://www.tiktok.com/@{pseudo}/{genre}/{identifiant}"

    if nom == "instagram":
        if pseudo is not None:
            _verifier_pseudo(pseudo, _PSEUDO_AUTRES)
        genre = _type(type_publication, ("post", "reel"), "Instagram")
        # Shortcode ou identifiant numérique en entrée, shortcode en sortie.
        shortcode = identifiant_vers_shortcode(identifiant_instagram(identifiant))
        chemin = "reel" if genre == "reel" else "p"
        return f"https://www.instagram.com/{chemin}/{shortcode}/"

    if nom == "youtube":
        if pseudo is not None:
            _verifier_pseudo(pseudo, _PSEUDO_AUTRES)
        if not _ID_YOUTUBE.fullmatch(identifiant):
            raise ValueError(
                f"Identifiant YouTube invalide (11 caractères attendus) : {identifiant!r}"
            )
        genre = _type(type_publication, ("video", "short"), "YouTube")
        if genre == "short" and seconde is None:
            return f"https://www.youtube.com/shorts/{identifiant}"
        url = f"https://www.youtube.com/watch?v={identifiant}"
        return url if seconde is None else f"{url}&t={seconde}s"

    raise ValueError(f"Plateforme inconnue : {plateforme!r} (attendu : tiktok, instagram, youtube).")
