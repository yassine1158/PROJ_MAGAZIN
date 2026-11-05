"""Écrans du quotidien : accueil, entrées, sorties, retours de chantier, historique des bons, articles, inventaire."""
import difflib
import hashlib
import io
import json
import logging
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from urllib.parse import unquote, urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, DecimalField, ExpressionWrapper, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Replace
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import exports, imports, rapports, services, views
from .bureau import livrer
from .forms import (
    JOURS_RETOUR_MAGASINIER, ArticleForm, InventaireForm, ParametresForm, SocieteDepartForm, date_du_bon, nombre_saisi,
    prix_saisi,
)
from .models import (
    Article, Bloc, BonEntree, BonRetour, BonSortie, Categorie, Chantier, Engin, Etagere, Fournisseur, Inventaire, LigneEntree,
    LigneInventaire, LigneRetour, LigneSortie, MouvementStock, Parametres,
)
from .recherche import normaliser, recherche_locale
from .utils import nombre, ordre_emplacement, quantite
from .views import VALEUR, VALEUR_MVT
from .views_reglages import responsable

journal = logging.getLogger(__name__)

TYPES_BONS = {'entree': BonEntree, 'sortie': BonSortie, 'retour': BonRetour, 'inventaire': Inventaire}
STATUTS_BONS = {'brouillon': ('BROUILLON', 'Brouillons'), 'annule': ('ANNULE', 'Annulés'), 'valide': ('VALIDE', 'Validés')}
NOMS_BONS = {BonEntree: 'entree', BonSortie: 'sortie', BonRetour: 'retour', Inventaire: 'inventaire'}


class Connexion(LoginView):
    template_name = 'stock/connexion.html'
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        if not get_user_model().objects.exists():  # tout premier lancement
            return redirect('stock:bienvenue')
        suivant = request.POST.get('next') or request.GET.get('next') or ''
        if request.user.is_authenticated and not request.user.is_staff and suivant.startswith('/admin'):
            # Sinon : administration → connexion → administration… sans fin pour un magasinier.
            messages.error(request, 'Réservé aux responsables.')
            return redirect('stock:accueil')
        return super().dispatch(request, *args, **kwargs)


def _sur_le_pc(request):
    """Version bureau, devant l'ordinateur où le logiciel est installé (pas un téléphone du magasin)."""
    from .reseau import est_local
    return settings.BUREAU and est_local(request)


def mot_de_passe_oublie(request):
    """Mot de passe oublié : sur l'ordinateur du magasin seulement, on choisit le compte et on tape un nouveau mot
    de passe. Avoir l'ordinateur en main, c'est déjà avoir les données : cela ne donne pas plus d'accès."""
    if not _sur_le_pc(request):
        messages.error(request, "Mot de passe oublié : changez-le sur l'ordinateur où le logiciel est installé, "
                                'ou demandez à un responsable.')
        return redirect('stock:connexion')
    comptes = get_user_model().objects.filter(is_active=True).order_by('-is_staff', 'username')
    erreurs, choisi = [], request.POST.get('compte', '')
    if request.method == 'POST':
        utilisateur = comptes.filter(pk=choisi).first() if choisi.isdigit() else None
        mdp, mdp2 = request.POST.get('password', ''), request.POST.get('password2', '')
        if not utilisateur:
            erreurs.append('Choisissez le compte.')
        if mdp != mdp2:
            erreurs.append('Les deux mots de passe ne sont pas identiques.')
        try:
            validate_password(mdp, utilisateur)
        except ValidationError as e:
            erreurs += e.messages
        if not erreurs:
            utilisateur.set_password(mdp)
            utilisateur.save(update_fields=['password'])
            journal.warning('Mot de passe changé depuis « Mot de passe oublié » : %s', utilisateur.get_username())
            messages.success(request, f'Nouveau mot de passe enregistré pour « {utilisateur.get_username()} » : '
                                      'connectez-vous.')
            return redirect('stock:connexion')
    return render(request, 'stock/mot_de_passe_oublie.html', {'comptes': comptes, 'erreurs': erreurs,
                                                               'choisi': choisi})


def _article_choix(a):
    """Article tel que l'affiche le sélecteur des bons."""
    return {
        'id': a.pk, 'code': a.code, 'designation': a.designation, 'unite': a.unite,
        'stock': str(a.stock), 'stock_txt': quantite(a.stock), 'prix': str(a.prix_moyen),
        'emplacement': a.emplacement,
    }


def _singulier(mot):
    """« gants » → « gant », « tuyaux » → « tuyau » (assez pour une recherche « contient »)."""
    return mot[:-1] if len(mot) >= 4 and mot[-1] in 'sx' else mot


ACCENTS = {'a': 'àâä', 'c': 'ç', 'e': 'éèêë', 'i': 'îï', 'o': 'ôö', 'u': 'ùûü'}


def _motif(mot):
    """Expression régulière d'un mot tapé sans accents : « cable » trouve aussi « Câble » (avec iregex)."""
    return ''.join(f'[{c}{ACCENTS[c]}]' if c in ACCENTS else re.escape(c) for c in normaliser(mot))


def _filtrer_texte(qs, q):
    """Articles de qs dont le code commence par q, ou qui contiennent chaque mot de q (au singulier, avec ou sans
    accents) dans la désignation, les autres noms, la référence ou le code."""
    mots = Q()
    for mot in map(_singulier, normaliser(q).split()[:6]):
        motif = _motif(mot)
        mots &= (Q(designation__iregex=motif) | Q(mots_cles__iregex=motif) | Q(reference_fabricant__icontains=mot)
                 | Q(code__icontains=mot))
    return qs.filter(Q(code__istartswith=q) | mots)


def _code_compact(champ):
    """Code sans tiret, espace, point ni barre : « HM-1540 » → « HM1540 » (code tapé ou lu par une douchette)."""
    for signe in '- ./':
        champ = Replace(champ, Value(signe), Value(''))
    return champ


def code_scanne(texte):
    """Adresse d'une étiquette lue par une douchette (« http://…/a/FH-0042/ ») → « FH-0042 » ; sinon le texte."""
    trouve = re.fullmatch(r'https?://[^/\s]+(?:/[^\s]*)?/a/([^?#\s]+?)/?(?:[?#]\S*)?', texte.strip())
    return unquote(trouve.group(1)) if trouve else texte


@login_required
@require_GET
def api_articles(request):
    """Sélecteur d'article des bons, à chaque frappe : le code exact (douchette) en tête, puis une recherche en base ;
    la recherche tolérante (fautes, pluriels), plus lente, seulement quand la base trouve peu de chose et qu'aucun
    code ne correspond exactement. L'adresse d'une étiquette QR (douchette 2D) vaut son code."""
    return JsonResponse({'articles': [_article_choix(a) for a in chercher_articles(request.GET.get('q', ''))]})


def chercher_articles(q):
    """Les 15 articles actifs qui répondent le mieux à ce qui est tapé (sélecteur des bons, demande de pièces)."""
    q = code_scanne(q).strip()[:100]
    actifs = Article.objects.filter(actif=True).select_related('etagere__bloc')
    if q:
        articles = list(actifs.filter(code__iexact=q)[:1])
        compact = re.sub(r'[-./\s]', '', q)
        if not articles and compact:
            articles = list(actifs.annotate(compact=_code_compact(F('code'))).filter(compact__iexact=compact)[:1])
        exact = bool(articles)
        premier = _motif(_singulier((normaliser(q).split() or [''])[0]))
        rang = Case(When(code__istartswith=q, then=Value(0)), When(designation__iregex=f'^{premier}', then=Value(1)),
                    When(designation__iregex=fr'\b{premier}', then=Value(2)), default=Value(3),
                    output_field=IntegerField())
        articles += _filtrer_texte(actifs, q).exclude(pk__in=[a.pk for a in articles]).annotate(rang=rang) \
            .order_by('rang', 'designation', 'code')[:15 - len(articles)]
        if len(articles) < 5 and len(q) >= 4 and not exact:
            deja = {a.pk for a in articles}
            articles += [a for a in recherche_locale([q], limite=15) if a.pk not in deja][:15 - len(articles)]
    else:
        articles = list(actifs[:15])
    return articles


@login_required
@require_POST
def api_article_creer(request):
    """Article inconnu pendant une entrée : création rapide (désignation, unité, catégorie ; code automatique)."""
    try:
        donnees = json.loads(request.body or b'{}')
    except ValueError:
        donnees = None
    if not isinstance(donnees, dict):
        return JsonResponse({'erreurs': ['Données illisibles : rechargez la page.']}, status=400)
    designation = _espaces(str(donnees.get('designation') or ''))
    meme_nom = Article.objects.filter(designation__iexact=designation, actif=True).first() if designation else None
    if meme_nom:
        return JsonResponse({'erreurs': [f'« {meme_nom.designation} » existe déjà ({meme_nom.code}) : '
                                         'choisissez-le dans la liste.']}, status=400)
    form = ArticleForm({
        'designation': designation, 'unite': str(donnees.get('unite') or 'U'),
        'categorie_nom': _espaces(str(donnees.get('categorie') or '')), 'code': '', 'stock_min': '', 'actif': 'on',
    })
    if not form.is_valid():
        return JsonResponse({'erreurs': [f'{form[champ].label} : {_minuscule(m)}' if champ in form.fields else m
                                         for champ, liste in form.errors.items() for m in liste]}, status=400)
    with transaction.atomic():
        article = form.save()
    return JsonResponse({'article': _article_choix(article)}, status=201)


# =========================================================
# Accueil
# =========================================================

