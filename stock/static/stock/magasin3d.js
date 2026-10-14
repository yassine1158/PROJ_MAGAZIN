// Vue 3D du magasin : blocs au sol, étagères avec niveaux, et repère sur l'emplacement cherché.
// Coordonnées en mètres : x vers la droite, z vers le fond, y vers le haut.
// Pour rester fluide sur un grand magasin : les étagères sont regroupées en lots fusionnés (cartons en
// InstancedMesh), une étagère lointaine n'est plus qu'une boîte texturée, les matériaux sont partagés
// et l'image n'est redessinée que si quelque chose bouge.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const sombre = () => window.matchMedia('(prefers-color-scheme: dark)').matches;
const EPAISSEUR_PLATEAU = 0.04;
const COULEUR_ETAGERE = 0x94a3b8;
const COULEUR_CIBLE = 0xef4444;
const COULEUR_CHOIX = 0x2563eb;
const COULEURS_CARTONS = [0xc8a165, 0xb98d55, 0xd6b47c, 0x9ca3af, 0x60a5fa].map((c) => new THREE.Color(c));
const CHAMP = 45; // champ de vision vertical (degrés)
const DUREE_PULSATION = 8000; // ensuite la cible reste rouge sans clignoter : plus aucun calcul au repos
const OPACITE_ESTOMPEE = 0.16;
const HAUT = new THREE.Vector3(0, 1, 0);

// Grand magasin : au-delà de SEUIL_GRAND triangles, pas d'ombres, et seules les étagères proches sont détaillées.
const SEUIL_GRAND = 40000;
const DETAIL_PX = 60; // un lot est détaillé quand ses étagères font au moins 60 px de haut à l'écran
const BUDGET_DETAIL = 40000; // triangles détaillés au plus en même temps
const IMAGE_LENTE = 55; // ms par image : au-delà, résolution réduite pendant les mouvements

// Boîte unité partagée. Faces dans l'ordre de three : +x, -x, dessus, dessous, +z, -z.
const BOITE = new THREE.BoxGeometry(1, 1, 1);
BOITE.userData.partagee = true;
const TOUTES = [0, 1, 2, 3, 4, 5];
const SANS_DESSOUS = [0, 1, 2, 4, 5];
const COTES = [0, 1, 4, 5];
const CARTON = boiteReduite(SANS_DESSOUS); // un carton posé : on ne voit jamais son dessous

// Étiquettes : hauteur voulue en mètres, bornée à l'écran (en pixels) pour rester lisible de loin
// sans masquer la vue de près.
const STYLE_BLOC = { monde: 1.3, min: 22, max: 40, devant: true, rang: 1, reductible: true };
const STYLE_OBJET = { monde: 0.45, min: 15, max: 26, rang: 2, reductible: true };
const STYLE_CODE = { monde: 0.35, min: 16, max: 26, rang: 3 };
const CODE_LISIBLE = 12; // un code d'étagère s'affiche quand il ferait au moins 12 px à sa taille réelle
const CODE_LISIBLE_BLOC = 10; // … ou 10 px dans le bloc de l'article cherché
const CODES_MAX = 10; // codes d'étagère affichés en même temps, les plus proches d'abord
const FOND_CODE = 'rgba(15,23,42,.8)';

// Étagère vue de loin : un niveau (cartons sur un plateau) répété en largeur et en hauteur.
const TUILE = 1.2; // largeur représentée par la texture, en mètres

function boiteReduite(faces) {
  const g = new THREE.BoxGeometry(1, 1, 1);
  const index = g.index.array;
  g.setIndex(faces.flatMap((f) => Array.from(index.slice(f * 6, f * 6 + 6))));
  g.clearGroups();
  g.userData.partagee = true;
  return g;
}

function dessinerTexte(texte, fond, couleur) {
  const taille = 56;
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  const police = `700 ${taille}px Inter, system-ui, sans-serif`;
  ctx.font = police;
  canvas.width = Math.ceil(ctx.measureText(texte).width + taille * 0.9);
  canvas.height = Math.ceil(taille * 1.5);
  ctx.font = police;
  ctx.fillStyle = fond;
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(0, 0, canvas.width, canvas.height, taille * 0.35);
  else ctx.rect(0, 0, canvas.width, canvas.height);
  ctx.fill();
  ctx.fillStyle = couleur;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(texte, canvas.width / 2, canvas.height / 2 + taille * 0.04);
  return canvas;
}

// Un rayonnage ouvert : fond clair, cartons de couleur, plateau en bas (mêmes couleurs que de près).
function textureRayonnage() {
  const canvas = document.createElement('canvas');
  canvas.width = 256;
  canvas.height = 128;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = sombre() ? '#334155' : '#dbe2ea';
  ctx.fillRect(0, 0, 256, 128);
  const cartons = [[4, 70, 0.7, '#c8a165'], [80, 52, 0.86, '#60a5fa'], [138, 46, 0.55, '#9ca3af'], [190, 62, 0.74, '#d6b47c']];
  for (const [x, l, h, couleur] of cartons) {
    ctx.fillStyle = couleur;
    ctx.fillRect(x, 114 - Math.round(h * 108), l, Math.round(h * 108));
  }
  ctx.fillStyle = sombre() ? '#64748b' : '#f1f5f9';
  ctx.fillRect(0, 114, 256, 14);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
  return texture;
}

// Pseudo-aléatoire stable : les cartons décoratifs restent au même endroit à chaque chargement.
function aleatoire(graine) {
  let s = graine % 2147483647 || 1;
  return () => (s = (s * 16807) % 2147483647) / 2147483647;
}

// Ne dessine que les plages d'indices hors des étagères retirées (dessinées à part, en surbrillance).
function grouper(geometrie, retirees) {
  geometrie.clearGroups();
  let debut = 0;
  for (const p of retirees.sort((a, b) => a.debut - b.debut)) {
    if (p.debut > debut) geometrie.addGroup(debut, p.debut - debut, 0);
    debut = Math.max(debut, p.debut + p.nb);
  }
  if (debut < geometrie.index.count) geometrie.addGroup(debut, geometrie.index.count - debut, 0);
}

// Fusion de boîtes en une seule géométrie : des milliers de boîtes, un seul appel de dessin.
// La matrice ne contient qu'un déplacement et une rotation : les normales tournent avec elle.
class Fusion {
  constructor({ couleurs = false, uv = false } = {}) {
    this.positions = [];
    this.normales = [];
    this.couleurs = couleurs ? [] : null;
    this.uv = uv ? [] : null;
    this.indices = [];
  }

  boite(matrice, x, y, z, l, h, p, { couleur, faces = TOUTES, uv } = {}) {
    const m = matrice.elements;
    const pos = BOITE.attributes.position.array, nor = BOITE.attributes.normal.array;
    const uvs = BOITE.attributes.uv.array, index = BOITE.index.array;
    for (const f of faces) {
      const base = this.positions.length / 3;
      for (let k = f * 4; k < f * 4 + 4; k++) {
        const i = k * 3;
        const vx = x + pos[i] * l, vy = y + pos[i + 1] * h, vz = z + pos[i + 2] * p;
        this.positions.push(m[0] * vx + m[4] * vy + m[8] * vz + m[12], m[1] * vx + m[5] * vy + m[9] * vz + m[13],
          m[2] * vx + m[6] * vy + m[10] * vz + m[14]);
        const nx = nor[i], ny = nor[i + 1], nz = nor[i + 2];
        this.normales.push(m[0] * nx + m[4] * ny + m[8] * nz, m[1] * nx + m[5] * ny + m[9] * nz, m[2] * nx + m[6] * ny + m[10] * nz);
        if (this.couleurs) this.couleurs.push(couleur.r, couleur.g, couleur.b);
        if (this.uv) this.uv.push(...uv(f, uvs[k * 2], uvs[k * 2 + 1]));
      }
      for (let j = f * 6; j < f * 6 + 6; j++) this.indices.push(base + index[j] - f * 4);
    }
  }

  // Plage d'indices remplie par `remplir` : permet ensuite de ne pas dessiner une étagère.
  plage(remplir) {
    const debut = this.indices.length;
    remplir();
    return { debut, nb: this.indices.length - debut };
  }

