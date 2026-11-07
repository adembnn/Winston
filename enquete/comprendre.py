"""Comprendre la question du journaliste, et résumer une pièce en 25 mots.

Image simple : un standardiste à deux vitesses. Pendant la frappe, il lit la
question sur-le-champ, sans réfléchir (`variantes_locales`, instantané, sans
modèle). Dès que le journaliste fait une pause, il appelle un enquêteur chevronné
(Claude, par `claude -p` et l'abonnement d'Adem, aucune clé d'API payante) qui
reformule, choisit le type d'affaire, dresse la liste de questions et les
hypothèses concurrentes (`comprendre`). Si l'enquêteur ne répond pas à temps, on
garde la lecture instantanée : l'outil ne reste jamais muet.

Règle anti-triche (kickoff/liens-intelligents.md) : les prompts ci-dessous ne
contiennent que des mécanismes généraux, aucun nom, date ou fait des affaires
du banc d'essai.

Ligne de commande : `python -m enquete.comprendre "question" [--graine URL]`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import unicodedata

from enquete.liens import url_origine
from enquete.reseau import Role, Solidite
from enquete.temps import date_depuis_identifiant

DELAI_COMPRENDRE_S = 150.0  # mesuré : ~86 s pour un appel complet avec Sonnet
DELAI_RESUME_S = 90.0

CHAMPS = ("transcript", "onScreenText", "caption")
LIBELLES_CHAMPS = {
    "transcript": "ce qui est dit",
    "onScreenText": "ce qui est écrit à l'écran",
    "caption": "la légende",
}

GABARITS = (
    "arnaque commerciale",
    "désinformation santé",
    "rumeur virale",
    "contenus recyclés",
    "fabrication",
    "campagne coordonnée",
)
GABARIT_LIBRE = "libre"
ROLES = tuple(r.value for r in Role)

# ---------------------------------------------------------------------------
# 1. Lecture instantanée, sans modèle
# ---------------------------------------------------------------------------

_MOTS_VIDES = frozenset(
    """a à au aux avec ce ces cet cette ceci cela ça ceux celle celles dans de des du
    donc dont elle elles en entre et est es sont suis être été étaient était sera
    eux il ils je j l la le les leur leurs lui ma mais me même mes moi mon ne ni
    nos notre nous on ou où par pas pour qu que quel quelle quels quelles qui quoi
    sa sans se ses si son sur ta te tes toi ton tu un une vos votre vous y
    comment pourquoi quand combien derrière autour peut peuvent faire fait ont avoir
    ai as avons avez the of and""".split()
)


def sans_accents(texte: str) -> str:
    decompose = unicodedata.normalize("NFD", texte)
    return "".join(c for c in decompose if not unicodedata.combining(c))


def normaliser_question(question: str) -> str:
    """Forme de comparaison : minuscules, sans accents, sans ponctuation, espaces simples."""
    bas = sans_accents(question.casefold())
    bas = re.sub(r"[^\w#@]+", " ", bas)
    return re.sub(r"\s+", " ", bas).strip()


_UNITES = [
    "zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf",
    "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize",
    "dix-sept", "dix-huit", "dix-neuf",
]
_DIZAINES = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}


def nombre_en_lettres(n: int) -> str | None:
    """0 à 99 en toutes lettres (« 17 » donne « dix-sept »). None au-delà."""
    if not 0 <= n <= 99:
        return None
    if n < 20:
        return _UNITES[n]
    d, u = divmod(n, 10)
    if d == 7:
        return "soixante-et-onze" if u == 1 else "soixante-" + _UNITES[10 + u]
    if d == 9:
        return "quatre-vingt-" + _UNITES[10 + u]
    if d == 8:
        return "quatre-vingts" if u == 0 else "quatre-vingt-" + _UNITES[u]
    mot = _DIZAINES[d]
    if u == 0:
        return mot
    return mot + ("-et-un" if u == 1 else "-" + _UNITES[u])


def _singulier_pluriel(mot: str) -> list[str]:
    if len(mot) < 3 or mot.isdigit():
        return [mot]
    if mot.endswith(("s", "x")):
        return [mot, mot[:-1]]
    if mot.endswith("al"):
        return [mot, mot[:-2] + "aux"]
    if mot.endswith("au"):
        return [mot, mot + "x"]
    return [mot, mot + "s"]


def _variantes_mot(mot: str) -> list[str]:
    """Singulier/pluriel, sans accents, nombre en lettres, hashtag d'un mot."""
    sortie: list[str] = []

    def ajouter(v: str) -> None:
        if v and v != mot and v not in sortie:
            sortie.append(v)

    for forme in _singulier_pluriel(mot):
        ajouter(forme)
    for forme in [mot, *sortie]:
        ajouter(sans_accents(forme))
    if mot.isdigit() and (lettres := nombre_en_lettres(int(mot))):
        ajouter(lettres)
    if not mot.startswith(("#", "@")):
        ajouter("#" + sans_accents(mot).replace("-", ""))
    return sortie


def variantes_locales(question: str) -> dict[str, Any]:
    """Lecture instantanée de la question, sans modèle, appelée à chaque frappe.

    Rend : `mots` (chaque mot clé avec ses variantes et les champs où il sera
    cherché), `expressions` (suites de mots clés voisins, avec forme hashtag),
    `champs` (parole, texte à l'écran, légende), `source` = "local".
    """
    jetons = re.findall(r"[#@]?\w[\w'’-]*", question)
    mots: list[dict[str, Any]] = []
    vus: set[str] = set()
    segments: list[list[str]] = [[]]
    for brut in jetons:
        mot = re.split(r"['’]", brut.casefold())[-1].strip("-")  # « l'arnaque » donne « arnaque »
        cle = sans_accents(mot)
        if not mot or cle in _MOTS_VIDES or mot in _MOTS_VIDES or (len(mot) < 3 and not mot.isdigit()):
            segments.append([])
            continue
        segments[-1].append(mot)
        if cle in vus:
            continue
        vus.add(cle)
        mots.append({"mot": mot, "variantes": _variantes_mot(mot), "champs": list(CHAMPS)})

    expressions: list[dict[str, Any]] = []
    for seg in segments:
        if len(seg) < 2:
            continue
        phrase = " ".join(seg)
        singulier = " ".join(m[:-1] if m.endswith("s") and len(m) > 3 else m for m in seg)
        variantes: list[str] = []
        en_lettres = " ".join(
            (nombre_en_lettres(int(m)) or m) if m.isdigit() else m for m in seg
        )
        for v in (
            singulier,
            en_lettres,
            sans_accents(phrase),
            "#" + sans_accents(singulier).replace(" ", "").replace("-", ""),
            "#" + sans_accents(phrase).replace(" ", "").replace("-", ""),
        ):
            if v != phrase and v not in variantes:
                variantes.append(v)
        expressions.append({"expression": phrase, "variantes": variantes, "champs": list(CHAMPS)})

    return {
        "source": "local",
        "question": question,
        "mots": mots,
        "expressions": expressions,
        "champs": [{"champ": c, "libelle": LIBELLES_CHAMPS[c]} for c in CHAMPS],
    }


