from decimal import Decimal, InvalidOperation

from django import forms

from .models import Article, Categorie, Etagere


def decimal_saisi(texte):
    """« 1 250,5 » → Decimal('1250.5') ; None si vide ou invalide."""
    texte = (texte or '').replace(' ', '').replace('\xa0', '').replace(' ', '').replace(',', '.')
    if not texte:
        return None
    try:
        valeur = Decimal(texte)
    except InvalidOperation:
        return None
    return valeur if valeur.is_finite() else None


class ChoixEtagere(forms.ModelChoiceField):
    def label_from_instance(self, etagere):
        return f'Étagère {etagere.code} ({etagere.nb_niveaux} niveaux)'


class ArticleForm(forms.ModelForm):
    categorie_nom = forms.CharField(label='Catégorie', max_length=100,
                                    help_text='Choisissez dans la liste ou tapez une nouvelle catégorie.')
    etagere = ChoixEtagere(queryset=Etagere.objects.select_related('bloc'), required=False,
                           label='Étagère', empty_label='— Pas encore rangé —')
    stock_initial = forms.CharField(label='Stock de départ', required=False,
                                    help_text='Quantité déjà présente au magasin (facultatif).')
    prix_initial = forms.CharField(label="Prix d'achat unitaire", required=False,
                                   help_text='Sert à calculer la valeur du stock (facultatif).')

    class Meta:
        model = Article
        fields = ['code', 'designation', 'unite', 'reference_fabricant', 'mots_cles',
                  'etagere', 'niveau', 'case', 'stock_min', 'photo', 'actif']
        labels = {'stock_min': "Seuil d'alerte (stock minimum)", 'mots_cles': 'Autres noms utilisés'}
        help_texts = {
            'mots_cles': 'Comment les gens l\'appellent au magasin, séparés par des virgules. Ex. : filtre zit, filtre huile.',
            'stock_min': 'Quand le stock descend à ce chiffre, l\'article passe « À commander ».',
            'niveau': '1 = en bas.',
        }
        widgets = {
            'mots_cles': forms.TextInput(),
            'stock_min': forms.TextInput(attrs={'inputmode': 'decimal'}),
            'niveau': forms.NumberInput(attrs={'min': 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['categorie_nom'].initial = self.instance.categorie.nom
            del self.fields['stock_initial']
            del self.fields['prix_initial']
        self.fields['categorie_nom'].widget.attrs['list'] = 'liste-categories'
        # Étagères regroupées par bloc dans la liste déroulante.
        groupes = {}
        for e in self.fields['etagere'].queryset:
            groupes.setdefault(str(e.bloc), []).append((e.pk, self.fields['etagere'].label_from_instance(e)))
        self.fields['etagere'].choices = [('', '— Pas encore rangé —')] + list(groupes.items())

    def _decimal(self, nom, positif=True):
        valeur = decimal_saisi(self.cleaned_data.get(nom))
        if self.cleaned_data.get(nom) and valeur is None:
            raise forms.ValidationError('Nombre invalide.')
        if valeur is not None and positif and valeur < 0:
            raise forms.ValidationError('Le nombre doit être positif.')
        return valeur

    def clean_stock_initial(self):
        return self._decimal('stock_initial')

    def clean_prix_initial(self):
        return self._decimal('prix_initial')

    def clean_niveau(self):
        niveau = self.cleaned_data.get('niveau')
        etagere = self.cleaned_data.get('etagere')
        if niveau and etagere and niveau > etagere.nb_niveaux:
            raise forms.ValidationError(f"Cette étagère n'a que {etagere.nb_niveaux} niveaux.")
        return niveau

    def save(self, commit=True):
        nom = self.cleaned_data['categorie_nom'].strip()
        categorie = Categorie.objects.filter(nom__iexact=nom).first() or Categorie.objects.create(nom=nom)
        self.instance.categorie = categorie
        return super().save(commit)
