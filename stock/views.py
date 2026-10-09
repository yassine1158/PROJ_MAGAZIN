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
from .bureau import livrer
from .models import Article, Bloc, BonEntree, BonSortie, Inventaire, MouvementStock
from .recherche import PhotoIllisible, article_par_code, ia_disponible, rechercher
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
        'url': reverse('stock:article_modifier', args=[article.pk]),
    }


# =========================================================
# Recherche IA + plan 3D
# =========================================================

LIMITE_RESULTATS = 12


def _resultat_recherche(question, photo=None, avec_ia=False):
    """Réponse de la recherche : d'abord locale (immédiate) ; avec_ia=True pour la compléter avec l'IA."""
    if not photo:
        article = article_par_code(question)  # code exact (douchette, code recopié) : cet article seul
        if article:
            return {'ia': False, 'ia_disponible': False, 'exact': True, 'explication': None, 'termes': [],
                    'resultats': [_article_json(article)], 'plus': False}
    interpretation, resultats = rechercher(question, photo, avec_ia=avec_ia, limite=LIMITE_RESULTATS + 1)
    donnees = {
        'ia': interpretation is not None,
        'ia_disponible': ia_disponible(),
        'exact': False,
        'explication': interpretation.explication if interpretation else None,
        'termes': interpretation.termes if interpretation else [],
        'resultats': [_article_json(a) for a in resultats[:LIMITE_RESULTATS]],
        'plus': len(resultats) > LIMITE_RESULTATS,
    }
    if photo and not interpretation and not question.strip():
        donnees['message'] = ("La recherche par photo ne répond pas pour le moment (connexion Internet ?). "
                              "Écrivez le nom de l'article.")
    return donnees


@login_required
def recherche(request):
    """Page « Trouver un article ». Avec ?q= (barre de recherche, douchette), les résultats locaux
    sont calculés tout de suite et inclus dans la page."""
    question = request.GET.get('q', '').strip()[:500]
    initial = None
    if question:
        initial = _resultat_recherche(question)
    elif request.GET.get('article', '').isdecimal():
        article = (Article.objects.filter(pk=int(request.GET['article']))
                   .select_related('categorie', 'etagere__bloc').first())
        if article:
            initial = {'ia_disponible': False, 'exact': True, 'resultats': [_article_json(article)]}
    return render(request, 'stock/recherche.html', {
        'ia_active': settings.MAGASIN_IA_ACTIVE, 'q': question, 'initial': initial,
    })


@login_required
@require_POST
def api_recherche(request):
    """POST q (et photo). ?ia=0 : recherche locale seule (réponse immédiate) ; ?ia=1 : avec l'IA."""
    question = request.POST.get('q', '')[:500]
    photo = request.FILES.get('photo')
    if not question.strip() and not photo:
        return JsonResponse({'erreur': 'Écrivez ce que vous cherchez ou prenez une photo.'}, status=400)
    if photo and not settings.MAGASIN_IA_ACTIVE:
        return JsonResponse({'erreur': "La recherche par photo n'est pas activée : demandez au responsable."},
                            status=400)
    try:
        return JsonResponse(_resultat_recherche(question, photo, avec_ia=request.GET.get('ia') != '0'))
    except PhotoIllisible as e:
        return JsonResponse({'erreur': str(e)}, status=400)


@login_required
@require_GET
def api_article(request, pk):
    article = get_object_or_404(Article.objects.select_related('categorie', 'etagere__bloc'), pk=pk)
    return JsonResponse(_article_json(article))


@login_required
@require_GET
def api_plan(request):
    from .plan import plan_json

    return JsonResponse(plan_json())


@login_required
@require_POST
def api_plan_enregistrer(request):
    """Enregistre le plan dessiné dans l'éditeur (responsables seulement)."""
    import json

    from .plan import PlanInvalide, enregistrer

    if not request.user.is_staff:
        return JsonResponse({'erreurs': ['Réservé aux responsables.']}, status=403)
    try:
        donnees = json.loads(request.body)
    except ValueError:
        return JsonResponse({'erreurs': ['Données illisibles.']}, status=400)
    try:
        return JsonResponse(enregistrer(donnees))
    except PlanInvalide as e:
        return JsonResponse({'erreurs': e.erreurs}, status=400)


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
        return livrer(request, exports.mouvements(mouvements))
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
    """Bon imprimé ; ?exemplaires=2 : exemplaire magasin + exemplaire réceptionnaire. Visible par tous, comme
    le détail du bon (un magasinier imprime les bons qu'il fait)."""
    Modele = MODELES_BONS.get(modele)
    if Modele is None:
        raise Http404
    bon = get_object_or_404(Modele, pk=pk)  # comme le détail du bon : tout compte connecté peut l'imprimer
    from .pdf import reponse_pdf  # reportlab : chargé seulement à l'impression

    exemplaires = 2 if request.GET.get('exemplaires') == '2' else 1
    return livrer(request, reponse_pdf([bon], bon.numero, utilisateur=request.user, exemplaires=exemplaires))
