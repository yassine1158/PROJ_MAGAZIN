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
    # Pour imprimer les étiquettes des nouveaux articles. Vide pour un article ancien jamais mouvementé.
    cree_le = models.DateTimeField('créé le', auto_now_add=True, null=True)

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
    annule_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True,
                                   editable=False, related_name='+', verbose_name='annulé par')
    date_annulation = models.DateTimeField("date d'annulation", null=True, editable=False)
    motif_annulation = models.CharField("motif de l'annulation", max_length=255, blank=True, editable=False)
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
    # Prix moyen de l'article au jour du bon, figé au moment de la validation.
    prix_unitaire = models.DecimalField('prix unitaire', default=0, editable=False, **PRIX)

    class Meta:
        verbose_name = 'ligne'

    def __str__(self):
        return f'{self.article.code} × {self.quantite}'

    @property
    def montant(self):
        return (self.quantite or 0) * (self.prix_unitaire or 0)


class BonRetour(Bon):
    """Marchandise non utilisée rapportée d'un chantier : le stock remonte, au coût de la sortie."""
    PREFIXE = 'BR'

    chantier = models.ForeignKey(Chantier, on_delete=models.PROTECT)
    engin = models.ForeignKey(Engin, on_delete=models.PROTECT, null=True, blank=True,
                              help_text="Facultatif : engin ou camion d'où revient la marchandise.")
    rapporte_par = models.CharField('rapporté par', max_length=150)
    sortie = models.ForeignKey(BonSortie, on_delete=models.SET_NULL, null=True, blank=True, related_name='retours',
                               verbose_name="bon de sortie d'origine")

    class Meta(Bon.Meta):
        verbose_name = 'bon de retour'
        verbose_name_plural = 'bons de retour'

    def total(self):
        return sum((l.montant for l in self.lignes.all()), Decimal('0'))


class LigneRetour(models.Model):
    bon = models.ForeignKey(BonRetour, on_delete=models.CASCADE, related_name='lignes')
    article = models.ForeignKey(Article, on_delete=models.PROTECT)
    quantite = models.DecimalField('quantité', validators=STRICT_POSITIF, **QTE)
    # Coût de la sortie (prix de la sortie d'origine, sinon coût moyen des sorties vers ce chantier), figé à la
    # validation : le retour retire de la consommation la valeur que la sortie y avait mise.
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

    # Ce qui est compté (« Tout le magasin », « Bloc A – Pièces »…) ; vide pour la correction d'un seul article.
    perimetre = models.CharField('périmètre', max_length=150, blank=True, editable=False)

    class Meta(Bon.Meta):
        verbose_name = 'inventaire'

    def total(self):
        return sum((l.ecart * l.article.prix_moyen for l in self.lignes.all() if l.ecart is not None), Decimal('0'))


class LigneInventaire(models.Model):
    bon = models.ForeignKey(Inventaire, on_delete=models.CASCADE, related_name='lignes')
    article = models.ForeignKey(Article, on_delete=models.PROTECT)
    # Vide = pas encore compté (la ligne est ignorée à la validation).
    stock_compte = models.DecimalField('stock compté', null=True, blank=True, validators=POSITIF, **QTE)
    # Moment où la quantité comptée a été saisie : les mouvements faits après sont repris à la validation.
    compte_le = models.DateTimeField('compté le', null=True, blank=True, editable=False)
    # Stock du logiciel au moment du comptage, calculé à la validation (écart = compté − théorique).
    stock_theorique = models.DecimalField('stock théorique', null=True, editable=False, **QTE)

    class Meta:
        verbose_name = 'ligne'
        constraints = [
            models.UniqueConstraint(fields=['bon', 'article'], name='inventaire_article_unique'),
        ]

    def __str__(self):
        return f'{self.article.code} : {"non compté" if self.stock_compte is None else self.stock_compte}'

    @property
    def ignoree(self):
        """Ligne validée sans effet sur le stock : non comptée, ou recomptée depuis par un autre inventaire."""
        return self.bon.statut == Bon.VALIDE and self.stock_theorique is None

    @property
    def ecart(self):
        if self.stock_compte is None or self.ignoree:
            return None
        theorique = self.stock_theorique if self.stock_theorique is not None else self.article.stock
        return self.stock_compte - theorique


# =========================================================
# Demandes de pièces (QR code affiché à l'atelier)
# =========================================================

