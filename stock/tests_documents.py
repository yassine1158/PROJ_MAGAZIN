"""Documents : bons imprimés (PDF), montant en lettres, rapports et exports Excel."""
import importlib
import io
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Paragraph

from . import rapports, services
from .models import (
    Article, Bloc, BonEntree, BonSortie, Categorie, Chantier, Engin, Etagere, Fournisseur, Inventaire, LigneEntree,
    LigneSortie, MouvementStock, Parametres,
)
from .utils import montant_en_lettres, nombre

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


def classeur(reponse):
    from openpyxl import load_workbook

    return load_workbook(io.BytesIO(reponse.content))


def a_minuit(jour, heure=10):
    return timezone.make_aware(datetime.combine(jour, time(heure)))


class RapportsBase(TestCase):
    def setUp(self):
        cache.delete(Parametres.CLE_CACHE)
        Utilisateur = get_user_model()
        self.chef = Utilisateur.objects.create_superuser('awa', 'a@a.ci', 'motdepasse-solide',
                                                         first_name='Awa', last_name='Koné')
        self.magasinier = Utilisateur.objects.create_user('koffi', password='motdepasse-solide')
        p = Parametres.actuels()
        p.nom_societe = 'BTP Ivoire'
        p.save()
        bloc = Bloc.objects.create(code='A', nom='Pièces moteur')
        etagere = Etagere.objects.create(bloc=bloc, code='R1')
        self.materiaux = Categorie.objects.create(nom='Matériaux')
        self.filtres = Categorie.objects.create(nom='Filtres')
        self.ciment = Article.objects.create(code='CIM-45', designation='Ciment CPJ 45', categorie=self.materiaux,
                                             unite='SAC', stock_min=10)
        self.filtre = Article.objects.create(code='FH-01', designation='Filtre à huile', categorie=self.filtres,
                                             etagere=etagere, niveau=2, stock_min=2)
        self.pont = Chantier.objects.create(nom='Pont de Cocody')
        self.centrale = Chantier.objects.create(nom='Centrale Yopougon')
        self.malaxeur = Engin.objects.create(code='MLX-07', designation='Camion malaxeur')
        self.cfao = Fournisseur.objects.create(nom='CFAO')

    def entree(self, *lignes, fournisseur=None, le=None):
        bon = BonEntree.objects.create(fournisseur=fournisseur, cree_par=self.chef)
        for article, qte, prix in lignes:
            LigneEntree.objects.create(bon=bon, article=article, quantite=qte, prix_unitaire=prix)
        return self.dater(services.valider(bon, self.chef), le)

    def sortie(self, *lignes, chantier=None, engin=None, le=None):
        bon = BonSortie.objects.create(chantier=chantier or self.pont, engin=engin, demandeur='Jean', cree_par=self.chef)
        for article, qte in lignes:
            LigneSortie.objects.create(bon=bon, article=article, quantite=qte)
        return self.dater(services.valider(bon, self.chef), le)

    def dater(self, bon, le):
        """Bon (et ses mouvements) daté d'un autre jour, à 10 h."""
        if le:
            quand = a_minuit(le)
            type(bon).objects.filter(pk=bon.pk).update(date=quand)
            MouvementStock.objects.filter(reference=bon.numero).update(date=quand)
        return bon

    def page(self, nom, params='', utilisateur=None):
        self.client.force_login(utilisateur or self.chef)
        reponse = self.client.get(reverse(f'stock:{nom}') + params)
        self.assertEqual(reponse.status_code, 200)
        return reponse


class SortiesAnnuleesTests(RapportsBase):
    def setUp(self):
        super().setUp()
        self.entree((self.ciment, 100, 5000), (self.filtre, 10, 18500))
        self.annulee = self.sortie((self.ciment, 20), chantier=self.pont)
        self.sortie((self.ciment, 3), (self.filtre, 2), chantier=self.centrale, engin=self.malaxeur)
        services.annuler(self.annulee, self.chef)
        self.attendu = 3 * 5000 + 2 * 18500  # la sortie annulée (100 000) ne compte pas

    def test_memes_chiffres_accueil_tableau_de_bord_consommation(self):
        self.assertEqual(rapports.total_sorties(timezone.localdate().replace(day=1)), self.attendu)
        accueil = self.page('accueil', utilisateur=self.magasinier)
        tableau = self.page('dashboard')
        conso = self.page('consommation')
        self.assertEqual(accueil.context['sorties_mois'], nombre(self.attendu))
        self.assertEqual(tableau.context['sorties_mois'], nombre(self.attendu))
        self.assertEqual(conso.context['total'], self.attendu)
        self.assertEqual([c['nom'] for c in tableau.context['par_chantier']], ['Centrale Yopougon'])
        self.assertEqual([g['nom'] for g in conso.context['groupes']], ['Centrale Yopougon'])
        self.assertNotContains(conso, 'Pont de Cocody</h2>')

    def test_annulation_le_mois_suivant_sans_ligne_negative(self):
        mois_dernier = timezone.localdate().replace(day=1) - timedelta(days=20)
        bon = self.sortie((self.ciment, 5), chantier=self.pont, le=mois_dernier)
        services.annuler(bon, self.chef)  # annulée aujourd'hui, un autre mois que la sortie
        du, au = rapports.raccourcis()[1][1:]
        self.assertEqual(rapports.consommation(du, au)['groupes'], [])
        self.assertEqual(self.page('consommation').context['total'], self.attendu)

    def test_retour_de_chantier_reduit_la_consommation(self):
        MouvementStock.objects.create(article=self.ciment, type=rapports.RETOUR, quantite=1, stock_apres=78,
                                      prix_unitaire=5000, reference='BR-2026-00001', chantier=self.centrale)
        donnees = rapports.consommation(timezone.localdate().replace(day=1), timezone.localdate())
        self.assertEqual(donnees['total'], self.attendu - 5000)
        ciment, = [l for l in donnees['groupes'][0]['lignes'] if l['code'] == 'CIM-45']
        self.assertEqual(ciment['qte'], 2)
        self.assertEqual(rapports.total_sorties(timezone.localdate().replace(day=1)), self.attendu - 5000)

    def test_regroupements_et_filtre_chantier(self):
        self.sortie((self.ciment, 1), chantier=self.pont)
        conso = self.page('consommation', '?par=engin')
        self.assertEqual([g['nom'] for g in conso.context['groupes']], ['MLX-07 – Camion malaxeur', 'Sans engin'])
        self.assertEqual(conso.context['total'], self.attendu + 5000)
        par_article = self.page('consommation', '?par=article').context
        self.assertEqual(len(par_article['groupes']), 1)
        self.assertEqual([l['code'] for l in par_article['groupes'][0]['lignes']], ['FH-01', 'CIM-45'])  # le plus cher d'abord
        filtre = self.page('consommation', f'?chantier={self.pont.pk}').context
        self.assertEqual(filtre['total'], 5000)
        self.assertEqual(self.page('consommation', '?chantier=abc&par=nimporte').context['par'], 'chantier')

    def test_ecran_imprimable(self):
        conso = self.page('consommation', utilisateur=self.magasinier)
        self.assertContains(conso, 'class="carte filtres-rapport no-print"')
        self.assertContains(conso, 'Mois dernier')
        self.assertContains(conso, 'Par engin')
        app_css = (__import__('pathlib').Path(__file__).parent / 'static/stock/app.css').read_text()
        self.assertRegex(app_css, r'@media print \{\n  \.bandeau-essai')

    def test_excel_deux_onglets(self):
        du = timezone.localdate().replace(day=1)
        rep = self.page('consommation', f'?du={du:%Y-%m-%d}&au={timezone.localdate():%Y-%m-%d}&format=excel')
        self.assertIn(f'filename="consommation_{du:%Y-%m-%d}_au_{timezone.localdate():%Y-%m-%d}.xlsx"',
                      rep['Content-Disposition'])
        wb = classeur(rep)
        self.assertEqual(wb.sheetnames, ['Récapitulatif', 'Détail'])
        recap, detail = wb['Récapitulatif'], wb['Détail']
        self.assertEqual(recap['A1'].value, 'BTP Ivoire')
        self.assertIn('Récapitulatif · Du ', recap['A2'].value)
        self.assertRegex(recap['A3'].value, r'^Édité le \d\d/\d\d/\d{4} à \d\d:\d\d par Awa Koné$')
        self.assertEqual([c.value for c in recap[5]], ['Chantier', 'Code', 'Désignation', 'Quantité', 'Unité',
                                                      'Valeur (FCFA)'])
        lignes = [[c.value for c in ligne] for ligne in recap.iter_rows(min_row=6)]
        self.assertEqual(lignes[-2][0], 'Sous-total Centrale Yopougon')
        self.assertEqual(lignes[-1][0], 'TOTAL')
        self.assertEqual(lignes[-1][5], self.attendu)
        self.assertTrue(recap.cell(recap.max_row, 1).font.b)
        self.assertTrue(recap.cell(recap.max_row - 1, 6).font.b)
        self.assertEqual(recap['F6'].number_format, '#,##0')
        self.assertEqual(recap['E6'].value, 'Sac')
        colonne = [c.value for c in detail['A'][5:]]
        self.assertEqual(len(colonne), 3)
        self.assertTrue(all(isinstance(d, datetime) for d in colonne[:2]))
        self.assertEqual(colonne[2], 'TOTAL')
        self.assertEqual(detail['A6'].number_format, 'dd/mm/yyyy hh:mm')
        self.assertEqual(detail.cell(detail.max_row, 11).value, self.attendu)

    def test_pdf(self):
        with sans_compression():
            rep = self.page('consommation', '?format=pdf')
        self.assertEqual(rep['Content-Type'], 'application/pdf')
        texte = ' '.join(pages_pdf(rep.content))
        for attendu in ('BTP Ivoire', 'CONSOMMATION PAR CHANTIER', 'Récapitulatif', 'Centrale Yopougon', 'Sous-total',
                        'TOTAL GÉNÉRAL', nombre(self.attendu).replace('\xa0', ' '), 'Page 1/1', 'par Awa Koné'):
            self.assertIn(attendu, texte)
        self.assertNotIn('Pont de Cocody', texte)

    def test_unites_en_clair_au_pluriel(self):
        self.assertEqual([rapports.unite_qte(u, q) for u, q in (
            ('SAC', 1), ('SAC', 50), ('M', Decimal('7.5')), ('M', Decimal('1.5')), ('U', -4), ('JEU', 3),
            ('M2', 12), ('BOITE', 2), ('INCONNUE', 2), ('', 3))],
            ['sac', 'sacs', 'mètres', 'mètre', 'unités', 'jeux', 'mètres carrés', 'boîtes', 'inconnues', ''])
        self.assertContains(self.page('consommation'), '3 <span class="muted">sacs</span>')

    def test_raccourcis(self):
        self.assertEqual(rapports.raccourcis(date(2026, 3, 15)), [
            ('Ce mois', date(2026, 3, 1), date(2026, 3, 15)),
            ('Mois dernier', date(2026, 2, 1), date(2026, 2, 28)),
            ('Cette année', date(2026, 1, 1), date(2026, 3, 15)),
        ])
        self.assertEqual(rapports.raccourcis(date(2026, 1, 9))[1][1:], (date(2025, 12, 1), date(2025, 12, 31)))
        self.assertEqual(rapports.periode({'du': '2026-05-31', 'au': '2026-05-01'}), (date(2026, 5, 1), date(2026, 5, 31)))


