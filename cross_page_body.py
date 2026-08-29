from __future__ import annotations

import base64
import concurrent.futures
import json
import mimetypes
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence


MODEL_SAMPLE_COUNT = 3
SEARCH_RADIUS_PAGES = 12
PROMPT_VERSION = "m4-cross-page-body-v1"

_TI_CODE_RE = re.compile(r"\b[A-Z]{1,5}\d{4}[A-Z]\b")
_SOT_CODE_RE = re.compile(r"\bSOT\d{3,4}(?:-\d+)?\b", re.IGNORECASE)
_DRAWING_CODE_RE = re.compile(r"\b\d{2}-\d{2}-\d{4}\b")
_CASE_CODE_RE = re.compile(r"\bCASE\s+([A-Z0-9]+(?:-\d+)?)\b", re.IGNORECASE)
_PACKAGE_BEFORE_RE = re.compile(r"\b([A-Z]{2,5})\s+PACKAGE\b", re.IGNORECASE)
_PAREN_CODE_RE = re.compile(r"\(([A-Z]{2,5})\)\s*[-\u2013\u2014]?\s*\d", re.IGNORECASE)
_SHORT_CODE_STOPLIST = {
    "BODY",
    "CASE",
    "DATA",
    "DIM",
    "DO",
    "IS",
    "JEDEC",
    "LAND",
    "LEAD",
    "MAX",
    "MIN",
    "NOTE",
    "OF",
    "OUTLINE",
    "PACKAGE",
    "REF",
    "THIS",
    "TYP",
}
_BODY_TERMS = (
    "package outline",
    "package dimensions",
    "mechanical data",
    "mechanical dimensions",
    "package drawing",
    "outline dimensions",
    "body dimensions",
)
_NON_BODY_TERMS = (
    "table of contents",
    "tape and reel",
    "carrier tape",
    "reel dimensions",
    "ordering information",
    "package materials information",
)

_BODY_SYSTEM_PROMPT = """You transcribe one package-outline drawing from a datasheet.
Do not calculate, convert, average, infer, or generate a footprint. Copy only values and labels
that are directly visible on this image. package_markings must contain exact visible package codes
or package drawing identifiers, not a guessed generic family name.

For every directly visible package-outline dimension, return one dimensions record. value must be
one continuous number copied from raw. Keep MAX/MIN/REF/TYP and tolerance text in raw. Do not merge
limits or tolerance terms. belongs_to must be package_outline. Describe endpoints along the image
axis explicitly, using horizontal/vertical or left/right/top/bottom wording. Lead, pitch, height,
and overall dimensions may be transcribed but must use their honest role. Never label a lead or
overall dimension as body_length/body_width. Return JSON only."""

_BODY_USER_PROMPT = """Copy the exact package marking(s) and all directly visible package-outline
dimensions from this package drawing. Do not read a land-pattern, stencil, paste, or solder-pad
table. Do not perform arithmetic. If an item is unclear, omit it."""

_DIMENSION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "symbol",
        "value",
        "unit",
        "raw",
        "role",
        "belongs_to",
        "endpoints",
        "is_derived",
    ],
    "properties": {
        "symbol": {"type": "string", "minLength": 1},
        "value": {
            "type": "string",
            "pattern": r"^[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)$",
        },
        "unit": {"type": "string", "minLength": 1},
        "raw": {"type": "string", "minLength": 1},
        "role": {
            "type": "string",
            "enum": [
                "body_height",
                "body_length",
                "body_width",
                "lead_width",
                "other",
                "overall_span",
                "pitch",
            ],
        },
        "belongs_to": {"type": "string", "enum": ["package_outline"]},
        "endpoints": {"type": "string", "minLength": 1},
        "is_derived": {"type": "boolean"},
    },
}

BODY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["package_markings", "dimensions"],
    "properties": {
        "package_markings": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "dimensions": {
            "type": "array",
            "minItems": 1,
            "items": _DIMENSION_SCHEMA,
        },
    },
}


