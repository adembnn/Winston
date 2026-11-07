// Branchement de l'écran sur le serveur local (enquete/serveur.py).
//
// Image simple : la prise murale. L'écran (n'importe quelle planche) n'a qu'à brancher
// ces cinq fonctions ; tout ce qu'il affiche vient de l'état recalculé ici depuis le flux.
// Module ES sans dépendance : navigateur et Node 18+.
//
//   import { surFrappe, lancer, suivre, resume, exporter } from "./branchement.js";
//   surFrappe(texte, graine, ({ locales, graine, profond, enCours }) => dessiner(...));
//   const id = await lancer({ question, graine, mots });
//   const arret = suivre(id, { mode: "rejeu", dureeCible: 22, quand: (evt, etat) => dessiner(etat) });
//
// Champs d'`etat` que l'écran peut lire (recalculés depuis le flux seul) :
//   compteurs.par_role          { role: nombre de nœuds }, tous les nœuds (celui du serveur)
//   compteurs.sources_par_role  { role: nombre de sources } : nœuds de type video, publication_texte,
//                               document, site, societe ou personne. Ni comptes, ni grappes, ni
//                               phrases, ni sons, ni hashtags. Vidéos, presse, études, registres.
//   zones                       [{id, titre, pourquoi, tente, autour, pistes: [{geste, ou, comment,
//                               difficulte}], role_manquant, version, motif}] : la dernière version de
//                               chaque zone d'ombre, dans l'ordre d'apparition (compte = zones.length).
//   derniere                    {genre: "noeud" | "question" | "zone", id, role, type} : le dernier nœud ou la
//                               dernière question ou zone arrivés, un seul à la fois ; il porte le jaune.
//                               Pour une zone : {genre: "zone", id, role: role_manquant, type: "zone", titre}.
//                               Une nouvelle version d'un nœud déjà vu (classement, rôle changé)
//                               remplace l'ancienne : le nœud ne compte qu'une fois.
//
// Page servie par le serveur (http://127.0.0.1:8800/) : rien à régler. Ailleurs (Node, autre
// port) : `configurer({ base: "http://127.0.0.1:8800" })` ou `creerBranchement({ base })`.

export const ROLES = ["origine", "amplificateur", "beneficiaire", "infrastructure", "victime",
  "temoin", "expert", "contradicteur", "mis_en_cause", "inconnu"];

const MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.",
  "nov.", "déc."];

// ---------------------------------------------------------------------------
// L'état, recalculé depuis le flux seul
// ---------------------------------------------------------------------------

export const TYPES_SOURCE = ["video", "publication_texte", "document", "site", "societe", "personne"];

function recalculerSources(etat) {
  const sources = {};
  for (const [role, liste] of Object.entries(etat.pieces)) {
    sources[role] = liste.filter((p) => TYPES_SOURCE.includes(p.type)).length;
  }
  etat.compteurs = { ...etat.compteurs, sources_par_role: sources };
}

export function etatInitial() {
  const pieces = {};
  for (const r of ROLES) pieces[r] = [];
  return {
    n: 0,
    question: null,        // texte de la question centrale
    pieces,                // { role: [noeud, ...] } : dernière version de chaque nœud
    liens: [],             // dernière version de chaque lien
    compteurs: { pieces: 0, liens: 0, preuves: 0, zones_d_ombre: 0, par_role: {}, sources_par_role: {}, videos_lues: 0 },
    zones: [],             // [{id, titre, pourquoi, tente, autour, pistes, role_manquant, version}] : dernière version de chaque zone d'ombre
    questions: [],         // [{id, texte, statut, numero, origine, ...}] dans l'ordre d'apparition
    recherches: [],        // [{mot, champ, total, ...}]
    etape: null,           // {nom, detail} de l'étape en cours, sinon null
    etapes: {},            // { nom: "debut" | "fin" }
    derniere: null,        // ce qui vient d'arriver (nœud ou question) : il porte le jaune
    cas: null, date: null, duree_reelle_s: null,
    accelere: false, facteur: null,
    mention: null,         // « Rejoué ×k : enquête du ... », seulement si le flux est accéléré
    ok: null, erreur: null,
    termine: false,
  };
}

function dateEnMots(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  return m ? Number(m[3]) + " " + MOIS[Number(m[2]) - 1] + " " + m[1] : "";
}

function dureeEnMots(s) {
  s = Math.round(s);
  if (s < 60) return s + " s";
  if (s < 3600) return Math.round(s / 60) + " min";
  const h = Math.floor(s / 3600);
  return h + " h " + String(Math.round((s - h * 3600) / 60)).padStart(2, "0") + " min";
}

function nombreK(k) {
  return k >= 10 ? String(Math.round(k)) : String(Math.round(k * 10) / 10).replace(".", ",");
}

export function mentionAcceleration({ facteur, duree_reelle_s, date }) {
  if (!(facteur > 1) || !(duree_reelle_s > 0)) return null;
  const cible = Math.round(duree_reelle_s / facteur);
  const quand = dateEnMots(date);
  return "Rejoué ×" + nombreK(facteur) + (quand ? " : enquête du " + quand + "," : " : enquête de") +
    " " + dureeEnMots(duree_reelle_s) + " ramenées à " + cible + " s";
}