class EtatADateTests(RapportsBase):
    def setUp(self):
        super().setUp()
        aujourd_hui = timezone.localdate()
        self.j1, self.j2, self.j3 = aujourd_hui - timedelta(days=30), aujourd_hui - timedelta(days=20), \
            aujourd_hui - timedelta(days=10)
        self.entree((self.ciment, 100, 5000), (self.filtre, 10, 18000), le=self.j1)
        self.sortie((self.ciment, 40), le=self.j2)
        self.entree((self.ciment, 60, 6000), le=self.j3)  # prix moyen : (60 × 5000 + 60 × 6000) / 120 = 5500

    def ligne(self, donnees, code):
        return next(l for g in donnees['groupes'] for l in g['lignes'] if l['code'] == code)

    def test_quantites_et_valeurs_a_chaque_date(self):
        avant = rapports.etat_a_date(self.j1 - timedelta(days=1))
        self.assertEqual((avant['nb'], avant['total']), (0, 0))
        j1 = rapports.etat_a_date(self.j1)
        self.assertEqual(j1['total'], 100 * 5000 + 10 * 18000)
        j2 = rapports.etat_a_date(self.j2 + timedelta(days=1))
        self.assertEqual(self.ligne(j2, 'CIM-45')['qte'], 60)
        self.assertEqual(self.ligne(j2, 'CIM-45')['valeur'], 60 * 5000)
        j3 = rapports.etat_a_date(self.j3)
        self.assertEqual(self.ligne(j3, 'CIM-45')['pm'], 5500)
        self.assertEqual(j3['total'], 120 * 5500 + 10 * 18000)
        aujourd_hui = rapports.etat_a_date(timezone.localdate())
        self.assertEqual(aujourd_hui['total'], sum(a.stock * a.prix_moyen for a in Article.objects.all()))
        self.assertEqual(j3['total'], aujourd_hui['total'])
        self.assertEqual(j3['nb_approx'], 0)

    def test_sous_totaux_par_categorie_et_par_bloc(self):
        par_categorie = rapports.etat_a_date(self.j3)
        self.assertEqual([(g['nom'], g['total']) for g in par_categorie['groupes']],
                         [('Filtres', 180000), ('Matériaux', 660000)])
        par_bloc = rapports.etat_a_date(self.j3, par='bloc')
        self.assertEqual([g['nom'] for g in par_bloc['groupes']], ['Bloc A – Pièces moteur', 'Non rangé'])
        vide = Article.objects.create(code='VIDE', designation='Jamais reçu', categorie=self.filtres)
        self.assertNotIn(vide.code, [l['code'] for g in par_categorie['groupes'] for l in g['lignes']])
        avec_zero = rapports.etat_a_date(self.j3, avec_zero=True)
        self.assertEqual(self.ligne(avec_zero, 'VIDE')['qte'], 0)

    def test_ancien_mouvement_sans_prix_moyen_signale(self):
        MouvementStock.objects.filter(article=self.filtre).update(prix_moyen_apres=None)
        donnees = rapports.etat_a_date(self.j2)
        filtre = self.ligne(donnees, 'FH-01')
        self.assertTrue(filtre['approx'])
        self.assertEqual(filtre['pm'], Decimal('18000'))  # prix moyen actuel
        self.assertEqual(donnees['nb_approx'], 1)
        self.assertContains(self.page('etat_stock_date', f'?date={self.j2:%Y-%m-%d}'), 'mise à jour du logiciel')

    def test_migration_retrouve_les_prix_moyens(self):
        attendus = list(MouvementStock.objects.order_by('id').values_list('prix_moyen_apres', flat=True))
        self.assertNotIn(None, attendus)
        MouvementStock.objects.update(prix_moyen_apres=None)
        migration = importlib.import_module('stock.migrations.0007_mouvement_prix_moyen_apres')
        from django.apps import apps
        migration.retrouver_prix_moyens(apps, None)
        self.assertEqual(list(MouvementStock.objects.order_by('id').values_list('prix_moyen_apres', flat=True)),
                         attendus)
        # Historique incohérent (prix moyen modifié à la main) : rien n'est écrit pour cet article.
        MouvementStock.objects.update(prix_moyen_apres=None)
        Article.objects.filter(pk=self.ciment.pk).update(prix_moyen=1)
        migration.retrouver_prix_moyens(apps, None)
        self.assertFalse(MouvementStock.objects.filter(article=self.ciment, prix_moyen_apres__isnull=False).exists())
        self.assertFalse(MouvementStock.objects.filter(article=self.filtre, prix_moyen_apres__isnull=True).exists())

    def test_page_excel_et_pdf(self):
        page = self.page('etat_stock_date', f'?date={self.j2:%Y-%m-%d}&par=bloc', utilisateur=self.magasinier)
        self.assertContains(page, f'État du stock au {self.j2:%d/%m/%Y}')
        self.assertContains(page, 'Bloc A – Pièces moteur')
        futur = self.page('etat_stock_date', '?date=2999-01-01')
        self.assertEqual(futur.context['jour'], timezone.localdate())
        wb = classeur(self.page('etat_stock_date', f'?date={self.j3:%Y-%m-%d}&format=excel'))
        ws = wb.active
        self.assertEqual(ws.title, f'État du stock au {self.j3:%d-%m-%Y}')
        self.assertEqual(ws.cell(ws.max_row, 1).value, 'TOTAL')
        self.assertEqual(ws.cell(ws.max_row, 7).value, 840000)
        self.assertIn('Sous-total Filtres', [c.value for c in ws['A']])
        self.assertEqual(ws['F6'].number_format, '#,##0')
        with sans_compression():
            rep = self.page('etat_stock_date', f'?date={self.j3:%Y-%m-%d}&format=pdf')
        self.assertIn(f'etat_stock_au_{self.j3:%Y-%m-%d}.pdf', rep['Content-Disposition'])
        texte = ' '.join(pages_pdf(rep.content))
        for attendu in (f'ÉTAT DU STOCK AU {self.j3:%d/%m/%Y}', 'Le magasinier', 'Le responsable', 'Signature :',
                        'Sous-total', 'TOTAL GÉNÉRAL', nombre(840000).replace('\xa0', ' ')):
            self.assertIn(attendu, texte)
        self.assertEqual(len(pages_pdf(rep.content)), 1)  # total et signatures sur la page du tableau


class JournalTests(RapportsBase):
    def setUp(self):
        super().setUp()
        self.e1 = self.entree((self.ciment, 100, 5000), fournisseur=self.cfao)
        self.entree((self.filtre, 10, 18000))
        self.s1 = self.sortie((self.ciment, 5), chantier=self.pont, engin=self.malaxeur)
        self.sortie((self.filtre, 1), chantier=self.centrale)

    def test_filtres_et_totaux(self):
        page = self.page('journal', utilisateur=self.magasinier)
        self.assertEqual(page.context['totaux'], {'entrees': 680000, 'sorties': 43000, 'nb': 4, 'nb_annules': 0})
        self.assertContains(page, reverse('stock:bon_detail', args=['sortie', self.s1.pk]))
        cas = {
            '?type=SORTIE': 2, '?article=cim-45': 2, '?article=filtre': 2, f'?chantier={self.pont.pk}': 1,
            f'?fournisseur={self.cfao.pk}': 1, f'?engin={self.malaxeur.pk}': 1, '?chantier=xyz': 4,
            f'?du={timezone.localdate() + timedelta(days=1):%Y-%m-%d}&au={timezone.localdate() + timedelta(days=2):%Y-%m-%d}': 0,
        }
        for params, nb in cas.items():
            with self.subTest(params=params):
                self.assertEqual(self.page('journal', params).context['totaux']['nb'], nb)

    def test_pagination_par_100(self):
        self.entree(*[(self.filtre, 1, 18000)] * 120)
        page = self.page('journal')
        self.assertEqual(len(page.context['page'].object_list), 100)
        self.assertContains(page, 'Page 1 sur 2')
        self.assertEqual(len(self.page('journal', '?page=2').context['page'].object_list), 24)

    def test_excel_avec_valeur(self):
        rep = self.page('journal', '?format=excel')
        self.assertIn('filename="mouvements_', rep['Content-Disposition'])
        ws = classeur(rep).active
        entetes = [c.value for c in ws[5]]
        self.assertIn('Valeur (FCFA)', entetes)
        valeur = entetes.index('Valeur (FCFA)') + 1
        self.assertEqual(sorted(ws.cell(r, valeur).value for r in range(6, 10)), [-25000, -18000, 180000, 500000])
        self.assertEqual(ws.cell(ws.max_row - 1, 1).value, 'Total des entrées')
        self.assertEqual(ws.cell(ws.max_row - 1, valeur).value, 680000)
        self.assertEqual(ws.cell(ws.max_row, valeur).value, -43000)
        self.assertIsInstance(ws['A6'].value, datetime)
        self.assertEqual(ws.auto_filter.ref, 'A5:N9')
        self.assertEqual(ws['A6'].value.microsecond, 0)

    def test_bon_annule_affiche_mais_pas_compte(self):
        services.annuler(self.s1, self.chef)
        page = self.page('journal')
        self.assertEqual(page.context['totaux'], {'entrees': 680000, 'sorties': 18000, 'nb': 5, 'nb_annules': 2})
        self.assertContains(page, 'class="ligne-annulee"', count=2)
        self.assertContains(page, 'Les 2 lignes de bons annulés sont affichées mais ne comptent pas.')
        ws = classeur(self.page('journal', '?format=excel')).active
        remarques = {ws.cell(r, 2).value: ws.cell(r, 14).value for r in range(6, ws.max_row - 1)}
        self.assertEqual(remarques[self.s1.numero], 'Bon annulé')
        self.assertEqual(remarques[self.e1.numero], None)
        self.assertEqual(ws.cell(ws.max_row - 1, 1).value, 'Total des entrées (hors bons annulés)')
        self.assertEqual((ws.cell(ws.max_row - 1, 10).value, ws.cell(ws.max_row, 10).value), (680000, -18000))


class ExcelTests(RapportsBase):
    def test_etat_du_stock_mis_en_forme(self):
        self.entree((self.ciment, Decimal('12.5'), 5000), (self.filtre, 3, 18500))
        rupture = Article.objects.create(code='VIDE', designation='Article vide', categorie=self.filtres, stock_min=1)
        self.client.force_login(self.chef)
        rep = self.client.get(reverse('stock:articles') + '?format=excel')
        self.assertRegex(rep['Content-Disposition'], r'filename="etat_stock_\d{4}-\d\d-\d\d\.xlsx"')
        ws = classeur(rep).active
        self.assertEqual(ws['A1'].value, 'BTP Ivoire')
        self.assertEqual(ws['A2'].value, 'État du stock')
        self.assertIn('par Awa Koné', ws['A3'].value)
        self.assertEqual(ws['A4'].value, None)
        self.assertEqual(ws.freeze_panes, 'A6')
        self.assertEqual(ws.auto_filter.ref, 'A5:J8')
        self.assertEqual(ws.print_title_rows, '$5:$5')
        self.assertEqual(ws.page_setup.orientation, 'landscape')
        self.assertEqual(ws.page_setup.fitToWidth, 1)
        self.assertTrue(ws.sheet_properties.pageSetUpPr.fitToPage)
        lignes = {ligne[0].value: ligne for ligne in ws.iter_rows(min_row=6)}
        ciment, vide = lignes['CIM-45'], lignes['VIDE']
        self.assertEqual(ciment[3].value, 'Sac')
        self.assertEqual((ciment[5].value, ciment[5].number_format), (12.5, '#,##0.###'))
        self.assertEqual((lignes['FH-01'][5].value, lignes['FH-01'][5].number_format), (3, '#,##0'))
        self.assertEqual((ciment[8].value, ciment[8].number_format), (62500, '#,##0'))
        self.assertEqual(ciment[9].value, None)
        self.assertEqual(vide[9].value, 'Rupture')
        Article.objects.filter(pk=rupture.pk).update(stock=1, stock_min=5)
        ws = classeur(self.client.get(reverse('stock:articles') + f'?format=excel&categorie={self.filtres.pk}')).active
        self.assertEqual(ws['A2'].value, 'État du stock · Filtres')
        self.assertEqual({l[0].value: l[9].value for l in ws.iter_rows(min_row=6, max_row=ws.max_row - 1)},
                         {'FH-01': None, 'VIDE': 'À commander'})
        total = ws[ws.max_row]
        self.assertEqual(total[0].value, 'TOTAL')
        self.assertTrue(total[0].font.b and total[8].font.b)

    def test_pas_de_formule_injectee(self):
        from . import exports

        rep = exports.reponse_excel('Test', ['Texte', ('Montant', exports.MONTANT)], [
            ['=HYPERLINK("http://pirate.example","Cliquer")', 1], ['+33 1 23', 2], ['-x', 3], ['@SOMME(A1)', 4],
            ['Normal', Decimal('1234.6')],
        ], 'test')
        ws = classeur(rep).active
        textes = [ws.cell(r, 1) for r in range(6, 11)]
        self.assertEqual([c.data_type for c in textes], ['s'] * 5)
        self.assertEqual(textes[0].value, '=HYPERLINK("http://pirate.example","Cliquer")')
        self.assertEqual(ws['B10'].value, 1235)
        self.assertFalse(any(c.data_type == 'f' for ligne in ws.iter_rows() for c in ligne))

    def test_caracteres_de_controle_retires(self):
        """Un caractère invisible (\\x07…) refusé par Excel, dans une désignation ou une recherche, ne fait plus
        échouer l'export."""
        from . import exports

        # Désignation enregistrée avant la correction (ou par un autre écran) : les exports la nettoient.
        Article.objects.filter(pk=self.filtre.pk).update(designation='Filtre\x07 à huile\x1b')
        self.entree((self.filtre, 3, 1000))
        self.sortie((self.filtre, 1))
        self.client.force_login(self.chef)
        rep = self.client.get(reverse('stock:articles') + '?format=excel')
        self.assertEqual(rep.status_code, 200)
        self.assertIn('Filtre à huile', [c.value for ligne in classeur(rep).active.iter_rows() for c in ligne])
        ws = classeur(self.client.get(reverse('stock:articles') + '?format=excel&q=filtre%07')).active
        self.assertEqual(ws['A2'].value, 'État du stock · « filtre »')
        for url in (reverse('stock:etat_stock_date') + '?format=excel', reverse('stock:journal') + '?format=excel',
                    reverse('stock:article_historique', args=[self.filtre.pk]) + '?format=excel',
                    reverse('stock:articles') + '?filtre=alerte&format=excel'):
            with self.subTest(url):
                ws = classeur(self.client.get(url)).active
                self.assertIn('Filtre à huile', ' '.join(str(c.value) for ligne in ws.iter_rows() for c in ligne))
        # Titre, filtres, en-têtes, lignes et total : tout passe par le même nettoyage.
        ws = classeur(exports.reponse_excel('Titre\x01', ['Texte\x02', ('Qté\x0b', exports.QTE)], [['a\x07b\tc', 1]],
                                            'test', filtres='q\x1f', pied=[['Total\x0c', 1]])).active
        self.assertEqual((ws.title, ws['A2'].value, ws['A5'].value, ws['B5'].value),
                         ('Titre', 'Titre · q', 'Texte', 'Qté'))
        self.assertEqual((ws['A6'].value, ws['A7'].value), ('ab\tc', 'Total'))


class TableauDeBordTests(RapportsBase):
    def test_valeur_par_categorie_et_brouillons(self):
        self.entree((self.ciment, 10, 5000), (self.filtre, 2, 18000))
        BonSortie.objects.create(chantier=self.pont, demandeur='Jean')
        Inventaire.objects.create()
        valide = self.sortie((self.ciment, 1))
        page = self.page('dashboard')
        self.assertEqual([(c['nom'], c['valeur']) for c in page.context['par_categorie']],
                         [('Matériaux', 45000), ('Filtres', 36000)])
        self.assertEqual(page.context['brouillons'], 2)
        self.assertContains(page, '?statut=brouillon')
        bons = self.page('bons', '?statut=brouillon', utilisateur=self.magasinier)
        self.assertEqual(len(bons.context['bons']), 2)
        self.assertNotContains(bons, valide.numero)
        self.assertContains(bons, 'Brouillons seulement')
        self.assertEqual(len(self.page('bons').context['bons']), 4)


