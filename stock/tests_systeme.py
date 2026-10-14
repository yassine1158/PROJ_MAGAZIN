import io
import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from config import produit

from . import assistance, licence, mises_a_jour, reseau, sauvegarde, services
from .models import Article, BonEntree, Categorie, LigneEntree, MouvementStock, Parametres


def base_minimale(chemin, marque='actuelle'):
    """Petite base SQLite qui a les tables attendues dans une sauvegarde du logiciel."""
    connexion = sqlite3.connect(str(chemin))
    connexion.execute('CREATE TABLE django_migrations (id INTEGER PRIMARY KEY, app TEXT, name TEXT)')
    connexion.execute('CREATE TABLE stock_article (id INTEGER PRIMARY KEY, code TEXT)')
    connexion.execute('INSERT INTO stock_article (code) VALUES (?)', [marque])
    connexion.commit()
    connexion.close()


def lire_base(chemin, requete='SELECT code FROM stock_article'):
    connexion = sqlite3.connect(str(chemin))
    try:
        return connexion.execute(requete).fetchone()[0]
    finally:
        connexion.close()


def base_de_l_archive(archive, dossier, requete='SELECT code FROM stock_article'):
    with zipfile.ZipFile(archive) as z:
        z.extract('db.sqlite3', dossier)
    return lire_base(Path(dossier) / 'db.sqlite3', requete)


class AvecDossier:
    """Dossier de données temporaire (sauvegardes, photos, fichier d'état)."""

    def setUp(self):
        super().setUp()
        temporaire = tempfile.TemporaryDirectory()
        self.addCleanup(temporaire.cleanup)
        self.dossier = Path(temporaire.name)
        self.enterContext(override_settings(DATA_DIR=self.dossier, MEDIA_ROOT=self.dossier / 'media'))
        cache.clear()

    def autre_dossier(self):
        temporaire = tempfile.TemporaryDirectory()
        self.addCleanup(temporaire.cleanup)
        return Path(temporaire.name)

    def archive_valide(self, marque='sauvegardee', photo='a.jpg'):
        source = self.autre_dossier()
        base_minimale(source / 'db.sqlite3', marque)
        (source / 'media' / 'articles').mkdir(parents=True)
        (source / 'media' / 'articles' / photo).write_bytes(b'photo')
        return sauvegarde.sauvegarder('manuelle', source)


class SauvegardeTests(AvecDossier, SimpleTestCase):
    def setUp(self):
        super().setUp()
        base_minimale(self.dossier / 'db.sqlite3')
        (self.dossier / 'media' / 'articles').mkdir(parents=True)
        (self.dossier / 'media' / 'articles' / 'b.jpg').write_bytes(b'ancienne photo')

    def test_sauvegarde_zip_lisible(self):
        chemin = sauvegarde.sauvegarder('manuelle', self.dossier)
        self.assertRegex(chemin.name, rf'^{produit.NOM}-\d{{4}}-\d{{2}}-\d{{2}}-\d{{6}}-manuelle\.zip$')
        with zipfile.ZipFile(chemin) as z:
            self.assertEqual(set(z.namelist()), {'db.sqlite3', 'media/articles/b.jpg', 'info.json'})
            info = json.loads(z.read('info.json'))
        self.assertEqual((info['raison'], info['version']), ('manuelle', produit.VERSION))
        self.assertEqual(base_de_l_archive(chemin, self.autre_dossier()), 'actuelle')
        self.assertEqual(sauvegarde.verifier_archive(chemin)['raison'], 'manuelle')
        liste = sauvegarde.lister(self.dossier)
        self.assertEqual([(s['nom'], s['libelle']) for s in liste], [(chemin.name, 'Faite à la main')])
        self.assertEqual(list((self.dossier / 'sauvegardes').iterdir()), [chemin])  # aucun fichier temporaire

    def test_rotation(self):
        dossier = self.dossier / 'sauvegardes'
        dossier.mkdir()
        maintenant = datetime(2026, 10, 8, 12, 0)

        def creer(quand):
            (dossier / f'{produit.NOM}-{quand:%Y-%m-%d-%H%M%S}-auto.zip').write_bytes(b'')

        for jours in range(15):  # du 8 octobre au 24 septembre
            creer(maintenant - timedelta(days=jours))
        for mois in range(1, 15):  # le 15 de chacun des 14 mois précédents
            annee, numero = divmod(2026 * 12 + 9 - mois, 12)
            creer(datetime(annee, numero + 1, 15, 9, 0))
        (dossier / 'autre.zip').write_bytes(b'')

        sauvegarde.rotation(dossier, maintenant)
        dates = [s['date'] for s in sauvegarde.lister(self.dossier)]
        self.assertEqual(len(dates), 20)
        self.assertEqual(dates[:10], [maintenant - timedelta(days=j) for j in range(10)])
        self.assertEqual(dates[10:], [datetime(2026 - (m > 9), (9 - m) % 12 + 1, 15, 9, 0) for m in range(2, 12)])
        self.assertTrue((dossier / 'autre.zip').exists())

    def test_rotation_apres_sauvegarde(self):
        dossier = self.dossier / 'sauvegardes'
        dossier.mkdir()
        hier = datetime.now() - timedelta(days=1)
        for minutes in range(12):
            (dossier / f'{produit.NOM}-{hier - timedelta(minutes=minutes):%Y-%m-%d-%H%M%S}-auto.zip').write_bytes(b'')
        chemin = sauvegarde.sauvegarder('manuelle', self.dossier)
        noms = [s['nom'] for s in sauvegarde.lister(self.dossier)]
        self.assertEqual(len(noms), 10)
        self.assertEqual(noms[0], chemin.name)

    def test_sauvegarde_auto_une_fois_par_jour(self):
        premiere = sauvegarde.sauvegarde_auto(self.dossier)
        self.assertTrue(premiere.name.endswith('-auto.zip'))
        self.assertIsNone(sauvegarde.sauvegarde_auto(self.dossier))
        avant_hier = datetime.now() - timedelta(days=2)
        premiere.rename(premiere.with_name(f'{produit.NOM}-{avant_hier:%Y-%m-%d-%H%M%S}-auto.zip'))
        self.assertIsNotNone(sauvegarde.sauvegarde_auto(self.dossier))
        self.assertEqual(len(sauvegarde.lister(self.dossier)), 2)

    def test_sans_base_message_clair(self):
        (self.dossier / 'db.sqlite3').unlink()
        with self.assertRaisesMessage(sauvegarde.ErreurSauvegarde, 'Aucune base'):
            sauvegarde.sauvegarder('manuelle', self.dossier)

    def test_archive_invalide(self):
        temporaire = self.autre_dossier()

        def archive(nom, fichiers):
            chemin = temporaire / nom
            with zipfile.ZipFile(chemin, 'w') as z:
                for nom_fichier, contenu in fichiers.items():
                    z.writestr(nom_fichier, contenu)
            return chemin

        sans_migrations = temporaire / 'autre.sqlite3'
        connexion = sqlite3.connect(str(sans_migrations))
        connexion.execute('CREATE TABLE t (x)')
        connexion.commit()
        connexion.close()
        bonne_base = (self.dossier / 'db.sqlite3').read_bytes()
        pas_un_zip = temporaire / 'photo.zip'
        pas_un_zip.write_bytes(b'ceci n est pas un zip')
        cas = [
            (pas_un_zip, 'zip valide'),
            (archive('vide.zip', {'info.json': '{}'}), 'ne contient pas de base'),
            (archive('abimee.zip', {'db.sqlite3': b'nimporte quoi' * 200}), 'illisible'),
            (archive('etrangere.zip', {'db.sqlite3': sans_migrations.read_bytes()}), 'ne contient pas les données'),
            (archive('chemins.zip', {'db.sqlite3': bonne_base, '../../evil.txt': 'x'}), 'chemins interdits'),
            (archive('future.zip', {'db.sqlite3': bonne_base, 'info.json': '{"version": "99.0.0"}'}), 'plus récente'),
        ]
        for chemin, message in cas:
            with self.subTest(chemin.name), self.assertRaisesMessage(ValueError, message):
                sauvegarde.verifier_archive(chemin)
            with self.assertRaises(ValueError):
                sauvegarde.preparer_restauration(chemin, self.dossier)
            self.assertFalse(sauvegarde.restauration_en_attente(self.dossier))
        self.assertEqual(sorted(p.name for p in self.dossier.iterdir()), ['db.sqlite3', 'media'])

    def test_restauration_au_demarrage(self):
        sauvegarde.preparer_restauration(self.archive_valide(), self.dossier)
        self.assertTrue(sauvegarde.restauration_en_attente(self.dossier))
        resultat = sauvegarde.appliquer_restauration_en_attente(self.dossier)
        self.assertTrue(resultat['ok'], resultat)
        self.assertEqual(lire_base(self.dossier / 'db.sqlite3'), 'sauvegardee')
        self.assertTrue((self.dossier / 'media' / 'articles' / 'a.jpg').exists())
        self.assertFalse((self.dossier / 'media' / 'articles' / 'b.jpg').exists())
        self.assertFalse(sauvegarde.restauration_en_attente(self.dossier))
        self.assertFalse((self.dossier / sauvegarde.ANCIEN).exists())
        self.assertFalse((self.dossier / sauvegarde.EXTRAIT).exists())
        avant = [s for s in sauvegarde.lister(self.dossier) if s['raison'] == 'avant-restauration']
        self.assertEqual(len(avant), 1)
        self.assertEqual(base_de_l_archive(avant[0]['chemin'], self.autre_dossier()), 'actuelle')
        with zipfile.ZipFile(avant[0]['chemin']) as z:
            self.assertIn('media/articles/b.jpg', z.namelist())
        self.assertTrue(sauvegarde.lire_etat(self.dossier)['restauration']['ok'])
        self.assertIsNone(sauvegarde.appliquer_restauration_en_attente(self.dossier))

    def test_restauration_echouee_sans_perte(self):
        sauvegarde.preparer_restauration(self.archive_valide(), self.dossier)
        vrai_replace = os.replace

        def replace(source, cible):
            if sauvegarde.EXTRAIT in str(source) and Path(cible).name == 'db.sqlite3':
                raise PermissionError('fichier utilisé par un autre programme')
            return vrai_replace(source, cible)

        with mock.patch('stock.sauvegarde.os.replace', side_effect=replace), \
                self.assertLogs('stock.sauvegarde', 'ERROR'):
            resultat = sauvegarde.appliquer_restauration_en_attente(self.dossier)
        self.assertFalse(resultat['ok'])
        self.assertIn("n'ont pas été modifiées", resultat['message'])
        self.assertEqual(lire_base(self.dossier / 'db.sqlite3'), 'actuelle')
        self.assertTrue((self.dossier / 'media' / 'articles' / 'b.jpg').exists())
        self.assertFalse((self.dossier / sauvegarde.ANCIEN).exists())
        self.assertTrue(sauvegarde.restauration_en_attente(self.dossier))  # retentée au prochain démarrage

    def test_reprise_apres_coupure_de_courant(self):
        sauvegarde.preparer_restauration(self.archive_valide(), self.dossier)
        ancien = self.dossier / sauvegarde.ANCIEN
        ancien.mkdir()
        os.replace(self.dossier / 'db.sqlite3', ancien / 'db.sqlite3')  # coupure en plein remplacement
        os.replace(self.dossier / 'media', ancien / 'media')
        resultat = sauvegarde.appliquer_restauration_en_attente(self.dossier)
        self.assertTrue(resultat['ok'], resultat)
        self.assertEqual(lire_base(self.dossier / 'db.sqlite3'), 'sauvegardee')
        avant = next(s for s in sauvegarde.lister(self.dossier) if s['raison'] == 'avant-restauration')
        self.assertEqual(base_de_l_archive(avant['chemin'], self.autre_dossier()), 'actuelle')

    def test_restauration_refusee_si_archive_abimee(self):
        (self.dossier / sauvegarde.EN_ATTENTE).write_bytes(b'abime')
        with self.assertLogs('stock.sauvegarde', 'ERROR'):
            resultat = sauvegarde.appliquer_restauration_en_attente(self.dossier)
        self.assertFalse(resultat['ok'])
        self.assertEqual(lire_base(self.dossier / 'db.sqlite3'), 'actuelle')
        self.assertFalse(sauvegarde.restauration_en_attente(self.dossier))
        self.assertTrue((self.dossier / sauvegarde.REFUSEE).exists())

    def test_copie_sur_cle_usb(self):
        chemin = sauvegarde.sauvegarder('manuelle', self.dossier)
        cle = self.autre_dossier()
        with mock.patch.object(sauvegarde, 'cles_usb', return_value=[]), \
                self.assertRaisesMessage(ValueError, 'Aucune clé USB'):
            sauvegarde.copier_sur_cle(chemin.name, dossier=self.dossier)
        with mock.patch.object(sauvegarde, 'cles_usb',
                               return_value=[{'racine': str(cle), 'nom': 'CLE (E:)', 'libre': 10}]), \
                self.assertRaisesMessage(ValueError, 'pleine'):
            sauvegarde.copier_sur_cle(chemin.name, dossier=self.dossier)
        with mock.patch.object(sauvegarde, 'cles_usb',
                               return_value=[{'racine': str(cle), 'nom': 'CLE (E:)', 'libre': 10 ** 9}]):
            copie, _ = sauvegarde.copier_sur_cle(chemin.name, dossier=self.dossier)
            with self.assertRaisesMessage(ValueError, "n'existe plus"):
                sauvegarde.copier_sur_cle('../db.sqlite3', dossier=self.dossier)
        self.assertEqual(copie, cle / produit.NOM / chemin.name)
        self.assertEqual(copie.read_bytes(), chemin.read_bytes())
        self.assertEqual(sauvegarde.lire_etat(self.dossier)['copie_usb']['nom'], chemin.name)

    def test_cles_usb_sans_erreur(self):
        cles = sauvegarde.cles_usb()
        self.assertIsInstance(cles, list)
        if sys.platform != 'win32':
            self.assertEqual(cles, [])


