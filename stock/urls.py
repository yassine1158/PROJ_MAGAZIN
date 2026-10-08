from django.contrib.auth.views import LogoutView
from django.urls import path
from django.views.generic import RedirectView

from . import views, views_magasin as v, views_reglages as r

app_name = 'stock'

urlpatterns = [
    path('', v.accueil, name='accueil'),
    path('connexion/', v.Connexion.as_view(), name='connexion'),
    path('bienvenue/', v.bienvenue, name='bienvenue'),
    path('activation/', v.activation, name='activation'),
    path('reglages/ia/', v.reglages_ia, name='reglages_ia'),
    path('reglages/societe/', r.societe, name='reglages_societe'),
    path('reglages/plan/', r.plan, name='reglages_plan'),
    path('reglages/plan/blocs/nouveau/', r.bloc_form, name='bloc_nouveau'),
    path('reglages/plan/blocs/<int:pk>/', r.bloc_form, name='bloc_modifier'),
    path('reglages/plan/blocs/<int:pk>/supprimer/', r.bloc_supprimer, name='bloc_supprimer'),
    path('reglages/plan/blocs/<int:bloc_pk>/etageres/nouvelle/', r.etagere_form, name='etagere_nouvelle'),
    path('reglages/plan/blocs/<int:bloc_pk>/rangee/', r.rangee, name='rangee'),
    path('reglages/plan/etageres/<int:pk>/', r.etagere_form, name='etagere_modifier'),
    path('reglages/plan/etageres/<int:pk>/supprimer/', r.etagere_supprimer, name='etagere_supprimer'),
    path('reglages/listes/', r.listes, name='reglages_listes_defaut'),
    path('reglages/listes/<str:type_>/', r.listes, name='reglages_listes'),
    path('reglages/listes/<str:type_>/<int:pk>/', r.liste_modifier, name='liste_modifier'),
    path('reglages/listes/<str:type_>/<int:pk>/supprimer/', r.liste_supprimer, name='liste_supprimer'),
    path('reglages/utilisateurs/', r.utilisateurs, name='reglages_utilisateurs'),
    path('reglages/utilisateurs/nouveau/', r.utilisateur_form, name='utilisateur_nouveau'),
    path('reglages/utilisateurs/<int:pk>/', r.utilisateur_form, name='utilisateur_modifier'),
    path('deconnexion/', LogoutView.as_view(next_page='stock:connexion'), name='deconnexion'),
    path('recherche/', views.recherche, name='recherche'),

    path('entree/', v.entree, name='entree'),
    path('sortie/', v.sortie, name='sortie'),
    path('bons/', v.bons, name='bons'),
    path('bons/<str:type_>/<int:pk>/', v.bon_detail, name='bon_detail'),
    path('bons/<str:type_>/<int:pk>/annuler/', v.bon_annuler, name='bon_annuler'),
    path('bons/<str:modele>/<int:pk>/pdf/', views.bon_pdf, name='bon_pdf'),

    path('articles/', v.articles, name='articles'),
    path('articles/nouveau/', v.article_nouveau, name='article_nouveau'),
    path('articles/<int:pk>/', v.article_modifier, name='article_modifier'),
    path('articles/<int:pk>/corriger/', v.article_corriger, name='article_corriger'),

    path('tableau-de-bord/', views.dashboard, name='dashboard'),
    path('consommation/', views.consommation, name='consommation'),
    path('etat-stock/', RedirectView.as_view(pattern_name='stock:articles', query_string=True), name='etat_stock'),

    path('api/recherche/', views.api_recherche, name='api_recherche'),
    path('api/articles/', v.api_articles, name='api_articles'),
    path('api/articles/<int:pk>/', views.api_article, name='api_article'),
    path('api/plan/', views.api_plan, name='api_plan'),
    path('api/plan/enregistrer/', views.api_plan_enregistrer, name='api_plan_enregistrer'),
    path('reglages/plan/editeur/', r.editeur_plan, name='editeur_plan'),
]
