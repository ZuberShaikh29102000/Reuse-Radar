"""Data model (SPEC section 5), filled by the offline pipeline's store stage.

The request path only reads these tables. Indexes follow the triage-queue query: open gaps (not
"published"), ordered by severity then confidence, filtered by product type and paper year; see
docs/adr/0004 for the query plan and the columns added beyond the SPEC sketch.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from pgvector.django import HnswIndex, VectorField

from reuse_radar.llm.schemas import PRODUCT_TYPES

EMBEDDING_DIM = 384


class ProcessingStatus(models.TextChoices):
    """How far the pipeline got with a paper; makes papers that cannot be processed visible."""

    HARVESTED = "harvested"
    NO_ARXIV_ID = "no_arxiv_id"
    NO_LATEX = "no_latex"
    SOURCE_ERROR = "source_error"
    FILTER_ERROR = "filter_error"
    EXTRACTION_ERROR = "extraction_error"
    EXTRACTED = "extracted"
    RECONCILE_ERROR = "reconcile_error"
    RECONCILED = "reconciled"


class Paper(models.Model):
    inspire_id = models.BigIntegerField(primary_key=True)
    arxiv_id = models.CharField(max_length=32, null=True, blank=True)
    title = models.TextField()
    collaboration = models.CharField(max_length=200, blank=True)
    earliest_date = models.DateField()
    hepdata_record_id = models.IntegerField(null=True, blank=True)
    hepdata_record_doi = models.CharField(max_length=100, blank=True)
    hepdata_version = models.IntegerField(null=True, blank=True)
    readiness_score = models.FloatField(null=True, blank=True)
    extraction_version = models.CharField(max_length=64, blank=True)
    last_extracted_at = models.DateTimeField(null=True, blank=True)
    processing_status = models.CharField(
        max_length=32, choices=ProcessingStatus.choices, default=ProcessingStatus.HARVESTED
    )

    class Meta:
        indexes = [
            models.Index(fields=["earliest_date"], name="paper_date_idx"),
            models.Index(fields=["readiness_score"], name="paper_readiness_idx"),
            models.Index(fields=["hepdata_record_id"], name="paper_hepdata_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.inspire_id}: {self.title[:60]}"


class DeclaredProduct(models.Model):
    paper = models.ForeignKey(
        Paper, on_delete=models.CASCADE, related_name="products", db_column="inspire_id"
    )
    product_type = models.CharField(max_length=32, choices=[(t, t) for t in PRODUCT_TYPES])
    description = models.TextField()
    evidence_span = models.TextField()
    evidence_section = models.TextField()
    evidence_kind = models.CharField(max_length=32, blank=True)
    confidence = models.FloatField()
    embedding = VectorField(dimensions=EMBEDDING_DIM)
    # Stable identity across pipeline reruns: sha256(product_type, evidence_span, description).
    # Lets reloads update rows in place so reviews (the gold set) keep pointing at them.
    fingerprint = models.CharField(max_length=64)
    # False once a newer extraction no longer produces this product. Rows with reviews are kept
    # (never deleted) so the evaluation gold set survives prompt changes.
    is_current = models.BooleanField(default=True)
    extraction_version = models.CharField(max_length=64)
    provider = models.CharField(max_length=32, blank=True)
    model = models.CharField(max_length=64, blank=True)
    merged_duplicates = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["paper", "fingerprint"], name="product_fingerprint_uq"),
            models.CheckConstraint(
                condition=Q(confidence__gte=0) & Q(confidence__lte=1), name="product_conf_range"
            ),
        ]
        indexes = [
            models.Index(fields=["product_type"], name="product_type_idx"),
            HnswIndex(
                name="product_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]


class PartKind(models.TextChoices):
    TABLE = "table"
    RESOURCE = "resource"


class PublishedTable(models.Model):
    hepdata_record_id = models.IntegerField()
    table_doi = models.CharField(max_length=120, unique=True)
    name = models.TextField()
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=16, choices=PartKind.choices, default=PartKind.TABLE)
    resource_type = models.CharField(max_length=64, blank=True)
    embedding = VectorField(dimensions=EMBEDDING_DIM)

    class Meta:
        indexes = [
            models.Index(fields=["hepdata_record_id"], name="table_record_idx"),
            HnswIndex(
                name="table_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]


class GapStatus(models.TextChoices):
    PUBLISHED = "published"  # found on HEPData: not a gap, kept for coverage statistics
    UNCERTAIN = "uncertain"  # a plausible match exists; a curator should decide
    MISSING = "missing"  # the paper has a HEPData record, but not this product
    NO_RECORD = "no_record"  # the paper has no HEPData record at all


class Gap(models.Model):
    declared_product = models.OneToOneField(
        DeclaredProduct, on_delete=models.CASCADE, related_name="gap"
    )
    status = models.CharField(max_length=16, choices=GapStatus.choices)
    severity = models.SmallIntegerField()  # 0 = not a gap, 3 = most urgent
    matched_table = models.ForeignKey(
        PublishedTable, on_delete=models.SET_NULL, null=True, blank=True, related_name="gaps"
    )
    match_score = models.FloatField(null=True, blank=True)
    embedding_similarity = models.FloatField(null=True, blank=True)
    caption_overlap = models.FloatField(null=True, blank=True)
    reconcile_version = models.CharField(max_length=16)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(severity__gte=0) & Q(severity__lte=3), name="gap_severity_range"
            ),
        ]
        indexes = [
            # The triage queue: open gaps, most severe first. Partial, so the "published" rows
            # (not gaps) never bloat it.
            models.Index(
                fields=["-severity", "status"],
                name="gap_triage_idx",
                condition=~Q(status="published"),
            ),
            models.Index(fields=["status"], name="gap_status_idx"),
        ]


class Verdict(models.TextChoices):
    ACCEPT = "accept"  # the product is real and the gap status is right
    REJECT = "reject"  # not a real product, or wrongly classified


class Review(models.Model):
    # PROTECT: the database refuses to delete a reviewed product. Reviews are the gold set.
    declared_product = models.ForeignKey(
        DeclaredProduct, on_delete=models.PROTECT, related_name="reviews"
    )
    verdict = models.CharField(max_length=16, choices=Verdict.choices)
    reviewer = models.CharField(max_length=150)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["declared_product", "-created_at"], name="review_product_idx"),
        ]
