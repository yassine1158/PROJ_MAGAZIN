from django.conf import settings


def magasin(request):
    return {'SOCIETE': settings.MAGASIN_SOCIETE, 'DEVISE': settings.MAGASIN_DEVISE}
