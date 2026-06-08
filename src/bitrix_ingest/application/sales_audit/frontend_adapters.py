"""Frontend-facing adapters for sales-audit reports."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

_SLA_RESPONSE_MINUTES = 30

_TRIGGER_META = {
    "response_sla": {
        "label": "Ответ > 30 минут",
        "severity": 5,
        "recommendation": "Ответить клиенту сейчас, признать задержку, дать конкретный ответ и поставить задачу на следующий контакт.",
    },
    "missed_call": {
        "label": "Пропущенный звонок",
        "severity": 5,
        "recommendation": "Перезвонить клиенту. Если не дозвонились, отправить сообщение с временем повторного звонка.",
    },
    "no_next_step": {
        "label": "Нет следующего шага",
        "severity": 4,
        "recommendation": "Зафиксировать следующий шаг в CRM: встреча, Zoom, замер, расчет, оплата или контрольный звонок.",
    },
    "no_active_task": {
        "label": "Нет активной задачи",
        "severity": 4,
        "recommendation": "Поставить активную задачу по сделке с ближайшим дедлайном и понятным результатом.",
    },
    "overdue_task": {
        "label": "Просроченная задача",
        "severity": 4,
        "recommendation": "Обновить просроченную задачу и связаться с клиентом до конца рабочего дня.",
    },
    "no_need_identified": {
        "label": "Продажа в лоб",
        "severity": 3,
        "recommendation": "Вернуться к квалификации: уточнить задачу, сроки, объем, бюджет и критерии выбора.",
    },
}


def load_sales_quality_features(sales_quality_dir: Path | None) -> list[dict[str, Any]]:
    """Load normalized sales-quality feature JSON files from a run directory."""
    if not sales_quality_dir:
        return []
    features_dir = sales_quality_dir / "features"
    if not features_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(features_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(raw, dict):
            rows.append(raw)
    return rows


def build_frontend_sales_audit_data(
    *,
    features: list[dict[str, Any]],
    report: dict[str, Any],
    scope_deals: list[dict[str, Any]] | None = None,
    portal_base_url: str = "",
) -> dict[str, Any]:
    """Build arrays consumed by the service-2.0 frontend screens."""
    manager_names = _report_manager_names(report)
    deal_index = _build_deal_index(
        report,
        scope_deals or [],
        portal_base_url,
        manager_names=manager_names,
    )
    interactions = build_interaction_index(
        features=features,
        deal_index=deal_index,
        portal_base_url=portal_base_url,
        manager_names=manager_names,
    )
    urgent_alerts = build_urgent_alerts(
        interactions=interactions,
        report=report,
        deal_index=deal_index,
        portal_base_url=portal_base_url,
    )
    return {
        "interaction_index": interactions,
        "whatsapp_interactions": [
            row for row in interactions if row.get("channel") == "whatsapp"
        ],
        "call_interactions": [
            row for row in interactions if row.get("channel") == "call"
        ],
        "urgent_alerts": urgent_alerts,
        "alerts_dashboard": {
            "rows": urgent_alerts,
            "source": "sales_quality_features + sales_analytics_tasks",
        },
    }


def build_interaction_index(
    *,
    features: list[dict[str, Any]],
    deal_index: dict[str, dict[str, Any]] | None = None,
    portal_base_url: str = "",
    manager_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Flatten sales-quality features into rows for calls and WhatsApp tables."""
    deal_index = deal_index or {}
    manager_names = manager_names or {}
    rows = [
        _interaction_row(
            feature,
            deal_index=deal_index,
            portal_base_url=portal_base_url,
            manager_names=manager_names,
        )
        for feature in features
        if isinstance(feature, dict)
    ]
    return sorted(rows, key=lambda row: _timestamp(row.get("created_at")), reverse=True)


def enrich_frontend_manager_names(report: dict[str, Any]) -> dict[str, Any]:
    """Fill frontend manager names from report references for saved reports."""
    if not isinstance(report, dict):
        return report
    manager_names = _report_manager_names(report)
    if not manager_names:
        return report

    enriched = dict(report)
    for key in ("interaction_index", "whatsapp_interactions", "call_interactions"):
        if key in enriched:
            enriched[key] = _enrich_manager_rows(enriched.get(key), manager_names)

    if "urgent_alerts" in enriched:
        enriched["urgent_alerts"] = _enrich_alert_rows(enriched.get("urgent_alerts"), manager_names)

    dashboard = enriched.get("alerts_dashboard")
    if isinstance(dashboard, dict) and "rows" in dashboard:
        enriched["alerts_dashboard"] = {
            **dashboard,
            "rows": _enrich_alert_rows(dashboard.get("rows"), manager_names),
        }

    return enriched


