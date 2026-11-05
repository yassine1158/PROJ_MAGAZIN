"""Demandes de pièces : formulaire ouvert par le QR code affiché à l'atelier (téléphone, sans compte), puis
validation au magasin (une sortie de stock est créée et imprimée) ou refus."""
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import services
from .forms import nombre_saisi
from .models import Article, BonSortie, Chantier, DemandePieces, Engin, LigneDemande, LigneSortie
from .utils import quantite
from .views_magasin import _espaces, _par_nom, _trouver, _verifier_nom, chercher_articles
from .views_reglages import responsable

LIGNES_MAXI = 40
EN_ATTENTE_MAXI = 300  # au-delà, le formulaire refuse (envois en boucle depuis le Wi-Fi du magasin)


# =========================================================
# Côté atelier : téléphone, sans compte
# =========================================================

def _lire_lignes(post):
    """Lignes du formulaire (article_<n> ou libre_<n>, qte_<n>) → ([(article, désignation, quantité)], erreurs)."""
    numeros = sorted({int(cle.split('_')[1]) for cle in post if cle.startswith('qte_') and cle[4:].isdigit()})
    ids = [post.get(f'article_{n}', '') for n in numeros]
    articles = Article.objects.filter(actif=True).in_bulk([int(i) for i in ids if i.isdigit()])
    lignes, erreurs = [], []
    for n, id_ in zip(numeros, ids):
        article = articles.get(int(id_)) if id_.isdigit() else None
        libre = _espaces(post.get(f'libre_{n}'))[:200]
        texte = post.get(f'qte_{n}', '')
        if not article and not libre:
            if texte.strip():
                erreurs.append('Une quantité est tapée sans pièce : choisissez la pièce ou videz la ligne.')
            continue
        nom = article.designation if article else libre
        try:
            qte = nombre_saisi(texte)
        except ValidationError as e:
            erreurs.append(f'{nom} : {e.messages[0]}')
            continue
        if not qte:
            erreurs.append(f'{nom} : indiquez la quantité.')
            continue
        lignes.append((article, '' if article else libre, qte))
    if len(lignes) > LIGNES_MAXI:
        erreurs.append(f'{LIGNES_MAXI} pièces au plus par demande : faites une deuxième demande pour la suite.')
    return lignes, erreurs


def _listes():
    return {'chantiers': Chantier.objects.filter(actif=True).values_list('nom', flat=True),
            'engins': Engin.objects.filter(actif=True).values_list('code', flat=True)}


def demande(request):
    """Formulaire de demande de pièces (QR code de l'atelier)."""
    contexte = {**_listes(), 'valeurs': {}, 'lignes': []}
    if request.method == 'POST':
        jeton = request.POST.get('jeton', '')
        jeton = jeton if len(jeton) == 32 and jeton.isalnum() else uuid.uuid4().hex
        if deja := DemandePieces.objects.filter(jeton=jeton).first():  # formulaire envoyé deux fois
            return redirect('stock:demande_suivi', deja.jeton)
        valeurs = {c: _espaces(request.POST.get(c)) for c in ('demandeur', 'telephone', 'chantier', 'engin')}
        valeurs['observation'] = request.POST.get('observation', '').strip()[:2000]
        lignes, erreurs = _lire_lignes(request.POST)
        if not valeurs['demandeur']:
            erreurs.insert(0, 'Indiquez votre nom.')
        if not lignes and not erreurs:
            erreurs.append('Ajoutez au moins une pièce.')
        if DemandePieces.objects.filter(statut=DemandePieces.EN_ATTENTE).count() >= EN_ATTENTE_MAXI:
            erreurs = ['Trop de demandes attendent déjà au magasin : adressez-vous au magasinier.']
        if not erreurs:
            chantier = _trouver(Chantier, 'nom', valeurs['chantier']) if valeurs['chantier'] else None
            engin = _trouver(Engin, 'code', valeurs['engin']) if valeurs['engin'] else None
            with transaction.atomic():
                demande = DemandePieces.objects.create(
                    demandeur=valeurs['demandeur'][:150], telephone=valeurs['telephone'][:30],
                    chantier=chantier, chantier_texte='' if chantier else valeurs['chantier'][:150],
                    engin=engin, engin_texte='' if engin else valeurs['engin'][:50],
                    observation=valeurs['observation'], jeton=jeton)
                LigneDemande.objects.bulk_create([LigneDemande(demande=demande, article=a, designation=d, quantite=q)
                                                  for a, d, q in lignes])
            return redirect('stock:demande_suivi', demande.jeton)
        contexte.update(erreurs=erreurs, valeurs=valeurs, jeton=jeton, lignes=[
            {'article': a, 'designation': d or a.designation, 'quantite': quantite(q)} for a, d, q in lignes])
    contexte.setdefault('jeton', uuid.uuid4().hex)
    return render(request, 'stock/demande_form.html', contexte)


