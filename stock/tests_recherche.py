"""Tests de la recherche d'articles : recherche locale, IA, page « Trouver un article », barre globale."""
import json
import time
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from . import recherche
from .models import Article, Bloc, Categorie, Etagere
from .recherche import phonetique, recherche_locale


class RechercheBase(TestCase):
    def setUp(self):
        recherche.vider_index()
        recherche._pause_ia_jusqu_a = 0.0
        self.responsable = get_user_model().objects.create_superuser('chef', 'c@c.ci', 'motdepasse-solide')
        self.magasinier = get_user_model().objects.create_user('mag', 'm@m.ci', 'motdepasse-solide')
        filtres = Categorie.objects.create(nom='Filtres')
        lubrifiants = Categorie.objects.create(nom='Lubrifiants')
        materiaux = Categorie.objects.create(nom='Matériaux')
        electricite = Categorie.objects.create(nom='Électricité')
        bloc_a = Bloc.objects.create(code='A', nom='Pièces moteur')
        bloc_b = Bloc.objects.create(code='B', nom='Matériaux', x=14)
        self.a_r1 = Etagere.objects.create(bloc=bloc_a, code='R1')
        self.a_r2 = Etagere.objects.create(bloc=bloc_a, code='R2')
        self.b_r1 = Etagere.objects.create(bloc=bloc_b, code='R1')

        def article(code, designation, categorie, etagere=None, niveau=None, **autres):
            return Article.objects.create(code=code, designation=designation, categorie=categorie, etagere=etagere,
                                          niveau=niveau, **autres)

        self.filtre_huile = article('FH-VOL-01', 'Filtre à huile Volvo FMX', filtres, self.a_r1, 2,
                                    mots_cles='filtre huile')
        self.filtre_gasoil = article('FG-VOL-01', 'Filtre à gasoil Volvo FMX', filtres, self.a_r1, 3,
                                     mots_cles='filtre carburant', reference_fabricant='21707134')
        self.huile = article('HM-1540', 'Huile moteur 15W40 (fût 208 L)', lubrifiants, self.a_r2, 1)
        self.ciment = article('CIM-CPJ45', 'Ciment CPJ 45 (sac 50 kg)', materiaux, self.b_r1, 1, mots_cles='sac ciment')
        self.sable = article('SAB-RIV', 'Sable de rivière (tonne)', materiaux, self.b_r1, 2)
        self.cable = article('CAB-25', 'Câble électrique 2,5 mm²', electricite)
        self.fer10 = article('FER-HA10', 'Fer à béton HA 10 (barre 12 m)', materiaux, self.b_r1, 3)
        self.fer12 = article('FER-HA12', 'Fer à béton HA 12 (barre 12 m)', materiaux, self.b_r1, 3)
        self.brouette = article('BRT-01', 'Brouette renforcée', materiaux)

    def codes(self, *termes, **options):
        return [a.code for a in recherche_locale(list(termes), **options)]

    def premier(self, *termes):
        resultats = self.codes(*termes)
        return resultats[0] if resultats else None


class CodesTapesAutrementTests(RechercheBase):
    def test_code_sans_tiret_ni_espace(self):
        self.assertEqual(self.premier('HM1540'), 'HM-1540')
        self.assertEqual(self.premier('hm 1540'), 'HM-1540')
        self.assertEqual(self.premier('FH VOL 01'), 'FH-VOL-01')
        self.assertEqual(self.premier('fhvol01'), 'FH-VOL-01')
        self.assertEqual(self.premier('fh-vol-01'), 'FH-VOL-01')

    def test_dimensions_ecrites_autrement(self):
        for terme in ['15 w 40', '15W-40', '15w40', '15W 40']:
            self.assertEqual(self.premier(terme), 'HM-1540', terme)

    def test_reference_fabricant(self):
        self.assertEqual(self.premier('21707134'), 'FG-VOL-01')
        self.assertEqual(self.premier('217 071 34'), 'FG-VOL-01')

    def test_article_par_code(self):
        self.assertEqual(recherche.article_par_code('hm1540'), self.huile)
        self.assertEqual(recherche.article_par_code(' FH VOL 01 '), self.filtre_huile)
        self.assertIsNone(recherche.article_par_code('HM'))
        self.assertIsNone(recherche.article_par_code(''))


