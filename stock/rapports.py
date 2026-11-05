"""Calculs des rapports : consommation, journal des mouvements, état du stock à une date.

L'accueil, le tableau de bord et la consommation passent tous par sorties_valorisees() : une sortie dont le
bon a été annulé ne compte nulle part, et les chiffres sont les mêmes sur tous les écrans.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.apps import apps
from django.db.models import Count, DecimalField, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum
from django.utils import timezone

from .models import Article, BonEntree, BonSortie, Chantier, Engin, Fournisseur, Inventaire, LigneEntree, MouvementStock

# Retour de chantier : quantité positive (la marchandise revient en stock) ; il réduit la consommation du chantier.
RETOUR = getattr(MouvementStock, 'RETOUR', 'RETOUR')
TYPES_CONSOMMATION = [MouvementStock.SORTIE, RETOUR]

_DECIMAL = DecimalField(max_digits=30, decimal_places=2)
VALEUR = ExpressionWrapper(F('stock') * F('prix_moyen'), output_field=_DECIMAL)
# Valeur consommée : positive pour une sortie, négative pour un retour.
VALEUR_MVT = ExpressionWrapper(-F('quantite') * F('prix_unitaire'), output_field=_DECIMAL)

REGROUPEMENTS = {'chantier': 'Par chantier', 'engin': 'Par engin', 'article': 'Par article'}
SANS = {'chantier': 'Sans chantier', 'engin': 'Sans engin', 'bloc': 'Non rangé'}


def unite_claire(code):
    """« SAC » → « Sac » (unités en clair dans les rapports)."""
    return dict(Article.UNITES).get(code, code or '')


PLURIELS = {'Jeu': 'jeux', 'Rouleau': 'rouleaux', 'Mètre carré': 'mètres carrés', 'Mètre cube': 'mètres cubes'}


def unite_qte(code, qte):
    """Unité à écrire après une quantité : « 1 sac », « 7,5 mètres », « 4 unités »."""
    nom = unite_claire(code)
    if not nom or abs(qte or 0) < 2 or nom[-1] in 'sxz':
        return nom.lower()
    if nom in PLURIELS:
        return PLURIELS[nom]
    return nom.lower() if ' ' in nom else nom.lower() + 's'


def debut(jour):
    """Minuit, heure locale, au début de `jour`."""
    return timezone.make_aware(datetime.combine(jour, time.min))


def fin(jour):
    """Minuit, heure locale, à la fin de `jour` (les dates avant fin(jour) sont ce jour-là ou avant)."""
    return debut(jour + timedelta(days=1))


def lire_date(texte, defaut):
    try:
        return date.fromisoformat((texte or '').strip())
    except ValueError:
        return defaut


def raccourcis(aujourd_hui=None):
    """[(libellé, du, au), …] : Ce mois, Mois dernier, Cette année."""
    a = aujourd_hui or timezone.localdate()
    fin_mois_dernier = a.replace(day=1) - timedelta(days=1)
    return [
        ('Ce mois', a.replace(day=1), a),
        ('Mois dernier', fin_mois_dernier.replace(day=1), fin_mois_dernier),
        ('Cette année', a.replace(month=1, day=1), a),
    ]


def texte_periode(du=None, au=None):
    """« Du 01/10/2026 au 09/10/2026 », « Depuis le … », « Jusqu'au … » ou « Tout l'historique »."""
    if du and au:
        return f'Du {du:%d/%m/%Y} au {au:%d/%m/%Y}'
    if du:
        return f'Depuis le {du:%d/%m/%Y}'
    if au:
        return f"Jusqu'au {au:%d/%m/%Y}"
    return "Tout l'historique"


def periode(params):
    """(du, au) lus dans ?du=&au= (AAAA-MM-JJ) ; par défaut du 1er du mois à aujourd'hui."""
    a = timezone.localdate()
    du, au = lire_date(params.get('du'), a.replace(day=1)), lire_date(params.get('au'), a)
    return (au, du) if du > au else (du, au)


def _objet(Modele, texte):
    texte = (texte or '').strip()
    if not (texte.isascii() and texte.isdigit() and len(texte) < 19):
        return None
    return Modele.objects.filter(pk=int(texte)).first()


