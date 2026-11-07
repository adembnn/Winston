// Réseau d'enquête : une question au centre, des rôles autour. Toute l'information vient de l'enquête
// affichée (export du serveur d'enquête ou flux en direct). Mise en page déterministe, sans physique :
// chaque élément a un emplacement calculé depuis la taille de la fenêtre.
const P = new URLSearchParams(location.search);
const ID = P.get("enquete") || "";
const STATIQUE = P.has("statique");
const MODE = P.get("mode") === "direct" ? "direct" : "rejeu";
const BASE = (P.get("base") || "http://127.0.0.1:8802").replace(/\/$/, "");
const VERIF = P.has("verif");

const ROLES = ["origine", "amplificateur", "beneficiaire", "infrastructure", "victime", "temoin", "expert",
  "contradicteur", "mis_en_cause", "inconnu"];
const LIBELLE = {
  origine: "Plus ancienne trace retrouvée", amplificateur: "Relais", beneficiaire: "À qui ça profite",
  infrastructure: "Moyens techniques", victime: "Personnes qui se disent lésées", temoin: "Témoins directs",
  expert: "Experts", contradicteur: "Voix contraires", mis_en_cause: "Mis en cause", inconnu: "À classer",
};
const TYPE_LIBELLE = { video: "Vidéo", compte: "Compte", publication_texte: "Publication", document: "Document",
  site: "Site", societe: "Société", personne: "Personne", grappe: "Groupe de contenus" };

const PICTO = {
  video: '<rect x="6" y="10" width="36" height="28" rx="3"/><path d="M20 18l12 6-12 6z"/>',
  compte: '<circle cx="24" cy="17" r="8"/><path d="M8 43c2-10 10-14 16-14s14 4 16 14"/>',
  publication_texte: '<rect x="8" y="6" width="32" height="36" rx="3"/><path d="M14 16h20M14 24h20M14 32h12"/>',
  document: '<path d="M12 5h17l9 9v29H12z"/><path d="M29 5v9h9M18 24h14M18 32h14"/>',
  site: '<circle cx="24" cy="24" r="18"/><path d="M6 24h36M24 6c-9 9-9 27 0 36M24 6c9 9 9 27 0 36"/>',
  societe: '<path d="M8 42V10h20v32M28 20h12v22M4 42h40M14 18h8M14 26h8M14 34h8"/>',
  grappe: '<circle cx="17" cy="29" r="9"/><circle cx="31" cy="29" r="9"/><circle cx="24" cy="16" r="9"/>',
};
PICTO.personne = PICTO.compte;
const picto = (t) => '<svg viewBox="0 0 48 48" aria-hidden="true">' + (PICTO[t] || '<rect x="8" y="8" width="32" height="32" rx="3"/>') + "</svg>";

const COURT = { origine: "Origine", amplificateur: "Relais", beneficiaire: "Bénéficiaires", infrastructure: "Moyens", victime: "Lésés",
  temoin: "Témoins", expert: "Experts", contradicteur: "Voix contraires", mis_en_cause: "Mis en cause", inconnu: "À classer" };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const plur = (n, s, p) => n + " " + (n > 1 ? p : s);

// ---------------------------------------------------------------- modèle
let M = null;            // {question, cas, roles:{role:[pieces]}, zones:[{id,titre}], zonesGenre, lectures, termine}
let niveau = null;       // rôle ouvert ou null
let selId = null;        // pièce sélectionnée (niveau 2)
let jauneRole = null;    // rôle arrivé il y a moins de 600 ms
let questionTapee = P.get("q") || P.get("question") || "";

let VIGN = {};   // vignettes-<enquete>.json : piece_id -> URL de la vraie image
// Une image n'illustre une pièce que si elle est la pièce elle-même (capture, miniature, aperçu de la source) :
// jamais un article de presse (photo d'illustration), jamais une affirmation, une personne seulement si son profil public est vérifié.
function imageOK(p) {
  if (p.type === "publication_texte" || p.type === "affirmation") return false;
  if (p.type === "personne") return !!(p.attributs && p.attributs.profil_public_verifie === true);
  return true;
}
const estHttp = (u) => typeof u === "string" && /^https?:\/\//.test(u);
// `image` d'une pièce ou d'un dossier : objet {src, provenance: "capture" | "illustration", source_url, credit, seconde…}
// capture -> source_url (URL publique) ; illustration -> src si c'est une URL http. Sinon null.
function urlImageObjet(o, ok) {
  if (!o) return "";
  if (typeof o === "string") return o;
  if (o.provenance === "capture" && estHttp(o.source_url)) return o.source_url;
  if (o.provenance === "illustration" && estHttp(o.src) && ok) return o.src;
  if (!o.provenance) return estHttp(o.source_url) ? o.source_url : estHttp(o.src) ? o.src : "";
  return "";
}
let EXCL = new Set();   // exclues-<enquete>.json : pièces dont l'image ne doit jamais s'afficher
const interdite = (u) => /(^|\/)frames\//.test(u) || /brut-videos/i.test(u);
function imageDe(p) {
  if (EXCL.has(p.id)) return "";
  const u = imageBrute(p);
  return u && !interdite(u) ? u : "";
}
function imageBrute(p) {
  const o = urlImageObjet(p.image, imageOK(p));
  if (o) return o;
  const v = VIGN[p.id];
  // chemin local (captures/…, images/… : illustrations ou captures choisies par l'équipe) : toujours permis
  if (typeof v === "string" && v && !estHttp(v)) return v;
  if (estHttp(v) && imageOK(p)) return v;
  const a = p.attributs || {};
  if (imageOK(p)) {
    if (estHttp(a.image)) return a.image;
    const k = Array.isArray(a.images_cles) && a.images_cles[0] && a.images_cles[0].url;
    if (estHttp(k)) return k;
  }
  return "";
}
// provenance affichée sous le panneau : « capture » ou « illustration, crédit »
function legendeImage(p) {
  const o = p.image && typeof p.image === "object" ? p.image : null;
  if (o && o.provenance === "capture") return "capture";
  if (o && o.provenance === "illustration") return "illustration" + (o.credit ? ", " + o.credit : "");
  const v = VIGN[p.id];
  if (typeof v === "string" && /^captures\//.test(v)) return "capture";
  if (typeof v === "string" && v && !estHttp(v)) return "illustration";
  return "";
}
const nbPreuves = (p) => (p.preuves && p.preuves.length) || (p.compteurs && p.compteurs.preuves) || 0;

const sansNumero = (t) => String(t || "").replace(/^\s*\d+\s*[.)]\s*/, "");
function zonesDe(zonesOmbre, questions) {
  const zo = (zonesOmbre || []).filter((z) => z && (z.titre || z.texte));
  if (zo.length) return { genre: "zone", liste: zo.map((z) => ({ id: z.id, titre: sansNumero(z.titre || z.texte), pourquoi: z.pourquoi || "", mots: z.pourquoi_en_mots || z.mots || "", tente: Array.isArray(z.tente) ? z.tente : [], autour: Array.isArray(z.autour) ? z.autour : [], pistes: Array.isArray(z.pistes) ? z.pistes : [] })) };
  const qo = (questions || []).filter((q) => q && (!q.statut || q.statut === "ouverte"));
  return { genre: "question", liste: qo.map((q) => ({ id: q.id, titre: sansNumero(q.texte || q.titre) })) };
}