# ---------------------------------------------------------------------------
# 2. La graine : un lien collé
# ---------------------------------------------------------------------------

_MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
_MOIS_LONGS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
               "septembre", "octobre", "novembre", "décembre"]

_RE_TIKTOK = re.compile(
    r"https?://(?:www\.|m\.)?tiktok\.com/@(?P<pseudo>[A-Za-z0-9._]{1,24})/(?P<type>video|photo)/(?P<id>\d{15,20})",
    re.IGNORECASE,
)
_RE_INSTAGRAM = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:(?P<pseudo>[A-Za-z0-9._]{1,30})/)?"
    r"(?P<type>p|reels?|tv)/(?P<id>[A-Za-z0-9_-]{5,11})",
    re.IGNORECASE,
)
_RE_YOUTUBE = re.compile(
    r"https?://(?:(?:www\.|m\.)?youtube\.com/(?:watch\?(?:[^\s#]*&)?v=|shorts/)|youtu\.be/)"
    r"(?P<id>[A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)


def _date_lisible(date: datetime) -> str:
    return f"{date.day} {_MOIS[date.month - 1]} {date.year} vers {date.hour} h {date.minute:02d} UTC"


def _marge_lisible(marge_s: float) -> str:
    if marge_s >= 3600:
        return f"{int(marge_s // 3600)} h {int(marge_s % 3600 // 60):02d} min"
    if marge_s >= 60:
        return f"{int(marge_s // 60)} min"
    return f"{int(marge_s)} s"


def lire_graine(texte: str) -> dict[str, Any] | None:
    """Reconnaît un lien TikTok, Instagram ou YouTube collé. Calcul local, sans réseau.

    Rend plateforme, pseudo (None si le lien n'en porte pas), identifiant, type,
    date lue dans l'identifiant avec sa marge (None pour YouTube : l'identifiant
    est aléatoire) et l'URL d'origine. None si rien n'est reconnu.
    """
    texte = (texte or "").strip()
    if m := _RE_TIKTOK.search(texte):
        plateforme, pseudo, identifiant = "tiktok", m["pseudo"], m["id"]
        genre = m["type"].lower()
        url = url_origine("tiktok", identifiant, pseudo=pseudo, type_publication=genre)
    elif m := _RE_INSTAGRAM.search(texte):
        plateforme, pseudo, identifiant = "instagram", m["pseudo"], m["id"]
        genre = "post" if m["type"].lower() == "p" else "reel"
        try:
            url = url_origine("instagram", identifiant, pseudo=pseudo, type_publication=genre)
        except ValueError:
            return None
    elif m := _RE_YOUTUBE.search(texte):
        plateforme, pseudo, identifiant = "youtube", None, m["id"]
        genre = "short" if "/shorts/" in m[0].lower() else "video"
        url = url_origine("youtube", identifiant, type_publication=genre)
    else:
        return None

    graine: dict[str, Any] = {
        "plateforme": plateforme,
        "pseudo": pseudo,
        "identifiant": identifiant,
        "type": genre,
        "url_origine": url,
        "date": None,
        "marge_secondes": None,
        "date_lisible": None,
        "methode_date": None,
    }
    try:
        d = date_depuis_identifiant(plateforme, identifiant, genre)
    except ValueError as e:  # DateNonDecodable en est une sous-classe
        graine["date_erreur"] = str(e)
    else:
        graine.update(
            date=d.date.isoformat(),
            marge_secondes=d.marge_secondes,
            date_lisible=_date_lisible(d.date),
            methode_date=d.methode,
        )
    graine["phrase"] = _phrase_graine(graine)
    return graine


def _phrase_graine(g: dict[str, Any]) -> str:
    nom = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube"}[g["plateforme"]]
    qui = f" de @{g['pseudo']}" if g["pseudo"] else ""
    if g["date_lisible"]:
        return (
            f"Publication {nom}{qui}, mise en ligne le {g['date_lisible']} "
            f"(à {_marge_lisible(g['marge_secondes'])} près, lu dans l'identifiant)."
        )
    return f"Publication {nom}{qui}. La date n'est pas lisible dans l'identifiant."


# ---------------------------------------------------------------------------
# 3. Appel à Claude (claude -p), délai, cache
# ---------------------------------------------------------------------------

Executeur = Callable[[str, float], str]


class ErreurClaude(RuntimeError):
    """Claude n'a pas répondu, ou pas de façon exploitable."""


def extraire_json(texte: str) -> dict:
    """Le premier objet JSON d'une réponse, même entouré de texte ou de barrières ```json.

    Même analyse que `appel_claude.extraire_json` (branche du raisonnement).
    """
    brut = texte.strip()
    barriere = re.search(r"```(?:json)?\s*(.*?)```", brut, re.DOTALL)
    if barriere:
        brut = barriere.group(1).strip()
    debut = brut.find("{")
    if debut < 0:
        raise ErreurClaude(f"aucun objet JSON dans la réponse : {texte[:200]!r}")
    profondeur, dans_chaine, echappe = 0, False, False
    for i in range(debut, len(brut)):
        c = brut[i]
        if dans_chaine:
            if echappe:
                echappe = False
            elif c == "\\":
                echappe = True
            elif c == '"':
                dans_chaine = False
            continue
        if c == '"':
            dans_chaine = True
        elif c == "{":
            profondeur += 1
        elif c == "}":
            profondeur -= 1
            if profondeur == 0:
                try:
                    objet = json.loads(brut[debut:i + 1])
                except json.JSONDecodeError as e:
                    raise ErreurClaude(f"JSON illisible : {e}") from e
                if not isinstance(objet, dict):
                    raise ErreurClaude("la réponse n'est pas un objet JSON")
                return objet
    raise ErreurClaude("objet JSON jamais refermé")


def executeur_claude(modele: str = "sonnet") -> Executeur:
    """L'exécuteur réel : `claude -p`, sans outil, depuis un dossier neutre.

    Même exécuteur que raisonnement.py (`appel_claude.ExecuteurCLI`, branche
    worktree-agent-a263e76e0c3e3755c), recopié : même commande, même dossier
    neutre, clé d'API retirée de l'environnement (abonnement seulement).
    À fusionner en un seul à l'intégration.
    """
    dossier_neutre = Path(tempfile.gettempdir()) / "enquete-claude-neutre"

    def appeler(prompt: str, delai: float) -> str:
        dossier_neutre.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k.upper() != "ANTHROPIC_API_KEY"}
        cmd = ["claude", "-p", prompt, "--model", modele, "--output-format", "json",
               "--tools", "", "--permission-mode", "dontAsk", "--no-session-persistence",
               "--disable-slash-commands"]
        try:
            r = subprocess.run(cmd, cwd=dossier_neutre, env=env, capture_output=True, text=True,
                               encoding="utf-8", timeout=delai)
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"délai de {delai:g} s dépassé") from e
        try:
            sortie = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise ErreurClaude(f"sortie illisible ({r.stderr[:200]!r})") from e
        if sortie.get("is_error"):
            raise ErreurClaude(str(sortie.get("result")))
        return str(sortie.get("result", ""))

    return appeler


