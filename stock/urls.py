from django.contrib.auth.views import LogoutView
from django.urls import path
from django.views.generic import RedirectView

from . import views, views_magasin as v

app_name = 'stock'

urlpatterns = [
    path('', v.accueil, name='accueil'),
    path('connexion/', v.Connexion.as_view(), name='connexion'),
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
]
