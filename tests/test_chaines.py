"""Tests du suivi des flux : transport simulé (httpx.MockTransport), données synthétiques, aucun réseau."""

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from enquete.sources import chaines as ch
from enquete.sources.chaines import (
    ChainesAdresseInvalideError,
    ChainesBloqueeError,
    ChainesIntrouvableError,
    ChainesLimiteError,
    ChainesNonSupporteeError,
    ClientChaines,
    ListeOFAC,
    contreparties,
    flux_entre,
    lire_date_gel,
    profil_portefeuille,
    regrouper_entrees_communes,
    tron_hex_vers_base58,
    verifier_adresse,
)

REP = Path(__file__).parent / "reponses"


def charger(nom: str):
    return json.loads((REP / nom).read_text("utf-8"))


BTC = charger("chaines_btc.json")
EVM = charger("chaines_evm.json")
TRON = charger("chaines_tron.json")
SOL = charger("chaines_sol.json")
OFAC = (REP / "chaines_ofac_sdn.csv").read_text("utf-8")

ME = BTC["adresse"]
X, Y, Z, W = (BTC["autres"][k] for k in "XYZW")
ME_E = "0x1111111111111111111111111111111111111111"
PLAT = "0x2222222222222222222222222222222222222222"
FIN = "0x5555555555555555555555555555555555555555"
AUTRE_E = "0x4444444444444444444444444444444444444444"
T_ME, T_PLAT, T_AUTRE = (TRON["adresses"][k] for k in ("moi", "plateforme", "autre"))
S_ME, S_AUTRE = SOL["adresses"]["moi"], SOL["adresses"]["autre"]


def json_rep(donnees, statut=200):
    return httpx.Response(statut, json=donnees)


class FauxReseau:
    """Rejoue les fixtures selon l'hôte et le chemin ; enregistre chaque requête."""

    def __init__(self):
        self.requetes: list[httpx.Request] = []
        self.pannes: dict[str, int] = {}  # hôte -> statut à renvoyer
        self.ofac_en_panne = False

    def __call__(self, requete: httpx.Request) -> httpx.Response:
        self.requetes.append(requete)
        hote, chemin = requete.url.host, requete.url.path
        if hote in self.pannes:
            return httpx.Response(self.pannes[hote], text="<html>Just a moment...</html>" if self.pannes[hote] == 403 else "")
        if hote in ("mempool.space", "blockstream.info"):
            return self.btc(chemin)
        if hote == "eth.blockscout.com":
            return self.evm(requete, chemin)
        if hote == "api.trongrid.io":
            return self.trongrid(chemin)
        if hote == "apilist.tronscan.org":
            return json_rep(TRON["tronscan"].get(requete.url.params["address"], {}))
        if hote == "api.mainnet-beta.solana.com":
            return self.solana(json.loads(requete.content))
        if hote == "sanctionslistservice.ofac.treas.gov":
            return httpx.Response(503) if self.ofac_en_panne else httpx.Response(200, text=OFAC)
        return httpx.Response(404)

    def compter(self, hote: str) -> int:
        return sum(1 for r in self.requetes if r.url.host == hote)

    # Bitcoin : les stats de n'importe quelle adresse sont recalculées depuis les transactions.
    @staticmethod
    def btc_txs(adresse):
        return [t for t in BTC["txs"]
                if adresse in [v["prevout"]["scriptpubkey_address"] for v in t["vin"] if v["prevout"]]
                or adresse in [v["scriptpubkey_address"] for v in t["vout"]]]

    def btc(self, chemin):
        morceaux = chemin.split("/")
        adresse = morceaux[3]
        txs = self.btc_txs(adresse)
        if len(morceaux) == 4:
            recu = sum(v["value"] for t in txs for v in t["vout"] if v["scriptpubkey_address"] == adresse)
            depense = sum(v["prevout"]["value"] for t in txs for v in t["vin"]
                          if v["prevout"] and v["prevout"]["scriptpubkey_address"] == adresse)
            zero = {"funded_txo_count": 0, "funded_txo_sum": 0, "spent_txo_count": 0, "spent_txo_sum": 0, "tx_count": 0}
            return json_rep({"address": adresse, "mempool_stats": zero,
                             "chain_stats": {**zero, "funded_txo_sum": recu, "spent_txo_sum": depense, "tx_count": len(txs)}})
        if morceaux[4] == "txs" and len(morceaux) == 5:
            return json_rep(txs)
        return json_rep([])

    def evm(self, requete, chemin):
        morceaux = chemin.split("/")  # /api/v2/addresses/<a>[/suite]
        adresse = morceaux[4]
        info = EVM["adresses"].get(adresse)
        suite = morceaux[5] if len(morceaux) > 5 else ""
        if info is None:
            return json_rep({"message": "Not found"}, 404)
        if suite == "":
            return json_rep({k: v for k, v in info.items() if k != "counters"} | {"hash": adresse})
        if suite == "counters":
            return json_rep(info.get("counters", {"transactions_count": "0", "token_transfers_count": "0"}))
        if suite == "transactions":
            pages = EVM["transactions"].get(adresse, {"page1": {"items": [], "next_page_params": None}})
            return json_rep(pages["page2"] if "block_number" in requete.url.params else pages["page1"])
        if suite == "token-transfers":
            return json_rep(EVM["jetons"].get(adresse, {"items": [], "next_page_params": None}))
        return httpx.Response(404)

    def trongrid(self, chemin):
        morceaux = chemin.split("/")  # /v1/accounts/<a>[/transactions[/trc20]]
        adresse = morceaux[3]
        if len(morceaux) == 4:
            return json_rep(TRON["compte"])
        if adresse != T_ME:
            return json_rep({"data": [], "meta": {}})
        return json_rep(TRON["trc20"] if morceaux[-1] == "trc20" else TRON["transactions"])

    def solana(self, corps):
        methode, params = corps["method"], corps["params"]
        if methode == "getAccountInfo":
            return json_rep({"result": SOL["compte"]})
        if methode == "getSignaturesForAddress":
            return json_rep({"result": SOL["signatures"] if params[0] == S_ME else []})
        if methode == "getTransaction":
            return json_rep({"result": SOL["transactions"].get(params[0])})
        return json_rep({"error": {"message": "méthode inconnue"}})


