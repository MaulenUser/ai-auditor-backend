"""AggregateFeatureService — cross-call/chat statistics over feature JSON files."""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..ports import JsonSink

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AggregateFeatureRequest:
    features_dir: Path
    output_dir: Path
    limit: int = 0


def _pct(count: int, total: int) -> float:
    return round(count / total * 100, 1) if total else 0.0


def _distribution(values: list[str], total: int) -> dict[str, Any]:
    counts = Counter(values)
    return {
        v: {"count": c, "pct": _pct(c, total)}
        for v, c in sorted(counts.items(), key=lambda x: -x[1])
    }


def _yn_distribution(values: list[str], total: int) -> dict[str, Any]:
    return _distribution(values, total)


class AggregateFeatureService:
    """Reads all feature JSON files in a directory and computes aggregate statistics."""

    def __init__(self, sink: JsonSink) -> None:
        self._sink = sink

    def execute(self, request: AggregateFeatureRequest) -> None:
        feature_files = sorted(request.features_dir.glob("*.json"))
        if request.limit > 0:
            feature_files = feature_files[: request.limit]

        if not feature_files:
            raise FileNotFoundError(
                f"No feature JSON files found in {request.features_dir}"
            )

        features = []
        load_errors = []
        for path in feature_files:
            try:
                features.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                load_errors.append({"file": str(path), "error": str(exc)})

        total = len(features)
        logger.info("Aggregating %d feature files from %s", total, request.features_dir)

        aggregate = self._compute(features, total)
        aggregate["load_errors"] = load_errors
        aggregate["load_errors_count"] = len(load_errors)

        request.output_dir.mkdir(parents=True, exist_ok=True)
        self._sink.write(request.output_dir / "aggregate.json", aggregate)

        logger.info("Aggregation completed.")
        logger.info("Total features analysed: %d", total)
        logger.info("Files saved to %s", request.output_dir.resolve())

    # ------------------------------------------------------------------

    def _compute(self, features: list[dict[str, Any]], total: int) -> dict[str, Any]:
        relevance: list[str] = []
        outcome_status: list[str] = []
        interest_level: list[str] = []
        client_emotion: list[str] = []
        tags_all: list[str] = []

        qual_fields = [
            "need_identified", "budget_discussed",
            "timeline_discussed", "decision_maker_identified",
        ]
        sales_fields = [
            "manager_introduced_self", "manager_asked_questions",
            "manager_presented_service", "manager_rushed_to_pitch",
            "manager_agreed_next_step",
        ]
        qual_vals: dict[str, list[str]] = defaultdict(list)
        sales_vals: dict[str, list[str]] = defaultdict(list)

        quality_flag_counts: Counter = Counter()
        manager_stats: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

        for feat in features:
            relevance.append(str(feat.get("relevance_to_company") or "unknown"))

            outcome = feat.get("outcome") or {}
            outcome_status.append(str(outcome.get("status") or "unknown"))

            sentiment = feat.get("sentiment") or {}
            interest_level.append(str(sentiment.get("client_interest_level") or "unknown"))
            client_emotion.append(str(sentiment.get("client_emotion") or "unknown"))

            tags_all.extend(feat.get("tags") or [])

            qual = feat.get("qualification") or {}
            for f in qual_fields:
                qual_vals[f].append(str(qual.get(f) or "unknown"))

            sales = feat.get("sales_process") or {}
            for f in sales_fields:
                sales_vals[f].append(str(sales.get(f) or "unknown"))

            qf = feat.get("quality_flags") or {}
            for flag, val in qf.items():
                if val is True:
                    quality_flag_counts[flag] += 1

            source = feat.get("source") or {}
            manager_id = str(
                source.get("responsible_id")
                or source.get("assigned_by_id")
                or "unknown"
            )
            manager_stats[manager_id]["outcome_status"].append(
                str(outcome.get("status") or "unknown")
            )
            manager_stats[manager_id]["interest_level"].append(
                str(sentiment.get("client_interest_level") or "unknown")
            )
            manager_stats[manager_id]["relevance"].append(
                str(feat.get("relevance_to_company") or "unknown")
            )

        tag_counter = Counter(tags_all)
        top_tags = [
            {"tag": tag, "count": count, "pct": _pct(count, total)}
            for tag, count in tag_counter.most_common(30)
        ]

        per_manager = {}
        for mgr_id, data in sorted(manager_stats.items()):
            n = len(data["outcome_status"])
            per_manager[mgr_id] = {
                "total": n,
                "outcome_status": _distribution(data["outcome_status"], n),
                "client_interest_level": _distribution(data["interest_level"], n),
                "relevance_to_company": _distribution(data["relevance"], n),
            }

        return {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "total": total,
            "relevance_to_company": _distribution(relevance, total),
            "outcome_status": _distribution(outcome_status, total),
            "sentiment": {
                "client_interest_level": _distribution(interest_level, total),
                "client_emotion": _distribution(client_emotion, total),
            },
            "qualification": {
                f: _yn_distribution(vals, total)
                for f, vals in qual_vals.items()
            },
            "sales_process": {
                f: _yn_distribution(vals, total)
                for f, vals in sales_vals.items()
            },
            "quality_flags": {
                flag: {"count": cnt, "pct": _pct(cnt, total)}
                for flag, cnt in sorted(quality_flag_counts.items(), key=lambda x: -x[1])
            },
            "top_tags": top_tags,
            "per_manager": per_manager,
        }