  geometrie() {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(this.positions, 3));
    g.setAttribute('normal', new THREE.Float32BufferAttribute(this.normales, 3));
    if (this.couleurs) g.setAttribute('color', new THREE.Float32BufferAttribute(this.couleurs, 3));
    if (this.uv) g.setAttribute('uv', new THREE.Float32BufferAttribute(this.uv, 2));
    g.setIndex(this.indices);
    g.computeBoundingSphere();
    return g;
  }
}

const MURAUX = { porte: 0, porte_entree: 0, portail: 0, fenetre: 1.0 }; // type → hauteur du bas de l'ouverture

// Ouvertures d'un mur : portes et fenêtres posées dessus (le long du mur, à moins de 40 cm de son axe).
export function ouvertures(m, elements) {
  const dx = m.x2 - m.x1, dz = m.z2 - m.z1, longueur = Math.hypot(dx, dz);
  if (longueur < 0.01) return [];
  const ux = dx / longueur, uz = dz / longueur;
  const liste = [];
  for (const e of elements) {
    if (!(e.type in MURAUX)) continue;
    const a = (e.rotation || 0) * Math.PI / 180;
    if (Math.abs(Math.cos(a) * uz - Math.sin(a) * ux) > 0.3) continue; // pas dans le sens du mur
    const t = (e.x - m.x1) * ux + (e.z - m.z1) * uz;
    const distance = Math.abs((e.x - m.x1) * uz - (e.z - m.z1) * ux);
    if (distance > (m.epaisseur || 0.2) / 2 + 0.4 || t < -e.largeur / 2 || t > longueur + e.largeur / 2) continue;
    const bas = MURAUX[e.type];
    liste.push({ t0: Math.max(0, t - e.largeur / 2), t1: Math.min(longueur, t + e.largeur / 2), bas, haut: bas + (e.hauteur || 2) });
  }
  return liste.sort((p, q) => p.t0 - q.t0);
}

