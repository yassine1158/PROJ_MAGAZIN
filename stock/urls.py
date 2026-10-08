from django.urls import path

from . import views

app_name = 'stock'

urlpatterns = [
    path('', views.recherche, name='recherche'),
    path('tableau-de-bord/', views.dashboard, name='dashboard'),
    path('etat-stock/', views.etat_stock, name='etat_stock'),
    path('consommation/', views.consommation, name='consommation'),
    path('bons/<str:modele>/<int:pk>/pdf/', views.bon_pdf, name='bon_pdf'),
    path('api/recherche/', views.api_recherche, name='api_recherche'),
    path('api/articles/<int:pk>/', views.api_article, name='api_article'),
    path('api/plan/', views.api_plan, name='api_plan'),
]
