from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import exports
from .models import Article, Bloc, BonEntree, BonSortie, Inventaire, MouvementStock
from .pdf import reponse_pdf
from .recherche import rechercher
from .utils import nombre, quantite

MODELES_BONS = {'bonentree': BonEntree, 'bonsortie': BonSortie, 'inventaire': Inventaire}


def _article_json(article):
    etagere = article.etagere
    return {
        'id': article.pk,
        'code': article.code,
        'designation': article.designation,
        'categorie': article.categorie.nom,
        'stock': quantite(article.stock),
        'unite': article.unite,
        'alerte': article.en_alerte,
        'rupture': article.stock <= 0,
        'photo': article.photo.url if article.photo else None,
        'emplacement': article.emplacement,
        'bloc': etagere.bloc.code if etagere else None,
        'bloc_nom': etagere.bloc.nom if etagere else None,
        'etagere_id': etagere.pk if etagere else None,
        'etagere': etagere.code if etagere else None,
        'niveau': article.niveau,
        'case': article.case,
        'admin_url': reverse('admin:stock_article_change', args=[article.pk]),
    }


# =========================================================
# Recherche IA + plan 3D
# =========================================================

@login_required
def recherche(request):
    return render(request, 'stock/recherche.html', {'ia_active': settings.MAGASIN_IA_ACTIVE})


@login_required
@require_POST
def api_recherche(request):
    question = request.POST.get('q', '')[:500]
    photo = request.FILES.get('photo')
    if not question.strip() and not photo:
        return JsonResponse({'erreur': 'Écrivez ce que vous cherchez ou prenez une photo.'}, status=400)
    if photo and not settings.MAGASIN_IA_ACTIVE:
        return JsonResponse({'erreur': "La recherche par photo nécessite l'IA (clé ANTHROPIC_API_KEY)."},
                            status=400)
    try:
        interpretation, resultats = rechercher(question, photo)
    except ValueError as e:
        return JsonResponse({'erreur': str(e)}, status=400)
    return JsonResponse({
        'ia': interpretation is not None,
        'explication': interpretation.explication if interpretation else None,
        'termes': interpretation.termes if interpretation else [],
        'resultats': [_article_json(a) for a in resultats],
    })


@login_required
@require_GET
def api_article(request, pk):
    article = get_object_or_404(Article.objects.select_related('categorie', 'etagere__bloc'), pk=pk)
    return JsonResponse(_article_json(article))


@login_required
@require_GET
def api_plan(request):
    blocs = Bloc.objects.prefetch_related('etageres')
    return JsonResponse({'blocs': [
        {
            'code': b.code, 'nom': b.nom, 'couleur': b.couleur,
            'x': float(b.x), 'z': float(b.z), 'largeur': float(b.largeur), 'profondeur': float(b.profondeur),
            'etageres': [
                {
                    'id': e.pk, 'code': e.code, 'x': float(e.x), 'z': float(e.z),
                    'largeur': float(e.largeur), 'profondeur': float(e.profondeur),
                    'hauteur': float(e.hauteur), 'niveaux': e.nb_niveaux, 'tournee': e.tournee,
                }
                for e in b.etageres.all()
            ],
        }
        for b in blocs
    ]})


# =========================================================
# Tableau de bord et rapports
# =========================================================

VALEUR = ExpressionWrapper(F('stock') * F('prix_moyen'), output_field=DecimalField(max_digits=30, decimal_places=2))
VALEUR_MVT = ExpressionWrapper(-F('quantite') * F('prix_unitaire'),
                               output_field=DecimalField(max_digits=30, decimal_places=2))


