"""Les pivots : des indices concrets qui font passer d'une pièce à une autre.

Un enquêteur qui suit l'argent et l'infrastructure ne compare pas des textes, il suit des
objets précis : le lien en bio, le domaine, la boutique, le code promo, le SIREN, le pseudo
réutilisé, le canal Telegram, le même son. Ce module les extrait des résultats Oriane, pèse leur
rareté, et propose des liens candidats entre comptes. Il nourrit l'agent « à qui ça profite ».

Règles :
- jamais d'e-mail, de numéro de téléphone ni de lien wa.me : ces pistes sont désactivées. Les
  extraits gardés les masquent même quand ils entourent un autre pivot ;
- chaque pivot garde ses preuves : identifiant de la vidéo, champ, extrait, seconde (transcription) ;
- l'unité de comptage est le COMPTE (plateforme + pseudo), pas la vidéo : un compte qui répète sa
  phrase cinquante fois ne rend pas son code « répandu » ;
- un lien candidat reste une piste (`statut="piste"`) tant que l'agent des liens ne l'a pas validé.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from enquete.liens import url_origine
from enquete.reseau import Preuve
from enquete.sources.infra import Indice

# ---------------------------------------------------------------------------
# Types de pivots
# ---------------------------------------------------------------------------

DOMAINE = "domaine"
URL = "url"
PAGE_DE_LIENS = "page_de_liens"
CODE_PROMO = "code_promo"
BOUTIQUE = "boutique"
PAIEMENT = "paiement"
PORTEFEUILLE = "portefeuille"
QR = "qr"
AFFILIATION = "affiliation"
PSEUDO_CITE = "pseudo_cite"
PSEUDO_REUTILISE = "pseudo_reutilise"
HASHTAG = "hashtag"
SON = "son"
SIREN = "siren"
SIRET = "siret"
TELEGRAM = "telegram"

# Type de lien candidat produit quand deux comptes ou plus partagent la valeur.
TYPE_LIEN = {
    DOMAINE: "même infrastructure",
    URL: "même infrastructure",
    PAGE_DE_LIENS: "même infrastructure",
    TELEGRAM: "même infrastructure",
    CODE_PROMO: "même argent",
    BOUTIQUE: "même argent",
    PAIEMENT: "même argent",
    PORTEFEUILLE: "même argent",
    QR: "même infrastructure",
    AFFILIATION: "même argent",
    SIREN: "même argent",
    SIRET: "même argent",
    SON: "même son",
    PSEUDO_CITE: "cite le même compte",
    PSEUDO_REUTILISE: "même pseudo sur plusieurs plateformes",
    HASHTAG: "même hashtag",
}

# Explication innocente la plus courante, pour que l'agent des liens la teste (doctrine : un lien
# fort a toujours une explication banale en face).
EXPLICATION_INNOCENTE = {
    DOMAINE: "Deux comptes citent le même site de bonne foi (article, source commune).",
    URL: "Deux comptes partagent le même article ou la même page publique.",
    PAGE_DE_LIENS: "Une même page de liens peut être un compte partagé ou un agent commun, mais aussi une marque et ses ambassadeurs déclarés.",
    TELEGRAM: "Un canal public suivi par beaucoup de monde est cité par des comptes sans lien entre eux.",
    CODE_PROMO: "Un code d'affiliation public (programme ouvert) est repris par des créateurs indépendants.",
    BOUTIQUE: "Une même boutique est citée comme vendeur par des acheteurs ou des ambassadeurs déclarés.",
    PAIEMENT: "Une cagnotte ou un lien de paiement public est relayé par des comptes solidaires ou clients.",
    PORTEFEUILLE: "Une adresse de don publique (ONG, campagne) est relayée par des comptes sans lien entre eux.",
    QR: "Un même QR code de campagne ou de ticket est diffusé par des comptes qui le reprennent de bonne foi.",
    AFFILIATION: "Un programme d'affiliation ouvert peut donner un identifiant public repris par des comptes indépendants.",
    SIREN: "Une même société peut être citée dans des mentions légales par des clients indépendants.",
    SIRET: "Un même établissement peut être cité par des clients indépendants.",
    SON: "Un son tendance est repris par des milliers de comptes sans coordination.",
    PSEUDO_CITE: "Un compte populaire est cité par des comptes sans lien entre eux.",
    PSEUDO_REUTILISE: "Un pseudo courant peut être pris par deux personnes différentes.",
    HASHTAG: "Un hashtag d'actualité est repris par tout le monde.",
}

# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PreuvePivot:
    """Où le pivot a été vu : la vidéo, le champ, l'extrait exact, la seconde."""

    compte: str  # « plateforme:pseudo »
    champ: str  # caption, transcript, onScreenText, bio, bioLink, audioPlatformId...
    extrait: str
    contenu_id: str | None = None  # identifiant Oriane de la vidéo (None pour une bio de profil)
    url: str | None = None  # lien d'origine reconstruit
    debut_s: float | None = None  # seconde de début, seulement pour la transcription
    fin_s: float | None = None
    via: str | None = None  # page de liens dépliée pour trouver ce pivot

    def vers_dict(self) -> dict[str, Any]:
        return {
            "compte": self.compte, "contenu_id": self.contenu_id, "champ": self.champ,
            "extrait": self.extrait, "url": self.url, "debut_s": self.debut_s, "fin_s": self.fin_s,
            "via": self.via,
        }

    def vers_preuve(self, date_collecte: datetime | None = None, source_donnee: str = "Oriane") -> Preuve:
        """La même preuve au format du réseau."""
        return Preuve(
            url=self.url or "https://connect.oriane.xyz",
            extrait=self.extrait,
            source_donnee=source_donnee,
            date_collecte=date_collecte or datetime.now(UTC),
            debut_s=self.debut_s,
            fin_s=self.fin_s if self.debut_s is not None else None,
        )


@dataclass(frozen=True)
class Pivot:
    type: str
    valeur: str
    comptes: tuple[str, ...]
    preuves: tuple[PreuvePivot, ...]
    poids_rarete: float = 0.0
    banal: bool = False
    indices: list[Indice] = field(default_factory=list)
    a_deplier: bool = False  # page de liens dont on n'a pas lu les liens sortants
    sous_type: str | None = None  # portefeuille : la chaîne ; paiement : le service

    @property
    def n_comptes(self) -> int:
        return len(self.comptes)

    def vers_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "sous_type": self.sous_type,
            "valeur": self.valeur,
            "poids_rarete": self.poids_rarete,
            "banal": self.banal,
            "comptes": list(self.comptes),
            "preuves": [p.vers_dict() for p in self.preuves],
            "indices": [i.vers_dict() for i in self.indices],  # les portefeuilles restent à []: autre module
        }


