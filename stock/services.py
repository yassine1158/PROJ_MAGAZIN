"""Règles de gestion du stock.

Le stock d'un article ne change QUE par ces fonctions : validation ou annulation
d'un bon. Chaque changement écrit une ligne dans MouvementStock (historique).

Le verrouillage repose sur le mode IMMEDIATE de SQLite (config/settings.py) : deux validations
simultanées s'attendent. select_for_update est gardé pour une autre base de données.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Min, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Article, Bon, BonEntree, BonRetour, BonSortie, Inventaire, LigneInventaire, MouvementStock
from .utils import ordre_emplacement, quantite as qte_lisible, quantite_unite

CENTIME = Decimal('0.01')


class StockError(ValidationError):
    pass


def _verrouiller(bon):
    """Relit le bon en le verrouillant, pour éviter une double validation."""
    bon = type(bon).objects.select_for_update().get(pk=bon.pk)
    if bon.statut != Bon.BROUILLON:
        raise StockError(f'{bon.numero} est déjà {bon.get_statut_display().lower()}.')
    return bon


def _articles(ids):
    return {a.pk: a for a in Article.objects.select_for_update().filter(pk__in=ids)}


def _cmp(stock, prix_moyen, quantite, prix):
    """Prix moyen pondéré après l'entrée de `quantite` au prix `prix`.

    Un prix à 0 signifie « prix non renseigné » : le prix moyen ne change pas.
    Un stock sans prix connu (prix moyen à 0) prend le premier prix connu.
    """
    if not prix:
        return prix_moyen
    if not prix_moyen and quantite > 0:
        return Decimal(prix).quantize(CENTIME)
    nouveau_stock = stock + quantite
    if nouveau_stock <= 0:
        return prix_moyen
    return ((stock * prix_moyen + quantite * prix) / nouveau_stock).quantize(CENTIME)


def _date_mouvement(bon):
    """Date des mouvements d'une entrée ou d'une sortie : le jour du bon, à l'heure de l'enregistrement."""
    maintenant = timezone.localtime()
    jour = timezone.localtime(bon.date).date()
    if jour >= maintenant.date():
        return timezone.now()
    return timezone.make_aware(datetime.combine(jour, maintenant.time()))


def _controler_comptages(date, articles):
    """Refuse un bon daté avant le comptage d'un de ses articles par un inventaire déjà validé.

    Le comptage a fixé le stock réel à ce moment-là : un mouvement placé avant lui compterait l'écart une
    deuxième fois (même règle que pour annuler un bon). Un inventaire pas encore validé ne gêne pas : il ne
    reprend que les mouvements faits après le comptage.
    """
    ligne = (LigneInventaire.objects
             .filter(article_id__in=articles, bon__statut=Bon.VALIDE, stock_compte__isnull=False,
                     stock_theorique__isnull=False)
             .annotate(moment=Coalesce('compte_le', 'bon__date')).filter(moment__gt=date)
             .select_related('bon').order_by('-moment').first())
    if ligne:
        quand = timezone.localtime(ligne.moment)
        raise StockError([
            f'Impossible de dater ce bon avant le {quand:%d/%m/%Y} à {quand:%H:%M} : l\'inventaire '
            f'{ligne.bon.numero} a compté « {articles[ligne.article_id].designation} » à ce moment-là.',
            'Le comptage tient déjà compte de ce qui s\'est passé avant lui : datez le bon après ce comptage, '
            'puis corrigez le stock si besoin sur la fiche de l\'article.'])


# Mouvements qui changent le prix moyen : la marchandise entre (ou ressort, pour une annulation) à son propre prix.
TYPES_PRIX_MOYEN = [MouvementStock.ENTREE, MouvementStock.RETOUR, MouvementStock.ANNULATION]


def _cmp_mouvement(stock, prix_moyen, quantite, prix):
    """_cmp pour un mouvement de l'historique. Retirer une entrée (annulation) ne donne jamais un prix négatif :
    si d'autres mouvements ont eu lieu depuis, le prix moyen est gardé."""
    nouveau = _cmp(stock, prix_moyen, quantite, prix)
    return nouveau if nouveau >= 0 else prix_moyen


