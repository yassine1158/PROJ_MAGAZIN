// Vue 3D du magasin : blocs au sol, étagères avec niveaux, et repère sur l'emplacement cherché.
// Coordonnées en mètres : x vers la droite, z vers le fond, y vers le haut.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const sombre = () => window.matchMedia('(prefers-color-scheme: dark)').matches;
const EPAISSEUR_PLATEAU = 0.04;
const COULEUR_ETAGERE = 0x94a3b8;
const COULEUR_CIBLE = 0xef4444;

function etiquette(texte, { taille = 64, fond = 'rgba(15,23,42,.85)', couleur = '#fff' } = {}) {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  ctx.font = `700 ${taille}px Inter, system-ui, sans-serif`;
  const largeur = ctx.measureText(texte).width + taille;
  canvas.width = largeur;
  canvas.height = taille * 1.6;
  ctx.font = `700 ${taille}px Inter, system-ui, sans-serif`;
  ctx.fillStyle = fond;
  const r = taille * 0.4;
  ctx.beginPath();
  ctx.roundRect(0, 0, canvas.width, canvas.height, r);
  ctx.fill();
  ctx.fillStyle = couleur;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(texte, canvas.width / 2, canvas.height / 2 + 2);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthTest: false }));
  const echelle = 0.006 * taille / 64;
  sprite.scale.set(canvas.width * echelle, canvas.height * echelle, 1);
  sprite.renderOrder = 10;
  return sprite;
}