# =========================================================
# Listes rapides et lisibles
# =========================================================

class ListeArticlesTests(RapportsBase):
    def lignes(self, page):
        return [a.code for a in page.context['articles']]

    def test_pagination_par_50_et_total(self):
        Article.objects.bulk_create([
            Article(code=f'VIS-{i:03d}', designation=f'Vis {i}', categorie=self.filtres, stock=2, prix_moyen=100)
            for i in range(60)])
        page = self.page('articles', f'?categorie={self.filtres.pk}')
        self.assertEqual(len(page.context['articles']), 50)
        self.assertEqual(page.context['total'], nombre(60 * 200))
        self.assertContains(page, '61 articles')
        self.assertContains(page, f'href="?categorie={self.filtres.pk}&amp;page=2"')
        suite = self.page('articles', f'?categorie={self.filtres.pk}&page=2')
        self.assertEqual(len(suite.context['articles']), 11)
        self.assertContains(suite, 'Page 2 sur 2')
        self.assertEqual(len(self.page('articles', '?page=99').context['articles']), 12)

    def test_recherche_tolerante_puis_filtres(self):
        Article.objects.create(code='SAC-VIDE', designation='Sac vide pour ciment', categorie=self.materiaux)
        cas = {'ciment': ['CIM-45', 'SAC-VIDE'], 'ciments': ['CIM-45'], 'cimant': ['CIM-45'], 'flitre': ['FH-01'], 'cim': ['CIM-45', 'SAC-VIDE'],
               'fh-0': ['FH-01'], 'huile': ['FH-01']}
        for q, attendu in cas.items():
            with self.subTest(q=q):
                self.assertEqual(self.lignes(self.page('articles', f'?q={q}'))[:len(attendu)], attendu)
        self.assertEqual(self.lignes(self.page('articles', f'?q=ciment&categorie={self.filtres.pk}')), [])
        vide = self.page('articles', '?q=zzzz')
        self.assertContains(vide, 'Aucun article ne correspond à « zzzz »')
        self.assertContains(vide, 'Effacer la recherche')
        self.assertNotContains(vide, "Aucun article pour l'instant")
        self.assertContains(self.page('articles', f'?q=ciment&categorie={self.filtres.pk}'), 'avec ces filtres')
        Article.objects.filter(code='SAC-VIDE').update(actif=False)
        self.assertEqual(self.lignes(self.page('articles', '?q=sac vide&filtre=inactifs')), ['SAC-VIDE'])

    def test_etats_vides(self):
        Article.objects.all().delete()
        page = self.page('articles')
        self.assertContains(page, '0 article ·')
        self.assertContains(page, "Aucun article pour l'instant")
        self.assertContains(page, reverse('stock:article_nouveau'))
        self.assertContains(self.page('accueil'), 'Aucun article encore')

    def test_bloc_et_etagere_ranges_par_niveau(self):
        etagere = self.filtre.etagere
        autre = Etagere.objects.create(bloc=etagere.bloc, code='R2')
        bloc_b = Bloc.objects.create(code='B')
        for code, e, niveau in (('N4', etagere, 4), ('N1', etagere, 1), ('NX', etagere, None), ('R2-1', autre, 1),
                                ('B-1', Etagere.objects.create(bloc=bloc_b, code='P1'), 1)):
            Article.objects.create(code=code, designation=code, categorie=self.filtres, etagere=e, niveau=niveau)
        page = self.page('articles', f'?etagere={etagere.pk}')
        self.assertEqual(self.lignes(page), ['N1', 'FH-01', 'N4', 'NX'])
        self.assertEqual(page.context['bloc'], etagere.bloc)
        self.assertContains(page, 'Étagère R2')
        self.assertEqual(self.lignes(self.page('articles', f'?bloc={etagere.bloc.pk}')), ['N1', 'FH-01', 'N4', 'NX', 'R2-1'])
        self.assertEqual(self.lignes(self.page('articles', f'?bloc={bloc_b.pk}')), ['B-1'])
        self.assertEqual(len(self.page('articles', '?bloc=abc&etagere=²&categorie=xyz').context['articles']), 7)

    def test_lisible_au_telephone_et_images_differees(self):
        Article.objects.filter(pk=self.filtre.pk).update(photo='articles/filtre.png')
        page = self.page('articles').content.decode()
        self.assertIn('loading="lazy"', page)
        entetes = re.search(r'<thead>(.*?)</thead>', page, re.S).group(1)
        self.assertIn('<th class="masquer-mobile">Catégorie</th>', entetes)
        self.assertIn('<th class="num masquer-mobile">Valeur</th>', entetes)
        self.assertIn('<th>Article</th>', entetes)
        self.assertIn('<th class="num">Stock</th>', entetes)
        self.assertIn(reverse('stock:article_historique', args=[self.filtre.pk]), page)
        self.assertIn('FH-01<span class="seulement-telephone"> · A › R1 › niv. 2</span>', page)


class AutocompletionTests(RapportsBase):
    def chercher(self, q):
        self.client.force_login(self.magasinier)
        return [a['code'] for a in self.client.get(reverse('stock:api_articles'), {'q': q}).json()['articles']]

    def test_code_exact_en_tete_puis_base(self):
        Article.objects.create(code='FH-01-B', designation='Bague filtre', categorie=self.filtres)
        Article.objects.create(code='AFH-01', designation='Autre', categorie=self.filtres)
        Article.objects.create(code='FH-02', designation='Filtre désactivé', categorie=self.filtres, actif=False)
        self.assertEqual(self.chercher('fh-01'), ['FH-01', 'FH-01-B', 'AFH-01'])
        self.assertEqual(self.chercher('fh01')[0], 'FH-01')  # douchette ou code tapé sans tiret
        self.assertEqual(self.chercher('FH 01')[0], 'FH-01')
        Article.objects.create(code='ZZ-9', designation='Joint de filtre', categorie=self.filtres)
        self.assertEqual(self.chercher('filtres')[:2], ['FH-01', 'FH-01-B'])  # désignation qui commence par le mot
        self.assertEqual(self.chercher('ci'), ['CIM-45'])
        self.assertEqual(self.chercher('filtre huile')[0], 'FH-01')
        self.assertNotIn('FH-02', self.chercher('filtre'))
        self.assertEqual(len(self.chercher('')), 5)

    def test_sans_accents_ni_majuscules(self):
        Article.objects.create(code='EC-12', designation='Écrou HM 12', categorie=self.filtres)
        Article.objects.create(code='CB-3', designation='Câble acier', categorie=self.filtres, mots_cles='fil tressé')
        Article.objects.create(code='T-1', designation='Tôle ondulée', categorie=self.filtres)
        with mock.patch('stock.views_magasin.recherche_locale', return_value=[]):  # la base seule suffit
            self.assertEqual(self.chercher('ecr'), ['EC-12'])
            self.assertEqual(self.chercher('ÉCROU'), ['EC-12'])
            self.assertEqual(self.chercher('cables'), ['CB-3'])
            self.assertEqual(self.chercher('tresse'), ['CB-3'])
            self.assertEqual(self.chercher('tole ondulee'), ['T-1'])
            self.assertEqual(self.chercher('a huile'), ['FH-01'])
            self.assertEqual(self.chercher('(%_'), [])

    def test_recherche_tolerante_seulement_si_peu_de_resultats(self):
        Article.objects.bulk_create([Article(code=f'C-{i}', designation=f'Câble {i}', categorie=self.filtres)
                                     for i in range(20)])
        with mock.patch('stock.views_magasin.recherche_locale', return_value=[]) as tolerante:
            self.assertEqual(len(self.chercher('câble')), 15)
            self.assertEqual(len(self.chercher('câbles')), 15)  # pluriel trouvé en base
            self.chercher('cim')  # moins de 4 lettres
            self.chercher('CIM-45')  # code exact
            tolerante.assert_not_called()
            self.chercher('cimant')
            tolerante.assert_called_once_with(['cimant'], limite=15)
        self.assertEqual(self.chercher('cimant'), ['CIM-45'])


class HistoriqueBonsTests(RapportsBase):
    def setUp(self):
        super().setUp()
        aujourd_hui = timezone.localdate()
        self.ancien = self.entree((self.ciment, 50, 5000), (self.ciment, 10, 5000), fournisseur=self.cfao,
                                  le=aujourd_hui - timedelta(days=40))
        self.entree((self.filtre, 5, 1000))
        self.recent = self.sortie((self.filtre, 1))
        self.annule = services.annuler(self.sortie((self.ciment, 2), chantier=self.centrale), self.chef)

    def numeros(self, params=''):
        return [b.numero for b, _ in self.page('bons', params).context['bons']]

    def test_recherche_par_article_sans_doublon(self):
        self.assertEqual(self.numeros('?q=cim-45'), [self.annule.numero, self.ancien.numero])
        self.assertEqual(self.numeros('?q=ciment cpj'), [self.annule.numero, self.ancien.numero])
        self.assertEqual(self.numeros('?q=huile&type=sortie'), [self.recent.numero])
        self.assertEqual(self.numeros('?q=cfao'), [self.ancien.numero])
        self.assertContains(self.page('bons', '?q=zzz'), 'Aucun bon ne correspond à « zzz »')

    def test_periode_statut_type(self):
        aujourd_hui = timezone.localdate()
        avant = (aujourd_hui - timedelta(days=41)).isoformat()
        apres = (aujourd_hui - timedelta(days=30)).isoformat()
        self.assertEqual(self.numeros(f'?du={avant}&au={apres}'), [self.ancien.numero])
        self.assertEqual(self.numeros(f'?du={apres}&au={avant}'), [self.ancien.numero])
        self.assertEqual(self.numeros(f'?du={aujourd_hui.isoformat()}&type=sortie'), [self.annule.numero, self.recent.numero])
        self.assertEqual(self.numeros('?statut=annule'), [self.annule.numero])
        self.assertEqual(len(self.numeros('?du=n-importe-quoi&type=autre&statut=x')), 4)
        page = self.page('bons', f'?du={avant}&au={apres}')
        self.assertContains(page, '1 bon trouvé')
        self.assertContains(page, 'Toutes les dates')

    def test_pagination_au_lieu_de_la_coupe(self):
        BonEntree.objects.bulk_create([BonEntree(numero=f'BE-T-{i:03d}', cree_par=self.chef) for i in range(170)])
        page = self.page('bons', '?type=entree&q=BE-T')
        self.assertEqual(len(page.context['bons']), 50)
        self.assertEqual(page.context['page'].paginator.count, 170)
        self.assertContains(page, 'href="?type=entree&amp;q=BE-T&amp;page=2"')
        self.assertEqual(len(self.page('bons', '?type=entree&q=BE-T&page=4').context['bons']), 20)

    def test_lisible_au_telephone(self):
        page = self.page('bons').content.decode()
        self.assertIn('<th class="masquer-mobile">Date</th>', page)
        self.assertIn(f'<a class="sans-coupure" href="{reverse("stock:bon_detail", args=["sortie", self.recent.pk])}">',
                      page)
        self.assertIn('Pont de Cocody', page)
        detail = self.client.get(reverse('stock:bon_detail', args=['sortie', self.recent.pk]))
        self.assertContains(detail, 'class="data detail-bon"')

    def test_retours_de_chantier_quand_ils_existent(self):
        from django.apps import apps
        self.assertIs(rapports.modeles_bons()['retour'], apps.get_model('stock', 'BonRetour'))  # module du retour présent
        with mock.patch('stock.rapports.apps.get_model', return_value=BonSortie):
            self.assertIs(rapports.modeles_bons()['retour'], BonSortie)
            page = self.page('bons', '?type=retour')
            self.assertEqual(len(page.context['bons']), 2)
            self.assertContains(page, '>Retours</a>')