# =========================================================
# Consommation (sorties moins retours, bons annulés exclus)
# =========================================================

def bons_annules():
    """Numéros des bons annulés après validation : leurs mouvements ont été contre-passés."""
    return MouvementStock.objects.filter(type=MouvementStock.ANNULATION).values('reference')


def sorties_valorisees(du=None, au=None):
    """Mouvements consommés (sorties, retours de chantier) du `du` au `au` inclus, sans ceux des bons annulés.

    Leur valeur se calcule avec VALEUR_MVT (prix moyen figé à la sortie)."""
    qs = MouvementStock.objects.filter(type__in=TYPES_CONSOMMATION).exclude(reference__in=bons_annules())
    if du:
        qs = qs.filter(date__gte=debut(du))
    if au:
        qs = qs.filter(date__lt=debut(au + timedelta(days=1)))
    return qs


def total_sorties(du=None, au=None):
    return sorties_valorisees(du, au).aggregate(v=Sum(VALEUR_MVT))['v'] or Decimal('0')


def _non_negligeable(valeur):
    """Valeur consommée qui compte dans un rapport : au moins une unité de monnaie. Un retour rend la valeur de la
    sortie ; s'il est valorisé au coût moyen de plusieurs sorties, il peut rester une fraction due à l'arrondi."""
    return bool((valeur or Decimal('0')).quantize(Decimal('1')))


def par_chantier(du=None, au=None):
    """[{'nom', 'valeur'}, …] du plus gros consommateur au plus petit."""
    lignes = (sorties_valorisees(du, au).values('chantier__nom').annotate(valeur=Sum(VALEUR_MVT))
              .order_by('-valeur', 'chantier__nom'))
    return [{'nom': l['chantier__nom'] or SANS['chantier'], 'valeur': l['valeur'] or Decimal('0')}
            for l in lignes if _non_negligeable(l['valeur'])]


def _nom_groupe(par, ligne):
    if par == 'chantier':
        return ligne['chantier__nom'] or SANS['chantier']
    if par == 'engin':
        if not ligne['engin__code']:
            return SANS['engin']
        return ' – '.join(filter(None, [ligne['engin__code'], ligne['engin__designation']]))
    return ''


def consommation(du, au, par='chantier', chantier=None):
    """Consommation de la période, regroupée par chantier, par engin ou par article.

    Renvoie {'groupes': [{'nom', 'lignes': [{'code', 'designation', 'unite', 'qte', 'valeur'}], 'total'}],
    'total', 'nb_lignes'}. Par article : un seul groupe sans nom, du plus coûteux au moins coûteux.
    """
    qs = sorties_valorisees(du, au)
    if chantier:
        qs = qs.filter(chantier=chantier)
    cles = {'chantier': ['chantier__nom'], 'engin': ['engin__code', 'engin__designation']}.get(par, [])
    lignes = (qs.values(*cles, 'article__code', 'article__designation', 'article__unite')
              .annotate(qte=Sum('quantite'), valeur=Sum(VALEUR_MVT))
              .order_by(*cles, 'article__code'))
    groupes = {}
    for l in lignes:
        if not l['qte'] and not _non_negligeable(l['valeur']):
            continue  # tout est revenu du chantier
        nom = _nom_groupe(par, l)
        groupe = groupes.setdefault(nom, {'nom': nom, 'lignes': [], 'total': Decimal('0')})
        valeur = l['valeur'] or Decimal('0')
        groupe['lignes'].append({'code': l['article__code'], 'designation': l['article__designation'],
                                 'unite': unite_claire(l['article__unite']), 'qte': -l['qte'], 'valeur': valeur,
                                 'unite_qte': unite_qte(l['article__unite'], l['qte'])})
        groupe['total'] += valeur
    groupes = list(groupes.values())
    if par in ('chantier', 'engin'):
        groupes.sort(key=lambda g: (g['nom'] == SANS[par], g['nom'].lower()))
    for g in groupes:
        if par == 'article':
            g['lignes'].sort(key=lambda l: (-l['valeur'], l['code']))
    return {'groupes': groupes, 'total': sum((g['total'] for g in groupes), Decimal('0')),
            'nb_lignes': sum(len(g['lignes']) for g in groupes)}


