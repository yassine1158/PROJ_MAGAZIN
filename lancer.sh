#!/usr/bin/env bash
# Lancement du logiciel (Linux / macOS) : ./lancer.sh
set -e
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
. venv/bin/activate
pip install -q --disable-pip-version-check -r requirements.txt
python manage.py migrate -v0
if ! python manage.py shell -c "from django.contrib.auth import get_user_model as U; import sys; sys.exit(0 if U().objects.filter(is_superuser=True).exists() else 1)"; then
  echo "Création du compte administrateur :"
  python manage.py createsuperuser
  read -r -p "Installer un magasin d'exemple pour essayer ? (o/N) : " demo
  [[ "$demo" =~ ^[oO]$ ]] && python manage.py demo
fi
echo "Le logiciel est ouvert sur http://127.0.0.1:8000 (Ctrl+C pour arrêter)"
python manage.py runserver 127.0.0.1:8000
