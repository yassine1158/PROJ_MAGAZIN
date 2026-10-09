from django.conf import settings
from django.templatetags.static import static

from config import produit

from .models import Article, Parametres
from .reseau import est_local
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
        'VERSION_BUREAU': settings.BUREAU,
        'BUREAU': settings.BUREAU and est_local(request),  # devant l'ordinateur (un téléphone reçoit les fichiers)
    }
    if produit.CLE_PUBLIQUE:
        from .licence import etat
        contexte['LICENCE'] = getattr(request, 'licence', None) or etat()
    if getattr(request, 'user', None) and request.user.is_authenticated:
        contexte['NB_ALERTES'] = Article.objects.en_alerte().count()
        from .sauvegarde import exemple_present
        contexte['EXEMPLE'] = exemple_present()
    return contexte


def mise_a_jour(request):
    """Carte « Nouvelle version disponible » : logiciel Windows, responsable, devant l'ordinateur."""
    utilisateur = getattr(request, 'user', None)
    if not (settings.BUREAU and utilisateur and utilisateur.is_staff and est_local(request)):
        return {}
    from .mises_a_jour import derniere
    return {'MISE_A_JOUR': derniere()}
