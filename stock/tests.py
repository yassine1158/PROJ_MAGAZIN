import io
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
        for url in [reverse('stock:recherche'), reverse('stock:dashboard'), reverse('stock:articles'),
                    reverse('stock:consommation'), reverse('stock:api_plan'),
                    reverse('stock:api_article', args=[self.filtre.pk]),
                    reverse('admin:stock_article_changelist'), reverse('admin:stock_article_change', args=[self.filtre.pk]),
                    reverse('admin:stock_bonsortie_changelist'), reverse('admin:stock_bonsortie_change', args=[bon.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        pdf = self.client.get(reverse('stock:bon_pdf', args=['bonsortie', bon.pk]))
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        xlsx = self.client.get(reverse('stock:articles') + '?format=excel')
        self.assertIn('spreadsheetml', xlsx['Content-Type'])

    def test_demo(self):
        Article.objects.all().delete()
        Etagere.objects.all().delete()
        Bloc.objects.all().delete()
        call_command('demo', stdout=io.StringIO())
        self.assertGreater(Article.objects.count(), 10)
        self.assertTrue(BonSortie.objects.filter(statut='VALIDE').exists())


class EcransTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_connexion(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('stock:connexion')).status_code, 200)
        self.assertEqual(self.client.get('/admin/login/').status_code, 200)
        rep = self.client.post(reverse('stock:connexion'), {'username': 'admin', 'password': 'motdepasse-solide'})
        self.assertRedirects(rep, reverse('stock:accueil'))

    def test_pages_principales(self):
        for nom in ['accueil', 'entree', 'sortie', 'bons', 'articles', 'article_nouveau', 'recherche',
                    'dashboard', 'consommation']:
            self.assertEqual(self.client.get(reverse(f'stock:{nom}')).status_code, 200, nom)
        self.assertEqual(self.client.get(reverse('stock:article_modifier', args=[self.filtre.pk])).status_code, 200)

    def test_entree_simple(self):
        rep = self.client.post(reverse('stock:entree'), {
            'fournisseur': 'Nouveau fournisseur', 'reference': 'BL-1',
            'article': [self.filtre.pk, ''], 'quantite': ['10', ''], 'prix': ['1 500,50', ''],
        })
        bon = BonEntree.objects.get()
        self.assertRedirects(rep, reverse('stock:bon_detail', args=['entree', bon.pk]))
        self.assertEqual(bon.statut, 'VALIDE')
        self.assertEqual(bon.fournisseur.nom, 'Nouveau fournisseur')
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 10)
        self.assertEqual(self.filtre.prix_moyen, Decimal('1500.50'))
        self.assertEqual(self.client.get(rep.url).status_code, 200)

    def test_sortie_cree_le_chantier_et_refuse_le_stock_insuffisant(self):
        self.entree(self.filtre, 3, 100)
        rep = self.client.post(reverse('stock:sortie'), {
            'chantier': 'Nouveau chantier', 'demandeur': 'Ali', 'article': [self.filtre.pk], 'quantite': ['5'],
        })
        self.assertEqual(rep.status_code, 200)
        self.assertContains(rep, 'stock insuffisant')
        self.assertFalse(BonSortie.objects.exists())  # rien n'est gardé
        self.assertFalse(Chantier.objects.filter(nom='Nouveau chantier').exists())
        rep = self.client.post(reverse('stock:sortie'), {
            'chantier': 'nouveau CHANTIER', 'demandeur': 'Ali', 'article': [self.filtre.pk], 'quantite': ['2'],
        })
        bon = BonSortie.objects.get()
        self.assertRedirects(rep, reverse('stock:bon_detail', args=['sortie', bon.pk]))
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 1)
        # Annulation depuis l'écran du bon
        self.client.post(reverse('stock:bon_annuler', args=['sortie', bon.pk]))
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 3)

    def test_sortie_incomplete(self):
        rep = self.client.post(reverse('stock:sortie'), {'chantier': '', 'demandeur': '', 'article': [''], 'quantite': ['']})
        self.assertContains(rep, 'Indiquez le chantier')
        self.assertContains(rep, 'Ajoutez au moins un article')

    def test_nouvel_article_avec_stock_de_depart(self):
        rep = self.client.post(reverse('stock:article_nouveau'), {
            'code': 'GANT-01', 'designation': 'Gants', 'categorie_nom': 'epi', 'unite': 'U', 'stock_min': '5',
            'etagere': self.etagere.pk, 'niveau': '3', 'stock_initial': '20', 'prix_initial': '1500', 'actif': 'on',
        })
        self.assertRedirects(rep, reverse('stock:articles'))
        gants = Article.objects.get(code='GANT-01')
        self.assertEqual(gants.stock, 20)
        self.assertEqual(gants.categorie.nom, 'epi')
        self.assertEqual(gants.emplacement, 'Bloc A › Étagère R1 › Niveau 3')

    def test_niveau_trop_haut(self):
        rep = self.client.post(reverse('stock:article_nouveau'), {
            'code': 'X', 'designation': 'X', 'categorie_nom': 'Filtres', 'unite': 'U', 'stock_min': '0',
            'etagere': self.etagere.pk, 'niveau': '9',
        })
        self.assertContains(rep, 'que 4 niveaux')

    def test_corriger_le_stock(self):
        self.entree(self.filtre, 10, 100)
        self.client.post(reverse('stock:article_corriger', args=[self.filtre.pk]), {'stock_compte': '8'})
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 8)
        self.assertEqual(Inventaire.objects.get().lignes.get().ecart, -2)

    def test_api_articles(self):
        data = self.client.get(reverse('stock:api_articles'), {'q': 'huile'}).json()
        self.assertEqual(data['articles'][0]['code'], 'FH-01')