# =========================================================
# Tableau de bord
# =========================================================

def valeur_par_categorie():
    """[{'nom', 'nb', 'valeur', 'pourcent'}, …] pour les articles actifs, de la plus grosse valeur à la plus petite."""
    lignes = list(Article.objects.filter(actif=True).values('categorie__nom')
                  .annotate(nb=Count('id'), valeur=Sum(VALEUR)).order_by('-valeur', 'categorie__nom'))
    total = sum((l['valeur'] or 0 for l in lignes), Decimal('0'))
    return [{'nom': l['categorie__nom'], 'nb': l['nb'], 'valeur': l['valeur'] or Decimal('0'),
             'pourcent': int((l['valeur'] or 0) * 100 / total) if total else 0} for l in lignes]


# =========================================================
# Journal des mouvements
# =========================================================

VALEUR_LIGNE = ExpressionWrapper(F('quantite') * F('prix_unitaire'), output_field=_DECIMAL)


def journal(params):
    """Mouvements filtrés par ?du, au, type, article (code ou nom), chantier, engin, fournisseur (identifiants).

    Renvoie (mouvements, choix retenus, description des filtres pour les titres)."""
    du, au = periode(params)
    qs = MouvementStock.objects.filter(date__gte=debut(du), date__lt=debut(au + timedelta(days=1)))
    choix = {'du': du, 'au': au, 'type': '', 'article': '', 'chantier': None, 'engin': None, 'fournisseur': None}
    description = [f'Du {du:%d/%m/%Y} au {au:%d/%m/%Y}']
    types = dict(MouvementStock.TYPES)
    if params.get('type') in types:
        choix['type'] = params['type']
        qs = qs.filter(type=choix['type'])
        description.append(types[choix['type']])
    article = (params.get('article') or '').strip()[:100]
    if article:
        choix['article'] = article
        exact = Article.objects.filter(code__iexact=article).first()
        if exact:
            qs = qs.filter(article=exact)
        else:
            qs = qs.filter(Q(article__code__icontains=article) | Q(article__designation__icontains=article))
        description.append(f'Article « {article} »')
    for nom, Modele in (('chantier', Chantier), ('engin', Engin), ('fournisseur', Fournisseur)):
        objet = _objet(Modele, params.get(nom))
        if objet is None:
            continue
        choix[nom] = objet
        if nom == 'fournisseur':
            qs = qs.filter(reference__in=BonEntree.objects.filter(fournisseur=objet).values('numero'))
        else:
            qs = qs.filter(**{nom: objet})
        description.append(f'{nom.capitalize()} {objet.code if nom == "engin" else objet.nom}')
    return qs.order_by('-date', '-id'), choix, ' · '.join(description)


def totaux_journal(qs):
    """Valeur des mouvements qui ajoutent du stock (entrées) et de ceux qui en retirent (sorties).

    Les mouvements d'un bon annulé (le mouvement d'origine et sa contre-passation) s'annulent : ils restent
    dans la liste mais ne comptent dans aucun total, comme dans la consommation."""
    annules = Q(reference__in=bons_annules())
    t = qs.aggregate(entrees=Sum(VALEUR_LIGNE, filter=Q(quantite__gt=0) & ~annules),
                     sorties=Sum(VALEUR_LIGNE, filter=Q(quantite__lt=0) & ~annules),
                     nb=Count('id'), nb_annules=Count('id', filter=annules))
    return {'entrees': t['entrees'] or Decimal('0'), 'sorties': -(t['sorties'] or Decimal('0')), 'nb': t['nb'],
            'nb_annules': t['nb_annules']}


def references_annulees(references):
    """Parmi ces numéros de bon, ceux des bons annulés après validation."""
    return set(MouvementStock.objects.filter(type=MouvementStock.ANNULATION, reference__in=references)
               .values_list('reference', flat=True))


# =========================================================
# État du stock à une date
# =========================================================

GROUPES_ETAT = {'categorie': 'Par catégorie', 'bloc': 'Par bloc'}


