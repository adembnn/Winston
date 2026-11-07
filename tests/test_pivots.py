"""Tests des pivots : données fictives au format réel d'Oriane, aucun appel réseau."""

import hashlib
import math

from enquete.pivots import (
    bech32_valide,
    evm_valide,
    extraire_pivots,
    keccak256,
    luhn_valide,
    pivots_pour_agents,
    poids_rarete,
    relier_par_pivots,
    siren_valide,
    siret_valide,
    trouver_pivots_dans_texte,
)

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58check(version: int, charge: bytes) -> str:
    brut = bytes([version]) + charge
    brut += hashlib.sha256(hashlib.sha256(brut).digest()).digest()[:4]
    n = int.from_bytes(brut, "big")
    texte = ""
    while n:
        n, r = divmod(n, 58)
        texte = B58[r] + texte
    return "1" * (len(brut) - len(brut.lstrip(b"\x00"))) + texte


def avec_cle_luhn(base: str) -> str:
    for c in range(10):
        if luhn_valide(base + str(c)):
            return base + str(c)
    raise AssertionError


SIREN = avec_cle_luhn("55210055")  # 9 chiffres
SIRET = avec_cle_luhn(SIREN + "0001")  # 14 chiffres
BTC = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"
BTC_BECH32 = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
EVM_EIP55 = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
TRON = base58check(0x41, bytes(range(1, 21)))
SOLANA = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


def contenu(cid, pseudo, plateforme="tiktok", **champs):
    base = {"id": cid, "platform": plateforme, "profileHandle": pseudo, "platformId": "7698316626466311456",
            "platformType": "video", "caption": None, "hashtags": [], "mentions": [], "media": []}
    base.update(champs)
    return base


def trouves(texte):
    return {(t, v) for t, v, _d, _f in trouver_pivots_dans_texte(texte)}


# ------------------------------------------------------------- contrôles

def test_luhn_siren_siret():
    assert siren_valide(SIREN) and not siren_valide(SIREN[:-1] + str((int(SIREN[-1]) + 1) % 10))
    assert siret_valide(SIRET) and not siret_valide(SIRET[:-1] + str((int(SIRET[-1]) + 1) % 10))
    assert not siren_valide("12345678")


def test_siren_exige_un_contexte_et_un_luhn_valide():
    espace = f"{SIREN[:3]} {SIREN[3:6]} {SIREN[6:]}"
    assert ("siren", SIREN) in trouves(f"Mentions légales : SAS EXEMPLE, SIREN {espace}")
    assert ("siren", SIREN) not in trouves(f"Le numéro gagnant est {espace}")  # pas de contexte
    assert not [t for t in trouves("SIREN 123 456 789") if t[0] == "siren"]  # Luhn faux
    r = trouves(f"RCS Paris {SIRET}")
    assert ("siret", SIRET) in r and ("siren", SIREN) in r


def test_keccak_et_eip55():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert evm_valide(EVM_EIP55)
    assert not evm_valide(EVM_EIP55.replace("Aed", "AEd"))
    assert evm_valide("0x" + "ab" * 20)  # casse uniforme : pas de somme de contrôle


def test_bech32_et_bech32m():
    assert bech32_valide(BTC_BECH32) == "bitcoin"
    assert bech32_valide(BTC_BECH32.upper()) == "bitcoin"
    assert bech32_valide("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0") == "bitcoin"
    assert bech32_valide(BTC_BECH32[:-1] + "p") is None


# ---------------------------------------------------------- portefeuilles

def test_portefeuilles_valides_par_chaine():
    texte = (f"BTC {BTC} / {BTC_BECH32} ETH {EVM_EIP55} TRX {TRON} "
             f"LTC LbTjMGN7gELw4KbeyQf6cTCq859hD18guE DOGE DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L")
    r = trouves(texte)
    assert ("portefeuille", f"bitcoin|{BTC}") in r
    assert ("portefeuille", f"bitcoin|{BTC_BECH32}") in r
    assert ("portefeuille", f"evm|{EVM_EIP55.lower()}") in r
    assert ("portefeuille", f"tron|{TRON}") in r
    assert ("portefeuille", "litecoin|LbTjMGN7gELw4KbeyQf6cTCq859hD18guE") in r
    assert ("portefeuille", "dogecoin|DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L") in r