// options.masques() : éléments de la page posés sur la 3D (fiche, boutons) que le cadrage doit éviter.
// options.lienEtagere(etagere) : adresse « Voir les articles » de la fiche d'une étagère touchée.
export class Magasin3D {
  constructor(conteneur, options = {}) {
    this.conteneur = conteneur;
    this.options = options;
    this.etageres = new Map(); // id -> étagère (voir _decrireEtagere)
    this.liste = [];
    this.lots = [];
    this.etiquettes = [];
    this.textures = new Map();
    this.animationCamera = null;
    this.marges = { haut: 0, bas: 0 };
    this.largeur = 1;
    this.hauteur = 1;
    this.ratio = Math.min(window.devicePixelRatio || 1, 2);
    this.reduit = false; // image en cours calculée en résolution réduite
    this.lent = false; // appareil lent : résolution réduite pendant les mouvements
    this.durees = [];
    this.grand = false;
    this.ombres = true;
    this._image = this._image.bind(this);
    this._v = new THREE.Vector3();
    this._v2 = new THREE.Vector3();
    this._avant = new THREE.Vector3();
    this._frustum = new THREE.Frustum();
    this._m = new THREE.Matrix4();

    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(this.ratio);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.shadowMap.autoUpdate = false; // les ombres ne changent qu'avec le plan ou la cible
    conteneur.prepend(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(CHAMP, 1, 0.1, 500);
    this.controles = new OrbitControls(this.camera, this.renderer.domElement);
    this.controles.enableDamping = true;
    this.controles.dampingFactor = 0.1; // inertie courte : la vue s'arrête vite, plus de calcul ensuite
    this.controles.maxPolarAngle = Math.PI / 2.05;
    this.controles.addEventListener('start', () => { this.animationCamera = null; });
    this.controles.addEventListener('change', () => { if (!this._dansImage) this.demanderRendu(); });

    this.scene.add(new THREE.HemisphereLight(0xffffff, sombre() ? 0x1e293b : 0xcbd5e1, sombre() ? 1.4 : 1.8));
    this.soleil = new THREE.DirectionalLight(0xffffff, sombre() ? 1.2 : 1.6);
    this.soleil.castShadow = true;
    this.soleil.shadow.mapSize.set(2048, 2048);
    this.scene.add(this.soleil, this.soleil.target);

    this.materiaux = this._creerMateriaux();
    this.matsObjets = new Map();
    this.repere = this._creerRepere();
    this.scene.add(this.repere);
    this.contenu = new THREE.Group(); // tout ce qui vient du plan (vidé à chaque nouveau chargement)
    this.scene.add(this.contenu);
    this.bulle = this._creerBulle();
    this._ecouterToucher();

    new ResizeObserver(() => this._redimensionner()).observe(conteneur);
    this._redimensionner();
  }

  // ------------------------------------------------------------------ rendu à la demande
  demanderRendu() {
    if (this._prochaineImage == null) this._prochaineImage = requestAnimationFrame(this._image);
  }

  _image() {
    this._prochaineImage = null;
    const maintenant = performance.now();
    if (this._suite && !this.reduit && this._derniere) this._mesurer(maintenant - this._derniere);
    let mouvement = false;
    const a = this.animationCamera;
    if (a) {
      const k = Math.min(1, (maintenant - a.debut) / a.duree);
      const lisse = k < 0.5 ? 4 * k ** 3 : 1 - (-2 * k + 2) ** 3 / 2;
      this.camera.position.lerpVectors(a.dePos, a.aPos, lisse);
      this.controles.target.lerpVectors(a.deCible, a.aCible, lisse);
      this._appliquerMarges(a.deMarges.haut + (a.aMarges.haut - a.deMarges.haut) * lisse,
        a.deMarges.bas + (a.aMarges.bas - a.deMarges.bas) * lisse);
      if (k === 1) this.animationCamera = null;
      else mouvement = true;
    }
    this._dansImage = true;
    if (this.controles.update() && this._bouge()) mouvement = true; // geste en cours ou inertie
    this._dansImage = false;
    const pulse = Boolean(this.cible) && maintenant < this.finPulsation;
    if (this.cible) {
      const p = pulse ? (Math.sin(maintenant / 200) + 1) / 2 : 0.5;
      this.materiaux.niveauCible.emissive.setRGB(0.5 + p * 0.5, 0.05, 0.05);
    }
    // Appareil lent : pendant un mouvement, image en basse résolution ; la dernière image est nette.
    this._resolution(mouvement && this.lent);
    this.camera.updateMatrixWorld();
    this._majLots();
    this._majRepere(maintenant, pulse);
    this._majEtiquettes();
    this.renderer.render(this.scene, this.camera);
    this._placerBulle();
    this._suite = mouvement || (pulse && !this.lent);
    this._derniere = maintenant;
    if (this._suite) this.demanderRendu();
    else if (pulse) this._minuteur ||= setTimeout(() => { this._minuteur = null; this.demanderRendu(); }, 120); // appareil lent : clignotement moins fréquent
  }

  // L'inertie s'éteint lentement : en dessous d'un tiers de pixel par image, on arrête de redessiner.
  _bouge() {
    const p = this.camera.position, c = this.controles.target;
    const seuil = p.distanceTo(c) * 4e-4;
    const bouge = !this._dernierePos || p.distanceTo(this._dernierePos) > seuil || c.distanceTo(this._derniereCible) > seuil;
    (this._dernierePos ||= new THREE.Vector3()).copy(p);
    (this._derniereCible ||= new THREE.Vector3()).copy(c);
    return bouge;
  }

  _mesurer(duree) {
    if (duree > 600) return; // onglet caché, page occupée…
    this.durees.push(duree);
    if (this.durees.length > 6) this.durees.shift();
    if (this.durees.length >= 4 && this.durees.reduce((s, d) => s + d, 0) / this.durees.length > IMAGE_LENTE) this.lent = true;
  }

  _resolution(reduit) {
    if (reduit === this.reduit) return;
    this.reduit = reduit;
    this.renderer.setPixelRatio(reduit ? Math.max(0.5, this.ratio / 2) : this.ratio);
    this.renderer.setSize(this.largeur, this.hauteur, false);
  }

  _redimensionner() {
    const { clientWidth: l, clientHeight: h } = this.conteneur;
    if (!l || !h) return;
    this.largeur = l;
    this.hauteur = h;
    this.echelleTitres = Math.min(1, Math.max(0.75, Math.min(l, h) / 600)); // titres un peu plus petits sur téléphone
    this.renderer.setSize(l, h, false);
    this._appliquerMarges(Math.min(this.marges.haut, h * 0.4), Math.min(this.marges.bas, h * 0.4));
    // Redessin immédiat : sinon le canvas reste vide pendant une image.
    if (this._prochaineImage != null) cancelAnimationFrame(this._prochaineImage);
    this._image();
  }

  // Le centre de la vue est placé au milieu de la partie libre de l'écran (sous la fiche, au-dessus
  // des boutons) sans changer l'échelle de l'image.
  _appliquerMarges(haut, bas) {
    const W = this.largeur, H = this.hauteur;
    this.marges = { haut, bas };
    const cy = haut + (H - haut - bas) / 2;
    const tan = Math.tan(THREE.MathUtils.degToRad(CHAMP / 2));
    if (Math.abs(cy - H / 2) < 0.5) {
      this.camera.fov = CHAMP;
      this.camera.aspect = W / H;
      this.camera.clearViewOffset();
    } else {
      const Hp = 2 * Math.max(cy, H - cy);
      this.camera.fov = THREE.MathUtils.radToDeg(2 * Math.atan(tan * Hp / H));
      this.camera.aspect = W / Hp;
      this.camera.setViewOffset(W, Hp, 0, Hp / 2 - cy, W, H);
    }
    this.camera.updateProjectionMatrix();
    this.pixelsParMetre = H / (2 * tan); // taille à l'écran d'un mètre vu à 1 m
  }

  // Une fiche ou une barre de boutons assez large posée en haut ou en bas de la 3D réduit la zone utile.
  _margesPage() {
    const r = this.conteneur.getBoundingClientRect();
    const m = { haut: 0, bas: 0 };
    for (const el of this.options.masques?.() || []) {
      if (!el || el.offsetParent === null) continue;
      const b = el.getBoundingClientRect();
      if (b.width < r.width * 0.35 || !b.height || b.bottom <= r.top || b.top >= r.bottom) continue;
      if ((b.top + b.bottom) / 2 < r.top + r.height / 2) m.haut = Math.max(m.haut, b.bottom - r.top + 8);
      else m.bas = Math.max(m.bas, r.bottom - b.top + 8);
    }
    m.haut = Math.min(m.haut, r.height * 0.4);
    m.bas = Math.min(m.bas, r.height * 0.4);
    return m;
  }

  // ------------------------------------------------------------------ chargement du plan
  _creerMateriaux() {
    const std = (o) => new THREE.MeshStandardMaterial(o);
    const estompe = (m) => Object.assign(m.clone(), { transparent: true, opacity: OPACITE_ESTOMPEE, depthWrite: false });
    const M = {
      structure: std({ color: COULEUR_ETAGERE, metalness: 0.6, roughness: 0.4 }),
      plateau: std({ color: sombre() ? 0x475569 : 0xcbd5e1, roughness: 0.7 }),
      carton: std({ color: 0xffffff, roughness: 0.9 }),
      lointain: std({ map: textureRayonnage(), roughness: 0.85 }),
      structureCible: std({ color: COULEUR_CIBLE, emissive: 0x7f1d1d, metalness: 0.6, roughness: 0.4 }),
      structureChoix: std({ color: COULEUR_CHOIX, emissive: 0x1e3a8a, metalness: 0.5, roughness: 0.4 }),
      niveauCible: std({ color: COULEUR_CIBLE, emissive: 0x991b1b, roughness: 0.7 }),
      murs: std({ vertexColors: true, roughness: 0.9 }),
      zones: std({ vertexColors: true, transparent: true, opacity: 0.22 }),
      sol: std({ color: sombre() ? 0x1e293b : 0xe2e8f0, roughness: 0.95 }),
      contours: new THREE.LineBasicMaterial({ vertexColors: true }),
    };
    M.structureEstompee = estompe(M.structure);
    M.plateauEstompe = estompe(M.plateau);
    M.lointainEstompe = estompe(M.lointain);
    for (const m of Object.values(M)) m.userData.partage = true;
    return M;
  }

  _vider() {
    this._fermerBulle();
    this._enleverCible();
    this.contenu.traverse((o) => {
      if (o.isInstancedMesh) o.dispose();
      if (o.geometry && !o.geometry.userData.partagee) o.geometry.dispose();
      for (const m of [].concat(o.material || [])) if (!m.userData.partage) m.dispose();
    });
    for (const { texture, materiaux } of this.textures.values()) {
      texture.dispose();
      for (const m of materiaux.values()) m.dispose();
    }
    this.textures = new Map();
    this.scene.remove(this.contenu);
    this.contenu = new THREE.Group();
    this.scene.add(this.contenu);
    this.etageres = new Map();
    this.liste = [];
    this.lots = [];
    this.etiquettes = [];
    this.estompe = false;
    this.grand = false;
    this.ombres = true;
    this.limites = null;
    this.repere.visible = false;
    this.demanderRendu();
  }

  // garderVue : redessine sans bouger la caméra (aperçu en direct de l'éditeur de plan).
  charger(plan, { garderVue = false } = {}) {
    const dejaCharge = Boolean(this.limites);
    this._vider();
    const blocs = plan.blocs || [];
    const murs = plan.murs || [];
    const elements = plan.elements || [];
    if (!blocs.length && !murs.length && !elements.length) return false;
    let minX = Infinity, minZ = Infinity, maxX = -Infinity, maxZ = -Infinity, maxH = 2;
    for (const b of blocs) {
      minX = Math.min(minX, b.x); minZ = Math.min(minZ, b.z);
      maxX = Math.max(maxX, b.x + b.largeur); maxZ = Math.max(maxZ, b.z + b.profondeur);
      for (const e of b.etageres) maxH = Math.max(maxH, e.hauteur);
    }
    for (const m of murs) {
      minX = Math.min(minX, m.x1, m.x2); maxX = Math.max(maxX, m.x1, m.x2);
      minZ = Math.min(minZ, m.z1, m.z2); maxZ = Math.max(maxZ, m.z1, m.z2);
      maxH = Math.max(maxH, m.hauteur || 3);
    }
    for (const e of elements) {
      const r = Math.max(e.largeur, e.profondeur) / 2;
      minX = Math.min(minX, e.x - r); maxX = Math.max(maxX, e.x + r);
      minZ = Math.min(minZ, e.z - r); maxZ = Math.max(maxZ, e.z + r);
    }
    this.limites = { minX, minZ, maxX, maxZ, maxH };
    this.centre = new THREE.Vector3((minX + maxX) / 2, 0, (minZ + maxZ) / 2);
    this.taille = Math.max(maxX - minX, maxZ - minZ, 10);
    this.camera.far = Math.max(500, this.taille * 8);
    this.controles.maxDistance = this.taille * 4 + 30;

    // Sol
    const marge = 4;
    const sol = new THREE.Mesh(new THREE.PlaneGeometry(maxX - minX + marge * 2, maxZ - minZ + marge * 2), this.materiaux.sol);
    sol.rotation.x = -Math.PI / 2;
    sol.position.copy(this.centre);
    sol.receiveShadow = true;
    this.contenu.add(sol);
    const grille = new THREE.GridHelper(this.taille + marge * 2, Math.round(this.taille + marge * 2),
      sombre() ? 0x334155 : 0xcbd5e1, sombre() ? 0x253145 : 0xd9e0ea);
    grille.position.set(this.centre.x, 0.002, this.centre.z);
    this.contenu.add(grille);

    const s = this.soleil;
    s.position.set(this.centre.x + this.taille * 0.4, this.taille, this.centre.z + this.taille * 0.6);
    s.target.position.copy(this.centre);
    s.target.updateMatrixWorld();
    const cam = s.shadow.camera;
    cam.left = cam.bottom = -this.taille;
    cam.right = cam.top = this.taille;
    cam.far = this.taille * 4;
    cam.updateProjectionMatrix();

    this._ajouterBlocs(blocs, maxH);
    this._ajouterEtageres();
    this._ajouterMurs(murs, elements);
    for (const e of elements) this._ajouterElement(e);
    s.castShadow = this.ombres;
    this.renderer.shadowMap.needsUpdate = true;
    if (!garderVue || !dejaCharge) this.vueEnsemble(false);
    this.demanderRendu();
    return true;
  }

  // Une étiquette : texture partagée par toutes les étiquettes de même texte.
  _etiquette(texte, fond, style, { couleur = '#fff', position } = {}) {
    const cle = `${texte}\u0000${fond}\u0000${couleur}`;
    let t = this.textures.get(cle);
    if (!t) {
      const canvas = dessinerTexte(texte, fond, couleur);
      const texture = new THREE.CanvasTexture(canvas);
      texture.colorSpace = THREE.SRGBColorSpace;
      t = { texture, ratio: canvas.width / canvas.height, materiaux: new Map() };
      this.textures.set(cle, t);
    }
    const devant = Boolean(style.devant);
    if (!t.materiaux.has(devant)) {
      t.materiaux.set(devant, new THREE.SpriteMaterial({ map: t.texture, depthTest: !devant, depthWrite: false }));
    }
    const sprite = new THREE.Sprite(t.materiaux.get(devant));
    sprite.renderOrder = devant ? 12 : 10;
    sprite.userData = { ...style, ratio: t.ratio };
    if (position) sprite.position.copy(position);
    return sprite;
  }

  _ajouterBlocs(blocs, maxH) {
    const zones = new Fusion({ couleurs: true });
    const contours = [], couleursContours = [];
    const identite = new THREE.Matrix4();
    for (const b of blocs) {
      const couleur = new THREE.Color(b.couleur || '#3b82f6');
      const cx = b.x + b.largeur / 2, cz = b.z + b.profondeur / 2;
      zones.boite(identite, cx, 0.01, cz, b.largeur, 0.001, b.profondeur, { couleur, faces: [2] });
      const coins = [[b.x, b.z], [b.x + b.largeur, b.z], [b.x + b.largeur, b.z + b.profondeur], [b.x, b.z + b.profondeur]];
      coins.forEach(([x, z], i) => {
        const [x2, z2] = coins[(i + 1) % 4];
        contours.push(x, 0.02, z, x2, 0.02, z2);
        couleursContours.push(couleur.r, couleur.g, couleur.b, couleur.r, couleur.g, couleur.b);
      });
      const titre = this._etiquette(`BLOC ${b.code}`, b.couleur || '#3b82f6', STYLE_BLOC,
        { position: new THREE.Vector3(cx, maxH + 1.2, cz) });
      titre.userData.bloc = b;
      this.contenu.add(titre);
      this.etiquettes.push(titre);
      for (const e of b.etageres) this.liste.push(this._decrireEtagere(b, e, this.liste.length));
    }
    if (!blocs.length) return;
    const sol = new THREE.Mesh(zones.geometrie(), this.materiaux.zones);
    sol.receiveShadow = true;
    this.contenu.add(sol);
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(contours, 3));
    g.setAttribute('color', new THREE.Float32BufferAttribute(couleursContours, 3));
    this.contenu.add(new THREE.LineSegments(g, this.materiaux.contours));
  }

