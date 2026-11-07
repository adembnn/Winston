// Test du branchement : node interface/test_branchement.mjs
// Démarre le vrai serveur (python -m enquete.serveur, port libre, racine temporaire), y copie le
// journal d'un vrai passage et le rejoue. Aucun appel à Claude ni à Oriane.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { creerBranchement, etatInitial, appliquer } from "./branchement.js";

const DEPOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
// Le rejeu d'un vrai passage demande son journal : JOURNAL_REEL=<chemin du journal.sqlite>. Sans lui, ce test est sauté.
const JOURNAL_REEL = process.env.JOURNAL_REEL || "";

const attendre = (ms) => new Promise((ok) => setTimeout(ok, ms));
let reussis = 0;
async function test(nom, corps) {
  try { await corps(); reussis++; console.log("ok   " + nom); }
  catch (e) { console.error("ECHEC " + nom + "\n" + (e.stack || e)); process.exitCode = 1; }
}

// ---------------------------------------------------------------------------
// surFrappe, avec un faux fetch
// ---------------------------------------------------------------------------

function fauxFetch(reponses) {
  const appels = [];
  const aller = async (url) => {
    const u = new URL(url, "http://x");
    const appel = { q: u.searchParams.get("q"), profond: u.searchParams.get("profond"),
      graine: u.searchParams.get("graine"), t: Date.now() };
    appels.push(appel);
    const { delai = 0, corps } = reponses(appel);
    await attendre(delai);
    return { ok: true, status: 200, json: async () => corps };
  };
  return { aller, appels };
}

await test("surFrappe : un seul appel après plusieurs frappes rapprochées (150 ms)", async () => {
  const { aller, appels } = fauxFetch((a) => ({ corps: { locales: { mots: [a.q] } } }));
  const b = creerBranchement({ fetch: aller, delaiProfond: 5000 });
  const recus = [];
  for (const t of ["s", "sé", "séi", "séis"]) { b.surFrappe(t, "", (r) => recus.push(r)); await attendre(40); }
  assert.equal(appels.length, 0, "rien n'est parti avant la fin du délai");
  await attendre(250);
  assert.equal(appels.length, 1);
  assert.equal(appels[0].q, "séis");
  assert.equal(appels[0].profond, null);
  assert.deepEqual(recus.map((r) => r.locales.mots[0]), ["séis"]);
  assert.equal(recus[0].enCours, true, "la lecture profonde est attendue");
  b.surFrappe("", "", () => {});  // annule le minuteur profond
});

await test("surFrappe : 1,5 s sans frappe, la lecture profonde part (profond=1)", async () => {
  const { aller, appels } = fauxFetch((a) => ({
    corps: a.profond ? { locales: { mots: ["m"] }, profond: { reformulation: "R" } } : { locales: { mots: ["m"] } } }));
  const b = creerBranchement({ fetch: aller, delaiFrappe: 20, delaiProfond: 150 });
  const recus = [];
  b.surFrappe("séisme", "https://www.tiktok.com/@a/video/1", (r) => recus.push(r));
  await attendre(400);
  assert.deepEqual(appels.map((a) => a.profond), [null, "1"]);
  assert.equal(appels[1].graine, "https://www.tiktok.com/@a/video/1");
  assert.equal(recus.length, 2);
  assert.equal(recus[0].profond, null);
  assert.equal(recus[1].profond.reformulation, "R");
  assert.equal(recus[1].enCours, false);
});

await test("surFrappe : une réponse périmée n'écrase jamais une plus récente", async () => {
  // La 1re requête (« a ») est lente, la 2de (« ab ») est rapide : « a » arrive en dernier.
  const { aller } = fauxFetch((a) => ({ delai: a.q === "a" ? 400 : 5, corps: { locales: { mots: [a.q] } } }));
  const b = creerBranchement({ fetch: aller, delaiFrappe: 30, delaiProfond: 5000 });
  const recus = [];
  b.surFrappe("a", "", (r) => recus.push(r.locales.mots[0]));
  await attendre(80);   // la requête « a » est partie
  b.surFrappe("ab", "", (r) => recus.push(r.locales.mots[0]));
  await attendre(600);  // « a » est revenue entre-temps
  assert.deepEqual(recus, ["ab"]);
  b.surFrappe("", "", () => {});
});

await test("surFrappe : une réponse rapide tardive n'écrase pas la lecture profonde", async () => {
  const { aller } = fauxFetch((a) => a.profond
    ? { delai: 5, corps: { locales: { mots: ["profond"] }, profond: { reformulation: "R" } } }
    : { delai: 300, corps: { locales: { mots: ["rapide"] } } });
  const b = creerBranchement({ fetch: aller, delaiFrappe: 10, delaiProfond: 50 });
  const recus = [];
  b.surFrappe("séisme", "", (r) => recus.push(r));
  await attendre(500);
  assert.equal(recus.length, 1);
  assert.equal(recus[0].profond.reformulation, "R");
});

// ---------------------------------------------------------------------------
// Le reste, contre le vrai serveur
// ---------------------------------------------------------------------------