class SauvegardeEcransTests(AvecDossier, TestCase):
    def setUp(self):
        super().setUp()
        Utilisateur = get_user_model()
        self.responsable = Utilisateur.objects.create_user('chef', password='motdepasse-solide', is_staff=True)
        self.magasinier = Utilisateur.objects.create_user('ali', password='motdepasse-solide')
        self.client.force_login(self.responsable)
        base_minimale(self.dossier / 'db.sqlite3')  # pour créer des sauvegardes sans toucher à la base des tests
        self.archive = sauvegarde.sauvegarder('manuelle', self.dossier)

    def test_page(self):
        rep = self.client.get(reverse('stock:reglages_sauvegarde'))
        self.assertContains(rep, 'Sauvegarder maintenant')
        self.assertContains(rep, reverse('stock:sauvegarde_telecharger', args=[self.archive.name]))
        self.assertContains(rep, 'Repartir de zéro')
        self.assertNotContains(rep, 'Copier sur la clé USB')  # seulement dans la version Windows
        self.assertContains(self.client.get('/'), reverse('stock:reglages_sauvegarde'))

    def test_telecharger(self):
        rep = self.client.get(reverse('stock:sauvegarde_telecharger', args=[self.archive.name]))
        self.assertEqual(rep['Content-Type'], 'application/zip')
        self.assertTrue(b''.join(rep.streaming_content).startswith(b'PK'))
        rep.close()
        rep = self.client.get(reverse('stock:sauvegarde_telecharger', args=['secret.key']))
        self.assertRedirects(rep, reverse('stock:reglages_sauvegarde'))

    def test_telecharger_version_bureau(self):
        with override_settings(BUREAU=True), mock.patch('stock.bureau.Path.home', return_value=self.dossier), \
                mock.patch('stock.bureau.ouvrir_fichier') as ouvrir:
            rep = self.client.get(reverse('stock:sauvegarde_telecharger', args=[self.archive.name]),
                                  HTTP_REFERER='/reglages/sauvegarde/')
        self.assertRedirects(rep, '/reglages/sauvegarde/', fetch_redirect_response=False)
        self.assertEqual(ouvrir.call_args[0][0].read_bytes(), self.archive.read_bytes())

    def test_copier_sur_cle_usb(self):
        cle = self.autre_dossier()
        url = reverse('stock:sauvegarde_usb', args=[self.archive.name])
        with override_settings(BUREAU=True):
            with mock.patch.object(sauvegarde, 'cles_usb', return_value=[]):
                self.assertContains(self.client.get(reverse('stock:reglages_sauvegarde')), 'aucune clé branchée')
                rep = self.client.post(url, follow=True)
            self.assertContains(rep, 'Aucune clé USB trouvée')
            with mock.patch.object(sauvegarde, 'cles_usb',
                                   return_value=[{'racine': str(cle), 'nom': 'KINGSTON (E:)', 'libre': 10 ** 9}]):
                rep = self.client.post(url, follow=True)
            self.assertContains(rep, 'Sauvegarde copiée sur la clé KINGSTON (E:)')
            self.assertContains(rep, 'Dernière copie sur clé USB : <b>le')
        self.assertTrue((cle / produit.NOM / self.archive.name).exists())

    def test_ouvrir_le_dossier(self):
        url = reverse('stock:sauvegarde_dossier')
        with mock.patch('stock.views_systeme.ouvrir_fichier') as ouvrir:
            self.assertContains(self.client.post(url, follow=True), 'seulement dans le logiciel installé')
            ouvrir.assert_not_called()
            with override_settings(BUREAU=True):
                self.client.post(url)
            ouvrir.assert_called_once_with(self.dossier / 'sauvegardes')

    def test_restaurer(self):
        url = reverse('stock:sauvegarde_restaurer')
        contenu = self.archive_valide().read_bytes()
        rep = self.client.post(url, {'fichier': SimpleUploadedFile('s.zip', contenu)}, follow=True)
        self.assertContains(rep, 'tapez RESTAURER')
        rep = self.client.post(url, {'fichier': SimpleUploadedFile('s.zip', b'abime'), 'confirmation': 'RESTAURER'},
                               follow=True)
        self.assertContains(rep, 'zip valide')
        self.assertFalse(sauvegarde.restauration_en_attente())
        rep = self.client.post(url, {'fichier': SimpleUploadedFile('s.zip', contenu), 'confirmation': ' restaurer '},
                               follow=True)
        self.assertContains(rep, 'Redémarrez le serveur')
        self.assertContains(rep, 'Une restauration est prête')
        self.assertEqual((self.dossier / sauvegarde.EN_ATTENTE).read_bytes(), contenu)
        rep = self.client.post(reverse('stock:sauvegarde_annuler_restauration'), follow=True)
        self.assertContains(rep, 'Restauration annulée')
        self.assertFalse(sauvegarde.restauration_en_attente())
        with override_settings(BUREAU=True):
            rep = self.client.post(url, {'nom': self.archive.name, 'confirmation': 'RESTAURER'}, follow=True)
        self.assertContains(rep, 'Fermez le logiciel puis rouvrez-le')
        self.assertEqual((self.dossier / sauvegarde.EN_ATTENTE).read_bytes(), self.archive.read_bytes())

    def test_resultat_de_restauration_montre_une_fois(self):
        sauvegarde._ecrire_etat(restauration={'ok': True, 'message': 'Sauvegarde du 01/10/2026 à 10:00 restaurée.'})
        url = reverse('stock:reglages_sauvegarde')
        self.assertContains(self.client.get(url), 'Sauvegarde du 01/10/2026 à 10:00 restaurée.')
        self.assertNotContains(self.client.get(url), 'Sauvegarde du 01/10/2026')

    def test_repartir_a_zero_demande_confirmation(self):
        Categorie.objects.create(nom='Filtres')
        rep = self.client.post(reverse('stock:repartir_a_zero'), {'confirmation': 'oui'}, follow=True)
        self.assertContains(rep, "Rien n&#x27;a été effacé")
        self.assertTrue(Categorie.objects.exists())

    def test_magasinier_refuse(self):
        call_command('demo', stdout=io.StringIO())
        nb_articles = Article.objects.count()
        self.client.force_login(self.magasinier)
        accueil = reverse('stock:accueil')
        for methode, url, donnees in [
            ('get', reverse('stock:reglages_sauvegarde'), {}),
            ('post', reverse('stock:sauvegarde_nouvelle'), {}),
            ('get', reverse('stock:sauvegarde_telecharger', args=[self.archive.name]), {}),
            ('post', reverse('stock:sauvegarde_usb', args=[self.archive.name]), {}),
            ('post', reverse('stock:sauvegarde_dossier'), {}),
            ('post', reverse('stock:sauvegarde_restaurer'), {'nom': self.archive.name, 'confirmation': 'RESTAURER'}),
            ('post', reverse('stock:repartir_a_zero'), {'confirmation': 'EFFACER'}),
            ('get', reverse('stock:exemple_effacer'), {}),
            ('post', reverse('stock:exemple_effacer'), {}),
        ]:
            with self.subTest(url):
                self.assertRedirects(getattr(self.client, methode)(url, donnees), accueil)
        self.assertEqual(Article.objects.count(), nb_articles)
        self.assertFalse(sauvegarde.restauration_en_attente())
        self.assertEqual(len(sauvegarde.lister()), 1)
        self.assertNotContains(self.client.get(accueil), reverse('stock:reglages_sauvegarde'))

    def test_bandeau_exemple(self):
        accueil = reverse('stock:accueil')
        self.assertNotContains(self.client.get(accueil), 'bandeau-exemple')
        self.assertRedirects(self.client.get(reverse('stock:exemple_effacer')), accueil)
        call_command('demo', stdout=io.StringIO())
        rep = self.client.get(accueil)
        self.assertContains(rep, "Magasin d'exemple : ces articles sont fictifs.")
        self.assertContains(rep, reverse('stock:exemple_effacer'))
        rep = self.client.get(reverse('stock:exemple_effacer'))
        self.assertContains(rep, f'<b>{Article.objects.count()}</b> articles')
        self.assertContains(rep, "Effacer l'exemple et commencer")
        self.client.force_login(self.magasinier)
        rep = self.client.get(accueil)
        self.assertContains(rep, 'Prévenez votre responsable.')
        self.assertNotContains(rep, reverse('stock:exemple_effacer'))


