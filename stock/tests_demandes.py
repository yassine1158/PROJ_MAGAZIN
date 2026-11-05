"""Demandes de pièces : formulaire du QR code (téléphone, sans compte), suivi, validation et refus au magasin."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import BonSortie, DemandePieces, MouvementStock
from .tests import Base


class DemandeFormulaireTests(Base):
    def envoyer(self, **donnees):
        jeton = self.client.get(reverse('stock:demande')).context['jeton']
        return self.client.post(reverse('stock:demande'), {'jeton': jeton, **donnees})

    def test_formulaire_ouvert_sans_compte(self):
        rep = self.client.get(reverse('stock:demande'))
        self.assertEqual(rep.status_code, 200)
        self.assertContains(rep, 'Demande de pièces')
        self.assertContains(rep, '<option value="Pont">', html=False)

    def test_recherche_de_pieces_sans_prix(self):
        self.entree(self.filtre, 3, 1000)
        rep = self.client.get(reverse('stock:demande_articles'), {'q': 'filtre'}).json()
        piece = rep['articles'][0]
        self.assertEqual((piece['code'], piece['disponible']), ('FH-01', True))
        self.assertNotIn('prix', piece)
        self.assertNotIn('stock', piece)

    def test_envoi_puis_suivi(self):
        rep = self.envoyer(demandeur='  Koffi   Yao ', chantier='pont', engin='CAT-320', telephone='0700',
                           article_1=str(self.filtre.pk), qte_1='2', libre_2='Joint de culasse', qte_2='1',
                           observation='Urgent')
        demande = DemandePieces.objects.get()
        self.assertRedirects(rep, reverse('stock:demande_suivi', args=[demande.jeton]))
        self.assertEqual((demande.demandeur, demande.chantier, demande.engin, demande.engin_texte),
                         ('Koffi Yao', self.chantier, None, 'CAT-320'))
        self.assertRegex(demande.numero, r'^DP-\d{4}-\d{5}$')
        self.assertEqual([(l.article, l.designation, l.quantite) for l in demande.lignes.all()],
                         [(self.filtre, '', 2), (None, 'Joint de culasse', 1)])
        suivi = self.client.get(rep.url)
        self.assertContains(suivi, 'en attente du magasin')
        self.assertContains(suivi, 'Joint de culasse')
        self.assertEqual(self.client.get(rep.url, {'format': 'json'}).json(), {'statut': 'EN_ATTENTE'})

    def test_double_envoi_une_seule_demande(self):
        jeton = self.client.get(reverse('stock:demande')).context['jeton']
        donnees = {'jeton': jeton, 'demandeur': 'Ali', 'article_1': str(self.filtre.pk), 'qte_1': '1'}
        self.client.post(reverse('stock:demande'), donnees)
        rep = self.client.post(reverse('stock:demande'), donnees)
        self.assertEqual(DemandePieces.objects.count(), 1)
        self.assertRedirects(rep, reverse('stock:demande_suivi', args=[jeton]))

    def test_erreurs_gardent_la_saisie(self):
        rep = self.envoyer(chantier='Pont', article_1=str(self.filtre.pk), qte_1='abc')
        self.assertContains(rep, 'Indiquez votre nom.')
        self.assertContains(rep, 'n&#x27;est pas un nombre')
        self.assertFalse(DemandePieces.objects.exists())
        rep = self.envoyer(demandeur='Ali')
        self.assertContains(rep, 'Ajoutez au moins une pièce.')

    def test_suivi_inconnu(self):
        self.assertEqual(self.client.get(reverse('stock:demande_suivi', args=['x' * 32])).status_code, 404)


class DemandeTraitementTests(Base):
    def setUp(self):
        super().setUp()
        self.entree(self.filtre, 5, 1000)
        self.demande = DemandePieces.objects.create(demandeur='Koffi', chantier=self.chantier, jeton='a' * 32)
        self.demande.lignes.create(article=self.filtre, quantite=3)
        self.demande.lignes.create(designation='Joint spécial', quantite=1)
        self.client.force_login(self.user)
        self.url = reverse('stock:demande_detail', args=[self.demande.pk])

    def test_pages_reservees_aux_comptes(self):
        self.client.logout()
        for url in (reverse('stock:demandes'), self.url, reverse('stock:api_demandes')):
            self.assertEqual(self.client.get(url).status_code, 302, url)

    def test_liste_compteur_et_alerte(self):
        rep = self.client.get(reverse('stock:demandes'))
        self.assertContains(rep, 'Koffi')
        self.assertEqual(rep.context['NB_DEMANDES'], 1)
        api = self.client.get(reverse('stock:api_demandes')).json()
        self.assertEqual((api['en_attente'], api['derniere']['numero'], api['derniere']['nb']),
                         (1, self.demande.numero, 2))

    def test_validation_cree_la_sortie_et_l_imprime(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'Joint spécial')
        rep = self.client.post(self.url, {'action': 'valider', 'chantier': 'Pont', 'demandeur': 'Koffi',
                                          f'qte_{self.demande.lignes.get(article=self.filtre).pk}': '2'})
        bon = BonSortie.objects.get()
        self.assertRedirects(rep, reverse('stock:bon_detail', args=['sortie', bon.pk]) + '?nouveau=1&imprimer=1',
                             fetch_redirect_response=False)
        self.demande.refresh_from_db()
        self.assertEqual((self.demande.statut, self.demande.sortie, self.demande.traite_par),
                         (DemandePieces.VALIDEE, bon, self.user))
        self.assertEqual((bon.statut, bon.demandeur, bon.chantier), (BonSortie.VALIDE, 'Koffi', self.chantier))
        self.assertIn(self.demande.numero, bon.observation)
        self.filtre.refresh_from_db()
        self.assertEqual(self.filtre.stock, 3)
        self.assertTrue(MouvementStock.objects.filter(reference=bon.numero, quantite=-2).exists())
        detail = self.client.get(rep.url)
        self.assertContains(detail, "getElementById('imprimer-bon')")
        # Deuxième clic (page restée ouverte) : rien de plus.
        self.client.post(self.url, {'action': 'valider', 'chantier': 'Pont', 'demandeur': 'Koffi',
                                    f'qte_{self.demande.lignes.get(article=self.filtre).pk}': '2'})
        self.assertEqual(BonSortie.objects.count(), 1)
        suivi = self.client.get(reverse('stock:demande_suivi', args=[self.demande.jeton]))
        self.assertContains(suivi, 'Validée')
        self.assertContains(suivi, bon.numero)

    def test_stock_insuffisant(self):
        ligne = self.demande.lignes.get(article=self.filtre)
        rep = self.client.post(self.url, {'action': 'valider', 'chantier': 'Pont', 'demandeur': 'Koffi',
                                          f'qte_{ligne.pk}': '9'})
        self.assertEqual(rep.status_code, 200)
        self.assertFalse(BonSortie.objects.exists())
        self.assertContains(rep, 'value="9"')
        self.demande.refresh_from_db()
        self.assertTrue(self.demande.en_attente)

    def test_chantier_inconnu_demande_confirmation(self):
        ligne = self.demande.lignes.get(article=self.filtre)
        donnees = {'action': 'valider', 'chantier': 'Route Nord', 'demandeur': 'Koffi', f'qte_{ligne.pk}': '1'}
        rep = self.client.post(self.url, donnees)
        self.assertContains(rep, 'Oui, créer ce chantier')
        self.assertFalse(BonSortie.objects.exists())
        self.client.post(self.url, {**donnees, 'creer_chantier': 'Route Nord'})
        self.assertEqual(BonSortie.objects.get().chantier.nom, 'Route Nord')

    def test_refus_avec_motif(self):
        rep = self.client.post(self.url, {'action': 'refuser', 'motif': ''})
        self.demande.refresh_from_db()
        self.assertTrue(self.demande.en_attente)
        self.client.post(self.url, {'action': 'refuser', 'motif': 'Pièce en commande'})
        self.demande.refresh_from_db()
        self.assertEqual((self.demande.statut, self.demande.motif_refus), (DemandePieces.REFUSEE, 'Pièce en commande'))
        self.assertContains(self.client.get(reverse('stock:demande_suivi', args=[self.demande.jeton])),
                            'Pièce en commande')
        self.client.post(reverse('stock:demande_rouvrir', args=[self.demande.pk]))
        self.demande.refresh_from_db()
        self.assertTrue(self.demande.en_attente)
        self.assertEqual(rep.status_code, 302)

    def test_pdf_de_la_demande(self):
        rep = self.client.get(reverse('stock:demande_pdf', args=[self.demande.pk]))
        self.assertEqual(rep.status_code, 200)
        self.assertEqual(rep['Content-Type'], 'application/pdf')

    def test_affiche_qr_code(self):
        with mock.patch('stock.etiquettes.adresse_base', return_value='http://192.168.1.20:8765'):
            rep = self.client.get(reverse('stock:demandes_affiche'))
        self.assertEqual(rep.status_code, 200)
        self.assertEqual(rep['Content-Type'], 'application/pdf')
        with mock.patch('stock.etiquettes.adresse_base', return_value='http://127.0.0.1:8000'):
            rep = self.client.get(reverse('stock:demandes_affiche'))  # un téléphone ne l'ouvrirait pas
        self.assertRedirects(rep, reverse('stock:demandes'), fetch_redirect_response=False)

    def test_affiche_reservee_aux_responsables(self):
        magasinier = get_user_model().objects.create_user('ali', password='motdepasse-solide')
        self.client.force_login(magasinier)
        self.assertRedirects(self.client.get(reverse('stock:demandes_affiche')), reverse('stock:accueil'),
                             fetch_redirect_response=False)
        self.assertEqual(self.client.get(self.url).status_code, 200)  # un magasinier peut servir une demande


class MotDePasseOublieTests(TestCase):
    def setUp(self):
        self.chef = get_user_model().objects.create_superuser('chef', 'c@c.fr', 'ancien-mot-de-passe')
        self.url = reverse('stock:mot_de_passe_oublie')

    def test_sur_le_pc_du_magasin(self):
        with self.settings(BUREAU=True):
            self.assertContains(self.client.get(reverse('stock:connexion')), 'Mot de passe oublié ?')
            rep = self.client.post(self.url, {'compte': self.chef.pk, 'password': 'Nouveau-Secret-2026',
                                              'password2': 'Nouveau-Secret-2026'})
            self.assertRedirects(rep, reverse('stock:connexion'), fetch_redirect_response=False)
        self.assertTrue(self.client.login(username='chef', password='Nouveau-Secret-2026'))

    def test_mots_de_passe_differents(self):
        with self.settings(BUREAU=True):
            rep = self.client.post(self.url, {'compte': self.chef.pk, 'password': 'Nouveau-Secret-2026',
                                              'password2': 'autre'})
        self.assertContains(rep, 'pas identiques')
        self.assertTrue(self.client.login(username='chef', password='ancien-mot-de-passe'))

    def test_refuse_depuis_un_telephone(self):
        with self.settings(BUREAU=True):
            rep = self.client.post(self.url, {'compte': self.chef.pk, 'password': 'Nouveau-Secret-2026',
                                              'password2': 'Nouveau-Secret-2026'}, REMOTE_ADDR='192.168.1.30')
            self.assertIn(rep.status_code, (302, 403))  # accès des téléphones fermé, ou ouvert : refusé
            with mock.patch('stock.reseau.est_actif', return_value=True):
                rep = self.client.post(self.url, {'compte': self.chef.pk, 'password': 'Nouveau-Secret-2026',
                                                  'password2': 'Nouveau-Secret-2026'}, REMOTE_ADDR='192.168.1.30')
                self.assertRedirects(rep, reverse('stock:connexion'), fetch_redirect_response=False)
                self.assertNotContains(self.client.get(reverse('stock:connexion'), REMOTE_ADDR='192.168.1.30'),
                                       'Mot de passe oublié ?')
        self.assertTrue(self.client.login(username='chef', password='ancien-mot-de-passe'))

    def test_refuse_hors_version_bureau(self):
        rep = self.client.post(self.url, {'compte': self.chef.pk, 'password': 'Nouveau-Secret-2026',
                                          'password2': 'Nouveau-Secret-2026'})
        self.assertRedirects(rep, reverse('stock:connexion'), fetch_redirect_response=False)
        self.assertTrue(self.client.login(username='chef', password='ancien-mot-de-passe'))
