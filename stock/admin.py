from django.contrib import admin, messages
from django.db.models import F
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from . import exports, services
from .models import (
    Article, Bloc, BonEntree, BonSortie, Categorie, Chantier, Engin, Etagere, Fournisseur,
    Inventaire, LigneEntree, LigneInventaire, LigneSortie, MouvementStock,
)
from .pdf import reponse_pdf
from .utils import nombre, quantite

COULEURS_STATUT = {'BROUILLON': '#f59e0b', 'VALIDE': '#16a34a', 'ANNULE': '#dc2626'}


def badge(texte, couleur):
    return format_html(
        '<span style="background:{};color:#fff;padding:3px 10px;border-radius:999px;font-weight:600;'
        'font-size:12px;white-space:nowrap">{}</span>', couleur, texte)


# =========================================================
# Référentiels
# =========================================================

@admin.register(Categorie)
class CategorieAdmin(admin.ModelAdmin):
    search_fields = ('nom',)


@admin.register(Fournisseur)
class FournisseurAdmin(admin.ModelAdmin):
    list_display = ('nom', 'telephone', 'email')
    search_fields = ('nom', 'telephone', 'email')


@admin.register(Chantier)
class ChantierAdmin(admin.ModelAdmin):
    list_display = ('nom', 'actif')
    list_filter = ('actif',)
    search_fields = ('nom',)


@admin.register(Engin)
class EnginAdmin(admin.ModelAdmin):
    list_display = ('code', 'designation', 'actif')
    list_filter = ('actif',)
    search_fields = ('code', 'designation')


class EtagereInline(admin.TabularInline):
    model = Etagere
    extra = 1
    fields = ('code', 'x', 'z', 'largeur', 'profondeur', 'hauteur', 'nb_niveaux', 'tournee')


@admin.register(Bloc)
class BlocAdmin(admin.ModelAdmin):
    list_display = ('code', 'nom', 'couleur_apercu', 'nb_etageres', 'voir_3d')
    search_fields = ('code', 'nom')
    inlines = [EtagereInline]

    @admin.display(description='Couleur')
    def couleur_apercu(self, obj):
        return format_html('<span style="display:inline-block;width:28px;height:16px;border-radius:4px;'
                           'background:{}"></span>', obj.couleur)

    @admin.display(description='Étagères')
    def nb_etageres(self, obj):
        return obj.etageres.count()

    @admin.display(description='')
    def voir_3d(self, obj):
        return format_html('<a href="{}?bloc={}">Voir en 3D</a>', reverse('stock:recherche'), obj.code)


@admin.register(Etagere)
class EtagereAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'bloc', 'nb_niveaux', 'nb_articles')
    list_filter = ('bloc',)
    search_fields = ('code', 'bloc__code')

    @admin.display(description='Articles')
    def nb_articles(self, obj):
        return obj.articles.count()


# =========================================================
# Articles
# =========================================================

class AlerteFilter(admin.SimpleListFilter):
    title = 'alerte stock'
    parameter_name = 'alerte'

    def lookups(self, request, model_admin):
        return [('oui', 'En alerte (≤ stock min)'), ('rupture', 'En rupture (0)')]

    def queryset(self, request, queryset):
        if self.value() == 'oui':
            return queryset.filter(stock__lte=F('stock_min'))
        if self.value() == 'rupture':
            return queryset.filter(stock__lte=0)
        return queryset


