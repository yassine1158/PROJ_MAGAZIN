import io
import re
import unicodedata
from decimal import Decimal
from pathlib import Path

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile, UploadedFile

from .models import Article, Bloc, Categorie, Chantier, Engin, Etagere, Fournisseur, Parametres
from .utils import nombre, quantite

QTE_MAXI = Decimal('1000000000')     # 1 milliard : reste loin de la limite des colonnes (11 chiffres)
PRIX_MAXI = Decimal('10000000000')   # 10 milliards
PHOTO_MAX_OCTETS = 15 * 1024 * 1024
PHOTO_MAX_COTE = 1600


def nombre_saisi(texte, decimales=3, maxi=QTE_MAXI, code_barre=True):
    """Nombre tapé dans une case : « 1 250,5 » → Decimal('1250.5') ; None si la case est vide.

    Refuse (forms.ValidationError, message en français) : texte qui n'est pas un nombre, nombre négatif,
    trop grand, trop de chiffres après la virgule, ou code-barres scanné par erreur (code_barre=True).
    """
    brut = str(texte if texte is not None else '').strip()
    if not brut:
        return None
    if code_barre and re.fullmatch(r'\d{8,}', brut):
        raise forms.ValidationError('Ceci ressemble à un code-barres. Tapez la quantité dans cette case.')
    texte = re.sub(r'\s', '', brut).replace(',', '.')
    if not re.fullmatch(r'[+-]?(\d+\.?\d*|\.\d+)', texte):
        raise forms.ValidationError(f'« {brut[:20]} » n\'est pas un nombre.')
    valeur = Decimal(texte)
    if valeur < 0:
        raise forms.ValidationError('Le nombre ne peut pas être négatif.')
    if valeur > maxi:
        raise forms.ValidationError(f'Nombre trop grand ({nombre(maxi)} au maximum).')
    if valeur != valeur.quantize(Decimal(1).scaleb(-decimales)):
        if not decimales:
            raise forms.ValidationError('Tapez un nombre entier, sans virgule.')
        raise forms.ValidationError(f'{decimales} chiffres après la virgule au maximum.')
    return abs(valeur)  # « -0 » devient 0


def prix_saisi(texte):
    return nombre_saisi(texte, decimales=2, maxi=PRIX_MAXI, code_barre=False)


def decimal_saisi(texte):
    """Ancienne aide, gardée pour compatibilité : None si la case est vide ou la saisie refusée."""
    try:
        return nombre_saisi(texte, maxi=PRIX_MAXI, code_barre=False)
    except forms.ValidationError:
        return None


def code_article_automatique(categorie):
    """Code d'un nouvel article sans code : 3 premières lettres de la catégorie + numéro (MAT-0012)."""
    lettres = ''.join(c for c in unicodedata.normalize('NFKD', categorie or '') if c.isascii() and c.isalpha())
    prefixe = lettres[:3].upper() or 'ART'
    numeros = [int(m.group(1)) for code in Article.objects.filter(code__istartswith=f'{prefixe}-')
               .values_list('code', flat=True) if (m := re.fullmatch(rf'{prefixe}-(\d+)', code, re.IGNORECASE))]
    n = max(numeros, default=0) + 1
    while Article.objects.filter(code__iexact=f'{prefixe}-{n:04d}').exists():
        n += 1
    return f'{prefixe}-{n:04d}'


def photo_reduite(fichier, cote=PHOTO_MAX_COTE):
    """Photo ramenée à 1600 px de côté au plus, en JPEG qualité 85 (pages légères sur téléphone)."""
    from PIL import Image, ImageOps

    fichier.seek(0)
    with Image.open(fichier) as originale:
        originale.draft('RGB', (cote, cote))  # JPEG : décodage directement en plus petit
        image = ImageOps.exif_transpose(originale)
        if image.mode in ('RGBA', 'LA', 'P', 'PA'):
            image = image.convert('RGBA')
            fond = Image.new('RGB', image.size, 'white')
            fond.paste(image, mask=image.getchannel('A'))
            image = fond
        else:
            image = image.convert('RGB')
        image.thumbnail((cote, cote))
        sortie = io.BytesIO()
        image.save(sortie, 'JPEG', quality=85, optimize=True)
    return SimpleUploadedFile(f'{Path(fichier.name).stem[:80] or "photo"}.jpg', sortie.getvalue(),
                              content_type='image/jpeg')


