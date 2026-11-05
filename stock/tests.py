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
        self.assertRedirects(rep, reverse('stock:bon_detail', args=['entree', bon.pk]) + '?nouveau=1')
        self.assertEqual(bon.statut, 'VALIDE')
        self.assertEqual(bon.fournisseur.nom, 'Nouveau fournisseur')
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 10)
        self.assertEqual(self.filtre.prix_moyen, Decimal('1500.50'))
        self.assertEqual(self.client.get(rep.url).status_code, 200)

    def test_sortie_cree_le_chantier_et_refuse_le_stock_insuffisant(self):
        self.entree(self.filtre, 3, 100)
        donnees = {'chantier': 'Nouveau chantier', 'demandeur': 'Ali', 'article': [self.filtre.pk], 'quantite': ['5']}
        rep = self.client.post(reverse('stock:sortie'), donnees)
        self.assertContains(rep, 'Oui, créer ce chantier')  # pas de création sans confirmation
        donnees['creer_chantier'] = 'Nouveau chantier'
        rep = self.client.post(reverse('stock:sortie'), donnees)
        self.assertEqual(rep.status_code, 200)
        self.assertContains(rep, 'Filtre à huile Volvo : il reste 3 unités, vous en demandez 5.')
        self.assertFalse(BonSortie.objects.exists())  # rien n'est gardé
        self.assertFalse(Chantier.objects.filter(nom='Nouveau chantier').exists())
        rep = self.client.post(reverse('stock:sortie'), {
            'chantier': 'nouveau  CHANTIER ', 'creer_chantier': 'Nouveau chantier', 'demandeur': 'Ali',
            'article': [self.filtre.pk], 'quantite': ['2'],
        })
        bon = BonSortie.objects.get()
        self.assertEqual(bon.chantier.nom, 'nouveau CHANTIER')
        self.assertRedirects(rep, reverse('stock:bon_detail', args=['sortie', bon.pk]) + '?nouveau=1')
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 1)
        # Annulation depuis l'écran du bon
        self.client.post(reverse('stock:bon_annuler', args=['sortie', bon.pk]), {'motif': 'Erreur'})
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
        self.assertRedirects(rep, reverse('stock:bon_detail', args=['sortie', bon.pk]) + '?nouveau=1')
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
        self.client.post(reverse('stock:bon_annuler', args=['sortie', bon.pk]), {'motif': 'Mauvais chantier'})
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
        self.assertEqual(premier.url, reverse('stock:bon_detail', args=['sortie', bon.pk]) + '?nouveau=1')
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
        self.client.post(reverse('stock:bon_annuler', args=['entree', BonEntree.objects.get().pk]),
                         {'motif': 'Erreur de saisie'})
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


# =========================================================
# Justesse du stock : prix moyen, inventaire, annulation, messages, dates, brouillons
# =========================================================

class JustesseBase(Base):
    def setUp(self):
        super().setUp()
        self.magasinier = get_user_model().objects.create_user('awa', password='Magasin-2026!')

    def il_y_a(self, **duree):
        from datetime import timedelta

        from django.utils import timezone

        return timezone.now() - timedelta(**duree)

    def entree_le(self, date, article, qte, prix=0):
        bon = BonEntree.objects.create(date=date)
        LigneEntree.objects.create(bon=bon, article=article, quantite=qte, prix_unitaire=prix)
        return services.valider(bon, self.user)

    def sortie_le(self, date, article, qte):
        bon = BonSortie.objects.create(date=date, chantier=self.chantier, demandeur='Ali')
        LigneSortie.objects.create(bon=bon, article=article, quantite=qte)
        return services.valider(bon, self.user)

    def inventaire(self, *lignes, date=None):
        """lignes : (article, quantité comptée ou None, compté le)."""
        inv = Inventaire.objects.create(**({'date': date} if date else {}))
        for article, compte, compte_le in lignes:
            LigneInventaire.objects.create(bon=inv, article=article, stock_compte=compte, compte_le=compte_le)
        return inv

    def stock(self, article):
        article.refresh_from_db()
        return article.stock


class PrixMoyenTests(JustesseBase):
    def test_annulation_d_une_entree_ne_rend_pas_le_prix_negatif(self):
        # Scénario de l'audit : BE1 10 à 1000, BE2 10 à 10, sortie de 5 → 15 à 505 ; annuler BE1.
        be1 = self.entree(self.filtre, 10, 1000)
        self.entree(self.filtre, 10, 10)
        services.valider(self.sortie(self.filtre, 5), self.user)
        self.filtre.refresh_from_db()
        self.assertEqual((self.filtre.stock, self.filtre.prix_moyen), (15, Decimal('505.00')))
        services.annuler(be1, self.user, 'test')
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 5)
        self.assertEqual(self.filtre.prix_moyen, Decimal('505.00'))  # et non -485
        self.assertGreaterEqual(self.filtre.valeur_stock, 0)
        self.assertGreater(MouvementStock.objects.get(type=MouvementStock.ANNULATION).prix_unitaire, 0)

    def test_annulation_qui_vide_le_stock_garde_le_prix_moyen(self):
        self.entree(self.filtre, 10, 1000)
        be = self.entree(self.filtre, 5, 2000)
        services.valider(self.sortie(self.filtre, 10), self.user)
        services.annuler(be, self.user, 'test')
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 0)
        self.assertEqual(self.filtre.prix_moyen, Decimal('1333.33'))

    def test_annulation_simple_recalcule_toujours(self):
        self.entree(self.filtre, 10, 1000)
        be = self.entree(self.filtre, 10, 3000)
        services.annuler(be, self.user, 'test')
        self.filtre.refresh_from_db()
        self.assertEqual((self.filtre.stock, self.filtre.prix_moyen), (10, Decimal('1000.00')))

    def test_stock_sans_prix_prend_le_premier_prix_connu(self):
        # Scénario de l'audit : 100 sacs sans prix puis 10 sacs à 5000 → 5000 (et non 454,55).
        self.entree(self.ciment, 100, 0)
        self.entree(self.ciment, 10, 5000)
        self.ciment.refresh_from_db()
        self.assertEqual(self.ciment.prix_moyen, Decimal('5000.00'))
        self.assertEqual(self.ciment.valeur_stock, Decimal('550000'))
        self.entree(self.ciment, 110, 6000)
        self.ciment.refresh_from_db()
        self.assertEqual(self.ciment.prix_moyen, Decimal('5500.00'))

    def test_cmp(self):
        d = Decimal
        self.assertEqual(services._cmp(d(100), d(0), d(10), d(5000)), d('5000.00'))
        self.assertEqual(services._cmp(d(10), d(1000), d(10), d(0)), d(1000))
        self.assertEqual(services._cmp(d(0), d(0), d(4), d('12.5')), d('12.50'))
        self.assertEqual(services._cmp(d(10), d(1000), d(10), d(2000)), d('1500.00'))


class InventaireJusteTests(JustesseBase):
    def test_sortie_entre_comptage_et_validation_n_est_pas_effacee(self):
        # Scénario de l'audit : 10 comptés le matin, sortie de 3 à midi, validation le soir → 7.
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 1000)
        inv = self.inventaire((self.filtre, Decimal('10'), self.il_y_a(hours=2)))
        services.valider(self.sortie(self.filtre, 3), self.user)
        services.valider(inv, self.user)
        self.assertEqual(self.stock(self.filtre), 7)
        self.assertFalse(MouvementStock.objects.filter(type=MouvementStock.INVENTAIRE).exists())
        ligne = inv.lignes.get()
        self.assertEqual(ligne.stock_theorique, 10)
        self.assertEqual(ligne.ecart, 0)

    def test_ecart_reel_garde_avec_la_sortie_faite_depuis(self):
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 1000)
        inv = self.inventaire((self.filtre, Decimal('8'), self.il_y_a(hours=2)))  # 2 manquent au comptage
        services.valider(self.sortie(self.filtre, 3), self.user)
        services.valider(inv, self.user)
        self.assertEqual(self.stock(self.filtre), 5)
        ligne = inv.lignes.get()
        self.assertEqual((ligne.stock_theorique, ligne.ecart), (10, -2))
        ajustement = MouvementStock.objects.get(type=MouvementStock.INVENTAIRE)
        self.assertEqual(ajustement.quantite, -2)
        # Daté au moment du comptage : la fiche de stock se lit dans l'ordre.
        historique = list(MouvementStock.objects.filter(article=self.filtre).order_by('date', 'id'))
        self.assertEqual([m.type for m in historique], ['ENTREE', 'INVENTAIRE', 'SORTIE'])
        self.assertEqual([m.stock_apres for m in historique], [10, 8, 5])

    def test_entree_entre_comptage_et_validation(self):
        self.entree_le(self.il_y_a(days=1), self.filtre, 4, 1000)
        inv = self.inventaire((self.filtre, Decimal('4'), self.il_y_a(hours=1)))
        self.entree(self.filtre, 6, 1000)
        services.valider(inv, self.user)
        self.assertEqual(self.stock(self.filtre), 10)

    def test_sans_heure_de_comptage_la_date_du_bon_sert(self):
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 1000)
        inv = self.inventaire((self.filtre, Decimal('10'), None), date=self.il_y_a(hours=3))
        services.valider(self.sortie(self.filtre, 4), self.user)
        services.valider(inv, self.user)
        self.assertEqual(self.stock(self.filtre), 6)

    def test_lignes_non_comptees_ignorees(self):
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 1000)
        self.entree_le(self.il_y_a(days=1), self.ciment, 50, 5000)
        inv = self.inventaire((self.filtre, Decimal('9'), self.il_y_a(hours=1)), (self.ciment, None, None))
        services.valider(inv, self.user)
        self.assertEqual(self.stock(self.filtre), 9)
        self.assertEqual(self.stock(self.ciment), 50)
        non_comptee = inv.lignes.get(article=self.ciment)
        self.assertIsNone(non_comptee.stock_theorique)
        self.assertIsNone(non_comptee.ecart)
        self.assertEqual(str(non_comptee), 'CIM-45 : non compté')
        self.assertEqual(inv.total(), Decimal('-1000'))
        self.client.force_login(self.user)
        page = self.client.get(reverse('stock:bon_detail', args=['inventaire', inv.pk]))
        self.assertContains(page, 'non compté')
        pdf = self.client.get(reverse('stock:bon_pdf', args=['inventaire', inv.pk]))
        self.assertEqual(pdf.status_code, 200)

    def test_aucune_ligne_comptee_refusee(self):
        inv = self.inventaire((self.filtre, None, None))
        with self.assertRaisesMessage(services.StockError, "Aucun article n'a été compté"):
            services.valider(inv, self.user)
        inv.refresh_from_db()
        self.assertEqual(inv.statut, Inventaire.BROUILLON)

    def test_annulation_d_un_bon_anterieur_au_comptage(self):
        # Sortie saisie hier par erreur (rien n'est sorti) ; comptage ce matin : 10 ; annulation ensuite.
        self.entree_le(self.il_y_a(days=2), self.filtre, 10, 1000)
        sortie = self.sortie_le(self.il_y_a(days=1), self.filtre, 3)
        inv = self.inventaire((self.filtre, Decimal('10'), self.il_y_a(hours=1)))
        services.annuler(sortie, self.user, 'Saisie en double')
        services.valider(inv, self.user)
        self.assertEqual(self.stock(self.filtre), 10)  # et non 13

    def test_sortie_faite_et_annulee_apres_le_comptage(self):
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 1000)
        inv = self.inventaire((self.filtre, Decimal('10'), self.il_y_a(hours=1)))
        sortie = services.valider(self.sortie(self.filtre, 3), self.user)
        services.annuler(sortie, self.user, 'Erreur')
        services.valider(inv, self.user)
        self.assertEqual(self.stock(self.filtre), 10)

    def test_comptage_depasse_par_une_correction_plus_recente(self):
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 1000)
        ancien = self.inventaire((self.filtre, Decimal('10'), self.il_y_a(hours=2)))
        correction = self.inventaire((self.filtre, Decimal('6'), self.il_y_a(hours=1)))
        services.valider(correction, self.user)
        services.valider(ancien, self.user)
        self.assertEqual(self.stock(self.filtre), 6)  # le comptage le plus récent l'emporte
        ligne = LigneInventaire.objects.get(bon=ancien)
        self.assertTrue(ligne.ignoree)
        self.assertIsNone(ligne.ecart)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('stock:bon_detail', args=['inventaire', ancien.pk])),
                            'recompté depuis')

    def test_comptage_plus_ancien_valide_apres_garde_le_plus_recent(self):
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 1000)
        ancien = self.inventaire((self.filtre, Decimal('9'), self.il_y_a(hours=3)))
        recent = self.inventaire((self.filtre, Decimal('7'), self.il_y_a(hours=1)))
        services.valider(ancien, self.user)
        self.assertEqual(self.stock(self.filtre), 9)
        services.valider(recent, self.user)
        self.assertEqual(self.stock(self.filtre), 7)

    def test_comptage_plus_petit_que_les_sorties_faites_depuis(self):
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 1000)
        inv = self.inventaire((self.filtre, Decimal('2'), self.il_y_a(hours=1)))
        services.valider(self.sortie(self.filtre, 5), self.user)
        with self.assertRaises(services.StockError) as erreur:
            services.valider(inv, self.user)
        self.assertEqual(erreur.exception.messages, [
            'Filtre à huile Volvo : le comptage indique 2 unités, mais il en est sorti 5 depuis. '
            'Recomptez cet article.'])
        self.assertEqual(self.stock(self.filtre), 5)

    def test_correction_depuis_la_fiche_article_note_l_heure(self):
        self.entree(self.filtre, 10, 100)
        self.client.force_login(self.user)
        self.client.post(reverse('stock:article_corriger', args=[self.filtre.pk]), {'stock_compte': '8'})
        ligne = LigneInventaire.objects.get()
        self.assertIsNotNone(ligne.compte_le)
        self.assertEqual((ligne.stock_theorique, ligne.ecart), (10, -2))

    def test_admin_note_l_heure_du_comptage(self):
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 1000)
        self.entree_le(self.il_y_a(days=1), self.ciment, 5, 1000)
        self.client.force_login(self.user)
        donnees = {
            'date_0': '01/10/2026', 'date_1': '08:00:00', 'observation': 'Inventaire',
            'lignes-TOTAL_FORMS': '2', 'lignes-INITIAL_FORMS': '0', 'lignes-MIN_NUM_FORMS': '0',
            'lignes-MAX_NUM_FORMS': '1000',
            'lignes-0-article': self.filtre.pk, 'lignes-0-stock_compte': '9',
            'lignes-1-article': self.ciment.pk, 'lignes-1-stock_compte': '',
        }
        rep = self.client.post(reverse('admin:stock_inventaire_add'), donnees)
        self.assertEqual(rep.status_code, 302, rep.content.decode()[:3000])
        inv = Inventaire.objects.get()
        compte = inv.lignes.get(article=self.filtre)
        self.assertIsNotNone(compte.compte_le)
        self.assertIsNone(inv.lignes.get(article=self.ciment).compte_le)
        # Modification d'une seule ligne : l'heure de l'autre ne bouge pas.
        donnees.update({'lignes-INITIAL_FORMS': '2', 'lignes-0-id': compte.pk, 'lignes-0-bon': inv.pk,
                        'lignes-1-id': inv.lignes.get(article=self.ciment).pk, 'lignes-1-bon': inv.pk,
                        'lignes-1-stock_compte': '5'})
        rep = self.client.post(reverse('admin:stock_inventaire_change', args=[inv.pk]), donnees)
        self.assertEqual(rep.status_code, 302, rep.content.decode()[:3000])
        self.assertEqual(inv.lignes.get(article=self.filtre).compte_le, compte.compte_le)
        self.assertIsNotNone(inv.lignes.get(article=self.ciment).compte_le)
        self.assertEqual(self.client.get(reverse('admin:stock_inventaire_change', args=[inv.pk])).status_code, 200)


