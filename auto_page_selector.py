from __future__ import annotations

import argparse
import json
import re
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


# Keep the deployed intake terms as a recall floor. This candidate is additive.
DIMENSION_TERMS = (
    "external dimensions",
    "mechanical dimensions",
    "package dimensions",
    "package outline",
    "mechanical data",
    "type & dimension",
    "type and dimension",
    "nominal case size",
    "case size",
    "outline drawing",
    "outlines",
    "outline",
    "product dimensions",
    "dimensions (unit",
    "dimensions(mm)",
    "dimensions (mm)",
    "dimensions in inches",
    "dimensions in millimeters",
    "dimensions – millimeters",
    "dimensions - millimeters",
    "dimensions",
    "dimension",
    "产品结构和尺寸",
    "产品尺寸",
    "产品尺寸dimensions",
    "尺寸图dimensions",
    "尺寸图",
    "制品各项寸法",
    "制品尺寸",
    "产品尺寸",
    "外观尺寸",
    "外形尺寸",
    "寸法图",
    "尺寸",
)

BASE_LAND_TERMS = (
    "recommended land pattern",
    "suggested land pattern",
    "land pattern design",
    "recommended land dimensions",
    "recommended land size",
    "recommended pad dimensions",
    "recommended solder pad dimension",
    "recommended solder pad dimensions",
    "recommended solder pad size",
    "recommend solder pad size",
    "soldering pad size recommended",
    "solder pad dimensions",
    "land pattern",
    "land dimensions",
    "recommended footprint",
    "footprint dimensions",
    "recommended soldering pattern",
    "recommended mounting pad geometries",
    "pad dimensions",
    "land/pad pattern size",
    "board land size",
    "land pattern design recommendations",
    "pcb layout",
    "board layout",
    "pad layout",
    "recommended pad layout",
    "soldering pad",
    "hole pattern",
    "焊盘尺寸",
    "推荐焊盘",
    "推荐封装",
)

# Explicit additions required by the current board task.
ADDED_LAND_TERMS = (
    "pc board layout",
    "printed circuit board pattern",
    "solderable area",
    "i/o pads and solderable",
    "pwb processing dimension",
    "mounting hole layout",
    "through hole pattern",
    "suggest layout",
    "recommended layout",
    "padlayout",
    "推奨ランド",
    "実装パターン",
)

LAND_TERMS = tuple(dict.fromkeys((*BASE_LAND_TERMS, *ADDED_LAND_TERMS)))

AMBIGUOUS_LAND_TERMS = (
    "land pattern",
    "pcb layout",
    "board layout",
    "pc board layout",
    "recommended layout",
    "pad layout",
    "suggest layout",
)

_RECOMMENDED_PAD_RE = re.compile(
    r"\brecommend(?:ed|able|end)?\s+(?:(?:solder|mounting|solderable)\s+)?pads?\b",
    re.IGNORECASE,
)
_SUGGESTED_PAD_RE = re.compile(
    r"\bsuggest(?:ed)?\s+(?:(?:solder|mounting)\s+)?pads?\b",
    re.IGNORECASE,
)
_GERMAN_RECOMMENDED_PADLAYOUT_RE = re.compile(
    r"\bempfohlen(?:e|en|er|es|em)?\s*padlayout\b",
    re.IGNORECASE,
)

FORBIDDEN_PAGE_TERMS = (
    "tape and reel",
    "carrier tape",
    "reel dimensions",
    "packaging",
    "packaging quantity",
    "reflow profile",
    "solderability test",
    "revision history",
)

_HEADING_MARKERS = (
    "fig.",
    "figure",
    "drawing",
    "layout",
    "pattern",
    "dimension",
    "outline",
    "footprint",
    "land",
    "pad",
)

_CAPTION_LINE_RE = re.compile(
    r"^(?:fig(?:ure)?\.?\s*\d+|table\s*\d+|"
    r"(?:recommended|suggested|suggest|pc\s+board|printed\s+circuit\s+board|pwb|"
    r"mounting|through\s+hole|solderable|i/o\s+pads?|package|mechanical|outline|"
    r"land|pad|padlayout|empfohlen\w*)\b|推奨ランド|実装パターン|推荐焊盘|焊盘尺寸).{0,180}$",
    re.IGNORECASE,
)


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _term_counts(text: str, terms: Iterable[str]) -> dict[str, int]:
    normalized = _normalized(text)
    return {term: normalized.count(term.lower()) for term in terms if term.lower() in normalized}


def _stem_counts(text: str) -> dict[str, int]:
    return {
        "recommended_pad_stem": len(_RECOMMENDED_PAD_RE.findall(text or "")),
        "suggested_pad_stem": len(_SUGGESTED_PAD_RE.findall(text or "")),
        "german_recommended_padlayout_stem": len(
            _GERMAN_RECOMMENDED_PADLAYOUT_RE.findall(text or "")
        ),
    }


