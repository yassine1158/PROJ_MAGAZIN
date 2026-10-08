"""Écrans du quotidien : accueil, entrées, sorties, historique des bons, articles."""
import io
import os
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.db.models import Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import exports, services
from .bureau import livrer
from .forms import ArticleForm, ParametresForm, SocieteDepartForm, decimal_saisi
from .models import (
    Article, Bloc, BonEntree, BonSortie, Categorie, Chantier, Engin, Fournisseur, Inventaire, LigneEntree,
    LigneInventaire, LigneSortie, MouvementStock, Parametres,
)
from .recherche import recherche_locale
from .utils import nombre, quantite
from .views import VALEUR, VALEUR_MVT

TYPES_BONS = {'entree': BonEntree, 'sortie': BonSortie, 'inventaire': Inventaire}
NOMS_BONS = {BonEntree: 'entree', BonSortie: 'sortie', Inventaire: 'inventaire'}


class Connexion(LoginView):
    template_name = 'stock/connexion.html'
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        if not get_user_model().objects.exists():  # tout premier lancement
            return redirect('stock:bienvenue')
        return super().dispatch(request, *args, **kwargs)


def _article_choix(a):
    """Article tel que l'affiche le sélecteur des bons."""
    return {
        'id': a.pk, 'code': a.code, 'designation': a.designation, 'unite': a.unite,
        'stock': str(a.stock), 'stock_txt': quantite(a.stock), 'prix': str(a.prix_moyen),
        'emplacement': a.emplacement,
    }


@login_required
@require_GET
def api_articles(request):
    q = request.GET.get('q', '').strip()[:100]
    if q:
        articles = recherche_locale([q], limite=15)
    else:
        articles = Article.objects.filter(actif=True).select_related('etagere__bloc')[:15]
    return JsonResponse({'articles': [_article_choix(a) for a in articles]})


# =========================================================
# Accueil
# =========================================================

@login_required
def accueil(request):
    articles = Article.objects.filter(actif=True)
    debut_mois = timezone.localdate().replace(day=1)
    sorties_mois = MouvementStock.objects.filter(type=MouvementStock.SORTIE, date__date__gte=debut_mois)
    derniers = sorted(
        list(BonEntree.objects.select_related('fournisseur')[:6]) + list(BonSortie.objects.select_related('chantier')[:6]),
        key=lambda b: b.date, reverse=True)[:6]
    return render(request, 'stock/accueil.html', {
        'nb_articles': articles.count(),
        'valeur_stock': nombre(articles.aggregate(v=Sum(VALEUR))['v'] or 0),
        'sorties_mois': nombre(sorties_mois.aggregate(v=Sum(VALEUR_MVT))['v'] or 0),
        'alertes': articles.en_alerte().select_related('etagere__bloc').order_by('stock')[:8],
        'derniers': [(b, NOMS_BONS[type(b)]) for b in derniers],
    })


# =========================================================
# Entrées et sorties
# =========================================================

def _lire_lignes(request, avec_prix):
    """Lit les lignes du formulaire. Renvoie (lignes, erreurs, lignes_pour_reafficher)."""
    ids = request.POST.getlist('article')
    qtes = request.POST.getlist('quantite')
    prix = request.POST.getlist('prix') if avec_prix else [''] * len(ids)
    articles = Article.objects.select_related('etagere__bloc').in_bulk([i for i in ids if i.isdigit()])
    lignes, erreurs, reaffichage = [], [], []
    for n, (i, q, p) in enumerate(zip(ids, qtes, prix + [''] * (len(ids) - len(prix))), start=1):
        if not i and not q.strip():
            continue
        article = articles.get(int(i)) if i.isdigit() else None
        quantite_ = decimal_saisi(q)
        prix_ = decimal_saisi(p) if avec_prix else None
        if article:
            reaffichage.append({**_article_choix(article), 'quantite': q, 'prix_saisi': p})
        if not article:
            erreurs.append(f'Ligne {n} : choisissez un article dans la liste.')
        elif quantite_ is None or quantite_ <= 0:
            erreurs.append(f'{article.code} : quantité invalide.')
        elif avec_prix and p.strip() and (prix_ is None or prix_ < 0):
            erreurs.append(f'{article.code} : prix invalide.')
        else:
            lignes.append((article, quantite_, prix_ or Decimal('0')))
    if not lignes and not erreurs:
        erreurs.append('Ajoutez au moins un article.')
    return lignes, erreurs, reaffichage


