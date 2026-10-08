from django.conf import settings
from django.templatetags.static import static

from config import produit

from .models import Article, Parametres
from .utils import nuances


def magasin(request):
    p = Parametres.actuels()
    primaire, fonce, clair = nuances(p.couleur)
    contexte = {
        'PRODUIT': produit.NOM,
        'PRODUIT_VERSION': produit.VERSION,
        'SOCIETE': p.nom_societe,
        'NOM_AFFICHE': p.nom_societe or produit.NOM,
        'LOGO_URL': p.logo.url if p.logo else static('stock/produit-logo.svg'),
        'LOGO_SOCIETE': bool(p.logo),
        'DEVISE': p.devise,
        'COULEURS': {'primaire': primaire, 'fonce': fonce, 'clair': clair},
        'BUREAU': settings.BUREAU,
    }
    if produit.CLE_PUBLIQUE:
        from .licence import etat
        contexte['LICENCE'] = getattr(request, 'licence', None) or etat()
    if getattr(request, 'user', None) and request.user.is_authenticated:
        contexte['NB_ALERTES'] = Article.objects.en_alerte().count()
    return contexte
