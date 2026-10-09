// Page « Trouver un article ». La recherche répond tout de suite (recherche locale), l'IA complète
// ensuite si elle est active. La vue 3D se charge à côté : la recherche n'en dépend jamais.
const cfg = window.MAGASIN;
const $ = (id) => document.getElementById(id);
const form = $('form-recherche');
const champ = $('q');
const champPhoto = $('photo'); // absent quand la recherche par photo n'est pas activée
const zoneResultats = $('resultats');
const zoneExplication = $('explication');
const fiche = $('fiche');
const petitEcran = window.matchMedia('(max-width: 1100px)');

let resultats = [];
let choisi = -1;
let numero = 0; // numéro de la dernière recherche : une réponse plus ancienne est ignorée
let vue = null; // vue 3D, quand elle est prête
let aMontrer = null; // article à montrer dès que la 3D est prête

// ---------- Outils ----------
function texte(s) {
  const d = document.createElement('div');
  d.textContent = s ?? '';
  return d.innerHTML.replace(/"/g, '&quot;');
}

function badgeStock(a) {
  if (a.rupture) return '<span class="badge badge-danger">Rupture</span>';
  if (a.alerte) return `<span class="badge badge-warn">${texte(a.stock)} ${texte(a.unite)} · à commander</span>`;
  return `<span class="badge badge-ok">${texte(a.stock)} ${texte(a.unite)}</span>`;
}

function chipsLieu(a) {
  if (!a.etagere_id) return '<span class="chip">Emplacement non renseigné</span>';
  let html = `<span class="chip bloc" style="background:var(--primaire)">Bloc ${texte(a.bloc)}</span>`;
  html += `<span class="chip">Étagère ${texte(a.etagere)}</span>`;
  if (a.niveau) html += `<span class="chip">Niveau ${texte(a.niveau)}</span>`;
  if (a.case) html += `<span class="chip">${texte(a.case)}</span>`;
  return html;
}

function message(html, attente = false) {
  zoneResultats.innerHTML = attente
    ? `<div class="vide attente"><div class="spinner"></div> ${html}</div>`
    : `<div class="vide">${html}</div>`;
  resultats = [];
  choisi = -1;
}

// ---------- 3D ----------
function effacer3D() {
  aMontrer = null;
  fiche.classList.remove('visible');
  if (vue) { vue.reinitialiser(); vue.vueEnsemble(); }
}

function montrer3D(a) {
  aMontrer = a;
  if (!vue || !a) return;
  if (!(a.etagere_id && vue.montrer(a.etagere_id, a.niveau))) {
    vue.reinitialiser();
    vue.vueEnsemble();
  }
}

async function charger3D() {
  const attente = $('chargement-3d');
  try {
    const [{ Magasin3D }, plan] = await Promise.all([
      import('./magasin3d.js'),
      fetch(cfg.apiPlan).then((rep) => rep.json()),
    ]);
    vue = new Magasin3D($('vue3d'));
    if (!vue.charger(plan)) $('plan-vide').style.display = 'grid';
    if (aMontrer) montrer3D(aMontrer);
  } catch (err) {
    console.error(err);
    vue = null;
    $('plan-vide').style.display = 'grid';
    $('plan-vide').innerHTML = "<p>La vue 3D ne peut pas s'afficher sur cet appareil. La recherche fonctionne quand même.</p>";
  } finally {
    attente?.remove();
  }
}

// ---------- Affichage ----------
function afficher(json, { garderChoix = false } = {}) {
  if (json.explication) {
    zoneExplication.innerHTML = `✦ ${texte(json.explication)}<div class="termes">${(json.termes || []).map((t) => `<span>${texte(t)}</span>`).join('')}</div>`;
    zoneExplication.hidden = false;
  } else {
    zoneExplication.hidden = true;
  }
  const liste = json.resultats || [];
  if (!liste.length) {
    message(texte(json.message || 'Aucun article trouvé. Essayez un autre mot ou le code de l\'article.'));
    effacer3D();
    return;
  }
  const idChoisi = garderChoix ? resultats[choisi]?.id : null;
  resultats = liste;
  zoneResultats.innerHTML = liste.map((a, i) => `
    <article class="carte resultat" data-i="${i}" role="option" tabindex="0" aria-selected="false">
      <div class="vignette">${a.photo ? `<img src="${texte(a.photo)}" alt="" loading="lazy">` : 'Pas de photo'}</div>
      <div>
        <div class="titre">${texte(a.designation)}</div>
        <div class="code">${texte(a.code)} · ${texte(a.categorie)} · ${badgeStock(a)}</div>
        <div class="lieu">${chipsLieu(a)}</div>
      </div>
    </article>`).join('')
    + (json.plus ? '<div class="resultats-plus">Seuls les 12 premiers articles sont affichés : précisez votre recherche.</div>' : '');
  const garde = liste.findIndex((a) => a.id === idChoisi);
  choisi = -1;
  selectionner(garde >= 0 ? garde : 0, { montrer: garde < 0 });
}

function selectionner(i, { defiler = false, visible = false, montrer = true } = {}) {
  const a = resultats[i];
  if (!a) return;
  const deja = i === choisi;
  choisi = i;
  zoneResultats.querySelectorAll('.resultat').forEach((el) => {
    const oui = Number(el.dataset.i) === i;
    el.classList.toggle('selectionne', oui);
    el.setAttribute('aria-selected', oui ? 'true' : 'false');
    if (oui && visible) el.scrollIntoView({ block: 'nearest' });
  });
  fiche.innerHTML = `
    <div class="fiche-lieu">${a.etagere_id ? texte(a.emplacement) : 'Emplacement non renseigné'}</div>
    <div class="fiche-titre"><span class="code">${texte(a.code)}</span> <b>${texte(a.designation)}</b></div>
    ${a.bloc_nom ? `<div class="code fiche-bloc">Bloc ${texte(a.bloc)} : ${texte(a.bloc_nom)}</div>` : ''}
    <div class="fiche-pied">${badgeStock(a)} <a href="${texte(a.url)}">Fiche article</a></div>
    ${a.photo ? `<img src="${texte(a.photo)}" alt="">` : ''}`;
  fiche.classList.add('visible');
  if (montrer && !deja) montrer3D(a);
  // Sur téléphone et tablette, la 3D est sous la liste : on y descend quand on touche un résultat.
  if (defiler && petitEcran.matches) $('vue3d').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ---------- Recherche ----------
async function envoyer(q, photo, avecIA) {
  const donnees = new FormData();
  donnees.append('q', q);
  if (photo) donnees.append('photo', photo);
  const rep = await fetch(`${cfg.apiRecherche}?ia=${avecIA ? 1 : 0}`, {
    method: 'POST', body: donnees, headers: { 'X-CSRFToken': cfg.csrf },
  });
  let json = null;
  try { json = await rep.json(); } catch { /* réponse non JSON */ }
  if (!rep.ok || !json) throw new Error(json?.erreur || 'La recherche a échoué. Réessayez.');
  return json;
}

// L'IA met quelques secondes : on montre d'abord les résultats directs, puis on les complète.
async function completerAvecIA(q, n, local) {
  if (local.resultats.length) {
    zoneExplication.innerHTML = '<span class="attente-ia"><span class="spinner"></span> L\'IA affine la recherche…</span>';
    zoneExplication.hidden = false;
  } else {
    message('Aucun résultat direct. L\'IA cherche…', true);
    effacer3D();
  }
  let json = null;
  try { json = await envoyer(q, null, true); } catch { /* on garde les résultats directs */ }
  if (n !== numero) return;
  if (json?.ia) afficher(json, { garderChoix: true });
  else if (local.resultats.length) zoneExplication.hidden = true;
  else afficher(local);
}

async function chercher() {
  const q = champ.value.trim();
  const photo = champPhoto?.files[0];
  if (!q && !photo) { champ.focus(); return; }
  const n = ++numero;
  history.replaceState(null, '', q ? `?q=${encodeURIComponent(q)}` : location.pathname);
  zoneExplication.hidden = true;
  message(photo ? 'Analyse de la photo…' : 'Recherche…', true);
  try {
    const json = await envoyer(q, photo, Boolean(photo));
    if (n !== numero) return;
    afficher(json);
    if (!photo && json.ia_disponible && !json.exact) completerAvecIA(q, n, json);
  } catch (err) {
    if (n === numero) message(texte(err.message));
  }
}

// ---------- Gestionnaires (attachés avant tout chargement) ----------
form.addEventListener('submit', (e) => { e.preventDefault(); chercher(); });
if (window.matchMedia('(max-width: 500px)').matches) champ.placeholder = 'Ex. : fer de 10, ciment…';

zoneResultats.addEventListener('click', (e) => {
  const el = e.target.closest('.resultat');
  if (el) selectionner(Number(el.dataset.i), { defiler: true });
});
zoneResultats.addEventListener('keydown', (e) => {
  const el = e.target.closest('.resultat');
  if (el && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    selectionner(Number(el.dataset.i), { defiler: true });
  }
});

// Flèches haut/bas : article suivant ou précédent (depuis le champ de recherche ou la liste).
document.addEventListener('keydown', (e) => {
  if ((e.key !== 'ArrowDown' && e.key !== 'ArrowUp') || e.altKey || e.ctrlKey || e.metaKey) return;
  if (!resultats.length || (e.target.closest('input, textarea, select') && e.target !== champ)) return;
  e.preventDefault();
  const i = Math.max(0, Math.min(resultats.length - 1, choisi + (e.key === 'ArrowDown' ? 1 : -1)));
  if (i === choisi) return;
  selectionner(i, { visible: true });
  if (document.activeElement?.classList.contains('resultat')) zoneResultats.querySelector(`[data-i="${i}"]`)?.focus();
});

if (champPhoto) {
  $('btn-photo').addEventListener('click', () => champPhoto.click());
  champPhoto.addEventListener('change', () => {
    const f = champPhoto.files[0];
    const apercu = $('photo-apercu');
    if (!f) { apercu.classList.remove('visible'); return; }
    apercu.querySelector('img').src = URL.createObjectURL(f);
    apercu.classList.add('visible');
    chercher();
  });
  $('photo-retirer').addEventListener('click', () => {
    champPhoto.value = '';
    $('photo-apercu').classList.remove('visible');
  });
}

// « Vue d'ensemble » prend de la hauteur sans perdre l'article : l'étagère reste en rouge.
$('btn-vue-ensemble').addEventListener('click', () => vue?.vueEnsemble());
$('btn-vue-dessus').addEventListener('click', () => vue?.vueDessus());

// Résultats déjà calculés par le serveur (?q=… depuis la barre de recherche ou une douchette, ?article=…).
const initial = JSON.parse($('recherche-initiale')?.textContent || 'null');
if (initial) {
  const n = ++numero;
  afficher(initial);
  const q = champ.value.trim();
  if (q && initial.ia_disponible && !initial.exact) completerAvecIA(q, n, initial);
}

charger3D();