def build_urgent_alerts(
    *,
    interactions: list[dict[str, Any]],
    report: dict[str, Any],
    deal_index: dict[str, dict[str, Any]] | None = None,
    portal_base_url: str = "",
) -> list[dict[str, Any]]:
    """Build per-deal urgent alerts from interaction and CRM-task signals."""
    deal_index = deal_index or {}
    grouped: dict[str, dict[str, Any]] = {}

    for row in interactions:
        if not _is_active_deal(row):
            continue
        triggers = _interaction_triggers(row)
        if not triggers:
            continue
        _merge_alert(
            grouped,
            _alert_base(row, deal_index=deal_index, portal_base_url=portal_base_url),
            triggers,
        )

    for task_row in _task_deal_rows(report):
        deal_id = _clean(task_row.get("deal_id") or task_row.get("id"))
        if not deal_id:
            continue
        triggers = []
        active_count = _int(task_row.get("active_task_count") or task_row.get("open_task_count"))
        overdue_count = _int(task_row.get("overdue_task_count") or task_row.get("overdue_tasks"))
        if active_count <= 0:
            triggers.append(_trigger("no_active_task", "По активной сделке не найдено открытых задач."))
        if overdue_count > 0:
            triggers.append(_trigger("overdue_task", f"В сделке просроченных задач: {overdue_count}."))
        if not triggers:
            continue
        indexed = deal_index.get(deal_id, {})
        base = {
            "id": deal_id,
            "deal_id": deal_id,
            "deal_title": indexed.get("deal_title") or task_row.get("deal_title") or f"Сделка #{deal_id}",
            "deal_url": indexed.get("deal_url") or _deal_url(deal_id, portal_base_url),
            "manager_id": _clean(indexed.get("manager_id") or task_row.get("manager_id")),
            "manager_label": indexed.get("manager_name") or task_row.get("manager_name") or _manager_label(task_row.get("manager_id")),
            "created_at": indexed.get("created_at") or task_row.get("created_at") or "",
        }
        _merge_alert(grouped, base, triggers)

    return sorted(
        (_finalize_alert(row) for row in grouped.values()),
        key=lambda row: (-_num(row.get("score")), _timestamp(row.get("created_at")) * -1),
    )


def _interaction_row(
    feature: dict[str, Any],
    *,
    deal_index: dict[str, dict[str, Any]],
    portal_base_url: str,
    manager_names: dict[str, str],
) -> dict[str, Any]:
    source = feature.get("source") or {}
    source_type = _source_type(source)
    deal_id = _clean(source.get("deal_id"))
    deal = deal_index.get(deal_id, {})
    response = feature.get("response_time") or {}
    stages = feature.get("sales_stages") or {}
    problems = feature.get("problems") or {}
    next_step = feature.get("next_step") or {}
    lead_quality = feature.get("lead_quality") or {}
    started_at = _clean(source.get("started_at") or feature.get("generated_at"))
    tags = [str(item) for item in feature.get("tags") or [] if str(item).strip()]
    source_file = _clean(source.get("source_file_path"))
    manager_id = _clean(source.get("manager_id") or deal.get("manager_id"))
    manager_name = _resolve_manager_name(
        manager_id,
        source.get("manager_name"),
        deal.get("manager_name"),
        manager_names=manager_names,
    )

    return {
        "interaction_id": _interaction_id(source, source_type),
        "channel": source_type,
        "created_at": started_at,
        "manager_id": manager_id,
        "manager_name": manager_name,
        "deal_id": deal_id,
        "deal_title": deal.get("deal_title") or (f"Сделка #{deal_id}" if deal_id else ""),
        "deal_url": deal.get("deal_url") or _deal_url(deal_id, portal_base_url),
        "crm_url": deal.get("deal_url") or _deal_url(deal_id, portal_base_url),
        "deal_stage_id": deal.get("stage_id") or "",
        "deal_stage_name": deal.get("stage_name") or "",
        "deal_stage_semantic_id": deal.get("stage_semantic_id") or "",
        "summary": _clean(feature.get("summary")),
        "primary_topic": _primary_topic(feature, tags),
        "client_request": _client_request(feature),
        "outcome_status": _outcome_status(feature),
        "need_identified": _yes_no(stages.get("need_identified")),
        "manager_asked_questions": "no" if bool(problems.get("manager_did_not_ask_questions")) else "yes",
        "manager_presented_service": _yes_no(stages.get("product_presented")),
        "manager_agreed_next_step": "yes" if _has_next_step(feature) else "no",
        "short_or_low_content": bool(problems.get("fragmented_or_low_content")),
        "non_sales_interaction": bool(problems.get("not_target_lead") or lead_quality.get("status") == "not_target"),
        "fragmented_or_unclear": bool(
            problems.get("fragmented_or_low_content") or problems.get("unclear_audio_or_text")
        ),
        "labels": tags,
        "tags": tags,
        "response_wait_minutes": _minutes(response.get("first_response_time_sec")),
        "first_response_time_sec": response.get("first_response_time_sec"),
        "avg_response_latency_sec": response.get("avg_response_latency_sec"),
        "stage_score_pct": feature.get("stage_score_pct"),
        "transcript_file_path": source_file if source_type == "call" else "",
        "conversation_file_path": source_file if source_type == "whatsapp" else "",
        "crm_activity_id": _clean(source.get("crm_activity_id")),
        "record_file_id": _clean(source.get("record_file_id")),
        "contact_id": _clean(source.get("contact_id")),
        "source": {
            **source,
            "manager_id": manager_id,
            "manager_name": manager_name,
            "transcript_file_path": source_file if source_type == "call" else "",
            "conversation_file_path": source_file if source_type == "whatsapp" else "",
            "deal_url": deal.get("deal_url") or _deal_url(deal_id, portal_base_url),
        },
    }