@login_required
def accueil(request):
    articles = Article.objects.filter(actif=True)
    debut_mois = timezone.localdate().replace(day=1)
    derniers = sorted(
        list(BonEntree.objects.select_related('fournisseur')[:6]) + list(BonSortie.objects.select_related('chantier')[:6]),
        key=lambda b: b.date, reverse=True)[:6]
    contexte = {
        'nb_articles': articles.count(),
        'valeur_stock': nombre(articles.aggregate(v=Sum(VALEUR))['v'] or 0),
        'sorties_mois': nombre(rapports.total_sorties(debut_mois)),
        'alertes': articles.en_alerte().select_related('etagere__bloc').order_by('stock')[:8],
        'derniers': [(b, NOMS_BONS[type(b)]) for b in derniers],
    }
    if request.user.is_staff:
        contexte['premiers_pas'] = _premiers_pas()
        contexte['magasin_vide'] = not Article.objects.exists() and not Bloc.objects.exists()
    return render(request, 'stock/accueil.html', contexte)


def _premiers_pas():
    """Carte « Bien démarrer » de l'accueil : étapes cochées selon l'état réel du magasin. None quand tout est fait
    ou que le guide est masqué. Les données d'exemple ne comptent pas."""
    from .management.commands.demo import REFERENCE_EXEMPLE

    p = Parametres.actuels()
    if p.premiers_pas_masques:
        return None
    exemple = BonEntree.objects.filter(reference=REFERENCE_EXEMPLE).values_list('numero', flat=True).first()
    stock = MouvementStock.objects.filter(quantite__gt=0, type__in=[MouvementStock.ENTREE, MouvementStock.INVENTAIRE])
    etapes = [
        {'titre': 'Ma société', 'texte': 'Nom, et adresse ou téléphone : ils sont imprimés en haut de vos bons.',
         'fait': bool(p.nom_societe and (p.adresse or p.telephone)),
         'liens': [('Compléter', reverse('stock:reglages_societe'))],
         'revoir': ('Modifier', reverse('stock:reglages_societe'))},
        {'titre': 'Plan du magasin', 'texte': 'Vos blocs et étagères : chaque article aura sa place, visible en 3D.',
         'fait': not exemple and Etagere.objects.exists(),
         'liens': [('Préparer le plan', reverse('stock:reglages_plan'))],
         'revoir': ('Voir le plan', reverse('stock:reglages_plan'))},
        {'titre': 'Articles', 'texte': 'Importez votre liste Excel en une fois, ou ajoutez les articles un par un.',
         'fait': not exemple and Article.objects.exists(),
         'liens': [('Importer depuis Excel', reverse('stock:import_articles')),
                   ('Nouvel article', reverse('stock:article_nouveau'))],
         'revoir': ('Voir les articles', reverse('stock:articles'))},
        {'titre': 'Stock de départ', 'texte': "Les quantités présentes aujourd'hui : colonne « Stock de départ » de "
                                              "l'import Excel, ou une entrée de stock.",
         'fait': (stock.exclude(reference=exemple) if exemple else stock).exists(),
         'liens': [('Entrée de stock', reverse('stock:entree'))], 'revoir': ('Voir les bons', reverse('stock:bons'))},
        {'titre': 'Équipe', 'texte': 'Un compte pour chaque magasinier : on sait qui a fait chaque entrée et chaque '
                                     'sortie.',
         'fait': get_user_model().objects.filter(is_active=True).count() >= 2,
         'liens': [('Créer un compte', reverse('stock:utilisateur_nouveau'))],
         'revoir': ('Voir les comptes', reverse('stock:reglages_utilisateurs'))},
    ]
    faites = sum(e['fait'] for e in etapes)
    if faites == len(etapes):
        return None
    next(e for e in etapes if not e['fait'])['suivante'] = True
    return {'etapes': etapes, 'faites': faites, 'total': len(etapes), 'pourcent': round(100 * faites / len(etapes)),
            'exemple': bool(exemple)}


@responsable
@require_POST
def premiers_pas(request):
    """Masque (ou réaffiche, afficher=1) la carte « Bien démarrer » de l'accueil."""
    afficher = request.POST.get('afficher') == '1'
    p = Parametres.objects.filter(pk=1).first() or Parametres(pk=1)
    p.premiers_pas_masques = not afficher
    p.save()
    if afficher:
        messages.success(request, "Le guide « Bien démarrer » est de nouveau affiché sur l'accueil.")
    else:
        messages.success(request, 'Guide « Bien démarrer » masqué. Pour le revoir : Réglages › Ma société.')
    return redirect('stock:accueil')


@responsable
@require_POST
def exemple_installer(request):
    """Magasin vide : installe le magasin d'exemple (commande demo) pour essayer le logiciel."""
    from django.core.management.base import CommandError

    from .management.commands.demo import Command as Demo

    if Article.objects.exists() or Bloc.objects.exists():
        messages.error(request, "Le magasin contient déjà des articles ou un plan : l'exemple ne s'installe que dans "
                                'un magasin vide.')
        return redirect('stock:accueil')
    try:
        call_command(Demo(), stdout=io.StringIO())
    except (CommandError, IntegrityError):
        messages.error(request, "L'exemple n'a pas pu être installé. Rien n'a été modifié.")
    else:
        messages.success(request, "Magasin d'exemple installé : essayez « Trouver un article », une sortie ou les "
                                  'rapports. Ces articles sont fictifs.')
    return redirect('stock:accueil')


# =========================================================
# Entrées et sorties
# =========================================================

def _identifiant(texte):
    """« 12 » → 12 ; None pour tout ce qui n'est pas un identifiant (« abc », « ² », nombre géant…)."""
    texte = (texte or '').strip()
    return int(texte) if texte.isascii() and texte.isdigit() and len(texte) < 19 else None


def _minuscule(texte):
    return texte[:1].lower() + texte[1:]


def _espaces(texte):
    """« Chantier  Pont » → « Chantier Pont » : espaces en trop et caractères de contrôle retirés."""
    return ' '.join(re.sub(r'[\x00-\x08\x0e-\x1f\x7f]', '', texte or '').split())  # \t, \n… : espaces


def _lire_lignes(request, avec_prix, vides_ignorees=False):
    """Lit les lignes du formulaire. Renvoie (lignes, erreurs, lignes_pour_reafficher).

    vides_ignorees (retour) : un article sans quantité (ou 0) n'est pas revenu, sa ligne est ignorée."""
    ids = request.POST.getlist('article')

    def colonne(nom):
        valeurs = request.POST.getlist(nom)[:len(ids)]
        return valeurs + [''] * (len(ids) - len(valeurs))

    prix = colonne('prix') if avec_prix else [''] * len(ids)
    articles = Article.objects.select_related('etagere__bloc').in_bulk(
        [n for n in map(_identifiant, ids) if n is not None])
    lignes, erreurs, reaffichage = [], [], []
    for n, (i, q, p, texte) in enumerate(zip(ids, colonne('quantite'), prix, colonne('article_texte')), start=1):
        texte = _espaces(texte)[:200]
        if not i and not q.strip() and not texte:
            continue
        article = articles.get(_identifiant(i))
        if not article:
            # Texte tapé mais article pas choisi : la ligne revient telle quelle, rien n'est perdu.
            reaffichage.append({'texte': texte, 'quantite': q, 'prix_saisi': p})
            erreurs.append(f'Ligne {n} : « {texte[:60]} » n\'est pas choisi : cliquez sur l\'article dans la liste.'
                           if texte else f'Ligne {n} : choisissez un article dans la liste.')
            continue
        reaffichage.append({**_article_choix(article), 'quantite': q, 'prix_saisi': p})
        try:
            quantite_ = nombre_saisi(q)
            if not quantite_ and vides_ignorees:
                continue
            if not quantite_:
                raise ValidationError('Indiquez une quantité plus grande que 0.')
        except ValidationError as e:
            erreurs.append(f'Ligne {n} – {article.designation} – quantité : {_minuscule(e.messages[0])}')
            continue
        try:
            prix_ = prix_saisi(p) if avec_prix else None
        except ValidationError as e:
            erreurs.append(f'Ligne {n} – {article.designation} – prix : {_minuscule(e.messages[0])}')
            continue
        lignes.append((article, quantite_, prix_ or Decimal('0')))
    if not lignes and not erreurs:
        erreurs.append('Indiquez la quantité rapportée pour au moins un article.' if vides_ignorees and reaffichage
                       else 'Ajoutez au moins un article.')
    return lignes, erreurs, reaffichage


def _nouveau_jeton(request):
    """Jeton anti double envoi, mis dans un champ caché du formulaire et gardé en session."""
    jeton = uuid.uuid4().hex
    request.session['jetons_bons'] = request.session.get('jetons_bons', [])[-49:] + [jeton]
    return jeton


def _jeton_recu(request):
    jeton = request.POST.get('jeton', '')
    return jeton if jeton in request.session.get('jetons_bons', []) else None


def _deja_enregistre(request, Modele, jeton):
    """Formulaire déjà envoyé (double clic, page renvoyée) : on montre le bon créé la première fois."""
    bon = Modele.objects.filter(jeton=jeton).first() if jeton else None
    if bon is None:
        return None
    messages.info(request, f'{bon.numero} était déjà enregistré : il n\'a pas été créé une deuxième fois.')
    return redirect('stock:bon_detail', NOMS_BONS[Modele], bon.pk)


def _date_saisie(request, erreurs):
    """Date du bon choisie dans le formulaire (maintenant si la case est vide)."""
    try:
        return date_du_bon(request.POST.get('date'), responsable=request.user.is_staff)
    except ValidationError as e:
        erreurs.insert(0, e.messages[0])
        return None


def _contexte_date(request):
    aujourd_hui = timezone.localdate()
    mini = '' if request.user.is_staff else (aujourd_hui - timedelta(days=JOURS_RETOUR_MAGASINIER)).isoformat()
    return {'aujourd_hui': aujourd_hui.isoformat(), 'date_mini': mini, 'jours_retour': JOURS_RETOUR_MAGASINIER}


def _par_nom(modele, champ, valeur):
    valeur = _espaces(valeur)
    if not valeur:
        return None
    return modele.objects.filter(**{f'{champ}__iexact': valeur}).first() or modele.objects.create(**{champ: valeur})