@dataclass(frozen=True)
class PackageToken:
    value: str
    kind: str
    raw: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class BodyPageSelection:
    status: str
    land_page_1based: int
    body_page_1based: int | None
    landing_tokens: tuple[PackageToken, ...]
    matched_tokens: tuple[str, ...]
    binding_source: str
    requires_model_token_match: bool
    reason: str
    inspected_pages: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "land_page_1based": self.land_page_1based,
            "body_page_1based": self.body_page_1based,
            "landing_tokens": [item.to_dict() for item in self.landing_tokens],
            "matched_tokens": list(self.matched_tokens),
            "binding_source": self.binding_source,
            "requires_model_token_match": self.requires_model_token_match,
            "reason": self.reason,
            "inspected_pages": list(self.inspected_pages),
        }


@dataclass(frozen=True)
class BodyModelConsensus:
    transcription: Any
    package_markings: tuple[str, ...]
    matching_tokens: tuple[str, ...]
    sample_transcriptions: tuple[Any, ...]
    sample_markings: tuple[tuple[str, ...], ...]
    raw_responses: tuple[dict[str, Any], ...]
    request_ids: tuple[str, ...]
    stats: dict[str, Any]


@dataclass(frozen=True)
class CrossPageBodyResult:
    status: str
    selection: BodyPageSelection
    body_x: float | None
    body_y: float | None
    sources: dict[str, str]
    provenance: dict[str, Any]
    warnings: tuple[str, ...]
    model_call_count: int
    strict_schema_count: int
    package_markings: tuple[str, ...] = ()
    matching_tokens: tuple[str, ...] = ()
    transcription: Any | None = None
    sample_transcriptions: tuple[Any, ...] = ()
    sample_markings: tuple[tuple[str, ...], ...] = ()
    raw_responses: tuple[dict[str, Any], ...] = ()
    request_ids: tuple[str, ...] = ()
    consensus_stats: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "m4_cross_page_body_result_v1",
            "status": self.status,
            "selection": self.selection.to_dict(),
            "body_x": self.body_x,
            "body_y": self.body_y,
            "sources": dict(self.sources),
            "provenance": dict(self.provenance),
            "warnings": list(self.warnings),
            "model_call_count": self.model_call_count,
            "strict_schema_count": self.strict_schema_count,
            "package_markings": list(self.package_markings),
            "matching_tokens": list(self.matching_tokens),
            "sample_markings": [list(items) for items in self.sample_markings],
            "request_id_presence": [bool(value) for value in self.request_ids],
            "consensus_stats": dict(self.consensus_stats or {}),
            "transcription": (
                self.transcription.to_dict() if self.transcription is not None else None
            ),
        }


