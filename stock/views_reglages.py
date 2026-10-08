"""Réglages : société, plan du magasin, listes (chantiers, engins…), utilisateurs.

Réservés aux responsables (comptes « Responsable »).
"""
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Count, ProtectedError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import LISTES, BlocForm, EtagereForm, ParametresForm, RangeeForm, UtilisateurForm, formulaire_liste
from .models import Bloc, Etagere, Parametres


def responsable(vue):
    @wraps(vue)
    @login_required
    def verifiee(request, *args, **kwargs):
        if not request.user.is_staff:
            messages.error(request, 'Cette page est réservée aux responsables.')
            return redirect('stock:accueil')
        return vue(request, *args, **kwargs)
    return verifiee


# =========================================================
# Ma société
# =========================================================

@responsable
def societe(request):
    form = ParametresForm(request.POST or None, request.FILES or None, instance=Parametres.actuels())
    if request.method == 'POST':
        if request.POST.get('retirer_logo'):
            p = Parametres.actuels()
            p.logo.delete(save=False)
            p.logo = ''
            p.save()
            messages.success(request, 'Logo retiré.')
            return redirect('stock:reglages_societe')
        if form.is_valid():
            form.save()
            messages.success(request, 'Réglages de la société enregistrés.')
            return redirect('stock:reglages_societe')
    return render(request, 'stock/reglages/societe.html', {'form': form, 'parametres': Parametres.actuels()})


# =========================================================
# Plan du magasin
# =========================================================

def _plan_svg():
    """Données du plan vu de dessus (en mètres) pour le dessin SVG."""
    blocs = list(Bloc.objects.prefetch_related('etageres').annotate(nb_articles=Count('etageres__articles')))
    if not blocs:
        return None
    min_x = min(float(b.x) for b in blocs)
    min_z = min(float(b.z) for b in blocs)
    max_x = max(float(b.x + b.largeur) for b in blocs)
    max_z = max(float(b.z + b.profondeur) for b in blocs)
    marge = 1.0
    dessin = []
    for b in blocs:
        etageres = []
        for e in b.etageres.all():
            largeur, profondeur = (e.profondeur, e.largeur) if e.tournee else (e.largeur, e.profondeur)
            etageres.append({'objet': e, 'x': float(b.x + e.x), 'z': float(b.z + e.z),
                             'l': float(largeur), 'p': float(profondeur)})
        dessin.append({'objet': b, 'x': float(b.x), 'z': float(b.z), 'l': float(b.largeur),
                       'p': float(b.profondeur), 'etageres': etageres})
    return {
        'blocs': dessin,
        'vue': f'{min_x - marge} {min_z - marge} {max_x - min_x + 2 * marge} {max_z - min_z + 2 * marge}',
    }


@responsable
def plan(request):
    blocs = Bloc.objects.prefetch_related('etageres').annotate(nb_articles=Count('etageres__articles'))
    return render(request, 'stock/reglages/plan.html', {'blocs': blocs, 'svg': _plan_svg()})


@responsable
def bloc_form(request, pk=None):
    bloc = get_object_or_404(Bloc, pk=pk) if pk else None
    suivant = Bloc.objects.order_by('-x').first()
    initial = {}
    if not bloc:  # nouveau bloc : placé à droite du dernier, lettre suivante
        codes = set(Bloc.objects.values_list('code', flat=True))
        lettre = next((c for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if c not in codes), '')
        initial = {'code': lettre, 'x': float(suivant.x + suivant.largeur) + 2 if suivant else 0, 'z': 0}
    form = BlocForm(request.POST or None, instance=bloc, initial=initial)
    if request.method == 'POST' and form.is_valid():
        bloc = form.save()
        messages.success(request, f'Bloc {bloc.code} enregistré.')
        return redirect('stock:reglages_plan')
    return render(request, 'stock/reglages/formulaire.html', {
        'form': form, 'titre': f'Bloc {bloc.code}' if bloc else 'Nouveau bloc',
        'sous_titre': 'Un bloc est une zone du magasin (ex. : Pièces moteur, Pneus, Ciment).',
        'retour': 'stock:reglages_plan',
        'supprimer': reverse('stock:bloc_supprimer', args=[bloc.pk]) if bloc else None,
        'avertissement_suppression': f'Supprimer le bloc {bloc.code} et ses étagères ? '
                                     'Les articles rangés dedans deviendront « non rangés ».' if bloc else '',
    })


@responsable
@require_POST
def bloc_supprimer(request, pk):
    bloc = get_object_or_404(Bloc, pk=pk)
    bloc.delete()
    messages.success(request, f'Bloc {bloc.code} supprimé.')
    return redirect('stock:reglages_plan')