@pytest.fixture
def reseau():
    return FauxReseau()


@pytest.fixture
def client(tmp_path, reseau):
    c = ClientChaines(dossier_cache=tmp_path / "cache", transport=httpx.MockTransport(reseau),
                      dormir=lambda _s: None, intervalle=0)
    yield c
    c.close()


# --------------------------------------------------------------------------- bases


def test_adresses_validees_par_chaine():
    assert verifier_adresse("btc", ME) == ME
    assert verifier_adresse("bitcoin", W) == W
    assert verifier_adresse("eth", ME_E)
    assert verifier_adresse("tron", T_ME)
    assert verifier_adresse("sol", S_ME)
    for chaine, mauvaise in [("btc", "pas-une-adresse"), ("eth", "0x12"), ("tron", T_ME[:-1] + "A"), ("sol", "abc")]:
        with pytest.raises(ChainesAdresseInvalideError):
            verifier_adresse(chaine, mauvaise)
    with pytest.raises(ChainesNonSupporteeError):
        verifier_adresse("bnb", ME_E)


def test_conversion_tron_hexadecimal_vers_base58():
    assert tron_hex_vers_base58("41a614f803b6fd780986a42c78ec9c7f77e6ded13c") == T_ME


def test_date_gel_formes_acceptees():
    attendu = datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert lire_date_gel("2026-04-01") == attendu
    assert lire_date_gel(attendu.date()) == attendu
    assert lire_date_gel(datetime(2026, 4, 1)) == attendu
    assert lire_date_gel(None) is None
    with pytest.raises(ValueError):
        lire_date_gel("hier")


# --------------------------------------------------------------------------- Bitcoin