def normalize_token(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    return re.sub(r"[\s_]+", "", text)


def _append_token(
    output: list[PackageToken],
    value: str,
    kind: str,
    raw: str,
) -> None:
    normalized = normalize_token(value)
    if not normalized or normalized in _SHORT_CODE_STOPLIST:
        return
    if any(item.value == normalized for item in output):
        return
    output.append(PackageToken(normalized, kind, raw.strip()))


def extract_package_tokens(text: str) -> tuple[PackageToken, ...]:
    output: list[PackageToken] = []
    source = unicodedata.normalize("NFKC", text or "")
    for regex, kind in (
        (_TI_CODE_RE, "vendor_drawing_code"),
        (_SOT_CODE_RE, "jedec_or_vendor_code"),
        (_DRAWING_CODE_RE, "drawing_number"),
    ):
        for match in regex.finditer(source):
            _append_token(output, match.group(0), kind, match.group(0))
    for match in _CASE_CODE_RE.finditer(source):
        _append_token(
            output,
            "CASE" + match.group(1),
            "case_code",
            match.group(0),
        )
    for regex, kind in (
        (_PACKAGE_BEFORE_RE, "context_package_code"),
        (_PAREN_CODE_RE, "context_package_code"),
    ):
        for match in regex.finditer(source):
            _append_token(output, match.group(1), kind, match.group(0))
    return tuple(output)


def token_occurs(token: str, text: str) -> bool:
    normalized = normalize_token(token)
    source = unicodedata.normalize("NFKC", text or "").upper()
    if re.fullmatch(r"[A-Z]{2,5}", normalized):
        return bool(
            re.search(
                rf"(?<![A-Z0-9]){re.escape(normalized)}(?![A-Z0-9])",
                source,
            )
        )
    compact = re.sub(r"[\s_]+", "", source)
    return normalized in compact


def matching_package_tokens(
    tokens: Sequence[PackageToken],
    text: str,
) -> tuple[str, ...]:
    return tuple(item.value for item in tokens if token_occurs(item.value, text))


def _body_page_score(text: str) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    lowered = unicodedata.normalize("NFKC", text or "").lower()
    body_hits = tuple(term for term in _BODY_TERMS if term in lowered)
    non_body_hits = tuple(term for term in _NON_BODY_TERMS if term in lowered)
    return 4 * len(body_hits) - 8 * len(non_body_hits), body_hits, non_body_hits


def select_body_page_from_texts(
    page_texts: dict[int, str],
    land_page_1based: int,
) -> BodyPageSelection:
    landing_text = page_texts.get(land_page_1based, "")
    tokens = extract_package_tokens(landing_text)
    inspected: list[dict[str, Any]] = []
    exact_candidates: list[tuple[tuple[int, int, int], int, tuple[str, ...]]] = []
    for page_1based in sorted(page_texts):
        if page_1based == land_page_1based:
            continue
        text = page_texts[page_1based]
        shared = matching_package_tokens(tokens, text)
        score, body_hits, non_body_hits = _body_page_score(text)
        distance = abs(page_1based - land_page_1based)
        inspected.append(
            {
                "page_1based": page_1based,
                "distance": distance,
                "matched_tokens": list(shared),
                "body_terms": list(body_hits),
                "non_body_terms": list(non_body_hits),
                "body_score": score,
            }
        )
        if shared and score > 0:
            preceding = 1 if page_1based < land_page_1based else 0
            exact_candidates.append(((preceding, -distance, score), page_1based, shared))
    if exact_candidates:
        _rank, selected_page, shared = max(exact_candidates, key=lambda item: item[0])
        return BodyPageSelection(
            status="selected_text_bound",
            land_page_1based=land_page_1based,
            body_page_1based=selected_page,
            landing_tokens=tokens,
            matched_tokens=shared,
            binding_source="pdf_text_exact_package_token",
            requires_model_token_match=False,
            reason="Nearest package-outline page with an exact landing-page package token.",
            inspected_pages=tuple(inspected),
        )
    fallback = land_page_1based - 1 if land_page_1based > 1 else None
    if tokens and fallback is not None and fallback in page_texts:
        return BodyPageSelection(
            status="selected_model_binding_required",
            land_page_1based=land_page_1based,
            body_page_1based=fallback,
            landing_tokens=tokens,
            matched_tokens=(),
            binding_source="model_exact_package_token_required",
            requires_model_token_match=True,
            reason=(
                "No other text-extractable package-outline page carried the exact token; "
                "the immediate preceding page may be sampled once but is rejected unless "
                "two model samples copy the exact landing-page token."
            ),
            inspected_pages=tuple(inspected),
        )
    return BodyPageSelection(
        status="no_safe_page",
        land_page_1based=land_page_1based,
        body_page_1based=None,
        landing_tokens=tokens,
        matched_tokens=(),
        binding_source="none",
        requires_model_token_match=False,
        reason="No strong landing-page package token or no eligible second page.",
        inspected_pages=tuple(inspected),
    )


def _extract_pdf_page_texts(
    pdf_path: Path,
    land_page_1based: int,
    radius: int = SEARCH_RADIUS_PAGES,
) -> dict[int, str]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(pdf_path))
    output: dict[int, str] = {}
    try:
        page_count = len(document)
        if not 1 <= land_page_1based <= page_count:
            raise ValueError(f"land page {land_page_1based} outside 1..{page_count}")
        first = max(1, land_page_1based - radius)
        last = min(page_count, land_page_1based + radius)
        for page_1based in range(first, last + 1):
            page = document[page_1based - 1]
            try:
                text_page = page.get_textpage()
                try:
                    output[page_1based] = text_page.get_text_range() or ""
                finally:
                    text_page.close()
            finally:
                page.close()
    finally:
        document.close()
    return output


def select_cross_page_body_page(
    pdf_path: Path,
    land_page_1based: int,
) -> BodyPageSelection:
    page_texts = _extract_pdf_page_texts(pdf_path, land_page_1based)
    return select_body_page_from_texts(page_texts, land_page_1based)