function depuisExport(d) {
  const roles = {};
  for (const r of ROLES) roles[r] = [];
  const roleImg = {};
  for (const dos of d.dossiers || []) { roles[dos.role] = (roles[dos.role] || []).concat(dos.pieces || []); const u = urlImageObjet(dos.image, true); if (u && !interdite(u) && !EXCL.has(dos.image && dos.image.piece_id) && dos.role !== "inconnu") roleImg[dos.role] = u; }
  const q = d.question;
  const z = zonesDe(d.zones_d_ombre, d.questions_ouvertes);
  return {
    question: typeof q === "string" ? q : (q && (q.texte || q.question)) || "",
    cas: (q && q.cas === "fictif") || d.cas === "fictif" || ID === "stylos-minceur",
    roles, roleImg, zones: z.liste, zonesGenre: z.genre, lectures: (d.compteurs && d.compteurs.videos_lues) || null, termine: true,
  };
}
function depuisEtat(e) {
  const roles = {};
  for (const r of ROLES) roles[r] = (e.pieces && e.pieces[r]) || [];
  const z = zonesDe(e.zones, e.questions);
  return {
    question: e.question || questionTapee || "",
    cas: e.cas === "fictif" || ID === "stylos-minceur",
    roles, zones: z.liste, zonesGenre: z.genre,
    lectures: e.compteurs ? e.compteurs.videos_lues : null, termine: !!e.termine,
  };
}

// ---------------------------------------------------------------- noeuds DOM
const $scene = document.getElementById("scene");
const $traits = document.getElementById("traits");
const $info = document.getElementById("info");
const noeuds = new Map();
let vus = new Set();
function nd(key, classe, tag = "div") {
  vus.add(key);
  let e = noeuds.get(key);
  if (!e) {
    e = document.createElement(tag);
    e.className = "a " + classe + (STATIQUE ? "" : " nouveau");
    e.dataset.v = key;
    $scene.appendChild(e);
    noeuds.set(key, e);
  }
  return e;
}
function pose(e, x, y, w, h) {
  e.__x = x; e.__w = w != null ? w : e.offsetWidth;
  e.style.left = Math.round(x) + "px";
  e.style.top = Math.round(y) + "px";
  if (w != null) e.style.width = Math.round(w) + "px";
  if (h != null) e.style.height = Math.round(h) + "px";
}
function html(e, h) { if (e.__h !== h) { e.innerHTML = h; e.__h = h; } }
function info(e, t) { if (t) e.setAttribute("data-info", t); else e.removeAttribute("data-info"); }

// Ajuste un bloc de texte : plus grande police qui tient en `lignes` lignes, largeur resserrée sur le texte.
function fit(e, texte, wmax, fmax, fmin, lignes, lh) {
  if (e.__t !== texte) { e.textContent = texte; e.__t = texte; }
  e.style.height = "auto";
  e.style.width = Math.round(wmax) + "px";
  let f = Math.round(fmax), tient = false;
  for (; f >= fmin; f -= 2) {
    e.style.fontSize = f + "px";
    if (e.scrollHeight <= lignes * f * lh + 2) { tient = true; break; }
  }
  if (!tient) { f = fmin; e.style.fontSize = f + "px"; }
  const h0 = e.scrollHeight;
  const rg = document.createRange();
  rg.selectNodeContents(e);
  const w = Math.min(wmax, Math.ceil(rg.getBoundingClientRect().width) + 6);
  e.style.width = w + "px";
  if (e.scrollHeight > h0 + 1) e.style.width = Math.round(wmax) + "px";
  return { w: e.offsetWidth, h: e.scrollHeight, f, tient };
}

// ---------------------------------------------------------------- calculs sur les données
const piecesDe = (r) => (M.roles[r] || []);
const totalPieces = () => ROLES.reduce((s, r) => s + piecesDe(r).length, 0);
const totalPreuvesRole = (r) => piecesDe(r).reduce((s, p) => s + nbPreuves(p), 0);
function typeDominant(r) {
  const c = {};
  for (const p of piecesDe(r)) c[p.type] = (c[p.type] || 0) + 1;
  return Object.entries(c).sort((a, b) => b[1] - a[1])[0]?.[0] || "document";
}
const candidats = (r) => [...(M.roleImg && M.roleImg[r] ? [M.roleImg[r]] : []), ...piecesDe(r).map((p) => p)].sort((x, y) => (typeof x === "string" ? -1 : typeof y === "string" ? 1 : rang(x) - rang(y))).map((x) => (typeof x === "string" ? x : imageDe(x))).filter(Boolean);
const imageRole = (r) => candidats(r)[0] || "";

const rang = (p) => (p.solidite && p.solidite.matiere === "solide" ? 0 : p.solidite && p.solidite.matiere === "indice" ? 1 : 2);
const faible = (r) => !piecesDe(r).some((p) => rang(p) === 0);

function sousTitre() {
  const FAM = [[["video"], "vidéo", "vidéos"], [["publication_texte", "document"], "article ou document", "articles et documents"],
    [["site"], "site", "sites"], [["societe"], "société ou registre", "registres et sociétés"]];
  const cpt = {};
  for (const r of ROLES) for (const p of piecesDe(r)) cpt[p.type] = (cpt[p.type] || 0) + 1;
  if (M.lectures && M.lectures > (cpt.video || 0)) cpt.video = M.lectures;
  const n = FAM.map((f) => f[0].reduce((x, t) => x + (cpt[t] || 0), 0));
  const total = n.reduce((x, y) => x + y, 0);
  if (!total) return "Lecture en cours…";
  const parts = FAM.map((f, i) => (n[i] ? plur(n[i], f[1], f[2]) : "")).filter(Boolean);
  return `${plur(total, "source lue", "sources lues")} : ${parts.join(", ")}`;
}

// ---------------------------------------------------------------- rendu
let K = 1, W = 0, H = 0;
let bande = null;   // rectangle réservé à l'infobulle de repli

function rendre() {
  if (!M) return;
  W = window.innerWidth; H = window.innerHeight;
  K = clamp(Math.min(W / 1398, H / 782), 1, 1.9);
  document.documentElement.style.setProperty("--k", K);
  vus = new Set();
  const f20 = Math.round(20 * K);
  const m = Math.max(32, Math.round(W * 0.028));

  // logo
  const logo = nd("logo", "logo");
  if (!logo.firstChild) logo.innerHTML = '<img src="brut-logo.svg" alt="Brut." style="display:block;width:100%;filter:invert(1)">';
  const lw = Math.round(112 * K), lh = Math.round(lw * 28.65174 / 79.37507);
  pose(logo, W - m - lw, m, lw, lh);

  // bandeau cas fictif
  let bw = 0, bh = 0;
  if (M.cas) {
    const b = nd("bandeau", "bandeau");
    b.style.fontSize = f20 + "px";
    html(b, "Cas fictif,<br>données d'exemple");
    b.style.width = "auto";
    bw = b.offsetWidth; bh = b.offsetHeight;
    pose(b, m, H - m - bh, bw, bh);
  }

  // sous-titre
  const sub = nd("soustitre", "", "div");
  sub.id = "sous";
  const subMax = bw ? W - 2 * (m + bw + 24) : W * 0.78;
  const sf = fit(sub, niveau === "__zones" ? sousTitreZones() : niveau ? sousTitreNiveau() : sousTitre(), subMax, 26 * K, f20, 1, 1.2);
  pose(sub, (W - sf.w) / 2, H - m - sf.h, sf.w, sf.h);

  const aTop = m + lh + 16;
  const aBot = H - m - Math.max(sf.h, bh) - 16;

  if (niveau === "__zones") niveauZones(m, aTop, aBot, f20); else if (niveau) niveau2(m, aTop, aBot, f20, lh); else niveau1(m, aTop, aBot, f20);

  for (const [k, e] of noeuds) if (!vus.has(k)) { e.remove(); noeuds.delete(k); }
  if (!niveau) document.body.classList.remove("n2"); else document.body.classList.add("n2");
  if (STATIQUE && VERIF && !document.body.classList.contains("fige")) verifier();
}

const MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."];
const NOMS = { tiktok: "TikTok", instagram: "Instagram", youtube: "YouTube", x: "X", twitter: "X", facebook: "Facebook",
  reddit: "Reddit", telegram: "Telegram", bluesky: "Bluesky" };