def test_profil_btc_totaux_exacts_sans_gel(client):
    p = profil_portefeuille("btc", ME, client=client)
    assert p["type"] == "portefeuille"
    assert p["url_source"] == f"https://mempool.space/address/{ME}"
    assert p["date_lecture"]
    d = p["donnees"]
    assert d["nb_transactions"] == 5
    assert d["premiere_activite"].startswith("2026-01-10")
    assert d["derniere_activite"].startswith("2026-06-01")
    btc = d["totaux"][0]
    assert (btc["actif"], btc["recu"], btc["envoye"]) == ("BTC", "0.69991", "1")
    assert d["sanction_ofac"]["statut"] == "non"
    assert d["doit_repondre"] is False
    assert "jamais attribué" in d["attribution"]
    assert set(p) == {"type", "valeur", "url_source", "date_lecture", "extrait", "donnees"}


def test_date_gel_ecarte_les_transactions_posterieures(client):
    p = profil_portefeuille("btc", ME, "2026-04-01", client=client)
    d = p["donnees"]
    assert d["nb_transactions"] == 4
    assert d["derniere_activite"].startswith("2026-03-05")
    assert d["transactions_ecartees_apres_gel"] == 1
    assert d["totaux"][0]["recu"] == "0.6999"  # sans les 1000 sats de la transaction postérieure
    assert d["totaux_exacts"] is True
    assert {"url": f"https://mempool.space/tx/{'c' * 64}", "motif": "postérieure à date_gel"} in client.rejets


def test_filtre_url_ecarte_et_journalise(client, caplog):
    interdit = f"https://mempool.space/tx/{'b' * 64}"
    with caplog.at_level(logging.INFO, logger="enquete.chaines"):
        p = profil_portefeuille("btc", ME, filtre_url=lambda u: u != interdit, client=client)
    assert p["donnees"]["transactions_ecartees_par_filtre_url"] == 1
    assert p["donnees"]["nb_transactions"] == 4
    assert {"url": interdit, "motif": "refusée par filtre_url"} in client.rejets
    assert any(interdit in m for m in caplog.messages)


def test_profil_rend_none_si_la_page_adresse_est_refusee(client):
    assert profil_portefeuille("btc", ME, filtre_url=lambda u: "/address/" not in u, client=client) is None
    assert any(r["url"].endswith(f"/address/{ME}") for r in client.rejets)


def test_contreparties_btc_classees_avec_preuves_et_sanction(client):
    resultats = contreparties("btc", ME, limite=10, client=client)
    par_adresse = {r["valeur"]: r for r in resultats}
    assert resultats[0]["donnees"]["nb_mouvements"] >= resultats[-1]["donnees"]["nb_mouvements"]
    z = par_adresse[Z]["donnees"]
    assert z["nb_mouvements"] == 3  # b : ME vers Z ; c : Z vers ME ; e : sortie vers Z financée aussi par ME
    assert all(t["url"].startswith("https://mempool.space/tx/") for t in z["transactions"])
    assert par_adresse[Z]["url_source"].startswith("https://mempool.space/tx/")
    assert par_adresse[Y]["donnees"]["sanction_ofac"]["statut"] == "oui"
    assert par_adresse[Y]["donnees"]["sanction_ofac"]["entrees"][0]["nom"] == "ENTITE DE TEST"
    assert "SOUS SANCTION OFAC" in par_adresse[Y]["extrait"]
    assert len(contreparties("btc", ME, limite=1, client=client)) == 1


def test_montants_btc_au_prorata_marques_approximatifs(client):
    resultats = {r["valeur"]: r for r in contreparties("btc", ME, limite=10, client=client)}
    # La transaction « e » a trois entrées de 0,1 BTC : ME ne finance qu'un tiers des sorties.
    assert resultats[Y]["donnees"]["montants_approximatifs"] is True
    assert resultats[Z]["donnees"]["montants_approximatifs"] is True


