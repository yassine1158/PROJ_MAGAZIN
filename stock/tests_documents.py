"""Documents : bons imprimés (PDF) et montant en lettres."""
import re
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Paragraph

from . import services
from .models import (
    Article, BonEntree, BonSortie, Categorie, Chantier, Fournisseur, LigneEntree, LigneSortie, Parametres,
)
from .utils import montant_en_lettres

CODE_LONG = 'FILTRE-HUILE-CAT-320D-1R0739XX'


def _decoder(chaine):
    def remplacer(m):
        s = m.group(1)
        if s[:1].isdigit():
            return bytes([int(s, 8)])
        return {b'n': b'\n', b'r': b'\r', b't': b'\t'}.get(s, s)
    return re.sub(rb'\\([0-7]{1,3}|.)', remplacer, chaine).decode('cp1252')


def pages_pdf(contenu):
    """Texte de chaque page d'un PDF produit sans compression."""
    pages = []
    for flux in re.findall(rb'stream\r?\n(.*?)endstream', contenu, re.S):
        chaines = re.findall(rb'\(((?:\\.|[^\\)])*)\) Tj', flux)
        if chaines:
            pages.append(' '.join(_decoder(c) for c in chaines))
    return pages


def sans_compression():
    return mock.patch('reportlab.rl_config.pageCompression', 0)


class MontantEnLettresTests(TestCase):
    def test_nombres(self):
        attendus = {
            0: 'zéro', 1: 'un', 16: 'seize', 17: 'dix-sept', 21: 'vingt et un', 70: 'soixante-dix',
            71: 'soixante et onze', 80: 'quatre-vingts', 81: 'quatre-vingt-un', 91: 'quatre-vingt-onze',
            100: 'cent', 101: 'cent un', 200: 'deux cents', 201: 'deux cent un', 1000: 'mille',
            2000: 'deux mille', 21000: 'vingt et un mille', 80000: 'quatre-vingt mille', 200000: 'deux cent mille',
            1000000: 'un million', 2000000: 'deux millions', 80000000: 'quatre-vingts millions',
            200000000: 'deux cents millions', 1000000000: 'un milliard',
            2080500: 'deux millions quatre-vingt mille cinq cents',
            2705500: 'deux millions sept cent cinq mille cinq cents',
        }
        for n, texte in attendus.items():
            with self.subTest(n=n):
                self.assertEqual(montant_en_lettres(n), texte)

    def test_arrondi_a_l_unite(self):
        self.assertEqual(montant_en_lettres(Decimal('1999.60')), 'deux mille')
        self.assertEqual(montant_en_lettres('12.40'), 'douze')
        self.assertEqual(montant_en_lettres(None), 'zéro')