const HOTES = [["tiktok.", "TikTok"], ["instagram.", "Instagram"], ["youtube.", "YouTube"], ["youtu.be", "YouTube"], ["twitter.", "X"], ["x.com", "X"]];
const maj1 = (t) => t.charAt(0).toUpperCase() + t.slice(1);
function dateDe(p) {
  const a = p.attributs || {};
  for (const k of ["date", "publiee", "publiee_le_selon_oriane", "mise_en_ligne", "premier_vu", "premiere_date", "creee_le", "indexe_depuis"]) {
    const m = typeof a[k] === "string" && /^(\d{4})-(\d{2})-(\d{2})/.exec(a[k]);
    if (m) return Number(m[3]) + " " + MOIS[Number(m[2]) - 1] + " " + m[1];
  }
  return "";
}
function sourceDe(p) {
  const a = p.attributs || {};
  const pf = typeof a.plateforme === "string" ? a.plateforme.toLowerCase() : "";
  if (NOMS[pf]) return NOMS[pf];
  const url = a.url || (p.preuves && p.preuves[0] && p.preuves[0].url) || "";
  if (/^https?:\/\//.test(url)) {
    try {
      const h = new URL(url).hostname.replace(/^www\./, "");
      const k = HOTES.find(([x]) => h.includes(x));
      return k ? k[1] : h;
    } catch (_) { /* adresse illisible */ }
  }
  return pf ? maj1(pf) : (TYPE_LIBELLE[p.type] || p.type || "");
}
function meilleure(r) {
  return [...piecesDe(r)].sort((x, y) => rang(x) - rang(y) || ((y.preuves && y.preuves.length ? 1 : 0) - (x.preuves && x.preuves.length ? 1 : 0)))[0];
}

function imgHtml(cands, type) {
  if (!cands.length) return picto(type);
  return `<img src="${esc(cands[0])}" referrerpolicy="no-referrer" alt="" data-c="${esc(JSON.stringify(cands))}" data-i="0" data-t="${esc(type)}">`;
}
document.addEventListener("error", (ev) => {
  const im = ev.target;
  if (!(im instanceof HTMLImageElement) || !im.dataset.c) return;
  const c = JSON.parse(im.dataset.c), i = (Number(im.dataset.i) || 0) + 1;
  if (i < c.length) { im.dataset.i = i; im.src = c[i]; } else { im.parentElement.innerHTML = picto(im.dataset.t); }
}, true);

function carte(e, label, n, cands, type, f20, sh, faibleCadre, avecCompteur) {
  const sig = [label, n, cands.join(","), type, avecCompteur].join("|");
  if (e.__sig !== sig) {
    e.__sig = sig;
    e.innerHTML = '<div class="img">' + imgHtml(cands, type) + "</div>" +
      '<div class="etiq"><span class="nom">' + esc(label) + "</span>" + (avecCompteur ? '<b class="cpt">' + n + "</b>" : "") + "</div>";
  }
  e.style.fontSize = f20 + "px";
  e.firstChild.nextSibling.style.height = sh + "px";
  e.classList.toggle("faible", !!faibleCadre);
}

function carteRole(e, r, n, cands, type, f20, sh, h, lab) {
  if (true) { e.classList.remove("texte"); carte(e, lab || LIBELLE[r], n, cands, type, f20, sh, false, true); return; }
  const p = meilleure(r);
  const src = sourceDe(p), date = dateDe(p) || TYPE_LIBELLE[p.type] || "";
  const sig = ["T", LIBELLE[r], n, src, date, p.type].join("|");
  if (e.__sig !== sig) {
    e.__sig = sig;
    e.innerHTML = `<div class="tx"><div class="pc">${picto(p.type)}</div><div class="tn">${esc(src)}</div><div class="td">${esc(date)}</div></div>` +
      `<div class="etiq"><span class="nom">${esc(LIBELLE[r])}</span><b class="cpt">${n}</b></div>`;
  }
  e.classList.add("texte"); e.classList.remove("faible");
  e.style.fontSize = f20 + "px";
  e.lastChild.style.height = sh + "px";
  const place = h - 4 - sh - 20 * K;
  const f24 = Math.round(24 * K);
  e.querySelector(".tn").style.webkitLineClamp = place > 2.4 * f24 * 1.1 ? "2" : "1";
  e.querySelector(".pc").style.display = place > 3.4 * f24 * 1.1 ? "block" : "none";
}

// Une grande question : sa première proposition, jusqu'à la première parenthèse, terminée par « ? »
function titreCourt(q) {
  if (q.length <= 90) return q;
  let i = q.indexOf(" (");
  if (i < 20) { const m = /[,;] /.exec(q.slice(40)); i = m ? 40 + m.index : -1; }
  if (i < 20) return q;
  const t = q.slice(0, i).trim().replace(/[,;:\s]+$/, "");
  return /\?$/.test(t) ? t : t + " ?";
}

// ---- tracé des traits
function intervalle(p0, p1, b) {   // entrée/sortie du segment p0->p1 dans la boîte b (paramètres t)
  let tin = -Infinity, tout = Infinity;
  for (const [a, d, lo, hi] of [[p0[0], p1[0] - p0[0], b.x, b.x + b.w], [p0[1], p1[1] - p0[1], b.y, b.y + b.h]]) {
    if (d === 0) { if (a < lo || a > hi) return null; continue; }
    const u = (lo - a) / d, v = (hi - a) / d;
    tin = Math.max(tin, Math.min(u, v)); tout = Math.min(tout, Math.max(u, v));
  }
  return tin < tout ? [tin, tout] : null;
}
const dedans = (p, b) => p[0] > b.x && p[0] < b.x + b.w && p[1] > b.y && p[1] < b.y + b.h;
function frappe(pts, o) {
  const b = { x: o.x - 3, y: o.y - 3, w: o.w + 6, h: o.h + 6 };
  for (let i = 0; i + 1 < pts.length; i++) if (intervalle(pts[i], pts[i + 1], b) && (() => { const t = intervalle(pts[i], pts[i + 1], b); return t[1] > 0 && t[0] < 1; })()) return true;
  return false;
}
function route(c0, b0, c1, b1, obst) {
  const len = Math.hypot(c1[0] - c0[0], c1[1] - c0[1]);
  const mid = [(c0[0] + c1[0]) / 2, (c0[1] + c1[1]) / 2];
  const nx = -(c1[1] - c0[1]) / len, ny = (c1[0] - c0[0]) / len;
  for (const k of [0, 0.18, -0.18, 0.32, -0.32, 0.5, -0.5]) {
    let pts;
    if (k === 0) {
      const i0 = intervalle(c0, c1, b0), i1 = intervalle(c0, c1, b1);
      if (!i0 || !i1 || i0[1] >= i1[0]) continue;
      const L = (t) => [c0[0] + (c1[0] - c0[0]) * t, c0[1] + (c1[1] - c0[1]) * t];
      pts = [L(i0[1]), L(i1[0])];
    } else {
      const q = [mid[0] + nx * k * len, mid[1] + ny * k * len], N = 240, all = [];
      for (let i = 0; i <= N; i++) { const t = i / N, u = 1 - t; all.push([u * u * c0[0] + 2 * u * t * q[0] + t * t * c1[0], u * u * c0[1] + 2 * u * t * q[1] + t * t * c1[1]]); }
      let a = 0, z = N;
      while (a < N && dedans(all[a], b0)) a++;
      while (z > 0 && dedans(all[z], b1)) z--;
      if (a >= z) continue;
      pts = all.slice(a, z + 1).filter((_, i) => i % 6 === 0 || i === z - a);
    }
    if (!obst.some((o) => frappe(pts, o))) return pts;
  }
  const i0 = intervalle(c0, c1, b0), i1 = intervalle(c0, c1, b1);
  const L = (t) => [c0[0] + (c1[0] - c0[0]) * t, c0[1] + (c1[1] - c0[1]) * t];
  return [L(i0 ? i0[1] : 0.2), L(i1 ? i1[0] : 0.8)];
}