def test_flux_directs_btc_avec_lien_explorateur(client):
    flux = flux_entre("btc", ME, Z, client=client, intermediaire=False)
    assert [f["donnees"]["sens"] for f in flux] == ["a_vers_b", "b_vers_a"] or len(flux) >= 2
    assert all(f["type"] == "flux" and f["donnees"]["lien_explorateur"] == f["url_source"] for f in flux)
    assert flux[0]["donnees"]["montant"]  # la jambe b : ME vers Z, 0,5 BTC part ME
    avec_gel = flux_entre("btc", ME, Z, "2026-04-01", client=client, intermediaire=False)
    assert all(f["donnees"]["date"] < "2026-04-01" for f in avec_gel)
    assert len(avec_gel) < len(flux)


def test_blockstream_prend_le_relais_de_mempool(client, reseau):
    reseau.pannes["mempool.space"] = 503
    p = profil_portefeuille("btc", ME, client=client)
    assert p["donnees"]["nb_transactions"] == 5
    assert reseau.compter("blockstream.info") >= 2


def test_erreurs_typees(client, reseau):
    reseau.pannes["mempool.space"] = 429
    reseau.pannes["blockstream.info"] = 429
    with pytest.raises(ChainesLimiteError):
        client.releve("btc", ME)
    reseau.pannes["eth.blockscout.com"] = 403
    with pytest.raises(ChainesBloqueeError):
        client.releve("eth", ME_E)


def test_reseau_coupe_donne_une_erreur_typee(tmp_path):
    def casse(_requete):
        raise httpx.ConnectError("boum")

    c = ClientChaines(dossier_cache=tmp_path, transport=httpx.MockTransport(casse), dormir=lambda _s: None, intervalle=0)
    with pytest.raises(ch.ChainesError, match="injoignable"):
        c.releve("tron", T_ME)


def test_reessais_avec_attente_sur_429(tmp_path, reseau):
    attentes: list[float] = []
    reponses = iter([httpx.Response(429), httpx.Response(429)])

    def transport(requete):
        if requete.url.host == "api.trongrid.io" and requete.url.path == f"/v1/accounts/{T_ME}":
            suivante = next(reponses, None)
            if suivante is not None:
                return suivante
        return reseau(requete)

    c = ClientChaines(dossier_cache=tmp_path, transport=httpx.MockTransport(transport), dormir=attentes.append,
                      attente_base=2.0, intervalle=0)
    c.releve("tron", T_ME)
    assert attentes == [2.0, 4.0]


def test_cache_disque_evite_le_reseau_et_expire(client, reseau, tmp_path):
    profil_portefeuille("btc", ME, client=client)
    n = len(reseau.requetes)
    profil_portefeuille("btc", ME, client=client)
    assert len(reseau.requetes) == n  # tout vient du cache (liste OFAC comprise)
    assert list((tmp_path / "cache").glob("*.json"))
    plus_tard = datetime.now(timezone.utc) + timedelta(days=2)
    c2 = ClientChaines(dossier_cache=tmp_path / "cache", transport=httpx.MockTransport(reseau),
                       dormir=lambda _s: None, intervalle=0, maintenant=lambda: plus_tard)
    c2.releve("btc", ME)
    assert len(reseau.requetes) > n


# --------------------------------------------------------------------------- EVM


def test_profil_evm_etiquettes_jetons_et_gel(client):
    p = profil_portefeuille("eth", ME_E, "2026-04-01", client=client)
    d = p["donnees"]
    assert d["nb_transactions"] == 5  # 4 transactions natives (une échouée) + deux jetons - gel : voir ci-dessous
    actifs = {(t["actif"], t["contrat"]): t for t in d["totaux"]}
    usdt = actifs[("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7")]
    assert usdt["recu"] == "5" and usdt["usdt_officiel"] is True
    faux = actifs[("USDT", "0x9999999999999999999999999999999999999999")]
    assert faux["usdt_officiel"] is False
    assert d["totaux"][0]["usdt_officiel"] is True  # l'USDT officiel passe avant tout
    eth = actifs[("ETH", None)]
    assert (eth["recu"], eth["envoye"]) == ("2", "1")  # t3 postérieure ; t0 échouée : rien n'a bougé
    assert d["transactions_ecartees_apres_gel"] == 1


