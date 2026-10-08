from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

admin.site.site_header = 'Magasin SI BÉTON'
admin.site.site_title = 'Magasin SI BÉTON'
admin.site.index_title = 'Administration du magasin'

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('stock.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
