/* mouvement.js : le réseau se construit sous nos yeux. Autonome (injecte son CSS), aucune écriture dans le HTML.
 * API publique window.Mouvement (chaque appel joue l'animation une fois). ?flux coupe la séquence scriptée : le réseau attend les appels.
 *  depart()                      question seule, rôles masqués (silhouettes pointillées avec ?flux), impulsion
 *  arriveRole(id,{image,compteur}) le trait pousse, le rôle se développe, passe en jaune 600 ms (id : origine, amplificateurs, ...)
 *  compteur(id,n)                le compteur du rôle monte (ou descend) jusqu'à n
 *  lien(id,'prouve'|'indice'|'casse')  trait plein, trait pointillé, ou trait qui se fend et disparaît
 *  zone(id,'silhouette'|'revele')       une zone d'ombre passe en contour seul, ou se révèle (les zones arrivent en dernier, une par une)
 *  question(n,...)               retirée : la liste des questions n'existe plus (remplacée par les zones d'ombre), l'appel ne fait rien
 *  ligne(texte)                  la ligne vivante, en bas
 *  fin()                         rend la main (sous-titre de conclusion). Tout clic ou toute touche appelle fin() tout seul.
 * Séquence scriptée : passe par ces mêmes fonctions. ?rejouer rejoue la construction depuis #niveau1. Événement document « etat » écouté. */