@dataclass(frozen=True)
class LienCandidat:
    """Une valeur partagée par au moins deux comptes. Reste une piste jusqu'à validation."""

    type: str  # « même infrastructure », « même argent », « même son »...
    pivot_type: str
    valeur: str
    comptes: tuple[str, ...]
    poids: float
    banal: bool
    preuves: tuple[PreuvePivot, ...]
    explication_innocente: str
    statut: str = "piste"
    sous_type: str | None = None
    fort: bool = False  # même portefeuille ou même identifiant de paiement, hors banal

    def vers_dict(self) -> dict[str, Any]:
        return {
            "type": self.type, "pivot_type": self.pivot_type, "valeur": self.valeur,
            "comptes": list(self.comptes), "poids": self.poids, "banal": self.banal,
            "preuves": [p.vers_dict() for p in self.preuves],
            "explication_innocente": self.explication_innocente, "statut": self.statut,
            "sous_type": self.sous_type, "fort": self.fort,
        }


# ---------------------------------------------------------------------------
# Pistes désactivées : e-mail, téléphone, WhatsApp
# ---------------------------------------------------------------------------

_RE_EMAIL = re.compile(r"[\w.+%-]+@[\w-]+(?:\.[\w-]+)+")
_RE_WHATSAPP = re.compile(
    r"(?i)(?:https?://)?(?:wa\.me|wa\.link|api\.whatsapp\.com|chat\.whatsapp\.com|whatsapp\.com)/\S*"
)
_RE_TEL_FR = re.compile(r"(?<![\w])(?:(?:\+|00)33[ .\-]?|0)[1-9](?:[ .\-]?\d{2}){4}(?![\w])")
_RE_TEL_INTER = re.compile(r"(?<![\w])\+\d{1,3}[ .\-]?\d(?:[ .\-]?\d){7,12}(?![\w])")
_RE_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")


def _iban_valide(brut: str) -> bool:
    """Contrôle mod 97 : seuls de vrais IBAN sont masqués, pas n'importe quel code."""
    iban = brut.replace(" ", "")
    if not 15 <= len(iban) <= 34:
        return False
    rearrange = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in rearrange)) % 97 == 1


class _Iban:
    """Même interface qu'un motif `re` pour `sub`, restreinte aux IBAN valides."""

    @staticmethod
    def sub(remplacement, texte: str) -> str:
        def _r(m: re.Match[str]) -> str:
            if not _iban_valide(m.group(0)):
                return m.group(0)
            return remplacement(m) if callable(remplacement) else remplacement
        return _RE_IBAN.sub(_r, texte)


_PISTES_OFF = (_RE_EMAIL, _RE_WHATSAPP, _Iban, _RE_TEL_FR, _RE_TEL_INTER)


def _blanchir(texte: str) -> str:
    """Remplace e-mails, téléphones et liens WhatsApp par des espaces, longueur inchangée."""
    for motif in _PISTES_OFF:
        texte = motif.sub(lambda m: " " * len(m.group(0)), texte)
    return texte


def _masquer(texte: str) -> str:
    """Pour un extrait gardé : les pistes désactivées deviennent « [masqué] »."""
    for motif in _PISTES_OFF:
        texte = motif.sub("[masqué]", texte)
    return texte


# ---------------------------------------------------------------------------
# Domaines et URL
# ---------------------------------------------------------------------------

_TLD = (
    "com|fr|net|org|io|co|shop|store|app|me|tv|xyz|info|eu|ch|be|de|es|it|uk|us|ca|ly|to|gg|link|site|"
    "online|live|ai|dev|page|bio|ru|pl|nl|pt|ma|dz|tn|sn|ci|cm|cc|biz|club|top|vip|world|news|media|"
    "blog|tech|asia|ink|one|fun|cloud|agency|digital|today|network|ws|im|ng|za|br|mx|ar|jp|cn|in|au|nz|tk|ml"
)
_RE_EXPLICITE = re.compile(r"""(?i)\b(?:https?://|www\.)[^\s<>"'`\])}]+""")
_RE_NU = re.compile(
    r"(?<![\w@./:%-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:" + _TLD + r"))(?![\w-])"
    r"(/[^\s<>\"'`\])}]*)?"
)
_PONCTUATION_FINALE = ".,;:!?)»”’'…"

_SLD_COMPOSES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.br", "com.au", "co.jp", "co.nz", "com.tr", "com.mx",
    "co.za", "com.ar", "co.in", "com.cn", "com.pt", "com.es",
}
# Hébergeurs partagés : l'identité est le sous-domaine, pas le domaine enregistrable.
_HEBERGES = {
    "myshopify.com", "wixsite.com", "blogspot.com", "wordpress.com", "carrd.co", "github.io",
    "netlify.app", "vercel.app", "webflow.io", "squarespace.com", "notion.site", "substack.com",
    "medium.com", "beehiiv.com", "ghost.io", "weebly.com", "bubbleapps.io", "lovable.app",
    "pages.dev", "framer.website", "framer.app", "gumroad.com", "bigcartel.com", "tumblr.com",
    "mailchimpsites.com", "godaddysites.com", "my.canva.site", "canva.site", "linktr.ee",
}
# Plateformes : leur domaine ne prouve rien, seul le contenu précis (lien, profil) compte.
_PLATEFORMES = {
    "instagram.com", "tiktok.com", "youtube.com", "youtu.be", "facebook.com", "fb.com", "fb.watch", "fb.me",
    "x.com", "twitter.com", "threads.net", "bsky.app", "snapchat.com", "twitch.tv", "linkedin.com",
    "pinterest.com", "pinterest.fr", "reddit.com", "wikipedia.org", "google.com", "google.fr", "apple.com",
    "spotify.com", "deezer.com", "soundcloud.com", "discord.com", "discord.gg", "telegram.org", "bit.ly",
    "tinyurl.com", "t.co", "ow.ly", "buff.ly", "is.gd", "cutt.ly", "rb.gy", "shorturl.at", "lnkd.in",
    "amzn.to", "amzn.eu", "goo.gl", "tiny.cc", "rebrand.ly", "vm.tiktok.com", "vt.tiktok.com",
    "apple.co", "spoti.fi", "oriane.xyz", "amazon.com", "amazon.fr", "amazon.de", "amazon.co.uk", "amazon.es",
    "temu.com", "shein.com", "aliexpress.com", "ebay.com", "ebay.fr", "etsy.com", "walmart.com", "vinted.fr",
    "vinted.com", "leboncoin.fr", "fnac.com", "cdiscount.com", "gmail.com", "outlook.com", "yahoo.fr", "hotmail.com", "icloud.com",
}
_RACCOURCISSEURS = {
    "bit.ly", "tinyurl.com", "t.co", "ow.ly", "buff.ly", "is.gd", "cutt.ly", "rb.gy", "shorturl.at",
    "lnkd.in", "amzn.to", "amzn.eu", "goo.gl", "tiny.cc", "rebrand.ly", "vm.tiktok.com", "vt.tiktok.com",
    "fb.me", "fb.watch", "youtu.be", "discord.gg", "apple.co", "spoti.fi", "spotify.link", "dlvr.it", "wp.me",
    "lnk.to", "ift.tt", "po.st", "mailchi.mp",
}
_SERVICES_LIEN_BIO = {
    "linktr.ee", "beacons.ai", "bio.link", "linkin.bio", "lnk.bio", "campsite.bio", "solo.to",
    "taplink.cc", "allmylinks.com", "hoo.be", "direct.me", "milkshake.app", "lnk.to", "withkoji.com",
    "stan.store", "bio.site", "flowcode.com", "tap.bio", "lit.link", "komi.io", "linktree.com",
    "linkr.bio", "linkpop.com", "l.ead.me", "ffm.to", "smarturl.it", "heylink.me", "bento.me",
}
_REDIRECTEURS = {
    "l.instagram.com": "u", "l.facebook.com": "u", "lm.facebook.com": "u", "www.youtube.com": "q",
    "youtube.com": "q", "out.reddit.com": "url", "href.li": None,
}
_BOUTIQUES = ("etsy.com", "vinted.fr", "vinted.com", "depop.com", "stan.store", "shop.tiktok.com",
              "aliexpress.com", "aliexpress.us", "temu.com", "shein.com", "ebay.fr", "ebay.com", "leboncoin.fr",
              "gumroad.com", "payhip.com", "teespring.com", "spreadshirt.fr", "redbubble.com", "ltk.app",
              "shopmy.us", "liketoknow.it", "shop-links.co", "myshopify.com", "bigcartel.com")

