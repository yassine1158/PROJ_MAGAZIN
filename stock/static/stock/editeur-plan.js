// Éditeur graphique du plan du magasin : murs, blocs et étagères, vus de dessus.
// Unités : mètres. x vers la droite, z vers le bas de l'écran (vers le fond du magasin dans la 3D).
(() => {
  const CFG = window.EDITEUR;
  const NS = 'http://www.w3.org/2000/svg';
  const $ = (id) => document.getElementById(id);
  const svg = $('plan');
  const PALETTE = ['#2563eb', '#16a34a', '#d97706', '#9333ea', '#dc2626', '#0891b2', '#db2777', '#65a30d'];

  // ---------------------------------------------------------------- état
  let etat = { blocs: [], etageres: [], murs: [] };
  let selection = null;           // { type: 'bloc'|'etagere'|'mur', cle }
  let outil = 'selection';
  let aimant = true;
  let modifie = false;
  let historique = [];
  let compteur = 0;
  const nouvelleCle = (p) => `${p}n${++compteur}`;
  let vue = { x: -2, z: -2, l: 30, h: 20 }; // viewBox

  function depuisServeur(plan) {
    const e = { blocs: [], etageres: [], murs: [] };
    for (const b of plan.blocs) {
      const cle = `b${b.id}`;
      e.blocs.push({ cle, id: b.id, code: b.code, nom: b.nom, couleur: b.couleur, x: b.x, z: b.z,
        largeur: b.largeur, profondeur: b.profondeur });
      for (const t of b.etageres) {
        e.etageres.push({ cle: `e${t.id}`, id: t.id, bloc: cle, code: t.code, x: b.x + t.x, z: b.z + t.z,
          largeur: t.largeur, profondeur: t.profondeur, hauteur: t.hauteur, niveaux: t.niveaux,
          tournee: t.tournee, articles: t.articles || 0 });
      }
    }
    for (const m of plan.murs) e.murs.push({ cle: `m${m.id}`, ...m });
    return e;
  }

  // Plan au format de la vue 3D (étagères relatives à leur bloc).
  function versPlan3D() {
    return {
      blocs: etat.blocs.map((b) => ({
        ...b, etageres: etat.etageres.filter((t) => t.bloc === b.cle).map((t) => ({ ...t, x: t.x - b.x, z: t.z - b.z })),
      })),
      murs: etat.murs,
    };
  }

  const copie = (o) => JSON.parse(JSON.stringify(o));
  function memoriser() {
    historique.push(copie(etat));
    if (historique.length > 100) historique.shift();
    modifie = true;
    majBoutons();
  }
  function annuler() {
    if (!historique.length) return;
    etat = historique.pop();
    if (selection && !trouver(selection)) selection = null;
    modifie = true;
    toutRedessiner();
  }

  // ---------------------------------------------------------------- outils géométriques
  const arrondi = (v) => Math.round(v * 100) / 100;
  const pas = () => (aimant ? 0.5 : 0.05);
  const caler = (v) => arrondi(Math.round(v / pas()) * pas());
  const emprise = (t) => (t.tournee ? { l: t.profondeur, p: t.largeur } : { l: t.largeur, p: t.profondeur });
  const liste = (type) => etat[{ bloc: 'blocs', etagere: 'etageres', mur: 'murs' }[type]];
  const trouver = (s) => s && liste(s.type).find((o) => o.cle === s.cle);
  const blocEn = (x, z) => etat.blocs.find((b) => x >= b.x && x <= b.x + b.largeur && z >= b.z && z <= b.z + b.profondeur);
  const metresParPixel = () => vue.l / svg.clientWidth;

  function pointMonde(ev) {
    const p = svg.createSVGPoint();
    p.x = ev.clientX; p.y = ev.clientY;
    const r = p.matrixTransform(svg.getScreenCTM().inverse());
    return { x: r.x, z: r.y };
  }

  function codeBlocLibre() {
    const pris = new Set(etat.blocs.map((b) => b.code.toUpperCase()));
    for (const c of 'ABCDEFGHIJKLMNOPQRSTUVWXYZ') if (!pris.has(c)) return c;
    let n = 1; while (pris.has(`B${n}`)) n++; return `B${n}`;
  }
  function codeEtagereLibre(blocCle, prefixe = 'E') {
    const pris = new Set(etat.etageres.filter((t) => t.bloc === blocCle).map((t) => t.code.toUpperCase()));
    let n = 1; while (pris.has(`${prefixe}${n}`.toUpperCase())) n++; return `${prefixe}${n}`;
  }

  // ---------------------------------------------------------------- dessin SVG
  const el = (nom, attrs = {}, parent = null) => {
    const n = document.createElementNS(NS, nom);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };

  // Grille : un trait par mètre, plus marqué tous les 5 m (seulement la partie visible).
  function dessinerGrille() {
    const g = $('grille');
    g.replaceChildren();
    const ecart = vue.l > 120 ? 5 : 1;
    const x0 = Math.floor(vue.x / ecart) * ecart, z0 = Math.floor(vue.z / ecart) * ecart;
    const trait = (x1, y1, x2, y2, fort) => el('line', { x1, y1, x2, y2, class: fort ? 'grille-forte' : 'grille-fine',
      'vector-effect': 'non-scaling-stroke' }, g);
    for (let x = x0; x <= vue.x + vue.l; x += ecart) trait(x, vue.z, x, vue.z + vue.h, Math.round(x) % 5 === 0);
    for (let z = z0; z <= vue.z + vue.h; z += ecart) trait(vue.x, z, vue.x + vue.l, z, Math.round(z) % 5 === 0);
  }

  function dessiner() {
    svg.setAttribute('viewBox', `${vue.x} ${vue.z} ${vue.l} ${vue.h}`);
    dessinerGrille();
    const contenu = $('contenu');
    contenu.replaceChildren();
    const mpp = metresParPixel();

    for (const b of etat.blocs) {
      const g = el('g', { 'data-type': 'bloc', 'data-cle': b.cle, class: 'objet' }, contenu);
      el('rect', { x: b.x, y: b.z, width: b.largeur, height: b.profondeur, fill: b.couleur, 'fill-opacity': 0.13,
        stroke: b.couleur, 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke', rx: 0.15 }, g);
      const t = el('text', { x: b.x + 0.05, y: b.z - 0.15, class: 'lbl-bloc', fill: b.couleur,
        'font-size': Math.max(0.35, Math.min(0.75, b.largeur / 9)) }, g);
      t.textContent = `Bloc ${b.code}${b.nom ? ' · ' + b.nom : ''}`;
    }
    for (const t of etat.etageres) {
      const { l, p } = emprise(t);
      const g = el('g', { 'data-type': 'etagere', 'data-cle': t.cle, class: 'objet' }, contenu);
      el('rect', { x: t.x, y: t.z, width: l, height: p, class: 'etagere', rx: 0.04,
        'stroke-width': 1, 'vector-effect': 'non-scaling-stroke' }, g);
      const txt = el('text', { x: t.x + l / 2, y: t.z + p / 2, class: 'lbl-etagere',
        'font-size': Math.max(0.18, Math.min(0.42, Math.min(l, p) * 0.55)) }, g);
      txt.textContent = t.code;
    }
    for (const m of etat.murs) {
      const g = el('g', { 'data-type': 'mur', 'data-cle': m.cle, class: 'objet' }, contenu);
      el('line', { x1: m.x1, y1: m.z1, x2: m.x2, y2: m.z2, class: 'mur', 'stroke-width': m.epaisseur || 0.2,
        'stroke-linecap': 'square' }, g);
      // zone de clic plus large que le mur
      el('line', { x1: m.x1, y1: m.z1, x2: m.x2, y2: m.z2, stroke: 'transparent', 'stroke-width': 14 * mpp }, g);
    }

    // Sélection et poignées
    const o = trouver(selection);
    if (o) {
      const taille = 11 * mpp;
      const poignee = (x, z, nom) => el('rect', { x: x - taille / 2, y: z - taille / 2, width: taille, height: taille,
        class: 'poignee', 'data-poignee': nom, 'stroke-width': 1.5, 'vector-effect': 'non-scaling-stroke' }, contenu);
      if (selection.type === 'mur') {
        el('line', { x1: o.x1, y1: o.z1, x2: o.x2, y2: o.z2, class: 'contour-selection', 'stroke-width': 2,
          'vector-effect': 'non-scaling-stroke' }, contenu);
        poignee(o.x1, o.z1, 'p1'); poignee(o.x2, o.z2, 'p2');
      } else {
        const r = rect(o);
        el('rect', { x: r.x, y: r.z, width: r.l, height: r.p, class: 'contour-selection', 'stroke-width': 2,
          'vector-effect': 'non-scaling-stroke' }, contenu);
        poignee(r.x, r.z, 'nw'); poignee(r.x + r.l, r.z, 'ne');
        poignee(r.x, r.z + r.p, 'sw'); poignee(r.x + r.l, r.z + r.p, 'se');
      }
    }
    if (trace) dessinerTrace(contenu);
  }

  function rect(o) {
    if (o.largeur !== undefined && o.niveaux !== undefined) { const { l, p } = emprise(o); return { x: o.x, z: o.z, l, p }; }
    return { x: o.x, z: o.z, l: o.largeur, p: o.profondeur };
  }

  function dessinerTrace(contenu) {
    if (trace.type === 'mur') {
      el('line', { x1: trace.x1, y1: trace.z1, x2: trace.x2, y2: trace.z2, class: 'mur trace', 'stroke-width': 0.2,
        'stroke-linecap': 'square' }, contenu);
    } else {
      const x = Math.min(trace.x1, trace.x2), z = Math.min(trace.z1, trace.z2);
      el('rect', { x, y: z, width: Math.abs(trace.x2 - trace.x1), height: Math.abs(trace.z2 - trace.z1),
        class: 'trace-rect', 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke' }, contenu);
    }
  }

  // ---------------------------------------------------------------- panneau de propriétés
  function champ(label, nom, valeur, type = 'number', extra = '') {
    const v = valeur ?? '';
    return `<div class="champ"><label>${label}</label><input type="${type}" data-prop="${nom}" value="${String(v).replace(/"/g, '&quot;')}" ${type === 'number' ? 'step="0.1"' : ''} ${extra}></div>`;
  }

  function panneau() {
    const zone = $('proprietes');
    const o = trouver(selection);
    if (!o) {
      zone.innerHTML = $('aide-' + outil).innerHTML;
      return;
    }
    let html = '';
    if (selection.type === 'bloc') {
      const n = etat.etageres.filter((t) => t.bloc === o.cle).length;
      html = `<h3>Bloc ${o.code}</h3><p class="muted petit">${n} étagère(s)</p>
        <div class="grille-2">${champ('Code', 'code', o.code, 'text', 'maxlength="10"')}${champ('Couleur', 'couleur', o.couleur, 'color')}</div>
        ${champ('Nom', 'nom', o.nom, 'text', 'maxlength="100" placeholder="Ex. : Pièces moteur"')}
        <div class="grille-2">${champ('Largeur (m)', 'largeur', o.largeur)}${champ('Profondeur (m)', 'profondeur', o.profondeur)}</div>
        <div class="grille-2">${champ('Position X (m)', 'x', o.x)}${champ('Position Z (m)', 'z', o.z)}</div>`;
    } else if (selection.type === 'etagere') {
      const options = etat.blocs.map((b) => `<option value="${b.cle}" ${b.cle === o.bloc ? 'selected' : ''}>Bloc ${b.code}</option>`).join('');
      html = `<h3>Étagère ${o.code}</h3>
        ${o.articles ? `<p class="muted petit">${o.articles} article(s) rangé(s) ici</p>` : ''}
        <div class="grille-2">${champ('Code', 'code', o.code, 'text', 'maxlength="20"')}
          <div class="champ"><label>Bloc</label><select data-prop="bloc">${options}</select></div></div>
        <div class="grille-2">${champ('Largeur (m)', 'largeur', o.largeur)}${champ('Profondeur (m)', 'profondeur', o.profondeur)}</div>
        <div class="grille-2">${champ('Hauteur (m)', 'hauteur', o.hauteur)}${champ('Niveaux', 'niveaux', o.niveaux, 'number', 'min="1" max="30" step="1"')}</div>
        <div class="grille-2">${champ('Position X (m)', 'x', o.x)}${champ('Position Z (m)', 'z', o.z)}</div>
        <button class="btn" data-action="tourner" style="width:100%">↻ Tourner de 90°</button>`;
    } else {
      const longueur = Math.hypot(o.x2 - o.x1, o.z2 - o.z1);
      html = `<h3>Mur</h3><p class="muted petit">Longueur : <b>${longueur.toFixed(2).replace('.', ',')} m</b></p>
        <div class="grille-2">${champ('Épaisseur (m)', 'epaisseur', o.epaisseur)}${champ('Hauteur (m)', 'hauteur', o.hauteur)}</div>
        <div class="grille-2">${champ('Début X', 'x1', o.x1)}${champ('Début Z', 'z1', o.z1)}</div>
        <div class="grille-2">${champ('Fin X', 'x2', o.x2)}${champ('Fin Z', 'z2', o.z2)}</div>`;
    }
    html += `<div class="actions-objet">
      <button class="btn" data-action="dupliquer">⧉ Dupliquer</button>
      <button class="btn btn-rouge" data-action="supprimer">Supprimer</button></div>`;
    zone.innerHTML = html;
  }

  $('proprietes').addEventListener('change', (ev) => {
    const prop = ev.target.dataset.prop;
    const o = trouver(selection);
    if (!prop || !o) return;
    memoriser();
    let v = ev.target.value;
    if (ev.target.type === 'number') {
      v = parseFloat(String(v).replace(',', '.'));
      if (!Number.isFinite(v)) { panneau(); return; }
      if (['largeur', 'profondeur', 'hauteur', 'epaisseur'].includes(prop)) v = Math.max(0.1, v);
      if (prop === 'niveaux') v = Math.max(1, Math.min(30, Math.round(v)));
      v = arrondi(v);
    }
    if (selection.type === 'bloc' && (prop === 'x' || prop === 'z')) {
      const d = v - o[prop];
      for (const t of etat.etageres) if (t.bloc === o.cle) t[prop] = arrondi(t[prop] + d);
    }
    o[prop] = v;
    toutRedessiner();
  });

  $('proprietes').addEventListener('click', (ev) => {
    const action = ev.target.closest('[data-action]')?.dataset.action;
    if (action === 'supprimer') supprimer();
    if (action === 'dupliquer') dupliquer();
    if (action === 'tourner') tourner();
  });

  // ---------------------------------------------------------------- actions
  function supprimer() {
    const o = trouver(selection);
    if (!o) return;
    if (selection.type === 'bloc') {
      const enfants = etat.etageres.filter((t) => t.bloc === o.cle);
      const articles = enfants.reduce((s, t) => s + (t.articles || 0), 0);
      if (enfants.length && !confirm(`Supprimer le bloc ${o.code} et ses ${enfants.length} étagère(s) ?`
        + (articles ? `\n${articles} article(s) deviendront « non rangés ».` : ''))) return;
      memoriser();
      etat.etageres = etat.etageres.filter((t) => t.bloc !== o.cle);
      etat.blocs = etat.blocs.filter((b) => b !== o);
    } else if (selection.type === 'etagere') {
      if (o.articles && !confirm(`${o.articles} article(s) sont rangés sur l'étagère ${o.code}. Ils deviendront « non rangés ». Supprimer ?`)) return;
      memoriser();
      etat.etageres = etat.etageres.filter((t) => t !== o);
    } else {
      memoriser();
      etat.murs = etat.murs.filter((m) => m !== o);
    }
    selection = null;
    toutRedessiner();
  }

  function dupliquer() {
    const o = trouver(selection);
    if (!o) return;
    memoriser();
    if (selection.type === 'mur') {
      const d = 1;
      const m = { ...o, cle: nouvelleCle('m'), id: null, x1: o.x1 + d, z1: o.z1 + d, x2: o.x2 + d, z2: o.z2 + d };
      etat.murs.push(m); selection = { type: 'mur', cle: m.cle };
    } else if (selection.type === 'etagere') {
      const { l } = emprise(o);
      const t = { ...o, cle: nouvelleCle('e'), id: null, articles: 0, x: arrondi(o.x + l + 0.2),
        code: codeEtagereLibre(o.bloc, (o.code.match(/^\D*/) || ['E'])[0] || 'E') };
      const b = blocEn(t.x + emprise(t).l / 2, t.z + emprise(t).p / 2);
      if (b) t.bloc = b.cle;
      t.code = codeEtagereLibre(t.bloc, (o.code.match(/^\D*/) || ['E'])[0] || 'E');
      etat.etageres.push(t); selection = { type: 'etagere', cle: t.cle };
    } else {
      const b = { ...o, cle: nouvelleCle('b'), id: null, code: codeBlocLibre(), x: arrondi(o.x + o.largeur + 1),
        couleur: PALETTE[etat.blocs.length % PALETTE.length] };
      etat.blocs.push(b);
      for (const t of etat.etageres.filter((e) => e.bloc === o.cle)) {
        etat.etageres.push({ ...t, cle: nouvelleCle('e'), id: null, articles: 0, bloc: b.cle, x: arrondi(t.x + o.largeur + 1) });
      }
      selection = { type: 'bloc', cle: b.cle };
    }
    toutRedessiner();
  }

  function tourner() {
    const o = trouver(selection);
    if (!o || selection.type !== 'etagere') return;
    memoriser();
    const avant = emprise(o);
    const cx = o.x + avant.l / 2, cz = o.z + avant.p / 2;
    o.tournee = !o.tournee;
    const apres = emprise(o);
    o.x = arrondi(cx - apres.l / 2); o.z = arrondi(cz - apres.p / 2);
    toutRedessiner();
  }

  function deplacer(o, type, dx, dz) {
    if (type === 'mur') { o.x1 = arrondi(o.x1 + dx); o.x2 = arrondi(o.x2 + dx); o.z1 = arrondi(o.z1 + dz); o.z2 = arrondi(o.z2 + dz); return; }
    o.x = arrondi(o.x + dx); o.z = arrondi(o.z + dz);
    if (type === 'bloc') for (const t of etat.etageres) if (t.bloc === o.cle) { t.x = arrondi(t.x + dx); t.z = arrondi(t.z + dz); }
  }

  // Une étagère posée dans un autre bloc change de bloc.
  function rattacher(t) {
    const { l, p } = emprise(t);
    const b = blocEn(t.x + l / 2, t.z + p / 2);
    if (b && b.cle !== t.bloc) {
      t.bloc = b.cle;
      if (etat.etageres.some((e) => e !== t && e.bloc === b.cle && e.code.toUpperCase() === t.code.toUpperCase())) {
        t.code = codeEtagereLibre(b.cle);
      }
      message(`Étagère ${t.code} déplacée dans le bloc ${b.code}.`);
    }
  }

  // ---------------------------------------------------------------- souris
  let glisse = null;   // { mode: 'deplacer'|'poignee'|'vue', ... }
  let trace = null;    // dessin en cours (mur, bloc, étagère)

  svg.addEventListener('pointerdown', (ev) => {
    if (ev.button === 2 || ev.button === 1) { // clic droit / molette : déplacer la vue
      glisse = { mode: 'vue', depart: { x: ev.clientX, y: ev.clientY }, vue: { ...vue } };
      svg.setPointerCapture(ev.pointerId);
      return;
    }
    const p = pointMonde(ev);
    const poignee = ev.target.dataset.poignee;
    const cible = ev.target.closest('.objet');

    if (outil === 'selection') {
      if (poignee && selection) {
        memoriser();
        glisse = { mode: 'poignee', poignee, objet: trouver(selection), type: selection.type };
      } else if (cible) {
        selection = { type: cible.dataset.type, cle: cible.dataset.cle };
        memoriserAuPremierMouvement = true;
        glisse = { mode: 'deplacer', objet: trouver(selection), type: selection.type, dernier: p,
          cumul: { x: 0, z: 0 }, origine: copie(trouver(selection)) };
        panneau();
      } else {
        selection = null;
        glisse = { mode: 'vue', depart: { x: ev.clientX, y: ev.clientY }, vue: { ...vue } };
        panneau();
      }
      svg.setPointerCapture(ev.pointerId);
      dessiner();
      return;
    }
    const x = caler(p.x), z = caler(p.z);
    trace = { type: outil, x1: x, z1: z, x2: x, z2: z };
    svg.setPointerCapture(ev.pointerId);
    dessiner();
  });

  let memoriserAuPremierMouvement = false;

  svg.addEventListener('pointermove', (ev) => {
    const p = pointMonde(ev);
    $('coord').textContent = `x ${p.x.toFixed(1).replace('.', ',')} m · z ${p.z.toFixed(1).replace('.', ',')} m`;
    if (glisse?.mode === 'vue') {
      const k = vue.l / svg.clientWidth;
      vue.x = glisse.vue.x - (ev.clientX - glisse.depart.x) * k;
      vue.z = glisse.vue.z - (ev.clientY - glisse.depart.y) * k;
      dessiner();
      return;
    }
    if (glisse?.mode === 'deplacer') {
      // Déplacement calé sur la grille, à partir de la position d'origine.
      const o = glisse.objet;
      const brut = { x: p.x - glisse.dernier.x, z: p.z - glisse.dernier.z };
      const ref = glisse.type === 'mur' ? { x: glisse.origine.x1, z: glisse.origine.z1 } : { x: glisse.origine.x, z: glisse.origine.z };
      const cibleX = caler(ref.x + brut.x), cibleZ = caler(ref.z + brut.z);
      const actuel = glisse.type === 'mur' ? { x: o.x1, z: o.z1 } : { x: o.x, z: o.z };
      const dx = cibleX - actuel.x, dz = cibleZ - actuel.z;
      if (dx || dz) {
        if (memoriserAuPremierMouvement) { memoriser(); memoriserAuPremierMouvement = false; }
        deplacer(o, glisse.type, dx, dz);
        dessiner();
        afficherMesure(o, glisse.type);
      }
      return;
    }
    if (glisse?.mode === 'poignee') {
      redimensionner(glisse, caler(p.x), caler(p.z), ev.shiftKey);
      dessiner();
      afficherMesure(glisse.objet, glisse.type);
      return;
    }
    if (trace) {
      let x = caler(p.x), z = caler(p.z);
      if (trace.type === 'mur' && !ev.shiftKey) { // murs droits par défaut ; Maj = n'importe quel angle
        if (Math.abs(x - trace.x1) >= Math.abs(z - trace.z1)) z = trace.z1; else x = trace.x1;
      }
      trace.x2 = x; trace.z2 = z;
      dessiner();
      const l = trace.type === 'mur' ? Math.hypot(x - trace.x1, z - trace.z1) : null;
      $('mesure').textContent = l !== null ? `Longueur : ${l.toFixed(2).replace('.', ',')} m`
        : `${Math.abs(x - trace.x1).toFixed(1).replace('.', ',')} × ${Math.abs(z - trace.z1).toFixed(1).replace('.', ',')} m`;
    }
  });

  svg.addEventListener('pointerup', () => {
    if (glisse?.mode === 'deplacer' && glisse.type === 'etagere' && !memoriserAuPremierMouvement) rattacher(glisse.objet);
    if (glisse && glisse.mode !== 'vue') toutRedessiner();
    glisse = null;
    memoriserAuPremierMouvement = false;
    if (trace) { terminerTrace(); trace = null; }
    $('mesure').textContent = '';
  });

  svg.addEventListener('contextmenu', (ev) => ev.preventDefault());

  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    zoomer(ev.deltaY > 0 ? 1.15 : 1 / 1.15, pointMonde(ev));
  }, { passive: false });

  function zoomer(facteur, centre = { x: vue.x + vue.l / 2, z: vue.z + vue.h / 2 }) {
    const l = Math.min(400, Math.max(3, vue.l * facteur));
    const k = l / vue.l;
    vue = { x: centre.x - (centre.x - vue.x) * k, z: centre.z - (centre.z - vue.z) * k, l, h: vue.h * k };
    dessiner();
  }

  function ajuster() {
    const xs = [], zs = [];
    for (const b of etat.blocs) { xs.push(b.x, b.x + b.largeur); zs.push(b.z, b.z + b.profondeur); }
    for (const m of etat.murs) { xs.push(m.x1, m.x2); zs.push(m.z1, m.z2); }
    const ratio = svg.clientHeight / svg.clientWidth || 0.6;
    if (!xs.length) { vue = { x: -2, z: -2, l: 30, h: 30 * ratio }; dessiner(); return; }
    const minX = Math.min(...xs) - 2, maxX = Math.max(...xs) + 2, minZ = Math.min(...zs) - 2, maxZ = Math.max(...zs) + 2;
    const l = Math.max(maxX - minX, (maxZ - minZ) / ratio);
    vue = { x: (minX + maxX) / 2 - l / 2, z: (minZ + maxZ) / 2 - (l * ratio) / 2, l, h: l * ratio };
    dessiner();
  }

  function redimensionner(g, x, z, garderRapport) {
    const o = g.objet;
    if (g.type === 'mur') {
      if (g.poignee === 'p1') { o.x1 = x; o.z1 = z; } else { o.x2 = x; o.z2 = z; }
      return;
    }
    const r = rect(o);
    // Coin opposé fixe.
    const fixeX = g.poignee.includes('w') ? r.x + r.l : r.x;
    const fixeZ = g.poignee.includes('n') ? r.z + r.p : r.z;
    const mini = g.type === 'bloc' ? 0.5 : 0.2;
    let l = Math.max(mini, Math.abs(x - fixeX)), p = Math.max(mini, Math.abs(z - fixeZ));
    if (garderRapport) { const k = Math.max(l / r.l, p / r.p); l = arrondi(r.l * k); p = arrondi(r.p * k); }
    const nx = g.poignee.includes('w') ? fixeX - l : fixeX;
    const nz = g.poignee.includes('n') ? fixeZ - p : fixeZ;
    if (g.type === 'bloc') {
      o.x = arrondi(nx); o.z = arrondi(nz); o.largeur = arrondi(l); o.profondeur = arrondi(p);
    } else {
      o.x = arrondi(nx); o.z = arrondi(nz);
      if (o.tournee) { o.profondeur = arrondi(l); o.largeur = arrondi(p); } else { o.largeur = arrondi(l); o.profondeur = arrondi(p); }
    }
  }

  function afficherMesure(o, type) {
    if (type === 'mur') {
      $('mesure').textContent = `Longueur : ${Math.hypot(o.x2 - o.x1, o.z2 - o.z1).toFixed(2).replace('.', ',')} m`;
    } else {
      const r = rect(o);
      $('mesure').textContent = `${r.l.toFixed(2).replace('.', ',')} × ${r.p.toFixed(2).replace('.', ',')} m · position ${r.x.toFixed(1).replace('.', ',')} ; ${r.z.toFixed(1).replace('.', ',')}`;
    }
  }

  function terminerTrace() {
    const t = trace;
    const x = Math.min(t.x1, t.x2), z = Math.min(t.z1, t.z2);
    let l = Math.abs(t.x2 - t.x1), p = Math.abs(t.z2 - t.z1);
    if (t.type === 'mur') {
      if (Math.hypot(t.x2 - t.x1, t.z2 - t.z1) < 0.2) return;
      memoriser();
      const m = { cle: nouvelleCle('m'), id: null, x1: t.x1, z1: t.z1, x2: t.x2, z2: t.z2, epaisseur: 0.2, hauteur: 3 };
      etat.murs.push(m);
      selection = { type: 'mur', cle: m.cle };
      toutRedessiner();
      return; // l'outil Mur reste actif pour enchaîner les murs
    }
    if (t.type === 'bloc') {
      if (l < 0.5 || p < 0.5) { l = 8; p = 6; } // simple clic : bloc de 8 × 6 m
      memoriser();
      const b = { cle: nouvelleCle('b'), id: null, code: codeBlocLibre(), nom: '', couleur: PALETTE[etat.blocs.length % PALETTE.length],
        x, z, largeur: arrondi(l), profondeur: arrondi(p) };
      etat.blocs.push(b);
      selection = { type: 'bloc', cle: b.cle };
    } else if (t.type === 'etagere') {
      if (l < 0.2 || p < 0.2) { l = 2; p = 0.9; } // simple clic : étagère standard 2 × 0,9 m
      const b = blocEn(x + l / 2, z + p / 2);
      if (!b) { message('Dessinez l\'étagère à l\'intérieur d\'un bloc.', true); return; }
      memoriser();
      const tournee = p > l; // dessinée « debout » : étagère tournée
      const e = { cle: nouvelleCle('e'), id: null, bloc: b.cle, code: codeEtagereLibre(b.cle), x, z,
        largeur: arrondi(tournee ? p : l), profondeur: arrondi(tournee ? l : p), hauteur: 2.4, niveaux: 4, tournee, articles: 0 };
      etat.etageres.push(e);
      selection = { type: 'etagere', cle: e.cle };
    }
    choisirOutil('selection');
    toutRedessiner();
  }

  // ---------------------------------------------------------------- clavier
  document.addEventListener('keydown', (ev) => {
    if (ev.target.closest('input, select, textarea')) return;
    const touche = ev.key.toLowerCase();
    if ((ev.ctrlKey || ev.metaKey) && touche === 'z') { ev.preventDefault(); annuler(); return; }
    if ((ev.ctrlKey || ev.metaKey) && touche === 's') { ev.preventDefault(); enregistrer(); return; }
    if ((ev.ctrlKey || ev.metaKey) && touche === 'd') { ev.preventDefault(); dupliquer(); return; }
    if (touche === 'delete' || touche === 'backspace') { ev.preventDefault(); supprimer(); return; }
    if (touche === 'escape') { trace = null; selection = null; choisirOutil('selection'); toutRedessiner(); return; }
    const raccourcis = { v: 'selection', m: 'mur', b: 'bloc', e: 'etagere' };
    if (raccourcis[touche] && !ev.ctrlKey) { choisirOutil(raccourcis[touche]); return; }
    if (touche === 'r') { tourner(); return; }
    const fleches = { arrowleft: [-1, 0], arrowright: [1, 0], arrowup: [0, -1], arrowdown: [0, 1] };
    if (fleches[touche] && trouver(selection)) {
      ev.preventDefault();
      memoriser();
      const k = ev.shiftKey ? 1 : pas() === 0.5 ? 0.5 : 0.05;
      deplacer(trouver(selection), selection.type, fleches[touche][0] * k, fleches[touche][1] * k);
      if (selection.type === 'etagere') rattacher(trouver(selection));
      toutRedessiner();
    }
  });

  // ---------------------------------------------------------------- barre d'outils
  function choisirOutil(nom) {
    outil = nom;
    document.querySelectorAll('[data-outil]').forEach((b) => b.classList.toggle('actif', b.dataset.outil === nom));
    svg.dataset.outil = nom;
    if (!trouver(selection)) panneau();
  }
  document.querySelectorAll('[data-outil]').forEach((b) => b.addEventListener('click', () => choisirOutil(b.dataset.outil)));
  $('btn-annuler').onclick = annuler;
  $('btn-zoom-plus').onclick = () => zoomer(1 / 1.3);
  $('btn-zoom-moins').onclick = () => zoomer(1.3);
  $('btn-ajuster').onclick = ajuster;
  $('btn-aimant').onclick = () => { aimant = !aimant; $('btn-aimant').classList.toggle('actif', aimant); };

  function majBoutons() {
    $('btn-annuler').disabled = !historique.length;
    $('btn-enregistrer').disabled = !modifie;
    $('etat-sauvegarde').textContent = modifie ? 'Modifications non enregistrées' : 'Plan enregistré';
    $('etat-sauvegarde').classList.toggle('non-enregistre', modifie);
  }

  function toutRedessiner() {
    dessiner();
    panneau();
    majBoutons();
    if (vue3dActive) majVue3D();
  }

  let minuteurMessage;
  function message(texte, erreur = false) {
    const m = $('message-editeur');
    m.textContent = texte;
    m.className = 'message-editeur visible' + (erreur ? ' erreur' : '');
    clearTimeout(minuteurMessage);
    minuteurMessage = setTimeout(() => { m.className = 'message-editeur'; }, erreur ? 6000 : 3000);
  }

  // ---------------------------------------------------------------- 2D / 3D
  let vue3d = null, vue3dActive = false;
  async function majVue3D() {
    if (!vue3d) {
      const { Magasin3D } = await import(CFG.module3d);
      vue3d = new Magasin3D($('vue3d'));
    }
    vue3d.charger(versPlan3D(), { garderVue: true });
  }
  document.querySelectorAll('[data-onglet]').forEach((b) => b.addEventListener('click', async () => {
    vue3dActive = b.dataset.onglet === '3d';
    document.querySelectorAll('[data-onglet]').forEach((x) => x.classList.toggle('actif', x === b));
    $('zone-2d').hidden = vue3dActive;
    $('zone-3d').hidden = !vue3dActive;
    document.querySelector('.outils-dessin').classList.toggle('desactive', vue3dActive);
    if (vue3dActive) {
      try { await majVue3D(); } catch (e) { console.error(e); message('La vue 3D ne peut pas s\'afficher sur cet appareil.', true); }
    } else {
      ajusterSiVide();
    }
  }));

  // ---------------------------------------------------------------- enregistrement
  async function enregistrer() {
    if (!modifie) return;
    const bouton = $('btn-enregistrer');
    bouton.disabled = true;
    const donnees = {
      blocs: etat.blocs.map(({ cle, id, code, nom, couleur, x, z, largeur, profondeur }) => ({ cle, id, code, nom, couleur, x, z, largeur, profondeur })),
      etageres: etat.etageres.map(({ id, bloc, code, x, z, largeur, profondeur, hauteur, niveaux, tournee }) => ({ id, bloc, code, x, z, largeur, profondeur, hauteur, niveaux, tournee })),
      murs: etat.murs.map(({ id, x1, z1, x2, z2, epaisseur, hauteur }) => ({ id, x1, z1, x2, z2, epaisseur, hauteur })),
    };
    try {
      const rep = await fetch(CFG.apiEnregistrer, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CFG.csrf }, body: JSON.stringify(donnees),
      });
      const json = await rep.json();
      if (!rep.ok) throw new Error((json.erreurs || ['Erreur inconnue']).join('\n'));
      etat = depuisServeur(json); // les nouveaux objets reçoivent leur numéro définitif
      historique = [];
      modifie = false;
      selection = null;
      toutRedessiner();
      message('Plan enregistré. La recherche 3D utilise maintenant ce plan.');
    } catch (e) {
      message(e.message, true);
      majBoutons();
    }
  }
  $('btn-enregistrer').onclick = enregistrer;
  window.addEventListener('beforeunload', (ev) => { if (modifie) { ev.preventDefault(); ev.returnValue = ''; } });

  // ---------------------------------------------------------------- démarrage
  function ajusterSiVide() { if (!svg.dataset.ajuste) { ajuster(); svg.dataset.ajuste = '1'; } }
  etat = depuisServeur(JSON.parse($('plan-initial').textContent));
  new ResizeObserver(() => { if (svg.clientWidth) { vue.h = vue.l * (svg.clientHeight / svg.clientWidth); dessiner(); } }).observe(svg);
  choisirOutil('selection');
  ajuster();
  svg.dataset.ajuste = '1';
  toutRedessiner();
})();
