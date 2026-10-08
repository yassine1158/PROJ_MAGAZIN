const cfg = window.MAGASIN;
const $ = (id) => document.getElementById(id);
const form = $('form-recherche');
const champ = $('q');
const champPhoto = $('photo');
const zoneResultats = $('resultats');
const zoneExplication = $('explication');
const fiche = $('fiche');
let resultats = [];

// ---------- 3D ----------
// Chargée à part : si la 3D échoue (vieux téléphone, WebGL absent), la recherche fonctionne quand même.
let vue = null;
try {
  const { Magasin3D } = await import('./magasin3d.js');
  vue = new Magasin3D($('vue3d'));
  const plan = await (await fetch(cfg.apiPlan)).json();
  if (!vue.charger(plan)) $('plan-vide').style.display = 'grid';
} catch (err) {
  console.error(err);
  $('plan-vide').style.display = 'grid';
  $('plan-vide').innerHTML = '<p>La vue 3D ne peut pas s\'afficher sur cet appareil (WebGL indisponible).</p>';
}
$('btn-vue-ensemble').onclick = () => { vue?.reinitialiser(); vue?.vueEnsemble(); fiche.classList.remove('visible'); };
$('btn-vue-dessus').onclick = () => vue?.vueDessus();

// ---------- Outils ----------
function texte(s) {
  const d = document.createElement('div');
  d.textContent = s ?? '';
  return d.innerHTML;
}

function badgeStock(a) {
  if (a.rupture) return `<span class="badge badge-danger">Rupture</span>`;
  if (a.alerte) return `<span class="badge badge-warn">${texte(a.stock)} ${texte(a.unite)} · à commander</span>`;
  return `<span class="badge badge-ok">${texte(a.stock)} ${texte(a.unite)}</span>`;
}

function chipsLieu(a) {
  if (!a.etagere_id) return '<span class="chip">Emplacement non renseigné</span>';
  let html = `<span class="chip bloc" style="background:var(--vert)">Bloc ${texte(a.bloc)}</span>`;
  html += `<span class="chip">Étagère ${texte(a.etagere)}</span>`;
  if (a.niveau) html += `<span class="chip">Niveau ${texte(a.niveau)}</span>`;
  if (a.case) html += `<span class="chip">${texte(a.case)}</span>`;
  return html;
}

// ---------- Affichage ----------
function afficherResultats(liste) {
  resultats = liste;
  if (!liste.length) {
    zoneResultats.innerHTML = '<div class="vide">Aucun article trouvé. Essayez un autre mot, ou une photo.</div>';
    return;
  }
  zoneResultats.innerHTML = liste.map((a, i) => `
    <article class="card resultat" data-i="${i}">
      <div class="vignette">${a.photo ? `<img src="${texte(a.photo)}" alt="">` : 'Pas de photo'}</div>
      <div>
        <div class="titre">${texte(a.designation)}</div>
        <div class="code">${texte(a.code)} · ${texte(a.categorie)} · ${badgeStock(a)}</div>
        <div class="lieu">${chipsLieu(a)}</div>
      </div>
    </article>`).join('');
  zoneResultats.querySelectorAll('.resultat').forEach((el) => {
    el.onclick = () => selectionner(Number(el.dataset.i));
  });
  selectionner(0);
}

function selectionner(i) {
  const a = resultats[i];
  if (!a) return;
  zoneResultats.querySelectorAll('.resultat').forEach((el) => el.classList.toggle('selectionne', Number(el.dataset.i) === i));
  fiche.innerHTML = `
    <div class="code">${texte(a.code)}</div>
    <div class="titre"><b>${texte(a.designation)}</b></div>
    <div class="grand-lieu">${a.etagere_id ? texte(a.emplacement) : 'Emplacement non renseigné'}</div>
    ${a.bloc_nom ? `<div class="code">Bloc ${texte(a.bloc)} : ${texte(a.bloc_nom)}</div>` : ''}
    <div style="margin-top:6px">${badgeStock(a)} · <a href="${texte(a.admin_url)}">Fiche article</a></div>
    ${a.photo ? `<img src="${texte(a.photo)}" alt="">` : ''}`;
  fiche.classList.add('visible');
  if (vue) {
    if (a.etagere_id) vue.montrer(a.etagere_id, a.niveau);
    else { vue.reinitialiser(); vue.vueEnsemble(); }
  }
}

// ---------- Recherche ----------
async function chercher() {
  const q = champ.value.trim();
  const photo = champPhoto.files[0];
  if (!q && !photo) return;
  zoneExplication.hidden = true;
  zoneResultats.innerHTML = '<div class="vide" style="display:flex;gap:10px;justify-content:center"><div class="spinner"></div> Recherche…</div>';

  const donnees = new FormData();
  donnees.append('q', q);
  if (photo) donnees.append('photo', photo);
  try {
    const rep = await fetch(cfg.apiRecherche, { method: 'POST', body: donnees, headers: { 'X-CSRFToken': cfg.csrf } });
    const json = await rep.json();
    if (!rep.ok) throw new Error(json.erreur || 'Erreur');
    if (json.explication) {
      zoneExplication.innerHTML = `✦ ${texte(json.explication)}<div class="termes">${json.termes.map((t) => `<span>${texte(t)}</span>`).join('')}</div>`;
      zoneExplication.hidden = false;
    }
    afficherResultats(json.resultats);
  } catch (err) {
    zoneResultats.innerHTML = `<div class="vide">${texte(err.message)}</div>`;
  }
}

form.addEventListener('submit', (e) => { e.preventDefault(); chercher(); });

$('btn-photo').onclick = () => champPhoto.click();
champPhoto.onchange = () => {
  const f = champPhoto.files[0];
  const apercu = $('photo-apercu');
  if (!f) { apercu.classList.remove('visible'); return; }
  apercu.querySelector('img').src = URL.createObjectURL(f);
  apercu.classList.add('visible');
  chercher();
};
$('photo-retirer').onclick = () => {
  champPhoto.value = '';
  $('photo-apercu').classList.remove('visible');
};

// Lien direct depuis l'administration : /?article=12
const params = new URLSearchParams(location.search);
if (params.get('q')) {
  champ.value = params.get('q');
  chercher();
} else if (params.get('article')) {
  const rep = await fetch(cfg.apiArticle + params.get('article') + '/');
  if (rep.ok) afficherResultats([await rep.json()]);
}
