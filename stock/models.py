from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F
from django.utils import timezone

QTE = {'max_digits': 14, 'decimal_places': 3}
PRIX = {'max_digits': 16, 'decimal_places': 2}
POSITIF = [MinValueValidator(Decimal('0'))]
STRICT_POSITIF = [MinValueValidator(Decimal('0.001'))]


# =========================================================
# Référentiels
# =========================================================

class Categorie(models.Model):
    nom = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ['nom']
        verbose_name = 'catégorie'

    def __str__(self):
        return self.nom


class Fournisseur(models.Model):
    nom = models.CharField(max_length=150, unique=True)
    telephone = models.CharField('téléphone', max_length=50, blank=True)
    email = models.EmailField(blank=True)
    adresse = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['nom']

    def __str__(self):
        return self.nom


class Chantier(models.Model):
    nom = models.CharField(max_length=150, unique=True)
    actif = models.BooleanField(default=True)

    class Meta:
        ordering = ['nom']

    def __str__(self):
        return self.nom


class Engin(models.Model):
    code = models.CharField(max_length=50, unique=True)
    designation = models.CharField('désignation', max_length=150, blank=True)
    actif = models.BooleanField(default=True)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'{self.code} – {self.designation}' if self.designation else self.code


# =========================================================
# Plan du magasin (pour la localisation et la vue 3D)
# Coordonnées en mètres : x vers la droite, z vers le fond.
# =========================================================

class Bloc(models.Model):
    code = models.CharField(max_length=10, unique=True, help_text='Ex. : A, B, C…')
    nom = models.CharField(max_length=100, blank=True, help_text='Ex. : Pièces moteur, Ciment…')
    couleur = models.CharField(max_length=7, default='#3b82f6', help_text='Couleur dans la vue 3D (#rrggbb).')
    x = models.DecimalField('position X (m)', max_digits=7, decimal_places=2, default=0)
    z = models.DecimalField('position Z (m)', max_digits=7, decimal_places=2, default=0)
    largeur = models.DecimalField('largeur (m)', max_digits=7, decimal_places=2, default=10)
    profondeur = models.DecimalField('profondeur (m)', max_digits=7, decimal_places=2, default=8)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'Bloc {self.code}' + (f' – {self.nom}' if self.nom else '')


class Etagere(models.Model):
    bloc = models.ForeignKey(Bloc, on_delete=models.CASCADE, related_name='etageres')
    code = models.CharField(max_length=20, help_text='Ex. : R1, E03…')
    x = models.DecimalField('position X dans le bloc (m)', max_digits=7, decimal_places=2, default=1)
    z = models.DecimalField('position Z dans le bloc (m)', max_digits=7, decimal_places=2, default=1)
    largeur = models.DecimalField('largeur (m)', max_digits=6, decimal_places=2, default=3)
    profondeur = models.DecimalField('profondeur (m)', max_digits=6, decimal_places=2, default=1)
    hauteur = models.DecimalField('hauteur (m)', max_digits=6, decimal_places=2, default=2.5)
    nb_niveaux = models.PositiveSmallIntegerField('nombre de niveaux', default=4)
    tournee = models.BooleanField('tournée de 90°', default=False)

    class Meta:
        ordering = ['bloc__code', 'code']
        verbose_name = 'étagère'
        constraints = [models.UniqueConstraint(fields=['bloc', 'code'], name='etagere_unique_par_bloc')]

    def __str__(self):
        return f'{self.bloc.code} / {self.code}'


class Mur(models.Model):
    """Mur du magasin, tracé dans l'éditeur de plan (segment de (x1, z1) à (x2, z2), en mètres)."""
    x1 = models.DecimalField(max_digits=7, decimal_places=2)
    z1 = models.DecimalField(max_digits=7, decimal_places=2)
    x2 = models.DecimalField(max_digits=7, decimal_places=2)
    z2 = models.DecimalField(max_digits=7, decimal_places=2)
    epaisseur = models.DecimalField('épaisseur (m)', max_digits=4, decimal_places=2, default=Decimal('0.20'))
    hauteur = models.DecimalField('hauteur (m)', max_digits=5, decimal_places=2, default=Decimal('3.00'))
    couleur = models.CharField(max_length=7, default='#d6d3d1')

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'Mur ({self.x1};{self.z1}) → ({self.x2};{self.z2})'