class AnnulationTests(JustesseBase):
    def test_annulation_refusee_apres_un_inventaire(self):
        # Scénario de l'audit : sortie de 5, inventaire qui compte 5, puis annulation de la sortie.
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 1000)
        sortie = services.valider(self.sortie(self.filtre, 5), self.user)
        services.valider(self.inventaire((self.filtre, Decimal('5'), None)), self.user)
        with self.assertRaisesMessage(services.StockError, 'un inventaire a été fait depuis'):
            services.annuler(sortie, self.user, 'Erreur')
        sortie.refresh_from_db()
        self.assertEqual(sortie.statut, BonSortie.VALIDE)
        self.assertEqual(self.stock(self.filtre), 5)

    def test_annulation_refusee_apres_une_correction_de_stock(self):
        entree = self.entree(self.filtre, 10, 1000)
        self.client.force_login(self.user)
        self.client.post(reverse('stock:article_corriger', args=[self.filtre.pk]), {'stock_compte': '9'})
        rep = self.client.post(reverse('stock:bon_annuler', args=['entree', entree.pk]), {'motif': 'Erreur'},
                               follow=True)
        self.assertContains(rep, 'un inventaire a été fait depuis')
        self.assertContains(rep, 'Faites une correction du stock')
        self.assertEqual(self.stock(self.filtre), 9)

    def test_annulation_permise_si_l_inventaire_est_plus_ancien_ou_sur_un_autre_article(self):
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 1000)
        self.entree_le(self.il_y_a(days=1), self.ciment, 10, 1000)
        services.valider(self.inventaire((self.filtre, Decimal('10'), None)), self.user)
        sortie = services.valider(self.sortie(self.filtre, 2), self.user)
        services.valider(self.inventaire((self.ciment, Decimal('10'), None)), self.user)
        services.annuler(sortie, self.user, 'Erreur')
        self.assertEqual(self.stock(self.filtre), 10)

    def test_qui_quand_pourquoi(self):
        self.entree(self.filtre, 10, 1000)
        sortie = services.valider(self.sortie(self.filtre, 2), self.user)
        services.annuler(sortie, self.magasinier, '  Mauvais chantier  ')
        sortie.refresh_from_db()
        self.assertEqual(sortie.annule_par, self.magasinier)
        self.assertIsNotNone(sortie.date_annulation)
        self.assertEqual(sortie.motif_annulation, 'Mauvais chantier')
        self.assertEqual(sortie.valide_par, self.user)  # la validation reste tracée
        self.assertIsNotNone(sortie.date_validation)
        brouillon = self.sortie(self.filtre, 1)
        services.annuler(brouillon, self.user, 'Inutile')
        brouillon.refresh_from_db()
        self.assertEqual((brouillon.statut, brouillon.annule_par, brouillon.motif_annulation),
                         ('ANNULE', self.user, 'Inutile'))
        self.assertIsNone(brouillon.valide_par)

    def test_motif_obligatoire_et_affiche(self):
        self.entree(self.filtre, 10, 1000)
        self.user.first_name = 'Koffi'
        self.user.save()
        sortie = services.valider(self.sortie(self.filtre, 2), self.user)
        self.client.force_login(self.user)
        url = reverse('stock:bon_detail', args=['sortie', sortie.pk])
        page = self.client.get(url)
        self.assertContains(page, 'name="motif"')
        self.assertContains(page, 'required')
        rep = self.client.post(reverse('stock:bon_annuler', args=['sortie', sortie.pk]), {'motif': '   '}, follow=True)
        self.assertContains(rep, "Indiquez le motif de l&#x27;annulation")
        sortie.refresh_from_db()
        self.assertEqual(sortie.statut, 'VALIDE')
        self.client.post(reverse('stock:bon_annuler', args=['sortie', sortie.pk]), {'motif': 'Mauvais <b>chantier</b>'})
        sortie.refresh_from_db()
        self.assertEqual(sortie.statut, 'ANNULE')
        page = self.client.get(url)
        self.assertContains(page, 'Bon annulé</b> par Koffi le ')
        self.assertContains(page, ': Mauvais &lt;b&gt;chantier&lt;/b&gt;')
        self.assertNotContains(page, 'name="motif"')
        self.assertEqual(self.stock(self.filtre), 10)

    def test_admin_annule_avec_un_motif(self):
        self.entree(self.filtre, 10, 1000)
        sortie = services.valider(self.sortie(self.filtre, 2), self.user)
        self.client.force_login(self.user)
        self.client.post(reverse('admin:stock_bonsortie_changelist'),
                         {'action': 'annuler_bons', '_selected_action': [sortie.pk]})
        sortie.refresh_from_db()
        self.assertEqual((sortie.statut, sortie.annule_par), ('ANNULE', self.user))
        self.assertTrue(sortie.motif_annulation)


class MessagesStockTests(JustesseBase):
    def setUp(self):
        super().setUp()
        self.kit = Article.objects.create(code='JT-KIT-320', designation='Kit joints vérin pelle 320',
                                          categorie=self.cat, unite='JEU')
        self.entree(self.kit, 1, 85000)

    def test_message_stock_insuffisant_clair(self):
        with self.assertRaises(services.StockError) as erreur:
            services.valider(self.sortie(self.kit, 5), self.user)
        self.assertEqual(erreur.exception.messages, ['Kit joints vérin pelle 320 : il reste 1 JEU, vous en demandez 5.'])

    def test_message_a_l_ecran_sans_numero_fantome(self):
        self.client.force_login(self.magasinier)
        rep = self.client.post(reverse('stock:sortie'), {
            'chantier': 'Pont', 'demandeur': 'Ali', 'article': [self.kit.pk, self.filtre.pk], 'quantite': ['5', '2,5']})
        self.assertContains(rep, 'Kit joints vérin pelle 320 : il reste 1 JEU, vous en demandez 5.')
        self.assertContains(rep, "Filtre à huile Volvo : il n&#x27;en reste plus, vous en demandez 2,5.")
        self.assertNotContains(rep, 'BS-')
        self.assertFalse(BonSortie.objects.exists())

    def test_annulation_d_une_entree_deja_sortie(self):
        entree = self.entree(self.ciment, Decimal('12.5'), 5000)
        services.valider(self.sortie(self.ciment, 10), self.user)
        with self.assertRaises(services.StockError) as erreur:
            services.annuler(entree, self.user, 'Erreur')
        self.assertEqual(erreur.exception.messages, [
            "Impossible d'annuler : une partie de la marchandise est déjà sortie.",
            'Ciment CPJ 45 sac 50 kg : il reste 2,5 SAC, ce bon en a fait entrer 12,5.'])

    def test_autres_messages(self):
        with self.assertRaisesMessage(services.StockError, 'Le bon ne contient aucun article.'):
            services.valider(BonSortie.objects.create(chantier=self.chantier, demandeur='Ali'), self.user)
        inv = services.valider(self.inventaire((self.kit, Decimal('1'), None)), self.user)
        with self.assertRaisesMessage(services.StockError, 'Un inventaire validé ne peut pas être annulé'):
            services.annuler(inv, self.user)

    def test_quantite_unite(self):
        from .utils import quantite_unite

        self.assertEqual(quantite_unite(Decimal('1.000'), 'JEU'), '1 JEU')
        self.assertEqual(quantite_unite(Decimal('1'), 'U'), '1 unité')
        self.assertEqual(quantite_unite(Decimal('3'), 'U'), '3 unités')
        self.assertEqual(quantite_unite(Decimal('12.500'), 'M2'), '12,5 m²')
        self.assertEqual(quantite_unite(Decimal('1500'), 'SAC'), '1\xa0500 SAC')


class DateDesBonsTests(JustesseBase):
    def setUp(self):
        super().setUp()
        self.entree_le(self.il_y_a(days=20), self.filtre, 10, 1000)

    def jour(self, decalage):
        from datetime import timedelta

        from django.utils import timezone

        return timezone.localdate() + timedelta(days=decalage)

    def test_date_du_bon(self):
        from django import forms
        from django.utils import timezone

        from .forms import date_du_bon

        maintenant = timezone.localtime()
        self.assertLess(abs((date_du_bon('') - maintenant).total_seconds()), 5)
        self.assertLess(abs((date_du_bon(self.jour(0).isoformat()) - maintenant).total_seconds()), 5)
        hier = timezone.localtime(date_du_bon(self.jour(-1).isoformat()))
        self.assertEqual(hier.date(), self.jour(-1))
        self.assertEqual(hier.hour, maintenant.hour)
        self.assertEqual(timezone.localtime(date_du_bon(self.jour(-7).isoformat())).date(), self.jour(-7))
        for texte, responsable, message in [
            (self.jour(1).isoformat(), True, 'futur'),
            (self.jour(-8).isoformat(), False, '7 jours au plus'),
            ('31/02/2026', True, 'calendrier'),
            ('1890-01-01', True, 'trop ancienne'),
        ]:
            with self.assertRaisesMessage(forms.ValidationError, message):
                date_du_bon(texte, responsable)
        self.assertEqual(timezone.localtime(date_du_bon(self.jour(-60).isoformat(), True)).date(), self.jour(-60))

    def test_formulaire_propose_la_date_du_jour(self):
        self.client.force_login(self.magasinier)
        page = self.client.get(reverse('stock:sortie'))
        self.assertContains(page, f'type="date" value="{self.jour(0).isoformat()}" max="{self.jour(0).isoformat()}"')
        self.assertContains(page, f'min="{self.jour(-7).isoformat()}"')
        self.client.force_login(self.user)
        page = self.client.get(reverse('stock:entree'))
        self.assertContains(page, 'name="date"')
        self.assertNotContains(page, 'min="')

    def test_sortie_saisie_apres_coup(self):
        from django.utils import timezone

        self.client.force_login(self.magasinier)
        rep = self.client.post(reverse('stock:sortie'), {
            'chantier': 'Pont', 'demandeur': 'Ali', 'date': self.jour(-3).isoformat(),
            'article': [self.filtre.pk], 'quantite': ['2']})
        bon = BonSortie.objects.get()
        self.assertRedirects(rep, reverse('stock:bon_detail', args=['sortie', bon.pk]) + '?nouveau=1')
        self.assertEqual(timezone.localtime(bon.date).date(), self.jour(-3))
        mvt = MouvementStock.objects.get(reference=bon.numero)
        self.assertEqual(timezone.localtime(mvt.date).date(), self.jour(-3))
        self.assertEqual(timezone.localtime(mvt.date).hour, timezone.localtime().hour)
        page = self.client.get(rep.url)
        self.assertContains(page, f'enregistré le {self.jour(0):%d/%m/%Y}')
        # Le rapport de consommation range la sortie à sa vraie date.
        rapport = self.client.get(reverse('stock:consommation'), {'du': self.jour(-3).isoformat(),
                                                                  'au': self.jour(-3).isoformat()})
        self.assertContains(rapport, 'FH-01')

    def test_entree_saisie_apres_coup_par_un_responsable(self):
        from django.utils import timezone

        self.client.force_login(self.user)
        self.client.post(reverse('stock:entree'), {'date': self.jour(-15).isoformat(), 'article': [self.filtre.pk],
                                                   'quantite': ['5'], 'prix': ['1000']})
        bon = BonEntree.objects.get(date__gt=self.il_y_a(days=16), date__lt=self.il_y_a(days=14))
        self.assertEqual(timezone.localtime(MouvementStock.objects.get(reference=bon.numero).date).date(), self.jour(-15))

    def test_dates_refusees_a_l_ecran(self):
        self.client.force_login(self.magasinier)
        donnees = {'chantier': 'Pont', 'demandeur': 'Ali', 'article': [self.filtre.pk], 'quantite': ['2']}
        rep = self.client.post(reverse('stock:sortie'), {**donnees, 'date': self.jour(-8).isoformat()})
        self.assertContains(rep, 'vous pouvez remonter de 7 jours au plus')
        self.assertContains(rep, f'value="{self.jour(-8).isoformat()}"')  # la saisie est gardée
        rep = self.client.post(reverse('stock:sortie'), {**donnees, 'date': self.jour(1).isoformat()})
        self.assertContains(rep, 'dans le futur')
        rep = self.client.post(reverse('stock:entree'), {'date': 'n importe quoi', 'article': [self.filtre.pk],
                                                         'quantite': ['1']})
        self.assertContains(rep, 'choisissez une date dans le calendrier')
        self.assertFalse(BonSortie.objects.exists())
        self.assertEqual(BonEntree.objects.count(), 1)
        self.assertEqual(self.stock(self.filtre), 10)

    def test_stock_apres_suit_l_ordre_des_dates(self):
        # 20 j : +10 ; aujourd'hui : -3 ; puis une sortie de 2 datée d'il y a 2 jours, saisie en dernier.
        services.valider(self.sortie(self.filtre, 3), self.user)
        self.sortie_le(self.il_y_a(days=2), self.filtre, 2)
        self.entree_le(self.il_y_a(days=5), self.filtre, 4, 1000)
        historique = list(MouvementStock.objects.filter(article=self.filtre).order_by('date', 'id'))
        self.assertEqual([m.quantite for m in historique], [10, 4, -2, -3])
        self.assertEqual([m.stock_apres for m in historique], [10, 14, 12, 9])
        self.assertEqual(self.stock(self.filtre), 9)


