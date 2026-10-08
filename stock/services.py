"""Règles de gestion du stock.

Le stock d'un article ne change QUE par ces fonctions : validation ou annulation
d'un bon. Chaque changement écrit une ligne dans MouvementStock (historique).
"""
from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Article, Bon, BonEntree, BonSortie, Inventaire, MouvementStock

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
    """
    if not prix:
        return prix_moyen
    nouveau_stock = stock + quantite
    if nouveau_stock <= 0:
        return prix_moyen
    return ((stock * prix_moyen + quantite * prix) / nouveau_stock).quantize(CENTIME)


def _mouvement(article, type_, quantite, prix, bon, user, **extra):
    MouvementStock.objects.create(
        article=article, type=type_, quantite=quantite, stock_apres=article.stock,
        prix_unitaire=prix, reference=bon.numero, utilisateur=user, **extra,
    )


def _marquer(bon, statut, user):
    bon.statut = statut
    if statut == Bon.VALIDE:
        bon.valide_par = user
        bon.date_validation = timezone.now()
    bon.save(update_fields=['statut', 'valide_par', 'date_validation'])
    return bon


@transaction.atomic
def valider_entree(bon, user):
    bon = _verrouiller(bon)
    lignes = list(bon.lignes.all())
    if not lignes:
        raise StockError(f'{bon.numero} : aucune ligne.')
    articles = _articles([l.article_id for l in lignes])
    for ligne in lignes:
        article = articles[ligne.article_id]
        article.prix_moyen = _cmp(article.stock, article.prix_moyen, ligne.quantite, ligne.prix_unitaire)
        article.stock += ligne.quantite
        article.save(update_fields=['stock', 'prix_moyen'])
        _mouvement(article, MouvementStock.ENTREE, ligne.quantite,
                   ligne.prix_unitaire or article.prix_moyen, bon, user)
    return _marquer(bon, Bon.VALIDE, user)


@transaction.atomic
def valider_sortie(bon, user):
    bon = _verrouiller(bon)
    lignes = list(bon.lignes.all())
    if not lignes:
        raise StockError(f'{bon.numero} : aucune ligne.')
    articles = _articles([l.article_id for l in lignes])

    demande = defaultdict(Decimal)
    for ligne in lignes:
        demande[ligne.article_id] += ligne.quantite
    manquants = [
        f'{articles[pk].code} (demandé {qte}, en stock {articles[pk].stock} {articles[pk].unite})'
        for pk, qte in demande.items() if qte > articles[pk].stock
    ]
    if manquants:
        raise StockError(f'{bon.numero} : stock insuffisant pour ' + ', '.join(manquants) + '.')

    for ligne in lignes:
        article = articles[ligne.article_id]
        article.stock -= ligne.quantite
        article.save(update_fields=['stock'])
        ligne.prix_unitaire = article.prix_moyen
        ligne.save(update_fields=['prix_unitaire'])
        _mouvement(article, MouvementStock.SORTIE, -ligne.quantite, article.prix_moyen, bon, user,
                   chantier=bon.chantier, engin=bon.engin)
    return _marquer(bon, Bon.VALIDE, user)


@transaction.atomic
def valider_inventaire(bon, user):
    bon = _verrouiller(bon)
    lignes = list(bon.lignes.all())
    if not lignes:
        raise StockError(f'{bon.numero} : aucune ligne.')
    articles = _articles([l.article_id for l in lignes])
    for ligne in lignes:
        article = articles[ligne.article_id]
        ligne.stock_theorique = article.stock
        ligne.save(update_fields=['stock_theorique'])
        ecart = ligne.stock_compte - article.stock
        if ecart:
            article.stock = ligne.stock_compte
            article.save(update_fields=['stock'])
            _mouvement(article, MouvementStock.INVENTAIRE, ecart, article.prix_moyen, bon, user)
    return _marquer(bon, Bon.VALIDE, user)


@transaction.atomic
def annuler(bon, user):
    """Annule un bon. Un bon validé est contre-passé (le stock revient en arrière)."""
    bon = type(bon).objects.select_for_update().get(pk=bon.pk)
    if bon.statut == Bon.ANNULE:
        raise StockError(f'{bon.numero} est déjà annulé.')
    if bon.statut == Bon.BROUILLON:
        return _marquer(bon, Bon.ANNULE, user)
    if isinstance(bon, Inventaire):
        raise StockError(f'{bon.numero} : un inventaire validé ne peut pas être annulé. '
                         'Faites un nouvel inventaire pour corriger.')

    lignes = list(bon.lignes.all())
    articles = _articles([l.article_id for l in lignes])

    if isinstance(bon, BonEntree):
        retire = defaultdict(Decimal)
        for ligne in lignes:
            retire[ligne.article_id] += ligne.quantite
        manquants = [articles[pk].code for pk, qte in retire.items() if qte > articles[pk].stock]
        if manquants:
            raise StockError(f'{bon.numero} : impossible d\'annuler, une partie de la marchandise '
                             f'est déjà sortie ({", ".join(manquants)}).')
        for ligne in lignes:
            article = articles[ligne.article_id]
            article.prix_moyen = _cmp(article.stock, article.prix_moyen, -ligne.quantite, ligne.prix_unitaire)
            article.stock -= ligne.quantite
            article.save(update_fields=['stock', 'prix_moyen'])
            _mouvement(article, MouvementStock.ANNULATION, -ligne.quantite,
                       ligne.prix_unitaire or article.prix_moyen, bon, user)
    elif isinstance(bon, BonSortie):
        for ligne in lignes:
            article = articles[ligne.article_id]
            article.prix_moyen = _cmp(article.stock, article.prix_moyen, ligne.quantite, ligne.prix_unitaire)
            article.stock += ligne.quantite
            article.save(update_fields=['stock', 'prix_moyen'])
            _mouvement(article, MouvementStock.ANNULATION, ligne.quantite, ligne.prix_unitaire, bon, user,
                       chantier=bon.chantier, engin=bon.engin)
    return _marquer(bon, Bon.ANNULE, user)


VALIDATEURS = {
    BonEntree: valider_entree,
    BonSortie: valider_sortie,
    Inventaire: valider_inventaire,
}


def valider(bon, user):
    return VALIDATEURS[type(bon)](bon, user)
