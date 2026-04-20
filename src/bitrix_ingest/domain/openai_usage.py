"""OpenAI usage-tracking helpers — mirrors openai-usage-utils.ps1."""
from __future__ import annotations

from typing import Any


def extract_usage_event(
    response: dict[str, Any],
    *,
    stage: str,
    entity_type: str,
    entity_id: str,
    source_file_path: str,
    model: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    usage = response.get("usage") or {}
    if not usage:
        return None
    in_details = usage.get("input_token_details") or {}
    out_details = usage.get("output_token_details") or {}
    record: dict[str, Any] = {
        "stage": stage,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source_file_path": source_file_path,
        "model": model,
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "total_tokens": int(usage.get("total_tokens", 0)),
        "audio_input_tokens": int(
            in_details.get("audio_input_tokens", 0) or in_details.get("audio", 0)
        ),
        "text_input_tokens": int(
            in_details.get("text_input_tokens", 0) or in_details.get("text", 0)
        ),
        "cached_tokens": int(in_details.get("cached_tokens", 0)),
        "reasoning_tokens": int(out_details.get("reasoning_tokens", 0)),
    }
    if extra:
        record.update(extra)
    return record


def summarize_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total_requests": len(events),
        "total_input_tokens": sum(e.get("input_tokens", 0) for e in events),
        "total_output_tokens": sum(e.get("output_tokens", 0) for e in events),
        "total_tokens": sum(e.get("total_tokens", 0) for e in events),
        "total_audio_input_tokens": sum(e.get("audio_input_tokens", 0) for e in events),
        "total_cached_tokens": sum(e.get("cached_tokens", 0) for e in events),
    }