class BrouillonsTests(JustesseBase):
    def setUp(self):
        super().setUp()
        self.entree(self.filtre, 10, 1000)
        self.brouillon = self.sortie(self.filtre, 4)
        self.url = reverse('stock:bon_detail', args=['sortie', self.brouillon.pk])

    def test_responsable_valide_le_brouillon(self):
        self.client.force_login(self.user)
        page = self.client.get(self.url)
        self.assertContains(page, "le stock n'a pas encore changé")
        self.assertContains(page, reverse('stock:bon_valider', args=['sortie', self.brouillon.pk]))
        self.assertContains(page, reverse('stock:bon_supprimer', args=['sortie', self.brouillon.pk]))
        self.assertNotContains(page, 'Annuler ce bon')
        rep = self.client.post(reverse('stock:bon_valider', args=['sortie', self.brouillon.pk]), follow=True)
        self.assertRedirects(rep, self.url)
        self.assertContains(rep, 'est validé')
        self.brouillon.refresh_from_db()
        self.assertEqual((self.brouillon.statut, self.brouillon.valide_par), ('VALIDE', self.user))
        self.assertEqual(self.stock(self.filtre), 6)
        self.assertNotContains(rep, reverse('stock:bon_valider', args=['sortie', self.brouillon.pk]))

    def test_validation_refusee_affiche_le_message(self):
        LigneSortie.objects.create(bon=self.brouillon, article=self.filtre, quantite=7)
        self.client.force_login(self.user)
        rep = self.client.post(reverse('stock:bon_valider', args=['sortie', self.brouillon.pk]), follow=True)
        self.assertContains(rep, 'Filtre à huile Volvo : il reste 10 unités, vous en demandez 11.')
        self.brouillon.refresh_from_db()
        self.assertEqual(self.brouillon.statut, 'BROUILLON')

    def test_responsable_supprime_le_brouillon(self):
        self.client.force_login(self.user)
        rep = self.client.post(reverse('stock:bon_supprimer', args=['sortie', self.brouillon.pk]), follow=True)
        self.assertRedirects(rep, reverse('stock:bons'))
        self.assertContains(rep, 'supprimé')
        self.assertFalse(BonSortie.objects.filter(pk=self.brouillon.pk).exists())
        self.assertFalse(LigneSortie.objects.exists())
        self.assertEqual(self.stock(self.filtre), 10)

    def test_un_bon_valide_ne_se_supprime_pas(self):
        bon = BonEntree.objects.get()
        self.client.force_login(self.user)
        rep = self.client.post(reverse('stock:bon_supprimer', args=['entree', bon.pk]), follow=True)
        self.assertContains(rep, 'seul un brouillon peut être supprimé')
        self.assertTrue(BonEntree.objects.filter(pk=bon.pk).exists())
        self.assertEqual(self.client.get(reverse('stock:bon_supprimer', args=['entree', bon.pk])).status_code, 405)

    def test_magasinier_ne_valide_ni_ne_supprime(self):
        self.client.force_login(self.magasinier)
        page = self.client.get(self.url)
        self.assertContains(page, 'Un responsable doit le valider')
        self.assertNotContains(page, reverse('stock:bon_valider', args=['sortie', self.brouillon.pk]))
        for nom in ['bon_valider', 'bon_supprimer']:
            rep = self.client.post(reverse(f'stock:{nom}', args=['sortie', self.brouillon.pk]))
            self.assertRedirects(rep, reverse('stock:accueil'))
        self.brouillon.refresh_from_db()
        self.assertEqual(self.brouillon.statut, 'BROUILLON')

    def test_inventaire_brouillon_valide_depuis_l_ecran(self):
        inv = self.inventaire((self.filtre, Decimal('9'), None), (self.ciment, None, None))
        self.client.force_login(self.user)
        page = self.client.get(reverse('stock:bon_detail', args=['inventaire', inv.pk]))
        self.assertContains(page, 'non compté')
        self.client.post(reverse('stock:bon_valider', args=['inventaire', inv.pk]))
        self.assertEqual(self.stock(self.filtre), 9)
        page = self.client.get(reverse('stock:bon_detail', args=['inventaire', inv.pk]))
        self.assertContains(page, '<b>-1</b>')


# =========================================================
# Saisie des bons sans erreur : ligne non choisie, chantier et engin, préremplissage, création rapide, impression
# =========================================================

class SaisieBonsTests(JustesseBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.magasinier)
        self.entree(self.filtre, 10, 1000)

    def sortir(self, **donnees):
        return self.client.post(reverse('stock:sortie'), {
            'chantier': 'Pont', 'demandeur': 'Ali', 'article': [self.filtre.pk], 'quantite': ['1'], **donnees})

    def test_ligne_tapee_mais_pas_choisie_revient_avec_texte_et_quantite(self):
        rep = self.client.post(reverse('stock:entree'), {
            'article': [self.filtre.pk, ''], 'article_texte': ['Filtre à huile Volvo (FH-01)', 'ciment  colle'],
            'quantite': ['2', '5'], 'prix': ['1000', '3 500'],
        })
        self.assertEqual(rep.status_code, 200)
        self.assertContains(rep, 'Ligne 2 : « ciment colle » n&#x27;est pas choisi')
        lignes = rep.context['lignes_initiales']
        self.assertEqual([l.get('id') for l in lignes], [self.filtre.pk, None])
        self.assertEqual(lignes[1], {'texte': 'ciment colle', 'quantite': '5', 'prix_saisi': '3 500'})
        self.assertContains(rep, 'data-modifie')  # saisie toujours protégée après l'erreur
        self.assertEqual(BonEntree.objects.count(), 1)  # seulement l'entrée du départ

    def test_ligne_sans_article_ni_texte_mais_avec_quantite(self):
        rep = self.sortir(article=[''], article_texte=[''], quantite=['3'])
        self.assertContains(rep, 'Ligne 1 : choisissez un article dans la liste.')
        self.assertEqual(rep.context['lignes_initiales'], [{'texte': '', 'quantite': '3', 'prix_saisi': ''}])
        self.assertFalse(BonSortie.objects.exists())

    def test_chantier_avec_espaces_en_trop_et_casse_differente(self):
        rep = self.sortir(chantier='  pONT  ')
        self.assertEqual(BonSortie.objects.get().chantier, self.chantier)
        self.assertEqual(Chantier.objects.count(), 1)
        self.assertEqual(rep.status_code, 302)

    def test_faute_de_frappe_sur_le_chantier_propose_le_bon_nom(self):
        Chantier.objects.create(nom='Chantier Pont de Bingerville')
        rep = self.sortir(chantier='Chantier Pont de Bingervile')
        self.assertEqual(rep.status_code, 200)
        self.assertContains(rep, 'Nouveau chantier ?')
        self.assertContains(rep, 'Vouliez-vous dire')
        self.assertContains(rep, 'data-valeur="Chantier Pont de Bingerville"')
        self.assertContains(rep, 'name="creer_chantier" value="Chantier Pont de Bingervile"')
        self.assertContains(rep, 'Le chantier « Chantier Pont de Bingervile » n&#x27;existe pas encore')
        self.assertFalse(Chantier.objects.filter(nom='Chantier Pont de Bingervile').exists())
        self.assertFalse(BonSortie.objects.exists())
        # La case cochée pour un autre nom ne vaut pas confirmation.
        self.sortir(chantier='Chantier Pont de Bingervile', creer_chantier='Autre chantier')
        self.assertFalse(BonSortie.objects.exists())
        rep = self.sortir(chantier='Chantier Pont de Bingervile', creer_chantier='Chantier Pont de Bingervile')
        self.assertEqual(rep.status_code, 302)
        self.assertEqual(BonSortie.objects.get().chantier.nom, 'Chantier Pont de Bingervile')

    def test_chantier_cree_mais_stock_refuse_garde_la_case_cochee(self):
        rep = self.sortir(chantier='Ecole', creer_chantier='Ecole', quantite=['50'])
        self.assertContains(rep, 'il reste 10 unités, vous en demandez 50')
        self.assertContains(rep, 'value="Ecole" checked')
        self.assertFalse(Chantier.objects.filter(nom='Ecole').exists())

    def test_chantier_cloture_refuse(self):
        Chantier.objects.create(nom='Ancien chantier', actif=False)
        rep = self.sortir(chantier='ancien chantier', creer_chantier='ancien chantier')
        self.assertContains(rep, 'Le chantier « Ancien chantier » est clôturé')
        self.assertFalse(BonSortie.objects.exists())
        self.assertEqual(Chantier.objects.filter(nom__iexact='ancien chantier').count(), 1)

    def test_engin_inconnu_inactif_et_existant(self):
        from .models import Engin

        Engin.objects.create(code='PEL-320')
        Engin.objects.create(code='CAM-01', actif=False)
        rep = self.sortir(engin='PEL-302')
        self.assertContains(rep, 'Nouvel engin ?')
        self.assertContains(rep, 'data-valeur="PEL-320"')
        self.assertContains(rep, 'name="creer_engin"')
        rep = self.sortir(engin='cam-01')
        self.assertContains(rep, 'L&#x27;engin « CAM-01 » n&#x27;est plus en service')
        self.assertFalse(BonSortie.objects.exists())
        self.sortir(engin=' pel-320 ')
        self.assertEqual(BonSortie.objects.get().engin.code, 'PEL-320')
        self.sortir(engin='PEL-302', creer_engin='PEL-302')
        self.assertTrue(Engin.objects.filter(code='PEL-302').exists())
        self.assertEqual(Engin.objects.count(), 3)

    def test_sortie_ou_entree_d_un_article_depuis_sa_fiche(self):
        page = self.client.get(reverse('stock:article_modifier', args=[self.filtre.pk]))
        self.assertContains(page, f'/sortie/?article={self.filtre.pk}')
        self.assertContains(page, f'/entree/?article={self.filtre.pk}')
        self.assertContains(page, 'data-proteger')
        for nom in ['sortie', 'entree']:
            rep = self.client.get(reverse(f'stock:{nom}'), {'article': self.filtre.pk})
            [ligne] = rep.context['lignes_initiales']
            self.assertEqual((ligne['id'], ligne['code']), (self.filtre.pk, 'FH-01'))
            self.assertContains(rep, 'data-proteger')
        for valeur in ['abc', '999999', '9' * 40]:
            rep = self.client.get(reverse('stock:sortie'), {'article': valeur, 'depuis': valeur})
            self.assertEqual(rep.context['lignes_initiales'], [])
        self.filtre.actif = False
        self.filtre.save()
        self.assertEqual(self.client.get(reverse('stock:sortie'), {'article': self.filtre.pk}).context['lignes_initiales'], [])
        self.assertNotContains(self.client.get(reverse('stock:article_modifier', args=[self.filtre.pk])), '/sortie/?article=')

    def test_refaire_une_sortie(self):
        from .models import Engin

        self.entree(self.ciment, 20, 5000)
        self.sortir(engin='PEL-1', creer_engin='PEL-1', observation='Dalle', article=[self.filtre.pk, self.ciment.pk],
                    quantite=['2', '1,5'])
        bon = BonSortie.objects.get()
        detail = self.client.get(reverse('stock:bon_detail', args=['sortie', bon.pk]))
        self.assertContains(detail, f'/sortie/?depuis={bon.pk}')
        self.assertContains(detail, 'Refaire ce bon')
        rep = self.client.get(reverse('stock:sortie'), {'depuis': bon.pk})
        self.assertEqual({k: rep.context['valeurs'][k] for k in ['chantier', 'engin', 'demandeur', 'observation']},
                         {'chantier': 'Pont', 'engin': 'PEL-1', 'demandeur': 'Ali', 'observation': 'Dalle'})
        self.assertEqual([(l['id'], l['quantite']) for l in rep.context['lignes_initiales']],
                         [(self.filtre.pk, '2'), (self.ciment.pk, '1,5')])
        self.assertContains(rep, f'Copie de {bon.numero}')
        # Chantier clôturé et engin retiré depuis : pas recopiés, on prévient.
        Chantier.objects.update(actif=False)
        Engin.objects.update(actif=False)
        rep = self.client.get(reverse('stock:sortie'), {'depuis': bon.pk})
        self.assertEqual((rep.context['valeurs']['chantier'], rep.context['valeurs']['engin']), ('', ''))
        self.assertContains(rep, 'est clôturé : choisissez le chantier')
        Article.objects.filter(pk=self.ciment.pk).update(actif=False)
        rep = self.client.get(reverse('stock:sortie'), {'depuis': bon.pk})
        self.assertEqual([l['id'] for l in rep.context['lignes_initiales']], [self.filtre.pk])
        self.assertContains(rep, 'Articles désactivés, non recopiés : 1.')

    def test_refaire_une_entree(self):
        self.client.post(reverse('stock:entree'), {'fournisseur': '  Cfao   Motors ', 'reference': 'BL-9',
                                                   'article': [self.filtre.pk], 'quantite': ['4'], 'prix': ['1 250,5']})
        bon = BonEntree.objects.latest('pk')
        self.assertEqual(bon.fournisseur.nom, 'Cfao Motors')
        rep = self.client.get(reverse('stock:entree'), {'depuis': bon.pk})
        self.assertEqual(rep.context['valeurs']['fournisseur'], 'Cfao Motors')
        self.assertNotIn('reference', rep.context['valeurs'])  # le n° de BL change à chaque livraison
        [ligne] = rep.context['lignes_initiales']
        self.assertEqual((ligne['quantite'], ligne['prix_saisi']), ('4', '1\xa0250,5'))

    def test_apres_enregistrement_imprimer_le_bon(self):
        rep = self.sortir()
        bon = BonSortie.objects.get()
        self.assertTrue(rep.url.endswith('?nouveau=1'))
        page = self.client.get(rep.url)
        self.assertContains(page, 'Imprimer le bon')
        self.assertContains(page, 'id="imprimer-bon" autofocus')
        self.assertContains(page, 'Imprimez-le pour le faire signer')
        self.assertContains(page, 'btn btn-vert btn-grand" id="imprimer-bon"')
        page = self.client.get(reverse('stock:bon_detail', args=['sortie', bon.pk]))
        self.assertNotContains(page, 'Imprimer le bon')
        self.assertNotContains(page, 'Imprimez-le pour le faire signer')
        # Bon annulé : plus d'invitation à l'imprimer pour signature.
        services.annuler(bon, self.user, 'Erreur')
        self.assertNotContains(self.client.get(rep.url), 'Imprimez-le pour le faire signer')

    def test_sortie_bloquee_si_stock_manque_cote_ecran(self):
        page = self.client.get(reverse('stock:sortie'))
        self.assertContains(page, 'id="raison-bloque"')
        self.assertContains(page, 'name="jeton"')
        self.assertContains(self.client.get(reverse('stock:accueil')), 'beforeunload')