function remplacer(liste, element) {
  const i = liste.findIndex((x) => x.id === element.id);
  if (i >= 0) liste[i] = element; else liste.push(element);
}

// Applique un événement du flux à l'état (sans horloge, sans affichage). Renvoie l'état.
export function appliquer(etat, evt) {
  const d = evt.donnees || {};
  if (typeof evt.n === "number") etat.n = evt.n;
  switch (evt.type) {
    case "question":
      if (d.centrale) { etat.question = d.texte; break; }
      {
        const connue = etat.questions.find((q) => q.id === d.id);
        const q = { ...d, numero: connue ? connue.numero : etat.questions.length + 1 };
        remplacer(etat.questions, q);
        etat.derniere = { genre: "question", id: d.id, role: null, type: "question", statut: d.statut, texte: d.texte };
      }
      break;
    case "zone":
      remplacer(etat.zones, d);
      etat.derniere = { genre: "zone", id: d.id, role: d.role_manquant, type: "zone", titre: d.titre };
      break;
    case "noeud": {
      for (const liste of Object.values(etat.pieces)) {
        const i = liste.findIndex((p) => p.id === d.id);
        if (i >= 0) liste.splice(i, 1);
      }
      (etat.pieces[d.role] = etat.pieces[d.role] || []).push(d);
      etat.derniere = { genre: "noeud", id: d.id, role: d.role, type: d.type, libelle: d.libelle };
      recalculerSources(etat);
      break;
    }
    case "lien":
      remplacer(etat.liens, d);
      break;
    case "compteur":
      etat.compteurs = d;
      recalculerSources(etat);
      break;
    case "recherche":
      etat.recherches.push({ mot: d.mot, champ: d.champ, total: d.total, source: d.source, recus: d.recus });
      break;
    case "etape":
      etat.etapes[d.nom] = d.etat;
      if (d.etat === "debut") etat.etape = { nom: d.nom, detail: d.detail || null };
      else if (etat.etape && etat.etape.nom === d.nom) etat.etape = null;
      if (d.nom === "enquete" && d.etat === "fin") {
        etat.cas = d.cas ?? etat.cas;
        etat.date = d.date ?? etat.date;
        etat.duree_reelle_s = d.duree_reelle_s ?? etat.duree_reelle_s;
      }
      break;
    case "fin":
      etat.termine = true;
      etat.etape = null;
      etat.ok = d.ok !== false;
      etat.erreur = d.erreur || null;
      if (d.compteurs) { etat.compteurs = d.compteurs; recalculerSources(etat); }
      if (d.duree_reelle_s != null) etat.duree_reelle_s = d.duree_reelle_s;
      etat.accelere = !!evt.accelere;
      etat.facteur = evt.facteur ?? null;
      etat.mention = evt.accelere
        ? mentionAcceleration({ facteur: evt.facteur, duree_reelle_s: etat.duree_reelle_s, date: etat.date || evt.horodatage })
        : null;
      break;
    default:
      break;
  }
  return etat;
}

// ---------------------------------------------------------------------------
// Lecture d'un flux SSE par fetch (Node, ou navigateur sans EventSource)
// ---------------------------------------------------------------------------

function lireFluxFetch(url, { fetch: aller, surMessage, surErreur }) {
  const controle = new AbortController();
  (async () => {
    try {
      const reponse = await aller(url, { signal: controle.signal, headers: { Accept: "text/event-stream" } });
      if (!reponse.ok) {
        let message = "HTTP " + reponse.status;
        try { message = (await reponse.json()).erreur || message; } catch { /* corps non JSON */ }
        surErreur(new Error(message));
        return;
      }
      const lecteur = reponse.body.getReader();
      const decodeur = new TextDecoder("utf-8");
      let reste = "";
      for (;;) {
        const { done, value } = await lecteur.read();
        if (done) break;
        reste += decodeur.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let coupe;
        while ((coupe = reste.indexOf("\n\n")) >= 0) {
          const bloc = reste.slice(0, coupe);
          reste = reste.slice(coupe + 2);
          const donnees = bloc.split("\n").filter((l) => l.startsWith("data:"))
            .map((l) => l.slice(5).replace(/^ /, "")).join("\n");
          if (donnees) surMessage(donnees);
        }
      }
    } catch (e) {
      if (!controle.signal.aborted) surErreur(e);
    }
  })();
  return () => controle.abort();
}

function lireFluxEventSource(url, { EventSource: Source, surMessage, surErreur }) {
  const source = new Source(url);
  source.onmessage = (m) => surMessage(m.data);
  // Une coupure : EventSource se reconnecte seul avec Last-Event-ID (le serveur reprend à `depuis`).
  source.onerror = () => { if (source.readyState === 2) surErreur(new Error("flux fermé")); };
  return () => source.close();
}

// ---------------------------------------------------------------------------
// Les cinq prises
// ---------------------------------------------------------------------------

