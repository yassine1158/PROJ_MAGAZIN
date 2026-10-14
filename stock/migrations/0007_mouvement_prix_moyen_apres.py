from decimal import Decimal

from django.db import migrations, models

CENTIME = Decimal('0.01')


def _cmp(stock, prix_moyen, quantite, prix):
    """Même calcul que services._cmp au moment de cette migration."""
    if not prix:
        return prix_moyen
    nouveau_stock = stock + quantite
    if nouveau_stock <= 0:
        return prix_moyen
    return ((stock * prix_moyen + quantite * prix) / nouveau_stock).quantize(CENTIME)


def retrouver_prix_moyens(apps, schema_editor):
    """Rejoue l'historique de chaque article pour retrouver son prix moyen après chaque mouvement passé.

    Seules les entrées et les annulations changent le prix moyen. Si le calcul ne retombe pas sur le prix
    moyen actuel de l'article, rien n'est écrit pour lui : l'état du stock à une date utilisera alors le prix
    moyen actuel, en le signalant.
    """
    Article = apps.get_model('stock', 'Article')
    MouvementStock = apps.get_model('stock', 'MouvementStock')
    for article in Article.objects.only('pk', 'prix_moyen').iterator():
        mouvements = list(MouvementStock.objects.filter(article_id=article.pk).order_by('id')
                          .only('id', 'type', 'quantite', 'stock_apres', 'prix_unitaire'))
        prix_moyen = Decimal('0')
        for m in mouvements:
            if m.type in ('ENTREE', 'ANNULATION'):
                prix_moyen = _cmp(m.stock_apres - m.quantite, prix_moyen, m.quantite, m.prix_unitaire)
            m.prix_moyen_apres = prix_moyen
        if mouvements and prix_moyen == article.prix_moyen:
            MouvementStock.objects.bulk_update(mouvements, ['prix_moyen_apres'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('stock', '0006_parametres_rccm_ncc_prix_sortie'),
    ]

    operations = [
        migrations.AddField(
            model_name='mouvementstock',
            name='prix_moyen_apres',
            field=models.DecimalField(blank=True, decimal_places=2, editable=False, max_digits=16, null=True,
                                      verbose_name='prix moyen après'),
        ),
        migrations.RunPython(retrouver_prix_moyens, migrations.RunPython.noop),
    ]