class CreationRapideArticleTests(Base):
    def setUp(self):
        super().setUp()
        self.magasinier = get_user_model().objects.create_user('awa', password='Magasin-2026!')
        self.client.force_login(self.magasinier)
        self.url = reverse('stock:api_article_creer')

    def creer(self, **donnees):
        import json

        return self.client.post(self.url, json.dumps(donnees), content_type='application/json')

    def test_creation_avec_code_automatique(self):
        rep = self.creer(designation='  Tôle   ondulée 2 m ', unite='FEUILLE', categorie='Matériaux')
        self.assertEqual(rep.status_code, 201)
        article = Article.objects.get(designation='Tôle ondulée 2 m')
        self.assertEqual(rep.json()['article']['id'], article.pk)
        self.assertEqual(article.code, 'MAT-0001')
        self.assertEqual((article.unite, article.categorie.nom, article.actif, article.stock), ('FEUILLE', 'Matériaux', True, 0))
        page = self.client.get(reverse('stock:entree'))
        self.assertContains(page, 'id="creer-article"')
        self.assertContains(page, '<option value="FEUILLE">Feuille</option>', html=True)

    def test_caracteres_de_controle_retires(self):
        rep = self.creer(designation='Filtre\u0007 huile\x1b\tmoteur', unite='U', categorie='Filtres\x00')
        self.assertEqual(rep.status_code, 201)
        article = Article.objects.get(pk=rep.json()['article']['id'])
        self.assertEqual((article.designation, article.categorie.nom), ('Filtre huile moteur', 'Filtres'))
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('stock:articles') + '?format=excel').status_code, 200)

    def test_saisies_refusees(self):
        rep = self.creer(designation='', unite='U', categorie='')
        self.assertEqual(rep.status_code, 400)
        self.assertIn('Désignation : ce champ est obligatoire.', rep.json()['erreurs'])
        self.assertIn('Catégorie : ce champ est obligatoire.', rep.json()['erreurs'])
        rep = self.creer(designation='Truc', unite='XXL', categorie='Divers')
        self.assertEqual(rep.status_code, 400)
        self.assertTrue(rep.json()['erreurs'][0].startswith('Unité'))
        rep = self.creer(designation='filtre à huile VOLVO', unite='U', categorie='Filtres')
        self.assertEqual(rep.status_code, 400)
        self.assertIn('existe déjà (FH-01)', rep.json()['erreurs'][0])
        rep = self.client.post(self.url, 'pas du json', content_type='application/json')
        self.assertEqual(rep.status_code, 400)
        rep = self.client.post(self.url, '[1, 2]', content_type='application/json')
        self.assertEqual(rep.status_code, 400)
        self.assertEqual(Article.objects.count(), 2)

    def test_reserve_aux_comptes_connectes_en_post_avec_csrf(self):
        from django.test import Client

        self.assertEqual(self.client.get(self.url).status_code, 405)
        anonyme = self.client_class()
        self.assertEqual(anonyme.post(self.url, '{}', content_type='application/json').status_code, 302)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.magasinier)
        self.assertEqual(strict.post(self.url, '{"designation": "X", "categorie": "Y"}',
                                     content_type='application/json').status_code, 403)
        self.assertEqual(Article.objects.count(), 2)


# =========================================================
# Retour de chantier
# =========================================================
from django.db.models import Sum  # noqa: E402

from .models import BonRetour, Engin, LigneRetour  # noqa: E402


class RetourChantierBase(JustesseBase):
    def setUp(self):
        super().setUp()
        self.engin = Engin.objects.create(code='PEL-01', designation='Pelle')
        self.route = Chantier.objects.create(nom='Route')
        self.entree(self.ciment, 100, 5000)

    def sortir(self, *lignes, chantier=None, le=None):
        bon = BonSortie.objects.create(chantier=chantier or self.chantier, engin=self.engin, demandeur='Ali',
                                       **({'date': le} if le else {}))
        for article, qte in lignes:
            LigneSortie.objects.create(bon=bon, article=article, quantite=qte)
        return services.valider(bon, self.user)

    def rendre(self, *lignes, chantier=None, valider=True, le=None, sortie=None):
        bon = BonRetour.objects.create(chantier=chantier or self.chantier, engin=self.engin, rapporte_par='Koffi',
                                       sortie=sortie, **({'date': le} if le else {}))
        for article, qte in lignes:
            LigneRetour.objects.create(bon=bon, article=article, quantite=qte)
        return services.valider(bon, self.user) if valider else bon

    def consommation(self, article, chantier=None):
        """Convention de signe : consommation d'un chantier = −Σ quantite (SORTIE, RETOUR, ANNULATION)."""
        types = [MouvementStock.SORTIE, MouvementStock.RETOUR, MouvementStock.ANNULATION]
        total = MouvementStock.objects.filter(chantier=chantier or self.chantier, article=article, type__in=types)
        return -(total.aggregate(s=Sum('quantite'))['s'] or 0)


class RetourChantierStockTests(RetourChantierBase):
    def test_numerotation_br(self):
        premier = self.rendre(valider=False)
        second = self.rendre(valider=False)
        self.assertEqual(premier.numero, f'BR-{premier.date:%Y}-00001')  # suite propre aux retours
        self.assertRegex(second.numero, r'^BR-\d{4}-\d{5}$')
        self.assertEqual(second.numero, f'BR-{second.date:%Y}-{second.pk:05d}')
        self.assertTrue(BonEntree.objects.get().numero.startswith('BE-'))

    def test_retour_partiel_remonte_le_stock_au_cout_de_la_sortie(self):
        self.sortir((self.ciment, 30))
        self.entree(self.ciment, 50, 6000)  # 70 à 5000 + 50 à 6000 → 120 à 5416,67
        self.ciment.refresh_from_db()
        self.assertEqual(self.ciment.prix_moyen, Decimal('5416.67'))
        bon = self.rendre((self.ciment, 10))
        self.ciment.refresh_from_db()
        # Les 10 sacs reviennent au prix où ils sont sortis : (120 × 5416,67 + 10 × 5000) / 130.
        self.assertEqual((self.ciment.stock, self.ciment.prix_moyen), (130, Decimal('5384.62')))
        self.assertEqual(bon.statut, BonRetour.VALIDE)
        self.assertEqual(bon.lignes.get().prix_unitaire, Decimal('5000.00'))  # coût de la sortie
        mvt = MouvementStock.objects.get(type=MouvementStock.RETOUR)
        self.assertEqual((mvt.quantite, mvt.stock_apres, mvt.prix_unitaire), (10, 130, Decimal('5000.00')))
        self.assertEqual(mvt.prix_moyen_apres, Decimal('5384.62'))
        self.assertEqual((mvt.chantier, mvt.engin, mvt.reference), (self.chantier, self.engin, bon.numero))
        self.assertEqual(self.consommation(self.ciment), 20)  # 30 sortis − 10 revenus

    def test_on_ne_rend_pas_plus_que_ce_qui_est_parti(self):
        self.sortir((self.ciment, 30))
        trop = self.rendre((self.ciment, 31), valider=False)
        with self.assertRaisesMessage(services.StockError, 'On ne peut pas rendre plus que ce que le chantier « Pont » a reçu'):
            services.valider(trop, self.user)
        trop.refresh_from_db()
        self.assertEqual(trop.statut, BonRetour.BROUILLON)
        self.assertEqual(self.stock(self.ciment), 70)
        self.assertFalse(MouvementStock.objects.filter(type=MouvementStock.RETOUR).exists())
        self.rendre((self.ciment, 20))
        with self.assertRaisesMessage(services.StockError, 'le chantier en a reçu 10 SAC (retours déjà faits déduits)'):
            self.rendre((self.ciment, 11))
        self.rendre((self.ciment, 4), (self.ciment, 6))  # deux lignes du même article : 10 en tout
        self.assertEqual(self.stock(self.ciment), 100)
        self.assertEqual(self.consommation(self.ciment), 0)

    def test_retour_d_un_chantier_qui_n_a_rien_recu(self):
        self.sortir((self.ciment, 30))
        with self.assertRaisesMessage(services.StockError, "rien n'est parti sur ce chantier"):
            self.rendre((self.ciment, 5), chantier=self.route)
        self.assertEqual(self.stock(self.ciment), 70)

    def test_annulation_d_un_retour(self):
        self.sortir((self.ciment, 30))
        bon = self.rendre((self.ciment, 10))
        services.annuler(bon, self.user, 'Quantité fausse')
        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonRetour.ANNULE)
        self.ciment.refresh_from_db()
        self.assertEqual((self.ciment.stock, self.ciment.prix_moyen), (70, Decimal('5000.00')))
        annulation = MouvementStock.objects.get(type=MouvementStock.ANNULATION)
        self.assertEqual((annulation.quantite, annulation.chantier, annulation.engin), (-10, self.chantier, self.engin))
        self.assertEqual(self.consommation(self.ciment), 30)
        self.rendre((self.ciment, 30))  # tout peut de nouveau revenir

    def test_annulation_d_un_retour_refusee_si_la_marchandise_est_ressortie(self):
        self.entree(self.filtre, 5, 1000)
        self.sortir((self.filtre, 5))
        bon = self.rendre((self.filtre, 2))
        self.sortir((self.filtre, 2), chantier=self.route)
        with self.assertRaisesMessage(services.StockError, 'déjà ressortie du magasin'):
            services.annuler(bon, self.user, 'Erreur')
        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonRetour.VALIDE)
        self.assertEqual(self.stock(self.filtre), 0)

    def test_annulation_d_une_sortie_deja_revenue_en_partie(self):
        sortie = self.sortir((self.ciment, 30))
        bon = self.rendre((self.ciment, 10))
        with self.assertRaisesMessage(services.StockError, f'déjà revenue du chantier ({bon.numero})'):
            services.annuler(sortie, self.user, 'Erreur')
        self.assertEqual(self.stock(self.ciment), 80)
        services.annuler(bon, self.user, 'Erreur')
        services.annuler(sortie, self.user, 'Erreur')
        self.assertEqual(self.stock(self.ciment), 100)
        self.assertEqual(self.consommation(self.ciment), 0)

    def test_annulation_d_une_autre_sortie_du_meme_chantier_reste_possible(self):
        premiere = self.sortir((self.ciment, 30))
        self.sortir((self.ciment, 30))
        self.rendre((self.ciment, 10))
        services.annuler(premiere, self.user, 'Doublon')  # 50 encore sur le chantier ≥ 30
        self.assertEqual(self.stock(self.ciment), 80)
        self.assertEqual(self.consommation(self.ciment), 20)

    def test_inventaire_garde_un_retour_fait_apres_le_comptage(self):
        from django.utils import timezone

        self.sortir((self.ciment, 30))
        inv = self.inventaire((self.ciment, Decimal('70'), timezone.now()))
        self.rendre((self.ciment, 10))
        services.valider(inv, self.user)
        self.assertEqual(self.stock(self.ciment), 80)

    def test_reste_sur_chantier(self):
        self.entree(self.filtre, 5, 1000)
        self.sortir((self.ciment, 30), (self.filtre, 3))
        self.sortir((self.ciment, 5), chantier=self.route)
        self.rendre((self.ciment, 12))
        reste = services.reste_sur_chantier(self.chantier)
        self.assertEqual((reste[self.ciment.pk], reste[self.filtre.pk]), (18, 3))
        self.assertEqual(services.reste_sur_chantier(self.route, [self.ciment.pk])[self.ciment.pk], 5)