# Chantier et engin d'une sortie : (case du formulaire, champ du modèle, messages).
LISTES_SORTIE = {
    Chantier: ('chantier', 'nom', {
        'inconnu': 'Le chantier « {nom} » n\'existe pas encore : vérifiez l\'orthographe, '
                   'ou cochez « Oui, créer ce chantier ».',
        'inactif': 'Le chantier « {nom} » est clôturé : choisissez un chantier en cours '
                   '(un responsable peut le rouvrir dans Chantiers, engins…).',
    }),
    Engin: ('engin', 'code', {
        'inconnu': 'L\'engin « {nom} » n\'existe pas encore : vérifiez le code, ou cochez « Oui, créer cet engin ».',
        'inactif': 'L\'engin « {nom} » n\'est plus en service : choisissez un autre engin ou laissez la case vide.',
    }),
}


def _trouver(modele, champ, nom):
    """Chantier ou engin existant à ce nom, sans tenir compte des majuscules ni des espaces en trop."""
    return modele.objects.filter(**{f'{champ}__iexact': nom}).first() or next(
        (o for o in modele.objects.all() if _espaces(getattr(o, champ)).lower() == nom.lower()), None)  # « é », « É »


def _verifier_nom(request, modele, erreurs, retour=False):
    """Chantier ou engin tapé dans une sortie → (existant, nom à créer, proposition à afficher).

    Un nom inconnu n'est créé que si la case « Oui, créer… » est cochée : sans cela, une faute de frappe
    créerait un second chantier et couperait les rapports en deux. Un chantier clôturé est refusé.
    Retour : un chantier clôturé est permis (on rapporte le reste en fin de chantier), un chantier inconnu
    est refusé (rien n'y est parti)."""
    case, champ, textes = LISTES_SORTIE[modele]
    nom = _espaces(request.POST.get(case))[:modele._meta.get_field(champ).max_length]
    if not nom:
        return None, None, None
    existant = _trouver(modele, champ, nom)
    if existant:
        if not existant.actif and not retour:
            erreurs.append(textes['inactif'].format(nom=getattr(existant, champ)))
        return existant, None, None
    connus = modele.objects.all() if retour else modele.objects.filter(actif=True)
    connus = {n.lower(): n for n in connus.values_list(champ, flat=True)}
    proches = [connus[n] for n in difflib.get_close_matches(nom.lower(), list(connus), n=3, cutoff=0.6)]
    if retour and modele is Chantier:
        erreurs.append(f'Le chantier « {nom} » n\'existe pas : choisissez dans la liste le chantier '
                       'd\'où revient la marchandise.')
        return None, None, {'nom': nom, 'proches': proches}
    confirme = _espaces(request.POST.get(f'creer_{case}')).lower() == nom.lower()
    if not confirme:
        erreurs.append(textes['inconnu'].format(nom=nom))
    return None, nom, {'nom': nom, 'proches': proches, 'coche': confirme}


def _prerempli(request, Modele):
    """Formulaire prêt à l'emploi : ?article=<pk> (une ligne prête) ou ?depuis=<pk> (« Refaire ce bon »).
    Renvoie (valeurs de l'en-tête, lignes)."""
    valeurs, lignes = {}, []
    if pk := _identifiant(request.GET.get('article')):
        article = Article.objects.select_related('etagere__bloc').filter(pk=pk, actif=True).first()
        lignes += [_article_choix(article)] if article else []
    pk = _identifiant(request.GET.get('depuis'))
    bon = Modele.objects.filter(pk=pk).first() if pk else None
    if bon is None:
        return valeurs, lignes
    oublies = 0
    for ligne in bon.lignes.select_related('article__etagere__bloc'):
        if not ligne.article.actif:
            oublies += 1
            continue
        lignes.append({**_article_choix(ligne.article), 'quantite': quantite(ligne.quantite),
                       'prix_saisi': quantite(ligne.prix_unitaire) if Modele is BonEntree and ligne.prix_unitaire else ''})
    valeurs['observation'] = bon.observation
    if Modele is BonEntree:
        valeurs['fournisseur'] = bon.fournisseur.nom if bon.fournisseur else ''
    else:
        valeurs['demandeur'] = bon.demandeur
        valeurs['chantier'] = bon.chantier.nom if bon.chantier.actif else ''
        valeurs['engin'] = bon.engin.code if bon.engin and bon.engin.actif else ''
        if not bon.chantier.actif:
            messages.warning(request, f'Le chantier « {bon.chantier.nom} » est clôturé : choisissez le chantier.')
    if oublies:
        messages.warning(request, f'Articles désactivés, non recopiés : {oublies}.')
    messages.info(request, f'Copie de {bon.numero} : vérifiez les quantités puis enregistrez.')
    return valeurs, lignes


@login_required
def entree(request):
    valeurs, lignes_initiales = _prerempli(request, BonEntree) if request.method == 'GET' else ({}, [])
    contexte = {'type': 'entree', 'fournisseurs': Fournisseur.objects.values_list('nom', flat=True),
                'lignes_initiales': lignes_initiales, 'valeurs': valeurs, **_contexte_date(request),
                'unites': Article.UNITES, 'categories': Categorie.objects.values_list('nom', flat=True)}
    jeton = None
    if request.method == 'POST':
        jeton = _jeton_recu(request)
        if deja := _deja_enregistre(request, BonEntree, jeton):
            return deja
        lignes, erreurs, reaffichage = _lire_lignes(request, avec_prix=True)
        date_bon = _date_saisie(request, erreurs)
        if not erreurs:
            try:
                with transaction.atomic():
                    if deja := _deja_enregistre(request, BonEntree, jeton):  # envoi simultané : relu sous verrou
                        return deja
                    bon = BonEntree.objects.create(
                        date=date_bon,
                        fournisseur=_par_nom(Fournisseur, 'nom', request.POST.get('fournisseur')),
                        reference=request.POST.get('reference', '').strip()[:100],
                        observation=request.POST.get('observation', '').strip(),
                        cree_par=request.user, jeton=jeton,
                    )
                    LigneEntree.objects.bulk_create(
                        [LigneEntree(bon=bon, article=a, quantite=q, prix_unitaire=p) for a, q, p in lignes])
                    services.valider(bon, request.user)
            except services.StockError as e:
                erreurs = e.messages
            else:
                messages.success(request, f'Entrée {bon.numero} enregistrée : le stock est mis à jour.')
                return _bon_enregistre('entree', bon)
        contexte.update(erreurs=erreurs, lignes_initiales=reaffichage, valeurs=request.POST)
    contexte['jeton'] = jeton or _nouveau_jeton(request)
    return render(request, 'stock/bon_form.html', contexte)


@login_required
def sortie(request):
    valeurs, lignes_initiales = _prerempli(request, BonSortie) if request.method == 'GET' else ({}, [])
    contexte = {'type': 'sortie',
                'chantiers': Chantier.objects.filter(actif=True).values_list('nom', flat=True),
                'engins': Engin.objects.filter(actif=True).values_list('code', flat=True),
                'lignes_initiales': lignes_initiales, 'valeurs': {'demandeur': '', **valeurs},
                **_contexte_date(request)}
    jeton = None
    if request.method == 'POST':
        jeton = _jeton_recu(request)
        if deja := _deja_enregistre(request, BonSortie, jeton):
            return deja
        lignes, erreurs, reaffichage = _lire_lignes(request, avec_prix=False)
        date_bon = _date_saisie(request, erreurs)
        entete = []
        if not _espaces(request.POST.get('demandeur')):
            entete.append('Indiquez qui reçoit la marchandise.')
        if not _espaces(request.POST.get('chantier')):
            entete.append('Indiquez le chantier.')
        chantier, chantier_a_creer, contexte['nouveau_chantier'] = _verifier_nom(request, Chantier, entete)
        engin, engin_a_creer, contexte['nouvel_engin'] = _verifier_nom(request, Engin, entete)
        erreurs = entete + erreurs
        if not erreurs:
            try:
                with transaction.atomic():
                    if deja := _deja_enregistre(request, BonSortie, jeton):  # envoi simultané : relu sous verrou
                        return deja
                    bon = BonSortie.objects.create(
                        date=date_bon,
                        chantier=chantier or _par_nom(Chantier, 'nom', chantier_a_creer),
                        engin=engin or _par_nom(Engin, 'code', engin_a_creer),
                        demandeur=_espaces(request.POST['demandeur'])[:150],
                        observation=request.POST.get('observation', '').strip(),
                        cree_par=request.user, jeton=jeton,
                    )
                    LigneSortie.objects.bulk_create([LigneSortie(bon=bon, article=a, quantite=q) for a, q, _ in lignes])
                    services.valider(bon, request.user)
            except services.StockError as e:
                erreurs = e.messages
            else:
                messages.success(request, f'Sortie {bon.numero} enregistrée : le stock est mis à jour.')
                return _bon_enregistre('sortie', bon)
        valeurs = request.POST.copy()
        for champ in ['chantier', 'engin', 'demandeur']:
            valeurs[champ] = _espaces(valeurs.get(champ))
        contexte.update(erreurs=erreurs, lignes_initiales=reaffichage, valeurs=valeurs)
    contexte['jeton'] = jeton or _nouveau_jeton(request)
    return render(request, 'stock/bon_form.html', contexte)


def _chantiers_du_retour():
    """Chantiers où il reste de la marchandise partie du magasin et pas encore revenue."""
    restes = (MouvementStock.objects.filter(type__in=services.TYPES_CHANTIER, chantier__isnull=False)
              .values('chantier__nom', 'article_id').annotate(s=Sum('quantite')).filter(s__lt=0))
    return sorted({r['chantier__nom'] for r in restes}, key=str.lower)


