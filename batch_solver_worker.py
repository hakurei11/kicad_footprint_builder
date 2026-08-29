from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


sys.dont_write_bytecode = True
PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import batch_pipeline as batch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcription", type=Path, required=True)
    parser.add_argument("--page-text", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    api = batch._plugin_api()
    payload = json.loads(args.transcription.read_text(encoding="utf-8"))
    for dimension in payload.get("dimensions") or []:
        if isinstance(dimension, dict):
            dimension.pop("role_original", None)
    transcription = api.parse_transcription(payload)
    page_text = args.page_text.read_text(encoding="utf-8")
    resolution = batch.solve_all_families(api, transcription, page_text)
    result = resolution.to_dict()
    result["prefill"] = resolution.prefill.to_dict() if resolution.prefill is not None else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": resolution.status, "families": list(resolution.families)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