function niveau1(m, aTop, aBot, f20) {
  const pleins = ROLES.filter((r) => r !== "inconnu" && piecesDe(r).length > 0);
  const fixes = ["origine", "mis_en_cause"].filter((r) => pleins.includes(r));
  const prim = [...fixes, ...pleins.filter((r) => !fixes.includes(r)).sort((a, b) => piecesDe(b).length - piecesDe(a).length || ROLES.indexOf(a) - ROLES.indexOf(b)).slice(0, 6 - fixes.length)];
  const primOrdre = ROLES.filter((r) => prim.includes(r));
  const autres = pleins.filter((r) => !prim.includes(r));
  const vides = (STATIQUE || M.termine) ? ROLES.filter((r) => r !== "inconnu" && !piecesDe(r).length) : [];
  const aH = aBot - aTop;

  const gut = Math.max(48, Math.round(W * 0.04));
  const tit = nd("titre", "", "h1");
  tit.id = "titre";
  const court = titreCourt(M.question);
  info(tit, court !== M.question ? M.question : "");
  let cw = 0, Wc = 0, tf = null;
  for (const frac of [0.2, 0.18, 0.165]) {
    cw = clamp(Math.round(W * frac), 200, 400);
    const dispo = W - 2 * (m + cw + gut);
    Wc = frac === 0.2 ? Math.min(Math.round(W * 0.46), dispo) : dispo;
    tf = fit(tit, court, Wc, Math.min(Math.round(W * 0.046), 84 * K), Math.round(26 * K), 2, 1.06);
    if (!tf.tient) tf = fit(tit, court, Wc, Math.min(44 * K, 2 * tf.f + 8), Math.round(22 * K), 3, 1.06);
    if (tf.tient) break;
  }
  const th = tf.h, tw = tf.w;
  const zw = Math.min(tw, Math.max(Math.round(300 * K), Math.round(0.9 * tw)), Math.round(440 * K));

  const used = new Set(), choisis = {};
  for (const r of [...primOrdre, ...autres].sort((a, b) => piecesDe(b).length - piecesDe(a).length || ROLES.indexOf(a) - ROLES.indexOf(b))) {
    const c = candidats(r).filter((u) => !used.has(u));
    choisis[r] = c;
    if (c[0]) used.add(c[0]);
  }
  // cartes : photo à cadre blanc, la taille dit le poids du rôle, bords intérieurs alignés
  const g = Math.round(18 * K);
  const nL = Math.ceil(primOrdre.length / 2), nR = Math.floor(primOrdre.length / 2);
  const cell = Math.min(Math.floor((aH - 2 * g) / 3), Math.round(cw * 0.95));
  const nmax = Math.max(1, ...primOrdre.map((r) => piecesDe(r).length));
  const colH = (n) => n * cell + (n - 1) * g;
  const slots = [];
  const obst = [];
  primOrdre.forEach((r, i) => {
    const gauche = i < nL, idx = gauche ? i : i - nL, n = gauche ? nL : nR;
    const t = 0.58 + 0.42 * Math.sqrt(piecesDe(r).length / nmax);
    const w = Math.round(cw * t), h = Math.round(cell * t);
    const cyc = aTop + (aH - colH(n)) / 2 + idx * (cell + g) + cell / 2;
    slots.push({ r, w, h, x: gauche ? m + cw - w : W - m - cw, y: cyc - h / 2, cy: cyc, gauche });
  });
  for (const s of slots) {
    const e = nd("carte:" + s.r, "carte");
    e.dataset.role = s.r;
    const n = piecesDe(s.r).length;
    const lignes = LIBELLE[s.r].length * f20 * 0.5 + 56 > s.w - 16 ? 2 : 1;
    const sh = Math.round(lignes * f20 * 1.1 + 12);
    carteRole(e, s.r, n, choisis[s.r], typeDominant(s.r), f20, sh, s.h, LIBELLE[s.r].length * f20 * 0.5 + 56 > 2 * (s.w - 16) ? COURT[s.r] : LIBELLE[s.r]);
    e.querySelector(".nom").style.webkitLineClamp = String(lignes);
    e.classList.toggle("jaune", jauneRole === s.r);
    pose(e, s.x, s.y, s.w, s.h);
    info(e, `${LIBELLE[s.r]} : ${plur(n, "pièce", "pièces")}. Cliquer pour ouvrir.`);
  }

  // colonne centrale
  const cx = W / 2;
  const ms = Math.round(72 * K), gapC = Math.round(10 * K);
  const flux = [];
  for (const r of autres) {
    const e = nd("mini:" + r, "mini");
    e.dataset.role = r;
    const cands = choisis[r];
    const sig = cands.join(",") + "|" + piecesDe(r).length + "|" + ms;
    if (e.__sig !== sig) {
      e.__sig = sig;
      e.innerHTML = `<div class="th" style="width:${ms}px;height:${ms}px"><div class="img">${imgHtml(cands, typeDominant(r))}</div><b class="cpt">${piecesDe(r).length}</b></div><div class="nm">${esc(COURT[r])}</div>`;
    }
    e.style.fontSize = f20 + "px";
    e.querySelector(".nm").style.fontSize = Math.round(17 * Math.sqrt(K)) + "px";
    e.style.width = "auto"; e.style.height = "auto";
    e.classList.toggle("jaune", jauneRole === r);
    info(e, `${LIBELLE[r]} : ${plur(piecesDe(r).length, "pièce", "pièces")}. Cliquer pour ouvrir.`);
    flux.push({ e, w: e.offsetWidth, h: e.offsetHeight });
  }
  const fluxV = [];
  const dd = Math.round(14 * K);
  const nInc = piecesDe("inconnu").length;
  if (nInc) {
    const e = nd("aclasser", "lab");
    e.dataset.role = "inconnu";
    e.style.fontSize = f20 + "px"; e.style.width = "auto"; e.style.cursor = "pointer";
    html(e, `À classer&nbsp;<b class="cpt" style="border:2px solid #fff;padding:0 6px">${nInc}</b>`);
    e.style.height = "auto";
    const h = e.offsetHeight;
    info(e, `À classer : ${plur(nInc, "pièce", "pièces")} dont le rôle reste à établir. Cliquer pour ouvrir.`);
    fluxV.push({ e, w: e.offsetWidth, h });
  }
  for (const r of vides) {
    const e = nd("point:" + r, "point");
    info(e, `${LIBELLE[r]} : aucune pièce trouvée.`);
    fluxV.push({ e, w: dd, h: dd, centre: dd });
  }
  const lignesF = [];
  const empiler = (items) => {
    let cur = null;
    for (const it of items) {
      if (!cur || cur.w + gapC + it.w > zw) { cur = { items: [], w: 0, h: it.h }; lignesF.push(cur); }
      cur.h = Math.max(cur.h, it.h);
      cur.w += (cur.items.length ? gapC : 0) + it.w;
      cur.items.push(it);
    }
  };
  empiler(flux); empiler(fluxV);
  const gapL = Math.round(8 * K);
  const miniH = lignesF.reduce((x, l) => x + l.h, 0) + Math.max(0, lignesF.length - 1) * gapL;

  const zones = M.zones;
  let zh = 0, zwz = 0;
  const ze = zones.length ? nd("zones", "zones") : null;
  if (ze) {
    const genre = ["zone d'ombre", "zones d'ombre"];
    ze.style.fontSize = f20 + "px";
    ze.innerHTML = `<div class="zt">${plur(zones.length, genre[0], genre[1])}</div>`;
    ze.style.width = "auto"; ze.style.height = "auto"; ze.style.whiteSpace = "nowrap";
    zwz = ze.offsetWidth; zh = ze.offsetHeight;
    ze.style.cursor = "pointer";
    info(ze, "Cliquer pour lire les " + zones.length + " " + (zones.length > 1 ? genre[1] : genre[0]) + ".");
  }
  let bandLines = 3;
  const bandH = () => Math.round(bandLines * f20 * 1.2 + 16 * K + 4);
  const gT = Math.round(8 * K), gB = Math.round(10 * K), gZ = Math.round(18 * K);
  const total = () => th + gT + bandH() + (miniH ? gB + miniH : 0) + (zones.length ? gZ + zh : 0);
  while (total() > aH && bandLines > 2) bandLines--;

  let y = Math.max(aTop, aTop + (aH - total()) / 2);
  pose(tit, cx - tw / 2, y, tw, th);
  const ty = y, tb = y + th;
  y = tb + gT;
  bande = { x: cx - Wc / 2, y, w: Wc, h: bandH() };
  y += bandH();
  if (miniH) {
    y += gB;
    for (const l of lignesF) {
      let x = cx - l.w / 2;
      for (const it of l.items) {
        pose(it.e, x, y + (l.h - it.h) / 2, it.e.classList.contains("lab") ? null : it.w, it.e.classList.contains("lab") ? null : it.h); if (it.e.dataset.role) obst.push({ x, y: y + (l.h - it.h) / 2, w: it.w, h: it.h });
        x += it.w + gapC;
      }
      y += l.h + gapL;
    }
    y -= gapL;
  }
  if (zones.length) { y += gZ; pose(ze, cx - zwz / 2, y, zwz, zh); obst.push({ x: cx - zwz / 2, y, w: zwz, h: zh }); }

  // traits : du centre de la question au centre de la carte, coupés au bord des deux boîtes
  const boite = { x: cx - tw / 2, y: ty, w: tw, h: th };
  const c0 = [cx, ty + th / 2];
  let tr = "";
  const autresCartes = (s) => slots.filter((o) => o !== s).map((o) => ({ x: o.x, y: o.y, w: o.w, h: o.h }));
  for (const s of slots) {
    const pts = route(c0, boite, [s.x + s.w / 2, s.cy], { x: s.x, y: s.y, w: s.w, h: s.h }, [...autresCartes(s), ...obst]);
    tr += `<polyline data-verif-ligne="${s.r}" class="${faible(s.r) ? "p" : ""}" points="${pts.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ")}"/>`;
  }
  $traits.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if ($traits.__tr !== tr) {
    $traits.__tr = tr;
    if (!STATIQUE && !document.body.classList.contains("fige")) {
      $traits.style.opacity = 0; clearTimeout($traits.__t);
      $traits.__t = setTimeout(() => { $traits.innerHTML = tr; $traits.style.opacity = 1; }, 650);
    } else { clearTimeout($traits.__t); $traits.innerHTML = tr; $traits.style.opacity = 1; }
  } else if (document.body.classList.contains("fige")) { clearTimeout($traits.__t); $traits.innerHTML = tr; $traits.style.opacity = 1; }
}