def _prerempli_retour(request):
    """« Retour de ce bon » (?sortie=<pk>) : chantier, engin et articles de la sortie repris, quantités à taper.
    Renvoie (valeurs de l'en-tête, lignes, sortie d'origine)."""
    pk = _identifiant(request.GET.get('sortie'))
    sortie = BonSortie.objects.select_related('chantier', 'engin').filter(pk=pk).first() if pk else None
    if sortie is None:
        return {}, [], None
    if sortie.statut != BonSortie.VALIDE:
        messages.warning(request, f'{sortie.numero} est {sortie.get_statut_display().lower()} : '
                                  'rien n\'est parti sur le chantier avec ce bon.')
        return {}, [], None
    lignes, vus = [], set()
    for ligne in sortie.lignes.select_related('article__etagere__bloc'):
        if ligne.article_id not in vus:
            vus.add(ligne.article_id)
            lignes.append({**_article_choix(ligne.article), 'quantite': ''})
    return {'chantier': sortie.chantier.nom, 'engin': sortie.engin.code if sortie.engin else ''}, lignes, sortie


@login_required
def retour(request):
    """Retour de chantier : marchandise non utilisée rapportée au magasin (le stock remonte, la consommation
    du chantier baisse)."""
    valeurs, lignes_initiales, origine = _prerempli_retour(request) if request.method == 'GET' else ({}, [], None)
    contexte = {'type': 'retour', 'chantiers': _chantiers_du_retour(),
                'engins': Engin.objects.filter(actif=True).values_list('code', flat=True),
                'lignes_initiales': lignes_initiales, 'valeurs': {'rapporte_par': '', **valeurs},
                'origine': origine, **_contexte_date(request)}
    jeton = None
    if request.method == 'POST':
        jeton = _jeton_recu(request)
        if deja := _deja_enregistre(request, BonRetour, jeton):
            return deja
        lignes, erreurs, reaffichage = _lire_lignes(request, avec_prix=False, vides_ignorees=True)
        date_bon = _date_saisie(request, erreurs)
        entete = []
        if not _espaces(request.POST.get('chantier')):
            entete.append('Indiquez le chantier d\'où revient la marchandise.')
        if not _espaces(request.POST.get('rapporte_par')):
            entete.append('Indiquez qui rapporte la marchandise.')
        chantier, _, contexte['nouveau_chantier'] = _verifier_nom(request, Chantier, entete, retour=True)
        engin, engin_a_creer, contexte['nouvel_engin'] = _verifier_nom(request, Engin, entete, retour=True)
        # Sortie d'origine (« Retour de ce bon ») : gardée seulement si le chantier n'a pas été changé.
        pk = _identifiant(request.POST.get('sortie'))
        origine = contexte['origine'] = BonSortie.objects.filter(
            pk=pk, statut=BonSortie.VALIDE, chantier=chantier).first() if pk and chantier else None
        if origine and date_bon and timezone.localtime(date_bon).date() < timezone.localtime(origine.date).date():
            entete.append(f'Date : le retour ne peut pas être daté avant la sortie {origine.numero} '
                          f'({timezone.localtime(origine.date):%d/%m/%Y}).')
        erreurs = entete + erreurs
        if not erreurs:
            try:
                with transaction.atomic():
                    if deja := _deja_enregistre(request, BonRetour, jeton):  # envoi simultané : relu sous verrou
                        return deja
                    bon = BonRetour.objects.create(
                        date=date_bon, chantier=chantier, sortie=origine,
                        engin=engin or _par_nom(Engin, 'code', engin_a_creer),
                        rapporte_par=_espaces(request.POST['rapporte_par'])[:150],
                        observation=request.POST.get('observation', '').strip(),
                        cree_par=request.user, jeton=jeton,
                    )
                    LigneRetour.objects.bulk_create([LigneRetour(bon=bon, article=a, quantite=q) for a, q, _ in lignes])
                    services.valider(bon, request.user)
            except services.StockError as e:
                erreurs = e.messages
            else:
                messages.success(request, f'Retour {bon.numero} enregistré : le stock est mis à jour.')
                return _bon_enregistre('retour', bon)
        valeurs = request.POST.copy()
        for champ in ['chantier', 'engin', 'rapporte_par']:
            valeurs[champ] = _espaces(valeurs.get(champ))
        contexte.update(erreurs=erreurs, lignes_initiales=reaffichage, valeurs=valeurs)
    contexte['jeton'] = jeton or _nouveau_jeton(request)
    return render(request, 'stock/bon_form.html', contexte)


@login_required
@require_GET
def api_reste_chantier(request):
    """Retour : par article, ce qui est parti sur le chantier et pas encore revenu (pour prévenir un retour en trop)."""
    nom = _espaces(request.GET.get('chantier'))[:150]
    chantier = _trouver(Chantier, 'nom', nom) if nom else None
    if chantier is None:
        return JsonResponse({'chantier': None, 'reste': {}})
    reste = services.reste_sur_chantier(chantier)
    return JsonResponse({'chantier': chantier.nom,
                         'reste': {str(pk): [str(q), quantite(q)] for pk, q in reste.items() if q > 0}})


def _bon_enregistre(type_, bon):
    """Après l'enregistrement : le détail du bon, avec « Imprimer le bon » mis en avant."""
    return redirect(f"{reverse('stock:bon_detail', args=[type_, bon.pk])}?nouveau=1")


@login_required
def bons(request):
    """Historique des bons, 50 par page : type, statut, période, et recherche (numéro, chantier, fournisseur,
    ou article des lignes)."""
    modeles = rapports.modeles_bons()
    type_ = request.GET.get('type', '')
    type_ = type_ if type_ in modeles else ''
    q = request.GET.get('q', '').strip()[:100]
    statut = request.GET.get('statut', '')
    statut = statut if statut in STATUTS_BONS else ''
    du, au = rapports.lire_date(request.GET.get('du'), None), rapports.lire_date(request.GET.get('au'), None)
    if du and au and du > au:
        du, au = au, du
    cles = []
    for nom, Modele in modeles.items():
        if type_ and type_ != nom:
            continue
        qs = Modele.objects.all()
        if statut:
            qs = qs.filter(statut=STATUTS_BONS[statut][0])
        if du:
            qs = qs.filter(date__gte=rapports.debut(du))
        if au:
            qs = qs.filter(date__lt=rapports.fin(au))
        if q:
            qs = qs.filter(rapports.recherche_bons(Modele, q)).distinct()
        cles += [(date, pk, nom) for pk, date in qs.values_list('pk', 'date')]
    cles.sort(reverse=True)
    page = views.paginer(request, cles, 50)
    charges = {}
    for nom, Modele in modeles.items():
        pks = [pk for _, pk, n in page.object_list if n == nom]
        if pks:
            for b in Modele.objects.filter(pk__in=pks).select_related(*rapports.relations_bon(Modele)) \
                    .prefetch_related('lignes__article'):
                charges[(nom, b.pk)] = b
    page.object_list = [(charges[(nom, pk)], nom) for _, pk, nom in page.object_list]
    for b, nom in page.object_list:
        b.pour_de = rapports.libelle_bon(nom, b)
    filtre_actif = bool(q or du or au or statut)
    return render(request, 'stock/bons.html', {
        'bons': page.object_list, 'page': page, 'type': type_, 'q': q, 'statut': statut, 'du': du, 'au': au,
        'statut_libelle': STATUTS_BONS.get(statut, ('', ''))[1], 'statuts': STATUTS_BONS,
        'onglets': [{'libelle': libelle, 'actif': cle == type_, 'lien': views._lien(request, type=cle, page=None)}
                    for cle, libelle in (('', 'Tous'), ('entree', 'Entrées'), ('sortie', 'Sorties'),
                                         ('retour', 'Retours'), ('inventaire', 'Corrections'))
                    if not cle or cle in modeles],
        'raccourcis': [{'libelle': 'Toutes les dates', 'actif': not du and not au,
                        'lien': views._lien(request, du=None, au=None, page=None)}] + views._raccourcis(request, du, au),
        'filtre_actif': filtre_actif,
        'lien_tout_statut': views._lien(request, statut=None, page=None),
        'lien_effacer': views._lien(request, q=None, du=None, au=None, statut=None, page=None),
    })


def _bon(type_, pk):
    Modele = TYPES_BONS.get(type_)
    if Modele is None:
        raise Http404
    return get_object_or_404(Modele, pk=pk)


@login_required
def bon_detail(request, type_, pk):
    bon = _bon(type_, pk)
    saisi_le = bon.date_validation
    if saisi_le and timezone.localtime(saisi_le).date() == timezone.localtime(bon.date).date():
        saisi_le = None  # affiché seulement pour un bon daté après coup
    return render(request, 'stock/bon_detail.html', {
        'bon': bon, 'type': type_, 'lignes': bon.lignes.select_related('article'),
        'modele': type(bon)._meta.model_name, 'saisi_le': saisi_le,
        'retours': bon.retours.order_by('date', 'pk') if type_ == 'sortie' else [],
        # Bon tout juste enregistré : on invite à l'imprimer pour le faire signer.
        'nouveau': request.GET.get('nouveau') == '1' and type_ != 'inventaire' and bon.statut == bon.VALIDE,
    })


@responsable
@require_POST
def bon_annuler(request, type_, pk):
    bon = _bon(type_, pk)
    motif = request.POST.get('motif', '').strip()
    if not motif:
        messages.error(request, "Indiquez le motif de l'annulation.")
        return redirect('stock:bon_detail', type_, pk)
    try:
        services.annuler(bon, request.user, motif)
        messages.success(request, f'{bon.numero} est annulé : le stock est remis comme avant.')
    except services.StockError as e:
        messages.error(request, ' '.join(e.messages))
    return redirect('stock:bon_detail', type_, pk)


@responsable
@require_POST
def bon_valider(request, type_, pk):
    """Brouillon (créé dans l'administration) : validation par un responsable."""
    bon = _bon(type_, pk)
    try:
        services.valider(bon, request.user)
        messages.success(request, f'{bon.numero} est validé : le stock est mis à jour.')
    except services.StockError as e:
        messages.error(request, ' '.join(e.messages))
    return redirect('stock:bon_detail', type_, pk)


@responsable
@require_POST
def bon_supprimer(request, type_, pk):
    bon = _bon(type_, pk)
    with transaction.atomic():
        bon = type(bon).objects.select_for_update().get(pk=bon.pk)
        if bon.statut != bon.BROUILLON:
            messages.error(request, f'{bon.numero} est {bon.get_statut_display().lower()} : '
                                    'seul un brouillon peut être supprimé.')
            return redirect('stock:bon_detail', type_, pk)
        bon.delete()
    messages.success(request, f'Brouillon {bon.numero} supprimé.')
    return redirect('stock:bons')