@admin.action(description='📊 Exporter en Excel')
def exporter_articles(modeladmin, request, queryset):
    return exports.etat_stock(queryset.select_related('categorie', 'etagere__bloc'))


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ('miniature', 'code', 'designation', 'categorie', 'emplacement_affiche', 'stock_affiche',
                    'stock_min_affiche', 'prix_moyen_affiche', 'valeur_affichee', 'alerte')
    list_display_links = ('code', 'designation')
    list_filter = (AlerteFilter, 'categorie', 'etagere__bloc', 'actif')
    search_fields = ('code', 'designation', 'reference_fabricant', 'mots_cles')
    list_select_related = ('categorie', 'etagere__bloc')
    autocomplete_fields = ('categorie', 'etagere')
    readonly_fields = ('stock_affiche', 'prix_moyen_affiche', 'valeur_affichee', 'photo_grande', 'historique')
    actions = [exporter_articles]
    fieldsets = (
        (None, {'fields': ('code', 'designation', 'categorie', 'unite', 'reference_fabricant', 'mots_cles', 'actif')}),
        ('Emplacement', {'fields': ('etagere', 'niveau', 'case')}),
        ('Photo', {'fields': ('photo', 'photo_grande')}),
        ('Stock', {'fields': ('stock_min', 'stock_affiche', 'prix_moyen_affiche', 'valeur_affichee')}),
        ('Historique', {'fields': ('historique',)}),
    )

    @admin.display(description='')
    def miniature(self, obj):
        if not obj.photo:
            return ''
        return format_html('<img src="{}" style="width:40px;height:40px;object-fit:cover;border-radius:6px">',
                           obj.photo.url)

    @admin.display(description='')
    def photo_grande(self, obj):
        if not obj.photo:
            return '—'
        return format_html('<img src="{}" style="max-width:320px;border-radius:8px">', obj.photo.url)

    @admin.display(description='Emplacement')
    def emplacement_affiche(self, obj):
        if not obj.etagere_id:
            return '—'
        return format_html('<a href="{}?article={}">{}</a>', reverse('stock:recherche'), obj.pk, obj.emplacement)

    @admin.display(description='Stock', ordering='stock')
    def stock_affiche(self, obj):
        return f'{quantite(obj.stock)} {obj.unite}'

    @admin.display(description='Stock min', ordering='stock_min')
    def stock_min_affiche(self, obj):
        return quantite(obj.stock_min)

    @admin.display(description='Prix moyen', ordering='prix_moyen')
    def prix_moyen_affiche(self, obj):
        return nombre(obj.prix_moyen)

    @admin.display(description='Valeur')
    def valeur_affichee(self, obj):
        return nombre(obj.valeur_stock)

    @admin.display(description='Alerte')
    def alerte(self, obj):
        if obj.stock <= 0:
            return badge('Rupture', '#dc2626')
        if obj.en_alerte:
            return badge('À commander', '#f59e0b')
        return badge('OK', '#16a34a')

    @admin.display(description='Derniers mouvements')
    def historique(self, obj):
        if not obj.pk:
            return '—'
        lignes = obj.mouvements.select_related('chantier')[:15]
        if not lignes:
            return 'Aucun mouvement.'
        rows = format_html_join('', '<tr><td>{}</td><td>{}</td><td>{}</td><td style="text-align:right">{}</td>'
                                    '<td style="text-align:right">{}</td><td>{}</td></tr>', (
            (m.date.strftime('%d/%m/%Y'), m.reference, m.get_type_display(), f'{m.quantite:+}',
             quantite(m.stock_apres), m.chantier or '') for m in lignes))
        return format_html('<table class="table table-sm"><tr><th>Date</th><th>Bon</th><th>Type</th>'
                           '<th>Qté</th><th>Stock après</th><th>Chantier</th></tr>{}</table>', rows)


# =========================================================
# Bons
# =========================================================

@admin.action(description='✅ Valider (met à jour le stock)')
def valider_bons(modeladmin, request, queryset):
    ok = 0
    for bon in queryset:
        try:
            services.valider(bon, request.user)
            ok += 1
        except services.StockError as e:
            modeladmin.message_user(request, ' '.join(e.messages), messages.ERROR)
    if ok:
        modeladmin.message_user(request, f'{ok} bon(s) validé(s).', messages.SUCCESS)


@admin.action(description='❌ Annuler')
def annuler_bons(modeladmin, request, queryset):
    ok = 0
    for bon in queryset:
        try:
            services.annuler(bon, request.user)
            ok += 1
        except services.StockError as e:
            modeladmin.message_user(request, ' '.join(e.messages), messages.ERROR)
    if ok:
        modeladmin.message_user(request, f'{ok} bon(s) annulé(s).', messages.SUCCESS)


@admin.action(description='🖨️ Imprimer (PDF)')
def imprimer_bons(modeladmin, request, queryset):
    return reponse_pdf(list(queryset), f'{queryset.model.PREFIXE}_{queryset.count()}_bons')


class LigneInlineBase(admin.TabularInline):
    extra = 3
    autocomplete_fields = ('article',)

    def _modifiable(self, obj):
        return obj is None or obj.modifiable

    def has_add_permission(self, request, obj=None):
        return self._modifiable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._modifiable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._modifiable(obj) and super().has_delete_permission(request, obj)


class LigneEntreeInline(LigneInlineBase):
    model = LigneEntree
    fields = ('article', 'quantite', 'prix_unitaire')


