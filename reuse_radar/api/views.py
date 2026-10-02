"""Read-only API views.

SPEC section 2, item 2: nothing here calls a model, LLM or embedding. Search uses Postgres
full-text search; "similar products" compares embeddings already stored by the pipeline
(pgvector cosine distance). A test enforces that the request path never imports the LLM router,
the pipeline or the embedding library.
"""

from __future__ import annotations

from typing import Any

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import connection
from django.db.models import Avg, Count, QuerySet
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from pgvector.django import CosineDistance
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from reuse_radar.api.auth import Curator, CuratorTokenAuthentication, IsCurator
from reuse_radar.api.models import (
    DeclaredProduct,
    Gap,
    GapStatus,
    Paper,
    ProcessingStatus,
    Review,
)
from reuse_radar.api.serializers import (
    GapSerializer,
    PaperGapSerializer,
    PaperGapsSerializer,
    PaperSummarySerializer,
    ReviewCreateSerializer,
    ReviewSerializer,
    SearchResultSerializer,
    SimilarProductSerializer,
)
from reuse_radar.llm.schemas import PRODUCT_TYPES

OPEN_STATUSES = (GapStatus.MISSING, GapStatus.UNCERTAIN, GapStatus.NO_RECORD)


# --- parameter parsing ------------------------------------------------------------------------


def _int(request: Request, name: str, low: int, high: int) -> int | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError({name: f"must be an integer between {low} and {high}"}) from None
    if not low <= value <= high:
        raise ValidationError({name: f"must be between {low} and {high}"})
    return value


def _choices(request: Request, name: str, allowed: tuple[str, ...]) -> list[str] | None:
    raw = request.query_params.get(name)
    if not raw:
        return None
    values = [v.strip() for v in raw.split(",") if v.strip()]
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise ValidationError({name: f"unknown value(s) {unknown}; allowed: {list(allowed)}"})
    return values


_GAP_STATUSES = tuple(GapStatus.values)
_PROCESSING = tuple(ProcessingStatus.values)

GAP_FILTERS = [
    OpenApiParameter(
        "status",
        str,
        description=f"Comma-separated gap statuses {list(_GAP_STATUSES)}. Default: open gaps "
        "(missing, uncertain, no_record).",
    ),
    OpenApiParameter("product_type", str, description=f"Comma-separated {list(PRODUCT_TYPES)}."),
    OpenApiParameter("year", int, description="Paper year (earliest date)."),
    OpenApiParameter("severity", int, description="Exact severity, 1 (low) to 3 (high)."),
    OpenApiParameter("min_severity", int, description="Minimum severity, 0 to 3."),
]


# --- triage queue -----------------------------------------------------------------------------


@extend_schema(
    summary="Triage queue of gaps",
    description="Declared data products and their HEPData status, most severe first.",
    parameters=GAP_FILTERS,
)
class GapListView(ListAPIView[Gap]):
    serializer_class = GapSerializer

    def get_queryset(self) -> QuerySet[Gap]:
        request = self.request
        statuses = _choices(request, "status", _GAP_STATUSES) or list(OPEN_STATUSES)
        queryset = Gap.objects.filter(
            status__in=statuses, declared_product__is_current=True
        ).select_related("declared_product__paper", "matched_table")
        queryset = queryset.prefetch_related("declared_product__reviews")
        if types := _choices(request, "product_type", PRODUCT_TYPES):
            queryset = queryset.filter(declared_product__product_type__in=types)
        if (year := _int(request, "year", 1900, 2100)) is not None:
            queryset = queryset.filter(declared_product__paper__earliest_date__year=year)
        if (severity := _int(request, "severity", 0, 3)) is not None:
            queryset = queryset.filter(severity=severity)
        if (min_severity := _int(request, "min_severity", 0, 3)) is not None:
            queryset = queryset.filter(severity__gte=min_severity)
        return queryset.order_by("-severity", "-declared_product__confidence", "id")


# --- papers -----------------------------------------------------------------------------------