_RESEAUX_SOCIAUX = {  # hôte -> (réseau, motif du pseudo dans le chemin)
    "instagram.com": ("instagram", re.compile(r"/([A-Za-z0-9_.]{2,30})/?")),
    "tiktok.com": ("tiktok", re.compile(r"/@([A-Za-z0-9_.]{2,30})")),
    "x.com": ("x", re.compile(r"/([A-Za-z0-9_]{2,15})/?")),
    "twitter.com": ("x", re.compile(r"/([A-Za-z0-9_]{2,15})/?")),
    "youtube.com": ("youtube", re.compile(r"/(?:@|c/|user/)([A-Za-z0-9_.-]{2,40})")),
    "facebook.com": ("facebook", re.compile(r"/([A-Za-z0-9.]{3,50})/?")),
    "threads.net": ("threads", re.compile(r"/@([A-Za-z0-9_.]{2,30})")),
    "bsky.app": ("bluesky", re.compile(r"/profile/([A-Za-z0-9_.-]{2,60})")),
    "snapchat.com": ("snapchat", re.compile(r"/add/([A-Za-z0-9_.-]{2,30})")),
    "twitch.tv": ("twitch", re.compile(r"/([A-Za-z0-9_]{3,25})/?")),
}
_CHEMINS_NON_PROFIL = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "share", "s", "i", "intent",
    "hashtag", "search", "home", "status", "sharer", "watch", "groups", "events", "pages", "login",
    "privacy", "help", "about", "settings", "directory", "video", "videos", "photo", "tag", "tags",
    "music", "discover", "channel", "embed", "playlist", "shorts", "results", "feed", "gaming", "live",
    "profile", "people", "marketplace", "business", "policies", "legal", "terms", "ads", "download",
}
_PARAMS_SUIVI = re.compile(r"(?i)^(utm_.*|fbclid|igshid|igsh|gclid|si|feature|ref_src|mc_cid|mc_eid|_ga|"
                           r"s|t|cid|source|campaign|share_id|is_from_webapp|sender_device|web_id|lang|hl)$")
_PARAMS_AFFILIATION = re.compile(r"(?i)^(ref|aff|affid|affiliate|affiliate_id|partner|partner_id|"
                                 r"via|tag|aid|adid|irclickid|clickref|sscid|click_id)$")
_PARAMS_CODE = re.compile(r"(?i)^(code|coupon|promo|promocode|discount|discount_code|voucher)$")


def _registrable(hote: str) -> str:
    morceaux = [m for m in hote.lower().strip(".").split(".") if m]
    if len(morceaux) <= 2:
        return ".".join(morceaux)
    if ".".join(morceaux[-2:]) in _SLD_COMPOSES:
        return ".".join(morceaux[-3:])
    return ".".join(morceaux[-2:])


def _identite_domaine(hote: str) -> str:
    """Le domaine qui identifie un opérateur : enregistrable, ou sous-domaine pour un hébergeur partagé."""
    hote = hote.lower().strip(".")
    if hote.startswith("www."):
        hote = hote[4:]
    reg = _registrable(hote)
    return hote if reg in _HEBERGES else reg


def _appartient(hote: str, domaines: Iterable[str]) -> bool:
    hote = hote.lower()
    return any(hote == d or hote.endswith("." + d) for d in domaines)


def _normaliser_url(brut: str) -> tuple[str, str, str, list[tuple[str, str]]] | None:
    """(hôte, chemin, requête brute, paramètres) d'une URL nettoyée, ou None si illisible.
    Les redirecteurs (l.instagram.com, ...) sont dépliés."""
    tronquee = brut.endswith(("...", "…"))  # lien coupé par la plateforme : le chemin n'est pas fiable
    brut = brut.rstrip(_PONCTUATION_FINALE)
    if tronquee:
        brut = brut.split("/", 3)[0] + "//" + brut.split("/", 3)[2] if brut.count("/") >= 2 else brut
    if not re.match(r"(?i)^[a-z][a-z0-9+.-]*://", brut):
        brut = "https://" + brut
    try:
        morceaux = urlsplit(brut)
        hote = (morceaux.hostname or "").lower().strip(".")
    except ValueError:
        return None
    if not hote or "." not in hote or morceaux.scheme.lower() not in ("http", "https"):
        return None
    if hote.startswith("www."):
        hote = hote[4:]
    params = [(k, v) for k, vs in parse_qs(morceaux.query).items() for v in vs]
    cle_redirection = _REDIRECTEURS.get(hote)
    if cle_redirection and (hote != "youtube.com" or morceaux.path.startswith("/redirect")):
        cibles = [v for k, v in params if k == cle_redirection]
        if cibles:
            return _normaliser_url(unquote(cibles[0]))
    return hote, morceaux.path or "/", morceaux.query, params


def _valeur_url(hote: str, chemin: str, params: list[tuple[str, str]]) -> str:
    gardes = [(k, v) for k, v in params if not _PARAMS_SUIVI.match(k)]
    requete = "&".join(f"{k}={v}" for k, v in sorted(gardes))
    chemin = chemin.rstrip("/") if chemin != "/" else ""
    return f"{hote}{chemin}" + (f"?{requete}" if requete else "")


Trouvaille = tuple[str, str, int, int]  # (type, valeur, début, fin) dans le texte


def _trouvailles_urls(texte: str, masque: str) -> Iterator[Trouvaille]:
    """Domaines, URL, profils, pages de liens, boutiques, cagnottes, affiliation et codes en paramètre."""
    deja: set[tuple[int, int]] = set()
    candidats: list[tuple[str, int, int]] = []
    for m in _RE_EXPLICITE.finditer(masque):
        candidats.append((m.group(0), m.start(), m.end()))
        deja.add((m.start(), m.end()))
    for m in _RE_NU.finditer(masque):
        if any(d <= m.start() < f for d, f in deja):
            continue
        candidats.append((m.group(0), m.start(), m.end()))
    for brut, debut, fin in candidats:
        lu = _normaliser_url(brut)
        if lu is None:
            continue
        hote, chemin, _requete, params = lu
        yield from _classer_url(hote, chemin, params, debut, fin)


