"""Build an executive sales dashboard from Bitrix CRM and sales-quality outputs."""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..date_range import within_any_record_datetime_range
from ..ports import BitrixGateway, JsonSink

logger = logging.getLogger(__name__)

_DEAL_SELECT = [
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
    "DATE_CREATE",
    "DATE_MODIFY",
    "CLOSEDATE",
    "CLOSED",
    "COMMENTS",
    "LAST_ACTIVITY_TIME",
    "LAST_COMMUNICATION_TIME",
]
_USER_SELECT = ["ID", "NAME", "LAST_NAME", "EMAIL", "ACTIVE"]
_CONTACT_SELECT = ["ID", "NAME", "LAST_NAME", "PHONE", "EMAIL", "ASSIGNED_BY_ID"]
_TASK_ACTIVITY_SELECT = [
    "ID",
    "SUBJECT",
    "OWNER_ID",
    "OWNER_TYPE_ID",
    "RESPONSIBLE_ID",
    "DEADLINE",
    "COMPLETED",
    "STATUS",
    "PROVIDER_ID",
    "PROVIDER_TYPE_ID",
    "LAST_UPDATED",
]

_QUESTION_STATUS = {
    "ready": "ready",
    "formula_required": "formula_required",
    "needs_input": "needs_input",
}