def test_adresses_invalides_refusees():
    faux_btc = BTC[:-1] + ("3" if BTC[-1] != "3" else "4")
    assert not [t for t in trouves(f"adresse {faux_btc}") if t[0] == "portefeuille"]
    assert not [t for t in trouves("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAEd") if t[0] == "portefeuille"]


def test_solana_demande_un_contexte():
    assert ("portefeuille", f"solana|{SOLANA}") in trouves(f"Don en Solana : {SOLANA}")
    assert not [t for t in trouves(f"identifiant technique {SOLANA}") if t[0] == "portefeuille"]


def test_portefeuille_en_uri_de_qr():
    assert ("portefeuille", f"bitcoin|{BTC_BECH32}") in trouves(f"bitcoin:{BTC_BECH32}?amount=0.01")


def test_iban_jamais_extrait_et_masque_dans_l_extrait():
    iban = "FR76 3000 6000 0112 3456 7890 189"
    res = [contenu("c1", "a", caption=f"Virement {iban} puis code PROMOX1 merci")]
    (p,) = [p for p in extraire_pivots(res) if p.type == "code_promo"]
    assert iban not in p.preuves[0].extrait and "[masqué]" in p.preuves[0].extrait
    assert not [t for t in trouves(iban) if t[0] not in ()]


# --------------------------------------------------------------- paiements

def test_liens_de_paiement_avec_beneficiaire():
    r = trouves("https://paypal.me/Alice https://ko-fi.com/bob https://www.helloasso.com/associations/mon-asso/collectes/x "
                "https://www.gofundme.com/f/aide-pour-x https://revolut.me/chloe https://www.tipeee.com/dan "
                "https://www.patreon.com/eve https://buymeacoffee.com/fay https://buy.stripe.com/AbC123 "
                "https://www.leetchi.com/fr/c/pour-gabi https://pots.lydia-app.com/collect/XYZ")
    assert ("paiement", "paypal.me|alice") in r
    assert ("paiement", "ko-fi|bob") in r
    assert ("paiement", "helloasso|mon-asso") in r
    assert ("paiement", "gofundme|aide-pour-x") in r
    assert ("paiement", "revolut.me|chloe") in r
    assert ("paiement", "tipeee|dan") in r
    assert ("paiement", "patreon|eve") in r
    assert ("paiement", "buymeacoffee|fay") in r
    assert ("paiement", "stripe|AbC123") in r
    assert ("paiement", "leetchi|pour-gabi") in r
    assert ("paiement", "lydia|XYZ") in r


def test_lien_de_paiement_generique_sans_beneficiaire_ignore():
    assert not [t for t in trouves("https://www.paypal.com/donate https://paypal.me") if t[0] in ("paiement", "domaine")]


# ------------------------------------------------- pistes désactivées

def test_jamais_d_email_telephone_ni_whatsapp():
    texte = ("Écris à jean.dupont@gmail.com ou au 06 12 34 56 78, +33 6 12 34 56 78, "
             "wa.me/33612345678, https://api.whatsapp.com/send?phone=33612345678, tel 0033612345678")
    assert trouves(texte) == set()
    res = [contenu("c1", "a", caption=texte + " code GRATUIT10 #promo")]
    for p in extraire_pivots(res):
        for pr in p.preuves:
            assert "@gmail" not in pr.extrait and "06 12 34" not in pr.extrait and "wa.me" not in pr.extrait
    assert not any(p.valeur.endswith("gmail.com") for p in extraire_pivots(res))