def _avec_delai(fn: Callable[[], str], delai: float) -> str:
    """Exécute fn et renonce après `delai` secondes (le fil abandonné est un démon)."""
    boite: dict[str, Any] = {}

    def cible() -> None:
        try:
            boite["ok"] = fn()
        except BaseException as e:  # noqa: BLE001 - relayé à l'appelant
            boite["erreur"] = e

    fil = threading.Thread(target=cible, daemon=True)
    fil.start()
    fil.join(delai)
    if fil.is_alive():
        raise TimeoutError(f"délai de {delai:g} s dépassé")
    if "erreur" in boite:
        raise boite["erreur"]
    return boite["ok"]


def dossier_cache() -> Path:
    base = os.environ.get("ENQUETE_CACHE")
    return Path(base) if base else Path.home() / ".cache" / "enquete" / "comprendre"


def _cle_cache(question: str, graine: dict | None, modele: str) -> str:
    g = (graine or {}).get("url_origine", "")
    brut = f"{normaliser_question(question)}|{g}|{modele}"
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 4. comprendre()
# ---------------------------------------------------------------------------

_PROMPT_COMPRENDRE = """\
Tu es l'enquêteur en chef d'un outil d'enquête sur les réseaux sociaux (TikTok, Instagram, YouTube) pour une rédaction. Un journaliste vient de poser une question. Tu la comprends et tu prépares l'enquête. Tu ne connais pas encore la réponse : tu ne l'inventes pas.

QUESTION DU JOURNALISTE : {question}
{bloc_graine}
LES SIX GABARITS D'AFFAIRE (choisis un ou deux, sinon « libre ») :
- "arnaque commerciale" : promesse de produit ou d'argent. Cases : première annonce, influenceurs et pubs, qui encaisse, boutique/lien en bio/code promo/domaine, victimes, autorité qui a agi.
- "désinformation santé" : une affirmation de santé fausse ou dangereuse. Cases : l'affirmation mot pour mot, qui vend le remède, expert qui a répondu, agence sanitaire, cas graves.
- "rumeur virale" : un récit qui se propage. Cases : première occurrence, plateforme de départ, bascule en masse, mutation du récit, chiffres et périmètres, démenti.
- "contenus recyclés" : une vieille image présentée comme récente. Cases : date et lieu réels de l'original, première reprise, légendes changeantes, fact-checks.
- "fabrication" : faux propos, montage, contenu généré. Cases : source d'origine, ce qui a été changé, personne usurpée, démenti, fabricant.
- "campagne coordonnée" : comptes qui agissent de concert. Cases : gestes répétés à la même heure, même texte, infrastructure partagée, attribution par une autorité.

LES DIX RÔLES (champ "role" des questions, valeurs exactes) : {roles}.

LA DOCTRINE : cherche d'abord ce qui ne colle pas (dates, comptes neufs, même phrase sur plusieurs comptes). Demande d'où ça vient, à qui ça profite (argent, audience, influence, nuisance), qui fait tourner la machine (lien en bio, domaine, société, code promo), quelle est l'affaire derrière l'affaire, qui doit répondre. Aucune accusation sans source solide.

LES CHAMPS D'UN INDEX DE VIDÉOS où chercher un mot : "transcript" (ce qui est dit), "onScreenText" (texte à l'écran), "caption" (légende). Valeurs exactes.

Réponds par UN SEUL objet JSON valide (pas de texte autour, pas de balises de code), en français, avec exactement ces clés :
{{
  "reformulation": "une seule phrase qui dit ce que le journaliste cherche à établir",
  "gabarits": ["un ou deux gabarits de la liste, ou \\"libre\\""],
  "mots": [ {{"mot": "...", "variantes": ["singulier/pluriel, synonymes courants, formes de langage parlé, hashtag, orthographes"], "champs": ["transcript", "onScreenText", "caption"]}} ],
  "questions": [ {{"texte": "question ouverte à laquelle l'enquête doit répondre", "role": "un des dix rôles", "pistes": ["piste 1 : où chercher, par quelle source", "piste 2"]}} ],
  "hypotheses": [ {{"texte": "une explication possible", "contre_le_journaliste": false, "test_qui_prouve": "...", "test_qui_refute": "..."}} ],
  "angle_mort": "ce qu'un index de vidéos publiques ne voit pas pour cette affaire (en une ou deux phrases)"
}}
Contraintes : "mots" 4 à 8 entrées ; "questions" 8 à 12 entrées, chacune avec exactement 2 pistes ; "hypotheses" 3 ou 4 entrées dont au moins une avec "contre_le_journaliste": true (une explication qui lui donne tort, par exemple un contenu innocent ou authentique) ; chaque mot a au moins 2 variantes.
"""


def _bloc_graine(graine: dict | None) -> str:
    if not graine:
        return "GRAINE : aucune, la question seule.\n"
    elements = [f"plateforme {graine.get('plateforme')}"]
    if graine.get("pseudo"):
        elements.append(f"pseudo @{graine['pseudo']}")
    if graine.get("date_lisible"):
        elements.append(f"date lue dans l'identifiant : {graine['date_lisible']}")
    return "GRAINE (publication collée par le journaliste, non vérifiée) : " + ", ".join(elements) + ".\n"