@dataclass(frozen=True)
class BuildExecutiveReportRequest:
    output_dir: Path
    sales_quality_dir: Path = Path("export/sales-quality")
    scope: str = "analyzed"  # analyzed | bitrix
    date_from: str | None = None
    date_to: str | None = None
    category_ids: list[str] | None = None
    responsible_id: str | None = None
    responsible_ids: list[str] | None = None
    deal_ids: list[str] | None = None
    limit: int = 0
    average_ticket_kzt: float | None = None
    expected_conversion_pct: float | None = None
    rating_formula: str = "default"
    portal_base_url: str = "https://sapaplast.bitrix24.kz"
    max_reanimation_cards: int = 100


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _money(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct(count: int, total: int) -> float:
    return round(count / total * 100, 1) if total else 0.0


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _manager_name(row: dict[str, Any] | None, manager_id: str) -> str:
    if not row:
        return manager_id
    parts = [
        str(row.get("NAME") or "").strip(),
        str(row.get("LAST_NAME") or "").strip(),
    ]
    return " ".join(part for part in parts if part) or manager_id


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _responsible_ids(request: BuildExecutiveReportRequest) -> list[str]:
    ids = [str(v).strip() for v in (request.responsible_ids or []) if str(v).strip()]
    if not ids and request.responsible_id:
        ids = [str(request.responsible_id).strip()]
    return ids


class BuildExecutiveReportService:
    def __init__(self, gateway: BitrixGateway, sink: JsonSink) -> None:
        self._gateway = gateway
        self._sink = sink

    def execute(self, request: BuildExecutiveReportRequest) -> None:
        output_dir = request.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        sales_quality_report = self._load_sales_quality_report(request.sales_quality_dir)
        features = self._load_sales_quality_features(request.sales_quality_dir)
        feature_deal_ids = self._collect_feature_deal_ids(features)
        explicit_deal_ids = [str(v) for v in (request.deal_ids or []) if str(v).strip()]
        if request.scope == "analyzed":
            deal_ids = explicit_deal_ids or feature_deal_ids
        else:
            deal_ids = explicit_deal_ids

        users = self._load_users()
        deals = self._load_deals(request, deal_ids=deal_ids)
        deals_by_id = {str(deal.get("ID") or ""): deal for deal in deals}
        stage_names = self._load_stage_names(deals)
        contacts = self._load_contacts(deals)
        task_activities = self._load_task_activities(deals_by_id.keys())

        dashboard = self._build_deal_dashboard(deals, users)
        task_status = self._build_task_status(deals, task_activities, users)
        stage_compliance = self._build_stage_compliance(sales_quality_report)
        response_speed = self._build_response_speed(sales_quality_report)
        failure_reasons = self._build_failure_reasons(deals, features, users)
        reanimation = self._build_reanimation_cards(
            deals,
            features,
            users,
            contacts,
            request.portal_base_url,
            max_cards=request.max_reanimation_cards,
        )
        lost_revenue = self._build_lost_revenue(
            dashboard,
            average_ticket_kzt=request.average_ticket_kzt,
            expected_conversion_pct=request.expected_conversion_pct,
        )
        top_problems = self._top_problems(sales_quality_report)
        top_growth = self._top_growth(top_problems)
        rating = self._build_integral_rating(
            dashboard=dashboard,
            task_status=task_status,
            stage_compliance=stage_compliance,
            response_speed=response_speed,
            formula=request.rating_formula,
        )

        report = {
            "generated_at": _now().isoformat(),
            "scope": {
                "mode": request.scope,
                "date_from": request.date_from,
                "date_to": request.date_to,
                "category_ids": request.category_ids or [],
                "responsible_id": request.responsible_id or "",
                "responsible_ids": _responsible_ids(request),
                "requested_deal_ids_count": len(deal_ids),
                "sales_quality_deal_ids_count": len(feature_deal_ids),
                "deals_loaded": len(deals),
                "sales_quality_features": len(features),
            },
            "data_readiness": self._build_data_readiness(
                lost_revenue=lost_revenue,
                rating=rating,
            ),
            "integral_rating": rating,
            "deal_dashboard": dashboard,
            "task_status": task_status,
            "sales_stage_compliance": stage_compliance,
            "lead_response_speed": response_speed,
            "failure_reasons": failure_reasons,
            "failed_deal_reanimation": reanimation,
            "lost_revenue": lost_revenue,
            "top_3_problems": top_problems[:3],
            "top_3_growth_opportunities": top_growth[:3],
            "references": {
                "stage_names": stage_names,
                "manager_names": {
                    manager_id: _manager_name(row, manager_id)
                    for manager_id, row in sorted(users.items())
                },
            },
        }

        self._sink.write(output_dir / "executive-report.json", report)
        (output_dir / "executive-report.md").write_text(
            self._markdown(report),
            encoding="utf-8",
        )
        logger.info("Executive report saved to %s", output_dir.resolve())

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_sales_quality_report(self, sales_quality_dir: Path) -> dict[str, Any]:
        path = sales_quality_dir / "report.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_sales_quality_features(self, sales_quality_dir: Path) -> list[dict[str, Any]]:
        features_dir = sales_quality_dir / "features"
        if not features_dir.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(features_dir.glob("*.json")):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping invalid sales-quality feature %s: %s", path, exc)
        return rows

    def _collect_feature_deal_ids(self, features: list[dict[str, Any]]) -> list[str]:
        ids: list[str] = []
        for feature in features:
            source = feature.get("source") or {}
            deal_id = str(source.get("deal_id") or "").strip()
            if deal_id:
                ids.append(deal_id)
        return sorted(set(ids), key=lambda v: int(v) if v.isdigit() else v)

    def _load_users(self) -> dict[str, dict[str, Any]]:
        response = self._gateway.call("user.get", body={"filter": {"ACTIVE": True}})
        result = response.get("result") or []
        rows = result if isinstance(result, list) else [result]
        return {str(row.get("ID") or ""): row for row in rows if row.get("ID")}

    def _load_deals(
        self,
        request: BuildExecutiveReportRequest,
        *,
        deal_ids: list[str],
    ) -> list[dict[str, Any]]:
        if request.scope == "analyzed":
            if not deal_ids:
                return []
            rows: list[dict[str, Any]] = []
            for chunk in _chunked(deal_ids, 50):
                rows.extend(
                    self._gateway.list_all(
                        "crm.deal.list",
                        select=_DEAL_SELECT,
                        filter={"ID": chunk},
                        order={"ID": "ASC"},
                        context="executive report analyzed deals",
                    )
                )
            return self._filter_deals(rows, request)

        deal_filter: dict[str, Any] = {}
        clean_categories = [str(v) for v in (request.category_ids or []) if str(v).strip()]
        if clean_categories:
            deal_filter["CATEGORY_ID"] = (
                clean_categories if len(clean_categories) > 1 else clean_categories[0]
            )
        responsible_ids = _responsible_ids(request)
        if responsible_ids:
            deal_filter["ASSIGNED_BY_ID"] = (
                responsible_ids if len(responsible_ids) > 1 else responsible_ids[0]
            )

        rows = self._gateway.list_all(
            "crm.deal.list",
            select=_DEAL_SELECT,
            filter=deal_filter,
            order={"DATE_MODIFY": "DESC"},
            context="executive report bitrix deals",
            limit=request.limit if request.limit > 0 else None,
        )
        rows = self._filter_deals(rows, request)
        if explicit_allow := set(deal_ids):
            rows = [row for row in rows if str(row.get("ID") or "") in explicit_allow]
        return rows[: request.limit] if request.limit > 0 else rows

    def _filter_deals(
        self,
        deals: list[dict[str, Any]],
        request: BuildExecutiveReportRequest,
    ) -> list[dict[str, Any]]:
        rows = deals
        if request.date_from or request.date_to:
            rows = [
                deal
                for deal in rows
                if within_any_record_datetime_range(
                    deal,
                    fields=("DATE_CREATE", "DATE_MODIFY", "CLOSEDATE"),
                    date_from=request.date_from,
                    date_to=request.date_to,
                )
            ]
        responsible_ids = _responsible_ids(request)
        if responsible_ids:
            allow_responsible = set(responsible_ids)
            rows = [
                deal
                for deal in rows
                if str(deal.get("ASSIGNED_BY_ID") or "") in allow_responsible
            ]
        clean_categories = [str(v) for v in (request.category_ids or []) if str(v).strip()]
        if clean_categories:
            allow = set(clean_categories)
            rows = [deal for deal in rows if str(deal.get("CATEGORY_ID") or "") in allow]
        return rows

    def _load_stage_names(self, deals: list[dict[str, Any]]) -> dict[str, str]:
        category_ids = sorted({str(deal.get("CATEGORY_ID") or "0") for deal in deals})
        entity_ids = ["DEAL_STAGE" if cid == "0" else f"C{cid}:DEAL_STAGE" for cid in category_ids]
        if not entity_ids:
            entity_ids = ["DEAL_STAGE"]

        names: dict[str, str] = {}
        for entity_id in entity_ids:
            rows = self._gateway.list_all(
                "crm.status.list",
                select=["STATUS_ID", "NAME", "ENTITY_ID"],
                filter={"ENTITY_ID": entity_id},
                order={"SORT": "ASC"},
                context=f"executive report stages {entity_id}",
            )
            for row in rows:
                status_id = str(row.get("STATUS_ID") or "")
                if status_id:
                    names[status_id] = str(row.get("NAME") or status_id)
        return names

    def _load_contacts(self, deals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        contact_ids = sorted(
            {
                str(deal.get("CONTACT_ID") or "")
                for deal in deals
                if str(deal.get("CONTACT_ID") or "").strip()
                and str(deal.get("CONTACT_ID") or "") != "0"
            }
        )
        contacts: dict[str, dict[str, Any]] = {}
        for chunk in _chunked(contact_ids, 50):
            rows = self._gateway.list_all(
                "crm.contact.list",
                select=_CONTACT_SELECT,
                filter={"ID": chunk},
                order={"ID": "ASC"},
                context="executive report contacts",
            )
            contacts.update({str(row.get("ID") or ""): row for row in rows if row.get("ID")})
        return contacts

    def _load_task_activities(self, deal_ids: Any) -> dict[str, list[dict[str, Any]]]:
        tasks: dict[str, list[dict[str, Any]]] = defaultdict(list)
        clean_ids = sorted(
            {str(v) for v in deal_ids if str(v).strip()},
            key=lambda v: int(v) if v.isdigit() else v,
        )
        for chunk in _chunked(clean_ids, 50):
            rows = self._gateway.list_all(
                "crm.activity.list",
                select=_TASK_ACTIVITY_SELECT,
                filter={
                    "OWNER_TYPE_ID": 2,
                    "OWNER_ID": chunk,
                    "PROVIDER_ID": "CRM_TASKS_TASK",
                },
                order={"DEADLINE": "ASC"},
                context="executive report task activities",
            )
            for row in rows:
                owner_id = str(row.get("OWNER_ID") or "")
                if owner_id:
                    tasks[owner_id].append(row)
        return dict(tasks)

    # ------------------------------------------------------------------
    # Report sections
    # ------------------------------------------------------------------

    def _build_deal_dashboard(
        self,
        deals: list[dict[str, Any]],
        users: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
            in_work = [d for d in rows if str(d.get("STAGE_SEMANTIC_ID") or "") == "P"]
            won = [d for d in rows if str(d.get("STAGE_SEMANTIC_ID") or "") == "S"]
            failed = [d for d in rows if str(d.get("STAGE_SEMANTIC_ID") or "") == "F"]
            closed = won + failed
            return {
                "total_deals": len(rows),
                "total_amount": round(sum(_money(d.get("OPPORTUNITY")) for d in rows), 2),
                "in_work_count": len(in_work),
                "in_work_amount": round(sum(_money(d.get("OPPORTUNITY")) for d in in_work), 2),
                "won_count": len(won),
                "won_amount": round(sum(_money(d.get("OPPORTUNITY")) for d in won), 2),
                "failed_count": len(failed),
                "failed_amount": round(sum(_money(d.get("OPPORTUNITY")) for d in failed), 2),
                "closed_count": len(closed),
                "win_rate_closed_pct": _pct(len(won), len(closed)),
                "currency": self._main_currency(rows),
            }

        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for deal in deals:
            groups[str(deal.get("ASSIGNED_BY_ID") or "unknown")].append(deal)
        per_manager = []
        for manager_id, rows in sorted(groups.items()):
            per_manager.append(
                {
                    "manager_id": manager_id,
                    "manager_name": _manager_name(users.get(manager_id), manager_id),
                    **summary(rows),
                }
            )
        return {
            "department": summary(deals),
            "per_manager": per_manager,
        }

    def _main_currency(self, deals: list[dict[str, Any]]) -> str:
        counter = Counter(str(deal.get("CURRENCY_ID") or "") for deal in deals)
        if not counter:
            return ""
        return counter.most_common(1)[0][0]

    def _build_task_status(
        self,
        deals: list[dict[str, Any]],
        tasks_by_deal: dict[str, list[dict[str, Any]]],
        users: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        in_work = [deal for deal in deals if str(deal.get("STAGE_SEMANTIC_ID") or "") == "P"]
        now = _now()

        def is_open(task: dict[str, Any]) -> bool:
            return str(task.get("COMPLETED") or "") != "Y"

        def is_overdue(task: dict[str, Any]) -> bool:
            deadline = _parse_dt(task.get("DEADLINE"))
            return is_open(task) and deadline is not None and deadline < now

        def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
            without_open = []
            with_overdue = []
            with_open = []
            for deal in rows:
                deal_id = str(deal.get("ID") or "")
                tasks = tasks_by_deal.get(deal_id, [])
                open_tasks = [task for task in tasks if is_open(task)]
                overdue_tasks = [task for task in tasks if is_overdue(task)]
                if open_tasks:
                    with_open.append(deal_id)
                else:
                    without_open.append(deal_id)
                if overdue_tasks:
                    with_overdue.append(deal_id)
            return {
                "in_work_deals": len(rows),
                "with_open_tasks": len(with_open),
                "without_open_tasks": len(without_open),
                "with_overdue_tasks": len(with_overdue),
                "without_open_tasks_pct": _pct(len(without_open), len(rows)),
                "with_overdue_tasks_pct": _pct(len(with_overdue), len(rows)),
            }

        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for deal in in_work:
            groups[str(deal.get("ASSIGNED_BY_ID") or "unknown")].append(deal)
        per_manager = []
        for manager_id, rows in sorted(groups.items()):
            per_manager.append(
                {
                    "manager_id": manager_id,
                    "manager_name": _manager_name(users.get(manager_id), manager_id),
                    **summary(rows),
                }
            )
        return {
            "source": "crm.activity.list PROVIDER_ID=CRM_TASKS_TASK",
            "department": summary(in_work),
            "per_manager": per_manager,
        }

    def _build_stage_compliance(self, sales_quality_report: dict[str, Any]) -> dict[str, Any]:
        return {
            "overall_stage_score_pct": sales_quality_report.get("overall_stage_score_pct"),
            "stage_funnel": sales_quality_report.get("stage_funnel") or [],
            "manager_heatmap": (sales_quality_report.get("visuals") or {}).get(
                "manager_heatmap",
                [],
            ),
        }

    def _build_response_speed(self, sales_quality_report: dict[str, Any]) -> dict[str, Any]:
        per_manager = []
        known_values = []
        slow_weighted = 0
        known_count_total = 0
        for manager in sales_quality_report.get("per_manager") or []:
            response = manager.get("response_time") or {}
            known = int(response.get("known_count") or 0)
            avg_sec = response.get("avg_first_response_time_sec")
            slow_pct = float(response.get("slow_pct") or 0)
            if avg_sec is not None and known > 0:
                known_values.extend([float(avg_sec)] * known)
            slow_weighted += slow_pct * known
            known_count_total += known
            per_manager.append(
                {
                    "manager_id": str(manager.get("manager_id") or ""),
                    "manager_name": str(manager.get("manager_name") or ""),
                    "known_count": known,
                    "avg_first_response_time_sec": avg_sec,
                    "avg_first_response_time_min": round(float(avg_sec) / 60, 1)
                    if avg_sec is not None
                    else None,
                    "slow_pct": slow_pct,
                }
            )
        department_avg = _avg(known_values)
        return {
            "source": "sales-quality WhatsApp first_manager_response_time_sec",
            "department": {
                "known_count": known_count_total,
                "avg_first_response_time_sec": department_avg,
                "avg_first_response_time_min": round(department_avg / 60, 1)
                if department_avg is not None
                else None,
                "slow_pct": round(slow_weighted / known_count_total, 1)
                if known_count_total
                else 0.0,
            },
            "per_manager": per_manager,
        }

    def _build_failure_reasons(
        self,
        deals: list[dict[str, Any]],
        features: list[dict[str, Any]],
        users: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        failed_deals = [
            deal
            for deal in deals
            if str(deal.get("STAGE_SEMANTIC_ID") or "") == "F"
        ]
        failed_ids = {str(deal.get("ID") or "") for deal in failed_deals}
        by_deal = self._features_by_deal(features)
        failed_features = [
            feature
            for deal_id in failed_ids
            for feature in by_deal.get(deal_id, [])
        ]

        def problem_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            counts: Counter[str] = Counter()
            labels = self._problem_labels()
            for feature in rows:
                for key, value in (feature.get("problems") or {}).items():
                    if value:
                        counts[key] += 1
            total = len(rows)
            return [
                {
                    "key": key,
                    "label": labels.get(key, key),
                    "count": count,
                    "total": total,
                    "pct": _pct(count, total),
                }
                for key, count in counts.most_common()
            ]

        manager_failed_deals: dict[str, list[dict[str, Any]]] = defaultdict(list)
        manager_features: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for deal in failed_deals:
            deal_id = str(deal.get("ID") or "")
            manager_id = str(deal.get("ASSIGNED_BY_ID") or "unknown")
            manager_failed_deals[manager_id].append(deal)
            manager_features[manager_id].extend(by_deal.get(deal_id, []))

        per_manager = []
        for manager_id, failed_rows in sorted(manager_failed_deals.items()):
            feature_rows = manager_features.get(manager_id, [])
            per_manager.append(
                {
                    "manager_id": manager_id,
                    "manager_name": _manager_name(users.get(manager_id), manager_id),
                    "failed_deals_count": len(failed_rows),
                    "failed_amount": round(
                        sum(_money(deal.get("OPPORTUNITY")) for deal in failed_rows),
                        2,
                    ),
                    "failed_interactions_analyzed": len(feature_rows),
                    "top_reasons": problem_rows(feature_rows)[:5],
                }
            )
        return {
            "failed_deals_count": len(failed_ids),
            "failed_interactions_analyzed": len(failed_features),
            "department_top_reasons": problem_rows(failed_features)[:10],
            "per_manager": per_manager,
        }

    def _build_reanimation_cards(
        self,
        deals: list[dict[str, Any]],
        features: list[dict[str, Any]],
        users: dict[str, dict[str, Any]],
        contacts: dict[str, dict[str, Any]],
        portal_base_url: str,
        *,
        max_cards: int,
    ) -> dict[str, Any]:
        failed = [
            deal
            for deal in deals
            if str(deal.get("STAGE_SEMANTIC_ID") or "") == "F"
        ]
        by_deal = self._features_by_deal(features)
        cards = []
        for deal in failed:
            deal_id = str(deal.get("ID") or "")
            deal_features = by_deal.get(deal_id, [])
            contact_id = str(deal.get("CONTACT_ID") or "")
            contact = contacts.get(contact_id, {})
            manager_id = str(deal.get("ASSIGNED_BY_ID") or "unknown")
            problems = self._deal_problem_keys(deal_features)
            cards.append(
                {
                    "deal_id": deal_id,
                    "deal_title": str(deal.get("TITLE") or ""),
                    "manager_id": manager_id,
                    "manager_name": _manager_name(users.get(manager_id), manager_id),
                    "contact_id": contact_id,
                    "contact_name": self._contact_name(contact, contact_id),
                    "phone": self._first_multi_value(contact.get("PHONE")),
                    "opportunity": _money(deal.get("OPPORTUNITY")),
                    "currency": str(deal.get("CURRENCY_ID") or ""),
                    "deal_url": f"{portal_base_url.rstrip('/')}/crm/deal/details/{deal_id}/",
                    "contact_url": f"{portal_base_url.rstrip('/')}/crm/contact/details/{contact_id}/"
                    if contact_id
                    else "",
                    "priority": self._reanimation_priority(deal_features, problems),
                    "reason_signals": problems[:6],
                    "recommendations": self._reanimation_recommendations(problems),
                }
            )
        priority_order = {"high": 0, "medium": 1, "low": 2}
        cards = sorted(
            cards,
            key=lambda row: (priority_order.get(row["priority"], 9), -row["opportunity"]),
        )
        return {
            "total_failed_deals": len(failed),
            "cards_returned": min(len(cards), max_cards),
            "cards": cards[:max_cards],
        }

    def _build_lost_revenue(
        self,
        dashboard: dict[str, Any],
        *,
        average_ticket_kzt: float | None,
        expected_conversion_pct: float | None,
    ) -> dict[str, Any]:
        failed_count = int((dashboard.get("department") or {}).get("failed_count") or 0)
        if average_ticket_kzt is None or expected_conversion_pct is None:
            return {
                "available": False,
                "formula": "failed_deals_count * expected_conversion_rate * average_ticket_kzt",
                "failed_deals_count": failed_count,
                "expected_conversion_pct": expected_conversion_pct,
                "average_ticket_kzt": average_ticket_kzt,
                "estimated_lost_revenue_kzt": None,
                "missing_inputs": [
                    name
                    for name, value in [
                        ("average_ticket_kzt", average_ticket_kzt),
                        ("expected_conversion_pct", expected_conversion_pct),
                    ]
                    if value is None
                ],
            }
        estimate = failed_count * (expected_conversion_pct / 100) * average_ticket_kzt
        return {
            "available": True,
            "formula": "failed_deals_count * expected_conversion_rate * average_ticket_kzt",
            "failed_deals_count": failed_count,
            "expected_conversion_pct": expected_conversion_pct,
            "average_ticket_kzt": average_ticket_kzt,
            "estimated_lost_revenue_kzt": round(estimate, 2),
            "missing_inputs": [],
        }

    def _top_problems(self, sales_quality_report: dict[str, Any]) -> list[dict[str, Any]]:
        return list(sales_quality_report.get("top_problems") or [])

    def _top_growth(self, top_problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
        mapping = {
            "missing_qualification": {
                "title": "Ввести обязательный чеклист квалификации",
                "impact": "Повысит качество заявки и точность предложения.",
            },
            "no_offer_or_usp": {
                "title": "Добавить УТП/оффер в каждый диалог",
                "impact": "Усилит ценность предложения и отличия от конкурентов.",
            },
            "missing_next_step": {
                "title": "Фиксировать следующий шаг в каждом контакте",
                "impact": "Снизит потери после первого касания.",
            },
            "weak_presentation": {
                "title": "Усилить презентацию продукта под потребность клиента",
                "impact": "Сделает предложение конкретнее и убедительнее.",
            },
            "manager_did_not_ask_questions": {
                "title": "Обучить менеджеров задавать диагностические вопросы",
                "impact": "Поможет выявлять размеры, сроки, объект и критерии выбора.",
            },
            "slow_response": {
                "title": "Сократить время первого ответа",
                "impact": "Уменьшит потери горячих лидов.",
            },
        }
        rows = []
        for problem in top_problems:
            key = problem.get("key")
            config = mapping.get(key)
            if not config:
                continue
            rows.append(
                {
                    "source_problem": problem,
                    "title": config["title"],
                    "impact": config["impact"],
                    "priority": "high" if float(problem.get("pct") or 0) >= 50 else "medium",
                }
            )
        return rows

    def _build_integral_rating(
        self,
        *,
        dashboard: dict[str, Any],
        task_status: dict[str, Any],
        stage_compliance: dict[str, Any],
        response_speed: dict[str, Any],
        formula: str,
    ) -> dict[str, Any]:
        stage_score = float(stage_compliance.get("overall_stage_score_pct") or 0)
        win_rate = float((dashboard.get("department") or {}).get("win_rate_closed_pct") or 0)
        response_score = 100 - float(
            (response_speed.get("department") or {}).get("slow_pct") or 0
        )
        task_dept = task_status.get("department") or {}
        task_hygiene = 100 - max(
            float(task_dept.get("without_open_tasks_pct") or 0),
            float(task_dept.get("with_overdue_tasks_pct") or 0),
        )
        weights = {
            "stage_compliance": 0.40,
            "closed_win_rate": 0.20,
            "response_speed": 0.20,
            "task_hygiene": 0.20,
        }
        score_pct = (
            stage_score * weights["stage_compliance"]
            + win_rate * weights["closed_win_rate"]
            + response_score * weights["response_speed"]
            + task_hygiene * weights["task_hygiene"]
        )
        return {
            "available": True,
            "score_10": round(score_pct / 10, 1),
            "score_pct": round(score_pct, 1),
            "formula": formula,
            "weights": weights,
            "components": {
                "stage_compliance_pct": stage_score,
                "closed_win_rate_pct": win_rate,
                "response_speed_score_pct": round(response_score, 1),
                "task_hygiene_score_pct": round(task_hygiene, 1),
            },
            "note": "Formula is explicit and can be adjusted for the business.",
        }

    def _build_data_readiness(
        self,
        *,
        lost_revenue: dict[str, Any],
        rating: dict[str, Any],
    ) -> list[dict[str, str]]:
        return [
            {
                "question": "Общий рейтинг отдела продаж",
                "status": _QUESTION_STATUS["ready"] if rating.get("available") else _QUESTION_STATUS["formula_required"],
                "source": "integral_rating",
            },
            {
                "question": "Дашборд по сделкам",
                "status": _QUESTION_STATUS["ready"],
                "source": "crm.deal.list",
            },
            {
                "question": "Статус задач",
                "status": _QUESTION_STATUS["ready"],
                "source": "crm.activity.list PROVIDER_ID=CRM_TASKS_TASK",
            },
            {
                "question": "Соблюдение этапов продаж",
                "status": _QUESTION_STATUS["ready"],
                "source": "sales-quality/report.json",
            },
            {
                "question": "Скорость обработки лидов",
                "status": _QUESTION_STATUS["ready"],
                "source": "sales-quality response_time",
            },
            {
                "question": "Причины отказа",
                "status": _QUESTION_STATUS["ready"],
                "source": "failed deals + sales-quality features",
            },
            {
                "question": "Анализ проваленных сделок / реанимация",
                "status": _QUESTION_STATUS["ready"],
                "source": "failed deals + contacts + sales-quality features",
            },
            {
                "question": "Расчет упущенной выгоды",
                "status": _QUESTION_STATUS["ready"] if lost_revenue.get("available") else _QUESTION_STATUS["formula_required"],
                "source": "lost_revenue",
            },
            {
                "question": "Топ-3 проблемы отдела продаж",
                "status": _QUESTION_STATUS["ready"],
                "source": "sales-quality top_problems",
            },
            {
                "question": "Топ-3 точки роста",
                "status": _QUESTION_STATUS["ready"],
                "source": "derived from top_problems",
            },
        ]

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _features_by_deal(self, features: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for feature in features:
            source = feature.get("source") or {}
            deal_id = str(source.get("deal_id") or "").strip()
            if deal_id:
                rows[deal_id].append(feature)
        return dict(rows)

    def _problem_labels(self) -> dict[str, str]:
        return {
            "has_objections": "Есть возражения клиента",
            "missing_qualification": "Нет полноценной квалификации",
            "missing_next_step": "Нет следующего шага",
            "weak_presentation": "Слабая презентация",
            "slow_response": "Долгий ответ",
            "not_target_lead": "Нецелевой лид",
            "manager_did_not_ask_questions": "Менеджер не задает вопросы",
            "no_offer_or_usp": "Нет акции/УТП/оффера",
            "client_negative_or_cold": "Низкий интерес или негатив",
            "fragmented_or_low_content": "Мало полезного контента",
            "unclear_audio_or_text": "Плохое качество текста/аудио",
        }

    def _deal_problem_keys(self, features: list[dict[str, Any]]) -> list[str]:
        counts: Counter[str] = Counter()
        labels = self._problem_labels()
        for feature in features:
            for key, value in (feature.get("problems") or {}).items():
                if value:
                    counts[key] += 1
        return [labels.get(key, key) for key, _count in counts.most_common()]

    def _reanimation_priority(
        self,
        features: list[dict[str, Any]],
        problems: list[str],
    ) -> str:
        if not features:
            return "low"
        lead_statuses = {
            str((feature.get("lead_quality") or {}).get("status") or "")
            for feature in features
        }
        if "target" in lead_statuses or "possibly_target" in lead_statuses:
            if "Нет следующего шага" in problems or "Долгий ответ" in problems:
                return "high"
            return "medium"
        if "Нецелевой лид" in problems:
            return "low"
        return "medium"

    def _reanimation_recommendations(self, problems: list[str]) -> list[str]:
        recs: list[str] = []
        if "Нет полноценной квалификации" in problems or "Менеджер не задает вопросы" in problems:
            recs.append("Вернуться с 3-5 короткими вопросами: тип изделия, размеры, город/адрес, срок установки, нужен ли замер.")
        if "Нет акции/УТП/оффера" in problems or "Слабая презентация" in problems:
            recs.append("Отправить конкретный оффер: подходящий продукт, преимущество, срок, ориентир цены или следующий расчет.")
        if "Нет следующего шага" in problems:
            recs.append("Предложить конкретное действие: замер, звонок, визит в шоурум или расчет до определенного времени.")
        if "Долгий ответ" in problems:
            recs.append("Начать с короткого извинения за задержку и сразу дать полезный следующий шаг.")
        if not recs:
            recs.append("Проверить историю общения и предложить клиенту один простой следующий шаг.")
        return recs

    def _contact_name(self, contact: dict[str, Any], fallback: str) -> str:
        parts = [
            str(contact.get("NAME") or "").strip(),
            str(contact.get("LAST_NAME") or "").strip(),
        ]
        return " ".join(part for part in parts if part) or fallback

    def _first_multi_value(self, values: Any) -> str:
        if isinstance(values, list) and values:
            first = values[0]
            if isinstance(first, dict):
                return str(first.get("VALUE") or "")
        return ""

    def _markdown(self, report: dict[str, Any]) -> str:
        dashboard = report["deal_dashboard"]["department"]
        task = report["task_status"]["department"]
        response = report["lead_response_speed"]["department"]
        lost = report["lost_revenue"]
        lines = [
            "# Executive Sales Report",
            "",
            f"Generated: {report['generated_at']}",
            f"Scope: {report['scope']['mode']}, deals loaded: {report['scope']['deals_loaded']}",
            "",
            "## Summary",
            "",
            f"- Integral rating: {report['integral_rating']['score_10']} / 10",
            f"- Deals in work: {dashboard['in_work_count']}",
            f"- Won deals: {dashboard['won_count']} / amount {dashboard['won_amount']} {dashboard['currency']}",
            f"- Failed deals: {dashboard['failed_count']}",
            f"- Stage compliance: {report['sales_stage_compliance'].get('overall_stage_score_pct')}%",
            f"- Avg first response: {response.get('avg_first_response_time_min')} min",
            f"- In-work deals without open tasks: {task['without_open_tasks']}",
            f"- In-work deals with overdue tasks: {task['with_overdue_tasks']}",
            "",
            "## Top Problems",
            "",
        ]
        for idx, problem in enumerate(report.get("top_3_problems") or [], start=1):
            lines.append(f"{idx}. {problem['label']} - {problem['pct']}%")
        lines.extend(["", "## Growth Opportunities", ""])
        for idx, growth in enumerate(report.get("top_3_growth_opportunities") or [], start=1):
            lines.append(f"{idx}. {growth['title']} ({growth['priority']})")
        lines.extend(["", "## Lost Revenue", ""])
        if lost.get("available"):
            lines.append(
                f"Estimated lost revenue: {lost['estimated_lost_revenue_kzt']} KZT"
            )
        else:
            lines.append(
                "Lost revenue is not calculated. Missing inputs: "
                + ", ".join(lost.get("missing_inputs") or [])
            )
        lines.append("")
        return "\n".join(lines)