def _mouvement(article, type_, quantite, prix, bon, user, date=None, **extra):
    """Écrit le mouvement et enregistre l'article : article.stock est déjà mis à jour, le prix moyen l'est ici.

    prix : prix du mouvement. Celui d'une entrée, d'un retour ou d'une annulation entre dans le prix moyen.
    None : au prix moyen à la date du mouvement, qui ne change pas (sortie, entrée sans prix).

    Un mouvement daté dans le passé (bon saisi après coup) prend sa place dans l'historique, qui est
    trié par date : son « stock après » est le stock à cette date, et il est valorisé au prix moyen de cette
    date. Les mouvements plus récents sont décalés de la même quantité et leur prix moyen est recalculé dans
    l'ordre, comme si le bon avait été saisi à temps (même calcul que la migration 0007). La fiche de stock et
    l'état du stock à une date restent ainsi justes, et le prix moyen de l'article est celui du dernier mouvement.
    Renvoie le mouvement écrit.
    """
    date = date or timezone.now()
    suivants = list(MouvementStock.objects.filter(article=article, date__gt=date).order_by('date', 'id')
                    .only('id', 'type', 'quantite', 'stock_apres', 'prix_unitaire'))
    stock_apres = article.stock - sum((m.quantite for m in suivants), Decimal('0'))
    stock_avant, prix_moyen, connu = stock_apres - quantite, article.prix_moyen, True
    if suivants:
        precedent = (MouvementStock.objects.filter(article=article, date__lte=date).order_by('-date', '-id')
                     .values_list('prix_moyen_apres').first())
        prix_moyen = precedent[0] if precedent else Decimal('0')
        if prix_moyen is None:
            # Mouvement ancien enregistré sans prix moyen : l'historique n'est pas connu. Calcul sur le stock
            # actuel, et les prix moyens suivants sont effacés (l'état du stock à une date le signale).
            stock_avant, prix_moyen, connu = article.stock - quantite, article.prix_moyen, False
    if prix is None:
        prix = prix_moyen
    if type_ in TYPES_PRIX_MOYEN:
        prix_moyen = _cmp_mouvement(stock_avant, prix_moyen, quantite, prix)
    prix_moyen_apres = prix_moyen if connu else None
    for m in suivants:
        m.stock_apres += quantite
        if connu and m.type in TYPES_PRIX_MOYEN:
            prix_moyen = _cmp_mouvement(m.stock_apres - m.quantite, prix_moyen, m.quantite, m.prix_unitaire)
        m.prix_moyen_apres = prix_moyen if connu else None
    MouvementStock.objects.bulk_update(suivants, ['stock_apres', 'prix_moyen_apres'], batch_size=500)
    article.prix_moyen = prix_moyen
    article.save(update_fields=['stock', 'prix_moyen'])
    return MouvementStock.objects.create(
        article=article, type=type_, quantite=quantite, stock_apres=stock_apres, date=date,
        prix_unitaire=prix, prix_moyen_apres=prix_moyen_apres, reference=bon.numero, utilisateur=user, **extra,
    )


def _marquer(bon, statut, user, motif=''):
    bon.statut = statut
    if statut == Bon.VALIDE:
        bon.valide_par, bon.date_validation = user, timezone.now()
        champs = ['valide_par', 'date_validation']
    else:
        bon.annule_par, bon.date_annulation, bon.motif_annulation = user, timezone.now(), motif.strip()[:255]
        champs = ['annule_par', 'date_annulation', 'motif_annulation']
    bon.save(update_fields=['statut', *champs])
    return bon


def _lignes(bon):
    lignes = list(bon.lignes.all())
    if not lignes:
        raise StockError('Le bon ne contient aucun article.')
    return lignes


@transaction.atomic
def valider_entree(bon, user):
    bon = _verrouiller(bon)
    lignes = _lignes(bon)
    articles = _articles([l.article_id for l in lignes])
    date = _date_mouvement(bon)
    _controler_comptages(date, articles)
    for ligne in lignes:
        article = articles[ligne.article_id]
        article.stock += ligne.quantite
        _mouvement(article, MouvementStock.ENTREE, ligne.quantite, ligne.prix_unitaire or None, bon, user, date=date)
    return _marquer(bon, Bon.VALIDE, user)