class BonsImprimesTests(TestCase):
    def setUp(self):
        cache.delete(Parametres.CLE_CACHE)  # réglages d'un autre test encore en cache
        Utilisateur = get_user_model()
        self.chef = Utilisateur.objects.create_superuser('awa', 'a@a.ci', 'motdepasse-solide',
                                                         first_name='Awa', last_name='Koné')
        self.magasinier = Utilisateur.objects.create_user('koffi', password='motdepasse-solide',
                                                          first_name='Koffi', last_name='Yao')
        cat = Categorie.objects.create(nom='Pièces')
        self.long = Article.objects.create(code=CODE_LONG, designation='Filtre à huile moteur Caterpillar 320D',
                                           categorie=cat, unite='M2')
        self.ciment = Article.objects.create(code='CIM-45', designation='Ciment CPJ 45', categorie=cat, unite='SAC')
        self.chantier = Chantier.objects.create(nom='Pont de Cocody')
        p = Parametres.actuels()
        p.nom_societe, p.rccm, p.ncc = 'BTP Ivoire', 'CI-ABJ-2019-B-12345', '1912345 K'
        p.save()

    def entree(self, *lignes, valider=True):
        bon = BonEntree.objects.create(fournisseur=Fournisseur.objects.get_or_create(nom='CFAO')[0],
                                       reference='FAC-458', cree_par=self.chef)
        for article, qte, prix in lignes:
            LigneEntree.objects.create(bon=bon, article=article, quantite=qte, prix_unitaire=prix)
        return services.valider(bon, self.chef) if valider else bon

    def sortie(self, *lignes, valider=True, par=None):
        bon = BonSortie.objects.create(chantier=self.chantier, demandeur='Jean Kouassi', cree_par=par or self.chef)
        for article, qte in lignes:
            LigneSortie.objects.create(bon=bon, article=article, quantite=qte)
        return services.valider(bon, self.chef) if valider else bon

    def imprimer(self, bon, utilisateur=None, parametres=''):
        self.client.force_login(utilisateur or self.chef)
        with sans_compression():
            rep = self.client.get(reverse('stock:bon_pdf', args=[bon._meta.model_name, bon.pk]) + parametres)
        self.assertEqual(rep.status_code, 200)
        self.assertEqual(rep['Content-Type'], 'application/pdf')
        return pages_pdf(rep.content)

    def test_magasinier_imprime_son_bon(self):
        self.entree((self.ciment, 10, 5000))
        bon = self.sortie((self.ciment, 2), par=self.magasinier)
        pages = self.imprimer(bon, self.magasinier)
        self.assertIn('Imprimé le', pages[0])
        self.assertIn('par Koffi Yao', pages[0])

    def test_bon_de_sortie_complet(self):
        self.entree((self.ciment, 10, 5000))
        bon = self.sortie((self.ciment, 2))
        texte, = self.imprimer(bon)
        for attendu in ('BTP Ivoire', 'RCCM : CI-ABJ-2019-B-12345', 'NCC : 1912345 K', f'BON DE SORTIE N° {bon.numero}',
                        'Reçu par', 'Jean Kouassi', 'Établi par', 'Validé par', 'Awa Koné le ', 'Sac',
                        'Arrêté le présent bon à la somme de :', 'dix mille francs CFA', 'Le réceptionnaire',
                        'Nom :', 'Date :', 'Signature :', f'{bon.numero} · Page 1/1 · Imprimé le', 'MagaStock'):
            self.assertIn(attendu, texte)
        self.assertNotIn('Demandeur', texte)
        self.assertNotIn('BROUILLON', texte)
        self.assertNotIn('ANNULÉ', texte)

    def test_montant_en_lettres_et_unites_en_clair(self):
        bon = self.entree((self.long, Decimal('12.5'), 166440))
        texte, = self.imprimer(bon)
        self.assertIn('2 080 500', texte)
        self.assertIn('deux millions quatre-vingt mille cinq cents francs CFA', texte)
        self.assertIn('Mètre carré', texte)
        self.assertIn('Le livreur', texte)
        bon = self.entree((self.ciment, 200, 5000))
        self.assertIn('un million de francs CFA', self.imprimer(bon)[0])

    def test_code_long_replie_dans_sa_colonne(self):
        from .pdf import MARGE_CELLULE, _replier, _tableau

        for code in (CODE_LONG, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123', 'A' * 50):
            morceaux = _replier(code, 25 * mm)
            self.assertEqual(''.join(morceaux), code)
            self.assertTrue(all(stringWidth(m, 'Helvetica', 8.5) <= 25 * mm for m in morceaux), morceaux)
        self.assertTrue(all(m.endswith('-') for m in _replier(CODE_LONG, 25 * mm)[:-1]))  # coupé après les tirets
        longueurs = [len(m) for m in _replier('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123', 24 * mm)]
        self.assertLessEqual(max(longueurs) - min(longueurs), 3, longueurs)  # lignes égales, pas de reste isolé
        table = _tableau(['Code', 'Désignation', 'Montant'], [[CODE_LONG, Paragraph('x'), '123 456 789 012 345']],
                         total='123 456 789 012 345', largeurs=[30 * mm, 120 * mm, 20 * mm])[0]
        table.wrap(180 * mm, 500 * mm)
        largeurs = table._colWidths
        self.assertAlmostEqual(sum(largeurs), 180 * mm)
        code = table._cellvalues[1][0][0]  # case de la 1re colonne (paragraphe du code)
        self.assertLessEqual(code.minWidth(), largeurs[0] - 2 * MARGE_CELLULE)
        besoin = stringWidth('123 456 789 012 345', 'Helvetica-Bold', 9) + 2 * MARGE_CELLULE
        self.assertGreaterEqual(largeurs[2], besoin)  # colonne des montants élargie, rien ne déborde

    def test_filigrane_et_pages_numerotees(self):
        bon = self.sortie(*[(self.ciment, 1)] * 70, valider=False)
        pages = self.imprimer(bon)
        self.assertGreaterEqual(len(pages), 3)
        for i, texte in enumerate(pages, 1):
            self.assertIn('BROUILLON', texte)
            self.assertIn(f'{bon.numero} · Page {i}/{len(pages)} · ', texte)
        services.annuler(bon, self.chef)
        self.assertTrue(all('ANNULÉ' in texte for texte in self.imprimer(bon)))

    def test_signatures_jamais_coupees(self):
        nb_pages = set()
        for nb in range(18, 34):
            with self.subTest(lignes=nb):
                bon = self.sortie(*[(self.ciment, 1)] * nb, valider=False)
                pages = self.imprimer(bon)
                nb_pages.add(len(pages))
                avec = [t for t in pages if 'Signature :' in t or 'Le réceptionnaire' in t]
                self.assertEqual(len(avec), 1)
                self.assertEqual(avec[0].count('Signature :'), 2)
                self.assertEqual(avec[0].count('Nom :'), 2)
                self.assertIn('Jean Kouassi', avec[0])
        self.assertEqual(nb_pages, {1, 2})  # le passage à la 2e page est bien couvert

    def test_deux_exemplaires(self):
        self.entree((self.ciment, 10, 5000))
        bon = self.sortie((self.ciment, 2))
        pages = self.imprimer(bon, parametres='?exemplaires=2')
        self.assertEqual(len(pages), 2)
        self.assertIn('EXEMPLAIRE MAGASIN', pages[0])
        self.assertIn('EXEMPLAIRE RÉCEPTIONNAIRE', pages[1])
        self.assertTrue(all(f'{bon.numero} · Page 1/1' in t for t in pages))
        self.assertNotIn('EXEMPLAIRE', self.imprimer(bon)[0])
        entree = BonEntree.objects.get()
        self.assertIn('EXEMPLAIRE LIVREUR', self.imprimer(entree, parametres='?exemplaires=2')[1])

    def test_prix_masques_sur_les_sorties(self):
        self.entree((self.ciment, 10, 5000))
        bon = self.sortie((self.ciment, 2))
        p = Parametres.actuels()
        p.masquer_prix_sortie = True
        p.save()
        texte, = self.imprimer(bon)
        self.assertIn('Ciment CPJ 45', texte)
        for absent in ('P.U.', 'Montant', 'TOTAL', 'Arrêté', '10 000'):
            self.assertNotIn(absent, texte)
        self.assertIn('Arrêté', self.imprimer(BonEntree.objects.get())[0])

    def test_sans_utilisateur_ni_options(self):
        """Appel historique (action de l'admin, vérification du programme) : reponse_pdf(bons, nom)."""
        from .pdf import reponse_pdf

        bons = [self.entree((self.ciment, 1, 100)), self.sortie((self.ciment, 1), valider=False)]
        with sans_compression():
            pages = pages_pdf(reponse_pdf(bons, 'lot').content)
        self.assertEqual(len(pages), 2)
        self.assertIn(f'{bons[0].numero} · Page 1/1 · Imprimé le', pages[0])
        self.assertNotIn(' par ', pages[0].split('Page 1/1')[1])
        self.assertIn(f'{bons[1].numero} · Page 1/1', pages[1])
        self.assertNotIn('BROUILLON', pages[0])
        self.assertIn('BROUILLON', pages[1])

    def test_reglages_societe(self):
        self.client.force_login(self.chef)
        url = reverse('stock:reglages_societe')
        page = self.client.get(url)
        self.assertContains(page, 'name="rccm"')
        self.assertContains(page, 'name="masquer_prix_sortie"')
        rep = self.client.post(url, {'nom_societe': 'BTP Ivoire', 'couleur': '#2457d6', 'devise': 'FCFA',
                                     'signataire': 'Le magasinier', 'rccm': 'CI-ABJ-2020-B-1', 'ncc': '2012345 A',
                                     'masquer_prix_sortie': 'on'})
        self.assertRedirects(rep, url)
        p = Parametres.objects.get()
        self.assertEqual((p.rccm, p.ncc, p.masquer_prix_sortie), ('CI-ABJ-2020-B-1', '2012345 A', True))
