/* flux.js : branche la maquette « réseau d'enquête » sur le vrai flux d'événements d'une enquête.
 * Ligne à ajouter dans le HTML, avant </body> :   <script type="module" src="flux.js"></script>
 * Actif seulement si l'adresse contient ?flux (option : &enquete=<id>&mode=rejeu|direct ; défaut stylos-minceur, rejeu).
 * Sans ?flux, ne fait rien : la démo scriptée reste intacte. Ne touche pas au DOM : tout passe par window.Mouvement.
 * Serveur absent ou erreur : un console.warn, puis Mouvement.fin() rend la page dans son état final normal. */

const QS = new URLSearchParams(location.search);
const BASE = QS.get('base') || QS.get('serveur') || 'http://127.0.0.1:8802';

/* rôle du flux -> id de rôle de la page */
const ROLE = {
  origine: 'origine', amplificateur: 'amplificateurs', beneficiaire: 'beneficiaires', infrastructure: 'infrastructure',
  victime: 'victimes', temoin: 'temoins', expert: 'experts', contradicteur: 'contradicteurs',
  mis_en_cause: 'miscause', inconnu: 'inconnus',
};
const DELAI_INCONNU = 400;   /* un « inconnu » peut encore être reclassé */
const DELAI_IMAGE = 260;     /* laisse arriver l'image d'un rôle si elle suit de peu */
const MAX_QUESTIONS_PAGE = 5; /* la page n'a que cinq lignes numérotées (la sixième est la saisie) */

const PLATEFORME = { tiktok: 'TikTok', instagram: 'Instagram', youtube: 'YouTube', x: 'X', reddit: 'Reddit', telegram: 'Telegram', bluesky: 'Bluesky' };
const CHAMP = { parole: 'parole', texte_ecran: "texte à l'écran", legende: 'légende', image: 'image', son: 'son', description: 'légende' };
const ETAPE = {
  type_affaire: "Qualifie le type d'affaire", chercher_candidats: 'Cherche les contenus', relire_en_full: 'Relit les plus pertinents',
  regrouper: 'Regroupe les formulations', archiver: 'Archive les preuves', construire_reseau: 'Construit le réseau',
};
const TYPE_LIBELLE = [['publication_texte', 'publication', 'publications'], ['document', 'document', 'documents'], ['site', 'site', 'sites'], ['societe', 'société', 'sociétés'], ['personne', 'personne', 'personnes']];

