from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from . import services
from .models import (
    Article, Bloc, BonEntree, BonSortie, Categorie, Chantier, Etagere, Inventaire, LigneEntree, LigneInventaire,
    LigneSortie, MouvementStock,
)
from .recherche import Interpretation, recherche_locale


class Base(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('admin', 'a@a.fr', 'motdepasse-solide')
        self.cat = Categorie.objects.create(nom='Filtres')
        bloc = Bloc.objects.create(code='A', nom='Pièces')
        self.etagere = Etagere.objects.create(bloc=bloc, code='R1')
        self.filtre = Article.objects.create(code='FH-01', designation='Filtre à huile Volvo', categorie=self.cat,
                                             etagere=self.etagere, niveau=2, mots_cles='filtre zit', stock_min=2)
        self.ciment = Article.objects.create(code='CIM-45', designation='Ciment CPJ 45 sac 50 kg',
                                             categorie=Categorie.objects.create(nom='Matériaux'), unite='SAC')
        self.chantier = Chantier.objects.create(nom='Pont')

    def entree(self, article, qte, prix):
        bon = BonEntree.objects.create()
        LigneEntree.objects.create(bon=bon, article=article, quantite=qte, prix_unitaire=prix)
        return services.valider(bon, self.user)

    def sortie(self, article, qte):
        bon = BonSortie.objects.create(chantier=self.chantier, demandeur='Ali')
        LigneSortie.objects.create(bon=bon, article=article, quantite=qte)
        return bon


class StockTests(Base):
    def test_numero_automatique(self):
        bon = BonEntree.objects.create()
        self.assertRegex(bon.numero, r'^BE-\d{4}-\d{5}$')

    def test_entree_augmente_le_stock_et_calcule_le_prix_moyen(self):
        self.entree(self.filtre, 10, 1000)
        self.entree(self.filtre, 10, 2000)
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 20)
        self.assertEqual(self.filtre.prix_moyen, Decimal('1500.00'))
        self.assertEqual(MouvementStock.objects.filter(article=self.filtre).count(), 2)

    def test_prix_zero_ne_change_pas_le_prix_moyen(self):
        self.entree(self.filtre, 10, 1000)
        self.entree(self.filtre, 10, 0)
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.prix_moyen, Decimal('1000.00'))

    def test_sortie_diminue_le_stock_au_prix_moyen(self):
        self.entree(self.filtre, 10, 1000)
        bon = services.valider(self.sortie(self.filtre, 4), self.user)
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 6)
        self.assertEqual(bon.lignes.get().prix_unitaire, Decimal('1000.00'))
        mvt = MouvementStock.objects.get(type=MouvementStock.SORTIE)
        self.assertEqual(mvt.quantite, -4)
        self.assertEqual(mvt.chantier, self.chantier)

    def test_sortie_refusee_si_stock_insuffisant(self):
        self.entree(self.filtre, 3, 1000)
        bon = self.sortie(self.filtre, 2)
        LigneSortie.objects.create(bon=bon, article=self.filtre, quantite=2)  # même article sur 2 lignes
        with self.assertRaises(services.StockError):
            services.valider(bon, self.user)
        self.filtre.refresh_from_db()
        bon.refresh_from_db()
        self.assertEqual(self.filtre.stock, 3)
        self.assertEqual(bon.statut, BonSortie.BROUILLON)

    def test_double_validation_impossible(self):
        bon = self.entree(self.filtre, 5, 100)
        with self.assertRaises(services.StockError):
            services.valider(bon, self.user)
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 5)

    def test_annulation_sortie_remet_le_stock(self):
        self.entree(self.filtre, 10, 1000)
        bon = services.valider(self.sortie(self.filtre, 4), self.user)
        services.annuler(bon, self.user)
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 10)
        self.assertEqual(self.filtre.prix_moyen, Decimal('1000.00'))

    def test_annulation_entree_refusee_si_marchandise_sortie(self):
        bon = self.entree(self.filtre, 5, 1000)
        services.valider(self.sortie(self.filtre, 3), self.user)
        with self.assertRaises(services.StockError):
            services.annuler(bon, self.user)

    def test_annulation_entree(self):
        self.entree(self.filtre, 10, 1000)
        bon = self.entree(self.filtre, 10, 2000)
        services.annuler(bon, self.user)
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 10)
        self.assertEqual(self.filtre.prix_moyen, Decimal('1000.00'))

    def test_inventaire_ajuste_le_stock(self):
        self.entree(self.filtre, 10, 1000)
        inv = Inventaire.objects.create()
        LigneInventaire.objects.create(bon=inv, article=self.filtre, stock_compte=7)
        services.valider(inv, self.user)
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 7)
        ligne = inv.lignes.get()
        self.assertEqual(ligne.stock_theorique, 10)
        self.assertEqual(ligne.ecart, -3)
        with self.assertRaises(services.StockError):
            services.annuler(inv, self.user)

    def test_alerte(self):
        self.entree(self.filtre, 2, 1000)
        self.assertIn(self.filtre, Article.objects.en_alerte())
        self.entree(self.filtre, 1, 1000)
        self.assertNotIn(self.filtre, Article.objects.en_alerte())