class ViderDonneesTests(AvecDossier, TransactionTestCase):
    """Avec une vraie base (pas de transaction ouverte) : la sauvegarde lit la base des tests."""

    def setUp(self):
        super().setUp()
        Utilisateur = get_user_model()
        self.responsable = Utilisateur.objects.create_superuser('chef', 'c@c.fr', 'motdepasse-solide')
        self.magasinier = Utilisateur.objects.create_user('ali', password='motdepasse-solide')

    def installer_exemple(self):
        p = Parametres.actuels()
        p.nom_societe, p.cle_licence, p.debut_essai = 'BTP Abidjan', 'CLE-DE-TEST', date(2026, 9, 1)
        p.save()
        (self.dossier / '.essai').write_text('2026-09-01')
        call_command('demo', stdout=io.StringIO())
        (self.dossier / 'media' / 'articles').mkdir(parents=True)
        (self.dossier / 'media' / 'articles' / 'filtre.jpg').write_bytes(b'photo')
        Article.objects.filter(code='FH-VOL-01').update(photo='articles/filtre.jpg')

    def test_vider_donnees(self):
        self.installer_exemple()
        nb_articles = Article.objects.count()
        self.assertTrue(sauvegarde.exemple_present())
        chemin = sauvegarde.vider_donnees()

        for modele in apps.get_app_config('stock').get_models():
            if modele is not Parametres:
                self.assertFalse(modele.objects.exists(), modele.__name__)
        cache.clear()
        p = Parametres.actuels()
        self.assertEqual((p.nom_societe, p.cle_licence, p.debut_essai),
                         ('BTP Abidjan', 'CLE-DE-TEST', date(2026, 9, 1)))
        self.assertEqual(get_user_model().objects.count(), 2)
        self.assertTrue((self.dossier / '.essai').exists())
        self.assertFalse((self.dossier / 'media' / 'articles' / 'filtre.jpg').exists())
        self.assertFalse(sauvegarde.exemple_present())
        self.assertTrue(BonEntree.objects.create().numero.endswith('-00001'))

        self.assertTrue(chemin.name.endswith('-avant-vidage.zip'))
        self.assertEqual(base_de_l_archive(chemin, self.autre_dossier(), 'SELECT count(*) FROM stock_article'),
                         nb_articles)
        with zipfile.ZipFile(chemin) as z:
            self.assertIn('media/articles/filtre.jpg', z.namelist())

    def test_effacer_exemple_depuis_le_bandeau(self):
        self.installer_exemple()
        self.client.force_login(self.responsable)
        rep = self.client.post(reverse('stock:exemple_effacer'), follow=True)
        self.assertRedirects(rep, reverse('stock:accueil'))
        self.assertContains(rep, 'Données effacées')
        self.assertNotContains(rep, 'bandeau-exemple')
        self.assertFalse(Article.objects.exists())
        self.assertEqual([s['raison'] for s in sauvegarde.lister()], ['avant-vidage'])

    def test_repartir_de_zero(self):
        self.installer_exemple()
        self.client.force_login(self.responsable)
        self.client.post(reverse('stock:repartir_a_zero'), {'confirmation': 'EFFACER'})
        self.assertFalse(Article.objects.exists())
        self.assertEqual(Parametres.actuels().nom_societe, 'BTP Abidjan')

    def test_rien_n_est_efface_si_la_sauvegarde_echoue(self):
        self.installer_exemple()
        self.client.force_login(self.responsable)
        with mock.patch.object(sauvegarde, 'sauvegarder', side_effect=sauvegarde.ErreurSauvegarde('Disque plein.')):
            rep = self.client.post(reverse('stock:repartir_a_zero'), {'confirmation': 'EFFACER'}, follow=True)
        self.assertContains(rep, 'Disque plein.')
        self.assertTrue(Article.objects.exists())

    def test_sauvegarder_maintenant(self):
        Categorie.objects.create(nom='Filtres')
        self.client.force_login(self.responsable)
        rep = self.client.post(reverse('stock:sauvegarde_nouvelle'), follow=True)
        self.assertContains(rep, 'Sauvegarde faite')
        (chemin,) = [s['chemin'] for s in sauvegarde.lister()]
        self.assertEqual(base_de_l_archive(chemin, self.autre_dossier(), 'SELECT nom FROM stock_categorie'), 'Filtres')
        self.assertIsNone(sauvegarde.sauvegarde_auto())


# =========================================================
# Accès depuis les téléphones
# =========================================================

TELEPHONE = {'REMOTE_ADDR': '192.168.1.30', 'HTTP_HOST': '192.168.1.20:8765'}


def application_essai(environ, start_response):
    start_response('200 OK', [('Content-Type', 'text/plain')])
    return [b'bonjour ' + environ['HTTP_HOST'].encode()]


class ReseauTests(AvecDossier, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(reseau.arreter)

    def test_reglage_par_defaut_et_enregistrement(self):
        self.assertEqual(reseau.lire_reglage(), {'actif': False, 'port': 8765})
        reseau.ecrire_reglage(actif=True)
        reseau.ecrire_reglage(port=9000)
        self.assertEqual(json.loads((self.dossier / 'reseau.json').read_text()), {'actif': True, 'port': 9000})
        (self.dossier / 'reseau.json').write_text('{abîmé')
        self.assertEqual(reseau.lire_reglage(), {'actif': False, 'port': 8765})
        (self.dossier / 'reseau.json').write_text('{"actif": "oui", "port": 80}')
        self.assertEqual(reseau.lire_reglage(), {'actif': False, 'port': 8765})

    def test_port_valide(self):
        for port in ['8765', 1024, '65535', ' 9000 ']:
            self.assertTrue(reseau.port_valide(port), port)
        for port in ['80', '65536', '', 'abc', '8765a', '-1', None, '٨٧٦٥']:
            self.assertFalse(reseau.port_valide(port), port)

    def udp(self, ip):
        prise = mock.MagicMock()
        prise.__enter__.return_value = prise
        if ip:
            prise.getsockname.return_value = (ip, 50000)
        else:
            prise.connect.side_effect = OSError('réseau injoignable')
        return mock.patch('stock.reseau.socket.socket', return_value=prise)

    def test_ip_locale(self):
        with self.udp('192.168.1.20'), mock.patch('stock.reseau.socket.gethostbyname_ex',
                                                  return_value=('pc', [], ['127.0.1.1', '10.0.0.7'])):
            self.assertEqual(reseau.ip_locale(), '192.168.1.20')
            self.assertEqual(reseau.adresse(), 'http://192.168.1.20:8765/')
            self.assertEqual(reseau.autres_adresses(), ['http://10.0.0.7:8765/'])
        # sans internet : adresse des cartes réseau, sans la boucle locale ni l'adresse automatique de secours
        with self.udp(None), mock.patch('stock.reseau.socket.gethostbyname_ex',
                                        return_value=('pc', [], ['127.0.1.1', '169.254.3.4', '172.20.1.5'])):
            self.assertEqual(reseau.ip_locale(), '172.20.1.5')
        with self.udp('127.0.0.1'), mock.patch('stock.reseau.socket.gethostbyname_ex', side_effect=OSError):
            self.assertIsNone(reseau.ip_locale())
            self.assertIsNone(reseau.adresse())

    def test_adresse_publique(self):
        from django.test import RequestFactory

        requete = RequestFactory().get('/')
        self.assertEqual(reseau.adresse_publique(requete), 'http://testserver/')
        with mock.patch('stock.reseau.est_actif', return_value=True), \
                mock.patch('stock.reseau.ip_locale', return_value='192.168.1.20'):
            self.assertEqual(reseau.adresse_publique(requete), 'http://192.168.1.20:8765/')

    def test_hotes_autorises(self):
        for hote in ['127.0.0.1:51234', 'localhost', '[::1]:8000', '192.168.1.20:8765', '10.1.2.3', '172.16.0.9:8765',
                     '172.31.255.1', '169.254.10.20:8765', 'testserver']:
            self.assertTrue(reseau.hote_autorise(hote), hote)
        for hote in ['', 'exemple.com', 'exemple.com:8765', '192.168.1.20.nip.io:8765', '8.8.8.8', '172.32.0.1',
                     '0.0.0.0:8765', '41.202.10.5:8765']:
            self.assertFalse(reseau.hote_autorise(hote), hote)

    def test_qr_code_svg(self):
        svg = reseau.qr_svg('http://192.168.1.20:8765/')
        self.assertIn('<svg', svg)
        self.assertGreater(svg.count('<rect'), 50)
        self.assertNotEqual(svg, reseau.qr_svg('http://192.168.1.21:8765/'))
        uri = reseau.qr_data_uri('http://192.168.1.20:8765/')
        self.assertTrue(uri.startswith('data:image/svg+xml;base64,'))
        import base64
        self.assertEqual(base64.b64decode(uri.split(',', 1)[1]).decode(), svg)

    def test_serveur_reseau(self):
        import socket
        import urllib.request

        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            libre = s.getsockname()[1]
        if libre > 65000:
            libre = 50123
        reseau.ecrire_reglage(port=libre)
        occupe = socket.socket()
        self.addCleanup(occupe.close)
        try:
            occupe.bind(('0.0.0.0', libre))
            occupe.listen()
        except OSError:
            self.skipTest('port de test indisponible')
        port = reseau.demarrer(application_essai)
        self.assertGreater(port, libre)  # port occupé : le suivant est pris
        self.assertLessEqual(port, libre + 5)
        self.assertTrue(reseau.est_actif())
        self.assertEqual(reseau.demarrer(application_essai), port)  # déjà ouvert : rien ne change
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=10) as rep:
            self.assertEqual(rep.read(), f'bonjour 127.0.0.1:{port}'.encode())
        reseau.arreter()
        self.assertFalse(reseau.est_actif())
        with self.assertRaises(OSError):
            urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=3)

    def test_tous_les_ports_occupes(self):
        with mock.patch('stock.reseau._ouvrir_port', side_effect=OSError('occupé')):
            with self.assertRaisesRegex(reseau.ErreurReseau, 'déjà utilisés'):
                reseau.demarrer(application_essai)
        self.assertFalse(reseau.est_actif())
        self.assertIn('déjà utilisés', reseau.derniere_erreur())

    def test_demarrer_si_actif(self):
        with mock.patch('stock.reseau.demarrer') as demarrer:
            self.assertIsNone(reseau.demarrer_si_actif(application_essai))
            demarrer.assert_not_called()
            reseau.ecrire_reglage(actif=True)
            reseau.demarrer_si_actif(application_essai)
            demarrer.assert_called_once_with(application_essai)
            demarrer.side_effect = RuntimeError('panne')
            with self.assertLogs('stock', 'ERROR'):
                self.assertIsNone(reseau.demarrer_si_actif(application_essai))  # ne fait jamais planter le logiciel