_PAPER_ORDERING = {
    "readiness": ("readiness_score", "inspire_id"),
    "-readiness": ("-readiness_score", "inspire_id"),
    "date": ("earliest_date", "inspire_id"),
    "-date": ("-earliest_date", "inspire_id"),
}


@extend_schema(
    summary="Papers",
    parameters=[
        OpenApiParameter("year", int),
        OpenApiParameter(
            "processing_status", str, description=f"Comma-separated {list(_PROCESSING)}."
        ),
        OpenApiParameter(
            "has_hepdata", bool, description="Only papers with (or without) a record."
        ),
        OpenApiParameter("ordering", str, enum=list(_PAPER_ORDERING), description="Default -date."),
    ],
)
class PaperListView(ListAPIView[Paper]):
    serializer_class = PaperSummarySerializer

    def get_queryset(self) -> QuerySet[Paper]:
        request = self.request
        queryset = Paper.objects.all()
        if (year := _int(request, "year", 1900, 2100)) is not None:
            queryset = queryset.filter(earliest_date__year=year)
        if statuses := _choices(request, "processing_status", _PROCESSING):
            queryset = queryset.filter(processing_status__in=statuses)
        has_hepdata = request.query_params.get("has_hepdata")
        if has_hepdata is not None:
            if has_hepdata not in ("true", "false"):
                raise ValidationError({"has_hepdata": "must be true or false"})
            queryset = queryset.filter(hepdata_record_id__isnull=has_hepdata == "false")
        ordering = request.query_params.get("ordering", "-date")
        if ordering not in _PAPER_ORDERING:
            raise ValidationError({"ordering": f"one of {list(_PAPER_ORDERING)}"})
        return queryset.order_by(*_PAPER_ORDERING[ordering])


class PaperGapsView(APIView):
    @extend_schema(
        summary="One paper and all its declared products",
        parameters=[OpenApiParameter("status", str, description="Comma-separated gap statuses.")],
        responses=PaperGapsSerializer,
    )
    def get(self, request: Request, inspire_id: int) -> Response:
        paper = get_object_or_404(Paper, inspire_id=inspire_id)
        gaps = Gap.objects.filter(
            declared_product__paper=paper, declared_product__is_current=True
        ).select_related("declared_product", "matched_table")
        gaps = gaps.prefetch_related("declared_product__reviews")
        if statuses := _choices(request, "status", _GAP_STATUSES):
            gaps = gaps.filter(status__in=statuses)
        ordered = gaps.order_by("-severity", "-declared_product__confidence", "id")
        return Response(
            {
                "paper": PaperSummarySerializer(paper).data,
                "gaps": PaperGapSerializer(ordered, many=True).data,
            }
        )


# --- statistics -------------------------------------------------------------------------------


def _counts(queryset: QuerySet[Any], field: str) -> dict[str, int]:
    return {
        str(row[field]): row["n"]
        for row in queryset.values(field).annotate(n=Count("pk")).order_by(field)
    }


class StatsView(APIView):
    @extend_schema(
        summary="Corpus-wide statistics",
        responses=inline_serializer(
            "Stats",
            {
                "papers": serializers.DictField(),
                "products": serializers.DictField(),
                "gaps": serializers.DictField(),
            },
        ),
    )
    def get(self, request: Request) -> Response:
        papers = Paper.objects.all()
        products = DeclaredProduct.objects.filter(is_current=True)
        gaps = Gap.objects.filter(declared_product__is_current=True)
        return Response(
            {
                "papers": {
                    "total": papers.count(),
                    "by_processing_status": _counts(papers, "processing_status"),
                    "with_hepdata_record": papers.filter(hepdata_record_id__isnull=False).count(),
                    "mean_readiness_score": papers.aggregate(m=Avg("readiness_score"))["m"],
                },
                "products": {
                    "total": products.count(),
                    "by_type": _counts(products, "product_type"),
                },
                "gaps": {
                    "open": gaps.filter(status__in=OPEN_STATUSES).count(),
                    "by_status": _counts(gaps, "status"),
                    "by_severity": _counts(gaps.filter(status__in=OPEN_STATUSES), "severity"),
                },
            }
        )