class Element(models.Model):
    """Objet posé sur le plan : porte, fenêtre, bureau, poteau, quai, zone au sol, texte…

    Position = centre de l'objet ; rotation en degrés (sens des aiguilles d'une montre, vu de dessus).
    La liste des types et leurs tailles par défaut sont dans stock/plan.py (TYPES_ELEMENTS).
    """
    type = models.CharField(max_length=20)
    nom = models.CharField(max_length=60, blank=True)
    x = models.DecimalField(max_digits=7, decimal_places=2)
    z = models.DecimalField(max_digits=7, decimal_places=2)
    largeur = models.DecimalField(max_digits=6, decimal_places=2)
    profondeur = models.DecimalField(max_digits=6, decimal_places=2)
    hauteur = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    rotation = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0'))
    couleur = models.CharField(max_length=7, default='#64748b')

    class Meta:
        ordering = ['id']
        verbose_name = 'élément du plan'

    def __str__(self):
        return self.nom or self.type


class ArticleQuerySet(models.QuerySet):
    def en_alerte(self):
        """« À commander » : articles actifs dont le seuil est renseigné et atteint."""
        return self.filter(actif=True, stock_min__gt=0, stock__lte=F('stock_min'))


class Article(models.Model):
    UNITES = [
        ('U', 'Unité'),
        ('KG', 'Kilogramme'),
        ('T', 'Tonne'),
        ('L', 'Litre'),
        ('M', 'Mètre'),
        ('M2', 'Mètre carré'),
        ('M3', 'Mètre cube'),
        ('SAC', 'Sac'),
        ('FUT', 'Fût'),
        ('BID', 'Bidon'),
        ('BOITE', 'Boîte'),
        ('JEU', 'Jeu'),
        ('ROUL', 'Rouleau'),
        ('PAIRE', 'Paire'),
        ('CARTON', 'Carton'),
        ('PAQ', 'Paquet'),
        ('BARRE', 'Barre'),
        ('LOT', 'Lot'),
        ('FEUILLE', 'Feuille'),
    ]

    code = models.CharField(max_length=50, unique=True)
    designation = models.CharField('désignation', max_length=200)
    categorie = models.ForeignKey(Categorie, on_delete=models.PROTECT, verbose_name='catégorie')
    unite = models.CharField('unité', max_length=10, choices=UNITES, default='U')
    etagere = models.ForeignKey(Etagere, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='articles', verbose_name='étagère')
    niveau = models.PositiveSmallIntegerField(null=True, blank=True, help_text='1 = niveau du bas.')
    case = models.CharField(max_length=30, blank=True, help_text='Précision facultative : case, bac, côté…')
    reference_fabricant = models.CharField('référence fabricant', max_length=100, blank=True)
    mots_cles = models.CharField('mots-clés', max_length=255, blank=True,
                                 help_text='Autres noms utilisés au magasin (français, darija…), séparés par des virgules.')
    photo = models.ImageField(upload_to='articles/', blank=True)
    stock_min = models.DecimalField('stock minimum', default=0, validators=POSITIF, **QTE,
                                    help_text="Une alerte s'affiche quand le stock atteint ce seuil.")
    # Mis à jour uniquement par la validation des bons (voir services.py).
    stock = models.DecimalField('stock actuel', default=0, editable=False, **QTE)
    prix_moyen = models.DecimalField('prix moyen pondéré', default=0, editable=False, **PRIX)
    actif = models.BooleanField(default=True)

    objects = ArticleQuerySet.as_manager()

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'{self.code} – {self.designation}'

    @property
    def valeur_stock(self):
        return (self.stock * self.prix_moyen).quantize(Decimal('1'))

    @property
    def en_alerte(self):
        return self.actif and self.stock_min > 0 and self.stock <= self.stock_min

    @property
    def emplacement(self):
        """Texte lisible : « Bloc A › Étagère R2 › Niveau 3 › case 4 »."""
        if not self.etagere_id:
            return ''
        parties = [f'Bloc {self.etagere.bloc.code}', f'Étagère {self.etagere.code}']
        if self.niveau:
            parties.append(f'Niveau {self.niveau}')
        if self.case:
            parties.append(self.case)
        return ' › '.join(parties)