@transaction.atomic
def valider_sortie(bon, user):
    """Sortie au prix moyen du jour du bon.

    Un bon daté dans le passé est contrôlé sur le stock de ce jour-là, sans faire passer sous zéro un
    mouvement plus récent : l'historique ne devient jamais négatif.
    """
    bon = _verrouiller(bon)
    lignes = _lignes(bon)
    articles = _articles([l.article_id for l in lignes])
    date = _date_mouvement(bon)
    _controler_comptages(date, articles)

    demande = defaultdict(Decimal)
    for ligne in lignes:
        demande[ligne.article_id] += ligne.quantite
    ensuite = {m['article_id']: m for m in (MouvementStock.objects.filter(article_id__in=demande, date__gt=date)
                                            .values('article_id').annotate(total=Sum('quantite'),
                                                                           mini=Min('stock_apres')))}
    jour = timezone.localtime(date)
    manquants = []
    for pk, qte in demande.items():
        article = articles[pk]
        if pk not in ensuite:
            if qte > article.stock:
                reste = (f'il reste {quantite_unite(article.stock, article.unite)}' if article.stock > 0
                         else "il n'en reste plus")
                manquants.append(f'{article.designation} : {reste}, vous en demandez {qte_lisible(qte)}.')
            continue
        a_date = article.stock - ensuite[pk]['total']
        plancher = min(a_date, ensuite[pk]['mini'])
        if qte <= plancher:
            continue
        if plancher < a_date:
            reste = (f'le {jour:%d/%m/%Y}, vous pouvez en sortir {quantite_unite(plancher, article.unite)} au plus '
                     '(le reste est sorti ensuite)' if plancher > 0
                     else f'rien ne peut sortir le {jour:%d/%m/%Y} (tout est sorti ensuite)')
        else:
            reste = (f'le {jour:%d/%m/%Y}, il n\'en restait que {quantite_unite(a_date, article.unite)}' if a_date > 0
                     else f'le {jour:%d/%m/%Y}, il n\'en restait plus')
        manquants.append(f'{article.designation} : {reste}, vous en demandez {qte_lisible(qte)}.')
    if manquants:
        raise StockError(manquants)

    for ligne in lignes:
        article = articles[ligne.article_id]
        article.stock -= ligne.quantite
        mouvement = _mouvement(article, MouvementStock.SORTIE, -ligne.quantite, None, bon, user, date=date,
                               chantier=bon.chantier, engin=bon.engin)
        ligne.prix_unitaire = mouvement.prix_unitaire
        ligne.save(update_fields=['prix_unitaire'])
    return _marquer(bon, Bon.VALIDE, user)


# Mouvements qui font aller ou revenir de la marchandise entre le magasin et un chantier.
TYPES_CHANTIER = [MouvementStock.SORTIE, MouvementStock.RETOUR, MouvementStock.ANNULATION]


def reste_sur_chantier(chantier, ids=None, jusqu_au=None):
    """Quantité de chaque article partie sur le chantier et pas revenue : sorties − retours.

    MouvementStock.quantite est la variation du stock du magasin : sortie −q, retour +q, annulation d'une
    sortie +q, annulation d'un retour −q (les annulations portent le chantier du bon annulé, celles des
    entrées n'en ont pas). La consommation d'un chantier vaut donc −Σ quantite sur ces types.
    ids : articles à regarder (None : tous) ; jusqu_au : seulement les mouvements faits à cette date ou avant.
    """
    mouvements = MouvementStock.objects.filter(chantier=chantier, type__in=TYPES_CHANTIER)
    if ids is not None:
        mouvements = mouvements.filter(article_id__in=ids)
    if jusqu_au is not None:
        mouvements = mouvements.filter(date__lte=jusqu_au)
    lignes = mouvements.values('article_id').annotate(s=Sum('quantite'))
    return defaultdict(Decimal, {l['article_id']: -l['s'] for l in lignes})


