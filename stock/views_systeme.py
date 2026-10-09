"""Réglages › Sauvegarde (copies des données, clé USB, restauration, version du logiciel et mises à jour),
effacement des données d'exemple et accès depuis les téléphones du magasin.

Réservés aux responsables.
"""
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import mises_a_jour, reseau, sauvegarde as s
from .bureau import livrer, ouvrir_fichier
from .views_reglages import responsable

RETOUR = 'stock:reglages_sauvegarde'


def _confirme(request, mot):
    return request.POST.get('confirmation', '').strip().upper() == mot


@responsable
def sauvegarde(request):
    etat = s.lire_etat()
    if etat.get('restauration'):
        s.oublier_resultat_restauration()  # message montré une seule fois
    copie_usb = etat.get('copie_usb') or {}
    try:
        copie_usb['date'] = datetime.fromisoformat(copie_usb['date'])
    except (KeyError, TypeError, ValueError):
        copie_usb = {}
    sauvegardes = s.lister()
    return render(request, 'stock/reglages/sauvegarde.html', {
        'sauvegardes': sauvegardes,
        'dossier': s.dossier_sauvegardes(),
        'cles': s.cles_usb() if settings.BUREAU else [],
        'copie_usb': copie_usb,
        'copie_usb_ancienne': bool(settings.BUREAU and sauvegardes and (
            not copie_usb or datetime.now() - copie_usb['date'] > timedelta(days=7))),
        'attente': s.restauration_en_attente(),
        'resultat': etat.get('restauration'),
        'comptes': s.compter_donnees(),
        'version': mises_a_jour.resume() if settings.BUREAU else None,
    })


@responsable
@require_POST
def mise_a_jour_verifier(request):
    if not settings.BUREAU:
        messages.error(request, 'Les mises à jour se vérifient dans le logiciel installé sur Windows.')
        return redirect(RETOUR)
    try:
        mises_a_jour.verifier_maintenant()
    except mises_a_jour.ErreurMiseAJour:
        pass  # le message est affiché dans la partie « Version du logiciel »
    return redirect(reverse(RETOUR) + '#version')


@responsable
@require_POST
def sauvegarde_nouvelle(request):
    try:
        chemin = s.sauvegarder('manuelle')
        messages.success(request, f'Sauvegarde faite : {chemin.name}')
    except s.ErreurSauvegarde as e:
        messages.error(request, str(e))
    return redirect(RETOUR)


@responsable
def sauvegarde_telecharger(request, nom):
    chemin = s.trouver(nom)
    if not chemin:
        messages.error(request, "Cette sauvegarde n'existe plus.")
        return redirect(RETOUR)
    if settings.BUREAU:
        reponse = HttpResponse(chemin.read_bytes(), content_type='application/zip')
        reponse['Content-Disposition'] = f'attachment; filename="{chemin.name}"'
        return livrer(request, reponse)
    return FileResponse(open(chemin, 'rb'), as_attachment=True, filename=chemin.name, content_type='application/zip')


@responsable
@require_POST
def sauvegarde_usb(request, nom):
    try:
        cible, cle = s.copier_sur_cle(nom, request.POST.get('cle') or None)
        messages.success(request, f'Sauvegarde copiée sur la clé {cle["nom"]}, dans le dossier '
                                  f'« {cible.parent.name} ». Vous pouvez retirer la clé.')
    except ValueError as e:
        messages.error(request, str(e))
    return redirect(RETOUR)


@responsable
@require_POST
def sauvegarde_dossier(request):
    dossier = s.dossier_sauvegardes()
    if not settings.BUREAU:
        messages.error(request, 'Cette action est disponible seulement dans le logiciel installé sur Windows.')
        return redirect(RETOUR)
    if not reseau.est_local(request):  # téléphone : le dossier s'ouvrirait sur l'écran de l'ordinateur
        messages.error(request, 'Cette action est disponible seulement sur l\'ordinateur où le logiciel est installé.')
        return redirect(RETOUR)
    try:
        dossier.mkdir(parents=True, exist_ok=True)
        ouvrir_fichier(dossier)
    except OSError:
        messages.error(request, f"Le dossier n'a pas pu être ouvert. Il se trouve ici : {dossier}")
    return redirect(RETOUR)


@responsable
@require_POST
def sauvegarde_restaurer(request):
    if not _confirme(request, 'RESTAURER'):
        messages.error(request, 'Rien n\'a été changé : pour confirmer, tapez RESTAURER dans la case prévue.')
        return redirect(RETOUR)
    nom = request.POST.get('nom', '')
    source = s.trouver(nom) if nom else request.FILES.get('fichier')
    if not source:
        messages.error(request, 'Choisissez une sauvegarde dans la liste, ou un fichier .zip.')
        return redirect(RETOUR)
    try:
        s.preparer_restauration(source)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect(RETOUR)
    if settings.BUREAU:
        messages.success(request, 'La sauvegarde est prête. Fermez le logiciel puis rouvrez-le : elle sera remise en '
                                  'place au démarrage (vos données actuelles sont sauvegardées juste avant).')
    else:
        messages.success(request, 'La sauvegarde est prête. Redémarrez le serveur pour la remettre en place '
                                  '(vos données actuelles sont sauvegardées juste avant).')
    return redirect(RETOUR)


