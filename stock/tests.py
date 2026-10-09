import io
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from . import services
from .models import (
    Article, Bloc, BonEntree, BonSortie, Categorie, Chantier, Etagere, Inventaire, LigneEntree, LigneInventaire, Parametres,
    LigneSortie, MouvementStock, Mur,
)
from .recherche import recherche_locale


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

    def test_pas_d_alerte_sans_seuil(self):
        # Ciment : stock 0 et seuil 0 → pas « À commander » (sinon tout le catalogue y passe au démarrage).
        self.assertNotIn(self.ciment, Article.objects.en_alerte())
        self.assertFalse(self.ciment.en_alerte)
        self.filtre.refresh_from_db()
        self.assertTrue(self.filtre.en_alerte)  # stock 0, seuil 2
        self.filtre.actif = False
        self.filtre.save()
        self.assertNotIn(self.filtre, Article.objects.en_alerte())
        self.assertFalse(self.filtre.en_alerte)


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
        ia = SimpleNamespace(termes=['ciment', 'sac de ciment'], categorie='Matériaux', explication='Du ciment.')
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
        self.assertEqual(Article.objects.get(code='EPI-GAN').unite, 'PAIRE')


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


class ReglagesTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_pages_reglages(self):
        for url in ['/reglages/societe/', '/reglages/plan/', '/reglages/plan/blocs/nouveau/',
                    f'/reglages/plan/blocs/{self.etagere.bloc.pk}/', f'/reglages/plan/blocs/{self.etagere.bloc.pk}/rangee/',
                    f'/reglages/plan/blocs/{self.etagere.bloc.pk}/etageres/nouvelle/', f'/reglages/plan/etageres/{self.etagere.pk}/',
                    '/reglages/listes/', '/reglages/listes/engins/', '/reglages/listes/fournisseurs/',
                    '/reglages/listes/categories/', f'/reglages/listes/chantiers/{self.chantier.pk}/',
                    '/reglages/utilisateurs/', '/reglages/utilisateurs/nouveau/', f'/reglages/utilisateurs/{self.user.pk}/']:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_magasinier_n_a_pas_acces_aux_reglages(self):
        magasinier = get_user_model().objects.create_user('ali', password='Magasin-2026!')
        self.client.force_login(magasinier)
        self.assertRedirects(self.client.get('/reglages/plan/'), reverse('stock:accueil'))
        self.assertEqual(self.client.get(reverse('stock:sortie')).status_code, 200)

    def test_societe_change_nom_et_couleur(self):
        rep = self.client.post('/reglages/societe/', {'nom_societe': 'Ma Société SA', 'couleur': '#138A3C',
                                                      'devise': 'TND', 'signataire': 'Le chef'})
        self.assertRedirects(rep, '/reglages/societe/')
        p = Parametres.actuels()
        self.assertEqual((p.nom_societe, p.couleur, p.devise), ('Ma Société SA', '#138a3c', 'TND'))
        page = self.client.get(reverse('stock:accueil')).content.decode()
        self.assertIn('--primaire: #138a3c', page)
        self.assertIn('Ma Société SA', page)

    def test_rangee_d_etageres(self):
        bloc = self.etagere.bloc
        rep = self.client.post(f'/reglages/plan/blocs/{bloc.pk}/rangee/', {
            'prefixe': 'R', 'debut': 2, 'nombre': 3, 'x': '1', 'z': '1', 'largeur': '2', 'profondeur': '0,9',
            'hauteur': '2.4', 'nb_niveaux': 5, 'espace': '0.5'})
        self.assertRedirects(rep, '/reglages/plan/')
        codes = list(bloc.etageres.order_by('x').values_list('code', 'x'))
        self.assertIn(('R4', Decimal('6.00')), codes)
        # Codes déjà pris : refusé
        rep = self.client.post(f'/reglages/plan/blocs/{bloc.pk}/rangee/', {
            'prefixe': 'R', 'debut': 1, 'nombre': 2, 'x': '1', 'z': '1', 'largeur': '2', 'profondeur': '1',
            'hauteur': '2', 'nb_niveaux': 4, 'espace': '0'})
        self.assertContains(rep, 'existent déjà')

    def test_supprimer_un_chantier_utilise_est_refuse(self):
        self.entree(self.filtre, 2, 100)
        bon = self.sortie(self.filtre, 1)
        rep = self.client.post(f'/reglages/listes/chantiers/{self.chantier.pk}/supprimer/', follow=True)
        self.assertContains(rep, 'impossible de le supprimer')
        self.assertTrue(Chantier.objects.filter(pk=self.chantier.pk).exists())
        bon.delete()

    def test_creer_un_magasinier(self):
        rep = self.client.post('/reglages/utilisateurs/nouveau/', {
            'first_name': 'Moussa', 'username': 'moussa', 'is_active': 'on', 'role': 'magasinier',
            'mot_de_passe': 'Magasin-2026!'})
        self.assertRedirects(rep, '/reglages/utilisateurs/')
        moussa = get_user_model().objects.get(username='moussa')
        self.assertFalse(moussa.is_staff)
        self.assertTrue(moussa.check_password('Magasin-2026!'))

    def test_on_ne_peut_pas_se_retirer_ses_droits(self):
        rep = self.client.post(f'/reglages/utilisateurs/{self.user.pk}/', {
            'username': 'admin', 'is_active': 'on', 'role': 'magasinier'})
        self.assertContains(rep, 'propres droits')
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)


class EditeurPlanTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.bloc = self.etagere.bloc

    def envoyer(self, donnees):
        import json
        return self.client.post(reverse('stock:api_plan_enregistrer'), json.dumps(donnees), content_type='application/json')

    def test_page_editeur(self):
        self.assertContains(self.client.get(reverse('stock:editeur_plan')), 'plan-initial')

    def test_enregistrer_murs_blocs_etageres(self):
        rep = self.envoyer({
            'blocs': [{'cle': f'b{self.bloc.pk}', 'id': self.bloc.pk, 'code': 'A', 'nom': 'Pièces', 'couleur': '#123456',
                       'x': 10, 'z': 5, 'largeur': 12, 'profondeur': 8},
                      {'cle': 'bn1', 'code': 'B', 'couleur': '#16a34a', 'x': 30, 'z': 5, 'largeur': 6, 'profondeur': 6}],
            'etageres': [{'id': self.etagere.pk, 'bloc': f'b{self.bloc.pk}', 'code': 'R1', 'x': 12, 'z': 6.5,
                          'largeur': 2, 'profondeur': 0.9, 'hauteur': 2.4, 'niveaux': 4, 'tournee': False},
                         {'bloc': 'bn1', 'code': 'E1', 'x': 31, 'z': 6, 'largeur': 2, 'profondeur': 1,
                          'hauteur': 2, 'niveaux': 3, 'tournee': True}],
            'murs': [{'x1': 0, 'z1': 0, 'x2': 40, 'z2': 0, 'epaisseur': 0.2, 'hauteur': 3},
                     {'x1': 5, 'z1': 5, 'x2': 5, 'z2': 5}],  # mur de longueur nulle : ignoré
        })
        self.assertEqual(rep.status_code, 200, rep.content)
        self.etagere.refresh_from_db()
        self.assertEqual((self.etagere.x, self.etagere.z), (Decimal('2.00'), Decimal('1.50')))  # relatif au bloc
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.etagere, self.etagere)  # l'article garde son étagère
        nouvelle = Etagere.objects.get(code='E1')
        self.assertEqual((nouvelle.bloc.code, nouvelle.x, nouvelle.tournee), ('B', Decimal('1.00'), True))
        self.assertEqual(Mur.objects.count(), 1)
        self.assertEqual(len(rep.json()['murs']), 1)

    def test_etagere_deplacee_vers_un_nouveau_bloc_garde_ses_articles(self):
        rep = self.envoyer({
            'blocs': [{'cle': 'bn1', 'code': 'A', 'couleur': '#16a34a', 'x': 0, 'z': 0, 'largeur': 6, 'profondeur': 6}],
            'etageres': [{'id': self.etagere.pk, 'bloc': 'bn1', 'code': 'R1', 'x': 1, 'z': 1, 'largeur': 2,
                          'profondeur': 1, 'hauteur': 2, 'niveaux': 4}],
            'murs': [],
        })
        self.assertEqual(rep.status_code, 200, rep.content)
        self.assertFalse(Bloc.objects.filter(pk=self.bloc.pk).exists())  # ancien bloc supprimé…
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.etagere_id, self.etagere.pk)  # …mais l'étagère et ses articles restent

    def test_echanger_les_codes_de_deux_blocs(self):
        autre = Bloc.objects.create(code='B')
        rep = self.envoyer({'blocs': [
            {'cle': 'b1', 'id': self.bloc.pk, 'code': 'B', 'x': 0, 'z': 0, 'largeur': 5, 'profondeur': 5},
            {'cle': 'b2', 'id': autre.pk, 'code': 'A', 'x': 6, 'z': 0, 'largeur': 5, 'profondeur': 5}],
            'etageres': [], 'murs': []})
        self.assertEqual(rep.status_code, 200, rep.content)
        self.bloc.refresh_from_db()
        self.assertEqual(self.bloc.code, 'B')

    def test_plan_invalide_ne_change_rien(self):
        rep = self.envoyer({'blocs': [
            {'cle': 'b1', 'code': 'A', 'x': 0, 'z': 0, 'largeur': 5, 'profondeur': 5},
            {'cle': 'b2', 'code': 'a', 'x': 6, 'z': 0, 'largeur': 5, 'profondeur': 5}], 'etageres': [], 'murs': []})
        self.assertEqual(rep.status_code, 400)
        self.assertIn('même code', rep.json()['erreurs'][0])
        rep = self.envoyer({'blocs': [{'cle': 'b1', 'code': 'Z', 'x': 0, 'z': 0, 'largeur': -5, 'profondeur': 5}],
                            'etageres': [], 'murs': []})
        self.assertEqual(rep.status_code, 400)
        self.assertTrue(Bloc.objects.filter(pk=self.bloc.pk, code='A').exists())  # tout est annulé
        self.assertTrue(Etagere.objects.filter(pk=self.etagere.pk).exists())

    def test_reserve_aux_responsables(self):
        magasinier = get_user_model().objects.create_user('ali', password='Magasin-2026!')
        self.client.force_login(magasinier)
        self.assertEqual(self.envoyer({'blocs': [], 'etageres': [], 'murs': []}).status_code, 403)
        self.assertTrue(Bloc.objects.exists())

    def test_objets_et_couleur_des_murs(self):
        rep = self.envoyer({
            'blocs': [], 'etageres': [],
            'murs': [{'x1': 0, 'z1': 0, 'x2': 20, 'z2': 0, 'couleur': '#AABBCC'}],
            'elements': [
                {'type': 'porte_entree', 'nom': 'Entrée', 'x': 10, 'z': 0, 'largeur': 2, 'profondeur': 0.2, 'hauteur': 2.4,
                 'rotation': 0, 'couleur': '#16a34a'},
                {'type': 'bureau', 'x': 4, 'z': 4, 'largeur': 4, 'profondeur': 3, 'hauteur': 2.6, 'rotation': 450},
            ]})
        self.assertEqual(rep.status_code, 200, rep.content)
        data = rep.json()
        self.assertEqual(data['murs'][0]['couleur'], '#aabbcc')
        bureau = next(e for e in data['elements'] if e['type'] == 'bureau')
        self.assertEqual(bureau['rotation'], 90.0)           # 450° → 90°
        self.assertEqual(bureau['couleur'], '#f59e0b')       # couleur par défaut du type
        self.assertFalse(Bloc.objects.exists())              # plan vide de blocs : tout a été remplacé

    def test_objet_inconnu_refuse(self):
        rep = self.envoyer({'blocs': [], 'etageres': [], 'murs': [], 'elements': [{'type': 'piscine', 'x': 0, 'z': 0}]})
        self.assertEqual(rep.status_code, 400)
        self.assertIn('inconnu', rep.json()['erreurs'][0])
        self.assertTrue(Bloc.objects.exists())