// Pseudo-aléatoire stable : les cartons décoratifs restent au même endroit à chaque chargement.
function aleatoire(graine) {
  let s = graine % 2147483647 || 1;
  return () => (s = (s * 16807) % 2147483647) / 2147483647;
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

export class Magasin3D {
  constructor(conteneur) {
    this.conteneur = conteneur;
    this.etageres = new Map(); // id -> { groupe, plateaux[], materiaux, centre, hauteurs[] }
    this.titresBlocs = [];
    this.horloge = new THREE.Clock();
    this.animationCamera = null;

    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    conteneur.prepend(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 500);
    this.controles = new OrbitControls(this.camera, this.renderer.domElement);
    this.controles.enableDamping = true;
    this.controles.maxPolarAngle = Math.PI / 2.05;
    this.controles.addEventListener('start', () => { this.animationCamera = null; });

    this.scene.add(new THREE.HemisphereLight(0xffffff, sombre() ? 0x1e293b : 0xcbd5e1, sombre() ? 1.4 : 1.8));
    this.soleil = new THREE.DirectionalLight(0xffffff, sombre() ? 1.2 : 1.6);
    this.soleil.castShadow = true;
    this.soleil.shadow.mapSize.set(2048, 2048);
    this.scene.add(this.soleil);

    this.repere = this._creerRepere();
    this.scene.add(this.repere);
    this.contenu = new THREE.Group(); // tout ce qui vient du plan (vidé à chaque nouveau chargement)
    this.scene.add(this.contenu);

    new ResizeObserver(() => this._redimensionner()).observe(conteneur);
    this._redimensionner();
    this.renderer.setAnimationLoop(() => this._boucle());
  }

  _vider() {
    this.contenu.traverse((o) => {
      o.geometry?.dispose();
      for (const m of [].concat(o.material || [])) { m.map?.dispose(); m.dispose(); }
    });
    this.scene.remove(this.contenu);
    this.contenu = new THREE.Group();
    this.scene.add(this.contenu);
    this.etageres = new Map();
    this.titresBlocs = [];
    this.cible = null;
    this.repere.visible = false;
  }

  // garderVue : redessine sans bouger la caméra (aperçu en direct de l'éditeur de plan).
  charger(plan, { garderVue = false } = {}) {
    const dejaCharge = Boolean(this.centre);
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
    }
    for (const e of elements) {
      const r = Math.max(e.largeur, e.profondeur) / 2;
      minX = Math.min(minX, e.x - r); maxX = Math.max(maxX, e.x + r);
      minZ = Math.min(minZ, e.z - r); maxZ = Math.max(maxZ, e.z + r);
    }
    this.limites = { minX, minZ, maxX, maxZ, maxH };
    this.centre = new THREE.Vector3((minX + maxX) / 2, 0, (minZ + maxZ) / 2);
    this.taille = Math.max(maxX - minX, maxZ - minZ, 10);

    // Sol
    const marge = 4;
    const sol = new THREE.Mesh(
      new THREE.PlaneGeometry(maxX - minX + marge * 2, maxZ - minZ + marge * 2),
      new THREE.MeshStandardMaterial({ color: sombre() ? 0x1e293b : 0xe2e8f0, roughness: 0.95 }),
    );
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
    this.scene.add(s.target);
    const cam = s.shadow.camera;
    cam.left = cam.bottom = -this.taille;
    cam.right = cam.top = this.taille;
    cam.far = this.taille * 4;
    cam.updateProjectionMatrix();

    for (const b of blocs) this._ajouterBloc(b, maxH);
    for (const m of murs) this._ajouterMur(m, ouvertures(m, elements));
    for (const e of elements) this._ajouterElement(e);
    if (!garderVue || !dejaCharge) this.vueEnsemble(false);
    return true;
  }

  // Un mur est découpé en morceaux pour laisser les ouvertures (portes, fenêtres).
  _ajouterMur(m, trous = []) {
    const dx = m.x2 - m.x1, dz = m.z2 - m.z1;
    const longueur = Math.hypot(dx, dz);
    if (longueur < 0.01) return;
    const H = m.hauteur || 3, ep = m.epaisseur || 0.2;
    const mat = new THREE.MeshStandardMaterial({ color: m.couleur || (sombre() ? 0x64748b : 0xd6d3d1), roughness: 0.9 });
    const angle = -Math.atan2(dz, dx);
    const ux = dx / longueur, uz = dz / longueur;
    const morceau = (t0, t1, y0, y1) => { // de t0 à t1 le long du mur, de y0 à y1 en hauteur
      if (t1 - t0 < 0.01 || y1 - y0 < 0.01) return;
      const bout0 = t0 <= 0.001 ? ep / 2 : 0, bout1 = t1 >= longueur - 0.001 ? ep / 2 : 0; // angles bien fermés
      const l = t1 - t0 + bout0 + bout1, milieu = (t0 - bout0 + t1 + bout1) / 2;
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(l, y1 - y0, ep), mat);
      mesh.position.set(m.x1 + ux * milieu, (y0 + y1) / 2, m.z1 + uz * milieu);
      mesh.rotation.y = angle;
      mesh.castShadow = mesh.receiveShadow = true;
      this.contenu.add(mesh);
    };
    let t = 0;
    for (const o of trous) {
      morceau(t, o.t0, 0, H);
      morceau(o.t0, o.t1, 0, Math.min(o.bas, H));       // allège sous une fenêtre
      morceau(o.t0, o.t1, Math.min(o.haut, H), H);       // linteau au-dessus
      t = Math.max(t, o.t1);
    }
    morceau(t, longueur, 0, H);
  }

  _ajouterElement(e) {
    const g = new THREE.Group();
    g.position.set(e.x, 0, e.z);
    g.rotation.y = -(e.rotation || 0) * Math.PI / 180;
    const L = e.largeur, P = e.profondeur, H = e.hauteur || 0;
    const mat = (couleur, extra = {}) => new THREE.MeshStandardMaterial({ color: couleur, roughness: 0.8, ...extra });
    const boite = (l, h, p, y, matiere, x = 0, z = 0) => {
      const b = new THREE.Mesh(new THREE.BoxGeometry(l, h, p), matiere);
      b.position.set(x, y, z); b.castShadow = b.receiveShadow = true; g.add(b); return b;
    };
    const sol = (couleur, opacite) => {
      const z = new THREE.Mesh(new THREE.PlaneGeometry(L, P), mat(couleur, { transparent: true, opacity: opacite }));
      z.rotation.x = -Math.PI / 2; z.position.y = 0.015; z.receiveShadow = true; g.add(z);
    };
    const titre = (texte, y, couleur = '#16202a') => {
      if (!texte) return;
      const t = etiquette(texte, { taille: 54, fond: couleur }); t.position.set(0, y, 0); g.add(t);
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

  _ajouterBloc(b, maxH) {
    const couleur = new THREE.Color(b.couleur || '#3b82f6');
    const zone = new THREE.Mesh(
      new THREE.PlaneGeometry(b.largeur, b.profondeur),
      new THREE.MeshStandardMaterial({ color: couleur, transparent: true, opacity: 0.22 }),
    );
    zone.rotation.x = -Math.PI / 2;
    zone.position.set(b.x + b.largeur / 2, 0.01, b.z + b.profondeur / 2);
    zone.receiveShadow = true;
    this.contenu.add(zone);

    const contour = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(b.largeur, 0.001, b.profondeur)),
      new THREE.LineBasicMaterial({ color: couleur }),
    );
    contour.position.set(zone.position.x, 0.02, zone.position.z);
    this.contenu.add(contour);

    const titre = etiquette(`BLOC ${b.code}`, { taille: 90, fond: b.couleur || '#3b82f6' });
    titre.position.set(zone.position.x, maxH + 1.2, zone.position.z);
    this.contenu.add(titre);
    this.titresBlocs.push(titre);

    for (const e of b.etageres) this._ajouterEtagere(b, e);
  }

  _ajouterEtagere(b, e) {
    const groupe = new THREE.Group();
    const L = e.largeur, P = e.profondeur, H = e.hauteur;
    groupe.position.set(b.x + e.x + (e.tournee ? P : L) / 2, 0, b.z + e.z + (e.tournee ? L : P) / 2);
    if (e.tournee) groupe.rotation.y = Math.PI / 2;

    const matStructure = new THREE.MeshStandardMaterial({ color: COULEUR_ETAGERE, metalness: 0.6, roughness: 0.4 });
    const matPlateau = new THREE.MeshStandardMaterial({ color: sombre() ? 0x475569 : 0xcbd5e1, roughness: 0.7 });
    const poteau = new THREE.BoxGeometry(0.06, H, 0.06);
    for (const [px, pz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
      const m = new THREE.Mesh(poteau, matStructure);
      m.position.set(px * (L / 2 - 0.03), H / 2, pz * (P / 2 - 0.03));
      m.castShadow = true;
      groupe.add(m);
    }

    const n = Math.max(1, e.niveaux);
    const pas = H / n;
    const plateaux = [];
    const hauteurs = [];
    const hasard = aleatoire(e.id * 7919);
    const couleursCartons = [0xc8a165, 0xb98d55, 0xd6b47c, 0x9ca3af, 0x60a5fa];
    for (let i = 0; i < n; i++) {
      const y = i === 0 ? 0.12 : i * pas;
      hauteurs.push(y);
      const plateau = new THREE.Mesh(new THREE.BoxGeometry(L, EPAISSEUR_PLATEAU, P), matPlateau.clone());
      plateau.position.y = y;
      plateau.castShadow = plateau.receiveShadow = true;
      groupe.add(plateau);
      plateaux.push(plateau);

      // Quelques cartons pour donner vie à l'étagère.
      let x = -L / 2 + 0.1;
      while (x < L / 2 - 0.4) {
        const w = 0.3 + hasard() * 0.4;
        const h = Math.min(pas * (0.4 + hasard() * 0.45), pas - 0.1);
        if (x + w > L / 2 - 0.1) break;
        if (hasard() > 0.2) {
          const carton = new THREE.Mesh(
            new THREE.BoxGeometry(w * 0.92, h, P * (0.6 + hasard() * 0.3)),
            new THREE.MeshStandardMaterial({ color: couleursCartons[Math.floor(hasard() * couleursCartons.length)], roughness: 0.9 }),
          );
          carton.position.set(x + w / 2, y + EPAISSEUR_PLATEAU / 2 + h / 2, 0);
          carton.castShadow = true;
          groupe.add(carton);
        }
        x += w + 0.05;
      }
    }

    const nom = etiquette(e.code, { taille: 48, fond: 'rgba(15,23,42,.75)' });
    nom.position.set(0, H + 0.35, 0);
    groupe.add(nom);

    this.contenu.add(groupe);
    groupe.updateMatrixWorld(true);
    const materiaux = new Set();
    groupe.traverse((o) => { if (o.material) materiaux.add(o.material); });
    const boite = new THREE.Box3().setFromObject(groupe);
    this.etageres.set(e.id, { groupe, plateaux, hauteurs, pas, matStructure, materiaux, boite, H, L, P });
  }

  _creerRepere() {
    const repere = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial({ color: COULEUR_CIBLE, emissive: COULEUR_CIBLE, emissiveIntensity: 0.6 });
    const cone = new THREE.Mesh(new THREE.ConeGeometry(0.22, 0.5, 24), mat);
    cone.rotation.x = Math.PI; // pointe vers le bas
    const boule = new THREE.Mesh(new THREE.SphereGeometry(0.24, 24, 16), mat);
    boule.position.y = 0.35;
    repere.add(cone, boule);
    repere.visible = false;
    return repere;
  }

  _estomper(e, oui) {
    for (const m of e.materiaux) {
      m.transparent = oui;
      m.opacity = oui ? 0.15 : 1;
      m.depthWrite = !oui;
      m.needsUpdate = true;
    }
    e.groupe.traverse((o) => { if (o.isMesh) o.castShadow = !oui; });
  }

  reinitialiser() {
    // Les grands titres « BLOC X » ne servent qu'en vue d'ensemble : de près, ils cachent la vue.
    for (const t of this.titresBlocs) t.visible = true;
    for (const e of this.etageres.values()) {
      this._estomper(e, false);
      e.matStructure.color.setHex(COULEUR_ETAGERE);
      e.matStructure.emissive.setHex(0x000000);
      for (const p of e.plateaux) {
        p.material.emissive.setHex(0x000000);
        p.material.color.setHex(sombre() ? 0x475569 : 0xcbd5e1);
      }
    }
    this.cible = null;
    this.repere.visible = false;
  }

  // Met en évidence l'étagère (et le niveau) de l'article, puis y amène la caméra.
  montrer(etagereId, niveau) {
    this.reinitialiser();
    const e = this.etageres.get(etagereId);
    if (!e) return false;
    // Les autres étagères deviennent transparentes pour bien voir la cible.
    for (const autre of this.etageres.values()) if (autre !== e) this._estomper(autre, true);
    for (const t of this.titresBlocs) t.visible = false;
    e.matStructure.color.setHex(COULEUR_CIBLE);
    e.matStructure.emissive.setHex(0x7f1d1d);

    const index = niveau ? Math.min(Math.max(niveau, 1), e.plateaux.length) - 1 : null;
    const yNiveau = index !== null ? e.hauteurs[index] : e.H / 2;
    if (index !== null) this.cible = e.plateaux[index];

    const point = new THREE.Vector3(0, yNiveau, 0).applyMatrix4(e.groupe.matrixWorld);
    // Le repère flotte au-dessus de l'étagère ; le niveau exact clignote en rouge.
    this.repere.position.set(point.x, e.H + 0.9, point.z);
    this.repereY = this.repere.position.y;
    this.repere.visible = true;

    // La caméra se place devant l'étagère, en légère plongée, du côté où la vue est la plus dégagée.
    const distance = Math.max(e.L, e.H) * 1.6 + 2;
    const candidates = [1, -1].map((sens) => {
      const face = new THREE.Vector3(0, 0, sens).applyQuaternion(e.groupe.quaternion);
      const cote = new THREE.Vector3(1, 0, 0).applyQuaternion(e.groupe.quaternion);
      return point.clone()
        .add(face.multiplyScalar(distance))
        .add(cote.multiplyScalar(distance * 0.35))
        .add(new THREE.Vector3(0, distance * 0.55, 0));
    });
    const obstacles = (pos) => {
      const rayon = new THREE.Ray(pos, point.clone().sub(pos).normalize());
      const longueur = pos.distanceTo(point);
      let n = 0;
      for (const autre of this.etageres.values()) {
        if (autre === e) continue;
        const impact = rayon.intersectBox(autre.boite, new THREE.Vector3());
        if (impact && impact.distanceTo(pos) < longueur) n++;
      }
      return n;
    };
    const position = obstacles(candidates[1]) < obstacles(candidates[0]) ? candidates[1] : candidates[0];
    this._volerVers(position, point);
    return true;
  }

  vueEnsemble(anime = true) {
    if (!this.centre) return;
    const d = this.taille * 1.1;
    const pos = new THREE.Vector3(this.centre.x + d * 0.6, d * 0.75, this.centre.z + d * 0.9);
    anime ? this._volerVers(pos, this.centre.clone()) : this._placer(pos, this.centre);
  }

  vueDessus() {
    if (!this.centre) return;
    this._volerVers(new THREE.Vector3(this.centre.x, this.taille * 1.5, this.centre.z + 0.01), this.centre.clone());
  }

  _placer(position, cible) {
    this.camera.position.copy(position);
    this.controles.target.copy(cible);
    this.controles.update();
  }

  _volerVers(position, cible) {
    this.animationCamera = {
      debut: performance.now(), duree: 1100,
      dePos: this.camera.position.clone(), deCible: this.controles.target.clone(),
      aPos: position, aCible: cible,
    };
  }

  _redimensionner() {
    const { clientWidth: l, clientHeight: h } = this.conteneur;
    if (!l || !h) return;
    this.renderer.setSize(l, h, false);
    this.camera.aspect = l / h;
    this.camera.updateProjectionMatrix();
  }

  _boucle() {
    const t = this.horloge.getElapsedTime();
    const a = this.animationCamera;
    if (a) {
      const k = Math.min(1, (performance.now() - a.debut) / a.duree);
      const lisse = k < 0.5 ? 4 * k ** 3 : 1 - (-2 * k + 2) ** 3 / 2;
      this.camera.position.lerpVectors(a.dePos, a.aPos, lisse);
      this.controles.target.lerpVectors(a.deCible, a.aCible, lisse);
      if (k === 1) this.animationCamera = null;
    }
    if (this.repere.visible) {
      this.repere.position.y = this.repereY + Math.sin(t * 3) * 0.12;
      this.repere.rotation.y = t * 1.5;
    }
    if (this.cible) {
      const pulse = (Math.sin(t * 5) + 1) / 2;
      this.cible.material.color.setHex(COULEUR_CIBLE);
      this.cible.material.emissive.setRGB(0.5 + pulse * 0.5, 0.05, 0.05);
    }
    this.controles.update();
    this.renderer.render(this.scene, this.camera);
  }
}
