from __future__ import annotations

import datetime as dt

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings

from reuse_radar.api.auth import configured_tokens
from reuse_radar.api.models import DeclaredProduct, Gap, Paper, Review

pytestmark = pytest.mark.django_db
API = override_settings(ALLOWED_HOSTS=["testserver"], SECURE_SSL_REDIRECT=False)
ALICE = "alice-token-0123456789abcdef"
BOB = "bob-token-0123456789abcdefgh"


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEWER_TOKENS", f"alice:{ALICE},bob:{BOB}")


@pytest.fixture
def product() -> DeclaredProduct:
    paper = Paper.objects.create(
        inspire_id=1, title="P", earliest_date=dt.date(2024, 1, 1), processing_status="reconciled"
    )
    prod = DeclaredProduct.objects.create(
        paper=paper,
        product_type="likelihood",
        description="Full likelihood",
        evidence_span="Full likelihoods are published.",
        evidence_section="Results",
        confidence=0.9,
        embedding=[1.0] + [0.0] * 383,
        fingerprint="fp1",
        extraction_version="v1",
    )
    Gap.objects.create(declared_product=prod, status="missing", severity=3, reconcile_version="1")
    return prod


def post(body: dict[str, object], token: str | None = None) -> tuple[int, dict[str, object]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with API:
        response = Client().post(
            "/api/reviews", body, content_type="application/json", headers=headers
        )
    return response.status_code, response.json()


def test_curator_can_accept_and_name_is_recorded(product: DeclaredProduct) -> None:
    code, data = post({"product_id": product.pk, "verdict": "accept", "note": "checked"}, ALICE)
    assert code == 201
    assert data["reviewer"] == "alice" and data["verdict"] == "accept"
    assert Review.objects.get().reviewer == "alice"


def test_reviewer_name_comes_from_the_token_not_the_body(product: DeclaredProduct) -> None:
    code, data = post({"product_id": product.pk, "verdict": "reject", "reviewer": "mallory"}, BOB)
    assert code == 201 and data["reviewer"] == "bob"


@pytest.mark.parametrize("token", [None, "not-a-real-token-but-long-enough"])
def test_missing_or_unknown_token_is_rejected(product: DeclaredProduct, token: str | None) -> None:
    code, _ = post({"product_id": product.pk, "verdict": "accept"}, token)
    assert code in (401, 403)
    assert Review.objects.count() == 0


def test_invalid_payloads_are_400(product: DeclaredProduct) -> None:
    assert post({"product_id": product.pk, "verdict": "maybe"}, ALICE)[0] == 400
    assert post({"verdict": "accept"}, ALICE)[0] == 400
    assert (
        post({"product_id": product.pk, "verdict": "accept", "note": "x" * 2001}, ALICE)[0] == 400
    )


def test_superseded_or_unknown_products_are_404(product: DeclaredProduct) -> None:
    assert post({"product_id": 999999, "verdict": "accept"}, ALICE)[0] == 404
    product.is_current = False
    product.save()
    assert post({"product_id": product.pk, "verdict": "accept"}, ALICE)[0] == 404


def test_history_and_latest_review_are_visible_publicly(product: DeclaredProduct) -> None:
    post({"product_id": product.pk, "verdict": "reject"}, ALICE)
    post({"product_id": product.pk, "verdict": "accept"}, BOB)
    with API:
        history = Client().get(f"/api/products/{product.pk}/reviews").json()
        gaps = Client().get("/api/gaps").json()
    assert [r["verdict"] for r in history] == ["accept", "reject"]
    latest = gaps["results"][0]["product"]["latest_review"]
    assert latest["verdict"] == "accept" and latest["reviewer"] == "bob"


def test_read_endpoints_still_need_no_token(product: DeclaredProduct) -> None:
    with API:
        assert Client().get("/api/gaps").status_code == 200


def test_weak_tokens_fail_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEWER_TOKENS", "carol:short")
    with pytest.raises(ImproperlyConfigured, match="at least"):
        configured_tokens()


def test_no_tokens_configured_means_nobody_can_write(
    product: DeclaredProduct, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEWER_TOKENS", "")
    assert post({"product_id": product.pk, "verdict": "accept"}, ALICE)[0] in (401, 403)
