# Migration: PowerShell → Python ingestion layer

## What was ported

| PowerShell script | Python module | Entry point |
|---|---|---|
| `bitrix-export.ps1` | `src/bitrix_ingest/cli/export_crm.py` | `bitrix-export-crm` or `python -m bitrix_ingest.cli.export_crm` |
| `bitrix-call-records-scan.ps1` | `src/bitrix_ingest/cli/scan_call_records.py` | `bitrix-scan-call-records` or `python -m bitrix_ingest.cli.scan_call_records` |
| `bitrix-export-whatsapp-timeline.ps1` | `src/bitrix_ingest/cli/export_whatsapp.py` | `bitrix-export-whatsapp` or `python -m bitrix_ingest.cli.export_whatsapp` |

The package is now layered:
- `src/bitrix_ingest/domain/` - pure entities and domain rules
- `src/bitrix_ingest/application/` - use-case orchestration + ports
- `src/bitrix_ingest/infrastructure/` - HTTP, persistence, logging adapters
- `src/bitrix_ingest/cli/` - thin command-line entry points

## Installation

```bash
pip install -e ".[dev]"        # editable install + dev deps (pytest)
pip install -e .               # runtime only
```

Requires Python 3.11+, `requests>=2.31`.

## Running the exporters

### CRM base snapshot

```bash
python -m bitrix_ingest.cli.export_crm \
  --webhook-base-url "https://your-portal.bitrix24.ru/rest/1/yourtoken/" \
  [--output-dir export] \
  [--modified-from "2024-01-01"] \
  [--skip-users] \
  [--skip-activities]
```

Output (same as PS): `export/profile.json`, `users.json`, `deals.json`,
`contacts.json`, `companies.json`, `leads.json`, `activities.json`.

### Call-records scan

```bash
python -m bitrix_ingest.cli.scan_call_records \
  --webhook-base-url "https://your-portal.bitrix24.ru/rest/1/yourtoken/" \
  [--output-dir export/call-records-scan] \
  [--limit 20]
```

Output: `activities.source.json`, `voximplant.raw.json`, `scan-results.json`,
`recording-candidates.json`, `successful-calls.json`, `scan-errors.json`.

### WhatsApp timeline export

```bash
python -m bitrix_ingest.cli.export_whatsapp \
  --webhook-base-url "https://your-portal.bitrix24.ru/rest/1/yourtoken/" \
  [--output-dir export/whatsapp-timeline] \
  [--modified-from "2024-01-01"] \
  [--limit 200] \
  [--deal-ids 42 99 123] \
  [--skip-existing] \
  [--page-delay 0.5]
```

Output: `profile.json`, `deals.source.json`, `conversations/deal_{id}.json`,
`raw/deal_{id}.timeline.json`, `report.json`, `errors.json`.

## Running tests

```bash
pytest
```

---

## Behavioural differences from PowerShell

### Improvements (safe, always active)

#### `retries-added-to-call-scan`
The original `bitrix-call-records-scan.ps1` had **no retry logic** in its
`Invoke-BitrixMethod`. The Python `BitrixClient` adds exponential-backoff retries
(max 4 attempts, delays 1 s / 2 s / 4 s + ±20 % jitter) for HTTP 429/500/502/503/504
and connection timeouts **for all three exporters**.

This is a safe improvement; it does not change output structure.

#### `json-indent`
PowerShell `ConvertTo-Json` uses 4-space indentation; Python uses 2-space.
Downstream scripts that parse JSON are unaffected. If a diff tool compares old
and new files, every line will differ due to indentation alone.

#### `utf8-bom`
PowerShell `Set-Content -Encoding UTF8` on Windows writes a UTF-8 BOM.
Python writes UTF-8 without BOM. Downstream JSON parsers are unaffected
(BOM is whitespace in JSON). Hex-diff of first 3 bytes will differ.

---

### Preserved quirks (intentional, documented)

#### `call-scan-limit`
**Risk level: medium.**

The PS script calls `crm.activity.list` **once** (no pagination) and slices
`Select-Object -First $Limit` from whatever Bitrix returns on that single page
(default max 50 rows per page in Bitrix).

If `$Limit > 50`, the PS script silently returns at most 50 rows because it
never fetches page 2. The Python port preserves this behaviour:

```python
raw_result = response.get("result") or []
activities = raw_result[:limit]
```

**What to do if you need more than 50 recent calls:**
Switch the activity fetch in
`src/bitrix_ingest/application/call_records/scan_service.py`
from `gateway.call()` to `gateway.list_all()`, then apply the limit after full
pagination. This is a small, localised change when you need it.

#### `whatsapp-limit`
**Risk level: low.**

`--limit` in the WhatsApp exporter controls how many WhatsApp deals are
**processed**, not how many are fetched from the API. All deals matching
`--modified-from` are always fetched via full pagination, then filtered
client-side for `SOURCE_ID` starting with `"WZ"` or `TITLE` containing
`"WhatsApp"`, sorted by `DATE_MODIFY DESC`, and finally sliced to `limit`.

This matches the PS script (`Select-Object -First $Limit` ran after a full
`Get-BitrixList`). For portals with a large deal count this can be slow.
A future optimisation would push the WhatsApp filter into the API request
body, but that requires portal-specific field configuration and is out of
scope for this migration.

#### `whatsapp-detection-pattern`
Deals are detected as WhatsApp deals when:
- `SOURCE_ID` starts with `"WZ"` (case-sensitive, matches PS `-like "WZ*"`), **or**
- `TITLE` contains `"WhatsApp"` (case-insensitive, matches PS `-like "*WhatsApp*"`)

If your portal uses a different SOURCE_ID prefix or title pattern, adjust the
filter in `src/bitrix_ingest/application/whatsapp/deal_filter.py`.

#### `voximplant-raw-response-shape`
`voximplant.raw.json` stores the full parsed API response under the `"Response"`
key (including `result`, `time`, etc.), matching the PS `$statResponse` object.
Downstream consumers that only read `Response.result` are unaffected.