class RetourChantierEcransTests(RetourChantierBase):
    def setUp(self):
        super().setUp()
        self.entree(self.filtre, 10, 1000)
        self.sortie_bon = self.sortir((self.ciment, 30), (self.filtre, 4))
        self.client.force_login(self.magasinier)

    def rendre_ecran(self, **donnees):
        return self.client.post(reverse('stock:retour'), {
            'chantier': 'Pont', 'engin': 'PEL-01', 'rapporte_par': 'Koffi',
            'article': [self.ciment.pk], 'quantite': ['5'], **donnees})

    def test_menu_et_bouton_retour_de_ce_bon(self):
        accueil = self.client.get(reverse('stock:accueil'))
        self.assertContains(accueil, f'href="{reverse("stock:retour")}"')
        self.assertContains(accueil, 'Retour de chantier')
        detail = self.client.get(reverse('stock:bon_detail', args=['sortie', self.sortie_bon.pk]))
        self.assertContains(detail, f'{reverse("stock:retour")}?sortie={self.sortie_bon.pk}')
        self.assertContains(detail, 'Retour de ce bon')
        services.annuler(self.sortir((self.ciment, 1)), self.user, 'Erreur')
        annulee = BonSortie.objects.get(statut=BonSortie.ANNULE)
        detail = self.client.get(reverse('stock:bon_detail', args=['sortie', annulee.pk]))
        self.assertNotContains(detail, 'Retour de ce bon')

    def test_retour_prerempli_depuis_la_sortie(self):
        rep = self.client.get(reverse('stock:retour'), {'sortie': self.sortie_bon.pk})
        self.assertEqual(rep.status_code, 200)
        self.assertEqual((rep.context['valeurs']['chantier'], rep.context['valeurs']['engin']), ('Pont', 'PEL-01'))
        lignes = rep.context['lignes_initiales']
        self.assertEqual([l['id'] for l in lignes], [self.ciment.pk, self.filtre.pk])
        self.assertEqual({l['quantite'] for l in lignes}, {''})  # quantités à taper
        self.assertContains(rep, f'name="sortie" value="{self.sortie_bon.pk}"')
        self.assertContains(rep, 'Quantité rapportée')
        self.assertContains(rep, 'Enregistrer le retour')
        self.assertIn('Pont', rep.context['chantiers'])
        self.assertNotIn('Route', rep.context['chantiers'])  # rien n'y est parti

    def test_enregistrement_d_un_retour_partiel(self):
        rep = self.rendre_ecran(sortie=self.sortie_bon.pk, article=[self.ciment.pk, self.filtre.pk],
                                quantite=['12', ''])
        bon = BonRetour.objects.get()
        self.assertRedirects(rep, reverse('stock:bon_detail', args=['retour', bon.pk]) + '?nouveau=1')
        self.assertEqual((bon.sortie, bon.chantier, bon.engin, bon.rapporte_par), (self.sortie_bon, self.chantier, self.engin, 'Koffi'))
        self.assertEqual((bon.cree_par, bon.valide_par), (self.magasinier, self.magasinier))
        self.assertEqual([(l.article, l.quantite) for l in bon.lignes.all()], [(self.ciment, 12)])  # ligne vide ignorée
        self.assertEqual((self.stock(self.ciment), self.stock(self.filtre)), (82, 6))
        detail = self.client.get(rep['Location'])
        self.assertContains(detail, f'Bon de retour {bon.numero}')
        self.assertContains(detail, 'Rapporté par')
        self.assertContains(detail, 'par la personne qui rapporte la marchandise (Koffi)')
        self.assertContains(detail, self.sortie_bon.numero)
        self.assertNotContains(detail, 'Annuler ce bon')  # magasinier
        sortie = self.client.get(reverse('stock:bon_detail', args=['sortie', self.sortie_bon.pk]))
        self.assertContains(sortie, 'Retours de ce bon')
        self.assertContains(sortie, bon.numero)

    def test_erreurs_de_saisie(self):
        rep = self.rendre_ecran(chantier='', rapporte_par=' ', quantite=[''])
        self.assertEqual(rep.context['erreurs'][:3], [
            "Indiquez le chantier d'où revient la marchandise.", 'Indiquez qui rapporte la marchandise.',
            'Indiquez la quantité rapportée pour au moins un article.'])
        rep = self.rendre_ecran(chantier='Pnt')
        self.assertIn('Le chantier « Pnt » n\'existe pas', rep.context['erreurs'][0])
        self.assertEqual(rep.context['nouveau_chantier']['proches'], ['Pont'])
        self.assertNotContains(rep, 'name="creer_chantier"')  # pas de création d'un chantier par un retour
        rep = self.rendre_ecran(quantite=['31'])
        self.assertIn('On ne peut pas rendre plus que ce que le chantier « Pont » a reçu.', rep.context['erreurs'])
        self.assertEqual(rep.context['lignes_initiales'][0]['quantite'], '31')
        rep = self.rendre_ecran(quantite=['abc'])
        self.assertIn('n\'est pas un nombre', rep.context['erreurs'][0])
        self.assertFalse(BonRetour.objects.exists())
        self.assertEqual(Chantier.objects.count(), 2)
        self.assertEqual(self.stock(self.ciment), 70)

    def test_chantier_cloture_peut_rendre_son_reste(self):
        self.chantier.actif = False
        self.chantier.save()
        self.rendre_ecran(chantier='pont ')
        self.assertEqual(BonRetour.objects.get().chantier, self.chantier)
        self.assertEqual(self.stock(self.ciment), 75)

    def test_sortie_d_origine_oubliee_si_le_chantier_change(self):
        self.sortir((self.ciment, 10), chantier=self.route)
        self.rendre_ecran(chantier='Route', sortie=self.sortie_bon.pk)
        self.assertIsNone(BonRetour.objects.get().sortie)

    def test_retour_date_avant_la_sortie_refuse(self):
        from datetime import timedelta

        from django.utils import timezone

        self.entree_le(self.il_y_a(days=3), self.ciment, 10, 5000)  # du stock à cette date
        ancienne = self.sortie_le(self.il_y_a(days=2), self.ciment, 3)
        avant = (timezone.localdate() - timedelta(days=4)).isoformat()
        rep = self.rendre_ecran(sortie=ancienne.pk, date=avant)
        self.assertIn('ne peut pas être daté avant la sortie', rep.context['erreurs'][0])
        self.assertFalse(BonRetour.objects.exists())

    def test_double_envoi(self):
        jeton = self.client.get(reverse('stock:retour')).context['jeton']
        premier = self.rendre_ecran(jeton=jeton)
        second = self.rendre_ecran(jeton=jeton)
        self.assertEqual(BonRetour.objects.count(), 1)
        self.assertEqual(second['Location'], reverse('stock:bon_detail', args=['retour', BonRetour.objects.get().pk]))
        self.assertEqual(premier.status_code, 302)
        self.assertEqual(self.stock(self.ciment), 75)

    def test_reste_sur_le_chantier_pour_l_ecran(self):
        self.rendre((self.ciment, 12))
        donnees = self.client.get(reverse('stock:api_reste_chantier'), {'chantier': ' pont'}).json()
        self.assertEqual(donnees['chantier'], 'Pont')
        self.assertEqual({pk: (Decimal(q), txt) for pk, (q, txt) in donnees['reste'].items()},
                         {str(self.ciment.pk): (18, '18'), str(self.filtre.pk): (4, '4')})
        self.assertEqual(self.client.get(reverse('stock:api_reste_chantier'), {'chantier': 'Inconnu'}).json(),
                         {'chantier': None, 'reste': {}})

    def test_droits_annulation_reservee_aux_responsables(self):
        self.rendre_ecran()
        bon = BonRetour.objects.get()
        url = reverse('stock:bon_annuler', args=['retour', bon.pk])
        self.assertRedirects(self.client.post(url, {'motif': 'Erreur'}), reverse('stock:accueil'))
        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonRetour.VALIDE)
        self.client.force_login(self.user)
        detail = self.client.get(reverse('stock:bon_detail', args=['retour', bon.pk]))
        self.assertContains(detail, 'Annuler ce bon')
        self.client.post(url, {'motif': 'Erreur de quantité'})
        bon.refresh_from_db()
        self.assertEqual((bon.statut, bon.annule_par, bon.motif_annulation), (BonRetour.ANNULE, self.user, 'Erreur de quantité'))
        self.assertEqual(self.stock(self.ciment), 70)

    def test_pdf_du_bon_de_retour(self):
        self.rendre_ecran(sortie=self.sortie_bon.pk)
        bon = BonRetour.objects.get()
        pdf = self.client.get(reverse('stock:bon_pdf', args=['bonretour', bon.pk]))  # magasinier
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        self.assertTrue(pdf.content.startswith(b'%PDF'))
        self.assertIn(bon.numero, pdf['Content-Disposition'])
        from .pdf import GENERATEURS, _bon_retour

        textes = [getattr(e, 'text', '') for e in _bon_retour(bon)]
        self.assertIn(f'BON DE RETOUR DE CHANTIER N° {bon.numero}', textes)
        self.assertIs(GENERATEURS[BonRetour], _bon_retour)
        services.annuler(bon, self.user, 'Erreur')
        self.assertEqual(self.client.get(reverse('stock:bon_pdf', args=['bonretour', bon.pk])).status_code, 200)

    def test_historique_et_recherche_par_numero(self):
        self.rendre_ecran()
        bon = BonRetour.objects.get()
        liste = self.client.get(reverse('stock:bons'))
        self.assertContains(liste, bon.numero)
        self.assertContains(liste, 'badge-retour')
        self.assertContains(liste, reverse('stock:bon_detail', args=['retour', bon.pk]))
        retours = self.client.get(reverse('stock:bons'), {'type': 'retour'})
        self.assertEqual([b for b, _ in retours.context['bons']], [bon])
        trouve = self.client.get(reverse('stock:bons'), {'q': 'BR-'})
        self.assertEqual([(b.numero, t) for b, t in trouve.context['bons']], [(bon.numero, 'retour')])
        trouve = self.client.get(reverse('stock:bons'), {'q': 'koffi', 'type': 'retour'})
        self.assertEqual(len(trouve.context['bons']), 1)


# =========================================================
# Bons saisis après coup : inventaire déjà validé, stock et prix moyen à chaque date, coût des retours
# =========================================================
from datetime import datetime, time, timedelta  # noqa: E402

from django.utils import timezone  # noqa: E402

from . import rapports  # noqa: E402


class AntidateBase(RetourChantierBase):
    def jour(self, decalage):
        return timezone.localdate() + timedelta(days=decalage)

    def historique(self, article):
        return list(MouvementStock.objects.filter(article=article).order_by('date', 'id'))

    def ligne_etat(self, decalage, code):
        donnees = rapports.etat_a_date(self.jour(decalage))
        return next((l for g in donnees['groupes'] for l in g['lignes'] if l['code'] == code), None)

    def verifier_historique(self, article):
        """Stock jamais négatif ; prix moyen après chaque mouvement = calcul repris depuis le début dans l'ordre des
        dates (comme la migration 0007, retours compris) ; prix moyen et stock de l'article = ceux du dernier."""
        historique, prix_moyen, attendus = self.historique(article), Decimal('0'), []
        for m in historique:
            if m.type in services.TYPES_PRIX_MOYEN:
                prix_moyen = services._cmp_mouvement(m.stock_apres - m.quantite, prix_moyen, m.quantite,
                                                     m.prix_unitaire)
            attendus.append(prix_moyen)
        self.assertEqual([m.prix_moyen_apres for m in historique], attendus)
        article.refresh_from_db()
        self.assertEqual((article.stock, article.prix_moyen), (historique[-1].stock_apres, attendus[-1]))
        self.assertTrue(all(m.stock_apres >= 0 for m in historique))


class BonAvantInventaireTests(AntidateBase):
    """Bon daté avant un comptage déjà validé : refusé, sinon l'écart trouvé serait compté deux fois."""

    def setUp(self):
        super().setUp()
        self.entree_le(self.il_y_a(days=5), self.filtre, 100, 1000)
        self.sortir((self.filtre, 20), le=self.il_y_a(days=4))  # 20 partis sur le chantier Pont
        # Une sortie papier de 10, datée d'avant-hier, a été oubliée : le comptage d'hier trouve 70 au lieu de 80.
        self.inv = services.valider(self.inventaire((self.filtre, Decimal('70'), self.il_y_a(days=1))), self.user)
        self.assertEqual(self.stock(self.filtre), 70)

    def test_sortie_oubliee_datee_avant_le_comptage(self):
        bon = BonSortie.objects.create(date=self.il_y_a(days=2), chantier=self.chantier, demandeur='Ali')
        LigneSortie.objects.create(bon=bon, article=self.filtre, quantite=10)
        with self.assertRaises(services.StockError) as erreur:
            services.valider(bon, self.user)
        message = ' '.join(erreur.exception.messages)
        self.assertIn(f"l'inventaire {self.inv.numero} a compté « Filtre à huile Volvo » à ce moment-là", message)
        self.assertIn('datez le bon après ce comptage', message)
        bon.refresh_from_db()
        self.assertEqual(bon.statut, BonSortie.BROUILLON)
        self.assertEqual(self.stock(self.filtre), 70)  # et non 60 : le manque est déjà dans l'écart du comptage
        # Daté après le comptage, le bon passe, et le comptage garde sa quantité dans l'historique.
        bon.date = timezone.now()
        bon.save()
        services.valider(bon, self.user)
        self.assertEqual(self.stock(self.filtre), 60)
        self.assertEqual(MouvementStock.objects.get(type=MouvementStock.INVENTAIRE).stock_apres, 70)
        self.verifier_historique(self.filtre)

    def test_entree_et_retour_dates_avant_le_comptage(self):
        with self.assertRaisesMessage(services.StockError, self.inv.numero):
            self.entree_le(self.il_y_a(days=2), self.filtre, 5, 1000)
        with self.assertRaisesMessage(services.StockError, self.inv.numero):
            self.rendre((self.filtre, 5), le=self.il_y_a(days=2))
        self.assertEqual(self.stock(self.filtre), 70)
        self.rendre((self.filtre, 5))  # daté d'aujourd'hui : après le comptage
        self.assertEqual(self.stock(self.filtre), 75)

    def test_inventaire_pas_encore_valide_ne_gene_pas(self):
        # Même oubli sur le ciment, mais la sortie est saisie avant la validation du comptage : même résultat.
        self.entree_le(self.il_y_a(days=3), self.ciment, 10, 5000)
        comptage = self.inventaire((self.ciment, Decimal('5'), self.il_y_a(days=1)))
        self.sortie_le(self.il_y_a(days=2), self.ciment, 5)
        services.valider(comptage, self.user)
        self.assertEqual(self.stock(self.ciment), 105)  # 5 comptés + les 100 sacs entrés aujourd'hui

    def test_message_a_l_ecran(self):
        self.client.force_login(self.magasinier)
        rep = self.client.post(reverse('stock:sortie'), {
            'chantier': 'Pont', 'demandeur': 'Ali', 'date': self.jour(-2).isoformat(),
            'article': [self.filtre.pk], 'quantite': ['10']})
        self.assertContains(rep, self.inv.numero)
        self.assertContains(rep, 'datez le bon après ce comptage')
        self.assertEqual(BonSortie.objects.count(), 1)  # seulement celle du début
        self.assertEqual(self.stock(self.filtre), 70)


