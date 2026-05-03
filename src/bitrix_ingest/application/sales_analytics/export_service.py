"""Export Bitrix sales analytics into the central database."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from typing import Any

from ..ports import BitrixGateway
from ...infrastructure.database.sales_analytics_repository import SalesAnalyticsRepository


DEAL_SELECT = [
    "ID",
    "TITLE",
    "STAGE_ID",
    "STAGE_SEMANTIC_ID",
    "CATEGORY_ID",
    "ASSIGNED_BY_ID",
    "OPPORTUNITY",
    "CURRENCY_ID",
    "CONTACT_ID",
    "COMPANY_ID",
    "TYPE_ID",
    "SOURCE_ID",
    "SOURCE_DESCRIPTION",
    "DATE_CREATE",
    "DATE_MODIFY",
    "BEGINDATE",
    "CLOSEDATE",
    "CLOSED",
]

USER_SELECT = [
    "ID",
    "NAME",
    "SECOND_NAME",
    "LAST_NAME",
    "ACTIVE",
    "WORK_POSITION",
    "UF_DEPARTMENT",
]

TASK_SELECT = [
    "ID",
    "TITLE",
    "STATUS",
    "DEADLINE",
    "CLOSED_DATE",
    "RESPONSIBLE_ID",
    "CREATED_BY",
    "CREATED_DATE",
    "CHANGED_DATE",
    "UF_CRM_TASK",
]

LEAD_SELECT = [
    "ID",
    "TITLE",
    "STATUS_ID",
    "STATUS_SEMANTIC_ID",
    "STATUS_DESCRIPTION",
    "ASSIGNED_BY_ID",
    "OPPORTUNITY",
    "CURRENCY_ID",
    "SOURCE_ID",
    "SOURCE_DESCRIPTION",
    "DATE_CREATE",
    "DATE_MODIFY",
    "DATE_CLOSED",
    "COMMENTS",
]

SALE_ORDER_SELECT = [
    "id",
    "accountNumber",
    "dateInsert",
    "dateUpdate",
    "statusId",
    "price",
    "currency",
    "payed",
    "sumPaid",
    "responsibleId",
    "orderTopic",
]

SALE_PAYMENT_SELECT = [
    "id",
    "orderId",
    "accountNumber",
    "paid",
    "datePaid",
    "dateBill",
    "sum",
    "currency",
    "responsibleId",
    "paySystemName",
]

INVOICE_SELECT = [
    "ID",
    "ACCOUNT_NUMBER",
    "ORDER_TOPIC",
    "DATE_INSERT",
    "DATE_UPDATE",
    "DATE_BILL",
    "STATUS_ID",
    "PRICE",
    "CURRENCY",
    "CURRENCY_ID",
    "RESPONSIBLE_ID",
]

CRM_BINDING = re.compile(r"^([A-Z]+)_(\d+)$")


@dataclass(frozen=True)
class ExportSalesAnalyticsRequest:
    tenant_id: str
    run_id: str
    date_from: str
    date_to: str
    category_ids: list[str] | None = None
    responsible_ids: list[str] | None = None
    include_tasks: bool = True
    include_leads: bool = True
    include_revenue: bool = True
    limit: int = 0


class ExportSalesAnalyticsService:
    def __init__(
        self,
        *,
        gateway: BitrixGateway,
        repository: SalesAnalyticsRepository,
    ) -> None:
        self._gateway = gateway
        self._repo = repository

    def execute(self, request: ExportSalesAnalyticsRequest) -> dict[str, Any]:
        period_start = _bound(request.date_from, end_of_day=False)
        period_end = _bound(request.date_to, end_of_day=True)

        users = self._load_users()
        departments = self._load_departments()
        categories = self._load_categories()
        stages = self._load_stages(categories)

        filters = self._scope_filter(request)
        active = self._list_deals(
            {**filters, "CLOSED": "N", "<=DATE_CREATE": period_end},
            limit=request.limit,
        )
        closed = self._list_deals(
            {**filters, "CLOSED": "Y", ">=CLOSEDATE": period_start, "<=CLOSEDATE": period_end},
            limit=request.limit,
        )
        created = self._list_deals(
            {**filters, ">=DATE_CREATE": period_start, "<=DATE_CREATE": period_end},
            limit=request.limit,
        )
        modified = self._list_deals(
            {**filters, ">=DATE_MODIFY": period_start, "<=DATE_MODIFY": period_end},
            limit=request.limit,
        )

        merged = _merge_deals(
            {
                "active_as_of_to": active,
                "closed_in_period": closed,
                "created_in_period": created,
                "modified_in_period": modified,
            }
        )
        normalized_deals = self._normalize_deals(
            merged,
            users=users,
            departments=departments,
            categories=categories,
            stages=stages,
        )
        active_deal_ids = {
            str(row["id"])
            for row in normalized_deals
            if int(row.get("active_as_of_to") or 0) == 1
        }

        tasks: list[dict[str, Any]] = []
        bindings: list[dict[str, Any]] = []
        if request.include_tasks:
            task_rows = self._load_crm_tasks()
            tasks, bindings = _normalize_tasks(task_rows, active_deal_ids=active_deal_ids)

        normalized_leads: list[dict[str, Any]] = []
        if request.include_leads:
            lead_rows = self._load_leads(request, period_start=period_start, period_end=period_end)
            lead_statuses = self._load_lead_statuses()
            normalized_leads = self._normalize_leads(
                lead_rows,
                users=users,
                statuses=lead_statuses,
            )

        revenue_errors: list[str] = []
        revenue_documents: list[dict[str, Any]] = []
        if request.include_revenue:
            revenue_documents = self._load_revenue_documents(
                request,
                period_start=period_start,
                period_end=period_end,
                errors=revenue_errors,
            )

        meta = {
            "date_from": request.date_from,
            "date_to": request.date_to,
            "period_start": period_start,
            "period_end": period_end,
            "deals_unique": len(normalized_deals),
            "active_as_of_to": len(active),
            "closed_in_period": len(closed),
            "created_in_period": len(created),
            "modified_in_period": len(modified),
            "users": len(users),
            "departments": len(departments),
            "categories": len(categories),
            "stages": len(stages),
            "tasks_exported": len(tasks),
            "task_bindings_exported": len(bindings),
            "leads_exported": len(normalized_leads),
            "revenue_documents_exported": len(revenue_documents),
            "revenue_errors": json.dumps(revenue_errors, ensure_ascii=False),
        }
        return self._repo.replace_snapshot(
            tenant_id=request.tenant_id,
            run_id=request.run_id,
            meta=meta,
            deals=normalized_deals,
            tasks=tasks,
            task_bindings=bindings,
            leads=normalized_leads,
            revenue_documents=revenue_documents,
        )

    def _scope_filter(self, request: ExportSalesAnalyticsRequest) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        categories = [str(v).strip() for v in (request.category_ids or []) if str(v).strip()]
        if categories:
            filters["CATEGORY_ID"] = categories if len(categories) > 1 else categories[0]
        responsible = [str(v).strip() for v in (request.responsible_ids or []) if str(v).strip()]
        if responsible:
            filters["ASSIGNED_BY_ID"] = responsible if len(responsible) > 1 else responsible[0]
        return filters

    def _list_deals(self, filters: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
        return self._gateway.list_all(
            "crm.deal.list",
            select=DEAL_SELECT,
            filter=filters,
            order={"ID": "ASC"},
            context="postgres sales analytics deals",
            limit=limit if limit > 0 else None,
        )

    def _load_leads(
        self,
        request: ExportSalesAnalyticsRequest,
        *,
        period_start: str,
        period_end: str,
    ) -> list[dict[str, Any]]:
        filters = self._lead_scope_filter(request)
        created = self._gateway.list_all(
            "crm.lead.list",
            select=LEAD_SELECT,
            filter={**filters, ">=DATE_CREATE": period_start, "<=DATE_CREATE": period_end},
            order={"ID": "ASC"},
            context="postgres sales analytics leads created",
            limit=request.limit if request.limit > 0 else None,
        )
        closed = self._gateway.list_all(
            "crm.lead.list",
            select=LEAD_SELECT,
            filter={**filters, ">=DATE_CLOSED": period_start, "<=DATE_CLOSED": period_end},
            order={"ID": "ASC"},
            context="postgres sales analytics leads closed",
            limit=request.limit if request.limit > 0 else None,
        )
        return _merge_records_by_id(created, closed)

    def _lead_scope_filter(self, request: ExportSalesAnalyticsRequest) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        responsible = [str(v).strip() for v in (request.responsible_ids or []) if str(v).strip()]
        if responsible:
            filters["ASSIGNED_BY_ID"] = responsible if len(responsible) > 1 else responsible[0]
        return filters

    def _load_lead_statuses(self) -> dict[str, dict[str, Any]]:
        statuses = self._status_list("STATUS")
        return {
            str(row.get("STATUS_ID") or row.get("ID") or ""): row
            for row in statuses
            if row.get("STATUS_ID") or row.get("ID")
        }

    def _load_revenue_documents(
        self,
        request: ExportSalesAnalyticsRequest,
        *,
        period_start: str,
        period_end: str,
        errors: list[str],
    ) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        order_filter: dict[str, Any] = {">=dateInsert": period_start, "<=dateInsert": period_end}
        payment_filter: dict[str, Any] = {">=dateBill": period_start, "<=dateBill": period_end}
        invoice_filter: dict[str, Any] = {">=DATE_INSERT": period_start, "<=DATE_INSERT": period_end}
        responsible = [str(v).strip() for v in (request.responsible_ids or []) if str(v).strip()]
        if responsible:
            order_filter["responsibleId"] = responsible if len(responsible) > 1 else responsible[0]
            payment_filter["responsibleId"] = responsible if len(responsible) > 1 else responsible[0]
            invoice_filter["RESPONSIBLE_ID"] = responsible if len(responsible) > 1 else responsible[0]

        documents.extend(
            _normalize_sale_orders(
                self._safe_list_all(
                    "sale.order.list",
                    select=SALE_ORDER_SELECT,
                    filter_=order_filter,
                    order={"id": "ASC"},
                    context="postgres revenue sale orders",
                    limit=request.limit,
                    errors=errors,
                )
            )
        )
        documents.extend(
            _normalize_sale_payments(
                self._safe_list_all(
                    "sale.payment.list",
                    select=SALE_PAYMENT_SELECT,
                    filter_=payment_filter,
                    order={"id": "ASC"},
                    context="postgres revenue sale payments",
                    limit=request.limit,
                    errors=errors,
                )
            )
        )
        documents.extend(
            _normalize_invoices(
                self._safe_list_all(
                    "crm.invoice.list",
                    select=INVOICE_SELECT,
                    filter_=invoice_filter,
                    order={"ID": "ASC"},
                    context="postgres revenue crm invoices",
                    limit=request.limit,
                    errors=errors,
                )
            )
        )
        return documents

    def _safe_list_all(
        self,
        method: str,
        *,
        select: list[str],
        filter_: dict[str, Any],
        order: dict[str, Any],
        context: str,
        limit: int,
        errors: list[str],
    ) -> list[dict[str, Any]]:
        try:
            return self._gateway.list_all(
                method,
                select=select,
                filter=filter_,
                order=order,
                context=context,
                limit=limit if limit > 0 else None,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{method}: {type(exc).__name__}: {exc}")
            return []

    def _load_users(self) -> dict[str, dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            response = self._gateway.call(
                "user.get",
                body={
                    "FILTER": {},
                    "SORT": "ID",
                    "ORDER": "ASC",
                    "start": start,
                },
                label=f"user.get sales analytics start={start}",
            )
            result = response.get("result") or []
            page_rows = result if isinstance(result, list) else [result]
            rows.extend(row for row in page_rows if isinstance(row, dict))
            next_start = response.get("next")
            if next_start is None:
                break
            start = int(next_start)
        return {str(row.get("ID") or ""): row for row in rows if row.get("ID")}

    def _load_departments(self) -> dict[str, dict[str, Any]]:
        response = self._gateway.call("department.get")
        result = response.get("result") or []
        rows = result if isinstance(result, list) else [result]
        return {str(row.get("ID") or ""): row for row in rows if isinstance(row, dict) and row.get("ID")}

    def _load_categories(self) -> dict[str, dict[str, Any]]:
        response = self._gateway.call("crm.dealcategory.list")
        result = response.get("result") or []
        rows = result if isinstance(result, list) else [result]
        categories = {
            str(row.get("ID") or "0"): row
            for row in rows
            if isinstance(row, dict) and row.get("ID") is not None
        }
        categories.setdefault("0", {"ID": "0", "NAME": "Default pipeline", "SORT": "0"})
        return categories

    def _load_stages(self, categories: dict[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        stages: dict[tuple[str, str], dict[str, Any]] = {}
        default_rows = self._status_list("DEAL_STAGE")
        for row in default_rows:
            status_id = str(row.get("STATUS_ID") or row.get("ID") or "")
            if status_id:
                stages[("0", status_id)] = row
        for category_id in categories:
            if category_id == "0":
                continue
            try:
                response = self._gateway.call("crm.dealcategory.stage.list", body={"id": category_id})
                result = response.get("result") or []
                rows = result if isinstance(result, list) else [result]
            except Exception:
                rows = self._status_list(f"C{category_id}:DEAL_STAGE")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                status_id = str(row.get("STATUS_ID") or row.get("ID") or "")
                if status_id:
                    stages[(category_id, status_id)] = row
        return stages

    def _status_list(self, entity_id: str) -> list[dict[str, Any]]:
        response = self._gateway.call(
            "crm.status.list",
            body={"filter": {"ENTITY_ID": entity_id}, "order": {"SORT": "ASC"}},
        )
        result = response.get("result") or []
        return [row for row in (result if isinstance(result, list) else [result]) if isinstance(row, dict)]

    def _load_crm_tasks(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            response = self._gateway.call(
                "tasks.task.list",
                body={
                    "order": {"ID": "ASC"},
                    "filter": {"!UF_CRM_TASK": False},
                    "select": TASK_SELECT,
                    "start": start,
                },
                label=f"tasks.task.list CRM linked start={start}",
            )
            result = response.get("result")
            page_rows = result.get("tasks") if isinstance(result, dict) else result
            if not isinstance(page_rows, list):
                break
            rows.extend(row for row in page_rows if isinstance(row, dict))
            next_start = response.get("next")
            if next_start is None:
                break
            start = int(next_start)
        return rows

    def _normalize_leads(
        self,
        leads: list[dict[str, Any]],
        *,
        users: dict[str, dict[str, Any]],
        statuses: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows = []
        for lead in leads:
            lead_id = _to_int(lead.get("ID"))
            if lead_id is None:
                continue
            manager_id = str(lead.get("ASSIGNED_BY_ID") or "")
            status_id = _clean(lead.get("STATUS_ID"))
            status = statuses.get(status_id) or {}
            rows.append(
                {
                    "id": lead_id,
                    "title": _clean(lead.get("TITLE")),
                    "status_id": status_id,
                    "status_name": _clean(status.get("NAME")) or status_id,
                    "status_semantic_id": _clean(lead.get("STATUS_SEMANTIC_ID")),
                    "status_description": _clean(lead.get("STATUS_DESCRIPTION")),
                    "assigned_by_id": _to_int(manager_id),
                    "manager_name": _user_name(users.get(manager_id) or {}, manager_id),
                    "opportunity": _money(lead.get("OPPORTUNITY")),
                    "currency_id": _clean(lead.get("CURRENCY_ID")),
                    "source_id": _clean(lead.get("SOURCE_ID")),
                    "source_description": _clean(lead.get("SOURCE_DESCRIPTION")),
                    "date_create": _clean(lead.get("DATE_CREATE")),
                    "date_modify": _clean(lead.get("DATE_MODIFY")),
                    "date_closed": _clean(lead.get("DATE_CLOSED")),
                    "comments": _clean(lead.get("COMMENTS")),
                    "raw": lead,
                }
            )
        return sorted(rows, key=lambda row: int(row["id"]))

    def _normalize_deals(
        self,
        deals: list[dict[str, Any]],
        *,
        users: dict[str, dict[str, Any]],
        departments: dict[str, dict[str, Any]],
        categories: dict[str, dict[str, Any]],
        stages: dict[tuple[str, str], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows = []
        for deal in deals:
            deal_id = _to_int(deal.get("ID"))
            if deal_id is None:
                continue
            manager_id = str(deal.get("ASSIGNED_BY_ID") or "")
            category_id = str(_to_int(deal.get("CATEGORY_ID")) or 0)
            stage_id = str(deal.get("STAGE_ID") or "")
            user = users.get(manager_id) or {}
            department = _primary_department(user, departments)
            category = categories.get(category_id) or {}
            stage = stages.get((category_id, stage_id)) or {}
            rows.append(
                {
                    "id": deal_id,
                    "title": _clean(deal.get("TITLE")),
                    "category_id": _to_int(category_id) or 0,
                    "pipeline_name": _clean(category.get("NAME")) or (
                        "Default pipeline" if category_id == "0" else f"Pipeline #{category_id}"
                    ),
                    "stage_id": stage_id,
                    "stage_name": _clean(stage.get("NAME")) or stage_id,
                    "stage_semantic_id": _clean(deal.get("STAGE_SEMANTIC_ID")),
                    "assigned_by_id": _to_int(manager_id),
                    "manager_name": _user_name(user, manager_id),
                    "department_name": _clean(department.get("NAME")) or "No department",
                    "opportunity": _money(deal.get("OPPORTUNITY")),
                    "currency_id": _clean(deal.get("CURRENCY_ID")),
                    "contact_id": _to_int(deal.get("CONTACT_ID")),
                    "company_id": _to_int(deal.get("COMPANY_ID")),
                    "type_id": _clean(deal.get("TYPE_ID")),
                    "source_id": _clean(deal.get("SOURCE_ID")),
                    "source_description": _clean(deal.get("SOURCE_DESCRIPTION")),
                    "date_create": _clean(deal.get("DATE_CREATE")),
                    "date_modify": _clean(deal.get("DATE_MODIFY")),
                    "begin_date": _clean(deal.get("BEGINDATE")),
                    "close_date": _clean(deal.get("CLOSEDATE")),
                    "closed": _clean(deal.get("CLOSED")),
                    "active_as_of_to": int(bool(deal.get("_active_as_of_to"))),
                    "closed_in_period": int(bool(deal.get("_closed_in_period"))),
                    "created_in_period": int(bool(deal.get("_created_in_period"))),
                    "modified_in_period": int(bool(deal.get("_modified_in_period"))),
                    "raw": deal,
                }
            )
        return sorted(rows, key=lambda row: int(row["id"]))


def _normalize_tasks(
    tasks: list[dict[str, Any]],
    *,
    active_deal_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    task_rows = []
    binding_rows = []
    seen_tasks: set[int] = set()
    for task in tasks:
        task_id = _to_int(_field(task, "id", "ID"))
        if task_id is None:
            continue
        crm_values = _crm_values(_field(task, "ufCrmTask", "UF_CRM_TASK"))
        deal_bindings = []
        for raw_value in crm_values:
            parsed = _parse_crm_binding(raw_value)
            if not parsed:
                continue
            entity_type, entity_id = parsed
            if entity_type != "D" or str(entity_id) not in active_deal_ids:
                continue
            deal_bindings.append((entity_type, entity_id, raw_value))
        if not deal_bindings:
            continue
        status = _to_int(_field(task, "status", "STATUS"))
        closed_date = _clean(_field(task, "closedDate", "CLOSED_DATE"))
        deadline = _clean(_field(task, "deadline", "DEADLINE"))
        deadline_utc = _to_utc_iso(deadline)
        closed_date_utc = _to_utc_iso(closed_date)
        is_completed = int(status == 5 or bool(closed_date_utc))
        is_overdue = int(
            not is_completed
            and deadline_utc is not None
            and deadline_utc < datetime.now(tz=timezone.utc).isoformat()
        )
        if task_id not in seen_tasks:
            seen_tasks.add(task_id)
            task_rows.append(
                {
                    "id": task_id,
                    "title": _clean(_field(task, "title", "TITLE")),
                    "status": status,
                    "deadline": deadline,
                    "deadline_utc": deadline_utc,
                    "closed_date": closed_date,
                    "closed_date_utc": closed_date_utc,
                    "responsible_id": _to_int(_field(task, "responsibleId", "RESPONSIBLE_ID")),
                    "created_by": _to_int(_field(task, "createdBy", "CREATED_BY")),
                    "created_date": _clean(_field(task, "createdDate", "CREATED_DATE")),
                    "changed_date": _clean(_field(task, "changedDate", "CHANGED_DATE")),
                    "is_completed": is_completed,
                    "is_overdue": is_overdue,
                    "raw": task,
                }
            )
        for entity_type, entity_id, raw_value in deal_bindings:
            binding_rows.append(
                {
                    "task_id": task_id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "raw_value": raw_value,
                }
            )
    return task_rows, binding_rows


def _normalize_sale_orders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docs = []
    for row in rows:
        document_id = _clean(_field(row, "id", "ID"))
        if not document_id:
            continue
        amount = _money(_field(row, "price", "PRICE"))
        paid = _clean(_field(row, "payed", "PAYED"))
        paid_amount = _money(_field(row, "sumPaid", "SUM_PAID"))
        if _is_paid(paid) and paid_amount == 0:
            paid_amount = amount
        docs.append(
            {
                "source": "sale_order",
                "document_id": document_id,
                "account_number": _clean(_field(row, "accountNumber", "ACCOUNT_NUMBER")),
                "order_id": document_id,
                "status_id": _clean(_field(row, "statusId", "STATUS_ID")),
                "paid": paid,
                "date_create": _clean(_field(row, "dateInsert", "DATE_INSERT")),
                "date_update": _clean(_field(row, "dateUpdate", "DATE_UPDATE")),
                "date_paid": "",
                "amount": amount,
                "paid_amount": paid_amount if _is_paid(paid) else 0.0,
                "currency_id": _clean(_field(row, "currency", "CURRENCY", "currencyId", "CURRENCY_ID")),
                "responsible_id": _to_int(_field(row, "responsibleId", "RESPONSIBLE_ID")),
                "raw": row,
            }
        )
    return docs


def _normalize_sale_payments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docs = []
    for row in rows:
        document_id = _clean(_field(row, "id", "ID"))
        if not document_id:
            continue
        paid = _clean(_field(row, "paid", "PAID"))
        amount = _money(_field(row, "sum", "SUM"))
        docs.append(
            {
                "source": "sale_payment",
                "document_id": document_id,
                "account_number": _clean(_field(row, "accountNumber", "ACCOUNT_NUMBER")),
                "order_id": _clean(_field(row, "orderId", "ORDER_ID")),
                "status_id": _clean(_field(row, "paySystemName", "PAY_SYSTEM_NAME")),
                "paid": paid,
                "date_create": _clean(_field(row, "dateBill", "DATE_BILL")),
                "date_update": "",
                "date_paid": _clean(_field(row, "datePaid", "DATE_PAID")),
                "amount": amount,
                "paid_amount": amount if _is_paid(paid) else 0.0,
                "currency_id": _clean(_field(row, "currency", "CURRENCY")),
                "responsible_id": _to_int(_field(row, "responsibleId", "RESPONSIBLE_ID")),
                "raw": row,
            }
        )
    return docs


def _normalize_invoices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docs = []
    for row in rows:
        document_id = _clean(_field(row, "ID", "id"))
        if not document_id:
            continue
        status_id = _clean(_field(row, "STATUS_ID", "statusId"))
        paid = "Y" if status_id.upper() in {"P", "PAID"} else ""
        amount = _money(_field(row, "PRICE", "price"))
        docs.append(
            {
                "source": "crm_invoice",
                "document_id": document_id,
                "account_number": _clean(_field(row, "ACCOUNT_NUMBER", "accountNumber")),
                "order_id": "",
                "status_id": status_id,
                "paid": paid,
                "date_create": _clean(_field(row, "DATE_INSERT", "dateInsert")),
                "date_update": _clean(_field(row, "DATE_UPDATE", "dateUpdate")),
                "date_paid": "",
                "amount": amount,
                "paid_amount": amount if _is_paid(paid) else 0.0,
                "currency_id": _clean(_field(row, "CURRENCY_ID", "CURRENCY", "currency")),
                "responsible_id": _to_int(_field(row, "RESPONSIBLE_ID", "responsibleId")),
                "raw": row,
            }
        )
    return docs


def _merge_deals(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for flag, rows in groups.items():
        for row in rows:
            deal_id = str(row.get("ID") or "").strip()
            if not deal_id:
                continue
            current = by_id.setdefault(deal_id, dict(row))
            current[f"_{flag}"] = True
            for key, value in row.items():
                current.setdefault(key, value)
    for row in by_id.values():
        for flag in groups:
            row.setdefault(f"_{flag}", False)
    return sorted(by_id.values(), key=lambda item: _to_int(item.get("ID")) or 0)


def _merge_records_by_id(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for rows in groups:
        for row in rows:
            record_id = str(row.get("ID") or row.get("id") or "").strip()
            if not record_id:
                continue
            current = by_id.setdefault(record_id, dict(row))
            for key, value in row.items():
                current.setdefault(key, value)
    return sorted(by_id.values(), key=lambda item: _to_int(item.get("ID") or item.get("id")) or 0)


def _primary_department(user: dict[str, Any], departments: dict[str, dict[str, Any]]) -> dict[str, Any]:
    raw = user.get("UF_DEPARTMENT")
    department_id = ""
    if isinstance(raw, list) and raw:
        department_id = str(raw[0])
    elif raw:
        department_id = str(raw)
    return departments.get(department_id) or {}


def _user_name(user: dict[str, Any], fallback: str) -> str:
    parts = [
        _clean(user.get("LAST_NAME")),
        _clean(user.get("NAME")),
    ]
    return " ".join(part for part in parts if part) or f"User #{fallback}"


def _bound(value: str, *, end_of_day: bool) -> str:
    text = str(value or "").strip()
    if "T" in text:
        return text
    suffix = dt_time.max if end_of_day else dt_time.min
    return datetime.combine(datetime.fromisoformat(text).date(), suffix).replace(microsecond=0).isoformat()


def _to_utc_iso(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _crm_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _parse_crm_binding(value: str) -> tuple[str, int] | None:
    match = CRM_BINDING.match(value.strip())
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _field(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _is_paid(value: Any) -> bool:
    return str(value or "").strip().lower() in {"y", "1", "true", "paid", "p"}


def _money(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int | None:
    try:
        text = str(value or "").strip()
        if not text:
            return None
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value).strip()