class LicenceTests(TestCase):
    """Essai gratuit, blocage à la fin de l'essai et activation par clé signée."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from . import licence

        from django.core.cache import cache

        cache.clear()  # paramètres mis en cache par d'autres tests
        self.licence = licence
        self.privee = Ed25519PrivateKey.generate()
        publique = self.privee.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.dossier = tempfile.TemporaryDirectory()
        self.addCleanup(self.dossier.cleanup)
        for patch in (mock.patch('config.produit.CLE_PUBLIQUE', licence._b64(publique)),
                      mock.patch.object(licence, 'code_machine', return_value='AAAA-1111'),
                      override_settings(DATA_DIR=Path(self.dossier.name))):
            self.enterContext(patch)
        self.user = get_user_model().objects.create_superuser('admin', 'a@a.fr', 'motdepasse-solide')
        self.client.force_login(self.user)

    def expirer(self):
        from datetime import date, timedelta

        p = Parametres.actuels()
        p.debut_essai = date.today() - timedelta(days=31)
        p.save()

    def test_sans_cle_publique_pas_de_verrou(self):
        with mock.patch('config.produit.CLE_PUBLIQUE', ''):
            self.expirer()
            self.assertEqual(self.licence.etat().mode, 'libre')
            self.assertEqual(self.client.get('/').status_code, 200)

    def test_essai_puis_blocage(self):
        etat = self.licence.etat()
        self.assertEqual((etat.mode, etat.jours_restants), ('essai', 30))
        self.assertContains(self.client.get('/'), "Version d'essai")
        self.expirer()
        self.assertRedirects(self.client.get('/articles/'), reverse('stock:activation'))
        self.assertContains(self.client.get(reverse('stock:activation')), 'AAAA-1111')

    def test_essai_garde_la_date_la_plus_ancienne(self):
        from datetime import date, timedelta

        ancien = date.today() - timedelta(days=40)
        (self.licence.settings.DATA_DIR / '.essai').write_text(ancien.isoformat())
        self.assertEqual(self.licence.debut_essai(), ancien)
        self.assertTrue(self.licence.etat().bloque)

    def test_activation(self):
        self.expirer()
        cle = self.licence.signer(self.privee, 'Client Test', 'aaaa-1111')
        rep = self.client.post(reverse('stock:activation'), {'cle': cle[:40] + '\n' + cle[40:]})
        self.assertEqual(rep.status_code, 302)
        etat = self.licence.etat()
        self.assertEqual((etat.mode, etat.client), ('active', 'Client Test'))
        self.assertEqual(self.client.get('/articles/').status_code, 200)

    def test_cle_refusee(self):
        autre = self.licence.signer(self.privee, 'X', 'BBBB-2222')
        with self.assertRaisesMessage(ValueError, 'autre ordinateur'):
            self.licence.lire_cle(autre)
        fausse = autre.split('.')[0] + '.' + self.licence._b64(b'0' * 64)
        with self.assertRaisesMessage(ValueError, "n'est pas valable"):
            self.licence.lire_cle(fausse)
        self.client.post(reverse('stock:activation'), {'cle': 'nimporte.quoi'})
        self.assertEqual(Parametres.actuels().cle_licence, '')


# =========================================================
# Correctifs bloquants et droits
# =========================================================

class DroitsTests(Base):
    def setUp(self):
        super().setUp()
        self.magasinier = get_user_model().objects.create_user('awa', password='Magasin-2026!')
        self.entree(self.filtre, 10, 1000)

    def test_magasinier_imprime_sa_sortie(self):
        self.client.force_login(self.magasinier)
        rep = self.client.post(reverse('stock:sortie'), {
            'chantier': 'Pont', 'demandeur': 'Ali', 'article': [self.filtre.pk], 'quantite': ['2']})
        bon = BonSortie.objects.get()
        self.assertRedirects(rep, reverse('stock:bon_detail', args=['sortie', bon.pk]))
        pdf = self.client.get(reverse('stock:bon_pdf', args=['bonsortie', bon.pk]))
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        self.assertTrue(pdf.content.startswith(b'%PDF'))

    def test_lien_admin_ne_boucle_plus_pour_un_magasinier(self):
        self.client.force_login(self.magasinier)
        page = f'/admin/stock/article/{self.filtre.pk}/change/'
        rep = self.client.get(f'/admin/login/?next={page}')
        self.assertRedirects(rep, reverse('stock:accueil'))
        rep = self.client.get(page, follow=True)
        self.assertEqual(rep.status_code, 200)
        self.assertLessEqual(len(rep.redirect_chain), 3)
        self.assertEqual(rep.redirect_chain[-1][0], reverse('stock:accueil'))
        self.assertContains(rep, 'Réservé aux responsables')
        # Un responsable arrive bien dans l'administration.
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(page).status_code, 200)

    def test_magasinier_ne_peut_ni_annuler_ni_corriger(self):
        bon = services.valider(self.sortie(self.filtre, 3), self.user)
        self.client.force_login(self.magasinier)
        detail = self.client.get(reverse('stock:bon_detail', args=['sortie', bon.pk]))
        self.assertNotContains(detail, 'Annuler ce bon')
        self.assertContains(detail, 'Imprimer')
        rep = self.client.post(reverse('stock:bon_annuler', args=['sortie', bon.pk]))
        self.assertRedirects(rep, reverse('stock:accueil'))
        bon.refresh_from_db()
        self.assertEqual(bon.statut, 'VALIDE')
        fiche = self.client.get(reverse('stock:article_modifier', args=[self.filtre.pk]))
        self.assertNotContains(fiche, reverse('stock:article_corriger', args=[self.filtre.pk]))
        self.assertContains(fiche, 'Prévenez un responsable')
        rep = self.client.post(reverse('stock:article_corriger', args=[self.filtre.pk]), {'stock_compte': '50'})
        self.assertRedirects(rep, reverse('stock:accueil'))
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 7)

    def test_responsable_voit_et_utilise_les_boutons(self):
        bon = services.valider(self.sortie(self.filtre, 3), self.user)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('stock:bon_detail', args=['sortie', bon.pk])), 'Annuler ce bon')
        self.assertContains(self.client.get(reverse('stock:article_modifier', args=[self.filtre.pk])),
                            reverse('stock:article_corriger', args=[self.filtre.pk]))
        self.client.post(reverse('stock:bon_annuler', args=['sortie', bon.pk]))
        bon.refresh_from_db()
        self.assertEqual(bon.statut, 'ANNULE')


class DoubleEnvoiTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.entree(self.filtre, 10, 1000)

    def test_meme_sortie_envoyee_deux_fois(self):
        jeton = self.client.get(reverse('stock:sortie')).context['jeton']
        self.assertIn(jeton, self.client.session['jetons_bons'])
        donnees = {'jeton': jeton, 'chantier': 'Pont', 'demandeur': 'Ali', 'article': [self.filtre.pk], 'quantite': ['2']}
        premier = self.client.post(reverse('stock:sortie'), donnees)
        second = self.client.post(reverse('stock:sortie'), donnees, follow=True)
        bon = BonSortie.objects.get()
        self.assertEqual(premier.url, reverse('stock:bon_detail', args=['sortie', bon.pk]))
        self.assertRedirects(second, reverse('stock:bon_detail', args=['sortie', bon.pk]))
        self.assertContains(second, 'déjà enregistré')
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 8)

    def test_meme_entree_envoyee_deux_fois(self):
        jeton = self.client.get(reverse('stock:entree')).context['jeton']
        donnees = {'jeton': jeton, 'article': [self.filtre.pk], 'quantite': ['5'], 'prix': ['1000']}
        self.client.post(reverse('stock:entree'), donnees)
        self.client.post(reverse('stock:entree'), donnees)
        self.assertEqual(BonEntree.objects.filter(jeton=jeton).count(), 1)
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 15)

    def test_erreur_puis_correction_garde_le_meme_jeton(self):
        jeton = self.client.get(reverse('stock:sortie')).context['jeton']
        donnees = {'jeton': jeton, 'chantier': 'Pont', 'demandeur': 'Ali', 'article': [self.filtre.pk], 'quantite': ['abc']}
        rep = self.client.post(reverse('stock:sortie'), donnees)
        self.assertEqual(rep.context['jeton'], jeton)
        self.assertContains(rep, f'value="{jeton}"')
        donnees['quantite'] = ['1']
        self.client.post(reverse('stock:sortie'), donnees)
        self.assertEqual(BonSortie.objects.get().jeton, jeton)

    def test_jeton_inconnu_ignore(self):
        self.client.post(reverse('stock:sortie'), {'jeton': 'x' * 32, 'chantier': 'Pont', 'demandeur': 'Ali',
                                                   'article': [self.filtre.pk], 'quantite': ['1']})
        self.assertIsNone(BonSortie.objects.get().jeton)

    def test_bouton_desactive_a_l_envoi(self):
        page = self.client.get(reverse('stock:sortie')).content.decode()
        self.assertIn("addEventListener('submit'", page)
        self.assertIn('Enregistrement…', page)

    def test_sqlite_en_mode_immediate(self):
        from django.db import connection

        self.assertEqual(connection.settings_dict['OPTIONS']['transaction_mode'], 'IMMEDIATE')
        self.assertEqual(connection.settings_dict['OPTIONS']['timeout'], 20)


class SaisieNombresTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.entree(self.filtre, 10, 1000)

    def test_nombre_saisi(self):
        from django.core.exceptions import ValidationError

        from .forms import nombre_saisi, prix_saisi

        self.assertIsNone(nombre_saisi('  '))
        self.assertEqual(nombre_saisi('1 250,5'), Decimal('1250.5'))
        self.assertEqual(nombre_saisi('1 250'), Decimal('1250'))
        self.assertEqual(prix_saisi('15000000'), Decimal('15000000'))
        refus = {
            'abc': "n'est pas un nombre", '1e5': "n'est pas un nombre", '-3': 'négatif',
            '0,0004': '3 chiffres après la virgule', '2000000000,5': 'trop grand',
            '3017620422003': 'code-barres', '12345678': 'code-barres',
        }
        for texte, message in refus.items():
            with self.assertRaisesMessage(ValidationError, message):
                nombre_saisi(texte)
        with self.assertRaisesMessage(ValidationError, '2 chiffres après la virgule'):
            prix_saisi('10,555')
        with self.assertRaisesMessage(ValidationError, 'trop grand'):
            prix_saisi('1' + '0' * 17)

    def test_sortie_saisies_refusees_sans_erreur_500(self):
        cas = {'999999999999': 'code-barres', '1e20': 'est pas un nombre', '0,0004': 'chiffres après la virgule',
               '-5': 'négatif', '0': 'plus grande que 0', '': 'plus grande que 0', '1 000 000 001': 'trop grand'}
        for qte, message in cas.items():
            rep = self.client.post(reverse('stock:sortie'), {
                'chantier': 'Pont', 'demandeur': 'Ali', 'article': [self.filtre.pk], 'quantite': [qte]})
            self.assertEqual(rep.status_code, 200, qte)
            self.assertContains(rep, message)
            self.assertContains(rep, 'Filtre à huile Volvo')  # la ligne est réaffichée pour être corrigée
        for identifiant in ['²', '9' * 30, 'abc']:
            rep = self.client.post(reverse('stock:sortie'), {
                'chantier': 'Pont', 'demandeur': 'Ali', 'article': [identifiant], 'quantite': ['1']})
            self.assertContains(rep, 'choisissez un article')
        self.assertFalse(BonSortie.objects.exists())

    def test_entree_prix_refuse(self):
        for prix in ['1e17', '1' + '0' * 17, '12,345', '-1']:
            rep = self.client.post(reverse('stock:entree'), {'article': [self.filtre.pk], 'quantite': ['1'],
                                                             'prix': [prix]})
            self.assertEqual(rep.status_code, 200, prix)
            self.assertContains(rep, 'prix :')
        self.assertEqual(BonEntree.objects.count(), 1)

    def test_correction_refusee(self):
        for compte in ['1e20', '99999999999999', 'abc', '-2', '']:
            rep = self.client.post(reverse('stock:article_corriger', args=[self.filtre.pk]), {'stock_compte': compte},
                                   follow=True)
            self.assertEqual(rep.status_code, 200, compte)
            self.assertContains(rep, 'Quantité comptée')
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 10)
        self.client.post(reverse('stock:article_corriger', args=[self.filtre.pk]), {'stock_compte': '7,5'})
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, Decimal('7.5'))

    def test_fiche_article_saisies_refusees(self):
        base = {'designation': 'X', 'categorie_nom': 'Filtres', 'unite': 'U', 'actif': 'on'}
        for champ, valeur in [('stock_initial', '1e15'), ('stock_initial', '9' * 15), ('prix_initial', '1e17'),
                              ('stock_min', 'abc'), ('stock_min', '-1'), ('niveau', '99999999999'),
                              ('niveau', '9' * 25)]:
            rep = self.client.post(reverse('stock:article_nouveau'), {**base, champ: valeur})
            self.assertEqual(rep.status_code, 200, (champ, valeur))
            self.assertTrue(rep.context['form'].errors, (champ, valeur))
        self.assertFalse(Article.objects.filter(designation='X').exists())


class FicheArticleTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_seuil_affiche_sans_zeros(self):
        self.filtre.stock_min = Decimal('100')
        self.filtre.save()
        page = self.client.get(reverse('stock:article_modifier', args=[self.filtre.pk])).content.decode()
        self.assertIn('name="stock_min" value="100"', page)
        self.assertNotIn('100,000', page)

    def test_code_en_double_refuse_meme_casse_differente(self):
        rep = self.client.post(reverse('stock:article_nouveau'), {
            'code': ' fh-01 ', 'designation': 'Doublon', 'categorie_nom': 'Filtres', 'unite': 'U', 'actif': 'on'})
        self.assertContains(rep, 'Ce code existe déjà : Filtre à huile Volvo')
        self.assertFalse(Article.objects.filter(designation='Doublon').exists())
        # Le même article peut garder son propre code.
        rep = self.client.post(reverse('stock:article_modifier', args=[self.filtre.pk]), {
            'code': 'FH-01', 'designation': 'Filtre à huile Volvo', 'categorie_nom': 'Filtres', 'unite': 'U',
            'stock_min': '2', 'actif': 'on'})
        self.assertRedirects(rep, reverse('stock:articles'))

    def test_code_cree_automatiquement(self):
        def creer(designation, categorie):
            self.client.post(reverse('stock:article_nouveau'), {
                'code': '', 'designation': designation, 'categorie_nom': categorie, 'unite': 'U', 'actif': 'on'})
            return Article.objects.get(designation=designation).code

        self.assertEqual(creer('Sable', 'Matériaux'), 'MAT-0001')
        self.assertEqual(creer('Gravier', 'matériaux'), 'MAT-0002')
        Article.objects.create(code='ECL-0041', designation='Lampe', categorie=self.cat)
        self.assertEqual(creer('Projecteur', 'Éclairage'), 'ECL-0042')
        self.assertEqual(creer('Divers', '123'), 'ART-0001')
        page = self.client.get(reverse('stock:article_nouveau')).content.decode()
        self.assertIn('Laissez vide', page)

    def test_unite_bloquee_apres_mouvement(self):
        donnees = {'code': 'CIM-45', 'designation': 'Ciment', 'categorie_nom': 'Matériaux', 'unite': 'KG',
                   'stock_min': '0', 'actif': 'on'}
        rep = self.client.post(reverse('stock:article_modifier', args=[self.ciment.pk]), donnees)
        self.assertRedirects(rep, reverse('stock:articles'))  # aucun mouvement : changement accepté
        self.entree(self.ciment, 5, 100)
        page = self.client.get(reverse('stock:article_modifier', args=[self.ciment.pk]))
        self.assertTrue(page.context['form'].fields['unite'].disabled)
        self.assertContains(page, 'Ne peut plus changer')
        donnees['unite'] = 'SAC'  # envoyé quand même : ignoré
        rep = self.client.post(reverse('stock:article_modifier', args=[self.ciment.pk]), donnees)
        self.assertRedirects(rep, reverse('stock:articles'))
        self.ciment.refresh_from_db()
        self.assertEqual(self.ciment.unite, 'KG')

    def test_desactivation_refusee_avec_du_stock(self):
        self.entree(self.ciment, 5, 100)
        donnees = {'code': 'CIM-45', 'designation': 'Ciment', 'categorie_nom': 'Matériaux', 'unite': 'SAC',
                   'stock_min': '0'}  # case « actif » décochée
        rep = self.client.post(reverse('stock:article_modifier', args=[self.ciment.pk]), donnees)
        self.assertContains(rep, 'mettez d&#x27;abord le stock à 0')
        self.ciment.refresh_from_db()
        self.assertTrue(self.ciment.actif)
        self.client.post(reverse('stock:article_corriger', args=[self.ciment.pk]), {'stock_compte': '0'})
        rep = self.client.post(reverse('stock:article_modifier', args=[self.ciment.pk]), donnees)
        self.assertRedirects(rep, reverse('stock:articles'))
        self.ciment.refresh_from_db()
        self.assertFalse(self.ciment.actif)

    def test_photo_reduite_en_jpeg(self):
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        tampon = io.BytesIO()
        Image.new('RGBA', (3200, 2000), (200, 30, 30, 128)).save(tampon, 'PNG')
        donnees = {'code': 'FH-01', 'designation': 'Filtre à huile Volvo', 'categorie_nom': 'Filtres', 'unite': 'U',
                   'stock_min': '2', 'actif': 'on'}
        with tempfile.TemporaryDirectory() as dossier, override_settings(MEDIA_ROOT=dossier):
            photo = SimpleUploadedFile('grande.png', tampon.getvalue(), content_type='image/png')
            rep = self.client.post(reverse('stock:article_modifier', args=[self.filtre.pk]), {**donnees, 'photo': photo})
            self.assertRedirects(rep, reverse('stock:articles'))
            self.filtre.refresh_from_db()
            self.assertTrue(self.filtre.photo.name.endswith('.jpg'))
            with Image.open(self.filtre.photo.path) as image:
                self.assertEqual((image.format, max(image.size)), ('JPEG', 1600))
            with mock.patch('stock.forms.PHOTO_MAX_OCTETS', 1000):
                photo = SimpleUploadedFile('grande.png', tampon.getvalue(), content_type='image/png')
                rep = self.client.post(reverse('stock:article_modifier', args=[self.filtre.pk]),
                                       {**donnees, 'photo': photo})
                self.assertContains(rep, 'Photo trop lourde')

    def test_nouvelles_unites(self):
        unites = dict(Article.UNITES)
        for code in ['PAIRE', 'CARTON', 'PAQ', 'BARRE', 'LOT', 'FEUILLE']:
            self.assertIn(code, unites)
        rep = self.client.post(reverse('stock:article_nouveau'), {
            'designation': 'Gants cuir', 'categorie_nom': 'EPI', 'unite': 'PAIRE', 'actif': 'on'})
        self.assertRedirects(rep, reverse('stock:articles'))
        self.assertEqual(Article.objects.get(designation='Gants cuir').get_unite_display(), 'Paire')


class AffichageTests(Base):
    def test_milliers_insecables(self):
        from .utils import nombre, quantite

        self.assertEqual(nombre(2080000), '2\xa0080\xa0000')
        self.assertEqual(quantite(Decimal('12500.500')), '12\xa0500,5')
        self.assertEqual(quantite(Decimal('100.000')), '100')

    def test_message_affiche_une_seule_fois(self):
        self.client.force_login(self.user)
        rep = self.client.post(reverse('stock:entree'), {'article': [self.filtre.pk], 'quantite': ['3'],
                                                         'prix': ['100']}, follow=True)
        self.assertEqual(rep.content.decode().count('message-success'), 1)
        self.assertContains(rep, 'enregistrée')
        # Les pages qui n'affichaient pas les messages les montrent maintenant (ici : l'historique).
        self.client.post(reverse('stock:bon_annuler', args=['entree', BonEntree.objects.get().pk]))
        self.assertContains(self.client.get(reverse('stock:bons')), 'est annulé', count=1)


class PremierLancementErreursTests(TestCase):
    def test_toutes_les_erreurs_de_mot_de_passe_ensemble(self):
        rep = self.client.post(reverse('stock:bienvenue'), {'username': 'awa', 'password': '1234',
                                                            'password2': '12345'})
        for texte in ['pas identiques', 'trop court', 'entièrement numérique']:
            self.assertContains(rep, texte)
        self.assertNotContains(rep, 'value="1234"')
        self.assertContains(rep, 'value="awa"')
        self.assertFalse(get_user_model().objects.exists())

    def test_mot_de_passe_garde_quand_il_est_bon(self):
        rep = self.client.post(reverse('stock:bienvenue'), {'username': '', 'password': 'Magasin-2026!',
                                                            'password2': 'Magasin-2026!'})
        self.assertContains(rep, 'Choisissez un nom d&#x27;utilisateur')
        self.assertContains(rep, 'value="Magasin-2026!"', count=2)


class ReglagesSecuriteTests(TestCase):
    def test_cle_secrete_creee_puis_reutilisee(self):
        import tempfile
        from pathlib import Path

        from config import settings as reglages

        with tempfile.TemporaryDirectory() as dossier, mock.patch.object(reglages, 'DATA_DIR', Path(dossier)):
            cle = reglages._secret_local()
            self.assertGreater(len(cle), 40)
            self.assertEqual(reglages._secret_local(), cle)
            self.assertEqual((Path(dossier) / 'secret.key').read_text().strip(), cle)

    def test_page_expiree_en_francais(self):
        from django.test import Client

        user = get_user_model().objects.create_user('awa', password='Magasin-2026!')
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        rep = client.post(reverse('stock:sortie'), {'chantier': 'X'})
        self.assertEqual(rep.status_code, 403)
        self.assertContains(rep, 'La page a expiré, rechargez-la', status_code=403)
        self.assertContains(rep, "Retour à l'accueil", status_code=403)