# =========================================================
# Bons (entrée, sortie, inventaire)
# =========================================================

class Bon(models.Model):
    BROUILLON, VALIDE, ANNULE = 'BROUILLON', 'VALIDE', 'ANNULE'
    STATUTS = [
        (BROUILLON, 'Brouillon'),
        (VALIDE, 'Validé'),
        (ANNULE, 'Annulé'),
    ]
    PREFIXE = 'BON'

    numero = models.CharField('numéro', max_length=30, unique=True, editable=False, blank=True)
    date = models.DateTimeField(default=timezone.now)
    statut = models.CharField(max_length=10, choices=STATUTS, default=BROUILLON, editable=False)
    observation = models.TextField(blank=True)
    cree_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True,
                                 editable=False, related_name='+', verbose_name='créé par')
    valide_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True,
                                   editable=False, related_name='+', verbose_name='validé par')
    date_validation = models.DateTimeField(null=True, editable=False)
    # Jeton du formulaire qui a créé le bon : un deuxième envoi du même formulaire ne crée pas un second bon.
    jeton = models.CharField(max_length=32, null=True, blank=True, unique=True, editable=False)

    class Meta:
        abstract = True
        ordering = ['-date', '-id']

    def __str__(self):
        return self.numero or f'{self.PREFIXE} (nouveau)'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.numero:
            self.numero = f'{self.PREFIXE}-{self.date:%Y}-{self.pk:05d}'
            super().save(update_fields=['numero'])

    @property
    def modifiable(self):
        return self.statut == self.BROUILLON


class BonEntree(Bon):
    PREFIXE = 'BE'

    fournisseur = models.ForeignKey(Fournisseur, on_delete=models.PROTECT, null=True, blank=True)
    reference = models.CharField('réf. BL / facture', max_length=100, blank=True)

    class Meta(Bon.Meta):
        verbose_name = "bon d'entrée"
        verbose_name_plural = "bons d'entrée"

    def total(self):
        return sum((l.montant for l in self.lignes.all()), Decimal('0'))


class LigneEntree(models.Model):
    bon = models.ForeignKey(BonEntree, on_delete=models.CASCADE, related_name='lignes')
    article = models.ForeignKey(Article, on_delete=models.PROTECT)
    quantite = models.DecimalField('quantité', validators=STRICT_POSITIF, **QTE)
    prix_unitaire = models.DecimalField('prix unitaire', default=0, validators=POSITIF, **PRIX)

    class Meta:
        verbose_name = 'ligne'

    def __str__(self):
        return f'{self.article.code} × {self.quantite}'

    @property
    def montant(self):
        return (self.quantite or 0) * (self.prix_unitaire or 0)


class BonSortie(Bon):
    PREFIXE = 'BS'

    chantier = models.ForeignKey(Chantier, on_delete=models.PROTECT)
    engin = models.ForeignKey(Engin, on_delete=models.PROTECT, null=True, blank=True,
                              help_text='Facultatif : engin ou camion destinataire.')
    demandeur = models.CharField(max_length=150)

    class Meta(Bon.Meta):
        verbose_name = 'bon de sortie'
        verbose_name_plural = 'bons de sortie'

    def total(self):
        return sum((l.montant for l in self.lignes.all()), Decimal('0'))


class LigneSortie(models.Model):
    bon = models.ForeignKey(BonSortie, on_delete=models.CASCADE, related_name='lignes')
    article = models.ForeignKey(Article, on_delete=models.PROTECT)
    quantite = models.DecimalField('quantité', validators=STRICT_POSITIF, **QTE)
    # Prix moyen de l'article figé au moment de la validation.
    prix_unitaire = models.DecimalField('prix unitaire', default=0, editable=False, **PRIX)

    class Meta:
        verbose_name = 'ligne'

    def __str__(self):
        return f'{self.article.code} × {self.quantite}'

    @property
    def montant(self):
        return (self.quantite or 0) * (self.prix_unitaire or 0)


class Inventaire(Bon):
    PREFIXE = 'INV'

    class Meta(Bon.Meta):
        verbose_name = 'inventaire'

    def total(self):
        return sum((l.ecart * l.article.prix_moyen for l in self.lignes.all()), Decimal('0'))


