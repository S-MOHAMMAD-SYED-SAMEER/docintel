"""Registry of document-type extractors.

A new document type is a new module plus one entry here. The pipeline resolves
an extractor by `doc_type` and never names a type itself.
"""

from app.extractors.base import ExtractedField, Extractor, Money, UnknownDocumentType
from app.extractors.invoice import Invoice, InvoiceExtractor, InvoiceLineItem
from app.extractors.purchase_order import (
    PurchaseOrder,
    PurchaseOrderExtractor,
    PurchaseOrderLineItem,
)

_EXTRACTORS: dict[str, Extractor] = {
    extractor.doc_type: extractor
    for extractor in (InvoiceExtractor(), PurchaseOrderExtractor())
}


def get_extractor(doc_type: str) -> Extractor:
    try:
        return _EXTRACTORS[doc_type]
    except KeyError:
        raise UnknownDocumentType(
            f"No extractor registered for doc_type {doc_type!r}. "
            f"Known types: {', '.join(sorted(_EXTRACTORS)) or 'none'}."
        ) from None


def registered_doc_types() -> tuple[str, ...]:
    return tuple(sorted(_EXTRACTORS))


__all__ = [
    "ExtractedField",
    "Extractor",
    "Invoice",
    "InvoiceExtractor",
    "InvoiceLineItem",
    "Money",
    "PurchaseOrder",
    "PurchaseOrderExtractor",
    "PurchaseOrderLineItem",
    "UnknownDocumentType",
    "get_extractor",
    "registered_doc_types",
]