class RechercheTests(Base):
    def test_recherche_locale_par_mot_cle_et_faute(self):
        self.assertEqual(recherche_locale(['filtre zit'])[0], self.filtre)
        self.assertEqual(recherche_locale(['filtr huille'])[0], self.filtre)
        self.assertEqual(recherche_locale(['FH-01'])[0], self.filtre)
        self.assertEqual(recherche_locale(['ciment'])[0], self.ciment)
        self.assertEqual(recherche_locale(['xyz']), [])

    def test_emplacement(self):
        self.assertEqual(self.filtre.emplacement, 'Bloc A › Étagère R1 › Niveau 2')
        self.assertEqual(self.ciment.emplacement, '')

    @override_settings(MAGASIN_IA_ACTIVE=False)
    def test_api_sans_ia(self):
        self.client.force_login(self.user)
        rep = self.client.post(reverse('stock:api_recherche'), {'q': 'filtre huile'})
        self.assertEqual(rep.status_code, 200)
        data = rep.json()
        self.assertFalse(data['ia'])
        self.assertEqual(data['resultats'][0]['code'], 'FH-01')
        self.assertEqual(data['resultats'][0]['bloc'], 'A')
        self.assertEqual(data['resultats'][0]['niveau'], 2)

    def test_api_avec_ia(self):
        self.client.force_login(self.user)
        ia = Interpretation(termes=['ciment', 'sac de ciment'], categorie='Matériaux', explication='Du ciment.')
        with mock.patch('stock.recherche.interpreter', return_value=ia):
            rep = self.client.post(reverse('stock:api_recherche'), {'q': 'smenta'})
        data = rep.json()
        self.assertTrue(data['ia'])
        self.assertEqual(data['resultats'][0]['code'], 'CIM-45')

    def test_api_ia_en_panne_retombe_sur_la_recherche_locale(self):
        self.client.force_login(self.user)
        with mock.patch('stock.recherche.interpreter', side_effect=RuntimeError('panne')):
            rep = self.client.post(reverse('stock:api_recherche'), {'q': 'filtre'})
        self.assertEqual(rep.json()['resultats'][0]['code'], 'FH-01')

    def test_connexion_obligatoire(self):
        rep = self.client.post(reverse('stock:api_recherche'), {'q': 'filtre'})
        self.assertEqual(rep.status_code, 302)


class PagesTests(Base):
    def test_pages(self):
        self.client.force_login(self.user)
        self.entree(self.filtre, 5, 1000)
        bon = services.valider(self.sortie(self.filtre, 1), self.user)
        for url in [reverse('stock:recherche'), reverse('stock:dashboard'), reverse('stock:etat_stock'),
                    reverse('stock:consommation'), reverse('stock:api_plan'),
                    reverse('stock:api_article', args=[self.filtre.pk]),
                    reverse('admin:stock_article_changelist'), reverse('admin:stock_article_change', args=[self.filtre.pk]),
                    reverse('admin:stock_bonsortie_changelist'), reverse('admin:stock_bonsortie_change', args=[bon.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        pdf = self.client.get(reverse('stock:bon_pdf', args=['bonsortie', bon.pk]))
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        xlsx = self.client.get(reverse('stock:etat_stock') + '?format=excel')
        self.assertIn('spreadsheetml', xlsx['Content-Type'])

    def test_demo(self):
        Article.objects.all().delete()
        Etagere.objects.all().delete()
        Bloc.objects.all().delete()
        call_command('demo', stdout=open('/dev/null', 'w'))
        self.assertGreater(Article.objects.count(), 10)
        self.assertTrue(BonSortie.objects.filter(statut='VALIDE').exists())