class LigneInventaire(models.Model):
    bon = models.ForeignKey(Inventaire, on_delete=models.CASCADE, related_name='lignes')
    article = models.ForeignKey(Article, on_delete=models.PROTECT)
    stock_compte = models.DecimalField('stock compté', validators=POSITIF, **QTE)
    # Stock du logiciel figé au moment de la validation.
    stock_theorique = models.DecimalField('stock théorique', null=True, editable=False, **QTE)

    class Meta:
        verbose_name = 'ligne'
        constraints = [
            models.UniqueConstraint(fields=['bon', 'article'], name='inventaire_article_unique'),
        ]

    def __str__(self):
        return f'{self.article.code} : {self.stock_compte}'

    @property
    def ecart(self):
        theorique = self.stock_theorique if self.stock_theorique is not None else self.article.stock
        return (self.stock_compte or 0) - theorique


# =========================================================
# Journal des mouvements (historique, lecture seule)
# =========================================================

class MouvementStock(models.Model):
    ENTREE, SORTIE, INVENTAIRE, ANNULATION = 'ENTREE', 'SORTIE', 'INVENTAIRE', 'ANNULATION'
    TYPES = [
        (ENTREE, 'Entrée'),
        (SORTIE, 'Sortie'),
        (INVENTAIRE, "Ajustement d'inventaire"),
        (ANNULATION, 'Annulation'),
    ]

    date = models.DateTimeField(default=timezone.now)
    article = models.ForeignKey(Article, on_delete=models.PROTECT, related_name='mouvements')
    type = models.CharField(max_length=12, choices=TYPES)
    quantite = models.DecimalField('quantité', help_text='Positive en entrée, négative en sortie.', **QTE)
    stock_apres = models.DecimalField('stock après', **QTE)
    prix_unitaire = models.DecimalField('prix unitaire', default=0, **PRIX)
    reference = models.CharField('bon', max_length=30)
    chantier = models.ForeignKey(Chantier, on_delete=models.PROTECT, null=True, blank=True)
    engin = models.ForeignKey(Engin, on_delete=models.PROTECT, null=True, blank=True)
    utilisateur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True)

    class Meta:
        ordering = ['-date', '-id']
        verbose_name = 'mouvement de stock'
        verbose_name_plural = 'mouvements de stock'

    def __str__(self):
        return f'{self.reference} | {self.article.code} | {self.quantite:+}'


# =========================================================
# Paramètres de la société (logiciel vendu à plusieurs sociétés)
# =========================================================

class Parametres(models.Model):
    """Une seule ligne : nom, logo et couleur de la société qui utilise le logiciel."""
    nom_societe = models.CharField('nom de la société', max_length=150, blank=True)
    logo = models.ImageField(upload_to='societe/', blank=True)
    couleur = models.CharField('couleur principale', max_length=7, default='#2457d6')
    adresse = models.CharField(max_length=255, blank=True)
    telephone = models.CharField('téléphone', max_length=60, blank=True)
    devise = models.CharField(max_length=10, default='FCFA')
    signataire = models.CharField('signature des bons', max_length=100, default='Le magasinier',
                                  help_text='Texte sous la case de signature, sur les bons imprimés.')
    rccm = models.CharField('RCCM', max_length=60, blank=True,
                            help_text='N° du registre du commerce, imprimé en haut des bons.')
    ncc = models.CharField('N° compte contribuable (NCC)', max_length=30, blank=True)
    masquer_prix_sortie = models.BooleanField('Ne pas imprimer les prix sur les bons de sortie', default=False)
    debut_essai = models.DateField("début de l'essai", null=True, blank=True)
    cle_licence = models.TextField("clé d'activation", blank=True)

    CLE_CACHE = 'parametres-societe'

    class Meta:
        verbose_name = 'paramètres de la société'
        verbose_name_plural = 'paramètres de la société'

    def __str__(self):
        return self.nom_societe or 'Ma société'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        from django.core.cache import cache
        cache.delete(self.CLE_CACHE)

    @classmethod
    def actuels(cls):
        from django.core.cache import cache
        parametres = cache.get(cls.CLE_CACHE)
        if parametres is None:
            parametres = cls.objects.filter(pk=1).first() or cls(pk=1)
            cache.set(cls.CLE_CACHE, parametres, 300)
        return parametres