@responsable
def etagere_form(request, bloc_pk=None, pk=None):
    etagere = get_object_or_404(Etagere.objects.select_related('bloc'), pk=pk) if pk else None
    bloc = etagere.bloc if etagere else get_object_or_404(Bloc, pk=bloc_pk)
    initial = {}
    if not etagere:
        derniere = bloc.etageres.order_by('-x').first()
        initial = {'code': f'E{bloc.etageres.count() + 1}',
                   'x': float(derniere.x + derniere.largeur) + 0.2 if derniere else 1, 'z': 1}
    form = EtagereForm(request.POST or None, instance=etagere, bloc=bloc, initial=initial)
    if request.method == 'POST' and form.is_valid():
        etagere = form.save()
        messages.success(request, f'Étagère {etagere} enregistrée.')
        return redirect('stock:reglages_plan')
    return render(request, 'stock/reglages/formulaire.html', {
        'form': form, 'titre': f'Étagère {etagere}' if etagere else f'Nouvelle étagère dans le bloc {bloc.code}',
        'sous_titre': 'Positions et tailles en mètres. Le niveau 1 est en bas.',
        'retour': 'stock:reglages_plan',
        'supprimer': reverse('stock:etagere_supprimer', args=[etagere.pk]) if etagere else None,
        'avertissement_suppression': f'Supprimer l\'étagère {etagere} ? Ses articles deviendront « non rangés ».'
                                     if etagere else '',
    })


@responsable
@require_POST
def etagere_supprimer(request, pk):
    etagere = get_object_or_404(Etagere, pk=pk)
    nom = str(etagere)
    etagere.delete()
    messages.success(request, f'Étagère {nom} supprimée.')
    return redirect('stock:reglages_plan')


@responsable
def rangee(request, bloc_pk):
    bloc = get_object_or_404(Bloc, pk=bloc_pk)
    form = RangeeForm(request.POST or None, bloc=bloc)
    if request.method == 'POST' and form.is_valid():
        creees = form.creer()
        messages.success(request, f'{len(creees)} étagères ajoutées au bloc {bloc.code}.')
        return redirect('stock:reglages_plan')
    return render(request, 'stock/reglages/formulaire.html', {
        'form': form, 'titre': f'Ajouter une rangée d\'étagères – bloc {bloc.code}',
        'sous_titre': 'Plusieurs étagères identiques, alignées de gauche à droite.',
        'retour': 'stock:reglages_plan',
    })


# =========================================================
# Listes : chantiers, engins, fournisseurs, catégories
# =========================================================

@responsable
def listes(request, type_='chantiers'):
    if type_ not in LISTES:
        raise Http404
    modele, champs, titre, nom = LISTES[type_]
    form = formulaire_liste(type_)(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'{nom.capitalize()} ajouté(e).')
        return redirect('stock:reglages_listes', type_)
    elements = modele.objects.all()
    return render(request, 'stock/reglages/listes.html', {
        'type': type_, 'types': [(cle, v[2]) for cle, v in LISTES.items()], 'titre': titre, 'nom': nom,
        'champs': [modele._meta.get_field(c) for c in champs], 'elements': elements, 'form': form,
    })


@responsable
def liste_modifier(request, type_, pk):
    if type_ not in LISTES:
        raise Http404
    modele, _, titre, nom = LISTES[type_]
    element = get_object_or_404(modele, pk=pk)
    form = formulaire_liste(type_)(request.POST or None, instance=element)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'{element} enregistré(e).')
        return redirect('stock:reglages_listes', type_)
    return render(request, 'stock/reglages/formulaire.html', {
        'form': form, 'titre': f'{nom.capitalize()} : {element}', 'retour_url': reverse('stock:reglages_listes', args=[type_]),
        'supprimer': reverse('stock:liste_supprimer', args=[type_, pk]),
        'avertissement_suppression': f'Supprimer « {element} » ?',
    })


@responsable
@require_POST
def liste_supprimer(request, type_, pk):
    if type_ not in LISTES:
        raise Http404
    modele = LISTES[type_][0]
    element = get_object_or_404(modele, pk=pk)
    try:
        element.delete()
        messages.success(request, f'« {element} » supprimé(e).')
    except ProtectedError:
        conseil = ' Décochez « actif » pour ne plus le proposer.' if hasattr(element, 'actif') else ''
        messages.error(request, f'« {element} » est utilisé dans des bons ou des articles : impossible de le supprimer.'
                                + conseil)
    return redirect('stock:reglages_listes', type_)


# =========================================================
# Utilisateurs
# =========================================================

@responsable
def utilisateurs(request):
    return render(request, 'stock/reglages/utilisateurs.html', {
        'utilisateurs': User.objects.order_by('-is_active', 'username'),
    })


@responsable
def utilisateur_form(request, pk=None):
    utilisateur = get_object_or_404(User, pk=pk) if pk else None
    form = UtilisateurForm(request.POST or None, instance=utilisateur)
    if request.method == 'POST' and form.is_valid():
        soi_meme = utilisateur and utilisateur.pk == request.user.pk
        if soi_meme and (not form.cleaned_data['is_active'] or form.cleaned_data['role'] != 'responsable'):
            form.add_error(None, 'Vous ne pouvez pas retirer vos propres droits de responsable ni bloquer votre compte.')
        else:
            compte = form.save()
            if soi_meme and form.cleaned_data.get('mot_de_passe'):
                from django.contrib.auth import update_session_auth_hash
                update_session_auth_hash(request, compte)
            messages.success(request, f'Compte « {compte.username} » enregistré.')
            return redirect('stock:reglages_utilisateurs')
    return render(request, 'stock/reglages/formulaire.html', {
        'form': form, 'titre': f'Compte « {utilisateur.username} »' if utilisateur else 'Nouveau compte',
        'sous_titre': 'Chaque personne a son compte : on sait qui a fait chaque entrée et chaque sortie.',
        'retour': 'stock:reglages_utilisateurs',
    })