// La page des zones d'ombre (clic sur le petit cadre) : la zone choisie en grand, ce qu'on a tenté, les pistes
let zSel = 0;
function trouverPiece(id) {
  for (const r of ROLES) { const p = piecesDe(r).find((x) => x.id === id); if (p) return { p, r }; }
  return null;
}
function sousTitreZones() {
  const z = M.zones[zSel] || {};
  const n = (z.pistes || []).length;
  return `Zone ${zSel + 1} sur ${M.zones.length}. ` + (n ? `Ici, l'outil s'arrête. ${plur(n, "piste", "pistes")} pour aller plus loin.` : "Échap pour revenir.");
}
function niveauZones(m, aTop, aBot, f20) {
  $traits.innerHTML = ""; $traits.__tr = "";
  const Z = M.zones;
  if (zSel >= Z.length) zSel = 0;
  const z = Z[zSel] || { titre: "" };
  const g = Math.round(16 * K);
  const rt = nd("retour", "retour");
  rt.style.fontSize = f20 + "px";
  const rh = Math.round(f20 * 1.2 + 14);
  html(rt, "← La question");
  info(rt, "Revenir à la question. Raccourci : Échap.");
  pose(rt, m, m, null, rh);
  const sq = Math.round(22 * K), gs = Math.round(8 * K);
  const x0 = m + rt.offsetWidth + Math.round(24 * K);
  const place = W - m - Math.round(112 * K) - Math.round(32 * K) - x0;
  const nsq = Math.max(1, Math.min(Z.length, Math.floor(place / (sq + gs))));
  for (let i = 0; i < nsq; i++) {
    const e = nd("zs:" + i, "zsq");
    e.dataset.zi = i;
    e.classList.toggle("sel", i === zSel);
    pose(e, x0 + i * (sq + gs), m + (rh - sq) / 2, sq, sq);
    info(e, `${i + 1}. ${Z[i].titre}`);
  }
  const top = m + rh + Math.round(18 * K);
  const Wb = W - 2 * m;
  const tente = (z.tente || []).slice(0, 3), pistes = (z.pistes || []).slice(0, 3);
  const autour = (z.autour || []).map(trouverPiece).filter(Boolean).slice(0, 4);
  const bx = nd("zbox", "zbox");
  bx.classList.toggle("deux", tente.length > 0 || autour.length > 0);
  const maxH = Math.floor((aBot - top) * (pistes.length ? 0.44 : 0.5));
  const build = (f) => {
    bx.style.fontSize = f20 + "px";
    bx.innerHTML = `<div class="zl1"><h2 style="font-size:${f}px">${esc(z.titre)}</h2>` +
      (z.pourquoi || z.mots ? `<p>${z.pourquoi ? `<span class="tag">${esc(z.pourquoi)}</span> ` : ""}${esc(z.mots || "")}</p>` : "") + "</div>" +
      (tente.length || autour.length ? `<div class="zr1">${tente.length ? `<div class="lbl">Déjà tenté</div>${tente.map((t) => `<div>${esc(t)}</div>`).join("")}` : ""}` +
      (autour.length ? `<div class="lbl" style="margin-top:${Math.round(10 * K)}px">Autour</div><div class="aut">${autour.map(({ p, r }) => `<div class="au" data-au="${esc(p.id)}" data-aur="${r}" data-info="${esc(p.libelle || p.id)}"><div class="th" style="width:${Math.round(64 * K)}px;height:${Math.round(64 * K)}px">${imageDe(p) ? imgHtml([imageDe(p)], p.type) : picto(p.type)}</div><div class="nm">${esc(sourceDe(p))}</div></div>`).join("")}</div>` : "") + "</div>" : "");
    bx.style.width = Wb + "px"; bx.style.height = "auto";
    return bx.offsetHeight;
  };
  let h = 0;
  for (let f = Math.round(40 * K); f >= Math.round(26 * K); f -= 2) { h = build(f); if (h <= maxH) break; }
  pose(bx, m, top, Wb, h);
  const y0 = top + h + g;
  const bx0 = x0 + nsq * (sq + gs) + Math.round(16 * K);
  bande = { x: bx0, y: 8, w: Math.max(200, W - m - Math.round(112 * K) - Math.round(16 * K) - bx0), h: top - 14 };
  if (pistes.length) {
    const wc = Math.floor((Wb - 2 * g) / 3), hc = aBot - y0;
    const gw = pistes.length * wc + (pistes.length - 1) * g, gx = m + (Wb - gw) / 2;
    pistes.forEach((p, i) => {
      const e = nd("piste:" + i, "piste");
      const lignes = Math.max(1, Math.floor((hc - 2 * 14 * K - 2 * 22 * K * 1.15 - 2 * f20 * 1.2 - (f20 * 1.2 + 8) - 36) / (f20 * 1.25)));
      html(e, `<div class="pt"><span class="num">${i + 1}</span><span class="ge" style="font-size:${Math.round(22 * K)}px">${esc(p.geste || "")}</span></div>` +
        (p.ou ? `<div class="ou">${esc(p.ou)}</div>` : "") + `<div class="co" style="-webkit-line-clamp:${lignes}">${esc(p.comment || "")}</div>` +
        (p.difficulte ? `<div class="di">Difficulté : ${esc(p.difficulte)}</div>` : "") +
        ([p.url, p.lien, p.url_origine].find(estHttp) ? `<a class="src" href="${esc([p.url, p.lien, p.url_origine].find(estHttp))}" target="_blank" rel="noopener">Ouvrir la source ↗${M.cas ? " (exemple)" : ""}</a>` : ""));
      e.style.fontSize = f20 + "px";
      pose(e, gx + i * (wc + g), y0, wc, hc);
      info(e, [p.geste, p.ou, p.comment].filter(Boolean).join(" · "));
    });
  } else {
    const autres = Z.map((zz, i) => [zz, i]).filter(([, i]) => i !== zSel).slice(0, 9);
    const cols = 3, rows = Math.ceil(autres.length / cols) || 1;
    const w = Math.floor((Wb - (cols - 1) * g) / cols);
    const hh = Math.min(Math.round(120 * K), Math.floor((aBot - y0 - (rows - 1) * g) / rows));
    autres.forEach(([zz, i], k) => {
      const e = nd("zq:" + i, "zq");
      e.dataset.zi = i;
      e.style.fontSize = f20 + "px";
      html(e, esc((i + 1) + ". " + zz.titre));
      e.style.webkitLineClamp = String(Math.max(1, Math.floor((hh - 12) / (f20 * 1.2))));
      pose(e, m + (k % cols) * (w + g), y0 + Math.floor(k / cols) * (hh + g), w, hh);
      info(e, zz.titre);
    });
  }
}

