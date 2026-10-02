"""JSON shapes of the public API. Read-only; every value was computed by the offline pipeline."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from reuse_radar.api.models import DeclaredProduct, Gap, Paper, PublishedTable


class PaperSummarySerializer(serializers.ModelSerializer[Paper]):
    year = serializers.SerializerMethodField()
    inspire_url = serializers.SerializerMethodField()
    arxiv_url = serializers.SerializerMethodField()
    hepdata_url = serializers.SerializerMethodField()

    class Meta:
        model = Paper
        fields = [
            "inspire_id",
            "arxiv_id",
            "title",
            "collaboration",
            "earliest_date",
            "year",
            "readiness_score",
            "processing_status",
            "hepdata_record_id",
            "hepdata_version",
            "inspire_url",
            "arxiv_url",
            "hepdata_url",
        ]

    def get_year(self, paper: Paper) -> int:
        return paper.earliest_date.year

    def get_inspire_url(self, paper: Paper) -> str:
        return f"https://inspirehep.net/literature/{paper.inspire_id}"

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_arxiv_url(self, paper: Paper) -> str | None:
        return f"https://arxiv.org/abs/{paper.arxiv_id}" if paper.arxiv_id else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_hepdata_url(self, paper: Paper) -> str | None:
        if paper.hepdata_record_id is None:
            return None
        return f"https://www.hepdata.net/record/ins{paper.inspire_id}"


class TableSerializer(serializers.ModelSerializer[PublishedTable]):
    doi_url = serializers.SerializerMethodField()

    class Meta:
        model = PublishedTable
        fields = ["table_doi", "name", "description", "kind", "resource_type", "doi_url"]

    def get_doi_url(self, table: PublishedTable) -> str:
        return f"https://doi.org/{table.table_doi}"


class ProductSerializer(serializers.ModelSerializer[DeclaredProduct]):
    class Meta:
        model = DeclaredProduct
        fields = [
            "id",
            "product_type",
            "description",
            "evidence_span",
            "evidence_section",
            "evidence_kind",
            "confidence",
            "merged_duplicates",
            "extraction_version",
        ]


class GapFieldsMixin(serializers.Serializer[Gap]):
    matched_table = TableSerializer(allow_null=True)
    match = serializers.SerializerMethodField()

    @extend_schema_field(
        {
            "type": "object",
            "nullable": True,
            "properties": {
                "score": {"type": "number"},
                "embedding_similarity": {"type": "number"},
                "caption_overlap": {"type": "number"},
            },
        }
    )
    def get_match(self, gap: Gap) -> dict[str, Any] | None:
        if gap.match_score is None:
            return None
        return {
            "score": gap.match_score,
            "embedding_similarity": gap.embedding_similarity,
            "caption_overlap": gap.caption_overlap,
        }


class GapSerializer(GapFieldsMixin, serializers.ModelSerializer[Gap]):
    """One entry of the triage queue: a declared product, its paper and its HEPData status."""

    product = ProductSerializer(source="declared_product")
    paper = PaperSummarySerializer(source="declared_product.paper")

    class Meta:
        model = Gap
        fields = ["id", "status", "severity", "product", "paper", "matched_table", "match"]


class PaperGapSerializer(GapFieldsMixin, serializers.ModelSerializer[Gap]):
    product = ProductSerializer(source="declared_product")

    class Meta:
        model = Gap
        fields = ["id", "status", "severity", "product", "matched_table", "match"]


class PaperGapsSerializer(serializers.Serializer[Paper]):
    paper = PaperSummarySerializer(source="*")
    gaps = PaperGapSerializer(many=True)


class SimilarProductSerializer(serializers.ModelSerializer[DeclaredProduct]):
    distance = serializers.FloatField(help_text="Cosine distance; 0 = identical meaning.")
    paper = PaperSummarySerializer()
    gap_status = serializers.CharField(source="gap.status", allow_null=True)

    class Meta:
        model = DeclaredProduct
        fields = ["id", "product_type", "description", "distance", "gap_status", "paper"]


class SearchResultSerializer(serializers.ModelSerializer[DeclaredProduct]):
    rank = serializers.FloatField()
    paper = PaperSummarySerializer()
    gap_status = serializers.CharField(source="gap.status", allow_null=True)
    severity = serializers.IntegerField(source="gap.severity", allow_null=True)

    class Meta:
        model = DeclaredProduct
        fields = [
            "id",
            "product_type",
            "description",
            "evidence_span",
            "rank",
            "gap_status",
            "severity",
            "paper",
        ]
