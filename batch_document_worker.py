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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if payload.get("schema") != "batch_document_worker_input_v1":
        raise ValueError("unsupported document worker input")
    document = batch.BatchDocument(
        document_index=int(payload["document_index"]),
        pdf_path=Path(payload["pdf_path"]),
        expected_sha256=str(payload["expected_sha256"]),
        tier=str(payload.get("tier") or ""),
    )
    registry_path = (
        Path(payload["canonical_registry_path"]).expanduser().resolve()
        if payload.get("canonical_registry_path")
        else None
    )
    if registry_path is not None:
        expected_registry_sha256 = str(
            payload.get("canonical_registry_sha256") or ""
        ).upper()
        actual_registry_sha256 = batch.sha256_file(registry_path)
        if not expected_registry_sha256 or actual_registry_sha256 != expected_registry_sha256:
            raise ValueError("canonical registry hash mismatch")
    c3_terms_path = (
        Path(payload["c3_terms_path"]).expanduser().resolve()
        if payload.get("c3_terms_path")
        else None
    )
    if c3_terms_path is not None:
        expected_c3_sha256 = str(payload.get("c3_terms_sha256") or "").upper()
        actual_c3_sha256 = batch.sha256_file(c3_terms_path)
        if not expected_c3_sha256 or actual_c3_sha256 != expected_c3_sha256:
            raise ValueError("C3 terms hash mismatch")
    runner = batch.BatchLiveRunner(
        batch.BatchConfig(
            document_concurrency=2,
            model_sample_concurrency=3,
            page_suggestion_limit=3,
            dry_run=False,
            canonical_registry_path=registry_path,
            c3_terms_path=c3_terms_path,
        )
    )
    record = runner._process_document_local(document, args.output_root.resolve(), resume=False)
    print(
        json.dumps(
            {"record_id": record.get("record_id"), "status": record.get("status")},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