def _parse_body_payload(api: Any, payload: Any) -> tuple[Any, tuple[str, ...]]:
    if not isinstance(payload, dict) or set(payload) != {"package_markings", "dimensions"}:
        raise api.E1aError("M4 body response fields do not match the strict contract.")
    markings = payload.get("package_markings")
    dimensions = payload.get("dimensions")
    if not isinstance(markings, list) or any(not isinstance(item, str) for item in markings):
        raise api.E1aError("M4 package_markings must be a string array.")
    transcription = api.parse_transcription(
        {
            "dimensions": dimensions,
            "pin_count": None,
            "recommended_land_pattern": False,
            "paste_evidence_visible": False,
            "unmarked_roles": [],
            "family_proposal": "other",
            "family_proposal_reason": "Package-outline page only; family is not proposed here.",
        }
    )
    filtered = tuple(
        item
        for item in transcription.dimensions
        if item.belongs_to == "package_outline"
        and item.role in {
            "body_height",
            "body_length",
            "body_width",
            "lead_width",
            "other",
            "overall_span",
            "pitch",
        }
        and not item.is_derived
    )
    if not filtered:
        raise api.E1aError("M4 body response has no direct package-outline dimensions.")
    return replace(transcription, dimensions=filtered), tuple(item.strip() for item in markings if item.strip())


def _body_request_once(api: Any, image_path: Path, config: Any) -> tuple[Any, tuple[str, ...], dict[str, Any], str]:
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "model": config.model_name,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _BODY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            _BODY_USER_PROMPT
                            + "\nPROMPT_VERSION="
                            + PROMPT_VERSION
                            + "\nOUTPUT_SCHEMA="
                            + json.dumps(BODY_OUTPUT_SCHEMA, ensure_ascii=False, separators=(",", ":"))
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{encoded}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "m4_cross_page_body",
                "strict": True,
                "schema": BODY_OUTPUT_SCHEMA,
            },
        },
    }
    raw = api._post_json(config, payload)
    content = api._strip_json_fence(api._message_content(raw))
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise api.E1aError(f"M4 body response is not JSON: {exc.msg}") from exc
    transcription, markings = _parse_body_payload(api, parsed)
    return transcription, markings, raw, str(raw.get("id") or "")


def _sample_token_votes(
    tokens: Sequence[PackageToken],
    sample_markings: Sequence[Sequence[str]],
) -> tuple[tuple[str, ...], dict[str, int]]:
    votes: Counter[str] = Counter()
    for markings in sample_markings:
        matched_in_sample = {
            token.value
            for token in tokens
            if any(token_occurs(token.value, marking) for marking in markings)
        }
        votes.update(matched_in_sample)
    accepted = tuple(sorted(token for token, count in votes.items() if count >= 2))
    return accepted, dict(sorted(votes.items()))


def transcribe_body_page(
    api: Any,
    image_path: Path,
    config: Any,
    landing_tokens: Sequence[PackageToken],
) -> BodyModelConsensus:
    results: list[tuple[Any, tuple[str, ...], dict[str, Any], str] | None] = [None] * MODEL_SAMPLE_COUNT
    errors: list[str] = [""] * MODEL_SAMPLE_COUNT
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MODEL_SAMPLE_COUNT,
        thread_name_prefix="m4-body-vlm",
    ) as executor:
        futures = [executor.submit(_body_request_once, api, image_path, config) for _ in range(MODEL_SAMPLE_COUNT)]
        for index, future in enumerate(futures):
            try:
                results[index] = future.result()
            except Exception as exc:
                errors[index] = type(exc).__name__
    successful = [item for item in results if item is not None]
    if len(successful) < 2:
        raise api.E1aError(
            f"M4 body sampling insufficient: {len(successful)}/{MODEL_SAMPLE_COUNT}; "
            f"error_types={[value for value in errors if value]}"
        )
    transcriptions = tuple(item[0] for item in successful)
    merged, stats = api.merge_transcription_consensus(
        transcriptions,
        third_sample_attempted=len(successful) >= 3,
        third_sample_error=next((value for value in errors if value), ""),
    )
    markings = tuple(item[1] for item in successful)
    matching_tokens, token_votes = _sample_token_votes(landing_tokens, markings)
    merged_markings = tuple(
        sorted(
            marking
            for marking, count in Counter(
                normalize_token(marking)
                for sample in markings
                for marking in set(sample)
            ).items()
            if marking and count >= 2
        )
    )
    stats = {
        **dict(stats),
        "schema": "m4_cross_page_body_consensus_v1",
        "strict_schema": True,
        "network_sample_attempt_count": MODEL_SAMPLE_COUNT,
        "network_sample_success_count": len(successful),
        "network_sample_failure_count": MODEL_SAMPLE_COUNT - len(successful),
        "network_error_types": [value for value in errors if value],
        "package_token_votes": token_votes,
        "matching_package_tokens": list(matching_tokens),
    }
    return BodyModelConsensus(
        transcription=merged,
        package_markings=merged_markings,
        matching_tokens=matching_tokens,
        sample_transcriptions=transcriptions,
        sample_markings=markings,
        raw_responses=tuple(item[2] for item in successful),
        request_ids=tuple(item[3] for item in successful),
        stats=stats,
    )