class PrixMoyenHistoriqueTests(AntidateBase):
    """Bon saisi après coup : le prix moyen des mouvements plus récents est recalculé dans l'ordre des dates."""

    def test_entree_oubliee_revalorise_les_mouvements_suivants(self):
        self.entree_le(self.il_y_a(days=10), self.filtre, 10, 100)
        self.sortie_le(self.il_y_a(days=2), self.filtre, 10)
        self.entree_le(self.il_y_a(days=5), self.filtre, 1000, 5000)  # bon de fin de mois saisi le mois suivant
        historique = self.historique(self.filtre)
        self.assertEqual([(m.type, m.stock_apres) for m in historique],
                         [('ENTREE', 10), ('ENTREE', 1010), ('SORTIE', 1000)])
        # (10 × 100 + 1000 × 5000) / 1010 = 4951,49 après l'entrée, et encore après la sortie.
        self.assertEqual([m.prix_moyen_apres for m in historique], [100, Decimal('4951.49'), Decimal('4951.49')])
        self.verifier_historique(self.filtre)
        hier = self.ligne_etat(-1, 'FH-01')
        self.assertEqual((hier['qte'], hier['pm'], hier['valeur'], hier['approx']),
                         (1000, Decimal('4951.49'), Decimal('4951490'), False))  # et non 100 000 FCFA
        self.assertEqual(self.ligne_etat(-6, 'FH-01')['valeur'], 1000)
        fiche = rapports.fiche_stock(self.filtre, au=self.jour(-1))
        self.assertEqual((fiche['stock_fin'], fiche['valeur_fin']), (1000, Decimal('4951490')))

    def test_entree_oubliee_avant_une_entree_plus_recente(self):
        self.entree_le(self.il_y_a(days=10), self.filtre, 10, 100)
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 500)  # prix moyen 300
        self.entree_le(self.il_y_a(days=5), self.filtre, 20, 400)
        # Dans l'ordre des dates : 10 à 100 + 20 à 400 → 300, puis + 10 à 500 → (30 × 300 + 10 × 500) / 40 = 350.
        self.assertEqual([m.prix_moyen_apres for m in self.historique(self.filtre)], [100, 300, 350])
        self.verifier_historique(self.filtre)

    def test_sortie_oubliee_au_prix_moyen_de_son_jour(self):
        self.entree_le(self.il_y_a(days=10), self.filtre, 10, 100)
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 300)  # prix moyen 200
        sortie = self.sortie_le(self.il_y_a(days=5), self.filtre, 5)
        self.assertEqual(sortie.lignes.get().prix_unitaire, 100)  # prix moyen de ce jour-là
        # Hier : 5 à 100 + 10 à 300 → 233,33 ; la valeur du stock vaut bien 1000 + 3000 − 500.
        self.assertEqual([m.prix_moyen_apres for m in self.historique(self.filtre)],
                         [100, 100, Decimal('233.33')])
        self.verifier_historique(self.filtre)

    def test_historique_juste_avec_retours_et_annulations(self):
        self.entree_le(self.il_y_a(days=10), self.filtre, 50, 1000)
        self.sortir((self.filtre, 20), le=self.il_y_a(days=8))
        self.entree_le(self.il_y_a(days=6), self.filtre, 30, 2000)
        self.sortir((self.filtre, 10), le=self.il_y_a(days=3))
        retour = self.rendre((self.filtre, 15), le=self.il_y_a(days=4))
        self.assertEqual(retour.lignes.get().prix_unitaire, 1000)  # coût de la sortie, pas le prix moyen du jour
        self.entree(self.filtre, 10, 0)  # sans prix
        oubliee = self.entree_le(self.il_y_a(days=2), self.filtre, 5, 3000)
        self.verifier_historique(self.filtre)
        services.annuler(oubliee, self.user, 'Doublon')
        self.verifier_historique(self.filtre)

    def test_ancien_historique_sans_prix_moyen(self):
        self.entree_le(self.il_y_a(days=10), self.filtre, 10, 100)
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 300)  # prix moyen 200
        # Mouvements enregistrés avant la mise à jour qui a ajouté le prix moyen après chaque mouvement.
        MouvementStock.objects.filter(article=self.filtre).update(prix_moyen_apres=None)
        self.entree_le(self.il_y_a(days=5), self.filtre, 20, 400)
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.prix_moyen, 300)  # calcul sur le stock actuel : (20 × 200 + 20 × 400) / 40
        self.assertFalse(MouvementStock.objects.filter(article=self.filtre, prix_moyen_apres__isnull=False).exists())
        self.assertTrue(self.ligne_etat(-1, 'FH-01')['approx'])  # signalé, et non faux sans le dire


class SortieAntidateeTests(AntidateBase):
    """Bon daté dans le passé : contrôlé sur le stock de ce jour-là, l'historique ne devient jamais négatif."""

    def test_sortie_datee_avant_l_arrivee_du_stock(self):
        self.entree_le(self.il_y_a(days=1), self.filtre, 10, 5000)
        bon = BonSortie.objects.create(date=self.il_y_a(days=3), chantier=self.chantier, demandeur='Ali')
        LigneSortie.objects.create(bon=bon, article=self.filtre, quantite=6)
        with self.assertRaises(services.StockError) as erreur:
            services.valider(bon, self.user)
        self.assertEqual(erreur.exception.messages, [
            f"Filtre à huile Volvo : le {self.jour(-3):%d/%m/%Y}, il n'en restait plus, vous en demandez 6."])
        self.assertEqual(self.stock(self.filtre), 10)
        self.assertFalse(MouvementStock.objects.filter(type=MouvementStock.SORTIE).exists())
        self.assertEqual(rapports.etat_a_date(self.jour(-2))['total'], 0)  # et non −30 000 FCFA

    def test_sortie_qui_rendrait_negative_une_sortie_plus_recente(self):
        self.entree_le(self.il_y_a(days=5), self.filtre, 10, 1000)
        self.sortie_le(self.il_y_a(days=1), self.filtre, 8)  # il en reste 2
        with self.assertRaises(services.StockError) as erreur:
            self.sortie_le(self.il_y_a(days=3), self.filtre, 5)
        self.assertEqual(erreur.exception.messages, [
            f'Filtre à huile Volvo : le {self.jour(-3):%d/%m/%Y}, vous pouvez en sortir 2 unités au plus '
            '(le reste est sorti ensuite), vous en demandez 5.'])
        self.sortie_le(self.il_y_a(days=3), self.filtre, 2)
        self.assertEqual([m.stock_apres for m in self.historique(self.filtre)], [10, 8, 0])
        self.verifier_historique(self.filtre)

    def test_retour_date_avant_la_sortie_du_chantier(self):
        self.sortir((self.ciment, 30))  # aujourd'hui
        with self.assertRaises(services.StockError) as erreur:
            self.rendre((self.ciment, 10), le=self.il_y_a(days=3))
        self.assertIn("Ciment CPJ 45 sac 50 kg : à cette date, rien n'était parti sur ce chantier (ou tout était "
                      "déjà revenu), vous en rendez 10.", erreur.exception.messages)
        self.assertEqual(self.stock(self.ciment), 70)
        self.rendre((self.ciment, 10))  # daté d'aujourd'hui : accepté
        self.assertEqual(self.stock(self.ciment), 80)

    def test_retour_antidate_ne_rend_pas_impossible_un_retour_suivant(self):
        self.entree_le(self.il_y_a(days=10), self.filtre, 50, 1000)
        self.sortir((self.filtre, 30), le=self.il_y_a(days=3))
        self.rendre((self.filtre, 25), le=self.il_y_a(days=1))  # il en reste 5 sur le chantier
        self.sortir((self.filtre, 20))  # et 25 depuis aujourd'hui
        with self.assertRaises(services.StockError) as erreur:
            self.rendre((self.filtre, 10), le=self.il_y_a(days=2))
        self.assertIn("Filtre à huile Volvo : à cette date, vous pouvez en rendre 5 unités au plus (d'autres "
                      "retours ont été faits ensuite), vous en rendez 10.", erreur.exception.messages)
        self.rendre((self.filtre, 5), le=self.il_y_a(days=2))
        self.assertEqual(services.reste_sur_chantier(self.chantier)[self.filtre.pk], 20)
        self.assertEqual(services.reste_sur_chantier(self.chantier, jusqu_au=self.il_y_a(days=1, hours=-1))
                         [self.filtre.pk], 0)
        self.verifier_historique(self.filtre)

    def test_retour_du_meme_jour_passe_apres_la_sortie(self):
        self.entree_le(self.il_y_a(days=10), self.filtre, 50, 1000)
        sortie = self.sortir((self.filtre, 30), le=self.il_y_a(days=1))
        # Sortie enregistrée hier en fin de journée : un retour daté d'hier passe après elle dans l'historique.
        fin_hier = timezone.make_aware(datetime.combine(self.jour(0), time.min)) - timedelta(seconds=1)
        MouvementStock.objects.filter(reference=sortie.numero).update(date=fin_hier)
        self.rendre((self.filtre, 10), le=self.il_y_a(days=1))
        self.assertEqual([m.type for m in self.historique(self.filtre)], ['ENTREE', 'SORTIE', 'RETOUR'])
        self.assertGreaterEqual(MouvementStock.objects.get(type=MouvementStock.RETOUR).date, fin_hier)
        self.verifier_historique(self.filtre)


class CoutDuRetourTests(AntidateBase):
    """Retour de chantier valorisé au coût de la sortie : un chantier qui rend tout ne coûte rien."""

    def test_retour_complet_apres_un_changement_de_prix(self):
        self.entree(self.filtre, 20, 5000)
        self.sortir((self.filtre, 10))  # 50 000 FCFA sur le chantier Pont
        self.entree(self.filtre, 10, 8000)  # prix moyen : (10 × 5000 + 10 × 8000) / 20 = 6500
        bon = self.rendre((self.filtre, 10))
        self.assertEqual(bon.lignes.get().prix_unitaire, Decimal('5000.00'))
        self.assertEqual(MouvementStock.objects.get(type=MouvementStock.RETOUR).prix_unitaire, Decimal('5000.00'))
        self.filtre.refresh_from_db()
        # Les 10 reviennent à 5000 et entrent dans le prix moyen : (20 × 6500 + 10 × 5000) / 30 = 6000.
        self.assertEqual((self.filtre.stock, self.filtre.prix_moyen), (30, Decimal('6000.00')))
        aujourd_hui = timezone.localdate()
        self.assertEqual(rapports.consommation(aujourd_hui, aujourd_hui), {'groupes': [], 'total': 0, 'nb_lignes': 0})
        self.assertEqual(rapports.total_sorties(aujourd_hui), 0)  # et non −15 000 FCFA
        self.assertEqual(rapports.par_chantier(aujourd_hui), [])
        # Annulation du retour : calcul inverse, comme pour une entrée.
        services.annuler(bon, self.user, 'Erreur')
        self.filtre.refresh_from_db()
        self.assertEqual((self.filtre.stock, self.filtre.prix_moyen), (20, Decimal('6500.00')))
        self.assertEqual(rapports.total_sorties(aujourd_hui), 50000)
        self.verifier_historique(self.filtre)

    def test_prix_de_la_sortie_d_origine_sinon_cout_moyen_du_chantier(self):
        self.entree(self.filtre, 20, 5000)
        self.sortir((self.filtre, 10))  # à 5000
        self.entree(self.filtre, 10, 8000)  # prix moyen 6500
        seconde = self.sortir((self.filtre, 10))  # à 6500
        self.assertEqual(self.rendre((self.filtre, 4), sortie=seconde).lignes.get().prix_unitaire, 6500)
        # Sans sortie d'origine : coût moyen de ce qui reste sur le chantier, (50 000 + 65 000 − 26 000) / 16.
        self.assertEqual(self.rendre((self.filtre, 4)).lignes.get().prix_unitaire, Decimal('5562.50'))
        self.rendre((self.filtre, 12))
        aujourd_hui = timezone.localdate()
        self.assertEqual(rapports.consommation(aujourd_hui, aujourd_hui)['groupes'], [])
        self.assertEqual(rapports.total_sorties(aujourd_hui), 0)
        self.verifier_historique(self.filtre)

    def test_reste_d_arrondi_sans_ligne_dans_la_consommation(self):
        self.entree(self.filtre, 1, 100)
        self.sortir((self.filtre, 1))  # à 100
        self.entree(self.filtre, 2, 101)
        self.sortir((self.filtre, 2))  # à 101
        self.assertEqual(self.rendre((self.filtre, 3)).lignes.get().prix_unitaire, Decimal('100.67'))  # 302 / 3
        aujourd_hui = timezone.localdate()
        self.assertEqual(rapports.consommation(aujourd_hui, aujourd_hui)['groupes'], [])
        self.assertEqual(rapports.par_chantier(aujourd_hui), [])


# =========================================================
# Inventaire dans l'interface simple : périmètre, comptage au téléphone, validation, fiches PDF
# =========================================================

