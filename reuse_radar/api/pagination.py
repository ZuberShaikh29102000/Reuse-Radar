"""Default pagination. Kept out of views.py: DRF imports it while building its generic views,
so defining it there causes a circular import."""

from rest_framework.pagination import PageNumberPagination


class Pagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
