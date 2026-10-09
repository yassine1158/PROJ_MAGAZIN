// Applique config.js (prix, liens), puis le menu, les onglets d'aperçu et les animations.
(function () {
  const c = window.MAGASTOCK || {};
  const message = encodeURIComponent('Bonjour, je souhaite acheter MagaStock. Code de mon ordinateur : ');
  const liens = {
    telechargement: c.telechargement,
    wave: c.wave,
    whatsapp: c.whatsapp && `https://wa.me/${c.whatsapp}?text=${message}`,
    'whatsapp-contact': c.whatsapp && `https://wa.me/${c.whatsapp}`,
    telephone: c.telephone && `tel:${c.telephone.replace(/[^+\d]/g, '')}`,
    email: c.email && `mailto:${c.email}`,
  };
  const textes = { telephone: c.telephone, email: c.email };

  // Liens : un lien non renseigné masque son bouton.
  document.querySelectorAll('[data-lien]').forEach(a => {
    const cle = a.dataset.lien;
    if (!liens[cle]) { a.remove(); return; }
    a.href = liens[cle];
    if (textes[cle]) a.textContent = textes[cle];
  });
  document.querySelectorAll('[data-masquer-si]').forEach(el => { if (liens[el.dataset.masquerSi]) el.remove(); });

  // Lien direct …/releases/latest/download/<fichier> : si la dernière version publiée n'a pas encore ce
  // fichier (versions d'avant le nom fixe), on donne directement son installateur .exe, sans page GitHub.
  const direct = /^https:\/\/github\.com\/([\w.-]+\/[\w.-]+)\/releases\/latest\/download\/([^/?#]+)$/.exec(c.telechargement || '');
  if (direct && window.fetch) {
    fetch(`https://api.github.com/repos/${direct[1]}/releases/latest`)
      .then(r => (r.ok ? r.json() : null))
      .then(version => {
        const fichiers = (version && version.assets) || [];
        if (fichiers.some(f => f.name === direct[2])) return;
        const exe = fichiers.find(f => /\.exe$/i.test(f.name) && /^https:\/\/github\.com\//.test(f.browser_download_url));
        if (exe) document.querySelectorAll('[data-lien="telechargement"]').forEach(a => { a.href = exe.browser_download_url; });
      })
      .catch(() => {});
  }
  if (c.prix) document.querySelectorAll('[data-prix]').forEach(el => { el.textContent = c.prix; });
  if (c.essaiJours) document.querySelectorAll('[data-essai]').forEach(el => { el.textContent = c.essaiJours; });
  document.getElementById('annee').textContent = new Date().getFullYear();

  // En-tête : fond blanc après le haut de page ; barre d'achat sur mobile.
  const entete = document.getElementById('entete');
  const barre = document.getElementById('barre-mobile');
  const auDefilement = () => {
    entete.classList.toggle('defile', scrollY > 30);
    barre.classList.toggle('visible', scrollY > innerHeight * 0.8);
  };
  addEventListener('scroll', auDefilement, { passive: true });
  auDefilement();

  // Menu mobile.
  const burger = document.getElementById('burger');
  const fermer = () => { entete.classList.remove('ouvert'); burger.setAttribute('aria-expanded', 'false'); };
  burger.addEventListener('click', () => {
    const ouvert = entete.classList.toggle('ouvert');
    burger.setAttribute('aria-expanded', String(ouvert));
    if (ouvert) entete.classList.add('defile');
    else auDefilement();
  });
  document.querySelectorAll('#nav a').forEach(a => a.addEventListener('click', fermer));

  // Onglets de l'aperçu.
  const image = document.getElementById('apercu-image');
  const titre = document.getElementById('apercu-titre');
  const legende = document.getElementById('apercu-legende');
  const onglets = [...document.querySelectorAll('.onglets button')];
  onglets.forEach(b => {
    new Image().src = `images/${b.dataset.image}.jpg`;  // préchargement
    b.addEventListener('click', () => {
      onglets.forEach(o => o.setAttribute('aria-selected', String(o === b)));
      image.style.opacity = 0;
      setTimeout(() => {
        image.src = `images/${b.dataset.image}.jpg`;
        image.alt = b.textContent;
        titre.textContent = `MagaStock — ${b.textContent}`;
        legende.textContent = b.dataset.legende;
        image.style.opacity = 1;
      }, 180);
    });
  });

  // Apparition des blocs au défilement.
  const blocs = document.querySelectorAll('.revele');
  if ('IntersectionObserver' in window) {
    const obs = new IntersectionObserver(entrees => entrees.forEach(e => {
      if (e.isIntersecting) { e.target.classList.add('vu'); obs.unobserve(e.target); }
    }), { rootMargin: '0px 0px -60px 0px', threshold: 0.08 });
    blocs.forEach((el, i) => { el.style.transitionDelay = `${(i % 4) * 60}ms`; obs.observe(el); });
  } else {
    document.documentElement.classList.add('sans-io');
  }
})();