class InventaireEcransBase(JustesseBase):
    def setUp(self):
        super().setUp()
        self.joint = Article.objects.create(code='JT-01', designation='Joint torique', categorie=self.cat,
                                            etagere=self.etagere, niveau=1)
        Article.objects.create(code='OLD-01', designation='Ancien filtre', categorie=self.cat, etagere=self.etagere,
                               niveau=1, actif=False)
        self.bloc_b = Bloc.objects.create(code='B', nom='Pneus')
        self.p1 = Etagere.objects.create(bloc=self.bloc_b, code='P1')
        self.pneu = Article.objects.create(code='PN-01', designation='Pneu 12.00 R20', categorie=self.cat,
                                           etagere=self.p1, niveau=1)
        self.entree(self.filtre, 10, 1000)
        self.entree(self.ciment, 50, 5000)
        self.entree(self.pneu, 4, 100000)

    def creer(self, **donnees):
        self.client.force_login(self.user)
        return self.client.post(reverse('stock:inventaire_nouveau'), {'perimetre': 'tout', **donnees})

    def compter(self, inv, article, texte, en_direct=True):
        ligne = inv.lignes.get(article=article)
        entetes = {'HTTP_X_REQUESTED_WITH': 'fetch'} if en_direct else {}
        return self.client.post(reverse('stock:inventaire_compter', args=[inv.pk]),
                                {f'compte_{ligne.pk}': texte}, **entetes)

    def pdf_lu(self, url, **params):
        """Ouvre un PDF d'inventaire → (réponse, textes des paragraphes et des cellules)."""
        from reportlab.platypus import KeepTogether, Paragraph, Table

        from . import pdf

        elements, document = [], pdf._document_inventaire

        def capter(liste, *args):
            elements.extend(liste)  # reportlab vide la liste en construisant le PDF
            return document(liste, *args)
        with mock.patch('stock.pdf._document_inventaire', side_effect=capter):
            rep = self.client.get(url, params)
        textes = []

        def lire(e):
            if isinstance(e, Paragraph):
                textes.append(e.text)
            elif isinstance(e, Table):
                for rang in e._cellvalues:
                    for cellule in rang:
                        lire(cellule) if isinstance(cellule, (Paragraph, list, tuple)) else textes.append(str(cellule))
            elif isinstance(e, (KeepTogether, list, tuple)):
                # reportlab range les paragraphes des cellules dans des tuples une fois le tableau mis en page
                for x in getattr(e, '_content', e):
                    lire(x)
        for e in elements:
            lire(e)
        return rep, textes


class InventaireCreationTests(InventaireEcransBase):
    def test_menu_et_liste_pour_tous(self):
        self.client.force_login(self.magasinier)
        page = self.client.get(reverse('stock:inventaires'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'href="{reverse("stock:inventaires")}"')
        self.assertContains(page, 'Aucun inventaire en cours')
        self.assertNotContains(page, reverse('stock:inventaire_nouveau'))
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('stock:inventaires')), reverse('stock:inventaire_nouveau'))
        self.assertEqual(self.client.get(reverse('stock:inventaire_nouveau')).status_code, 200)

    def test_tout_le_magasin_trie_par_emplacement(self):
        rep = self.creer(observation='  Fin   d\'année ')
        inv = Inventaire.objects.get()
        self.assertRedirects(rep, reverse('stock:inventaire', args=[inv.pk]))
        self.assertEqual((inv.statut, inv.perimetre, inv.observation, inv.cree_par),
                         ('BROUILLON', 'Tout le magasin', "Fin d'année", self.user))
        lignes = list(inv.lignes.order_by('pk'))
        # Bloc A étagère R1 niveau 1, puis niveau 2, puis bloc B, puis les articles non rangés ; inactif exclu.
        self.assertEqual([l.article.code for l in lignes], ['JT-01', 'FH-01', 'PN-01', 'CIM-45'])
        self.assertTrue(all(l.stock_compte is None and l.compte_le is None for l in lignes))
        self.assertEqual(self.stock(self.filtre), 10)
        page = self.client.get(rep.url)
        self.assertEqual([g['titre'] for g in page.context['groupes']],
                         ['Bloc A › Étagère R1', 'Bloc B › Étagère P1', 'Articles non rangés'])
        self.assertContains(page, 'inputmode="decimal"', count=4)
        self.assertContains(page, '<b id="nb-comptes">0</b>')

    def test_par_bloc_etagere_categorie(self):
        cas = [({'perimetre': 'bloc', 'bloc': self.etagere.bloc.pk}, 'Bloc A – Pièces', ['JT-01', 'FH-01']),
               ({'perimetre': 'etagere', 'etagere': self.p1.pk}, 'Bloc B › Étagère P1', ['PN-01']),
               ({'perimetre': 'categorie', 'categorie': self.ciment.categorie.pk}, 'Catégorie Matériaux', ['CIM-45'])]
        for donnees, perimetre, codes in cas:
            with self.subTest(perimetre):
                self.creer(**donnees)
                inv = Inventaire.objects.latest('pk')
                self.assertEqual(inv.perimetre, perimetre)
                self.assertEqual([l.article.code for l in inv.lignes.order_by('pk')], codes)

    def test_choix_incomplet_ou_vide_refuse(self):
        rep = self.creer(perimetre='bloc')
        self.assertContains(rep, 'Choisissez le bloc à compter.')
        vide = Bloc.objects.create(code='Z')
        rep = self.creer(perimetre='bloc', bloc=vide.pk)
        self.assertContains(rep, 'aucun article actif à compter ici')
        rep = self.creer(perimetre='etagere', etagere='999999')
        self.assertContains(rep, "Cette étagère n&#x27;existe plus")
        self.assertFalse(Inventaire.objects.exists())
        # Une liste remplie mais non choisie ne gêne pas.
        self.creer(perimetre='tout', bloc='999999')
        self.assertEqual(Inventaire.objects.get().perimetre, 'Tout le magasin')

    def test_avertit_si_un_autre_inventaire_compte_les_memes_articles(self):
        self.creer(perimetre='etagere', etagere=self.etagere.pk)
        premier = Inventaire.objects.get()
        rep = self.creer(perimetre='bloc', bloc=self.etagere.bloc.pk)
        self.assertContains(self.client.get(rep.url), f'aussi dans un autre inventaire en cours ({premier.numero})')

    def test_magasinier_ne_cree_ni_ne_valide_ni_ne_supprime(self):
        self.creer()
        inv = Inventaire.objects.get()
        self.client.force_login(self.magasinier)
        for url in [reverse('stock:inventaire_nouveau'), reverse('stock:inventaire_valider', args=[inv.pk])]:
            self.assertRedirects(self.client.get(url), reverse('stock:accueil'))
            self.assertRedirects(self.client.post(url, {'perimetre': 'tout'}), reverse('stock:accueil'))
        self.assertRedirects(self.client.post(reverse('stock:inventaire_supprimer', args=[inv.pk])),
                             reverse('stock:accueil'))
        self.assertEqual(Inventaire.objects.get().statut, 'BROUILLON')
        page = self.client.get(reverse('stock:inventaire', args=[inv.pk]))
        self.assertNotContains(page, reverse('stock:inventaire_valider', args=[inv.pk]))
        self.assertNotContains(page, reverse('stock:inventaire_supprimer', args=[inv.pk]))


class InventaireComptageTests(InventaireEcransBase):
    def setUp(self):
        super().setUp()
        self.creer()
        self.inv = Inventaire.objects.get()
        self.client.force_login(self.magasinier)

    def test_magasinier_compte_une_case_a_la_fois(self):
        rep = self.compter(self.inv, self.filtre, ' 8,5 ')
        self.assertEqual(rep.status_code, 200)
        ligne = self.inv.lignes.get(article=self.filtre)
        donnees = rep.json()
        self.assertEqual(donnees['lignes'], {str(ligne.pk): {'valeur': '8,5', 'heure': donnees['lignes'][str(ligne.pk)]['heure']}})
        self.assertEqual((donnees['comptes'], donnees['total'], donnees['erreurs']), (1, 4, {}))
        self.assertEqual(ligne.stock_compte, Decimal('8.5'))
        self.assertIsNotNone(ligne.compte_le)
        self.assertEqual(self.stock(self.filtre), 10)  # rien ne change avant la validation
        rep = self.compter(self.inv, self.filtre, 'douze')
        self.assertEqual(rep.json()['erreurs'], {str(ligne.pk): '« douze » n\'est pas un nombre.'})
        self.assertEqual(self.inv.lignes.get(article=self.filtre).stock_compte, Decimal('8.5'))
        rep = self.compter(self.inv, self.filtre, '1234567890123')
        self.assertIn('code-barres', rep.json()['erreurs'][str(ligne.pk)])
        rep = self.compter(self.inv, self.filtre, '')  # case vidée : redevient « non compté »
        self.assertEqual((rep.json()['lignes'][str(ligne.pk)]['valeur'], rep.json()['comptes']), ('', 0))
        ligne.refresh_from_db()
        self.assertEqual((ligne.stock_compte, ligne.compte_le), (None, None))

    def test_zero_est_un_comptage(self):
        self.compter(self.inv, self.pneu, '0')
        ligne = self.inv.lignes.get(article=self.pneu)
        self.assertEqual(ligne.stock_compte, 0)
        page = self.client.get(reverse('stock:inventaire', args=[self.inv.pk]))
        self.assertContains(page, f'name="compte_{ligne.pk}" value="0"')
        self.assertContains(page, '<b id="nb-comptes">1</b>')

    def test_enregistrer_une_etagere_sans_ecraser_un_collegue(self):
        filtre, joint = self.inv.lignes.get(article=self.filtre), self.inv.lignes.get(article=self.joint)
        # Un collègue a compté le filtre après l'ouverture de la page : la case était vide ici.
        LigneInventaire.objects.filter(pk=filtre.pk).update(stock_compte=7)
        rep = self.client.post(reverse('stock:inventaire_compter', args=[self.inv.pk]), {
            'groupe': f'etagere-{self.etagere.pk}',
            f'compte_{filtre.pk}': '', f'avant_{filtre.pk}': '',
            f'compte_{joint.pk}': '3', f'avant_{joint.pk}': ''})
        self.assertRedirects(rep, reverse('stock:inventaire', args=[self.inv.pk]) + f'#etagere-{self.etagere.pk}')
        self.assertEqual(self.inv.lignes.get(pk=filtre.pk).stock_compte, 7)
        self.assertEqual(self.inv.lignes.get(pk=joint.pk).stock_compte, 3)
        # Une erreur : la page revient avec la saisie gardée et le message à côté de la case.
        rep = self.client.post(reverse('stock:inventaire_compter', args=[self.inv.pk]), {
            f'compte_{joint.pk}': '-2', f'avant_{joint.pk}': '3'})
        self.assertEqual(rep.status_code, 200)
        self.assertContains(rep, f'name="compte_{joint.pk}" value="-2"')
        self.assertContains(rep, 'Le nombre ne peut pas être négatif.')
        self.assertEqual(self.inv.lignes.get(pk=joint.pk).stock_compte, 3)

    def test_stock_du_logiciel_cache_au_magasinier(self):
        page = self.client.get(reverse('stock:inventaire', args=[self.inv.pk]), {'theorique': '1'})
        self.assertFalse(page.context['theorique'])
        self.assertNotContains(page, 'data-theorique')
        self.assertNotContains(page, 'stock du logiciel</a>')
        self.client.force_login(self.user)
        page = self.client.get(reverse('stock:inventaire', args=[self.inv.pk]))
        self.assertNotContains(page, 'data-theorique')  # comptage à l'aveugle par défaut
        self.assertContains(page, 'Afficher le stock du logiciel')
        page = self.client.get(reverse('stock:inventaire', args=[self.inv.pk]), {'theorique': '1'})
        self.assertContains(page, 'data-theorique="10.000"')

    def test_fiche_de_comptage(self):
        self.compter(self.inv, self.filtre, '9')
        rep, textes = self.pdf_lu(reverse('stock:inventaire_fiche', args=[self.inv.pk]), theorique='1')
        self.assertEqual(rep['Content-Type'], 'application/pdf')
        self.assertTrue(rep.content.startswith(b'%PDF'))
        self.assertIn(f'{self.inv.numero}-fiche-comptage.pdf', rep['Content-Disposition'])
        self.assertIn(f'FICHE DE COMPTAGE N° {self.inv.numero}', textes)
        self.assertNotIn('Théorique', textes)  # magasinier : pas de stock du logiciel, même demandé
        self.assertNotIn('BROUILLON', ' '.join(textes))
        for colonne in ['Niv.', 'Code', 'Désignation', 'Unité', 'Compté', 'Remarque', 'Compté par', 'Contrôlé par', 'Date']:
            self.assertIn(colonne, textes)
        sections = [t for t in textes if 'article' in t and ('Bloc' in t or 'non rangés' in t)]
        self.assertEqual(len(sections), 3)
        self.assertTrue(sections[0].startswith('Bloc A › Étagère R1 – Pièces'))
        self.assertTrue(sections[2].startswith('Articles non rangés'))
        codes = [t for t in textes if t in {'JT-01', 'FH-01', 'PN-01', 'CIM-45'}]
        self.assertEqual(codes, ['JT-01', 'FH-01', 'PN-01', 'CIM-45'])
        self.assertIn('SAC', textes)
        # Responsable : colonne « Théorique » sur demande ; « reste » : seulement les articles pas encore comptés.
        self.client.force_login(self.user)
        _, textes = self.pdf_lu(reverse('stock:inventaire_fiche', args=[self.inv.pk]), theorique='1', reste='1')
        self.assertIn('Théorique', textes)
        self.assertIn('50', textes)
        self.assertNotIn('FH-01', textes)