@override_settings(BUREAU=True, ALLOWED_HOSTS=['*', 'testserver'])
class ReseauMiddlewareTests(AvecDossier, TestCase):
    def setUp(self):
        super().setUp()
        self.responsable = get_user_model().objects.create_user('chef', password='motdepasse-solide', is_staff=True)

    def test_ordinateur_et_hotes_prives_acceptes(self):
        for hote in ['127.0.0.1:51234', 'localhost:51234', '192.168.1.20:8765', '10.0.0.5:8765']:
            self.assertEqual(self.client.get('/connexion/', HTTP_HOST=hote).status_code, 200, hote)

    def test_hote_public_refuse(self):
        for hote in ['exemple.com', 'pirate.exemple.com:8765', '8.8.8.8:8765']:
            rep = self.client.get('/connexion/', HTTP_HOST=hote)
            self.assertEqual(rep.status_code, 400, hote)
            self.assertContains(rep, 'Adresse non reconnue', status_code=400)

    def test_telephone_refuse_si_acces_desactive(self):
        rep = self.client.get('/connexion/', **TELEPHONE)
        self.assertContains(rep, 'Accès des téléphones désactivé', status_code=403)
        with mock.patch('stock.reseau.est_actif', return_value=True):
            self.assertEqual(self.client.get('/connexion/', **TELEPHONE).status_code, 200)
            rep = self.client.get('/connexion/', REMOTE_ADDR='41.202.10.5', HTTP_HOST='192.168.1.20:8765')
            self.assertContains(rep, 'Accès refusé', status_code=403)  # appareil hors du réseau du magasin
            self.assertEqual(self.client.get('/bienvenue/', **TELEPHONE).status_code, 403)

    def test_version_web_non_filtree(self):
        with override_settings(BUREAU=False):
            self.assertEqual(self.client.get('/connexion/', **TELEPHONE).status_code, 200)

    def test_connexion_et_formulaire_depuis_un_telephone(self):
        from django.test import Client

        telephone = Client(enforce_csrf_checks=True, **TELEPHONE)
        origine = {'HTTP_ORIGIN': 'http://192.168.1.20:8765'}
        with mock.patch('stock.reseau.est_actif', return_value=True):
            telephone.get('/connexion/')
            jeton = telephone.cookies['csrftoken'].value
            rep = telephone.post('/connexion/', {'username': 'chef', 'password': 'motdepasse-solide',
                                                 'csrfmiddlewaretoken': jeton}, **origine)
            self.assertRedirects(rep, reverse('stock:accueil'), fetch_redirect_response=False)
            self.assertEqual(telephone.get('/').status_code, 200)
            jeton = telephone.cookies['csrftoken'].value
            url = reverse('stock:reglages_telephones')
            # formulaire envoyé depuis un autre site : refusé par la protection CSRF
            rep = telephone.post(url, {'action': 'desactiver', 'csrfmiddlewaretoken': jeton},
                                 HTTP_ORIGIN='http://pirate.exemple.com')
            self.assertEqual(rep.status_code, 403)
            rep = telephone.post(url, {'action': 'desactiver'}, **origine)
            self.assertEqual(rep.status_code, 403)
            reseau.ecrire_reglage(actif=True)
            with mock.patch('stock.reseau.arreter') as arreter:
                rep = telephone.post(url, {'action': 'desactiver', 'csrfmiddlewaretoken': jeton}, follow=True, **origine)
            self.assertContains(rep, 'seulement sur l&#x27;ordinateur')  # le jeton passe, mais pas depuis un téléphone
            arreter.assert_not_called()
            self.assertTrue(reseau.lire_reglage()['actif'])

    def test_photos_reservees_aux_comptes(self):
        from django.contrib.auth.models import AnonymousUser
        from django.http import Http404
        from django.test import RequestFactory

        (self.dossier / 'media' / 'articles').mkdir(parents=True)
        (self.dossier / 'media' / 'articles' / 'filtre.jpg').write_bytes(b'photo')
        (self.dossier / 'media' / 'societe').mkdir()
        (self.dossier / 'media' / 'societe' / 'logo.png').write_bytes(b'logo')

        def demande(chemin, utilisateur, **meta):
            requete = RequestFactory().get('/media/' + chemin, **meta)
            requete.user = utilisateur
            reponse = reseau.media(requete, chemin)
            reponse.close()  # sinon Windows garde le fichier ouvert et refuse de vider le dossier temporaire
            return reponse

        self.assertEqual(demande('articles/filtre.jpg', AnonymousUser()).status_code, 200)  # sur l'ordinateur
        self.assertEqual(demande('articles/filtre.jpg', self.responsable, **TELEPHONE).status_code, 200)
        self.assertEqual(demande('societe/logo.png', AnonymousUser(), **TELEPHONE).status_code, 200)
        with self.assertRaises(Http404):
            demande('articles/filtre.jpg', AnonymousUser(), **TELEPHONE)

    def test_dossier_pas_ouvert_depuis_un_telephone(self):
        self.client.force_login(self.responsable)
        with mock.patch('stock.reseau.est_actif', return_value=True), \
                mock.patch('stock.views_systeme.ouvrir_fichier') as ouvrir:
            rep = self.client.post(reverse('stock:sauvegarde_dossier'), follow=True, **TELEPHONE)
        self.assertContains(rep, 'seulement sur l&#x27;ordinateur')
        self.assertNotContains(rep, 'Ouvrir le dossier')  # bouton réservé à l'écran de l'ordinateur
        ouvrir.assert_not_called()

    def test_fichiers_rendus_au_telephone(self):
        from .bureau import livrer

        from django.http import HttpResponse
        from django.test import RequestFactory

        reponse = HttpResponse(b'%PDF', content_type='application/pdf')
        reponse['Content-Disposition'] = 'attachment; filename="bon.pdf"'
        requete = RequestFactory().get('/', **TELEPHONE)
        with mock.patch('stock.bureau.ouvrir_fichier') as ouvrir:
            self.assertIs(livrer(requete, reponse), reponse)  # le PDF va au téléphone, rien ne s'ouvre sur le PC
        ouvrir.assert_not_called()


@override_settings(BUREAU=True, ALLOWED_HOSTS=['*', 'testserver'])
class AccesTelephonesEcranTests(AvecDossier, TestCase):
    def setUp(self):
        super().setUp()
        Utilisateur = get_user_model()
        self.responsable = Utilisateur.objects.create_user('chef', password='motdepasse-solide', is_staff=True)
        self.magasinier = Utilisateur.objects.create_user('ali', password='motdepasse-solide')
        self.client.force_login(self.responsable)
        self.url = reverse('stock:reglages_telephones')
        self.enterContext(mock.patch('stock.reseau.ip_locale', return_value='192.168.1.20'))
        self.addCleanup(reseau.arreter)

    def test_reserve_aux_responsables(self):
        self.client.force_login(self.magasinier)
        with mock.patch('stock.reseau.demarrer') as demarrer:
            self.assertRedirects(self.client.get(self.url), reverse('stock:accueil'))
            self.assertRedirects(self.client.post(self.url, {'action': 'activer'}), reverse('stock:accueil'))
        demarrer.assert_not_called()
        self.assertFalse((self.dossier / 'reseau.json').exists())
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_page_desactivee(self):
        rep = self.client.get(self.url)
        self.assertContains(rep, 'Désactivé')
        self.assertContains(rep, "Activer l'accès des téléphones")
        self.assertContains(rep, 'Réseaux privés')
        self.assertContains(rep, 'réseau du magasin')
        self.assertNotContains(rep, 'data:image/svg+xml')
        self.assertContains(self.client.get('/'), self.url)  # lien dans le menu Réglages

    def test_activer_puis_desactiver(self):
        with mock.patch('stock.reseau.demarrer', return_value=8765) as demarrer:
            rep = self.client.post(self.url, {'action': 'activer'}, follow=True)
        demarrer.assert_called_once()
        self.assertContains(rep, 'Accès des téléphones activé')
        self.assertEqual(json.loads((self.dossier / 'reseau.json').read_text()), {'actif': True, 'port': 8765})
        with mock.patch('stock.reseau.est_actif', return_value=True):
            rep = self.client.get(self.url)
        self.assertContains(rep, 'http://192.168.1.20:8765/')
        self.assertContains(rep, 'adresse-geante')
        self.assertContains(rep, 'data:image/svg+xml;base64,')
        self.assertContains(rep, "Désactiver l'accès des téléphones")
        with mock.patch('stock.reseau.arreter') as arreter:
            rep = self.client.post(self.url, {'action': 'desactiver'}, follow=True)
        arreter.assert_called_once()
        self.assertContains(rep, 'Accès des téléphones désactivé')
        self.assertFalse(reseau.lire_reglage()['actif'])

    def test_activation_en_vrai(self):
        import socket

        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            reseau.ecrire_reglage(port=s.getsockname()[1])
        with mock.patch('django.core.wsgi.get_wsgi_application', return_value=application_essai):
            self.client.post(self.url, {'action': 'activer'})
        self.assertTrue(reseau.est_actif())  # effet immédiat, dans le même programme
        self.client.post(self.url, {'action': 'desactiver'})
        self.assertFalse(reseau.est_actif())

    def test_echec_de_l_activation(self):
        with mock.patch('stock.reseau.demarrer', side_effect=reseau.ErreurReseau('Le port 8765 est déjà utilisé.')):
            rep = self.client.post(self.url, {'action': 'activer'}, follow=True)
        self.assertContains(rep, 'n&#x27;a pas pu être activé. Le port 8765 est déjà utilisé.')
        self.assertFalse(reseau.lire_reglage()['actif'])
        with mock.patch('stock.reseau.demarrer', side_effect=RuntimeError('panne')), self.assertLogs('stock', 'ERROR'):
            rep = self.client.post(self.url, {'action': 'activer'}, follow=True)
        self.assertContains(rep, 'n&#x27;a pas pu être activé.')
        self.assertFalse(reseau.lire_reglage()['actif'])

    def test_changer_le_port(self):
        rep = self.client.post(self.url, {'action': 'port', 'port': '80'}, follow=True)
        self.assertContains(rep, 'entre 1024 et 65535')
        self.assertEqual(reseau.lire_reglage()['port'], 8765)
        with mock.patch('stock.reseau.demarrer') as demarrer:
            rep = self.client.post(self.url, {'action': 'port', 'port': '9000'}, follow=True)
        self.assertContains(rep, 'Port enregistré : 9000')
        demarrer.assert_not_called()  # accès désactivé : rien ne s'ouvre
        reseau.ecrire_reglage(actif=True)
        with mock.patch('stock.reseau.demarrer', return_value=9001) as demarrer:
            rep = self.client.post(self.url, {'action': 'port', 'port': '9001'}, follow=True)
        demarrer.assert_called_once()  # accès actif : rouvert tout de suite sur le nouveau port
        self.assertEqual(reseau.lire_reglage(), {'actif': True, 'port': 9001})

    def test_modifiable_seulement_depuis_l_ordinateur(self):
        with mock.patch('stock.reseau.est_actif', return_value=True), mock.patch('stock.reseau.demarrer') as demarrer:
            rep = self.client.get(self.url, **TELEPHONE)
            self.assertContains(rep, "Ce réglage se change seulement sur l'ordinateur")
            self.assertNotContains(rep, 'name="action"')
            rep = self.client.post(self.url, {'action': 'activer'}, follow=True, **TELEPHONE)
        self.assertContains(rep, 'Rien n&#x27;a été changé')
        demarrer.assert_not_called()
        self.assertFalse((self.dossier / 'reseau.json').exists())

    def test_version_web(self):
        with override_settings(BUREAU=False), mock.patch('stock.reseau.demarrer') as demarrer:
            rep = self.client.get(self.url)
            self.assertContains(rep, 'se règle sur ce serveur')
            self.assertContains(rep, 'data:image/svg+xml;base64,')
            self.assertNotContains(rep, 'name="action"')
            rep = self.client.post(self.url, {'action': 'activer'}, follow=True)
        self.assertContains(rep, 'Rien n&#x27;a été changé')
        demarrer.assert_not_called()

    def test_reglage_impossible_a_enregistrer(self):
        with mock.patch('stock.reseau.ecrire_reglage', side_effect=OSError('disque plein')), \
                self.assertLogs('stock', 'ERROR'):
            rep = self.client.post(self.url, {'action': 'activer'}, follow=True)
        self.assertContains(rep, 'n&#x27;a pas pu être enregistré')


# =========================================================
# Mises à jour et instance unique (version Windows)
# =========================================================

URL_INSTALLATEUR = 'https://github.com/yassine1158/PROJ_MAGAZIN/releases/download/v99.0.0/MagaStock-Installation.exe'