def _date_retour(bon, ids):
    """Date des mouvements d'un retour : comme pour une sortie, mais jamais avant la sortie d'origine, ni avant
    une sortie vers ce chantier faite le même jour : la marchandise revient après être partie."""
    date = _date_mouvement(bon)
    lendemain = timezone.make_aware(datetime.combine(timezone.localtime(date).date() + timedelta(days=1), time.min))
    condition = Q(date__lt=lendemain)
    if bon.sortie_id:
        condition |= Q(reference=bon.sortie.numero)
    derniere = (MouvementStock.objects.filter(condition, type=MouvementStock.SORTIE, chantier_id=bon.chantier_id,
                                              article_id__in=ids, date__gt=date).aggregate(d=Max('date'))['d'])
    return max(date, derniere) if derniere else date


def _couts_de_sortie(bon, ids, date):
    """Coût de la marchandise qui revient du chantier, par article : son prix sur la sortie d'origine quand elle
    est indiquée, sinon le coût moyen de ce qui est parti sur le chantier et pas encore revenu à cette date.

    Le retour retire ainsi de la consommation du chantier la valeur que la sortie y avait mise, même si le prix
    moyen a changé entre-temps : un chantier qui rend tout ne coûte rien.
    """
    quantites, valeurs = defaultdict(Decimal), defaultdict(Decimal)
    if bon.sortie_id:
        for pk, q, prix in bon.sortie.lignes.filter(article_id__in=ids).values_list('article_id', 'quantite',
                                                                                    'prix_unitaire'):
            quantites[pk] += q
            valeurs[pk] += q * prix
    autres = [pk for pk in ids if pk not in quantites]
    if autres:
        for pk, q, prix in (MouvementStock.objects
                            .filter(chantier_id=bon.chantier_id, type__in=TYPES_CHANTIER, article_id__in=autres,
                                    date__lte=date)
                            .values_list('article_id', 'quantite', 'prix_unitaire')):
            quantites[pk] -= q
            valeurs[pk] -= q * prix
    return {pk: (valeurs[pk] / q).quantize(CENTIME) if q > 0 and valeurs[pk] > 0 else Decimal('0')
            for pk, q in quantites.items()}


@transaction.atomic
def valider_retour(bon, user):
    """Retour de chantier : le stock remonte au coût de la sortie (voir _couts_de_sortie), qui entre dans le
    prix moyen comme une entrée.

    On ne rend pas plus que ce qui est parti sur le chantier (retours déjà faits déduits), à la date du retour
    comme après elle : sinon la consommation du chantier deviendrait négative.
    """
    bon = _verrouiller(bon)
    lignes = _lignes(bon)
    articles = _articles([l.article_id for l in lignes])

    rendu = defaultdict(Decimal)
    for ligne in lignes:
        rendu[ligne.article_id] += ligne.quantite
    date = _date_retour(bon, list(rendu))
    _controler_comptages(date, articles)
    parti = reste_sur_chantier(bon.chantier_id, list(rendu), jusqu_au=date)
    # Retour daté dans le passé : les retours faits après cette date doivent rester possibles.
    plancher = {pk: parti[pk] for pk in rendu}
    reste, ensuite = dict(plancher), set()
    for pk, q in (MouvementStock.objects
                  .filter(chantier_id=bon.chantier_id, type__in=TYPES_CHANTIER, article_id__in=rendu, date__gt=date)
                  .order_by('date', 'id').values_list('article_id', 'quantite')):
        reste[pk] -= q
        plancher[pk] = min(plancher[pk], reste[pk])
        ensuite.add(pk)
    trop = []
    for pk, qte in rendu.items():
        article = articles[pk]
        if qte <= plancher[pk]:
            continue
        if plancher[pk] < parti[pk]:
            recu = (f'à cette date, vous pouvez en rendre {quantite_unite(plancher[pk], article.unite)} au plus '
                    "(d'autres retours ont été faits ensuite)" if plancher[pk] > 0
                    else "rien ne peut être rendu à cette date (d'autres retours ont été faits ensuite)")
        elif pk in ensuite:
            recu = (f'à cette date, le chantier en avait reçu {quantite_unite(parti[pk], article.unite)} '
                    '(retours déjà faits déduits)' if parti[pk] > 0
                    else "à cette date, rien n'était parti sur ce chantier (ou tout était déjà revenu)")
        else:
            recu = (f'le chantier en a reçu {quantite_unite(parti[pk], article.unite)} (retours déjà faits déduits)'
                    if parti[pk] > 0 else "rien n'est parti sur ce chantier (ou tout est déjà revenu)")
        trop.append(f'{article.designation} : {recu}, vous en rendez {qte_lisible(qte)}.')
    if trop:
        raise StockError([f'On ne peut pas rendre plus que ce que le chantier « {bon.chantier} » a reçu.', *trop,
                          "Si cette marchandise ne vient pas de ce chantier, faites une entrée de stock."])

    couts = _couts_de_sortie(bon, list(rendu), date)
    for ligne in lignes:
        article = articles[ligne.article_id]
        article.stock += ligne.quantite
        ligne.prix_unitaire = couts.get(ligne.article_id, Decimal('0'))
        ligne.save(update_fields=['prix_unitaire'])
        _mouvement(article, MouvementStock.RETOUR, ligne.quantite, ligne.prix_unitaire, bon, user, date=date,
                   chantier=bon.chantier, engin=bon.engin)
    return _marquer(bon, Bon.VALIDE, user)


