"""URL routes of the public read-only API."""

from django.urls import URLPattern, URLResolver, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from reuse_radar.api import views

urlpatterns: list[URLPattern | URLResolver] = [
    path("api/gaps", views.GapListView.as_view(), name="gap-list"),
    path("api/papers", views.PaperListView.as_view(), name="paper-list"),
    path("api/papers/<int:inspire_id>/gaps", views.PaperGapsView.as_view(), name="paper-gaps"),
    path("api/stats", views.StatsView.as_view(), name="stats"),
    path("api/search", views.SearchView.as_view(), name="search"),
    path(
        "api/products/<int:pk>/similar",
        views.SimilarProductsView.as_view(),
        name="product-similar",
    ),
    path("api/reviews", views.ReviewCreateView.as_view(), name="review-create"),
    path(
        "api/products/<int:pk>/reviews",
        views.ProductReviewsView.as_view(),
        name="product-reviews",
    ),
    path("api/schema", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    path("healthz", views.health, name="health"),
]
