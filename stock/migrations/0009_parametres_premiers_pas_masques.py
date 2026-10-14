from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stock', '0008_article_cree_le'),
    ]

    operations = [
        migrations.AddField(
            model_name='parametres',
            name='premiers_pas_masques',
            field=models.BooleanField(default=False, verbose_name='guide « Bien démarrer » masqué'),
        ),
    ]