class ChoixEtagere(forms.ModelChoiceField):
    def label_from_instance(self, etagere):
        return f'Étagère {etagere.code} ({etagere.nb_niveaux} niveaux)'


class ArticleForm(forms.ModelForm):
    categorie_nom = forms.CharField(label='Catégorie', max_length=100,
                                    help_text='Choisissez dans la liste ou tapez une nouvelle catégorie.')
    etagere = ChoixEtagere(queryset=Etagere.objects.select_related('bloc'), required=False,
                           label='Étagère', empty_label='— Pas encore rangé —')
    stock_min = forms.CharField(label="Seuil d'alerte (stock minimum)", required=False,
                                widget=forms.TextInput(attrs={'inputmode': 'decimal'}),
                                help_text="Quand le stock descend à ce chiffre, l'article passe « À commander ». "
                                          "0 ou vide : pas d'alerte.")
    stock_initial = forms.CharField(label='Stock de départ', required=False,
                                    widget=forms.TextInput(attrs={'inputmode': 'decimal'}),
                                    help_text='Quantité déjà présente au magasin (facultatif).')
    prix_initial = forms.CharField(label="Prix d'achat unitaire", required=False,
                                   widget=forms.TextInput(attrs={'inputmode': 'decimal'}),
                                   help_text='Sert à calculer la valeur du stock (facultatif).')

    class Meta:
        model = Article
        fields = ['code', 'designation', 'unite', 'reference_fabricant', 'mots_cles',
                  'etagere', 'niveau', 'case', 'stock_min', 'photo', 'actif']
        labels = {'mots_cles': 'Autres noms utilisés'}
        help_texts = {
            'mots_cles': 'Comment les gens l\'appellent au magasin, séparés par des virgules. Ex. : filtre zit, filtre huile.',
            'niveau': '1 = en bas.',
        }
        widgets = {
            'mots_cles': forms.TextInput(),
            'niveau': forms.NumberInput(attrs={'min': 1}),
            'photo': forms.ClearableFileInput(attrs={'accept': 'image/*'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['categorie_nom'].initial = self.instance.categorie.nom
            del self.fields['stock_initial']
            del self.fields['prix_initial']
            if self.instance.mouvements.exists():
                # Tout l'historique est compté dans cette unité : la changer fausserait les quantités passées.
                self.fields['unite'].disabled = True
                self.fields['unite'].help_text = ('Ne peut plus changer : le stock est déjà compté dans cette unité. '
                                                  'Créez un nouvel article si besoin.')
        else:
            self.fields['code'].required = False
            self.fields['code'].help_text = 'Laissez vide : un code est créé tout seul (ex. MAT-0012).'
        self.initial['stock_min'] = quantite(self.instance.stock_min)  # « 100 » et non « 100,000 »
        self.fields['categorie_nom'].widget.attrs['list'] = 'liste-categories'
        # Étagères regroupées par bloc dans la liste déroulante.
        groupes = {}
        for e in self.fields['etagere'].queryset:
            groupes.setdefault(str(e.bloc), []).append((e.pk, self.fields['etagere'].label_from_instance(e)))
        self.fields['etagere'].choices = [('', '— Pas encore rangé —')] + list(groupes.items())

    def clean_code(self):
        code = self.cleaned_data.get('code', '').strip()
        autre = Article.objects.filter(code__iexact=code).exclude(pk=self.instance.pk).first() if code else None
        if autre:
            raise forms.ValidationError(f'Ce code existe déjà : {autre.designation} ({autre.code}).')
        return code

    def clean_actif(self):
        actif = self.cleaned_data.get('actif')
        stock = self.instance.stock
        if self.instance.pk and self.instance.actif and not actif and stock != 0:
            raise forms.ValidationError(
                f'Il reste {quantite(stock)} {self.instance.unite} en stock : mettez d\'abord le stock à 0 '
                'par une correction, puis désactivez l\'article.')
        return actif

    def clean_stock_min(self):
        return nombre_saisi(self.cleaned_data.get('stock_min')) or Decimal('0')

    def clean_stock_initial(self):
        return nombre_saisi(self.cleaned_data.get('stock_initial'))

    def clean_prix_initial(self):
        return prix_saisi(self.cleaned_data.get('prix_initial'))

    def clean_photo(self):
        photo = self.cleaned_data.get('photo')
        if not isinstance(photo, UploadedFile):
            return photo  # pas de nouvelle photo
        if photo.size > PHOTO_MAX_OCTETS:
            raise forms.ValidationError('Photo trop lourde : 15 Mo au maximum.')
        try:
            return photo_reduite(photo)
        except Exception:
            raise forms.ValidationError('Cette photo ne peut pas être lue. Essayez une autre image (JPG ou PNG).')

    def clean_niveau(self):
        niveau = self.cleaned_data.get('niveau')
        etagere = self.cleaned_data.get('etagere')
        if niveau and etagere and niveau > etagere.nb_niveaux:
            raise forms.ValidationError(f"Cette étagère n'a que {etagere.nb_niveaux} niveaux.")
        if niveau and niveau > 99:
            raise forms.ValidationError("Ce niveau n'existe pas : 1 = en bas.")
        return niveau

    def save(self, commit=True):
        nom = self.cleaned_data['categorie_nom'].strip()
        categorie = Categorie.objects.filter(nom__iexact=nom).first() or Categorie.objects.create(nom=nom)
        self.instance.categorie = categorie
        if not self.instance.code:
            self.instance.code = code_article_automatique(categorie.nom)
        return super().save(commit)


class ParametresForm(forms.ModelForm):
    class Meta:
        model = Parametres
        fields = ['nom_societe', 'logo', 'couleur', 'adresse', 'telephone', 'devise', 'signataire',
                  'rccm', 'ncc', 'masquer_prix_sortie']
        widgets = {'couleur': forms.TextInput(attrs={'type': 'color'})}
        help_texts = {
            'logo': 'Affiché dans le logiciel et sur les bons imprimés (PNG ou JPG).',
            'couleur': 'Couleur des boutons et du menu.',
            'devise': 'Ex. : FCFA, TND, EUR.',
            'signataire': 'Titre de la case de signature du magasin, sur les bons imprimés.',
            'rccm': 'Ex. : CI-ABJ-2019-B-12345. Imprimé en haut des bons.',
            'ncc': 'Ex. : 1912345 K. Imprimé en haut des bons.',
        }

    def clean_couleur(self):
        couleur = self.cleaned_data['couleur']
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', couleur or ''):
            raise forms.ValidationError('Couleur invalide.')
        return couleur.lower()


class SocieteDepartForm(ParametresForm):
    """Écran de premier lancement : seulement le nom et le logo."""
    class Meta(ParametresForm.Meta):
        fields = ['nom_societe', 'logo']


# =========================================================
# Réglages : plan du magasin, listes, utilisateurs
# =========================================================

class BlocForm(forms.ModelForm):
    class Meta:
        model = Bloc
        fields = ['code', 'nom', 'couleur', 'x', 'z', 'largeur', 'profondeur']
        localized_fields = ['x', 'z', 'largeur', 'profondeur']  # accepte « 2,5 » comme « 2.5 »
        labels = {'x': 'Position depuis la gauche (m)', 'z': "Position depuis l'entrée (m)"}
        widgets = {'couleur': forms.TextInput(attrs={'type': 'color'})}


class EtagereForm(forms.ModelForm):
    class Meta:
        model = Etagere
        fields = ['code', 'x', 'z', 'largeur', 'profondeur', 'hauteur', 'nb_niveaux', 'tournee']
        localized_fields = ['x', 'z', 'largeur', 'profondeur', 'hauteur']
        labels = {'x': 'Position dans le bloc, depuis la gauche (m)', 'z': "Position dans le bloc, depuis l'avant (m)",
                  'tournee': 'Tournée de 90° (posée dans la profondeur)'}

    def __init__(self, *args, bloc=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.bloc = bloc or self.instance.bloc

    def clean_code(self):
        code = self.cleaned_data['code'].strip()
        doublon = Etagere.objects.filter(bloc=self.bloc, code__iexact=code).exclude(pk=self.instance.pk)
        if doublon.exists():
            raise forms.ValidationError(f'Le bloc {self.bloc.code} a déjà une étagère {code}.')
        return code

    def save(self, commit=True):
        self.instance.bloc = self.bloc
        return super().save(commit)


class RangeeForm(forms.Form):
    """Ajoute plusieurs étagères identiques alignées, en une fois."""
    prefixe = forms.CharField(label='Début du code', max_length=10, initial='E',
                              help_text='Les étagères seront nommées E1, E2, E3…')
    debut = forms.IntegerField(label='Premier numéro', min_value=0, initial=1)
    nombre = forms.IntegerField(label="Nombre d'étagères", min_value=1, max_value=50, initial=4)
    x = forms.DecimalField(localize=True, label='Position de la première, depuis la gauche (m)', min_value=0, initial=1)
    z = forms.DecimalField(localize=True, label="Position depuis l'avant du bloc (m)", min_value=0, initial=1)
    largeur = forms.DecimalField(localize=True, label='Largeur de chaque étagère (m)', min_value=Decimal('0.3'), initial=2)
    profondeur = forms.DecimalField(localize=True, label='Profondeur (m)', min_value=Decimal('0.2'), initial=Decimal('0.9'))
    hauteur = forms.DecimalField(localize=True, label='Hauteur (m)', min_value=Decimal('0.5'), initial=Decimal('2.4'))
    nb_niveaux = forms.IntegerField(label='Niveaux', min_value=1, max_value=20, initial=4)
    espace = forms.DecimalField(localize=True, label='Espace entre deux étagères (m)', min_value=0, initial=Decimal('0.2'))

    def __init__(self, *args, bloc, **kwargs):
        super().__init__(*args, **kwargs)
        self.bloc = bloc

    def clean(self):
        donnees = super().clean()
        if self.errors:
            return donnees
        existants = {c.lower() for c in self.bloc.etageres.values_list('code', flat=True)}
        codes = [f"{donnees['prefixe']}{donnees['debut'] + i}" for i in range(donnees['nombre'])]
        deja = [c for c in codes if c.lower() in existants]
        if deja:
            raise forms.ValidationError(f"Ces codes existent déjà dans le bloc : {', '.join(deja)}.")
        donnees['codes'] = codes
        return donnees

    def creer(self):
        d = self.cleaned_data
        return [
            Etagere.objects.create(
                bloc=self.bloc, code=code, x=d['x'] + i * (d['largeur'] + d['espace']), z=d['z'],
                largeur=d['largeur'], profondeur=d['profondeur'], hauteur=d['hauteur'], nb_niveaux=d['nb_niveaux'],
            )
            for i, code in enumerate(d['codes'])
        ]


LISTES = {
    'chantiers': (Chantier, ['nom', 'actif'], 'Chantiers', 'chantier'),
    'engins': (Engin, ['code', 'designation', 'actif'], 'Engins et camions', 'engin'),
    'fournisseurs': (Fournisseur, ['nom', 'telephone', 'email', 'adresse'], 'Fournisseurs', 'fournisseur'),
    'categories': (Categorie, ['nom'], "Catégories d'articles", 'catégorie'),
}


def formulaire_liste(type_):
    modele, champs, _, _ = LISTES[type_]
    return forms.modelform_factory(modele, fields=champs)


class UtilisateurForm(forms.ModelForm):
    ROLES = [('magasinier', 'Magasinier : entrées, sorties, articles'),
             ('responsable', 'Responsable : tout, y compris les réglages')]
    role = forms.ChoiceField(label='Rôle', choices=ROLES, widget=forms.RadioSelect, initial='magasinier')
    mot_de_passe = forms.CharField(label='Mot de passe', required=False, widget=forms.PasswordInput,
                                   help_text='Au moins 8 caractères.')

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'username', 'is_active']
        labels = {'first_name': 'Prénom', 'last_name': 'Nom', 'username': "Nom d'utilisateur (pour se connecter)",
                  'is_active': 'Compte actif (décocher pour bloquer la connexion)'}
        help_texts = {'username': ''}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['role'].initial = 'responsable' if self.instance.is_staff else 'magasinier'
            self.fields['mot_de_passe'].label = 'Nouveau mot de passe'
            self.fields['mot_de_passe'].help_text = 'Laisser vide pour ne pas le changer.'
        else:
            self.fields['mot_de_passe'].required = True

    def clean_mot_de_passe(self):
        mdp = self.cleaned_data.get('mot_de_passe')
        if mdp:
            password_validation.validate_password(mdp, self.instance)
        return mdp

    def save(self, commit=True):
        utilisateur = super().save(commit=False)
        responsable = self.cleaned_data['role'] == 'responsable'
        utilisateur.is_staff = responsable
        utilisateur.is_superuser = responsable
        if self.cleaned_data.get('mot_de_passe'):
            utilisateur.set_password(self.cleaned_data['mot_de_passe'])
        if commit:
            utilisateur.save()
        return utilisateur
