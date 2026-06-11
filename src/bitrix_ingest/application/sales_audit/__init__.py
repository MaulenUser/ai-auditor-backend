"""Sales audit report assembly."""

from .frontend_adapters import (
    build_frontend_sales_audit_data,
    enrich_frontend_deal_urls,
    enrich_frontend_manager_names,
)
from .report_builder import build_sales_audit_report

__all__ = [
    "build_frontend_sales_audit_data",
    "build_sales_audit_report",
    "enrich_frontend_deal_urls",
    "enrich_frontend_manager_names",
]
