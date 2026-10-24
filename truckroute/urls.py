from django.urls import path

from geocoding import views

urlpatterns = [
    path("api/v1/addresses/parse", views.parse_view),
    path("api/v1/geocode", views.geocode_view),
    path("api/v1/geocode/runs/<uuid:run_id>", views.run_view),
]