  // Étagères regroupées en lots : chaque lot tient en trois géométries (structures, plateaux, cartons).
  // Grand magasin : lots par carré de sol, dessinés en détail seulement de près (voir _majLots).
  _ajouterEtageres() {
    if (!this.liste.length) return;
    this.grand = this.liste.reduce((s, e) => s + e.triangles, 0) > SEUIL_GRAND;
    this.ombres = !this.grand;
    this.lots = this._regrouper();
    for (const lot of this.lots) {
      if (this.grand) this._lointain(lot);
      else this._detailler(lot);
    }
    for (const e of this.liste) {
      if (e.id != null) this.etageres.set(e.id, e);
      const code = this._etiquette(e.code, FOND_CODE, STYLE_CODE,
        { position: new THREE.Vector3(e.centre.x, e.H + 0.3, e.centre.z) });
      code.userData.etagere = e;
      this.contenu.add(code);
      this.etiquettes.push(code);
    }
  }

  _regrouper() {
    if (!this.grand) return [this._lot(this.liste)];
    let minX = Infinity, minZ = Infinity, maxX = -Infinity, maxZ = -Infinity;
    for (const { centre: c } of this.liste) {
      minX = Math.min(minX, c.x); maxX = Math.max(maxX, c.x);
      minZ = Math.min(minZ, c.z); maxZ = Math.max(maxZ, c.z);
    }
    const cote = Math.max(8, Math.sqrt(((maxX - minX) * (maxZ - minZ)) / 40)); // une quarantaine de lots
    const cases = new Map();
    for (const e of this.liste) {
      const cle = `${Math.floor((e.centre.x - minX) / cote)},${Math.floor((e.centre.z - minZ) / cote)}`;
      if (!cases.has(cle)) cases.set(cle, []);
      cases.get(cle).push(e);
    }
    return [...cases.values()].map((etageres) => this._lot(etageres));
  }

  _lot(etageres) {
    const boite = new THREE.Box3();
    const lot = { etageres, triangles: 0, hauteur: 0, masque: null, detail: null, lointain: null };
    for (const e of etageres) {
      boite.union(e.boite);
      lot.triangles += e.triangles;
      lot.hauteur = Math.max(lot.hauteur, e.H);
      e.lot = lot;
    }
    lot.sphere = boite.getBoundingSphere(new THREE.Sphere());
    return lot;
  }

  _maillage(fusion, materiau, parent) {
    const m = new THREE.Mesh(fusion.geometrie(), [materiau]);
    m.castShadow = !this.estompe;
    m.receiveShadow = true;
    parent.add(m);
    return m;
  }

  // Lot en détail (construit à la première utilisation sur un grand magasin).
  _detailler(lot) {
    const structures = new Fusion(), plateaux = new Fusion();
    let nbCartons = 0;
    for (const e of lot.etageres) {
      e.s = structures.plage(() => {
        for (const [x, y, z, l, h, p] of e.poteaux) structures.boite(e.matrice, x, y, z, l, h, p, { faces: COTES });
      });
      e.p = plateaux.plage(() => {
        for (const y of e.hauteurs) plateaux.boite(e.matrice, 0, y, 0, e.L, EPAISSEUR_PLATEAU, e.P);
      });
      e.c = { debut: nbCartons, nb: e.boitesCartons.length };
      nbCartons += e.c.nb;
    }
    const M = this.materiaux, o = this.estompe;
    const groupe = new THREE.Group();
    lot.structures = this._maillage(structures, o ? M.structureEstompee : M.structure, groupe);
    lot.plateaux = this._maillage(plateaux, o ? M.plateauEstompe : M.plateau, groupe);
    if (nbCartons) {
      lot.cartons = this._cartons(lot.etageres, nbCartons);
      lot.cartons.visible = !o;
      lot.cartonsOrigine = lot.cartons.instanceMatrix.array.slice();
      groupe.add(lot.cartons);
    }
    lot.detail = groupe;
    lot.masque = null;
    this._masquer(lot);
    this.contenu.add(groupe);
  }

  // Lot vu de loin : une boîte texturée par étagère.
  _lointain(lot) {
    const fusion = new Fusion({ uv: true });
    for (const e of lot.etageres) {
      const n = e.hauteurs.length;
      // Dessus : les cartons du dernier niveau vus d'en haut ; côtés : un motif par niveau.
      const uv = (face, u, v) => (face === 2 ? [(u * e.L) / TUILE, 0.12 + v * 0.4] : [(u * (face < 2 ? e.P : e.L)) / TUILE, v * n]);
      e.l = fusion.plage(() => fusion.boite(e.matrice, 0, e.H / 2, 0, e.L, e.H, e.P, { faces: SANS_DESSOUS, uv }));
    }
    lot.lointain = new THREE.Mesh(fusion.geometrie(), [this.estompe ? this.materiaux.lointainEstompe : this.materiaux.lointain]);
    this._masquer(lot);
    this.contenu.add(lot.lointain);
  }