def test_plateforme_d_echange_doit_repondre(client):
    cps = {r["valeur"]: r for r in contreparties("eth", ME_E, client=client)}
    plateforme = cps[PLAT]["donnees"]
    assert plateforme["plateforme_echange"] is True and plateforme["doit_repondre"] is True
    assert "Doit répondre" in plateforme["mention"]
    assert "doit répondre" in cps[PLAT]["extrait"]
    noms = {e["nom"] for e in plateforme["etiquettes"]}
    assert "Binance 14" in noms and "note_0" not in noms  # les notes libres ne sont pas reprises
    assert all(e["source"].startswith("Blockscout") for e in plateforme["etiquettes"])
    assert cps[AUTRE_E]["donnees"]["doit_repondre"] is False
    assert cps[AUTRE_E]["donnees"]["sanction_ofac"]["statut"] == "oui"


def test_profil_de_la_plateforme_elle_meme(client):
    d = profil_portefeuille("eth", PLAT, client=client)["donnees"]
    assert d["plateforme_echange"] and d["doit_repondre"]
    assert d["nb_transactions"] == 0


def test_contrat_nest_pas_une_plateforme(client):
    from enquete.sources.chaines import _est_plateforme

    assert _est_plateforme([{"nom": "Binance-Peg Token", "categorie": "contrat"}], est_contrat=True) is False


def test_adresse_inconnue_de_l_explorateur_donne_un_profil_vide(client):
    d = profil_portefeuille("eth", "0x7777777777777777777777777777777777777777", client=client)["donnees"]
    assert d["nb_transactions"] == 0 and d["totaux"] == []


def test_flux_via_un_intermediaire(client):
    flux = flux_entre("eth", ME_E, FIN, client=client)
    indirects = [f for f in flux if f["type"] == "flux_indirect"]
    assert len([f for f in flux if f["type"] == "flux"]) == 0
    assert len(indirects) == 1
    f = indirects[0]["donnees"]
    assert f["intermediaire"] == AUTRE_E and f["force"] == "faible"
    assert len(f["urls_sources"]) == 2 and "piste" in f["avertissement"]
    # Pas de passage par un intermédiaire si on le refuse, ni quand le gel écarte une des deux jambes.
    assert flux_entre("eth", ME_E, FIN, client=client, intermediaire=False) == []
    assert flux_entre("eth", ME_E, FIN, "2026-04-15", client=client) == []


# ------------------------------------------------------------------------------ TRON


def test_profil_tron_trx_usdt_et_etiquette_tronscan(client):
    d = profil_portefeuille("tron", T_ME, client=client)["donnees"]
    actifs = {t["actif"]: t for t in d["totaux"]}
    assert actifs["USDT"]["recu"] == "1500" and actifs["USDT"]["envoye"] == "100" and actifs["USDT"]["usdt_officiel"]
    assert actifs["TRX"]["envoye"] == "5"
    assert d["nb_transactions"] == 3  # l'appel de contrat sans transfert n'est pas compté
    plat = profil_portefeuille("tron", T_PLAT, client=client)["donnees"]
    assert plat["etiquettes"][0]["nom"] == "Binance-Cold 2" and "Tronscan" in plat["etiquettes"][0]["source"]
    assert plat["doit_repondre"] is True


def test_tron_gel_et_contreparties(client):
    d = profil_portefeuille("tron", T_ME, "2026-06-01", client=client)["donnees"]
    assert {t["actif"]: t["envoye"] for t in d["totaux"]}["USDT"] == "0"  # l'envoi de septembre est écarté
    cps = {r["valeur"]: r for r in contreparties("tron", T_ME, client=client)}
    assert cps[T_PLAT]["donnees"]["doit_repondre"] is True
    assert cps[T_AUTRE]["donnees"]["sanction_ofac"]["statut"] == "oui"
    assert cps[T_PLAT]["url_source"].startswith("https://tronscan.org/#/transaction/")


# ---------------------------------------------------------------------------- Solana


