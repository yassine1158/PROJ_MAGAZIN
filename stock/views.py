from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import exports, rapports
from .bureau import livrer
from .models import Article, Bloc, BonEntree, BonRetour, BonSortie, Chantier, Engin, Fournisseur, Inventaire, MouvementStock
from .rapports import VALEUR, VALEUR_MVT  # noqa: F401  (utilisées aussi par views_magasin)
from .recherche import PhotoIllisible, article_par_code, ia_disponible, rechercher
from .utils import nombre, quantite

MODELES_BONS = {'bonentree': BonEntree, 'bonsortie': BonSortie, 'bonretour': BonRetour, 'inventaire': Inventaire}


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
    except (ValueError, RecursionError):
        return JsonResponse({'erreurs': ['Données illisibles.']}, status=400)
    try:
        return JsonResponse(enregistrer(donnees))
    except PlanInvalide as e:
        return JsonResponse({'erreurs': e.erreurs, 'cibles': e.cibles}, status=400)


# =========================================================
# Tableau de bord et rapports
# =========================================================

@login_required
def dashboard(request):
    articles = Article.objects.filter(actif=True)
    debut_mois = timezone.localdate().replace(day=1)
    alertes = articles.en_alerte().select_related('categorie', 'etagere__bloc').order_by('stock')[:20]
    par_chantier = rapports.par_chantier(debut_mois)[:8]
    max_chantier = max([c['valeur'] for c in par_chantier], default=0) or 1
    contexte = {
        'nb_articles': articles.count(),
        'valeur_stock': nombre(articles.aggregate(v=Sum(VALEUR))['v'] or 0),
        'nb_alertes': articles.en_alerte().count(),
        'nb_ruptures': articles.filter(stock__lte=0).count(),
        'sorties_mois': nombre(rapports.total_sorties(debut_mois)),
        'brouillons': sum(M.objects.filter(statut=M.BROUILLON).count() for M in (BonEntree, BonSortie, Inventaire)),
        'alertes': alertes,
        'par_chantier': [
            {'nom': c['nom'], 'valeur': nombre(c['valeur']), 'pourcent': int(c['valeur'] * 100 / max_chantier)}
            for c in par_chantier
        ],
        'par_categorie': rapports.valeur_par_categorie(),
        'derniers': list(MouvementStock.objects.select_related('article', 'chantier')[:12]),
    }
    for m in contexte['derniers']:
        m.unite_qte = rapports.unite_qte(m.article.unite, m.quantite)
    return render(request, 'stock/dashboard.html', contexte)


def _lien(request, **changements):
    """Paramètres de la page actuelle modifiés (pour les liens de période, de page…), sans « format »."""
    params = request.GET.copy()
    params.pop('format', None)
    for cle, valeur in changements.items():
        if valeur in (None, ''):
            params.pop(cle, None)
        else:
            params[cle] = str(valeur)
    return params.urlencode()


def _raccourcis(request, du, au):
    return [{'libelle': libelle, 'actif': (d, a) == (du, au), 'lien': _lien(request, du=d, au=a, page=None)}
            for libelle, d, a in rapports.raccourcis()]


def paginer(request, objets, par_page):
    """Page demandée (?page=) d'une liste ou d'une requête ; page.lien_precedent et page.lien_suivant gardent les
    autres paramètres (recherche, filtres)."""
    page = Paginator(objets, par_page).get_page(request.GET.get('page'))
    page.object_list = list(page.object_list)
    page.lien_precedent = _lien(request, page=page.previous_page_number()) if page.has_previous() else ''
    page.lien_suivant = _lien(request, page=page.next_page_number()) if page.has_next() else ''
    return page


def lien_bon(type_, bon):
    return reverse('stock:bon_detail', args=[type_, bon.pk]) if bon is not None else ''