@login_required
def dashboard(request):
    articles = Article.objects.filter(actif=True)
    debut_mois = timezone.localdate().replace(day=1)
    sorties_mois = MouvementStock.objects.filter(type=MouvementStock.SORTIE, date__date__gte=debut_mois)
    alertes = articles.en_alerte().select_related('categorie', 'etagere__bloc').order_by('stock')[:20]
    par_chantier = (sorties_mois.values('chantier__nom').annotate(valeur=Sum(VALEUR_MVT))
                    .order_by('-valeur')[:8])
    max_chantier = max([c['valeur'] or 0 for c in par_chantier], default=0) or 1
    contexte = {
        'nb_articles': articles.count(),
        'valeur_stock': nombre(articles.aggregate(v=Sum(VALEUR))['v'] or 0),
        'nb_alertes': articles.en_alerte().count(),
        'nb_ruptures': articles.filter(stock__lte=0).count(),
        'sorties_mois': nombre(sorties_mois.aggregate(v=Sum(VALEUR_MVT))['v'] or 0),
        'brouillons': BonEntree.objects.filter(statut='BROUILLON').count()
                      + BonSortie.objects.filter(statut='BROUILLON').count(),
        'alertes': alertes,
        'par_chantier': [
            {'nom': c['chantier__nom'] or '—', 'valeur': nombre(c['valeur'] or 0),
             'pourcent': int((c['valeur'] or 0) * 100 / max_chantier)}
            for c in par_chantier
        ],
        'derniers': MouvementStock.objects.select_related('article', 'chantier')[:12],
    }
    return render(request, 'stock/dashboard.html', contexte)


@login_required
def etat_stock(request):
    q = request.GET.get('q', '').strip()
    articles = Article.objects.filter(actif=True).select_related('categorie', 'etagere__bloc')
    if q:
        articles = articles.filter(Q(code__icontains=q) | Q(designation__icontains=q)
                                   | Q(categorie__nom__icontains=q) | Q(mots_cles__icontains=q))
    if request.GET.get('alerte'):
        articles = articles.filter(stock__lte=F('stock_min'))
    if request.GET.get('format') == 'excel':
        return exports.etat_stock(articles)
    total = sum((a.valeur_stock for a in articles), Decimal('0'))
    return render(request, 'stock/etat_stock.html', {'articles': articles, 'q': q, 'total': nombre(total),
                                                      'alerte': request.GET.get('alerte')})


def _date(texte, defaut):
    try:
        return date.fromisoformat(texte)
    except (TypeError, ValueError):
        return defaut


@login_required
def consommation(request):
    aujourd_hui = timezone.localdate()
    du = _date(request.GET.get('du'), aujourd_hui.replace(day=1))
    au = _date(request.GET.get('au'), aujourd_hui)
    mouvements = MouvementStock.objects.filter(
        type__in=[MouvementStock.SORTIE, MouvementStock.ANNULATION], chantier__isnull=False,
        date__date__gte=du, date__date__lt=au + timedelta(days=1),
    )
    if request.GET.get('format') == 'excel':
        return exports.mouvements(mouvements)
    lignes = (mouvements.values('chantier__nom', 'article__code', 'article__designation', 'article__unite')
              .annotate(qte=Sum('quantite'), valeur=Sum(VALEUR_MVT))
              .order_by('chantier__nom', 'article__code'))
    chantiers = {}
    for l in lignes:
        if not l['qte']:
            continue  # sortie entièrement annulée
        c = chantiers.setdefault(l['chantier__nom'], {'lignes': [], 'total': Decimal('0')})
        c['lignes'].append({**l, 'qte': quantite(-l['qte']), 'valeur_txt': nombre(l['valeur'])})
        c['total'] += l['valeur'] or 0
    for c in chantiers.values():
        c['total'] = nombre(c['total'])
    return render(request, 'stock/consommation.html', {'chantiers': chantiers, 'du': du, 'au': au})


@login_required
def bon_pdf(request, modele, pk):
    Modele = MODELES_BONS.get(modele)
    if Modele is None:
        raise Http404
    if not request.user.has_perm(f'stock.view_{modele}'):
        raise Http404
    bon = get_object_or_404(Modele, pk=pk)
    return reponse_pdf([bon], bon.numero)
