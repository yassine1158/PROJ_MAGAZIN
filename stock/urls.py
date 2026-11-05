from django.contrib.auth.views import LogoutView
from django.urls import path
from django.views.generic import RedirectView

from . import views, views_demandes as d, views_magasin as v, views_reglages as r

app_name = 'stock'

urlpatterns = [
    path('', v.accueil, name='accueil'),
    path('connexion/', v.Connexion.as_view(), name='connexion'),
    path('connexion/mot-de-passe-oublie/', v.mot_de_passe_oublie, name='mot_de_passe_oublie'),
    path('bienvenue/', v.bienvenue, name='bienvenue'),
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
    path('retour/', v.retour, name='retour'),
    # Demandes de pièces : formulaire du QR code de l'atelier (sans compte), puis traitement au magasin.
    path('demande/', d.demande, name='demande'),
    path('demande/pieces/', d.demande_articles, name='demande_articles'),
    path('demande/suivi/<str:jeton>/', d.demande_suivi, name='demande_suivi'),
    path('demandes/', d.demandes, name='demandes'),
    path('demandes/affiche/', d.demandes_affiche, name='demandes_affiche'),
    path('demandes/<int:pk>/', d.demande_detail, name='demande_detail'),
    path('demandes/<int:pk>/pdf/', d.demande_pdf, name='demande_pdf'),
    path('demandes/<int:pk>/rouvrir/', d.demande_annuler_refus, name='demande_rouvrir'),
    path('api/demandes/', d.api_demandes, name='api_demandes'),
    path('bons/', v.bons, name='bons'),
    path('bons/<str:type_>/<int:pk>/', v.bon_detail, name='bon_detail'),
    path('bons/<str:type_>/<int:pk>/annuler/', v.bon_annuler, name='bon_annuler'),
    path('bons/<str:type_>/<int:pk>/valider/', v.bon_valider, name='bon_valider'),
    path('bons/<str:type_>/<int:pk>/supprimer/', v.bon_supprimer, name='bon_supprimer'),
    path('bons/<str:modele>/<int:pk>/pdf/', views.bon_pdf, name='bon_pdf'),

    path('articles/', v.articles, name='articles'),
    path('articles/nouveau/', v.article_nouveau, name='article_nouveau'),
    path('articles/<int:pk>/', v.article_modifier, name='article_modifier'),
    path('articles/<int:pk>/corriger/', v.article_corriger, name='article_corriger'),
    path('articles/<int:pk>/historique/', v.article_historique, name='article_historique'),
    path('articles/importer/', v.import_articles, name='import_articles'),
    path('articles/importer/apercu/', v.import_apercu, name='import_apercu'),
    path('articles/importer/confirmer/', v.import_confirmer, name='import_confirmer'),
    path('articles/importer/termine/', v.import_resultat, name='import_resultat'),
    path('articles/importer/modele/', v.import_modele, name='import_modele'),
    path('articles/importer/rapport/', v.import_rapport, name='import_rapport'),
    path('premiers-pas/', v.premiers_pas, name='premiers_pas'),
    path('exemple/installer/', v.exemple_installer, name='exemple_installer'),

    path('inventaires/', v.inventaires, name='inventaires'),
    path('inventaires/nouveau/', v.inventaire_nouveau, name='inventaire_nouveau'),
    path('inventaires/<int:pk>/', v.inventaire, name='inventaire'),
    path('inventaires/<int:pk>/compter/', v.inventaire_compter, name='inventaire_compter'),
    path('inventaires/<int:pk>/fiche/', v.inventaire_fiche, name='inventaire_fiche'),
    path('inventaires/<int:pk>/valider/', v.inventaire_valider, name='inventaire_valider'),
    path('inventaires/<int:pk>/resultat/', v.inventaire_resultat, name='inventaire_resultat'),
    path('inventaires/<int:pk>/supprimer/', v.inventaire_supprimer, name='inventaire_supprimer'),

    path('tableau-de-bord/', views.dashboard, name='dashboard'),
    path('consommation/', views.consommation, name='consommation'),
    path('rapports/mouvements/', views.journal, name='journal'),
    path('rapports/etat-stock/', views.etat_stock_date, name='etat_stock_date'),
    path('etat-stock/', RedirectView.as_view(pattern_name='stock:articles', query_string=True), name='etat_stock'),

    path('api/recherche/', views.api_recherche, name='api_recherche'),
    path('api/articles/', v.api_articles, name='api_articles'),
    path('api/articles/creer/', v.api_article_creer, name='api_article_creer'),
    path('api/retour/reste/', v.api_reste_chantier, name='api_reste_chantier'),
    path('api/articles/<int:pk>/', views.api_article, name='api_article'),
    path('api/plan/', views.api_plan, name='api_plan'),
    path('api/plan/enregistrer/', views.api_plan_enregistrer, name='api_plan_enregistrer'),
    path('reglages/plan/editeur/', r.editeur_plan, name='editeur_plan'),

    path('etiquettes/', v.etiquettes, name='etiquettes'),
    path('a/<path:code>/', v.qr_article, name='qr_article'),
    path('e/<int:pk>/', v.qr_etagere, name='qr_etagere'),
]

# Système
from . import views_systeme as sy  # noqa: E402

urlpatterns += [
    path('reglages/sauvegarde/', sy.sauvegarde, name='reglages_sauvegarde'),
    path('reglages/sauvegarde/nouvelle/', sy.sauvegarde_nouvelle, name='sauvegarde_nouvelle'),
    path('reglages/sauvegarde/dossier/', sy.sauvegarde_dossier, name='sauvegarde_dossier'),
    path('reglages/sauvegarde/restaurer/', sy.sauvegarde_restaurer, name='sauvegarde_restaurer'),
    path('reglages/sauvegarde/restaurer/annuler/', sy.sauvegarde_annuler_restauration,
         name='sauvegarde_annuler_restauration'),
    path('reglages/sauvegarde/repartir-a-zero/', sy.repartir_a_zero, name='repartir_a_zero'),
    path('reglages/sauvegarde/<str:nom>/telecharger/', sy.sauvegarde_telecharger, name='sauvegarde_telecharger'),
    path('reglages/sauvegarde/<str:nom>/usb/', sy.sauvegarde_usb, name='sauvegarde_usb'),
    path('reglages/exemple/effacer/', sy.exemple_effacer, name='exemple_effacer'),
    path('reglages/telephones/', sy.telephones, name='reglages_telephones'),
    path('activation/', sy.activation, name='activation'),
    path('aide/', sy.aide, name='aide'),
    path('aide/mise-a-jour/verifier/', sy.mise_a_jour_verifier, name='mise_a_jour_verifier'),
    path('aide/dossier/', sy.aide_dossier, name='aide_dossier'),
    path('aide/rapport/', sy.aide_rapport, name='aide_rapport'),
]
