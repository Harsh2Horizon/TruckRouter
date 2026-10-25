from django.urls import path

from fuel_optimization import views as fuel_views
from geocoding import views

urlpatterns = [
    path("api/fuel/greedy/", fuel_views.greedy_view),
    path("api/fuel/dijkstra/", fuel_views.dijkstra_view),
    path("api/v1/addresses/parse", views.parse_view),
    path("api/v1/geocode", views.geocode_view),
    path("api/v1/geocode/runs/<uuid:run_id>", views.run_view),
]
