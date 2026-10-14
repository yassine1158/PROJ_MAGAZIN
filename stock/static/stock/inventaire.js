// Comptage d'un inventaire : chaque quantité est enregistrée dès qu'on quitte la case (ou avec « Enregistrer »).
// Si le réseau coupe, la saisie reste dans la case, marquée « non enregistrée », jusqu'au prochain essai.
(() => {
  const page = document.getElementById('comptage');
  if (!page) return;
  const url = page.dataset.url;
  const formulaires = [...page.querySelectorAll('form.groupe-comptage')];
  const champs = [...page.querySelectorAll('.ligne-comptage input[name^="compte_"]')];
  const recherche = document.getElementById('filtre-texte');
  const nonComptes = document.getElementById('filtre-non-comptes');
  const alerte = document.getElementById('alerte-comptage');

  const normaliser = (s) => (s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const nombre = (s) => {
    const t = String(s ?? '').replace(/\s/g, '').replace(',', '.');
    return /^(\d+\.?\d*|\.\d+)$/.test(t) ? parseFloat(t) : NaN;
  };
  const formaterQte = (n) => n.toLocaleString('fr-FR', { maximumFractionDigits: 3 });
  const ligneDe = (champ) => champ.closest('.ligne-comptage');
  const avantDe = (champ) => champ.form.elements[`avant_${ligneDe(champ).dataset.ligne}`];
  const modifie = (champ) => champ.value.trim() !== avantDe(champ).value.trim();
  const jeton = (form) => form.querySelector('[name=csrfmiddlewaretoken]').value;

  page.querySelectorAll('.ligne-comptage').forEach((l) => { l._texte = normaliser(l.dataset.texte); });

  function montrerAlerte(texte) {
    alerte.querySelector('div').textContent = texte;
    alerte.hidden = !texte;
  }

  // État affiché à droite de la case : vide, « … » (envoi), ✓ (enregistré), ! (erreur ou pas encore enregistré).
  function etat(champ, classe, signe, message) {
    const ligne = ligneDe(champ);
    ligne.classList.remove('en-cours', 'ok', 'erreur', 'attente');
    if (classe) ligne.classList.add(classe);
    ligne.querySelector('.lc-etat').textContent = signe;
    const erreur = ligne.querySelector('.lc-erreur');
    erreur.textContent = message || '';
    erreur.hidden = !message;
  }

  function etatEnregistre(champ, heure) {
    const ligne = ligneDe(champ);
    const compte = avantDe(champ).value.trim() !== '';
    ligne.classList.toggle('compte', compte);
    etat(champ, compte ? 'ok' : '', compte ? '✓' : '');
    if (heure) ligne.querySelector('.lc-etat').title = compte ? `Enregistré à ${heure}` : '';
  }

  function majEcart(champ) {
    const ligne = ligneDe(champ);
    const zone = ligne.querySelector('.lc-ecart');
    if (!zone) return;
    const compte = nombre(champ.value);
    const theorique = nombre(ligne.dataset.theorique);
    zone.classList.remove('ecart-plus', 'ecart-moins');
    if (Number.isNaN(compte) || Number.isNaN(theorique)) { zone.textContent = ''; return; }
    const ecart = Math.round((compte - theorique) * 1000) / 1000;
    zone.textContent = ecart ? `écart ${ecart > 0 ? '+' : '−'}${formaterQte(Math.abs(ecart))}` : 'juste';
    if (ecart) zone.classList.add(ecart > 0 ? 'ecart-plus' : 'ecart-moins');
  }

  function majCompteurs(comptes, total) {
    if (comptes !== undefined) {
      document.getElementById('nb-comptes').textContent = comptes;
      document.getElementById('nb-total').textContent = total;
      document.getElementById('barre-comptes').style.width = `${total ? Math.round(100 * comptes / total) : 0}%`;
    }
    formulaires.forEach((form) => {
      const liste = [...form.querySelectorAll('input[name^="compte_"]')];
      const faits = liste.filter((c) => avantDe(c).value.trim() !== '').length;
      form.querySelector('.compteur-groupe').innerHTML = '';
      const b = document.createElement('b');
      b.textContent = faits;
      form.querySelector('.compteur-groupe').append(b, ` / ${liste.length}`);
    });
  }

  // Les envois partent l'un après l'autre : une valeur plus ancienne n'écrase jamais une plus récente.
  // Par paquets de 300 cases : le serveur refuse un envoi de plus de 1000 champs.
  let file = Promise.resolve(true);
  const enregistrer = (liste) => {
    let toutBon = true;
    for (let i = 0; i < Math.max(liste.length, 1); i += 300) {
      const paquet = liste.slice(i, i + 300);
      file = file.then(() => envoyer(paquet), () => envoyer(paquet)).then((ok) => { toutBon = toutBon && ok; return ok; });
    }
    return file.then(() => toutBon);
  };

  async function envoyer(liste) {
    const aEnvoyer = [];
    liste.forEach((c) => { if (modifie(c)) aEnvoyer.push([c, c.value]); else etatEnregistre(c); });
    if (!aEnvoyer.length) return true;
    const donnees = new FormData();
    donnees.append('csrfmiddlewaretoken', jeton(aEnvoyer[0][0].form));
    aEnvoyer.forEach(([c, v]) => { donnees.append(c.name, v); etat(c, 'en-cours', '…'); });
    let reponse;
    try {
      const r = await fetch(url, {
        method: 'POST', body: donnees, credentials: 'same-origin',
        headers: { 'X-Requested-With': 'fetch', Accept: 'application/json' },
      });
      reponse = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(reponse.erreur || (r.status === 403
        ? 'Votre session a expiré : rechargez la page (vos chiffres non enregistrés restent à retaper).'
        : 'Le serveur a refusé l\'enregistrement : rechargez la page.'));
    } catch (e) {
      aEnvoyer.forEach(([c]) => etat(c, 'attente', '!', 'Pas encore enregistré : touchez « Enregistrer » pour réessayer.'));
      montrerAlerte(e instanceof TypeError ? 'Pas de connexion avec le logiciel : vos chiffres restent dans les cases. '
        + 'Touchez « Enregistrer » quand la connexion revient.' : e.message);
      return false;
    }
    montrerAlerte('');
    let toutBon = true;
    aEnvoyer.forEach(([c, v]) => {
      const id = ligneDe(c).dataset.ligne;
      const erreur = reponse.erreurs?.[id];
      const fait = reponse.lignes?.[id];
      if (erreur) { toutBon = false; etat(c, 'erreur', '!', erreur); return; }
      if (!fait) return;
      avantDe(c).value = fait.valeur;
      if (c.value === v) c.value = fait.valeur;  // « 12.50 » s'affiche « 12,5 »
      if (modifie(c)) { etat(c, '', ''); return; }  // retapé pendant l'envoi : repartira en quittant la case
      etatEnregistre(c, fait.heure);
      majEcart(c);
    });
    majCompteurs(reponse.comptes, reponse.total);
    return toutBon;
  }

  // Case suivante (visible) : la touche Entrée fait passer à l'article suivant, comme sur une feuille.
  function suivant(champ) {
    const visibles = champs.filter((c) => !ligneDe(c).hidden && !c.closest('form').hidden);
    return visibles[visibles.indexOf(champ) + 1];
  }

  champs.forEach((champ) => {
    champ.addEventListener('change', () => enregistrer([champ]));
    champ.addEventListener('input', () => {
      majEcart(champ);
      const ligne = ligneDe(champ);
      if (ligne.classList.contains('ok') && modifie(champ)) etat(champ, '', '');
    });
    champ.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const prochain = suivant(champ);
      if (prochain) { prochain.focus(); prochain.select(); } else { champ.blur(); }
    });
    champ.addEventListener('focus', () => champ.select());
    majEcart(champ);
  });

  formulaires.forEach((form) => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const bouton = form.querySelector('.groupe-pied .btn');
      const info = form.querySelector('.etat-groupe');
      bouton.disabled = true;
      const liste = [...form.querySelectorAll('input[name^="compte_"]')];
      const ok = await enregistrer(liste);
      bouton.disabled = false;
      const restent = liste.filter(modifie).length;
      info.textContent = ok && !restent ? 'Tout est enregistré.' : 'Des cases ne sont pas enregistrées (en rouge).';
      info.classList.toggle('erreur-champ', !(ok && !restent));
    });
  });

  // Recherche et filtre « non comptés » : on masque des lignes, rien n'est perdu.
  function filtrer() {
    const mots = normaliser(recherche.value).split(/\s+/).filter(Boolean);
    const seulementNon = nonComptes.checked;
    let total = 0;
    formulaires.forEach((form) => {
      let visibles = 0;
      form.querySelectorAll('.ligne-comptage').forEach((ligne) => {
        const champ = ligne.querySelector('input[name^="compte_"]');
        const compte = avantDe(champ).value.trim() !== '' && !modifie(champ);
        const voir = mots.every((m) => ligne._texte.includes(m)) && !(seulementNon && compte);
        ligne.hidden = !voir;
        visibles += voir;
      });
      form.hidden = !visibles;
      total += visibles;
    });
    document.getElementById('rien-a-afficher').hidden = total > 0;
  }
  recherche.addEventListener('input', filtrer);
  nonComptes.addEventListener('change', filtrer);

  const allerA = document.getElementById('aller-a');
  allerA?.addEventListener('change', () => {
    const groupe = document.getElementById(allerA.value);
    if (groupe) {
      if (groupe.hidden) { recherche.value = ''; nonComptes.checked = false; filtrer(); }
      groupe.scrollIntoView({ behavior: 'smooth', block: 'start' });
      groupe.querySelector('input[name^="compte_"]')?.focus({ preventScroll: true });
    }
    allerA.value = '';
  });

  // Un lien (Valider, menu…) touché juste après une saisie : on enregistre d'abord, puis on change de page.
  document.addEventListener('click', async (e) => {
    const lien = e.target.closest('a[href]');
    if (!lien || lien.target === '_blank' || e.ctrlKey || e.metaKey || !champs.some(modifie)) return;
    e.preventDefault();
    if (await enregistrer(champs.filter(modifie))) { window.location.href = lien.href; return; }
    const enRouge = champs.find((c) => modifie(c) && !ligneDe(c).hidden);
    if (!alerte.querySelector('div').textContent) {
      montrerAlerte('Des quantités ne sont pas enregistrées (cases en rouge) : corrigez-les ou videz-les, puis recommencez.');
    }
    if (enRouge) {
      enRouge.scrollIntoView({ block: 'center' });
      enRouge.focus({ preventScroll: true });
    }
  });

  // Quitter la page avec des chiffres pas encore enregistrés : on prévient.
  window.addEventListener('beforeunload', (e) => {
    if (champs.some(modifie)) { e.preventDefault(); e.returnValue = ''; }
  });
})();