(function () {
  'use strict';
  if (window.Mouvement) return;
  var D = document, H = D.documentElement;
  var reduce = false, FLUX = false, REJOUER = false;
  try { reduce = matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}
  var QS = new URLSearchParams(location.search);
  FLUX = QS.has('flux'); REJOUER = QS.has('rejouer');
  var now = function () { return performance.now(); };
  var clamp = function (x, a, b) { return Math.max(a, Math.min(b, x)); };
  var outQ = function (t) { return 1 - Math.pow(1 - clamp(t, 0, 1), 4); };
  var outE = function (t) { t = clamp(t, 0, 1); return t >= 1 ? 1 : 1 - Math.pow(2, -10 * t); };
  var $ = function (s, r) { return (r || D).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || D).querySelectorAll(s)); };
  var EASE = 'cubic-bezier(0.16,1,0.3,1)';

  var ORDER = ['origine', 'amplificateurs', 'infrastructure', 'beneficiaires', 'contradicteurs', 'victimes', 'temoins', 'experts', 'miscause', 'inconnus'];
  var FALLBACK_WIRES = ['origine', 'amplificateurs', 'beneficiaires', 'infrastructure', 'contradicteurs', 'inconnus', 'victimes', 'temoins', 'experts', 'miscause', 'question-posee'];
  var ID2R = { origin: 'origine', benef: 'beneficiaires', infra: 'infrastructure', contra: 'contradicteurs', inconnus: 'inconnus', victimes: 'victimes', temoins: 'temoins', experts: 'experts', mis: 'miscause', ampc: 'amplificateurs', qx: 'question-posee' };
  var NAMES = { origine: 'plus ancienne trace retrouvee', amplificateurs: 'relais', beneficiaires: 'a qui ca profite', infrastructure: 'moyens techniques', contradicteurs: 'voix contraires', victimes: 'personnes qui se disent lesees', temoins: 'temoins directs', experts: 'experts', miscause: 'mis en cause' };
  var norm = function (s) { return String(s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[’']/g, "'").trim(); };

  /* ---------------- CSS ---------------- */
  var CSS = [
    ':root{--mv-e:cubic-bezier(.16,1,.3,1)}',
    '.mv-w{display:inline-block;white-space:pre;transition:opacity .24s var(--mv-e),translate .6s var(--mv-e)}',
    '.mv-pre .mv-w{opacity:0;translate:0 .24em}',
    '.it{transition:opacity .15s linear,scale .1s var(--mv-e)}',
    '.it .im{transition:filter .7s var(--mv-e),transform .7s var(--mv-e)}',
    '.it.mv-g{opacity:0;pointer-events:none!important}',
    '.it.mv-g .im{filter:brightness(0);transform:scale(1.04)}',
    '.it.mv-g.mv-sil{opacity:.4;border-style:dashed}',
    '.it:active,#q:active,.b1:active,.b2:active,#rail button:active,#bc button:active{scale:.98}',
    '#q{transition:scale .1s var(--mv-e)}',
    '.it[data-id="qx"].contour{border-style:dashed}',
    '.lab{transition:opacity .5s var(--mv-e),translate .5s var(--mv-e),background-color .45s ease,color .45s ease}',
    '.lab.mv-g{opacity:0;translate:0 8px}',
    '.lab.mv-f{visibility:visible!important}',
    '@keyframes mv-pop{from{opacity:0;translate:0 8px}to{opacity:1;translate:0 0}}',
    '.lab.mv-pop{animation:mv-pop .5s var(--mv-e) both}',
    '.lab.chip.mv-y{background:#ffd817}',
    '.lab.plain.mv-y{color:#ffd817}',
    '.zgh{transition:background-color .45s ease,color .45s ease}',
    '.zgh.mv-y{background:#ffd817}',
    '.zn{transition:border-color .5s ease,background-color .2s ease}',
    '.zn>*{transition:opacity .5s var(--mv-e)}',
    '.zn.mv-zs{border-color:rgba(255,255,255,.4)}',
    '.zn.mv-zs>*{opacity:0}',
    'html.mv-hs #sub{opacity:0}',
    '#sub{transition:opacity .4s ease}',
    '#mv-live{position:fixed;left:50%;bottom:calc(30px*var(--u,1));transform:translateX(-50%);z-index:60;white-space:nowrap;font-weight:800;',
    ' font-size:max(31px,calc(34px*var(--u,1)));line-height:1.12;letter-spacing:-.015em;text-shadow:0 2px 10px rgba(0,0,0,.65);pointer-events:none;opacity:0;transition:opacity .4s ease;font-variant-numeric:tabular-nums}',
    '#mv-live.on{opacity:1}',
    '.mv-dot{display:inline-block;width:.32em;height:.32em;background:#fff;margin-right:.5em;vertical-align:.12em;animation:mv-blink 1s steps(1) infinite}',
    '@keyframes mv-blink{50%{opacity:.15}}',
    '.mv-ring{position:fixed;left:0;top:0;border:2px solid #fff;border-radius:50%;pointer-events:none;z-index:25;opacity:0}',
    '.mv-tip,.mv-pt{position:fixed;left:0;top:0;width:calc(8px*var(--u,1));height:calc(8px*var(--u,1));margin:calc(-4px*var(--u,1)) 0 0 calc(-4px*var(--u,1));background:#fff;z-index:26;pointer-events:none}',
    '#mv-fx{position:fixed;inset:0;width:100%;height:100%;z-index:4;pointer-events:none;overflow:visible}',
    '#mv-fx path{fill:none;stroke:#fff;stroke-width:2;stroke-dasharray:9 8}',
    '.mv-pc{position:fixed;left:0;top:0;width:calc(72px*var(--u,1));height:calc(50px*var(--u,1));border:3px solid #fff;background:#080808 center/cover;z-index:6;pointer-events:none}',
    '#wires .vis{transition:stroke-width .15s var(--mv-e),stroke-opacity .15s ease}',
    '#wires g.mv-hov .vis{stroke-width:3px}',
    '#wires.mv-hovering g:not(.mv-hov) .vis{stroke-opacity:.55}',
    '#wires g.mv-broken{display:none}',
    '@keyframes mv-sl{from{opacity:0;translate:24px 0}to{opacity:1;translate:0 0}}',
    '@keyframes mv-up{from{opacity:0;translate:0 18px}to{opacity:1;translate:0 0}}',
    '@keyframes mv-fade{from{opacity:0}to{opacity:1}}',
    '#panel:not(.off){animation:mv-sl .6s var(--mv-e) both}',
    '#proof:not(.off),#zone:not(.off){animation:mv-up .6s var(--mv-e) both}',
    '#banner:not(.off){animation:mv-up .6s var(--mv-e) .25s both}',
    '#bc:not(.off),#rail:not(.off){animation:mv-fade .4s ease both}',
    '#pbar.on{animation:mv-fade .5s ease .1s both}',
    'html.mv-na #panel,html.mv-na #proof,html.mv-na #zone,html.mv-na #banner,html.mv-na #bc,html.mv-na #rail,html.mv-na #pbar{animation:none!important}',
    '@media (prefers-reduced-motion:reduce){',
    ' .mv-w,.lab{transition:opacity .2s linear!important;translate:none!important}',
    ' .mv-pre .mv-w{translate:none}',
    ' .lab.mv-g{translate:none}',
    ' .it,.it .im{transition:opacity .2s linear!important}',
    ' .it.mv-g .im{transform:none}',
    ' #panel:not(.off),#proof:not(.off),#zone:not(.off),#banner:not(.off){animation:mv-fade .25s linear both}',
    ' .lab.mv-pop{animation:mv-fade .25s linear both}',
    '}'
  ].join('\n');
  var st0 = D.createElement('style'); st0.id = 'mv-css'; st0.textContent = CSS; D.head.appendChild(st0);

  /* ---------------- outils ---------------- */
  var TW = [];
  function tw(dur, fn, delay, grp) { var o = { t0: now() + (delay || 0), dur: dur, fn: fn, grp: grp || null, dead: false }; TW.push(o); return o; }
  function cancelGrp(g) { TW.forEach(function (o) { if (o.grp === g) o.dead = true; }); }
  function cnt(el, to, dur, delay, grp) {
    if (!el) return;
    var from = parseInt(el.textContent, 10) || 0;
    el.setAttribute('data-valeur', to);
    if (reduce || from === to) { el.textContent = String(to); return; }
    TW.forEach(function (o) { if (o.el === el) o.dead = true; });
    var o2 = tw(dur || 700, function (p) { el.textContent = String(Math.round(from + (to - from) * outE(p))); }, delay || 0, grp);
    o2.el = el;
  }
  function parseD(s) {
    var m = /M\s*(-?[\d.]+)[ ,]+(-?[\d.]+)\s*L\s*(-?[\d.]+)[ ,]+(-?[\d.]+)/.exec(s || '');
    return m ? { ax: +m[1], ay: +m[2], ex: +m[3], ey: +m[4] } : null;
  }
  function roleOfItem(e) {
    if (e.dataset.role) return e.dataset.role;
    var id = e.dataset.id || '';
    if (/^t\d+$/.test(id)) return 'amplificateurs';
    return ID2R[id] || null;
  }
  function roleOfLab(l) {
    if (l.dataset.role) return l.dataset.role;
    var c = l.classList;
    if (c.contains('zg')) return 'inconnus';
    if (!c.contains('chip') && !c.contains('plain')) return null;
    var t = norm(l.firstChild && l.firstChild.textContent);
    for (var k in NAMES) if (t === NAMES[k]) return k;
    return null;
  }
  function wireMap() {
    var m = {};
    $$('#wires > g').forEach(function (g, i) {
      var id = g.getAttribute('data-lien') || FALLBACK_WIRES[i];
      if (id) m[id] = { g: g, v: $('.vis', g), h: $('.hit', g) };
    });
    return m;
  }
  function wrapWords(el, per) {
    var txt = el.textContent, out = [];
    if (el.id === 'q') {
      Array.prototype.slice.call(el.childNodes).forEach(function (n) {
        if (n.nodeType !== 3) return;
        var frag = D.createDocumentFragment();
        n.textContent.split(/(\s+)/).forEach(function (p) {
          if (!p) return;
          if (/^\s+$/.test(p)) { frag.appendChild(D.createTextNode(p)); return; }
          var s = D.createElement('span'); s.className = 'mv-w'; s.textContent = p; frag.appendChild(s); out.push(s);
        });
        el.replaceChild(frag, n);
      });
    } else {
      el.textContent = ''; el.dataset.mvT = txt;
      txt.split(/(\s+)/).forEach(function (p) {
        if (!p) return;
        if (/^\s+$/.test(p)) { el.appendChild(D.createTextNode(p)); return; }
        var s = D.createElement('span'); s.className = 'mv-w'; s.textContent = p; el.appendChild(s); out.push(s);
      });
    }
    out.forEach(function (s, i) { s.style.transitionDelay = (i * per) + 'ms,' + (i * per) + 'ms'; });
    return out;
  }
  function typeEl(el, per, delay) {
    if (!el) return;
    if (reduce) { try { el.animate([{ opacity: 0 }, { opacity: 1 }], 200); } catch (e) {} return; }
    wrapWords(el, per);
    el.classList.add('mv-pre'); void el.offsetWidth;
    setTimeout(function () { el.classList.remove('mv-pre'); }, delay || 20);
  }
  function typeSub() {
    var s = $('#sub'); if (!s || !s.textContent.trim()) return;
    var n = s.textContent.trim().split(/\s+/).length;
    typeEl(s, Math.min(45, 700 / n), 20);
  }

  /* ---------------- état global ---------------- */
  var st = { nom: 'niveau0', n2: 0 };
  var B = { on: false, wd: 0, timers: [], gated: [], forced: [], cnt: [], yel: null, yelT: 0, ringAt: 0, flux: false };
  var JOBS = {};
  var SC = null; /* séquence scriptée */
  var openN = 0, linesShown = {};

  function later(fn, ms) { var t = setTimeout(function () { try { fn(); } catch (e) {} }, ms); B.timers.push(t); return t; }

  /* ---------------- ligne vivante ---------------- */
  var live = D.createElement('div'); live.id = 'mv-live'; live.setAttribute('aria-live', 'off');
  live.innerHTML = '<i class="mv-dot"></i><span id="mv-lt"></span>';
  D.body.appendChild(live);
  var liveT = $('#mv-lt');

  /* ---------------- jobs (un rôle = un trait + des cadres + des étiquettes) ---------------- */
  function makeJob(id) {
    var W = wireMap(), w = W[id];
    var j = {
      id: id, w: w || null, go: false, armed: false, started: false, done: false, revealed: false,
      still: 0, prev: null, a: null, b: null, startAt: 0, tip: null, opts: {}, items: [], labs: [], hold: true
    };
    $$('.it').forEach(function (e) { if (roleOfItem(e) === id) j.items.push(e); });
    if (id === 'question-posee') { j.items = $$('.it[data-id="qx"]'); j.labs = $$('.lab.plain').filter(function (l) { return !roleOfLab(l) && !$('.n', l); }); }
    else {
      $$('.lab').forEach(function (l) { if (roleOfLab(l) === id) j.labs.push(l); });
      /* rôles en vignette : l'étiquette du niveau 1 est la « plain », le « chip » ne sert qu'à l'ouverture du dossier */
      var pl = j.labs.filter(function (l) { return l.classList.contains('plain'); });
      if (pl.length) j.labs = pl;
    }
    JOBS[id] = j; return j;
  }
  var DEV = { origine: 600, amplificateurs: 600, infrastructure: 600, beneficiaires: 600, contradicteurs: 600, victimes: 260, temoins: 260, experts: 260, miscause: 260, inconnus: 600 };
  function gateJob(j, sil, dev) {
    if (dev) j.dev = dev;
    j.items.forEach(function (e) {
      e.classList.add('mv-g'); if (sil) e.classList.add('mv-sil'); B.gated.push(e);
      var im = dev && e.querySelector('.im'); if (im) im.style.transitionDuration = dev + 'ms';
    });
    j.labs.forEach(function (l) {
      l.classList.add('mv-g'); B.gated.push(l);
      $$('.zn', l).forEach(function (z) { z.classList.add('mv-zs'); });
      $$('[data-compteur]', l).forEach(function (c) { c.dataset.mvFinal = c.getAttribute('data-valeur') || c.textContent; c.textContent = '0'; B.cnt.push(c); });
    });
  }
  function markY(el) {
    if (B.yel) B.yel.classList.remove('mv-y');
    clearTimeout(B.yelT);
    if (!el) return;
    B.yel = el; el.classList.add('mv-y');
    B.yelT = setTimeout(function () { el.classList.remove('mv-y'); if (B.yel === el) B.yel = null; }, 600);
  }
  function startJob(j) {
    j.started = true; j.startAt = now();
    if (!reduce) { var t = D.createElement('i'); t.className = 'mv-tip'; D.body.appendChild(t); j.tip = t; }
  }
  function revealJob(j) {
    if (j.revealed) return; j.revealed = true;
    var o = j.opts || {};
    j.items.forEach(function (e) {
      if (o.image && e.querySelector('.im')) e.querySelector('.im').style.backgroundImage = 'url(' + o.image + ')';
      e.classList.remove('mv-g', 'mv-sil');
    });
    j.labs.forEach(function (l) {
      l.classList.add('mv-f'); B.forced.push(l); l.classList.remove('mv-g');
      var c = $('[data-compteur]', l);
      if (c && j.id !== 'inconnus') {
        var to = o.compteur != null ? o.compteur : parseInt(c.dataset.mvFinal || c.getAttribute('data-valeur'), 10) || 0;
        cnt(c, to, j.dev || 700, 80, 'rev');
      }
      if (j.id === 'inconnus') {
        /* les zones d'ombre arrivent en dernier, une par une : le contour d'abord, puis le texte ; le compteur monte avec elles */
        var zs = $$('.zn:not(.zx)', l), zc = $('[data-compteur]', l);
        if (zc) zc.textContent = '0';
        zs.forEach(function (z, i) {
          later(function () { z.classList.remove('mv-zs'); if (zc) cnt(zc, i + 1, 260, 0, 'rev'); }, 380 + i * 420);
        });
      }
    });
    var tgt = j.labs.filter(function (l) { return l.classList.contains('chip') || l.classList.contains('plain') || l.classList.contains('zg'); })[0];
    if (tgt) { var yb = tgt.classList.contains('zg') ? $('.zgh', tgt) : tgt; markY(yb); }
    if (j.onReveal) j.onReveal();
  }
  function releaseJob(j) {
    j.done = true; j.hold = false;
    if (j.tip) { j.tip.remove(); j.tip = null; }
  }

  /* ---------------- impulsion ---------------- */
  function ring() {
    if (reduce) return;
    var q = $('#q'); if (!q) return;
    var r = q.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    var d = Math.round(Math.hypot(innerWidth, innerHeight) * 0.34);
    var el = D.createElement('i'); el.className = 'mv-ring';
    el.style.width = d + 'px'; el.style.height = d + 'px'; el.style.marginLeft = (-d / 2) + 'px'; el.style.marginTop = (-d / 2) + 'px';
    el.style.left = cx + 'px'; el.style.top = cy + 'px';
    D.body.appendChild(el);
    try {
      var a = el.animate([{ transform: 'scale(.05)', opacity: .9 }, { transform: 'scale(1)', opacity: 0 }], { duration: 600, easing: EASE, fill: 'forwards' });
      a.onfinish = function () { el.remove(); };
    } catch (e) { el.remove(); }
  }

  /* ---------------- API ---------------- */
  var API = {};
  API.depart = function (o) {
    if (reduce) return;
    o = o || {};
    endCore(true, true);
    B.on = true; B.flux = !!o.flux || FLUX; H.classList.add('mv-hs');
    JOBS = {};
    ORDER.concat(['question-posee']).forEach(function (id) {
      if (id === 'question-posee') return;
      var j = makeJob(id); if (!j.w) return;
      gateJob(j, B.flux, B.flux ? 0 : DEV[id]);
    });
    INCreset();
    live.classList.add('on'); liveT.textContent = o.texte || '';
    B.ringAt = now() + 420; B.ringDone = false; B.pre = false;
    clearTimeout(B.wd); B.wd = setTimeout(function () { endCore(true); }, B.flux ? 60000 : 20000);
  };
  API.arriveRole = function (id, o) {
    if (reduce) return;
    var j = JOBS[id]; if (!j) { if (!B.on) return; j = makeJob(id); if (!j.w) return; gateJob(j); }
    j.opts = o || {}; j.go = true;
  };
  API.compteur = function (id, n) {
    var j = JOBS[id]; var labs = j ? j.labs : $$('.lab').filter(function (l) { return roleOfLab(l) === id; });
    if (j && !j.revealed) { j.opts = j.opts || {}; j.opts.compteur = n; return; }
    labs.forEach(function (l) { var c = $('[data-compteur]', l); if (c) cnt(c, n, 600, 0, 'cmp'); });
  };
  API.lien = function (id, etat) {
    var w = wireMap()[id]; if (!w) return;
    if (etat === 'casse') return casser(id);
    if (w.v) { if (etat === 'indice') w.v.classList.add('dash'); else w.v.classList.remove('dash'); }
  };
  API.ligne = function (texte) {
    liveT.textContent = texte; live.classList.add('on');
  };
  var inc = null;
  function INCreset() {
    inc = $('.lab.dzl'); openN = 0; linesShown = {};
    if (!inc) return;
    $$('li[data-question-ouverte]', inc).forEach(function (li) { li.style.clipPath = 'inset(0 100% 0 0)'; });
  }
  function incCounter() { return inc && $('[data-compteur]', inc); }
  API.question = function (n, etat, texte) {
    inc = inc || $('.lab.dzl'); if (!inc) return;
    var li = $('li[data-question-ouverte="' + n + '"]', inc); if (!li) return;
    if (etat === 'fermee') { strike(li, n); return; }
    if (texte && !$('input', li)) li.textContent = n + '. ' + texte;
    if (!linesShown[n]) {
      linesShown[n] = 1;
      li.style.transition = reduce ? 'none' : 'clip-path .3s ' + EASE;
      li.style.clipPath = 'inset(0 0 0 0)';
      setTimeout(function () { li.style.clipPath = ''; li.style.transition = ''; }, 500);
      if (n <= 5) { openN++; cnt(incCounter(), openN, 200, 0, 'inc'); }
    }
  };
  API.zone = function (id, etat) {
    var z = $('.zn[data-zone="' + id + '"]'); if (!z) return;
    if (etat === 'silhouette') z.classList.add('mv-zs'); else z.classList.remove('mv-zs');
  };
  API.fin = function () { endCore(false); };
  API.lancer = function () { if (reduce) return; if (FLUX) API.depart({ flux: true }); else scriptedStart(); };
  function preGate() {
    H.classList.add('mv-hs'); B.pre = true;
    ORDER.forEach(function (id) { var j = makeJob(id); gateJob(j, false, DEV[id]); });
    JOBS = {};
  }

  function strike(li, n) {
    if (li.classList.contains('mv-st')) return;
    li.classList.add('mv-st', 'mv-sa');
    var c = incCounter(), b = inc && $('b', inc);
    if (c) { var to = Math.max(0, (parseInt(c.textContent, 10) || 0) - 1); cnt(c, to, 300, 0, 'inc'); if (b) { b.classList.add('mv-y'); setTimeout(function () { b.classList.remove('mv-y'); }, 600); } }
  }

  /* ---------------- fin / saut à l'état final ---------------- */
  function endCore(skip, quiet) {
    var was = B.on;
    clearTimeout(B.wd);
    B.timers.forEach(clearTimeout); B.timers = [];
    cancelGrp('rev'); cancelGrp('inc');
    Object.keys(JOBS).forEach(function (k) { var j = JOBS[k]; releaseJob(j); });
    JOBS = {}; SC = null;
    B.gated.forEach(function (e) { e.classList.remove('mv-g', 'mv-sil'); }); B.gated = [];
    $$('.zn.mv-zs').forEach(function (z) { z.classList.remove('mv-zs'); });
    B.cnt.forEach(function (c) { if (c.dataset.mvFinal != null) { c.textContent = c.dataset.mvFinal; c.setAttribute('data-valeur', c.dataset.mvFinal); delete c.dataset.mvFinal; } }); B.cnt = [];
    if (inc) $$('li[data-question-ouverte]', inc).forEach(function (li) { li.style.clipPath = ''; li.style.transition = ''; });
    var fc = incCounter(); if (fc && was) { var tot = QS_COUNT(); fc.textContent = String(tot); fc.setAttribute('data-valeur', tot); }
    setTimeout(function () { B.forced.forEach(function (l) { l.classList.remove('mv-f'); }); B.forced = []; }, was && !skip ? 1200 : 1200);
    markY(null);
    live.classList.remove('on');
    B.on = false;
    if (quiet) return;
    if (was) {
      if (!skip) { typeSub(); }
      H.classList.remove('mv-hs');
    } else H.classList.remove('mv-hs');
  }
  function QS_COUNT() { if (!inc) return 0; return $$('li[data-question-ouverte]', inc).filter(function (li) { return !li.classList.contains('mv-st') && !$('.f', li) && !$('input', li) && !(/^6\./.test(li.textContent)); }).length || $$('li[data-question-ouverte]', inc).filter(function (li) { return !$('input', li) && !$('.f', li); }).length; }

  /* ---------------- séquence scriptée (passe par l'API) ---------------- */
  var LIVE = { v: 214, a: 61, e: 23, r: 14 };
  function scriptedStart() {
    API.depart(); API.ligne(liveText(0));
    SC = { k: 0, nextAt: 0, S0: 0, lastReveal: 0, tEnd: 0, lines: false };
  }
  function liveText(p) {
    var v = Math.round(LIVE.v * p), a = Math.round(LIVE.a * p), e = Math.round(LIVE.e * p), r = Math.round(LIVE.r * p);
    return (v + a + e + r) + ' sources lues : ' + v + ' vidéos, ' + a + ' articles, ' + e + ' études, ' + r + ' registres';
  }
  function scriptedTick(t) {
    if (!SC) return;
    if (!B.ringDone && t >= B.ringAt) { B.ringDone = true; ring(); SC.S0 = t + 520; SC.nextAt = SC.S0; }
    if (!SC.S0) return;
    var span = 5200;
    var p = clamp((t - SC.S0) / span, 0, 1);
    if (!SC.lines) API.ligne(liveText(outQ(p)));
    if (SC.k < ORDER.length && t >= SC.nextAt) {
      var j = JOBS[ORDER[SC.k]];
      if (!j) { SC.k++; return; }
      if (j.armed) {
        var last = SC.k === ORDER.length - 1;
        if (last) j.onReveal = function () {
          SC.lines = true; API.ligne(liveText(1));
          later(function () { API.fin(); }, 380 + 3 * 420 + 500);
        };
        API.arriveRole(ORDER[SC.k]); SC.nextAt = t + (DEV[ORDER[SC.k]] || 600); SC.k++;
      }
    }
  }

  /* ---------------- boucle (après celle du HTML) ---------------- */
  var labOn = new WeakMap(), firstLab = true;
  var fx = null;
  function tick() {
    var t = now();
    for (var i = TW.length - 1; i >= 0; i--) {
      var o = TW[i]; if (o.dead) { TW.splice(i, 1); continue; }
      var p = (t - o.t0) / o.dur; if (p < 0) continue;
      o.fn(clamp(p, 0, 1));
      if (p >= 1) { o.dead = true; TW.splice(i, 1); }
    }
    var ids = Object.keys(JOBS);
    for (var k = 0; k < ids.length; k++) {
      var j = JOBS[ids[k]]; if (!j.w || !j.w.v || j.done) continue;
      if (!j.started) {
        var d = j.w.v.getAttribute('d');
        if (j.hold) {
          var q = parseD(d);
          if (!j.armed) {
            if (q) {
              if (j.prev && Math.hypot(q.ex - j.prev.ex, q.ey - j.prev.ey) < 0.04) j.still++; else j.still = 0;
              j.prev = q;
              if (j.still >= 3) { j.armed = true; j.a = { x: q.ax, y: q.ay }; j.b = { x: q.ex, y: q.ey }; }
            } else { j.still = 0; j.prev = null; }
          }
          if (d) { j.w.v.setAttribute('d', ''); if (j.w.h) j.w.h.setAttribute('d', ''); }
        }
        if (j.go && j.armed) startJob(j);
      }
      if (j.started && !j.done) {
        var u = (t - j.startAt) / 800, pp = outQ(u);
        var ex = j.a.x + (j.b.x - j.a.x) * pp, ey = j.a.y + (j.b.y - j.a.y) * pp;
        j.w.v.setAttribute('d', 'M' + j.a.x.toFixed(1) + ' ' + j.a.y.toFixed(1) + 'L' + ex.toFixed(1) + ' ' + ey.toFixed(1));
        if (j.w.h) j.w.h.setAttribute('d', '');
        if (j.tip) j.tip.style.transform = 'translate(' + ex + 'px,' + ey + 'px)';
        if (u >= 0.5 && !j.revealed) revealJob(j);
        if (u >= 1) { if (!j.revealed) revealJob(j); releaseJob(j); }
      }
    }
    if (SC) scriptedTick(t);
    if (B.on && B.flux && !B.ringDone && t >= B.ringAt) { B.ringDone = true; ring(); }
    /* apparition d'étiquettes (hors construction) */
    var labs = $$('.lab');
    labs.forEach(function (l) {
      var on = l.classList.contains('on');
      if (labOn.get(l) !== on) {
        labOn.set(l, on);
        if (on && !firstLab) labAppear(l);
      }
    });
    firstLab = false;
  }
  function labAppear(l) {
    if (B.on || l.classList.contains('mv-g') || l.classList.contains('mv-f')) return;
    l.classList.add('mv-pop');
    l.addEventListener('animationend', function f() { l.classList.remove('mv-pop'); l.removeEventListener('animationend', f); });
    if (st.nom === 'niveau2' && !reduce) {
      var c = $('.cn[data-compteur]', l);
      if (c && !c._mv) { c._mv = 1; var v = parseInt(c.getAttribute('data-valeur'), 10) || 0; c.textContent = '0'; cnt(c, v, 700, 80); }
    }
  }
  function loop() { try { tick(); } catch (e) { window.__mvErr = String(e && e.stack || e); try { endCore(true); } catch (e2) {} } requestAnimationFrame(loop); }

  /* ---------------- fil qui casse ---------------- */
  function casser(id) {
    var w = wireMap()[id]; if (!w || !w.v) return;
    var q = parseD(w.v.getAttribute('d')); if (!q) return;
    if (!fx) { fx = D.createElementNS('http://www.w3.org/2000/svg', 'svg'); fx.id = 'mv-fx'; D.body.appendChild(fx); }
    var NS = 'http://www.w3.org/2000/svg';
    var pA = D.createElementNS(NS, 'path'), pB = D.createElementNS(NS, 'path');
    fx.appendChild(pA); fx.appendChild(pB);
    var mx = (q.ax + q.ex) / 2, my = (q.ay + q.ey) / 2, dx = q.ex - q.ax, dy = q.ey - q.ay, L = Math.hypot(dx, dy) || 1, ux = dx / L, uy = dy / L;
    w.g.classList.add('mv-broken');
    var D0 = 400;
    if (reduce) { setTimeout(function () { pA.remove(); pB.remove(); }, 200); return; }
    tw(D0, function (p) {
      var gap = 14 * outQ(p / 0.3 > 1 ? 1 : p / 0.3), r = outQ(p);
      var aE = { x: mx - ux * gap, y: my - uy * gap }, bE = { x: mx + ux * gap, y: my + uy * gap };
      var aTip = { x: aE.x + (q.ax - aE.x) * r, y: aE.y + (q.ay - aE.y) * r }, bTip = { x: bE.x + (q.ex - bE.x) * r, y: bE.y + (q.ey - bE.y) * r };
      pA.setAttribute('d', 'M' + q.ax + ' ' + q.ay + 'L' + aTip.x + ' ' + aTip.y);
      pB.setAttribute('d', 'M' + q.ex + ' ' + q.ey + 'L' + bTip.x + ' ' + bTip.y);
      pA.style.opacity = pB.style.opacity = String(1 - p * p);
      if (p >= 1) { pA.remove(); pB.remove(); }
    });
  }
  API.casserLien = casser;

  /* ---------------- poser une question (observer de la liste) ---------------- */
  var seenClosed = {}, lastCount = null, posed = false;
  function watchInc() {
    var el = $('.lab.dzl'); if (!el) return;
    inc = el;
    var rd = function () {
      var c = $('[data-compteur]', el);
      return c ? parseInt(c.getAttribute('data-valeur'), 10) : null;
    };
    lastCount = rd();
    $$('li[data-question-ouverte]', el).forEach(function (li) { if ($('.f', li)) { li.classList.add('mv-st'); seenClosed[li.dataset.questionOuverte] = 1; } });
    posed = !$('li[data-question-ouverte="6"] input', el);
    new MutationObserver(function () {
      if (B.on) { return; }
      var c = $('[data-compteur]', el), b = $('b', el), nv = rd();
      $$('li[data-question-ouverte]', el).forEach(function (li) {
        var n = li.dataset.questionOuverte;
        if ($('.f', li)) { li.classList.add('mv-st'); if (!seenClosed[n]) { seenClosed[n] = 1; li.classList.add('mv-sa'); } }
      });
      if (nv != null && lastCount != null && nv < lastCount && c) {
        c.textContent = String(lastCount); cnt(c, nv, 300);
        if (b) { b.classList.add('mv-y'); setTimeout(function () { b.classList.remove('mv-y'); }, 600); }
      }
      lastCount = nv;
      var np = !$('li[data-question-ouverte="6"] input', el);
      if (np && !posed) { posed = true; poser(); }
    }).observe(el, { childList: true });
  }
  function poser() {
    if (reduce) return;
    var j = makeJob('question-posee'); if (!j.w) return;
    gateJob(j); j.go = true;
    j.onReveal = function () {
      var qx = j.items[0]; if (!qx) return;
      var imgs = ['images/p-presse.jpg', 'images/p-registre.jpg'], els = [];
      imgs.forEach(function (src, i) {
        var e = D.createElement('i'); e.className = 'mv-pc'; e.style.backgroundImage = 'url(' + src + ')'; e.style.opacity = '0'; D.body.appendChild(e); els.push(e);
        setTimeout(function () { e.style.transition = 'opacity .4s ease'; e.style.opacity = '1'; }, 1000 * (i + 1));
      });
      var raf = function () {
        var r = qx.getBoundingClientRect();
        els.forEach(function (e, i) { e.style.transform = 'translate(' + (r.left + r.width / 2 - 40 + i * 84) + 'px,' + (r.bottom + 16 + i * 4) + 'px)'; });
        if (D.body.contains(els[0])) requestAnimationFrame(raf);
      };
      raf();
      setTimeout(function () { /* les pièces restent tant que l'on est au niveau 1 */ }, 0);
      var off = function () { if (st.nom !== 'niveau1') { els.forEach(function (e) { e.remove(); }); document.removeEventListener('etat', off); } };
      document.addEventListener('etat', off);
    };
    B.timers.push(setTimeout(function () { releaseGate(j); }, 15000));
  }
  function releaseGate(j) { j.items.concat(j.labs).forEach(function (e) { e.classList.remove('mv-g'); }); }

  /* ---------------- survol / entrées ---------------- */
  var wiresEl = $('#wires');
  function setHover(id) {
    if (!wiresEl) return;
    $$('#wires g.mv-hov').forEach(function (g) { g.classList.remove('mv-hov'); });
    if (!id) { wiresEl.classList.remove('mv-hovering'); return; }
    var g = $('#wires g[data-lien="' + id + '"]');
    if (!g) { wiresEl.classList.remove('mv-hovering'); return; }
    g.classList.add('mv-hov'); wiresEl.classList.add('mv-hovering');
  }
  D.addEventListener('mouseover', function (e) {
    if (st.nom !== 'niveau1' || B.on) { setHover(null); return; }
    var t = e.target; if (!t || !t.closest) return;
    var g = t.closest('#wires g[data-lien]'), id = g && g.getAttribute('data-lien');
    if (!id) { var it = t.closest('.it[data-role]'); id = it && it.dataset.role; }
    setHover(id || null);
  }, true);
  D.documentElement.addEventListener('mouseleave', function () { setHover(null); });

  function particule(id) {
    if (reduce) return;
    var w = wireMap()[id]; if (!w || !w.v) return;
    var q = parseD(w.v.getAttribute('d')); if (!q) return;
    var p = D.createElement('i'); p.className = 'mv-pt'; D.body.appendChild(p);
    try {
      var a = p.animate([{ transform: 'translate(' + q.ax + 'px,' + q.ay + 'px)', opacity: 1 }, { transform: 'translate(' + q.ex + 'px,' + q.ey + 'px)', opacity: 1, offset: .85 }, { transform: 'translate(' + q.ex + 'px,' + q.ey + 'px)', opacity: 0 }], { duration: 640, easing: EASE, fill: 'forwards' });
      a.onfinish = function () { p.remove(); };
    } catch (e) { p.remove(); }
  }

  /* ---------------- niveau 2 ---------------- */
  function flux2() {
    if (reduce) return;
    /* composition finale d'emblée, piles vides, puis chaque vidéo tombe sur la pile de son compte (25 ms d'écart) */
    var tags = $$('.lab.tag').filter(function (l) { return $('.cn[data-compteur]', l); });
    var counts = tags.map(function (l) { return parseInt($('.cn', l).getAttribute('data-valeur'), 10) || 0; });
    var cum = [], acc = 0; counts.forEach(function (n) { cum.push(acc); acc += n; });
    var grp = function (k) { var g = 0; for (var i = 0; i < cum.length; i++) if (k >= cum[i]) g = i; return g; };
    var have = tags.map(function () { return 0; });
    var tiles = $$('.it').filter(function (e) { return /^t\d+$/.test(e.dataset.id || '') && !e.classList.contains('ghost'); })
      .sort(function (x, y) { return parseInt(x.dataset.id.slice(1), 10) - parseInt(y.dataset.id.slice(1), 10); });
    var upd = function (g) { have[g]++; var c = $('.cn', tags[g]); if (c) c.textContent = String(have[g]); tags[g].classList.remove('mv-g'); };
    tags.forEach(function (l) { l.classList.add('mv-g', 'mv-f'); $('.cn', l).textContent = '0'; });
    var anim = [];
    tiles.forEach(function (e) {
      var k = parseInt(e.dataset.id.slice(1), 10);
      if (getComputedStyle(e).visibility === 'visible') { upd(grp(k)); return; }
      e.style.transition = 'none'; e.style.opacity = '0'; e.style.translate = '0 -12px'; anim.push(e);
    });
    void D.body.offsetWidth;
    anim.forEach(function (e, i) {
      var k = parseInt(e.dataset.id.slice(1), 10), dl = i * 25;
      e.style.transition = 'opacity .25s ' + EASE + ' ' + dl + 'ms,translate .25s ' + EASE + ' ' + dl + 'ms';
      e.style.opacity = '1'; e.style.translate = '0 0';
      setTimeout(function () { upd(grp(k)); }, dl);
    });
    setTimeout(function () {
      tiles.forEach(function (e) { e.style.transition = ''; e.style.opacity = ''; e.style.translate = ''; });
      tags.forEach(function (l, i) { l.classList.remove('mv-g', 'mv-f'); $('.cn', l).textContent = String(counts[i]); });
    }, anim.length * 25 + 400);
  }
  function rangement() {
    var tags = $$('.lab.tag').filter(function (l) { return $('.cn[data-compteur]', l); });
    tags.forEach(function (l) {
      l.classList.add('mv-f', 'mv-pop');
      l.addEventListener('animationend', function f() { l.classList.remove('mv-pop'); l.removeEventListener('animationend', f); });
      var c = $('.cn[data-compteur]', l); c._mv = 1;
      var v = parseInt(c.getAttribute('data-valeur'), 10) || 0; c.textContent = '0'; cnt(c, v, 700, 60);
      setTimeout(function () { l.classList.remove('mv-f'); }, 1300);
    });
  }
  var subEl = $('#sub');
  if (subEl) new MutationObserver(function () {
    if (B.on || H.classList.contains('mv-hs')) return;
    if (subEl.dataset.mvT === subEl.textContent && subEl.querySelector('.mv-w')) return;
    typeSub();
    if (st.nom === 'niveau2') { st.n2++; if (st.n2 >= 2 || st.depuis === 'preuve') rangement(); }
  }).observe(subEl, { childList: true, characterData: true, subtree: true });

  /* ---------------- événements d'état ---------------- */
  D.addEventListener('etat', function (e) {
    var d = e.detail || {}, nom = d.nom, depuis = d.depuis;
    try {
      if (B.on && nom !== 'niveau1' && nom !== 'niveau0') endCore(true);
      var prevNom = st.nom;
      st.nom = nom; st.depuis = depuis; st.n2 = 0;
      setHover(null);
      if (nom === 'niveau1' && (depuis === 'niveau0' || (!depuis && prevNom === 'niveau0')) && !B.on) {
        if (!reduce) scriptOrWait();
      } else if (nom === 'piece' && depuis === 'niveau1') particule(d.id);
      else if (nom === 'niveau2' && depuis === 'niveau1') { particule('amplificateurs'); flux2(); }
      else if (nom === 'niveau2' && depuis === 'preuve') { /* rangement géré par le sous-titre */ }
    } catch (err) {}
  });
  function scriptOrWait() { if (FLUX) API.depart({ flux: true }); else scriptedStart(); }

  /* tout clic ou toute touche saute à l'état final */
  function skip() { if (B.on) endCore(true); }
  addEventListener('pointerdown', skip, true);
  addEventListener('keydown', skip, true);

  /* ---------------- démarrage ---------------- */
  var navUrl = '';
  try { navUrl = performance.getEntriesByType('navigation')[0].name || ''; } catch (e) {}
  var anchored = /#(niveau[12]|preuve|dossier-|zone-)/.test(navUrl), n0anch = /#niveau0/.test(navUrl);
  var h0 = location.hash;
  st.nom = ({ '#niveau0': 'niveau0', '#niveau1': 'niveau1', '#niveau2': 'niveau2', '#preuve': 'preuve' })[h0] || (h0.indexOf('#dossier-') === 0 ? 'piece' : h0.indexOf('#zone-') === 0 ? 'zone' : 'niveau0');
  window.Mouvement = API;
  if (st.nom !== 'niveau0') anchored = true; /* repli : l'adresse d'origine n'est pas toujours lisible */
  if (anchored) {
    /* ancre directe : aucune animation d'entrée. On ne rend la main qu'à une vraie navigation (clic, touche, changement d'ancre, autre niveau), pas à un re-rendu dû à un redimensionnement. */
    H.classList.add('mv-na');
    var naOff = function () { H.classList.remove('mv-na'); D.removeEventListener('etat', naEtat); removeEventListener('pointerdown', naOff, true); removeEventListener('keydown', naOff, true); removeEventListener('hashchange', naOff); };
    var naStart = st.nom, naEtat = function (e) { if (e.detail && e.detail.nom !== naStart) naOff(); };
    D.addEventListener('etat', naEtat); addEventListener('pointerdown', naOff, true); addEventListener('keydown', naOff, true); addEventListener('hashchange', naOff);
  }
  watchInc();
  try {
    if (!anchored && st.nom === 'niveau0' && !reduce) {
      /* ouverture : la question s'écrit mot à mot, puis le HTML passe seul au niveau 1 */
      preGate();
      var q = $('#q');
      if (q) { wrapWords(q, 60); q.classList.add('mv-pre'); void q.offsetWidth; setTimeout(function () { q.classList.remove('mv-pre'); }, 250); }
      var n0 = $$('.lab.dzc')[0];
      if (n0 && !n0anch) { n0.classList.add('mv-g'); B.gated.push(n0); B.wd = setTimeout(function () { endCore(true); }, 20000); }
    } else if (REJOUER && st.nom === 'niveau1' && !reduce) {
      setTimeout(function () { if (FLUX) API.depart({ flux: true }); else scriptedStart(); }, 60);
    }
  } catch (e) {}
  requestAnimationFrame(loop);
})();