def version_json(version='99.0.0', **champs):
    info = {'version': version, 'date': '2026-10-09', 'taille_mo': 38.1, 'sha256': 'ab' * 32,
            'url': URL_INSTALLATEUR, 'notes': 'Import Excel des articles.\nCorrections.'}
    info.update(champs)
    return info


def reponse(contenu):
    """Réponse de urlopen (objet utilisable avec « with »)."""
    if not isinstance(contenu, bytes):
        contenu = json.dumps(contenu, ensure_ascii=False).encode('utf-8-sig')
    return io.BytesIO(contenu)


class MisesAJourTests(AvecDossier, SimpleTestCase):
    def ecrire(self, **etat):
        (self.dossier / mises_a_jour.FICHIER).write_text(json.dumps(etat), encoding='utf-8')

    def internet(self, *reponses):
        return mock.patch('urllib.request.urlopen', side_effect=list(reponses))

    def test_comparaison_des_versions(self):
        plus_recente = mises_a_jour.plus_recente
        self.assertTrue(plus_recente('1.10.0', '1.9.0'))
        self.assertFalse(plus_recente('1.9.0', '1.10.0'))
        self.assertFalse(plus_recente('1.4.0', '1.4.0'))
        self.assertFalse(plus_recente('1.4', '1.4.0'))
        self.assertTrue(plus_recente('2.0', '1.99.99'))
        self.assertTrue(plus_recente('v1.5.0', '1.4.0'))
        self.assertTrue(plus_recente('1.4.0.1', '1.4.0'))
        for invalide in ['', 'abc', '1.5.0-beta', '1..0', '²', '1.2.3.4.5', None, 15]:
            self.assertFalse(plus_recente(invalide, '1.0.0'), invalide)
            self.assertIsNone(mises_a_jour.numero(invalide))
        self.assertEqual(mises_a_jour.numero(' 1.10 '), (1, 10, 0, 0))
        self.assertEqual(mises_a_jour.numero(produit.VERSION)[:3], tuple(map(int, produit.VERSION.split('.'))))

    def test_derniere_depuis_le_fichier(self):
        self.assertIsNone(mises_a_jour.derniere())  # aucun fichier
        self.ecrire(**version_json())
        self.assertEqual(mises_a_jour.derniere()['version'], '99.0.0')
        self.assertEqual(mises_a_jour.derniere()['url'], URL_INSTALLATEUR)
        self.ecrire(**version_json(produit.VERSION))
        self.assertIsNone(mises_a_jour.derniere())  # déjà installée
        self.ecrire(**version_json('0.1.0'))
        self.assertIsNone(mises_a_jour.derniere())
        for url in ['javascript:alert(1)', 'http://exemple.com/a.exe', '', None, 'https://exemple.com/a b.exe']:
            self.ecrire(**version_json(url=url))
            self.assertIsNone(mises_a_jour.derniere(), url)
        (self.dossier / mises_a_jour.FICHIER).write_text('{pas du json', encoding='utf-8')
        self.assertIsNone(mises_a_jour.derniere())
        (self.dossier / mises_a_jour.FICHIER).write_text('[1, 2]', encoding='utf-8')
        self.assertIsNone(mises_a_jour.derniere())

    def test_verifier_lit_version_json(self):
        with self.internet(reponse(version_json())) as urlopen:
            maj = mises_a_jour.verifier()
        self.assertEqual(maj['version'], '99.0.0')
        self.assertEqual(maj['notes'], 'Import Excel des articles.\nCorrections.')
        requete = urlopen.call_args[0][0]
        self.assertEqual(requete.full_url, produit.MAJ_URL)
        self.assertEqual(urlopen.call_args[1]['timeout'], 10)
        etat = mises_a_jour.lire_etat()
        self.assertEqual(etat['version'], '99.0.0')
        self.assertTrue(etat['verifie_le'])

    def test_une_verification_par_jour(self):
        with self.internet(reponse(version_json())):
            mises_a_jour.verifier()
        with self.internet() as urlopen:
            self.assertEqual(mises_a_jour.verifier()['version'], '99.0.0')  # résultat gardé, sans internet
        urlopen.assert_not_called()
        for il_y_a in [timedelta(hours=25), timedelta(hours=-2)]:  # vieux de plus de 24 h, ou horloge reculée
            self.ecrire(**version_json(), verifie_le=(datetime.now() - il_y_a).isoformat())
            with self.internet(reponse(version_json('99.1.0'))) as urlopen:
                self.assertEqual(mises_a_jour.verifier()['version'], '99.1.0')
            urlopen.assert_called_once()
        with self.internet(reponse(version_json(produit.VERSION))) as urlopen:
            self.assertIsNone(mises_a_jour.verifier(force=True))  # « Vérifier les mises à jour »
        urlopen.assert_called_once()

    def test_hors_ligne_sans_erreur(self):
        self.ecrire(**version_json(), verifie_le=(datetime.now() - timedelta(days=3)).isoformat())
        with self.internet(urllib.error.URLError('pas de réseau')):
            maj = mises_a_jour.verifier()
        self.assertEqual(maj['version'], '99.0.0')  # le résultat précédent reste affiché
        self.assertIn('internet', mises_a_jour.lire_etat()['erreur'])
        with self.internet() as urlopen:
            mises_a_jour.verifier()
        urlopen.assert_not_called()  # pas de nouvel essai à chaque ouverture
        with self.internet(TimeoutError()):
            self.assertEqual(mises_a_jour.verifier(force=True)['version'], '99.0.0')

    def test_version_json_absent_ou_invalide(self):
        absent = urllib.error.HTTPError(produit.MAJ_URL, 404, 'Not Found', {}, None)
        for contenu in [absent, urllib.error.HTTPError(produit.MAJ_URL, 500, 'Erreur', {}, None),
                        reponse(b'{pas du json'), reponse(b'\xff\xfe\x00'), reponse([1, 2]), reponse({}),
                        reponse(version_json('abc')), reponse(version_json(url='ftp://exemple.com/a.exe')),
                        reponse(b' ' * (mises_a_jour.TAILLE_MAX + 1)), ValueError('adresse'), OSError('disque')]:
            (self.dossier / mises_a_jour.FICHIER).unlink(missing_ok=True)
            with self.internet(contenu):
                self.assertIsNone(mises_a_jour.verifier(), contenu)
            with self.internet(contenu), self.assertRaises(mises_a_jour.ErreurMiseAJour):
                mises_a_jour.verifier_maintenant()
        with self.internet(absent), self.assertRaisesRegex(mises_a_jour.ErreurMiseAJour, 'Aucune information'):
            mises_a_jour.verifier_maintenant()

    def test_dossier_en_lecture_seule(self):
        with self.internet(reponse(version_json())), mock.patch('pathlib.Path.write_text', side_effect=OSError):
            with self.assertLogs('stock', 'WARNING'):
                self.assertIsNone(mises_a_jour.verifier())  # rien d'enregistré, mais aucune erreur

    def test_verification_de_fond(self):
        import bureau

        with mock.patch.object(mises_a_jour, 'verifier', side_effect=RuntimeError('panne')) as verifier, \
                self.assertLogs('stock', 'ERROR'):
            bureau.mises_a_jour_de_fond(delai=0)
        verifier.assert_called_once_with()


class InstanceUniqueTests(SimpleTestCase):
    def windows(self, deja_ouvert, fenetres=(), classes=None):
        kernel32, user32 = mock.Mock(), mock.Mock()
        user32.FindWindowExW.side_effect = list(fenetres) + [None]
        user32.IsIconic.return_value = True

        def nom_de_classe(fenetre, tampon, taille):
            tampon.value = (classes or {}).get(fenetre, 'WindowsForms10.Window.8.app.0')
            return len(tampon.value)
        user32.GetClassNameW.side_effect = nom_de_classe
        self.enterContext(mock.patch.object(sys, 'platform', 'win32'))
        self.enterContext(mock.patch('ctypes.WinDLL', create=True,
                                     side_effect=lambda nom, **_: kernel32 if nom == 'kernel32' else user32))
        self.enterContext(mock.patch('ctypes.get_last_error', create=True, return_value=183 if deja_ouvert else 0))
        return kernel32, user32

    def test_premiere_copie(self):
        import bureau

        kernel32, user32 = self.windows(deja_ouvert=False)
        self.assertTrue(bureau.instance_unique())
        self.assertEqual(kernel32.CreateMutexW.call_args[0][2], produit.MUTEX)
        user32.SetForegroundWindow.assert_not_called()

    def test_deja_ouvert_ramene_la_fenetre(self):
        import bureau

        _, user32 = self.windows(deja_ouvert=True, fenetres=[222, 111], classes={222: 'CabinetWClass'})
        self.assertFalse(bureau.instance_unique())
        self.assertEqual(user32.FindWindowExW.call_args_list[0][0][3], produit.NOM)
        user32.ShowWindow.assert_called_once_with(111, 9)  # pas le dossier « MagaStock » de l'Explorateur
        user32.SetForegroundWindow.assert_called_once_with(111)

    def test_deja_ouvert_sans_fenetre(self):
        import bureau

        _, user32 = self.windows(deja_ouvert=True)
        self.assertFalse(bureau.instance_unique())
        user32.SetForegroundWindow.assert_not_called()

    def test_seconde_copie_ne_demarre_pas(self):
        import bureau

        with mock.patch.object(bureau, 'instance_unique', return_value=False) as unique, \
                mock.patch.object(bureau, 'dossier_donnees') as dossier, mock.patch.object(sys, 'argv', ['bureau.py']):
            bureau.main()
        unique.assert_called_once_with()
        dossier.assert_not_called()  # ni base ouverte, ni serveur, ni fenêtre
        with mock.patch.object(bureau, 'instance_unique') as unique, \
                mock.patch.object(bureau, 'dossier_donnees', side_effect=RuntimeError('arrêt')), \
                mock.patch.object(sys, 'argv', ['bureau.py', '--test']), self.assertRaises(RuntimeError):
            bureau.main()
        unique.assert_not_called()  # vérification à la construction : pas de verrou

    def test_hors_windows_ou_en_cas_d_erreur(self):
        import bureau

        with mock.patch.object(sys, 'platform', 'linux'), mock.patch('ctypes.WinDLL', create=True) as windll:
            self.assertTrue(bureau.instance_unique())  # hors Windows : aucun verrou
        windll.assert_not_called()
        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch('ctypes.WinDLL', create=True, side_effect=OSError('kernel32 introuvable')):
            self.assertTrue(bureau.instance_unique())  # erreur : le logiciel s'ouvre quand même