class FicheDeStockTests(RapportsBase):
    def setUp(self):
        super().setUp()
        aujourd_hui = timezone.localdate()
        self.e1 = self.entree((self.ciment, 100, 5000), fournisseur=self.cfao, le=aujourd_hui - timedelta(days=60))
        self.s1 = self.sortie((self.ciment, 30), le=aujourd_hui - timedelta(days=45))
        self.e2 = self.entree((self.ciment, 20, 0), le=aujourd_hui - timedelta(days=10))
        self.s2 = self.sortie((self.ciment, 12), chantier=self.centrale, engin=self.malaxeur)
        self.s3 = services.annuler(self.sortie((self.ciment, 4)), self.chef)
        self.url = reverse('stock:article_historique', args=[self.ciment.pk])

    def test_page(self):
        self.assertEqual(self.url, f'/articles/{self.ciment.pk}/historique/')
        self.client.force_login(self.magasinier)
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['fiche']['nb'], 6)
        self.assertEqual((page.context['fiche']['entrees'], page.context['fiche']['sorties']), (124, 46))
        self.assertEqual(page.context['fiche']['stock_fin'], 78)
        self.assertEqual(page.context['conso']['qte'], 12)
        self.assertEqual(page.context['achat']['prix'], 5000)
        self.assertEqual(page.context['achat']['fournisseur'], self.cfao)
        self.assertContains(page, reverse('stock:bon_detail', args=['sortie', self.s2.pk]))
        self.assertContains(page, reverse('stock:bon_detail', args=['entree', self.e1.pk]))
        self.assertContains(page, 'Centrale Yopougon · MLX-07')
        self.assertContains(page, 'bon annulé', count=2)
        self.assertContains(page, 'CFAO')
        self.assertEqual(self.client.get(reverse('stock:article_historique', args=[999])).status_code, 404)

    def test_mouvements_sans_utilisateur(self):
        # Données d'exemple installées par la commande demo : mouvements sans utilisateur.
        MouvementStock.objects.update(utilisateur=None)
        self.client.force_login(self.chef)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.page('journal', '?du=2000-01-01').status_code, 200)

    def test_periode_et_stock_au_debut(self):
        aujourd_hui = timezone.localdate()
        du = aujourd_hui - timedelta(days=50)
        self.client.force_login(self.chef)
        page = self.client.get(self.url, {'du': du.isoformat(), 'au': (aujourd_hui - timedelta(days=5)).isoformat()})
        fiche = page.context['fiche']
        self.assertEqual((fiche['stock_debut'], fiche['valeur_debut']), (100, 500000))
        self.assertEqual((fiche['nb'], fiche['entrees'], fiche['sorties'], fiche['stock_fin']), (2, 20, 30, 90))
        self.assertContains(page, f'Stock au {du:%d/%m/%Y}')

    def test_pagination(self):
        self.entree(*[(self.ciment, 1, 5000)] * 60)
        self.client.force_login(self.chef)
        page = self.client.get(self.url)
        self.assertEqual(len(page.context['page'].object_list), 50)
        self.assertContains(page, 'mouvements 1 à 50')
        self.assertEqual(len(self.client.get(self.url, {'page': 2}).context['page'].object_list), 16)

    def test_excel(self):
        self.client.force_login(self.chef)
        du = timezone.localdate() - timedelta(days=50)
        rep = self.client.get(self.url, {'format': 'excel', 'du': du.isoformat()})
        self.assertIn('filename="fiche_stock_CIM-45_', rep['Content-Disposition'])
        ws = classeur(rep).active
        self.assertIn('CIM-45 – Ciment CPJ 45', ws['A2'].value)
        self.assertEqual(ws['C6'].value, f'Stock au {du:%d/%m/%Y}')
        self.assertEqual((ws['G6'].value, ws['K6'].value), (100, 500000))
        self.assertTrue(ws['C6'].font.b)
        references = [ws.cell(r, 2).value for r in range(7, ws.max_row)]
        self.assertEqual(references[:2], [self.s1.numero, self.e2.numero])
        self.assertEqual(ws.cell(7, 6).value, 30)
        total = [c.value for c in ws[ws.max_row]]
        self.assertEqual((total[0], total[4], total[5], total[6]), ('TOTAL', 24, 46, 78))
        self.assertEqual(ws.cell(ws.max_row - 1, 13).value, 'Bon annulé')

    def test_pdf(self):
        self.client.force_login(self.chef)
        with sans_compression():
            rep = self.client.get(self.url, {'format': 'pdf'})
        self.assertEqual(rep['Content-Type'], 'application/pdf')
        texte = ' '.join(pages_pdf(rep.content))
        for attendu in ('FICHE DE STOCK', 'CIM-45', self.e1.numero, self.s2.numero, 'CFAO', 'Totaux', "Tout l'historique",
                        '(bon annulé)', 'Page 1/1', 'Le responsable'):
            self.assertIn(attendu, texte)
        self.assertLess(texte.index(self.e1.numero), texte.index(self.s2.numero))


class ACommanderTests(RapportsBase):
    def setUp(self):
        super().setUp()
        self.sika = Fournisseur.objects.create(nom='Sika')
        self.entree((self.ciment, 5, 4800), fournisseur=self.cfao)
        self.entree((self.ciment, 1, 0), fournisseur=self.sika)  # dernier fournisseur, sans prix
        annule = self.entree((self.ciment, 1, 9999), fournisseur=self.cfao)
        services.annuler(annule, self.chef)
        self.entree((self.filtre, 1, 18000), fournisseur=self.sika)
        self.gant = Article.objects.create(code='GANT', designation='Gants', categorie=self.materiaux, stock_min=4)
        Article.objects.create(code='OK', designation='Bien fourni', categorie=self.materiaux, stock_min=1, stock=50)

    def test_donnees(self):
        donnees = rapports.a_commander(Article.objects.en_alerte())
        self.assertEqual([g['nom'] for g in donnees['groupes']], ['Sika', rapports.SANS_FOURNISSEUR])
        sika = {l['code']: l for l in donnees['groupes'][0]['lignes']}
        self.assertEqual((sika['CIM-45']['qte'], sika['CIM-45']['prix'], sika['CIM-45']['montant']), (14, 4800, 67200))
        self.assertEqual((sika['FH-01']['qte'], sika['FH-01']['prix']), (3, 18000))
        gant = donnees['groupes'][1]['lignes'][0]
        self.assertEqual((gant['qte'], gant['prix'], gant['montant']), (8, None, None))
        self.assertEqual(donnees['total'], 67200 + 54000)
        self.assertEqual(rapports.quantite_a_commander(Article(stock=30, stock_min=10)), 0)

    def test_page_excel_pdf(self):
        page = self.page('articles', '?filtre=alerte')
        self.assertContains(page, 'Imprimer la liste')
        self.assertContains(page, '<b>14</b>')
        self.assertNotContains(self.page('articles'), 'Imprimer la liste')
        rep = self.page('articles', '?filtre=alerte&format=excel')
        self.assertRegex(rep['Content-Disposition'], r'filename="a_commander_\d{4}-\d\d-\d\d\.xlsx"')
        ws = classeur(rep).active
        self.assertEqual(ws.title, 'À commander')
        lignes = [[c.value for c in ligne] for ligne in ws.iter_rows(min_row=6)]
        self.assertEqual([l[1] for l in lignes], ['CIM-45', 'FH-01', None, 'GANT', None, f'{3} article(s)'])
        self.assertEqual(lignes[0][6:9], [14, 4800, 67200])
        self.assertEqual(lignes[2][0], 'Sous-total Sika')
        self.assertTrue(ws.cell(8, 1).font.b)
        self.assertEqual(lignes[-1][8], 121200)
        with sans_compression():
            rep = self.page('articles', '?filtre=alerte&format=pdf', utilisateur=self.magasinier)
        texte = ' '.join(pages_pdf(rep.content))
        for attendu in ('ARTICLES À COMMANDER', 'Sika', rapports.SANS_FOURNISSEUR, 'Récapitulatif', 'CIM-45', 'GANT',
                        '4 800', '67 200', 'TOTAL GÉNÉRAL', 'Le responsable', 'Page 1/1'):
            self.assertIn(attendu, texte)
        self.assertNotIn('Bien fourni', texte)
        filtre = self.page('articles', f'?filtre=alerte&format=excel&categorie={self.filtres.pk}')
        self.assertEqual([c.value for c in classeur(filtre).active['B']][5:6], ['FH-01'])


def nb_pages(contenu):
    return len(re.findall(rb'/Type /Page\b(?!s)', contenu))


