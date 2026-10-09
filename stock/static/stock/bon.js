// Lignes d'un bon d'entrée ou de sortie : choix de l'article en tapant, quantité, prix.
(() => {
  const { type, api } = window.BON;
  const avecPrix = type === 'entree';
  const corps = document.getElementById('lignes');
  const totalEl = document.getElementById('total');

  const echapper = (s) => { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; };
  const nombre = (s) => parseFloat(String(s ?? '').replace(/\s/g, '').replace(',', '.'));
  const formater = (n) => Math.round(n).toLocaleString('fr-FR');

  function majTotal() {
    if (!totalEl) return;
    let total = 0;
    corps.querySelectorAll('tr').forEach((tr) => {
      const q = nombre(tr.querySelector('[name=quantite]').value);
      const p = nombre(tr.querySelector('[name=prix]')?.value);
      if (q > 0 && p > 0) total += q * p;
    });
    totalEl.textContent = formater(total);
  }

  function majStock(tr) {
    const info = tr.querySelector('.info-stock');
    const a = tr._article;
    if (!a) { info.textContent = ''; info.classList.remove('manque'); return; }
    const q = nombre(tr.querySelector('[name=quantite]').value);
    const stock = parseFloat(a.stock);
    let texte = `En stock : ${a.stock_txt} ${a.unite}`;
    if (a.emplacement) texte += ` · ${a.emplacement}`;
    const manque = type === 'sortie' && q > stock;
    if (manque) texte = `Stock insuffisant : il reste ${a.stock_txt} ${a.unite}`;
    const codeBarre = /^\d{8,}$/.test(tr.querySelector('[name=quantite]').value.trim());
    if (codeBarre) texte = 'Ceci ressemble à un code-barres : tapez la quantité.';
    info.textContent = texte;
    info.classList.toggle('manque', manque || codeBarre);
  }

  function choisir(tr, a) {
    tr._article = a;
    tr.querySelector('[name=article]').value = a.id;
    tr.querySelector('.saisie-article').value = `${a.designation} (${a.code})`;
    tr.querySelector('.unite').textContent = a.unite;
    const prix = tr.querySelector('[name=prix]');
    if (prix && !prix.value && parseFloat(a.prix) > 0) prix.value = Math.round(parseFloat(a.prix));
    majStock(tr);
    majTotal();
    tr.querySelector('[name=quantite]').focus();
  }

  function ajouterLigne(init = {}) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="article">
        <div class="choix-article">
          <input type="text" class="saisie-article" placeholder="Tapez le nom ou le code de l'article…">
          <input type="hidden" name="article">
          <div class="liste"></div>
        </div>
        <div class="info-stock"></div>
      </td>
      <td class="qte">
        <div style="display:flex;align-items:center;gap:6px">
          <input name="quantite" inputmode="decimal" placeholder="Qté" value="${echapper(init.quantite)}">
          <span class="unite muted petit"></span>
        </div>
      </td>
      ${avecPrix ? `<td class="prix"><input name="prix" inputmode="decimal" placeholder="Prix" value="${echapper(init.prix_saisi)}"></td>` : ''}
      <td class="suppr"><button type="button" class="btn btn-icone btn-rouge" title="Retirer">
        <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>
      </button></td>`;
    corps.appendChild(tr);

    const saisie = tr.querySelector('.saisie-article');
    const liste = tr.querySelector('.liste');
    let options = [];
    let active = -1;
    let minuteur = null;

    async function chercher() {
      const q = saisie.value.trim();
      const rep = await fetch(`${api}?q=${encodeURIComponent(q)}`);
      options = (await rep.json()).articles;
      active = options.length ? 0 : -1;
      liste.innerHTML = options.length
        ? options.map((a, i) => `
            <div class="option${i === 0 ? ' active' : ''}" data-i="${i}">
              <div><b>${echapper(a.designation)}</b><small>${echapper(a.code)}${a.emplacement ? ' · ' + echapper(a.emplacement) : ''}</small></div>
              <span class="badge ${parseFloat(a.stock) > 0 ? 'badge-ok' : 'badge-danger'}">${echapper(a.stock_txt)} ${echapper(a.unite)}</span>
            </div>`).join('')
        : '<div class="option muted">Aucun article trouvé</div>';
      liste.classList.add('ouverte');
    }

    saisie.addEventListener('input', () => {
      tr._article = null;
      tr.querySelector('[name=article]').value = '';
      majStock(tr);
      clearTimeout(minuteur);
      minuteur = setTimeout(chercher, 200);
    });
    saisie.addEventListener('focus', () => { if (!tr._article) chercher(); });
    saisie.addEventListener('keydown', (e) => {
      if (!liste.classList.contains('ouverte')) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        active = Math.max(0, Math.min(options.length - 1, active + (e.key === 'ArrowDown' ? 1 : -1)));
        liste.querySelectorAll('.option').forEach((o, i) => o.classList.toggle('active', i === active));
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (options[active]) { choisir(tr, options[active]); liste.classList.remove('ouverte'); }
      } else if (e.key === 'Escape') {
        liste.classList.remove('ouverte');
      }
    });
    liste.addEventListener('mousedown', (e) => {
      const o = e.target.closest('.option[data-i]');
      if (!o) return;
      e.preventDefault();
      choisir(tr, options[Number(o.dataset.i)]);
      liste.classList.remove('ouverte');
    });
    saisie.addEventListener('blur', () => setTimeout(() => liste.classList.remove('ouverte'), 150));

    tr.querySelector('[name=quantite]').addEventListener('input', () => { majStock(tr); majTotal(); });
    tr.querySelector('[name=prix]')?.addEventListener('input', majTotal);
    tr.querySelector('.suppr button').addEventListener('click', () => {
      tr.remove();
      if (!corps.children.length) ajouterLigne();
      majTotal();
    });

    if (init.id) choisir(tr, init); else if (corps.children.length > 1) saisie.focus();
    return tr;
  }

  document.getElementById('ajouter-ligne').addEventListener('click', () => ajouterLigne());

  // Entrée dans le champ quantité : passe à la ligne suivante au lieu d'envoyer le formulaire.
  corps.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.target.name === 'quantite' || e.target.name === 'prix')) {
      e.preventDefault();
      ajouterLigne();
    }
  });

  const initiales = JSON.parse(document.getElementById('lignes-initiales').textContent);
  if (initiales.length) initiales.forEach((l) => ajouterLigne(l)); else ajouterLigne();
  majTotal();
})();