# --- search -----------------------------------------------------------------------------------


@extend_schema(
    summary="Full-text search over declared products and paper titles",
    description="Postgres full-text search (web-search syntax: quotes, OR, -exclusion). "
    "No model is called.",
    parameters=[OpenApiParameter("q", str, required=True, description="2 to 200 characters.")],
)
class SearchView(ListAPIView[DeclaredProduct]):
    serializer_class = SearchResultSerializer

    def get_queryset(self) -> QuerySet[DeclaredProduct]:
        q = self.request.query_params.get("q", "").strip()
        if not 2 <= len(q) <= 200:
            raise ValidationError({"q": "must be 2 to 200 characters"})
        vector = (
            SearchVector("description", weight="A", config="english")
            + SearchVector("paper__title", weight="B", config="english")
            + SearchVector("evidence_span", weight="C", config="english")
        )
        query = SearchQuery(q, search_type="websearch", config="english")
        return (
            DeclaredProduct.objects.filter(is_current=True)
            # Filter on the match itself (tsvector @@ tsquery): ts_rank gives non-matching rows a
            # tiny positive rank, so "rank > 0" returned every product (seen in a smoke test).
            .annotate(document=vector, rank=SearchRank(vector, query))
            .filter(document=query)
            .select_related("paper", "gap")
            .order_by("-rank", "id")
        )


class SimilarProductsView(APIView):
    @extend_schema(
        summary="Products with the most similar meaning (stored embeddings, pgvector)",
        parameters=[OpenApiParameter("limit", int, description="1 to 50, default 10.")],
        responses=SimilarProductSerializer(many=True),
    )
    def get(self, request: Request, pk: int) -> Response:
        product = get_object_or_404(DeclaredProduct, pk=pk, is_current=True)
        limit = _int(request, "limit", 1, 50) or 10
        similar = (
            DeclaredProduct.objects.filter(is_current=True)
            .exclude(pk=product.pk)
            .annotate(distance=CosineDistance("embedding", product.embedding))
            .select_related("paper", "gap")
            .order_by("distance")[:limit]
        )
        return Response(SimilarProductSerializer(similar, many=True).data)


# --- curator reviews (the one write endpoint) ------------------------------------------------


class ReviewCreateView(APIView):
    """Record a curator's verdict on a declared product. Requires a curator token."""

    authentication_classes = [CuratorTokenAuthentication]
    permission_classes = [IsCurator]
    throttle_scope = "reviews"
    throttle_classes = [ScopedRateThrottle]

    @extend_schema(
        summary="Accept or reject a declared product (curators only)",
        request=ReviewCreateSerializer,
        responses={201: ReviewSerializer},
    )
    def post(self, request: Request) -> Response:
        payload = ReviewCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        product = get_object_or_404(
            DeclaredProduct, pk=payload.validated_data["product_id"], is_current=True
        )
        assert isinstance(request.user, Curator)
        review = Review.objects.create(
            declared_product=product,
            verdict=payload.validated_data["verdict"],
            reviewer=request.user.name,
            note=payload.validated_data["note"],
        )
        return Response(ReviewSerializer(review).data, status=status.HTTP_201_CREATED)


class ProductReviewsView(APIView):
    @extend_schema(
        summary="Review history of a declared product",
        responses=ReviewSerializer(many=True),
    )
    def get(self, request: Request, pk: int) -> Response:
        product = get_object_or_404(DeclaredProduct, pk=pk)
        reviews = product.reviews.order_by("-created_at")
        return Response(ReviewSerializer(reviews, many=True).data)


# --- health -----------------------------------------------------------------------------------


@extend_schema(exclude=True)
def health(request: HttpRequest) -> JsonResponse:
    """Liveness and database check for Render."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})