class EtiquettesTests(RapportsBase):
    def setUp(self):
        super().setUp()
        self.etagere = self.filtre.etagere
        self.r2 = Etagere.objects.create(bloc=self.etagere.bloc, code='R2', nb_niveaux=3)
        self.bloc_b = Bloc.objects.create(code='B', nom='Pneus', couleur='#f5d90a')
        self.p1 = Etagere.objects.create(bloc=self.bloc_b, code='P1')

    def imprimer(self, utilisateur=None, hote='testserver', **donnees):
        """POST de l'écran Étiquettes ; renvoie (réponse, adresses passées aux QR codes)."""
        self.client.force_login(utilisateur or self.chef)
        adresses = []
        from . import etiquettes
        dessiner = etiquettes._dessiner_qr

        def espion(c, texte, *args):
            adresses.append(texte)
            dessiner(c, texte, *args)
        with mock.patch.object(etiquettes, '_dessiner_qr', espion):
            reponse = self.client.post(reverse('stock:etiquettes'), donnees, HTTP_HOST=hote)
        return reponse, adresses

    def test_planche_articles_pdf_et_nombre_de_pages(self):
        from . import etiquettes
        Article.objects.bulk_create([Article(code=f'VIS-{i:03d}', designation=f'Vis {i}', categorie=self.filtres)
                                     for i in range(28)])
        with self.settings(ALLOWED_HOSTS=['192.168.1.20', 'testserver']):
            rep, adresses = self.imprimer(hote='192.168.1.20', format='a4-24', exemplaires='1', depart='1')
            self.assertEqual(rep.status_code, 200)
            self.assertEqual(rep['Content-Type'], 'application/pdf')
            self.assertRegex(rep['Content-Disposition'], r'filename="etiquettes_articles_\d{4}-\d\d-\d\d\.pdf"')
            self.assertTrue(rep.content.startswith(b'%PDF'))
            self.assertEqual(len(adresses), 30)
            self.assertEqual(nb_pages(rep.content), 2)  # 30 étiquettes, 24 par page
            self.assertEqual(nb_pages(self.imprimer(format='a4-65')[0].content), 1)
            self.assertEqual(nb_pages(self.imprimer(format='a4-24', exemplaires='3')[0].content), 4)  # 90
            self.assertEqual(nb_pages(self.imprimer(format='a4-24', depart='20')[0].content), 3)  # 19 + 30 = 49
            self.assertEqual(nb_pages(self.imprimer(format='a4-24', depart='999')[0].content), 3)  # ramené à 24
            self.assertEqual(nb_pages(self.imprimer(format='inconnu', exemplaires='abc')[0].content), 2)
        self.assertEqual([etiquettes.nombre_pages(n, 'a4-24') for n in (0, 1, 24, 25)], [0, 1, 1, 2])
        self.assertEqual(etiquettes.nombre_pages(65, 'a4-65'), 1)
        self.assertEqual(etiquettes.nombre_pages(10, 'a4-24', depart=20), 2)

    def test_contenu_des_qr_codes_et_textes(self):
        bizarre = Article.objects.create(code='AB 12/3#', designation='Article au code bizarre', categorie=self.filtres)
        with self.settings(ALLOWED_HOSTS=['192.168.1.20']), sans_compression():
            rep, adresses = self.imprimer(hote='192.168.1.20:8765', format='a4-24')
        self.assertEqual(adresses, ['http://192.168.1.20:8765/a/FH-01/', 'http://192.168.1.20:8765/a/AB%2012/3%23/',
                                    'http://192.168.1.20:8765/a/CIM-45/'])  # rangés dans l'ordre du magasin
        texte = ' '.join(pages_pdf(rep.content))
        for attendu in ('FH-01', 'Filtre à huile', 'Bloc A › Ét. R1 › Niv. 2', 'CIM-45', 'Ciment CPJ 45'):
            self.assertIn(attendu, texte)
        self.client.force_login(self.magasinier)
        chemin = adresses[1].removeprefix('http://192.168.1.20:8765')
        self.assertRedirects(self.client.get(chemin), f'/recherche/?article={bizarre.pk}',
                             fetch_redirect_response=False)

    def test_textes_ajustes_a_la_place(self):
        from . import etiquettes
        lignes, taille = etiquettes._couper_code('PDR-CAT-320D-000-XL-1R0739-FILTRE', 31 * mm, 15, 9)
        self.assertEqual(len(lignes), 2)
        self.assertTrue(all(stringWidth(l, 'Helvetica-Bold', taille) <= 31 * mm for l in lignes))
        self.assertEqual(etiquettes._couper_code('FH-01', 31 * mm, 15, 9), (['FH-01'], 15))
        long = 'Roulement à billes étanche pour moyeu de roue avant de chargeuse Caterpillar 950H (jeu de 2)'
        lignes = etiquettes._couper_texte(long, 'Helvetica', 8, 31 * mm, 3)
        self.assertEqual(len(lignes), 3)
        self.assertTrue(lignes[-1].endswith('…'))
        self.assertTrue(all(stringWidth(l, 'Helvetica', 8) <= 31 * mm for l in lignes))
        self.assertEqual(etiquettes._couper_texte('Graisse (seau 18 kg)', 'Helvetica', 8, 21 * mm, 3),
                         ['Graisse (seau', '18 kg)'])  # le nombre reste avec son unité
        self.filtre.case = 'Bac 4'
        self.assertEqual(etiquettes._emplacement(self.filtre, 200 * mm, 7.5)[0], 'Bloc A › Étagère R1 › Niv. 2 › Bac 4')
        self.assertEqual(etiquettes._emplacement(self.filtre, 17 * mm, 5.5)[0], 'A › R1 › Niv. 2')
        self.assertEqual(etiquettes._emplacement(self.ciment, 30 * mm, 7.5), ('', 0))

    def test_adresse_des_telephones(self):
        from . import etiquettes
        # Seule la fonction est remplacée : le module stock.reseau sert aussi d'intergiciel (middleware).
        with mock.patch('stock.reseau.adresse_publique', return_value='http://192.168.1.50:8765/') as adresse_publique:
            _, adresses = self.imprimer(hote='127.0.0.1:51234', choisi=[str(self.filtre.pk)], cases='1')
            self.assertEqual(adresses, ['http://192.168.1.50:8765/a/FH-01/'])
            adresse_publique.return_value = None  # accès des téléphones pas activé
            _, adresses = self.imprimer(hote='127.0.0.1:51234', choisi=[str(self.filtre.pk)], cases='1')
            self.assertEqual(adresses, ['http://127.0.0.1:51234/a/FH-01/'])
        self.assertTrue(etiquettes.adresse_locale('http://127.0.0.1:51234'))
        self.assertTrue(etiquettes.adresse_locale('http://localhost:8000'))
        self.assertFalse(etiquettes.adresse_locale('http://192.168.1.50:8765'))
        self.client.force_login(self.chef)
        self.assertContains(self.client.get(reverse('stock:etiquettes'), HTTP_HOST='127.0.0.1:8000'),
                            "qui ne s'ouvre que sur cet ordinateur")
        with self.settings(ALLOWED_HOSTS=['192.168.1.20']):
            self.assertNotContains(self.client.get(reverse('stock:etiquettes'), HTTP_HOST='192.168.1.20'),
                                   'que sur cet ordinateur')

    def test_cases_a_cocher_exemplaires_et_limite(self):
        _, adresses = self.imprimer(cases='1', choisi=[str(self.ciment.pk), '99999', 'x'], exemplaires='2')
        self.assertEqual(adresses, ['http://testserver/a/CIM-45/'] * 2)
        rep, adresses = self.imprimer(cases='1')
        self.assertRedirects(rep, reverse('stock:etiquettes'), fetch_redirect_response=False)
        self.assertEqual(adresses, [])
        messages = [str(m) for m in self.client.get(reverse('stock:etiquettes')).context['messages']]
        self.assertEqual(messages, ['Aucune étiquette à imprimer : cochez au moins un article.'])
        rep, _ = self.imprimer(cases='1', categorie=str(self.filtres.pk), choisi=[str(self.ciment.pk)])
        self.assertEqual(rep.status_code, 302)  # le ciment n'est pas dans la catégorie filtrée
        self.assertEqual(rep['Location'], f'{reverse("stock:etiquettes")}?categorie={self.filtres.pk}')
        with mock.patch('stock.views_magasin.LIMITE_ETIQUETTES', 3):
            rep, adresses = self.imprimer(exemplaires='2')
        self.assertEqual((rep.status_code, adresses), (302, []))

    def test_ecran_et_filtres(self):
        Article.objects.create(code='OLD-1', designation='Désactivé', categorie=self.filtres, actif=False)
        page = self.page('etiquettes')
        self.assertContains(page, '24 par page')
        self.assertContains(page, '65 par page')
        self.assertEqual([a.code for a in page.context['articles']], ['FH-01', 'CIM-45'])
        self.assertContains(page, '<b>2</b> étiquettes · <b>1</b> page A4', html=False)
        self.assertContains(page, 'name="choisi" value="%d" checked' % self.filtre.pk)
        self.assertContains(page, '<svg class="qr"')
        self.assertContains(page, 'http://testserver/a/FH-01/')

        def codes(params):
            return [a.code for a in self.page('etiquettes', params).context['articles']]
        self.assertEqual(codes(f'?categorie={self.materiaux.pk}'), ['CIM-45'])
        self.assertEqual(codes(f'?bloc={self.etagere.bloc.pk}'), ['FH-01'])
        self.assertEqual(codes(f'?etagere={self.r2.pk}'), [])
        self.assertEqual(codes('?q=ciment'), ['CIM-45'])
        self.assertEqual(codes(f'?article={self.ciment.pk}'), ['CIM-45'])
        Article.objects.filter(pk=self.ciment.pk).update(cree_le=timezone.now() - timedelta(days=40))
        Article.objects.filter(pk=self.filtre.pk).update(cree_le=None)  # ancien article jamais mouvementé
        nouveau = Article.objects.create(code='NEW-1', designation='Nouveau', categorie=self.filtres)
        self.assertIsNotNone(nouveau.cree_le)
        hier = timezone.localdate() - timedelta(days=1)
        self.assertEqual(codes(f'?depuis={hier:%Y-%m-%d}'), ['NEW-1'])
        self.assertEqual(codes('?depuis=nimporte'), ['FH-01', 'CIM-45', 'NEW-1'])
        page = self.page('etiquettes', f'?depuis={hier:%Y-%m-%d}&categorie={self.filtres.pk}')
        self.assertContains(page, f'<input type="hidden" name="depuis" value="{hier:%Y-%m-%d}">', html=False)
        with mock.patch('stock.views_magasin.LIMITE_CASES', 2):
            page = self.page('etiquettes')
        self.assertFalse(page.context['cases'])
        self.assertContains(page, 'ils seront tous imprimés')
        self.assertNotContains(page, 'name="choisi"')

    def test_etiquettes_etageres(self):
        page = self.page('etiquettes', f'?quoi=etageres&etagere={self.r2.pk}')
        self.assertEqual([e.code for e in page.context['etageres']], ['R1', 'R2'])  # le bloc de l'étagère
        self.assertContains(page, f'name="choisi" value="{self.r2.pk}" checked')
        self.assertNotContains(page, f'name="choisi" value="{self.etagere.pk}" checked')
        self.assertEqual(len(self.page('etiquettes', '?quoi=etageres').context['etageres']), 3)
        with sans_compression():
            rep, adresses = self.imprimer(quoi='etageres', choisi=[str(e.pk) for e in Etagere.objects.all()])
        self.assertEqual(rep.status_code, 200)
        self.assertRegex(rep['Content-Disposition'], r'filename="etiquettes_etageres_\d{4}-\d\d-\d\d\.pdf"')
        self.assertEqual(adresses, [f'http://testserver/e/{e.pk}/' for e in (self.etagere, self.r2, self.p1)])
        self.assertEqual(nb_pages(rep.content), 1)
        texte = ' '.join(pages_pdf(rep.content))
        for attendu in ('BLOC A', 'Pièces moteur', 'Bloc A – Étagère R2', 'Niveau 1 (bas)', 'Niveau 3', 'BLOC B'):
            self.assertIn(attendu, texte)
        Etagere.objects.bulk_create([Etagere(bloc=self.bloc_b, code=f'P{i}') for i in range(2, 4)])
        rep, _ = self.imprimer(quoi='etageres', choisi=[str(e.pk) for e in Etagere.objects.all()])
        self.assertEqual(nb_pages(rep.content), 2)  # 5 étagères, 4 par page
        rep, _ = self.imprimer(quoi='etageres', bloc=str(self.bloc_b.pk))
        self.assertEqual(rep['Location'], f'{reverse("stock:etiquettes")}?bloc={self.bloc_b.pk}&quoi=etageres')

    def test_routes_courtes(self):
        a, e = reverse('stock:qr_article', args=['FH-01']), reverse('stock:qr_etagere', args=[self.etagere.pk])
        self.assertEqual((a, e), ('/a/FH-01/', f'/e/{self.etagere.pk}/'))
        for chemin in (a, e):  # connexion exigée, puis retour à l'adresse scannée
            self.assertRedirects(self.client.get(chemin), f'{reverse("stock:connexion")}?next={chemin}',
                                 fetch_redirect_response=False)
        rep = self.client.post(reverse('stock:connexion'), {'username': 'koffi', 'password': 'motdepasse-solide',
                                                            'next': a})
        self.assertRedirects(rep, a, fetch_redirect_response=False)
        self.assertRedirects(self.client.get(a), f'/recherche/?article={self.filtre.pk}', fetch_redirect_response=False)
        self.assertRedirects(self.client.get('/a/fh-01/'), f'/recherche/?article={self.filtre.pk}',
                             fetch_redirect_response=False)
        self.assertRedirects(self.client.get(e), f'/articles/?etagere={self.etagere.pk}')
        self.assertEqual(self.client.get('/e/99999/').status_code, 404)
        rep = self.client.get('/a/ZZ-404/', follow=True)
        self.assertEqual(rep.redirect_chain, [('/articles/?q=ZZ-404', 302)])
        self.assertContains(rep, 'Aucun article n&#x27;a le code « ZZ-404 »')

    def test_reserve_aux_responsables(self):
        url = reverse('stock:etiquettes')
        self.assertRedirects(self.client.get(url), f'{reverse("stock:connexion")}?next={url}',
                             fetch_redirect_response=False)
        self.client.force_login(self.magasinier)
        self.assertRedirects(self.client.get(url), reverse('stock:accueil'), fetch_redirect_response=False)
        rep, adresses = self.imprimer(utilisateur=self.magasinier)
        self.assertEqual((rep.status_code, rep['Location'], adresses), (302, reverse('stock:accueil'), []))
        pages = [reverse('stock:accueil'), reverse('stock:articles'),
                 reverse('stock:article_historique', args=[self.filtre.pk])]
        for page in pages:  # menu, bouton de la liste des articles, bouton de la fiche de stock
            self.client.force_login(self.magasinier)
            self.assertNotContains(self.client.get(page), url)
            self.client.force_login(self.chef)
            self.assertContains(self.client.get(page), url)
        self.assertContains(self.page('articles', f'?categorie={self.filtres.pk}&page=2'),
                            f'{url}?categorie={self.filtres.pk}"')
        self.assertContains(self.client.get(pages[2]), f'{url}?article={self.filtre.pk}')
        plan = self.page('reglages_plan')
        self.assertContains(plan, f'{url}?quoi=etageres&amp;bloc={self.etagere.bloc.pk}')
        self.assertContains(plan, f'{url}?quoi=etageres&amp;etagere={self.r2.pk}')

    def test_douchette_dans_les_bons(self):
        from .views_magasin import code_scanne
        self.client.force_login(self.magasinier)

        def premier(q):
            return self.client.get(reverse('stock:api_articles'), {'q': q}).json()['articles'][0]['code']
        Article.objects.create(code='AB 12', designation='Code avec espace', categorie=self.filtres)
        self.assertEqual(premier('FH-01'), 'FH-01')
        self.assertEqual(premier('http://192.168.1.20:8765/a/FH-01/'), 'FH-01')
        self.assertEqual(premier('http://192.168.1.20:8765/a/CIM-45'), 'CIM-45')
        self.assertEqual(premier('http://127.0.0.1:8000/a/AB%2012/'), 'AB 12')
        self.assertEqual(code_scanne('filtre a/huile'), 'filtre a/huile')
        self.assertEqual(code_scanne('http://x/e/3/'), 'http://x/e/3/')


class DateDeCreationMiseAJourTests(TransactionTestCase):
    """Date de création des articles existants à la mise à jour : les vraies migrations, sur la base des tests."""

    def setUp(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        dernieres = MigrationExecutor(connection).loader.graph.leaf_nodes('stock')
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(dernieres))
        self.recu = timezone.make_aware(datetime(2026, 3, 2, 8, 0))

    def migrer(self, nom):
        """Amène la base à cette migration → modèles de ce moment-là."""
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        return MigrationExecutor(connection).migrate([('stock', nom)]).apps

    def test_mise_a_jour_depuis_la_1_5_0(self):
        anciens = self.migrer('0007_mouvement_prix_moyen_apres')  # juste avant l'ajout de la date de création
        categorie = anciens.get_model('stock', 'Categorie').objects.create(nom='Filtres')
        Ancien = anciens.get_model('stock', 'Article')
        filtre = Ancien.objects.create(code='FH-01', designation='Filtre à huile', categorie=categorie)
        Ancien.objects.create(code='CIM-45', designation='Ciment', categorie=categorie)
        anciens.get_model('stock', 'MouvementStock').objects.create(
            article=filtre, type='ENTREE', quantite=5, stock_apres=5, reference='BE-2026-00001', date=self.recu)
        nouveaux = self.migrer('0008_article_cree_le')
        dates = dict(nouveaux.get_model('stock', 'Article').objects.values_list('code', 'cree_le'))
        self.assertEqual(dates, {'FH-01': self.recu, 'CIM-45': None})  # jamais mouvementé : sans date

    def test_reparation_des_copies_passees_en_1_6_0(self):
        # En 1.6.0, 0008 donnait aux articles jamais mouvementés l'heure de la mise à jour, prise juste avant
        # qu'elle soit notée comme faite.
        from django.db.migrations.recorder import MigrationRecorder
        faite = MigrationRecorder.Migration.objects.get(app='stock', name='0008_article_cree_le').applied
        categorie = Categorie.objects.create(nom='Filtres')

        def article(code, cree_le=None):
            a = Article.objects.create(code=code, designation=code, categorie=categorie)
            if cree_le:
                Article.objects.filter(pk=a.pk).update(cree_le=cree_le)
            return a

        def entree(a, le=None):
            MouvementStock.objects.create(article=a, type=MouvementStock.ENTREE, quantite=5, stock_apres=5,
                                          reference='BE-2026-00001', date=le or timezone.now())
        entree(article('FH-01', self.recu), self.recu)  # daté par 0008 d'après son premier mouvement
        article('CIM-45', faite - timedelta(seconds=1))  # jamais mouvementé
        entree(article('PN-01', faite - timedelta(seconds=1)))  # mouvementé seulement après la mise à jour
        article('NEUF')  # créé après la mise à jour
        article('ANCIEN', faite - timedelta(days=2))  # loin de l'heure de la mise à jour : on n'y touche pas
        self.migrer('0012_fusion_demarrage')
        self.migrer('0013_reparer_cree_le')
        dates = dict(Article.objects.values_list('code', 'cree_le'))
        self.assertEqual(dates['FH-01'], self.recu)
        self.assertEqual((dates['CIM-45'], dates['PN-01']), (None, None))
        self.assertGreater(dates['NEUF'], faite)
        self.assertEqual(dates['ANCIEN'], faite - timedelta(days=2))


# =========================================================
# Bien démarrer : import Excel des articles, premiers pas, magasin d'exemple
# =========================================================

TITRES_MODELE = ('Code', 'Désignation', 'Catégorie', 'Unité', 'Stock de départ', 'Prix unitaire', 'Emplacement',
                 'Stock minimum', 'Mots-clés', 'Référence fabricant')