class InventaireValidationTests(InventaireEcransBase):
    def setUp(self):
        super().setUp()
        self.creer()
        self.inv = Inventaire.objects.get()
        self.client.force_login(self.magasinier)
        self.compter(self.inv, self.filtre, '8')   # 2 manquent
        self.compter(self.inv, self.pneu, '4')     # juste
        self.compter(self.inv, self.joint, '3')    # 3 trouvés sans stock dans le logiciel
        self.client.force_login(self.user)
        self.url = reverse('stock:inventaire_valider', args=[self.inv.pk])

    def test_recapitulatif_des_ecarts(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        ecarts = {e['article'].code: (e['theorique'], e['ecart'], e['valeur']) for e in page.context['ecarts']}
        self.assertEqual(ecarts, {'FH-01': (10, -2, -2000), 'JT-01': (0, 3, 0)})
        self.assertEqual((page.context['justes'], page.context['nb_comptees']), (1, 3))
        self.assertEqual([l.article.code for l in page.context['non_comptees']], ['CIM-45'])
        self.assertEqual((page.context['plus'], page.context['moins'], page.context['valeur_nette']), (0, -2000, -2000))
        self.assertContains(page, '1 article non compté')
        self.assertContains(page, 'Ciment CPJ 45 sac 50 kg')
        self.assertContains(page, 'Valider : mettre le stock à jour')
        self.assertEqual(Inventaire.objects.get().statut, 'BROUILLON')

    def test_validation_corrige_le_stock_et_trace_les_ecarts(self):
        empreinte = self.client.get(self.url).context['empreinte']
        rep = self.client.post(self.url, {'empreinte': empreinte})
        self.assertRedirects(rep, reverse('stock:inventaires') + f'?valide={self.inv.pk}', fetch_redirect_response=False)
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.statut, self.inv.valide_par), ('VALIDE', self.user))
        self.assertEqual([self.stock(a) for a in (self.filtre, self.pneu, self.joint, self.ciment)], [8, 4, 3, 50])
        mouvements = MouvementStock.objects.filter(type=MouvementStock.INVENTAIRE, reference=self.inv.numero)
        self.assertEqual(sorted((m.article.code, m.quantite, m.prix_unitaire) for m in mouvements),
                         [('FH-01', -2, 1000), ('JT-01', 3, 0)])
        self.assertIsNone(self.inv.lignes.get(article=self.ciment).stock_theorique)  # non compté : ignoré
        liste = self.client.get(rep.url)
        self.assertContains(liste, '2 écarts corrigés dans le stock')
        self.assertContains(liste, reverse('stock:inventaire_resultat', args=[self.inv.pk]))
        self.assertEqual(list(liste.context['termines']), [self.inv])
        self.assertEqual((liste.context['termines'][0].nb_ecarts, liste.context['termines'][0].valeur_ecarts), (2, -2000))
        resultat = self.client.get(reverse('stock:inventaire', args=[self.inv.pk]))
        self.assertTemplateUsed(resultat, 'stock/inventaire_resultat.html')
        self.assertEqual([l.article.code for l in resultat.context['lignes']], ['JT-01', 'FH-01'])
        self.assertEqual(resultat.context['valeur'], -2000)

    def test_sortie_faite_apres_le_comptage_reprise(self):
        services.valider(self.sortie(self.filtre, 3), self.user)  # 8 comptés, puis 3 sortis
        empreinte = self.client.get(self.url).context['empreinte']
        self.client.post(self.url, {'empreinte': empreinte})
        self.assertEqual(self.stock(self.filtre), 5)

    def test_chaque_article_a_son_heure_de_comptage(self):
        # Filtre compté avant la sortie ; pneu recompté après la sienne : seule la sortie du filtre est reprise.
        services.valider(self.sortie(self.filtre, 3), self.user)
        services.valider(self.sortie(self.pneu, 1), self.user)
        self.client.force_login(self.magasinier)
        self.compter(self.inv, self.pneu, '3')
        self.client.force_login(self.user)
        page = self.client.get(self.url)
        ecarts = {e['article'].code: (e['theorique'], e['ecart']) for e in page.context['ecarts']}
        self.assertEqual(ecarts, {'FH-01': (10, -2), 'JT-01': (0, 3)})
        self.client.post(self.url, {'empreinte': page.context['empreinte']})
        self.assertEqual([self.stock(a) for a in (self.filtre, self.pneu, self.joint)], [5, 3, 3])
        self.assertFalse(MouvementStock.objects.filter(type=MouvementStock.INVENTAIRE, article=self.pneu).exists())
        self.assertEqual(self.inv.lignes.get(article=self.pneu).stock_theorique, 3)

    def test_comptage_change_pendant_la_lecture(self):
        empreinte = self.client.get(self.url).context['empreinte']
        self.compter(self.inv, self.ciment, '49')
        rep = self.client.post(self.url, {'empreinte': empreinte})
        self.assertEqual(rep.status_code, 200)
        self.assertContains(rep, 'vérifiez le récapitulatif')
        self.assertEqual(Inventaire.objects.get().statut, 'BROUILLON')
        self.assertEqual(self.stock(self.ciment), 50)

    def compter_10_puis_sortir_4(self):
        """10 filtres comptés, puis 4 sortis ; le responsable ouvre le récapitulatif : filtre sans écart."""
        self.client.force_login(self.magasinier)
        self.compter(self.inv, self.filtre, '10')
        services.valider(self.sortie(self.filtre, 4), self.user)
        self.client.force_login(self.user)
        page = self.client.get(self.url)
        self.assertNotIn('FH-01', [e['article'].code for e in page.context['ecarts']])
        self.client.force_login(self.magasinier)
        return page.context['empreinte']

    def test_meme_quantite_renvoyee_garde_l_heure_du_comptage(self):
        empreinte = self.compter_10_puis_sortir_4()
        compte_le = self.inv.lignes.get(article=self.filtre).compte_le
        # Le téléphone renvoie « 10 » (connexion retrouvée) : ce n'est pas un nouveau comptage.
        self.compter(self.inv, self.filtre, '10')
        self.compter(self.inv, self.filtre, '10', en_direct=False)
        self.assertEqual(self.inv.lignes.get(article=self.filtre).compte_le, compte_le)
        self.client.force_login(self.user)
        self.client.post(self.url, {'empreinte': empreinte})
        self.assertEqual(Inventaire.objects.get().statut, 'VALIDE')
        self.assertEqual(self.stock(self.filtre), 6)  # la sortie faite après le comptage n'est pas effacée
        self.assertFalse(MouvementStock.objects.filter(type=MouvementStock.INVENTAIRE, article=self.filtre).exists())

    def test_recompte_pendant_la_lecture_du_responsable(self):
        empreinte = self.compter_10_puis_sortir_4()
        # Recompté après la sortie (9 par erreur, puis 10) : même quantité, mais le stock juste devient 10.
        self.compter(self.inv, self.filtre, '9')
        self.compter(self.inv, self.filtre, '10')
        self.client.force_login(self.user)
        rep = self.client.post(self.url, {'empreinte': empreinte})
        self.assertContains(rep, 'vérifiez le récapitulatif')
        self.assertEqual(Inventaire.objects.get().statut, 'BROUILLON')
        self.assertEqual(self.stock(self.filtre), 6)
        self.assertEqual({e['article'].code: e['ecart'] for e in rep.context['ecarts']}['FH-01'], 4)  # visible

    def test_comptage_impossible_bloque_la_validation(self):
        services.valider(self.sortie(self.filtre, 9), self.user)  # 8 comptés, mais 9 sortis depuis
        page = self.client.get(self.url)
        self.assertContains(page, 'Recomptez cet article')
        self.assertNotContains(page, 'Valider : mettre le stock à jour')
        rep = self.client.post(self.url, {'empreinte': page.context['empreinte']})
        self.assertContains(rep, 'Recomptez cet article')
        self.assertEqual(Inventaire.objects.get().statut, 'BROUILLON')

    def test_inventaire_valide_n_est_plus_modifiable(self):
        self.client.post(self.url, {'empreinte': self.client.get(self.url).context['empreinte']})
        rep = self.compter(self.inv, self.filtre, '1')
        self.assertEqual(rep.status_code, 409)
        self.assertIn('ne peut plus être modifié', rep.json()['erreur'])
        rep = self.compter(self.inv, self.filtre, '1', en_direct=False)
        self.assertRedirects(rep, reverse('stock:inventaire', args=[self.inv.pk]))
        self.assertEqual(self.inv.lignes.get(article=self.filtre).stock_compte, 8)
        self.assertRedirects(self.client.get(self.url), reverse('stock:inventaire', args=[self.inv.pk]))
        rep = self.client.post(reverse('stock:inventaire_supprimer', args=[self.inv.pk]), follow=True)
        self.assertContains(rep, 'seul un inventaire en cours peut être supprimé')
        self.assertTrue(Inventaire.objects.filter(pk=self.inv.pk).exists())
        self.assertEqual(self.stock(self.filtre), 8)

    def test_pdf_du_resultat(self):
        self.assertRedirects(self.client.get(reverse('stock:inventaire_resultat', args=[self.inv.pk])),
                             reverse('stock:inventaire_fiche', args=[self.inv.pk]), fetch_redirect_response=False)
        self.client.post(self.url, {'empreinte': self.client.get(self.url).context['empreinte']})
        self.client.force_login(self.magasinier)
        rep, textes = self.pdf_lu(reverse('stock:inventaire_resultat', args=[self.inv.pk]))
        self.assertTrue(rep.content.startswith(b'%PDF'))
        self.assertIn(f'{self.inv.numero}-resultat.pdf', rep['Content-Disposition'])
        self.assertIn(f"RÉSULTAT D'INVENTAIRE N° {self.inv.numero}", textes)
        self.assertIn('2 articles · -2\xa0000 FCFA', textes)
        self.assertIn('-2', textes)
        self.assertIn('+3', textes)
        self.assertIn('Articles non comptés : stock inchangé (1)', textes)
        # Depuis l'historique des bons, « Imprimer » donne le même résultat ; la fiche du bon mène à l'inventaire.
        detail = self.client.get(reverse('stock:bon_detail', args=['inventaire', self.inv.pk]))
        self.assertContains(detail, reverse('stock:inventaire_resultat', args=[self.inv.pk]))
        self.assertContains(detail, reverse('stock:inventaire', args=[self.inv.pk]))

    def test_pdf_ouvert_directement_dans_la_version_bureau(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as dossier, \
                override_settings(BUREAU=True, DATA_DIR=Path(dossier)), \
                mock.patch('stock.bureau.Path.home', return_value=Path(dossier)), \
                mock.patch('stock.bureau.ouvrir_fichier') as ouvrir:
            page = reverse('stock:inventaire', args=[self.inv.pk])
            rep = self.client.get(reverse('stock:inventaire_fiche', args=[self.inv.pk]), HTTP_REFERER=page)
            self.assertRedirects(rep, page, fetch_redirect_response=False)
            self.assertEqual(ouvrir.call_args[0][0].name, f'{self.inv.numero}-fiche-comptage.pdf')

    def test_suppression_d_un_inventaire_en_cours(self):
        self.assertEqual(self.client.get(reverse('stock:inventaire_supprimer', args=[self.inv.pk])).status_code, 405)
        rep = self.client.post(reverse('stock:inventaire_supprimer', args=[self.inv.pk]), follow=True)
        self.assertRedirects(rep, reverse('stock:inventaires'))
        self.assertContains(rep, 'supprimé')
        self.assertFalse(Inventaire.objects.exists())
        self.assertFalse(LigneInventaire.objects.exists())
        self.assertEqual(self.stock(self.filtre), 10)

    def test_liste_sans_les_corrections_d_un_seul_article(self):
        self.client.post(reverse('stock:article_corriger', args=[self.pneu.pk]), {'stock_compte': '5'})
        liste = self.client.get(reverse('stock:inventaires'))
        self.assertEqual(list(liste.context['en_cours']), [self.inv])
        self.assertEqual((liste.context['en_cours'][0].comptes, liste.context['en_cours'][0].nb), (3, 4))
        self.assertEqual(liste.context['termines'], [])
        self.assertContains(liste, '<b>3</b> / 4 comptés')

    def test_liste_du_plus_recent_au_plus_ancien(self):
        self.client.post(self.url, {'empreinte': self.client.get(self.url).context['empreinte']})
        self.creer(perimetre='etagere', etagere=self.p1.pk)
        second = Inventaire.objects.latest('pk')
        self.compter(second, self.pneu, '4')
        url = reverse('stock:inventaire_valider', args=[second.pk])
        rep = self.client.post(url, {'empreinte': self.client.get(url).context['empreinte']})
        liste = self.client.get(rep.url)
        self.assertEqual(liste.context['termines'], [second, self.inv])
        self.assertContains(liste, f'Inventaire {second.numero} validé :</b>')
        self.assertContains(liste, 'aucun écart, le stock était juste.')

    def test_imprimer_depuis_l_historique_des_bons(self):
        pdf = reverse('stock:bon_pdf', args=['inventaire', self.inv.pk])
        self.assertRedirects(self.client.get(pdf), reverse('stock:inventaire_fiche', args=[self.inv.pk]),
                             fetch_redirect_response=False)
        detail = self.client.get(reverse('stock:bon_detail', args=['inventaire', self.inv.pk]))
        self.assertContains(detail, reverse('stock:inventaire_valider', args=[self.inv.pk]))
        self.assertContains(detail, '<span class="muted">10</span>')
        self.client.force_login(self.magasinier)
        detail = self.client.get(reverse('stock:bon_detail', args=['inventaire', self.inv.pk]))
        self.assertNotContains(detail, '<span class="muted">10</span>')  # pas de stock du logiciel au magasinier
        self.client.force_login(self.user)
        self.client.post(self.url, {'empreinte': self.client.get(self.url).context['empreinte']})
        self.assertRedirects(self.client.get(pdf), reverse('stock:inventaire_resultat', args=[self.inv.pk]),
                             fetch_redirect_response=False)
        # Une correction d'un seul article garde son PDF habituel.
        self.client.post(reverse('stock:article_corriger', args=[self.pneu.pk]), {'stock_compte': '5'})
        correction = Inventaire.objects.exclude(pk=self.inv.pk).get()
        rep = self.client.get(reverse('stock:bon_pdf', args=['inventaire', correction.pk]))
        self.assertEqual(rep['Content-Type'], 'application/pdf')