def _interaction_triggers(row: dict[str, Any]) -> list[dict[str, Any]]:
    triggers = []
    wait_minutes = _num(row.get("response_wait_minutes"))
    if wait_minutes > _SLA_RESPONSE_MINUTES and _is_working_time(row.get("created_at")):
        triggers.append(_trigger("response_sla", f"Клиент ждет ответа {round(wait_minutes, 1)} мин в рабочее время."))
    if row.get("channel") == "call" and str(row.get("outcome_status") or "").lower() in {"no_answer", "missed_call"}:
        triggers.append(_trigger("missed_call", "В сделке есть пропущенный звонок или неуспешная попытка связи."))
    if str(row.get("manager_agreed_next_step") or "").lower() != "yes":
        triggers.append(_trigger("no_next_step", "Коммуникация завершилась без конкретного следующего шага."))
    if str(row.get("need_identified") or "").lower() != "yes" and str(row.get("manager_presented_service") or "").lower() == "yes":
        triggers.append(_trigger("no_need_identified", "Менеджер перешел к презентации до выявления потребности клиента."))
    return triggers


def _task_deal_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    task_status = report.get("task_status") or {}
    return [
        row
        for row in task_status.get("deals") or task_status.get("per_deal") or []
        if isinstance(row, dict)
    ]


def _alert_base(
    row: dict[str, Any],
    *,
    deal_index: dict[str, dict[str, Any]],
    portal_base_url: str,
) -> dict[str, Any]:
    deal_id = _clean(row.get("deal_id"))
    indexed = deal_index.get(deal_id, {})
    return {
        "id": deal_id or _clean(row.get("interaction_id")),
        "deal_id": deal_id,
        "deal_title": indexed.get("deal_title") or row.get("deal_title") or f"Сделка #{deal_id}",
        "deal_url": indexed.get("deal_url") or row.get("deal_url") or _deal_url(deal_id, portal_base_url),
        "manager_id": _clean(row.get("manager_id")),
        "manager_label": row.get("manager_name") or _manager_label(row.get("manager_id")),
        "created_at": row.get("created_at") or "",
    }


def _merge_alert(
    grouped: dict[str, dict[str, Any]],
    base: dict[str, Any],
    triggers: list[dict[str, Any]],
) -> None:
    key = _clean(base.get("deal_id") or base.get("id"))
    if not key:
        return
    current = grouped.get(key)
    if current is None:
        current = {**base, "triggers": []}
        grouped[key] = current
    existing = {trigger.get("type") for trigger in current["triggers"]}
    for trigger in triggers:
        if trigger.get("type") not in existing:
            current["triggers"].append(trigger)
            existing.add(trigger.get("type"))


