"""Paramètres Django du logiciel (nom du produit : config/produit.py).

En production web, définir les variables d'environnement :
  DJANGO_SECRET_KEY, DJANGO_DEBUG=0, DJANGO_ALLOWED_HOSTS=exemple.pythonanywhere.com
La version bureau (bureau.py) définit MAGASIN_BUREAU=1 et MAGASIN_DATA_DIR.
"""
import os
import secrets
from pathlib import Path

from config import produit

BASE_DIR = Path(__file__).resolve().parent.parent

# Version bureau : données dans le dossier de l'utilisateur (base, photos, clés).
BUREAU = os.environ.get('MAGASIN_BUREAU') == '1'
DATA_DIR = Path(os.environ.get('MAGASIN_DATA_DIR') or BASE_DIR)


def _secret_local():
    """Clé secrète propre à cette installation, créée au premier lancement."""
    fichier = DATA_DIR / 'secret.key'
    if not fichier.exists():
        fichier.write_text(secrets.token_urlsafe(50))
    return fichier.read_text().strip()


if BUREAU:
    SECRET_KEY = _secret_local()
    DEBUG = False
    ALLOWED_HOSTS = ['127.0.0.1', 'localhost']
else:
    SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'dev-only-a-remplacer-en-production-0f3b9c1e7a')
    DEBUG = os.environ.get('DJANGO_DEBUG', '1') == '1'
    ALLOWED_HOSTS = [h for h in os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h]

INSTALLED_APPS = [
    'jazzmin',  # doit rester avant django.contrib.admin
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'stock',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    *(['whitenoise.middleware.WhiteNoiseMiddleware'] if BUREAU else []),
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'stock.context_processors.magasin',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': DATA_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'fr'
TIME_ZONE = 'Africa/Abidjan'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = None if BUREAU else BASE_DIR / 'staticfiles'
MEDIA_URL = 'media/'
MEDIA_ROOT = DATA_DIR / 'media'
# Version bureau : les fichiers du logiciel sont servis directement depuis le programme.
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = False

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'stock:connexion'
LOGIN_REDIRECT_URL = 'stock:accueil'

# Nom, logo, couleur, devise de la société : écran Réglages › Ma société (modèle stock.Parametres).

# --- Recherche IA ---
# Sans clé, la recherche fonctionne quand même (recherche locale sans IA).
# Clé : https://console.anthropic.com → API keys, puis ANTHROPIC_API_KEY=... dans l'environnement.
MAGASIN_IA_MODELE = os.environ.get('MAGASIN_IA_MODELE', 'claude-opus-5-5')
# Version bureau : la clé peut aussi être enregistrée depuis l'écran Réglages › Recherche IA.
MAGASIN_CLE_IA_FICHIER = DATA_DIR / 'cle-ia.txt'
if not os.environ.get('ANTHROPIC_API_KEY') and MAGASIN_CLE_IA_FICHIER.exists():
    os.environ['ANTHROPIC_API_KEY'] = MAGASIN_CLE_IA_FICHIER.read_text().strip()
MAGASIN_IA_ACTIVE = bool(os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN'))

if BUREAU:
    # Pas de console : les erreurs vont dans un fichier, pour pouvoir les envoyer.
    LOGGING = {
        'version': 1,
        'disable_existing_loggers': False,
        'handlers': {'fichier': {'class': 'logging.FileHandler', 'filename': DATA_DIR / 'journal.log',
                                 'encoding': 'utf-8', 'level': 'WARNING'}},
        'root': {'handlers': ['fichier'], 'level': 'WARNING'},
    }

JAZZMIN_SETTINGS = {
    'site_title': produit.NOM,
    'site_header': 'Magasin',
    'site_brand': produit.NOM,
    'site_logo': 'stock/produit-icone.png',
    'login_logo': 'stock/produit-icone.png',
    'welcome_sign': 'Gestion du magasin',
    'copyright': produit.NOM,
    'topmenu_links': [
        {'name': '← Retour au magasin', 'url': 'stock:accueil'},
    ],
    'order_with_respect_to': [
        'stock', 'stock.article', 'stock.bonentree', 'stock.bonsortie',
        'stock.inventaire', 'stock.mouvementstock',
    ],
    'icons': {
        'auth.user': 'fas fa-user',
        'auth.group': 'fas fa-users',
        'stock.article': 'fas fa-box',
        'stock.bloc': 'fas fa-th-large',
        'stock.etagere': 'fas fa-layer-group',
        'stock.categorie': 'fas fa-tags',
        'stock.fournisseur': 'fas fa-truck',
        'stock.chantier': 'fas fa-hard-hat',
        'stock.engin': 'fas fa-truck-monster',
        'stock.bonentree': 'fas fa-arrow-circle-down',
        'stock.bonsortie': 'fas fa-arrow-circle-up',
        'stock.inventaire': 'fas fa-clipboard-check',
        'stock.mouvementstock': 'fas fa-exchange-alt',
    },
    'changeform_format': 'single',
    'use_google_fonts_cdn': False,  # aucune ressource internet : affichage immédiat
    'custom_css': 'stock/admin.css',
}