class FautesTests(RechercheBase):
    def test_phonetique(self):
        self.assertEqual(phonetique('ciment'), phonetique('ciman'))
        self.assertEqual(phonetique('ciment'), phonetique('simen'))
        self.assertEqual(phonetique('ciments'), phonetique('ciment'))
        self.assertEqual(phonetique('gazoil'), phonetique('gasoil'))
        self.assertEqual(phonetique('taule'), phonetique('tole'))
        self.assertEqual(phonetique('brouete'), phonetique('brouette'))
        self.assertEqual(phonetique('peinture'), phonetique('pinture'))
        self.assertNotEqual(phonetique('sable'), phonetique('cable'))
        self.assertNotEqual(phonetique('ciment'), phonetique('sable'))

    def test_fautes_de_frappe_et_de_son(self):
        for terme in ['ciman', 'simen', 'siment', 'cimen', 'ciments', 'CIMENT']:
            self.assertEqual(self.premier(terme), 'CIM-CPJ45', terme)
        self.assertEqual(self.premier('gazoil'), 'FG-VOL-01')
        self.assertEqual(self.premier('filtr huille'), 'FH-VOL-01')
        self.assertEqual(self.premier('brouete'), 'BRT-01')

    def test_pas_de_faux_positif_grossier(self):
        self.assertEqual(self.codes('sable'), ['SAB-RIV'])
        self.assertEqual(self.codes('cable'), ['CAB-25'])
        self.assertEqual(self.codes('xyzxyz'), [])
        self.assertNotIn('SAB-RIV', self.codes('ciman'))

    def test_mots_vides_et_nombres(self):
        self.assertEqual(self.codes('fer de 10')[:2], ['FER-HA10', 'FER-HA12'])
        self.assertEqual(self.premier('sac de ciment'), 'CIM-CPJ45')


class EmplacementTests(RechercheBase):
    def test_etagere(self):
        self.assertEqual(set(self.codes('R1')), {'FH-VOL-01', 'FG-VOL-01', 'CIM-CPJ45', 'SAB-RIV', 'FER-HA10', 'FER-HA12'})
        self.assertEqual(set(self.codes('R2')), {'HM-1540'})

    def test_bloc_puis_etagere(self):
        for terme in ['A R1', 'a-r1', 'AR1', 'bloc A étagère R1']:
            self.assertEqual(set(self.codes(terme)), {'FH-VOL-01', 'FG-VOL-01'}, terme)
        self.assertEqual(set(self.codes('bloc A')), {'FH-VOL-01', 'FG-VOL-01', 'HM-1540'})

    def test_niveau(self):
        self.assertEqual(self.premier('A R1 niveau 3'), 'FG-VOL-01')

    def test_poids_faible(self):
        """Un nom d'article passe avant un emplacement qui lui ressemble."""
        Article.objects.create(code='R1-JOINT', designation='Joint R1', categorie=Categorie.objects.first())
        self.assertEqual(self.premier('R1'), 'R1-JOINT')