@login_required
def consommation(request):
    du, au = rapports.periode(request.GET)
    par = request.GET.get('par') if request.GET.get('par') in rapports.REGROUPEMENTS else 'chantier'
    chantier = rapports._objet(Chantier, request.GET.get('chantier'))
    donnees = rapports.consommation(du, au, par, chantier)
    format_ = request.GET.get('format')
    if format_ == 'excel':
        return livrer(request, _consommation_excel(donnees, du, au, par, chantier, request.user))
    if format_ == 'pdf':
        from .pdf import rapport_consommation  # reportlab : chargé seulement à l'impression

        return livrer(request, rapport_consommation(donnees, du, au, par, chantier, request.user))
    return render(request, 'stock/consommation.html', {
        **donnees, 'du': du, 'au': au, 'par': par, 'chantier': chantier,
        'onglets': [{'libelle': libelle, 'actif': cle == par, 'lien': _lien(request, par=cle)}
                    for cle, libelle in rapports.REGROUPEMENTS.items()],
        'chantiers': Chantier.objects.all(),
        'raccourcis': _raccourcis(request, du, au), 'lien_excel': _lien(request, format='excel'),
        'lien_pdf': _lien(request, format='pdf'),
    })


def _consommation_excel(donnees, du, au, par, chantier, utilisateur):
    """Deux onglets : Récapitulatif (avec sous-totaux) et Détail (chaque sortie ou retour)."""
    filtres = ' · '.join(filter(None, [f'Du {du:%d/%m/%Y} au {au:%d/%m/%Y}',
                                       f'Chantier {chantier.nom}' if chantier else '']))
    article = ['Code', 'Désignation', ('Quantité', exports.QTE), 'Unité', (exports.entete_valeur(), exports.MONTANT)]
    if par == 'article':
        colonnes, lignes = article, [[l['code'], l['designation'], l['qte'], l['unite'], l['valeur']]
                                     for g in donnees['groupes'] for l in g['lignes']]
        pied = [['TOTAL', '', '', '', donnees['total']]]
    else:
        colonnes, lignes = [par.capitalize()] + article, []
        for g in donnees['groupes']:
            lignes += [[g['nom'], l['code'], l['designation'], l['qte'], l['unite'], l['valeur']] for l in g['lignes']]
            lignes.append(exports.Gras([f'Sous-total {g["nom"]}', '', '', '', '', g['total']]))
        pied = [['TOTAL', '', '', '', '', donnees['total']]]
    detail = rapports.sorties_valorisees(du, au).select_related('article', 'chantier', 'engin', 'utilisateur')
    if chantier:
        detail = detail.filter(chantier=chantier)
    lignes_detail, total_detail = [], Decimal('0')
    for m in detail.order_by('date', 'id'):
        valeur = -m.quantite * m.prix_unitaire
        total_detail += valeur
        lignes_detail.append([m.date, m.reference, m.get_type_display(), m.chantier.nom if m.chantier else '',
                              m.engin.code if m.engin else '', m.article.code, m.article.designation, -m.quantite,
                              m.article.get_unite_display(), m.prix_unitaire, valeur])
    return exports.classeur([
        exports.feuille('Récapitulatif', colonnes, lignes, f'{filtres} · {rapports.REGROUPEMENTS[par].lower()}', pied),
        exports.feuille('Détail', [('Date', exports.DATE_HEURE), 'Bon', 'Type', 'Chantier', 'Engin', 'Code',
                                   'Désignation', ('Quantité', exports.QTE), 'Unité', ('Prix unitaire', exports.PRIX),
                                   (exports.entete_valeur(), exports.MONTANT)],
                        lignes_detail, filtres, [['TOTAL'] + [''] * 9 + [total_detail]]),
    ], f'consommation_{du:%Y-%m-%d}_au_{au:%Y-%m-%d}', utilisateur)


@login_required
def journal(request):
    """Journal des mouvements : tout ce qui est entré et sorti, filtrable, 100 lignes par page."""
    mouvements, choix, description = rapports.journal(request.GET)
    if request.GET.get('format') == 'excel':
        du, au = choix['du'], choix['au']
        return livrer(request, exports.mouvements(mouvements, request.user, description,
                                                  f'mouvements_{du:%Y-%m-%d}_au_{au:%Y-%m-%d}'))
    page = paginer(request, mouvements.select_related('article', 'chantier', 'engin', 'utilisateur')
                   .annotate(valeur=rapports.VALEUR_LIGNE), 100)
    for m in rapports.decrire_mouvements(page.object_list):
        m.lien = lien_bon(m.type_bon, m.bon)
        m.unite_qte = rapports.unite_qte(m.article.unite, m.quantite)
    return render(request, 'stock/journal.html', {
        'page': page, 'choix': choix, 'totaux': rapports.totaux_journal(mouvements),
        'types': MouvementStock.TYPES, 'chantiers': Chantier.objects.all(), 'engins': Engin.objects.all(),
        'fournisseurs': Fournisseur.objects.all(), 'raccourcis': _raccourcis(request, choix['du'], choix['au']),
        'lien_excel': _lien(request, format='excel', page=None),
        'filtre_actif': any(choix[c] for c in ('type', 'article', 'chantier', 'engin', 'fournisseur')),
    })