// ---------------------------------------------------------------- niveau 2 : les pièces d'un rôle
function sousTitreNiveau() {
  const n = piecesDe(niveau).length;
  return `${LIBELLE[niveau]} : ${plur(n, "pièce", "pièces")}. Échap pour revenir.`;
}

function mots(p) {
  const en = p.solidite && p.solidite.en_mots;
  if (!en) return ["Non évaluée", "Non évaluée"];
  const t = en.split(";").map((s) => s.trim());
  const maj = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  return [maj(t[0] || "Non évaluée"), maj(t[1] || "Non évaluée")];
}

function cartePiece(e, p, img, f24) {
  const src = sourceDe(p), date = dateDe(p) || TYPE_LIBELLE[p.type] || "";
  const sig = ["PP", p.id, img, src, date].join("|");
  if (e.__sig !== sig) {
    e.__sig = sig;
    e.innerHTML = `<div class="img">${imgHtml(img ? [img] : [], p.type)}</div><div class="etiq"><span class="nom"><b>${esc(src)}</b><br>${esc(date)}</span></div>`;
  }
  e.classList.remove("texte");
  e.classList.remove("faible");
  e.style.fontSize = f24 + "px";
  { const t = e.querySelector(".etiq"); t.style.height = Math.round(2 * f24 * 1.15 + 12) + "px"; e.querySelector(".nom").style.webkitLineClamp = "2"; }
}

function niveau2(m, aTop, aBot, f20, lh) {
  $traits.innerHTML = ""; $traits.__tr = "";
  const r = niveau;
  const toutes = [...piecesDe(r)].sort((a, b) => rang(a) - rang(b));
  const vis = toutes.slice(0, 6);
  const reste = toutes.length - vis.length;
  if (!vis.find((p) => p.id === selId)) selId = vis[0] ? vis[0].id : null;
  const sel = vis.find((p) => p.id === selId);

  const panX = Math.round(W * 0.665), panW = W - m - panX;
  const gut = Math.max(40, Math.round(W * 0.035));
  const Wm = panX - gut - m;
  const g = Math.round(20 * K);
  const f24 = Math.round(24 * K);

  // en-tête
  const rt = nd("retour", "retour");
  rt.style.fontSize = f20 + "px";
  const rh = Math.round(f20 * 1.2 + 14);
  html(rt, "← La question");
  info(rt, "Revenir à la question. Raccourci : Échap.");
  pose(rt, m, m, null, rh);
  const t2 = nd("titre2", "", "h1");
  t2.id = "titre2";
  const tf = fit(t2, LIBELLE[r], Wm, 52 * K, Math.round(28 * K), 2, 1.06);
  const ty = m + rh + Math.round(10 * K);
  pose(t2, m, ty, tf.w, tf.h);
  const hdrBottom = ty + tf.h;

  const bandH = Math.round(3 * f20 * 1.2 + 16 * K + 4);
  bande = { x: m, y: aBot - bandH, w: Wm, h: bandH };
  const plusH = reste > 0 ? Math.round(f20 * 1.2 + 10) : 0;

  // mis en cause : le droit de réponse, une ligne
  let repH = 0;
  if (r === "mis_en_cause" && vis.length) {
    const e = nd("reponse", "reponse");
    e.style.fontSize = Math.round(28 * K) + "px";
    e.style.width = Wm + "px"; e.style.height = "auto";
    html(e, "Demander une réponse à " + esc(vis[0].libelle || vis[0].id));
    repH = e.offsetHeight;
    pose(e, m, bande.y - 12 - repH, Wm, repH);
  }
  const gridTop = hdrBottom + Math.round(24 * K);
  const gridBot = bande.y - 12 - (plusH ? plusH + 10 : 0) - (repH ? repH + 24 : 0);
  const n = vis.length;
  const cols = n <= 3 ? Math.max(n, 1) : 3;
  const rows = Math.ceil(n / cols) || 1;
  const cw = Math.min(Math.round((n === 1 ? 380 : 300) * K), Math.floor((Wm - (cols - 1) * g) / cols));
  const ch = Math.min(Math.round(cw * 0.85), Math.floor((gridBot - gridTop - (rows - 1) * g) / rows));
  const gw = cols * cw + (cols - 1) * g, gh = rows * ch + (rows - 1) * g;
  const gx = m + (Wm - gw) / 2, gy = gridTop + (gridBot - gridTop - gh) / 2;
  const usedImg = new Set();
  vis.forEach((p, i) => {
    let img = imageDe(p);
    if (img && usedImg.has(img)) img = "";
    if (img) usedImg.add(img);
    const e = nd("piece:" + p.id, "carte piece");
    e.dataset.piece = p.id;
    cartePiece(e, p, img, f24);
    e.classList.toggle("sel", p.id === selId);
    const c = i % cols, rr = Math.floor(i / cols);
    pose(e, gx + c * (cw + g), gy + rr * (ch + g), cw, ch);
    info(e, `${p.libelle || p.id} : ${mots(p)[0].toLowerCase()}.`);
  });
  if (reste > 0) {
    const e = nd("plus", "plus");
    e.style.fontSize = f20 + "px"; e.style.height = plusH + "px";
    html(e, reste > 1 ? `+${reste} autres pièces` : "+1 autre pièce");
    const pw = e.offsetWidth;
    pose(e, gx + (gw - pw) / 2, gy + gh + 10, null, plusH);
  }

  // panneau de résumé, allégé : la pièce, sa citation, ses deux notes sur une ligne, le lien
  const pn = nd("panneau", "panneau");
  pose(pn, panX, aTop, panW, aBot - aTop);
  pn.style.fontSize = f20 + "px";
  let h = "";
  if (sel) {
    const [fs, ci] = mots(sel);
    const date = dateDe(sel);
    const type = [TYPE_LIBELLE[sel.type] || sel.type, sourceDe(sel), date].filter(Boolean).join(" · ");
    const pr = (sel.preuves || [])[0];
    const ext = pr && pr.extrait ? (pr.extrait.length > 200 ? pr.extrait.slice(0, 197).trimEnd() + "…" : pr.extrait) : "";
    const aS = sel.attributs || {};
    const imgSrc = sel.image && typeof sel.image === "object" ? sel.image.source_url : "";
    const url = [pr && pr.url, aS.url, aS.url_origine, imgSrc].find(estHttp) || "";
    const sec = pr && typeof pr.debut_s === "number" ? ` (à ${Math.floor(pr.debut_s / 60)}:${String(Math.round(pr.debut_s % 60)).padStart(2, "0")})` : (sel.image && sel.image.seconde != null && sel.image.provenance === "capture" ? ` (à ${Math.floor(sel.image.seconde / 60)}:${String(Math.round(sel.image.seconde % 60)).padStart(2, "0")})` : "");
    h = `<h2 style="font-size:${Math.round(28 * K)}px">${esc(sel.libelle || sel.id)}</h2>` +
      `<div class="typ">${esc(type)}</div>` +
      (ext ? `<div class="ext" style="-webkit-line-clamp:4" title="${esc(pr.extrait)}">« ${esc(ext)} »${esc(sec)}</div>` : "") +
      `<div class="notes">${esc(fs)} · ${esc(ci.charAt(0).toLowerCase() + ci.slice(1))}</div>` +
      (imageDe(sel) && legendeImage(sel) ? `<div class="leg">${esc(legendeImage(sel))}</div>` : "") +
      (url ? `<a href="${esc(url)}" target="_blank" rel="noopener">Ouvrir la source ↗${M.cas ? " (exemple)" : ""}</a>` : "");
  }
  html(pn, h);
}

