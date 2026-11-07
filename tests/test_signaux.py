"""Tests des signaux faibles : pour chaque détecteur, un cas qui déclenche et un cas voisin, innocent, qui ne déclenche pas.

Tout est synthétique et hors réseau. Les identifiants TikTok sont fabriqués pour porter la date voulue
(32 bits de poids fort = heure Unix), comme ceux d'Oriane.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from enquete import signaux as S
from enquete.pivots import Pivot, PreuvePivot
from enquete.reseau import Solidite
from enquete.sources.infra import Indice

BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
PHRASE = "le gouvernement cache la vérité sur les prix de l'énergie depuis des mois"
_n = itertools.count(1)


def tid(ts: datetime, extra: int = 7) -> str:
    return str((int(ts.timestamp()) << 32) | extra)


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def video(compte: str, ts: datetime, *, parole: str = "", hashtags=(), legende: str = "", ecran=(), son=None,
          licence: bool = False, abonnes: int = 1000, plateforme: str = "tiktok", cree=None, posts=None,
          suit=None, bio=None, nom=None, ref=None, **extra) -> dict:
    n = next(_n)
    v: dict = {
        "id": f"cnt_{n:05d}", "platform": plateforme, "profileHandle": compte, "platformId": tid(ts, n),
        "platformType": "video", "publishedAt": iso(ts + timedelta(seconds=5)), "caption": legende,
        "hashtags": list(hashtags), "profileFollowersCount": abonnes, "profileId": f"prf_{compte}",
        "media": [{"type": "video", "onScreenText": list(ecran), "transcript": parole,
                   "transcriptChunks": [{"startSeconds": 3, "endSeconds": 9, "text": parole}] if parole else []}],
        "updatedAt": iso(ref or ts + timedelta(days=1)),
    }
    if son:
        v.update(audioPlatformId=son, audioType="licensed" if licence else "original", audioCopyrighted=licence)
    if cree is not None:
        v["profilePlatformId"] = tid(cree, 99)
    if posts is not None:
        v["profilePostsCount"] = posts
    if suit is not None:
        v["profileFollowingCount"] = suit
    if bio is not None:
        v["profileBio"] = bio
    if nom is not None:
        v["profileDisplayName"] = nom
    v.update(extra)
    return v


def remplissage(n: int, debut: datetime = BASE) -> list[dict]:
    """n vidéos de n comptes distincts, sans rien en commun : le corpus qui rend les hashtags non banals."""
    return [video(f"filler{i}", debut + timedelta(minutes=17 * i + 3), parole=f"mot{i}a mot{i}b mot{i}c mot{i}d mot{i}e "
                                                                           f"mot{i}f mot{i}g mot{i}h")
            for i in range(n)]


def dit_pareil(n: int, *, ecart_h: float = 4.0, abonnes: dict | None = None, sons: dict | None = None) -> list[dict]:
    out = []
    for i in range(n):
        c = f"relais{i}"
        out.append(video(c, BASE + timedelta(hours=ecart_h * i),
                         parole=f"uu{i}a uu{i}b uu{i}c uu{i}d uu{i}e {PHRASE} vv{i}a vv{i}b vv{i}c vv{i}d vv{i}e",
                         abonnes=(abonnes or {}).get(i, 1000), son=(sons or {}).get(i)))
    return out


def phrase_normalisee() -> str:
    return S._norm(PHRASE)


# --------------------------------------------------------------------------- contrat

def _signal_valide(**ecart):
    base = dict(
        type_="gabarit_commun", titre="Un titre court", hypothese="Une hypothèse au conditionnel.",
        innocent="Une explication innocente précise.", force="faible", premiere=None,
        preuves=[S.PreuveSignal(piece="cnt_1", url="https://x", extrait="e", compte="tiktok:a")],
        question="Qui a fait cela ?", pistes=("a · b · gratuit", "c · d · gratuit"))
    base.update(ecart)
    return S._signal(base.pop("type_"), base.pop("titre"), base.pop("hypothese"), base.pop("innocent"),
                     base.pop("force"), base.pop("premiere"), base.pop("preuves"), base.pop("question"),
                     base.pop("pistes"), **base)


def test_contrat_signal_valide():
    s = _signal_valide()
    assert s.id.startswith("sf-") and len(s.id) == 13
    assert s.pieces == ("cnt_1",) and s.comptes == ("tiktok:a",)


def test_contrat_titre_de_13_mots_refuse():
    with pytest.raises(ValueError, match="12 mots"):
        _signal_valide(titre="un deux trois quatre cinq six sept huit neuf dix onze douze treize")
    _signal_valide(titre="un deux trois quatre cinq six sept huit neuf dix onze douze")


@pytest.mark.parametrize("mot", ["bot", "faux compte", "coordonné", "opération", "propagande", "désinformation",
                                 "manipulation", "ingérence", "complot", "coupable", "fraude", "arnaque",
                                 "escroquerie", "troll", "prouve que"])
def test_contrat_mot_refuse(mot):
    with pytest.raises(ValueError, match="mot refusé"):
        _signal_valide(hypothese=f"Il s'agirait d'un {mot} organisé.")


def test_contrat_coordination_interdite_avant_calibration():
    with pytest.raises(ValueError, match="mot refusé"):
        _signal_valide(innocent="Une coordination entre les comptes.")
    assert S.mots_refuses("des robots qui parlent") == []  # « bot » ne se trouve pas dans « robot »


def test_contrat_force_au_dessus_du_plafond_refusee():
    preuves = [S.PreuveSignal(piece="cnt_1", url="https://x", extrait="e", compte="tiktok:a")]
    for type_ in ("comptes_sans_passe", "cadence_hors_norme", "compte_reveille", "profils_jumeaux",
                  "publications_rapprochees", "identifiant_de_suivi_partage"):
        assert S.plafond(type_) == "moyenne"
        with pytest.raises(ValueError, match="plafonne"):
            _signal_valide(type_=type_, force="forte", preuves=preuves)
    assert S.plafond("commentateurs_recents") == "faible"
    assert S.plafond("formulation_partagee") == "forte"


def test_contrat_explication_pistes_question_preuves():
    with pytest.raises(ValueError, match="innocente"):
        _signal_valide(innocent="  ")
    with pytest.raises(ValueError, match="2 ou 3 pistes"):
        _signal_valide(pistes=("a · b · gratuit",))
    with pytest.raises(ValueError, match="\\?"):
        _signal_valide(question="Qui a fait cela.")
    s = _signal_valide()
    with pytest.raises(ValueError, match="pièce sans preuve"):
        S.SignalFaible(**{**s.__dict__, "pieces": ("cnt_1", "cnt_2")})
    with pytest.raises(ValueError, match="compte sans preuve"):
        S.SignalFaible(**{**s.__dict__, "comptes": ("tiktok:a", "tiktok:b")})


def test_calibres_est_vide_aujourdhui():
    assert S.CALIBRES == frozenset()


# --------------------------------------------------------------------------- horloges

def test_date_video_lue_dans_l_identifiant():
    v = video("a", BASE)
    d, marge, etiquette = S._date_info(v)
    assert d == BASE and marge == 18 and "TikTok" in etiquette
    sans_id = {**v, "platformId": "pas-un-nombre"}
    assert S._date_info(sans_id)[2].startswith("publishedAt")


# --------------------------------------------------------------------------- témoin

def test_mesurer_temoin():
    vs = [video(f"t{i}", BASE + timedelta(days=i), cree=BASE - timedelta(days=400), posts=100 + i, suit=10, bio="ok",
                ref=BASE + timedelta(days=60)) for i in range(10)]
    vs.append(video("neuf", BASE, cree=BASE - timedelta(days=5), posts=1, suit=0, bio=""))
    t = S.mesurer_temoin(vs)
    assert t["comptes_datables"] == 11 and t["comptes_moins_90j"] == 1 and t["comptes_3_videos_ou_moins"] == 1
    assert t["rythme_max"] > t["rythme_median"] > 0
    assert S.TEMOIN_FOOTBALL["comptes_moins_90j"] == 0 and S.TEMOIN_FOOTBALL["comptes_datables"] == 89


# --------------------------------------------------------------------------- D1

def test_d1_declenche_faible_sans_sonde():
    r = S.formulation_partagee(dit_pareil(6))
    assert len(r) == 1 and r[0].force == "faible"
    s = r[0]
    assert s.titre == "6 vidéos, 6 comptes : la même phrase, mot pour mot"
    assert s.mesure("videos") == 6 and s.mesure("comptes") == 6 and s.mesure("grappes") == 6
    assert S.chiffre_moment_fort(s) == "6 vidéos · 6 comptes · 6 grappes indépendantes"
    assert s.premiere_apparition == BASE
    assert "identifiant TikTok" in s.preuves[0].extrait
    assert s.preuves[0].seconde == 3
    assert len(s.mesure("primo_diffuseurs")) == 3


def test_d1_moyenne_et_forte_avec_sonde_de_rarete():
    cle = phrase_normalisee()
    moyenne = S.formulation_partagee(dit_pareil(6, ecart_h=4), rarete={cle: 120})
    assert moyenne[0].force == "forte" or moyenne[0].force == "moyenne"
    # 6 comptes en 20 h, 6 grappes, mais aucun compte de 100 000 abonnés : moyenne
    assert moyenne[0].force == "moyenne"
    forte = S.formulation_partagee(dit_pareil(6, ecart_h=4, abonnes={3: 150_000}), rarete={cle: 120})
    assert forte[0].force == "forte" and forte[0].mesure("avance_s") == 12 * 3600
    trop_courante = S.formulation_partagee(dit_pareil(6, abonnes={3: 150_000}), rarete={cle: 5000})
    assert trop_courante[0].force == "faible"


def test_d1_les_grappes_regroupent_un_meme_son():
    sons = {i: "7000000000000000001" for i in range(6)}
    s = S.formulation_partagee(dit_pareil(6, sons=sons))[0]
    assert s.mesure("grappes") == 1 and s.mesure("comptes") == 6


def test_d1_innocent_phrase_du_temoin_ou_trop_peu_de_comptes():
    frags = {" ".join(f) for f in S._fragments(phrase_normalisee().split(), 7)}
    assert S.formulation_partagee(dit_pareil(6), temoin={"fragments_7": frozenset(frags)}) == []
    assert S.formulation_partagee(dit_pareil(2)) == []
    un_compte = [video("seul", BASE + timedelta(hours=i), parole=f"jour {i} " + PHRASE + f" fin {i}") for i in range(6)]
    assert S.formulation_partagee(un_compte) == []  # une phrase répétée par un seul compte n'est pas une phrase partagée


# --------------------------------------------------------------------------- D2

def _gabarit(n: int, *, ecart_h: float = 1.0, tags=("zeta1", "zeta2", "zeta3", "zeta4"), avec_ecran=True,
             avec_legende=True) -> list[dict]:
    return [video(f"gab{i}", BASE + timedelta(hours=ecart_h * i), hashtags=[f"#{t}" for t in tags],
                  ecran=["SECRET DU MOIS", "ce que personne ne te dit sur ton argent"] if avec_ecran else (),
                  legende="ce que mon banquier ne veut pas que tu saches vraiment sur ton épargne" if avec_legende else "")
            for i in range(n)]


def test_d2_trois_gabarits_en_72h_est_fort():
    r = S.gabarit_commun(_gabarit(5) + remplissage(200))
    assert len(r) == 1 and r[0].force == "forte" and r[0].mesure("gabarits") == 3
    assert r[0].titre == "5 comptes, même texte à l'écran, mêmes hashtags dans le même ordre"


def test_d2_deux_gabarits_moyenne_un_gabarit_faible():
    deux = S.gabarit_commun(_gabarit(5, avec_legende=False) + remplissage(200))
    assert deux[0].force == "moyenne" and deux[0].mesure("gabarits") == 2
    un = S.gabarit_commun(_gabarit(5, avec_ecran=False, avec_legende=False) + remplissage(200))
    assert un[0].force == "faible" and un[0].titre.startswith("5 comptes, mêmes hashtags")
    dix = S.gabarit_commun(_gabarit(10, avec_ecran=False, avec_legende=False) + remplissage(400))
    assert dix[0].force == "moyenne"


def test_d2_innocent_quatre_comptes_ou_hashtags_banals():
    assert S.gabarit_commun(_gabarit(4) + remplissage(200)) == []
    banals = _gabarit(5, tags=("fyp", "viral", "foryou", "pourtoi"), avec_ecran=False, avec_legende=False)
    assert S.gabarit_commun(banals + remplissage(200)) == []
    # sans corpus, les mêmes hashtags sont portés par tout le monde : banals, pas un gabarit
    assert S.gabarit_commun(_gabarit(5, avec_ecran=False, avec_legende=False)) == []


# --------------------------------------------------------------------------- D3

def _son(n_reprises: int, ecart_min: float, *, licence: bool = False, createur_trouve: bool = True) -> list[dict]:
    t_createur = BASE
    t_son = t_createur + timedelta(seconds=20 if createur_trouve else -4000)
    son = tid(t_son, 3)
    vs = [video("createur", t_createur, son=son, licence=licence)]
    vs += [video(f"repreneur{i}", t_createur + timedelta(minutes=30 + ecart_min * i), son=son, licence=licence)
           for i in range(n_reprises)]
    return vs


def test_d3_declenche_selon_la_vitesse():
    forte = S.son_lance_par_peu(_son(10, 10))
    assert len(forte) == 1 and forte[0].force == "forte" and forte[0].premiere_apparition == BASE + timedelta(seconds=20)
    assert forte[0].titre == "Son créé par un compte, repris par 10 comptes en 6 heures"
    moyenne = S.son_lance_par_peu(_son(6, 30))
    assert moyenne[0].force == "moyenne" and moyenne[0].mesure("comptes_6h") >= 5
    faible = S.son_lance_par_peu(_son(6, 400))
    assert faible[0].force == "faible" and faible[0].titre.endswith("72 heures")
    assert faible[0].mesure("delai_1er_au_5e_s") == 4 * 400 * 60


def test_d3_innocent_son_sous_licence_lent_ou_sans_origine():
    assert S.son_lance_par_peu(_son(10, 10, licence=True)) == []
    assert S.son_lance_par_peu(_son(6, 2400)) == []  # 6 comptes en 4 jours
    assert S.son_lance_par_peu(_son(10, 10, createur_trouve=False)) == []  # son plus ancien que toute vidéo : pas de créateur
    assert S.son_lance_par_peu(_son(3, 10)) == []


def test_d3_usages_de_la_sonde_comptent():
    vs = _son(2, 10)
    son = vs[0]["audioPlatformId"]
    usages = {son: {"videos": [video(f"autre{i}", BASE + timedelta(hours=1, minutes=i)) for i in range(6)],
                    "totalCount": 6}}
    r = S.son_lance_par_peu(vs, usages)
    assert r and r[0].mesure("totalCount") == 6 and r[0].mesure("comptes_6h") == 8


# --------------------------------------------------------------------------- D4

def _correspondance(ecart: timedelta, scores=(0.95, 0.95, 0.95), autre_compte: str = "ancien"):
    v = video("diffuseur", BASE, legende="images du séisme de ce matin")
    ancien = video(autre_compte, BASE - ecart, legende="ancien tremblement")
    matches = [[{"id": ancien["id"], "platform": "tiktok", "platformId": ancien["platformId"], "profileHandle": autre_compte,
                 "publishedAt": ancien["publishedAt"], "score": s}] for s in scores]
    return v, ancien, {v["id"]: matches}


def test_d4_forte_moyenne_faible():
    for ecart, force in ((timedelta(days=40), "forte"), (timedelta(days=10), "moyenne"), (timedelta(hours=30), "faible")):
        v, ancien, corr = _correspondance(ecart)
        r = S.sequence_deja_vue([v, ancien], corr)
        assert len(r) == 1 and r[0].force == force
        assert r[0].mesure("images_concordantes") == 3
    v, ancien, corr = _correspondance(timedelta(days=40))
    assert r[0].titre.startswith("Images déjà publiées")
    assert S.sequence_deja_vue([v, ancien], corr)[0].titre == "Images déjà publiées 40 jours plus tôt par un autre compte"


def test_d4_une_date_revendiquee_remplace_la_date_de_publication():
    v, ancien, corr = _correspondance(timedelta(hours=2))
    r = S.sequence_deja_vue([v, ancien], corr, {v["id"]: BASE})
    assert r == []
    revendiquee = S.sequence_deja_vue([v, ancien], corr, {v["id"]: (BASE + timedelta(days=9)).isoformat()})
    assert revendiquee[0].force == "moyenne"


def test_d4_innocent_score_faible_une_image_ou_meme_compte():
    v, ancien, corr = _correspondance(timedelta(days=40), scores=(0.85, 0.85, 0.85))
    assert S.sequence_deja_vue([v, ancien], corr) == []
    v, ancien, corr = _correspondance(timedelta(days=40), scores=(0.95, 0.5, 0.5))
    assert S.sequence_deja_vue([v, ancien], corr) == []
    v, ancien, corr = _correspondance(timedelta(days=40), autre_compte="diffuseur")
    assert S.sequence_deja_vue([v, ancien], corr) == []
    v, ancien, corr = _correspondance(timedelta(hours=5))
    assert S.sequence_deja_vue([v, ancien], corr) == []  # marge de fuseau


# --------------------------------------------------------------------------- D5

def _groupe_comptes(neufs: int, anciens: int) -> list[dict]:
    t = BASE
    vs = [video(f"neuf{i}", t + timedelta(minutes=i), cree=t - timedelta(days=20), posts=1, suit=0, bio="") for i in range(neufs)]
    vs += [video(f"ancien{i}", t + timedelta(minutes=i), cree=t - timedelta(days=900), posts=400, suit=80, bio="ma bio") for i in range(anciens)]
    return vs


def test_d5_declenche_moyenne_plafonnee():
    r = S.comptes_sans_passe(_groupe_comptes(6, 6))
    assert len(r) == 1 and r[0].force == "moyenne"
    assert r[0].titre == "6 des 12 comptes ont moins de 90 jours (ancienneté estimée)"
    assert r[0].mesure("p") < 0.01
    faible = S.comptes_sans_passe(_groupe_comptes(3, 20))
    assert faible[0].force == "faible"


def test_d5_innocent_peu_de_comptes_neufs_ou_profils_anciens():
    assert S.comptes_sans_passe(_groupe_comptes(2, 20)) == []
    assert S.comptes_sans_passe(_groupe_comptes(0, 20)) == []
    # des comptes récents mais avec un profil rempli : un seul point, pas « sans passé »
    remplis = [video(f"recent{i}", BASE, cree=BASE - timedelta(days=20), posts=50, suit=30, bio="une vraie bio") for i in range(8)]
    assert S.comptes_sans_passe(remplis) == []


def test_d5_pseudo_par_defaut_et_ancienneté_non_datable():
    vs = [video(f"user{1234567890123 + i}", BASE, cree=BASE - timedelta(days=30)) for i in range(4)]
    assert S.comptes_sans_passe(vs)[0].force == "faible"  # A + E
    apres = [video(f"user{1234567890123 + i}", BASE, cree=BASE + timedelta(days=30)) for i in range(4)]
    assert S.comptes_sans_passe(apres) == []  # compte « créé » après la vidéo : non datable, un seul point


def test_d5_commentateurs_faible_au_plus_et_sans_noeud():
    vid = video("v", BASE)
    vid["popularComments"] = ([{"profilePlatformId": tid(BASE - timedelta(days=5), i), "publishedAt": iso(BASE),
                                "content": "x"} for i in range(12)]
                              + [{"profilePlatformId": tid(BASE - timedelta(days=800), i), "publishedAt": iso(BASE),
                                  "content": "y"} for i in range(20)])
    r = S.commentateurs_recents([vid])
    assert len(r) == 1 and r[0].force == "faible" and r[0].comptes == ()
    ex = S.pour_export(r)
    assert ex == {"noeuds": [], "liens": [], "questions": []}


# --------------------------------------------------------------------------- D6

def test_d6_une_cadence_de_10_par_jour_est_faible_trois_comptes_moyenne():
    ref = BASE + timedelta(days=100)

    def gros(i, posts):
        return video(f"gros{i}", BASE + timedelta(days=99), cree=BASE, posts=posts, ref=ref)

    un = S.cadence_hors_norme([gros(0, 3000)], reference=ref)
    assert len(un) == 1 and un[0].force == "faible" and un[0].titre == "Un compte publie 30,0 vidéos par jour depuis sa création"
    trois = S.cadence_hors_norme([gros(i, 3000) for i in range(3)], reference=ref)
    assert len(trois) == 1 and trois[0].force == "moyenne" and trois[0].mesure("comptes") == 3


def test_d6_innocent_rythme_normal_ou_compte_trop_jeune():
    ref = BASE + timedelta(days=100)
    normal = video("normal", BASE + timedelta(days=99), cree=BASE, posts=200, ref=ref)
    assert S.cadence_hors_norme([normal], reference=ref) == []
    jeune = video("jeune", BASE + timedelta(days=3), cree=BASE + timedelta(days=1), posts=300, ref=BASE + timedelta(days=4))
    assert S.cadence_hors_norme([jeune]) == []  # moins de 7 jours d'ancienneté


# --------------------------------------------------------------------------- D7

def _reveille(posts: int, recents: int, dossier: int):
    ref = BASE + timedelta(days=120)
    vs = [video("dormeur", ref - timedelta(days=2 * i + 1), cree=ref - timedelta(days=1100), posts=posts, ref=ref)
          for i in range(dossier)]
    return vs, {"tiktok:dormeur": recents}, ref


def test_d7_declenche_faible_puis_moyenne():
    vs, rec, ref = _reveille(posts=40, recents=30, dossier=4)
    r = S.compte_reveille(vs, rec, ref)
    assert len(r) == 1 and r[0].force == "faible" and r[0].titre == "Compte de 3 ans, quasi muet, soudain 30 vidéos en 90 jours"
    vs, rec, ref = _reveille(posts=40, recents=30, dossier=16)
    r = S.compte_reveille(vs, rec, ref)
    assert r[0].force == "moyenne" and r[0].titre == "Compte de 3 ans, quasi muet, soudain 16 vidéos sur ce sujet"


def test_d7_innocent_peu_de_videos_recentes_ou_compte_toujours_actif():
    vs, rec, ref = _reveille(posts=40, recents=3, dossier=3)
    assert S.compte_reveille(vs, rec, ref) == []
    vs, rec, ref = _reveille(posts=3000, recents=30, dossier=16)
    assert S.compte_reveille(vs, rec, ref) == []
    jeune = video("jeune", BASE, cree=BASE - timedelta(days=100), posts=40)
    assert S.compte_reveille([jeune], {"tiktok:jeune": 30}, BASE) == []
    assert S.compte_reveille([jeune], None, BASE) == []


# --------------------------------------------------------------------------- D8

NOMS_JUMEAUX = ["alpha1", "bravo2", "charlie3", "delta4", "echo5"]


def _jumeaux(n: int, *, bio="atelier de contenus pour la bonne cause du mois", nom="Équipe Info", pseudo=None):
    return [video(pseudo.format(i=i) if pseudo else NOMS_JUMEAUX[i], BASE + timedelta(minutes=i), bio=bio, nom=nom)
            for i in range(n)]


def test_d8_declenche_selon_le_nombre_de_traits_et_de_comptes():
    r = S.profils_jumeaux(_jumeaux(3))
    assert len(r) == 1 and r[0].force == "faible"
    assert r[0].titre == "3 profils partagent la même biographie et le même nom affiché"
    assert S.profils_jumeaux(_jumeaux(5))[0].force == "moyenne"
    photos = {f"tiktok:{NOMS_JUMEAUX[i]}": 0xF0F0F0F0F0F0F0F0 ^ (1 << i) for i in range(3)}
    trois_traits = S.profils_jumeaux(_jumeaux(3), photos)
    assert trois_traits[0].force == "moyenne" and trois_traits[0].mesure("traits") == ("bio", "nom", "photo")
    moule = S.profils_jumeaux(_jumeaux(3, pseudo="equipe{i}"))
    assert moule[0].mesure("traits") == ("bio", "nom", "pseudo") and moule[0].force == "moyenne"


def test_d8_innocent_un_seul_trait_ou_deux_comptes():
    seul = _jumeaux(4, nom=None, pseudo="compte_{i}x")
    assert S.profils_jumeaux([{**v, "profileDisplayName": f"Nom {k}"} for k, v in enumerate(seul)]) == []
    assert S.profils_jumeaux(_jumeaux(2)) == []
    photos = {f"tiktok:{NOMS_JUMEAUX[i]}": (0xF0F0F0F0F0F0F0F0 if i == 0 else 0x0F0F0F0F0F0F0F0F) for i in range(3)}
    bios_differentes = [video(f"p{i}ab", BASE, bio=f"bio {i}", nom=f"n{i}x") for i in range(3)]
    assert S.profils_jumeaux(bios_differentes, photos) == []
    assert S.distance_hamming(0b1011, 0b0010) == 2


def test_d8_empreinte_photo_ressemble_a_elle_meme():
    pytest.importorskip("PIL")
    import io

    from PIL import Image

    def png(couleurs):
        im = Image.new("RGB", (32, 32))
        im.putdata([couleurs((i % 32, i // 32)) for i in range(1024)])
        b = io.BytesIO()
        im.save(b, "PNG")
        return b.getvalue()

    a = S.empreinte_photo(png(lambda p: (p[0] * 8, 0, 0)))
    b = S.empreinte_photo(png(lambda p: (p[0] * 8 + 3, 10, 10)))
    c = S.empreinte_photo(png(lambda p: (255 - p[0] * 8, 0, 0)))
    assert S.distance_hamming(a, b) <= 8 < S.distance_hamming(a, c)


# --------------------------------------------------------------------------- D9

def _paire(jours: int, deux_minutes=True, tags=("zorg",), comptes=("alpha", "bravo")) -> tuple[list[dict], dict]:
    """Chaque compte publie à une heure différente chaque jour, mais les comptes publient ensemble."""
    vs = []
    heures = [8, 9, 11, 13, 15, 17, 19, 21][:jours]
    for j, h in enumerate(heures):
        base = BASE.replace(hour=0, minute=0) + timedelta(days=j, hours=h)
        for k, c in enumerate(comptes):
            vs.append(video(c, base + timedelta(seconds=40 * k), hashtags=[f"#{t}" for t in tags]))
    return vs, {"hashtag:zorg": vs}


def test_d9_une_paire_qui_publie_ensemble_huit_fois():
    vs, groupes = _paire(8)
    r = S.publications_rapprochees(vs, groupes)
    assert len(r) == 1 and r[0].force == "faible" and r[0].titre == "2 comptes publient à moins de 5 minutes d'écart, 8 fois"
    assert r[0].mesure("p_max") < 0.01
    assert "identifiant décodé" in r[0].preuves[0].extrait


def test_d9_trois_comptes_relies_deux_a_deux_font_une_moyenne():
    vs, groupes = _paire(8, comptes=("alpha", "bravo", "charlie"))
    r = S.publications_rapprochees(vs, groupes)
    assert len(r) == 1 and r[0].force == "moyenne" and r[0].mesure("comptes") == 3


def test_d9_innocent_peu_de_coincidences_ou_horaire_habituel():
    vs, groupes = _paire(2)
    assert S.publications_rapprochees(vs, groupes) == []  # r = 2
    # deux comptes qui publient tous les jours à 18 h : un horaire habituel, pas un signe de lien
    habitude = []
    for j in range(8):
        t = BASE.replace(hour=18, minute=0) + timedelta(days=j)
        habitude += [video("matin", t), video("soir", t + timedelta(seconds=30))]
    assert S.publications_rapprochees(habitude, {"g": habitude}) == []
    reels = [{**v, "platform": "youtube"} for v in _paire(8)[0]]
    assert S.publications_rapprochees(reels, {"g": reels}) == []  # date YouTube non sûre


# --------------------------------------------------------------------------- D10

def _serie(base=1, jour=20, instagram=0, **extra):
    jours = {(datetime(2026, 8, 1) + timedelta(days=k)).date().isoformat(): base for k in range(28)}
    j0 = (datetime(2026, 8, 1) + timedelta(days=28)).date().isoformat()
    jours[j0] = jour
    auteurs = {"tiktok": jours}
    if instagram:
        auteurs["instagram"] = {j0: instagram}
    return j0, {"sujet": {"type": "phrase", "auteurs": auteurs, "nouveaute": False, **extra}}


def test_d10_rafale_seule_nouveaute_franchissement():
    j0, series = _serie()
    r = S.sujet_qui_monte(series, None)
    assert len(r) == 1 and r[0].force == "faible" and "écho de presse non mesuré" in r[0].preuves[0].extrait
    j0, series = _serie(nouveaute=True)
    assert S.sujet_qui_monte(series, {"sujet": {j0: 1}})[0].force == "moyenne"
    j0, series = _serie(instagram=6)
    fort = S.sujet_qui_monte(series, {"sujet": {j0: 1}})
    assert fort[0].force == "forte" and fort[0].mesure("franchissement") and fort[0].titre.endswith("la presse en parle peu")
    j0, series = _serie(instagram=6)
    zero = S.sujet_qui_monte(series, {"sujet": {}})
    assert zero[0].titre.endswith("la presse n'en parle pas") and zero[0].question_ouverte.startswith("Qu'est-ce qui a déclenché")


def test_d10_innocent_presse_deja_la_petite_hausse_ou_montee_continue():
    j0, series = _serie(instagram=6)
    assert S.sujet_qui_monte(series, {"sujet": {j0: 12}}) == []  # plus de 3 articles : déjà visible
    j0, series = _serie(jour=6)
    assert S.sujet_qui_monte(series, None) == []  # moins de 8 auteurs
    jours = {(datetime(2026, 8, 1) + timedelta(days=k)).date().isoformat(): 30 for k in range(29)}
    assert S.sujet_qui_monte({"x": {"auteurs": {"tiktok": jours}}}, None) == []  # un rythme stable n'est pas une rafale
    # un sujet qui apparaît d'un coup : il faut dire que l'index le couvrait déjà avant (`debut`)
    d = {"2026-10-20": 25}
    assert S.sujet_qui_monte({"x": {"auteurs": {"tiktok": d}}}, None) == []  # historique inconnu : pas de rafale
    neuf = S.sujet_qui_monte({"x": {"auteurs": {"tiktok": d}, "debut": "2026-08-01"}}, None)
    assert neuf and neuf[0].force == "moyenne" and neuf[0].mesure("nouveaute")


# --------------------------------------------------------------------------- pivots (D11 à D15)

def _pivot(type_, valeur, videos_citantes, indices=(), sous_type=None) -> Pivot:
    preuves = tuple(PreuvePivot(compte=f"tiktok:{v['profileHandle']}", champ="caption", extrait=f"voir {valeur}",
                                contenu_id=v["id"], url=f"https://www.tiktok.com/@{v['profileHandle']}/video/{v['platformId']}")
                    for v in videos_citantes)
    return Pivot(type=type_, valeur=valeur, comptes=tuple(sorted({p.compte for p in preuves})), preuves=preuves,
                 indices=list(indices), sous_type=sous_type)


def _rdap(domaine: str, creation: datetime, registrar="Registre X", evenements=None) -> Indice:
    return Indice("rdap", domaine, f"https://rdap.org/domain/{domaine}", BASE, '{"ldhName": "%s"}' % domaine,
                  f"{domaine} créé le {creation:%Y-%m-%d}",
                  {"domaine": domaine, "creation": iso(creation), "registrar": registrar, "dns": [],
                   "evenements": evenements or {"registration": iso(creation)}})


def _crt(domaine: str, premier: datetime, freres=()) -> Indice:
    return Indice("crt.sh", domaine, "https://crt.sh/?q=" + domaine, BASE, "{}", "certificats",
                  {"domaine": domaine, "premier_certificat": iso(premier), "freres": list(freres)})


def test_d11_age_du_domaine():
    v = video("a", BASE)
    for age, force in ((20, "forte"), (100, "faible")):
        p = _pivot("domaine", "offre-speciale.fr", [v], [_rdap("offre-speciale.fr", BASE - timedelta(days=age))])
        r = S.domaine_recent([p], [v])
        assert len(r) == 1 and r[0].force == force
        assert r[0].titre == f"Site enregistré {age} jours avant la première vidéo qui le cite"
        assert "domaine:offre-speciale.fr" in r[0].pieces and r[0].comptes == ("tiktok:a",)
    assert r[0].preuves[0].extrait.endswith("]") and "identifiant TikTok" in r[0].preuves[0].extrait


def test_d11_innocent_domaine_de_deux_ans_ou_creation_apres_la_video():
    v = video("a", BASE)
    vieux = _pivot("domaine", "ancien.fr", [v], [_rdap("ancien.fr", BASE - timedelta(days=730))])
    assert S.domaine_recent([vieux], [v]) == []
    apres = _pivot("domaine", "apres.fr", [v], [_rdap("apres.fr", BASE + timedelta(days=3))])
    assert S.domaine_recent([apres], [v]) == []
    sans_indice = _pivot("domaine", "inconnu.fr", [v])
    assert S.domaine_recent([sans_indice], [v]) == []


def test_d11_hebergeur_partage_utilise_le_premier_certificat():
    v = video("a", BASE)
    p = _pivot("domaine", "boutique.myshopify.com", [v],
               [_rdap("boutique.myshopify.com", BASE - timedelta(days=3000)), _crt("boutique.myshopify.com", BASE - timedelta(days=10))])
    r = S.domaine_recent([p], [v])
    assert r and r[0].force == "forte" and r[0].titre.startswith("Site enregistré 10 jours")


def test_d11_transfert_recent_et_lot_de_domaines():
    v = video("a", BASE)
    transfert = _pivot("domaine", "revendu.fr", [v], [_rdap("revendu.fr", BASE - timedelta(days=900), evenements={
        "registration": iso(BASE - timedelta(days=900)), "transfer": iso(BASE - timedelta(days=40)),
        "last changed": iso(BASE - timedelta(days=1))})])
    r = S.domaine_recent([transfert], [v])
    assert r and r[0].force == "faible" and r[0].titre.startswith("Site transféré 40 jours")
    # « last changed » seul ne compte jamais comme un transfert
    seul = _pivot("domaine", "modifie.fr", [v], [_rdap("modifie.fr", BASE - timedelta(days=900), evenements={
        "registration": iso(BASE - timedelta(days=900)), "last changed": iso(BASE - timedelta(days=5))})])
    assert S.domaine_recent([seul], [v]) == []
    vs = [video(f"c{i}", BASE + timedelta(hours=i)) for i in range(3)]
    lot = [_pivot("domaine", f"promo{i}.fr", [vs[i]], [_rdap(f"promo{i}.fr", BASE - timedelta(days=100, hours=10 * i), registrar="R")])
           for i in range(3)]
    r = S.domaine_recent(lot, vs)
    assert {s.force for s in r} == {"moyenne"} and any("3 domaines du dossier créés à moins de 48 heures" in s.titre for s in r)
    autres_registres = [_pivot("domaine", f"promo{i}.fr", [vs[i]], [_rdap(f"promo{i}.fr", BASE - timedelta(days=100, hours=10 * i),
                                                                       registrar=f"R{i}")]) for i in range(2)]
    assert [s.force for s in S.domaine_recent(autres_registres, vs)] == ["faible", "faible"]


# --------------------------------------------------------------------------- D12

def _page(*ids) -> str:
    return "<html><script>" + " ".join(ids) + "</script></html>"


def test_d12_identifiant_partage():
    pages = {"a.fr": _page("ca-pub-1234567890123456"), "b.fr": _page("ca-pub-1234567890123456"), "c.fr": _page("ca-pub-6543210987654321")}
    r = S.identifiant_de_suivi_partage(pages, [])
    assert len(r) == 1 and r[0].force == "faible"
    assert r[0].titre == "2 sites présentés comme distincts partagent le même identifiant AdSense"
    assert set(r[0].pieces) == {"domaine:a.fr", "domaine:b.fr"}


def test_d12_deux_types_moyenne_plafonnee():
    pages = {d: _page("ca-pub-1234567890123456", "UA-1234567-1" if d == "a.fr" else "UA-1234567-2")
             for d in ("a.fr", "b.fr", "c.fr")}
    r = S.identifiant_de_suivi_partage(pages, [])
    assert r[0].force == "moyenne" and r[0].titre == "3 sites présentés comme distincts partagent 2 types d'identifiants"


def test_d12_registrar_et_creations_proches_valent_un_cran():
    v = video("a", BASE)
    pivots = [_pivot("domaine", d, [v], [_rdap(d, BASE - timedelta(days=30 + k))]) for k, d in enumerate(("a.fr", "b.fr"))]
    pages = {"a.fr": [(BASE, _page("GTM-ABC1234"))], "b.fr": [(BASE - timedelta(days=5), _page("GTM-ABC1234"))]}
    r = S.identifiant_de_suivi_partage(pages, pivots)
    assert r[0].force == "moyenne" and r[0].premiere_apparition == BASE - timedelta(days=5)


def test_d12_innocent_pas_d_identifiant_commun():
    pages = {"a.fr": _page("ca-pub-1234567890123456"), "b.fr": _page("ca-pub-6543210987654321"), "c.fr": "<html></html>"}
    assert S.identifiant_de_suivi_partage(pages, []) == []
    assert S.identifiants_de_suivi(_page("G-ABCDEF1234", "G-STRINGLIKE")) == {("GA4", "G-ABCDEF1234")}


# --------------------------------------------------------------------------- D13

def _societe(siren: str, creation: datetime, adresse="12 rue Neuve", dirigeants=()) -> Indice:
    return Indice("recherche-entreprises", siren, "https://recherche-entreprises.api.gouv.fr/search?q=" + siren, BASE,
                  '{"siren": "%s"}' % siren, f"société {siren}",
                  {"siren": siren, "date_creation": creation.date().isoformat(), "nom": f"Société {siren}",
                   "siege": {"adresse": adresse, "code_postal": "75001"}, "dirigeants": list(dirigeants), "etat": "A"})


def test_d13_ecart_entre_creation_et_premiere_video():
    v = video("a", BASE)
    p = _pivot("siren", "552100554", [v], [_societe("552100554", BASE - timedelta(days=40))])
    r = S.societe_recente([p], [v])
    assert len(r) == 1 and r[0].force == "forte" and r[0].titre == "Société immatriculée 40 jours avant la première vidéo qui la cite"
    assert r[0].pieces[-1] == "siren:552100554"


def test_d13_innocent_vieille_societe_domiciliation_ou_creation_apres():
    v = video("a", BASE)
    vieille = _pivot("siren", "552100554", [v], [_societe("552100554", BASE - timedelta(days=800))])
    assert S.societe_recente([vieille], [v]) == []
    apres = _pivot("siren", "552100554", [v], [_societe("552100554", BASE + timedelta(days=5))])
    assert S.societe_recente([apres], [v]) == []
    domiciliee = _pivot("siren", "552100554", [v], [_societe("552100554", BASE - timedelta(days=40), adresse="5 av X, domiciliation")])
    assert S.societe_recente([domiciliee], [v])[0].force == "moyenne"  # un cran de moins


def test_d13_registre_partage_entre_societes_du_dossier():
    vs = [video(f"c{i}", BASE) for i in range(3)]
    gens = [{"type_dirigeant": "personne morale", "siren": "999888777"}]
    trois = [_pivot("siren", f"55210055{i}", [vs[i]], [_societe(f"55210055{i}", BASE - timedelta(days=200 + 3 * i), dirigeants=gens)])
             for i in range(3)]
    r = S.societe_recente(trois, vs)
    assert {s.force for s in r} == {"moyenne"} and len(r) == 4  # une par société (91-365 j + registre) + le groupe
    deux = S.societe_recente(trois[:2], vs)
    assert [s.force for s in deux if "partagent" in s.titre] == ["faible"]
    loin = [_pivot("siren", "552100550", [vs[0]], [_societe("552100550", BASE - timedelta(days=200), dirigeants=gens)]),
            _pivot("siren", "552100551", [vs[1]], [_societe("552100551", BASE - timedelta(days=100), dirigeants=gens)])]
    assert S.societe_recente(loin, vs) == []  # créations à plus de 30 jours d'écart
    domicilies = [_pivot("siren", f"55210055{i}", [vs[i]], [_societe(f"55210055{i}", BASE - timedelta(days=200), adresse="1 rue Z, centre d'affaires")])
                  for i in range(2)]
    assert S.societe_recente(domicilies, vs) == []  # même adresse de domiciliation à deux : rien


# --------------------------------------------------------------------------- D14

def _code(code: str, n: int, ecart_h=10, mention_pour=()) -> tuple[list[dict], Pivot]:
    vs = []
    for i in range(n):
        legende = "mon code " + code + (" #ad partenariat" if i in mention_pour else "")
        vs.append(video(f"creat{i}", BASE + timedelta(hours=ecart_h * i), legende=legende))
    return vs, _pivot("code_promo", code, vs)


def test_d14_forces_selon_le_nombre_de_comptes():
    for n, force in ((3, "faible"), (5, "moyenne"), (10, "forte")):
        vs, p = _code("ZEBRA20", n)
        r = S.code_promo_en_serie([p], vs)
        assert len(r) == 1 and r[0].force == force and r[0].mesure("comptes") == n
    vs, p = _code("ZEBRA20", 5, mention_pour=(0, 1))
    s = S.code_promo_en_serie([p], vs)[0]
    assert s.mesure("videos_sans_mention_lisible") == 3 and s.titre == "Le même code promo chez 5 créateurs en 2 jours"


def test_d14_innocent_code_generique_code_individuel_ou_hors_fenetre():
    vs, p = _code("BIENVENUE10", 10)
    assert S.code_promo_en_serie([p], vs) == []
    vs, p = _code("ZEBRA20", 2)
    assert S.code_promo_en_serie([p], vs) == []
    vs, p = _code("ZEBRA20", 6, ecart_h=100)  # 6 comptes en 25 jours : 2 seulement dans la première semaine
    assert S.code_promo_en_serie([p], vs) == []


# --------------------------------------------------------------------------- D15

def _releve(premiere: datetime, ofac=False) -> dict:
    return {"type": "portefeuille", "valeur": "x", "url_source": "https://blockstream.info/address/x", "date_lecture": iso(BASE),
            "extrait": "bc1 · 3 transaction(s)", "donnees": {"premiere_activite": iso(premiere),
                                                              "sanction_ofac": {"statut": "oui" if ofac else "non"}}}


ADR1 = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
ADR2 = "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"


def _portefeuille(adresse: str, v: dict) -> Pivot:
    return _pivot("portefeuille", adresse, [v], sous_type="btc")


def test_d15_adresse_neuve_ofac_et_liens():
    v = video("a", BASE)
    p = _portefeuille(ADR1, v)
    neuve = S.portefeuille_neuf([p], [v], {ADR1: _releve(BASE - timedelta(days=3))})
    assert len(neuve) == 1 and neuve[0].force == "faible"
    assert neuve[0].titre == "Adresse de dons créée 3 jours avant le premier appel en vidéo"
    ofac = S.portefeuille_neuf([p], [v], {"profils": {ADR1: _releve(BASE - timedelta(days=900), ofac=True)}})
    assert ofac[0].force == "forte"
    v2 = video("b", BASE)
    deux = [p, _portefeuille(ADR2, v2)]
    profils = {ADR1: _releve(BASE - timedelta(days=900)), ADR2: _releve(BASE - timedelta(days=800))}
    liees = S.portefeuille_neuf(deux, [v, v2], {"profils": profils, "flux": [{"de": ADR1, "vers": ADR2}]})
    assert [s.force for s in liees] == ["moyenne", "moyenne"]


def test_d15_innocent_adresse_ancienne_sans_lien():
    v = video("a", BASE)
    p = _portefeuille(ADR1, v)
    assert S.portefeuille_neuf([p], [v], {ADR1: _releve(BASE - timedelta(days=60))}) == []
    assert S.portefeuille_neuf([p], [v], None) == []
    deux = [p, _portefeuille(ADR2, v)]
    profils = {ADR1: _releve(BASE - timedelta(days=900)), ADR2: _releve(BASE - timedelta(days=800))}
    assert S.portefeuille_neuf(deux, [v], {"profils": profils, "flux": []}) == []


# --------------------------------------------------------------------------- faisceau et export

def _signal_sur(type_: str, compte: str, piece: str, force: str = "faible") -> S.SignalFaible:
    return S._signal(type_, f"Signal {type_}", "Une hypothèse.", "Une explication innocente.", force,
                     BASE, [S.PreuveSignal(piece=piece, url="https://x", extrait=f"preuve {type_}", compte=compte)],
                     "Que s'est-il passé ?", ("a · b · gratuit", "c · d · gratuit"))


def test_faisceau_deux_familles_moyenne_trois_familles_forte():
    a = _signal_sur("formulation_partagee", "tiktok:x", "cnt_1")
    b = _signal_sur("comptes_sans_passe", "tiktok:x", "cnt_2")
    c = _signal_sur("domaine_recent", "tiktok:x", "domaine:z.fr")
    deux = S.faisceau([a, b])
    assert len(deux) == 1 and deux[0].force == "moyenne" and deux[0].mesure("statut") == "faisceau d'indices à recouper"
    assert deux[0].titre == "2 familles d'indices convergent sur le même compte"
    trois = S.faisceau([a, b, c])
    assert trois[0].force == "forte" and trois[0].mesure("statut") == "faisceau d'indices prioritaire"
    assert trois[0].comptes == ("tiktok:x",)


def test_faisceau_deux_detecteurs_d_une_meme_famille_comptent_une_fois():
    a = _signal_sur("formulation_partagee", "tiktok:x", "cnt_1")
    b = _signal_sur("gabarit_commun", "tiktok:x", "cnt_2")
    assert S.faisceau([a, b]) == []
    seul = S.faisceau([a])
    assert seul == []
    autres_comptes = S.faisceau([a, _signal_sur("comptes_sans_passe", "tiktok:y", "cnt_9")])
    assert autres_comptes == []  # aucun objet commun


def test_faisceau_sur_une_piece_commune():
    a = _signal_sur("formulation_partagee", "tiktok:x", "cnt_1")
    b = _signal_sur("son_lance_par_peu", "tiktok:y", "cnt_1")
    c = _signal_sur("cadence_hors_norme", "tiktok:z", "cnt_1")
    r = S.faisceau([a, b, c])
    assert r and r[0].pieces == ("cnt_1",) and r[0].titre == "2 familles d'indices convergent sur la même pièce"


def test_export_noeuds_liens_questions():
    a = _signal_sur("formulation_partagee", "tiktok:x", "cnt_1", "forte")
    b = _signal_sur("comptes_sans_passe", "tiktok:x", "cnt_2", "faible")
    f = S.faisceau([a, b])
    ex = S.pour_export([a, b, *f], lu_le=BASE)
    ids = {n["id"] for n in ex["noeuds"]}
    assert ids == {a.id, b.id, f[0].id}
    noeud = next(n for n in ex["noeuds"] if n["id"] == a.id)
    assert noeud["type"] == "signal_faible" and noeud["role"] == "inconnu" and noeud["solidite"] == Solidite("C", 3)
    assert set(noeud["attributs"]) >= {"type", "famille", "force", "statut", "calibre", "hypothese", "explication_innocente",
                                       "premiere_apparition", "question_ouverte", "pistes", "dossiers_touches"}
    assert noeud["attributs"]["statut"] == "piste à vérifier" and noeud["attributs"]["famille"] == "contenu_dit"
    assert next(n for n in ex["noeuds"] if n["id"] == f[0].id)["attributs"]["statut"] == "faisceau d'indices à recouper"
    for lien in ex["liens"]:
        assert lien["statut"] == "indice" and lien["preuves"] and lien["agent"] == "signaux.py"
        assert lien["force_points"] in (0.5, 1.0, 1.5) and lien["source"] in ids
    types = {(lien["type_lien"], lien["genre_cible"]) for lien in ex["liens"]}
    assert types == {("signale", "piece"), ("concerne", "compte"), ("recoupe", "signal")}
    # le signal faible isolé (b) reste sur sa fiche ; le fort (a) et le faisceau entrent dans la liste
    assert [q["noeud"] for q in ex["questions"]] == [f[0].id, a.id]
    assert all(q["origine"] == "signal faible" and q["role"] == "inconnu" for q in ex["questions"])


def test_export_aucun_lien_sans_preuve_sur_donnees_synthetiques():
    tous = S.detecter(dit_pareil(6) + remplissage(30), [], tirages=50)
    ex = S.pour_export(tous)
    assert ex["noeuds"] and all(lien["preuves"] for lien in ex["liens"])
    cibles = {n["id"] for n in ex["noeuds"]}
    assert all(lien["source"] in cibles for lien in ex["liens"])


# --------------------------------------------------------------------------- orchestration

def test_detecter_lance_tout_et_ajoute_le_faisceau():
    nouveaux = []
    for i, v in enumerate(dit_pareil(6)):
        nouveaux.append({**v, "profilePlatformId": tid(BASE - timedelta(days=10 + i), 5), "profilePostsCount": 1,
                         "profileFollowingCount": 0, "profileBio": ""})
    res = S.detecter(nouveaux, [], tirages=50)
    types = {s.type for s in res}
    assert {"formulation_partagee", "comptes_sans_passe", "faisceau"} <= types
    f = next(s for s in res if s.type == "faisceau")
    assert f.force == "moyenne" and f.mesure("familles") == ("compte", "contenu")
    assert res == S._trier(res)


def test_detecter_innocent_corpus_neutre_ne_leve_rien_de_moyen_ou_fort():
    assert [s for s in S.detecter(remplissage(120), [], tirages=50) if s.force != "faible"] == []


# --------------------------------------------------------------------------- lot 2 (budget simulé)

class FauxBudget:
    def __init__(self, plafond: int = 10_000):
        self.appels: list[dict] = []
        self.depense = 0
        self.plafond = plafond

    def chercher(self, projection: str, **p):
        if self.depense + 10 > self.plafond:
            raise type("PlafondAtteint", (Exception,), {})("plafond atteint")
        self.depense += 10
        self.appels.append({"projection": projection, **p})
        filtres = p.get("filtres") or {}
        if "transcript" in filtres:
            return {"data": {"results": []}, "metadata": {"pagination": {"totalCount": 42}}}
        if "audioPlatformId" in filtres:
            return {"data": {"results": [video("autre", BASE)]}, "metadata": {"pagination": {"totalCount": 1}}}
        return {"data": {"results": []}, "metadata": {"pagination": {"totalCount": 7}}}


def test_collecter_pour_signaux_fait_les_sondes_du_lot_2():
    vs = dit_pareil(6, sons={i: "7000000000000000009" for i in range(6)})
    b = FauxBudget()
    sortie = S.collecter_pour_signaux(b, object(), vs, [])
    assert sortie["rarete"] == {phrase_normalisee(): 42}
    assert sortie["usages"]["7000000000000000009"]["totalCount"] == 1
    assert all(a["projection"] == "basic" for a in b.appels) and sortie["erreurs"] == []
    avec = S.detecter(vs, [], rarete=sortie["rarete"], usages=sortie["usages"], tirages=50)
    assert next(s for s in avec if s.type == "formulation_partagee").force in ("faible", "moyenne")


def test_collecter_s_arrete_au_plafond():
    vs = dit_pareil(6)
    sortie = S.collecter_pour_signaux(FauxBudget(plafond=0), object(), vs, [])
    assert sortie["rarete"] == {} and any("PlafondAtteint" in e for e in sortie["erreurs"])