const mv = () => window.Mouvement;
const court = (t, n = 84) => { t = String(t || '').trim(); return t.length > n ? t.slice(0, n - 1).replace(/\s+\S*$/, '') + '…' : t; };
const sur = (f) => { try { return f(); } catch (e) { console.warn('[flux]', e && e.message ? e.message : e); } };
const dormir = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  if (!QS.has('flux')) return;
  const idEnquete = QS.get('enquete') || 'stylos-minceur';
  const mode = QS.get('mode') === 'direct' ? 'direct' : 'rejeu';

  /* la page et Mouvement doivent être prêts ; Mouvement lance lui-même depart() avec ?flux en passant au niveau 1 */
  for (let i = 0; i < 40 && !mv(); i++) await dormir(100);
  if (!mv()) { console.warn('[flux] Mouvement absent'); return; }
  for (let i = 0; i < 30 && !document.documentElement.classList.contains('mv-hs'); i++) await dormir(100);
  if (!document.documentElement.classList.contains('mv-hs')) sur(() => mv().depart());
  await dormir(500); /* l'impulsion de départ passe d'abord */

  let branchement;
  try {
    branchement = await import(BASE + '/interface/branchement.js');
    branchement.configurer({ base: BASE });
  } catch (e) {
    console.warn('[flux] serveur d\'enquête injoignable (' + BASE + ')', e && e.message);
    sur(() => mv().fin());
    return;
  }

  /* ---------- état propre au branchement ---------- */
  const roleDuNoeud = new Map();   // id de nœud -> rôle du flux (dernière version)
  const montre = new Set();        // ids de rôles page déjà apparus
  const attente = new Map();       // id de rôle page -> {timer, image}
  const compteurAffiche = new Map();
  const timersCompteur = new Map();
  const lienParRole = new Map();   // id page -> {prouve:Set, indice:Set} d'ids de liens
  const lienAffiche = new Map();   // id page -> 'prouve' | 'indice' | 'casse'
  const liens = new Map();         // id de lien -> {source, cible, etat}
  const questionsVues = new Set();
  let dernierEtat = null;
  let derniereRecherche = null, tRecherche = 0, etapeTexte = '', facteur = null;
  let derniereLigne = '', fini = false, casFictif = false;

  const imageDe = (d) => {
    const a = d.attributs || {};
    if (a.image && /^https?:/.test(a.image)) return a.image;
    const k = Array.isArray(a.images_cles) ? a.images_cles.find((x) => x && /^https?:/.test(x.url)) : null;
    return k ? k.url : null;
  };

  function appliquerLiens(pid) {
    if (!montre.has(pid)) return;
    const l = lienParRole.get(pid); if (!l) return;
    let cible = null;
    if (l.prouve.size) cible = 'prouve'; else if (l.indice.size) cible = 'indice'; else if (l.casses) cible = 'casse';
    if (!cible || lienAffiche.get(pid) === cible) return;
    lienAffiche.set(pid, cible);
    sur(() => mv().lien(pid, cible));
  }

  function montrer(pid, image) {
    if (montre.has(pid) || fini) return;
    montre.add(pid);
    let n;
    if (pid !== 'inconnus') {
      const s = (dernierEtat && dernierEtat.compteurs && dernierEtat.compteurs.sources_par_role) || {};
      const r = Object.keys(ROLE).find((k) => ROLE[k] === pid);
      n = s[r] || 0;
      compteurAffiche.set(pid, n);
    }
    sur(() => mv().arriveRole(pid, { image: image || undefined, compteur: n }));
    setTimeout(() => appliquerLiens(pid), 120);
    if (pid === 'inconnus') setTimeout(syncInconnus, 1000);
  }

  /* le compteur des questions ouvertes : la révélation du rôle « inconnus » le remet à 0 ; on le recale après coup */
  let syncJusqua = 0, syncTimer = null;
  function syncInconnus() {
    if (mv().zone) return;
    syncJusqua = Date.now() + 12000;
    if (syncTimer) return;
    syncTimer = setInterval(() => {
      const etat = dernierEtat;
      if (!etat || Date.now() > syncJusqua) { clearInterval(syncTimer); syncTimer = null; return; }
      if (!montre.has('inconnus') || (fini && casFictif)) return;
      const n = etat.questions.filter((q) => q.numero <= MAX_QUESTIONS_PAGE && q.statut !== 'comblee').length;
      const c = document.querySelector('.lab.dzl [data-compteur]');
      if (c && parseInt(c.textContent, 10) !== n) sur(() => mv().compteur('inconnus', n));
    }, 700);
  }

  function planifierRole(pid, image, delai) {
    if (montre.has(pid)) return;
    const a = attente.get(pid);
    if (a) { if (image && !a.image) a.image = image; return; }
    const o = { image, timer: setTimeout(() => {
      attente.delete(pid);
      if (pid === 'inconnus' && !(dernierEtat && (dernierEtat.pieces.inconnu || []).length)) return; // tout reclassé entre-temps
      montrer(pid, o.image);
    }, delai) };
    attente.set(pid, o);
  }

  function majCompteur(pid, n) {
    if (pid === 'inconnus' || !montre.has(pid)) return;
    if (compteurAffiche.get(pid) === n) return;
    compteurAffiche.set(pid, n);
    clearTimeout(timersCompteur.get(pid));
    timersCompteur.set(pid, setTimeout(() => sur(() => mv().compteur(pid, n)), 120));
  }

  function tousCompteurs(etat) {
    const s = (etat.compteurs && etat.compteurs.sources_par_role) || {};
    for (const [r, n] of Object.entries(s)) { const pid = ROLE[r]; if (pid) { compteurAffiche.set(pid, compteurAffiche.get(pid)); majCompteur(pid, n); } }
  }

  /* ---------- ligne vivante : une seule ligne ---------- */
  function ligneCompteurs(etat) {
    const c = etat.compteurs || {};
    const parts = [];
    if (c.videos_lues) parts.push(c.videos_lues + (c.videos_lues > 1 ? ' vidéos lues' : ' vidéo lue'));
    for (const [t, un, plusieurs] of TYPE_LIBELLE) {
      let n = 0;
      for (const liste of Object.values(etat.pieces)) n += liste.filter((p) => p.type === t).length;
      if (n) parts.push(n + ' ' + (n > 1 ? plusieurs : un));
    }
    return parts.slice(0, 4).join(', ');
  }
  function suffixe() {
    if (!(facteur > 1)) return '';
    const k = facteur >= 10 ? Math.round(facteur) : String(Math.round(facteur * 10) / 10).replace('.', ',');
    return ' · rejoué ×' + k;
  }
  function ligneRecherche(r) {
    const p = PLATEFORME[r.source_plateforme || r.plateforme] || r.plateforme || '';
    const ch = CHAMP[r.champ];
    const mot = String(r.mot || '').slice(0, 42);
    const total = r.total != null ? ', ' + r.total + ' résultat' + (r.total > 1 ? 's' : '') : '';
    return 'Recherche : « ' + mot + ' »' + (ch ? ' (' + ch + ')' : '') + (p ? ' sur ' + p : '') + total;
  }
  function ecrireLigne() {
    if (fini || !dernierEtat) return;
    let t;
    if (derniereRecherche && Date.now() - tRecherche < 1400) t = ligneRecherche(derniereRecherche);
    else t = ligneCompteurs(dernierEtat) || etapeTexte;
    if (!t) return;
    t += suffixe();
    if (t !== derniereLigne) { derniereLigne = t; sur(() => mv().ligne(t)); }
  }
  const ticker = setInterval(ecrireLigne, 450);

  /* ---------- zones d'ombre (contrat : kickoff/design/zones-ombre.md) ---------- */
  const zonesEnvoyees = new Map(); // id -> signature de la dernière version envoyée
  let zonesServeur = false;        // le serveur a émis au moins une vraie zone : le repli ne sert plus
  function envoyerZone(z, duServeur = true) {
    if (!z || !z.id || !mv().zone) return;
    const sig = JSON.stringify([z.version, z]);
    if (zonesEnvoyees.get(z.id) === sig) return; /* une fois par zone, puis seulement si nouvelle version */
    zonesEnvoyees.set(z.id, sig);
    if (duServeur) zonesServeur = true;
    const rm = z.role_manquant;
    sur(() => mv().zone({ tente: [], autour: [], pistes: [], ...z, role_manquant: rm ? (ROLE[rm] || rm) : null })); /* beneficiaire -> beneficiaires, etc. */
  }
  function zonesDuFlux(d) { return d && Array.isArray(d.zones_d_ombre) ? d.zones_d_ombre : []; }

  /* ---------- traitement d'un événement ---------- */
  function surEvenement(evt, etat) {
    dernierEtat = etat;
    if (evt.accelere && evt.facteur) facteur = evt.facteur;
    const d = evt.donnees || {};
    if (evt.type === 'zone') envoyerZone(d);
    if (etat && Array.isArray(etat.zones) && evt.type === 'zone') etat.zones.forEach(envoyerZone);
    zonesDuFlux(d).forEach(envoyerZone);
    switch (evt.type) {
      case 'erreur':
        console.warn('[flux] flux interrompu :', d.message);
        terminer(etat);
        return;
      case 'question': {
        if (d.centrale) { casFictif = d.cas === 'fictif'; /* le cas fictif : la page l'affiche déjà (« Cas fictif » aussi) ; un vrai cas : on remplace le titre */
          if (!d.cas) sur(() => { const q = document.getElementById('q'); if (!q || !d.texte) return;
            const t = court(d.texte, 78), m = Math.round(t.length / 2);
            let c = t.lastIndexOf(' ', m); if (c < 8) c = t.indexOf(' ', m);
            q.textContent = ''; /* deux lignes équilibrées, comme le titre de la maquette */
            q.appendChild(document.createTextNode(c > 0 ? t.slice(0, c) : t));
            if (c > 0) { q.appendChild(document.createElement('br')); q.appendChild(document.createTextNode(t.slice(c + 1))); }
          });
          break;
        }
        if (mv().zone) break; /* les zones d'ombre remplacent la liste : Mouvement.question ne sert que de repli */
        const q = etat.questions.find((x) => x.id === d.id);
        if (!q || q.numero > MAX_QUESTIONS_PAGE) break;
        if (!questionsVues.has(q.id)) { questionsVues.add(q.id); sur(() => mv().question(q.numero, 'ouverte', court(q.texte))); }
        if (q.statut === 'comblee') sur(() => mv().question(q.numero, 'fermee', court(q.texte)));
        if (montre.has('inconnus')) syncInconnus();
        break;
      }
      case 'noeud': {
        const pid = ROLE[d.role];
        roleDuNoeud.set(d.id, d.role);
        if (!pid) break;
        const img = imageDe(d);
        if (pid === 'inconnus') planifierRole(pid, null, DELAI_INCONNU);
        else planifierRole(pid, img, DELAI_IMAGE);
        tousCompteurs(etat);
        break;
      }
      case 'lien': {
        const st = String(d.statut || '') + ' ' + String(d.motif || '');
        const mort = /retir|invalid|ecart|écart|rejet|supprim|casse|annul/i.test(st) || d.retire === true;
        const solide = !mort && d.statut === 'prouve' && ((d.solidite && d.solidite.matiere) || 'solide') !== 'indice' && ((d.solidite && d.solidite.matiere) || 'solide') !== 'inverifiable';
        const rs = [roleDuNoeud.get(d.source_id), roleDuNoeud.get(d.cible_id)].map((r) => ROLE[r]).filter((p) => p && p !== 'inconnus');
        const pid = rs[0] || (ROLE[roleDuNoeud.get(d.source_id)] || ROLE[roleDuNoeud.get(d.cible_id)]);
        if (!pid) break;
        const l = lienParRole.get(pid) || { prouve: new Set(), indice: new Set(), casses: 0 };
        lienParRole.set(pid, l);
        l.prouve.delete(d.id); l.indice.delete(d.id);
        if (mort) l.casses++; else if (solide) l.prouve.add(d.id); else l.indice.add(d.id);
        appliquerLiens(pid);
        break;
      }
      case 'compteur': tousCompteurs(etat); break;
      case 'recherche': derniereRecherche = d; tRecherche = Date.now(); break;
      case 'etape': if (d.etat === 'debut' && ETAPE[d.nom]) etapeTexte = ETAPE[d.nom]; break;
      case 'fin': terminer(etat); return;
      default: break;
    }
  }

  async function zonesFinales(etat) {
    if (!mv().zone) return;
    try {
      const ex = await branchement.exporter(idEnquete);
      zonesDuFlux(ex).forEach(envoyerZone);
    } catch (e) { console.warn('[flux] export indisponible', e && e.message); }
    if (zonesServeur) return;
    /* le serveur ne fournit pas encore de zones détaillées : une zone simple par question restée ouverte (titre seul, sans pistes inventées) */
    etat.questions.filter((q) => q.statut !== 'comblee').forEach((q) => envoyerZone({ id: 'q-' + q.id, titre: q.texte, pourquoi: 'introuvable', pourquoi_en_mots: 'Question restée ouverte : à creuser.' }, false));
  }

  function terminer(etat) {
    if (fini) return;
    zonesFinales(etat);
    clearInterval(ticker);
    for (const a of attente.values()) clearTimeout(a.timer);
    /* rôles encore en attente : on les montre, rien ne reste caché */
    if (!etat.erreur) {
      for (const [r, liste] of Object.entries(etat.pieces)) if (liste.length && ROLE[r]) montrer(ROLE[r], null);
      tousCompteurs(etat);
      const t = ligneCompteurs(etat);
      if (t) sur(() => mv().ligne(t + suffixe()));
    }
    /* cas fictif : la page rend son état final scripté, qui est le même. Vrai cas : on ne la laisse PAS retomber sur les chiffres fictifs
     * de la maquette (fin() les remet) : le réseau reste tel que le flux l'a construit, compteur des questions ouvertes remis au vrai chiffre. */
    setTimeout(() => {
      if (casFictif || etat.erreur) { fini = true; sur(() => mv().fin()); return; }
      if (etat.questions.length) montrer('inconnus', null);
      syncInconnus();
      fini = true;
    }, 1200);
    window.__fluxEtat = etat;
  }

  try {
    window.__fluxArret = branchement.suivre(idEnquete, { mode, dureeCible: 22, quand: (evt, etat) => sur(() => surEvenement(evt, etat)) });
  } catch (e) {
    console.warn('[flux] impossible de suivre l\'enquête', e && e.message);
    clearInterval(ticker);
    sur(() => mv().fin());
  }
}

main().catch((e) => { console.warn('[flux]', e && e.message); sur(() => mv() && mv().fin()); });
