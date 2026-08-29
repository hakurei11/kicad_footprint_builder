from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence


PLUGIN_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PLUGIN_DIR / "vendor"
for search_path in (PLUGIN_DIR, VENDOR_DIR):
    text = str(search_path)
    if text not in sys.path:
        sys.path.insert(0, text)

from auto_page_selector import suggest_pdf_pages  # noqa: E402
from cross_page_body import (  # noqa: E402
    body_missing,
    complete_cross_page_body,
    merge_body_into_geometry,
)
from canonical_footprint_matcher import (  # noqa: E402
    load_registry as load_canonical_registry,
    match_candidate as match_canonical_candidate,
)
from c3_standard_package_matcher import (  # noqa: E402
    SOURCE_MARKER as C3_SOURCE_MARKER,
    evaluate_c3_fallback,
    load_dictionary as load_c3_dictionary,
)


MODEL_SAMPLE_REQUESTS = 3
DEFAULT_DOCUMENT_CONCURRENCY = 4
MODEL_SAMPLE_CONCURRENCY_ENV = "KICAD_FP_MODEL_SAMPLE_CONCURRENCY"


def _environment_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


MODEL_SAMPLE_CONCURRENCY = _environment_int(
    MODEL_SAMPLE_CONCURRENCY_ENV,
    MODEL_SAMPLE_REQUESTS,
)
DEFAULT_MODEL_SAMPLE_CONCURRENCY = MODEL_SAMPLE_CONCURRENCY


@dataclass(frozen=True)
class BatchConfig:
    document_concurrency: int = DEFAULT_DOCUMENT_CONCURRENCY
    model_sample_concurrency: int = DEFAULT_MODEL_SAMPLE_CONCURRENCY
    page_suggestion_limit: int = 3
    dry_run: bool = True
    canonical_registry_path: Path | None = None
    c3_terms_path: Path | None = None

    def __post_init__(self) -> None:
        if self.document_concurrency < 1:
            raise ValueError("document_concurrency must be at least 1")
        if not 1 <= self.model_sample_concurrency <= MODEL_SAMPLE_REQUESTS:
            raise ValueError(
                "model_sample_concurrency must be between 1 and "
                f"{MODEL_SAMPLE_REQUESTS}"
            )
        if self.page_suggestion_limit < 1:
            raise ValueError("page_suggestion_limit must be at least 1")
        if (
            self.canonical_registry_path is not None
            and not self.canonical_registry_path.expanduser().resolve().is_file()
        ):
            raise ValueError(
                f"canonical registry does not exist: {self.canonical_registry_path}"
            )
        if (
            self.c3_terms_path is not None
            and not self.c3_terms_path.expanduser().resolve().is_file()
        ):
            raise ValueError(f"C3 terms do not exist: {self.c3_terms_path}")

    @property
    def hard_model_request_ceiling(self) -> int:
        return self.document_concurrency * MODEL_SAMPLE_REQUESTS

    @property
    def active_model_request_ceiling(self) -> int:
        return self.document_concurrency * self.model_sample_concurrency