  _cartons(etageres, nb) {
    const cartons = new THREE.InstancedMesh(CARTON, this.materiaux.carton, nb);
    const m = new THREE.Matrix4();
    let i = 0;
    for (const e of etageres) {
      for (const [x, y, z, l, h, p, couleur] of e.boitesCartons) {
        m.makeScale(l, h, p).setPosition(x, y, z).premultiply(e.matrice);
        cartons.setMatrixAt(i, m);
        cartons.setColorAt(i, COULEURS_CARTONS[couleur]);
        i++;
      }
    }
    cartons.castShadow = cartons.receiveShadow = true;
    cartons.computeBoundingSphere();
    return cartons;
  }

  _decrireEtagere(b, e, n) {
    const L = e.largeur, P = e.profondeur, H = e.hauteur;
    const centre = new THREE.Vector3(b.x + e.x + (e.tournee ? P : L) / 2, 0, b.z + e.z + (e.tournee ? L : P) / 2);
    const matrice = new THREE.Matrix4().makeRotationY(e.tournee ? Math.PI / 2 : 0).setPosition(centre);
    const poteaux = [[-1, -1], [1, -1], [-1, 1], [1, 1]]
      .map(([px, pz]) => [px * (L / 2 - 0.03), H / 2, pz * (P / 2 - 0.03), 0.06, H, 0.06]);
    const niveaux = Math.max(1, e.niveaux);
    const pas = H / niveaux;
    const hauteurs = [];
    const boitesCartons = [];
    const hasard = aleatoire(e.id * 7919);
    for (let i = 0; i < niveaux; i++) {
      const y = i === 0 ? 0.12 : i * pas;
      hauteurs.push(y);
      // Quelques cartons pour donner vie à l'étagère.
      let x = -L / 2 + 0.1;
      while (x < L / 2 - 0.4) {
        const w = 0.3 + hasard() * 0.4;
        const h = Math.min(pas * (0.4 + hasard() * 0.45), pas - 0.1);
        if (x + w > L / 2 - 0.1) break;
        if (hasard() > 0.2) {
          const p = P * (0.6 + hasard() * 0.3);
          const couleur = Math.floor(hasard() * COULEURS_CARTONS.length);
          if (h > 0.02) boitesCartons.push([x + w / 2, y + EPAISSEUR_PLATEAU / 2 + h / 2, 0, w * 0.92, h, p, couleur]);
        }
        x += w + 0.05;
      }
    }
    const lx = (e.tournee ? P : L) / 2, lz = (e.tournee ? L : P) / 2;
    const boite = new THREE.Box3(new THREE.Vector3(centre.x - lx, 0, centre.z - lz), new THREE.Vector3(centre.x + lx, H, centre.z + lz));
    return { n, id: e.id, code: e.code, donnees: e, bloc: b, matrice, centre, poteaux, hauteurs, boitesCartons, boite, H, L, P,
      quaternion: new THREE.Quaternion().setFromRotationMatrix(matrice),
      triangles: 4 * 8 + niveaux * 12 + boitesCartons.length * 10 };
  }

  // Un mur est découpé en morceaux pour laisser les ouvertures (portes, fenêtres). Tous les murs : une géométrie.
  _ajouterMurs(murs, elements) {
    if (!murs.length) return;
    const fusion = new Fusion({ couleurs: true });
    const matrice = new THREE.Matrix4();
    for (const m of murs) {
      const dx = m.x2 - m.x1, dz = m.z2 - m.z1;
      const longueur = Math.hypot(dx, dz);
      if (longueur < 0.01) continue;
      const H = m.hauteur || 3, ep = m.epaisseur || 0.2;
      const couleur = new THREE.Color(m.couleur || (sombre() ? 0x64748b : 0xd6d3d1));
      matrice.makeRotationY(-Math.atan2(dz, dx)).setPosition(m.x1, 0, m.z1);
      const morceau = (t0, t1, y0, y1) => { // de t0 à t1 le long du mur, de y0 à y1 en hauteur
        if (t1 - t0 < 0.01 || y1 - y0 < 0.01) return;
        const bout0 = t0 <= 0.001 ? ep / 2 : 0, bout1 = t1 >= longueur - 0.001 ? ep / 2 : 0; // angles bien fermés
        fusion.boite(matrice, (t0 - bout0 + t1 + bout1) / 2, (y0 + y1) / 2, 0, t1 - t0 + bout0 + bout1, y1 - y0, ep, { couleur });
      };
      let t = 0;
      for (const o of ouvertures(m, elements)) {
        morceau(t, o.t0, 0, H);
        morceau(o.t0, o.t1, 0, Math.min(o.bas, H));       // allège sous une fenêtre
        morceau(o.t0, o.t1, Math.min(o.haut, H), H);       // linteau au-dessus
        t = Math.max(t, o.t1);
      }
      morceau(t, longueur, 0, H);
    }
    if (!fusion.indices.length) return;
    const mesh = new THREE.Mesh(fusion.geometrie(), this.materiaux.murs);
    mesh.castShadow = mesh.receiveShadow = true;
    this.contenu.add(mesh);
  }

  _ajouterElement(e) {
    const g = new THREE.Group();
    g.position.set(e.x, 0, e.z);
    g.rotation.y = -(e.rotation || 0) * Math.PI / 180;
    const L = e.largeur, P = e.profondeur, H = e.hauteur || 0;
    const mat = (couleur, extra = {}) => { // un matériau par couleur et par aspect, gardé d'un chargement à l'autre
      const cle = JSON.stringify([couleur, extra]);
      if (!this.matsObjets.has(cle)) {
        const m = new THREE.MeshStandardMaterial({ color: couleur, roughness: 0.8, ...extra });
        m.userData.partage = true;
        this.matsObjets.set(cle, m);
      }
      return this.matsObjets.get(cle);
    };
    const boite = (l, h, p, y, matiere, x = 0, z = 0) => {
      const b = new THREE.Mesh(BOITE, matiere);
      b.scale.set(l, h, p);
      b.position.set(x, y, z); b.castShadow = b.receiveShadow = true; g.add(b); return b;
    };
    const sol = (couleur, opacite) => {
      const z = new THREE.Mesh(new THREE.PlaneGeometry(L, P), mat(couleur, { transparent: true, opacity: opacite }));
      z.rotation.x = -Math.PI / 2; z.position.y = 0.015; z.receiveShadow = true; g.add(z);
    };
    const titre = (texte, y, couleur = '#16202a') => {
      if (!texte) return;
      const t = this._etiquette(texte, couleur, STYLE_OBJET, { position: new THREE.Vector3(e.x, y, e.z) });
      this.contenu.add(t);
      this.etiquettes.push(t);
    };
    switch (e.type) {
      case 'porte':
        boite(L - 0.06, H, 0.05, H / 2, mat(e.couleur)); break;
      case 'porte_entree':
        boite(L / 2 - 0.04, H, 0.05, H / 2, mat(e.couleur, { transparent: true, opacity: 0.55 }), -L / 4);
        boite(L / 2 - 0.04, H, 0.05, H / 2, mat(e.couleur, { transparent: true, opacity: 0.55 }), L / 4);
        titre(e.nom || 'ENTRÉE', H + 0.6, e.couleur); break;
      case 'portail':
        boite(L, H, 0.06, H / 2, mat(e.couleur, { metalness: 0.5, transparent: true, opacity: 0.85 }));
        titre(e.nom, H + 0.6, e.couleur); break;
      case 'fenetre':
        boite(L, H, 0.04, 1 + H / 2, mat(e.couleur, { transparent: true, opacity: 0.35, metalness: 0.3 })); break;
      case 'bureau':
      case 'sanitaires': {
        sol(e.couleur, 0.35);
        const verre = mat(e.couleur, { transparent: true, opacity: e.type === 'bureau' ? 0.28 : 0.6 });
        boite(L, H, 0.08, H / 2, verre, 0, -P / 2); boite(L, H, 0.08, H / 2, verre, 0, P / 2);
        boite(0.08, H, P, H / 2, verre, -L / 2); boite(0.08, H, P, H / 2, verre, L / 2);
        if (e.type === 'bureau' && L > 1.6 && P > 1.2) { // table et chaise
          boite(Math.min(1.6, L * 0.5), 0.05, 0.8, 0.75, mat('#8b5e34'), 0, -P / 4);
          boite(0.45, 0.45, 0.45, 0.25, mat('#334155'), 0, -P / 4 + 0.75);
        }
        titre(e.nom || (e.type === 'bureau' ? 'Bureau' : 'WC'), H + 0.5, e.couleur); break;
      }
      case 'poteau':
        boite(L, H, P, H / 2, mat(e.couleur)); break;
      case 'quai':
        boite(L, H, P, H / 2, mat(e.couleur)); titre(e.nom, H + 0.6, e.couleur); break;
      case 'zone':
        sol(e.couleur, 0.35); titre(e.nom, 0.6, e.couleur); break;
      case 'escalier': {
        const n = Math.max(3, Math.round(H / 0.18));
        for (let i = 0; i < n; i++) {
          const h = (H * (i + 1)) / n;
          boite(L, h, P / n, h / 2, mat(e.couleur), 0, -P / 2 + (P / n) * (i + 0.5));
        }
        break;
      }
      case 'extincteur': {
        const c = new THREE.Mesh(new THREE.CylinderGeometry(Math.min(L, P) / 3, Math.min(L, P) / 3, H, 16), mat(e.couleur));
        c.position.y = H / 2; c.castShadow = true; g.add(c); break;
      }
      case 'texte':
        titre(e.nom || 'Texte', 2.2, e.couleur); break;
      default:
        boite(L, Math.max(H, 0.1), P, Math.max(H, 0.1) / 2, mat(e.couleur));
    }
    this.contenu.add(g);
  }