// ---------------------------------------------------------------- infobulle
const vis_ = (e) => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e); return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && parseFloat(cs.opacity) >= 0.05; };
const coupe = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

function montrerTitre(t) {
  const texte = t.getAttribute("data-info");
  if (!texte) return cacher();
  Object.assign($info.style, { display: "-webkit-box", webkitBoxOrient: "vertical", webkitLineClamp: "3", overflow: "hidden", maxHeight: "none" });
  $info.style.fontSize = Math.round(20 * K) + "px";
  $info.textContent = texte;
  const tr = t.getBoundingClientRect();
  $info.style.left = "0px"; $info.style.top = "0px"; $info.style.maxWidth = Math.round(tr.width) + "px"; $info.style.width = Math.round(tr.width) + "px";
  const h = $info.offsetHeight, w = $info.offsetWidth;
  const autres = [...document.querySelectorAll("[data-v]")].filter((e) => e !== $info && e !== t && vis_(e)).map((e) => e.getBoundingClientRect());
  const fils = [...document.querySelectorAll("#traits polyline")].map((l) => l.getAttribute("points").split(" ").map((q) => q.split(",").map(Number)));
  const x = clamp(tr.left + (tr.width - w) / 2, 6, W - 6 - w);
  for (const y of [tr.bottom + 10, tr.top - 10 - h]) {
    if (y < 6 || y + h > H - 6) continue;
    const r = { left: x, top: y, right: x + w, bottom: y + h };
    if (autres.some((o) => coupe(r, o))) continue;
    const b = { x: x - 3, y: y - 3, w: w + 6, h: h + 6 };
    if (fils.some((p) => p.some((_, i) => i + 1 < p.length && (() => { const q = intervalle(p[i], p[i + 1], b); return q && q[1] > 0 && q[0] < 1; })()))) continue;
    $info.style.left = Math.round(x) + "px"; $info.style.top = Math.round(y) + "px";
    return;
  }
  cacher();   // pas d'espace libre : la question complète reste dans le résumé
}
function montrer(t) {
  if (t.dataset.v === "titre") return montrerTitre(t);
  Object.assign($info.style, { webkitLineClamp: "", webkitBoxOrient: "", width: "" });
  const texte = t.getAttribute("data-info");
  if (!texte) return cacher();
  $info.style.display = "block";
  $info.style.fontSize = Math.round(20 * K) + "px";
  $info.textContent = texte;
  $info.style.maxHeight = "none"; $info.style.overflow = "visible";
  const tr = t.getBoundingClientRect();
  const autres = [...document.querySelectorAll("[data-v]")]
    .filter((e) => e !== $info && e !== t && vis_(e)).map((e) => e.getBoundingClientRect());
  const mesure = (mw) => { $info.style.left = "0px"; $info.style.top = "0px"; $info.style.maxWidth = mw + "px"; return { w: $info.offsetWidth, h: $info.offsetHeight }; };
  const gap = 10, mw = Math.round(360 * K);
  const essais = [];
  const cands = ["r", "l", "b", "t"];
  for (const c of cands) {
    const s = mesure(mw);
    let x, y;
    if (c === "r") { x = tr.right + gap; y = tr.top + (tr.height - s.h) / 2; }
    else if (c === "l") { x = tr.left - gap - s.w; y = tr.top + (tr.height - s.h) / 2; }
    else if (c === "b") { x = tr.left + (tr.width - s.w) / 2; y = tr.bottom + gap; }
    else { x = tr.left + (tr.width - s.w) / 2; y = tr.top - gap - s.h; }
    x = clamp(x, 6, Math.max(6, W - 6 - s.w)); y = clamp(y, 6, Math.max(6, H - 6 - s.h));
    essais.push({ x, y, s });
  }
  let choix = null;
  for (const c of essais) {
    const r = { left: c.x, top: c.y, right: c.x + c.s.w, bottom: c.y + c.s.h };
    if (!coupe(r, tr) && !autres.some((o) => coupe(r, o))) { choix = c; break; }
  }
  if (!choix && bande) {
    let s = mesure(bande.w);
    if (s.h > bande.h) { $info.style.fontSize = "18px"; s = mesure(bande.w); }
    if (s.h > bande.h) { $info.style.maxHeight = bande.h + "px"; $info.style.overflow = "hidden"; s = mesure(bande.w); }
    choix = { x: bande.x + (bande.w - s.w) / 2, y: bande.y, s };
  }
  if (!choix) { const s = mesure(mw); choix = { x: clamp(tr.right + gap, 6, W - 6 - s.w), y: clamp(tr.top, 6, H - 6 - s.h), s }; }
  $info.style.left = Math.round(choix.x) + "px";
  $info.style.top = Math.round(choix.y) + "px";
}
function cacher() { $info.style.display = "none"; }
window.__reseau = { montrer, cacher };

let minuteTitre = 0;
document.addEventListener("pointerover", (ev) => {
  const t = ev.target.closest && ev.target.closest("[data-info]");
  clearTimeout(minuteTitre);
  if (t && t.dataset.v === "titre") { cacher(); minuteTitre = setTimeout(() => montrerTitre(t), 600); }
  else if (t) montrer(t);
  else cacher();
});
document.addEventListener("pointerleave", cacher);
function activer(cible) {
  if (!cible || !cible.closest) return;
  if (cible.closest("a")) return;
  const au = cible.closest("[data-au]");
  if (au && niveau === "__zones") { niveau = au.dataset.aur; selId = au.dataset.au; cacher(); planifier(); return; }
  const piece = cible.closest(".piece");
  if (piece) { selId = piece.dataset.piece; planifier(); return; }
  if (cible.closest("#titre2,.retour")) { remonter(); return; }
  const zi = cible.closest("[data-zi]");
  if (zi && niveau === "__zones") { zSel = Number(zi.dataset.zi); cacher(); planifier(); return; }
  if (!niveau && cible.closest(".zones")) { niveau = "__zones"; cacher(); planifier(); return; }
  const r = cible.closest("[data-role]");
  if (r && !niveau) ouvrir(r.dataset.role);
}
document.addEventListener("pointerup", (ev) => {
  if (ev.button > 0) return;
  activer(document.elementFromPoint(ev.clientX, ev.clientY) || ev.target);
});
document.addEventListener("click", (ev) => activer(ev.target));
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") remonter(); });
function ouvrir(r) { niveau = r; selId = null; cacher(); planifier(); }
function remonter() { if (niveau) { niveau = null; cacher(); planifier(); } }