def _normaliser_resultat(brut: dict[str, Any]) -> dict[str, Any]:
    """Valide la réponse de Claude et la ramène au contrat. Lève ErreurClaude sinon."""
    refl = brut.get("reformulation")
    if not isinstance(refl, str) or not refl.strip():
        raise ErreurClaude("reformulation absente")

    gabs = [g.strip().casefold() for g in (brut.get("gabarits") or []) if isinstance(g, str)]
    gabs = [g for g in gabs if g in GABARITS or g == GABARIT_LIBRE]
    gabs = list(dict.fromkeys(gabs))
    if len(gabs) > 1:
        gabs = [g for g in gabs if g != GABARIT_LIBRE]
    gabs = gabs[:2] or [GABARIT_LIBRE]

    mots = []
    for m in brut.get("mots") or []:
        if not isinstance(m, dict) or not isinstance(m.get("mot"), str) or not m["mot"].strip():
            continue
        champs = [c for c in (m.get("champs") or []) if c in CHAMPS] or list(CHAMPS)
        variantes = [v for v in (m.get("variantes") or []) if isinstance(v, str) and v.strip()]
        mots.append({"mot": m["mot"].strip(), "variantes": variantes, "champs": champs})
    if len(mots) < 4:
        raise ErreurClaude(f"{len(mots)} mots au lieu de 4 à 8")
    mots = mots[:8]

    questions = []
    for q in brut.get("questions") or []:
        if not isinstance(q, dict) or not isinstance(q.get("texte"), str):
            continue
        role = q.get("role") if q.get("role") in ROLES else Role.INCONNU.value
        pistes = [p for p in (q.get("pistes") or []) if isinstance(p, str) and p.strip()][:2]
        questions.append({"texte": q["texte"].strip(), "role": role, "pistes": pistes})
    if len(questions) < 8:
        raise ErreurClaude(f"{len(questions)} questions au lieu de 8 à 12")
    questions = questions[:12]

    hyps = []
    for h in brut.get("hypotheses") or []:
        if not isinstance(h, dict) or not isinstance(h.get("texte"), str):
            continue
        hyps.append({
            "texte": h["texte"].strip(),
            "contre_le_journaliste": bool(h.get("contre_le_journaliste")),
            "test_qui_prouve": str(h.get("test_qui_prouve") or ""),
            "test_qui_refute": str(h.get("test_qui_refute") or ""),
        })
    if len(hyps) < 3:
        raise ErreurClaude(f"{len(hyps)} hypothèses au lieu de 3 ou 4")
    hyps = hyps[:4]
    if not any(h["contre_le_journaliste"] for h in hyps):
        raise ErreurClaude("aucune hypothèse ne donne tort au journaliste")

    angle = brut.get("angle_mort")
    if not isinstance(angle, str) or not angle.strip():
        raise ErreurClaude("angle mort absent")

    return {
        "reformulation": refl.strip(),
        "gabarits": gabs,
        "mots": mots,
        "questions": questions,
        "hypotheses": hyps,
        "angle_mort": angle.strip(),
    }