def _finalize_alert(row: dict[str, Any]) -> dict[str, Any]:
    triggers = sorted(
        row.get("triggers") or [],
        key=lambda trigger: (-_int(trigger.get("severity")), str(trigger.get("label") or "")),
    )
    primary = triggers[0] if triggers else _trigger("no_next_step", "Сделка требует внимания.")
    reason = " ".join(str(trigger.get("reason") or "") for trigger in triggers).strip()
    recommendation = " ".join(
        str(trigger.get("recommendation") or "") for trigger in triggers[:3]
    ).strip()
    score = min(10, sum(_int(trigger.get("severity")) for trigger in triggers))
    return {
        **row,
        "score": score,
        "severity": score,
        "trigger_type": primary.get("type"),
        "reason": reason,
        "recommendation": recommendation,
        "what_wrong": reason,
        "what_to_do": recommendation,
        "triggers": triggers,
    }


def _trigger(trigger_type: str, reason: str) -> dict[str, Any]:
    meta = _TRIGGER_META[trigger_type]
    return {
        "type": trigger_type,
        "label": meta["label"],
        "severity": meta["severity"],
        "reason": reason,
        "recommendation": meta["recommendation"],
    }


def _build_deal_index(
    report: dict[str, Any],
    scope_deals: list[dict[str, Any]],
    portal_base_url: str,
    *,
    manager_names: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    manager_names = manager_names or {}
    for row in scope_deals:
        normalized = _normalize_deal(row, portal_base_url, manager_names=manager_names)
        if normalized.get("deal_id"):
            index[normalized["deal_id"]] = normalized
    report_deals = [
        *((report.get("deal_dashboard") or {}).get("deals") or []),
        *((report.get("task_status") or {}).get("deals") or []),
    ]
    for row in report_deals:
        normalized = _normalize_deal(row, portal_base_url, manager_names=manager_names)
        if not normalized.get("deal_id"):
            continue
        current = index.setdefault(normalized["deal_id"], {})
        for key, value in normalized.items():
            if value not in ("", None) and not current.get(key):
                current[key] = value
    return index


def _normalize_deal(
    row: dict[str, Any],
    portal_base_url: str,
    *,
    manager_names: dict[str, str],
) -> dict[str, Any]:
    deal_id = _clean(row.get("ID") or row.get("id") or row.get("deal_id"))
    manager_id = _clean(row.get("ASSIGNED_BY_ID") or row.get("assigned_by_id") or row.get("manager_id"))
    return {
        "deal_id": deal_id,
        "deal_title": _clean(row.get("TITLE") or row.get("title") or row.get("deal_title")) or (f"Сделка #{deal_id}" if deal_id else ""),
        "deal_url": _clean(row.get("deal_url") or row.get("crm_url")) or _deal_url(deal_id, portal_base_url),
        "manager_id": manager_id,
        "manager_name": _resolve_manager_name(
            manager_id,
            row.get("manager_name"),
            manager_names=manager_names,
        ),
        "stage_id": _clean(row.get("STAGE_ID") or row.get("stage_id")),
        "stage_name": _clean(row.get("stage_name") or row.get("STAGE_NAME")),
        "stage_semantic_id": _clean(row.get("STAGE_SEMANTIC_ID") or row.get("stage_semantic_id")),
        "created_at": _clean(row.get("DATE_CREATE") or row.get("date_create") or row.get("created_at")),
    }


def _source_type(source: dict[str, Any]) -> str:
    raw = str(source.get("source_type") or source.get("channel") or "").lower()
    if raw in {"call", "phone_call", "phone", "crm_call"}:
        return "call"
    if raw in {"whatsapp", "wa", "openline"}:
        return "whatsapp"
    return raw or "unknown"


def _interaction_id(source: dict[str, Any], source_type: str) -> str:
    source_id = _clean(source.get("source_id") or source.get("crm_activity_id") or source.get("deal_id"))
    return f"{source_type}-{source_id}" if source_id else source_type


def _primary_topic(feature: dict[str, Any], tags: list[str]) -> str:
    if tags:
        return tags[0]
    lead_quality = feature.get("lead_quality") or {}
    return _clean(lead_quality.get("reason")) or "Коммуникация с клиентом"


def _client_request(feature: dict[str, Any]) -> str:
    evidence = feature.get("evidence") or []
    if evidence:
        return _clean(evidence[0])
    return _clean(feature.get("summary"))


def _outcome_status(feature: dict[str, Any]) -> str:
    problems = feature.get("problems") or {}
    lead_quality = feature.get("lead_quality") or {}
    if problems.get("not_target_lead") or lead_quality.get("status") == "not_target":
        return "not_interested"
    if problems.get("client_negative_or_cold"):
        return "not_interested"
    if _has_next_step(feature):
        return "follow_up"
    if problems.get("missing_next_step") or problems.get("slow_response"):
        return "awaiting_response"
    return "qualified_interest"


def _has_next_step(feature: dict[str, Any]) -> bool:
    next_step = feature.get("next_step") or {}
    stages = feature.get("sales_stages") or {}
    flags = feature.get("stage_flags") or {}
    return (
        str(next_step.get("status") or "").lower() in {"agreed", "proposed"}
        or str(stages.get("next_step_attempted") or "").lower() == "yes"
        or str(stages.get("sale_attempted") or "").lower() == "yes"
        or bool(flags.get("next_step_or_sale"))
    )


def _is_active_deal(row: dict[str, Any]) -> bool:
    if row.get("non_sales_interaction"):
        return False
    semantic = str(row.get("deal_stage_semantic_id") or "").upper()
    return semantic not in {"S", "F"}


def _is_working_time(value: Any) -> bool:
    dt = _parse_dt(value)
    if dt is None:
        return True
    return dt.weekday() < 5 and 10 <= dt.hour < 19


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _timestamp(value: Any) -> float:
    dt = _parse_dt(value)
    return dt.timestamp() if dt else 0.0


def _deal_url(deal_id: str, portal_base_url: str) -> str:
    if not deal_id or not portal_base_url:
        return ""
    return f"{portal_base_url.rstrip('/')}/crm/deal/details/{deal_id}/"


def _manager_label(manager_id: Any) -> str:
    value = _clean(manager_id)
    return f"Менеджер #{value}" if value else "Менеджер не указан"


def _report_manager_names(report: dict[str, Any]) -> dict[str, str]:
    refs = report.get("references") or {}
    raw = refs.get("manager_names") or report.get("manager_names") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        _clean(manager_id): _clean(name)
        for manager_id, name in raw.items()
        if _clean(manager_id) and _clean(name)
    }


