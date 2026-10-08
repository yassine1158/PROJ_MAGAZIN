from django.conf import settings

from .models import Article


def magasin(request):
    contexte = {'SOCIETE': settings.MAGASIN_SOCIETE, 'DEVISE': settings.MAGASIN_DEVISE, 'BUREAU': settings.BUREAU}
    if getattr(request, 'user', None) and request.user.is_authenticated:
        contexte['NB_ALERTES'] = Article.objects.en_alerte().count()
    return contexte
