from django.db import migrations, models
from django.db.models import Min


def dater_articles(apps, schema_editor):
    """Articles déjà créés : la date de leur premier mouvement (ceux qui n'ont jamais bougé restent sans date)."""
    Article = apps.get_model('stock', 'Article')
    MouvementStock = apps.get_model('stock', 'MouvementStock')
    premiers = dict(MouvementStock.objects.values('article').annotate(premier=Min('date'))
                    .values_list('article', 'premier'))
    articles = list(Article.objects.filter(pk__in=premiers).only('pk'))
    for article in articles:
        article.cree_le = premiers[article.pk]
    Article.objects.bulk_update(articles, ['cree_le'], batch_size=500)
    # L'ajout du champ (auto_now_add) a daté tous les articles du moment de la mise à jour : on l'efface.
    Article.objects.exclude(pk__in=list(premiers)).update(cree_le=None)


class Migration(migrations.Migration):

    dependencies = [
        ('stock', '0007_mouvement_prix_moyen_apres'),
    ]

    operations = [
        migrations.AddField(
            model_name='article',
            name='cree_le',
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name='créé le'),
        ),
        migrations.RunPython(dater_articles, migrations.RunPython.noop),
    ]
