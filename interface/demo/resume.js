/* Page de résumé de l'enquête : s'ouvre au clic sur la question centrale ([data-question]) ou sur #resume.
 * Autonome : injecte son CSS et son DOM. Aucune dépendance au reste de la maquette, sauf :
 *  - la question affichée est lue dans [data-question] ;
 *  - un clic sur une pièce ou une zone ferme le résumé, puis pose #piece-<id> / #zone-<id> (ou #dossier-<rôle> pour les pièces de démo)
 *    et émet l'événement 'resume:ouvrir' {type, id} (annulable : preventDefault suffit à reprendre la main).
 * Données : sans ?flux, le cas fictif ci-dessous ; avec ?flux&enquete=<id>, GET <serveur>/api/resume?enquete=<id>
 * (contrat : resume {en_bref, etabli, mecanisme, acteurs, chronologie, chiffres, ombre, prochaines_etapes, qui_appeler, certitude}).
 * Un champ absent n'est pas affiché ; rien n'est inventé en mode flux. */
(function () {
  'use strict';
  if (window.Resume) return;

  var QS = new URLSearchParams(location.search);
  var FLUX = QS.has('flux');
  var BASE = QS.get('base') || QS.get('serveur') || 'http://127.0.0.1:8802';
  var ENQ = QS.get('enquete') || '';

  /* ---------- cas fictif (sans ?flux) ---------- */
  var DEMO = {
    en_bref: [
      'Une même phrase, « Un clic, trois kilos », revient dans 47 des 214 vidéos lues.',
      'Elle remonte à 6 sources : cinq comptes TikTok et un canal Telegram, qui renvoient vers deux boutiques au même identifiant publicitaire.',
      'Qui est derrière reste ouvert : une société créée le 4 oct. n’est reliée aux boutiques que par un seul indice.'
    ],
    etabli: [
      { texte: 'La plus ancienne trace retrouvée : la vidéo TikTok de @lea.glow, le 11 oct., à 22 h 47.', piece_id: 'video-lea' },
      { texte: '47 vidéos répètent « Un clic, trois kilos » mot pour mot, d’abord @marie_slim le 14 oct.', piece_id: 'video-marie' },
      { texte: 'Deux boutiques, minceur-stylo.shop (29 €) et slimpen-direct.com (27 €), partagent l’identifiant publicitaire 7731 5029. Domaines créés le 6 oct.', piece_id: 'boutique-minceur' },
      { texte: 'L’agence sanitaire indique, le 3 nov., qu’aucune autorisation n’existe pour ce produit.', piece_id: 'avis-agence' },
      { texte: 'Orvane Trading Ltd est créée le 4 oct. Gérant déclaré au registre : P. Vasseur (nom fictif).', piece_id: 'registre-orvane' }
    ],
    mecanisme: 'Des comptes sans lien affiché publient la même phrase à quelques jours d’écart, et leurs vidéos renvoient vers deux boutiques. L’outil lit la parole, le texte à l’écran et les légendes, regroupe les vidéos en séries, puis remonte chaque série jusqu’à sa source.',
    acteurs: [
      { role: 'Plus ancienne trace retrouvée', nom_ou_type: '@lea.glow, compte TikTok' },
      { role: 'Relais', nom_ou_type: 'quatre autres comptes TikTok et le canal Telegram « Stylo Minceur Promo » (2 300 abonnés)' },
      { role: 'À qui ça profite', nom_ou_type: 'Orvane Trading Ltd, lien avec les boutiques : possible' },
      { role: 'Moyens techniques', nom_ou_type: 'minceur-stylo.shop et slimpen-direct.com' },
      { role: 'Voix contraires', nom_ou_type: 'agence sanitaire, presse locale' },
      { role: 'Experts', nom_ou_type: 'Dr H. Roussel, endocrinologue' },
      { role: 'Personnes qui se disent lésées', nom_ou_type: 'un témoignage de malaise, sur un forum' }
    ],
    chronologie: [
      { date: '4 oct.', fait: 'Orvane Trading Ltd est créée.', piece_id: 'registre-orvane' },
      { date: '6 oct.', fait: 'Les domaines des deux boutiques sont créés.', piece_id: 'boutique-minceur' },
      { date: '11 oct.', fait: 'Vidéo de @lea.glow, 22 h 47 : la plus ancienne trace retrouvée.', piece_id: 'video-lea' },
      { date: '2 nov.', fait: 'La presse locale en parle.', piece_id: 'presse-locale' },
      { date: '3 nov.', fait: 'L’agence sanitaire : aucune autorisation pour ce produit.', piece_id: 'avis-agence' }
    ],
    chiffres: { videos: 214, repetent: 47, sources_independantes: 6 },
    ombre: ['z-orvane', 'z-telegram', 'z-malades'],
    prochaines_etapes: 'Lire le registre des bénéficiaires effectifs d’Orvane Trading, remonter le canal Telegram jusqu’à son premier message, demander à l’agence sanitaire les signalements reçus. Orvane Trading est sollicitée en dernier, avant parution.',
    qui_appeler: [
      { qui: 'Dr H. Roussel, endocrinologue (expert)', pourquoi: 'Combien de patients a-t-il vus avec ce produit, et depuis quand ?' },
      { qui: 'L’ancienne modératrice du canal (témoin, identité à vérifier)', pourquoi: 'Date de création du canal et premières consignes reçues.' },
      { qui: 'Orvane Trading Ltd (sollicitation avant parution)', pourquoi: 'Sa version sur le lien avec les boutiques, en dernier.' }
    ],
    certitude: 'La coordination de la phrase est très probable. Le lien entre Orvane Trading et les boutiques reste possible.'
  };
  var ZONES = {
    'z-orvane': { titre: 'Qui détient vraiment Orvane Trading ?', pourquoi: 'caché' },
    'z-telegram': { titre: 'Un message plus ancien que le 11 oct. existe-t-il ?', pourquoi: 'hors de portée' },
    'z-malades': { titre: 'Combien de personnes disent avoir eu un malaise après usage ?', pourquoi: 'introuvable' }
  };
  var IMGS = {
    'video-lea': 'images/v-lea.jpg', 'video-marie': 'images/v-marie.jpg', 'telegram-canal': 'images/v-telegram.jpg',
    'boutique-minceur': 'images/p-boutique.jpg', 'boutique-slimpen': 'images/p-produit.jpg', 'avis-agence': 'images/p-agence.jpg',
    'presse-locale': 'images/p-presse.jpg', 'reddit-anon': 'images/p-victime.jpg', 'expert-roussel': 'images/p-expert.jpg',
    'miscause-demande': 'images/p-miscause.jpg', 'registre-orvane': 'images/p-registre.jpg'
  };
  var ROLE_HASH = { /* pièces de démo : dossier où les ouvrir quand la maquette ne gère pas #piece-<id> */
    'video-lea': 'origin', 'video-marie': 'amp', 'telegram-canal': 'amp', 'boutique-minceur': 'infra', 'boutique-slimpen': 'infra',
    'avis-agence': 'contra', 'presse-locale': 'contra', 'reddit-anon': 'victimes', 'expert-roussel': 'experts',
    'miscause-demande': 'mis', 'registre-orvane': 'benef'
  };

  /* ---------- CSS ---------- */
  var css = [
    '#rs{position:fixed;inset:0;z-index:8000;background:#080808;color:#fff;font-family:Archivo,system-ui,sans-serif;font-stretch:92%;font-weight:700;',
    '  font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;opacity:0;visibility:hidden;transform:translateY(14px);',
    '  transition:opacity 500ms cubic-bezier(.3,.7,.2,1),transform 500ms cubic-bezier(.3,.7,.2,1),visibility 0s linear 500ms;--u:1}',
    '#rs.on{opacity:1;visibility:visible;transform:none;transition-delay:0s}',
    '#rs *{box-sizing:border-box}',
    '#rs .rs-scroll{position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;scroll-behavior:smooth;scrollbar-width:thin;scrollbar-color:#555 #080808;',
    '  padding:calc(112px*var(--u)) max(28px,3.4vw) calc(120px*var(--u))}',
    '#rs .rs-top{position:absolute;left:0;right:0;top:0;height:calc(100px*var(--u));z-index:3;pointer-events:none;',
    '  background:linear-gradient(#080808 62%,rgba(8,8,8,0));padding:calc(26px*var(--u)) max(28px,3.4vw) 0;display:flex;justify-content:space-between;align-items:flex-start}',
    '#rs .rs-top>*{pointer-events:auto}',
    '#rs .rs-back{display:inline-flex;align-items:center;gap:10px;background:none;border:2px solid #fff;color:#fff;font:inherit;font-size:max(18px,calc(21px*var(--u)));font-weight:800;',
    '  padding:calc(6px*var(--u)) calc(16px*var(--u));cursor:pointer;transition:background .2s,color .2s}',
    '#rs .rs-back:hover{background:#fff;color:#080808}',
    '#rs .rs-back svg{width:1em;height:1em}',
    '#rs .rs-logo{width:max(110px,calc(110px*var(--u)));filter:invert(1);pointer-events:none;margin-top:calc(-2px*var(--u))}',
    '#rs :focus-visible{outline:3px solid #fff;outline-offset:3px}',
    '#rs .rs-card :focus-visible,#rs .rs-ptr:focus-visible{outline-color:#080808}',
    '#rs .rs-page{max-width:calc(1560px*var(--u));margin:0 auto}',
    /* premier écran : question, en bref à gauche, établi à droite */
    '#rs .rs-first{min-height:calc(100vh - 112px*var(--u) - 40px);display:grid;grid-template-columns:minmax(0,1.08fr) minmax(0,1fr);gap:max(32px,4.2vw);align-items:start}',
    '#rs .rs-kick{font-size:max(18px,calc(20px*var(--u)));color:#c7c7c7;margin-bottom:calc(10px*var(--u));font-weight:700}',
    '#rs h1{font-size:max(34px,calc(50px*var(--u)));line-height:1.02;letter-spacing:-.02em;font-weight:800;margin-bottom:calc(30px*var(--u));text-wrap:balance}',
    '#rs .rs-bref p{font-size:max(22px,calc(31px*var(--u)));line-height:1.16;letter-spacing:-.012em;font-weight:800;margin-bottom:calc(14px*var(--u));text-wrap:pretty;max-width:26em}',
    '#rs .rs-bref p:first-of-type{font-size:max(24px,calc(36px*var(--u)))}',
    '#rs .rs-card{background:#fff;color:#080808;padding:calc(20px*var(--u)) calc(24px*var(--u)) calc(8px*var(--u))}',
    '#rs .rs-card h2{font-size:max(18px,calc(21px*var(--u)));font-weight:800;margin-bottom:calc(6px*var(--u))}',
    '#rs .rs-row{display:grid;grid-template-columns:calc(92px*var(--u)) minmax(0,1fr);gap:calc(16px*var(--u));align-items:center;padding:calc(12px*var(--u)) 0;border-top:2px solid #080808}',
    '#rs .rs-row.nopic{grid-template-columns:minmax(0,1fr)}',
    '#rs .rs-row p{font-size:max(18px,calc(21px*var(--u)));line-height:1.17;font-weight:800;text-wrap:pretty}',
    '#rs .rs-th{display:block;width:calc(92px*var(--u));height:calc(62px*var(--u));background:#222 center/cover no-repeat;border:2px solid #080808;position:relative;padding:0;cursor:pointer}',
    '#rs .rs-th:hover{outline:3px solid #080808;outline-offset:2px}',
    '#rs .rs-ptr{display:inline-flex;align-items:center;gap:6px;background:#080808;color:#fff;border:0;font:inherit;font-size:max(18px,calc(19px*var(--u)));font-weight:800;padding:2px 10px;margin-top:6px;cursor:pointer;text-decoration:none}',
    '#rs .rs-ptr:hover{background:#333}',
    '#rs .rs-ptr svg{width:.9em;height:.9em}',
    /* suite : étiquette à gauche, contenu à droite, filet blanc */
    '#rs .rs-sec{display:grid;grid-template-columns:minmax(150px,calc(250px*var(--u))) minmax(0,1fr);gap:max(24px,3vw);border-top:2px solid #fff;padding:calc(30px*var(--u)) 0 calc(44px*var(--u))}',
    '#rs .rs-sec:first-of-type{margin-top:calc(70px*var(--u))}',
    '#rs .rs-sec>h2{font-size:max(20px,calc(24px*var(--u)));font-weight:800;line-height:1.1;padding-top:.15em}',
    '#rs .rs-sec>h2 small{display:block;font-size:max(18px,calc(19px*var(--u)));color:#c7c7c7;font-weight:600;margin-top:6px;line-height:1.2}',
    '#rs .rs-body{min-width:0;max-width:calc(1060px*var(--u))}',
    '#rs .rs-txt{font-size:max(22px,calc(29px*var(--u)));line-height:1.2;letter-spacing:-.01em;font-weight:700;text-wrap:pretty;max-width:34em}',
    '#rs dl.rs-who{display:grid;grid-template-columns:minmax(180px,calc(330px*var(--u))) minmax(0,1fr);column-gap:calc(24px*var(--u))}',
    '#rs .rs-who dt{font-size:max(18px,calc(21px*var(--u)));color:#c7c7c7;font-weight:700;padding:calc(10px*var(--u)) 0;border-bottom:1px solid #444;line-height:1.2}',
    '#rs .rs-who dd{font-size:max(20px,calc(25px*var(--u)));font-weight:800;padding:calc(8px*var(--u)) 0;border-bottom:1px solid #444;line-height:1.2}',
    '#rs ol.rs-ch{list-style:none}',
    '#rs .rs-ch li{display:grid;grid-template-columns:calc(120px*var(--u)) minmax(0,1fr) auto;gap:calc(20px*var(--u));align-items:center;padding:calc(10px*var(--u)) 0;border-bottom:1px solid #444}',
    '#rs .rs-ch .d{background:#fff;color:#080808;font-weight:800;font-size:max(18px,calc(21px*var(--u)));padding:2px 10px;text-align:center;white-space:nowrap}',
    '#rs .rs-ch .f{font-size:max(20px,calc(25px*var(--u)));font-weight:800;line-height:1.18}',
    '#rs .rs-ch .rs-ptr{margin:0;background:none;border:2px solid #fff;color:#fff}',
    '#rs .rs-ch .rs-ptr:hover{background:#fff;color:#080808}',
    '#rs .rs-ticks{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:calc(22px*var(--u));max-width:calc(900px*var(--u))}',
    '#rs .rs-ticks i{width:calc(11px*var(--u));height:calc(18px*var(--u));border:1.5px solid #666}',
    '#rs .rs-ticks i.r{background:#fff;border-color:#fff}',
    '#rs .rs-num mark{background:#ffd817;color:#080808;padding:0 .12em;white-space:nowrap}',
    '#rs .rs-num b{font-weight:800;white-space:nowrap}',
    '#rs .rs-sub{font-size:max(18px,calc(21px*var(--u)));color:#c7c7c7;font-weight:600;margin-top:12px;max-width:34em;line-height:1.25}',
    '#rs a.rs-z{display:grid;grid-template-columns:calc(48px*var(--u)) minmax(0,1fr) auto;gap:calc(18px*var(--u));align-items:center;text-decoration:none;color:#fff;',
    '  border:2px dashed #fff;padding:calc(16px*var(--u)) calc(22px*var(--u));margin-bottom:calc(14px*var(--u));transition:background .2s,color .2s}',
    '#rs a.rs-z:hover{background:#fff;color:#080808}',
    '#rs a.rs-z .n{font-size:max(22px,calc(30px*var(--u)));font-weight:800}',
    '#rs a.rs-z .t{font-size:max(21px,calc(27px*var(--u)));font-weight:800;line-height:1.14}',
    '#rs a.rs-z .w{display:block;font-size:max(18px,calc(19px*var(--u)));font-weight:600;color:#c7c7c7;margin-top:4px}',
    '#rs a.rs-z:hover .w{color:#444}',
    '#rs a.rs-z .go{font-size:max(18px,calc(20px*var(--u)));font-weight:800;white-space:nowrap}',
    '#rs ol.rs-call{list-style:none;margin-top:calc(26px*var(--u));counter-reset:c}',
    '#rs .rs-call li{counter-increment:c;display:grid;grid-template-columns:calc(46px*var(--u)) minmax(0,1fr);padding:calc(12px*var(--u)) 0;border-top:1px solid #444}',
    '#rs .rs-call li::before{content:counter(c) ".";font-size:max(22px,calc(28px*var(--u)));font-weight:800}',
    '#rs .rs-call b{display:block;font-size:max(20px,calc(25px*var(--u)));font-weight:800;line-height:1.18}',
    '#rs .rs-call span{display:block;font-size:max(18px,calc(21px*var(--u)));color:#c7c7c7;font-weight:600;margin-top:3px;line-height:1.25}',
    '#rs .rs-lvl{background:#fff;color:#080808;padding:0 .3em;white-space:nowrap}',
    '#rs .rs-bot{position:absolute;left:0;right:0;bottom:0;height:calc(130px*var(--u));z-index:3;pointer-events:none;background:linear-gradient(rgba(8,8,8,0),#080808 62%)}',
    '#rs .rs-more{position:absolute;left:50%;bottom:calc(26px*var(--u));transform:translateX(-50%);z-index:4;background:#080808;border:2px solid #fff;color:#fff;font:inherit;',
    '  font-size:max(18px,calc(20px*var(--u)));font-weight:800;padding:calc(6px*var(--u)) calc(18px*var(--u));cursor:pointer;display:flex;gap:10px;align-items:center;transition:opacity .3s}',
    '#rs .rs-more:hover{background:#fff;color:#080808}',
    '#rs .rs-more.off{opacity:0;pointer-events:none}',
    '#rs .rs-fict{position:absolute;left:max(18px,calc(28px*var(--u)));bottom:max(14px,calc(22px*var(--u)));z-index:5;font-size:max(18px,calc(20px*var(--u)));line-height:1.15;color:#c7c7c7;pointer-events:none}',
    '#rs .rs-void{font-size:max(22px,calc(28px*var(--u)));font-weight:800;max-width:24em;line-height:1.2;padding-top:calc(60px*var(--u))}',
    '#rs .rs-void small{display:block;font-size:max(18px,calc(21px*var(--u)));color:#c7c7c7;font-weight:600;margin-top:10px}',
    '@media (max-width:1100px){#rs .rs-first{grid-template-columns:1fr}#rs .rs-sec{grid-template-columns:1fr;gap:12px}#rs dl.rs-who{grid-template-columns:1fr}}',
    '@media (prefers-reduced-motion:reduce){#rs{transition-duration:1ms}#rs .rs-scroll{scroll-behavior:auto}}'
  ].join('\n');

  /* ---------- utilitaires ---------- */
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function present(v) { return v != null && v !== '' && !(Array.isArray(v) && !v.length); }
  var ARROW = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 15L15 5M7 5h8v8" fill="none" stroke="currentColor" stroke-width="2.4"/></svg>';
  var BACK = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M16 10H4m5-5l-5 5 5 5" fill="none" stroke="currentColor" stroke-width="2.4"/></svg>';
  function nf(n) { n = +n; return isFinite(n) ? String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ') : esc(n); }
  function arr(v) { return Array.isArray(v) ? v : (present(v) ? [v] : []); }
  function pieceBtn(id, label, cls) {
    return '<button type="button" class="' + (cls || 'rs-ptr') + '" data-piece="' + esc(id) + '">' + label + '</button>';
  }
  function thumb(id) {
    var src = !FLUX && IMGS[id];
    if (!src) return '';
    return '<button type="button" class="rs-th" data-piece="' + esc(id) + '" style="background-image:url(' + src + ')" aria-label="Ouvrir la pièce"></button>';
  }
  function zoneInfo(z) {
    if (z && typeof z === 'object') return { id: z.id, titre: z.titre || z.title, pourquoi: z.pourquoi };
    var k = ZONES[z];
    return { id: z, titre: k && !FLUX ? k.titre : (k ? k.titre : null), pourquoi: k ? k.pourquoi : null };
  }

  /* ---------- rendu ---------- */
  function fil(x, n) { return x.slice(0, n); }
  function render(R, question) {
    var h = [];
    var bref = arr(R.en_bref);
    var etabli = arr(R.etabli).filter(function (e) { return e && e.texte; });
    h.push('<div class="rs-page"><div class="rs-first"><div class="rs-bref"><div class="rs-kick">Résumé de l’enquête</div>',
      '<h1>' + esc(question) + '</h1>');
    if (bref.length) {
      h.push('<div class="rs-kick">En bref</div>');
      bref.forEach(function (p) { h.push('<p>' + esc(p) + '</p>'); });
    }
    h.push('</div>');
    if (etabli.length) {
      h.push('<div class="rs-card"><h2>Ce qui est établi, pièce à l’appui</h2>');
      etabli.forEach(function (e) {
        var th = e.piece_id ? thumb(e.piece_id) : '';
        h.push('<div class="rs-row' + (th ? '' : ' nopic') + '">' + th + '<div><p>' + esc(e.texte) + '</p>' +
          (e.piece_id && !th ? pieceBtn(e.piece_id, 'Voir la pièce ' + ARROW) : '') + '</div></div>');
      });
      h.push('</div>');
    }
    h.push('</div>');

    function sec(t, sub, body, id) {
      h.push('<section class="rs-sec"' + (id ? ' id="' + id + '"' : '') + '><h2>' + t + (sub ? '<small>' + sub + '</small>' : '') + '</h2><div class="rs-body">' + body + '</div></section>');
    }
    if (present(R.mecanisme)) sec('Comment ça marche', '', '<p class="rs-txt">' + esc(R.mecanisme) + '</p>');
    var ac = arr(R.acteurs).filter(function (a) { return a && (a.role || a.nom_ou_type); });
    if (ac.length) sec('Qui', 'par rôle dans l’affaire', '<dl class="rs-who">' + ac.map(function (a) {
      return '<dt>' + esc(a.role) + '</dt><dd>' + esc(a.nom_ou_type) + '</dd>';
    }).join('') + '</dl>');
    var ch = fil(arr(R.chronologie).filter(function (c) { return c && c.date; }), 5);
    if (ch.length) sec('La chronologie', '', '<ol class="rs-ch">' + ch.map(function (c) {
      return '<li><span class="d">' + esc(c.date) + '</span><span class="f">' + esc(c.fait) + '</span>' +
        (c.piece_id ? pieceBtn(c.piece_id, 'La pièce ' + ARROW) : '<span></span>') + '</li>';
    }).join('') + '</ol>');
    var c = R.chiffres || {};
    if (present(c.videos) || present(c.repetent) || present(c.sources_independantes)) {
      var ticks = '';
      var tv = +c.videos, tr = +c.repetent;
      if (isFinite(tv) && tv > 0 && tv <= 600 && isFinite(tr) && tr >= 0 && tr <= tv) {
        for (var i = 0; i < tv; i++) ticks += '<i class="' + (i < tr ? 'r' : '') + '"></i>';
        ticks = '<div class="rs-ticks" aria-hidden="true">' + ticks + '</div>';
      }
      var parts = [];
      if (present(c.videos)) parts.push('<b>' + nf(c.videos) + '</b> vidéos lues.');
      if (present(c.repetent)) parts.push('<mark>' + nf(c.repetent) + ' répètent</mark> la même phrase.');
      if (present(c.sources_independantes)) parts.push('Elles remontent à <b>' + nf(c.sources_independantes) + '</b> sources.');
      sec('Les chiffres mesurés', 'comptés par l’outil, pas estimés', ticks + '<p class="rs-txt rs-num">' + parts.join(' ') + '</p>' +
        (!FLUX ? '<p class="rs-sub">Les six sources : cinq comptes TikTok et le canal Telegram « Stylo Minceur Promo », 2 300 abonnés.</p>' : ''));
    }
    var om = arr(R.ombre).map(zoneInfo).filter(function (z) { return z && z.id; });
    if (om.length) sec('Ce qui reste dans l’ombre', 'là où l’outil s’arrête', om.map(function (z, i) {
      return '<a class="rs-z" href="#zone-' + esc(z.id) + '" data-zone="' + esc(z.id) + '"><span class="n">' + (i + 1) + '.</span><span class="t">' +
        esc(z.titre || ('Zone d’ombre ' + (i + 1))) + (z.pourquoi ? '<span class="w">' + esc(z.pourquoi) + '</span>' : '') + '</span><span class="go">Voir la zone ' + ARROW.replace('<svg', '<svg style="width:.9em;height:.9em;vertical-align:-.1em"') + '</span></a>';
    }).join(''), 'rs-ombre');
    var qa = arr(R.qui_appeler).filter(function (q) { return q && q.qui; });
    if (present(R.prochaines_etapes) || qa.length) {
      sec('Les prochaines étapes', qa.length ? 'et qui appeler, dans cet ordre' : '',
        (present(R.prochaines_etapes) ? '<p class="rs-txt">' + esc(R.prochaines_etapes) + '</p>' : '') +
        (qa.length ? '<ol class="rs-call">' + qa.map(function (q) { return '<li><div><b>' + esc(q.qui) + '</b>' + (q.pourquoi ? '<span>' + esc(q.pourquoi) + '</span>' : '') + '</div></li>'; }).join('') + '</ol>' : ''));
    }
    if (present(R.certitude)) {
      var cert = esc(R.certitude).replace(/(très probable|non établi|établi|probable|possible)/gi, '<span class="rs-lvl">$1</span>');
      sec('Le niveau de certitude', 'en mots', '<p class="rs-txt">' + cert + '</p>');
    }
    h.push('</div>');
    return h.join('');
  }

  /* ---------- DOM ---------- */
  var root, scroll, more, backBtn, built = false, openState = false, prevHash = '', prevFocus = null, cache = null, loading = null;
  function build() {
    if (built) return; built = true;
    var st = document.createElement('style'); st.id = 'rs-css'; st.textContent = css; document.head.appendChild(st);
    root = document.createElement('div'); root.id = 'rs'; root.setAttribute('role', 'dialog'); root.setAttribute('aria-modal', 'true'); root.setAttribute('aria-label', 'Résumé de l’enquête');
    root.innerHTML = '<div class="rs-top"><button type="button" class="rs-back">' + BACK + 'Revenir au dossier</button><img class="rs-logo" src="brut-logo.svg" alt="Brut."></div>' +
      '<div class="rs-scroll" tabindex="-1"></div><div class="rs-bot"></div>' +
      (FLUX ? '' : '<div class="rs-fict">Cas fictif,<br>images d’illustration</div>') +
      '<button type="button" class="rs-more">Lire la suite <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><path d="M4 7l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2.4"/></svg></button>';
    document.body.appendChild(root);
    scroll = root.querySelector('.rs-scroll'); more = root.querySelector('.rs-more'); backBtn = root.querySelector('.rs-back');
    backBtn.addEventListener('click', function () { close(); });
    more.addEventListener('click', function () { scroll.scrollBy({ top: scroll.clientHeight * .8, behavior: 'smooth' }); });
    scroll.addEventListener('scroll', function () { more.classList.toggle('off', scroll.scrollTop > 40 || scroll.scrollHeight - scroll.clientHeight < 60); }, { passive: true });
    root.addEventListener('click', function (e) {
      var p = e.target.closest('[data-piece]'), z = e.target.closest('[data-zone]');
      if (p) { e.preventDefault(); go('piece', p.getAttribute('data-piece')); }
      else if (z) { e.preventDefault(); go('zone', z.getAttribute('data-zone')); }
    });
    sizeU(); addEventListener('resize', sizeU);
  }
  function sizeU() {
    if (!root) return;
    var u = Math.min(innerWidth / 1657, innerHeight / 806);
    root.style.setProperty('--u', Math.max(.78, Math.min(1.3, u)).toFixed(3));
  }
  function question() { var q = document.querySelector('[data-question]'); return q ? q.textContent.replace(/\s+/g, ' ').replace(/\s*:\s*/g, ' : ').trim() : 'Résumé de l’enquête'; }

  function fetchFlux() {
    if (loading) return loading;
    var c = new AbortController(), t = setTimeout(function () { c.abort(); }, 6000);
    loading = fetch(BASE + '/api/resume?enquete=' + encodeURIComponent(ENQ), { signal: c.signal })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { clearTimeout(t); if (d && d.erreur) return null; return d && (d.resume || d); })
      .catch(function () { clearTimeout(t); return null; })
      .then(function (d) { loading = null; return d; });
    return loading;
  }
  function hasContent(R) {
    if (!R || typeof R !== 'object') return false;
    return ['en_bref', 'etabli', 'mecanisme', 'acteurs', 'chronologie', 'chiffres', 'ombre', 'prochaines_etapes', 'qui_appeler', 'certitude'].some(function (k) {
      var v = R[k]; return v && (typeof v !== 'object' || Object.keys(v).length);
    });
  }
  function fill(R) {
    var q = question();
    scroll.innerHTML = hasContent(R) ? render(R, q) :
      '<div class="rs-page"><div class="rs-kick">Résumé de l’enquête</div><h1>' + esc(q) + '</h1><p class="rs-void">Le résumé n’est pas encore prêt pour cette enquête.<small>L’outil le rédige à partir des pièces versées au dossier ; revenez au dossier, il se complète au fil de la recherche.</small></p></div>';
    scroll.scrollTop = 0;
    more.classList.toggle('off', false);
    requestAnimationFrame(function () { scroll.dispatchEvent(new Event('scroll')); });
  }

  function open() {
    build();
    if (openState) return;
    openState = true;
    prevFocus = document.activeElement;
    prevHash = location.hash === '#resume' ? '' : location.hash;
    try { history.replaceState(null, '', '#resume'); } catch (e) { }
    if (FLUX) {
      if (cache) fill(cache);
      else { scroll.innerHTML = '<div class="rs-page"><div class="rs-kick">Résumé de l’enquête</div><h1>' + esc(question()) + '</h1><p class="rs-void">Le résumé se prépare…</p></div>'; }
      fetchFlux().then(function (d) { if (d) cache = d; if (openState) fill(cache || d); });
    } else fill(DEMO);
    root.classList.add('on');
    setTimeout(function () { if (openState) scroll.focus({ preventScroll: true }); }, 60);
    document.dispatchEvent(new CustomEvent('resume:etat', { detail: { ouvert: true } }));
  }
  function close(keepHash) {
    if (!openState) return;
    openState = false;
    root.classList.remove('on');
    if (!keepHash) { try { history.replaceState(null, '', prevHash || (location.pathname + location.search)); } catch (e) { } }
    if (prevFocus && prevFocus.focus && document.contains(prevFocus)) { try { prevFocus.focus({ preventScroll: true }); } catch (e) { } }
    document.dispatchEvent(new CustomEvent('resume:etat', { detail: { ouvert: false } }));
  }
  function go(type, id) {
    var ev = new CustomEvent('resume:ouvrir', { detail: { type: type, id: id }, cancelable: true });
    close();
    if (!document.dispatchEvent(ev)) return;
    var h = type === 'zone' ? '#zone-' + id : (!FLUX && ROLE_HASH[id] ? '#dossier-' + ROLE_HASH[id] : '#piece-' + id);
    if (location.hash === h) dispatchEvent(new HashChangeEvent('hashchange'));
    else location.hash = h;
  }

  /* ---------- écoute : phase de capture pour passer avant le gestionnaire du dossier ---------- */
  document.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('[data-question]');
    if (!t || (root && root.contains(e.target))) return;
    e.stopImmediatePropagation(); e.preventDefault();
    open();
  }, true);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && openState) { e.stopImmediatePropagation(); e.preventDefault(); close(); }
  }, true);
  addEventListener('hashchange', function () {
    if (location.hash === '#resume') open();
  });
  function hashDepart() { /* la maquette réécrit le hash au démarrage : on relit l'adresse d'origine */
    try { var n = performance.getEntriesByType('navigation')[0]; return n ? new URL(n.name).hash : location.hash; } catch (e) { return location.hash; }
  }
  function init() { if (location.hash === '#resume' || hashDepart() === '#resume') open(); else if (FLUX) { /* préchauffe */ build(); } }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();

  window.Resume = { ouvrir: open, fermer: close, donnees: function () { return FLUX ? cache : DEMO; } };
})();