# =========================================================
# Articles
# =========================================================

FILTRES_ARTICLES = {'alerte': 'À commander', 'rupture': 'En rupture', 'inactifs': 'Désactivés'}


def _chercher_articles(qs, q):
    """Identifiants des articles de qs qui correspondent à q : d'abord les plus pertinents de la recherche tolérante
    (pluriels, fautes, autres noms), puis ceux qui contiennent simplement le texte (début de code, désactivés…)."""
    autorises = set(qs.values_list('pk', flat=True))
    ids = [a.pk for a in recherche_locale([q], limite=None) if a.pk in autorises]
    deja = set(ids)
    return ids + [pk for pk in _filtrer_texte(qs, q).values_list('pk', flat=True) if pk not in deja]


@login_required
def articles(request):
    """Liste des articles, 50 par page : recherche, catégorie, alerte / rupture, bloc et étagère (rangés par
    niveau) ; ?format=excel ; « À commander » : ?format=pdf ou excel avec les quantités à commander."""
    q = request.GET.get('q', '').strip()[:100]
    filtre = request.GET.get('filtre', '')
    filtre = filtre if filtre in FILTRES_ARTICLES else ''
    categorie = rapports._objet(Categorie, request.GET.get('categorie'))
    etagere = rapports._objet(Etagere, request.GET.get('etagere'))
    bloc = etagere.bloc if etagere else rapports._objet(Bloc, request.GET.get('bloc'))
    qs = Article.objects.filter(actif=filtre != 'inactifs').select_related('categorie', 'etagere__bloc')
    if filtre == 'alerte':
        qs = qs.en_alerte()
    elif filtre == 'rupture':
        qs = qs.filter(stock__lte=0)
    if categorie:
        qs = qs.filter(categorie=categorie)
    niveau = F('niveau').asc(nulls_last=True)
    if etagere:
        qs = qs.filter(etagere=etagere).order_by(niveau, 'case', 'code')
    elif bloc:
        qs = qs.filter(etagere__bloc=bloc).order_by('etagere__code', niveau, 'case', 'code')
    ids = _chercher_articles(qs, q) if q else None

    def tous():
        if ids is None:
            return list(qs)
        trouves = qs.in_bulk(ids)
        return [trouves[pk] for pk in ids]

    format_ = request.GET.get('format')
    if format_ in ('excel', 'pdf'):
        filtres = ' · '.join(filter(None, [FILTRES_ARTICLES.get(filtre, ''), str(categorie or ''),
                                           str(etagere or bloc or ''), f'« {q} »' if q else '']))
        if filtre == 'alerte':
            donnees = rapports.a_commander(qs if ids is None else qs.filter(pk__in=ids))
            if format_ == 'pdf':
                from .pdf import rapport_a_commander  # reportlab : chargé seulement à l'impression

                return livrer(request, rapport_a_commander(donnees, request.user))
            return livrer(request, exports.a_commander(donnees, request.user))
        if format_ == 'excel':
            return livrer(request, exports.etat_stock(tous(), request.user, filtres))
    if ids is None:
        page = views.paginer(request, qs, 50)
        total = qs.aggregate(v=Sum(VALEUR))['v']
    else:
        page = views.paginer(request, ids, 50)
        trouves = qs.in_bulk(page.object_list)
        page.object_list = [trouves[pk] for pk in page.object_list]
        valeurs = dict(qs.annotate(v=VALEUR).values_list('pk', 'v'))
        total = sum((valeurs[pk] for pk in ids), Decimal('0'))
    if filtre == 'alerte':
        for a in page.object_list:
            a.a_commander = rapports.quantite_a_commander(a)
            a.unite_commande = rapports.unite_qte(a.unite, a.a_commander)
    return render(request, 'stock/articles.html', {
        'articles': page.object_list, 'page': page, 'q': q, 'filtre': filtre, 'categorie': categorie,
        'categories': Categorie.objects.all(), 'bloc': bloc, 'etagere': etagere, 'blocs': Bloc.objects.all(),
        'etageres': bloc.etageres.all() if bloc else [],
        'total': nombre(total or 0),
        'catalogue_vide': not page.object_list and not Article.objects.exists(),
        'filtre_actif': bool(filtre or categorie or bloc),
        'lien_excel': views._lien(request, format='excel', page=None),
        'lien_pdf': views._lien(request, format='pdf', page=None),
        'lien_sans_recherche': views._lien(request, q=None, page=None),
        'lien_sans_filtres': views._lien(request, filtre=None, categorie=None, bloc=None, etagere=None, page=None),
        'lien_etiquettes': views._lien(request, filtre=None, page=None),
    })


