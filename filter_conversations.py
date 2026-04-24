"""Filter deal JSON files to keep only client and manager messages."""
import json
from pathlib import Path

INPUT_DIR = Path("export/whatsapp-timeline/conversations")
OUTPUT_DIR = Path("export/whatsapp-timeline/conversations_filtered")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

files = list(INPUT_DIR.glob("deal_*.json"))
print(f"Found {len(files)} files")

for path in files:
    data = json.loads(path.read_text(encoding="utf-8"))
    original_count = len(data.get("messages", []))
    data["messages"] = [
        m for m in data.get("messages", [])
        if m.get("sender_role") in {"client", "manager"}
    ]
    filtered_count = len(data["messages"])
    out_path = OUTPUT_DIR / path.name
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{path.name}: {original_count} -> {filtered_count} messages")

print(f"\nDone. Filtered files saved to: {OUTPUT_DIR}")