def _classer_url(hote: str, chemin: str, params: list[tuple[str, str]], debut: int, fin: int) -> Iterator[Trouvaille]:
    if _appartient(hote, ("t.me", "telegram.me", "telegram.dog")):
        return  # traité par l'extracteur Telegram
    if _appartient(hote, ("wa.me", "whatsapp.com", "wa.link")):
        return  # piste désactivée
    reg = _registrable(hote)
    segments = [s for s in chemin.split("/") if s]

    # 1. Profil d'un réseau social : un pseudo cité.
    for domaine, (_reseau, motif) in _RESEAUX_SOCIAUX.items():
        if reg == domaine or hote == domaine:
            m = motif.match(chemin)
            if m and segments and segments[0].lower() not in _CHEMINS_NON_PROFIL:
                pseudo = m.group(1).lower().rstrip(".")
                yield PSEUDO_CITE, pseudo, debut, fin
                return
            break

    # 2. Service de lien en bio : la page elle-même est le pivot, on ne garde pas le domaine du service.
    if reg in _SERVICES_LIEN_BIO or hote in _SERVICES_LIEN_BIO:
        if segments:
            yield PAGE_DE_LIENS, f"{hote}/{segments[0].lower()}", debut, fin
        return

    # 3. Paiements, cagnottes, boutiques.
    paiement = _paiement(hote, segments)
    if paiement:
        yield PAIEMENT, f"{paiement[0]}{SEP}{paiement[1]}", debut, fin
        return
    if _appartient(hote, _SERVICES_PAIEMENT):
        return  # lien générique d'un service de paiement, sans bénéficiaire : rien à suivre
    if _appartient(hote, _BOUTIQUES) or ".myshopify." in hote:
        boutique = _valeur_boutique(hote, segments)
        if boutique:
            yield BOUTIQUE, boutique, debut, fin
            yield from _params(params, debut, fin, hote)
            return

    # 4. Paramètres d'affiliation et de code promo, quel que soit l'hôte.
    yield from _params(params, debut, fin, hote)

    # 5. Domaine et URL.
    if reg in _RACCOURCISSEURS or hote in _RACCOURCISSEURS:
        if segments:
            yield URL, _valeur_url(hote, chemin, params), debut, fin
        return
    if reg in _PLATEFORMES or hote in _PLATEFORMES:
        if segments:  # un contenu précis cité (vidéo, article) reste un pivot
            yield URL, _valeur_url(hote, chemin, params), debut, fin
        return
    yield DOMAINE, _identite_domaine(hote), debut, fin
    if segments:
        yield URL, _valeur_url(hote, chemin, params), debut, fin


SEP = "|"  # séparateur interne « sous-type|valeur », défait à la construction des pivots
_LANGUES = {"fr", "en", "es", "de", "it", "pt", "nl"}
_SERVICES_PAIEMENT = (
    "paypal.me", "paypal.com", "lydia-app.com", "lydia.me", "revolut.me", "revolut.com", "tipeee.com",
    "ko-fi.com", "patreon.com", "buymeacoffee.com", "buy.stripe.com", "donate.stripe.com", "leetchi.com",
    "gofundme.com", "helloasso.com", "utip.io", "ulule.com", "cash.app", "kisskissbankbank.com",
    "liberapay.com", "opencollective.com", "tipanddonation.com",
)
_SERVICE_PAR_HOTE = {
    "paypal.me": "paypal.me", "lydia-app.com": "lydia", "lydia.me": "lydia", "revolut.me": "revolut.me",
    "tipeee.com": "tipeee", "ko-fi.com": "ko-fi", "patreon.com": "patreon",
    "buymeacoffee.com": "buymeacoffee", "buy.stripe.com": "stripe", "donate.stripe.com": "stripe",
    "leetchi.com": "leetchi", "gofundme.com": "gofundme", "helloasso.com": "helloasso", "utip.io": "utip",
    "ulule.com": "ulule", "cash.app": "cash.app", "kisskissbankbank.com": "kisskissbankbank",
    "liberapay.com": "liberapay", "opencollective.com": "opencollective",
}
_SEGMENTS_NEUTRES = {"c", "f", "fr", "en", "es", "de", "it", "donate", "campaign", "campagne", "pot", "pots",
                     "collect", "associations", "formulaires", "boutique", "pay", "checkout", "user", "u",
                     "profile", "share", "cagnotte", "c", "p", "projects", "project", "creator", "creators"}


def _paiement(hote: str, segments: list[str]) -> tuple[str, str] | None:
    """(service, identifiant du bénéficiaire) d'un lien de paiement ou de cagnotte, sinon None."""
    service = next((nom for domaine, nom in _SERVICE_PAR_HOTE.items() if _appartient(hote, (domaine,))), None)
    if service is None:
        return None
    utiles = [x for x in segments if x.lower() not in _SEGMENTS_NEUTRES and x.lower() not in _LANGUES]
    if not utiles:
        return None
    if service == "patreon" and utiles[0].lower() in ("join", "c", "user"):
        utiles = utiles[1:]
        if not utiles:
            return None
    identifiant = utiles[0] if service in ("stripe", "lydia") else utiles[0].lower()
    return service, identifiant.lstrip("$")[:120]


def _valeur_boutique(hote: str, segments: list[str]) -> str | None:
    """Identité d'une boutique, seulement quand l'URL en porte une (sinon None : pas de pivot)."""
    if ".myshopify." in hote or hote.endswith(".gumroad.com") or hote.endswith(".bigcartel.com"):
        return hote
    reg = _registrable(hote)
    if reg == "etsy.com":
        for i, s in enumerate(segments):
            if s == "shop" and i + 1 < len(segments):
                return f"etsy.com/shop/{segments[i + 1].lower()}"
        return None
    if reg.startswith("vinted.") and len(segments) >= 2 and segments[0] == "member":
        return f"{reg}/member/{segments[1].lower()}"
    if reg.startswith("ebay.") and len(segments) >= 2 and segments[0] in ("usr", "str"):
        return f"ebay/{segments[0]}/{segments[1].lower()}"
    if reg.startswith("aliexpress.") and len(segments) >= 2 and segments[0] == "store":
        return f"aliexpress.com/store/{segments[1]}"
    if reg in ("depop.com", "stan.store", "payhip.com", "teespring.com", "redbubble.com", "gumroad.com") and segments:
        return f"{reg}/{segments[0].lower()}"
    return None


def _params(params: list[tuple[str, str]], debut: int, fin: int, hote: str) -> Iterator[Trouvaille]:
    for cle, valeur in params:
        if not valeur or len(valeur) > 60:
            continue
        if _PARAMS_CODE.match(cle):
            yield CODE_PROMO, valeur.upper(), debut, fin
        elif _PARAMS_AFFILIATION.match(cle) and len(valeur) >= 3:
            yield AFFILIATION, f"{_registrable(hote)}:{cle.lower()}={valeur}", debut, fin


# ---------------------------------------------------------------------------
# Codes promo
# ---------------------------------------------------------------------------