@require_GET
def demande_articles(request):
    """Recherche de pièces pour le formulaire : nom, code, unité et « disponible » (ni prix ni quantité exacte)."""
    articles = chercher_articles(request.GET.get('q', ''))
    return JsonResponse({'articles': [
        {'id': a.pk, 'code': a.code, 'designation': a.designation, 'unite': a.get_unite_display(),
         'disponible': a.stock > 0} for a in articles]})


def demande_suivi(request, jeton):
    """Page gardée par le demandeur : où en est sa demande (rafraîchie toute seule tant qu'elle attend)."""
    demande = get_object_or_404(DemandePieces, jeton=jeton)
    if request.GET.get('format') == 'json':
        return JsonResponse({'statut': demande.statut})
    return render(request, 'stock/demande_suivi.html', {
        'demande': demande, 'lignes': demande.lignes.select_related('article')})


# =========================================================
# Côté magasin
# =========================================================

@login_required
def demandes(request):
    """Demandes en attente (les plus anciennes d'abord), puis les dernières traitées."""
    attente = (DemandePieces.objects.filter(statut=DemandePieces.EN_ATTENTE).order_by('date', 'id')
               .select_related('chantier', 'engin').prefetch_related('lignes'))
    traitees = (DemandePieces.objects.exclude(statut=DemandePieces.EN_ATTENTE)
                .select_related('chantier', 'engin', 'sortie', 'traite_par').prefetch_related('lignes')[:50])
    return render(request, 'stock/demandes.html', {'attente': attente, 'traitees': traitees})


def _lignes_a_servir(demande, post):
    """Quantités données (qte_<ligne>) → ([(ligne, quantité)], erreurs). 0 ou vide : la pièce n'est pas donnée."""
    servies, erreurs = [], []
    for ligne in demande.lignes.filter(article__isnull=False).select_related('article'):
        try:
            qte = nombre_saisi(post.get(f'qte_{ligne.pk}', ''))
        except ValidationError as e:
            erreurs.append(f'{ligne.article.designation} : {e.messages[0]}')
            continue
        if qte:
            servies.append((ligne, qte))
    return servies, erreurs


@login_required
def demande_detail(request, pk):
    demande = get_object_or_404(DemandePieces.objects.select_related('chantier', 'engin', 'sortie', 'traite_par'),
                                pk=pk)
    lignes = list(demande.lignes.select_related('article'))
    contexte = {'demande': demande, **_listes(),
                'du_stock': [l for l in lignes if l.article], 'a_acheter': [l for l in lignes if not l.article],
                'valeurs': {'chantier': demande.nom_chantier, 'engin': demande.nom_engin,
                            'demandeur': demande.demandeur},
                'saisies': {}}
    if request.method == 'POST' and demande.en_attente:
        action = request.POST.get('action')
        if action == 'refuser':
            return _refuser(request, demande)
        if action == 'valider':
            reponse = _valider(request, demande, contexte)
            if reponse:
                return reponse
    for ligne in contexte['du_stock']:  # quantité à donner : celle demandée, ou ce qui vient d'être tapé
        ligne.a_donner = contexte['saisies'].get(f'qte_{ligne.pk}', quantite(ligne.quantite))
        ligne.manque = ligne.article.stock < ligne.quantite
    return render(request, 'stock/demande_detail.html', contexte)


def _refuser(request, demande):
    motif = _espaces(request.POST.get('motif'))[:255]
    if not motif:
        messages.error(request, 'Indiquez le motif du refus : le demandeur le verra sur son téléphone.')
        return redirect('stock:demande_detail', demande.pk)
    with transaction.atomic():
        fait = DemandePieces.objects.filter(pk=demande.pk, statut=DemandePieces.EN_ATTENTE).update(
            statut=DemandePieces.REFUSEE, motif_refus=motif, traite_par=request.user,
            date_traitement=timezone.now())
    if fait:
        messages.success(request, f'Demande {demande.numero} refusée.')
    else:
        messages.info(request, f'La demande {demande.numero} a déjà été traitée.')
    return redirect('stock:demandes')