def _pad_field_snapshot(values: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(
        sorted(
            (key, value)
            for key, value in values.items()
            if key not in {"body_x", "body_y"}
        )
    )


def resolve_body_with_existing_gates(
    api: Any,
    transcription: Any,
    family: str,
    land_combination: str,
    baseline_values: dict[str, Any],
    page_text: str,
) -> tuple[float | None, float | None, dict[str, str], dict[str, Any], tuple[str, ...]]:
    before = _pad_field_snapshot(baseline_values)
    values = dict(baseline_values)
    values["body_x"] = None
    values["body_y"] = None
    sources: dict[str, str] = {}
    warnings: list[str] = []
    notation = api.classify_page_dimension_notation(page_text)
    provenance = api._resolve_body_axis_provenance(
        transcription,
        family,
        land_combination,
        values,
        sources,
        warnings,
        notation,
    )
    complete_before_decimal = values.get("body_x") is not None and values.get("body_y") is not None
    api._apply_decimal_extension_guard(
        transcription,
        values,
        sources,
        {},
        provenance,
        page_text,
        warnings,
    )
    if complete_before_decimal and (values.get("body_x") is None or values.get("body_y") is None):
        provenance = api._invalidate_body_axis_provenance(
            provenance,
            "Cross-page body was rejected by the existing decimal-extension guard.",
        )
    complete_before_existing = values.get("body_x") is not None and values.get("body_y") is not None
    api._apply_body_overall_cross_guard(transcription, values, sources, warnings)
    api._apply_body_reasonableness_guard(family, values, sources, warnings)
    if complete_before_existing and (values.get("body_x") is None or values.get("body_y") is None):
        provenance = api._invalidate_body_axis_provenance(
            provenance,
            "Cross-page body was rejected by the existing body/overall reasonableness guard.",
        )
    if values.get("body_x") is None or values.get("body_y") is None:
        values["body_x"] = None
        values["body_y"] = None
    if _pad_field_snapshot(values) != before:
        raise api.E1aError("M4 cross-page body path changed a non-body geometry field.")
    if provenance.get("status") != "proven":
        values["body_x"] = None
        values["body_y"] = None
    return values["body_x"], values["body_y"], sources, provenance, tuple(warnings)


def geometry_pad_snapshot(geometry: Any) -> tuple[Any, ...]:
    pads = tuple(
        sorted(
            (
                str(item.number),
                round(float(item.x), 6),
                round(float(item.y), 6),
                round(float(item.size_x), 6),
                round(float(item.size_y), 6),
            )
            for item in geometry.pads()
        )
    )
    return (
        geometry.family,
        pads,
        round(float(geometry.tab_x), 6),
        round(float(geometry.tab_y), 6),
        int(geometry.dual_left_count),
        int(geometry.dual_right_count),
        round(float(geometry.center_y), 6),
        round(float(geometry.pitch_x), 6),
        int(geometry.quad_left_count),
        int(geometry.quad_right_count),
        int(geometry.quad_top_count),
        int(geometry.quad_bottom_count),
    )


def merge_body_into_geometry(api: Any, geometry: Any, body_x: float, body_y: float) -> Any:
    before = geometry_pad_snapshot(geometry)
    updated = replace(
        geometry,
        body_x=float(body_x),
        body_y=float(body_y),
        body_source="document",
    )
    updated.validate()
    if geometry_pad_snapshot(updated) != before:
        raise api.E1aError("M4 geometry merge changed pad/EP topology.")
    return updated


def complete_cross_page_body(
    api: Any,
    pdf_path: Path,
    land_page_1based: int,
    family: str,
    land_combination: str,
    baseline_values: dict[str, Any],
    workspace: Path,
    model_config: Any,
) -> CrossPageBodyResult:
    selection = select_cross_page_body_page(pdf_path, land_page_1based)
    if selection.body_page_1based is None:
        return CrossPageBodyResult(
            status="no_safe_page",
            selection=selection,
            body_x=None,
            body_y=None,
            sources={},
            provenance={},
            warnings=(selection.reason,),
            model_call_count=0,
            strict_schema_count=0,
        )
    body_workspace = workspace / f"body_page_{selection.body_page_1based:04d}"
    source = api.prepare_source_page(pdf_path, selection.body_page_1based, body_workspace)
    (body_workspace / "page_text.txt").write_text(source.page_text, encoding="utf-8", newline="\n")
    try:
        consensus = transcribe_body_page(
            api,
            source.image_path,
            model_config,
            selection.landing_tokens,
        )
    except Exception as exc:
        return CrossPageBodyResult(
            status="body_model_failed_optional",
            selection=selection,
            body_x=None,
            body_y=None,
            sources={},
            provenance={},
            warnings=(
                "Optional cross-page body completion failed; retained the proven "
                f"land solution and left body manual. {type(exc).__name__}: {exc}",
            ),
            model_call_count=MODEL_SAMPLE_COUNT,
            strict_schema_count=0,
        )
    binding_tokens = selection.matched_tokens or consensus.matching_tokens
    if selection.requires_model_token_match and not consensus.matching_tokens:
        return CrossPageBodyResult(
            status="package_token_mismatch",
            selection=selection,
            body_x=None,
            body_y=None,
            sources={},
            provenance={},
            warnings=("Second page did not reproduce the exact landing-page package token in two samples.",),
            model_call_count=MODEL_SAMPLE_COUNT,
            strict_schema_count=len(consensus.sample_transcriptions),
            package_markings=consensus.package_markings,
            matching_tokens=(),
            transcription=consensus.transcription,
            sample_transcriptions=consensus.sample_transcriptions,
            sample_markings=consensus.sample_markings,
            raw_responses=consensus.raw_responses,
            request_ids=consensus.request_ids,
            consensus_stats=consensus.stats,
        )
    transcription = api.apply_page_pattern_evidence(consensus.transcription, source.page_text)
    transcription, folding = api.fold_page_text_min_max_bounds(transcription, source.page_text)
    body_x, body_y, sources, provenance, warnings = resolve_body_with_existing_gates(
        api,
        transcription,
        family,
        land_combination,
        baseline_values,
        source.page_text,
    )
    provenance = {
        **dict(provenance),
        "cross_page": {
            "schema": "m4_cross_page_binding_v1",
            "land_page_1based": land_page_1based,
            "body_page_1based": selection.body_page_1based,
            "package_tokens": list(binding_tokens),
            "binding_source": selection.binding_source,
            "text_bound_folding": folding,
        },
    }
    status = "body_completed" if body_x is not None and body_y is not None else "body_unresolved"
    return CrossPageBodyResult(
        status=status,
        selection=selection,
        body_x=body_x,
        body_y=body_y,
        sources=sources,
        provenance=provenance,
        warnings=warnings,
        model_call_count=MODEL_SAMPLE_COUNT,
        strict_schema_count=len(consensus.sample_transcriptions),
        package_markings=consensus.package_markings,
        matching_tokens=tuple(binding_tokens),
        transcription=transcription,
        sample_transcriptions=consensus.sample_transcriptions,
        sample_markings=consensus.sample_markings,
        raw_responses=consensus.raw_responses,
        request_ids=consensus.request_ids,
        consensus_stats=consensus.stats,
    )


def body_missing(values: dict[str, Any]) -> bool:
    return values.get("body_x") is None or values.get("body_y") is None


def direct_package_body_dimensions(transcription: Any) -> tuple[Any, ...]:
    return tuple(
        item
        for item in transcription.dimensions
        if item.belongs_to == "package_outline"
        and item.role in {"body_length", "body_width"}
        and not item.is_derived
    )


def package_token_values(tokens: Iterable[PackageToken]) -> tuple[str, ...]:
    return tuple(item.value for item in tokens)
