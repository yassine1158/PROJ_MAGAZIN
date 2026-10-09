@echo off
chcp 65001 >nul
title MagaStock
cd /d "%~dp0"
setlocal EnableDelayedExpansion
rem Usage local : messages d'erreur detailles et fichiers servis par Django
set DJANGO_DEBUG=1

rem --- Python ---
set PY=python
%PY% --version >nul 2>&1 || set PY=py
%PY% --version >nul 2>&1 || (
  echo.
  echo  Python n'est pas installe.
  echo  Installez-le depuis https://www.python.org/downloads/
  echo  en cochant "Add python.exe to PATH", puis relancez ce fichier.
  echo.
  pause
  exit /b 1
)

rem --- Premiere installation ---
if not exist venv (
  echo  Premiere installation, patientez quelques minutes...
  %PY% -m venv venv || (pause & exit /b 1)
)
call venv\Scripts\activate.bat
python -m pip install -q --disable-pip-version-check -r requirements.txt || (
  echo  Installation impossible : verifiez la connexion internet.
  pause
  exit /b 1
)
python manage.py migrate -v0 || (pause & exit /b 1)

rem --- Compte administrateur (une seule fois) ---
python manage.py shell -c "from django.contrib.auth import get_user_model as U; import sys; sys.exit(0 if U().objects.filter(is_superuser=True).exists() else 1)"
if errorlevel 1 (
  echo.
  echo  Creation du compte administrateur :
  python manage.py createsuperuser || (pause & exit /b 1)
  echo.
  set /p DEMO=" Installer un magasin d'exemple pour essayer ? (O/N) : "
  if /i "!DEMO!"=="O" python manage.py demo
)

rem --- Lancement ---
echo.
echo  Le logiciel est ouvert sur http://127.0.0.1:8000
echo  Laissez cette fenetre ouverte. Pour arreter : fermez-la.
echo.
start "" http://127.0.0.1:8000
python manage.py runserver 127.0.0.1:8000
pause
