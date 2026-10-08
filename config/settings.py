"""Paramètres du logiciel Magasin SI BÉTON.

En production, définir les variables d'environnement :
  DJANGO_SECRET_KEY, DJANGO_DEBUG=0, DJANGO_ALLOWED_HOSTS=exemple.pythonanywhere.com
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'dev-only-a-remplacer-en-production-0f3b9c1e7a',
)
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
        'NAME': BASE_DIR / 'db.sqlite3',
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
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'stock:connexion'
LOGIN_REDIRECT_URL = 'stock:accueil'

# --- Paramètres du magasin ---
MAGASIN_SOCIETE = os.environ.get('MAGASIN_SOCIETE', 'SI BÉTON')
MAGASIN_DEVISE = os.environ.get('MAGASIN_DEVISE', 'FCFA')
MAGASIN_SIGNATAIRE = os.environ.get('MAGASIN_SIGNATAIRE', 'LE MAGASINIER')

# --- Recherche IA ---
# Sans clé, la recherche fonctionne quand même (recherche locale sans IA).
# Clé : https://console.anthropic.com → API keys, puis ANTHROPIC_API_KEY=... dans l'environnement.
MAGASIN_IA_MODELE = os.environ.get('MAGASIN_IA_MODELE', 'claude-opus-5-5')
MAGASIN_IA_ACTIVE = bool(os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN'))

JAZZMIN_SETTINGS = {
    'site_title': 'Magasin SI BÉTON',
    'site_header': 'Magasin',
    'site_brand': 'Magasin SI BÉTON',
    'site_logo': 'stock/logo.png',
    'login_logo': 'stock/logo.png',
    'welcome_sign': 'Gestion du magasin',
    'copyright': 'SI BÉTON',
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
}