def _groupe_etat(article, par):
    if par == 'bloc':
        if not article.etagere_id:
            return SANS['bloc']
        bloc = article.etagere.bloc
        return f'Bloc {bloc.code}' + (f' – {bloc.nom}' if bloc.nom else '')
    return article.categorie.nom


def etat_a_date(jour, par='categorie', avec_zero=False):
    """Quantité et valeur de chaque article le soir du `jour`, regroupées par catégorie ou par bloc.

    Quantité : stock après le dernier mouvement fait ce jour-là ou avant ; valeur : quantité × prix moyen
    après ce mouvement. Un mouvement ancien sans prix moyen enregistré est valorisé au prix moyen actuel
    (ligne marquée « approx »). Articles à zéro : seulement les articles actifs, si avec_zero.
    """
    articles = Article.objects.select_related('categorie', 'etagere__bloc')
    passe = jour < timezone.localdate()
    if passe:
        dernier = (MouvementStock.objects.filter(article=OuterRef('pk'), date__lt=debut(jour + timedelta(days=1)))
                   .order_by('-date', '-id'))
        articles = articles.annotate(qte_date=Subquery(dernier.values('stock_apres')[:1]),
                                     pm_date=Subquery(dernier.values('prix_moyen_apres')[:1]))
    groupes, nb_approx = {}, 0
    for a in articles:
        if passe:
            qte = a.qte_date if a.qte_date is not None else Decimal('0')
            pm, approx = (a.pm_date, False) if a.pm_date is not None else (a.prix_moyen, bool(qte))
        else:
            qte, pm, approx = a.stock, a.prix_moyen, False
        if not qte and not (avec_zero and a.actif):
            continue
        nom = _groupe_etat(a, par)
        groupe = groupes.setdefault(nom, {'nom': nom, 'lignes': [], 'total': Decimal('0')})
        valeur = qte * pm
        groupe['lignes'].append({'article': a, 'code': a.code, 'designation': a.designation,
                                 'unite': a.get_unite_display(), 'unite_qte': unite_qte(a.unite, qte), 'qte': qte,
                                 'pm': pm, 'valeur': valeur, 'approx': approx})
        groupe['total'] += valeur
        nb_approx += approx
    groupes = sorted(groupes.values(), key=lambda g: (g['nom'] == SANS['bloc'], g['nom'].lower()))
    return {'groupes': groupes, 'total': sum((g['total'] for g in groupes), Decimal('0')),
            'nb': sum(len(g['lignes']) for g in groupes), 'nb_approx': nb_approx}


# =========================================================
# Bons : types, recherche, description des mouvements
# =========================================================

def modeles_bons():
    """{'entree': BonEntree, 'sortie': BonSortie, 'inventaire': Inventaire}, plus 'retour' quand le bon de
    retour de chantier existe."""
    modeles = {'entree': BonEntree, 'sortie': BonSortie, 'inventaire': Inventaire}
    try:
        modeles['retour'] = apps.get_model('stock', 'BonRetour')
    except LookupError:
        pass
    return modeles


def champs(Modele):
    return {f.name for f in Modele._meta.get_fields()}


def relations_bon(Modele):
    """Liens à charger avec un bon pour l'afficher (fournisseur, chantier, engin)."""
    return [c for c in ('fournisseur', 'chantier', 'engin') if c in champs(Modele)]


def recherche_bons(Modele, q):
    """Condition « q » sur un type de bon : numéro, fournisseur, réf. BL, chantier, engin, demandeur, remarque,
    ou code / désignation d'un article des lignes (à utiliser avec .distinct())."""
    presents = champs(Modele)
    condition = Q(numero__icontains=q)
    if 'lignes' in presents:
        condition |= Q(lignes__article__code__icontains=q) | Q(lignes__article__designation__icontains=q)
    for champ, chemin in (('fournisseur', 'fournisseur__nom'), ('reference', 'reference'), ('chantier', 'chantier__nom'),
                          ('engin', 'engin__code'), ('demandeur', 'demandeur'), ('rapporte_par', 'rapporte_par'),
                          ('observation', 'observation')):
        if champ in presents:
            condition |= Q(**{f'{chemin}__icontains': q})
    return condition