@dataclass(frozen=True)
class BatchDocument:
    document_index: int
    pdf_path: Path
    expected_sha256: str = ""
    tier: str = ""
    truth_land_pages: tuple[int, ...] = ()
    source_record: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelSampleOutcome:
    sample_index: int
    status: str
    value: Any = None
    error_type: str = ""
    error_message: str = ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _redact_text(value: str) -> str:
    text = str(value)
    for environment_name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "VLM_API_KEY", "VLM_BASE_URL"):
        secret = os.environ.get(environment_name, "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9._-]{12,}", "[REDACTED_KEY]", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text, flags=re.I)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(_redact_value(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _iter_pdf_paths(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
        if path.is_file() and path.suffix.lower() == ".pdf":
            yield path.resolve()


def discover_unique_documents(root: Path) -> tuple[list[BatchDocument], dict[str, int]]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"PDF root does not exist: {root}")
    physical_count = 0
    seen: set[str] = set()
    documents: list[BatchDocument] = []
    for path in _iter_pdf_paths(root):
        physical_count += 1
        digest = sha256_file(path)
        if digest in seen:
            continue
        seen.add(digest)
        documents.append(BatchDocument(len(documents) + 1, path, digest))
    return documents, {
        "physical_pdf_count": physical_count,
        "unique_sha256_count": len(documents),
        "duplicate_pdf_count": physical_count - len(documents),
    }


def load_documents_from_ledger(
    ledger_path: Path,
) -> tuple[list[BatchDocument], dict[str, int]]:
    ledger_path = ledger_path.expanduser().resolve()
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    rows = payload.get("documents")
    if not isinstance(rows, list):
        raise ValueError("source ledger must contain a documents array")

    documents: list[BatchDocument] = []
    seen: set[str] = set()
    duplicate_count = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("source ledger document rows must be objects")
        digest = str(row.get("document_sha256") or "").strip().upper()
        path_text = str(row.get("canonical_path") or row.get("pdf_path") or "").strip()
        if not digest or not path_text:
            raise ValueError("source ledger row lacks document_sha256 or path")
        if digest in seen:
            duplicate_count += 1
            continue
        seen.add(digest)
        documents.append(
            BatchDocument(
                document_index=len(documents) + 1,
                pdf_path=Path(path_text).expanduser().resolve(),
                expected_sha256=digest,
            )
        )
    return documents, {
        "physical_pdf_count": len(rows),
        "unique_sha256_count": len(documents),
        "duplicate_pdf_count": duplicate_count,
    }


class ModelSampleCoordinator:
    """Bound submitted and running model work independently of corpus size."""

    def __init__(self, config: BatchConfig):
        self.config = config
        self._slots = threading.BoundedSemaphore(config.active_model_request_ceiling)
        self._executor = ThreadPoolExecutor(
            max_workers=config.active_model_request_ceiling,
            thread_name_prefix="footprint-model",
        )
        self._lock = threading.Lock()
        self._active = 0
        self._peak_active = 0
        self._submitted = 0

    @property
    def peak_active(self) -> int:
        with self._lock:
            return self._peak_active

    @property
    def submitted(self) -> int:
        with self._lock:
            return self._submitted

    def _call(
        self,
        sample_callable: Callable[[int], Any],
        sample_index: int,
    ) -> ModelSampleOutcome:
        with self._lock:
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)
        try:
            return ModelSampleOutcome(
                sample_index=sample_index,
                status="ok",
                value=sample_callable(sample_index),
            )
        except Exception as exc:
            return ModelSampleOutcome(
                sample_index=sample_index,
                status="failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        finally:
            with self._lock:
                self._active -= 1

    def _release_slot(self, _future: Future[ModelSampleOutcome]) -> None:
        self._slots.release()

    def run_three_samples(
        self,
        sample_callable: Callable[[int], Any],
    ) -> tuple[ModelSampleOutcome, ...]:
        next_index = 1
        pending: dict[Future[ModelSampleOutcome], int] = {}
        outcomes: list[ModelSampleOutcome] = []
        while next_index <= MODEL_SAMPLE_REQUESTS or pending:
            while (
                next_index <= MODEL_SAMPLE_REQUESTS
                and len(pending) < self.config.model_sample_concurrency
            ):
                self._slots.acquire()
                future = self._executor.submit(self._call, sample_callable, next_index)
                future.add_done_callback(self._release_slot)
                pending[future] = next_index
                with self._lock:
                    self._submitted += 1
                next_index += 1
            done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in done:
                pending.pop(future)
                outcomes.append(future.result())
        return tuple(sorted(outcomes, key=lambda item: item.sample_index))

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)


Selector = Callable[[str | Path, int], dict[str, object]]


class BatchDryRunner:
    def __init__(
        self,
        config: BatchConfig,
        *,
        selector: Selector = suggest_pdf_pages,
    ):
        if not config.dry_run:
            raise ValueError("this candidate exposes dry-run only")
        self.config = config
        self.selector = selector
        self._max_pending_documents = 0
        self._model_call_count = 0

    def _process_document(self, document: BatchDocument) -> dict[str, Any]:
        base: dict[str, Any] = {
            "document_index": document.document_index,
            "pdf_path": str(document.pdf_path),
            "expected_document_sha256": document.expected_sha256,
            "document_sha256": "",
            "hash_verified": False,
            "selected_page": None,
            "page_class": None,
            "fallback": None,
            "status": "selection_failed",
            "model_sample_requests": MODEL_SAMPLE_REQUESTS,
            "model_sample_concurrency": self.config.model_sample_concurrency,
            "model_call_count": 0,
            "selector_parse_status": None,
            "selector_parse_error": None,
            "failure_reason": "",
            "error_type": "",
        }
        try:
            actual_sha256 = sha256_file(document.pdf_path)
            base["document_sha256"] = actual_sha256
            if document.expected_sha256 and actual_sha256 != document.expected_sha256:
                base["status"] = "input_hash_mismatch"
                base["failure_reason"] = (
                    f"expected {document.expected_sha256}, observed {actual_sha256}"
                )
                return base
            base["hash_verified"] = True

            selection = self.selector(
                document.pdf_path,
                self.config.page_suggestion_limit,
            )
            base["selector_parse_status"] = selection.get("parse_status")
            base["selector_parse_error"] = selection.get("parse_error")
            suggestions = selection.get("suggestions")
            if not isinstance(suggestions, list) or not suggestions:
                base["status"] = "no_candidate"
                base["failure_reason"] = "page selector returned no candidates"
                return base
            selected = suggestions[0]
            if not isinstance(selected, dict):
                raise ValueError("page selector returned a non-object suggestion")
            base["selected_page"] = int(selected["page"])
            base["page_class"] = str(selected.get("page_class") or "none")
            base["fallback"] = bool(selected.get("fallback"))
            base["status"] = "pending_model"
            return base
        except Exception as exc:
            base["status"] = "selection_failed"
            base["error_type"] = type(exc).__name__
            base["failure_reason"] = str(exc)
            return base

    def run(
        self,
        documents: Sequence[BatchDocument],
        output_dir: Path,
        *,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        output_dir = output_dir.expanduser().resolve()
        if output_dir.exists() and any(output_dir.iterdir()):
            raise ValueError(f"output directory is not empty: {output_dir}")
        records_dir = output_dir / "records"
        records_dir.mkdir(parents=True, exist_ok=True)

        results: list[dict[str, Any]] = []
        iterator = iter(documents)
        pending: dict[Future[dict[str, Any]], BatchDocument] = {}

        def submit_next(executor: ThreadPoolExecutor) -> bool:
            try:
                document = next(iterator)
            except StopIteration:
                return False
            future = executor.submit(self._process_document, document)
            pending[future] = document
            self._max_pending_documents = max(
                self._max_pending_documents,
                len(pending),
            )
            return True

        with ThreadPoolExecutor(
            max_workers=self.config.document_concurrency,
            thread_name_prefix="footprint-document",
        ) as executor:
            while len(pending) < self.config.document_concurrency and submit_next(executor):
                pass
            while pending:
                done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in done:
                    document = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {
                            "document_index": document.document_index,
                            "pdf_path": str(document.pdf_path),
                            "expected_document_sha256": document.expected_sha256,
                            "document_sha256": "",
                            "hash_verified": False,
                            "selected_page": None,
                            "page_class": None,
                            "fallback": None,
                            "status": "worker_failed",
                            "model_sample_requests": MODEL_SAMPLE_REQUESTS,
                            "model_sample_concurrency": self.config.model_sample_concurrency,
                            "model_call_count": 0,
                            "selector_parse_status": None,
                            "selector_parse_error": None,
                            "failure_reason": str(exc),
                            "error_type": type(exc).__name__,
                        }
                    results.append(result)
                    digest = result.get("document_sha256") or document.expected_sha256
                    record_name = (
                        f"{document.document_index:04d}_"
                        f"{str(digest)[:16] or 'no_sha'}.json"
                    )
                    _write_json(records_dir / record_name, result)
                    submit_next(executor)

        results.sort(key=lambda row: int(row["document_index"]))
        status_counts = Counter(str(row["status"]) for row in results)
        summary = {
            "schema": "kicad_footprint_batch_dry_run_v1",
            "dry_run": True,
            "source": source or {},
            "input_document_count": len(documents),
            "processed_document_count": len(results),
            "pending_model_count": status_counts.get("pending_model", 0),
            "zero_candidate_count": status_counts.get("no_candidate", 0),
            "failed_document_count": sum(
                count
                for status, count in status_counts.items()
                if status not in {"pending_model", "no_candidate"}
            ),
            "fallback_document_count": sum(row.get("fallback") is True for row in results),
            "parse_error_document_count": sum(
                row.get("selector_parse_status") == "parse_error" for row in results
            ),
            "status_counts": dict(sorted(status_counts.items())),
            "model_call_count": self._model_call_count,
            "document_concurrency": self.config.document_concurrency,
            "max_pending_documents_observed": self._max_pending_documents,
            "model_sample_requests_per_document": MODEL_SAMPLE_REQUESTS,
            "model_sample_concurrency": self.config.model_sample_concurrency,
            "model_sample_concurrency_source": (
                "cli_or_config; environment fallback=" + MODEL_SAMPLE_CONCURRENCY_ENV
            ),
            "hard_model_request_ceiling": self.config.hard_model_request_ceiling,
            "active_model_request_ceiling": self.config.active_model_request_ceiling,
            "request_limit_scales_with_document_count": False,
            "batch_interrupted": False,
            "all_documents_accounted_for": len(results) == len(documents),
        }

        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / "batch_results.jsonl").open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            for result in results:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        with (output_dir / "model_queue.jsonl").open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            for result in results:
                if result["status"] == "pending_model":
                    handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        _write_json(output_dir / "batch_summary.json", summary)
        return summary


LIVE_TIERS = frozenset({"A_有推荐焊盘页", "B_无焊盘页"})
LEGACY_FAMILY_ORDER = ("CHIP", "SMX", "SOT3", "ASYM3")
FAMILY_ORDER = LEGACY_FAMILY_ORDER + ("INLINE3", "GRID4", "DUAL", "QUAD_EP")
RUN_ESTIMATE_LINE = (
    "预计：86 份 PDF；文档并发 2 × 每件 3 路 = 6 在途；"
    "约 1.3 小时；约 100 万输出 token。"
)
BATCH_SOLVER_TIMEOUT_SECONDS = 120.0
BATCH_DOCUMENT_TIMEOUT_SECONDS = 900.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_bom_feasibility_documents(
    ledger_path: Path,
    pdf_root: Path,
) -> tuple[list[BatchDocument], dict[str, Any]]:
    """Load the frozen 86-row A+B corpus without content-hash deduplication."""

    ledger_path = ledger_path.expanduser().resolve()
    pdf_root = pdf_root.expanduser().resolve()
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "bom89_batch_feasibility_v1":
        raise ValueError("source ledger is not bom89_batch_feasibility_v1")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("source ledger lacks records")
    documents: list[BatchDocument] = []
    excluded: list[dict[str, str]] = []
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("source ledger contains a non-object record")
        tier = str(row.get("tier") or "")
        if tier not in LIVE_TIERS:
            excluded.append({"pdf": str(row.get("pdf") or ""), "tier": tier})
            continue
        pdf_name = str(row.get("pdf") or "").strip()
        digest = str(row.get("sha256") or "").strip().upper()
        if not pdf_name or not digest:
            raise ValueError("source ledger record lacks pdf or sha256")
        documents.append(
            BatchDocument(
                document_index=len(documents) + 1,
                pdf_path=(pdf_root / pdf_name).resolve(),
                expected_sha256=digest,
                tier=tier,
                truth_land_pages=tuple(int(value) for value in row.get("truth_land_pages") or ()),
                source_record=dict(row),
            )
        )
    if len(documents) != 86:
        raise ValueError(f"frozen A+B corpus must contain 86 rows, observed {len(documents)}")
    return documents, {
        "kind": "frozen_bom_feasibility_ledger",
        "ledger_path": str(ledger_path),
        "ledger_sha256": sha256_file(ledger_path),
        "pdf_root": str(pdf_root),
        "physical_record_count": len(records),
        "included_record_count": len(documents),
        "excluded_record_count": len(excluded),
        "excluded_records": excluded,
        "content_unique_count": len({item.expected_sha256 for item in documents}),
        "physical_rows_preserved": True,
    }


def _plugin_api() -> Any:
    import importlib

    return importlib.import_module("kicad_footprint_builder_plugin")


def required_pad_fields(family: str) -> tuple[str, ...]:
    fields = ["pad_x", "pad_y", "center_x"]
    if family in {"SOT3", "GRID4", "DUAL"}:
        fields.append("pitch_y")
        if family == "DUAL":
            fields.extend(("dual_left_count", "dual_right_count"))
    elif family == "ASYM3":
        fields.extend(("pitch_y", "tab_x", "tab_y"))
    elif family == "QUAD_EP":
        fields.extend(
            (
                "pitch_y",
                "tab_x",
                "tab_y",
                "quad_left_count",
                "quad_right_count",
            )
        )
    return tuple(fields)


def required_geometry_fields(family: str) -> tuple[str, ...]:
    return required_pad_fields(family) + ("body_x", "body_y")


def _finite_positive(value: Any) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0


def _geometry_from_prefill(api: Any, family: str, prefill: Any) -> Any:
    values = prefill.values
    missing = [field for field in required_pad_fields(family) if not _finite_positive(values.get(field))]
    if missing:
        raise ValueError("missing pad fields: " + ",".join(missing))
    quad_has_top_bottom = family == "QUAD_EP" and bool(
        values.get("quad_top_count") or values.get("quad_bottom_count")
    )
    if quad_has_top_bottom:
        quad_missing = [
            field
            for field in ("center_y", "pitch_x", "quad_top_count", "quad_bottom_count")
            if not _finite_positive(values.get(field))
        ]
        if quad_missing:
            raise ValueError("missing four-sided QUAD/EP fields: " + ",".join(quad_missing))
    body_complete = all(_finite_positive(values.get(field)) for field in ("body_x", "body_y"))
    geometry = api.FootprintGeometry(
        family=family,
        pad_x=float(values["pad_x"]),
        pad_y=float(values["pad_y"]),
        center_x=float(values["center_x"]),
        center_y=float(values.get("center_y") or 0.0),
        pitch_y=float(values.get("pitch_y") or 0.0),
        pitch_x=float(values.get("pitch_x") or 0.0),
        body_x=float(values["body_x"]) if body_complete else None,
        body_y=float(values["body_y"]) if body_complete else None,
        body_source=prefill.body_source if body_complete else "absent_requires_user_input",
        tab_x=float(values.get("tab_x") or 0.0),
        tab_y=float(values.get("tab_y") or 0.0),
        dual_left_count=int(values.get("dual_left_count") or 0),
        dual_right_count=int(values.get("dual_right_count") or 0),
        quad_left_count=int(values.get("quad_left_count") or 0),
        quad_right_count=int(values.get("quad_right_count") or 0),
        quad_top_count=int(values.get("quad_top_count") or 0),
        quad_bottom_count=int(values.get("quad_bottom_count") or 0),
    )
    geometry.validate()
    return geometry


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


def geometry_signature(geometry: Any) -> tuple[Any, ...]:
    """Pad identity excludes family label and optional body evidence."""

    signature = [
        (
            str(pad.number),
            _rounded(pad.x),
            _rounded(pad.y),
            _rounded(pad.size_x),
            _rounded(pad.size_y),
        )
        for pad in geometry.pads()
    ]
    if geometry.family == "QUAD_EP":
        signature.append(
            (
                "EP",
                0.0,
                0.0,
                _rounded(geometry.tab_x),
                _rounded(geometry.tab_y),
            )
        )
    return tuple(sorted(signature))


def _numbered_ep_count_tolerance(
    decision: Any,
    pin_count: int | None,
) -> dict[str, Any]:
    """Allow only the documented convention where a numbered EP adds one pin."""

    pad_count = getattr(decision, "solder_land_pad_count", None)
    summaries = tuple(getattr(decision, "land_size_summary", ()) or ())
    ep_positive = (
        str(getattr(decision, "evidence_family", "") or "") == "QFN/DFN/EP"
        and any(str(item).startswith("ep_positive_evidence=") for item in summaries)
    )
    count_matches = False
    if pad_count is not None and pin_count is not None:
        count_matches = int(pin_count) == int(pad_count) + 1
    return {
        "applicable": bool(ep_positive and count_matches),
        "ep_positive_evidence": bool(ep_positive),
        "peripheral_solder_land_count": pad_count,
        "numbered_pin_count": pin_count,
        "count_relation": "pin_count == peripheral_solder_land_count + 1",
    }


def evaluate_candidate_family_gate(
    decision: Any,
    resolved_families: Sequence[str],
    pin_count: int | None,
) -> dict[str, Any]:
    """Require the deterministic family gate before any candidate write."""

    decision_status = str(getattr(decision, "status", "") or "")
    auto_family = getattr(decision, "auto_family", None)
    pad_count = getattr(decision, "solder_land_pad_count", None)
    numbered_ep = _numbered_ep_count_tolerance(decision, pin_count)
    numbered_ep_applied = bool(
        numbered_ep["applicable"] and set(resolved_families) == {"QUAD_EP"}
    )
    contradictions: list[str] = []
    if (
        pad_count is not None
        and pin_count is not None
        and int(pad_count) != int(pin_count)
        and not numbered_ep["applicable"]
    ):
        contradictions.append(
            f"solder_land_pad_count={int(pad_count)} 与 pin_count={int(pin_count)} 矛盾；"
            "必须人工确认族与拓扑。"
        )

    allowed = True
    reasons: list[str] = []
    if decision_status != "auto" and not numbered_ep_applied:
        allowed = False
        reasons.append(
            f"确定性族闸状态为 {decision_status or 'missing'}，不是 auto；"
            "几何唯一解不得覆盖族闸。"
        )
    if auto_family is None and not numbered_ep_applied:
        allowed = False
        reasons.append("确定性族闸没有 auto_family；拓扑未定，不得生成候选 footprint。")
    elif auto_family is not None and auto_family not in set(resolved_families):
        allowed = False
        reasons.append(
            f"族闸自动族 {auto_family} 不在几何解族 {list(resolved_families)} 中；"
            "两路证据不一致，转人工。"
        )
    reasons.extend(contradictions)
    if numbered_ep_applied:
        reasons.append(
            "EP 正证据在场，QUAD_EP 几何唯一，且 pin_count 恰为周边 solder_land "
            "身份数 + 1；按编号 EP 计脚容差放行。"
        )
    elif allowed:
        reasons.append(
            f"确定性族闸 auto={auto_family}，且与几何解族 {list(resolved_families)} 一致。"
        )
    return {
        "schema": "batch_candidate_family_gate_v1",
        "status": "passed" if allowed else "manual_required",
        "candidate_allowed": allowed,
        "decision_status": decision_status,
        "auto_family": auto_family,
        "resolved_families": list(resolved_families),
        "solder_land_pad_count": pad_count,
        "pin_count": pin_count,
        "numbered_ep_count_tolerance": {
            **numbered_ep,
            "applied": numbered_ep_applied,
        },
        "contradictions": contradictions,
        "reason": " ".join(reasons),
    }


@dataclass
class MultiFamilyResolution:
    status: str
    families: tuple[str, ...]
    geometry: Any | None
    prefill: Any | None
    normalized_transcription: Any | None
    trial_rows: list[dict[str, Any]]
    body_values: dict[str, float | None]
    body_sources: dict[str, str]
    body_axis_provenance: dict[str, Any]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "batch_multi_family_resolution_v1",
            "rule": (
                "new-family topology auto -> solve only that new family; otherwise try "
                "CHIP/SMX/SOT3/ASYM3 and accept only one deduplicated geometry group; "
                "INLINE3/GRID4/DUAL/QUAD_EP remain topology-gated"
            ),
            "model_proposal_used_for_selection": False,
            "status": self.status,
            "families": list(self.families),
            "geometry": asdict(self.geometry) if self.geometry is not None else None,
            "trial_rows": self.trial_rows,
            "body_values": self.body_values,
            "body_sources": self.body_sources,
            "body_axis_provenance": self.body_axis_provenance,
            "reason": self.reason,
        }