  // Grand magasin : un lot est détaillé s'il est dans le champ et assez près, dans la limite d'un budget
  // de triangles (les plus grands à l'écran d'abord) ; sinon ses étagères sont des boîtes texturées.
  _majLots() {
    if (!this.grand) return;
    const cam = this.camera;
    this._frustum.setFromProjectionMatrix(this._m.multiplyMatrices(cam.projectionMatrix, cam.matrixWorldInverse));
    const proches = [];
    for (const lot of this.lots) {
      lot.voulu = false;
      const d = Math.max(0.5, cam.position.distanceTo(lot.sphere.center) - lot.sphere.radius);
      lot.px = (lot.hauteur * this.pixelsParMetre) / d;
      if (lot.px >= DETAIL_PX && this._frustum.intersectsSphere(lot.sphere)) proches.push(lot);
    }
    proches.sort((a, b) => b.px - a.px);
    let budget = BUDGET_DETAIL;
    for (const lot of proches) {
      if (lot.triangles > budget && budget < BUDGET_DETAIL) continue;
      lot.voulu = true;
      budget -= lot.triangles;
    }
    for (const lot of this.lots) {
      if (lot.voulu && !lot.detail) this._detailler(lot);
      if (lot.detail) lot.detail.visible = lot.voulu;
      lot.lointain.visible = !lot.voulu;
    }
  }

  // ------------------------------------------------------------------ surbrillance (cible et étagère touchée)
  // L'étagère mise en avant n'est plus dessinée dans son lot : elle est redessinée seule, par-dessus.
  _masquer(lot) {
    const retirees = lot.etageres.filter((e) => e === this.cible?.e || e === this.choix?.e);
    const cle = retirees.map((e) => e.n).join(',');
    if (cle === lot.masque) return false;
    lot.masque = cle;
    for (const [maillage, plage] of [[lot.structures, 's'], [lot.plateaux, 'p'], [lot.lointain, 'l']]) {
      if (maillage) grouper(maillage.geometry, retirees.map((e) => e[plage]));
    }
    if (lot.cartons) {
      const t = lot.cartons.instanceMatrix.array;
      t.set(lot.cartonsOrigine);
      for (const e of retirees) {
        t.fill(0, e.c.debut * 16, (e.c.debut + e.c.nb) * 16);
        for (let i = e.c.debut; i < e.c.debut + e.c.nb; i++) t[i * 16 + 15] = 1; // boîte réduite à un point
      }
      lot.cartons.instanceMatrix.needsUpdate = true;
    }
    return true;
  }

  _majMasques() {
    let change = false;
    for (const lot of this.lots) if (this._masquer(lot)) change = true;
    if (change) this.renderer.shadowMap.needsUpdate = true;
    this.demanderRendu();
  }

  _surcouche(e, materiauStructure, niveau = null) {
    const g = new THREE.Group();
    g.matrixAutoUpdate = false;
    g.matrix.copy(e.matrice);
    const ajouter = (mat, x, y, z, l, h, p) => {
      const m = new THREE.Mesh(BOITE, mat);
      m.position.set(x, y, z);
      m.scale.set(l, h, p);
      m.castShadow = m.receiveShadow = true;
      g.add(m);
    };
    for (const [x, y, z, l, h, p] of e.poteaux) ajouter(materiauStructure, x, y, z, l, h, p);
    e.hauteurs.forEach((y, i) => ajouter(i === niveau ? this.materiaux.niveauCible : this.materiaux.plateau,
      0, y, 0, e.L, EPAISSEUR_PLATEAU, e.P));
    if (e.boitesCartons.length) g.add(this._cartons([{ ...e, matrice: new THREE.Matrix4() }], e.boitesCartons.length));
    this.scene.add(g);
    g.updateMatrixWorld(true);
    return g;
  }

  _retirerSurcouche(g) {
    if (!g) return;
    this.scene.remove(g);
    g.traverse((o) => { if (o.isInstancedMesh) o.dispose(); });
  }

  // Les autres étagères deviennent transparentes (sans leurs cartons) pour bien voir la cible.
  _estomper(oui) {
    if (this.estompe === oui) return;
    this.estompe = oui;
    const M = this.materiaux;
    for (const lot of this.lots) {
      if (lot.detail) {
        lot.structures.material = [oui ? M.structureEstompee : M.structure];
        lot.plateaux.material = [oui ? M.plateauEstompe : M.plateau];
        if (lot.cartons) lot.cartons.visible = !oui;
        lot.structures.castShadow = lot.plateaux.castShadow = !oui;
      }
      if (lot.lointain) lot.lointain.material = [oui ? M.lointainEstompe : M.lointain];
    }
    this.renderer.shadowMap.needsUpdate = true;
  }

  _enleverCible() {
    if (!this.cible) return;
    this._retirerSurcouche(this.cible.groupe);
    this.etiquetteCible?.removeFromParent();
    this.etiquetteCible = null;
    this.cible = null;
  }

  reinitialiser() {
    this._enleverCible();
    this._estomper(false);
    this._majMasques();
    this.repere.visible = false;
    this.demanderRendu();
  }

  _creerRepere() {
    const repere = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial({ color: COULEUR_CIBLE, emissive: COULEUR_CIBLE, emissiveIntensity: 0.6 });
    const cone = new THREE.Mesh(new THREE.ConeGeometry(0.22, 0.5, 24), mat);
    cone.rotation.x = Math.PI; // pointe vers le bas, posée en y = 0
    cone.position.y = 0.25;
    const boule = new THREE.Mesh(new THREE.SphereGeometry(0.24, 24, 16), mat);
    boule.position.y = 0.6;
    repere.add(cone, boule);
    repere.visible = false;
    repere.renderOrder = 11;
    return repere;
  }