def libelle_bon(type_, bon):
    """Pour qui ou de qui, en une ligne : fournisseur d'une entrée, chantier (et engin) d'une sortie ou d'un retour,
    remarque d'une correction."""
    if getattr(bon, 'chantier_id', None):
        return ' · '.join(filter(None, [bon.chantier.nom, bon.engin.code if getattr(bon, 'engin_id', None) else '']))
    if getattr(bon, 'fournisseur_id', None):
        return bon.fournisseur.nom
    remarque = (getattr(bon, 'observation', '') or '').strip()
    return remarque.splitlines()[0][:80] if remarque else {'inventaire': 'Inventaire'}.get(type_, '—')


def bons_par_numero(references):
    """{numéro: (type, bon)} pour ces numéros de bon."""
    trouves = {}
    for type_, Modele in modeles_bons().items():
        for bon in Modele.objects.filter(numero__in=references).select_related(*relations_bon(Modele)):
            trouves[bon.numero] = (type_, bon)
    return trouves


def _detail(m, bon):
    """Pour qui ou de qui : chantier et engin d'une sortie, fournisseur d'une entrée, sinon la remarque du bon."""
    if m.chantier_id:
        return ' · '.join(filter(None, [m.chantier.nom, m.engin.code if m.engin_id else '']))
    if bon is not None and getattr(bon, 'fournisseur_id', None):
        return bon.fournisseur.nom
    remarque = (getattr(bon, 'observation', '') or '').strip()
    return remarque.splitlines()[0][:80] if remarque else ''


def decrire_mouvements(mouvements):
    """Complète chaque mouvement : type_bon et bon (pour le lien vers le détail), detail, annule (bon annulé)."""
    mouvements = list(mouvements)
    references = {m.reference for m in mouvements}
    bons = bons_par_numero(references)
    annules = references_annulees(references)
    for m in mouvements:
        m.type_bon, m.bon = bons.get(m.reference, ('', None))
        m.detail = _detail(m, m.bon)
        m.annule = m.reference in annules
    return mouvements


# =========================================================
# Fiche de stock d'un article, derniers achats, à commander
# =========================================================

def fiche_stock(article, du=None, au=None):
    """Fiche de stock (kardex) d'un article, du `du` au `au` inclus (chaque borne est facultative).

    Renvoie {'mouvements' (du plus récent au plus ancien), 'stock_debut', 'valeur_debut', 'stock_fin',
    'valeur_fin', 'entrees', 'sorties' (quantités, annulations comprises : début + entrées − sorties = fin), 'nb'}."""
    qs = MouvementStock.objects.filter(article=article)
    if du:
        qs = qs.filter(date__gte=debut(du))
    if au:
        qs = qs.filter(date__lt=fin(au))
    avant = None
    if du:
        avant = MouvementStock.objects.filter(article=article, date__lt=debut(du)).order_by('-date', '-id').first()
    t = qs.aggregate(entrees=Sum('quantite', filter=Q(quantite__gt=0)), sorties=Sum('quantite', filter=Q(quantite__lt=0)),
                     nb=Count('id'))
    dernier = qs.order_by('-date', '-id').first()

    def etat(m):
        if m is None:
            return Decimal('0'), Decimal('0')
        prix = m.prix_moyen_apres if m.prix_moyen_apres is not None else article.prix_moyen
        return m.stock_apres, m.stock_apres * prix

    stock_debut, valeur_debut = etat(avant)
    stock_fin, valeur_fin = etat(dernier) if dernier else (stock_debut, valeur_debut)
    return {'mouvements': qs.order_by('-date', '-id'), 'stock_debut': stock_debut, 'valeur_debut': valeur_debut,
            'stock_fin': stock_fin, 'valeur_fin': valeur_fin, 'entrees': t['entrees'] or Decimal('0'),
            'sorties': -(t['sorties'] or Decimal('0')), 'nb': t['nb']}