def _par_nom(modele, champ, valeur):
    valeur = (valeur or '').strip()
    if not valeur:
        return None
    return modele.objects.filter(**{f'{champ}__iexact': valeur}).first() or modele.objects.create(**{champ: valeur})


@login_required
def entree(request):
    contexte = {'type': 'entree', 'fournisseurs': Fournisseur.objects.values_list('nom', flat=True),
                'lignes_initiales': [], 'valeurs': {}}
    if request.method == 'POST':
        lignes, erreurs, reaffichage = _lire_lignes(request, avec_prix=True)
        if not erreurs:
            try:
                with transaction.atomic():
                    bon = BonEntree.objects.create(
                        fournisseur=_par_nom(Fournisseur, 'nom', request.POST.get('fournisseur')),
                        reference=request.POST.get('reference', '').strip()[:100],
                        observation=request.POST.get('observation', '').strip(),
                        cree_par=request.user,
                    )
                    LigneEntree.objects.bulk_create(
                        [LigneEntree(bon=bon, article=a, quantite=q, prix_unitaire=p) for a, q, p in lignes])
                    services.valider(bon, request.user)
            except services.StockError as e:
                erreurs = e.messages
            else:
                messages.success(request, f'Entrée {bon.numero} enregistrée : le stock est mis à jour.')
                return redirect('stock:bon_detail', 'entree', bon.pk)
        contexte.update(erreurs=erreurs, lignes_initiales=reaffichage, valeurs=request.POST)
    return render(request, 'stock/bon_form.html', contexte)


@login_required
def sortie(request):
    contexte = {'type': 'sortie',
                'chantiers': Chantier.objects.filter(actif=True).values_list('nom', flat=True),
                'engins': Engin.objects.filter(actif=True).values_list('code', flat=True),
                'lignes_initiales': [], 'valeurs': {'demandeur': ''}}
    if request.method == 'POST':
        lignes, erreurs, reaffichage = _lire_lignes(request, avec_prix=False)
        if not request.POST.get('chantier', '').strip():
            erreurs.insert(0, 'Indiquez le chantier.')
        if not request.POST.get('demandeur', '').strip():
            erreurs.insert(0, 'Indiquez qui reçoit la marchandise.')
        if not erreurs:
            try:
                with transaction.atomic():
                    bon = BonSortie.objects.create(
                        chantier=_par_nom(Chantier, 'nom', request.POST['chantier']),
                        engin=_par_nom(Engin, 'code', request.POST.get('engin')),
                        demandeur=request.POST['demandeur'].strip()[:150],
                        observation=request.POST.get('observation', '').strip(),
                        cree_par=request.user,
                    )
                    LigneSortie.objects.bulk_create([LigneSortie(bon=bon, article=a, quantite=q) for a, q, _ in lignes])
                    services.valider(bon, request.user)
            except services.StockError as e:
                erreurs = e.messages
            else:
                messages.success(request, f'Sortie {bon.numero} enregistrée : le stock est mis à jour.')
                return redirect('stock:bon_detail', 'sortie', bon.pk)
        contexte.update(erreurs=erreurs, lignes_initiales=reaffichage, valeurs=request.POST)
    return render(request, 'stock/bon_form.html', contexte)