def test_profil_public_email_jamais_lu():
    profil = {"platform": "tiktok", "handle": "a", "bio": "rien", "publicEmail": "x@exemple.test"}
    assert extraire_pivots([contenu("c1", "a")], [profil]) == []


# -------------------------------------------------------- URL, domaines

def test_domaines_urls_et_depliage_des_services_de_lien():
    res = [contenu("c1", "a", caption="Ma boutique : https://www.Boutique-Exemple.fr/promo?utm_source=ig&ref=ami77 "
                                      "et aussi exemple-site.com. Mon canal t.me/CanalExemple")]
    p = {(x.type, x.valeur): x for x in extraire_pivots(res)}
    assert ("domaine", "boutique-exemple.fr") in p
    assert ("url", "boutique-exemple.fr/promo?ref=ami77") in p  # suivi retiré, affiliation gardée
    assert ("affiliation", "boutique-exemple.fr:ref=ami77") in p
    assert ("domaine", "exemple-site.com") in p
    assert ("telegram", "t.me/canalexemple") in p


def test_page_de_liens_a_deplier_puis_depliee():
    profil = {"platform": "instagram", "handle": "a", "bio": "x", "bioLink": "https://linktr.ee/Alice"}
    (p,) = [x for x in extraire_pivots([], [profil]) if x.type == "page_de_liens"]
    assert p.valeur == "linktr.ee/alice" and p.a_deplier
    deplie = extraire_pivots([], [profil], depliages={"linktr.ee/alice": ["https://shop-exemple.fr/x", "https://paypal.me/Alice"]})
    types = {(x.type, x.valeur) for x in deplie}
    assert ("domaine", "shop-exemple.fr") in types and ("paiement", "alice") in types
    dep = next(x for x in deplie if x.valeur == "shop-exemple.fr")
    assert dep.preuves[0].via == "linktr.ee/alice" and dep.preuves[0].champ == "bioLink"


def test_plateformes_et_raccourcisseurs_pas_de_domaine():
    types = trouves("https://www.youtube.com/watch?v=abc https://bit.ly/xyz https://www.instagram.com/someone/")
    assert not [t for t in types if t[0] == "domaine"]
    assert ("pseudo_cite", "someone") in types


def test_hebergeur_partage_identifie_par_sous_domaine():
    assert ("domaine", "monsite.carrd.co") in trouves("https://monsite.carrd.co/accueil")
    assert ("boutique", "ma-boutique.myshopify.com") in trouves("https://ma-boutique.myshopify.com/products/a")


def test_redirecteur_instagram_deplie():
    assert ("domaine", "cible-exemple.fr") in trouves("https://l.instagram.com/?u=https%3A%2F%2Fcible-exemple.fr%2Fx&e=1")


# --------------------------------------------------------------- codes

def test_codes_promo():
    r = trouves("Utilise le code NUTS10 ! Aussi -20 % avec le code FETE20 et 15% off with SOLDES15. Code de la route")
    assert {("code_promo", "NUTS10"), ("code_promo", "FETE20"), ("code_promo", "SOLDES15")} <= r
    assert not [t for t in trouves("Le code de la route et le code PIN") if t[0] == "code_promo"]


def test_preuve_avec_champ_extrait_et_seconde_de_transcription():
    media = [{"type": "video", "transcript": "bla. Utilise le code NUTS10 maintenant.",
              "transcriptChunks": [{"startSeconds": 0, "endSeconds": 2, "text": "bla."},
                                   {"startSeconds": 12.5, "endSeconds": 15, "text": "Utilise le code NUTS10 maintenant."}]}]
    res = [contenu("cnt_1", "a", plateforme="youtube", platformId="dQw4w9WgXcQ", platformType="video", media=media)]
    (p,) = [x for x in extraire_pivots(res) if x.type == "code_promo"]
    pr = p.preuves[0]
    assert (pr.contenu_id, pr.champ, pr.debut_s, pr.fin_s) == ("cnt_1", "transcript", 12.5, 15)
    assert pr.extrait == "Utilise le code NUTS10 maintenant."
    assert pr.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=12s"
    preuve = pr.vers_preuve()
    assert preuve.debut_s == 12.5 and preuve.extrait == pr.extrait