class MiseAJourEcransTests(AvecDossier, TestCase):
    def setUp(self):
        super().setUp()
        Utilisateur = get_user_model()
        self.responsable = Utilisateur.objects.create_user('chef', password='motdepasse-solide', is_staff=True)
        self.magasinier = Utilisateur.objects.create_user('ali', password='motdepasse-solide')
        self.client.force_login(self.responsable)
        self.enterContext(override_settings(BUREAU=True, ALLOWED_HOSTS=['*', 'testserver']))
        self.page = reverse('stock:aide')
        self.verifier = reverse('stock:mise_a_jour_verifier')

    def ecrire(self, **etat):
        (self.dossier / mises_a_jour.FICHIER).write_text(json.dumps(etat), encoding='utf-8')

    def test_carte_nouvelle_version(self):
        self.ecrire(**version_json(notes='Import Excel.\n<script>alert(1)</script>'))
        rep = self.client.get('/')
        self.assertContains(rep, 'Nouvelle version 99.0.0 disponible')
        self.assertContains(rep, f'href="{URL_INSTALLATEUR}" target="_blank"')
        self.assertContains(rep, 'Nouveautés')
        self.assertContains(rep, 'Import Excel.<br>&lt;script&gt;')
        self.assertNotContains(rep, '<script>alert(1)')

    def test_pas_de_carte(self):
        self.assertNotContains(self.client.get('/'), 'carte-maj')  # aucune vérification faite
        self.ecrire(**version_json(produit.VERSION))
        self.assertNotContains(self.client.get('/'), 'carte-maj')  # déjà à jour
        self.ecrire(**version_json())
        with mock.patch('stock.reseau.est_actif', return_value=True):
            rep = self.client.get('/', **TELEPHONE)
        self.assertNotContains(rep, 'carte-maj')  # téléphone du magasin
        with override_settings(BUREAU=False):
            self.assertNotContains(self.client.get('/'), 'carte-maj')  # version web
        self.client.force_login(self.magasinier)
        self.assertNotContains(self.client.get('/'), 'carte-maj')

    def test_bouton_verifier(self):
        rep = self.client.get(self.page)
        self.assertContains(rep, 'Version du logiciel')
        self.assertContains(rep, f'Version installée : <b>{produit.NOM} {produit.VERSION}</b>')
        self.assertContains(rep, f'action="{self.verifier}"')
        self.assertContains(rep, 'Vérifier les mises à jour</button>')
        self.assertContains(rep, 'csrfmiddlewaretoken')
        self.assertNotContains(self.client.get(reverse('stock:reglages_sauvegarde')), f'action="{self.verifier}"')
        self.assertEqual(self.client.get(self.verifier).status_code, 405)
        with mock.patch('urllib.request.urlopen', return_value=reponse(version_json())) as urlopen:
            rep = self.client.post(self.verifier)
        urlopen.assert_called_once()
        self.assertRedirects(rep, self.page + '#version')
        rep = self.client.get(self.page)
        self.assertContains(rep, 'Nouvelle version 99.0.0 disponible')
        self.assertContains(rep, 'Télécharger la version 99.0.0')
        self.assertContains(rep, 'Exécuter quand même')
        with mock.patch('urllib.request.urlopen', return_value=reponse(version_json(produit.VERSION))):
            self.client.post(self.verifier)
        rep = self.client.get(self.page)
        self.assertContains(rep, 'À jour')
        self.assertContains(rep, 'Dernière vérification : le')
        self.assertNotContains(rep, 'carte-maj')

    def test_verifier_hors_ligne(self):
        with mock.patch('urllib.request.urlopen', side_effect=urllib.error.URLError('hors ligne')):
            rep = self.client.post(self.verifier, follow=True)
        self.assertContains(rep, 'Vérification impossible le')
        self.assertContains(rep, 'connecté à internet')

    def test_reserve_aux_responsables_et_a_windows(self):
        self.client.force_login(self.magasinier)
        with mock.patch('urllib.request.urlopen') as urlopen:
            self.assertRedirects(self.client.post(self.verifier), reverse('stock:accueil'))
            self.client.force_login(self.responsable)
            with override_settings(BUREAU=False):
                self.assertNotContains(self.client.get(self.page), f'action="{self.verifier}"')
                rep = self.client.post(self.verifier, follow=True)
                self.assertContains(rep, 'logiciel installé sur Windows')
        urlopen.assert_not_called()


# =========================================================
# Licence : horloge reculée, copie commune, écran Licence
# =========================================================

class AvecLicence(AvecDossier):
    """Verrouillage actif (clé de test), copie commune du début d'essai (ProgramData) et date du jour réglable."""

    def setUp(self):
        super().setUp()
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self.privee = Ed25519PrivateKey.generate()
        publique = self.privee.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.commun = self.autre_dossier() / 'ProgramData' / produit.NOM / '.essai'
        self.enterContext(mock.patch('config.produit.CLE_PUBLIQUE', licence._b64(publique)))
        self.enterContext(mock.patch.object(licence, 'code_machine', return_value='AAAA-1111'))
        self.enterContext(override_settings(ESSAI_COMMUN=self.commun))
        self.horloge = self.enterContext(mock.patch.object(licence, '_aujourdhui', return_value=date(2026, 10, 9)))
        licence._ecriture_impossible.clear()

    def regler_horloge(self, jour):
        self.horloge.return_value = jour

    def ecrire_essai(self, contenu):
        (self.dossier / '.essai').write_text(contenu)

    def essai(self):
        return (self.dossier / '.essai').read_text()


class LicenceEssaiTests(AvecLicence, TestCase):
    def test_premier_lancement(self):
        etat = licence.etat()
        self.assertEqual((etat.mode, etat.jours_restants, etat.date_incorrecte), ('essai', 30, False))
        self.assertEqual(self.essai(), '2026-10-09;2026-10-09')
        self.assertEqual(self.commun.read_text(), '2026-10-09;2026-10-09')  # dossier commun créé
        self.assertEqual(Parametres.actuels().debut_essai, date(2026, 10, 9))

    def test_ancien_format_lu_puis_complete(self):
        self.ecrire_essai('2026-09-29')
        etat = licence.etat()
        self.assertEqual((etat.mode, etat.jours_restants), ('essai', 20))
        self.assertEqual(self.essai(), '2026-09-29;2026-10-09')

    def test_fichier_abime(self):
        self.ecrire_essai('n\'importe quoi;2026-13-45')
        self.assertEqual(licence.etat().jours_restants, 30)
        self.assertEqual(self.essai(), '2026-10-09;2026-10-09')

    def test_horloge_reculee_ne_rend_pas_de_jours(self):
        self.ecrire_essai('2026-09-29;2026-10-09')
        self.regler_horloge(date(2026, 9, 1))
        etat = licence.etat()
        self.assertEqual((etat.mode, etat.jours_restants, etat.date_incorrecte), ('essai', 20, True))
        self.assertEqual(self.essai(), '2026-09-29;2026-10-09')  # la dernière date vue ne recule jamais
        self.regler_horloge(date(2026, 10, 12))  # date corrigée
        etat = licence.etat()
        self.assertEqual((etat.jours_restants, etat.date_incorrecte), (17, False))
        self.assertEqual(self.essai(), '2026-09-29;2026-10-12')

    def test_horloge_reculee_apres_la_fin_de_l_essai(self):
        self.ecrire_essai('2026-09-01;2026-10-09')
        self.assertTrue(licence.etat().bloque)
        self.regler_horloge(date(2026, 9, 10))
        etat = licence.etat()
        self.assertTrue(etat.bloque)
        self.assertTrue(etat.date_incorrecte)

    def test_un_jour_d_ecart_tolere(self):
        self.ecrire_essai('2026-10-01;2026-10-09')
        self.regler_horloge(date(2026, 10, 8))  # fuseau horaire, minuit…
        self.assertFalse(licence.etat().date_incorrecte)
        self.regler_horloge(date(2026, 10, 7))
        self.assertTrue(licence.etat().date_incorrecte)

    def test_fichiers_mis_a_jour_une_fois_par_jour(self):
        with mock.patch.object(Path, 'write_text', autospec=True, side_effect=Path.write_text) as ecriture:
            licence.etat()
            self.assertEqual(ecriture.call_count, 2)  # dossier de données et copie commune
            licence.etat()
            licence.debut_essai()
            self.assertEqual(ecriture.call_count, 2)  # même jour : rien n'est réécrit
            self.regler_horloge(date(2026, 10, 10))
            licence.etat()
            self.assertEqual(ecriture.call_count, 4)
        self.assertEqual(self.essai(), '2026-10-09;2026-10-10')
        self.assertEqual(self.commun.read_text(), '2026-10-09;2026-10-10')

    def test_nouveau_compte_windows(self):
        """Dossier de données vide (autre compte Windows) : la copie commune garde le début de l'essai."""
        self.commun.parent.mkdir(parents=True)
        self.commun.write_text('2026-08-01;2026-09-15')
        self.assertTrue(licence.etat().bloque)
        self.assertEqual(self.essai(), '2026-08-01;2026-10-09')
        self.assertEqual(Parametres.actuels().debut_essai, date(2026, 8, 1))

    def test_le_plus_ancien_debut_compte(self):
        p = Parametres.actuels()
        p.debut_essai = date(2026, 9, 20)
        p.save()
        self.ecrire_essai('2026-10-01;2026-10-09')
        self.commun.parent.mkdir(parents=True)
        self.commun.write_text('2026-09-25')
        self.assertEqual(licence.debut_essai(), date(2026, 9, 20))
        self.assertEqual(self.commun.read_text(), '2026-09-20;2026-10-09')

    def test_copie_commune_impossible_a_ecrire(self):
        (self.dossier / 'fichier').write_text('')
        with override_settings(ESSAI_COMMUN=self.dossier / 'fichier' / '.essai'):  # dossier impossible à créer
            etat = licence.etat()
            self.assertEqual((etat.mode, etat.jours_restants), ('essai', 30))
            with mock.patch.object(Path, 'mkdir', autospec=True, side_effect=Path.mkdir) as creation:
                licence.etat()
            creation.assert_not_called()  # pas de nouvel essai le même jour
        self.assertEqual(self.essai(), '2026-10-09;2026-10-09')

    def test_ordinateur_active_reste_active(self):
        p = Parametres.actuels()
        p.cle_licence = licence.signer(self.privee, 'Client Test', 'AAAA-1111')
        p.debut_essai = date(2025, 1, 1)
        p.save()
        self.ecrire_essai('2025-01-01;2026-10-09')
        self.regler_horloge(date(2026, 1, 1))
        etat = licence.etat()
        self.assertEqual((etat.mode, etat.client, etat.date_incorrecte), ('active', 'Client Test', False))

    def test_sans_cle_publique_rien_n_est_ecrit(self):
        with mock.patch('config.produit.CLE_PUBLIQUE', ''):
            self.assertEqual(licence.etat().mode, 'libre')
        self.assertFalse((self.dossier / '.essai').exists())
        self.assertFalse(self.commun.exists())

    def test_reglage_par_defaut_hors_windows(self):
        from django.conf import settings

        with override_settings():
            del settings.ESSAI_COMMUN
            self.assertEqual(licence._fichiers_essai(), [self.dossier / '.essai'])


CONTACTS = {'WHATSAPP': '2250700000000', 'TELEPHONE_VENDEUR': '+225 27 22 00 00 00',
            'EMAIL_VENDEUR': 'vente@exemple.ci'}