@login_required
def bons(request):
    type_ = request.GET.get('type', '')
    q = request.GET.get('q', '').strip()
    listes = []
    for nom, Modele in TYPES_BONS.items():
        if type_ and type_ != nom:
            continue
        qs = Modele.objects.prefetch_related('lignes__article')
        if Modele is BonEntree:
            qs = qs.select_related('fournisseur')
            if q:
                qs = qs.filter(Q(numero__icontains=q) | Q(fournisseur__nom__icontains=q) | Q(reference__icontains=q))
        elif Modele is BonSortie:
            qs = qs.select_related('chantier', 'engin')
            if q:
                qs = qs.filter(Q(numero__icontains=q) | Q(chantier__nom__icontains=q) | Q(demandeur__icontains=q)
                               | Q(engin__code__icontains=q))
        elif q:
            qs = qs.filter(numero__icontains=q)
        listes += [(b, nom) for b in qs[:150]]
    listes.sort(key=lambda bn: bn[0].date, reverse=True)
    return render(request, 'stock/bons.html', {'bons': listes[:150], 'type': type_, 'q': q})


def _bon(type_, pk):
    Modele = TYPES_BONS.get(type_)
    if Modele is None:
        raise Http404
    return get_object_or_404(Modele, pk=pk)


@login_required
def bon_detail(request, type_, pk):
    bon = _bon(type_, pk)
    return render(request, 'stock/bon_detail.html', {
        'bon': bon, 'type': type_, 'lignes': bon.lignes.select_related('article'),
        'modele': type(bon)._meta.model_name,
    })


@login_required
@require_POST
def bon_annuler(request, type_, pk):
    bon = _bon(type_, pk)
    try:
        services.annuler(bon, request.user)
        messages.success(request, f'{bon.numero} est annulé : le stock est remis comme avant.')
    except services.StockError as e:
        messages.error(request, ' '.join(e.messages))
    return redirect('stock:bon_detail', type_, pk)


# =========================================================
# Articles
# =========================================================

@login_required
def articles(request):
    q = request.GET.get('q', '').strip()
    filtre = request.GET.get('filtre', '')
    categorie = request.GET.get('categorie', '')
    qs = Article.objects.filter(actif=True).select_related('categorie', 'etagere__bloc')
    if filtre == 'alerte':
        qs = qs.en_alerte()
    elif filtre == 'rupture':
        qs = qs.filter(stock__lte=0)
    elif filtre == 'inactifs':
        qs = Article.objects.filter(actif=False).select_related('categorie', 'etagere__bloc')
    if categorie.isdigit():
        qs = qs.filter(categorie_id=categorie)
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(designation__icontains=q) | Q(mots_cles__icontains=q)
                       | Q(reference_fabricant__icontains=q))
    if request.GET.get('format') == 'excel':
        return livrer(request, exports.etat_stock(qs))
    return render(request, 'stock/articles.html', {
        'articles': qs, 'q': q, 'filtre': filtre, 'categorie': categorie,
        'categories': Categorie.objects.all(),
        'total': nombre(sum((a.valeur_stock for a in qs), Decimal('0'))),
    })


def _article_page(request, form, article=None):
    return render(request, 'stock/article_form.html', {
        'form': form, 'article': article,
        'categories': Categorie.objects.values_list('nom', flat=True),
        'niveaux': {e.pk: e.nb_niveaux for e in form.fields['etagere'].queryset},
        'mouvements': article.mouvements.select_related('chantier')[:12] if article else [],
        'plan_vide': not Bloc.objects.exists(),
    })


@login_required
def article_nouveau(request):
    form = ArticleForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            article = form.save()
            stock = form.cleaned_data.get('stock_initial')
            if stock:
                bon = BonEntree.objects.create(cree_par=request.user, observation='Stock de départ')
                LigneEntree.objects.create(bon=bon, article=article, quantite=stock,
                                           prix_unitaire=form.cleaned_data.get('prix_initial') or 0)
                services.valider(bon, request.user)
        messages.success(request, f'Article {article.code} ajouté.')
        if request.POST.get('encore'):
            return redirect('stock:article_nouveau')
        return redirect('stock:articles')
    return _article_page(request, form)


@login_required
def article_modifier(request, pk):
    article = get_object_or_404(Article.objects.select_related('categorie', 'etagere__bloc'), pk=pk)
    form = ArticleForm(request.POST or None, request.FILES or None, instance=article)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'Article {article.code} enregistré.')
        return redirect('stock:articles')
    return _article_page(request, form, article)