def solve_all_families(api: Any, transcription: Any, page_text: str) -> MultiFamilyResolution:
    groups: dict[tuple[Any, ...], list[tuple[str, Any, Any, Any]]] = {}
    topology_partial_groups: dict[
        tuple[Any, ...], list[tuple[str, Any, Any]]
    ] = {}
    decimal_guard_partial_groups: dict[
        tuple[Any, ...], list[tuple[str, Any, Any]]
    ] = {}
    trial_rows: list[dict[str, Any]] = []
    body_candidates: dict[tuple[float, float], list[tuple[Any, Any]]] = {}
    deterministic_decision = api.decide_family(transcription, page_text)
    numbered_ep = _numbered_ep_count_tolerance(
        deterministic_decision,
        getattr(transcription, "pin_count", None),
    )
    if deterministic_decision.status == "reject_ep" and not numbered_ep["applicable"]:
        return MultiFamilyResolution(
            status="reject_ep",
            families=(),
            geometry=None,
            prefill=None,
            normalized_transcription=None,
            trial_rows=[],
            body_values={"body_x": None, "body_y": None},
            body_sources={},
            body_axis_provenance={},
            reason=deterministic_decision.reason,
        )
    if (
        deterministic_decision.evidence_family in {"DUAL", "DUAL/GRID4"}
        and deterministic_decision.status != "auto"
    ):
        return MultiFamilyResolution(
            status=deterministic_decision.status,
            families=(),
            geometry=None,
            prefill=None,
            normalized_transcription=None,
            trial_rows=[],
            body_values={"body_x": None, "body_y": None},
            body_sources={},
            body_axis_provenance={},
            reason=deterministic_decision.reason,
        )
    if deterministic_decision.status == "reject_ep" and numbered_ep["applicable"]:
        trial_families = ("QUAD_EP",)
    elif (
        deterministic_decision.status == "auto"
        and deterministic_decision.auto_family in {"INLINE3", "GRID4", "DUAL", "QUAD_EP"}
    ):
        trial_families = (deterministic_decision.auto_family,)
    else:
        trial_families = LEGACY_FAMILY_ORDER
    for family in trial_families:
        normalized, role_normalization = api.normalize_roles_for_family(transcription, family)
        try:
            prefill = api.infer_geometry_prefill(normalized, family, page_text=page_text)
            geometry = None
            geometry_error = ""
            if prefill.auto_pads:
                try:
                    geometry = _geometry_from_prefill(api, family, prefill)
                except Exception as exc:
                    geometry_error = _redact_text(exc)
            if all(_finite_positive(prefill.values.get(field)) for field in ("body_x", "body_y")):
                key = (
                    round(float(prefill.values["body_x"]), 4),
                    round(float(prefill.values["body_y"]), 4),
                )
                body_candidates.setdefault(key, []).append((prefill, normalized))
            if geometry is not None:
                groups.setdefault(geometry_signature(geometry), []).append(
                    (family, geometry, prefill, normalized)
                )
            elif prefill.status == "partial_auto":
                partial_key = tuple(
                    (
                        field,
                        round(float(value), 6)
                        if _finite_positive(value)
                        else None,
                    )
                    for field, value in sorted(prefill.values.items())
                )
                topology_partial_groups.setdefault(partial_key, []).append(
                    (family, prefill, normalized)
                )
            elif prefill.decimal_extension_guard.get("suspicious_fields"):
                partial_key = tuple(
                    (
                        field,
                        round(float(value), 6)
                        if _finite_positive(value)
                        else None,
                    )
                    for field, value in sorted(prefill.values.items())
                )
                decimal_guard_partial_groups.setdefault(partial_key, []).append(
                    (family, prefill, normalized)
                )
            trial_rows.append(
                {
                    "family": family,
                    "role_normalization": role_normalization,
                    "prefill": prefill.to_dict(),
                    "pad_solution": geometry is not None,
                    "geometry": asdict(geometry) if geometry is not None else None,
                    "geometry_signature": list(geometry_signature(geometry)) if geometry is not None else None,
                    "geometry_error": geometry_error,
                }
            )
        except Exception as exc:
            trial_rows.append(
                {
                    "family": family,
                    "pad_solution": False,
                    "error_type": type(exc).__name__,
                    "error": _redact_text(exc),
                }
            )

    body_values: dict[str, float | None] = {"body_x": None, "body_y": None}
    body_sources: dict[str, str] = {}
    body_axis_provenance: dict[str, Any] = {}
    if len(body_candidates) == 1:
        (body_x, body_y), candidates = next(iter(body_candidates.items()))
        body_values = {"body_x": body_x, "body_y": body_y}
        representative = candidates[0][0]
        body_sources = {
            field: str(representative.sources.get(field) or "document")
            for field in ("body_x", "body_y")
        }
        body_axis_provenance = dict(representative.body_axis_provenance)

    if len(groups) == 1:
        members = next(iter(groups.values()))
        representative = members[0]
        if len(body_candidates) == 1:
            (body_pair, _) = next(iter(body_candidates.items()))
            body_member = next(
                (
                    member
                    for member in members
                    if all(
                        _finite_positive(member[2].values.get(field))
                        for field in ("body_x", "body_y")
                    )
                    and (
                        round(float(member[2].values["body_x"]), 4),
                        round(float(member[2].values["body_y"]), 4),
                    )
                    == body_pair
                ),
                None,
            )
            if body_member is not None:
                representative = body_member
        return MultiFamilyResolution(
            status="unique_geometry",
            families=tuple(item[0] for item in members),
            geometry=representative[1],
            prefill=representative[2],
            normalized_transcription=representative[3],
            trial_rows=trial_rows,
            body_values=body_values,
            body_sources=body_sources,
            body_axis_provenance=body_axis_provenance,
            reason="八族中所有可解结果去重后仅一组；CHIP/SMX 同几何时保留双标签。",
        )
    if len(groups) > 1:
        return MultiFamilyResolution(
            status="multi_family_conflict",
            families=(),
            geometry=None,
            prefill=None,
            normalized_transcription=None,
            trial_rows=trial_rows,
            body_values=body_values,
            body_sources=body_sources,
            body_axis_provenance=body_axis_provenance,
            reason=f"八族形成 {len(groups)} 组不同焊盘几何；按规则整件转人工，不挑选。",
        )
    if len(topology_partial_groups) == 1:
        members = next(iter(topology_partial_groups.values()))
        representative = members[0]
        return MultiFamilyResolution(
            status="partial_auto",
            families=tuple(item[0] for item in members),
            geometry=None,
            prefill=representative[1],
            normalized_transcription=representative[2],
            trial_rows=trial_rows,
            body_values=body_values,
            body_sources=body_sources,
            body_axis_provenance=body_axis_provenance,
            reason=(
                "确定性拓扑已选中 QUAD_EP，并保留所有可证焊盘值；"
                "中心距仍需人工补齐，不生成候选 footprint。"
            ),
        )
    if len(decimal_guard_partial_groups) == 1:
        members = next(iter(decimal_guard_partial_groups.values()))
        representative = members[0]
        return MultiFamilyResolution(
            status="partial_auto_decimal_extension_review",
            families=tuple(item[0] for item in members),
            geometry=None,
            prefill=representative[1],
            normalized_transcription=representative[2],
            trial_rows=trial_rows,
            body_values=body_values,
            body_sources=body_sources,
            body_axis_provenance=body_axis_provenance,
            reason=(
                "小数末位延伸疑点闸仅清空受影响字段；其余自动值保留，"
                "不生成候选 footprint，等待人工补齐。"
            ),
        )
    return MultiFamilyResolution(
        status="no_pad_solution",
        families=(),
        geometry=None,
        prefill=None,
        normalized_transcription=None,
        trial_rows=trial_rows,
        body_values=body_values,
        body_sources=body_sources,
        body_axis_provenance=body_axis_provenance,
        reason="八族均未形成完整焊盘解；不生成退化 footprint。",
    )


def solve_all_families_isolated(
    transcription_path: Path,
    page_text_path: Path,
    output_path: Path,
    diagnostics_path: Path,
) -> dict[str, Any]:
    """Run the unchanged solver in a killable child so one case cannot stall the batch."""

    worker = PLUGIN_DIR / "batch_solver_worker.py"
    child_env = os.environ.copy()
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "VLM_API_KEY", "VLM_BASE_URL"):
        child_env.pop(name, None)
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable,
        "-B",
        str(worker),
        "--transcription",
        str(transcription_path),
        "--page-text",
        str(page_text_path),
        "--output",
        str(output_path),
    ]
    started = time.perf_counter()
    diagnostics: dict[str, Any] = {
        "schema": "batch_solver_process_v1",
        "isolated_process": True,
        "credentials_forwarded": False,
        "timeout_seconds": BATCH_SOLVER_TIMEOUT_SECONDS,
        "timed_out": False,
        "return_code": None,
        "wall_clock_seconds": None,
        "stdout": "",
        "stderr": "",
    }
    try:
        completed = subprocess.run(
            command,
            cwd=str(PLUGIN_DIR),
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=BATCH_SOLVER_TIMEOUT_SECONDS,
        )
        diagnostics["return_code"] = completed.returncode
        diagnostics["stdout"] = _redact_text(completed.stdout)[-4000:]
        diagnostics["stderr"] = _redact_text(completed.stderr)[-4000:]
    except subprocess.TimeoutExpired as exc:
        diagnostics["timed_out"] = True
        diagnostics["stdout"] = _redact_text(exc.stdout or "")[-4000:]
        diagnostics["stderr"] = _redact_text(exc.stderr or "")[-4000:]
    finally:
        diagnostics["wall_clock_seconds"] = round(time.perf_counter() - started, 3)
        _write_json(diagnostics_path, diagnostics)
    if diagnostics["timed_out"]:
        return {
            "status": "solver_timeout_manual_review",
            "families": [],
            "geometry": None,
            "prefill": None,
            "body_values": {"body_x": None, "body_y": None},
            "body_sources": {},
            "body_axis_provenance": {},
            "reason": "七族求解超过资源隔离时限；本件转人工，未选择任何解。",
        }
    if diagnostics["return_code"] != 0 or not output_path.is_file():
        return {
            "status": "solver_process_failed_manual_review",
            "families": [],
            "geometry": None,
            "prefill": None,
            "body_values": {"body_x": None, "body_y": None},
            "body_sources": {},
            "body_axis_provenance": {},
            "reason": "隔离求解进程失败；本件转人工，未选择任何解。",
        }
    return json.loads(output_path.read_text(encoding="utf-8"))


def _field_provenance(
    api: Any,
    field: str,
    value: float | None,
    source: str,
    transcription: Any,
    page: int,
) -> dict[str, Any]:
    raw_matches: list[dict[str, Any]] = []
    if value is not None:
        for dimension in transcription.dimensions:
            try:
                dimension_mm = api.dimension_value_mm(dimension)
            except Exception:
                continue
            if math.isclose(float(value), float(dimension_mm), rel_tol=0.0, abs_tol=0.011):
                raw_matches.append(asdict(dimension))
    return {
        "field": field,
        "value_mm": value,
        "source_page_1based": page,
        "solver_source": source,
        "raw_source_candidates": raw_matches,
    }


def _slug(value: str, fallback: str = "DATASHEET") -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.+-]+", "_", value).strip("_.")
    return (normalized or fallback)[:48]


def _candidate_name(document: BatchDocument, digest: str) -> str:
    return f"BATCH_{document.document_index:03d}_{_slug(document.pdf_path.stem)}_{digest[:8]}"[:80]


def route_candidate_output(
    api: Any,
    *,
    record: dict[str, Any],
    candidate_footprint_text: str,
    canonical_registry: dict[str, Any] | None,
    candidate_path: Path,
    name: str,
    geometry: Any,
    source_sha256: str,
) -> dict[str, Any]:
    canonical_match = match_canonical_candidate(
        record,
        candidate_footprint_text,
        canonical_registry,
    )
    if bool(canonical_match.get("suppress_candidate_file")):
        return {
            "canonical_match": canonical_match,
            "candidate_written": False,
            "candidate_path": None,
            "status": "canonical_match_ready_for_review",
        }
    api.write_candidate(
        candidate_path,
        name,
        geometry,
        source_sha256=source_sha256,
        anchor_status="batch_unconfirmed_review_required",
    )
    return {
        "canonical_match": canonical_match,
        "candidate_written": True,
        "candidate_path": candidate_path,
        "status": "candidate_ready_for_review",
    }


