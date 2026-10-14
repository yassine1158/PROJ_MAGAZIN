// Lignes d'un bon d'entrée, de sortie ou de retour : choix de l'article en tapant (ou à la douchette), quantité, prix.
(() => {
  const { type, api, apiCreer, apiReste } = window.BON;
  const avecPrix = type === 'entree';
  const form = document.getElementById('form-bon');
  const corps = document.getElementById('lignes');
  const totalEl = document.getElementById('total');
  const bouton = form.querySelector('.boutons-bas [type=submit]');
  const raison = document.getElementById('raison-bloque');

  const echapper = (s) => { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; };
  const nombre = (s) => parseFloat(String(s ?? '').replace(/\s/g, '').replace(',', '.'));
  const formater = (n) => Math.round(n).toLocaleString('fr-FR');
  const formaterQte = (n) => n.toLocaleString('fr-FR', { maximumFractionDigits: 3 });
  const memeCode = (a, texte) => a.code.toLowerCase() === texte.toLowerCase();
  const champQte = (tr) => tr.querySelector('[name=quantite]');
  // Retour : par article, ce qui est encore sur le chantier choisi ({id: [quantité, texte]}) ; null tant qu'on ne sait pas.
  let surChantier = null;

  function majTotal() {
    if (!totalEl) return;
    let total = 0;
    corps.querySelectorAll('tr').forEach((tr) => {
      const q = nombre(champQte(tr).value);
      const p = nombre(tr.querySelector('[name=prix]')?.value);
      if (q > 0 && p > 0) total += q * p;
    });
    totalEl.textContent = formater(total);
  }

  // Texte sous l'article : stock, emplacement, ou ce qui ne va pas. Renvoie vrai si le stock manque.
  function majStock(tr, demandes) {
    const info = tr.querySelector('.info-stock');
    const a = tr._article;
    if (!a) {
      if (!tr._aChoisir) { info.textContent = ''; info.classList.remove('manque'); }
      return false;
    }
    const brut = champQte(tr).value.trim();
    const q = nombre(brut);
    const stock = parseFloat(a.stock);
    const total = demandes[a.id] || 0;
    if (type === 'retour' && surChantier) return majRetour(tr, a, brut, q, total);
    const manque = type === 'sortie' && q > 0 && total > stock;
    const reste = stock > 0 ? `il reste ${a.stock_txt} ${a.unite}` : 'il n\'en reste plus';
    let texte = `En stock : ${a.stock_txt} ${a.unite}`;
    if (a.emplacement) texte += ` · ${a.emplacement}`;
    if (manque && q > stock) {
      texte = `Stock insuffisant : ${reste}, vous en demandez ${formaterQte(q)}. Baissez la quantité.`;
    } else if (manque) {
      texte = `Stock insuffisant : cet article est sur plusieurs lignes (${formaterQte(total)} en tout), ${reste}.`;
    }
    const codeBarre = /^\d{8,}$/.test(brut);
    if (codeBarre) texte = 'Ceci ressemble à un code-barres : tapez la quantité.';
    info.textContent = texte;
    info.classList.toggle('manque', manque || codeBarre);
    return manque;
  }

  // Retour : ce qui est encore sur le chantier, et où le ranger. Renvoie vrai si on rend plus que ce qui est parti.
  function majRetour(tr, a, brut, q, total) {
    const info = tr.querySelector('.info-stock');
    const [brutReste, resteTxt] = surChantier[a.id] || ['0', '0'];
    const reste = parseFloat(brutReste);
    const manque = q > 0 && total > reste;
    let texte = reste > 0 ? `Encore sur le chantier : ${resteTxt} ${a.unite}` : 'Pas parti sur ce chantier (ou déjà revenu).';
    if (reste > 0 && a.emplacement) texte += ` · à ranger : ${a.emplacement}`;
    if (manque && reste <= 0) {
      texte = 'Cet article n\'est pas parti sur ce chantier (ou il est déjà revenu) : retirez la ligne.';
    } else if (manque && q > reste) {
      texte = `Trop rendu : il n'en reste que ${resteTxt} ${a.unite} sur le chantier, vous en rendez ${formaterQte(q)}.`;
    } else if (manque) {
      texte = `Trop rendu : cet article est sur plusieurs lignes (${formaterQte(total)} en tout), il n'en reste que ${resteTxt} ${a.unite} sur le chantier.`;
    }
    const codeBarre = /^\d{8,}$/.test(brut);
    if (codeBarre) texte = 'Ceci ressemble à un code-barres : tapez la quantité.';
    info.textContent = texte;
    info.classList.toggle('manque', manque || codeBarre);
    return manque;
  }

  // Sortie : pas d'enregistrement tant qu'une ligne demande plus que le stock (toutes lignes d'un article réunies).
  // Retour : de même tant qu'une ligne rend plus que ce qui est encore sur le chantier.
  function majStocks() {
    const lignes = [...corps.querySelectorAll('tr')];
    const demandes = {};
    lignes.forEach((tr) => {
      const q = nombre(champQte(tr).value);
      if (tr._article && q > 0) demandes[tr._article.id] = (demandes[tr._article.id] || 0) + q;
    });
    const manques = lignes.filter((tr) => majStock(tr, demandes)).length;
    if (type === 'entree') return;
    bouton.disabled = manques > 0;
    raison.hidden = !manques;
    if (type === 'retour') {
      raison.textContent = manques > 1
        ? `${manques} lignes rendent plus que ce qui est parti sur le chantier : corrigez les quantités pour pouvoir enregistrer.`
        : 'Une ligne rend plus que ce qui est parti sur le chantier : corrigez la quantité pour pouvoir enregistrer.';
      return;
    }
    raison.textContent = manques > 1
      ? `${manques} lignes demandent plus que le stock : baissez les quantités pour pouvoir enregistrer.`
      : 'Une ligne demande plus que le stock : baissez la quantité pour pouvoir enregistrer.';
  }

  function choisir(tr, a, focus = true) {
    tr._article = a;
    tr._aChoisir = false;
    const saisie = tr.querySelector('.saisie-article');
    saisie.classList.remove('a-choisir');
    saisie.value = `${a.designation} (${a.code})`;
    tr.querySelector('[name=article]').value = a.id;
    tr.querySelector('[name=article_texte]').value = saisie.value;
    tr.querySelector('.unite').textContent = a.unite;
    const prix = tr.querySelector('[name=prix]');
    if (prix && !prix.value && parseFloat(a.prix) > 0) prix.value = Math.round(parseFloat(a.prix));
    majStocks();
    majTotal();
    if (focus) champQte(tr).focus();
  }

  function ajouterLigne(init = {}) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="article">
        <div class="choix-article">
          <input type="text" class="saisie-article" placeholder="Tapez le nom ou le code de l'article…" aria-label="Article">
          <input type="hidden" name="article">
          <input type="hidden" name="article_texte">
          <div class="liste"></div>
        </div>
        <div class="info-stock" aria-live="polite"></div>
      </td>
      <td class="qte">
        <div style="display:flex;align-items:center;gap:6px">
          <input name="quantite" inputmode="decimal" placeholder="Qté" aria-label="Quantité" value="${echapper(init.quantite)}">
          <span class="unite muted petit"></span>
        </div>
      </td>
      ${avecPrix ? `<td class="prix"><input name="prix" inputmode="decimal" placeholder="Prix" aria-label="Prix unitaire" value="${echapper(init.prix_saisi)}"></td>` : ''}
      <td class="suppr"><button type="button" class="btn btn-icone btn-rouge" title="Retirer">
        <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>
      </button></td>`;
    corps.appendChild(tr);

    const saisie = tr.querySelector('.saisie-article');
    const texteCache = tr.querySelector('[name=article_texte]');
    const liste = tr.querySelector('.liste');
    const info = tr.querySelector('.info-stock');
    let options = [];        // articles proposés pour le texte « demande »
    let active = -1;         // option surlignée (options.length = « Créer l'article »)
    let navigue = false;     // flèches utilisées : Entrée prend l'option surlignée
    let creerPossible = false;
    let minuteur = null;
    let controleur = null;   // recherche en cours (annulée si une autre part)
    let requete = null;
    let demande = null;      // texte de la dernière recherche lancée

    function fermer() { liste.classList.remove('ouverte'); }

    function afficher(q) {
      const html = options.map((a, i) => `
          <div class="option${i === active ? ' active' : ''}" data-i="${i}">
            <div><b>${echapper(a.designation)}</b><small>${echapper(a.code)}${a.emplacement ? ' · ' + echapper(a.emplacement) : ''}</small></div>
            <span class="badge ${parseFloat(a.stock) > 0 ? 'badge-ok' : 'badge-danger'}">${echapper(a.stock_txt)} ${echapper(a.unite)}</span>
          </div>`);
      if (!options.length) html.push('<div class="option muted">Aucun article trouvé</div>');
      if (creerPossible) {
        html.push(`<div class="option option-creer" data-i="${options.length}">+ Créer l'article « ${echapper(q)} »</div>`);
      }
      liste.innerHTML = html.join('');
      liste.classList.add('ouverte');
    }

    function chercher() {
      clearTimeout(minuteur);
      minuteur = null;
      controleur?.abort();
      const ctrl = controleur = new AbortController();
      const q = saisie.value.trim();
      demande = q;
      requete = fetch(`${api}?q=${encodeURIComponent(q)}`, { signal: ctrl.signal, headers: { Accept: 'application/json' } })
        .then((rep) => rep.json())
        .then((donnees) => {
          if (ctrl !== controleur) return;  // réponse arrivée trop tard : une autre recherche l'a remplacée
          options = donnees.articles || [];
          active = options.length ? 0 : -1;
          navigue = false;
          creerPossible = avecPrix && q.length >= 2
            && !options.some((a) => memeCode(a, q) || a.designation.toLowerCase() === q.toLowerCase());
          if (document.activeElement === saisie) afficher(q);
        })
        .catch((err) => {
          if (ctrl !== controleur || err.name === 'AbortError') return;
          options = [];
          demande = null;
          liste.innerHTML = '<div class="option muted">Recherche impossible : vérifiez la connexion, ou rechargez la page.</div>';
          liste.classList.add('ouverte');
        });
      return requete;
    }

    // Résultats pour le texte tapé, en attendant la recherche en cours (ou en la lançant tout de suite).
    async function resultats() {
      if (demande !== saisie.value.trim()) chercher();
      let r;
      do { r = requete; await r; } while (r !== requete);
      return options;
    }

    function signaler() {
      tr._aChoisir = true;
      saisie.classList.add('a-choisir');
      const texte = saisie.value.trim();
      info.textContent = !texte ? 'Choisissez un article dans la liste.'
        : options.length || demande !== texte ? 'Choisissez dans la liste : cliquez sur le bon article.'
          : `Aucun article ne correspond : vérifiez le nom ou le code${avecPrix ? ', ou créez-le' : ''}.`;
      info.classList.add('manque');
    }

    // Article tapé mais pas choisi (sortie du champ, envoi) : choisi d'office si un seul article correspond.
    async function resoudre() {
      if (tr._article) return true;
      const texte = saisie.value.trim();
      if (!texte) { signaler(); return false; }
      const trouves = await resultats();
      if (tr._article) return true;
      if (saisie.value.trim() !== texte) return false;
      const a = trouves.find((x) => memeCode(x, texte)) || (trouves.length === 1 ? trouves[0] : null);
      if (a) { choisir(tr, a, false); return true; }
      signaler();
      return false;
    }
    tr._resoudre = resoudre;

    // Entrée (clavier ou douchette) : le code exact s'il existe, sinon le premier article proposé.
    async function valider() {
      if (tr._article) { champQte(tr).focus(); return; }
      const texte = saisie.value.trim();
      if (!texte && !navigue) return;
      const trouves = await resultats();
      if (tr._article || saisie.value.trim() !== texte) return;  // choisi entre-temps, ou on a continué à taper
      if (navigue && active === trouves.length && creerPossible) { ouvrirCreation(tr, texte); return; }
      const a = (navigue && trouves[active]) || trouves.find((x) => memeCode(x, texte)) || trouves[0];
      if (a) { choisir(tr, a); fermer(); return; }
      afficher(texte);
      signaler();
    }

    function prendre(i) {
      if (i < options.length) { choisir(tr, options[i]); fermer(); } else if (creerPossible) { fermer(); ouvrirCreation(tr, saisie.value.trim()); }
    }

    saisie.addEventListener('input', () => {
      // La liste affichée correspond à l'ancien texte : on la vide pour qu'Entrée ne prenne pas un mauvais article.
      tr._article = null;
      tr._aChoisir = false;
      saisie.classList.remove('a-choisir');
      tr.querySelector('[name=article]').value = '';
      texteCache.value = saisie.value;
      controleur?.abort();
      controleur = null;
      options = [];
      active = -1;
      navigue = false;
      demande = null;
      fermer();
      liste.innerHTML = '';
      majStocks();
      clearTimeout(minuteur);
      minuteur = setTimeout(chercher, 200);
    });
    saisie.addEventListener('focus', () => { if (!tr._article) chercher(); });
    saisie.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();  // jamais d'envoi du formulaire depuis ce champ
        valider();
        return;
      }
      if (!liste.classList.contains('ouverte')) return;
      const nb = options.length + (creerPossible ? 1 : 0);
      if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && nb) {
        e.preventDefault();
        navigue = true;
        active = Math.max(0, Math.min(nb - 1, active + (e.key === 'ArrowDown' ? 1 : -1)));
        liste.querySelectorAll('.option[data-i]').forEach((o) => o.classList.toggle('active', Number(o.dataset.i) === active));
        liste.querySelector('.option.active')?.scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Escape') {
        fermer();
      }
    });
    liste.addEventListener('mousedown', (e) => {
      e.preventDefault();  // garde le curseur dans le champ (pas de « blur »)
      const o = e.target.closest('.option[data-i]');
      if (o) prendre(Number(o.dataset.i));
    });
    saisie.addEventListener('blur', () => {
      setTimeout(fermer, 150);
      if (!tr._creation && saisie.value.trim() && !tr._article) resoudre();
    });

    champQte(tr).addEventListener('input', () => { majStocks(); majTotal(); });
    tr.querySelector('[name=prix]')?.addEventListener('input', majTotal);
    tr.querySelector('.suppr button').addEventListener('click', () => {
      tr.remove();
      if (!corps.children.length) ajouterLigne();
      majStocks();
      majTotal();
    });

    if (init.id) {
      choisir(tr, init, false);
    } else if (init.texte) {
      // Ligne revenue du serveur sans article choisi : le texte et la quantité sont gardés.
      saisie.value = init.texte;
      texteCache.value = init.texte;
      signaler();
    } else if (corps.children.length > 1) {
      saisie.focus();
    }
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

  // Envoi : les articles tapés mais pas choisis sont d'abord cherchés ; s'il en reste, on montre la ligne.
  form.addEventListener('submit', async (e) => {
    const aVoir = [...corps.querySelectorAll('tr')].filter((tr) => !tr._article
      && (tr.querySelector('.saisie-article').value.trim() || champQte(tr).value.trim()));
    if (!aVoir.length) return;
    e.preventDefault();
    const resolus = await Promise.all(aVoir.map((tr) => tr._resoudre()));
    majStocks();
    if (resolus.every(Boolean) && !bouton.disabled) {
      form.requestSubmit();
    } else {
      const tr = aVoir[resolus.indexOf(false)] || aVoir[0];
      tr.querySelector('.saisie-article').scrollIntoView({ block: 'center' });
      tr.querySelector('.saisie-article').focus({ preventScroll: true });
    }
  });

  // Article inconnu pendant une entrée : petite fenêtre de création, puis l'article est choisi dans la ligne.
  const fenetre = document.getElementById('creer-article');
  let ligneEnCreation = null;
  function ouvrirCreation(tr, texte) {
    if (!fenetre || fenetre.open) return;
    ligneEnCreation = tr;
    tr._creation = true;
    fenetre.querySelector('.message').hidden = true;
    fenetre.querySelector('#ca-designation').value = texte;
    fenetre.showModal();
    fenetre.querySelector(fenetre.querySelector('#ca-categorie').value ? '#ca-designation' : '#ca-categorie').focus();
  }
  if (fenetre) {
    const formCreer = fenetre.querySelector('form');
    const erreur = fenetre.querySelector('.message');
    fenetre.querySelector('[data-fermer]').addEventListener('click', () => fenetre.close());
    fenetre.addEventListener('close', () => {
      const tr = ligneEnCreation;
      ligneEnCreation = null;
      if (!tr) return;
      tr._creation = false;
      if (!tr._article) tr.querySelector('.saisie-article').focus();
    });
    formCreer.addEventListener('submit', async (e) => {
      e.preventDefault();
      const envoi = formCreer.querySelector('[type=submit]');
      envoi.disabled = true;
      erreur.hidden = true;
      try {
        const rep = await fetch(apiCreer, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json', Accept: 'application/json',
            'X-CSRFToken': form.querySelector('[name=csrfmiddlewaretoken]').value,
          },
          body: JSON.stringify({
            designation: fenetre.querySelector('#ca-designation').value,
            unite: fenetre.querySelector('#ca-unite').value,
            categorie: fenetre.querySelector('#ca-categorie').value,
          }),
        });
        const donnees = await rep.json().catch(() => ({}));
        if (!rep.ok || !donnees.article) {
          erreur.textContent = (donnees.erreurs || ['Création impossible : rechargez la page puis réessayez.']).join(' ');
          erreur.hidden = false;
          return;
        }
        const tr = ligneEnCreation;
        ligneEnCreation = null;
        fenetre.close();
        if (tr) { tr._creation = false; choisir(tr, donnees.article); }
      } catch {
        erreur.textContent = 'Création impossible : vérifiez la connexion puis réessayez.';
        erreur.hidden = false;
      } finally {
        envoi.disabled = false;
      }
    });
  }

  // Sortie : chantier ou engin inconnu → proposition (« Vouliez-vous dire… ») et case « Oui, créer… ».
  document.querySelectorAll('[data-remplir]').forEach((b) => b.addEventListener('click', () => {
    const champ = document.getElementById(b.dataset.remplir);
    champ.value = b.dataset.valeur;
    champ.dispatchEvent(new Event('input', { bubbles: true }));
    champ.focus();
  }));
  document.querySelectorAll('.proposition-nom').forEach((boite) => {
    const champ = document.getElementById(boite.id.replace('proposition-', ''));
    const memeNom = () => champ.value.split(/\s+/).join(' ').trim().toLowerCase() === boite.dataset.nom.toLowerCase();
    champ.addEventListener('input', () => {
      boite.hidden = !memeNom();
      const caseCreer = boite.querySelector('input[type=checkbox]');
      if (boite.hidden && caseCreer) caseCreer.checked = false;
    });
  });

  // Retour : ce qui est encore sur le chantier, relu quand le chantier change.
  const champChantier = document.getElementById('chantier');
  let minuteurChantier = null;
  let controleurChantier = null;
  function chargerChantier() {
    clearTimeout(minuteurChantier);
    controleurChantier?.abort();
    const nom = champChantier.value.trim();
    if (!nom) { surChantier = null; majStocks(); return; }
    const ctrl = controleurChantier = new AbortController();
    fetch(`${apiReste}?chantier=${encodeURIComponent(nom)}`, { signal: ctrl.signal, headers: { Accept: 'application/json' } })
      .then((rep) => rep.json())
      .then((donnees) => {
        if (ctrl !== controleurChantier) return;
        surChantier = donnees.chantier ? donnees.reste : null;
        majStocks();
      })
      .catch(() => {
        if (ctrl !== controleurChantier) return;
        surChantier = null;  // le serveur vérifiera à l'enregistrement
        majStocks();
      });
  }
  if (type === 'retour' && champChantier) {
    champChantier.addEventListener('input', () => {
      controleurChantier?.abort();
      controleurChantier = null;
      surChantier = null;
      majStocks();
      clearTimeout(minuteurChantier);
      minuteurChantier = setTimeout(chargerChantier, 300);
    });
    chargerChantier();
    window.addEventListener('pageshow', (e) => { if (e.persisted) chargerChantier(); });
  }

  const initiales = JSON.parse(document.getElementById('lignes-initiales').textContent);
  if (initiales.length) initiales.forEach((l) => ajouterLigne(l)); else ajouterLigne();
  majStocks();
  majTotal();
  window.addEventListener('pageshow', (e) => { if (e.persisted) majStocks(); });
})();