def _mouvements_depuis(comptages):
    """{article: moment du comptage} → {article: entrées et sorties enregistrées après ce moment}.

    Les ajustements d'inventaire ne sont pas des mouvements de marchandise : ils sont exclus. L'annulation
    d'un bon fait avant le comptage aussi : le comptage voyait déjà la marchandise réelle.
    Tout est lu en deux requêtes, quel que soit le nombre d'articles comptés.
    """
    if not comptages:
        return {}
    marchandise = [MouvementStock.ENTREE, MouvementStock.SORTIE, MouvementStock.RETOUR]
    apres = defaultdict(list)
    for m in (MouvementStock.objects
              .filter(article_id__in=comptages, date__gt=min(comptages.values()),
                      type__in=marchandise + [MouvementStock.ANNULATION])
              .values('article_id', 'date', 'type', 'reference', 'quantite')):
        apres[m['article_id']].append(m)
    annules = {m['reference'] for liste in apres.values() for m in liste if m['type'] == MouvementStock.ANNULATION}
    origines = defaultdict(list)  # (article, bon annulé) → dates de ses mouvements de marchandise
    if annules:
        for a, ref, date in (MouvementStock.objects
                             .filter(article_id__in=comptages, reference__in=annules, type__in=marchandise)
                             .values_list('article_id', 'reference', 'date')):
            origines[a, ref].append(date)
    resultat = {}
    for article_id, moment in comptages.items():
        resultat[article_id] = sum((
            m['quantite'] for m in apres[article_id]
            if m['date'] > moment and not (m['type'] == MouvementStock.ANNULATION
                                           and any(d <= moment for d in origines[article_id, m['reference']]))
        ), Decimal('0'))
    return resultat


def _recomptes_depuis(bon, comptages):
    """Articles qu'un autre inventaire validé a comptés plus tard : ce comptage-ci est dépassé pour eux."""
    autres = (LigneInventaire.objects
              .filter(article_id__in=comptages, bon__statut=Bon.VALIDE, stock_compte__isnull=False)
              .exclude(bon_id=bon.pk).values_list('article_id', 'compte_le', 'bon__date'))
    return {a for a, compte_le, date in autres if (compte_le or date) > comptages[a]}


def _calculs_inventaire(bon, lignes, articles):
    """Lignes comptées → (calculs, lignes recomptées depuis par un autre inventaire, erreurs).

    calculs : (ligne, article, moment du comptage, stock juste = compté + entrées et sorties faites depuis)."""
    maintenant = timezone.now()
    moments = {l.article_id: min(l.compte_le or bon.date, maintenant) for l in lignes}
    recomptes = _recomptes_depuis(bon, moments)
    depuis = _mouvements_depuis({a: m for a, m in moments.items() if a not in recomptes})
    calculs, depassees, erreurs = [], [], []
    for ligne in lignes:
        article = articles[ligne.article_id]
        if article.pk in recomptes:
            depassees.append(ligne)
            continue
        cible = ligne.stock_compte + depuis[article.pk]
        if cible < 0:
            erreurs.append(f'{article.designation} : le comptage indique '
                           f'{quantite_unite(ligne.stock_compte, article.unite)}, mais il en est sorti '
                           f'{qte_lisible(-depuis[article.pk])} depuis. Recomptez cet article.')
        calculs.append((ligne, article, moments[article.pk], cible))
    return calculs, depassees, erreurs