def _valider(request, demande, contexte):
    """Crée et valide la sortie de stock des quantités données ; renvoie la redirection, ou None (erreurs dans
    le contexte)."""
    servies, erreurs = _lignes_a_servir(demande, request.POST)
    recu_par = _espaces(request.POST.get('demandeur'))[:150]
    if not recu_par:
        erreurs.insert(0, 'Indiquez qui reçoit les pièces.')
    if not _espaces(request.POST.get('chantier')):
        erreurs.insert(0, 'Indiquez le chantier.')
    chantier, chantier_a_creer, contexte['nouveau_chantier'] = _verifier_nom(request, Chantier, erreurs)
    engin, engin_a_creer, contexte['nouvel_engin'] = _verifier_nom(request, Engin, erreurs)
    if not servies and not erreurs:
        erreurs.append('Aucune pièce à donner : tapez au moins une quantité, ou refusez la demande.')
    if not erreurs:
        try:
            with transaction.atomic():
                if not DemandePieces.objects.filter(pk=demande.pk, statut=DemandePieces.EN_ATTENTE).exists():
                    messages.info(request, f'La demande {demande.numero} a déjà été traitée.')
                    return redirect('stock:demande_detail', demande.pk)
                remarque = f'Demande {demande.numero}' + (f' : {demande.observation}' if demande.observation else '')
                bon = BonSortie.objects.create(
                    chantier=chantier or _par_nom(Chantier, 'nom', chantier_a_creer),
                    engin=engin or _par_nom(Engin, 'code', engin_a_creer),
                    demandeur=recu_par, observation=remarque[:2000], cree_par=request.user)
                LigneSortie.objects.bulk_create([LigneSortie(bon=bon, article=l.article, quantite=q)
                                                 for l, q in servies])
                services.valider(bon, request.user)
                DemandePieces.objects.filter(pk=demande.pk).update(
                    statut=DemandePieces.VALIDEE, sortie=bon, traite_par=request.user,
                    date_traitement=timezone.now())
        except services.StockError as e:
            erreurs = e.messages
        else:
            messages.success(request, f'Demande {demande.numero} validée : sortie {bon.numero} enregistrée, '
                                      'le stock est mis à jour.')
            return redirect(f"{reverse('stock:bon_detail', args=['sortie', bon.pk])}?nouveau=1&imprimer=1")
    contexte.update(erreurs=erreurs, saisies={k: v for k, v in request.POST.items() if k.startswith('qte_')},
                    valeurs={c: _espaces(request.POST.get(c)) for c in ('chantier', 'engin', 'demandeur')})
    return None


@login_required
def demande_pdf(request, pk):
    from .bureau import livrer
    from .pdf import demande_pdf as pdf

    demande = get_object_or_404(DemandePieces, pk=pk)
    return livrer(request, pdf(demande, request.user))


@responsable
def demandes_affiche(request):
    """Affiche A4 avec le QR code du formulaire, à coller à l'atelier."""
    from . import etiquettes
    from .bureau import livrer

    base = etiquettes.adresse_base(request)
    if etiquettes.adresse_locale(base):
        messages.error(request, "Les téléphones ne peuvent pas encore ouvrir le logiciel : activez d'abord l'accès "
                                'des téléphones (Réglages › Accès téléphones), puis imprimez l\'affiche.')
        return redirect('stock:demandes')
    return livrer(request, etiquettes.affiche_demande(base))


@login_required
@require_GET
def api_demandes(request):
    """Nombre de demandes en attente et la plus récente : le menu et l'alerte se mettent à jour sans recharger."""
    attente = DemandePieces.objects.filter(statut=DemandePieces.EN_ATTENTE)
    derniere = attente.order_by('-date', '-id').first()
    return JsonResponse({'en_attente': attente.count(), 'derniere': derniere and {
        'id': derniere.pk, 'numero': derniere.numero, 'demandeur': derniere.demandeur,
        'nb': derniere.lignes.count(), 'url': reverse('stock:demande_detail', args=[derniere.pk])}})


@login_required
@require_POST
def demande_annuler_refus(request, pk):
    """Erreur de refus : la demande refusée repasse en attente."""
    fait = DemandePieces.objects.filter(pk=pk, statut=DemandePieces.REFUSEE).update(
        statut=DemandePieces.EN_ATTENTE, motif_refus='', traite_par=None, date_traitement=None)
    if fait:
        messages.success(request, 'La demande attend de nouveau.')
    return redirect('stock:demande_detail', pk)
