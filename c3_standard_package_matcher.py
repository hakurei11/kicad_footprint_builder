from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Sequence

from canonical_footprint_matcher import (
    approved_canonical_for_package,
    body_cross_check,
)


DICTIONARY_SCHEMA = "c3-normalized-term-proposal-v1"
RESULT_SCHEMA = "c3_standard_package_match_v1"
SOURCE_MARKER = "标准足迹（非本文档图纸）"
MODEL_CALL_COUNT = 0

_RELATION_MARKER = re.compile(
    r"\b(?:PACKAGE|CASE|OUTLINE|DIMENSIONS?|FOOT\s*PRINT|LAND\s*PATTERN|"
    r"MECHANICAL\s+DATA|ORDERING\s+INFORMATION)\b|封装|外形|尺寸",
    re.IGNORECASE,
)
_PASSIVE_RELATION_MARKER = re.compile(
    r"\b(?:EIA|SIZE\s*CODE|MM\s*\(\s*INCH\s*\)|MINIATURE\s+FOOTPRINT)\b",
    re.IGNORECASE,
)
_PASSIVE_PAIRED_SIZE_MARKER = re.compile(
    r"\b[0-9]{4}\s*\(\s*(?:EIA\s*)?[0-9]{4}\s*\)|"
    r"\bMINIATURE\s+FOOTPRINT\b",
    re.IGNORECASE,
)
_DRAWING_DIMENSION_MARKER = re.compile(
    r"\b(?:PACKAGE\s+)?DIMENSIONS?\b|\bDIMENSIONAL\s+OUTLINE\b",
    re.IGNORECASE,
)
_PACKAGE_LABEL_MARKER = re.compile(r"\bPACKAGE\b|封装", re.IGNORECASE)
_CASE_LABEL_MARKER = re.compile(r"\bCASE\b", re.IGNORECASE)
_OUTLINE_MARKER = re.compile(r"\bOUTLINE\b|\bPACKAGE\b", re.IGNORECASE)
_TABLE_HEADER_MARKER = re.compile(
    r"\b(?:PACKAGE\s+(?:NAME|CODE)|(?:NAME|CODE)\s+PACKAGE|CASE\s+(?:NAME|CODE)|"
    r"TYPE\s+NUMBER|PART\s+NUMBER|ORDERING\s+INFORMATION)\b",
    re.IGNORECASE,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().upper()


def _normalized_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized).strip()


def _term_pattern(term: str) -> re.Pattern[str]:
    pieces = re.findall(r"[A-Za-z]+|[0-9]+", unicodedata.normalize("NFKC", term))
    if not pieces:
        raise ValueError(f"C3 term has no alphanumeric token: {term!r}")
    body = r"[\s\-_/]*".join(re.escape(piece) for piece in pieces)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def load_dictionary(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if payload.get("schema") != DICTIONARY_SCHEMA:
        raise ValueError("unsupported C3 dictionary schema")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("C3 dictionary has no entries")
    if int(payload.get("entry_count") or 0) != len(entries):
        raise ValueError("C3 dictionary entry_count does not match entries")
    prepared: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("C3 dictionary contains a non-object entry")
        term = _normalized_text(raw.get("term"))
        normalized_key = _normalized_text(raw.get("normalized_key"))
        if not term or not normalized_key:
            raise ValueError("C3 dictionary contains an empty term")
        prepared.append({**raw, "term": term, "_pattern": _term_pattern(term)})
    payload["_entries"] = prepared
    payload["_loaded_from"] = str(resolved)
    payload["_loaded_sha256"] = sha256_file(resolved)
    return payload


def extract_pdf_text_pages(pdf_path: Path) -> dict[str, Any]:
    path = pdf_path.expanduser().resolve()
    pages: list[str] = []
    pdfium_error = ""
    try:
        import pypdfium2 as pdfium

        lock = globals().setdefault("_PDFIUM_C3_LOCK", threading.Lock())
        with lock:
            document = pdfium.PdfDocument(str(path))
            try:
                for index in range(len(document)):
                    page = document[index]
                    try:
                        text_page = page.get_textpage()
                        try:
                            pages.append(text_page.get_text_range() or "")
                        finally:
                            text_page.close()
                    finally:
                        page.close()
            finally:
                document.close()
        return {
            "status": "ok",
            "backend": "pypdfium2",
            "pages": pages,
            "error": "",
        }
    except Exception as exc:
        pdfium_error = f"{type(exc).__name__}: {exc}"

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path), strict=False)
        pages = [page.extract_text() or "" for page in reader.pages]
        return {
            "status": "ok",
            "backend": "pypdf",
            "pages": pages,
            "error": "",
        }
    except Exception as exc:
        return {
            "status": "parse_error",
            "backend": "unavailable",
            "pages": [],
            "error": (
                f"pypdfium2 {pdfium_error}; pypdf {type(exc).__name__}: {exc}"
            ),
        }


