"""Combine Postgres CRM analytics and AI executive analysis into one report."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def build_sales_audit_report(
    *,
    executive_report: dict[str, Any],
    sales_report: dict[str, Any],
    output_dir: Path | None = None,
    average_ticket_kzt: float | None = None,
    expected_conversion_pct: float | None = None,
) -> dict[str, Any]:
    """Return the final report used by the dashboard.

    `sales_report` is produced from the central database. No SQLite file is
    read here; the optional `output_dir` only mirrors the final JSON for legacy
    file-based consumers.
    """

    report: dict[str, Any] = dict(executive_report or {})
    report.setdefault("references", {})

    if sales_report.get("deal_dashboard"):
        report["deal_dashboard"] = sales_report["deal_dashboard"]
    if sales_report.get("task_status"):
        report["task_status"] = sales_report["task_status"]
    if sales_report.get("lead_status"):
        report["lead_status"] = sales_report["lead_status"]
    if sales_report.get("revenue_summary"):
        report["revenue_summary"] = sales_report["revenue_summary"]
    if sales_report.get("failure_reasons"):
        report["failure_reasons"] = _merge_failure_reasons(
            report.get("failure_reasons") or {},
            sales_report["failure_reasons"],
        )

    report["generated_at"] = _now_iso()
    report["scope"] = _build_scope(report.get("scope") or {}, sales_report)
    report["references"] = _merge_references(report.get("references") or {}, sales_report)
    report["lost_revenue"] = _build_lost_revenue(
        report.get("deal_dashboard") or {},
        current=report.get("lost_revenue") or {},
        average_ticket_kzt=average_ticket_kzt,
        expected_conversion_pct=expected_conversion_pct,
    )
    report["integral_rating"] = _build_integral_rating(report)
    report["top_3_problems"] = _top_problems(report)
    report["top_3_growth_opportunities"] = _top_growth(report["top_3_problems"])
    report["action_guide"] = _action_guide(report)
    report["data_readiness"] = _data_readiness(report)
    report["sales_audit_sources"] = {
        "analytics_storage": "postgres",
        "sales_run_id": sales_report.get("run_id", ""),
        "tenant_id": sales_report.get("tenant_id", ""),
        "tables": [
            "sales_analytics_deals",
            "sales_analytics_tasks",
            "sales_analytics_leads",
            "sales_analytics_revenue_documents",
        ],
    }

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_json(output_dir / "sales-audit-report.json", report)
        _write_json(output_dir / "executive-report.json", report)
        _write_json(output_dir / "sales-audit-summary.json", _summary(report))
        (output_dir / "sales-audit-report.md").write_text(
            _markdown(report),
            encoding="utf-8",
        )
    return report


def _merge_failure_reasons(ai: dict[str, Any], sql: dict[str, Any]) -> dict[str, Any]:
    if not sql.get("failed_deals_count"):
        return ai or sql
    return {
        **ai,
        **sql,
        "ai_department_top_reasons": ai.get("department_top_reasons") or [],
        "ai_failed_interactions_analyzed": ai.get("failed_interactions_analyzed") or 0,
        "source": "postgres_lost_stage_primary + ai_features_available",
    }


def _build_scope(current: dict[str, Any], sales_report: dict[str, Any]) -> dict[str, Any]:
    meta = sales_report.get("meta") or {}
    scope = dict(current)
    scope["mode"] = "sales_audit"
    scope["date_from"] = meta.get("date_from") or scope.get("date_from")
    scope["date_to"] = meta.get("date_to") or scope.get("date_to")
    scope["analytics_storage"] = "postgres"
    return scope


def _merge_references(current: dict[str, Any], sales_report: dict[str, Any]) -> dict[str, Any]:
    refs = dict(current)
    sales_refs = sales_report.get("references") or {}
    refs["manager_names"] = {
        **(refs.get("manager_names") or {}),
        **(sales_refs.get("manager_names") or {}),
    }
    refs["stage_names"] = {
        **(refs.get("stage_names") or {}),
        **(sales_refs.get("stage_names") or {}),
    }
    return refs


def _build_lost_revenue(
    dashboard: dict[str, Any],
    *,
    current: dict[str, Any],
    average_ticket_kzt: float | None,
    expected_conversion_pct: float | None,
) -> dict[str, Any]:
    department = dashboard.get("department") or {}
    failed_count = _int(department.get("failed_count"))
    average_ticket = (
        average_ticket_kzt
        if average_ticket_kzt is not None
        else _optional_num(current.get("average_ticket_kzt"))
    )
    expected_conversion = (
        expected_conversion_pct
        if expected_conversion_pct is not None
        else _optional_num(current.get("expected_conversion_pct"))
    )
    if expected_conversion is None:
        expected_conversion = _optional_num(department.get("win_rate_closed_pct"))

    missing = []
    if average_ticket is None:
        missing.append("average_ticket_kzt")
    if expected_conversion is None:
        missing.append("expected_conversion_pct")

    estimate = None
    if average_ticket is not None and expected_conversion is not None:
        estimate = round(failed_count * (expected_conversion / 100) * average_ticket, 2)

    return {
        "available": estimate is not None,
        "formula": "failed_deals_count * expected_conversion_rate * average_ticket_kzt",
        "failed_deals_count": failed_count,
        "expected_conversion_pct": expected_conversion,
        "average_ticket_kzt": average_ticket,
        "estimated_lost_revenue_kzt": estimate,
        "missing_inputs": missing,
        "conversion_source": "request_or_postgres_closed_win_rate",
    }


def _build_integral_rating(report: dict[str, Any]) -> dict[str, Any]:
    dashboard = report.get("deal_dashboard") or {}
    task_status = report.get("task_status") or {}
    stage_compliance = report.get("sales_stage_compliance") or {}
    response_speed = report.get("lead_response_speed") or {}

    stage_score = _num(stage_compliance.get("overall_stage_score_pct"))
    win_rate = _num((dashboard.get("department") or {}).get("win_rate_closed_pct"))
    response_dept = response_speed.get("department") or {}
    known_responses = _int(response_dept.get("known_count"))
    response_score = 100 - _num(response_dept.get("slow_pct")) if known_responses else 0.0
    task_dept = task_status.get("department") or {}
    task_hygiene = 100 - max(
        _num(task_dept.get("without_open_tasks_pct")),
        _num(task_dept.get("with_overdue_tasks_pct")),
    )
    task_hygiene = max(0.0, task_hygiene)

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
        "formula": "sales_audit_weighted",
        "weights": weights,
        "components": {
            "stage_compliance_pct": round(stage_score, 1),
            "closed_win_rate_pct": round(win_rate, 1),
            "response_speed_score_pct": round(response_score, 1),
            "task_hygiene_score_pct": round(task_hygiene, 1),
        },
        "note": "Postgres gives exact CRM numbers; AI gives communication-quality metrics.",
    }


def _top_problems(report: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [_normalize_ai_problem(row) for row in report.get("top_3_problems") or []]

    task = (report.get("task_status") or {}).get("department") or {}
    in_work = _int(task.get("in_work_deals"))
    without_tasks = _int(task.get("without_open_tasks"))
    overdue = _int(task.get("with_overdue_tasks"))
    if without_tasks:
        candidates.append({
            "key": "crm_tasks_missing",
            "label": "Активные сделки без задач",
            "count": without_tasks,
            "total": in_work,
            "pct": _pct(without_tasks, in_work),
        })
    if overdue:
        candidates.append({
            "key": "crm_tasks_overdue",
            "label": "Активные сделки с просроченными задачами",
            "count": overdue,
            "total": in_work,
            "pct": _pct(overdue, in_work),
        })

    dashboard = (report.get("deal_dashboard") or {}).get("department") or {}
    closed = _int(dashboard.get("closed_count"))
    win_rate = _num(dashboard.get("win_rate_closed_pct"))
    if closed and win_rate < 20:
        candidates.append({
            "key": "low_win_rate",
            "label": "Низкая доля успешных закрытий",
            "count": max(0, closed - _int(dashboard.get("won_count"))),
            "total": closed,
            "pct": round(100 - win_rate, 1),
        })

    failure = report.get("failure_reasons") or {}
    reason = (failure.get("department_top_reasons") or [{}])[0]
    if reason.get("count"):
        candidates.append({
            "key": f"lost_reason:{reason.get('key') or reason.get('label')}",
            "label": f"Частая причина отказа: {reason.get('label') or 'не указано'}",
            "count": _int(reason.get("count")),
            "total": _int(reason.get("total")) or _int(failure.get("failed_deals_count")),
            "pct": _num(reason.get("pct")),
        })

    lead_status = report.get("lead_status") or {}
    lead_dept = lead_status.get("department") or {}
    lead_junk = _int(lead_dept.get("junk_count"))
    lead_total = _int(lead_dept.get("total_leads"))
    if lead_junk:
        lead_reason = (lead_status.get("department_top_reasons") or [{}])[0]
        candidates.append({
            "key": f"lead_lost_reason:{lead_reason.get('key') or 'unknown'}",
            "label": f"Сливы лидов: {lead_reason.get('label') or 'не указано'}",
            "count": lead_junk,
            "total": lead_total,
            "pct": _pct(lead_junk, lead_total),
        })

    unique: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = str(item.get("key") or item.get("label") or "").strip()
        if key and key not in unique:
            unique[key] = item
    return sorted(unique.values(), key=lambda row: _num(row.get("pct")), reverse=True)[:3]


def _top_growth(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping = {
        "crm_tasks_missing": ("Внедрить KPI по работе в CRM", "Каждая активная сделка должна иметь следующий шаг или задачу."),
        "crm_tasks_overdue": ("Ввести ежедневный контроль просроченных задач", "Просрочки по follow-up нужно разбирать по менеджерам каждый день."),
        "low_win_rate": ("Разобрать проваленные сделки по причинам", "Планерка должна начинаться с причин отказа, а не только с количества заявок."),
        "slow_response": ("Ускорить первый ответ новым лидам", "Нужен SLA первого ответа и контроль заявок без реакции."),
        "missing_qualification": ("Провести обучение по выявлению потребности", "Менеджеры должны задавать вопросы до презентации продукта."),
        "missing_next_step": ("Фиксировать следующий шаг в каждом контакте", "Диалог без следующего шага должен считаться незавершенным."),
        "weak_presentation": ("Усилить презентацию продукта", "Предложение должно быть привязано к задаче клиента и выгодам."),
        "no_offer_or_usp": ("Встроить акции, УТП и офферы в скрипт", "Менеджер должен явно проговаривать ценность и следующий шаг."),
    }
    rows = []
    for problem in problems:
        key = str(problem.get("key") or "")
        config = mapping.get(key)
        if not config and key.startswith("lost_reason:"):
            config = ("Собрать базу возражений и ответов", "Топовые причины отказа нужно превратить в готовые ответы менеджеров.")
        if not config and key.startswith("lead_lost_reason:"):
            config = ("Разобрать причины слива лидов", "Статусы некачественных лидов нужно сверить с диалогами и причиной отказа клиента.")
        if not config:
            config = (str(problem.get("label") or "Точка роста"), "Разобрать проблему на планерке и назначить владельца изменения.")
        rows.append({
            "source_problem": problem,
            "title": config[0],
            "impact": config[1],
            "priority": "high" if _num(problem.get("pct")) >= 50 else "medium",
        })
    return rows[:3]


def _action_guide(report: dict[str, Any]) -> list[str]:
    guide: list[str] = []
    problem_keys = {str(row.get("key") or "") for row in report.get("top_3_problems") or []}
    if "crm_tasks_missing" in problem_keys or "crm_tasks_overdue" in problem_keys:
        guide.append("Внедрить KPI для соблюдения работы в CRM: у каждой сделки должна быть задача, просрочки разбираются ежедневно.")
    if "slow_response" in problem_keys:
        guide.append("Повысить контроль скорости реагирования на новые заявки и ввести SLA первого ответа.")
    if any(key in problem_keys for key in {"missing_qualification", "weak_presentation", "no_offer_or_usp", "missing_next_step"}):
        guide.append("Провести обучение по скриптам и этапам продаж: контакт, потребность, презентация, оффер, следующий шаг.")
    if any(key.startswith("lost_reason:") for key in problem_keys):
        guide.append("Составить список частых возражений и ответов на них, затем проверить знание у менеджеров.")
    if any(key.startswith("lead_lost_reason:") for key in problem_keys):
        guide.append("Разделить причины слива лидов по статусам и менеджерам, затем проверить, где это реальный нецелевой лид, а где потерянная обработка.")
    if _int((report.get("failure_reasons") or {}).get("failed_deals_count")):
        guide.append("Ввести регламент провала сделок: причина отказа обязательна, теплые сделки сначала возвращаются в follow-up.")
        guide.append("Обзвонить клиентов из выигранных и проигранных сделок: почему купили и почему не оплатили.")
    if _int(((report.get("deal_dashboard") or {}).get("department") or {}).get("in_work_count")):
        guide.append("Ежедневно замерять заявки, звонки, задачи, продажи и причины отказа по каждому менеджеру.")
    return guide[:8] or ["Назначить владельца отчета и еженедельно разбирать показатели по менеджерам."]


def _data_readiness(report: dict[str, Any]) -> list[dict[str, str]]:
    response = (report.get("lead_response_speed") or {}).get("department") or {}
    stage = report.get("sales_stage_compliance") or {}
    lost = report.get("lost_revenue") or {}
    leads = (report.get("lead_status") or {}).get("department") or {}
    revenue = (report.get("revenue_summary") or {}).get("department") or {}
    return [
        {"question": "Общий рейтинг отдела продаж", "status": "ready", "source": "integral_rating"},
        {"question": "Дашборд по сделкам", "status": "ready", "source": "postgres sales_analytics_deals"},
        {"question": "Топ-3 проблемы отдела продаж", "status": "ready", "source": "postgres + ai"},
        {"question": "Топ-3 точки роста", "status": "ready", "source": "derived action rules"},
        {"question": "Скорость обработки лидов", "status": "ready" if _int(response.get("known_count")) else "needs_input", "source": "AI communication analysis"},
        {"question": "Сделки без задач и с просроченными задачами", "status": "ready", "source": "postgres sales_analytics_tasks"},
        {"question": "Соблюдение этапов продаж", "status": "ready" if stage.get("overall_stage_score_pct") is not None else "needs_input", "source": "AI communication analysis"},
        {
            "question": "Причины слива лидов/сделок",
            "status": "ready" if _int(leads.get("total_leads")) else "partial",
            "source": "postgres leads + lost stages + AI features",
        },
        {"question": "Анализ проваленных сделок", "status": "ready", "source": "failed deals + AI features"},
        {"question": "Сколько денег не дозаработано", "status": "ready" if lost.get("available") else "formula_required", "source": "lost_revenue"},
        {
            "question": "Оплаты/счета для сверки выручки",
            "status": "ready" if _int(revenue.get("document_count")) else "partial",
            "source": "postgres sale orders/payments/invoices",
        },
    ]


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": report.get("generated_at"),
        "scope": report.get("scope"),
        "integral_rating": report.get("integral_rating"),
        "deal_dashboard": report.get("deal_dashboard"),
        "task_status": report.get("task_status"),
        "data_readiness": report.get("data_readiness"),
        "sources": report.get("sales_audit_sources"),
    }


def _normalize_ai_problem(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": row.get("key") or row.get("label") or "",
        "label": row.get("label") or row.get("title") or row.get("key") or "Проблема",
        "count": _int(row.get("count")),
        "total": _int(row.get("total")),
        "pct": _num(row.get("pct")),
    }


def _markdown(report: dict[str, Any]) -> str:
    dashboard = (report.get("deal_dashboard") or {}).get("department") or {}
    rating = report.get("integral_rating") or {}
    task = (report.get("task_status") or {}).get("department") or {}
    lost = report.get("lost_revenue") or {}
    lines = [
        "# Sales Audit Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        "",
        "## Summary",
        "",
        f"- Rating: {rating.get('score_10', 0)} / 10",
        f"- Deals in work: {dashboard.get('in_work_count', 0)}",
        f"- Won deals: {dashboard.get('won_count', 0)} / amount {dashboard.get('won_amount', 0)} {dashboard.get('currency', '')}",
        f"- Failed deals: {dashboard.get('failed_count', 0)}",
        f"- Deals without tasks: {task.get('without_open_tasks', 0)}",
        f"- Deals with overdue tasks: {task.get('with_overdue_tasks', 0)}",
        "",
        "## Top Problems",
        "",
    ]
    for index, problem in enumerate(report.get("top_3_problems") or [], start=1):
        lines.append(f"{index}. {problem.get('label', '')} - {problem.get('pct', 0)}%")
    lines.extend(["", "## Growth Opportunities", ""])
    for index, growth in enumerate(report.get("top_3_growth_opportunities") or [], start=1):
        lines.append(f"{index}. {growth.get('title', '')} ({growth.get('priority', '')})")
    lines.extend(["", "## Lost Revenue", ""])
    if lost.get("available"):
        lines.append(f"Estimated missed revenue: {lost.get('estimated_lost_revenue_kzt')} KZT")
    else:
        lines.append("Missed revenue is not calculated. Missing inputs: " + ", ".join(lost.get("missing_inputs") or []))
    lines.append("")
    return "\n".join(lines)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _optional_num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _pct(count: int | float, total: int | float) -> float:
    c = _num(count)
    t = _num(total)
    return round(c / t * 100, 1) if t else 0.0
