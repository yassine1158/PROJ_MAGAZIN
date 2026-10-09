"""Remplit le magasin avec un exemple (plan, articles, stock) pour essayer le logiciel.

    python manage.py demo
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stock import services
from stock.models import (
    Article, Bloc, BonEntree, BonSortie, Categorie, Chantier, Engin, Etagere, Fournisseur, LigneEntree, LigneSortie,
)

BLOCS = [
    # code, nom, couleur, x, z, largeur, profondeur, nb étagères
    ('A', 'Pièces moteur & filtres', '#2563eb', 0, 0, 12, 8, 4),
    ('B', 'Pneumatiques & hydraulique', '#16a34a', 14, 0, 10, 8, 3),
    ('C', 'Outillage & EPI', '#d97706', 0, 10, 12, 7, 4),
    ('D', 'Matériaux & adjuvants', '#9333ea', 14, 10, 10, 7, 2),
]

ARTICLES = [
    # code, désignation, catégorie, unité, bloc, étagère, niveau, mots-clés, stock min, qté, prix
    ('FH-VOL-01', 'Filtre à huile Volvo FMX', 'Filtres', 'U', 'A', 'R1', 2, 'filtre zit, filtre huile', 4, 12, 18500),
    ('FG-VOL-01', 'Filtre à gasoil Volvo FMX', 'Filtres', 'U', 'A', 'R1', 3, 'filtre gazoil, filtre carburant', 4, 3, 21000),
    ('FA-CAT-01', 'Filtre à air Caterpillar 320', 'Filtres', 'U', 'A', 'R2', 1, 'filtre air, filtre hwa', 2, 6, 64000),
    ('HM-1540', 'Huile moteur 15W40 (fût 208 L)', 'Lubrifiants', 'FUT', 'A', 'R3', 1, 'zit moteur, huile', 1, 4, 520000),
    ('HH-68', 'Huile hydraulique ISO 68 (bidon 20 L)', 'Lubrifiants', 'BID', 'A', 'R3', 2, 'zit hydraulique', 3, 10, 48000),
    ('GR-EP2', 'Graisse EP2 (seau 18 kg)', 'Lubrifiants', 'U', 'A', 'R4', 1, 'chahma, graisse', 2, 5, 39000),
    ('CO-ALT-24', 'Courroie alternateur camion malaxeur', 'Pièces moteur', 'U', 'A', 'R4', 3, 'courroie, sir', 2, 2, 27500),
    ('PN-1200R20', 'Pneu 12.00 R20 camion', 'Pneumatiques', 'U', 'B', 'P1', 1, 'pneu, roue, 3ajla', 4, 8, 285000),
    ('CH-1200R20', 'Chambre à air 12.00 R20', 'Pneumatiques', 'U', 'B', 'P1', 3, 'chambre air', 4, 10, 32000),
    ('FL-HYD-12', 'Flexible hydraulique 1/2" (m)', 'Hydraulique', 'M', 'B', 'P2', 2, 'flexible, tuyau, tiyo', 10, 45, 6500),
    ('JT-KIT-320', 'Kit joints vérin pelle 320', 'Hydraulique', 'JEU', 'B', 'P3', 2, 'joints, verin', 1, 1, 145000),
    ('EPI-CAS', 'Casque de chantier', 'EPI', 'U', 'C', 'O1', 3, 'casque, kasket', 10, 40, 3500),
    ('EPI-GAN', 'Gants de manutention (paire)', 'EPI', 'PAIRE', 'C', 'O1', 2, 'gants, gant', 20, 15, 1500),
    ('EPI-BOT-42', 'Chaussures de sécurité P42', 'EPI', 'U', 'C', 'O2', 1, 'sabbat, chaussures, bottes', 5, 9, 18000),
    ('OUT-MEU-125', 'Disque à meuler 125 mm', 'Outillage', 'U', 'C', 'O3', 2, 'disque, meule', 20, 60, 1200),
    ('OUT-CLE-COMB', 'Jeu de clés mixtes 8–32', 'Outillage', 'JEU', 'C', 'O4', 2, 'clé, mfateh', 1, 3, 42000),
    ('CIM-CPJ45', 'Ciment CPJ 45 (sac 50 kg)', 'Matériaux', 'SAC', 'D', 'M1', 1, 'ciment, smenta, sac ciment', 100, 400, 5200),
    ('ADJ-PLAST', 'Adjuvant plastifiant (bidon 25 L)', 'Adjuvants', 'BID', 'D', 'M2', 2, 'adjuvant, plastifiant', 4, 12, 38000),
]


class Command(BaseCommand):
    help = "Crée un magasin d'exemple (plan 3D, articles, stock, quelques sorties)."

    @transaction.atomic
    def handle(self, *args, **options):
        if Article.objects.exists() or Bloc.objects.exists():
            raise CommandError('La base contient déjà des données : la démonstration ne s\'installe que sur une base vide.')
        user = get_user_model().objects.filter(is_superuser=True).first()

        etageres = {}
        for code, nom, couleur, x, z, l, p, nb in BLOCS:
            bloc = Bloc.objects.create(code=code, nom=nom, couleur=couleur, x=x, z=z, largeur=l, profondeur=p)
            prefixe = {'A': 'R', 'B': 'P', 'C': 'O', 'D': 'M'}[code]
            largeur = Decimal(l - 2) / nb if code != 'D' else Decimal(3.5)
            for i in range(nb):
                e = Etagere.objects.create(
                    bloc=bloc, code=f'{prefixe}{i + 1}', x=Decimal(1) + i * largeur, z=Decimal(1.5),
                    largeur=min(largeur - Decimal(0.4), Decimal(3)), profondeur=Decimal(0.9),
                    hauteur=Decimal(2.4), nb_niveaux=4,
                )
                etageres[(code, e.code)] = e
            if code in 'AC':  # une deuxième rangée, dos à dos
                for i in range(nb):
                    e = Etagere.objects.create(
                        bloc=bloc, code=f'{prefixe}{i + 1 + nb}', x=Decimal(1) + i * largeur, z=Decimal(p - 2.4),
                        largeur=min(largeur - Decimal(0.4), Decimal(3)), profondeur=Decimal(0.9),
                        hauteur=Decimal(2.4), nb_niveaux=4,
                    )
                    etageres[(code, e.code)] = e

        fournisseur = Fournisseur.objects.create(nom='Fournisseur exemple', telephone='+225 00 00 00 00')
        entree = BonEntree.objects.create(fournisseur=fournisseur, reference='BL-DEMO-001', cree_par=user,
                                          observation='Stock initial (démonstration)')
        articles = {}
        for code, des, cat, unite, bloc, etg, niv, mots, smin, qte, prix in ARTICLES:
            a = Article.objects.create(
                code=code, designation=des, categorie=Categorie.objects.get_or_create(nom=cat)[0], unite=unite,
                etagere=etageres[(bloc, etg)], niveau=niv, mots_cles=mots, stock_min=smin,
            )
            articles[code] = a
            LigneEntree.objects.create(bon=entree, article=a, quantite=qte, prix_unitaire=prix)
        services.valider(entree, user)

        chantier = Chantier.objects.create(nom='Chantier Pont de Bingerville')
        Chantier.objects.create(nom='Centrale à béton Yopougon')
        engin = Engin.objects.create(code='MLX-07', designation='Camion malaxeur Volvo FMX')
        sortie = BonSortie.objects.create(chantier=chantier, engin=engin, demandeur='Chef de parc', cree_par=user)
        LigneSortie.objects.create(bon=sortie, article=articles['FH-VOL-01'], quantite=2)
        LigneSortie.objects.create(bon=sortie, article=articles['HM-1540'], quantite=1)
        services.valider(sortie, user)

        self.stdout.write(self.style.SUCCESS(
            f'Démonstration installée : {Bloc.objects.count()} blocs, {Etagere.objects.count()} étagères, '
            f'{Article.objects.count()} articles.'))