class IndexTests(RechercheBase):
    def test_article_cree_modifie_desactive(self):
        self.assertEqual(self.codes('marteau'), [])
        marteau = Article.objects.create(code='MAR-1', designation='Marteau de coffreur',
                                         categorie=Categorie.objects.first())
        self.assertEqual(self.codes('marteau'), ['MAR-1'])
        marteau.designation = 'Massette 2 kg'
        marteau.save()
        self.assertEqual(self.codes('marteau'), [])
        self.assertEqual(self.codes('massette'), ['MAR-1'])
        marteau.actif = False
        marteau.save()
        self.assertEqual(self.codes('massette'), [])

    def test_mouvement_de_stock_ne_vide_pas_l_index(self):
        self.codes('ciment')
        index = recherche._index
        self.assertIsNotNone(index)
        self.ciment.stock = 12
        self.ciment.save(update_fields=['stock'])
        self.assertIs(recherche._index, index)
        self.ciment.designation = 'Ciment CPA 42,5'
        self.ciment.save()
        self.assertIsNone(recherche._index)

    def test_etagere_renommee(self):
        self.assertEqual(self.codes('R2'), ['HM-1540'])
        self.a_r2.code = 'R9'
        self.a_r2.save()
        self.assertEqual(self.codes('R2'), [])
        self.assertEqual(self.codes('R9'), ['HM-1540'])

    def test_rapide_sur_5000_articles(self):
        categorie = Categorie.objects.first()
        noms = ['Filtre', 'Joint', 'Roulement', 'Boulon', 'Tuyau', 'Courroie', 'Pompe', 'Disque', 'Tôle', 'Peinture']
        Article.objects.bulk_create([
            Article(code=f'GEN-{i:05d}', designation=f'{noms[i % 10]} modèle {i} acier {i % 97} mm', categorie=categorie,
                    mots_cles=f'pièce {i % 13}', etagere=self.a_r1 if i % 2 else self.b_r1, niveau=1 + i % 3)
            for i in range(5000)
        ])
        recherche.vider_index()
        debut = time.perf_counter()
        recherche_locale(['ciment'])  # construit l'index
        construction = time.perf_counter() - debut
        pire = 0
        for terme in ['filtre huile', 'ciman', 'roulemant', 'HM1540', '15 w 40', 'A R1', 'xyzxyz']:
            debut = time.perf_counter()
            recherche_locale([terme])
            pire = max(pire, time.perf_counter() - debut)
        # Mesuré à environ 0,1 s pour la construction et 0,05 s par recherche ; marge pour les machines lentes.
        self.assertLess(construction, 2)
        self.assertLess(pire, 0.5)


class LienFicheTests(RechercheBase):
    @override_settings(MAGASIN_IA_ACTIVE=False)
    def test_magasinier_ouvre_la_fiche_article(self):
        self.client.force_login(self.magasinier)
        data = self.client.post(reverse('stock:api_recherche') + '?ia=0', {'q': 'filtre huile'}).json()
        article = data['resultats'][0]
        self.assertNotIn('admin_url', article)
        self.assertEqual(article['url'], reverse('stock:article_modifier', args=[self.filtre_huile.pk]))
        self.assertEqual(self.client.get(article['url']).status_code, 200)

    def test_api_article(self):
        self.client.force_login(self.magasinier)
        data = self.client.get(reverse('stock:api_article', args=[self.huile.pk])).json()
        self.assertEqual(data['url'], reverse('stock:article_modifier', args=[self.huile.pk]))