@login_required
@require_POST
def article_corriger(request, pk):
    """Correction du stock après comptage : crée un petit inventaire validé."""
    article = get_object_or_404(Article, pk=pk)
    compte = decimal_saisi(request.POST.get('stock_compte'))
    if compte is None or compte < 0:
        messages.error(request, 'Indiquez la quantité comptée (un nombre positif).')
    elif compte == article.stock:
        messages.info(request, 'Le stock était déjà juste : rien à corriger.')
    else:
        with transaction.atomic():
            inv = Inventaire.objects.create(cree_par=request.user,
                                            observation=request.POST.get('motif', '').strip() or 'Correction après comptage')
            LigneInventaire.objects.create(bon=inv, article=article, stock_compte=compte)
            services.valider(inv, request.user)
        messages.success(request, f'Stock de {article.code} corrigé : {quantite(compte)} {article.unite} ({inv.numero}).')
    return redirect('stock:article_modifier', pk)



# =========================================================
# Premier lancement et réglages
# =========================================================

def bienvenue(request):
    """Premier lancement : création du compte administrateur (seulement s'il n'existe aucun compte)."""
    User = get_user_model()
    if User.objects.exists():
        return redirect('stock:connexion')
    erreurs, valeurs = [], {}
    societe = SocieteDepartForm(request.POST or None, request.FILES or None, instance=Parametres.actuels())
    if request.method == 'POST':
        valeurs = request.POST
        if not societe.is_valid():
            erreurs += [e for liste in societe.errors.values() for e in liste]
        nom = request.POST.get('username', '').strip()
        mdp, mdp2 = request.POST.get('password', ''), request.POST.get('password2', '')
        if not nom:
            erreurs.append("Choisissez un nom d'utilisateur.")
        if mdp != mdp2:
            erreurs.append('Les deux mots de passe ne sont pas identiques.')
        if not erreurs:
            utilisateur = User(username=nom, first_name=request.POST.get('prenom', '').strip()[:150],
                               is_staff=True, is_superuser=True)
            try:
                validate_password(mdp, utilisateur)
            except ValidationError as e:
                erreurs += e.messages
        if not erreurs:
            utilisateur.set_password(mdp)
            utilisateur.save()
            societe.save()
            if request.POST.get('demo') and not Article.objects.exists():
                from .management.commands.demo import Command as Demo
                call_command(Demo(), stdout=io.StringIO())
            login(request, utilisateur)
            messages.success(request, 'Bienvenue ! Votre compte est créé.')
            return redirect('stock:accueil')
    return render(request, 'stock/bienvenue.html', {'erreurs': erreurs, 'valeurs': valeurs, 'societe': societe})


@login_required
def reglages_ia(request):
    if not request.user.is_staff:
        raise Http404
    if request.method == 'POST':
        cle = request.POST.get('cle', '').strip()
        fichier = settings.MAGASIN_CLE_IA_FICHIER
        if request.POST.get('supprimer'):
            fichier.unlink(missing_ok=True)
            os.environ.pop('ANTHROPIC_API_KEY', None)
            settings.MAGASIN_IA_ACTIVE = bool(os.environ.get('ANTHROPIC_AUTH_TOKEN'))
            messages.success(request, 'Clé supprimée : la recherche simple reste disponible.')
        elif cle.startswith('sk-ant-'):
            fichier.write_text(cle)
            os.environ['ANTHROPIC_API_KEY'] = cle
            settings.MAGASIN_IA_ACTIVE = True
            messages.success(request, 'Clé enregistrée : la recherche IA est activée.')
        else:
            messages.error(request, 'Cette clé ne semble pas valide (elle commence par « sk-ant- »).')
        return redirect('stock:reglages_ia')
    cle = os.environ.get('ANTHROPIC_API_KEY', '')
    return render(request, 'stock/reglages_ia.html', {
        'active': settings.MAGASIN_IA_ACTIVE,
        'cle_masquee': f'{cle[:10]}…{cle[-4:]}' if len(cle) > 20 else '',
    })