  // Met en évidence l'étagère (et le niveau) de l'article, puis y amène la caméra.
  montrer(etagereId, niveau) {
    this._enleverCible();
    this._fermerBulle();
    const e = this.etageres.get(etagereId);
    if (!e) {
      this.reinitialiser();
      return false;
    }
    const index = niveau ? Math.min(Math.max(niveau, 1), e.hauteurs.length) - 1 : null;
    this.cible = { e, groupe: this._surcouche(e, this.materiaux.structureCible, index) };
    this._estomper(true);
    this._majMasques();
    this.etiquetteCible = this._etiquette(e.code, '#dc2626', { ...STYLE_CODE, min: 22, max: 30, devant: true });
    this.scene.add(this.etiquetteCible);
    this.repere.position.set(e.centre.x, e.H + 0.2, e.centre.z);
    this.repereY = this.repere.position.y;
    this.repere.visible = true;
    this.finPulsation = performance.now() + DUREE_PULSATION;

    // La caméra se place devant l'étagère, en légère plongée, du côté où la vue est la plus dégagée.
    const yNiveau = index !== null ? e.hauteurs[index] : e.H / 2;
    const point = new THREE.Vector3(e.centre.x, yNiveau, e.centre.z);
    const marges = this._margesPage();
    const coins = this._coins(e.boite.min, e.boite.max, [0, e.H + 1.2]);
    const candidates = [1, -1].map((sens) => {
      const face = new THREE.Vector3(0, 0, sens).applyQuaternion(e.quaternion);
      const cote = new THREE.Vector3(1, 0, 0).applyQuaternion(e.quaternion);
      const direction = face.add(cote.multiplyScalar(0.35)).add(new THREE.Vector3(0, 0.55, 0)).normalize();
      const d = Math.max(this._ajuster(direction, point, coins, marges, false).distance * 1.3, 3.5);
      return point.clone().addScaledVector(direction, d);
    });
    const obstacles = (pos) => {
      const rayon = new THREE.Ray(pos, point.clone().sub(pos).normalize());
      const longueur = pos.distanceTo(point);
      let n = 0;
      for (const autre of this.liste) {
        if (autre === e) continue;
        const impact = rayon.intersectBox(autre.boite, this._v);
        if (impact && impact.distanceTo(pos) < longueur) n++;
      }
      return n;
    };
    const position = obstacles(candidates[1]) < obstacles(candidates[0]) ? candidates[1] : candidates[0];
    this._volerVers(position, point, marges);
    return true;
  }

  // ------------------------------------------------------------------ cadrage
  _coins(min, max, hauteurs) {
    const coins = [];
    for (const x of [min.x, max.x]) for (const z of [min.z, max.z]) for (const y of hauteurs) coins.push(new THREE.Vector3(x, y, z));
    return coins;
  }

  // Distance à laquelle la caméra, placée dans `direction` depuis `cible`, voit tous les `points` dans la
  // partie libre de l'écran (champs de vision horizontal et vertical). centrer : décale la cible pour que
  // l'ensemble soit au milieu de l'écran (elle reste au sol).
  _ajuster(direction, cible, points, marges, centrer = true) {
    const avant = direction.clone().negate();
    const droite = new THREE.Vector3().crossVectors(avant, HAUT).normalize();
    const haut = new THREE.Vector3().crossVectors(droite, avant);
    const tan = Math.tan(THREE.MathUtils.degToRad(CHAMP / 2)) * 0.92;
    const tanH = tan * this.largeur / this.hauteur;
    const tanV = tan * Math.max(0.2, (this.hauteur - marges.haut - marges.bas) / this.hauteur);
    const c = cible.clone();
    let d = 0;
    for (let tour = 0; ; tour++) {
      d = 0;
      for (const p of points) {
        const q = this._v.subVectors(p, c);
        const z = q.dot(avant);
        d = Math.max(d, Math.abs(q.dot(droite)) / tanH - z, Math.abs(q.dot(haut)) / tanV - z);
      }
      if (!centrer || tour === 2) break;
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      for (const p of points) {
        const q = this._v.subVectors(p, c);
        const prof = d + q.dot(avant);
        const tx = q.dot(droite) / prof, ty = q.dot(haut) / prof;
        x0 = Math.min(x0, tx); x1 = Math.max(x1, tx); y0 = Math.min(y0, ty); y1 = Math.max(y1, ty);
      }
      c.addScaledVector(droite, ((x0 + x1) / 2) * d).addScaledVector(haut, ((y0 + y1) / 2) * d);
      if (Math.abs(avant.y) > 0.05) c.addScaledVector(avant, -c.y / avant.y);
    }
    return { cible: c, distance: d, position: c.clone().addScaledVector(direction, d) };
  }

  // Deux orientations possibles (de face ou de côté) : on garde celle où le plan paraît le plus grand.
  _cadrer(directions, hauteurs) {
    if (!this.limites) return null;
    const { minX, minZ, maxX, maxZ } = this.limites;
    const coins = this._coins({ x: minX, z: minZ }, { x: maxX, z: maxZ }, hauteurs);
    const marges = this._margesPage();
    const [face, cote] = directions.map((d) => this._ajuster(d.normalize(), this.centre, coins, marges));
    const choix = cote.distance < face.distance * 0.8 ? cote : face;
    return { ...choix, marges };
  }

  // Tout le plan, en plongée ; l'article ciblé reste en rouge.
  vueEnsemble(anime = true) {
    const el = 0.74, az = 0.5;
    const dir = (a) => new THREE.Vector3(Math.sin(a) * Math.cos(el), Math.sin(el), Math.cos(a) * Math.cos(el));
    const vue = this._cadrer([dir(az), dir(Math.PI / 2 - 0.35)], [0, this.limites ? this.limites.maxH + 1.6 : 4]);
    if (!vue) return;
    this.finPulsation = performance.now() + DUREE_PULSATION;
    if (anime) this._volerVers(vue.position, vue.cible, vue.marges);
    else this._placer(vue.position, vue.cible, vue.marges);
  }

  vueDessus() {
    const vue = this._cadrer([new THREE.Vector3(0, 1, 0.001), new THREE.Vector3(0.001, 1, 0)], [0, this.limites?.maxH || 3]);
    if (!vue) return;
    this.finPulsation = performance.now() + DUREE_PULSATION;
    this._volerVers(vue.position, vue.cible, vue.marges);
  }

  _placer(position, cible, marges = this.marges) {
    this.animationCamera = null;
    this._appliquerMarges(marges.haut, marges.bas);
    this.camera.position.copy(position);
    this.controles.target.copy(cible);
    this.controles.update();
    this.demanderRendu();
  }

  _volerVers(position, cible, marges = this.marges) {
    this.animationCamera = {
      debut: performance.now(), duree: 1100,
      dePos: this.camera.position.clone(), deCible: this.controles.target.clone(),
      aPos: position, aCible: cible, deMarges: { ...this.marges }, aMarges: marges,
    };
    this.demanderRendu();
  }

  // ------------------------------------------------------------------ étiquettes et repère
  _profondeur(position) {
    return this._v2.subVectors(position, this.camera.position).dot(this._avant);
  }

  _taille(sprite, profondeur, px) {
    const h = (px * profondeur) / this.pixelsParMetre;
    sprite.scale.set(h * sprite.userData.ratio, h, 1);
    return h;
  }

  // Position à l'écran (en pixels CSS) d'un point de la scène.
  _ecran(position) {
    const p = this._v2.copy(position).project(this.camera);
    return { x: ((p.x + 1) / 2) * this.largeur, y: ((1 - p.y) / 2) * this.hauteur, dedans: Math.abs(p.x) < 1.2 && Math.abs(p.y) < 1.2 };
  }