_CODE = r"([A-Z][A-Z0-9_-]{2,19}|[0-9]{1,3}[A-Z][A-Z0-9_-]{1,17})"
_RE_CODES = (
    # « code NUTS10 », « code promo : NUTS10 », « coupon NUTS10 »
    re.compile(r"\b(?i:(?:code(?:\s+promo)?|coupon|promo\s+code|promocode))\b\s*[:=\-–]?\s*[«\"“'‘`]*\s*" + _CODE + r"(?![A-Za-z0-9])"),
    # « -20 % avec NUTS10 », « 20% off with NUTS10 »
    re.compile(r"(?i:[-−–]\s?\d{1,2}\s?%|\d{1,2}\s?%\s*(?:off|de\s+(?:réduction|remise)))\s*"
               r"(?i:avec|with|using|use|grâce\s+à)\s+(?i:(?:le|mon|the|my)\s+)?(?i:code\s+)?[«\"“'‘`]*" + _CODE + r"(?![A-Za-z0-9])"),
)
_MOTS_NON_CODES = {
    "PROMO", "CODE", "REDUCTION", "REMISE", "OFF", "LE", "LA", "DE", "DU", "DES", "LES", "AVEC", "POUR",
    "THE", "FOR", "AND", "ET", "OU", "PIN", "ISO", "ROUTE", "CIVIL", "PENAL", "SOURCE", "POSTAL", "APE",
    "NAF", "TVA", "SIREN", "SIRET", "BARRE", "BARRES", "ETHIQUE", "CADEAU", "PROMOTION", "COUPON",
    "ICI", "LIEN", "BIO", "USE", "WITH", "MON", "MA", "TON", "TA", "SUR", "EN", "UN", "UNE", "LIVE",
}


def _trouvailles_codes(masque: str) -> Iterator[Trouvaille]:
    for motif in _RE_CODES:
        for m in motif.finditer(masque):
            code = m.group(1)
            if code in _MOTS_NON_CODES or code.isdigit() or _RE_NU.fullmatch(code.lower()):
                continue
            yield CODE_PROMO, code.upper(), m.start(), m.end()


# ---------------------------------------------------------------------------
# SIREN, SIRET
# ---------------------------------------------------------------------------

_RE_NUM_ENTREPRISE = re.compile(r"(?<![\d])(\d{3}[ . ]?\d{3}[ . ]?\d{3})(?:[ . ]?(\d{5}))?(?![\d])")
_RE_CONTEXTE_ENTREPRISE = re.compile(
    r"(?i)(siren|siret|rcs|immatricul|\btva\b|société|sociétés|\bsas\b|\bsarl\b|\beurl\b|\bsasu\b|\bsci\b|"
    r"mentions légales|company number|registration)"
)


def luhn_valide(chiffres: str) -> bool:
    """Contrôle de Luhn d'une suite de chiffres."""
    if not chiffres.isdigit():
        return False
    total = 0
    for i, c in enumerate(reversed(chiffres)):
        n = int(c)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def siren_valide(siren: str) -> bool:
    return len(siren) == 9 and luhn_valide(siren)


def siret_valide(siret: str) -> bool:
    if len(siret) != 14 or not siret.isdigit():
        return False
    if siret.startswith("356000000"):  # La Poste : somme des chiffres multiple de 5
        return sum(int(c) for c in siret) % 5 == 0
    return luhn_valide(siret)


def _trouvailles_entreprise(masque: str) -> Iterator[Trouvaille]:
    for m in _RE_NUM_ENTREPRISE.finditer(masque):
        avant = masque[max(0, m.start() - 60): m.start()]
        if not _RE_CONTEXTE_ENTREPRISE.search(avant) and not re.search(r"(?i)\bFR\s?\d{2}\s?$", avant):
            continue
        siren = re.sub(r"\D", "", m.group(1))
        if m.group(2):
            siret = siren + m.group(2)
            if siret_valide(siret):
                yield SIRET, siret, m.start(), m.end()
                if siren_valide(siren):
                    yield SIREN, siren, m.start(), m.start() + len(m.group(1))
        elif siren_valide(siren):
            yield SIREN, siren, m.start(), m.end()


# ---------------------------------------------------------------------------
# Portefeuilles crypto (validés par somme de contrôle)
# ---------------------------------------------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_RE_B58 = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{26,44}(?![A-Za-z0-9])")
_RE_BECH32 = re.compile(r"(?i)(?<![A-Za-z0-9])(?:bc1|ltc1)[ac-hj-np-z02-9]{11,87}(?![A-Za-z0-9])")
_RE_EVM = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z])")
_CONTEXTE_SOLANA = re.compile(r"(?i)(sol\b|solana|wallet|portefeuille|adresse|address|\bdon\b|donat|tip\b|envoy|send|crypto)")
_VERSIONS_BASE58 = {0x00: "bitcoin", 0x05: "bitcoin", 0x30: "litecoin", 0x32: "litecoin", 0x1E: "dogecoin",
                    0x41: "tron"}
# Adresses de gravure ou de plateforme connues : banales par nature. Liste volontairement courte
# et non exhaustive ; au-delà, c'est la part du corpus qui décide (voir `est_banal`).
PORTEFEUILLES_PLATEFORMES = {
    "11111111111111111111114oLvT2",  # adresse de gravure Bitcoin
    "1111111111111111111114oLvT2",
    "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo",  # grand portefeuille d'une plateforme d'échange (Binance)
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "11111111111111111111111111111111",  # programme système Solana
}