def consommation_article(article, jours=30):
    """Consommé ces `jours` derniers jours (sorties moins retours, bons annulés exclus) et durée du stock à ce rythme."""
    aujourd_hui = timezone.localdate()
    t = (sorties_valorisees(aujourd_hui - timedelta(days=jours - 1), aujourd_hui).filter(article=article)
         .aggregate(qte=Sum('quantite'), valeur=Sum(VALEUR_MVT)))
    qte = -(t['qte'] or Decimal('0'))
    couverture = int(article.stock * jours / qte) if qte > 0 and article.stock > 0 else None
    return {'jours': jours, 'qte': qte, 'valeur': t['valeur'] or Decimal('0'), 'unite_qte': unite_qte(article.unite, qte),
            'couverture': couverture}


def _entrees_validees(article):
    """Lignes des bons d'entrée validés (les bons annulés ne comptent pas), de la plus récente à la plus ancienne."""
    return (LigneEntree.objects.filter(article=article, bon__statut=BonEntree.VALIDE)
            .order_by('-bon__date', '-bon__id', '-id'))


def dernier_achat(article):
    """{'prix', 'date', 'bon'} du dernier achat avec un prix, et {'fournisseur', 'date_fournisseur'} de la dernière
    entrée avec un fournisseur (None quand il n'y en a pas)."""
    lignes = _entrees_validees(article).select_related('bon__fournisseur')
    avec_prix = lignes.filter(prix_unitaire__gt=0).first()
    avec_fournisseur = lignes.filter(bon__fournisseur__isnull=False).first()
    return {
        'prix': avec_prix.prix_unitaire if avec_prix else None,
        'date': avec_prix.bon.date if avec_prix else None,
        'bon': avec_prix.bon if avec_prix else None,
        'fournisseur': avec_fournisseur.bon.fournisseur if avec_fournisseur else None,
        'date_fournisseur': avec_fournisseur.bon.date if avec_fournisseur else None,
    }


def avec_derniers_achats(articles):
    """Ajoute dernier_prix, date_dernier_achat et dernier_fournisseur à chaque article (une seule requête)."""
    lignes = _entrees_validees(OuterRef('pk'))
    avec_prix = lignes.filter(prix_unitaire__gt=0)
    return articles.annotate(
        dernier_prix=Subquery(avec_prix.values('prix_unitaire')[:1]),
        date_dernier_achat=Subquery(avec_prix.values('bon__date')[:1]),
        dernier_fournisseur=Subquery(lignes.filter(bon__fournisseur__isnull=False).values('bon__fournisseur__nom')[:1]),
    )


SANS_FOURNISSEUR = 'Fournisseur non renseigné'


def quantite_a_commander(article):
    """Quantité suggérée : de quoi revenir à deux fois le seuil d'alerte (jamais négative)."""
    return max(article.stock_min * 2 - article.stock, Decimal('0'))


def a_commander(articles):
    """Articles à commander regroupés par dernier fournisseur (« Fournisseur non renseigné » à la fin).

    Renvoie {'groupes': [{'nom', 'lignes': [{'article', 'code', 'designation', 'unite', 'stock', 'stock_min', 'qte',
    'prix', 'montant', 'date'}], 'total'}], 'total', 'nb'} ; montant estimé = quantité suggérée × dernier prix."""
    groupes = {}
    for a in avec_derniers_achats(articles).select_related('categorie', 'etagere__bloc').order_by('code'):
        nom = a.dernier_fournisseur or SANS_FOURNISSEUR
        groupe = groupes.setdefault(nom, {'nom': nom, 'lignes': [], 'total': Decimal('0')})
        qte = quantite_a_commander(a)
        montant = qte * a.dernier_prix if a.dernier_prix else None
        groupe['lignes'].append({'article': a, 'code': a.code, 'designation': a.designation,
                                 'unite': a.get_unite_display(), 'stock': a.stock, 'stock_min': a.stock_min, 'qte': qte,
                                 'unite_qte': unite_qte(a.unite, qte), 'prix': a.dernier_prix, 'montant': montant,
                                 'date': a.date_dernier_achat})
        groupe['total'] += montant or 0
    groupes = sorted(groupes.values(), key=lambda g: (g['nom'] == SANS_FOURNISSEUR, g['nom'].lower()))
    return {'groupes': groupes, 'total': sum((g['total'] for g in groupes), Decimal('0')),
            'nb': sum(len(g['lignes']) for g in groupes)}