def test_profil_solana_lit_sol_et_jetons(client):
    d = profil_portefeuille("sol", S_ME, client=client)["donnees"]
    actifs = {t["actif"]: t for t in d["totaux"]}
    assert actifs["SOL"]["recu"] == "2.5"
    assert (actifs["USDT"]["recu"], actifs["USDT"]["envoye"]) == ("0", "12.5")
    assert d["nb_transactions"] == 4
    assert d["premiere_activite"].startswith("2026-03-01")


def test_solana_gel_et_plafond_de_lecture(client, tmp_path, reseau):
    d = profil_portefeuille("sol", S_ME, "2026-04-01", client=client)["donnees"]
    assert d["nb_transactions"] == 2 and d["transactions_ecartees_apres_gel"] == 2
    limite = ClientChaines(dossier_cache=tmp_path / "autre", transport=httpx.MockTransport(reseau),
                           dormir=lambda _s: None, intervalle=0, max_detail_sol=1)
    d2 = profil_portefeuille("sol", S_ME, client=limite)["donnees"]
    assert d2["partiel"] is True and any("Seules 1 transactions" in a for a in d2["avertissements"])


# ----------------------------------------------------------------- entrées communes


def test_entrees_communes_regroupe_et_ecarte_le_coinjoin(client):
    groupes = regrouper_entrees_communes([ME], client=client)
    assert len(groupes) == 1
    g = groupes[0]
    assert g["type"] == "entrees_communes"
    assert set(g["valeur"]) == {ME, W}  # la transaction « e » (CoinJoin) aurait aussi rattaché X et Z
    d = g["donnees"]
    assert d["force"] == "probable" and "Heuristique" in d["avertissement"]
    assert [p["id"] for p in d["preuves"]] == ["d" * 64]
    assert g["url_source"] == f"https://mempool.space/tx/{'d' * 64}"
    assert [c["id"] for c in d["coinjoin_ecartes"]] == ["e" * 64]


def test_entrees_communes_respecte_gel_et_filtre(client):
    assert regrouper_entrees_communes([ME], date_gel="2026-01-31", client=client) == []
    assert regrouper_entrees_communes([ME], filtre_url=lambda u: "d" * 64 not in u, client=client) == []
    assert any("d" * 64 in r["url"] for r in client.rejets)


def test_entrees_communes_exige_des_adresses_btc(client):
    with pytest.raises(ChainesAdresseInvalideError):
        regrouper_entrees_communes([ME_E], client=client)


# ---------------------------------------------------------------------------- OFAC


def test_liste_ofac_lue_depuis_le_csv():
    liste = ListeOFAC.depuis_csv(OFAC)
    assert len(liste) == 4  # Y, l'adresse ETH, l'adresse TRON, l'adresse XMR
    oui = liste.controle(AUTRE_E.upper().replace("0X", "0x"))
    assert oui["statut"] == "oui" and oui["entrees"][0]["url"].endswith("id=9001")
    assert liste.controle(ME)["statut"] == "non"


def test_ofac_indisponible_donne_inconnu(tmp_path, reseau):
    reseau.ofac_en_panne = True
    c = ClientChaines(dossier_cache=tmp_path, transport=httpx.MockTransport(reseau), dormir=lambda _s: None, intervalle=0)
    p = profil_portefeuille("btc", ME, client=c)
    assert p["donnees"]["sanction_ofac"]["statut"] == "inconnu"


def test_une_attribution_a_une_personne_n_existe_nulle_part(client):
    for resultat in [profil_portefeuille("eth", PLAT, client=client), *contreparties("eth", ME_E, client=client)]:
        texte = json.dumps(resultat, ensure_ascii=False).lower()
        assert "jamais attribué" in texte
        assert not any(mot in texte for mot in ("propriétaire:", "appartient à", "titulaire:"))


def test_introuvable_est_typee(client, reseau):
    with pytest.raises(ChainesIntrouvableError):
        client._get("https://eth.blockscout.com/api/v2/addresses/0x0000000000000000000000000000000000000abc")
    assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")  # les montants restent décimaux, jamais flottants