def test_extrait_exact_dans_le_champ_source():
    legende = "Toute la gamme sur https://boutique-exemple.fr avec le code REMISE5, merci à tous " * 3
    (p,) = [x for x in extraire_pivots([contenu("c1", "a", caption=legende)]) if x.type == "domaine"]
    assert p.preuves[0].extrait in legende and p.preuves[0].champ == "caption"


# ----------------------------------------- pseudos, hashtags, son, structure

def test_pseudos_hashtags_son_et_champs_structures():
    res = [contenu("c1", "alice", caption="Merci @Bob et @alice ! #Promo", hashtags=["#Promo", "#autre"],
                   mentions=[{"profileHandle": "carol"}], audioPlatformId="555", audioTitle="Original audio",
                   audioAuthor="dan")]
    p = {(x.type, x.valeur) for x in extraire_pivots(res)}
    assert ("pseudo_cite", "bob") in p and ("pseudo_cite", "carol") in p
    assert ("pseudo_cite", "alice") not in p  # auto-citation écartée
    assert ("hashtag", "promo") in p and ("hashtag", "autre") in p
    assert ("son", "tiktok:555") in p


def test_pseudo_reutilise_sur_deux_plateformes():
    res = [contenu("c1", "meme_pseudo", plateforme="tiktok"), contenu("c2", "meme_pseudo", plateforme="instagram")]
    (p,) = [x for x in extraire_pivots(res) if x.type == "pseudo_reutilise"]
    assert p.comptes == ("instagram:meme_pseudo", "tiktok:meme_pseudo")


def test_doublons_de_resultats_comptes_une_fois():
    c = contenu("c1", "a", caption="code ABC123")
    (p,) = [x for x in extraire_pivots([c, dict(c)]) if x.type == "code_promo"]
    assert len(p.preuves) == 1


# ---------------------------------------------------------------- QR

def test_qr_donne_les_memes_pivots_avec_la_seconde():
    res = [contenu("cnt_1", "a", plateforme="youtube", platformId="dQw4w9WgXcQ")]
    qr = [{"video_id": "cnt_1", "seconde": 7, "contenu": f"bitcoin:{BTC_BECH32}?amount=1"},
          {"video_id": "dQw4w9WgXcQ", "seconde": 9, "contenu": "https://paypal.me/Alice"},
          {"video_id": "inconnue", "seconde": 1, "contenu": "https://x.example.com/"}]
    p = {(x.type, x.valeur): x for x in extraire_pivots(res, qr=qr)}
    wallet = p[("portefeuille", BTC_BECH32)]
    assert wallet.sous_type == "bitcoin"
    assert wallet.preuves[0].champ == "qr" and wallet.preuves[0].debut_s == 7
    assert wallet.preuves[0].url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=7s"
    assert p[("paiement", "alice")].sous_type == "paypal.me"
    assert ("qr", "paypal.me/Alice") in p
    assert not any(x.valeur == "x.example.com" for x in p.values())  # vidéo absente : ignoré


def test_meme_qr_sur_deux_comptes_lien_candidat():
    res = [contenu("c1", "a"), contenu("c2", "b")]
    qr = [{"video_id": "c1", "seconde": 3, "contenu": "https://pay.example.org/x"},
          {"video_id": "c2", "seconde": 4, "contenu": "https://pay.example.org/x"}]
    liens = relier_par_pivots(extraire_pivots(res, qr=qr))
    assert any(lien.pivot_type == "qr" for lien in liens)


# ------------------------------------------------------ rareté et liens

def test_poids_rarete_formule():
    assert poids_rarete(1, 100) == round(-math.log(2 / 101), 3)
    assert poids_rarete(0, 0) == 0
    assert poids_rarete(1, 1000) > poids_rarete(5, 1000) > poids_rarete(500, 1000)