export function creerBranchement(options = {}) {
  const reglages = {
    base: "", fetch: null, EventSource: null, transport: null,
    delaiFrappe: 150, delaiProfond: 1500, ...options,
  };
  const aller = (...a) => (reglages.fetch || globalThis.fetch)(...a);
  const adresse = (chemin, params) => {
    const q = new URLSearchParams();
    for (const [cle, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== "") q.set(cle, v);
    const texte = q.toString();
    return reglages.base + chemin + (texte ? "?" + texte : "");
  };

  async function json(url, init) {
    const reponse = await aller(url, init);
    let corps = null;
    try { corps = await reponse.json(); } catch { /* corps vide */ }
    if (!reponse.ok) throw new Error((corps && corps.erreur) || "HTTP " + reponse.status);
    return corps;
  }

  // -- frappe ---------------------------------------------------------------
  let generation = 0;
  let minuteurs = [];

  function surFrappe(texte, graine, rappel) {
    const moi = ++generation;
    for (const m of minuteurs) clearTimeout(m);
    minuteurs = [];
    const q = (texte || "").trim();
    const g = (graine || "").trim() || null;
    if (!q && !g) {
      rappel({ locales: null, graine: null, profond: null, enCours: false });
      return () => {};
    }
    let profondRecu = false;
    const vivant = () => moi === generation;
    const base = { locales: null, graine: null, profond: null };

    minuteurs.push(setTimeout(async () => {
      try {
        const r = await json(adresse("/api/comprendre", { q, graine: g }));
        // Une réponse périmée, ou arrivée après la lecture profonde, n'écrase rien.
        if (!vivant() || profondRecu) return;
        base.locales = r.locales || null;
        base.graine = r.graine || null;
        rappel({ ...base, enCours: !!q });
      } catch (erreur) {
        if (vivant() && !profondRecu) rappel({ ...base, enCours: false, erreur: erreur.message });
      }
    }, reglages.delaiFrappe));

    if (q) {
      minuteurs.push(setTimeout(async () => {
        try {
          const r = await json(adresse("/api/comprendre", { q, graine: g, profond: 1 }));
          if (!vivant()) return;
          profondRecu = true;
          rappel({ locales: r.locales || base.locales, graine: r.graine || base.graine,
            profond: r.profond || null, enCours: false });
        } catch (erreur) {
          if (vivant()) rappel({ ...base, enCours: false, erreur: erreur.message });
        }
      }, reglages.delaiProfond));
    }
    return () => { if (vivant()) { generation++; for (const m of minuteurs) clearTimeout(m); } };
  }

  // -- lancement ------------------------------------------------------------
  async function lancer({ question, graine, mots } = {}) {
    const corps = { question };
    if (graine) corps.graine = graine;
    if (mots) corps.mots = mots;
    const r = await json(adresse("/api/enquetes"), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corps),
    });
    return r.id;
  }

  // -- suivi ----------------------------------------------------------------
  function suivre(id, { mode = "direct", dureeCible = 22, quand = () => {} } = {}) {
    const etat = etatInitial();
    let ferme = false;
    let arreter = () => {};
    const copie = () => structuredClone(etat);

    const clore = () => { if (!ferme) { ferme = true; arreter(); } };
    const surMessage = (texte) => {
      if (ferme) return;
      let evt;
      try { evt = JSON.parse(texte); } catch { return; }
      if (typeof evt.n === "number" && evt.n <= etat.n) return;  // doublon d'une reconnexion
      appliquer(etat, evt);
      quand(evt, copie());
      if (evt.type === "fin") clore();
    };
    const surErreur = (erreur) => {
      if (ferme) return;
      const evt = { n: null, type: "erreur", donnees: { message: erreur.message }, accelere: false, facteur: null };
      etat.erreur = erreur.message;
      clore();
      quand(evt, copie());
    };

    const url = mode === "rejeu"
      ? adresse("/api/rejouer", { enquete: id, duree_cible: dureeCible })
      : adresse("/api/evenements", { enquete: id });
    const Source = reglages.EventSource || globalThis.EventSource;
    const natif = reglages.transport ? reglages.transport === "eventsource"
      : typeof window !== "undefined" && typeof Source === "function";
    arreter = natif
      ? lireFluxEventSource(url, { EventSource: Source, surMessage, surErreur })
      : lireFluxFetch(url, { fetch: aller, surMessage, surErreur });
    return clore;
  }

  const resume = (id, pieceId) => json(adresse("/api/resume", { enquete: id, piece: pieceId }));
  const exporter = (id) => json(adresse("/api/export", { enquete: id }));

  return { surFrappe, lancer, suivre, resume, exporter, reglages };
}

// L'instance par défaut : la page est servie par le serveur, donc même origine.
const defaut = creerBranchement();
export const surFrappe = defaut.surFrappe;
export const lancer = defaut.lancer;
export const suivre = defaut.suivre;
export const resume = defaut.resume;
export const exporter = defaut.exporter;
export const configurer = (options) => Object.assign(defaut.reglages, options);