class ApiRechercheTests(RechercheBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.magasinier)

    def poster(self, ia, **donnees):
        return self.client.post(reverse('stock:api_recherche') + f'?ia={ia}', donnees)

    @override_settings(MAGASIN_IA_ACTIVE=True)
    def test_d_abord_local_puis_ia(self):
        ia = SimpleNamespace(termes=['ciment', 'sac de ciment'], categorie='Matériaux', explication='Du ciment en sac.')
        with mock.patch('stock.recherche.interpreter', return_value=ia) as interpreter:
            local = self.poster(0, q='smenta').json()
            interpreter.assert_not_called()
            self.assertFalse(local['ia'])
            self.assertTrue(local['ia_disponible'])
            complete = self.poster(1, q='smenta').json()
            interpreter.assert_called_once()
        self.assertTrue(complete['ia'])
        self.assertEqual(complete['explication'], 'Du ciment en sac.')
        self.assertEqual(complete['resultats'][0]['code'], 'CIM-CPJ45')

    @override_settings(MAGASIN_IA_ACTIVE=True)
    def test_code_exact_sans_ia(self):
        with mock.patch('stock.recherche.interpreter') as interpreter:
            data = self.poster(0, q='HM1540').json()
            interpreter.assert_not_called()
        self.assertTrue(data['exact'])
        self.assertFalse(data['ia_disponible'])
        self.assertEqual([a['code'] for a in data['resultats']], ['HM-1540'])

    @override_settings(MAGASIN_IA_ACTIVE=False)
    def test_plus_de_12_resultats(self):
        categorie = Categorie.objects.first()
        for i in range(15):
            Article.objects.create(code=f'CL-{i}', designation=f'Clou de {i + 40} mm', categorie=categorie)
        data = self.poster(0, q='clou').json()
        self.assertEqual(len(data['resultats']), 12)
        self.assertTrue(data['plus'])

    @override_settings(MAGASIN_IA_ACTIVE=False)
    def test_photo_sans_ia(self):
        photo = SimpleUploadedFile('p.jpg', b'x', content_type='image/jpeg')
        rep = self.client.post(reverse('stock:api_recherche') + '?ia=1', {'q': '', 'photo': photo})
        self.assertEqual(rep.status_code, 400)
        self.assertIn('demandez au responsable', rep.json()['erreur'])

    @override_settings(MAGASIN_IA_ACTIVE=True)
    def test_photo_illisible(self):
        photo = SimpleUploadedFile('p.jpg', b'pas une image', content_type='image/jpeg')
        with mock.patch('anthropic.Anthropic') as client:
            rep = self.client.post(reverse('stock:api_recherche') + '?ia=1', {'q': '', 'photo': photo})
            client.assert_not_called()
        self.assertEqual(rep.status_code, 400)
        self.assertIn('photo lisible', rep.json()['erreur'])

    @override_settings(MAGASIN_IA_ACTIVE=True)
    def test_photo_quand_l_ia_ne_repond_pas(self):
        photo = SimpleUploadedFile('p.jpg', b'x', content_type='image/jpeg')
        with mock.patch('stock.recherche.interpreter', return_value=None):
            data = self.client.post(reverse('stock:api_recherche') + '?ia=1', {'q': '', 'photo': photo}).json()
        self.assertEqual(data['resultats'], [])
        self.assertIn("Écrivez le nom de l'article", data['message'])


@override_settings(MAGASIN_IA_ACTIVE=True)
class IaTests(RechercheBase):
    def client_ia(self, **parse):
        client = mock.MagicMock()
        if parse:
            client.return_value.beta.messages.parse.configure_mock(**parse)
        return mock.patch('anthropic.Anthropic', client)

    def test_delai_court_sans_nouvel_essai(self):
        reponse = SimpleNamespace(stop_reason='end_turn', parsed_output=SimpleNamespace(
            termes=['filtre à gasoil'], categorie='Filtres', explication='Un filtre à gasoil.'))
        with self.client_ia(return_value=reponse) as client:
            interpretation, resultats = recherche.rechercher('filtre gazoil volvo')
        client.assert_called_once_with(timeout=10, max_retries=0)
        self.assertEqual(interpretation.explication, 'Un filtre à gasoil.')
        self.assertEqual(resultats[0], self.filtre_gasoil)

    def test_panne_reseau_pause_de_5_minutes(self):
        import anthropic

        panne = anthropic.APITimeoutError(request=mock.Mock())
        with self.client_ia(side_effect=panne) as client, self.assertLogs('stock.recherche', 'WARNING'):
            interpretation, resultats = recherche.rechercher('ciment')
            self.assertIsNone(interpretation)
            self.assertEqual(resultats[0], self.ciment)
            self.assertFalse(recherche.ia_disponible())
            recherche.rechercher('ciment')
            self.assertEqual(client.call_count, 1)  # pas de nouvel appel pendant la pause
            with mock.patch('stock.recherche.time.monotonic', return_value=time.monotonic() + 301):
                self.assertTrue(recherche.ia_disponible())
                recherche.rechercher('ciment')
            self.assertEqual(client.call_count, 2)

    def test_reponse_invalide_retombe_sur_la_recherche_locale(self):
        from pydantic import BaseModel, ValidationError

        class Modele(BaseModel):
            termes: list[str]

        try:
            Modele.model_validate_json('{"termes": ["fil')
        except ValidationError as e:
            erreur = e
        with self.client_ia(side_effect=erreur), self.assertLogs('stock.recherche', 'ERROR'):
            interpretation, resultats = recherche.rechercher('filtre huile')
        self.assertIsNone(interpretation)
        self.assertEqual(resultats[0], self.filtre_huile)
        self.assertTrue(recherche.ia_disponible())  # pas une panne de réseau : pas de pause

    def test_api_reponse_invalide(self):
        self.client.force_login(self.magasinier)
        with mock.patch('stock.recherche.interpreter', side_effect=ValueError('Invalid JSON: EOF while parsing')), \
                self.assertLogs('stock.recherche', 'ERROR'):
            rep = self.client.post(reverse('stock:api_recherche') + '?ia=1', {'q': 'filtre huile'})
        self.assertEqual(rep.status_code, 200)
        self.assertEqual(rep.json()['resultats'][0]['code'], 'FH-VOL-01')

    def test_consignes_cote_d_ivoire(self):
        self.assertIn("Côte d'Ivoire", recherche.CONSIGNES)
        self.assertNotIn('darija', recherche.CONSIGNES.lower())
        self.assertNotIn('tunisi', recherche.CONSIGNES.lower())