class LigneSortieInline(LigneInlineBase):
    model = LigneSortie
    fields = ('article', 'quantite', 'prix_unitaire')
    readonly_fields = ('prix_unitaire',)


class LigneInventaireInline(LigneInlineBase):
    model = LigneInventaire
    fields = ('article', 'stock_theorique', 'stock_compte')
    readonly_fields = ('stock_theorique',)


class BonAdminBase(admin.ModelAdmin):
    actions = [valider_bons, annuler_bons, imprimer_bons]
    date_hierarchy = 'date'
    search_fields = ('numero', 'observation')
    readonly_fields = ('numero', 'statut_badge', 'cree_par', 'valide_par', 'date_validation')

    def get_readonly_fields(self, request, obj=None):
        champs = list(super().get_readonly_fields(request, obj))
        if obj and not obj.modifiable:
            champs += [f.name for f in obj._meta.fields if f.editable and f.name not in champs]
        return champs

    def has_delete_permission(self, request, obj=None):
        # Un bon validé ne se supprime pas : on l'annule (le stock est alors corrigé).
        if obj is not None and not obj.modifiable:
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not obj.cree_par_id:
            obj.cree_par = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description='Statut', ordering='statut')
    def statut_badge(self, obj):
        return badge(obj.get_statut_display(), COULEURS_STATUT.get(obj.statut, '#64748b'))

    @admin.display(description='')
    def pdf(self, obj):
        return format_html('<a href="{}" target="_blank">🖨️ PDF</a>',
                           reverse('stock:bon_pdf', args=[obj._meta.model_name, obj.pk]))

    @admin.display(description='Total')
    def total_affiche(self, obj):
        return nombre(obj.total())


@admin.register(BonEntree)
class BonEntreeAdmin(BonAdminBase):
    list_display = ('numero', 'date', 'fournisseur', 'reference', 'total_affiche', 'statut_badge', 'pdf')
    list_filter = ('statut', 'fournisseur')
    search_fields = BonAdminBase.search_fields + ('reference', 'fournisseur__nom')
    autocomplete_fields = ('fournisseur',)
    inlines = [LigneEntreeInline]
    fields = ('numero', 'statut_badge', 'date', 'fournisseur', 'reference', 'observation',
              'cree_par', 'valide_par', 'date_validation')


@admin.register(BonSortie)
class BonSortieAdmin(BonAdminBase):
    list_display = ('numero', 'date', 'chantier', 'engin', 'demandeur', 'total_affiche', 'statut_badge', 'pdf')
    list_filter = ('statut', 'chantier', 'engin')
    search_fields = BonAdminBase.search_fields + ('demandeur', 'chantier__nom', 'engin__code')
    autocomplete_fields = ('chantier', 'engin')
    inlines = [LigneSortieInline]
    fields = ('numero', 'statut_badge', 'date', 'chantier', 'engin', 'demandeur', 'observation',
              'cree_par', 'valide_par', 'date_validation')


@admin.register(Inventaire)
class InventaireAdmin(BonAdminBase):
    list_display = ('numero', 'date', 'observation', 'statut_badge', 'pdf')
    list_filter = ('statut',)
    inlines = [LigneInventaireInline]
    fields = ('numero', 'statut_badge', 'date', 'observation', 'cree_par', 'valide_par', 'date_validation')


# =========================================================
# Journal
# =========================================================

@admin.action(description='📊 Exporter en Excel')
def exporter_mouvements(modeladmin, request, queryset):
    return exports.mouvements(queryset)


@admin.register(MouvementStock)
class MouvementStockAdmin(admin.ModelAdmin):
    list_display = ('date', 'reference', 'type', 'article', 'quantite_affichee', 'stock_apres_affiche',
                    'chantier', 'engin', 'utilisateur')
    list_filter = ('type', 'chantier', 'engin', 'article__categorie')
    search_fields = ('reference', 'article__code', 'article__designation')
    date_hierarchy = 'date'
    list_select_related = ('article', 'chantier', 'engin', 'utilisateur')
    actions = [exporter_mouvements]

    @admin.display(description='Quantité', ordering='quantite')
    def quantite_affichee(self, obj):
        couleur = '#16a34a' if obj.quantite > 0 else '#dc2626'
        signe = '+' if obj.quantite > 0 else ''
        return format_html('<b style="color:{}">{}{} {}</b>', couleur, signe, quantite(obj.quantite),
                           obj.article.unite)

    @admin.display(description='Stock après')
    def stock_apres_affiche(self, obj):
        return quantite(obj.stock_apres)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