def apercu_inventaire(bon):
    """Ce que la validation de l'inventaire changerait, sans rien enregistrer (récapitulatif avant validation).

    Les calculs sont ceux de valider_inventaire : écart = stock juste − stock actuel, valeur au prix moyen."""
    lignes = list(bon.lignes.select_related('article__etagere__bloc').order_by(*ordre_emplacement('article__')))
    comptees = [l for l in lignes if l.stock_compte is not None]
    calculs, depassees, erreurs = _calculs_inventaire(bon, comptees, {l.article_id: l.article for l in comptees})
    ecarts, justes = [], 0
    for ligne, article, _, cible in calculs:
        ecart = cible - article.stock
        if not ecart:
            justes += 1
            continue
        ecarts.append({'ligne': ligne, 'article': article, 'theorique': ligne.stock_compte - ecart, 'ecart': ecart,
                       'valeur': (ecart * article.prix_moyen).quantize(Decimal('1'))})
    return {
        'lignes': lignes, 'ecarts': ecarts, 'justes': justes, 'erreurs': erreurs, 'depassees': depassees,
        'non_comptees': [l for l in lignes if l.stock_compte is None], 'nb_comptees': len(comptees),
        'plus': sum((e['valeur'] for e in ecarts if e['valeur'] > 0), Decimal('0')),
        'moins': sum((e['valeur'] for e in ecarts if e['valeur'] < 0), Decimal('0')),
    }


@transaction.atomic
def valider_inventaire(bon, user):
    """Applique les quantités comptées.

    Pour chaque ligne comptée : stock juste = quantité comptée + entrées et sorties faites depuis le
    comptage. Ainsi une sortie faite entre le comptage et la validation n'est pas effacée.
    Les lignes non comptées sont ignorées, comme celles qu'un inventaire plus récent a déjà recomptées.
    """
    bon = _verrouiller(bon)
    lignes = [l for l in _lignes(bon) if l.stock_compte is not None]
    if not lignes:
        raise StockError("Aucun article n'a été compté : saisissez au moins une quantité comptée.")
    articles = _articles([l.article_id for l in lignes])
    calculs, _, erreurs = _calculs_inventaire(bon, lignes, articles)
    if erreurs:
        raise StockError(erreurs)
    for ligne, article, moment, cible in calculs:
        ecart = cible - article.stock
        ligne.stock_theorique = ligne.stock_compte - ecart  # stock du logiciel au moment du comptage
        if ecart:
            article.stock = cible
            # Écart valorisé au prix moyen actuel, comme sur le récapitulatif avant validation.
            _mouvement(article, MouvementStock.INVENTAIRE, ecart, article.prix_moyen, bon, user, date=moment)
    LigneInventaire.objects.bulk_update([c[0] for c in calculs], ['stock_theorique'], batch_size=500)
    return _marquer(bon, Bon.VALIDE, user)


def _inventaire_depuis(bon, ids):
    """Inventaire validé après ce bon et qui a compté un de ses articles (ou None)."""
    depuis = bon.date_validation or bon.date
    ligne = (LigneInventaire.objects
             .filter(article_id__in=ids, stock_compte__isnull=False, bon__statut=Bon.VALIDE,
                     bon__date_validation__gt=depuis)
             .select_related('bon').order_by('bon__date_validation').first())
    return ligne.bon if ligne else None