class PageRechercheTests(RechercheBase):
    def initial(self, rep):
        html = rep.content.decode()
        debut = html.index('id="recherche-initiale"')
        return json.loads(html[html.index('>', debut) + 1:html.index('</script>', debut)])

    @override_settings(MAGASIN_IA_ACTIVE=False)
    def test_sans_ia(self):
        self.client.force_login(self.magasinier)
        rep = self.client.get(reverse('stock:recherche'))
        self.assertNotContains(rep, 'id="btn-photo"')
        self.assertNotContains(rep, 'Activer la recherche par photo')
        self.assertNotContains(rep, 'IA non configurée')
        self.assertContains(rep, 'fer de 10, sac de ciment, filtre gasoil')
        self.client.force_login(self.responsable)
        rep = self.client.get(reverse('stock:recherche'))
        self.assertContains(rep, 'Activer la recherche par photo')
        self.assertContains(rep, reverse('stock:reglages_ia'))

    @override_settings(MAGASIN_IA_ACTIVE=True)
    def test_avec_ia(self):
        self.client.force_login(self.magasinier)
        self.assertContains(self.client.get(reverse('stock:recherche')), 'id="btn-photo"')

    @override_settings(MAGASIN_IA_ACTIVE=False)
    def test_douchette_affiche_directement_l_article(self):
        self.client.force_login(self.magasinier)
        rep = self.client.get(reverse('stock:recherche'), {'q': 'FHVOL01'})
        initial = self.initial(rep)
        self.assertTrue(initial['exact'])
        self.assertEqual([a['code'] for a in initial['resultats']], ['FH-VOL-01'])

    @override_settings(MAGASIN_IA_ACTIVE=False)
    def test_resultats_inclus_dans_la_page(self):
        self.client.force_login(self.magasinier)
        initial = self.initial(self.client.get(reverse('stock:recherche'), {'q': 'ciman'}))
        self.assertEqual(initial['resultats'][0]['code'], 'CIM-CPJ45')
        initial = self.initial(self.client.get(reverse('stock:recherche'), {'article': self.huile.pk}))
        self.assertEqual(initial['resultats'][0]['code'], 'HM-1540')
        rep = self.client.get(reverse('stock:recherche'), {'article': 'abc'})
        self.assertEqual(rep.status_code, 200)
        self.assertContains(rep, '<script id="recherche-initiale" type="application/json">null</script>', html=False)

    def test_barre_de_recherche_globale(self):
        self.client.force_login(self.magasinier)
        rep = self.client.get(reverse('stock:accueil'))
        self.assertContains(rep, 'class="recherche-globale"', count=2)  # menu et barre du téléphone
        self.assertContains(rep, ' data-raccourci-recherche', count=2)
        self.assertContains(rep, f'action="{reverse("stock:recherche")}"')
        # Sur la page de recherche, le champ de la page suffit.
        rep = self.client.get(reverse('stock:recherche'))
        self.assertNotContains(rep, 'class="recherche-globale"')
        self.assertContains(rep, ' data-raccourci-recherche', count=1)