  // Taille à l'écran constante (bornée), codes d'étagère seulement de près ou dans le bloc ciblé,
  // et jamais deux étiquettes l'une sur l'autre : les plus importantes et les plus proches passent d'abord.
  _majEtiquettes() {
    this.camera.getWorldDirection(this._avant);
    const blocCible = this.cible?.e.bloc;
    // De près, seuls le titre du bloc ciblé et les codes voisins restent : les autres cacheraient la vue.
    const deTresPres = this.cible && this.camera.position.distanceTo(this.controles.target) < Math.max(14, this.taille * 0.3);
    const candidates = [];
    for (const s of this.etiquettes) {
      const u = s.userData;
      s.visible = false;
      const profondeur = this._profondeur(s.position);
      if (profondeur < 0.2) continue;
      const naturel = (u.monde * this.pixelsParMetre) / profondeur;
      let rang = u.rang;
      if (u.etagere) {
        const e = u.etagere;
        if (e === this.cible?.e) continue; // son code est affiché au-dessus du repère
        if (e === this.choix?.e) rang = 0;
        else if (naturel < (blocCible && e.bloc === blocCible ? CODE_LISIBLE_BLOC : CODE_LISIBLE)) continue;
      } else if (u.bloc && deTresPres && u.bloc !== blocCible) {
        continue;
      }
      const ecran = this._ecran(s.position);
      if (!ecran.dedans) continue;
      const k = u.reductible ? this.echelleTitres : 1;
      candidates.push({ s, rang, profondeur, px: Math.min(u.max * k, Math.max(u.min * k, naturel)), ...ecran });
    }
    candidates.sort((a, b) => a.rang - b.rang || a.profondeur - b.profondeur);
    const places = this._rectRepere ? [this._rectRepere] : [];
    let codes = 0;
    for (const c of candidates) {
      const h = c.px, l = h * c.s.userData.ratio;
      const r = { x0: c.x - l / 2 - 3, x1: c.x + l / 2 + 3, y0: c.y - h / 2 - 3, y1: c.y + h / 2 + 3 };
      if (r.x0 < 0 || r.y0 < 0 || r.x1 > this.largeur || r.y1 > this.hauteur) continue; // coupée par le bord
      if (places.some((p) => r.x0 < p.x1 && r.x1 > p.x0 && r.y0 < p.y1 && r.y1 > p.y0)) continue;
      if (c.rang === STYLE_CODE.rang && ++codes > CODES_MAX) continue;
      places.push(r);
      c.s.visible = true;
      this._taille(c.s, c.profondeur, c.px);
    }
  }

  _majRepere(maintenant, pulse) {
    const r = this.repere;
    this._rectRepere = null;
    if (!r.visible) return;
    this.camera.getWorldDirection(this._avant);
    r.position.y = this.repereY;
    const profondeur = Math.max(0.2, this._profondeur(r.position));
    // Toujours au moins 36 px de haut : on le voit aussi en vue d'ensemble.
    const k = Math.max(1, 36 / ((0.85 * this.pixelsParMetre) / profondeur));
    r.scale.setScalar(k);
    r.position.y = this.repereY + (pulse ? ((Math.sin(maintenant / 333) + 1) / 2) * 0.15 * k : 0);
    if (pulse) r.rotation.y = maintenant / 666;
    const t = this.etiquetteCible;
    if (!t) return;
    const u = t.userData;
    const px = Math.min(u.max, Math.max(u.min, (u.monde * this.pixelsParMetre) / profondeur));
    const h = this._taille(t, profondeur, px);
    t.position.set(r.position.x, r.position.y + 0.9 * k + h * 0.6, r.position.z);
    // Place réservée (repère et code) : aucune autre étiquette ne vient dessus.
    const bas = this._ecran(this._v.set(r.position.x, this.repereY, r.position.z));
    const haut = this._ecran(t.position);
    const demi = Math.max(px * u.ratio, 40) / 2 + 6;
    this._rectRepere = { x0: haut.x - demi, x1: haut.x + demi, y0: haut.y - px / 2 - 8, y1: Math.max(bas.y, haut.y + px / 2) + 10 };
  }

  // ------------------------------------------------------------------ clic ou toucher sur une étagère
  _ecouterToucher() {
    const el = this.renderer.domElement;
    let depart = null;
    el.addEventListener('pointerdown', (ev) => {
      depart = ev.isPrimary ? { x: ev.clientX, y: ev.clientY, t: performance.now(), id: ev.pointerId } : null;
    });
    el.addEventListener('pointercancel', () => { depart = null; });
    el.addEventListener('pointerup', (ev) => {
      const d = depart;
      depart = null;
      if (!d || ev.pointerId !== d.id || ev.button > 0) return;
      if (Math.hypot(ev.clientX - d.x, ev.clientY - d.y) > 8 || performance.now() - d.t > 700) return; // c'était un geste
      this._toucher(ev.clientX, ev.clientY);
    });
    document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && this.choix) this._fermerBulle(); });
  }

  etagereSous(x, y) {
    const r = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1);
    this._raycaster ||= new THREE.Raycaster();
    this._raycaster.setFromCamera(ndc, this.camera);
    let meilleure = null, distance = Infinity;
    for (const e of this.liste) {
      const impact = this._raycaster.ray.intersectBox(e.boite, this._v);
      if (!impact) continue;
      const d = impact.distanceTo(this._raycaster.ray.origin);
      if (d < distance) { distance = d; meilleure = e; }
    }
    return meilleure;
  }

  _toucher(x, y) {
    const e = this.etagereSous(x, y);
    if (!e) { this._fermerBulle(); return; }
    this.choisir(e);
  }

  // Petite fiche de l'étagère (code, niveaux, nombre d'articles, lien vers ses articles).
  choisir(e) {
    if (typeof e !== 'object') e = this.etageres.get(e);
    this._fermerBulle();
    if (!e) return;
    this.choix = { e, groupe: e === this.cible?.e ? null : this._surcouche(e, this.materiaux.structureChoix) };
    this._majMasques();
    this._remplirBulle(e);
    this.demanderRendu();
  }

  _creerBulle() {
    const bulle = document.createElement('div');
    bulle.className = 'bulle-etagere';
    bulle.hidden = true;
    bulle.setAttribute('role', 'dialog');
    const fermer = document.createElement('button');
    fermer.type = 'button';
    fermer.className = 'fermer';
    fermer.setAttribute('aria-label', 'Fermer');
    fermer.textContent = '×';
    fermer.addEventListener('click', () => this._fermerBulle());
    bulle.append(fermer, document.createElement('div'));
    this.conteneur.append(bulle);
    return bulle;
  }

  _remplirBulle(e) {
    const d = e.donnees;
    const n = d.actifs ?? d.articles;
    const corps = document.createElement('div');
    const titre = document.createElement('b');
    titre.textContent = `Étagère ${e.code}`;
    const details = document.createElement('div');
    details.className = 'details';
    const morceaux = [];
    if (e.bloc.code) morceaux.push(`Bloc ${e.bloc.code}`);
    morceaux.push(`${e.hauteurs.length} niveau${e.hauteurs.length > 1 ? 'x' : ''}`);
    if (n != null) morceaux.push(n ? `${n} article${n > 1 ? 's' : ''}` : 'aucun article');
    details.textContent = morceaux.join(' · ');
    corps.append(titre, details);
    const lien = e.id != null && n !== 0 ? this.options.lienEtagere?.(d) : null;
    if (lien) {
      const a = document.createElement('a');
      a.href = lien;
      a.textContent = 'Voir les articles';
      corps.append(a);
    }
    this.bulle.lastChild.replaceWith(corps);
    this.bulle.setAttribute('aria-label', titre.textContent);
    this.bulle.hidden = false;
    this._placerBulle();
  }

  _placerBulle() {
    if (!this.choix || this.bulle.hidden) return;
    const e = this.choix.e;
    const p = this._v.set(e.centre.x, e.H + 0.1, e.centre.z).project(this.camera);
    const b = this.bulle;
    if (p.z > 1 || Math.abs(p.x) > 1.2 || Math.abs(p.y) > 1.2) { b.style.visibility = 'hidden'; return; }
    b.style.visibility = '';
    const x = ((p.x + 1) / 2) * this.largeur, y = ((1 - p.y) / 2) * this.hauteur;
    const w = b.offsetWidth, h = b.offsetHeight;
    const gauche = Math.min(Math.max(8, x - w / 2), this.largeur - w - 8);
    const dessous = y - h - 14 < 8;
    const haut = dessous ? Math.min(y + 14, this.hauteur - h - 8) : y - h - 14;
    b.style.left = `${gauche}px`;
    b.style.top = `${haut}px`;
    b.style.setProperty('--fleche', `${Math.min(Math.max(14, x - gauche), w - 14)}px`);
    b.classList.toggle('dessous', dessous);
  }

  _fermerBulle() {
    if (!this.choix) return;
    this._retirerSurcouche(this.choix.groupe);
    this.choix = null;
    this.bulle.hidden = true;
    this._majMasques();
    this.demanderRendu();
  }
}