def _manual_input_bounds(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    families = tuple(record.get("accepted_families") or ())
    if families:
        family = families[0]
        required = list(required_geometry_fields(family))
        if family == "QUAD_EP" and bool(
            values.get("quad_top_count") or values.get("quad_bottom_count")
        ):
            required.extend(("center_y", "pitch_x", "quad_top_count", "quad_bottom_count"))
        missing = [field for field in required if not _finite_positive(values.get(field))]
        family_confirmation = len(families) != 1
        return {
            "exact_numeric": len(missing),
            "min_numeric": len(missing),
            "max_numeric": len(missing),
            "family_confirmation_required": family_confirmation,
            "missing_fields": missing,
        }
    body_missing = sum(not _finite_positive(values.get(field)) for field in ("body_x", "body_y"))
    return {
        "exact_numeric": None,
        "min_numeric": 3 + body_missing,
        "max_numeric": 6 + body_missing,
        "family_confirmation_required": True,
        "missing_fields": ["family", "pad_geometry"]
        + [field for field in ("body_x", "body_y") if not _finite_positive(values.get(field))],
    }


def _relative_link(from_dir: Path, target: Path) -> str:
    return Path(os.path.relpath(target, from_dir)).as_posix()


def _write_card_html(record: dict[str, Any], card_path: Path, output_root: Path) -> None:
    card_path.parent.mkdir(parents=True, exist_ok=True)
    values = record.get("values") or {}
    provenance_by_field = {
        str(item.get("field")): item for item in record.get("value_provenance") or []
    }
    rows: list[str] = []
    for field in (
        "pad_x", "pad_y", "center_x", "center_y", "pitch_y", "pitch_x",
        "tab_x", "tab_y", "body_x", "body_y", "dual_left_count",
        "dual_right_count", "quad_left_count", "quad_right_count",
        "quad_top_count", "quad_bottom_count",
    ):
        value = values.get(field)
        provenance = provenance_by_field.get(field, {})
        raw_candidates = provenance.get("raw_source_candidates") or []
        raw_text = "；".join(
            html.escape(str(item.get("raw") or item.get("symbol") or ""))
            for item in raw_candidates[:4]
        )
        source = html.escape(str(provenance.get("solver_source") or ""))
        page = provenance.get("source_page_1based")
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(field)}</code></td>"
            f"<td>{'' if value is None else html.escape(str(value))}</td>"
            f"<td>{'' if page is None else 'p.' + html.escape(str(page))}</td>"
            f"<td>{source}</td><td>{raw_text}</td></tr>"
        )
    source_image = record.get("source_image_path")
    preview_image = record.get("preview_path")
    source_html = (
        f'<img src="{html.escape(_relative_link(card_path.parent, output_root / source_image))}" alt="所选原图">'
        if source_image
        else '<div class="empty">无可用页图</div>'
    )
    preview_html = (
        f'<img src="{html.escape(_relative_link(card_path.parent, output_root / preview_image))}" alt="footprint 预览">'
        if preview_image
        else '<div class="empty">未生成预览</div>'
    )
    queue_link = html.escape(_relative_link(card_path.parent, output_root / "review_queue.html"))
    missing = record.get("manual_input_bounds", {}).get("missing_fields") or []
    warnings = record.get("warnings") or []
    accepted = record.get("accepted_families") or []
    family_decision = record.get("family_decision") or {}
    is_quad_ep = "QUAD_EP" in accepted or family_decision.get("auto_family") == "QUAD_EP"
    ep_coverage_html = ""
    if is_quad_ep and _finite_positive(values.get("tab_x")) and _finite_positive(values.get("tab_y")):
        ep_x = float(values["tab_x"])
        ep_y = float(values["tab_y"])
        scale = math.sqrt(0.70)
        paste_x = ep_x * scale
        paste_y = ep_y * scale
        area_ratio = paste_x * paste_y / (ep_x * ep_y)
        ep_coverage_html = (
            '<h2>EP 焊膏暂行规则</h2><div class="notice">'
            + html.escape(
                f"单块居中、同形状；EP {ep_x:.6g}×{ep_y:.6g} mm；"
                f"paste {paste_x:.6g}×{paste_y:.6g} mm；"
                f"双轴比例 sqrt(0.70)={scale:.9f}；面积覆盖率={area_ratio:.6f}。"
                "暂行规则，确认前须目视核对。"
            )
            + "</div>"
        )
    pin_mapping = record.get("pin_mapping_evidence") or {}
    pin_mapping_status = str(pin_mapping.get("status") or "not_applicable")
    pin_mapping_reason = str(pin_mapping.get("reason") or "无")
    pin_mapping_values = pin_mapping.get("mapping") or {}
    pin_mapping_text = "；".join(
        f"{key}→{value}" for key, value in sorted(pin_mapping_values.items())
    ) or "未建立编号映射"
    canonical_match = record.get("canonical_match") or {}
    canonical_status = str(canonical_match.get("status") or "registry_not_configured")
    canonical_reason = str(canonical_match.get("reason") or "")
    canonical_entry_id = str(
        canonical_match.get("canonical_entry_id")
        or (canonical_match.get("near_canonical") or {}).get("canonical_entry_id")
        or "—"
    )
    pad_comparison = (
        canonical_match.get("pad_comparison")
        or (canonical_match.get("near_canonical") or {}).get("pad_comparison")
        or {}
    )
    difference_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(str(item.get('field') or ''))}</code></td>"
        f"<td>{html.escape(str(item.get('document_value')))}</td>"
        f"<td>{html.escape(str(item.get('canonical_value')))}</td>"
        f"<td>{html.escape(str(item.get('delta_mm', '—')))}</td>"
        "</tr>"
        for item in (pad_comparison.get("differences") or [])
    ) or '<tr><td colspan="4">全部 pad 字段在 ±0.001 mm 内一致，或未配置正典比较。</td></tr>'
    provenance_rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(str(item.get('field') or ''))}</code></td>"
        f"<td>{html.escape(str((item.get('document') or {}).get('value')))}</td>"
        f"<td>{html.escape(str(((item.get('document') or {}).get('provenance') or {}).get('solver_source') or ''))}</td>"
        f"<td>{html.escape(str((item.get('canonical') or {}).get('value')))}</td>"
        f"<td>{html.escape(str(((item.get('canonical') or {}).get('provenance') or {}).get('solver_source') or ''))}</td>"
        "</tr>"
        for item in (canonical_match.get("geometry_provenance_comparison") or [])
    ) or '<tr><td colspan="5">无正典映射字段，或仍走普通新建流程。</td></tr>'
    canonical_html = (
        '<h2>标准封装复用审核</h2><div class="notice">'
        + html.escape(
            f"{canonical_status} · {canonical_entry_id} · {canonical_reason}"
        )
        + "</div>"
        + '<table><thead><tr><th>pad 字段</th><th>文档候选</th><th>正典</th><th>差值 mm</th></tr></thead>'
        + f"<tbody>{difference_rows}</tbody></table>"
        + '<h2>文档值与正典值出处</h2><table><thead><tr><th>字段</th><th>文档值</th><th>文档出处</th><th>正典值</th><th>正典出处</th></tr></thead>'
        + f"<tbody>{provenance_rows}</tbody></table>"
    )
    c3_match = record.get("c3_package_match") or {}
    c3_status = str(c3_match.get("status") or "dictionary_not_configured")
    c3_html = ""
    if c3_status in {
        "c3_package_recognized_missing_canonical",
        "c3_body_conflict_manual_review",
        "c3_canonical_mapping_ready_for_review",
    }:
        c3_lookup = c3_match.get("canonical_lookup") or {}
        c3_canonical = c3_lookup.get("canonical_entry") or {}
        c3_body = json.dumps(
            c3_match.get("body_cross_check") or {},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        c3_html = (
            '<h2>C3 标准封装识别</h2>'
            f'<div class="source-marker">{html.escape(str(c3_match.get("source_marker") or C3_SOURCE_MARKER))}</div>'
            '<table><tbody>'
            f'<tr><th>状态</th><td>{html.escape(c3_status)}</td></tr>'
            f'<tr><th>识别封装</th><td>{html.escape(str(c3_match.get("package_name") or "—"))}</td></tr>'
            f'<tr><th>原文页</th><td>{html.escape(str(c3_match.get("source_page_1based") or "—"))}</td></tr>'
            f'<tr><th>原文行</th><td>{html.escape(str(c3_match.get("source_line") or "—"))}</td></tr>'
            f'<tr><th>正典 ID</th><td>{html.escape(str(c3_canonical.get("entry_id") or "库内缺正典"))}</td></tr>'
            f'<tr><th>正典文件</th><td>{html.escape(str(c3_canonical.get("formal_path") or "不建、不猜"))}</td></tr>'
            '</tbody></table>'
            f'<h2>本体交叉核验（仅拒绝）</h2><pre>{html.escape(c3_body)}</pre>'
        )
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(str(record.get('record_id') or '审核卡'))}</title>
<style>
body{{margin:0;font-family:"Segoe UI","Microsoft YaHei",sans-serif;color:#1f2933;background:#f4f6f8}}
header{{padding:18px 24px;background:#fff;border-bottom:1px solid #d8dee5;display:flex;justify-content:space-between;gap:16px;align-items:center}}
main{{max-width:1500px;margin:0 auto;padding:20px 24px 40px}} h1{{font-size:20px;margin:0;letter-spacing:0}} h2{{font-size:16px;margin:22px 0 10px;letter-spacing:0}}
.status{{font-weight:600;color:#8a3b12}} .split{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.figure{{background:#fff;border:1px solid #d8dee5;border-radius:4px;overflow:hidden;min-height:260px}}
.figure h2{{margin:0;padding:10px 12px;border-bottom:1px solid #e5e9ee}} img{{display:block;width:100%;height:auto;max-height:680px;object-fit:contain;background:#fff}}
.empty{{min-height:260px;display:grid;place-items:center;color:#66737f}} table{{width:100%;border-collapse:collapse;background:#fff}}
th,td{{border:1px solid #d8dee5;padding:8px 9px;text-align:left;vertical-align:top;font-size:13px}} th{{background:#edf1f4}}
.meta{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}} .meta div{{background:#fff;border-left:3px solid #52616b;padding:9px}}
.notice{{padding:10px 12px;background:#fff3bf;border-left:4px solid #d69e00;margin:10px 0}} code{{font-family:Consolas,monospace}}
.source-marker{{padding:10px 12px;background:#e9f5ee;border-left:4px solid #287d4f;margin:10px 0;font-weight:600}} pre{{white-space:pre-wrap;background:#fff;border:1px solid #d8dee5;padding:12px}}
a{{color:#075f9b}} ul{{margin:6px 0 0;padding-left:20px}} @media(max-width:900px){{.split,.meta{{grid-template-columns:1fr}}}}
</style></head><body>
<header><h1>{html.escape(str(record.get('record_id') or ''))} · {html.escape(str(record.get('pdf_name') or ''))}</h1><a href="{queue_link}">审核队列</a></header>
<main><div class="meta">
<div>状态<br><span class="status">{html.escape(str(record.get('status') or ''))}</span></div>
<div>选页<br>{html.escape(str(record.get('selected_page') or '—'))}</div>
<div>候选族<br>{html.escape(', '.join(accepted) or '待人工')}</div>
<div>几何解族<br>{html.escape(', '.join(record.get('geometry_solution_families') or []) or '无')}</div>
<div>族闸<br>{html.escape(str((record.get('family_gate') or {}).get('status', '—')))}</div>
<div>焊盘编号<br>{html.escape(pin_mapping_status)}</div>
<div>锚定<br>仅提示 · {html.escape(str((record.get('anchor') or {}).get('can_generate', '—')))}</div>
</div>
<div class="split"><section class="figure"><h2>原图</h2>{source_html}</section><section class="figure"><h2>生成预览</h2>{preview_html}</section></div>
{ep_coverage_html}
{canonical_html}
{c3_html}
<h2>几何与出处</h2><table><thead><tr><th>字段</th><th>mm</th><th>页</th><th>求解出处</th><th>原文候选</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>焊盘编号证据</h2><div class="notice">{html.escape(pin_mapping_status)} · {html.escape(pin_mapping_reason)} · {html.escape(pin_mapping_text)}</div>
<h2>待人工</h2><div class="notice">{html.escape('、'.join(str(item) for item in missing) or '无缺项；仍须人工确认后入库')}</div>
<h2>失败或降级原因</h2><div class="notice">{html.escape(str(record.get('failure_reason') or record.get('resolution_reason') or '无'))}</div>
<h2>警告</h2><ul>{''.join('<li>'+html.escape(str(item))+'</li>' for item in warnings) or '<li>无</li>'}</ul>
</main></body></html>"""
    card_path.write_text(body, encoding="utf-8", newline="\n")


def _write_review_outputs(output_root: Path, records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    sorted_records = sorted(records, key=lambda row: int(row["document_index"]))
    queue_rows: list[dict[str, Any]] = []
    list_rows: list[str] = []
    for record in sorted_records:
        item_dir = output_root / str(record["item_dir"])
        card_path = item_dir / "card.html"
        _write_card_html(record, card_path, output_root)
        record_path = item_dir / "record.json"
        _write_json(record_path, record)
        queue_row = {
            "record_id": record["record_id"],
            "document_index": record["document_index"],
            "pdf_name": record.get("pdf_name"),
            "document_sha256": record.get("document_sha256"),
            "status": record.get("status"),
            "review_status": record.get("review_status", "pending"),
            "accepted_families": record.get("accepted_families") or [],
            "candidate_path": record.get("candidate_path"),
            "canonical_match_status": str(
                (record.get("canonical_match") or {}).get("status")
                or "registry_not_configured"
            ),
            "canonical_entry_id": (
                (record.get("canonical_match") or {}).get("canonical_entry_id")
                or (
                    (record.get("canonical_match") or {}).get("near_canonical")
                    or {}
                ).get("canonical_entry_id")
            ),
            "canonical_mapping_review_required": bool(
                (record.get("canonical_match") or {}).get("mapping_review_required")
            ),
            "c3_package_match_status": str(
                (record.get("c3_package_match") or {}).get("status")
                or "dictionary_not_configured"
            ),
            "c3_package_name": (record.get("c3_package_match") or {}).get("package_name"),
            "c3_source_page_1based": (record.get("c3_package_match") or {}).get("source_page_1based"),
            "c3_source_line": (record.get("c3_package_match") or {}).get("source_line"),
            "c3_source_marker": (record.get("c3_package_match") or {}).get("source_marker"),
            "pin_mapping_status": str(
                (record.get("pin_mapping_evidence") or {}).get("status") or "not_applicable"
            ),
            "record_path": _relative_link(output_root, record_path),
            "card_path": _relative_link(output_root, card_path),
            "manual_input_bounds": record.get("manual_input_bounds"),
        }
        queue_rows.append(queue_row)
        list_rows.append(
            "<tr>"
            f"<td>{int(record['document_index']):03d}</td>"
            f"<td><a href=\"{html.escape(queue_row['card_path'])}\">{html.escape(str(record.get('pdf_name') or ''))}</a></td>"
            f"<td>{html.escape(str(record.get('status') or ''))}</td>"
            f"<td>{html.escape(', '.join(record.get('accepted_families') or []) or '待人工')}</td>"
            f"<td>{html.escape(queue_row['canonical_match_status'])}</td>"
            f"<td>{html.escape(queue_row['c3_package_match_status'])}</td>"
            f"<td>{html.escape(queue_row['pin_mapping_status'])}</td>"
            f"<td>{html.escape('、'.join(record.get('manual_input_bounds', {}).get('missing_fields') or []))}</td>"
            "</tr>"
        )
    queue = {
        "schema": "kicad_footprint_review_queue_v1",
        "created_at": utc_now(),
        "confirmation_required_before_library_commit": True,
        "records": queue_rows,
    }
    _write_json(output_root / "review_queue.json", queue)
    index_html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>批量 footprint 审核队列</title><style>
body{{margin:0;font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f4f6f8;color:#1f2933}} header{{background:#fff;border-bottom:1px solid #d8dee5;padding:18px 24px}}
main{{max-width:1500px;margin:auto;padding:20px 24px}} h1{{font-size:22px;margin:0;letter-spacing:0}} table{{width:100%;border-collapse:collapse;background:#fff}}
th,td{{border:1px solid #d8dee5;padding:8px 9px;text-align:left;font-size:13px}} th{{background:#edf1f4;position:sticky;top:0}} a{{color:#075f9b}}
</style></head><body><header><h1>批量 footprint 审核队列</h1></header><main><table><thead><tr><th>#</th><th>资料</th><th>状态</th><th>候选族</th><th>正典复用</th><th>C3 封装</th><th>焊盘编号</th><th>待人工</th></tr></thead><tbody>{''.join(list_rows)}</tbody></table></main></body></html>"""
    (output_root / "review_queue.html").write_text(index_html, encoding="utf-8", newline="\n")
    return queue


def model_config_from_environment(api: Any, timeout_seconds: float) -> Any:
    base_url = (os.environ.get("OPENAI_BASE_URL") or os.environ.get("VLM_BASE_URL") or "").strip()
    api_key = (os.environ.get("OPENAI_API_KEY") or os.environ.get("VLM_API_KEY") or "").strip()
    model_name = (os.environ.get("OPENAI_MODEL") or os.environ.get("VLM_MODEL") or api.MODEL_NAME).strip()
    if not base_url or not api_key:
        raise RuntimeError("模型连接配置未通过子进程环境注入；未发起请求。")
    allow_insecure = (os.environ.get("E1A_ALLOW_INSECURE_HTTP") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    config = api.ModelConfig(
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
        allow_insecure_http=allow_insecure,
    )
    config.validate()
    return config


class BatchLiveRunner:
    def __init__(self, config: BatchConfig, *, selector: Selector = suggest_pdf_pages):
        if config.dry_run:
            raise ValueError("live runner requires dry_run=False")
        if config.document_concurrency != 2:
            raise ValueError("frozen v1 run requires document_concurrency=2")
        if config.model_sample_concurrency != 3:
            raise ValueError("frozen v1 run requires three concurrent samples per document")
        self.config = config
        self.selector = selector
        self.api = _plugin_api()
        if int(self.api.MODEL_SAMPLE_CONCURRENCY) != 3:
            raise RuntimeError("plugin model sample concurrency is not the frozen value 3")
        self.model_config = model_config_from_environment(self.api, 240.0)
        self.canonical_registry = load_canonical_registry(
            config.canonical_registry_path
        )
        self.c3_dictionary = load_c3_dictionary(config.c3_terms_path)
        self._max_pending_documents = 0

    def _base_record(self, document: BatchDocument, item_dir: Path) -> dict[str, Any]:
        return {
            "schema": "kicad_footprint_batch_record_v1",
            "record_id": f"BATCH-{document.document_index:03d}-{document.expected_sha256[:8]}",
            "document_index": document.document_index,
            "pdf_name": document.pdf_path.name,
            "pdf_path": str(document.pdf_path),
            "expected_document_sha256": document.expected_sha256,
            "document_sha256": "",
            "hash_verified": False,
            "tier": document.tier,
            "truth_metadata_not_used_for_selection": True,
            "selected_page": None,
            "page_class": None,
            "selector_fallback": None,
            "source_image_path": None,
            "source_image_sha256": "",
            "status": "started",
            "review_status": "pending",
            "model": str(self.model_config.model_name),
            "fixture_used": False,
            "model_sample_requests": 3,
            "model_sample_concurrency": 3,
            "model_call_count": 0,
            "family_proposal": None,
            "family_decision": None,
            "family_gate": None,
            "multi_family_status": None,
            "accepted_families": [],
            "geometry_solution_families": [],
            "values": {
                field: None
                for field in (
                    "pad_x", "pad_y", "center_x", "center_y", "pitch_y", "pitch_x",
                    "tab_x", "tab_y", "body_x", "body_y", "dual_left_count",
                    "dual_right_count", "quad_left_count", "quad_right_count",
                    "quad_top_count", "quad_bottom_count",
                )
            },
            "value_sources": {"auto": {}, "codex_manual": {}, "user_manual": {}},
            "value_provenance": [],
            "canonical_match": {
                "status": "registry_not_configured",
                "suppress_candidate_file": False,
                "mapping_review_required": False,
            },
            "c3_package_match": {
                "status": "dictionary_not_configured",
                "model_call_count": 0,
                "source_marker": None,
                "mapping_review_required": False,
            },
            "candidate_path": None,
            "preview_path": None,
            "anchor": None,
            "warnings": [],
            "failure_reason": "",
            "error_type": "",
            "item_dir": _relative_link(item_dir.parents[1], item_dir),
            "started_at": utc_now(),
            "finished_at": None,
            "batch_pipeline_sha256": sha256_file(Path(__file__).resolve()),
        }

    def _apply_c3_fallback(
        self,
        record: dict[str, Any],
        document: BatchDocument,
    ) -> None:
        if not record.get("hash_verified"):
            return
        canonical_status = str((record.get("canonical_match") or {}).get("status") or "")
        existing_candidate_count = int(
            bool(record.get("candidate_path"))
            or (
                bool((record.get("canonical_match") or {}).get("mapping_review_required"))
                and canonical_status != "registry_not_configured"
            )
        )
        result = evaluate_c3_fallback(
            document.pdf_path,
            self.c3_dictionary,
            self.canonical_registry,
            document_values=dict(record.get("values") or {}),
            existing_candidate_count=existing_candidate_count,
        )
        record["c3_package_match"] = result
        status = str(result.get("status") or "")
        if status == "skipped_existing_candidate":
            return
        if status == "c3_package_recognized_missing_canonical":
            record["status"] = status
            record["failure_reason"] = str(result.get("card_message") or result.get("reason") or "")
            record["warnings"].append(C3_SOURCE_MARKER)
            return
        if status == "c3_body_conflict_manual_review":
            record["status"] = status
            record["failure_reason"] = str(result.get("card_message") or result.get("reason") or "")
            record["warnings"].append(C3_SOURCE_MARKER)
            return
        if status == "c3_canonical_mapping_ready_for_review":
            lookup = result.get("canonical_lookup") or {}
            canonical_entry = lookup.get("canonical_entry") or {}
            record["canonical_match"] = {
                "status": status,
                "reason": result.get("reason"),
                "suppress_candidate_file": True,
                "mapping_review_required": True,
                "canonical_entry_id": canonical_entry.get("entry_id"),
                "canonical_path": canonical_entry.get("formal_path"),
                "document_package_code": result.get("package_name"),
                "document_package_code_evidence": result.get("evidence"),
                "body_cross_check": result.get("body_cross_check"),
                "source_marker": C3_SOURCE_MARKER,
            }
            record["status"] = "canonical_match_ready_for_review"
            record["failure_reason"] = ""
            record["warnings"].append(C3_SOURCE_MARKER)

    def _process_document_local(self, document: BatchDocument, output_root: Path, resume: bool) -> dict[str, Any]:
        item_dir = output_root / "items" / f"{document.document_index:03d}_{document.expected_sha256[:16]}"
        item_dir.mkdir(parents=True, exist_ok=True)
        record_path = item_dir / "record.json"
        if resume and record_path.is_file():
            return json.loads(record_path.read_text(encoding="utf-8"))
        record = self._base_record(document, item_dir)
        try:
            digest = sha256_file(document.pdf_path)
            record["document_sha256"] = digest
            if digest != document.expected_sha256:
                record["status"] = "input_hash_mismatch"
                record["failure_reason"] = f"expected {document.expected_sha256}, observed {digest}"
                return record
            record["hash_verified"] = True

            selection = self.selector(document.pdf_path, self.config.page_suggestion_limit)
            suggestions = selection.get("suggestions")
            if not isinstance(suggestions, list) or not suggestions:
                record["status"] = "no_page_candidate"
                record["failure_reason"] = "自动选页没有返回候选页。"
                return record
            selected = suggestions[0]
            if not isinstance(selected, dict):
                raise ValueError("page selector returned a non-object suggestion")
            selected_page = int(selected["page"])
            record["selected_page"] = selected_page
            record["page_class"] = str(selected.get("page_class") or "none")
            record["selector_fallback"] = bool(selected.get("fallback"))
            record["selector_suggestions"] = suggestions

            source_page = self.api.prepare_source_page(document.pdf_path, selected_page, item_dir)
            source_relative = _relative_link(output_root, source_page.image_path)
            record["source_image_path"] = source_relative
            record["source_image_sha256"] = sha256_file(source_page.image_path)
            record["page_text_extractor"] = source_page.text_extractor
            (item_dir / "page_text.txt").write_text(source_page.page_text, encoding="utf-8", newline="\n")

            model_started = time.perf_counter()
            model_result = self.api.transcribe_image(source_page.image_path, self.model_config)
            record["model_wall_clock_seconds"] = round(time.perf_counter() - model_started, 3)
            record["model_call_count"] = 3
            record["request_id_present"] = bool(model_result.request_id)
            record["sample_request_id_presence"] = [bool(value) for value in model_result.sample_request_ids]
            record["consensus"] = model_result.consensus_stats
            for index, sample in enumerate(model_result.sample_transcriptions, start=1):
                _write_json(item_dir / f"model_sample_{index:02d}_transcription.json", sample.to_dict())
            for index, raw in enumerate(model_result.sample_raw_responses, start=1):
                _write_json(item_dir / f"model_sample_{index:02d}_raw_response.json", raw)

            transcription = self.api.apply_page_pattern_evidence(
                model_result.transcription, source_page.page_text
            )
            transcription, text_bound_folding = self.api.fold_page_text_min_max_bounds(
                transcription, source_page.page_text
            )
            if transcription.discarded_dimensions:
                record["discarded_dimension_records"] = [
                    dict(item) for item in transcription.discarded_dimensions
                ]
                record["warnings"].append(
                    f"已逐记录丢弃 {len(transcription.discarded_dimensions)} 条无效尺寸；"
                    "其余尺寸继续处理。"
                )
            transcription_path = item_dir / "transcription.json"
            page_text_path = item_dir / "page_text.txt"
            _write_json(transcription_path, transcription.to_dict())
            _write_json(item_dir / "text_bound_folding.json", text_bound_folding)
            decision = self.api.decide_family(transcription, source_page.page_text)
            record["family_proposal"] = transcription.family_proposal
            record["family_decision"] = decision.to_dict()
            record["pin_mapping_evidence"] = self.api.new_family_pin_mapping_evidence(
                transcription,
                decision.auto_family or "",
            )

            resolution_path = item_dir / "multi_family_resolution.json"
            resolution = solve_all_families_isolated(
                transcription_path,
                page_text_path,
                resolution_path,
                item_dir / "solver_process.json",
            )
            if not resolution_path.is_file():
                _write_json(resolution_path, resolution)
            record["multi_family_status"] = str(resolution.get("status") or "")
            resolution_families = list(resolution.get("families") or [])
            record["geometry_solution_families"] = resolution_families
            family_gate = evaluate_candidate_family_gate(
                decision,
                resolution_families,
                transcription.pin_count,
            )
            record["family_gate"] = family_gate
            record["accepted_families"] = (
                resolution_families if family_gate["candidate_allowed"] else []
            )
            record["resolution_reason"] = str(resolution.get("reason") or "")
            geometry_payload = resolution.get("geometry")
            prefill = resolution.get("prefill")
            if isinstance(geometry_payload, dict) and isinstance(prefill, dict):
                geometry = self.api.FootprintGeometry(**geometry_payload)
                geometry.validate()
                values = dict(prefill.get("values") or {})
                record["values"].update(values)
                auto_sources = {
                    field: str((prefill.get("sources") or {}).get(field) or "")
                    for field, value in values.items()
                    if value is not None
                }
                record["value_sources"]["auto"] = auto_sources
                record["warnings"] = list(prefill.get("warnings") or [])
                record["body_source"] = str(prefill.get("body_source") or "")
                record["body_axis_provenance"] = dict(prefill.get("body_axis_provenance") or {})
                record["decimal_extension_guard"] = dict(
                    prefill.get("decimal_extension_guard") or {}
                )
                record["auto_pads"] = bool(prefill.get("auto_pads"))
                record["auto_full"] = bool(prefill.get("auto_full"))
                cross_page_result = None
                land_page_warnings = list(record["warnings"])
                if family_gate["candidate_allowed"] and body_missing(values):
                    combination = str(prefill.get("combination") or "")
                    if combination:
                        try:
                            cross_page_result = complete_cross_page_body(
                                self.api,
                                document.pdf_path,
                                selected_page,
                                geometry.family,
                                combination,
                                values,
                                item_dir / "cross_page_body",
                                self.model_config,
                            )
                        except Exception as exc:
                            record["m4_cross_page_body"] = {
                                "schema": "m4_cross_page_body_result_v1",
                                "status": "optional_stage_exception_body_left_manual",
                                "model_call_count": 0,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            }
                            record["land_page_warnings"] = land_page_warnings
                            record["warnings"].append(
                                "M4 可选跨页本体阶段异常；已保留页内焊盘解，本体留人工。"
                            )
                            cross_page_result = None
                        if cross_page_result is None:
                            pass
                        else:
                            record["model_call_count"] += cross_page_result.model_call_count
                            record["m4_cross_page_body"] = cross_page_result.to_dict()
                            record["land_page_warnings"] = land_page_warnings
                        cross_root = item_dir / "cross_page_body"
                        for index, sample in enumerate(
                            () if cross_page_result is None else
                            cross_page_result.sample_transcriptions,
                            start=1,
                        ):
                            _write_json(
                                cross_root / f"body_sample_{index:02d}_transcription.json",
                                sample.to_dict(),
                            )
                        for index, raw in enumerate(
                            () if cross_page_result is None else
                            cross_page_result.raw_responses,
                            start=1,
                        ):
                            _write_json(
                                cross_root / f"body_sample_{index:02d}_raw_response.json",
                                raw,
                            )
                        if cross_page_result is not None and cross_page_result.transcription is not None:
                            _write_json(
                                cross_root / "body_transcription.json",
                                cross_page_result.transcription.to_dict(),
                            )
                        if cross_page_result is not None and cross_page_result.status == "body_completed":
                            geometry = merge_body_into_geometry(
                                self.api,
                                geometry,
                                float(cross_page_result.body_x),
                                float(cross_page_result.body_y),
                            )
                            values["body_x"] = cross_page_result.body_x
                            values["body_y"] = cross_page_result.body_y
                            auto_sources.update(cross_page_result.sources)
                            record["values"].update(
                                {
                                    "body_x": cross_page_result.body_x,
                                    "body_y": cross_page_result.body_y,
                                }
                            )
                            record["value_sources"]["auto"] = auto_sources
                            record["body_source"] = "document"
                            record["body_axis_provenance"] = dict(
                                cross_page_result.provenance
                            )
                            record["auto_full"] = bool(record["auto_pads"])
                            record["warnings"] = [
                                warning
                                for warning in record["warnings"]
                                if "本体尺寸未定" not in warning
                                and "body 轴向来源无法" not in warning
                            ]
                            record["warnings"].append(
                                "M4 已由同一 PDF 的包代码绑定外形页补齐本体；"
                                "焊盘与 EP 字段保持逐项相同。"
                            )
                        elif cross_page_result is not None:
                            record["warnings"].extend(cross_page_result.warnings)
                    else:
                        record["m4_cross_page_body"] = {
                            "schema": "m4_cross_page_body_result_v1",
                            "status": "not_triggered_missing_land_combination",
                            "model_call_count": 0,
                        }
                anchor_values = dict(values)
                if cross_page_result is not None and cross_page_result.status == "body_completed":
                    anchor_values["body_x"] = None
                    anchor_values["body_y"] = None
                anchor = self.api.anchor_used_geometry_values(
                    transcription,
                    anchor_values,
                    source_kind=source_page.source_kind,
                    page_text=source_page.page_text,
                )
                record["anchor"] = anchor.to_dict()
                if cross_page_result is not None and cross_page_result.status == "body_completed":
                    body_page = int(cross_page_result.selection.body_page_1based or 0)
                    body_text_path = (
                        item_dir
                        / "cross_page_body"
                        / f"body_page_{body_page:04d}"
                        / "page_text.txt"
                    )
                    body_page_text = (
                        body_text_path.read_text(encoding="utf-8")
                        if body_text_path.is_file()
                        else ""
                    )
                    body_anchor = self.api.anchor_used_geometry_values(
                        cross_page_result.transcription,
                        {
                            "body_x": cross_page_result.body_x,
                            "body_y": cross_page_result.body_y,
                        },
                        source_kind="pdf",
                        page_text=body_page_text,
                    )
                    record["m4_cross_page_body"]["body_anchor"] = body_anchor.to_dict()
                record["warnings"].extend(family_gate["contradictions"])
                for check in anchor.checks:
                    if check.get("anchored") is False:
                        record["warnings"].append(
                            f"{check.get('field')}={check.get('value_mm')} 未能在文本层复核；"
                            "请人工看图确认。"
                        )
                for field, value in values.items():
                    if value is None:
                        continue
                    field_transcription = transcription
                    field_page = selected_page
                    if (
                        field in {"body_x", "body_y"}
                        and cross_page_result is not None
                        and cross_page_result.status == "body_completed"
                    ):
                        field_transcription = cross_page_result.transcription
                        field_page = int(
                            cross_page_result.selection.body_page_1based or selected_page
                        )
                    record["value_provenance"].append(
                        _field_provenance(
                            self.api,
                            field,
                            float(value),
                            auto_sources.get(field, ""),
                            field_transcription,
                            field_page,
                        )
                    )
                name = _candidate_name(document, digest)
                preview_path = item_dir / "preview.png"
                self.api.render_footprint_png(geometry, preview_path, name=name)
                record["preview_path"] = _relative_link(output_root, preview_path)
                if geometry.family == "QUAD_EP":
                    paste_x, paste_y = self.api._thermal_paste_size(
                        self.api.Pad(
                            "EP",
                            0.0,
                            0.0,
                            geometry.tab_x,
                            geometry.tab_y,
                            is_thermal=True,
                        )
                    )
                    record["ep_paste_rule"] = {
                        "status": "provisional_user_confirmed_rule",
                        "shape_count": 1,
                        "centered": True,
                        "same_shape_as_ep": True,
                        "scale_each_axis": math.sqrt(0.70),
                        "ep_size_mm": [geometry.tab_x, geometry.tab_y],
                        "paste_size_mm": [paste_x, paste_y],
                        "area_ratio": paste_x * paste_y / (geometry.tab_x * geometry.tab_y),
                        "acceptance": "0.700±0.001",
                    }
                if family_gate["candidate_allowed"]:
                    candidate_text = self.api.build_kicad_mod(
                        name,
                        geometry,
                        source_sha256=digest,
                        anchor_status="batch_unconfirmed_review_required",
                    )
                    candidate_path = (
                        output_root
                        / "unconfirmed_candidates.pretty"
                        / f"{name}.kicad_mod"
                    )
                    route = route_candidate_output(
                        self.api,
                        record=record,
                        candidate_footprint_text=candidate_text,
                        canonical_registry=self.canonical_registry,
                        candidate_path=candidate_path,
                        name=name,
                        geometry=geometry,
                        source_sha256=digest,
                    )
                    canonical_match = route["canonical_match"]
                    record["canonical_match"] = canonical_match
                    canonical_status = str(canonical_match.get("status") or "")
                    if canonical_status == "near_canonical_new_build_required":
                        near = canonical_match.get("near_canonical") or {}
                        record["warnings"].append(
                            "近似正典 "
                            f"{near.get('canonical_entry_id') or 'unknown'}；"
                            "至少一项 pad 字段超过 ±0.001 mm，按普通新建流程处理。"
                        )
                    elif canonical_status == "exact_canonical_proposal_detected":
                        record["warnings"].append(
                            "检测到精确正典提议，但注册表尚未经用户批准；"
                            "本次仍保留普通候选，不执行映射或合并。"
                        )
                    if not route["candidate_written"]:
                        record["candidate_path"] = None
                        record["status"] = route["status"]
                        record["warnings"].append(
                            "精确封装代码及全部 pad 字段均匹配已批准正典；"
                            "未生成新 .kicad_mod，等待人工确认组件到正典的映射。"
                        )
                    else:
                        record["candidate_path"] = _relative_link(
                            output_root,
                            route["candidate_path"],
                        )
                        record["status"] = route["status"]
                else:
                    record["status"] = "family_review_required"
                    record["failure_reason"] = family_gate["reason"]
                    record["warnings"].append(family_gate["reason"])
            elif (
                isinstance(prefill, dict)
                and record["multi_family_status"]
                in {"partial_auto", "partial_auto_decimal_extension_review"}
            ):
                values = dict(prefill.get("values") or {})
                record["values"].update(values)
                auto_sources = {
                    field: str((prefill.get("sources") or {}).get(field) or "")
                    for field, value in values.items()
                    if value is not None
                }
                record["value_sources"]["auto"] = auto_sources
                record["warnings"] = list(prefill.get("warnings") or [])
                record["body_source"] = str(prefill.get("body_source") or "")
                record["body_axis_provenance"] = dict(
                    prefill.get("body_axis_provenance") or {}
                )
                record["decimal_extension_guard"] = dict(
                    prefill.get("decimal_extension_guard") or {}
                )
                record["auto_pads"] = False
                record["auto_full"] = False
                anchor = self.api.anchor_used_geometry_values(
                    transcription,
                    values,
                    source_kind=source_page.source_kind,
                    page_text=source_page.page_text,
                )
                record["anchor"] = anchor.to_dict()
                for check in anchor.checks:
                    if check.get("anchored") is False:
                        record["warnings"].append(
                            f"{check.get('field')}={check.get('value_mm')} 未能在文本层复核；"
                            "请人工看图确认。"
                        )
                for field, value in values.items():
                    if value is None:
                        continue
                    record["value_provenance"].append(
                        _field_provenance(
                            self.api,
                            field,
                            float(value),
                            auto_sources.get(field, ""),
                            transcription,
                            selected_page,
                        )
                    )
                record["status"] = record["multi_family_status"]
                record["failure_reason"] = str(resolution.get("reason") or "")
            else:
                record["anchor"] = self.api.anchor_used_geometry_values(
                    transcription,
                    {},
                    source_kind=source_page.source_kind,
                    page_text=source_page.page_text,
                ).to_dict()
                body_values = dict(resolution.get("body_values") or {"body_x": None, "body_y": None})
                body_sources = dict(resolution.get("body_sources") or {})
                record["values"].update(body_values)
                record["value_sources"]["auto"].update(body_sources)
                record["body_axis_provenance"] = dict(resolution.get("body_axis_provenance") or {})
                for field in ("body_x", "body_y"):
                    value = body_values.get(field)
                    if value is not None:
                        record["value_provenance"].append(
                            _field_provenance(
                                self.api,
                                field,
                                float(value),
                                body_sources.get(field, ""),
                                transcription,
                                selected_page,
                            )
                        )
                record["status"] = str(resolution.get("status") or "solver_process_failed_manual_review")
                record["failure_reason"] = str(resolution.get("reason") or "隔离求解未返回可用结果。")
                record["auto_pads"] = False
                record["auto_full"] = False
        except Exception as exc:
            record["status"] = "document_failed"
            record["error_type"] = type(exc).__name__
            record["failure_reason"] = _redact_text(exc)
        finally:
            try:
                self._apply_c3_fallback(record, document)
            except Exception as exc:
                record["c3_package_match"] = {
                    "status": "c3_matcher_failed_manual_review",
                    "model_call_count": 0,
                    "source_marker": C3_SOURCE_MARKER,
                    "mapping_review_required": False,
                    "error_type": type(exc).__name__,
                    "reason": _redact_text(exc),
                }
                record["warnings"].append(
                    "C3 封装词表回退失败；保留原管线结果并转人工核对。"
                )
            record["manual_input_bounds"] = _manual_input_bounds(record)
            record["finished_at"] = utc_now()
            _write_json(record_path, record)
            _write_card_html(record, item_dir / "card.html", output_root)
        return record

    def _process_document(self, document: BatchDocument, output_root: Path, resume: bool) -> dict[str, Any]:
        item_dir = output_root / "items" / f"{document.document_index:03d}_{document.expected_sha256[:16]}"
        item_dir.mkdir(parents=True, exist_ok=True)
        record_path = item_dir / "record.json"
        if resume and record_path.is_file():
            return json.loads(record_path.read_text(encoding="utf-8"))

        worker_input = item_dir / "document_worker_input.json"
        _write_json(
            worker_input,
            {
                "schema": "batch_document_worker_input_v1",
                "document_index": document.document_index,
                "pdf_path": str(document.pdf_path),
                "expected_sha256": document.expected_sha256,
                "tier": document.tier,
                "truth_metadata_included": False,
                "canonical_registry_path": (
                    str(self.config.canonical_registry_path.expanduser().resolve())
                    if self.config.canonical_registry_path is not None
                    else None
                ),
                "canonical_registry_sha256": (
                    sha256_file(self.config.canonical_registry_path.expanduser().resolve())
                    if self.config.canonical_registry_path is not None
                    else None
                ),
                "c3_terms_path": (
                    str(self.config.c3_terms_path.expanduser().resolve())
                    if self.config.c3_terms_path is not None
                    else None
                ),
                "c3_terms_sha256": (
                    sha256_file(self.config.c3_terms_path.expanduser().resolve())
                    if self.config.c3_terms_path is not None
                    else None
                ),
            },
        )
        command = [
            sys.executable,
            "-B",
            str(PLUGIN_DIR / "batch_document_worker.py"),
            "--input",
            str(worker_input),
            "--output-root",
            str(output_root),
        ]
        child_env = os.environ.copy()
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        started = time.perf_counter()
        diagnostics: dict[str, Any] = {
            "schema": "batch_document_process_v1",
            "isolated_process": True,
            "credentials_forwarded_for_model_requests": True,
            "credentials_persisted": False,
            "timeout_seconds": BATCH_DOCUMENT_TIMEOUT_SECONDS,
            "timed_out": False,
            "return_code": None,
            "wall_clock_seconds": None,
            "stdout": "",
            "stderr": "",
        }
        try:
            completed = subprocess.run(
                command,
                cwd=str(PLUGIN_DIR),
                env=child_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=BATCH_DOCUMENT_TIMEOUT_SECONDS,
            )
            diagnostics["return_code"] = completed.returncode
            diagnostics["stdout"] = _redact_text(completed.stdout)[-4000:]
            diagnostics["stderr"] = _redact_text(completed.stderr)[-4000:]
        except subprocess.TimeoutExpired as exc:
            diagnostics["timed_out"] = True
            diagnostics["stdout"] = _redact_text(exc.stdout or "")[-4000:]
            diagnostics["stderr"] = _redact_text(exc.stderr or "")[-4000:]
        finally:
            diagnostics["wall_clock_seconds"] = round(time.perf_counter() - started, 3)
            _write_json(item_dir / "document_process.json", diagnostics)

        if record_path.is_file():
            return json.loads(record_path.read_text(encoding="utf-8"))
        record = self._base_record(document, item_dir)
        record["status"] = (
            "document_process_timeout_manual_review"
            if diagnostics["timed_out"]
            else "document_process_failed_manual_review"
        )
        record["model_call_count"] = 3
        record["failure_reason"] = (
            "单件完整链超过冻结资源上限；本件转人工，未生成 footprint。"
            if diagnostics["timed_out"]
            else "单件隔离进程失败；本件转人工，未生成 footprint。"
        )
        record["error_type"] = "TimeoutExpired" if diagnostics["timed_out"] else "ChildProcessError"
        try:
            digest = sha256_file(document.pdf_path)
            record["document_sha256"] = digest
            record["hash_verified"] = digest == document.expected_sha256
            self._apply_c3_fallback(record, document)
        except Exception as exc:
            record["warnings"].append(
                f"C3 封装词表回退失败：{type(exc).__name__}"
            )
        record["manual_input_bounds"] = _manual_input_bounds(record)
        record["finished_at"] = utc_now()
        _write_json(record_path, record)
        _write_card_html(record, item_dir / "card.html", output_root)
        return record

    def run(
        self,
        documents: Sequence[BatchDocument],
        output_root: Path,
        *,
        source: dict[str, Any],
        resume: bool = False,
    ) -> dict[str, Any]:
        output_root = output_root.expanduser().resolve()
        if output_root.exists() and any(output_root.iterdir()) and not resume:
            raise ValueError(f"output directory is not empty: {output_root}")
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "unconfirmed_candidates.pretty").mkdir(exist_ok=True)
        report_path = output_root / "BATCH_RUN_REPORT.md"
        report_path.write_text(
            RUN_ESTIMATE_LINE + "\n\n状态：运行中。所有 footprint 均为未确认候选。\n",
            encoding="utf-8",
            newline="\n",
        )
        run_manifest = {
            "schema": "kicad_footprint_batch_run_manifest_v1",
            "created_at": utc_now(),
            "estimate": RUN_ESTIMATE_LINE,
            "source": source,
            "input_document_count": len(documents),
            "document_concurrency": self.config.document_concurrency,
            "model_samples_per_document": 3,
            "active_model_request_ceiling": 6,
            "model": str(self.model_config.model_name),
            "fixture_used": False,
            "credentials_injected_by_environment": True,
            "credentials_persisted": False,
            "truth_metadata_used_for_selection": False,
            "resume": resume,
            "batch_pipeline_sha256": sha256_file(Path(__file__).resolve()),
            "main_plugin_sha256": sha256_file(PLUGIN_DIR / "kicad_footprint_builder_plugin.py"),
            "document_process_isolation": True,
            "document_process_timeout_seconds": BATCH_DOCUMENT_TIMEOUT_SECONDS,
            "solver_process_isolation": True,
            "solver_process_timeout_seconds": BATCH_SOLVER_TIMEOUT_SECONDS,
            "canonical_registry": (
                {
                    "path": str(
                        self.config.canonical_registry_path.expanduser().resolve()
                    ),
                    "sha256": sha256_file(
                        self.config.canonical_registry_path.expanduser().resolve()
                    ),
                    "authority": str(
                        (self.canonical_registry or {}).get("authority")
                        or "proposal_only"
                    ),
                    "deployment_blocked_without_user_approval": True,
                }
                if self.config.canonical_registry_path is not None
                else None
            ),
            "c3_dictionary": (
                {
                    "path": str(self.config.c3_terms_path.expanduser().resolve()),
                    "sha256": sha256_file(self.config.c3_terms_path.expanduser().resolve()),
                    "entry_count": int((self.c3_dictionary or {}).get("entry_count") or 0),
                    "model_call_count": 0,
                }
                if self.config.c3_terms_path is not None
                else None
            ),
        }
        _write_json(output_root / "run_manifest.json", run_manifest)

        results: list[dict[str, Any]] = []
        iterator = iter(documents)
        pending: dict[Future[dict[str, Any]], BatchDocument] = {}

        def submit_next(executor: ThreadPoolExecutor) -> bool:
            try:
                document = next(iterator)
            except StopIteration:
                return False
            future = executor.submit(self._process_document, document, output_root, resume)
            pending[future] = document
            self._max_pending_documents = max(self._max_pending_documents, len(pending))
            return True

        with ThreadPoolExecutor(
            max_workers=self.config.document_concurrency,
            thread_name_prefix="batch-document",
        ) as executor:
            while len(pending) < self.config.document_concurrency and submit_next(executor):
                pass
            while pending:
                done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in done:
                    document = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        item_dir = output_root / "items" / f"{document.document_index:03d}_{document.expected_sha256[:16]}"
                        item_dir.mkdir(parents=True, exist_ok=True)
                        result = self._base_record(document, item_dir)
                        result.update(
                            status="worker_failed",
                            error_type=type(exc).__name__,
                            failure_reason=_redact_text(exc),
                            finished_at=utc_now(),
                        )
                        result["manual_input_bounds"] = _manual_input_bounds(result)
                        _write_json(item_dir / "record.json", result)
                    results.append(result)
                    print(
                        json.dumps(
                            {
                                "completed": len(results),
                                "total": len(documents),
                                "record_id": result.get("record_id"),
                                "status": result.get("status"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    submit_next(executor)

        results.sort(key=lambda row: int(row["document_index"]))
        _write_review_outputs(output_root, results)
        with (output_root / "batch_results.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            for result in results:
                handle.write(json.dumps(_redact_value(result), ensure_ascii=False) + "\n")

        status_counts = Counter(str(row.get("status")) for row in results)
        candidate_rows = [row for row in results if row.get("candidate_path")]
        canonical_mapping_rows = [
            row
            for row in results
            if row.get("status") == "canonical_match_ready_for_review"
        ]
        c3_recognized_rows = [
            row
            for row in results
            if str((row.get("c3_package_match") or {}).get("status") or "")
            in {
                "c3_package_recognized_missing_canonical",
                "c3_body_conflict_manual_review",
                "c3_canonical_mapping_ready_for_review",
            }
        ]
        body_rows = [
            row for row in results
            if all(_finite_positive((row.get("values") or {}).get(field)) for field in ("body_x", "body_y"))
        ]
        exact_inputs = [
            int(row["manual_input_bounds"]["exact_numeric"])
            for row in results
            if row.get("manual_input_bounds", {}).get("exact_numeric") is not None
        ]
        min_inputs = [int(row["manual_input_bounds"]["min_numeric"]) for row in results]
        max_inputs = [int(row["manual_input_bounds"]["max_numeric"]) for row in results]
        summary = {
            "schema": "kicad_footprint_batch_live_summary_v1",
            "completed_at": utc_now(),
            "estimate": RUN_ESTIMATE_LINE,
            "source": source,
            "input_document_count": len(documents),
            "processed_document_count": len(results),
            "all_documents_accounted_for": len(results) == len(documents),
            "status_counts": dict(sorted(status_counts.items())),
            "candidate_footprint_count": len(candidate_rows),
            "canonical_mapping_review_count": len(canonical_mapping_rows),
            "c3_package_recognized_count": len(c3_recognized_rows),
            "c3_missing_canonical_count": sum(
                (row.get("c3_package_match") or {}).get("status")
                == "c3_package_recognized_missing_canonical"
                for row in c3_recognized_rows
            ),
            "c3_mapping_review_count": sum(
                (row.get("c3_package_match") or {}).get("status")
                == "c3_canonical_mapping_ready_for_review"
                for row in c3_recognized_rows
            ),
            "c3_body_reject_count": sum(
                (row.get("c3_package_match") or {}).get("status")
                == "c3_body_conflict_manual_review"
                for row in c3_recognized_rows
            ),
            "c3_model_call_count": 0,
            "build_rate": len(candidate_rows) / len(documents) if documents else 0.0,
            "body_filled_count": len(body_rows),
            "auto_pads_count": sum(bool(row.get("auto_pads")) for row in results),
            "auto_full_count": sum(bool(row.get("auto_full")) for row in results),
            "family_unique_count": sum(row.get("multi_family_status") == "unique_geometry" for row in results),
            "family_conflict_count": sum(row.get("multi_family_status") == "multi_family_conflict" for row in results),
            "no_pad_solution_count": sum(row.get("multi_family_status") == "no_pad_solution" for row in results),
            "source_counts": {
                "auto": sum(bool((row.get("value_sources") or {}).get("auto")) for row in results),
                "codex_manual": 0,
                "user_manual": 0,
            },
            "manual_numeric_inputs": {
                "exact_known_count": len(exact_inputs),
                "exact_known_mean": sum(exact_inputs) / len(exact_inputs) if exact_inputs else None,
                "exact_known_distribution": dict(sorted(Counter(exact_inputs).items())),
                "all_items_min_mean": sum(min_inputs) / len(min_inputs) if min_inputs else None,
                "all_items_max_mean": sum(max_inputs) / len(max_inputs) if max_inputs else None,
                "baseline": "6-9 inputs per item including family selection",
            },
            "wrong_value_items": {
                "status": "not_evaluated_pending_claude_frozen_20_item_manual_review",
                "count": None,
                "must_not_be_reported_as_zero_before_manual_review": True,
            },
            "model_call_count": sum(int(row.get("model_call_count") or 0) for row in results),
            "document_concurrency": self.config.document_concurrency,
            "active_model_request_ceiling": 6,
            "max_pending_documents_observed": self._max_pending_documents,
            "candidate_library": "unconfirmed_candidates.pretty",
            "confirmation_required_before_approved_library_write": True,
            "review_queue": "review_queue.json",
            "review_index": "review_queue.html",
        }
        _write_json(output_root / "batch_summary.json", summary)
        report_path.write_text(
            RUN_ESTIMATE_LINE
            + "\n\n"
            + f"状态：运行完成，处理 {len(results)}/{len(documents)}。\n\n"
            + f"待审核新建候选：{len(candidate_rows)}；正典映射待审核："
            + f"{len(canonical_mapping_rows)}；C3 识别：{len(c3_recognized_rows)}；"
            + f"本体填出：{len(body_rows)}。\n\n"
            + "错值件数：待 Claude 按冻结 20 件清单逐图核验，当前不得记为 0。\n",
            encoding="utf-8",
            newline="\n",
        )
        return summary


def _load_review_queue(queue_path: Path) -> tuple[Path, dict[str, Any]]:
    queue_path = queue_path.expanduser().resolve()
    if not queue_path.is_file():
        raise ValueError(f"review queue does not exist: {queue_path}")
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "kicad_footprint_review_queue_v1":
        raise ValueError("unsupported review queue schema")
    if not isinstance(payload.get("records"), list):
        raise ValueError("review queue lacks records")
    return queue_path, payload


def _review_row(queue: dict[str, Any], record_id: str) -> dict[str, Any]:
    matches = [row for row in queue["records"] if str(row.get("record_id")) == record_id]
    if len(matches) != 1:
        raise ValueError(f"record id must match exactly one queue row: {record_id}")
    return matches[0]


def _queue_child(root: Path, relative_path: Any) -> Path:
    text = str(relative_path or "").strip()
    if not text:
        raise ValueError("queue row lacks a required relative path")
    child = (root / text).resolve()
    try:
        child.relative_to(root)
    except ValueError as exc:
        raise ValueError("queue path escapes the batch output directory") from exc
    return child


def _load_review_record(
    queue_path: Path,
    queue: dict[str, Any],
    record_id: str,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    row = _review_row(queue, record_id)
    record_path = _queue_child(queue_path.parent, row.get("record_path"))
    if not record_path.is_file():
        raise ValueError(f"review record does not exist: {record_path}")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if str(record.get("record_id")) != record_id:
        raise ValueError("queue row and record payload disagree on record_id")
    return row, record_path, record


def _append_review_event(root: Path, event: dict[str, Any]) -> None:
    event_path = root / "review_events.jsonl"
    with event_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(_redact_value(event), ensure_ascii=False) + "\n")


def _persist_review_change(
    queue_path: Path,
    queue: dict[str, Any],
    row: dict[str, Any],
    record_path: Path,
    record: dict[str, Any],
    event: dict[str, Any],
) -> None:
    root = queue_path.parent
    row.update(
        status=record.get("status"),
        review_status=record.get("review_status"),
        accepted_families=record.get("accepted_families") or [],
        candidate_path=record.get("candidate_path"),
        manual_input_bounds=record.get("manual_input_bounds"),
    )
    _write_json(record_path, record)
    _write_json(queue_path, queue)
    card_path = _queue_child(root, row.get("card_path"))
    _write_card_html(record, card_path, root)
    _append_review_event(root, event)


def _parse_manual_values(assignments: Sequence[str]) -> dict[str, float]:
    allowed = {
        "pad_x", "pad_y", "center_x", "center_y", "pitch_y", "pitch_x",
        "tab_x", "tab_y", "body_x", "body_y",
    }
    values: dict[str, float] = {}
    for assignment in assignments:
        field, separator, raw_value = assignment.partition("=")
        field = field.strip()
        if not separator or field not in allowed:
            raise ValueError(f"manual value must be FIELD=NUMBER for a geometry field: {assignment}")
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise ValueError(f"manual value is not numeric: {assignment}") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"manual value must be finite and positive: {assignment}")
        values[field] = value
    return values


def _geometry_from_values(
    api: Any,
    family: str,
    values: dict[str, Any],
    *,
    small_pad_count: int = 2,
    tab_pad_number: int = 2,
    dual_left_count: int = 4,
    dual_right_count: int = 4,
    quad_left_count: int = 4,
    quad_right_count: int = 4,
    quad_top_count: int = 0,
    quad_bottom_count: int = 0,
) -> Any:
    family = family.upper()
    if family not in FAMILY_ORDER:
        raise ValueError(f"unsupported family: {family}")
    effective_values = dict(values)
    if family == "DUAL":
        effective_values["dual_left_count"] = dual_left_count
        effective_values["dual_right_count"] = dual_right_count
    elif family == "QUAD_EP":
        effective_values.update(
            quad_left_count=quad_left_count,
            quad_right_count=quad_right_count,
            quad_top_count=quad_top_count,
            quad_bottom_count=quad_bottom_count,
        )
    missing = [
        field
        for field in required_pad_fields(family)
        if not _finite_positive(effective_values.get(field))
    ]
    if missing:
        raise ValueError("missing required pad geometry: " + ",".join(missing))
    quad_has_top_bottom = family == "QUAD_EP" and bool(
        quad_top_count or quad_bottom_count
    )
    if quad_has_top_bottom:
        quad_missing = [
            field
            for field in ("center_y", "pitch_x")
            if not _finite_positive(effective_values.get(field))
        ]
        if quad_missing:
            raise ValueError("missing four-sided QUAD/EP geometry: " + ",".join(quad_missing))
    body_complete = all(_finite_positive(values.get(field)) for field in ("body_x", "body_y"))
    geometry = api.FootprintGeometry(
        family=family,
        pad_x=float(effective_values["pad_x"]),
        pad_y=float(effective_values["pad_y"]),
        center_x=float(effective_values["center_x"]),
        center_y=float(effective_values.get("center_y") or 0.0),
        pitch_y=float(effective_values.get("pitch_y") or 0.0),
        pitch_x=float(effective_values.get("pitch_x") or 0.0),
        body_x=float(effective_values["body_x"]) if body_complete else None,
        body_y=float(effective_values["body_y"]) if body_complete else None,
        body_source="user_manual" if body_complete else "absent_requires_user_input",
        tab_x=float(effective_values.get("tab_x") or 0.0),
        tab_y=float(effective_values.get("tab_y") or 0.0),
        small_pad_count=small_pad_count,
        tab_pad_number=tab_pad_number,
        dual_left_count=dual_left_count if family == "DUAL" else 0,
        dual_right_count=dual_right_count if family == "DUAL" else 0,
        quad_left_count=quad_left_count if family == "QUAD_EP" else 0,
        quad_right_count=quad_right_count if family == "QUAD_EP" else 0,
        quad_top_count=quad_top_count if family == "QUAD_EP" else 0,
        quad_bottom_count=quad_bottom_count if family == "QUAD_EP" else 0,
    )
    geometry.validate()
    return geometry


def review_list(queue_path: Path) -> dict[str, Any]:
    _, queue = _load_review_queue(queue_path)
    rows = queue["records"]
    return {
        "record_count": len(rows),
        "pending_count": sum(str(row.get("review_status")) == "pending" for row in rows),
        "approved_count": sum(str(row.get("review_status")) == "approved" for row in rows),
        "records": [
            {
                "record_id": row.get("record_id"),
                "pdf_name": row.get("pdf_name"),
                "status": row.get("status"),
                "review_status": row.get("review_status"),
                "accepted_families": row.get("accepted_families") or [],
                "missing_fields": (row.get("manual_input_bounds") or {}).get("missing_fields") or [],
                "card_path": row.get("card_path"),
            }
            for row in rows
        ],
    }


def review_approve(
    queue_path: Path,
    record_id: str,
    approved_library: Path,
    *,
    confirmed: bool,
    overwrite: bool = False,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("未提供 --confirm，禁止写入正式 footprint 库。")
    queue_path, queue = _load_review_queue(queue_path)
    row, record_path, record = _load_review_record(queue_path, queue, record_id)
    candidate_path = _queue_child(queue_path.parent, record.get("candidate_path"))
    api = _plugin_api()
    name = candidate_path.stem
    target = api.commit_candidate(
        candidate_path,
        approved_library,
        name,
        confirmed=True,
        overwrite=overwrite,
    )
    now = utc_now()
    record["review_status"] = "approved"
    record["approved_library_path"] = str(target)
    record["approved_at"] = now
    event = {
        "timestamp": now,
        "record_id": record_id,
        "action": "approve_existing_candidate",
        "confirmed": True,
        "target_path": str(target),
    }
    _persist_review_change(queue_path, queue, row, record_path, record, event)
    return event


def review_generate(
    queue_path: Path,
    record_id: str,
    family: str,
    assignments: Sequence[str],
    *,
    small_pad_count: int = 2,
    tab_pad_number: int = 2,
    dual_left_count: int = 4,
    dual_right_count: int = 4,
    quad_left_count: int = 4,
    quad_right_count: int = 4,
    quad_top_count: int = 0,
    quad_bottom_count: int = 0,
    confirmed: bool = False,
    approved_library: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    queue_path, queue = _load_review_queue(queue_path)
    row, record_path, record = _load_review_record(queue_path, queue, record_id)
    api = _plugin_api()
    manual_values = _parse_manual_values(assignments)
    values = dict(record.get("values") or {})
    values.update(manual_values)
    geometry = _geometry_from_values(
        api,
        family,
        values,
        small_pad_count=small_pad_count,
        tab_pad_number=tab_pad_number,
        dual_left_count=dual_left_count,
        dual_right_count=dual_right_count,
        quad_left_count=quad_left_count,
        quad_right_count=quad_right_count,
        quad_top_count=quad_top_count,
        quad_bottom_count=quad_bottom_count,
    )
    item_dir = record_path.parent
    name = f"{_slug(record_id)}_{family.upper()}"
    candidate_path = item_dir / f"{name}.kicad_mod"
    preview_path = item_dir / "preview_manual.png"
    api.write_candidate(
        candidate_path,
        name,
        geometry,
        source_sha256=str(record.get("document_sha256") or ""),
        anchor_status="user_manual_confirmed" if confirmed else "user_manual_unconfirmed",
    )
    api.render_footprint_png(geometry, preview_path, name=name)
    record["values"] = {
        field: getattr(geometry, field)
        for field in (
            "pad_x", "pad_y", "center_x", "center_y", "pitch_y", "pitch_x",
            "tab_x", "tab_y", "body_x", "body_y", "dual_left_count",
            "dual_right_count", "quad_left_count", "quad_right_count",
            "quad_top_count", "quad_bottom_count",
        )
    }
    record.setdefault("value_sources", {}).setdefault("user_manual", {}).update(
        {field: "review_cli_user_manual" for field in manual_values}
    )
    record["accepted_families"] = [family.upper()]
    record["candidate_path"] = _relative_link(queue_path.parent, candidate_path)
    record["preview_path"] = _relative_link(queue_path.parent, preview_path)
    record["status"] = "manual_candidate_ready"
    record["review_status"] = "pending"
    record["manual_input_bounds"] = _manual_input_bounds(record)
    now = utc_now()
    event: dict[str, Any] = {
        "timestamp": now,
        "record_id": record_id,
        "action": "generate_manual_candidate",
        "family": family.upper(),
        "manual_fields": sorted(manual_values),
        "confirmed": confirmed,
        "candidate_path": str(candidate_path),
    }
    if confirmed:
        if approved_library is None:
            raise ValueError("--confirm requires --approved-library")
        target = api.commit_candidate(
            candidate_path,
            approved_library,
            name,
            confirmed=True,
            overwrite=overwrite,
        )
        record["review_status"] = "approved"
        record["approved_library_path"] = str(target)
        record["approved_at"] = now
        event["target_path"] = str(target)
    _persist_review_change(queue_path, queue, row, record_path, record, event)
    return event


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KiCad footprint batch intake and review")
    commands = parser.add_subparsers(dest="command", required=True)

    dry_run = commands.add_parser("dry-run", help="zero-model page-selection skeleton")
    source = dry_run.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-ledger", type=Path)
    source.add_argument("--pdf-root", type=Path)
    dry_run.add_argument("--output", type=Path, required=True)
    dry_run.add_argument("--document-concurrency", type=int, default=DEFAULT_DOCUMENT_CONCURRENCY)
    dry_run.add_argument("--model-sample-concurrency", type=int, default=DEFAULT_MODEL_SAMPLE_CONCURRENCY)
    dry_run.add_argument("--page-limit", type=int, default=3)
    dry_run.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)

    live = commands.add_parser("live", help="run the frozen 86-row batch corpus")
    live.add_argument("--feasibility-ledger", type=Path, required=True)
    live.add_argument("--pdf-root", type=Path, required=True)
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--document-concurrency", type=int, default=2)
    live.add_argument("--model-sample-concurrency", type=int, default=3)
    live.add_argument("--page-limit", type=int, default=3)
    live.add_argument(
        "--canonical-registry",
        type=Path,
        default=PLUGIN_DIR / "canonical_footprint_registry.json",
        help="optional user-approved canonical registry; proposal-only registries never suppress candidates",
    )
    live.add_argument(
        "--c3-terms",
        type=Path,
        default=PLUGIN_DIR / "c3_standard_package_terms.json",
        help="approved deterministic C3 package dictionary; never calls a model",
    )
    live.add_argument("--resume", action="store_true")

    review_ls = commands.add_parser("review-list", help="list the human review queue")
    review_ls.add_argument("--queue", type=Path, required=True)

    approve = commands.add_parser("review-approve", help="confirm and commit an existing candidate")
    approve.add_argument("--queue", type=Path, required=True)
    approve.add_argument("--record-id", required=True)
    approve.add_argument("--approved-library", type=Path, required=True)
    approve.add_argument("--confirm", action="store_true")
    approve.add_argument("--overwrite", action="store_true")

    generate = commands.add_parser("review-generate", help="fill manual values and optionally commit")
    generate.add_argument("--queue", type=Path, required=True)
    generate.add_argument("--record-id", required=True)
    generate.add_argument("--family", choices=FAMILY_ORDER, required=True)
    generate.add_argument("--set", dest="assignments", action="append", default=[])
    generate.add_argument("--small-pad-count", type=int, default=2)
    generate.add_argument("--tab-pad-number", type=int, default=2)
    generate.add_argument("--dual-left-count", type=int, default=4)
    generate.add_argument("--dual-right-count", type=int, default=4)
    generate.add_argument("--quad-left-count", type=int, default=4)
    generate.add_argument("--quad-right-count", type=int, default=4)
    generate.add_argument("--quad-top-count", type=int, default=0)
    generate.add_argument("--quad-bottom-count", type=int, default=0)
    generate.add_argument("--approved-library", type=Path)
    generate.add_argument("--confirm", action="store_true")
    generate.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    commands = {"dry-run", "live", "review-list", "review-approve", "review-generate"}
    if raw_args and raw_args[0] not in commands:
        raw_args.insert(0, "dry-run")
    args = build_argument_parser().parse_args(raw_args)

    if args.command == "dry-run":
        config = BatchConfig(
            document_concurrency=args.document_concurrency,
            model_sample_concurrency=args.model_sample_concurrency,
            page_suggestion_limit=args.page_limit,
            dry_run=True,
        )
        if args.source_ledger:
            documents, source_counts = load_documents_from_ledger(args.source_ledger)
            source = {
                "kind": "ledger",
                "path": str(args.source_ledger.resolve()),
                "sha256": sha256_file(args.source_ledger.resolve()),
                **source_counts,
            }
        else:
            documents, source_counts = discover_unique_documents(args.pdf_root)
            source = {"kind": "pdf_root", "path": str(args.pdf_root.resolve()), **source_counts}
        result = BatchDryRunner(config).run(documents, args.output, source=source)
        exit_code = 0 if result["all_documents_accounted_for"] else 2
    elif args.command == "live":
        config = BatchConfig(
            document_concurrency=args.document_concurrency,
            model_sample_concurrency=args.model_sample_concurrency,
            page_suggestion_limit=args.page_limit,
            dry_run=False,
            canonical_registry_path=args.canonical_registry,
            c3_terms_path=args.c3_terms,
        )
        documents, source = load_bom_feasibility_documents(
            args.feasibility_ledger,
            args.pdf_root,
        )
        result = BatchLiveRunner(config).run(documents, args.output, source=source, resume=args.resume)
        exit_code = 0 if result["all_documents_accounted_for"] else 2
    elif args.command == "review-list":
        result = review_list(args.queue)
        exit_code = 0
    elif args.command == "review-approve":
        result = review_approve(
            args.queue,
            args.record_id,
            args.approved_library,
            confirmed=args.confirm,
            overwrite=args.overwrite,
        )
        exit_code = 0
    else:
        result = review_generate(
            args.queue,
            args.record_id,
            args.family,
            args.assignments,
            small_pad_count=args.small_pad_count,
            tab_pad_number=args.tab_pad_number,
            dual_left_count=args.dual_left_count,
            dual_right_count=args.dual_right_count,
            quad_left_count=args.quad_left_count,
            quad_right_count=args.quad_right_count,
            quad_top_count=args.quad_top_count,
            quad_bottom_count=args.quad_bottom_count,
            confirmed=args.confirm,
            approved_library=args.approved_library,
            overwrite=args.overwrite,
        )
        exit_code = 0
    print(json.dumps(_redact_value(result), ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