def fichier_excel(lignes, titres=TITRES_MODELE, nom='articles.xlsx', avant=()):
    """Fichier .xlsx en mémoire : lignes « avant » (titre…), titres des colonnes, puis les lignes."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for ligne in list(avant) + [list(titres)] + [list(l) for l in lignes]:
        ws.append(ligne)
    fichier = io.BytesIO()
    wb.save(fichier)
    fichier.seek(0)
    fichier.name = nom
    return fichier


def classeur_modifie(fichier, nom, contenu):
    """Copie d'un .xlsx où le morceau « nom » est remplacé par contenu (ajouté s'il manque, retiré si None)."""
    import zipfile

    sortie = io.BytesIO()
    with zipfile.ZipFile(fichier) as source, zipfile.ZipFile(sortie, 'w', zipfile.ZIP_DEFLATED) as copie:
        for info in source.infolist():
            if info.filename != nom:
                copie.writestr(info, source.read(info.filename))
        if contenu is not None:
            copie.writestr(nom, contenu)
    return sortie.getvalue()


class ImportBase(TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        from django.test import override_settings

        dossier = tempfile.TemporaryDirectory()
        self.addCleanup(dossier.cleanup)
        reglage = override_settings(DATA_DIR=Path(dossier.name))
        reglage.enable()
        self.addCleanup(reglage.disable)
        Utilisateur = get_user_model()
        self.chef = Utilisateur.objects.create_user('chef', password='x', is_staff=True, is_superuser=True)
        self.magasinier = Utilisateur.objects.create_user('awa', password='x')
        self.bloc_a = Bloc.objects.create(code='A')
        self.r1 = Etagere.objects.create(bloc=self.bloc_a, code='R1', nb_niveaux=4)
        self.filtres = Categorie.objects.create(nom='Filtres')
        self.filtre = Article.objects.create(code='FH-01', designation='Filtre à huile', categorie=self.filtres)
        self.client.force_login(self.chef)

    def analyser(self, lignes, titres=TITRES_MODELE, **options):
        from . import imports

        feuille, lues = imports.lire(fichier_excel(lignes, titres, **options), 'test.xlsx')
        return imports.analyser(lues, 'test.xlsx', feuille)

    def ligne(self, analyse, numero):
        return next(l for l in analyse.lignes if l.numero == numero)

    def envoyer(self, fichier):
        return self.client.post(reverse('stock:import_articles'), {'fichier': fichier})


class ImportApercuTests(ImportBase):
    def test_modele_a_telecharger(self):
        from openpyxl import load_workbook

        rep = self.client.get(reverse('stock:import_modele'))
        self.assertIn('spreadsheetml', rep['Content-Type'])
        self.assertIn('modele_import_articles.xlsx', rep['Content-Disposition'])
        wb = load_workbook(io.BytesIO(rep.content))
        self.assertEqual(wb.sheetnames, ['Articles', 'Aide'])
        self.assertEqual([c.value for c in wb['Articles'][1]][:2], ['Code', 'Désignation *'])
        self.assertEqual(wb['Articles'].max_row, 1)  # aucun exemple à effacer dans la feuille à remplir
        validation = wb['Articles'].data_validations.dataValidation[0]
        self.assertIn('SAC', validation.formula1)
        aide = ' '.join(str(c.value) for ligne in wb['Aide'].iter_rows() for c in ligne if c.value)
        for attendu in ('Désignation', 'Emplacement', 'Stock de départ', 'Kilogramme', 'pcs', 'Bloc A', 'R1 (4)'):
            self.assertIn(attendu, aide)
        # Le modèle rempli tel quel est relu sans erreur.
        ws = wb['Articles']
        ws.append(['', 'Ciment CPJ 45', 'Matériaux', 'SAC', 100, 5200, 'A-R1-1', 10, 'ciment', ''])
        fichier = io.BytesIO()
        wb.save(fichier)
        fichier.seek(0)
        from . import imports

        analyse = imports.analyser(imports.lire(fichier, 'modele.xlsx')[1])
        self.assertEqual(len(analyse.prets), 1)

    def test_reserve_aux_responsables(self):
        self.client.force_login(self.magasinier)
        for url in ('import_articles', 'import_apercu', 'import_resultat', 'import_modele', 'import_rapport'):
            self.assertRedirects(self.client.get(reverse(f'stock:{url}')), reverse('stock:accueil'),
                                 msg_prefix=url)
        rep = self.client.post(reverse('stock:import_articles'), {'fichier': fichier_excel([('X', 'Truc')])})
        self.assertRedirects(rep, reverse('stock:accueil'))
        self.assertRedirects(self.client.post(reverse('stock:import_confirmer')), reverse('stock:accueil'))
        self.assertEqual(Article.objects.count(), 1)
        self.assertNotContains(self.client.get(reverse('stock:articles')), reverse('stock:import_articles'))

    def test_apercu_avant_import_sans_rien_ecrire(self):
        rep = self.envoyer(fichier_excel([('CIM-45', 'Ciment CPJ 45', 'Matériaux', 'sac', 400, 5200, 'A-R1-1', 100),
                                     ('', '', 'Matériaux', 'sac', 1, 1)]))
        self.assertRedirects(rep, reverse('stock:import_apercu'))
        rep = self.client.get(reverse('stock:import_apercu'))
        self.assertContains(rep, "Vérifiez avant d'importer")
        self.assertContains(rep, 'Importer 1 article')
        self.assertContains(rep, 'Désignation vide')
        self.assertContains(rep, 'Matériaux')  # nouvelle catégorie annoncée
        self.assertEqual(rep.context['analyse'].avec_stock[0].stock, 400)
        self.assertEqual(Article.objects.count(), 1)
        self.assertFalse(Categorie.objects.filter(nom='Matériaux').exists())
        self.assertFalse(BonEntree.objects.exists())
        excel = self.client.get(reverse('stock:import_apercu') + '?format=excel')
        self.assertIn('spreadsheetml', excel['Content-Type'])

    def test_erreurs_et_avertissements_ligne_par_ligne(self):
        carton = 'CARTON' if 'CARTON' in dict(Article.UNITES) else 'U'
        analyse = self.analyser([
            ('', '', 'Filtres'),                                          # 2 : désignation vide
            ('fh-01', 'Autre filtre', 'Filtres'),                        # 3 : code déjà dans le logiciel
            ('NEW-1', 'Nouveau 1', 'Filtres', 'pcs', '1 250', '3 500 F'),  # 4
            ('new-1', 'Nouveau bis', 'Filtres'),                         # 5 : code en double dans le fichier
            ('', 'Filtre à huile'),                                      # 6 : déjà dans le logiciel sans code
            ('B-2', 'Boulon', '', 'xyz', '12,5', 'N/A'),                 # 7 : unité inconnue, sans catégorie
            ('B-3', 'Bidule', 'Filtres', 'ctn', 'abc'),                  # 8 : stock illisible
            ('B-4', 'Truc', 'Filtres', 'sacs', -2),                      # 9 : négatif
            ('B-5', 'Courroie', 'Pièces moteur', 'Pièces', 3, 27500, 'A-R1-2'),
            ('B-6', 'Graisse', 'Lubrifiants', 'kg', 5, '', 'A-R1-9'),     # 11 : niveau inexistant
            ('B-7', 'Pneu', 'Pneus', 'U', 2, '', 'B-P1-3'),               # 12 : étagère absente du plan
            ('B-8', 'Casque', 'EPI', 'U', '', '', 'Bloc A › Étagère R1 › Niveau 3'),
            ('B-9', 'Gants', 'EPI', 'paires', '', '', 'A'),              # 14 : emplacement incomplet
            ('', 'Joint', 'Filtres', 'ctn'),                             # 15 : code automatique
            ('TOTAL', '12 article(s)'),                                  # 16 : ligne de total d'un export
        ])
        l = lambda n: self.ligne(analyse, n)  # noqa: E731
        self.assertEqual(l(2).statut, 'erreur')
        self.assertIn('Désignation vide', l(2).messages[0])
        self.assertIn('existe déjà dans le logiciel (Filtre à huile)', l(3).messages[0])
        self.assertEqual((l(4).statut, l(4).unite, l(4).stock, l(4).prix), ('ok', 'U', 1250, 3500))
        self.assertIn('déjà utilisé ligne 4', l(5).messages[0])
        self.assertEqual(l(6).statut, 'erreur')
        self.assertIn('existe déjà dans le logiciel (code FH-01)', l(6).messages[0])
        self.assertEqual((l(7).statut, l(7).unite, l(7).stock, l(7).prix, l(7).categorie),
                         ('avertissement', 'U', Decimal('12.5'), None, 'Divers'))
        self.assertIn('Unité « xyz » inconnue', l(7).messages[0])
        self.assertEqual(l(8).statut, 'erreur')
        self.assertIn('« abc » n\'est pas un nombre', ' '.join(l(8).messages))
        self.assertIn('négatif', l(9).messages[0])
        self.assertEqual((l(10).statut, l(10).unite, l(10).etagere, l(10).niveau), ('ok', 'U', 'R1', 2))
        self.assertEqual((l(11).statut, l(11).niveau), ('avertissement', None))
        self.assertIn("n'a que 4 niveaux", l(11).messages[0])
        self.assertEqual((l(12).statut, l(12).nouvel_emplacement, l(12).emplacement), ('ok', True, 'B-P1-3'))
        self.assertEqual((l(13).etagere, l(13).niveau), ('R1', 3))
        self.assertIn('incomplet', l(14).messages[-1])
        self.assertEqual(l(14).etagere, '')
        self.assertEqual((l(15).code, l(15).code_auto, l(15).unite), ('FIL-0001', True, carton))
        self.assertIn('Ligne de total', l(16).messages[0])
        self.assertEqual(analyse.manquants, {'B': {'existe': False, 'etageres': {'P1': 3}}})
        self.assertEqual(analyse.nouvelles_categories, ['Divers', 'EPI', 'Lubrifiants', 'Pièces moteur', 'Pneus'])
        self.assertEqual(analyse.sans_categorie, 1)

    def test_titres_d_un_fichier_client(self):
        analyse = self.analyser(
            [('R-1', 'Fer de 10', 'Matériaux', 'barre', 300, '4 200 FCFA', 'A/R1/2', 50, 'fer 10', 'à revoir'),
             ('R-2', 'Ciment', 'Matériaux', 'sacs', '1.250,5', '5200', '', '', '', '')],
            titres=('Réf.', 'Libellé', 'Famille', 'U', 'Qté', 'PU (FCFA)', 'Localisation', 'Seuil', 'Autres noms',
                    'Observations'),
            avant=[['INVENTAIRE DU MAGASIN'], []])
        self.assertEqual([(t, c.cle if c else None) for t, c, _ in analyse.colonnes][-1], ('Observations', None))
        fer, ciment = analyse.lignes
        self.assertEqual((fer.numero, fer.code, fer.designation, fer.stock, fer.prix, fer.stock_min, fer.mots_cles),
                         (4, 'R-1', 'Fer de 10', 300, 4200, 50, 'fer 10'))
        self.assertEqual((fer.etagere, fer.niveau), ('R1', 2))
        self.assertEqual((ciment.unite, ciment.stock), ('SAC', Decimal('1250.5')))

    def test_csv_excel_francais_et_csv_a_virgules(self):
        from . import imports

        texte = ('Code;Désignation;Catégorie;Unité;Stock de départ;Prix unitaire\n'
                 'C-1;Câble 2,5 mm²;Électricité;m;12,5;850\n')
        feuille, lignes = imports.lire(io.BytesIO(texte.encode('cp1252')), 'export.csv')
        ligne = imports.analyser(lignes).lignes[0]
        self.assertEqual((feuille, ligne.designation, ligne.categorie, ligne.unite, ligne.stock),
                         ('CSV', 'Câble 2,5 mm²', 'Électricité', 'M', Decimal('12.5')))
        texte = 'Code,Désignation,Unité,Stock de départ\nC-2,"Tuyau PVC 100, gris",u,"1,5"\n'
        ligne = imports.analyser(imports.lire(io.BytesIO(texte.encode('utf-8-sig')), 'a.csv')[1]).lignes[0]
        self.assertEqual((ligne.designation, ligne.stock), ('Tuyau PVC 100, gris', Decimal('1.5')))

    def test_fichiers_refuses(self):
        cas = [
            (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\0' * 100, 'ancien.xls', 'format Excel ancien (.xls)'),
            (b'%PDF-1.4 ...', 'bon.pdf', 'Choisissez un fichier Excel'),
            (b'PK\x03\x04 pas un vrai classeur', 'casse.xlsx', "n'a pas pu être lu"),
            ('Référence;Quantité\nA;1\n'.encode(), 'sans_titre.csv', 'Colonne « Désignation » introuvable'),
            ('Désignation;Code\n'.encode(), 'vide.csv', 'Aucun article dans ce fichier'),
            # Guillemet jamais fermé : toute la suite du fichier tombe dans une seule cellule, trop longue.
            ('Désignation;Code\n"Ciment;C-1\n'.encode() + b'x' * 200_000, 'guillemet.csv', 'CSV est mal formé'),
            (fichier_excel([('C-1', 'Ciment')]).getvalue()[:400], 'coupe.xlsx', "n'a pas pu être lu"),
            (classeur_modifie(fichier_excel([('C-1', 'Ciment')]), 'xl/workbook.xml', None), 'sans_classeur.xlsx',
             "n'a pas pu être lu"),
        ]
        from django.core.files.uploadedfile import SimpleUploadedFile

        for contenu, nom, message in cas:
            with self.subTest(nom):
                rep = self.envoyer(SimpleUploadedFile(nom, contenu))
                self.assertEqual(rep.status_code, 200)
                self.assertContains(rep, message.replace("'", '&#x27;'))
        from pathlib import Path

        from django.conf import settings

        self.assertEqual(list((Path(settings.DATA_DIR) / 'imports').iterdir()), [])
        self.assertRedirects(self.client.get(reverse('stock:import_apercu')), reverse('stock:import_articles'))

    def test_fichier_abime_decouvert_en_lisant(self):
        """Classeur qui s'ouvre, mais dont la feuille est abîmée : message clair, jamais une erreur 500."""
        import zipfile

        from django.core.files.uploadedfile import SimpleUploadedFile

        feuille = 'xl/worksheets/sheet1.xml'
        bon = fichier_excel([('C-1', 'Ciment')])
        with zipfile.ZipFile(bon) as archive:
            xml = archive.read(feuille)
        fin = b'</row></sheetData>'
        cas = {
            'coupee.xlsx': xml[:len(xml) * 3 // 4],  # XML interrompu
            'texte_absent.xlsx': xml.replace(fin, b'<c r="K2" t="s"><v>99</v></c>' + fin),  # texte partagé inexistant
            'nombre_illisible.xlsx': xml.replace(fin, b'<c r="K2"><v>' + b'9' * 5000 + b'</v></c>' + fin),
        }
        for nom, contenu in cas.items():
            with self.subTest(nom), self.assertLogs('stock', 'WARNING'):
                rep = self.envoyer(SimpleUploadedFile(nom, classeur_modifie(bon, feuille, contenu)))
                self.assertEqual(rep.status_code, 200)
                self.assertContains(rep, 'n&#x27;a pas pu être lu')

    def test_valeurs_geantes(self):
        """Nombres de 30 chiffres, 1E+30 venu d'Excel, niveau de 5 000 chiffres : ligne refusée, pas d'erreur 500."""
        rep = self.envoyer(fichier_excel([
            ('G-1', 'Géant', 'Divers', 'U', 1e30, '9' * 30, 'A-R1-' + '9' * 5000, '1' * 40),
            ('G-2', 'Normal', 'Divers', 'U', 2, 100)]))
        self.assertRedirects(rep, reverse('stock:import_apercu'))
        rep = self.client.get(reverse('stock:import_apercu'))
        geant, normal = rep.context['analyse'].lignes
        self.assertEqual((geant.statut, normal.statut), ('erreur', 'ok'))
        for attendu in ('Stock de départ trop grand', 'Prix unitaire trop grand', 'Stock minimum trop grand',
                        'illisible : niveau laissé vide'):
            self.assertIn(attendu, ' '.join(geant.messages))
        self.assertLess(max(len(m) for m in geant.messages), 80)  # valeurs coupées dans les messages
        texte = 'Désignation;Stock de départ;Prix unitaire\nGéant;' + '9' * 30 + ';1E+30\n'
        from . import imports

        geant = imports.analyser(imports.lire(io.BytesIO(texte.encode()), 'a.csv')[1]).lignes[0]
        self.assertIn('Stock de départ trop grand', geant.messages[0])
        self.assertIn('« 1E+30 » n\'est pas un nombre', geant.messages[1])

    def test_bombe_de_decompression(self):
        """Un petit .xlsx qui cache des dizaines de Mo est refusé avant d'être ouvert (mémoire du PC du bureau)."""
        import os

        from django.core.files.uploadedfile import SimpleUploadedFile

        from . import imports

        bon = fichier_excel([('C-1', 'Ciment')])
        bombe = classeur_modifie(bon, 'xl/sharedStrings.xml', b' ' * (11 * 1024 * 1024))
        self.assertLess(len(bombe), 100_000)
        with mock.patch('openpyxl.load_workbook') as ouvrir:
            rep = self.envoyer(SimpleUploadedFile('bombe.xlsx', bombe))
        ouvrir.assert_not_called()
        self.assertContains(rep, 'trop gros une fois ouvert')
        self.assertContains(rep, 'modèle à télécharger')
        # Au-delà de la limite, même bien compressé normalement.
        with mock.patch.object(imports, 'MAX_OCTETS_OUVERT', 1024 * 1024):
            gros = classeur_modifie(bon, 'xl/media/photo.xml', os.urandom(600 * 1024).hex().encode())
            with self.assertRaisesMessage(imports.ImportImpossible, 'trop gros une fois ouvert'):
                imports.lire(io.BytesIO(gros), 'gros.xlsx')
            self.assertEqual(imports.lire(bon, 'bon.xlsx')[1][1][1][:2], ['C-1', 'Ciment'])

    def test_cellule_tout_au_bout_de_la_feuille(self):
        """Une cellule en colonne XFD ne fait pas remplir chaque ligne de 16 384 cases."""
        from openpyxl import Workbook

        from . import imports

        wb = Workbook()
        ws = wb.active
        ws.append(TITRES_MODELE)
        for i in range(1, 51):
            ws.append([f'C-{i}', f'Article {i}'])
            ws.cell(i + 1, 16384, 'oubli')
        fichier = io.BytesIO()
        wb.save(fichier)
        _, lignes = imports.lire(fichier, 'large.xlsx')
        self.assertEqual({len(valeurs) for _, valeurs in lignes}, {imports.MAX_COLONNES})
        self.assertEqual(len(imports.analyser(lignes).prets), 50)

    def test_points_des_milliers(self):
        """« 5.000 F » est un prix de 5 000 F ; une quantité « 2.500 » reste 2,5 mais est « à vérifier »."""
        from . import imports

        prix = {'5.000 F': 5000, '5.000': 5000, '12.500 FCFA': 12500, '1.250.000': 1250000, '-5.000': -5000,
                '12.5': Decimal('12.5'), '5,5': Decimal('5.5'), '1 250,50': Decimal('1250.5'),
                '1.250,50': Decimal('1250.5'), '1,250.50': Decimal('1250.5'), '0.500': Decimal('0.5'),
                '1.2345': Decimal('1.23'), '12.50': Decimal('12.5')}
        for texte, attendu in prix.items():
            with self.subTest(prix=texte):
                self.assertEqual(imports._nombre(texte, 2, milliers=True), attendu)
        quantites = {'2.500': Decimal('2.5'), '12.5': Decimal('12.5'), '1.500.000': 1500000,
                     '1 250,5': Decimal('1250.5')}
        for texte, attendu in quantites.items():
            with self.subTest(quantite=texte):
                self.assertEqual(imports._nombre(texte, 3), attendu)
        self.assertEqual(imports._nombre(2.5, 3, milliers=True), Decimal('2.5'))  # nombre d'Excel : jamais touché
        for texte in ('1.000.5', '5.00.000', '1.2.3'):
            with self.subTest(refuse=texte), self.assertRaises(ValueError):
                imports._nombre(texte, 2, milliers=True)

        analyse = self.analyser([
            ('P-1', 'Ciment', 'Matériaux', 'sac', '400', '5.000 F'),
            ('P-2', 'Gasoil', 'Carburant', 'L', '2.500', '1.250.000'),
            ('P-3', 'Fer', 'Matériaux', 'barre', 2.5, '12.5', '', '1.000'),
        ])
        ciment, gasoil, fer = analyse.lignes
        self.assertEqual((ciment.statut, ciment.stock, ciment.prix), ('ok', 400, 5000))
        self.assertEqual((gasoil.statut, gasoil.stock, gasoil.prix), ('avertissement', Decimal('2.5'), 1250000))
        self.assertEqual(gasoil.messages,
                         ['Stock de départ « 2.500 » lu 2,5 : pour 2\xa0500, écrivez 2500 sans point.'])
        self.assertEqual((fer.statut, fer.stock, fer.prix, fer.stock_min), ('avertissement', Decimal('2.5'),
                                                                            Decimal('12.5'), 1))
        self.assertEqual(fer.messages, ['Stock minimum « 1.000 » lu 1 : pour 1\xa0000, écrivez 1000 sans point.'])

    def test_caracteres_de_controle_dans_le_fichier(self):
        texte = ('Code;Désignation;Catégorie\nC-1;Ciment\x07 CPJ\x1b 45;Maté\x00riaux\n'.encode()
                 + 'C-2;Fer\x0bà béton;Matériaux\n'.encode())
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.assertRedirects(self.envoyer(SimpleUploadedFile('ctrl.csv', texte)), reverse('stock:import_apercu'))
        ciment, fer = self.client.get(reverse('stock:import_apercu')).context['analyse'].lignes
        self.assertEqual((ciment.designation, ciment.categorie, fer.designation), ('Ciment CPJ 45', 'Matériaux',
                                                                                   'Fer à béton'))
        excel = self.client.get(reverse('stock:import_apercu') + '?format=excel')
        self.assertEqual(excel.status_code, 200)
        self.assertIn('Ciment CPJ 45', [c.value for ligne in classeur(excel).active.iter_rows() for c in ligne])

    def test_reimport_d_un_export_du_logiciel(self):
        from . import exports, imports

        casque = Article.objects.create(code='EPI-CAS', designation='Casque', categorie=self.filtres, unite='KG',
                                        etagere=self.r1, niveau=2, case='bac 3', stock_min=5)
        export = exports.etat_stock(Article.objects.order_by('code'))
        casque.delete()
        analyse = imports.analyser(imports.lire(io.BytesIO(export.content), 'etat_stock.xlsx')[1])
        self.assertEqual([l.statut for l in analyse.lignes], ['ok', 'erreur', 'erreur'])  # EPI-CAS, FH-01, TOTAL
        recree = analyse.lignes[0]
        self.assertEqual((recree.code, recree.unite, recree.etagere, recree.niveau, recree.case, recree.stock_min),
                         ('EPI-CAS', 'KG', 'R1', 2, 'bac 3', 5))


class ImportArticlesTests(ImportBase):
    def importer(self, fichier, **options):
        self.assertRedirects(self.envoyer(fichier), reverse('stock:import_apercu'))
        return self.client.post(reverse('stock:import_confirmer'), options)

    def test_message_affiche_une_seule_fois(self):
        for url in (reverse('stock:import_articles'), reverse('stock:etiquettes'),
                    reverse('stock:article_historique', args=[self.filtre.pk])):
            self.client.get(reverse('stock:import_apercu'))  # sans fichier : un message pour la page suivante
            self.assertEqual(self.client.get(url).content.decode().count('abord le fichier à importer'), 1, url)

    def test_messages_affiches_seulement_par_la_page_de_base(self):
        from pathlib import Path
        for gabarit in (Path(__file__).parent / 'templates').rglob('*.html'):
            texte = gabarit.read_text(encoding='utf-8')
            if "{% extends 'stock/base.html' %}" in texte:
                self.assertNotIn('_messages.html', texte, gabarit.name)

    def test_import_en_un_seul_bon_de_stock_de_depart(self):
        rep = self.importer(fichier_excel([
            ('CIM-45', 'Ciment CPJ 45', 'Matériaux', 'sac', 400, 5200, 'A-R1-1', 100, 'ciment, sac de ciment'),
            ('FER-10', 'Fer à béton HA 10', 'Matériaux', 'U', 300, '4 200 F', 'A-R1-2', 50),
            ('', 'Disque à meuler', 'Outillage', 'U', '', 1200),             # prix sans stock
            ('', 'Gants', 'EPI', 'U', 15),                                    # stock sans prix
            ('FH-01', 'Doublon', 'Filtres', 'U', 5, 100),                     # ignoré
        ]))
        self.assertRedirects(rep, reverse('stock:import_resultat'))
        self.assertEqual(Article.objects.count(), 5)
        bon = BonEntree.objects.get()
        self.assertEqual(bon.statut, BonEntree.VALIDE)
        self.assertTrue(bon.observation.startswith('Stock de départ (import Excel du '))
        self.assertEqual(bon.lignes.count(), 3)
        self.assertEqual(MouvementStock.objects.filter(reference=bon.numero).count(), 3)
        ciment = Article.objects.get(code='CIM-45')
        self.assertEqual((ciment.stock, ciment.prix_moyen, ciment.unite, ciment.etagere, ciment.niveau,
                          ciment.stock_min), (400, 5200, 'SAC', self.r1, 1, 100))
        self.assertEqual(ciment.categorie.nom, 'Matériaux')
        disque = Article.objects.get(designation='Disque à meuler')
        self.assertEqual((disque.code, disque.stock, disque.prix_moyen), ('OUT-0001', 0, 1200))
        self.assertEqual(Article.objects.get(designation='Gants').stock, 15)
        self.assertEqual(sorted(Categorie.objects.values_list('nom', flat=True)),
                         ['EPI', 'Filtres', 'Matériaux', 'Outillage'])
        rep = self.client.get(reverse('stock:import_resultat'))
        self.assertContains(rep, '4 articles créés')
        self.assertContains(rep, '1 ligne ignorée')
        self.assertContains(rep, bon.numero)
        self.assertContains(rep, f'{4000 * 520 + 300 * 4200:,}'.replace(',', '\xa0'))  # espace insécable
        # Rapport : chaque ligne, créée ou ignorée, avec la raison.
        from openpyxl import load_workbook

        rapport = load_workbook(io.BytesIO(self.client.get(reverse('stock:import_rapport')).content)).active
        lignes = [[c.value for c in l] for l in rapport.iter_rows(min_row=6)]
        self.assertEqual([l[1] for l in lignes], ['Créé', 'Créé', 'Créé', 'Créé', 'Ignoré'])
        self.assertIn('existe déjà', lignes[-1][-1])
        self.assertEqual(lignes[2][2], 'OUT-0001 (automatique)')
        # Un second envoi du même formulaire ne crée rien de plus.
        self.assertRedirects(self.client.post(reverse('stock:import_confirmer')), reverse('stock:import_resultat'))
        self.assertEqual((Article.objects.count(), BonEntree.objects.count()), (5, 1))

    def test_emplacements_crees_ou_laisses_non_ranges(self):
        lignes = [('P-1', 'Pneu', 'Pneus', 'U', 2, 1000, 'B-P1-6'), ('P-2', 'Chambre', 'Pneus', 'U', 2, 0, 'B-P2-1'),
                  ('P-3', 'Valve', 'Pneus', 'U', 1, 0, 'A-R9-1')]
        self.importer(fichier_excel(lignes), emplacements='non')
        self.assertFalse(Bloc.objects.filter(code='B').exists())
        self.assertEqual(Article.objects.filter(code__startswith='P-', etagere__isnull=True).count(), 3)
        self.assertContains(self.client.get(reverse('stock:import_resultat')), '3 articles laissés « non rangés »')

        self.importer(fichier_excel([(f'Q{code[1:]}', *reste) for code, *reste in lignes]), emplacements='creer')
        pneu, chambre, valve = Article.objects.filter(code__startswith='Q-').order_by('code')
        self.assertEqual((pneu.etagere.bloc.code, pneu.etagere.code, pneu.niveau, pneu.etagere.nb_niveaux),
                         ('B', 'P1', 6, 6))
        self.assertEqual((chambre.etagere.code, chambre.etagere.nb_niveaux), ('P2', 4))
        self.assertEqual((valve.etagere.bloc, valve.etagere.code), (self.bloc_a, 'R9'))
        bloc_b = Bloc.objects.get(code='B')
        self.assertGreaterEqual(bloc_b.x, self.bloc_a.x + self.bloc_a.largeur)  # à droite du bloc A, sans le chevaucher
        p1, p2 = bloc_b.etageres.order_by('code')
        self.assertLessEqual(p2.x + p2.largeur, bloc_b.largeur)
        self.assertGreater(valve.etagere.z, self.r1.z + self.r1.profondeur)  # derrière l'étagère R1 existante
        self.assertContains(self.client.get(reverse('stock:import_resultat')), 'Vérifiez leur place')

    def test_etiquettes_des_seuls_articles_de_l_import(self):
        """« Imprimer leurs étiquettes » : les articles de cet import, pas ceux d'un autre import du même jour."""
        from urllib.parse import parse_qs, urlsplit

        from . import etiquettes

        self.importer(fichier_excel([('M-1', 'Matin 1', 'Divers'), ('M-2', 'Matin 2', 'Divers')]))
        self.importer(fichier_excel([('S-1', 'Soir 1', 'Divers'), ('S-2', 'Soir 2', 'Divers')]))
        lien = self.client.get(reverse('stock:import_resultat')).context['lien_etiquettes']
        page = self.client.get(lien)
        self.assertEqual(sorted(a.code for a in page.context['articles']), ['S-1', 'S-2'])
        self.assertContains(page, "Seulement les articles créés depuis l'import du")
        # Le filtre suit le formulaire d'impression et celui des filtres.
        apres = parse_qs(urlsplit(lien).query)['apres'][0]
        self.assertContains(page, f'<input type="hidden" name="apres" value="{apres}">', count=2, html=False)
        imprimees = []
        with mock.patch.object(etiquettes, '_dessiner_qr', lambda c, texte, *args: imprimees.append(texte)):
            rep = self.client.post(reverse('stock:etiquettes'), {'apres': apres, 'format': 'a4-24'})
        self.assertEqual(rep['Content-Type'], 'application/pdf')
        self.assertEqual(sorted(imprimees), ['http://testserver/a/S-1/', 'http://testserver/a/S-2/'])
        choisis = [str(Article.objects.get(code=code).pk) for code in ('M-1', 'S-1')]
        page = self.client.get(reverse('stock:etiquettes'), {'apres': apres, 'article': choisis})  # et un autre filtre
        self.assertEqual([a.code for a in page.context['articles']], ['S-1'])
        # Valeur illisible : ignorée.
        for valeur in ('nimporte', '0001-01-01T00:00:00+14:00', '9999-12-31T23:00:00-05:00', ''):
            with self.subTest(valeur):
                self.assertEqual(self.client.get(reverse('stock:etiquettes'), {'apres': valeur}).context['total'], 5)

    def test_transaction_tout_ou_rien(self):
        nb = (Article.objects.count(), Categorie.objects.count(), Bloc.objects.count(), Etagere.objects.count())
        fichier = fichier_excel([('N-1', 'Nouveau', 'Nouvelle catégorie', 'U', 5, 100, 'Z-Z1-1')])
        self.assertRedirects(self.envoyer(fichier), reverse('stock:import_apercu'))
        with mock.patch('stock.services.valider', side_effect=services.StockError('panne')):
            rep = self.client.post(reverse('stock:import_confirmer'), follow=True)
        self.assertRedirects(rep, reverse('stock:import_apercu'))
        self.assertContains(rep, "rien n&#x27;a été enregistré")
        self.assertEqual((Article.objects.count(), Categorie.objects.count(), Bloc.objects.count(),
                          Etagere.objects.count()), nb)
        self.assertFalse(BonEntree.objects.exists())
        # Le fichier est gardé : l'import peut être relancé.
        self.assertRedirects(self.client.post(reverse('stock:import_confirmer')), reverse('stock:import_resultat'))
        self.assertTrue(Article.objects.filter(code='N-1').exists())

    def test_500_lignes_en_moins_de_3_secondes(self):
        import time

        lignes = [(f'ART-{i:04d}', f'Article numéro {i}', f'Famille {i % 12}', ('pcs', 'kg', 'sac', 'L')[i % 4],
                   i % 50 + 1 if i % 3 else '', f'{1000 + i} F', f'{"ABCDE"[i % 5]}-R{i % 6 + 1}-{i % 4 + 1}', i % 10,
                   f'autre nom {i}', f'REF{i}') for i in range(500)]
        fichier = fichier_excel(lignes)
        debut = time.perf_counter()
        self.assertRedirects(self.envoyer(fichier), reverse('stock:import_apercu'))
        self.assertEqual(self.client.get(reverse('stock:import_apercu')).status_code, 200)
        self.assertRedirects(self.client.post(reverse('stock:import_confirmer')), reverse('stock:import_resultat'))
        duree = time.perf_counter() - debut
        self.assertLess(duree, 3, f'500 lignes : {duree:.2f} s')
        self.assertEqual(Article.objects.count(), 501)
        bon = BonEntree.objects.get()
        self.assertEqual(bon.lignes.count(), sum(1 for l in lignes if l[4]))
        self.assertEqual(Bloc.objects.count(), 5)
        self.assertEqual(Article.objects.filter(etagere__isnull=True).count(), 1)  # seulement FH-01


class PremiersPasTests(TestCase):
    def setUp(self):
        Utilisateur = get_user_model()
        self.chef = Utilisateur.objects.create_user('chef', password='x', is_staff=True, is_superuser=True)
        self.client.force_login(self.chef)
        cache.clear()

    def etat(self):
        rep = self.client.get(reverse('stock:accueil'))
        pas = rep.context['premiers_pas']
        return rep, pas and [e['fait'] for e in pas['etapes']]

    def test_etapes_cochees_selon_l_etat_reel(self):
        rep, etapes = self.etat()
        self.assertEqual(etapes, [False] * 5)
        self.assertContains(rep, 'Bien démarrer · 0/5')
        self.assertContains(rep, reverse('stock:import_articles'))
        self.assertNotContains(rep, 'Valeur du stock')  # pas de compteurs à zéro sur un magasin vide
        p = Parametres.actuels()
        p.nom_societe, p.telephone = 'BTP Lagunes', '+225 07 00 00 00'
        p.save()
        bloc = Bloc.objects.create(code='A')
        self.assertEqual(self.etat()[1], [True, False, False, False, False])  # un bloc sans étagère ne suffit pas
        Etagere.objects.create(bloc=bloc, code='R1')
        article = Article.objects.create(code='C-1', designation='Ciment', categorie=Categorie.objects.create(nom='M'))
        rep, etapes = self.etat()
        self.assertEqual(etapes, [True, True, True, False, False])
        self.assertContains(rep, 'Bien démarrer · 3/5')
        self.assertContains(rep, 'width:60%')
        bon = BonEntree.objects.create(cree_par=self.chef, observation='Stock de départ')
        LigneEntree.objects.create(bon=bon, article=article, quantite=10, prix_unitaire=5000)
        services.valider(bon, self.chef)
        self.assertEqual(self.etat()[1], [True, True, True, True, False])
        get_user_model().objects.create_user('awa', password='x')
        rep, etapes = self.etat()
        self.assertIsNone(etapes)  # tout est fait : la carte disparaît
        self.assertNotContains(rep, 'Bien démarrer')

    def test_masquer_puis_reafficher(self):
        rep = self.client.post(reverse('stock:premiers_pas'))
        self.assertRedirects(rep, reverse('stock:accueil'))
        self.assertTrue(Parametres.objects.get().premiers_pas_masques)
        self.assertNotContains(self.client.get(reverse('stock:accueil')), 'Bien démarrer')
        societe = self.client.get(reverse('stock:reglages_societe'))
        self.assertContains(societe, 'Le réafficher')
        self.client.post(reverse('stock:premiers_pas'), {'afficher': '1'})
        self.assertFalse(Parametres.objects.get().premiers_pas_masques)
        self.assertContains(self.client.get(reverse('stock:accueil')), 'Bien démarrer')
        self.assertEqual(self.client.get(reverse('stock:premiers_pas')).status_code, 405)

    def test_magasinier_ne_voit_aucun_lien_reserve(self):
        magasinier = get_user_model().objects.create_user('awa', password='x')
        self.client.force_login(magasinier)
        rep = self.client.get(reverse('stock:accueil'))
        self.assertContains(rep, 'Votre responsable prépare le magasin')
        for reserve in ('Bien démarrer', reverse('stock:import_articles'), reverse('stock:exemple_installer'),
                        reverse('stock:premiers_pas'), '/reglages/'):
            self.assertNotContains(rep, reserve)
        articles = self.client.get(reverse('stock:articles'))
        self.assertContains(articles, "Aucun article pour l'instant")
        self.assertNotContains(articles, reverse('stock:import_articles'))
        self.assertRedirects(self.client.post(reverse('stock:premiers_pas')), reverse('stock:accueil'))
        self.assertFalse(Parametres.objects.filter(premiers_pas_masques=True).exists())


class MagasinExempleTests(TestCase):
    def setUp(self):
        Utilisateur = get_user_model()
        self.chef = Utilisateur.objects.create_user('chef', password='x', is_staff=True, is_superuser=True)
        self.client.force_login(self.chef)

    def test_bouton_essayer_sur_un_magasin_vide(self):
        from .management.commands.demo import REFERENCE_EXEMPLE

        accueil = self.client.get(reverse('stock:accueil'))
        self.assertContains(accueil, "Essayer avec un magasin d'exemple")
        self.assertEqual(self.client.get(reverse('stock:exemple_installer')).status_code, 405)
        rep = self.client.post(reverse('stock:exemple_installer'), follow=True)
        self.assertContains(rep, "Magasin d&#x27;exemple installé")
        self.assertTrue(BonEntree.objects.filter(reference=REFERENCE_EXEMPLE, statut=BonEntree.VALIDE).exists())
        nb = Article.objects.count()
        self.assertGreater(nb, 15)
        # Le bouton disparaît ; l'exemple ne coche pas les étapes du vrai magasin.
        self.assertNotContains(rep, "Essayer avec un magasin d'exemple")
        self.assertContains(rep, "Vous êtes sur le magasin d'exemple")
        etapes = rep.context['premiers_pas']['etapes']
        self.assertEqual([e['fait'] for e in etapes[1:4]], [False, False, False])
        rep = self.client.post(reverse('stock:exemple_installer'), follow=True)
        self.assertContains(rep, 'ne s&#x27;installe que dans un magasin vide')
        self.assertEqual(Article.objects.count(), nb)

    def test_magasin_avec_un_plan_ou_magasinier_refuse(self):
        self.client.force_login(get_user_model().objects.create_user('awa', password='x'))
        self.assertRedirects(self.client.post(reverse('stock:exemple_installer')), reverse('stock:accueil'))
        self.assertFalse(Article.objects.exists())
        self.client.force_login(self.chef)
        Bloc.objects.create(code='A')
        self.assertNotContains(self.client.get(reverse('stock:accueil')), reverse('stock:exemple_installer'))
        self.client.post(reverse('stock:exemple_installer'))
        self.assertFalse(Article.objects.exists())

    def test_exemple_tourne_vers_la_cote_d_ivoire(self):
        from django.core.management import call_command

        Chantier.objects.create(nom='Chantier Pont de Bingerville')  # déjà là : réutilisé, pas d'erreur
        call_command('demo', stdout=io.StringIO())
        textes = ' '.join(Article.objects.values_list('mots_cles', flat=True)).lower()
        for mot in ('zit', 'smenta', 'chahma', '3ajla', 'tiyo', 'sabbat', 'mfateh', 'kasket', 'hwa', 'sir,'):
            self.assertNotIn(mot, textes)
        self.assertIn('fer de 10', textes)
        self.assertIn('filtre gasoil', textes)
        self.assertIn('sac de ciment', textes)
        unites = dict(Article.UNITES)
        self.assertTrue(all(u in unites for u in Article.objects.values_list('unite', flat=True)))
        self.assertTrue(Chantier.objects.filter(nom__icontains='Cocody').exists())
        self.assertEqual(Chantier.objects.filter(nom='Chantier Pont de Bingerville').count(), 1)
        for code in ('FH-VOL-01', 'EPI-GAN', 'CIM-CPJ45', 'FER-HA10'):
            self.assertTrue(Article.objects.filter(code=code).exists(), code)
        for e in Etagere.objects.select_related('bloc'):
            self.assertLessEqual(e.x + e.largeur, e.bloc.largeur, str(e))