class DemandePieces(models.Model):
    """Pièces demandées depuis un téléphone (QR code de l'atelier), sans compte : le magasin la valide
    (une sortie de stock est créée) ou la refuse."""
    EN_ATTENTE, VALIDEE, REFUSEE = 'EN_ATTENTE', 'VALIDEE', 'REFUSEE'
    STATUTS = [
        (EN_ATTENTE, 'En attente'),
        (VALIDEE, 'Validée'),
        (REFUSEE, 'Refusée'),
    ]
    PREFIXE = 'DP'

    numero = models.CharField('numéro', max_length=30, unique=True, editable=False, blank=True)
    date = models.DateTimeField(default=timezone.now)
    demandeur = models.CharField(max_length=150)
    telephone = models.CharField('téléphone', max_length=30, blank=True)
    chantier = models.ForeignKey(Chantier, on_delete=models.SET_NULL, null=True, blank=True)
    engin = models.ForeignKey(Engin, on_delete=models.SET_NULL, null=True, blank=True)
    # Chantier ou engin tapé par le demandeur quand il n'est pas dans la liste.
    chantier_texte = models.CharField('chantier (tapé)', max_length=150, blank=True)
    engin_texte = models.CharField('engin (tapé)', max_length=50, blank=True)
    observation = models.TextField(blank=True)
    statut = models.CharField(max_length=10, choices=STATUTS, default=EN_ATTENTE, editable=False)
    traite_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, editable=False,
                                   related_name='+', verbose_name='traitée par')
    date_traitement = models.DateTimeField('traitée le', null=True, editable=False)
    motif_refus = models.CharField('motif du refus', max_length=255, blank=True, editable=False)
    sortie = models.ForeignKey(BonSortie, on_delete=models.SET_NULL, null=True, blank=True, editable=False,
                               related_name='demandes', verbose_name='bon de sortie')
    # Lien de suivi donné au demandeur (page « où en est ma demande ? ») ; sert aussi contre le double envoi.
    jeton = models.CharField(max_length=32, unique=True, editable=False)

    class Meta:
        verbose_name = 'demande de pièces'
        verbose_name_plural = 'demandes de pièces'
        ordering = ['-date', '-id']

    def __str__(self):
        return self.numero or f'{self.PREFIXE} (nouvelle)'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.numero:
            self.numero = f'{self.PREFIXE}-{self.date:%Y}-{self.pk:05d}'
            super().save(update_fields=['numero'])

    @property
    def en_attente(self):
        return self.statut == self.EN_ATTENTE

    @property
    def nom_chantier(self):
        return self.chantier.nom if self.chantier else self.chantier_texte

    @property
    def nom_engin(self):
        return self.engin.code if self.engin else self.engin_texte


class LigneDemande(models.Model):
    demande = models.ForeignKey(DemandePieces, on_delete=models.CASCADE, related_name='lignes')
    # Vide : pièce hors catalogue, décrite par le demandeur (à acheter).
    article = models.ForeignKey(Article, on_delete=models.SET_NULL, null=True, blank=True)
    designation = models.CharField('désignation', max_length=200, blank=True)
    quantite = models.DecimalField('quantité', validators=STRICT_POSITIF, **QTE)

    class Meta:
        verbose_name = 'ligne'
        ordering = ['id']

    def __str__(self):
        return f'{self.libelle} × {self.quantite}'

    @property
    def libelle(self):
        return self.article.designation if self.article else self.designation


# =========================================================
# Journal des mouvements (historique, lecture seule)
# =========================================================

class MouvementStock(models.Model):
    ENTREE, SORTIE, INVENTAIRE, ANNULATION = 'ENTREE', 'SORTIE', 'INVENTAIRE', 'ANNULATION'
    RETOUR = 'RETOUR'
    TYPES = [
        (ENTREE, 'Entrée'),
        (SORTIE, 'Sortie'),
        (RETOUR, 'Retour de chantier'),
        (INVENTAIRE, "Ajustement d'inventaire"),
        (ANNULATION, 'Annulation'),
    ]

    date = models.DateTimeField(default=timezone.now)
    article = models.ForeignKey(Article, on_delete=models.PROTECT, related_name='mouvements')
    type = models.CharField(max_length=12, choices=TYPES)
    quantite = models.DecimalField('quantité', help_text='Positive en entrée, négative en sortie.', **QTE)
    stock_apres = models.DecimalField('stock après', **QTE)
    prix_unitaire = models.DecimalField('prix unitaire', default=0, **PRIX)
    # Prix moyen de l'article juste après le mouvement (valeur du stock à une date passée).
    # Vide pour les mouvements enregistrés avant son ajout, quand l'historique n'a pas permis de le retrouver.
    prix_moyen_apres = models.DecimalField('prix moyen après', null=True, blank=True, editable=False, **PRIX)
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
    premiers_pas_masques = models.BooleanField('guide « Bien démarrer » masqué', default=False)
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