def corpus_partage(n_comptes_total=40):
    res = [contenu(f"c{i}", f"compte{i}", caption=f"salut #banal{' #rare' if i < 2 else ''}") for i in range(n_comptes_total)]
    res[0]["caption"] += f" code SECRET77 {EVM_EIP55} https://paypal.me/Alice"
    res[1]["caption"] += f" code SECRET77 {EVM_EIP55} https://paypal.me/Alice"
    return res


def test_banal_si_repandu_et_rare_sinon():
    p = {(x.type, x.valeur): x for x in extraire_pivots(corpus_partage())}
    assert p[("hashtag", "banal")].banal and not p[("hashtag", "rare")].banal
    assert p[("hashtag", "banal")].poids_rarete < p[("hashtag", "rare")].poids_rarete
    assert not p[("code_promo", "SECRET77")].banal
    assert p[("hashtag", "fyp")].banal if ("hashtag", "fyp") in p else True


def test_hashtag_de_liste_banale_marque_banal_meme_rare():
    p = extraire_pivots([contenu("c1", "a", caption="#fyp #pourtoi")])
    assert all(x.banal for x in p)


def test_portefeuille_de_plateforme_banal():
    res = [contenu("c1", "a", caption="don 34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo")]
    (p,) = [x for x in extraire_pivots(res) if x.type == "portefeuille"]
    assert p.banal


def test_liens_candidats_deux_comptes_distincts_seulement():
    pivots = extraire_pivots(corpus_partage())
    liens = relier_par_pivots(pivots)
    par = {(lien.pivot_type, lien.valeur): lien for lien in liens}
    code = par[("code_promo", "SECRET77")]
    assert code.type == "même argent" and code.statut == "piste" and len(code.comptes) == 2
    assert code.preuves and code.poids == poids_rarete(2, 40)
    assert code.explication_innocente
    wallet = par[("portefeuille", EVM_EIP55.lower())]
    assert wallet.fort and wallet.sous_type == "evm" and wallet.type == "même argent"
    assert par[("paiement", "alice")].fort
    assert not par[("code_promo", "SECRET77")].fort
    assert liens[0].fort  # les liens forts passent en premier parmi les non banals
    assert liens[-1].banal


def test_un_seul_compte_ne_fait_pas_de_lien():
    res = [contenu(f"c{i}", "seul", caption="code SECRET77") for i in range(5)]
    assert relier_par_pivots(extraire_pivots(res)) == []


def test_meme_pseudo_de_service_different_pas_confondu():
    res = [contenu("c1", "a", caption="https://paypal.me/zoe"), contenu("c2", "b", caption="https://ko-fi.com/zoe")]
    assert relier_par_pivots([p for p in extraire_pivots(res) if p.type == "paiement"]) == []


def test_pivots_pour_agents_cles_et_ordre():
    sortie = pivots_pour_agents(extraire_pivots(corpus_partage()))
    assert set(sortie[0]) >= {"type", "valeur", "poids_rarete", "banal", "comptes", "preuves", "indices"}
    portefeuille = next(x for x in sortie if x["type"] == "portefeuille")
    assert portefeuille["indices"] == [] and portefeuille["sous_type"] == "evm"
    assert [x["banal"] for x in sortie] == sorted(x["banal"] for x in sortie)  # banals à la fin
    assert all(x["banal"] is False for x in pivots_pour_agents(extraire_pivots(corpus_partage()), inclure_banals=False))
    partages = pivots_pour_agents(extraire_pivots(corpus_partage()), seulement_partages=True)
    assert all(len(x["comptes"]) >= 2 for x in partages)
    assert len(pivots_pour_agents(extraire_pivots(corpus_partage()), limite=3)) == 3
    assert {"contenu_id", "champ", "extrait", "debut_s"} <= set(portefeuille["preuves"][0])