def _page_shape(text: str) -> tuple[float, int, bool]:
    lines = [_normalized(line) for line in (text or "").splitlines()]
    lengths = [len(line) for line in lines if line]
    median_line_length = float(statistics.median(lengths)) if lengths else 0.0
    word_count = len(re.findall(r"\b\w+\b", text or "", re.UNICODE))
    is_prose = median_line_length >= 20 or word_count >= 400
    return median_line_length, word_count, is_prose


def infer_heading_caption(text: str) -> str:
    """Fallback heading/caption extraction when PDF coordinates are unavailable."""
    lines = [_normalized(line) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    selected: list[str] = []
    for index, line in enumerate(lines):
        if index < 12 or _CAPTION_LINE_RE.match(line):
            selected.append(line)
    return "\n".join(dict.fromkeys(selected))


def page_text_and_heading(page: object) -> tuple[str, str]:
    """Read page text and infer headings/captions from block position and syntax."""
    text = page.get_text("text") or ""
    page_height = float(page.rect.height) or 1.0
    selected: list[str] = []
    for block in page.get_text("blocks") or []:
        if len(block) < 5:
            continue
        y0 = float(block[1])
        block_lines = [_normalized(line) for line in str(block[4] or "").splitlines()]
        for line in (item for item in block_lines if item):
            if y0 <= 0.28 * page_height or _CAPTION_LINE_RE.match(line):
                selected.append(line)
    if not selected:
        return text, infer_heading_caption(text)
    return text, "\n".join(dict.fromkeys(selected))


@dataclass(frozen=True)
class PageDecision:
    page: int
    score: int
    matched_terms: tuple[str, ...]
    matched_land_terms: tuple[str, ...]
    matched_dimension_terms: tuple[str, ...]
    heading_positive_terms: tuple[str, ...]
    forbidden_terms: tuple[str, ...]
    vetoed: bool
    page_class: str = "none"
    effective_land_terms: tuple[str, ...] = ()
    suppressed_ambiguous_land_terms: tuple[str, ...] = ()
    matched_stem_terms: tuple[str, ...] = ()
    median_line_length: float = 0.0
    word_count: int = 0
    prose_page: bool = False
    fallback: bool = False
    fallback_reason: str | None = None
    requires_user_confirmation: bool = True


def score_page(page: int, text: str, heading_caption: str | None = None) -> PageDecision:
    heading_caption = infer_heading_caption(text) if heading_caption is None else heading_caption
    land_hits = _term_counts(text, LAND_TERMS)
    dimension_hits = _term_counts(text, DIMENSION_TERMS)
    heading_hits = _term_counts(heading_caption, (*LAND_TERMS, *DIMENSION_TERMS))
    stem_hits = {term: count for term, count in _stem_counts(text).items() if count}
    heading_stem_hits = {term: count for term, count in _stem_counts(heading_caption).items() if count}
    forbidden_hits = _term_counts(text, FORBIDDEN_PAGE_TERMS)
    median_line_length, word_count, prose_page = _page_shape(text)

    # Generic layout phrases are references as often as they are package-land
    # drawings.  They must not create land-class priority by themselves.  A
    # page still keeps land priority when an independent, specific term such
    # as "recommended land pattern" or "solder pad dimensions" is present.
    suppressed_ambiguous = {
        term: count for term, count in land_hits.items() if term in AMBIGUOUS_LAND_TERMS
    }
    effective_land_hits = {
        term: count for term, count in land_hits.items() if term not in suppressed_ambiguous
    }
    # A generic phrase can still describe a real drawing when it is itself a
    # heading/caption and the same page independently identifies dimensions.
    # This preserves established outline/land pages without treating prose
    # references or application-layout examples as package-land evidence.
    if dimension_hits:
        effective_land_hits.update(
            {
                term: count
                for term, count in suppressed_ambiguous.items()
                if term in heading_hits
            }
        )
    effective_land_hits.update(stem_hits)
    scored_land_hits = dict(land_hits)
    scored_land_hits.update(stem_hits)
    scored_heading_hits = dict(heading_hits)
    scored_heading_hits.update(heading_stem_hits)

    # The E2 fix: a forbidden term cannot veto a page whose heading/caption
    # independently carries a positive land or mechanical signal.
    vetoed = bool(forbidden_hits) and not bool({**heading_hits, **heading_stem_hits})
    # Ambiguous terms lose land-class priority, but keep their legacy numeric
    # score so independent dimension evidence can still rank the page.
    added_hits = {term: count for term, count in scored_land_hits.items() if term in ADDED_LAND_TERMS}
    base_land_hits = {term: count for term, count in scored_land_hits.items() if term not in ADDED_LAND_TERMS}
    score = (
        20 * sum(added_hits.values())
        + 10 * sum(base_land_hits.values())
        + 3 * sum(dimension_hits.values())
        + 4 * len(scored_heading_hits)
    )
    if vetoed:
        score = 0
    elif forbidden_hits:
        score = max(1, score - 2 * len(forbidden_hits))

    if effective_land_hits:
        page_class = "land"
    elif dimension_hits:
        page_class = "dimension"
    else:
        page_class = "none"

    return PageDecision(
        page=page,
        score=score,
        matched_terms=tuple(sorted({*land_hits, *dimension_hits})),
        matched_land_terms=tuple(sorted(land_hits)),
        matched_dimension_terms=tuple(sorted(dimension_hits)),
        heading_positive_terms=tuple(sorted({*heading_hits, *heading_stem_hits})),
        forbidden_terms=tuple(sorted(forbidden_hits)),
        vetoed=vetoed,
        page_class=page_class,
        effective_land_terms=tuple(sorted(effective_land_hits)),
        suppressed_ambiguous_land_terms=tuple(sorted(suppressed_ambiguous)),
        matched_stem_terms=tuple(sorted(stem_hits)),
        median_line_length=median_line_length,
        word_count=word_count,
        prose_page=prose_page,
    )


def rank_page_texts(
    page_texts: Sequence[str],
    limit: int = 3,
    heading_captions: Sequence[str] | None = None,
) -> list[PageDecision]:
    decisions = [
        score_page(
            index + 1,
            text,
            None if heading_captions is None else heading_captions[index],
        )
        for index, text in enumerate(page_texts)
    ]
    ranked = sorted(
        (decision for decision in decisions if decision.score > 0 and not decision.vetoed),
        key=lambda decision: (
            -{"land": 2, "dimension": 1, "none": 0}[decision.page_class],
            -decision.score,
            decision.page,
        ),
    )[:limit]
    if ranked:
        return ranked

    # A suggestion list must never disappear. Fallback pages are explicitly
    # marked and remain user-controlled; they are not positive classifications.
    fallback_count = min(limit, max(1, len(page_texts)))
    reason = "no_positive_page_after_veto" if page_texts else "pdf_parse_error"
    return [
        PageDecision(
            page=page,
            score=0,
            matched_terms=(),
            matched_land_terms=(),
            matched_dimension_terms=(),
            heading_positive_terms=(),
            forbidden_terms=(),
            vetoed=False,
            fallback=True,
            fallback_reason=reason,
        )
        for page in range(1, fallback_count + 1)
    ]


def suggest_pdf_pages(pdf_path: str | Path, limit: int = 3) -> dict[str, object]:
    path = Path(pdf_path)
    page_texts: list[str] = []
    heading_captions: list[str] = []
    parse_error: str | None = None
    text_extractor = "pypdfium2"
    try:
        import threading

        import pypdfium2 as pdfium

        pdfium_lock = globals().setdefault("_PDFIUM_TEXT_LOCK", threading.Lock())
        with pdfium_lock:
            document = pdfium.PdfDocument(str(path))
            try:
                for page_index in range(len(document)):
                    page = document[page_index]
                    try:
                        text_page = page.get_textpage()
                        try:
                            text = text_page.get_text_range() or ""
                        finally:
                            text_page.close()
                    finally:
                        page.close()
                    page_texts.append(text)
                    heading_captions.append(infer_heading_caption(text))
            finally:
                document.close()
    except Exception as pdfium_exc:
        page_texts.clear()
        heading_captions.clear()
        # Both extractors are shipped in the plugin. Keep the approved ranker
        # unchanged and fall back only for text intake.
        try:
            from pypdf import PdfReader

            text_extractor = "pypdf"
            reader = PdfReader(str(path))
            for page in reader.pages:
                text = page.extract_text() or ""
                page_texts.append(text)
                heading_captions.append(infer_heading_caption(text))
        except Exception as pypdf_exc:  # Keep a human-selectable fallback for damaged PDFs.
            parse_error = (
                f"pypdfium2 {type(pdfium_exc).__name__}: {pdfium_exc}; "
                f"pypdf {type(pypdf_exc).__name__}: {pypdf_exc}"
            )
            text_extractor = "unavailable"

    suggestions = rank_page_texts(page_texts, limit=limit, heading_captions=heading_captions)
    return {
        "pdf_path": str(path),
        "page_count": len(page_texts),
        "parse_status": "ok" if parse_error is None else "parse_error",
        "parse_error": parse_error,
        "text_extractor": text_extractor,
        "suggestions": [asdict(suggestion) for suggestion in suggestions],
        "predicted_has_land_pattern_signal": any(
            suggestion.page_class == "land" and not suggestion.fallback for suggestion in suggestions
        ),
        "model_call_count": 0,
        "auto_submit": False,
        "user_can_override": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic package/land-pattern page suggestions")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = suggest_pdf_pages(args.pdf, limit=args.limit)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
