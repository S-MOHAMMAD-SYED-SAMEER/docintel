"""Shared building blocks for document-type extractors.

An extractor is the only thing that changes between document types: the schema
to fill, the prompt that describes it, and a version string for that prompt.
The pipeline never learns about individual types.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ExtractedField[T](BaseModel):
    """One field the model was asked to find, with what it thinks of its answer.

    `confidence` is the model's *self-reported* confidence and nothing more. It
    is stored as given and deliberately not trusted on its own — milestone 5
    combines it with deterministic checks to produce the score that actually
    routes a field to review.
    """

    model_config = ConfigDict(extra="forbid")

    value: T | None = Field(description="The extracted value, or null if absent.")
    confidence: float = Field(
        ge=0.0, le=1.0, description="How sure the model is, from 0 to 1."
    )
    source_page: int | None = Field(
        default=None, ge=1, description="1-based page the value was read from."
    )


class Extractor(Protocol):
    """What the pipeline needs to know about a document type."""

    doc_type: str
    prompt_version: str
    schema: type[BaseModel]
    prompt: str


class UnknownDocumentType(Exception):
    """No extractor is registered for the document's doc_type."""
