"""Sales audit report assembly."""

from .frontend_adapters import build_frontend_sales_audit_data
from .report_builder import build_sales_audit_report

__all__ = ["build_frontend_sales_audit_data", "build_sales_audit_report"]
