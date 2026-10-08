"""Magasin SI BÉTON – version bureau.

Lance le logiciel dans sa propre fenêtre (sans navigateur ni fenêtre noire).
Les données sont gardées dans %LOCALAPPDATA%\\Magasin SI BETON (base, photos, clés).

    python bureau.py           ouvre le logiciel
    python bureau.py --test    vérifie que tout démarre, sans ouvrir de fenêtre (utilisé à la construction)
"""
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

TITRE = 'Magasin SI BÉTON'


def dossier_donnees():
    base = os.environ.get('LOCALAPPDATA') or os.path.join(Path.home(), '.local', 'share')
    dossier = Path(base) / 'Magasin SI BETON'
    dossier.mkdir(parents=True, exist_ok=True)
    return dossier


def preparer(dossier):
    os.environ['MAGASIN_BUREAU'] = '1'
    os.environ['MAGASIN_DATA_DIR'] = str(dossier)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    from django.core.management import call_command

    django.setup()
    from django.core.management.commands.migrate import Command as Migrate

    call_command(Migrate(), interactive=False, verbosity=0)  # crée ou met à jour la base


def port_libre():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def demarrer_serveur(port):
    from waitress import create_server

    from config.wsgi import application

    serveur = create_server(application, host='127.0.0.1', port=port, threads=8)
    threading.Thread(target=serveur.run, daemon=True).start()
    for _ in range(100):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                return serveur
        except OSError:
            time.sleep(0.1)
    raise RuntimeError('Le serveur interne ne démarre pas.')


def verifier(url, dossier):
    """Contrôle sans fenêtre (à la construction) : pages, fichiers, exemple, PDF, Excel, fenêtre."""
    lignes = []
    for chemin in ['bienvenue/', 'connexion/', 'static/stock/app.css', 'static/stock/bon.js', 'static/admin/css/base.css',
                   'static/stock/vendor/three/three.module.min.js', 'admin/login/']:
        with urllib.request.urlopen(url + chemin, timeout=20) as reponse:
            assert reponse.status == 200, chemin
            lignes.append(f'OK {chemin} ({len(reponse.read())} octets)')
    from django.core.management import call_command

    from stock import exports
    from stock.management.commands.demo import Command as Demo
    from stock.models import Article, BonSortie
    from stock.pdf import reponse_pdf

    if not Article.objects.exists():
        call_command(Demo(), stdout=open(os.devnull, 'w'))
    lignes.append(f'OK exemple ({Article.objects.count()} articles)')
    lignes.append(f'OK PDF ({len(reponse_pdf([BonSortie.objects.first()], "test").content)} octets)')
    lignes.append(f'OK Excel ({len(exports.etat_stock(Article.objects.all()).content)} octets)')
    # Écrans après connexion (vérifie que gabarits et balises sont bien inclus dans le programme).
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.test import Client

    settings.ALLOWED_HOSTS.append('testserver')
    testeur, _ = get_user_model().objects.get_or_create(username='verification-construction',
                                                         defaults={'is_staff': True, 'is_superuser': True})
    client = Client()
    client.force_login(testeur)
    for chemin in ['/', '/entree/', '/sortie/', '/articles/', '/articles/nouveau/', '/bons/', '/recherche/',
                   '/tableau-de-bord/', '/consommation/', '/reglages/ia/', '/admin/', '/admin/stock/article/']:
        code = client.get(chemin).status_code
        assert code == 200, f'{chemin} → {code}'
        lignes.append(f'OK écran {chemin}')
    testeur.delete()
    import webview  # noqa: F401  (la bibliothèque de la fenêtre est bien incluse)
    lignes.append('OK fenêtre')
    (dossier / 'verification.txt').write_text('\n'.join(lignes) + '\nVérification réussie.\n', encoding='utf-8')


def signaler(dossier, message):
    """Sans console, on écrit l'erreur dans un fichier et on l'affiche dans une boîte de dialogue."""
    (dossier / 'erreur-demarrage.txt').write_text(message, encoding='utf-8')
    if sys.platform == 'win32' and '--test' not in sys.argv:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, f"Le logiciel n'a pas pu démarrer.\n\nDétails enregistrés dans :\n{dossier / 'erreur-demarrage.txt'}",
            TITRE, 0x10)
    else:
        print(message, file=sys.stderr)


def main():
    dossier = dossier_donnees()
    try:
        preparer(dossier)
        port = port_libre()
        serveur = demarrer_serveur(port)
        url = f'http://127.0.0.1:{port}/'
        if '--test' in sys.argv:
            verifier(url, dossier)
            serveur.close()
            return
        import webview

        icone = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'bureau' / 'icone.ico'
        webview.create_window(TITRE, url, width=1400, height=900, min_size=(1000, 650), maximized=True,
                              text_select=True, zoomable=True)
        try:
            webview.start(gui='edgechromium' if sys.platform == 'win32' else None, private_mode=False,
                          storage_path=str(dossier / 'fenetre'), icon=str(icone) if icone.exists() else None)
        except Exception as e:
            raise RuntimeError(
                "La fenêtre ne peut pas s'ouvrir. Installez « Microsoft Edge WebView2 Runtime » "
                '(gratuit) : https://go.microsoft.com/fwlink/p/?LinkId=2124703 puis relancez le logiciel.') from e
        serveur.close()
    except Exception:
        signaler(dossier, traceback.format_exc())
        sys.exit(1)


if __name__ == '__main__':
    main()
