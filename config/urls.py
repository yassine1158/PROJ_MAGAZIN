from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

from stock.views_magasin import Connexion

admin.site.site_header = 'Magasin SI BÉTON'
admin.site.site_title = 'Magasin SI BÉTON'
admin.site.index_title = 'Administration du magasin'

urlpatterns = [
    path('admin/login/', Connexion.as_view()),  # même page de connexion partout
    path('admin/', admin.site.urls),
    path('', include('stock.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.BUREAU:  # photos des articles, servies par le programme lui-même
    urlpatterns.append(re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}))
