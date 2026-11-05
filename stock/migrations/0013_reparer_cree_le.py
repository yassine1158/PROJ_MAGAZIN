from datetime import timedelta

from django.db import migrations
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Exists, OuterRef

# 0008 dure quelques secondes : l'heure qu'elle a donnée aux articles est prise juste avant qu'elle soit notée.
MARGE = timedelta(hours=1)


def reparer(apps, schema_editor):
    """Copies déjà passées en 1.6.0 : 0008 a donné l'heure de la mise à jour aux articles jamais mouvementés.

    Ces articles ont une date prise juste avant que 0008 soit notée, et aucun mouvement à cette date ou avant.
    Un article daté par 0008 d'après son premier mouvement a un mouvement à cette date ; un article créé après la
    mise à jour est plus récent. Seuls les premiers redeviennent sans date, comme lors d'une mise à jour 1.6.1.
    """
    faite = (MigrationRecorder(schema_editor.connection).migration_qs
             .filter(app='stock', name='0008_article_cree_le').values_list('applied', flat=True).first())
    if faite is None:
        return
    Article = apps.get_model('stock', 'Article')
    MouvementStock = apps.get_model('stock', 'MouvementStock')
    deja_bouge = MouvementStock.objects.filter(article=OuterRef('pk'), date__lte=OuterRef('cree_le'))
    (Article.objects.filter(cree_le__gte=faite - MARGE, cree_le__lte=faite)
     .exclude(Exists(deja_bouge)).update(cree_le=None))


class Migration(migrations.Migration):

    dependencies = [
        ('stock', '0012_fusion_demarrage'),
    ]

    operations = [
        migrations.RunPython(reparer, migrations.RunPython.noop),
    ]