def comprendre(
    question: str,
    graine: dict | None = None,
    executeur: Executeur | None = None,
    modele: str = "sonnet",
    delai: float = DELAI_COMPRENDRE_S,
    cache: Path | bool | None = None,
) -> dict[str, Any]:
    """Un seul appel à `claude -p` : reformulation, gabarits, mots, questions, hypothèses.

    `executeur(prompt, delai) -> str` remplace l'appel réel dans les tests.
    `cache` : dossier du cache (défaut `dossier_cache()`), `False` pour s'en passer.
    Rend toujours un résultat : en cas d'échec ou de délai dépassé, le repli sur
    `variantes_locales` avec `source` = "repli" et la raison dans `erreur`.
    """
    debut = datetime.now()
    dossier = None if cache is False else Path(cache) if cache else dossier_cache()
    fichier = dossier / f"{_cle_cache(question, graine, modele)}.json" if dossier else None
    if fichier is not None and fichier.is_file():
        try:
            reponse = json.loads(fichier.read_text(encoding="utf-8"))
            reponse["source"] = "cache"
            reponse["duree_s"] = 0.0
            return reponse
        except (OSError, json.JSONDecodeError):
            pass  # cache abîmé : on refait

    local = variantes_locales(question)
    prompt = _PROMPT_COMPRENDRE.format(
        question=question.strip(), bloc_graine=_bloc_graine(graine), roles=", ".join(ROLES)
    )
    appel = executeur or executeur_claude(modele)
    try:
        texte = _avec_delai(lambda: appel(prompt, delai), delai)
        resultat = _normaliser_resultat(extraire_json(texte))
    except Exception as e:  # noqa: BLE001 - tout échec bascule sur le repli
        return {
            **local,
            "source": "repli",
            "erreur": f"{type(e).__name__} : {e}",
            "reformulation": question.strip(),
            "gabarits": [GABARIT_LIBRE],
            "questions": [],
            "hypotheses": [],
            "angle_mort": "",
            "duree_s": round((datetime.now() - debut).total_seconds(), 2),
        }

    resultat = {
        "source": "claude",
        "question": question,
        **resultat,
        "mots_locaux": local["mots"],
        "expressions": local["expressions"],
        "champs": local["champs"],
    }
    if fichier is not None:
        try:
            fichier.parent.mkdir(parents=True, exist_ok=True)
            fichier.write_text(json.dumps(resultat, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass  # un cache impossible à écrire ne doit pas casser la réponse
    resultat["duree_s"] = round((datetime.now() - debut).total_seconds(), 2)
    return resultat


# ---------------------------------------------------------------------------
# 5. Résumé d'une pièce en 25 mots
# ---------------------------------------------------------------------------

MAX_MOTS_RESUME = 25


def _champ(piece: dict, *noms: str) -> str | None:
    """Premier champ texte non vide parmi `noms`, cherché dans la pièce puis ses attributs."""
    attrs = piece.get("attributs") or {}
    for nom in noms:
        for source in (piece, attrs):
            v = source.get(nom)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _faits_piece(piece: dict) -> dict[str, str | None]:
    """Ce que la pièce dit d'elle-même, rien d'autre."""
    sol = piece.get("solidite")
    solidite = None
    if isinstance(sol, Solidite):
        solidite = sol.en_mots()
    elif isinstance(sol, dict):
        solidite = sol.get("en_mots")
        if not solidite and sol.get("fiabilite_source") and sol.get("credibilite_information"):
            solidite = Solidite(sol["fiabilite_source"], int(sol["credibilite_information"])).en_mots()
    pseudo = _champ(piece, "pseudo", "auteur", "compte", "username")
    return {
        "libelle": _champ(piece, "libelle"),
        "type": _champ(piece, "type"),
        "role": _champ(piece, "role"),
        "pseudo": pseudo.lstrip("@") if pseudo else None,
        "date": _champ(piece, "date", "date_publication", "publie_le", "date_utc"),
        "solidite": solidite,
    }


def _date_analysee(date: str | None) -> datetime | None:
    if not date:
        return None
    try:
        return datetime.fromisoformat(date.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tronquer(texte: str, n: int) -> str:
    mots = texte.split()
    if len(mots) <= n:
        return texte
    return " ".join(mots[: n - 1]).rstrip(".,;:") + "…"


def _resume_repli(piece: dict, question: str = "") -> str:
    """Phrase de gabarit, remplie depuis les champs de la pièce. Jamais plus de 25 mots."""
    f = _faits_piece(piece)
    morceaux = [_tronquer(f["libelle"] or "Pièce du dossier", 8)]
    if f["pseudo"]:
        morceaux.append(f"par @{f['pseudo']}")
    if d := _date_analysee(f["date"]):
        morceaux.append(f"le {d.day} {_MOIS[d.month - 1]} {d.year}")
    elif f["date"]:
        morceaux.append(f"le {f['date']}")
    phrase = " ".join(morceaux) + "."
    if f["role"] and f["role"] != Role.INCONNU.value:
        phrase += f" Rôle : {f['role'].replace('_', ' ')}."
    if f["solidite"]:
        phrase += f" Solidité : {f['solidite']}."
    return _tronquer(phrase, MAX_MOTS_RESUME)


def verifier_resume(resume: str, piece: dict) -> list[str]:
    """Contrôles par code ; rend la liste des problèmes (vide = résumé accepté)."""
    if not resume or not resume.strip():
        return ["résumé vide"]
    problemes = []
    if len(resume.split()) > MAX_MOTS_RESUME:
        problemes.append(f"{len(resume.split())} mots (maximum {MAX_MOTS_RESUME})")
    f = _faits_piece(piece)
    bas = sans_accents(resume.casefold())
    if f["pseudo"] and sans_accents(f["pseudo"].casefold()) not in bas:
        problemes.append("le pseudo de la pièce est absent")
    d = _date_analysee(f["date"])
    if d is not None:
        mois = [sans_accents(_MOIS[d.month - 1].casefold()).strip("."),
                sans_accents(_MOIS_LONGS[d.month - 1].casefold())]
        jour_ok = re.search(rf"\b0?{d.day}\b", bas) is not None
        mois_ok = any(m in bas for m in mois) or re.search(rf"\b0?{d.month}\b", bas) is not None
        if not ((jour_ok and mois_ok) or d.date().isoformat() in bas):
            problemes.append("la date de la pièce est absente")
    elif f["date"]:
        if sans_accents(f["date"].casefold()) not in bas:
            problemes.append("la date de la pièce est absente")
    elif re.search(r"\b(19|20)\d{2}\b", resume):
        problemes.append("une année apparaît alors que la pièce n'a pas de date")
    if f["pseudo"] is None and re.search(r"@\w+", resume):
        problemes.append("un pseudo apparaît alors que la pièce n'en porte pas")
    return problemes


_PROMPT_RESUME = """\
Tu rédiges la fiche d'une pièce d'un dossier d'enquête pour un journaliste. Écris UNE phrase en français de 25 mots au maximum qui dit : de quoi il s'agit, qui l'a publié, quand, pourquoi la pièce est dans le dossier, et sa solidité en mots.

RÈGLES : utilise uniquement les informations ci-dessous. N'invente aucun fait, aucun nom, aucune date. Si une information manque, ne la mentionne pas. Écris le pseudo (avec @) et la date (jour, mois abrégé, année) tels qu'ils sont dans la pièce. Aucune accusation. Réponds par la phrase seule, sans guillemets ni commentaire.

QUESTION DE L'ENQUÊTE : {question}

PIÈCE (JSON) :
{piece}
"""


def resumer_piece(
    piece: dict,
    question: str,
    executeur: Executeur | None = None,
    modele: str = "sonnet",
    delai: float = DELAI_RESUME_S,
) -> str:
    """Résumé de 25 mots au plus. Contrôlé par code, repli sur un gabarit rempli."""
    appel = executeur or executeur_claude(modele)
    prompt = _PROMPT_RESUME.format(
        question=question.strip(), piece=json.dumps(piece, ensure_ascii=False, default=str, indent=1)
    )
    try:
        texte = _avec_delai(lambda: appel(prompt, delai), delai)
        texte = " ".join((texte or "").strip().strip('"«»').split())
        if not verifier_resume(texte, piece):
            return texte
    except Exception:  # noqa: BLE001
        pass
    return _resume_repli(piece, question)


# ---------------------------------------------------------------------------
# 6. Le résumé de l'enquête (page ouverte par un clic sur la question centrale)
# ---------------------------------------------------------------------------
#
# Les chiffres, les pièces établies, les acteurs, la chronologie, les zones
# d'ombre, les prochaines étapes, « qui appeler » et la certitude sont faits par
# du code, à partir de l'export. Seuls `en_bref` et `mecanisme` sont écrits par
# Claude, à partir de l'export seulement ; chaque phrase doit citer un
# `piece_id` de l'export et ne contenir que les nombres de `chiffres`, sinon
# la réponse est rejetée et des phrases faites par code prennent le relais.

DELAI_RESUME_ENQUETE_S = 120.0
ECHELLE_CERTITUDE = ("établi", "très probable", "probable", "possible", "non établi")
PLAFOND_CERTITUDE = "probable"  # tant qu'un humain n'a pas validé
VERSION_RESUME = 2  # 2 : verbes d'argent interdits (lexique)

_ROLES_PRIVES = ("temoin", "expert", "victime", "mis_en_cause")
_TYPES_NOMMABLES = ("societe", "site", "document")
_NOM_TYPE = {
    "video": "vidéo", "publication_texte": "publication", "document": "document",
    "site": "site", "societe": "société", "personne": "personne", "grappe": "série",
}
_ROLE_MOTS = {
    "origine": "plus ancienne trace retrouvée", "amplificateur": "amplification",
    "beneficiaire": "à qui ça profite", "infrastructure": "infrastructure",
    "victime": "victime ou personne concernée", "temoin": "témoin direct", "expert": "expert",
    "contradicteur": "contradicteur", "mis_en_cause": "mis en cause", "inconnu": "rôle à établir",
}
_CHAMPS_DATE = ("date", "date_publication", "publie_le", "date_utc", "premier_vu",
                "mise_en_ligne", "creee_le", "date_creation", "cree_le")
_FAIT_DATE = {
    "premier_vu": "Plus ancienne trace retrouvée", "mise_en_ligne": "Mise en ligne",
    "creee_le": "Création", "cree_le": "Création", "date_creation": "Création",
}
_TYPES_LIEN_SERIE = ("dit_la_meme_chose", "dit_phrase")
_MOTS_INTERDITS = ("arnaque", "escroquerie", "escroc", "coupable", "frauduleux", "fraudeur",
                   # lexique : aucun verbe d'argent sans pièce bancaire ou judiciaire
                   "encaiss", "empoch", "blanchi", "detourn", "premier vu")
_NOMBRES_EN_LETTRES = frozenset(
    "deux trois quatre cinq six sept huit neuf dix onze douze vingt trente quarante cinquante "
    "soixante cent mille million millions".split())


def _pieces_de(export: dict) -> list[dict]:
    return [p for d in export.get("dossiers", []) for p in d.get("pieces", [])]


def _matiere(piece: dict) -> str | None:
    sol = piece.get("solidite")
    return sol.get("matiere") if isinstance(sol, dict) else None


def _designation(piece: dict) -> str:
    """Comment nommer une pièce : le libellé pour une société, un site, un document ou un compte
    public ; le TYPE pour une personne ou pour tout ce qui porte le nom d'un témoin, d'un expert,
    d'une victime ou d'un mis en cause. Aucun particulier n'est nommé."""
    attrs = piece.get("attributs") or {}
    type_ = piece.get("type") or ""
    if type_ in _TYPES_NOMMABLES:
        return piece.get("libelle") or _NOM_TYPE.get(type_, type_)
    if type_ == "personne":
        return attrs.get("fonction") or "personne"
    if (piece.get("role") in _ROLES_PRIVES) or attrs.get("anonyme"):
        base = _NOM_TYPE.get(type_, type_ or "pièce")
        plateforme = attrs.get("plateforme")
        return f"{base} ({plateforme})" if plateforme else base
    return piece.get("libelle") or _NOM_TYPE.get(type_, type_ or "pièce")


def _noms_interdits(export: dict) -> set[str]:
    """Textes qui désigneraient un particulier : le libellé entier de toute pièce qu'on ne nomme
    pas, et le nom de famille probable (mots capitalisés du premier segment, s'il est court)."""
    interdits: set[str] = set()
    for p in _pieces_de(export):
        libelle = (p.get("libelle") or "").strip()
        if not libelle or _designation(p) == libelle:
            continue
        interdits.add(libelle)
        segment = libelle.split(",")[0].split()
        if 1 < len(segment) <= 3:
            interdits.update(m for m in segment[1:] if len(m) >= 4 and m[0].isupper() and m.isalpha())
    return interdits


def _anonymiser(texte: str, export: dict) -> str:
    for nom in sorted(_noms_interdits(export), key=len, reverse=True):
        texte = re.sub(re.escape(nom), "[personne]", texte, flags=re.IGNORECASE)
    return texte


def _nb_serie(grappe: dict, export: dict) -> int:
    attrs = grappe.get("attributs") or {}
    for cle in ("nb_videos", "videos"):
        if isinstance(attrs.get(cle), int):
            return attrs[cle]
    return len({l["source_id"] for l in export.get("liens", [])
                if l.get("cible_id") == grappe.get("id") and l.get("type_lien") in _TYPES_LIEN_SERIE})


def mesurer_chiffres(export: dict) -> dict[str, Any]:
    """Seulement des mesures. `videos_lues` vient du journal (le serveur le dépose dans l'export).
    `sources_independantes` vaut None : un nombre de séries n'est pas un nombre de sources
    indépendantes, ce chiffre viendra d'un module d'indépendance des sources."""
    grappes = [p for p in _pieces_de(export) if p.get("type") == "grappe"]
    lues = export.get("videos_lues")
    return {
        "videos": lues if isinstance(lues, int) else None,
        "repetent": sum(_nb_serie(g, export) for g in grappes),
        "series": len(grappes),
        "sources_independantes": None,
    }


def _pieces_solides(export: dict) -> list[dict]:
    solides = [p for p in _pieces_de(export) if _matiere(p) == "solide"]
    ids = {p["id"] for p in solides}
    # Une pièce qui désigne la même entité qu'une autre pièce solide ne s'établit qu'une fois.
    return [p for p in solides if (p.get("attributs") or {}).get("meme_entite_que") not in ids]


def _source_donnee(piece: dict) -> str | None:
    for pr in piece.get("preuves") or []:
        if pr.get("source_donnee"):
            return pr["source_donnee"]
    return None


def _etabli(export: dict) -> list[dict]:
    sorties = []
    for p in _pieces_solides(export):
        role = _ROLE_MOTS.get(p.get("role") or "inconnu", "rôle à établir")
        phrase = f"{_designation(p)} : {_NOM_TYPE.get(p.get('type'), 'pièce')} solide, rôle « {role} »"
        if src := _source_donnee(p):
            phrase += f", selon {src}"
        sorties.append({"texte": phrase + ".", "piece_id": p["id"]})
    return sorties


def _acteurs(export: dict) -> list[dict]:
    sorties = []
    for dossier in export.get("dossiers", []):
        role = dossier.get("role")
        pieces = [p for p in dossier.get("pieces", []) if p.get("type") != "grappe"]
        videos_publiques = [p for p in pieces if p.get("type") == "video" and _designation(p) == p.get("libelle")]
        if len(videos_publiques) > 3:
            sorties.append({"role": role, "nom_ou_type": f"{len(videos_publiques)} vidéos de comptes publics"})
            pieces = [p for p in pieces if p not in videos_publiques]
        sorties.extend({"role": role, "nom_ou_type": _designation(p)} for p in pieces)
    return sorties


def _date_cle(valeur: str) -> tuple[float, str]:
    d = _date_analysee(valeur)
    if d is None:
        return (float("inf"), valeur)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (d.timestamp(), valeur)


def _chronologie(export: dict) -> list[dict]:
    lignes = []
    for p in _pieces_de(export):
        if p.get("type") == "grappe":
            continue
        attrs = p.get("attributs") or {}
        for champ in _CHAMPS_DATE:
            valeur = attrs.get(champ) or p.get(champ)
            if isinstance(valeur, str) and valeur.strip():
                debut = _FAIT_DATE.get(champ, "Publication")
                lignes.append({"date": valeur.strip(), "fait": f"{debut} : {_designation(p)}", "piece_id": p["id"]})
                break
    return sorted(lignes, key=lambda l: _date_cle(l["date"]))


def _prochaines_etapes(export: dict) -> list[str]:
    etapes = []
    for zone in export.get("zones_d_ombre", []):
        pistes = zone.get("pistes") or []
        if pistes and pistes[0].get("geste"):
            etapes.append(pistes[0]["geste"])
    if any(p.get("role") == "mis_en_cause" for p in _pieces_de(export)):
        etapes.append("Envoyer la sollicitation au mis en cause")
    return etapes


def _qui_appeler(export: dict) -> list[dict]:
    """Les témoins d'abord, puis les experts, les mis en cause en dernier (avant publication)."""
    ordre = (("temoin", "témoin possible, identité à vérifier avant tout contact"),
             ("expert", "expert à interroger pour éclairer le dossier"),
             ("mis_en_cause", "à solliciter avant publication pour recueillir sa version"))
    sorties = []
    for role, pourquoi in ordre:
        for p in _pieces_de(export):
            if p.get("role") == role:
                sorties.append({"qui": _designation(p), "pourquoi": pourquoi, "piece_id": p["id"]})
    return sorties


def _certitude(export: dict) -> str:
    pieces = _pieces_de(export)
    solides = [p for p in pieces if _matiere(p) == "solide"]
    if not solides:
        brut = "possible" if any(_matiere(p) == "indice" for p in pieces) else "non établi"
    elif any(p.get("role") in ("beneficiaire", "mis_en_cause") for p in solides):
        brut = "probable"
    else:
        brut = "possible"
    # Plafond : rien ne dépasse « probable » sans validation humaine.
    return ECHELLE_CERTITUDE[max(ECHELLE_CERTITUDE.index(brut), ECHELLE_CERTITUDE.index(PLAFOND_CERTITUDE))]


def _lies(export: dict, roles_a: tuple[str, ...], roles_b: tuple[str, ...]) -> bool:
    role = {p["id"]: p.get("role") for p in _pieces_de(export)}
    return any((role.get(l["source_id"]) in roles_a and role.get(l["cible_id"]) in roles_b)
               or (role.get(l["source_id"]) in roles_b and role.get(l["cible_id"]) in roles_a)
               for l in export.get("liens", []))


def phrases_par_code(export: dict, chiffres: dict | None = None) -> dict[str, Any]:
    """`en_bref` et `mecanisme` faits par code, à partir des mesures et des rôles seulement."""
    chiffres = chiffres or mesurer_chiffres(export)
    pieces = _pieces_de(export)
    solides = _pieces_solides(export)
    phrases = []
    if chiffres["videos"] is not None and chiffres["series"]:
        phrases.append(f"{chiffres['videos']} vidéos lues, dont {chiffres['repetent']} répètent une phrase "
                       f"commune, rangées en {chiffres['series']} séries distinctes.")
    elif chiffres["series"]:
        phrases.append(f"{chiffres['repetent']} vidéos répètent une phrase commune, rangées en "
                       f"{chiffres['series']} séries distinctes.")
    else:
        phrases.append(f"{len(pieces)} pièces au dossier, sans série de vidéos qui se répètent.")
    if solides:
        phrases.append(f"{len(solides)} pièces solides au dossier ; le reste reste à recouper.")
    else:
        phrases.append("Aucune pièce solide pour l'instant : tout reste à recouper.")
    if n := len(export.get("zones_d_ombre", [])):
        phrases.append(f"{n} zones d'ombre restent ouvertes.")
    infra = [p["libelle"] for p in pieces if p.get("role") == "infrastructure" and p.get("type") in ("site", "societe")]
    benef = [p["libelle"] for p in pieces if p.get("role") == "beneficiaire" and p.get("type") == "societe"]
    mecanisme = []
    if infra:
        mecanisme.append("Moyens techniques relevés au dossier : " + ", ".join(infra[:3]) + ".")
    if benef:
        liaison = " ; des liens du dossier la rattachent à ces moyens techniques" if _lies(
            export, ("infrastructure",), ("beneficiaire",)) else ""
        mecanisme.append(f"À qui ça profite : le registre cite {', '.join(benef[:2])}{liaison}.")
    if chiffres["series"]:
        mecanisme.append("Des vidéos répètent la même phrase, par séries.")
    if not mecanisme:
        mecanisme.append("Le mécanisme n'est pas établi par les pièces actuelles du dossier.")
    return {"en_bref": " ".join(phrases[:4]), "mecanisme": " ".join(mecanisme[:2]), "source": "code"}


# --- Claude : prompt, contrôle par code, cache -------------------------------

_PROMPT_RESUME_ENQUETE = """\
Tu rédiges le résumé d'une enquête pour un journaliste de Brut. Tu écris à partir du DOSSIER ci-dessous et de rien d'autre.

RÈGLES
- Tu rends deux blocs : "en_bref" (2 à 4 phrases : ce qu'on sait, ce qui est solide, ce qui manque) et "mecanisme" (1 ou 2 phrases : comment l'affaire fonctionne, de la promesse commerciale à l'argent, si le dossier le montre ; sinon dis ce qui manque).
- CHAQUE phrase cite dans "pieces" au moins un "id" de pièce du dossier, et seulement des ids qui y figurent.
- Les seuls nombres permis sont ceux de CHIFFRES (jamais celui qui vaut null). N'écris aucun autre nombre : ni date en chiffres, ni durée, ni montant. Les séries se disent « séries distinctes », jamais « sources indépendantes ».
- Une pièce "indice" n'est jamais présentée comme un fait établi : au conditionnel ou avec « selon ». Une pièce "solide" est attribuée (« selon le registre »).
- Aucun particulier n'est nommé. Pas de « arnaque » : écris « promesse commerciale ». Pas de « coupable », pas d'accusation sans pièce solide. Aucun verbe d'argent (« encaisser », « empocher »…) : écris « est présentée comme l'exploitant de », d'après les mentions légales. Écris « Plus ancienne trace retrouvée », jamais « origine ».
- N'écris pas les ids dans le texte.

Réponds par UN SEUL objet JSON valide, sans texte autour ni balises :
{{"en_bref": [{{"texte": "...", "pieces": ["id"]}}], "mecanisme": [{{"texte": "...", "pieces": ["id"]}}]}}

CHIFFRES : {chiffres}

DOSSIER (JSON) :
{dossier}
"""


def _dossier_pour_claude(export: dict) -> dict:
    """Ce que Claude voit : l'export réduit et anonymisé. Les vidéos qui se répètent ne sont pas
    listées une à une (les séries les portent)."""
    pieces = []
    amplificatrices = []
    for p in _pieces_de(export):
        if p.get("type") == "video" and p.get("role") == "amplificateur":
            amplificatrices.append(p["id"])
            continue
        attrs = {k: v for k, v in (p.get("attributs") or {}).items() if isinstance(v, (str, int, bool))}
        pieces.append({
            "id": p["id"], "type": p.get("type"), "role": p.get("role"),
            "designation": _designation(p), "matiere": _matiere(p),
            "solidite": (p.get("solidite") or {}).get("en_mots"),
            "attributs": {k: _anonymiser(v, export) if isinstance(v, str) else v for k, v in attrs.items()},
            "source": _source_donnee(p),
        })
    return {
        "question": export.get("question"),
        "pieces": pieces[:80],
        "videos_amplificatrices_ids": amplificatrices[:5],
        "liens": [{"type": l.get("type_lien"), "de": l.get("source_id"), "vers": l.get("cible_id"),
                   "explication": _anonymiser(str(l.get("explication") or ""), export)}
                  for l in export.get("liens", []) if l.get("type_lien") not in _TYPES_LIEN_SERIE][:60],
        "zones_d_ombre": [{"id": z["id"], "titre": z.get("titre"), "pourquoi": z.get("pourquoi_en_mots")}
                          for z in export.get("zones_d_ombre", [])],
    }


def verifier_phrases(brut: dict, export: dict, chiffres: dict) -> list[str]:
    """Contrôles par code de la réponse de Claude ; liste des problèmes (vide = acceptée)."""
    problemes: list[str] = []
    ids = {p["id"] for p in _pieces_de(export)}
    permis = {str(v) for v in chiffres.values() if isinstance(v, int)}
    permis_lettres = {sans_accents(nombre_en_lettres(v)) for v in chiffres.values()
                      if isinstance(v, int) and nombre_en_lettres(v)}
    interdits = _noms_interdits(export)
    for bloc, (mini, maxi) in {"en_bref": (2, 4), "mecanisme": (1, 2)}.items():
        phrases = brut.get(bloc)
        if not isinstance(phrases, list) or not mini <= len(phrases) <= maxi:
            problemes.append(f"« {bloc} » : {mini} à {maxi} phrases attendues")
            continue
        for ph in phrases:
            texte = ph.get("texte") if isinstance(ph, dict) else None
            cites = ph.get("pieces") if isinstance(ph, dict) else None
            if not isinstance(texte, str) or not texte.strip():
                problemes.append(f"« {bloc} » : phrase vide")
                continue
            if not isinstance(cites, list) or not cites or any(c not in ids for c in cites):
                problemes.append(f"« {bloc} » : piece_id absent ou inconnu dans « {texte[:40]} »")
            nu = texte
            for i in sorted(ids, key=len, reverse=True):
                nu = nu.replace(i, " ")
            for n in re.findall(r"\d+", nu):
                if n not in permis:
                    problemes.append(f"nombre non mesuré : {n}")
            bas = sans_accents(nu.casefold())
            for mot in re.findall(r"[a-z]+", bas):
                if mot in _NOMBRES_EN_LETTRES and mot not in permis_lettres:
                    problemes.append(f"nombre en lettres non mesuré : {mot}")
            for mot in _MOTS_INTERDITS:
                if mot in bas:
                    problemes.append(f"mot interdit : {mot}")
            if re.search(r"sources? independantes?", bas):
                problemes.append("« sources indépendantes » : seules les séries sont mesurées")
            if any(nom.casefold() in nu.casefold() for nom in interdits):
                problemes.append("un particulier est nommé")
    return problemes


def _cle_cache_resume(dossier: dict, chiffres: dict, modele: str) -> str:
    brut = json.dumps([VERSION_RESUME, modele, chiffres, dossier], sort_keys=True, ensure_ascii=False, default=str)
    return "resume-" + hashlib.sha256(brut.encode("utf-8")).hexdigest()[:32]


def rediger_resume(
    export: dict,
    executeur: Executeur | None = None,
    modele: str = "sonnet",
    delai: float = DELAI_RESUME_ENQUETE_S,
    cache: bool = True,
    chiffres: dict | None = None,
) -> dict[str, Any]:
    """`en_bref` et `mecanisme` écrits par Claude à partir de l'export seulement, contrôlés par code.

    Rend {en_bref, mecanisme, source: "claude"} ; si Claude ne répond pas ou si sa réponse est
    rejetée : {en_bref, mecanisme, source: "code"}. Cache disque par empreinte de l'export
    (seules les réponses de Claude acceptées y entrent).
    """
    chiffres = chiffres or mesurer_chiffres(export)
    dossier = _dossier_pour_claude(export)
    fichier = dossier_cache() / f"{_cle_cache_resume(dossier, chiffres, modele)}.json"
    if cache:
        try:
            garde = json.loads(fichier.read_text(encoding="utf-8"))
            if garde.get("source") == "claude":
                return garde
        except (OSError, ValueError):
            pass
    appel = executeur or executeur_claude(modele)
    prompt = _PROMPT_RESUME_ENQUETE.format(
        chiffres=json.dumps(chiffres, ensure_ascii=False),
        dossier=json.dumps(dossier, ensure_ascii=False, default=str, indent=1))
    try:
        brut = extraire_json(_avec_delai(lambda: appel(prompt, delai), delai))
        defauts = verifier_phrases(brut, export, chiffres)
        if not defauts:
            resultat = {"en_bref": " ".join(p["texte"].strip() for p in brut["en_bref"]),
                        "mecanisme": " ".join(p["texte"].strip() for p in brut["mecanisme"]),
                        "source": "claude"}
            if cache:
                try:
                    fichier.parent.mkdir(parents=True, exist_ok=True)
                    fichier.write_text(json.dumps(resultat, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            return resultat
        raison = "réponse rejetée par le contrôle : " + " ; ".join(str(d) for d in defauts[:5])
    except Exception as erreur:  # noqa: BLE001 - Claude muet ou illisible : repli par code
        raison = f"{type(erreur).__name__} : {erreur}"
    # Le repli dit pourquoi : visible dans la réponse et dans la console du serveur.
    print(f"[résumé] repli sur les phrases par code ({raison})", file=sys.stderr, flush=True)
    return {**phrases_par_code(export, chiffres), "raison_repli": raison}


def resume_enquete(
    export: dict,
    profond: bool = True,
    executeur: Executeur | None = None,
    modele: str = "sonnet",
    delai: float = DELAI_RESUME_ENQUETE_S,
    cache: bool = True,
) -> dict[str, Any]:
    """Le résumé complet, au contrat de `GET /api/resume`."""
    chiffres = mesurer_chiffres(export)
    textes = (rediger_resume(export, executeur, modele, delai, cache, chiffres)
              if profond else phrases_par_code(export, chiffres))
    return {
        "en_bref": textes["en_bref"],
        "etabli": _etabli(export),
        "mecanisme": textes["mecanisme"],
        "acteurs": _acteurs(export),
        "chronologie": _chronologie(export),
        "chiffres": chiffres,
        "ombre": [z["id"] for z in export.get("zones_d_ombre", [])],
        "prochaines_etapes": _prochaines_etapes(export),
        "qui_appeler": _qui_appeler(export),
        "certitude": _certitude(export),
        "source": textes["source"],
    }


# ---------------------------------------------------------------------------
# 7. Ligne de commande
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m enquete.comprendre", description="Comprendre une question d'enquête.")
    p.add_argument("question")
    p.add_argument("--graine", help="lien TikTok, Instagram ou YouTube collé")
    p.add_argument("--modele", default="sonnet")
    p.add_argument("--delai", type=float, default=DELAI_COMPRENDRE_S)
    p.add_argument("--local", action="store_true", help="lecture instantanée seulement, sans Claude")
    p.add_argument("--sans-cache", action="store_true")
    a = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    graine = lire_graine(a.graine) if a.graine else None
    if a.local:
        sortie: dict[str, Any] = variantes_locales(a.question)
    else:
        sortie = comprendre(a.question, graine, modele=a.modele, delai=a.delai,
                            cache=False if a.sans_cache else None)
    if a.graine:
        sortie = {"graine": graine, **sortie}
    print(json.dumps(sortie, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