@responsable
@require_POST
def sauvegarde_annuler_restauration(request):
    s.annuler_restauration()
    messages.success(request, 'Restauration annulée : vos données actuelles sont gardées.')
    return redirect(RETOUR)


def _vider(request):
    try:
        chemin = s.vider_donnees()
    except s.ErreurSauvegarde as e:
        messages.error(request, f"Rien n'a été effacé. {e}")
        return redirect(RETOUR)
    messages.success(request, f'Données effacées (une copie a été gardée : {chemin.name}). Vous pouvez commencer : '
                              'dessinez le plan du magasin, puis ajoutez vos articles.')
    return redirect('stock:accueil')


@responsable
@require_POST
def repartir_a_zero(request):
    if not _confirme(request, 'EFFACER'):
        messages.error(request, "Rien n'a été effacé : pour confirmer, tapez EFFACER dans la case prévue.")
        return redirect(RETOUR)
    return _vider(request)


@responsable
def exemple_effacer(request):
    if not s.exemple_present():
        messages.info(request, "Il n'y a pas de magasin d'exemple à effacer.")
        return redirect('stock:accueil')
    if request.method == 'POST':
        return _vider(request)
    return render(request, 'stock/reglages/exemple_effacer.html', {'comptes': s.compter_donnees()})


# =========================================================
# Accès depuis les téléphones
# =========================================================

@responsable
def telephones(request):
    if request.method == 'POST':
        try:
            return _telephones_changer(request)
        except OSError:
            reseau.journal.exception('Réglage de l\'accès des téléphones impossible à enregistrer')
            messages.error(request, "Le réglage n'a pas pu être enregistré. Vérifiez qu'il reste de la place sur "
                                    "le disque, puis réessayez.")
            return redirect('stock:reglages_telephones')
    reglage = reseau.lire_reglage()
    actif = settings.BUREAU and reseau.est_actif()
    adresse = reseau.adresse() if actif else None
    adresse_web = request.build_absolute_uri('/')
    return render(request, 'stock/reglages/telephones.html', {
        'reglage': reglage,
        'actif': actif,
        'adresse': adresse,
        'autres_adresses': reseau.autres_adresses() if adresse else [],
        'qr': reseau.qr_data_uri(adresse or adresse_web) if adresse or not settings.BUREAU else '',
        'erreur': (reseau.derniere_erreur() or "L'accès des téléphones ne fonctionne pas pour le moment.")
        if settings.BUREAU and reglage['actif'] and not actif else '',
        'local': reseau.est_local(request),
        'adresse_web': adresse_web,
        'port_defaut': reseau.PORT_DEFAUT,
    })


def _telephones_changer(request):
    retour = redirect('stock:reglages_telephones')
    if not settings.BUREAU:
        messages.error(request, "Rien n'a été changé : dans la version web, l'accès se règle sur le serveur.")
        return retour
    if not reseau.est_local(request):
        messages.error(request, "Rien n'a été changé : ce réglage se change seulement sur l'ordinateur où le "
                                "logiciel est installé.")
        return retour
    action = request.POST.get('action')
    if action == 'desactiver':
        reseau.ecrire_reglage(actif=False)
        reseau.arreter()
        messages.success(request, 'Accès des téléphones désactivé : ils ne peuvent plus se connecter.')
        return retour
    if action == 'port':
        port = request.POST.get('port', '').strip()
        if not reseau.port_valide(port):
            messages.error(request, f'Le port doit être un nombre entre 1024 et 65535 (par défaut {reseau.PORT_DEFAUT}).')
            return retour
        reglage = reseau.ecrire_reglage(port=int(port))
        if not reglage['actif']:
            messages.success(request, f'Port enregistré : {port}.')
            return retour
    elif action != 'activer':
        return retour
    reseau.ecrire_reglage(actif=True)
    try:
        port = reseau.demarrer()
    except Exception as e:
        if not isinstance(e, reseau.ErreurReseau):
            reseau.journal.exception('Accès des téléphones impossible')
            e = 'Réessayez, ou redémarrez l\'ordinateur si cela continue.'
        reseau.ecrire_reglage(actif=False)
        messages.error(request, f"L'accès des téléphones n'a pas pu être activé. {e}")
        return retour
    messages.success(request, f'Accès des téléphones activé (port {port}). Sur le téléphone, ouvrez l\'adresse '
                              'ci-dessous ou scannez le QR code.')
    return retour
