"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

restauration = None
if os.environ.get('MAGASIN_BUREAU') != '1':  # la version bureau le fait elle-même (bureau.py)
    from stock.sauvegarde import appliquer_restauration_en_attente

    restauration = appliquer_restauration_en_attente()  # choisie dans Réglages › Sauvegarde, avant d'ouvrir la base

application = get_wsgi_application()

if restauration and restauration['ok']:  # sauvegarde d'une version plus ancienne : mise à niveau de la base
    from django.core.management import call_command

    call_command('migrate', interactive=False, verbosity=0)