@transaction.atomic
def annuler(bon, user, motif=''):
    """Annule un bon. Un bon validé est contre-passé (le stock revient en arrière)."""
    bon = type(bon).objects.select_for_update().get(pk=bon.pk)
    if bon.statut == Bon.ANNULE:
        raise StockError(f'{bon.numero} est déjà annulé.')
    if bon.statut == Bon.BROUILLON:
        return _marquer(bon, Bon.ANNULE, user, motif)
    if isinstance(bon, Inventaire):
        raise StockError('Un inventaire validé ne peut pas être annulé. '
                         'Faites une correction du stock sur la fiche de l\'article.')

    lignes = list(bon.lignes.all())
    articles = _articles([l.article_id for l in lignes])
    inventaire = _inventaire_depuis(bon, list(articles))
    if inventaire:
        raise StockError(f'Impossible d\'annuler : un inventaire a été fait depuis ({inventaire.numero}). '
                         'Faites une correction du stock sur la fiche de l\'article.')

    if isinstance(bon, BonEntree):
        retire = defaultdict(Decimal)
        for ligne in lignes:
            retire[ligne.article_id] += ligne.quantite
        manquants = [
            f'{articles[pk].designation} : il reste {quantite_unite(articles[pk].stock, articles[pk].unite)}, '
            f'ce bon en a fait entrer {qte_lisible(qte)}.'
            for pk, qte in retire.items() if qte > articles[pk].stock
        ]
        if manquants:
            raise StockError(['Impossible d\'annuler : une partie de la marchandise est déjà sortie.', *manquants])
        for ligne in lignes:
            article = articles[ligne.article_id]
            article.stock -= ligne.quantite
            # Calcul inverse de l'entrée. Il n'a de sens que s'il reste du stock et qu'il donne un prix positif ;
            # sinon (d'autres mouvements ont eu lieu depuis) le prix moyen actuel est gardé (_cmp_mouvement).
            _mouvement(article, MouvementStock.ANNULATION, -ligne.quantite, ligne.prix_unitaire or None, bon, user)
    elif isinstance(bon, BonSortie):
        sorti = defaultdict(Decimal)
        for ligne in lignes:
            sorti[ligne.article_id] += ligne.quantite
        parti = reste_sur_chantier(bon.chantier_id, list(sorti))
        revenus = [pk for pk, qte in sorti.items() if qte > parti[pk]]
        if revenus:
            retours = (BonRetour.objects.filter(statut=Bon.VALIDE, chantier_id=bon.chantier_id,
                                                lignes__article_id__in=revenus)
                       .order_by('date', 'pk').values_list('numero', flat=True).distinct())
            raise StockError([f'Impossible d\'annuler : une partie de ces articles est déjà revenue du chantier '
                              f'({", ".join(retours)}).', 'Annulez d\'abord le retour, puis ce bon.'])
        for ligne in lignes:
            article = articles[ligne.article_id]
            article.stock += ligne.quantite
            _mouvement(article, MouvementStock.ANNULATION, ligne.quantite, ligne.prix_unitaire, bon, user,
                       chantier=bon.chantier, engin=bon.engin)
    elif isinstance(bon, BonRetour):
        rendu = defaultdict(Decimal)
        for ligne in lignes:
            rendu[ligne.article_id] += ligne.quantite
        manquants = [
            f'{articles[pk].designation} : il reste {quantite_unite(articles[pk].stock, articles[pk].unite)}, '
            f'ce retour en a rendu {qte_lisible(qte)}.'
            for pk, qte in rendu.items() if qte > articles[pk].stock
        ]
        if manquants:
            raise StockError(['Impossible d\'annuler ce retour : une partie de la marchandise rendue est déjà '
                              'ressortie du magasin.', *manquants])
        for ligne in lignes:
            article = articles[ligne.article_id]
            article.stock -= ligne.quantite  # calcul inverse du retour, comme pour l'annulation d'une entrée
            _mouvement(article, MouvementStock.ANNULATION, -ligne.quantite, ligne.prix_unitaire, bon, user,
                       chantier=bon.chantier, engin=bon.engin)
    return _marquer(bon, Bon.ANNULE, user, motif)


VALIDATEURS = {
    BonEntree: valider_entree,
    BonSortie: valider_sortie,
    Inventaire: valider_inventaire,
    BonRetour: valider_retour,
}


def valider(bon, user):
    return VALIDATEURS[type(bon)](bon, user)