class EcranLicenceTests(AvecLicence, TestCase):
    def setUp(self):
        super().setUp()
        self.responsable = get_user_model().objects.create_user('chef', password='motdepasse-solide', is_staff=True)
        self.client.force_login(self.responsable)
        self.url = reverse('stock:activation')

    def contacts(self):
        self.enterContext(mock.patch.multiple('config.produit', **CONTACTS))

    def test_contacts_et_numero_en_clair(self):
        self.contacts()
        rep = self.client.get(self.url)
        self.assertContains(rep, 'Contact du vendeur')
        self.assertContains(rep, 'par WhatsApp au <b class="numero-clair">+225 07 00 00 00 00</b>')
        self.assertContains(rep, 'href="https://wa.me/2250700000000?text=Bonjour')
        self.assertContains(rep, 'href="tel:+2252722000000"')
        self.assertContains(rep, '+225 27 22 00 00 00')
        self.assertContains(rep, 'href="mailto:vente@exemple.ci"')
        self.assertContains(rep, 'le vendeur vous donne le numéro')
        self.assertContains(rep, 'id="copier-code" data-code="AAAA-1111"')
        self.assertNotContains(rep, 'Se déconnecter')  # essai en cours

    def test_sans_contacts(self):
        rep = self.client.get(self.url)
        self.assertNotContains(rep, 'Contact du vendeur')
        self.assertContains(rep, 'par Wave.')
        self.assertContains(rep, 'Copier le code')
        self.assertContains(rep, 'csrfmiddlewaretoken')

    def test_essai_termine(self):
        self.ecrire_essai('2026-08-01;2026-10-09')
        self.assertRedirects(self.client.get('/articles/'), self.url)
        rep = self.client.get(self.url)
        self.assertContains(rep, "L'essai gratuit est terminé")
        self.assertContains(rep, f'action="{reverse("stock:deconnexion")}"')
        self.assertContains(rep, 'Se déconnecter')
        self.assertContains(rep, f'href="{reverse("stock:aide")}#licence"')
        self.assertEqual(self.client.get(reverse('stock:aide')).status_code, 200)  # l'aide reste ouverte
        self.client.post(reverse('stock:deconnexion'))
        rep = self.client.get(self.url)
        self.assertContains(rep, "L'essai gratuit est terminé")
        self.assertNotContains(rep, 'Se déconnecter')  # personne n'est connecté

    def test_date_incorrecte(self):
        self.ecrire_essai('2026-10-01;2026-10-09')
        self.regler_horloge(date(2026, 9, 1))
        self.assertContains(self.client.get(self.url), 'La date de cet ordinateur semble incorrecte')
        self.regler_horloge(date(2026, 10, 9))
        self.assertNotContains(self.client.get(self.url), 'semble incorrecte')

    def test_mode_libre_sans_paiement(self):
        self.contacts()
        with mock.patch('config.produit.CLE_PUBLIQUE', ''):
            rep = self.client.get(self.url)
        self.assertContains(rep, "n'a pas besoin de clé d'activation")
        self.assertNotContains(rep, 'Payez')
        self.assertNotContains(rep, 'name="cle"')
        self.assertContains(rep, 'Contact du vendeur')

    def test_logiciel_active(self):
        self.contacts()
        p = Parametres.actuels()
        p.cle_licence = licence.signer(self.privee, 'BTP Abidjan', 'AAAA-1111')
        p.save()
        rep = self.client.get(self.url)
        self.assertContains(rep, 'Logiciel activé')
        self.assertNotContains(rep, 'Payez')
        self.assertContains(rep, 'Contact du vendeur')


class AssistanceTests(SimpleTestCase):
    def test_numero_lisible(self):
        self.assertEqual(assistance.numero_lisible('2250700000000'), '+225 07 00 00 00 00')
        self.assertEqual(assistance.numero_lisible('+225 07-00-00-00-00'), '+225 07 00 00 00 00')
        self.assertEqual(assistance.numero_lisible('33612345678'), '+33612345678')
        self.assertEqual(assistance.numero_lisible(''), '')

    def test_contacts_seulement_renseignes(self):
        with mock.patch.multiple('config.produit', WHATSAPP='', TELEPHONE_VENDEUR='', EMAIL_VENDEUR=''):
            self.assertEqual(assistance.contacts(), [])
            self.assertEqual(assistance.lien_whatsapp('Bonjour'), '')
        with mock.patch.multiple('config.produit', WHATSAPP='+225 07 00 00 00 00', TELEPHONE_VENDEUR='',
                                 EMAIL_VENDEUR='a@b.ci'):
            self.assertEqual([c['nom'] for c in assistance.contacts()], ['WhatsApp', 'E-mail'])
            self.assertEqual(assistance.lien_whatsapp('Bonjour à tous'),
                             'https://wa.me/2250700000000?text=Bonjour%20%C3%A0%20tous')


# =========================================================
# Aide
# =========================================================

ANCRES_AIDE = ['premiers-pas', 'trouver', 'entree', 'sortie', 'annuler', 'corriger', 'inventaire', 'import',
               'etiquettes', 'utilisateurs', 'sauvegarde', 'telephones', 'mises-a-jour', 'licence']


class AideTests(AvecDossier, TestCase):
    def setUp(self):
        super().setUp()
        Utilisateur = get_user_model()
        self.responsable = Utilisateur.objects.create_user('chef', password='motdepasse-solide', is_staff=True)
        self.magasinier = Utilisateur.objects.create_user('ali', password='motdepasse-solide')
        self.url = reverse('stock:aide')

    def test_connexion_obligatoire(self):
        self.assertRedirects(self.client.get(self.url), f"{reverse('stock:connexion')}?next={self.url}")

    def test_page_pour_tous(self):
        self.client.force_login(self.magasinier)
        rep = self.client.get(self.url)
        for ancre in ANCRES_AIDE:
            self.assertContains(rep, f'id="{ancre}"')
            self.assertContains(rep, f'href="#{ancre}"')
        self.assertContains(rep, f'Version installée : <b>{produit.NOM} {produit.VERSION}</b>')
        self.assertContains(rep, f'href="{produit.SITE_WEB}"')
        self.assertNotContains(rep, 'https://wa.me/')  # aucun numéro configuré
        self.assertNotContains(rep, 'Préparer un rapport')
        self.assertNotContains(rep, reverse('stock:mise_a_jour_verifier'))
        self.assertContains(self.client.get('/'), f'href="{self.url}"')  # lien « Aide » du menu

    def test_bouton_whatsapp(self):
        self.client.force_login(self.magasinier)
        with mock.patch('config.produit.WHATSAPP', '2250700000000'):
            rep = self.client.get(self.url)
        self.assertContains(rep, "Écrire à l'assistance sur WhatsApp")
        self.assertContains(rep, 'href="https://wa.me/2250700000000?text=Bonjour')
        self.assertContains(rep, '+225 07 00 00 00 00')

    def test_responsable_version_web(self):
        self.client.force_login(self.responsable)
        rep = self.client.get(self.url)
        self.assertNotContains(rep, 'Dossier des données')
        self.assertNotContains(rep, 'Préparer un rapport')

    @override_settings(BUREAU=True, ALLOWED_HOSTS=['*', 'testserver'])
    def test_responsable_version_bureau(self):
        self.client.force_login(self.responsable)
        rep = self.client.get(self.url)
        self.assertContains(rep, 'Dossier des données')
        self.assertContains(rep, str(self.dossier))
        for vue in ('aide_dossier', 'aide_rapport', 'mise_a_jour_verifier'):
            self.assertContains(rep, f'action="{reverse("stock:" + vue)}"')
        self.client.force_login(self.magasinier)
        self.assertNotContains(self.client.get(self.url), 'Dossier des données')

    @override_settings(BUREAU=True, ALLOWED_HOSTS=['*', 'testserver'])
    def test_ouvrir_le_dossier(self):
        url = reverse('stock:aide_dossier')
        self.client.force_login(self.magasinier)
        with mock.patch('stock.views_systeme.ouvrir_fichier') as ouvrir:
            self.assertRedirects(self.client.post(url), reverse('stock:accueil'))
            self.client.force_login(self.responsable)
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertRedirects(self.client.post(url), self.url + '#assistance', fetch_redirect_response=False)
            ouvrir.assert_called_once_with(self.dossier)
            ouvrir.side_effect = OSError
            self.assertContains(self.client.post(url, follow=True), 'pas pu être ouvert')
            with mock.patch('stock.reseau.est_actif', return_value=True):
                self.assertContains(self.client.post(url, follow=True, **TELEPHONE), 'disponible seulement sur')
            with override_settings(BUREAU=False):
                self.assertContains(self.client.post(url, follow=True), 'seulement dans le logiciel installé')
        self.assertEqual(ouvrir.call_count, 2)

    @override_settings(BUREAU=True, ALLOWED_HOSTS=['*', 'testserver'])
    def test_rapport_pour_l_assistance(self):
        (self.dossier / 'journal.log').write_text('ERROR panne', encoding='utf-8')
        (self.dossier / 'erreur-demarrage.txt').write_text('Traceback', encoding='utf-8')
        (self.dossier / 'secret.key').write_text('secret')
        (self.dossier / 'cle-ia.txt').write_text('sk-ant-secret')
        self.client.force_login(self.responsable)
        with mock.patch('stock.bureau.Path.home', return_value=self.dossier), \
                mock.patch('stock.bureau.ouvrir_fichier') as ouvrir:
            rep = self.client.post(reverse('stock:aide_rapport'), HTTP_REFERER=self.url)
        self.assertRedirects(rep, self.url, fetch_redirect_response=False)
        chemin = ouvrir.call_args[0][0]
        self.assertTrue(chemin.name.startswith('rapport-assistance-') and chemin.suffix == '.zip')
        with zipfile.ZipFile(chemin) as archive:
            self.assertEqual(sorted(archive.namelist()), ['erreur-demarrage.txt', 'informations.txt', 'journal.log'])
            infos = archive.read('informations.txt').decode()
        for texte in (f'{produit.NOM} {produit.VERSION}', 'Système : ', 'Python : ', 'Articles : 0',
                      'Code de l\'ordinateur : '):
            self.assertIn(texte, infos)
        with mock.patch('stock.reseau.est_actif', return_value=True):
            rep = self.client.post(reverse('stock:aide_rapport'), **TELEPHONE)
        self.assertEqual(rep['Content-Type'], 'application/zip')  # téléphone : le fichier est téléchargé

    def test_rapport_reserve(self):
        url = reverse('stock:aide_rapport')
        self.client.force_login(self.magasinier)
        self.assertRedirects(self.client.post(url), reverse('stock:accueil'))
        self.client.force_login(self.responsable)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertContains(self.client.post(url, follow=True), 'seulement dans le logiciel installé')

    @override_settings(BUREAU=True, ALLOWED_HOSTS=['*', 'testserver'])
    def test_rapport_impossible(self):
        self.client.force_login(self.responsable)
        with mock.patch.object(assistance, 'rapport', side_effect=RuntimeError('panne')), \
                self.assertLogs('stock', 'ERROR'):
            rep = self.client.post(reverse('stock:aide_rapport'), follow=True)
        self.assertContains(rep, 'pas pu être préparé')

    def test_informations_sans_base_lisible(self):
        with mock.patch.object(Article.objects, 'count', side_effect=RuntimeError('base abîmée')):
            infos = assistance.informations()
        self.assertIn('Données illisibles', infos)
        self.assertIn(produit.VERSION, infos)


# =========================================================
# Fichiers enregistrés et ouverts sur l'ordinateur (livrer)
# =========================================================

