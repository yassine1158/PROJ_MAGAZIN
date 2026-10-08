// Applique config.js : prix, liens, et masque les boutons dont le lien n'est pas encore renseigné.
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

  document.querySelectorAll('[data-lien]').forEach(a => {
    const cle = a.dataset.lien;
    if (!liens[cle]) { a.remove(); return; }
    a.href = liens[cle];
    if (textes[cle]) a.textContent = textes[cle];
  });
  document.querySelectorAll('[data-masquer-si]').forEach(el => { if (liens[el.dataset.masquerSi]) el.remove(); });
  if (c.prix) document.querySelectorAll('[data-prix]').forEach(el => { el.textContent = c.prix; });
  if (c.essaiJours) document.querySelectorAll('[data-essai]').forEach(el => { el.textContent = c.essaiJours; });
  document.getElementById('annee').textContent = new Date().getFullYear();
})();