@login_required
def etat_stock_date(request):
    """État du stock (quantités et valeur) à une date choisie, avec sous-totaux par catégorie ou par bloc."""
    aujourd_hui = timezone.localdate()
    jour = min(rapports.lire_date(request.GET.get('date'), aujourd_hui), aujourd_hui)
    par = 'bloc' if request.GET.get('par') == 'bloc' else 'categorie'
    avec_zero = request.GET.get('zero') == '1'
    donnees = rapports.etat_a_date(jour, par, avec_zero)
    format_ = request.GET.get('format')
    if format_ == 'excel':
        return livrer(request, _etat_excel(donnees, jour, par, request.user))
    if format_ == 'pdf':
        from .pdf import rapport_etat_stock  # reportlab : chargé seulement à l'impression

        return livrer(request, rapport_etat_stock(donnees, jour, par, request.user))
    return render(request, 'stock/etat_stock_date.html', {
        **donnees, 'jour': jour, 'par': par, 'avec_zero': avec_zero, 'aujourd_hui': aujourd_hui,
        'groupes_etat': rapports.GROUPES_ETAT,
        'dates_rapides': [{'libelle': libelle, 'actif': d == jour, 'lien': _lien(request, date=d)} for libelle, d in (
            ("Aujourd'hui", aujourd_hui),
            ('Fin du mois dernier', aujourd_hui.replace(day=1) - timedelta(days=1)),
            (f"Fin {aujourd_hui.year - 1}", date(aujourd_hui.year - 1, 12, 31)),
        )],
        'lien_excel': _lien(request, format='excel'), 'lien_pdf': _lien(request, format='pdf'),
    })


def _etat_excel(donnees, jour, par, utilisateur):
    nom_groupe = 'Bloc' if par == 'bloc' else 'Catégorie'
    lignes = []
    for g in donnees['groupes']:
        lignes += [[g['nom'], l['code'], l['designation'], l['unite'], l['qte'], l['pm'], l['valeur'],
                    'Prix moyen actuel' if l['approx'] else ''] for l in g['lignes']]
        lignes.append(exports.Gras([f'Sous-total {g["nom"]}', '', '', '', '', '', g['total'], '']))
    return exports.classeur([exports.feuille(
        f'État du stock au {jour:%d/%m/%Y}',
        [nom_groupe, 'Code', 'Désignation', 'Unité', ('Quantité', exports.QTE), ('Prix moyen', exports.PRIX),
         (exports.entete_valeur(), exports.MONTANT), 'Remarque'],
        lignes, f'{donnees["nb"]} article(s) · {rapports.GROUPES_ETAT[par].lower()}',
        [['TOTAL', '', '', '', '', '', donnees['total'], '']],
    )], f'etat_stock_au_{jour:%Y-%m-%d}', utilisateur)


@login_required
def bon_pdf(request, modele, pk):
    """Bon imprimé ; ?exemplaires=2 : exemplaire magasin + exemplaire réceptionnaire. Visible par tous, comme
    le détail du bon (un magasinier imprime les bons qu'il fait)."""
    Modele = MODELES_BONS.get(modele)
    if Modele is None:
        raise Http404
    bon = get_object_or_404(Modele, pk=pk)  # comme le détail du bon : tout compte connecté peut l'imprimer
    if isinstance(bon, Inventaire) and bon.perimetre:
        # Inventaire de l'écran Inventaire : fiche de comptage tant qu'il est en cours, sinon le résultat.
        from django.shortcuts import redirect

        return redirect('stock:inventaire_resultat' if bon.statut == Inventaire.VALIDE else 'stock:inventaire_fiche', pk)
    from .pdf import reponse_pdf  # reportlab : chargé seulement à l'impression

    exemplaires = 2 if request.GET.get('exemplaires') == '2' else 1
    return livrer(request, reponse_pdf([bon], bon.numero, utilisateur=request.user, exemplaires=exemplaires))