@override_settings(BUREAU=True, ALLOWED_HOSTS=['*', 'testserver'])
class LivrerTests(AvecDossier, TestCase):
    def setUp(self):
        super().setUp()
        from django.utils import timezone

        self.chef = get_user_model().objects.create_superuser('chef', 'chef@exemple.ci', 'motdepasse-solide')
        self.client.force_login(self.chef)
        self.enterContext(mock.patch('stock.bureau.Path.home', return_value=self.dossier))
        self.ouvrir = self.enterContext(mock.patch('stock.bureau.ouvrir_fichier'))
        self.documents = self.dossier / produit.NOM
        self.nom = f'etat_stock_{timezone.localdate():%Y-%m-%d}.xlsx'

    def verrouiller(self, *noms):
        """Fichiers ouverts dans Excel : Windows refuse de les remplacer."""
        self.documents.mkdir(parents=True, exist_ok=True)
        for nom in noms:
            (self.documents / nom).write_bytes(b'ouvert')
        ecrire = Path.write_bytes

        def write_bytes(chemin, contenu):
            if chemin.name in noms:
                raise PermissionError(13, 'Accès refusé', str(chemin))
            return ecrire(chemin, contenu)
        return mock.patch.object(Path, 'write_bytes', autospec=True, side_effect=write_bytes)

    def exporter(self):
        from django.contrib.messages import get_messages

        rep = self.client.get('/articles/?format=excel', HTTP_REFERER='/articles/')
        self.assertRedirects(rep, '/articles/', fetch_redirect_response=False)
        return ' '.join(str(m) for m in get_messages(rep.wsgi_request))

    def test_fichier_ouvert_nom_suivant(self):
        with self.verrouiller(self.nom):
            message = self.exporter()
        chemin = self.ouvrir.call_args[0][0]
        self.assertEqual(chemin.name, self.nom.replace('.xlsx', ' (2).xlsx'))
        self.assertTrue(chemin.read_bytes().startswith(b'PK'))
        self.assertEqual((self.documents / self.nom).read_bytes(), b'ouvert')
        self.assertIn('Fichier ouvert', message)

    def test_tous_les_noms_ouverts(self):
        noms = [self.nom] + [self.nom.replace('.xlsx', f' ({n}).xlsx') for n in range(2, 21)]
        with self.verrouiller(*noms):
            message = self.exporter()
        self.ouvrir.assert_not_called()
        self.assertIn(f'Le fichier {self.nom} est déjà ouvert dans un autre programme', message)
        self.assertIn('Fermez-le', message)

    def test_dossier_protege(self):
        with mock.patch.object(Path, 'write_bytes', side_effect=PermissionError(13, 'Accès refusé')):
            message = self.exporter()
        self.ouvrir.assert_not_called()
        self.assertIn("n'a pas pu être enregistré dans", message)

    def test_documents_introuvable(self):
        with mock.patch('stock.bureau.dossier_documents', side_effect=OSError('disque absent')):
            message = self.exporter()
        self.assertIn("n'a pas pu être enregistré", message)

    def test_nom_de_fichier_sans_chemin(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.http import HttpResponse

        from .bureau import livrer

        requete = RequestFactory().get('/')
        requete.session = {}
        requete._messages = FallbackStorage(requete)
        reponse = HttpResponse(b'%PDF', content_type='application/pdf')
        reponse['Content-Disposition'] = 'inline; filename="../../bon.pdf"'
        livrer(requete, reponse)
        self.assertEqual(self.ouvrir.call_args[0][0], self.documents / 'bon.pdf')


@override_settings(BUREAU=True, ALLOWED_HOSTS=['*', 'testserver'])
class AdminLivrerTests(AvecDossier, TestCase):
    """Exports et impressions de l'admin (inventaire compris) : ouverts sur l'ordinateur, comme ailleurs."""

    def setUp(self):
        super().setUp()
        self.chef = get_user_model().objects.create_superuser('chef', 'chef@exemple.ci', 'motdepasse-solide')
        self.client.force_login(self.chef)
        self.enterContext(mock.patch('stock.bureau.Path.home', return_value=self.dossier))
        self.ouvrir = self.enterContext(mock.patch('stock.bureau.ouvrir_fichier'))
        self.article = Article.objects.create(code='F-1', designation='Filtre',
                                              categorie=Categorie.objects.create(nom='Filtres'))
        self.bon = BonEntree.objects.create(cree_par=self.chef)
        LigneEntree.objects.create(bon=self.bon, article=self.article, quantite=5, prix_unitaire=100)
        services.valider(self.bon, self.chef)

    def action(self, modele, action, pk):
        url = reverse(f'admin:stock_{modele}_changelist')
        return self.client.post(url, {'action': action, '_selected_action': [pk]}, HTTP_REFERER=url)

    def test_exports_et_impressions_ouverts_sur_l_ordinateur(self):
        for modele, action, pk, extension in [
                ('article', 'exporter_articles', self.article.pk, '.xlsx'),
                ('bonentree', 'imprimer_bons', self.bon.pk, '.pdf'),
                ('mouvementstock', 'exporter_mouvements', MouvementStock.objects.get().pk, '.xlsx')]:
            rep = self.action(modele, action, pk)
            self.assertRedirects(rep, reverse(f'admin:stock_{modele}_changelist'), fetch_redirect_response=False)
            self.assertEqual(self.ouvrir.call_args[0][0].suffix, extension)
        self.assertEqual(self.ouvrir.call_count, 3)

    def test_version_web_piece_jointe(self):
        with override_settings(BUREAU=False):
            rep = self.action('article', 'exporter_articles', self.article.pk)
        self.assertIn('attachment', rep['Content-Disposition'])
        self.ouvrir.assert_not_called()

    def test_lien_pdf_sans_nouvel_onglet(self):
        from django.contrib import admin

        from .admin import BonEntreeAdmin

        lien = BonEntreeAdmin(BonEntree, admin.site).pdf
        self.assertNotIn('target', lien(self.bon))
        with override_settings(BUREAU=False):
            self.assertIn('target="_blank"', lien(self.bon))


# =========================================================
# Pages d'erreur, journal, fenêtre Windows
# =========================================================

class PagesErreurTests(TestCase):
    @override_settings(DEBUG=False)
    def test_404_en_francais(self):
        rep = self.client.get('/page-qui-n-existe-pas/')
        self.assertTemplateUsed(rep, '404.html')
        self.assertContains(rep, 'Page introuvable', status_code=404)
        self.assertContains(rep, '<a class="btn btn-principal" href="/">Retour à l\'accueil</a>', status_code=404)

    def test_500_sans_contexte(self):
        from django.views.defaults import server_error

        rep = server_error(RequestFactory().get('/'))
        contenu = rep.content.decode()
        self.assertEqual(rep.status_code, 500)
        self.assertIn('Une erreur est survenue', contenu)
        self.assertIn('href="/">Retour à l\'accueil</a>', contenu)
        self.assertIn('lang="fr"', contenu)


class JournalTests(SimpleTestCase):
    def test_journal_limite_en_version_bureau(self):
        import importlib.util
        import logging.handlers

        from django.conf import settings

        temporaire = tempfile.TemporaryDirectory()
        self.addCleanup(temporaire.cleanup)
        dossier = Path(temporaire.name)
        spec = importlib.util.spec_from_file_location('reglages_bureau_essai',
                                                      settings.BASE_DIR / 'config' / 'settings.py')
        reglages = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {'MAGASIN_BUREAU': '1', 'MAGASIN_DATA_DIR': str(dossier)}):
            spec.loader.exec_module(reglages)
        options = dict(reglages.LOGGING['handlers']['fichier'])
        self.assertEqual(options.pop('class'), 'logging.handlers.RotatingFileHandler')
        self.assertEqual((options['maxBytes'], options['backupCount']), (1_000_000, 3))
        self.assertEqual(options['filename'], dossier / 'journal.log')
        options.pop('level')
        journal = logging.handlers.RotatingFileHandler(**options)
        try:
            ligne = logging.LogRecord('stock', logging.WARNING, __file__, 1, 'x' * 1000, None, None)
            for _ in range(5000):  # 5 Mo d'erreurs
                journal.emit(ligne)
        finally:
            journal.close()
        fichiers = sorted(dossier.glob('journal.log*'))
        self.assertEqual([f.name for f in fichiers], ['journal.log', 'journal.log.1', 'journal.log.2', 'journal.log.3'])
        self.assertTrue(all(f.stat().st_size <= 1_000_000 for f in fichiers))


class FenetreTests(SimpleTestCase):
    def windows(self, reponse):
        user32 = mock.Mock()
        user32.MessageBoxW.return_value = reponse
        self.enterContext(mock.patch.object(sys, 'platform', 'win32'))
        self.enterContext(mock.patch('ctypes.windll', create=True, new=mock.Mock(user32=user32)))
        return user32

    def test_webview2_absent_ouvrir_la_page(self):
        import bureau

        user32 = self.windows(6)  # Oui
        with mock.patch('webbrowser.open') as navigateur:
            bureau.proposer_webview2()
        navigateur.assert_called_once_with(bureau.WEBVIEW2)
        texte, titre, options = user32.MessageBoxW.call_args[0][1:]
        self.assertIn('Ouvrir la page de téléchargement de WebView2 ?', texte)
        self.assertEqual((titre, options), (produit.NOM, 0x14))

    def test_webview2_absent_non(self):
        import bureau

        self.windows(7)  # Non
        with mock.patch('webbrowser.open') as navigateur:
            bureau.proposer_webview2()
        navigateur.assert_not_called()

    def test_webview2_sans_erreur(self):
        import bureau

        user32 = self.windows(6)
        user32.MessageBoxW.side_effect = OSError('user32')
        with mock.patch('webbrowser.open') as navigateur:
            bureau.proposer_webview2()  # aucune exception
        navigateur.assert_not_called()
        with mock.patch.object(sys, 'platform', 'linux'):
            user32.MessageBoxW.reset_mock()
            bureau.proposer_webview2()
        user32.MessageBoxW.assert_not_called()

    def lancer(self, webview):
        import bureau

        temporaire = tempfile.TemporaryDirectory()
        self.addCleanup(temporaire.cleanup)
        dossier = Path(temporaire.name)
        with mock.patch.dict(sys.modules, {'webview': webview}), mock.patch.object(sys, 'argv', ['bureau.py']), \
                mock.patch.object(bureau, 'instance_unique', return_value=True), \
                mock.patch.object(bureau, 'dossier_donnees', return_value=dossier), \
                mock.patch.object(bureau, 'proposer_webview2') as proposer, \
                mock.patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as fin:
            bureau.main()
        self.assertEqual(fin.exception.code, 1)
        proposer.assert_called_once_with()
        self.assertIn(bureau.WEBVIEW2, (dossier / 'erreur-demarrage.txt').read_text(encoding='utf-8'))

    def test_fenetre_impossible(self):
        from types import SimpleNamespace

        webview = SimpleNamespace(settings={}, create_window=mock.Mock(),
                                  start=mock.Mock(side_effect=RuntimeError('WebView2 absent')))
        self.lancer(webview)
        self.assertEqual(webview.settings, {'OPEN_EXTERNAL_LINKS_IN_BROWSER': True, 'ALLOW_DOWNLOADS': True})

    def test_reglage_inconnu_de_pywebview(self):
        from types import SimpleNamespace

        class Reglages(dict):
            def __setitem__(self, cle, valeur):
                if cle == 'ALLOW_DOWNLOADS':  # ancienne version de pywebview
                    raise KeyError(cle)
                super().__setitem__(cle, valeur)

        webview = SimpleNamespace(settings=Reglages(), create_window=mock.Mock(),
                                  start=mock.Mock(side_effect=RuntimeError))
        self.lancer(webview)
        self.assertEqual(dict(webview.settings), {'OPEN_EXTERNAL_LINKS_IN_BROWSER': True})
        webview.create_window.assert_called_once()

    def test_contact_sur_l_ecran_d_echec(self):
        import bureau

        with mock.patch.multiple('config.produit', WHATSAPP='2250700000000', TELEPHONE_VENDEUR='',
                                 EMAIL_VENDEUR='a@b.ci'):
            self.assertEqual(bureau.contact_assistance(), 'WhatsApp : +225 07 00 00 00 00 · E-mail : a@b.ci')
        with mock.patch.multiple('config.produit', WHATSAPP='', TELEPHONE_VENDEUR='', EMAIL_VENDEUR=''):
            self.assertEqual(bureau.contact_assistance(), '')
