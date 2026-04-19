"""Call-records scan (Bitrix activities → VoxImplant enrichment)."""
from .coercion import coerce_int, coerce_str
from .scan_service import CallRecordsScanRequest, CallRecordsScanService

__all__ = [
    "CallRecordsScanRequest",
    "CallRecordsScanService",
    "coerce_int",
    "coerce_str",
]