@login_required
def article_historique(request, pk):
    """Fiche de stock d'un article : chaque mouvement avec son bon, stock après, consommation récente, derniers
    achats ; ?du=&au= (facultatifs), ?format=excel ou pdf (du plus ancien au plus récent)."""
    article = get_object_or_404(Article.objects.select_related('categorie', 'etagere__bloc'), pk=pk)
    du, au = rapports.lire_date(request.GET.get('du'), None), rapports.lire_date(request.GET.get('au'), None)
    if du and au and du > au:
        du, au = au, du
    fiche = rapports.fiche_stock(article, du, au)
    format_ = request.GET.get('format')
    if format_ == 'excel':
        return livrer(request, exports.fiche_stock(article, fiche, du, au, request.user))
    if format_ == 'pdf':
        from .pdf import rapport_fiche_stock  # reportlab : chargé seulement à l'impression

        return livrer(request, rapport_fiche_stock(article, fiche, du, au, request.user))
    page = views.paginer(request, fiche['mouvements'].select_related('chantier', 'engin', 'utilisateur'), 50)
    for m in rapports.decrire_mouvements(page.object_list):
        m.lien = views.lien_bon(m.type_bon, m.bon)
        m.unite_qte = rapports.unite_qte(article.unite, m.quantite)
        m.valeur = abs(m.quantite * m.prix_unitaire)
        m.entree, m.sortie = (m.quantite, None) if m.quantite > 0 else (None, -m.quantite)
    return render(request, 'stock/article_historique.html', {
        'article': article, 'fiche': fiche, 'page': page, 'du': du, 'au': au,
        'conso': rapports.consommation_article(article), 'achat': rapports.dernier_achat(article),
        'valeur_stock': article.stock * article.prix_moyen,
        'unite_stock': rapports.unite_qte(article.unite, article.stock),
        'raccourcis': [{'libelle': 'Tout', 'actif': not du and not au,
                        'lien': views._lien(request, du=None, au=None, page=None)}] + views._raccourcis(request, du, au),
        'lien_excel': views._lien(request, format='excel', page=None),
        'lien_pdf': views._lien(request, format='pdf', page=None),
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


@responsable
@require_POST
def article_corriger(request, pk):
    """Correction du stock après comptage : crée un petit inventaire validé."""
    article = get_object_or_404(Article, pk=pk)
    try:
        compte = nombre_saisi(request.POST.get('stock_compte'))
    except ValidationError as e:
        compte, erreur = None, e.messages[0]
    else:
        erreur = 'Indiquez la quantité comptée.'
    if compte is None:
        messages.error(request, f'Quantité comptée : {_minuscule(erreur)}')
    elif compte == article.stock:
        messages.info(request, 'Le stock était déjà juste : rien à corriger.')
    else:
        with transaction.atomic():
            inv = Inventaire.objects.create(cree_par=request.user,
                                            observation=request.POST.get('motif', '').strip() or 'Correction après comptage')
            LigneInventaire.objects.create(bon=inv, article=article, stock_compte=compte, compte_le=timezone.now())
            services.valider(inv, request.user)
        messages.success(request, f'Stock de {article.code} corrigé : {quantite(compte)} {article.unite} ({inv.numero}).')
    return redirect('stock:article_modifier', pk)


# =========================================================
# Import Excel des articles (responsables)
# =========================================================

LIMITE_APERCU = 50
LIMITE_APERCU_TOUT = 2000


def _import_en_cours(request):
    """Fichier envoyé pour l'import (son jeton est en session) et son analyse ; (None, None) s'il n'y en a pas."""
    infos = request.session.get('import_articles') or {}
    chemin = imports.fichier_garde(infos.get('jeton'))
    if not chemin:
        return None, None
    feuille, lignes = imports.lire(chemin, infos.get('nom', ''))
    return infos, imports.analyser(lignes, infos.get('nom', ''), feuille)


@responsable
def import_articles(request):
    """Import Excel, étape 1 : modèle à télécharger et envoi du fichier (.xlsx ou .csv)."""
    if request.method == 'POST':
        fichier = request.FILES.get('fichier')
        if not fichier:
            messages.error(request, 'Choisissez votre fichier Excel (.xlsx) ou CSV.')
        else:
            jeton = None
            try:
                jeton = imports.garder(fichier)
                feuille, lignes = imports.lire(imports.fichier_garde(jeton), fichier.name)
                imports.analyser(lignes, fichier.name, feuille)
            except imports.ImportImpossible as e:
                imports.oublier(jeton)
                messages.error(request, str(e))
            else:
                imports.oublier((request.session.get('import_articles') or {}).get('jeton'))
                request.session['import_articles'] = {'jeton': jeton, 'nom': fichier.name[-150:]}
                return redirect('stock:import_apercu')
    return render(request, 'stock/import_articles.html', {
        'etape': 'envoi', 'colonnes': imports.COLONNES, 'unites': imports.unites_acceptees(),
        'max_lignes': imports.MAX_LIGNES, 'nb_articles': Article.objects.count(),
    })


@responsable
def import_apercu(request):
    """Étape 2 : chaque ligne du fichier avant l'import (prête, à vérifier ou ignorée) ; ?format=excel."""
    try:
        infos, analyse = _import_en_cours(request)
    except imports.ImportImpossible as e:
        messages.error(request, str(e))
        return redirect('stock:import_articles')
    if not analyse:
        messages.info(request, "Choisissez d'abord le fichier à importer.")
        return redirect('stock:import_articles')
    if request.GET.get('format') == 'excel':
        return livrer(request, imports.rapport_excel(imports.lignes_rapport(analyse), 'apercu_import_articles',
                                                     "Aperçu de l'import", analyse.nom_fichier, request.user))
    problemes = sorted(analyse.erreurs + analyse.avertissements, key=lambda l: l.numero)
    limite = LIMITE_APERCU_TOUT if request.GET.get('tout') else LIMITE_APERCU
    frequentes = Counter(m for l in problemes for m in l.messages)
    return render(request, 'stock/import_articles.html', {
        'etape': 'apercu', 'analyse': analyse, 'problemes': problemes[:limite], 'nb_problemes': len(problemes),
        'autres_problemes': max(0, len(problemes) - limite),
        'frequentes': [(m, n) for m, n in frequentes.most_common(6) if n >= 3],
        'prets': analyse.prets[:30], 'autres_prets': max(0, len(analyse.prets) - 30),
        'codes_auto': sum(1 for l in analyse.importables if l.code_auto),
    })


@responsable
@require_POST
def import_confirmer(request):
    """Étape 3 : import en une transaction (tout ou rien), puis résultat et rapport."""
    try:
        with transaction.atomic():  # relu et importé d'un bloc : un double envoi ne crée rien de plus
            infos, analyse = _import_en_cours(request)
            if not analyse:
                if request.session.get('import_resultat'):
                    return redirect('stock:import_resultat')
                messages.info(request, "Choisissez d'abord le fichier à importer.")
                return redirect('stock:import_articles')
            if not analyse.importables:
                messages.error(request, 'Aucune ligne à importer : corrigez le fichier, puis envoyez-le de nouveau.')
                return redirect('stock:import_apercu')
            debut = timezone.now()  # « Imprimer leurs étiquettes » : seulement les articles créés à partir d'ici
            resultat = imports.importer(analyse, request.user,
                                        creer_emplacements=request.POST.get('emplacements') != 'non')
    except imports.ImportImpossible as e:
        messages.error(request, str(e))
        return redirect('stock:import_articles')
    except (IntegrityError, services.StockError):
        messages.error(request, "L'import n'a pas pu se faire (un article a peut-être été créé entre-temps) : rien "
                                "n'a été enregistré. Vérifiez l'aperçu et recommencez.")
        return redirect('stock:import_apercu')
    imports.garder_rapport(infos['jeton'], analyse, resultat)
    imports.oublier(infos['jeton'])
    request.session['import_articles'] = {}
    request.session['import_resultat'] = {
        'jeton': infos['jeton'], 'fichier': analyse.nom_fichier, 'crees': resultat.crees, 'ignores': resultat.ignores,
        'bon': [resultat.bon.pk, resultat.bon.numero] if resultat.bon else None,
        'nb_lignes_bon': resultat.nb_lignes_bon, 'valeur': str(resultat.valeur_stock),
        'categories': resultat.categories, 'blocs': resultat.blocs, 'etageres': resultat.etageres,
        'non_ranges': resultat.non_ranges, 'duree': round(resultat.duree, 2), 'apres': debut.isoformat(),
    }
    return redirect('stock:import_resultat')


@responsable
def import_resultat(request):
    resultat = request.session.get('import_resultat')
    if not resultat:
        return redirect('stock:import_articles')
    # Import fait avant la mise à jour : seule la date du jour avait été gardée.
    filtre = {'apres': resultat['apres']} if resultat.get('apres') else {'depuis': resultat.get('jour', '')}
    return render(request, 'stock/import_articles.html', {
        'etape': 'resultat', 'resultat': resultat, 'valeur': Decimal(resultat['valeur']),
        'lien_etiquettes': f"{reverse('stock:etiquettes')}?{urlencode(filtre)}",
    })


@responsable
def import_modele(request):
    # Version bureau : un modèle déjà rempli dans Documents n'est jamais remplacé par un modèle vide.
    return livrer(request, imports.modele(), remplacer=False)


@responsable
def import_rapport(request):
    """Rapport du dernier import (Excel) : chaque ligne du fichier, créée ou ignorée, et pourquoi."""
    rapport = imports.lire_rapport((request.session.get('import_resultat') or {}).get('jeton'))
    if not rapport:
        messages.error(request, "Rapport introuvable : il est gardé deux jours après l'import.")
        return redirect('stock:import_articles')
    return livrer(request, imports.rapport_excel(
        rapport['lignes'], 'rapport_import_articles', "Rapport d'import",
        f"{rapport['fichier']} · {rapport['date']} · {rapport['crees']} créé(s) · {rapport['ignores']} ignoré(s)",
        request.user))


# =========================================================
# Étiquettes avec QR code
# =========================================================

LIMITE_CASES = 300  # au-delà, pas de cases à cocher : tous les articles trouvés sont imprimés
LIMITE_ETIQUETTES = 3000
ORDRE_MAGASIN = [F('etagere__bloc__code').asc(nulls_last=True), F('etagere__code').asc(nulls_last=True),
                 F('niveau').asc(nulls_last=True), 'case', 'code']
FILTRES_ETIQUETTES = ('q', 'categorie', 'bloc', 'etagere', 'depuis', 'apres', 'article')


def _identifiants(valeurs):
    return [int(v) for v in valeurs if v.isascii() and v.isdigit() and len(v) < 19]


def _moment(texte):
    """« 2026-10-14T09:30:00.123456+00:00 » (heure du début d'un import) → date et heure ; None si illisible."""
    try:
        moment = datetime.fromisoformat((texte or '').strip()[:40])
        if timezone.is_naive(moment):
            moment = timezone.make_aware(moment)
        return moment.astimezone(dt_timezone.utc)  # année 1 ou 9999 avec un décalage : refusée ici, pas en base
    except (ValueError, OverflowError):
        return None


def _entier(texte, defaut, mini, maxi):
    try:
        return max(mini, min(maxi, int(texte)))
    except (TypeError, ValueError):
        return defaut


def _filtres_etiquettes(params):
    """Filtres de l'écran Étiquettes (lus dans GET ou POST) et articles actifs qui y répondent, dans l'ordre du
    magasin (bloc, étagère, niveau) : les étiquettes sortent dans l'ordre où on les colle."""
    etagere = rapports._objet(Etagere, params.get('etagere'))
    choix = {
        'q': params.get('q', '').strip()[:100],
        'categorie': rapports._objet(Categorie, params.get('categorie')),
        'etagere': etagere,
        'bloc': etagere.bloc if etagere else rapports._objet(Bloc, params.get('bloc')),
        'depuis': rapports.lire_date(params.get('depuis'), None),
        'apres': _moment(params.get('apres')),
        'articles': _identifiants(params.getlist('article'))[:LIMITE_CASES],
    }
    qs = Article.objects.filter(actif=True).select_related('categorie', 'etagere__bloc')
    if choix['articles']:
        qs = qs.filter(pk__in=choix['articles'])
    if choix['categorie']:
        qs = qs.filter(categorie=choix['categorie'])
    if etagere:
        qs = qs.filter(etagere=etagere)
    elif choix['bloc']:
        qs = qs.filter(etagere__bloc=choix['bloc'])
    if choix['depuis']:
        qs = qs.filter(cree_le__gte=rapports.debut(choix['depuis']))
    if choix['apres']:  # articles créés par un import (lien « Imprimer leurs étiquettes »)
        qs = qs.filter(cree_le__gte=choix['apres'])
    if choix['q']:
        qs = qs.filter(pk__in=_chercher_articles(qs, choix['q']))
    return choix, qs.order_by(*ORDRE_MAGASIN)


def _lien_etiquettes(params, **changements):
    """Adresse de l'écran Étiquettes avec les mêmes filtres (et ce qui change)."""
    garder = {cle: params.getlist(cle) for cle in FILTRES_ETIQUETTES + ('quoi',) if params.get(cle)}
    garder.update({cle: valeur for cle, valeur in changements.items() if valeur})
    for cle, valeur in changements.items():
        if not valeur:
            garder.pop(cle, None)
    return reverse('stock:etiquettes') + (f'?{urlencode(garder, doseq=True)}' if garder else '')


@responsable
def etiquettes(request):
    """Étiquettes avec QR code à coller : articles (filtres, cases à cocher, format, exemplaires, planche entamée)
    ou étagères (une grande étiquette chacune). L'impression (POST) renvoie le PDF."""
    from . import etiquettes as planches  # reportlab : chargé seulement ici

    if request.method == 'POST':
        imprimer = _imprimer_etageres if request.POST.get('quoi') == 'etageres' else _imprimer_articles
        return imprimer(request, planches)
    quoi = 'etageres' if request.GET.get('quoi') == 'etageres' else 'articles'
    choix, qs = _filtres_etiquettes(request.GET)
    base = planches.adresse_base(request)
    contexte = {
        'quoi': quoi, 'choix': choix, 'base': base, 'base_locale': planches.adresse_locale(base),
        'categories': Categorie.objects.all(), 'blocs': Bloc.objects.all(),
        'etageres_bloc': choix['bloc'].etageres.all() if choix['bloc'] else [],
        'onglets': [{'libelle': libelle, 'actif': cle == quoi, 'lien': _lien_etiquettes(request.GET, quoi=cle)}
                    for cle, libelle in (('articles', "Étiquettes d'articles"), ('etageres', "Étiquettes d'étagères"))],
        'filtres': [(cle, valeur) for cle in FILTRES_ETIQUETTES for valeur in request.GET.getlist(cle) if valeur],
    }
    if quoi == 'etageres':
        etageres = Etagere.objects.select_related('bloc').annotate(
            nb_articles=Count('articles', filter=Q(articles__actif=True))).order_by('bloc__code', 'code')
        if choix['bloc']:
            etageres = etageres.filter(bloc=choix['bloc'])
        etageres = list(etageres)
        exemple = choix['etagere'] or (etageres[0] if etageres else None)
        contexte.update(etageres=etageres, par_page=planches.ETAGERES_PAR_PAGE, exemple=exemple,
                        lien_effacer=_lien_etiquettes(request.GET, bloc=None, etagere=None))
        if exemple:
            contexte.update(qr_exemple=planches.qr_svg(planches.lien_etagere(base, exemple)),
                            bandeau=planches.couleurs_bandeau(exemple.bloc.couleur),
                            niveaux_exemple=range(exemple.nb_niveaux, 0, -1))
        return render(request, 'stock/etiquettes.html', contexte)
    total = qs.count()
    articles = list(qs[:LIMITE_CASES if total <= LIMITE_CASES else 50])
    exemple = articles[0] if articles else None
    contexte.update(
        articles=articles, total=total, autres=total - len(articles), cases=total <= LIMITE_CASES,
        limite_cases=LIMITE_CASES, limite_etiquettes=LIMITE_ETIQUETTES,
        formats=planches.FORMATS, format_defaut=planches.FORMAT_DEFAUT, exemple=exemple,
        lieu_grand=planches.texte_emplacement(exemple, 'a4-24') if exemple else '',
        lieu_petit=planches.texte_emplacement(exemple, 'a4-65') if exemple else '',
        qr_exemple=planches.qr_svg(planches.lien_article(base, exemple)) if exemple else '',
        pages=planches.nombre_pages(total, planches.FORMAT_DEFAUT),
        filtre_actif=any(choix[cle] for cle in ('q', 'categorie', 'bloc', 'depuis', 'apres', 'articles')),
        lien_effacer=_lien_etiquettes(request.GET, **{cle: None for cle in FILTRES_ETIQUETTES}),
    )
    return render(request, 'stock/etiquettes.html', contexte)


def _imprimer_articles(request, planches):
    choix, qs = _filtres_etiquettes(request.POST)
    articles = list(qs)
    if request.POST.get('cases'):  # liste avec cases à cocher : seulement les articles cochés
        coches = set(_identifiants(request.POST.getlist('choisi')))
        articles = [a for a in articles if a.pk in coches]
    format_ = request.POST.get('format')
    format_ = format_ if format_ in planches.FORMATS else planches.FORMAT_DEFAUT
    exemplaires = _entier(request.POST.get('exemplaires'), 1, 1, 50)
    depart = _entier(request.POST.get('depart'), 1, 1, planches.FORMATS[format_].par_page)
    nombre_ = len(articles) * exemplaires
    if not nombre_:
        messages.error(request, 'Aucune étiquette à imprimer : cochez au moins un article.')
    elif nombre_ > LIMITE_ETIQUETTES:
        messages.error(request, f"{nombre_} étiquettes d'un coup, c'est trop (au plus {LIMITE_ETIQUETTES}) : "
                                "imprimez bloc par bloc ou catégorie par catégorie.")
    else:
        return livrer(request, planches.planche_articles(
            articles, planches.adresse_base(request), format_, exemplaires, depart,
            contours=bool(request.POST.get('contours')),
            nom_fichier=f'etiquettes_articles_{timezone.localdate():%Y-%m-%d}'))
    return redirect(_lien_etiquettes(request.POST))


def _imprimer_etageres(request, planches):
    etageres = list(Etagere.objects.filter(pk__in=_identifiants(request.POST.getlist('choisi')))
                    .select_related('bloc').order_by('bloc__code', 'code'))
    if not etageres:
        messages.error(request, 'Aucune étiquette à imprimer : cochez au moins une étagère.')
        return redirect(_lien_etiquettes(request.POST))
    return livrer(request, planches.etiquettes_etageres(
        etageres, planches.adresse_base(request), nom_fichier=f'etiquettes_etageres_{timezone.localdate():%Y-%m-%d}'))


@login_required
def qr_article(request, code):
    """Adresse du QR code d'une étiquette d'article : l'article dans « Trouver un article » (son emplacement en 3D)."""
    article = Article.objects.filter(code=code).first() or Article.objects.filter(code__iexact=code).first()
    if not article:
        messages.warning(request, f"Aucun article n'a le code « {code} » : il a peut-être été renommé.")
        return redirect(f"{reverse('stock:articles')}?{urlencode({'q': code})}")
    return redirect(f"{reverse('stock:recherche')}?article={article.pk}")


@login_required
def qr_etagere(request, pk):
    """Adresse du QR code d'une étiquette d'étagère : la liste des articles rangés sur cette étagère."""
    etagere = get_object_or_404(Etagere, pk=pk)
    return redirect(f"{reverse('stock:articles')}?etagere={etagere.pk}")


# =========================================================
# Inventaire : comptage du stock réel, puis validation des écarts
# =========================================================

VALEUR_ECART = ExpressionWrapper(F('quantite') * F('prix_unitaire'), output_field=DecimalField(max_digits=30, decimal_places=5))


def _inventaire(pk):
    return get_object_or_404(Inventaire.objects.select_related('cree_par', 'valide_par'), pk=pk)


def _lignes_inventaire(bon):
    return list(bon.lignes.select_related('article__etagere__bloc').order_by(*ordre_emplacement('article__')))


def _qte_saisie(valeur):
    """Quantité telle qu'on la retape dans une case : « 1250,5 » (sans espace entre les milliers)."""
    return '' if valeur is None else quantite(valeur).replace('\xa0', '')


def _groupes(lignes):
    """Lignes triées par emplacement → une liste par étagère (les articles non rangés à la fin)."""
    groupes = []
    for ligne in lignes:
        etagere = ligne.article.etagere
        cle = f'etagere-{etagere.pk}' if etagere else 'non-ranges'
        if not groupes or groupes[-1]['cle'] != cle:
            groupes.append({
                'cle': cle, 'lignes': [], 'comptes': 0,
                'titre': f'Bloc {etagere.bloc.code} › Étagère {etagere.code}' if etagere else 'Articles non rangés',
                'sous': etagere.bloc.nom if etagere else "Pas d'emplacement dans le plan",
            })
        groupes[-1]['lignes'].append(ligne)
        groupes[-1]['comptes'] += ligne.stock_compte is not None
    return groupes


@login_required
def inventaires(request):
    """Liste des inventaires : en cours (à compter) et validés. Les corrections d'un seul article n'y sont pas."""
    qs = Inventaire.objects.select_related('cree_par', 'valide_par').annotate(
        nb=Count('lignes'), comptes=Count('lignes', filter=Q(lignes__stock_compte__isnull=False)))
    en_cours = list(qs.filter(statut=Inventaire.BROUILLON).order_by('-date', '-pk'))
    termines = list(qs.exclude(statut=Inventaire.BROUILLON).filter(Q(perimetre__gt='') | Q(nb__gt=1))
                    .order_by(F('date_validation').desc(nulls_last=True), '-date', '-pk')[:100])
    ajustements = {m['reference']: m for m in MouvementStock.objects.filter(
        type=MouvementStock.INVENTAIRE, reference__in=[i.numero for i in termines],
    ).values('reference').annotate(nb=Count('id'), valeur=Sum(VALEUR_ECART))}
    for inv in en_cours:
        inv.pourcent = round(100 * inv.comptes / inv.nb) if inv.nb else 0
    for inv in termines:
        ajustement = ajustements.get(inv.numero, {})
        inv.nb_ecarts, inv.valeur_ecarts = ajustement.get('nb', 0), ajustement.get('valeur') or Decimal('0')
    # Inventaire tout juste validé : on annonce les écarts corrigés et on propose d'imprimer le résultat.
    pk = _identifiant(request.GET.get('valide'))
    valide = Inventaire.objects.filter(pk=pk, statut=Inventaire.VALIDE).first() if pk else None
    if valide:
        valide.nb_ecarts = MouvementStock.objects.filter(type=MouvementStock.INVENTAIRE, reference=valide.numero).count()
    return render(request, 'stock/inventaires.html', {'en_cours': en_cours, 'termines': termines, 'valide': valide})


@responsable
def inventaire_nouveau(request):
    form = InventaireForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        articles = form.articles()
        with transaction.atomic():
            bon = Inventaire.objects.create(cree_par=request.user, perimetre=form.libelle(),
                                            observation=_espaces(form.cleaned_data['observation']))
            ids = list(articles.order_by(*ordre_emplacement()).values_list('pk', flat=True))
            LigneInventaire.objects.bulk_create([LigneInventaire(bon=bon, article_id=a) for a in ids])
        autres = list(Inventaire.objects.filter(statut=Inventaire.BROUILLON, lignes__article__in=articles)
                      .exclude(pk=bon.pk).distinct().values_list('numero', flat=True))
        if autres:
            messages.warning(request, f'Attention : des articles sont aussi dans un autre inventaire en cours '
                                      f'({", ".join(autres)}). Le comptage le plus récent sera gardé.')
        messages.success(request, f'Inventaire {bon.numero} créé : {len(ids)} article{"s" if len(ids) > 1 else ""} '
                                  'à compter. Le stock ne change qu\'à la validation.')
        return redirect('stock:inventaire', bon.pk)
    actifs = Q(articles__actif=True)
    return render(request, 'stock/inventaire_nouveau.html', {
        'form': form, 'nb_tout': Article.objects.filter(actif=True).count(),
        'blocs': Bloc.objects.annotate(nb=Count('etageres__articles', filter=Q(etageres__articles__actif=True))),
        'etageres': Etagere.objects.select_related('bloc').annotate(nb=Count('articles', filter=actifs)),
        'categories': Categorie.objects.annotate(nb=Count('article', filter=Q(article__actif=True))),
    })


def _page_comptage(request, bon, saisies=None, erreurs=None):
    """Saisie des quantités comptées, étagère par étagère."""
    saisies, erreurs = saisies or {}, erreurs or {}
    lignes = _lignes_inventaire(bon)
    for ligne in lignes:
        ligne.avant = _qte_saisie(ligne.stock_compte)
        ligne.saisie = saisies.get(ligne.pk, ligne.avant)
        ligne.erreur = erreurs.get(ligne.pk)
    comptes = sum(l.stock_compte is not None for l in lignes)
    return render(request, 'stock/inventaire_comptage.html', {
        'bon': bon, 'groupes': _groupes(lignes), 'comptes': comptes, 'total': len(lignes),
        'pourcent': round(100 * comptes / len(lignes)) if lignes else 0, 'erreurs': list(erreurs.values()),
        # Comptage « à l'aveugle » par défaut : le stock du logiciel n'est montré qu'à un responsable qui le demande.
        'theorique': request.user.is_staff and request.GET.get('theorique') == '1',
    })


def _lignes_resultat(bon):
    """Lignes d'un inventaire validé, avec la valeur de l'écart enregistrée à la validation (prix moyen du moment)."""
    lignes = _lignes_inventaire(bon)
    valeurs = dict(MouvementStock.objects.filter(type=MouvementStock.INVENTAIRE, reference=bon.numero)
                   .values_list('article_id').annotate(v=Sum(VALEUR_ECART)))
    for ligne in lignes:
        ligne.valeur = valeurs.get(ligne.article_id) or Decimal('0')
    return lignes


@login_required
def inventaire(request, pk):
    bon = _inventaire(pk)
    if bon.modifiable:
        return _page_comptage(request, bon)
    lignes = _lignes_resultat(bon)
    ecarts = [l for l in lignes if l.ecart]
    tous = request.GET.get('tous') == '1' or not ecarts
    return render(request, 'stock/inventaire_resultat.html', {
        'bon': bon, 'lignes': lignes if tous else ecarts, 'tous': tous, 'nb_lignes': len(lignes),
        'nb_ecarts': len(ecarts), 'nb_comptees': sum(l.stock_compte is not None for l in lignes),
        'valeur': sum((l.valeur for l in ecarts), Decimal('0')),
    })


@login_required
@require_POST
def inventaire_compter(request, pk):
    """Quantités comptées : une case à la fois depuis la page (fetch), ou une étagère avec « Enregistrer ».

    Champs compte_<ligne>. Sans fetch, avant_<ligne> (valeur affichée au chargement de la page) permet de
    n'enregistrer que les cases changées sur cette page : le comptage d'un collègue n'est pas écrasé."""
    en_direct = request.headers.get('X-Requested-With') == 'fetch'
    saisies = {}
    for cle, texte in request.POST.items():
        n = _identifiant(cle[7:]) if cle.startswith('compte_') else None
        if n is not None and (en_direct or texte.strip() != request.POST.get(f'avant_{n}', '').strip()):
            saisies[n] = texte[:40]
    faites, erreurs = {}, {}
    with transaction.atomic():
        bon = get_object_or_404(Inventaire.objects.select_for_update(), pk=pk)
        if not bon.modifiable:
            texte = f'{bon.numero} est {bon.get_statut_display().lower()} : il ne peut plus être modifié.'
            if en_direct:
                return JsonResponse({'erreur': texte}, status=409)
            messages.error(request, texte)
            return redirect('stock:inventaire', pk)
        lignes = bon.lignes.in_bulk(list(saisies))
        maintenant = timezone.now()
        for n, texte in saisies.items():
            ligne = lignes.get(n)
            if ligne is None:
                erreurs[n] = 'Article introuvable dans cet inventaire : rechargez la page.'
                continue
            try:
                compte = nombre_saisi(texte)
            except ValidationError as e:
                erreurs[n] = e.messages[0]
                continue
            if compte == ligne.stock_compte:
                faites[n] = ligne  # même quantité renvoyée (connexion perdue, page restée ouverte) : heure gardée
                continue
            # Chaque saisie note l'heure du comptage : les entrées et sorties faites après seront reprises.
            ligne.stock_compte, ligne.compte_le = compte, (maintenant if compte is not None else None)
            ligne.save(update_fields=['stock_compte', 'compte_le'])
            faites[n] = ligne
        comptes = bon.lignes.filter(stock_compte__isnull=False).count()
        total = bon.lignes.count()
    if en_direct:
        return JsonResponse({
            'lignes': {str(n): {'valeur': _qte_saisie(l.stock_compte),
                                'heure': timezone.localtime(l.compte_le).strftime('%H:%M') if l.compte_le else ''}
                       for n, l in faites.items()},
            'erreurs': {str(n): m for n, m in erreurs.items()}, 'comptes': comptes, 'total': total,
        })
    if erreurs:
        messages.error(request, 'Certaines quantités ne sont pas enregistrées : corrigez les cases en rouge.')
        return _page_comptage(request, bon, saisies={n: saisies[n] for n in erreurs}, erreurs=erreurs)
    if faites:
        messages.success(request, f'{len(faites)} quantité{"s" if len(faites) > 1 else ""} '
                                  f'enregistrée{"s" if len(faites) > 1 else ""} ({comptes} / {total} comptés).')
    groupe = request.POST.get('groupe', '')
    ancre = f'#{groupe}' if groupe.replace('-', '').isalnum() and groupe.isascii() else ''
    return redirect(reverse('stock:inventaire', args=[pk]) + ancre)


@login_required
def inventaire_fiche(request, pk):
    """Fiche de comptage à imprimer (?reste=1 : seulement les articles pas encore comptés)."""
    bon = _inventaire(pk)
    lignes = _lignes_inventaire(bon)
    if request.GET.get('reste') == '1':
        lignes = [l for l in lignes if l.stock_compte is None]
    from .pdf import fiche_comptage  # reportlab : chargé seulement à l'impression

    return livrer(request, fiche_comptage(
        bon, lignes, avec_theorique=request.user.is_staff and request.GET.get('theorique') == '1'))


@login_required
def inventaire_resultat(request, pk):
    """Résultat imprimable d'un inventaire validé : écarts en quantité et en valeur."""
    bon = _inventaire(pk)
    if bon.statut != Inventaire.VALIDE:
        messages.info(request, f'{bon.numero} n\'est pas validé : voici la fiche de comptage.')
        return redirect('stock:inventaire_fiche', pk)
    from .pdf import resultat_inventaire

    return livrer(request, resultat_inventaire(bon, _lignes_resultat(bon)))


def _empreinte(lignes):
    """Résumé des comptages (quantité et heure) : validation refusée s'ils ont changé depuis le récapitulatif.

    L'heure compte aussi : les mouvements faits après elle sont repris, donc le stock appliqué en dépend.
    """
    texte = ';'.join(f'{l.pk}:{l.stock_compte}:{l.compte_le.isoformat() if l.compte_le else ""}'
                     for l in sorted(lignes, key=lambda l: l.pk))
    return hashlib.sha256(texte.encode()).hexdigest()[:20]


@responsable
def inventaire_valider(request, pk):
    """Récapitulatif des écarts (quantité et valeur), puis validation : le stock prend les quantités comptées."""
    bon = _inventaire(pk)
    if not bon.modifiable:
        messages.info(request, f'{bon.numero} est déjà {bon.get_statut_display().lower()}.')
        return redirect('stock:inventaire', pk)
    apercu = services.apercu_inventaire(bon)
    empreinte = _empreinte(apercu['lignes'])
    refus = []
    if request.method == 'POST':
        if request.POST.get('empreinte') != empreinte:
            refus = ["Des quantités ont été saisies ou changées pendant que vous lisiez : vérifiez le récapitulatif "
                     "à jour ci-dessous, puis validez."]
        else:
            try:
                services.valider(bon, request.user)
            except services.StockError as e:
                refus = e.messages
            else:
                # La liste annonce la validation et propose d'imprimer le résultat.
                return redirect(f"{reverse('stock:inventaires')}?valide={bon.pk}")
    return render(request, 'stock/inventaire_valider.html', {
        'bon': bon, **apercu, 'empreinte': empreinte, 'refus': refus,
        'valeur_nette': apercu['plus'] + apercu['moins'],
    })


@responsable
@require_POST
def inventaire_supprimer(request, pk):
    with transaction.atomic():
        bon = get_object_or_404(Inventaire.objects.select_for_update(), pk=pk)
        if not bon.modifiable:
            messages.error(request, f'{bon.numero} est {bon.get_statut_display().lower()} : '
                                    'seul un inventaire en cours peut être supprimé.')
            return redirect('stock:inventaire', pk)
        bon.delete()
    messages.success(request, f'Inventaire {bon.numero} supprimé : le stock n\'a pas changé.')
    return redirect('stock:inventaires')



# =========================================================
# Premier lancement et réglages
# =========================================================

def bienvenue(request):
    """Premier lancement : création du compte administrateur (seulement s'il n'existe aucun compte)."""
    User = get_user_model()
    if User.objects.exists():
        return redirect('stock:connexion')
    erreurs, valeurs, erreurs_mdp = [], {}, []
    societe = SocieteDepartForm(request.POST or None, request.FILES or None, instance=Parametres.actuels())
    if request.method == 'POST':
        valeurs = request.POST
        if not societe.is_valid():
            erreurs += [e for liste in societe.errors.values() for e in liste]
        nom = request.POST.get('username', '').strip()
        mdp, mdp2 = request.POST.get('password', ''), request.POST.get('password2', '')
        if not nom:
            erreurs.append("Choisissez un nom d'utilisateur.")
        utilisateur = User(username=nom, first_name=request.POST.get('prenom', '').strip()[:150],
                           is_staff=True, is_superuser=True)
        # Toutes les remarques sur le mot de passe en une fois, pas une par essai.
        if mdp != mdp2:
            erreurs_mdp.append('Les deux mots de passe ne sont pas identiques.')
        try:
            validate_password(mdp, utilisateur)
        except ValidationError as e:
            erreurs_mdp += e.messages
        erreurs += erreurs_mdp
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
    return render(request, 'stock/bienvenue.html', {
        'erreurs': erreurs, 'valeurs': valeurs, 'societe': societe,
        'garder_mdp': bool(valeurs) and not erreurs_mdp,  # mot de passe correct : inutile de le retaper
    })


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