def _identity_key(entry: dict[str, Any]) -> str:
    alias_group = str(entry.get("alias_group_id") or "").strip()
    return alias_group or f"TERM:{str(entry.get('normalized_key') or '').upper()}"


def _representative_term(dictionary: dict[str, Any], identity_key: str) -> str:
    for entry in dictionary.get("_entries") or []:
        if _identity_key(entry) == identity_key:
            return str(entry.get("term") or "")
    return ""


def _relation_mode(
    entry: dict[str, Any],
    line: str,
    context: str,
) -> str | None:
    evidence_class = str(entry.get("evidence_class") or "")
    if evidence_class == "authorized_passive_size_relation":
        return (
            "authorized_passive_size_relation"
            if _PASSIVE_RELATION_MARKER.search(line)
            and _PASSIVE_PAIRED_SIZE_MARKER.search(line)
            else None
        )
    if evidence_class == "table_package_column":
        if _RELATION_MARKER.search(line):
            return "declarative_same_line"
        return "table_package_header_relation" if _TABLE_HEADER_MARKER.search(context) else None
    if evidence_class == "declarative_package_drawing_dimensions":
        return (
            "declarative_drawing_dimensions_context"
            if _DRAWING_DIMENSION_MARKER.search(context)
            else None
        )
    if evidence_class == "declarative_package_label":
        return (
            "declarative_package_label_context"
            if _PACKAGE_LABEL_MARKER.search(context)
            else None
        )
    if evidence_class == "declarative_case_label":
        return (
            "declarative_case_label_context"
            if _CASE_LABEL_MARKER.search(context)
            else None
        )
    if evidence_class == "declarative_case_label_plus_table":
        return (
            "declarative_case_or_table_context"
            if _CASE_LABEL_MARKER.search(context) or _TABLE_HEADER_MARKER.search(context)
            else None
        )
    if evidence_class == "declarative_package_outline":
        return (
            "declarative_package_outline_context"
            if _OUTLINE_MARKER.search(context)
            else None
        )
    return "declarative_relation_context" if _RELATION_MARKER.search(context) else None


def match_page_texts(
    page_texts: Sequence[str],
    dictionary: dict[str, Any] | None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "status": "dictionary_not_configured",
        "model_call_count": MODEL_CALL_COUNT,
        "dictionary_path": None,
        "dictionary_sha256": None,
        "package_name": None,
        "package_names_for_registry": [],
        "identity_key": None,
        "source_page_1based": None,
        "source_line": None,
        "source_line_sha256": None,
        "evidence": [],
        "reason": "C3 dictionary was not configured",
    }
    if dictionary is None:
        return base
    base["dictionary_path"] = dictionary.get("_loaded_from")
    base["dictionary_sha256"] = dictionary.get("_loaded_sha256")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for page_number, page_text in enumerate(page_texts, start=1):
        lines = [_normalized_text(line) for line in str(page_text or "").splitlines()]
        lines = [line for line in lines if line]
        for line_index, line in enumerate(lines):
            start = max(0, line_index - 3)
            end = min(len(lines), line_index + 4)
            context = " | ".join(lines[start:end])
            for entry in dictionary.get("_entries") or []:
                pattern = entry["_pattern"]
                if pattern.search(line) is None:
                    continue
                relation_mode = _relation_mode(entry, line, context)
                if relation_mode is None:
                    continue
                key = _identity_key(entry)
                grouped.setdefault(key, []).append(
                    {
                        "term": entry["term"],
                        "normalized_key": entry["normalized_key"],
                        "alias_group_id": entry.get("alias_group_id"),
                        "aliases": list(entry.get("aliases") or []),
                        "source_page_1based": page_number,
                        "source_line": line,
                        "source_line_sha256": sha256_text(line),
                        "term_offset": int(pattern.search(line).start()),
                        "relation_mode": relation_mode,
                        "evidence_class": entry.get("evidence_class"),
                    }
                )

    if not grouped:
        base.update(
            status="no_approved_package_relation",
            reason="no approved C3 term has a package relationship context",
        )
        return base
    if len(grouped) != 1:
        base.update(
            status="ambiguous_package_relations",
            identity_keys=sorted(grouped),
            evidence=[item for key in sorted(grouped) for item in grouped[key]],
            reason="multiple distinct package identities are evidenced; do not guess",
        )
        return base

    identity_key, evidence = next(iter(grouped.items()))
    evidence = sorted(
        evidence,
        key=lambda item: (
            int(item["source_page_1based"]),
            str(item["source_line"]),
            int(item.get("term_offset") or 0),
            str(item["term"]),
        ),
    )
    representative = _representative_term(dictionary, identity_key)
    first = next(
        (item for item in evidence if item.get("term") == representative),
        evidence[0],
    )
    registry_names = sorted(
        {
            str(value)
            for item in evidence
            for value in (item["term"], *(item.get("aliases") or []))
            if str(value).strip()
        }
    )
    base.update(
        status="unique_package_relation",
        package_name=representative or first["term"],
        package_names_for_registry=registry_names,
        identity_key=identity_key,
        source_page_1based=first["source_page_1based"],
        source_line=first["source_line"],
        source_line_sha256=first["source_line_sha256"],
        evidence=evidence,
        reason="one package identity is supported by an approved relationship context",
    )
    return base