class PremierLancementTests(TestCase):
    def test_bienvenue_cree_le_compte_puis_disparait(self):
        self.assertRedirects(self.client.get(reverse('stock:connexion')), reverse('stock:bienvenue'))
        rep = self.client.post(reverse('stock:bienvenue'), {
            'username': 'yassine', 'password': 'Magasin-2026!', 'password2': 'Magasin-2026!', 'demo': '1'})
        self.assertRedirects(rep, reverse('stock:accueil'))
        self.assertTrue(get_user_model().objects.get(username='yassine').is_superuser)
        self.assertTrue(Article.objects.exists())
        self.client.logout()
        self.assertRedirects(self.client.get(reverse('stock:bienvenue')), reverse('stock:connexion'))

    def test_bienvenue_refuse_mot_de_passe_faible(self):
        rep = self.client.post(reverse('stock:bienvenue'), {'username': 'a', 'password': '123', 'password2': '123'})
        self.assertEqual(rep.status_code, 200)
        self.assertFalse(get_user_model().objects.exists())


class BureauTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_pdf_ouvert_directement(self):
        import tempfile
        from pathlib import Path
        self.entree(self.filtre, 2, 100)
        bon = BonEntree.objects.get()
        with tempfile.TemporaryDirectory() as dossier, \
                override_settings(BUREAU=True, DATA_DIR=Path(dossier)), \
                mock.patch('stock.bureau.Path.home', return_value=Path(dossier)), \
                mock.patch('stock.bureau.ouvrir_fichier') as ouvrir:
            rep = self.client.get(reverse('stock:bon_pdf', args=['bonentree', bon.pk]), HTTP_REFERER='/bons/')
            self.assertRedirects(rep, '/bons/', fetch_redirect_response=False)
            chemin = ouvrir.call_args[0][0]
            self.assertEqual(chemin.name, f'{bon.numero}.pdf')
            self.assertTrue(chemin.read_bytes().startswith(b'%PDF'))

    def test_reglages_ia(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as dossier, mock.patch.dict(os.environ, {}, clear=False), \
                override_settings(MAGASIN_CLE_IA_FICHIER=Path(dossier) / 'cle.txt', MAGASIN_IA_ACTIVE=False):
            self.client.post(reverse('stock:reglages_ia'), {'cle': 'pas-une-cle'})
            self.assertFalse((Path(dossier) / 'cle.txt').exists())
            self.client.post(reverse('stock:reglages_ia'), {'cle': 'sk-ant-api03-exemple-de-cle-1234'})
            self.assertEqual((Path(dossier) / 'cle.txt').read_text(), 'sk-ant-api03-exemple-de-cle-1234')
            self.assertContains(self.client.get(reverse('stock:reglages_ia')), 'Activée')
            self.client.post(reverse('stock:reglages_ia'), {'supprimer': '1'})
            self.assertFalse((Path(dossier) / 'cle.txt').exists())