// ---------------------------------------------------------------- vérification (?verif)
function fige(on) { document.body.classList.toggle("fige", on); }
function mesurer() { if (attente) { cancelAnimationFrame(attente); attente = 0; } fige(true); rendre(); return window.Verif.lancer(); }
function traitsOK() {
  const mauvais = [];
  const rects = [...document.querySelectorAll("[data-v]")].filter((e) => e !== $info && vis_(e)).map((e) => ({ n: e.dataset.v, r: e.getBoundingClientRect() }));
  const lignes = [...document.querySelectorAll("#traits polyline")].map((l) => ({ n: l.getAttribute("data-verif-ligne"), p: l.getAttribute("points").split(" ").map((q) => q.split(",").map(Number)) }));
  const tit = document.getElementById("titre").getBoundingClientRect();
  for (const l of lignes) {
    for (const o of rects) {
      const b = { x: o.r.left + 0.5, y: o.r.top + 0.5, w: o.r.width - 1, h: o.r.height - 1 };
      for (let i = 0; i + 1 < l.p.length; i++) { const t = intervalle(l.p[i], l.p[i + 1], b); if (t && t[1] > 0 && t[0] < 1) { mauvais.push([l.n, o.n]); break; } }
    }
    const [ex, ey] = l.p[0];
    const sur = Math.min(Math.abs(ex - tit.left), Math.abs(ex - tit.right), Math.abs(ey - tit.top), Math.abs(ey - tit.bottom)) <= 6;
    if (!sur) mauvais.push([l.n, "ne part pas du bord du titre"]);
  }
  const cr = (a, b, c, d) => { const o = (p, q, r) => (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]); return o(a, b, c) * o(a, b, d) < 0 && o(c, d, a) * o(c, d, b) < 0; };
  for (let i = 0; i < lignes.length; i++) for (let j = i + 1; j < lignes.length; j++) {
    let x = false;
    for (let a = 0; a + 1 < lignes[i].p.length && !x; a++) for (let b = 0; b + 1 < lignes[j].p.length && !x; b++) if (cr(lignes[i].p[a], lignes[i].p[a + 1], lignes[j].p[b], lignes[j].p[b + 1])) x = true;
    if (x) mauvais.push([lignes[i].n, "croise", lignes[j].n]);
  }
  return mauvais;
}
const instants = [];
function verifier() {
  if (!window.Verif) return;
  const base = mesurer();
  const echecs = [];
  let teste = 0;
  if (niveau === null && document.getElementById("titre")) for (const m of traitsOK()) echecs.push(["trait", ...m]);
  for (const t of [...document.querySelectorAll("[data-info]")]) {
    montrer(t);
    const r = window.Verif.lancer();
    teste++;
    for (const c of r.conflits) echecs.push(["infobulle sur " + (t.dataset.v || t.className), ...c]);
    for (const h of r.hors) echecs.push(["infobulle hors fenêtre sur " + (t.dataset.v || t.className), h.nom]);
  }
  cacher();
  const prem = document.querySelector("[data-info]");
  if (prem) montrer(prem);
  const echInst = instants.filter((i) => !i.ok).length;
  window.Verif.lancer({ extra: { mode: STATIQUE ? "statique" : "flux", infobulles_testees: teste, infobulles_en_conflit: echecs, sans_infobulle: { conflits: base.conflits.length, hors: base.hors.length }, instants }, echecs: echecs.length + echInst });
  cacher();
  fige(false);
}
function programmerVerifFlux() {
  for (const t of [4000, 9000, 14000]) {
    setTimeout(() => { const r = mesurer(); instants.push({ t, ok: r.ok, conflits: r.conflits.length, hors: r.hors.length, elements: r.elements }); fige(false); }, t);
  }
}

// ---------------------------------------------------------------- cycle
let attente = 0;
function planifier() { if (attente) return; attente = requestAnimationFrame(() => { attente = 0; rendre(); }); }
let rz = 0;
window.addEventListener("resize", () => {
  document.body.classList.add("rz");
  planifier();
  clearTimeout(rz);
  rz = setTimeout(() => document.body.classList.remove("rz"), 150);
});
if (document.fonts && document.fonts.addEventListener) document.fonts.addEventListener("loadingdone", planifier);

function signalerJaune(r) {
  jauneRole = r;
  clearTimeout(signalerJaune.t);
  signalerJaune.t = setTimeout(() => { jauneRole = null; planifier(); }, 600);
}

let verifFaite = false;
const seuils = [];
async function chargerVignettes() {
  try {
    const r = await fetch(`exclues-${encodeURIComponent(ID)}.json`, { cache: "no-store" });
    if (r.ok) { const j = await r.json(); EXCL = new Set(j.exclues || []); }
  } catch (_) { /* pas de liste d'exclusions */ }
  try {
    const r = await fetch(`vignettes-${encodeURIComponent(ID)}.json`, { cache: "no-store" });
    if (r.ok) VIGN = await r.json();
  } catch (_) { /* pas de vignettes pour cette enquête : cadres neutres */ }
}
async function demarrer() {
  await chargerVignettes();
  if (STATIQUE) document.body.classList.add("statique");
  const avecPolice = document.fonts && document.fonts.ready ? Promise.race([document.fonts.ready, new Promise((r) => setTimeout(r, 2500))]) : Promise.resolve();
  if (STATIQUE) {
    const rep = await fetch(`${BASE}/api/export?enquete=${encodeURIComponent(ID)}`);
    const d = await rep.json();
    M = depuisExport(d);
    await avecPolice;
    const ro = P.get("role");
    if (ro && ROLES.includes(ro)) niveau = ro;
    if (ro === "zones") niveau = "__zones";
    rendre();
    return;
  }
  // direct ou rejeu : la question d'abord, les rôles quand leurs pièces arrivent
  M = { question: questionTapee, cas: ID === "stylos-minceur", roles: Object.fromEntries(ROLES.map((r) => [r, []])), zones: [], zonesGenre: "question", lectures: null, termine: false };
  await avecPolice;
  if (!questionTapee) {
    try {
      const rep = await fetch(`${BASE}/api/export?enquete=${encodeURIComponent(ID)}`);
      const d = await rep.json();
      const q = typeof d.question === "string" ? d.question : (d.question && d.question.texte) || "";
      if (q) { M.question = q; questionTapee = q; if (MODE === "direct") M.cas = M.cas || depuisExport(d).cas; }
    } catch (_) { /* export encore vide : la question arrivera par le flux */ }
  }
  planifier();
  if (VERIF) {
    try {
      const d = await (await fetch(`${BASE}/api/export?enquete=${encodeURIComponent(ID)}`)).json();
      const T = depuisExport(d);
      const tot = ROLES.reduce((x, r) => x + (T.roles[r] || []).length, 0);
      for (const f of [0.25, 0.5, 0.75]) seuils.push({ n: Math.max(1, Math.round(tot * f)) });
    } catch (_) { /* pas de seuils */ }
  }
  try {
    const mod = await import(BASE + "/interface/branchement.js");
    mod.configurer({ base: BASE });
    mod.suivre(ID, {
      mode: MODE, dureeCible: 22,
      quand: (evt, etat) => {
        M = depuisEtat(etat);
        const d = etat.derniere;
        if (d && d.genre === "noeud" && d.role && evt.type === "noeud") {
          if (!(M.roles[d.role] && M.roles[d.role].length > 1)) { /* premier nœud du rôle */ }
          signalerJaune(d.role);
        }
        planifier();
        if (VERIF) {
          const p = totalPieces();
          while (seuils.length && p >= seuils[0].n) {
            const sq = seuils.shift();
            const r = mesurer();
            instants.push({ a_pieces: sq.n, pieces: p, ok: r.ok, conflits: r.conflits.length, hors: r.hors.length, elements: r.elements });
            fige(false);
          }
          if (etat.termine && !verifFaite) { verifFaite = true; verifier(); }
        }
      },
    });
  } catch (err) {
    console.error("flux indisponible", err);
  }
}
demarrer();
