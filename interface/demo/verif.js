// Vérification de mise en page : rien ne se chevauche, rien ne sort de la fenêtre.
// Usage : ?verif dans l'URL (lance tout seul), ou window.Verif.lancer([options]) dans la console.
// Éléments relevés : tout élément visible portant [data-verif] (ou [data-v]) ; à défaut, la liste
// de sélecteurs par défaut ci-dessous. Sont ignorés : display none, visibility hidden, opacité < 0,05
// (elle-même ou celle d'un parent), taille nulle. Deux éléments dont l'un contient l'autre ne sont pas
// comparés. Tolérance 0 (1/100 px de bruit d'arrondi). Le rapport JSON va dans document.title et dans
// <pre id="verif">.
(function () {
  const PAR_DEFAUT = [".carte", ".etiq", ".etiquette", ".img", ".image", "h1", ".titre", ".soustitre",
    ".sous-titre", ".bandeau", ".logo", ".infobulle", ".zones", ".zone"];
  const EPS = 0.01;
  let dejaFait = false;

  function opaque(el) {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) < 0.05) return false;
    }
    return true;
  }

  function nom(el) {
    const v = el.getAttribute("data-verif") || el.getAttribute("data-v");
    if (v) return v;
    const c = (el.className && typeof el.className === "string") ? "." + el.className.trim().split(/\s+/).join(".") : "";
    return el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + c;
  }

  function releve(racine) {
    let liste = [...(racine || document).querySelectorAll("[data-verif],[data-v]")];
    if (!liste.length) liste = [...(racine || document).querySelectorAll(PAR_DEFAUT.join(","))];
    return liste.filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && opaque(el);
    });
  }

  function lancer(options = {}) {
    const W = window.innerWidth, H = window.innerHeight;
    const els = releve(options.racine);
    const rects = els.map((el) => ({ el, nom: nom(el), r: el.getBoundingClientRect() }));
    const conflits = [], hors = [], debord = [];
    for (let i = 0; i < rects.length; i++) {
      const a = rects[i].r;
      if (a.left < -EPS || a.top < -EPS || a.right > W + EPS || a.bottom > H + EPS) {
        hors.push({ nom: rects[i].nom, rect: [Math.round(a.left), Math.round(a.top), Math.round(a.right), Math.round(a.bottom)] });
      }
      const el = rects[i].el;
      if (el.scrollHeight > el.clientHeight + 1 && getComputedStyle(el).overflow === "visible" && el.hasAttribute("data-clos")) {
        debord.push(rects[i].nom);
      }
      for (let j = i + 1; j < rects.length; j++) {
        const b = rects[j].r, eb = rects[j].el;
        if (el.contains(eb) || eb.contains(el)) continue;
        if (a.left < b.right - EPS && b.left < a.right - EPS && a.top < b.bottom - EPS && b.top < a.bottom - EPS) {
          conflits.push([rects[i].nom, rects[j].nom]);
        }
      }
    }
    const rapport = {
      ok: !conflits.length && !hors.length && !debord.length && !(options.echecs > 0),
      taille: [W, H],
      page: location.pathname.split("/").pop() + (new URLSearchParams(location.search).get("enquete") ? "?enquete=" + new URLSearchParams(location.search).get("enquete") : ""),
      elements: rects.length,
      conflits, hors, debord,
      infobulle: rects.some((x) => x.nom === "info" || x.nom.includes("infobulle")),
    };
    if (options.extra) Object.assign(rapport, options.extra);
    const texte = JSON.stringify(rapport);
    document.title = texte;
    let pre = document.getElementById("verif");
    if (!pre) {
      pre = document.createElement("pre");
      pre.id = "verif";
      pre.style.cssText = "position:fixed;left:-99999px;top:0;margin:0";
      document.body.appendChild(pre);
    }
    pre.textContent = texte;
    dejaFait = true;
    return rapport;
  }

  window.Verif = { lancer, releve };

  if (/[?&]verif(&|=|$)/.test(location.search)) {
    window.addEventListener("reseau-pret", () => { if (!dejaFait) setTimeout(() => lancer(), 50); });
    const delai = Number(new URLSearchParams(location.search).get("verifdelai")) || 2500;
    window.addEventListener("load", () => setTimeout(() => { if (!dejaFait) lancer(); }, delai));
  }
})();