def match_frozen_source_document(
    pdf_path: Path,
    dictionary: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if dictionary is None:
        return None
    digest = sha256_file(pdf_path.expanduser().resolve())
    entries = [
        entry
        for entry in (dictionary.get("_entries") or [])
        if str(entry.get("source_pdf_sha256") or "").upper() == digest
    ]
    if not entries:
        return None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        raw_source_line = str(entry.get("source_line") or "")
        frozen_line_sha = str(entry.get("source_line_sha256") or "").upper()
        if not frozen_line_sha or sha256_text(raw_source_line) != frozen_line_sha:
            continue
        source_line = _normalized_text(raw_source_line)
        pattern = entry["_pattern"]
        match = pattern.search(source_line)
        if match is None or _relation_mode(entry, source_line, source_line) is None:
            continue
        grouped.setdefault(_identity_key(entry), []).append(
            {
                "term": entry["term"],
                "normalized_key": entry["normalized_key"],
                "alias_group_id": entry.get("alias_group_id"),
                "aliases": list(entry.get("aliases") or []),
                "source_page_1based": int(entry.get("source_page") or 0),
                "source_line": raw_source_line,
                "source_line_sha256": frozen_line_sha,
                "term_offset": int(match.start()),
                "relation_mode": "frozen_hash_verified_source_relation",
                "evidence_class": entry.get("evidence_class"),
                "source_pdf_sha256_verified": digest,
            }
        )
    if len(grouped) != 1:
        return None
    identity_key, evidence = next(iter(grouped.items()))
    evidence = sorted(
        evidence,
        key=lambda item: (
            int(item["source_page_1based"]),
            str(item["source_line"]),
            int(item.get("term_offset") or 0),
            str(item["term"]),
        ),
    )
    if not evidence:
        return None
    representative = _representative_term(dictionary, identity_key)
    first = next(
        (item for item in evidence if item.get("term") == representative),
        evidence[0],
    )
    return {
        "schema": RESULT_SCHEMA,
        "status": "unique_package_relation",
        "model_call_count": MODEL_CALL_COUNT,
        "dictionary_path": dictionary.get("_loaded_from"),
        "dictionary_sha256": dictionary.get("_loaded_sha256"),
        "package_name": representative or first["term"],
        "package_names_for_registry": sorted(
            {
                str(value)
                for item in evidence
                for value in (item["term"], *(item.get("aliases") or []))
                if str(value).strip()
            }
        ),
        "identity_key": identity_key,
        "source_page_1based": first["source_page_1based"],
        "source_line": first["source_line"],
        "source_line_sha256": first["source_line_sha256"],
        "evidence": evidence,
        "reason": (
            "text extraction was unavailable; the approved source relation was used only "
            "after exact PDF SHA256 verification"
        ),
    }


def evaluate_c3_fallback(
    pdf_path: Path,
    dictionary: dict[str, Any] | None,
    registry: dict[str, Any] | None,
    *,
    document_values: dict[str, Any] | None = None,
    existing_candidate_count: int = 0,
) -> dict[str, Any]:
    if int(existing_candidate_count) > 0:
        return {
            "schema": RESULT_SCHEMA,
            "status": "skipped_existing_candidate",
            "model_call_count": MODEL_CALL_COUNT,
            "existing_candidate_count": int(existing_candidate_count),
            "source_marker": None,
            "mapping_review_required": False,
            "reason": "existing pipeline already produced a candidate or canonical mapping",
        }
    frozen_match = match_frozen_source_document(pdf_path, dictionary)
    if frozen_match is not None:
        matched = frozen_match
        extracted = {
            "status": "not_run_exact_frozen_source",
            "backend": "approved_dictionary_evidence",
            "pages": [],
            "error": "",
        }
    else:
        extracted = extract_pdf_text_pages(pdf_path)
        matched = match_page_texts(extracted["pages"], dictionary)
    matched["text_extraction"] = {
        "status": extracted["status"],
        "backend": extracted["backend"],
        "page_count": len(extracted["pages"]),
        "error": extracted["error"],
    }
    matched["existing_candidate_count"] = 0
    matched["source_marker"] = SOURCE_MARKER
    matched["mapping_review_required"] = False
    if matched["status"] != "unique_package_relation":
        return matched

    canonical_lookup = approved_canonical_for_package(
        registry,
        matched.get("package_names_for_registry") or [],
    )
    matched["canonical_lookup"] = canonical_lookup
    if canonical_lookup["status"] != "approved_canonical_unique":
        matched.update(
            status="c3_package_recognized_missing_canonical",
            card_message=f"已识别封装 {matched['package_name']}，库内缺正典",
            body_cross_check={
                "schema": "kicad_canonical_body_cross_check_v1",
                "status": "not_applicable_no_canonical",
                "policy": "reject_only",
                "mapping_allowed": False,
                "reason": "no approved canonical exists for this package",
            },
            reason="package recognized, but no user-approved canonical exists; do not build or guess",
        )
        return matched

    canonical_entry = dict(canonical_lookup["canonical_entry"] or {})
    tolerance = float((registry or {}).get("tolerance_mm", 0.001))
    cross_check = body_cross_check(
        dict(document_values or {}),
        canonical_entry,
        tolerance_mm=tolerance,
    )
    matched["body_cross_check"] = cross_check
    matched["canonical_entry_id"] = canonical_entry.get("entry_id")
    matched["canonical_path"] = canonical_entry.get("formal_path")
    if not cross_check["mapping_allowed"]:
        matched.update(
            status="c3_body_conflict_manual_review",
            card_message="本体尺寸与正典冲突，已拒绝自动映射并转人工",
            reason="body cross-check rejected the canonical mapping",
        )
        return matched
    matched.update(
        status="c3_canonical_mapping_ready_for_review",
        mapping_review_required=True,
        card_message=(
            f"已识别封装 {matched['package_name']}，匹配正典 "
            f"{canonical_entry.get('formal_name') or canonical_entry.get('entry_id')}；等待人工确认映射"
        ),
        reason="package and approved canonical agree; human confirmation is still required",
    )
    return matched


def write_c3_card(result: dict[str, Any], path: Path, *, pdf_name: str) -> None:
    evidence_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('source_page_1based') or ''))}</td>"
        f"<td>{html.escape(str(item.get('source_line') or ''))}</td>"
        f"<td>{html.escape(str(item.get('term') or ''))}</td>"
        f"<td>{html.escape(str(item.get('relation_mode') or ''))}</td>"
        "</tr>"
        for item in (result.get("evidence") or [])
    ) or '<tr><td colspan="4">没有通过关系闸的封装证据。</td></tr>'
    lookup = result.get("canonical_lookup") or {}
    canonical = lookup.get("canonical_entry") or {}
    body = result.get("body_cross_check") or {}
    body_text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2)
    card_message = str(result.get("card_message") or result.get("reason") or "")
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>C3 · {html.escape(pdf_name)}</title><style>
body{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;margin:0;background:#f4f6f8;color:#1f2933}}
main{{max-width:1200px;margin:auto;padding:24px}}h1{{font-size:21px}}h2{{font-size:16px;margin-top:24px}}
.notice{{background:#fff3bf;border-left:4px solid #d69e00;padding:12px;margin:12px 0}}
.source{{background:#e9f5ee;border-left:4px solid #287d4f;padding:12px;font-weight:600}}
table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{border:1px solid #d8dee5;padding:8px;text-align:left;vertical-align:top}}
th{{background:#edf1f4}}pre{{white-space:pre-wrap;background:#fff;border:1px solid #d8dee5;padding:12px}}code{{font-family:Consolas,monospace}}
</style></head><body><main>
<h1>{html.escape(pdf_name)}</h1><div class="notice">{html.escape(card_message)}</div>
<div class="source">{html.escape(str(result.get('source_marker') or SOURCE_MARKER))}</div>
<h2>封装识别出处</h2><table><thead><tr><th>页</th><th>原文</th><th>词表项</th><th>关系闸</th></tr></thead><tbody>{evidence_rows}</tbody></table>
<h2>正典对应</h2><table><tbody>
<tr><th>识别封装</th><td>{html.escape(str(result.get('package_name') or '—'))}</td></tr>
<tr><th>查找状态</th><td>{html.escape(str(lookup.get('status') or '—'))}</td></tr>
<tr><th>正典 ID</th><td><code>{html.escape(str(canonical.get('entry_id') or '库内缺正典'))}</code></td></tr>
<tr><th>正典文件</th><td>{html.escape(str(canonical.get('formal_path') or '不建、不猜'))}</td></tr>
</tbody></table><h2>本体交叉核验（仅拒绝）</h2><pre>{html.escape(body_text)}</pre>
</main></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8", newline="\n")