def _base58_decode(texte: str) -> bytes | None:
    n = 0
    for c in texte:
        i = _B58.find(c)
        if i < 0:
            return None
        n = n * 58 + i
    zeros = len(texte) - len(texte.lstrip("1"))
    return b"\x00" * zeros + n.to_bytes((n.bit_length() + 7) // 8, "big")


def _base58check(texte: str) -> tuple[int, bytes] | None:
    """(octet de version, charge utile) si la somme de contrôle est bonne."""
    brut = _base58_decode(texte)
    if brut is None or len(brut) != 25:
        return None
    if hashlib.sha256(hashlib.sha256(brut[:21]).digest()).digest()[:4] != brut[21:]:
        return None
    return brut[0], brut[1:21]


def _bech32_polymod(valeurs: list[int]) -> int:
    generateur = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in valeurs:
        haut = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= generateur[i] if (haut >> i) & 1 else 0
    return chk


def bech32_valide(adresse: str) -> str | None:
    """« bitcoin » ou « litecoin » si l'adresse bech32 / bech32m est valide, sinon None."""
    if adresse != adresse.lower() and adresse != adresse.upper():
        return None
    adresse = adresse.lower()
    hrp, _, donnees = adresse.rpartition("1")
    if hrp not in ("bc", "ltc") or len(donnees) < 7 or any(c not in _BECH32 for c in donnees):
        return None
    valeurs = [_BECH32.index(c) for c in donnees]
    contenu = [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]
    reste = _bech32_polymod(contenu + valeurs)
    version = valeurs[0]
    attendu = 1 if version == 0 else 0x2BC830A3
    if reste != attendu:
        return None
    # taille du programme de témoin : 8 bits regroupés depuis les symboles de 5 bits
    nb_bits = (len(valeurs) - 7) * 5
    taille = nb_bits // 8
    if not 2 <= taille <= 40 or (version == 0 and taille not in (20, 32)) or version > 16:
        return None
    return "bitcoin" if hrp == "bc" else "litecoin"


_KECCAK_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
    0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
    0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_KECCAK_ROT = ((0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61), (28, 55, 25, 21, 56),
               (27, 20, 39, 8, 14))
_M64 = (1 << 64) - 1


def _rol(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _M64 if n else x


def keccak256(donnees: bytes) -> bytes:
    """Keccak-256 (celui d'Ethereum, pas SHA3-256), en Python pur : sert à la casse EIP-55."""
    taux = 136
    bloc = bytearray(donnees) + b"\x01"
    bloc += b"\x00" * (-len(bloc) % taux)
    bloc[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for debut in range(0, len(bloc), taux):
        for i in range(taux // 8):
            a[i % 5][i // 5] ^= int.from_bytes(bloc[debut + 8 * i: debut + 8 * i + 8], "little")
        for rc in _KECCAK_RC:
            c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
            a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _KECCAK_ROT[x][y])
            a = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) & _M64 for y in range(5)] for x in range(5)]
            a[0][0] ^= rc
    sortie = b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return sortie


def evm_valide(adresse: str) -> bool:
    """0x + 40 hexadécimaux. Casse mixte : la somme de contrôle EIP-55 doit être juste."""
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", adresse):
        return False
    corps = adresse[2:]
    if corps == corps.lower() or corps == corps.upper():
        return True  # pas de somme de contrôle portée par la casse
    empreinte = keccak256(corps.lower().encode("ascii")).hex()
    return all((c.isupper() if int(empreinte[i], 16) >= 8 else c.islower()) or c.isdigit()
               for i, c in enumerate(corps))


def _trouvailles_portefeuilles(masque: str) -> Iterator[Trouvaille]:
    for m in _RE_EVM.finditer(masque):
        if evm_valide(m.group(0)):
            yield PORTEFEUILLE, f"evm{SEP}{m.group(0).lower()}", m.start(), m.end()
    for m in _RE_BECH32.finditer(masque):
        chaine = bech32_valide(m.group(0))
        if chaine:
            yield PORTEFEUILLE, f"{chaine}{SEP}{m.group(0).lower()}", m.start(), m.end()
    for m in _RE_B58.finditer(masque):
        mot = m.group(0)
        verifie = _base58check(mot)
        if verifie is not None:
            chaine = _VERSIONS_BASE58.get(verifie[0])
            bonne_forme = {"bitcoin": mot[0] in "13", "litecoin": mot[0] in "LM", "dogecoin": mot[0] == "D",
                           "tron": mot[0] == "T"}.get(chaine or "", False)
            if chaine and bonne_forme:
                yield PORTEFEUILLE, f"{chaine}{SEP}{mot}", m.start(), m.end()
            continue
        if 43 <= len(mot) <= 44 and _CONTEXTE_SOLANA.search(masque[max(0, m.start() - 80): m.start()]):
            brut = _base58_decode(mot)
            if brut is not None and len(brut) == 32:
                yield PORTEFEUILLE, f"solana{SEP}{mot}", m.start(), m.end()


# ---------------------------------------------------------------------------
# Pseudos, hashtags, Telegram
# ---------------------------------------------------------------------------

_RE_PSEUDO = re.compile(r"(?<![\w.@/#&])@([A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?)")
_RE_HASHTAG = re.compile(r"(?<![\w&#])#([^\W\d_][\w]{1,49})")
_RE_TELEGRAM = re.compile(
    r"(?i)(?<![\w@])(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?(\+[A-Za-z0-9_-]{6,}|joinchat/[A-Za-z0-9_-]+|[A-Za-z0-9_]{4,64})"
)
_TELEGRAM_NON_CANAL = {"share", "addstickers", "addemoji", "proxy", "socks", "login", "iv", "setlanguage",
                       "contact", "boost", "c", "s", "joinchat", "addlist", "giftcode"}
_HASHTAGS_BANALS = {
    "fyp", "foryou", "foryoupage", "pourtoi", "pourtoipage", "viral", "viralvideo", "trending", "tendance",
    "reels", "reelsinstagram", "reel", "explore", "explorepage", "instagood", "shorts", "short", "tiktok",
    "tiktokfrance", "france", "paris", "fypシ", "fy", "xyzbca", "foryoupageofficial", "love", "video",
    "videoviral", "capcut", "duet", "stitch", "actualité", "actualites", "actu", "news", "info", "infos",
    "apprendresurtiktok", "learnontiktok", "tiktokacademie", "creatorsearchinsights", "humour", "drole",
}


def _trouvailles_pseudos(masque: str) -> Iterator[Trouvaille]:
    for m in _RE_PSEUDO.finditer(masque):
        yield PSEUDO_CITE, m.group(1).lower().rstrip("."), m.start(), m.end()


def _trouvailles_hashtags(masque: str) -> Iterator[Trouvaille]:
    for m in _RE_HASHTAG.finditer(masque):
        yield HASHTAG, m.group(1).casefold(), m.start(), m.end()


def _trouvailles_telegram(masque: str) -> Iterator[Trouvaille]:
    for m in _RE_TELEGRAM.finditer(masque):
        nom = m.group(1)
        if nom.lower() in _TELEGRAM_NON_CANAL:
            continue
        valeur = nom if nom.startswith(("+", "joinchat/")) else nom.lower()
        yield TELEGRAM, f"t.me/{valeur}", m.start(), m.end()


def trouver_pivots_dans_texte(texte: str) -> list[Trouvaille]:
    """Tous les pivots d'un texte, avec leur position. Fonction pure, sans réseau."""
    masque = _blanchir(texte)
    trouves: list[Trouvaille] = []
    trouves.extend(_trouvailles_telegram(masque))
    trouves.extend(_trouvailles_urls(texte, masque))
    trouves.extend(_trouvailles_portefeuilles(masque))
    trouves.extend(_trouvailles_codes(masque))
    trouves.extend(_trouvailles_entreprise(masque))
    trouves.extend(_trouvailles_pseudos(masque))
    trouves.extend(_trouvailles_hashtags(masque))
    return trouves


def _valeur_qr(contenu: str) -> str:
    """Valeur comparable d'un QR : l'URL nettoyée si c'en est une, sinon le texte tel quel."""
    contenu = contenu.strip()
    lu = _normaliser_url(contenu) if re.match(r"(?i)^(https?://|www\.)\S+$", contenu) else None
    return _valeur_url(lu[0], lu[1], lu[3]) if lu else _masquer(contenu)[:200]


def _fenetre(texte: str, debut: int, fin: int, marge: int = 50) -> str:
    """Extrait exact autour de la trouvaille, pistes désactivées masquées."""
    morceau = texte[max(0, debut - marge): fin + marge].strip()
    return _masquer(morceau) or _masquer(texte[debut:fin])


# ---------------------------------------------------------------------------
# Lecture des résultats Oriane
# ---------------------------------------------------------------------------

def _cle_compte(plateforme: str | None, pseudo: str | None) -> str | None:
    if not pseudo:
        return None
    return f"{(plateforme or '?').lower()}:{pseudo.lower().lstrip('@')}"


class _Collecte:
    """Regroupe les trouvailles par (type, valeur) avec comptes et preuves."""

    MAX_PREUVES = 40

    def __init__(self) -> None:
        self.comptes: dict[tuple[str, str], set[str]] = {}
        self.preuves: dict[tuple[str, str], list[PreuvePivot]] = {}
        self.vus: set[tuple[Any, ...]] = set()
        self.a_deplier: set[str] = set()
        self.tous_comptes: set[str] = set()
        self.premier_contenu: dict[str, tuple[str | None, str | None]] = {}  # compte -> (id, url)
        self.handles: dict[str, set[str]] = {}  # pseudo -> comptes

    def ajouter(self, type_: str, valeur: str, preuve: PreuvePivot) -> None:
        cle = (type_, valeur)
        signature = (cle, preuve.compte, preuve.contenu_id, preuve.champ, preuve.extrait, preuve.debut_s, preuve.via)
        if signature in self.vus:
            return
        self.vus.add(signature)
        self.comptes.setdefault(cle, set()).add(preuve.compte)
        liste = self.preuves.setdefault(cle, [])
        if len(liste) < self.MAX_PREUVES:
            liste.append(preuve)


def _url_contenu(c: dict[str, Any], seconde: float | None = None) -> str | None:
    plateforme = str(c.get("platform") or "").lower()
    identifiant = c.get("platformId")
    if not plateforme or not identifiant:
        return None
    genre = str(c.get("platformType") or "").lower()
    if plateforme == "instagram":
        genre = "reel" if genre in ("reel", "video", "clip") else "post"
    elif plateforme == "youtube":
        genre = "short" if genre in ("short", "shorts") else "video"
    elif plateforme == "tiktok":
        genre = "photo" if genre in ("photo", "image", "carousel") else "video"
    try:
        return url_origine(plateforme, str(identifiant), c.get("profileHandle"), genre,
                           int(seconde) if seconde is not None and plateforme == "youtube" else None)
    except ValueError:
        return None


def _url_profil(plateforme: str, pseudo: str) -> str | None:
    modeles = {"instagram": "https://www.instagram.com/{}/", "tiktok": "https://www.tiktok.com/@{}",
               "youtube": "https://www.youtube.com/@{}"}
    return modeles[plateforme].format(pseudo) if plateforme in modeles else None


def _champs_texte(c: dict[str, Any]) -> Iterator[tuple[str, str, float | None, float | None]]:
    """(champ, texte, début_s, fin_s) pour chaque texte d'un contenu."""
    for nom in ("title", "caption", "profileBio"):
        if isinstance(c.get(nom), str) and c[nom].strip():
            yield nom, c[nom], None, None
    for media in c.get("media") or []:
        if isinstance(media.get("caption"), str) and media["caption"].strip():
            yield "media.caption", media["caption"], None, None
        for t in media.get("onScreenText") or []:
            if isinstance(t, str) and t.strip():
                yield "onScreenText", t, None, None
        morceaux = media.get("transcriptChunks") or []
        if morceaux:
            for ch in morceaux:
                if isinstance(ch.get("text"), str) and ch["text"].strip():
                    yield "transcript", ch["text"], ch.get("startSeconds"), ch.get("endSeconds")
        elif isinstance(media.get("transcript"), str) and media["transcript"].strip():
            yield "transcript", media["transcript"], None, None


def _enregistrer_texte(
    col: _Collecte, compte: str, contenu_id: str | None, url: str | None, champ: str, texte: str,
    debut_s: float | None, fin_s: float | None, via: str | None = None,
    depliages: dict[str, list[str]] | None = None, url_par_seconde: Callable[[float], str | None] | None = None,
) -> None:
    for type_, valeur, d, f in trouver_pivots_dans_texte(texte):
        if type_ == PSEUDO_CITE and compte.split(":", 1)[1] == valeur:
            continue  # un compte qui se cite lui-même n'apprend rien
        lien = url_par_seconde(debut_s) if (url_par_seconde and debut_s is not None) else url
        col.ajouter(type_, valeur, PreuvePivot(
            compte=compte, champ=champ, extrait=_fenetre(texte, d, f), contenu_id=contenu_id, url=lien,
            debut_s=debut_s, fin_s=fin_s if debut_s is not None else None, via=via))
        if type_ == PAGE_DE_LIENS:
            if depliages and valeur in depliages:
                for cible in depliages[valeur]:
                    _enregistrer_texte(col, compte, contenu_id, url, champ, cible, debut_s, fin_s,
                                       via=valeur, url_par_seconde=url_par_seconde)
            else:
                col.a_deplier.add(valeur)


def extraire_pivots(
    resultats: list[dict],
    profils: list[dict] | None = None,
    *,
    depliages: dict[str, list[str]] | None = None,
    qr: list[dict] | None = None,
) -> list[Pivot]:
    """Extrait les pivots des résultats Oriane (contenus) et des profils (bio, lien en bio).

    `depliages` : liens sortants des pages de liens déjà lues (clé « linktr.ee/nom »), produits
    par `Infra.deplier_lien_bio`. Sans eux, la page de liens reste un pivot marqué `a_deplier`.
    Les résultats en double (même `id`) ne comptent qu'une fois.

    `qr` : QR codes décodés sur les images clés, `{video_id, seconde, contenu}` (module `images`).
    Le contenu donne les mêmes pivots (URL, portefeuille, paiement) plus un pivot `qr`, avec la
    seconde comme preuve. `video_id` est l'`id` Oriane ou le `platformId` ; une vidéo absente des
    résultats est ignorée (on ne peut pas lui attribuer de compte).
    """
    col = _Collecte()
    deja_vus: set[str] = set()
    for c in resultats:
        cid = c.get("id")
        if cid is not None:
            if cid in deja_vus:
                continue
            deja_vus.add(cid)
        plateforme = str(c.get("platform") or "").lower()
        compte = _cle_compte(plateforme, c.get("profileHandle"))
        if compte is None:
            continue
        col.tous_comptes.add(compte)
        url = _url_contenu(c)
        col.premier_contenu.setdefault(compte, (cid, url))
        pseudo = compte.split(":", 1)[1]
        col.handles.setdefault(pseudo, set()).add(compte)

        for champ, texte, d, f in _champs_texte(c):
            par_seconde = (lambda s, c=c: _url_contenu(c, s)) if champ == "transcript" else None
            _enregistrer_texte(col, compte, cid, url, champ, texte, d, f, depliages=depliages,
                               url_par_seconde=par_seconde)
        for tag in c.get("hashtags") or []:
            if isinstance(tag, str) and tag.strip("#"):
                valeur = tag.strip().lstrip("#").casefold()
                col.ajouter(HASHTAG, valeur, PreuvePivot(compte, "hashtags", tag, cid, url))
        for mention in c.get("mentions") or []:
            cible = (mention.get("profileHandle") if isinstance(mention, dict) else None)
            if cible and cible.lower() != pseudo:
                col.ajouter(PSEUDO_CITE, cible.lower(), PreuvePivot(compte, "mentions", f"@{cible}", cid, url))
        if c.get("audioPlatformId"):
            valeur = f"{plateforme}:{c['audioPlatformId']}"
            extrait = f"audioPlatformId={c['audioPlatformId']}"
            titre = " — ".join(str(x) for x in (c.get("audioTitle"), c.get("audioAuthor")) if x)
            col.ajouter(SON, valeur, PreuvePivot(compte, "audioPlatformId", extrait + (f" ({titre})" if titre else ""),
                                                 cid, url))

    par_video: dict[str, dict] = {}
    for c in resultats:
        for cle in (c.get("id"), c.get("platformId")):
            if cle:
                par_video.setdefault(str(cle), c)
    for q in qr or []:
        c = par_video.get(str(q.get("video_id")))
        contenu = q.get("contenu")
        if c is None or not isinstance(contenu, str) or not contenu.strip():
            continue
        compte = _cle_compte(str(c.get("platform") or "").lower(), c.get("profileHandle"))
        if compte is None:
            continue
        col.tous_comptes.add(compte)
        seconde = q.get("seconde")
        seconde = float(seconde) if isinstance(seconde, (int, float)) else None
        par_seconde = lambda s, c=c: _url_contenu(c, s)  # noqa: E731
        url = par_seconde(seconde) if seconde is not None else _url_contenu(c)
        _enregistrer_texte(col, compte, c.get("id"), _url_contenu(c), "qr", contenu, seconde, None,
                           depliages=depliages, url_par_seconde=par_seconde)
        col.ajouter(QR, _valeur_qr(contenu), PreuvePivot(compte, "qr", _masquer(contenu.strip())[:200],
                                                         c.get("id"), url, seconde, None))

    for p in profils or []:
        plateforme = str(p.get("platform") or "").lower()
        compte = _cle_compte(plateforme, p.get("handle") or p.get("profileHandle"))
        if compte is None:
            continue
        col.tous_comptes.add(compte)
        pseudo = compte.split(":", 1)[1]
        col.handles.setdefault(pseudo, set()).add(compte)
        url = _url_profil(plateforme, pseudo)
        col.premier_contenu.setdefault(compte, (None, url))
        for champ in ("bio", "bioLink"):  # `publicEmail` n'est jamais lu : piste désactivée
            texte = p.get(champ)
            if isinstance(texte, str) and texte.strip():
                _enregistrer_texte(col, compte, None, url, champ, texte, None, None, depliages=depliages)

    # Pseudo réutilisé : le même pseudo sur au moins deux plateformes.
    for pseudo, comptes in col.handles.items():
        if len({c.split(":", 1)[0] for c in comptes}) >= 2:
            for compte in comptes:
                cid, url = col.premier_contenu.get(compte, (None, None))
                col.ajouter(PSEUDO_REUTILISE, pseudo, PreuvePivot(compte, "profileHandle", pseudo, cid, url))

    total = len(col.tous_comptes)
    pivots = []
    for (type_, valeur_interne), comptes in col.comptes.items():
        sous_type, _, valeur = valeur_interne.rpartition(SEP) if type_ in (PORTEFEUILLE, PAIEMENT) else (None, "", valeur_interne)
        poids = poids_rarete(len(comptes), total)
        pivots.append(Pivot(
            type=type_, valeur=valeur, sous_type=sous_type or None, comptes=tuple(sorted(comptes)),
            preuves=tuple(col.preuves[(type_, valeur_interne)]), poids_rarete=poids,
            banal=est_banal(type_, valeur, len(comptes), total), a_deplier=valeur in col.a_deplier))
    pivots.sort(key=lambda p: (-p.poids_rarete, _PRIORITE_TYPE.get(p.type, 9), p.valeur))
    return pivots


# ---------------------------------------------------------------------------
# Rareté
# ---------------------------------------------------------------------------

def poids_rarete(n: int, total: int) -> float:
    """−log((n+1)/(N+1)) : n comptes portent le pivot sur N comptes dans le corpus de l'affaire."""
    n, total = max(n, 0), max(total, n, 0)
    return round(-math.log((n + 1) / (total + 1)), 3)


# (part minimale du corpus, nombre minimal de comptes) à partir desquels un pivot est banal.
_SEUILS_BANALITE: dict[str, tuple[float, int]] = {
    HASHTAG: (0.03, 3),
    SON: (0.05, 4),
    PSEUDO_CITE: (0.05, 4),
}
_SEUIL_PAR_DEFAUT = (0.10, 5)


def est_banal(type_: str, valeur: str, n: int, total: int) -> bool:
    """Un point commun très répandu dans le corpus ne prouve rien."""
    if type_ == HASHTAG and valeur.casefold() in _HASHTAGS_BANALS:
        return True
    if type_ == PORTEFEUILLE and valeur.lower() in {a.lower() for a in PORTEFEUILLES_PLATEFORMES}:
        return True
    if total <= 0:
        return False
    part, minimum = _SEUILS_BANALITE.get(type_, _SEUIL_PAR_DEFAUT)
    return n >= minimum and n / total >= part


# ---------------------------------------------------------------------------
# Liens candidats et sortie pour les agents
# ---------------------------------------------------------------------------

# À poids égal, l'argent et l'infrastructure passent avant les pseudos et les hashtags.
_PRIORITE_TYPE = {
    SIREN: 0, SIRET: 0, CODE_PROMO: 1, BOUTIQUE: 1, PAIEMENT: 0, PORTEFEUILLE: 0, QR: 3, AFFILIATION: 1, PAGE_DE_LIENS: 2,
    TELEGRAM: 2, DOMAINE: 3, URL: 3, SON: 4, PSEUDO_REUTILISE: 5, PSEUDO_CITE: 6, HASHTAG: 7,
}


def relier_par_pivots(pivots: Iterable[Pivot], *, inclure_banals: bool = True) -> list[LienCandidat]:
    """Toute valeur portée par au moins deux comptes distincts devient un lien candidat typé.

    Les liens banals sont gardés mais marqués ; les plus lourds viennent en premier.
    """
    liens = []
    for p in pivots:
        if len(set(p.comptes)) < 2 or (p.banal and not inclure_banals):
            continue
        liens.append(LienCandidat(
            type=TYPE_LIEN.get(p.type, "lié par un pivot"), pivot_type=p.type, valeur=p.valeur,
            comptes=tuple(sorted(set(p.comptes))), poids=p.poids_rarete, banal=p.banal, preuves=p.preuves,
            explication_innocente=EXPLICATION_INNOCENTE.get(p.type, "Coïncidence ou usage commun."),
            sous_type=p.sous_type, fort=(p.type in (PORTEFEUILLE, PAIEMENT) and not p.banal),
        ))
    liens.sort(key=lambda lien: (lien.banal, not lien.fort, -lien.poids, _PRIORITE_TYPE.get(lien.pivot_type, 9), lien.valeur))
    return liens


def pivots_pour_agents(
    pivots: Iterable[Pivot],
    *,
    seulement_partages: bool = False,
    inclure_banals: bool = True,
    limite: int | None = None,
) -> list[dict[str, Any]]:
    """Pivots au format dictionnaire pour les agents : type, valeur, poids_rarete, banal, comptes,
    preuves, indices. Les plus rares d'abord, les banals à la fin."""
    sortie = [
        p for p in pivots
        if (inclure_banals or not p.banal) and (not seulement_partages or len(set(p.comptes)) >= 2)
    ]
    sortie.sort(key=lambda p: (p.banal, -p.poids_rarete, _PRIORITE_TYPE.get(p.type, 9), p.valeur))
    if limite is not None:
        sortie = sortie[:limite]
    return [p.vers_dict() for p in sortie]
