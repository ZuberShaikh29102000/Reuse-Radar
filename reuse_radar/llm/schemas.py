"""Extraction output schema: Pydantic models for validation plus the JSON Schema sent to providers.

The JSON Schema is written by hand rather than generated from Pydantic: Groq's strict mode
requires every property in `required` and `additionalProperties: false` on every object, and
both providers support only a subset of JSON Schema (no `title`, no `$defs`). A test asserts the
two definitions agree on field names and enum values.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

ProductType = Literal[
    "cross_section",
    "upper_limit",
    "efficiency_map",
    "likelihood",
    "covariance_matrix",
    "acceptance_table",
    "cutflow",
    "correlation_matrix",
    "other",
]
PRODUCT_TYPES: tuple[str, ...] = get_args(ProductType)


class ExtractedProduct(BaseModel):
    """One data product as returned by the model, before evidence verification."""

    model_config = ConfigDict(extra="forbid")

    product_type: ProductType
    description: str = Field(min_length=1, max_length=600)
    evidence_span: str = Field(min_length=1, max_length=2000)
    passage_id: int = Field(ge=1)
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    products: list[ExtractedProduct]


EXTRACTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_type": {
                        "type": "string",
                        "enum": list(PRODUCT_TYPES),
                        "description": "Kind of reusable data product.",
                    },
                    "description": {
                        "type": "string",
                        "description": "One sentence naming the product: quantity, process, "
                        "binning or variables, and confidence level where applicable.",
                    },
                    "evidence_span": {
                        "type": "string",
                        "description": "Text copied character-for-character from one passage, "
                        "including LaTeX commands, that shows the product exists.",
                    },
                    "passage_id": {
                        "type": "integer",
                        "description": "Number of the passage the evidence span is copied from.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "0 to 1: how sure you are this is a reusable data product.",
                    },
                },
                "required": [
                    "product_type",
                    "description",
                    "evidence_span",
                    "passage_id",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["products"],
    "additionalProperties": False,
}
