"""Dater une publication à partir de son identifiant.

Certaines plateformes fabriquent leurs identifiants à partir de l'heure :
l'identifiant contient alors la date de création, sans ouvrir la publication
(utile pour une vidéo supprimée ou privée dont on a seulement l'URL). Pur
calcul local, aucun appel réseau.

Ce que chaque plateforme permet :

- TikTok : les 32 bits de poids fort de l'identifiant (64 bits) sont l'heure
  Unix en secondes. Format non documenté par TikTok, établi par des chercheurs
  (Ryan Benson, 2020 : https://hindsig.ht/blog/tinkering-with-tiktok-timestamps/ ;
  arXiv 2504.13279). L'écart avec le champ `createTime` de la page est
  d'environ 5 s la plupart du temps, 18 s au pire dans l'échantillon publié
  (la date de l'identifiant précède la date affichée).
- Instagram : l'identifiant numérique d'une publication a 64 bits : 41 bits de
  millisecondes depuis une époque propre à Instagram, 13 bits de fragment de
  base de données, 10 bits de séquence (présentation « Sharding & IDs at
  Instagram », 2012). Le « shortcode » de l'URL est cet identifiant écrit en
  base 64. Cette correspondance n'est pas publiée par Instagram : méthode
  établie par la communauté, confirmée sur 1 000 publications par
  https://ws-dl.blogspot.com/2021/02/2021-02-20-creation-time-and-published.html
  (écart de 0 à 2 s pour une photo ou un carrousel, jusqu'à 60 min 32 s pour
  une vidéo envoyée avec une connexion lente : l'identifiant est créé à la fin
  du traitement du média, pas à l'envoi).
- YouTube : l'identifiant (11 caractères) est aléatoire et ne contient aucune
  date. La date de mise en ligne vient de l'API (`snippet.publishedAt`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Alphabet des shortcodes Instagram : base 64 « URL-safe », dans cet ordre.
_ALPHABET_IG = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_INDEX_IG = {c: i for i, c in enumerate(_ALPHABET_IG)}

# Époque d'Instagram : 24 août 2011, 21:07:01.721 UTC. Valeur estimée (non
# publiée) : 21:07:00 pour l'étude de l'université Old Dominion, 21:07:01.721
# pour maxintel.org. Choisir l'une ou l'autre change le résultat de moins de 2 s,
# ce qui reste sous la marge annoncée.
_EPOQUE_IG_MS = 1_314_220_021_721

# Un shortcode public a 11 caractères au plus (un identifiant sur 64 bits). Les
# publications privées ont un code beaucoup plus long (28 caractères) qui ne
# se décode pas de cette façon.
_LONGUEUR_MAX_SHORTCODE = 11

# Marges d'incertitude, en secondes (voir le docstring du module).
MARGE_TIKTOK_S = 18
MARGE_INSTAGRAM_PHOTO_S = 2
MARGE_INSTAGRAM_VIDEO_S = 60 * 60 + 32  # pire écart mesuré : 60 min 32 s

# Types Instagram dont l'identifiant est créé presque à l'instant de l'envoi.
_TYPES_INSTAGRAM_RAPIDES = {"post", "photo", "carrousel"}

# Garde-fou TikTok : avant 2016 (lancement de Douyin puis TikTok), un
# identifiant n'a pas ce format ; une date décodée dans le passé lointain
# signale donc une entrée qui n'est pas un identifiant de vidéo.
_TIKTOK_DATE_MIN = datetime(2016, 1, 1, tzinfo=UTC)


class DateNonDecodable(ValueError):
    """La date ne peut pas être lue dans cet identifiant."""


@dataclass(frozen=True)
class DateDecodee:
    """Résultat du décodage : une date et la marge dans laquelle la croire."""

    date: datetime  # toujours en UTC
    marge_secondes: float  # la vraie date de mise en ligne est à ± cette marge
    methode: str  # explication courte, à citer dans un rapport

    @property
    def intervalle(self) -> tuple[datetime, datetime]:
        marge = timedelta(seconds=self.marge_secondes)
        return self.date - marge, self.date + marge


def shortcode_vers_identifiant(shortcode: str) -> int:
    """Convertit un shortcode Instagram (`/p/<shortcode>/`) en identifiant numérique."""
    if not shortcode:
        raise ValueError("Shortcode Instagram vide.")
    numero = 0
    for caractere in shortcode:
        if caractere not in _INDEX_IG:
            raise ValueError(
                f"Shortcode Instagram invalide : caractère {caractere!r} "
                "(alphabet attendu : lettres, chiffres, '-' et '_')."
            )
        numero = numero * 64 + _INDEX_IG[caractere]
    return numero


def identifiant_vers_shortcode(identifiant: int) -> str:
    """Convertit un identifiant numérique Instagram en shortcode (sans zéros de tête)."""
    if isinstance(identifiant, bool) or not isinstance(identifiant, int) or identifiant <= 0:
        raise ValueError("L'identifiant numérique Instagram doit être un entier positif.")
    chiffres = []
    while identifiant:
        identifiant, reste = divmod(identifiant, 64)
        chiffres.append(_ALPHABET_IG[reste])
    return "".join(reversed(chiffres))


def identifiant_instagram(valeur: str) -> int:
    """Accepte un shortcode ou un identifiant numérique, rend l'identifiant numérique.

    Un identifiant « média_utilisateur » (`3600545900919030401_123`) est
    accepté : seule la partie avant le soulignement compte.
    """
    valeur = valeur.strip()
    debut, _, fin = valeur.partition("_")
    if fin and debut.isdigit():
        valeur = debut
    # Un identifiant numérique a 18 ou 19 chiffres ; un shortcode a 11 caractères
    # au plus, donc la longueur lève toute ambiguïté.
    if valeur.isascii() and valeur.isdigit() and len(valeur) > _LONGUEUR_MAX_SHORTCODE:
        return int(valeur)
    if len(valeur) > _LONGUEUR_MAX_SHORTCODE:
        raise DateNonDecodable(
            f"Code Instagram de {len(valeur)} caractères : probablement une publication "
            "privée, dont le code ne contient pas la date."
        )
    return shortcode_vers_identifiant(valeur)


def _date_tiktok(identifiant: str) -> DateDecodee:
    if not identifiant.isascii() or not identifiant.isdigit():
        raise ValueError(f"Identifiant TikTok invalide (chiffres attendus) : {identifiant!r}")
    numero = int(identifiant)
    if numero >= 2**64:
        raise ValueError("Identifiant TikTok invalide : dépasse 64 bits.")
    date = datetime.fromtimestamp(numero >> 32, tz=UTC)
    if date < _TIKTOK_DATE_MIN or date > datetime.now(UTC) + timedelta(days=1):
        raise DateNonDecodable(
            f"Cet identifiant TikTok donne la date {date:%Y-%m-%d}, hors de la période "
            "possible : ce n'est probablement pas un identifiant de vidéo."
        )
    return DateDecodee(
        date=date,
        marge_secondes=MARGE_TIKTOK_S,
        methode=(
            "TikTok : 32 bits de poids fort de l'identifiant = heure Unix (secondes). "
            "Méthode non documentée par TikTok ; écart habituel 5 s, 18 s au pire "
            "(la date de l'identifiant précède la date affichée)."
        ),
    )


def _date_instagram(identifiant: str, type_publication: str | None) -> DateDecodee:
    numero = identifiant_instagram(identifiant)
    if numero >= 2**64:
        raise ValueError("Identifiant Instagram invalide : dépasse 64 bits.")
    millisecondes = _EPOQUE_IG_MS + (numero >> 23)
    date = datetime.fromtimestamp(millisecondes / 1000, tz=UTC)
    if date > datetime.now(UTC) + timedelta(days=1):
        raise DateNonDecodable("Cet identifiant Instagram donne une date dans le futur.")

    if (type_publication or "").lower() in _TYPES_INSTAGRAM_RAPIDES:
        marge = MARGE_INSTAGRAM_PHOTO_S
    else:
        # Reel, vidéo, ou type inconnu : marge prudente, car l'identifiant d'une
        # vidéo peut être créé jusqu'à une heure après l'envoi.
        marge = MARGE_INSTAGRAM_VIDEO_S
    return DateDecodee(
        date=date,
        marge_secondes=marge,
        methode=(
            "Instagram : 41 bits de poids fort de l'identifiant = millisecondes depuis "
            "l'époque d'Instagram (24 août 2011) ; shortcode = identifiant en base 64. "
            "Correspondance non publiée par Instagram, établie par des chercheurs ; "
            "écart de 0 à 2 s pour une photo, jusqu'à 1 h pour une vidéo."
        ),
    )


def date_depuis_identifiant(
    plateforme: str, identifiant: str, type_publication: str | None = None
) -> DateDecodee:
    """Date de création d'une publication, lue dans son identifiant.

    `plateforme` : « tiktok », « instagram » ou « youtube ».
    `identifiant` : numérique pour TikTok ; shortcode ou numérique pour Instagram.
    `type_publication` : sert seulement à Instagram (« post », « photo »,
    « carrousel », « reel », « video ») pour choisir la marge d'incertitude.

    Lève `DateNonDecodable` quand l'identifiant ne porte pas de date (YouTube)
    et `ValueError` quand il est mal formé (`DateNonDecodable` en est une
    sous-classe : un seul `except ValueError` suffit à l'appelant).
    """
    nom = plateforme.strip().lower()
    identifiant = str(identifiant).strip()
    if nom == "tiktok":
        return _date_tiktok(identifiant)
    if nom == "instagram":
        return _date_instagram(identifiant, type_publication)
    if nom == "youtube":
        raise DateNonDecodable(
            "Date non décodable pour cette plateforme : un identifiant YouTube est "
            "aléatoire et ne contient aucune date. Utiliser `publishedAt` de l'API."
        )
    raise DateNonDecodable(f"Date non décodable pour cette plateforme : {plateforme!r}.")