def _resolve_manager_name(
    manager_id: Any,
    *candidates: Any,
    manager_names: dict[str, str],
) -> str:
    clean_id = _clean(manager_id)
    for candidate in candidates:
        name = _clean(candidate)
        if name and not _is_generated_manager_label(name, clean_id):
            return name
    if clean_id and manager_names.get(clean_id):
        return manager_names[clean_id]
    for candidate in candidates:
        name = _clean(candidate)
        if name:
            return name
    return _manager_label(clean_id)


def _is_generated_manager_label(value: str, manager_id: str) -> bool:
    if not manager_id:
        return False
    normalized = value.strip().lower().replace(" ", "")
    compact_id = manager_id.strip().lower()
    if normalized == compact_id:
        return True
    if normalized in {f"manager#{compact_id}", f"user{compact_id}", f"user#{compact_id}"}:
        return True
    return f"#{compact_id}" in normalized and len(normalized) <= len(compact_id) + 16


def _enrich_manager_rows(rows: Any, manager_names: dict[str, str]) -> Any:
    if not isinstance(rows, list):
        return rows
    enriched = []
    for row in rows:
        if not isinstance(row, dict):
            enriched.append(row)
            continue
        manager_id = _clean(row.get("manager_id"))
        manager_name = _resolve_manager_name(
            manager_id,
            row.get("manager_name"),
            manager_names=manager_names,
        )
        next_row = {**row, "manager_name": manager_name}
        source = row.get("source")
        if isinstance(source, dict):
            next_row["source"] = {**source, "manager_name": manager_name}
        enriched.append(next_row)
    return enriched


def _enrich_alert_rows(rows: Any, manager_names: dict[str, str]) -> Any:
    if not isinstance(rows, list):
        return rows
    enriched = []
    for row in rows:
        if not isinstance(row, dict):
            enriched.append(row)
            continue
        manager_id = _clean(row.get("manager_id"))
        manager_name = _resolve_manager_name(
            manager_id,
            row.get("manager_name"),
            row.get("manager_label"),
            manager_names=manager_names,
        )
        enriched.append({**row, "manager_name": manager_name, "manager_label": manager_name})
    return enriched


def _yes_no(value: Any) -> str:
    return "yes" if str(value or "").lower() == "yes" else "no"


def _minutes(value: Any) -> float | None:
    numeric = _optional_num(value)
    if numeric is None:
        return None
    return round(numeric / 60, 1)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _optional_num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