function demarrerServeur(racine) {
  return new Promise((ok, ko) => {
    const proc = spawn(process.env.PYTHON || "python",
      ["-m", "enquete.serveur", "--racine", racine, "--port", "0"],
      { cwd: DEPOT, env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" } });
    let sortie = "";
    const delai = setTimeout(() => ko(new Error("le serveur ne démarre pas : " + sortie)), 15000);
    proc.stdout.on("data", (d) => {
      sortie += d;
      const m = /Serveur sur (http:\/\/[\d.]+:\d+)/.exec(sortie);
      if (m) { clearTimeout(delai); ok({ proc, base: m[1] }); }
    });
    proc.stderr.on("data", (d) => { sortie += d; });
    proc.on("exit", (c) => { clearTimeout(delai); ko(new Error("serveur arrêté (" + c + ") : " + sortie)); });
  });
}

const racine = mkdtempSync(join(tmpdir(), "branchement-"));
let serveur = null;
if (!JOURNAL_REEL || !existsSync(JOURNAL_REEL)) {
  console.log("saut rejeu d'un vrai passage : JOURNAL_REEL non défini ou introuvable");
  rmSync(racine, { recursive: true, force: true });
  console.log(`\n${reussis} tests passés`);
  process.exit(process.exitCode || 0);
}
try {
  mkdirSync(join(racine, "affaire-10"));
  copyFileSync(JOURNAL_REEL, join(racine, "affaire-10", "reseau.sqlite"));
  serveur = await demarrerServeur(racine);
  const b = creerBranchement({ base: serveur.base, transport: "fetch" });

  await test("suivre en rejeu : ordre, rôles = export, vidéos lues, mention, fin, durée", async () => {
    const evenements = [];
    const t0 = Date.now();
    const final = await new Promise((ok, ko) => {
      const arret = b.suivre("affaire-10", {
        mode: "rejeu", dureeCible: 5,
        quand: (evt, etat) => {
          evenements.push(evt);
          if (evt.type === "erreur") ko(new Error(evt.donnees.message));
          if (etat.termine) ok({ etat, duree: (Date.now() - t0) / 1000 });
        },
      });
      setTimeout(() => { arret(); ko(new Error("pas de fin en 20 s")); }, 20000);
    });
    const { etat, duree } = final;

    // 1. dans l'ordre
    const ns = evenements.map((e) => e.n);
    assert.ok(ns.length > 100, "trop peu d'événements : " + ns.length);
    assert.deepEqual(ns, ns.map((_, i) => i + 1), "les n se suivent sans trou ni retour");
    assert.equal(evenements[0].type, "question");
    assert.equal(evenements.at(-1).type, "fin");

    // 2. les pièces par rôle = l'export
    const exp = await b.exporter("affaire-10");
    const attendu = {};
    for (const d of exp.dossiers) attendu[d.role] = d.pieces.map((p) => p.id).sort();
    const obtenu = {};
    for (const [role, liste] of Object.entries(etat.pieces)) obtenu[role] = liste.map((p) => p.id).sort();
    for (const role of new Set([...Object.keys(attendu), ...Object.keys(obtenu)])) {
      assert.deepEqual(obtenu[role] || [], attendu[role] || [], "pièces du rôle " + role);
    }
    const libellesExport = new Map(exp.dossiers.flatMap((d) => d.pieces.map((p) => [p.id, p.libelle])));
    for (const p of Object.values(etat.pieces).flat()) assert.equal(p.libelle, libellesExport.get(p.id));
    assert.equal(Object.values(etat.pieces).flat().length, exp.compteurs.pieces);
    assert.equal(etat.liens.length, exp.compteurs.liens);
    assert.equal(etat.question, exp.question);
    assert.equal(etat.questions.length, exp.questions_ouvertes.length);
    assert.deepEqual(etat.questions.map((q) => q.numero), etat.questions.map((_, i) => i + 1));
    assert.ok(etat.recherches.length > 0 && etat.recherches[0].mot && etat.recherches[0].champ);

    // 3. compteurs, mention, fin, durée
    assert.ok(etat.compteurs.videos_lues > 0, "videos_lues = " + etat.compteurs.videos_lues);
    assert.equal(etat.compteurs.pieces, exp.compteurs.pieces);
    assert.ok(etat.mention && etat.mention.includes("×"), "mention : " + etat.mention);
    assert.match(etat.mention, /^Rejoué ×\d/);
    assert.ok(etat.mention.endsWith("ramenées à 5 s"), etat.mention);
    assert.equal(etat.termine, true);
    assert.equal(etat.ok, true);
    assert.equal(etat.etape, null);
    assert.ok(etat.derniere && ["noeud", "question"].includes(etat.derniere.genre) && etat.derniere.id,
      "la dernière arrivée porte le jaune");
    const SRC = new Set(["video", "publication_texte", "document", "site", "societe", "personne"]);
    for (const d of exp.dossiers) {
      assert.equal(etat.compteurs.sources_par_role[d.role], d.pieces.filter((p) => SRC.has(p.type)).length, "sources " + d.role);
      assert.equal(etat.compteurs.par_role[d.role] || 0, d.pieces.length, "par_role " + d.role);
    }
    assert.ok(Object.values(etat.compteurs.sources_par_role).reduce((a, b) => a + b, 0) > 0);
    assert.ok(Math.abs(duree - 5) <= 1.5, "durée " + duree + " s");
    console.log("     " + ns.length + " événements en " + duree.toFixed(2) + " s · " + etat.mention +
      " · vidéos lues " + etat.compteurs.videos_lues);
  });

  await test("suivre en direct sur un journal fini, puis arrêt anticipé, export d'une enquête inconnue", async () => {
    let dernier = null;
    await new Promise((ok, ko) => {
      b.suivre("affaire-10", { mode: "direct", quand: (evt, etat) => {
        dernier = etat;
        if (evt.type === "erreur") ko(new Error(evt.donnees.message));
        if (etat.termine) ok();
      } });
    });
    assert.equal(dernier.accelere, false);
    assert.equal(dernier.mention, null);
    const arret = b.suivre("affaire-10", { mode: "rejeu", dureeCible: 5, quand: () => {} });
    arret();
    await assert.rejects(b.exporter("inconnue"), /enquête inconnue/);
  });

  await test("état recalculé depuis le flux seul : une même version remplace la précédente", async () => {
    const e = etatInitial();
    const noeud = (role, libelle) => ({ n: e.n + 1, type: "noeud", donnees: { id: "x", role, libelle, type: "compte" } });
    appliquer(e, noeud("inconnu", "v1"));
    appliquer(e, noeud("origine", "v2"));
    assert.equal(e.pieces.inconnu.length, 0);
    assert.deepEqual(e.pieces.origine.map((p) => p.libelle), ["v2"]);
  });

  await test("etat.zones : dernière version de chaque zone, ordre d'apparition, derniere = la zone", async () => {
    const e = etatInitial();
    assert.deepEqual(e.zones, []);
    const zone = (id, titre, version, role) => ({ n: e.n + 1, type: "zone", donnees: {
      id, titre, version, role_manquant: role, pourquoi: "p", tente: "t", autour: [],
      pistes: [{ geste: "g", ou: "o", comment: "c", difficulte: "facile" }] } });
    appliquer(e, zone("z1", "Qui dirige ?", 1, "mis_en_cause"));
    appliquer(e, zone("z2", "Qui fabrique ?", 1, "origine"));
    assert.deepEqual(e.derniere, { genre: "zone", id: "z2", role: "origine", type: "zone", titre: "Qui fabrique ?" });
    appliquer(e, zone("z1", "Qui dirige vraiment ?", 2, "mis_en_cause"));
    assert.deepEqual(e.zones.map((z) => [z.id, z.version]), [["z1", 2], ["z2", 1]]);
    assert.equal(e.zones[0].titre, "Qui dirige vraiment ?");
    assert.equal(e.derniere.id, "z1");
  });

  await test("resumeEnquete : bonne route, profond=0 seulement si profond: false", async () => {
    const urls = [];
    const faux = creerBranchement({ base: "http://x", fetch: async (url) => {
      urls.push(url);
      return { ok: true, status: 200, json: async () => ({ resume: {} }) };
    } });
    await faux.resumeEnquete("e1");
    await faux.resumeEnquete("e1", { profond: false });
    await faux.resume("e1", "p1");
    assert.deepEqual(urls, [
      "http://x/api/resume?enquete=e1",
      "http://x/api/resume?enquete=e1&profond=0",
      "http://x/api/resume_piece?enquete=e1&piece=p1",
    ]);
  });

  await test("resumeEnquete contre le vrai serveur : contrat, ids réels ; resume(piece) répond", async () => {
    const exp = await b.exporter("affaire-10");
    const { resume: r } = await b.resumeEnquete("affaire-10", { profond: false });
    assert.deepEqual(Object.keys(r).sort(), ["acteurs", "certitude", "chiffres", "chronologie", "en_bref",
      "etabli", "mecanisme", "ombre", "prochaines_etapes", "qui_appeler", "source"]);
    assert.equal(r.source, "code");
    assert.equal(r.chiffres.sources_independantes, null);
    assert.deepEqual(r.ombre, exp.zones_d_ombre.map((z) => z.id));
    const ids = new Set(exp.dossiers.flatMap((d) => d.pieces.map((p) => p.id)));
    for (const x of [...r.etabli, ...r.chronologie, ...r.qui_appeler]) assert.ok(ids.has(x.piece_id), x.piece_id);
    assert.ok(["non établi", "possible", "probable"].includes(r.certitude), r.certitude);
    const piece = exp.dossiers.flatMap((d) => d.pieces)[0];
    const rp = await b.resume("affaire-10", piece.id);
    assert.equal(rp.piece, piece.id);
    assert.equal(typeof rp.resume, "string");
  });
} finally {
  if (serveur) serveur.proc.kill();
  await attendre(200);
  try { rmSync(racine, { recursive: true, force: true }); } catch { /* le fichier est peut-être encore tenu */ }
}

console.log(process.exitCode ? "\nÉCHEC" : "\n" + reussis + " tests passés");
