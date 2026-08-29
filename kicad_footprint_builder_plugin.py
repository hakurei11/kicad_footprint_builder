from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import pcbnew
import wx


_PLUGIN_DIR = Path(__file__).resolve().parent
_VENDOR_DIR = _PLUGIN_DIR / "vendor"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))

from auto_page_selector import suggest_pdf_pages
from confirmation_flow import (
    ConfirmationError,
    apply_confirmations,
    list_confirmation_records,
    rollback_transaction,
)


def as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


# === E1b embedded E1a footprint builder ===

MODEL_NAME = "gpt-5.6-sol"
PROMPT_VERSION = "e1j-family-auto-prefill-v1"
SUPPORTED_FAMILIES = (
    "CHIP",
    "SMX",
    "SOT3",
    "ASYM3",
    "INLINE3",
    "GRID4",
    "DUAL",
    "QUAD_EP",
)
# The model contract is frozen.  New families are determined only by the
# deterministic solder-land topology gate, never by family_proposal.
FAMILY_PROPOSALS = ("CHIP", "SMX", "SOT3", "ASYM3", "other")
ENTERPRISE_SOLDER_MASK_MARGIN = 0.075
ENTERPRISE_SOLDER_PASTE_MARGIN = 0.0
THERMAL_PASTE_AREA_RATIO = 0.70
MODEL_SAMPLE_CONCURRENCY = 3
MODEL_SAMPLE_REQUESTS = 3
CHIP_EXTERNAL_TERMINAL_HINT = (
    "body 小于中心距，疑似端子外伸型（SMX），请检查族选择"
)
BODY_DRAWING_ORIENTATION_NOTICE = (
    "本体按原图朝向填入（横→X，竖→Y）；若你填写的焊盘朝向不同，请自行调换本体 X/Y"
)
MANUAL_GEOMETRY_FIELDS = (
    "pad_x",
    "pad_y",
    "center_x",
    "center_y",
    "pitch_y",
    "pitch_x",
    "tab_x",
    "tab_y",
    "body_x",
    "body_y",
)


class E1aError(RuntimeError):
    pass


@dataclass(frozen=True)
class Dimension:
    symbol: str
    value: str
    unit: str
    raw: str
    role: str = "other"
    belongs_to: str = "other"
    endpoints: str = ""
    is_derived: bool = False
    role_original: str = ""


@dataclass(frozen=True)
class Transcription:
    dimensions: tuple[Dimension, ...]
    pin_count: int | None
    recommended_land_pattern: bool
    paste_evidence_visible: bool = False
    unmarked_roles: tuple[str, ...] = ()
    family_proposal: str = "other"
    family_proposal_reason: str = ""
    discarded_dimensions: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "dimensions": [asdict(item) for item in self.dimensions],
            "pin_count": self.pin_count,
            "recommended_land_pattern": self.recommended_land_pattern,
            "paste_evidence_visible": self.paste_evidence_visible,
            "unmarked_roles": list(self.unmarked_roles),
            "family_proposal": self.family_proposal,
            "family_proposal_reason": self.family_proposal_reason,
        }
        if self.discarded_dimensions:
            payload["discarded_dimensions"] = [
                dict(item) for item in self.discarded_dimensions
            ]
        return payload


@dataclass(frozen=True)
class FamilyDecision:
    """Deterministic family gate; the model proposal is evidence, never authority."""

    model_proposal: str
    model_reason: str
    evidence_family: str | None
    auto_family: str | None
    status: str
    reason: str
    solder_land_pad_count: int | None
    land_size_summary: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_proposal": self.model_proposal,
            "model_reason": self.model_reason,
            "evidence_family": self.evidence_family,
            "auto_family": self.auto_family,
            "status": self.status,
            "reason": self.reason,
            "solder_land_pad_count": self.solder_land_pad_count,
            "land_size_summary": list(self.land_size_summary),
            "decision_source": "deterministic_solder_land_gate",
        }


@dataclass(frozen=True)
class GeometryPrefillResult:
    values: dict[str, float | None]
    sources: dict[str, str]
    warnings: tuple[str, ...]
    status: str
    combination: str | None
    message: str
    land_dimensions: tuple[Dimension, ...]
    excluded_half_dimensions: tuple[Dimension, ...]
    paste_dimensions: tuple[Dimension, ...] = ()
    unknown_pattern_dimensions: tuple[Dimension, ...] = ()
    paste_guard_status: str = "not_applicable"
    body_source: str = "unresolved"
    auto_pads: bool = False
    auto_full: bool = False
    relation_candidates: tuple[dict[str, Any], ...] = ()
    body_axis_provenance: dict[str, Any] = field(default_factory=dict)
    asym3_axis_consistency: dict[str, Any] = field(default_factory=dict)
    decimal_extension_guard: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": self.values,
            "sources": self.sources,
            "warnings": list(self.warnings),
            "status": self.status,
            "combination": self.combination,
            "message": self.message,
            "land_dimensions": [asdict(item) for item in self.land_dimensions],
            "excluded_half_dimensions": [
                asdict(item) for item in self.excluded_half_dimensions
            ],
            "paste_dimensions": [asdict(item) for item in self.paste_dimensions],
            "unknown_pattern_dimensions": [
                asdict(item) for item in self.unknown_pattern_dimensions
            ],
            "paste_guard_status": self.paste_guard_status,
            "body_source": self.body_source,
            "auto_pads": self.auto_pads,
            "auto_full": self.auto_full,
            "relation_candidates": list(self.relation_candidates),
            "body_axis_provenance": self.body_axis_provenance,
            "mapping_source": self.body_axis_provenance.get("mapping_source"),
            "asym3_axis_consistency": self.asym3_axis_consistency,
            "decimal_extension_guard": self.decimal_extension_guard,
        }


def geometry_prefill_notice_text(result: GeometryPrefillResult) -> str:
    notice_lines = [
        warning
        for warning in result.warnings
        if warning == CHIP_EXTERNAL_TERMINAL_HINT
    ]
    provenance = result.body_axis_provenance
    if (
        isinstance(provenance, dict)
        and provenance.get("mapping_source") == "drawing_orientation"
        and provenance.get("status") == "proven"
    ):
        notice_lines.append(BODY_DRAWING_ORIENTATION_NOTICE)
    notice_lines.append(result.message)
    return "\n".join(notice_lines)


@dataclass(frozen=True)
class AnchorResult:
    source_kind: str
    checks: tuple[dict[str, Any], ...]
    can_generate: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Pad:
    number: str
    x: float
    y: float
    size_x: float
    size_y: float
    is_thermal: bool = False


@dataclass(frozen=True)
class FootprintGeometry:
    family: str
    pad_x: float
    pad_y: float
    center_x: float
    pitch_y: float
    body_x: float | None
    body_y: float | None
    body_source: str = "document"
    tab_x: float = 0.0
    tab_y: float = 0.0
    small_pad_count: int = 2
    tab_pad_number: int = 2
    dual_left_count: int = 0
    dual_right_count: int = 0
    center_y: float = 0.0
    pitch_x: float = 0.0
    quad_left_count: int = 0
    quad_right_count: int = 0
    quad_top_count: int = 0
    quad_bottom_count: int = 0

    def validate(self) -> None:
        if self.family not in SUPPORTED_FAMILIES:
            raise E1aError(f"不支持的封装族：{self.family}")
        if self.body_source not in {
            "document",
            "absent_requires_user_input",
            "user_manual",
            "manual",
        }:
            raise E1aError(f"不支持的 body_source：{self.body_source}")
        values = {
            "pad_x": self.pad_x,
            "pad_y": self.pad_y,
            "center_x": self.center_x,
        }
        if self.body_x is not None:
            values["body_x"] = self.body_x
        if self.body_y is not None:
            values["body_y"] = self.body_y
        if self.family in {"SOT3", "GRID4", "DUAL"} or (
            self.family == "ASYM3" and self.small_pad_count == 2
        ):
            values["pitch_y"] = self.pitch_y
        if self.family == "ASYM3":
            values["tab_x"] = self.tab_x
            values["tab_y"] = self.tab_y
            if self.small_pad_count not in {1, 2}:
                raise E1aError("ASYM3 小焊盘数量只能是 1 或 2。")
            if self.tab_pad_number not in {1, 2, 3}:
                raise E1aError("ASYM3 大焊盘编号只能是 1、2 或 3。")
            if self.small_pad_count == 1 and self.tab_pad_number == 3:
                raise E1aError("ASYM3 一大一小时，大焊盘编号只能是 1 或 2。")
        if self.family == "DUAL":
            if self.dual_left_count <= 0 or self.dual_right_count <= 0:
                raise E1aError("DUAL 左右排焊盘数必须为正整数。")
            if self.dual_left_count != self.dual_right_count and sorted(
                (self.dual_left_count, self.dual_right_count)
            ) != [2, 3]:
                raise E1aError("DUAL 只允许等排，或已知的 3+2 不等排形态。")
        if self.family == "QUAD_EP":
            values["pitch_y"] = self.pitch_y
            values["tab_x"] = self.tab_x
            values["tab_y"] = self.tab_y
            if self.quad_left_count <= 0 or self.quad_right_count <= 0:
                raise E1aError("QUAD/EP 左右两排焊盘数必须为正整数。")
            if self.quad_left_count != self.quad_right_count:
                raise E1aError("QUAD/EP 左右两排必须对称。")
            top_bottom = (self.quad_top_count, self.quad_bottom_count)
            if any(count < 0 for count in top_bottom):
                raise E1aError("QUAD/EP 上下排焊盘数不得为负。")
            if (self.quad_top_count == 0) != (self.quad_bottom_count == 0):
                raise E1aError("QUAD/EP 上下排必须同时存在或同时为空。")
            if self.quad_top_count != self.quad_bottom_count:
                raise E1aError("QUAD/EP 上下两排必须对称。")
            if self.quad_top_count:
                values["center_y"] = self.center_y
                values["pitch_x"] = self.pitch_x
            peripheral_count = sum(
                (
                    self.quad_left_count,
                    self.quad_right_count,
                    self.quad_top_count,
                    self.quad_bottom_count,
                )
            )
            if peripheral_count > 48:
                raise E1aError("QUAD/EP 外围焊盘数不得超过 48。")
        for name, value in values.items():
            if not math.isfinite(value) or value <= 0:
                raise E1aError(f"{name} 必须是大于 0 的有限数值。")
        if self.family == "ASYM3":
            if self.center_x <= (self.pad_x + self.tab_x) / 2.0:
                raise E1aError("大小焊盘中心距过小，焊盘会重叠。")
        elif self.family == "INLINE3":
            if self.center_x <= self.pad_x:
                raise E1aError("IN-LINE-3 相邻焊盘中心距过小，焊盘会重叠。")
        elif self.family == "GRID4":
            if self.center_x <= self.pad_x or self.pitch_y <= self.pad_y:
                raise E1aError("GRID-4 行列中心距过小，焊盘会重叠。")
        elif self.family == "DUAL":
            if self.center_x <= self.pad_x:
                raise E1aError("DUAL 两排中心距过小，焊盘会重叠。")
            if max(self.dual_left_count, self.dual_right_count) > 1 and self.pitch_y <= self.pad_y:
                raise E1aError("DUAL 行内 pitch 过小，焊盘会重叠。")
        elif self.family == "QUAD_EP":
            if self.center_x <= self.pad_x:
                raise E1aError("QUAD/EP 左右排中心距过小，焊盘会重叠。")
            if max(self.quad_left_count, self.quad_right_count) > 1 and self.pitch_y <= self.pad_y:
                raise E1aError("QUAD/EP 左右排行内 pitch 过小，焊盘会重叠。")
            if self.quad_top_count:
                if self.center_y <= self.pad_x:
                    raise E1aError("QUAD/EP 上下排中心距过小，焊盘会重叠。")
                if self.quad_top_count > 1 and self.pitch_x <= self.pad_y:
                    raise E1aError("QUAD/EP 上下排行内 pitch 过小，焊盘会重叠。")
        elif self.center_x <= self.pad_x * 0.25:
            raise E1aError("左右焊盘中心距过小，预览会重叠。")

    @property
    def pin_count(self) -> int:
        if self.family in {"SOT3", "INLINE3"}:
            return 3
        if self.family == "GRID4":
            return 4
        if self.family == "DUAL":
            return self.dual_left_count + self.dual_right_count
        if self.family == "QUAD_EP":
            return sum(
                (
                    self.quad_left_count,
                    self.quad_right_count,
                    self.quad_top_count,
                    self.quad_bottom_count,
                )
            )
        if self.family == "ASYM3":
            return self.small_pad_count + 1
        return 2

    def pads(self) -> tuple[Pad, ...]:
        self.validate()
        half_x = self.center_x / 2.0
        if self.family == "SOT3":
            half_y = self.pitch_y / 2.0
            return (
                Pad("1", -half_x, half_y, self.pad_x, self.pad_y),
                Pad("2", -half_x, -half_y, self.pad_x, self.pad_y),
                Pad("3", half_x, 0.0, self.pad_x, self.pad_y),
            )
        if self.family == "ASYM3":
            available_numbers = [
                number for number in (1, 2, 3) if number != self.tab_pad_number
            ]
            if self.small_pad_count == 1:
                small_pads = (
                    Pad(str(available_numbers[0]), -half_x, 0.0, self.pad_x, self.pad_y),
                )
            else:
                half_y = self.pitch_y / 2.0
                small_pads = (
                    Pad(str(available_numbers[0]), -half_x, -half_y, self.pad_x, self.pad_y),
                    Pad(str(available_numbers[1]), -half_x, half_y, self.pad_x, self.pad_y),
                )
            return small_pads + (
                Pad(str(self.tab_pad_number), half_x, 0.0, self.tab_x, self.tab_y),
            )
        if self.family == "INLINE3":
            return (
                Pad("1", -self.center_x, 0.0, self.pad_x, self.pad_y),
                Pad("2", 0.0, 0.0, self.pad_x, self.pad_y),
                Pad("3", self.center_x, 0.0, self.pad_x, self.pad_y),
            )
        if self.family == "GRID4":
            half_y = self.pitch_y / 2.0
            return (
                Pad("1", -half_x, half_y, self.pad_x, self.pad_y),
                Pad("2", half_x, half_y, self.pad_x, self.pad_y),
                Pad("3", half_x, -half_y, self.pad_x, self.pad_y),
                Pad("4", -half_x, -half_y, self.pad_x, self.pad_y),
            )
        if self.family == "DUAL":
            def row_positions(count: int) -> tuple[float, ...]:
                origin = (count - 1) * self.pitch_y / 2.0
                return tuple(origin - index * self.pitch_y for index in range(count))

            left_positions = row_positions(self.dual_left_count)
            right_positions = tuple(reversed(row_positions(self.dual_right_count)))
            left = tuple(
                Pad(str(index + 1), -half_x, y, self.pad_x, self.pad_y)
                for index, y in enumerate(left_positions)
            )
            right = tuple(
                Pad(
                    str(self.dual_left_count + index + 1),
                    half_x,
                    y,
                    self.pad_x,
                    self.pad_y,
                )
                for index, y in enumerate(right_positions)
            )
            return left + right
        if self.family == "QUAD_EP":
            def row_positions(count: int, pitch: float) -> tuple[float, ...]:
                origin = (count - 1) * pitch / 2.0
                return tuple(origin - index * pitch for index in range(count))

            number = 1
            pads: list[Pad] = []
            for y in reversed(row_positions(self.quad_left_count, self.pitch_y)):
                pads.append(Pad(str(number), -half_x, y, self.pad_x, self.pad_y))
                number += 1
            if self.quad_bottom_count:
                for x in reversed(row_positions(self.quad_bottom_count, self.pitch_x)):
                    pads.append(
                        Pad(str(number), x, self.center_y / 2.0, self.pad_y, self.pad_x)
                    )
                    number += 1
            for y in row_positions(self.quad_right_count, self.pitch_y):
                pads.append(Pad(str(number), half_x, y, self.pad_x, self.pad_y))
                number += 1
            if self.quad_top_count:
                for x in row_positions(self.quad_top_count, self.pitch_x):
                    pads.append(
                        Pad(str(number), x, -self.center_y / 2.0, self.pad_y, self.pad_x)
                    )
                    number += 1
            return tuple(pads)
        return (
            Pad("1", -half_x, 0.0, self.pad_x, self.pad_y),
            Pad("2", half_x, 0.0, self.pad_x, self.pad_y),
        )


_VALUE_RE = re.compile(r"^[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)$")
_FOOTPRINT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+\-]{0,79}$")
GEOMETRY_ROLES = {
    "pad_width",
    "pad_height",
    "pad_gap",
    "pad_center_distance",
    "overall_span",
    "body_length",
    "body_width",
    "body_height",
    "tab_width",
    "tab_height",
    "half_pitch",
    "half_pitch_from_centerline",
    "center_to_edge",
    "gap_between_pads",
    "pitch",
    "paste_width",
    "paste_height",
    "lead_width",
    "other",
}
DIMENSION_BELONGS_TO = {
    "solder_land",
    "solder_paste",
    "package_outline",
    "occupied_area",
    "other",
}
_LEGACY_PATTERN_ROLES = {"lands", "paste", "unknown", "other"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_transcription(payload: dict[str, Any]) -> Transcription:
    if not isinstance(payload, dict):
        raise E1aError("模型输出顶层必须是 JSON 对象。")
    required = {"dimensions", "pin_count", "recommended_land_pattern"}
    allowed = required | {
        "paste_evidence_visible",
        "unmarked_roles",
        "family_proposal",
        "family_proposal_reason",
        "discarded_dimensions",
    }
    actual = set(payload)
    if not required.issubset(actual) or not actual.issubset(allowed):
        missing = sorted(required - actual)
        extra = sorted(actual - allowed)
        raise E1aError(f"模型输出字段不符合契约；缺少={missing}，多出={extra}。")

    raw_dimensions = payload.get("dimensions")
    if not isinstance(raw_dimensions, list) or not raw_dimensions:
        raise E1aError("模型没有返回任何尺寸记录。")
    raw_discarded = payload.get("discarded_dimensions", [])
    if not isinstance(raw_discarded, list) or any(
        not isinstance(item, dict) for item in raw_discarded
    ):
        raise E1aError("discarded_dimensions 必须是对象数组。")
    discarded_dimensions: list[dict[str, Any]] = [
        dict(item) for item in raw_discarded
    ]
    dimensions: list[Dimension] = []
    for index, item in enumerate(raw_dimensions, start=1):
        allowed_fields = {
            "symbol",
            "value",
            "unit",
            "raw",
            "role",
            "belongs_to",
            "endpoints",
            "is_derived",
        }
        if (
            not isinstance(item, dict)
            or not {"symbol", "value", "unit", "raw"}.issubset(item)
            or not set(item).issubset(allowed_fields)
        ):
            raise E1aError(f"第 {index} 条尺寸记录字段不符合契约。")
        symbol = str(item.get("symbol") or "").strip()
        value = str(item.get("value") or "").strip()
        unit = str(item.get("unit") or "").strip()
        raw = str(item.get("raw") or "").strip()
        raw_role = str(item.get("role") or "other").strip().lower()
        legacy_role = (
            raw_role in _LEGACY_PATTERN_ROLES
            and not {"belongs_to", "endpoints", "is_derived"}.intersection(item)
        )
        if legacy_role:
            role = "other"
            belongs_to = {
                "lands": "solder_land",
                "paste": "solder_paste",
                "unknown": "other",
                "other": "other",
            }[raw_role]
            endpoints = "legacy fixture: endpoints not recorded"
            is_derived = False
        else:
            role = raw_role
            belongs_to = str(item.get("belongs_to") or "other").strip().lower()
            endpoints = str(item.get("endpoints") or "").strip()
            is_derived = item.get("is_derived", False)
        role_original = str(item.get("role_original") or "").strip().lower()
        if not symbol or not value or not unit or not raw:
            raise E1aError(f"第 {index} 条尺寸记录存在空字段。")
        if not _VALUE_RE.fullmatch(value):
            raise E1aError(f"第 {index} 条 value 不是单个照抄数字：{value!r}")
        if value not in raw:
            raise E1aError(f"第 {index} 条 raw 不包含 value 原串：{value!r}")
        if role not in GEOMETRY_ROLES:
            raise E1aError(
                f"第 {index} 条 role 不在允许的几何角色枚举中：{role!r}"
            )
        if role_original and role_original not in GEOMETRY_ROLES:
            raise E1aError(
                f"第 {index} 条 role_original 不在允许的几何角色枚举中："
                f"{role_original!r}"
            )
        if belongs_to not in DIMENSION_BELONGS_TO:
            raise E1aError(f"第 {index} 条 belongs_to 不在允许枚举中：{belongs_to!r}")
        if not endpoints:
            raise E1aError(f"第 {index} 条 endpoints 为空。")
        if not isinstance(is_derived, bool):
            raise E1aError(f"第 {index} 条 is_derived 必须是布尔值。")
        dimension = Dimension(
            symbol=symbol,
            value=value,
            unit=unit,
            raw=raw,
            role=role,
            belongs_to=belongs_to,
            endpoints=endpoints,
            is_derived=is_derived,
            role_original=role_original,
        )
        try:
            dimension_value_mm(dimension)
        except E1aError as exc:
            discarded_dimensions.append(
                {
                    "record_index": index,
                    "reason": str(exc),
                    "record": asdict(dimension),
                }
            )
            continue
        dimensions.append(dimension)

    if not dimensions:
        if discarded_dimensions:
            raise E1aError("过滤无效尺寸记录后，没有剩余可用尺寸记录。")
        raise E1aError("模型没有返回任何尺寸记录。")

    pin_count = payload.get("pin_count")
    if pin_count is not None:
        if isinstance(pin_count, bool) or not isinstance(pin_count, int) or pin_count <= 0:
            raise E1aError("pin_count 必须是正整数或 null。")
    land_pattern = payload.get("recommended_land_pattern")
    if not isinstance(land_pattern, bool):
        raise E1aError("recommended_land_pattern 必须是布尔值。")
    paste_evidence_visible = payload.get("paste_evidence_visible", False)
    if not isinstance(paste_evidence_visible, bool):
        raise E1aError("paste_evidence_visible 必须是布尔值。")
    unmarked_roles = payload.get("unmarked_roles", [])
    if not isinstance(unmarked_roles, list) or any(
        not isinstance(role, str) or role not in GEOMETRY_ROLES
        for role in unmarked_roles
    ):
        raise E1aError("unmarked_roles 必须是几何角色枚举数组。")
    family_proposal = payload.get("family_proposal", "other")
    if not isinstance(family_proposal, str) or family_proposal not in FAMILY_PROPOSALS:
        raise E1aError(
            "family_proposal 必须是 CHIP、SMX、SOT3、ASYM3 或 other。"
        )
    family_proposal_reason = payload.get("family_proposal_reason", "")
    if not isinstance(family_proposal_reason, str):
        raise E1aError("family_proposal_reason 必须是一句文字判据。")
    family_proposal_reason = family_proposal_reason.strip()
    if "family_proposal" in payload and not family_proposal_reason:
        raise E1aError("模型给出 family_proposal 时必须同时给出一句判据。")
    return Transcription(
        dimensions=_deduplicate_dual_unit_dimensions(tuple(dimensions)),
        pin_count=pin_count,
        recommended_land_pattern=land_pattern,
        paste_evidence_visible=paste_evidence_visible,
        unmarked_roles=tuple(dict.fromkeys(unmarked_roles)),
        family_proposal=family_proposal,
        family_proposal_reason=family_proposal_reason,
        discarded_dimensions=tuple(discarded_dimensions),
    )


_CONSENSUS_ENDPOINT_REPLACEMENTS = (
    ("->", "→"),
    ("=>", "→"),
    ("至", "→"),
    ("到", "→"),
    ("产品外形图中", ""),
    ("推荐焊盘图中", ""),
    ("建议焊盘图中", ""),
    ("俯视图中", ""),
    ("侧视图中", ""),
    ("右视图中", ""),
    ("俯视图", ""),
    ("侧视图", ""),
    ("右视图", ""),
    ("上方方形视图", ""),
    ("小焊盘/端子焊盘", "小焊盘"),
    ("端子焊盘", "小焊盘"),
    ("solder land", "焊盘"),
    ("solder pad", "焊盘"),
    ("封装本体", "本体"),
    ("本体外形", "本体"),
    ("封装外形", "本体"),
    ("产品外形", "本体"),
    ("封装", "本体"),
    ("左侧", "左"),
    ("右侧", "右"),
    ("上侧", "上"),
    ("下侧", "下"),
    ("左端", "左"),
    ("右端", "右"),
    ("上端", "上"),
    ("下端", "下"),
    ("左边缘", "左缘"),
    ("右边缘", "右缘"),
    ("上边缘", "上缘"),
    ("下边缘", "下缘"),
    ("左外缘", "左缘"),
    ("右外缘", "右缘"),
    ("上外缘", "上缘"),
    ("下外缘", "下缘"),
    ("左侧面", "左缘"),
    ("右侧面", "右缘"),
    ("上表面", "上缘"),
    ("下表面", "下缘"),
    ("最外缘", "缘"),
    ("边缘", "缘"),
    ("同一", ""),
)


def normalize_consensus_endpoints(value: str) -> str:
    """Canonicalize endpoint wording without inferring a geometric role."""

    text = unicodedata.normalize("NFKC", value).strip().lower()
    for old, new in _CONSENSUS_ENDPOINT_REPLACEMENTS:
        text = text.replace(old, new)
    text = re.sub(r"左小焊盘(?:的)?外?缘", "左小焊盘左缘", text)
    text = re.sub(r"右小焊盘(?:的)?外?缘", "右小焊盘右缘", text)
    text = re.sub(r"左焊盘(?:的)?外?缘", "左焊盘左缘", text)
    text = re.sub(r"右焊盘(?:的)?外?缘", "右焊盘右缘", text)
    text = re.sub(r"[\s，,。；;：:、/\\()（）\[\]{}]+", "", text)
    text = re.sub(r"→+", "→", text)
    return text


def _normalize_consensus_number(value: str) -> str:
    try:
        number = Decimal(value.replace(",", ".")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except InvalidOperation:
        return unicodedata.normalize("NFKC", value).strip().lower()
    return "0.00" if number == 0 else format(number, ".2f")


def _normalize_consensus_unit(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().lower().replace(" ", "")
    if text in {"mm", "millimeter", "millimeters", "millimetre", "millimetres", "毫米"}:
        return "mm"
    if text in {"in", "inch", "inches", '"', "英寸"}:
        return "in"
    return text


def _consensus_belongs_key(item: Dimension) -> str:
    belongs_to = unicodedata.normalize("NFKC", item.belongs_to).strip().lower()
    return "" if belongs_to == "other" else belongs_to


def consensus_dimension_key(item: Dimension) -> tuple[str, str, str]:
    """Return the E1j-7 value-aligned key; endpoints are display-only."""

    return (
        _normalize_consensus_number(item.value),
        _normalize_consensus_unit(item.unit),
        _consensus_belongs_key(item),
    )


@dataclass
class _ConsensusDimensionSlot:
    value_key: str
    unit_key: str
    items: dict[int, Dimension]


def _consensus_base_key(item: Dimension) -> tuple[str, str]:
    return (
        _normalize_consensus_number(item.value),
        _normalize_consensus_unit(item.unit),
    )


def _slot_belongs_hint(slot: _ConsensusDimensionSlot) -> str:
    explicit = {
        _consensus_belongs_key(item)
        for item in slot.items.values()
        if _consensus_belongs_key(item)
    }
    if len(explicit) > 1:
        raise E1aError("共识多重集槽包含互斥 belongs_to，停止合并。")
    return next(iter(explicit), "")


def _endpoint_similarity(left: str, right: str) -> int:
    left_text = normalize_consensus_endpoints(left)
    right_text = normalize_consensus_endpoints(right)
    if not left_text or not right_text:
        return 0
    if left_text == right_text:
        return 10_000
    return int(round(SequenceMatcher(None, left_text, right_text).ratio() * 9_000))


def _slot_match_score(item: Dimension, slot: _ConsensusDimensionSlot) -> int | None:
    item_belongs = _consensus_belongs_key(item)
    slot_belongs = _slot_belongs_hint(slot)
    if item_belongs and slot_belongs and item_belongs != slot_belongs:
        return None
    if item_belongs and slot_belongs:
        belongs_score = 100_000
    elif item_belongs:
        belongs_score = 80_000
    elif slot_belongs:
        belongs_score = 60_000
    else:
        belongs_score = 40_000
    endpoint_score = max(
        (_endpoint_similarity(item.endpoints, existing.endpoints) for existing in slot.items.values()),
        default=0,
    )
    return belongs_score + endpoint_score


def _assignment_sort_key(assignments: tuple[int, ...], slot_count: int) -> tuple[int, ...]:
    return tuple(slot_count + 1 if value < 0 else value for value in assignments)


def _assign_sample_bucket(
    sample_index: int,
    items: list[Dimension],
    slots: list[_ConsensusDimensionSlot],
) -> None:
    """Maximize compatible multiset matches; endpoints only break same-value ties."""

    available = [index for index, slot in enumerate(slots) if sample_index not in slot.items]

    @lru_cache(maxsize=None)
    def solve(item_index: int, used_mask: int) -> tuple[int, int, tuple[int, ...]]:
        if item_index >= len(items):
            return (0, 0, ())
        unmatched = solve(item_index + 1, used_mask)
        best = (unmatched[0], unmatched[1], (-1,) + unmatched[2])
        for local_index, slot_index in enumerate(available):
            if used_mask & (1 << local_index):
                continue
            score = _slot_match_score(items[item_index], slots[slot_index])
            if score is None:
                continue
            tail = solve(item_index + 1, used_mask | (1 << local_index))
            candidate = (tail[0] + 1, tail[1] + score, (slot_index,) + tail[2])
            if candidate[:2] > best[:2] or (
                candidate[:2] == best[:2]
                and _assignment_sort_key(candidate[2], len(slots))
                < _assignment_sort_key(best[2], len(slots))
            ):
                best = candidate
        return best

    _matched, _score, assignments = solve(0, 0)
    for item, slot_index in zip(items, assignments):
        if slot_index < 0:
            slots.append(
                _ConsensusDimensionSlot(
                    value_key=_normalize_consensus_number(item.value),
                    unit_key=_normalize_consensus_unit(item.unit),
                    items={sample_index: item},
                )
            )
        else:
            slots[slot_index].items[sample_index] = item


def _align_consensus_dimensions(
    samples: tuple[Transcription, ...],
) -> list[_ConsensusDimensionSlot]:
    sample_buckets: list[dict[tuple[str, str], list[Dimension]]] = []
    base_order: list[tuple[str, str]] = []
    for sample in samples:
        buckets: dict[tuple[str, str], list[Dimension]] = {}
        for item in sample.dimensions:
            base_key = _consensus_base_key(item)
            buckets.setdefault(base_key, []).append(item)
            if base_key not in base_order:
                base_order.append(base_key)
        sample_buckets.append(buckets)

    aligned: list[_ConsensusDimensionSlot] = []
    for base_key in base_order:
        bucket_lists = [buckets.get(base_key, []) for buckets in sample_buckets]
        pivot_index = max(range(len(samples)), key=lambda index: (len(bucket_lists[index]), -index))
        slots = [
            _ConsensusDimensionSlot(base_key[0], base_key[1], {pivot_index: item})
            for item in bucket_lists[pivot_index]
        ]
        for sample_index in range(len(samples)):
            if sample_index == pivot_index:
                continue
            _assign_sample_bucket(sample_index, bucket_lists[sample_index], slots)
        aligned.extend(slots)
    return aligned


def _slot_has_initial_disagreement(slot: _ConsensusDimensionSlot) -> bool:
    left = slot.items.get(0)
    right = slot.items.get(1)
    return (
        left is None
        or right is None
        or left.role != right.role
        or left.belongs_to != right.belongs_to
        or left.is_derived != right.is_derived
    )


def _representative_dimension(
    slot: _ConsensusDimensionSlot,
    *,
    role: str,
    belongs_to: str,
    is_derived: bool,
    sample_count: int,
) -> Dimension:
    indexed = sorted(slot.items.items())
    candidates = [
        (index, item)
        for index, item in indexed
        if (role == "other" or item.role == role)
        and (belongs_to == "other" or item.belongs_to == belongs_to)
        and item.is_derived == is_derived
    ]
    if not candidates:
        candidates = [
            (index, item)
            for index, item in indexed
            if (role == "other" or item.role == role)
            and (belongs_to == "other" or item.belongs_to == belongs_to)
        ]
    if not candidates:
        candidates = indexed

    endpoint_counts = Counter(
        normalize_consensus_endpoints(item.endpoints) for _index, item in indexed
    )
    endpoint_key, endpoint_count = endpoint_counts.most_common(1)[0]
    if endpoint_key and endpoint_count > sample_count / 2.0:
        for _index, item in candidates:
            if normalize_consensus_endpoints(item.endpoints) == endpoint_key:
                return item

    def representative_score(entry: tuple[int, Dimension]) -> tuple[int, int]:
        index, item = entry
        similarity = sum(
            _endpoint_similarity(item.endpoints, other.endpoints)
            for _other_index, other in indexed
        )
        return (similarity, -index)

    return max(candidates, key=representative_score)[1]


def consensus_requires_third_sample(samples: tuple[Transcription, ...]) -> bool:
    if len(samples) != 2:
        raise E1aError("第三次采样判定必须恰好输入两份转录。")
    return any(_slot_has_initial_disagreement(slot) for slot in _align_consensus_dimensions(samples))


def _strict_majority(values: Iterable[Any], sample_count: int, default: Any) -> Any:
    counts = Counter(values)
    if not counts:
        return default
    value, count = counts.most_common(1)[0]
    return value if count > sample_count / 2.0 else default


def merge_transcription_consensus(
    samples: tuple[Transcription, ...],
    *,
    third_sample_attempted: bool = False,
    third_sample_error: str = "",
) -> tuple[Transcription, dict[str, Any]]:
    if len(samples) not in {2, 3}:
        raise E1aError("共识合并只接受 2 或 3 份成功转录。")
    initial_slots = _align_consensus_dimensions(samples[:2])
    initial_agreements = [
        slot
        for slot in initial_slots
        if 0 in slot.items
        and 1 in slot.items
        and slot.items[0].role == slot.items[1].role
    ]
    initial_role_disagreements = [
        slot
        for slot in initial_slots
        if 0 in slot.items
        and 1 in slot.items
        and slot.items[0].role != slot.items[1].role
    ]
    initial_belongs_disagreements = [
        slot
        for slot in initial_slots
        if 0 in slot.items
        and 1 in slot.items
        and slot.items[0].belongs_to != slot.items[1].belongs_to
    ]
    initial_derived_disagreements = [
        slot
        for slot in initial_slots
        if 0 in slot.items
        and 1 in slot.items
        and slot.items[0].is_derived != slot.items[1].is_derived
    ]
    initial_presence_disagreements = [
        slot for slot in initial_slots if (0 in slot.items) != (1 in slot.items)
    ]

    dimensions: list[Dimension] = []
    majority_resolved = 0
    downgraded_other = 0
    dropped_no_presence_majority = 0
    role_vote_disagreements = 0
    belongs_to_disagreements = 0
    is_derived_disagreements = 0
    aligned_slots = _align_consensus_dimensions(samples)
    for slot in aligned_slots:
        present = [slot.items[index] for index in sorted(slot.items)]
        if len(present) <= len(samples) / 2.0:
            dropped_no_presence_majority += 1
            continue
        roles = [item.role for item in present]
        if len(set(roles)) > 1:
            role_vote_disagreements += 1
        role = _strict_majority(roles, len(samples), "other")
        belongs_values = [item.belongs_to for item in present]
        belongs_to = _strict_majority(belongs_values, len(samples), "other")
        if len(set(belongs_values)) > 1:
            belongs_to_disagreements += 1
        derived_values = [item.is_derived for item in present]
        if len(set(derived_values)) > 1:
            is_derived_disagreements += 1
        is_derived = bool(
            _strict_majority(
                derived_values,
                len(samples),
                True,
            )
        )
        if role == "other" and any(value != "other" for value in roles):
            downgraded_other += 1
        if _slot_has_initial_disagreement(slot) and role != "other":
            majority_resolved += 1
        representative = _representative_dimension(
            slot,
            role=role,
            belongs_to=belongs_to,
            is_derived=is_derived,
            sample_count=len(samples),
        )
        dimensions.append(
            Dimension(
                symbol=representative.symbol,
                value=representative.value,
                unit=representative.unit,
                raw=representative.raw,
                role=role,
                belongs_to=belongs_to,
                endpoints=representative.endpoints,
                is_derived=is_derived,
            )
        )

    family_proposal = _strict_majority(
        (sample.family_proposal for sample in samples), len(samples), "other"
    )
    family_reason = next(
        (
            sample.family_proposal_reason
            for sample in samples
            if sample.family_proposal == family_proposal
        ),
        "共识采样未形成封装族多数。",
    )
    pin_count = _strict_majority(
        (sample.pin_count for sample in samples), len(samples), None
    )
    recommended_land_pattern = bool(
        _strict_majority(
            (sample.recommended_land_pattern for sample in samples),
            len(samples),
            False,
        )
    )
    stats = {
        "schema": "e1j7_value_aligned_multiset_consensus_v1",
        "alignment_key": "round_half_up(value,0.01)+unit+belongs_to; other belongs_to falls back to value+unit",
        "endpoints_in_key": False,
        "endpoints_usage": "display_and_same_value_fuzzy_tiebreak_only",
        "multiset_alignment": True,
        "requested_base_samples": 2,
        "sample_attempt_count": 3 if third_sample_attempted else 2,
        "sample_success_count": len(samples),
        "third_sample_triggered": third_sample_attempted,
        "third_sample_error": third_sample_error,
        "sample_dimension_counts": [len(sample.dimensions) for sample in samples],
        "input_max_sample_dimension_count": max(len(sample.dimensions) for sample in samples),
        "initial_key_union_count": len(initial_slots),
        "initial_role_agreement_count": len(initial_agreements),
        "initial_role_disagreement_count": len(initial_role_disagreements),
        "initial_belongs_to_disagreement_count": len(initial_belongs_disagreements),
        "initial_is_derived_disagreement_count": len(initial_derived_disagreements),
        "initial_presence_disagreement_count": len(initial_presence_disagreements),
        "initial_disagreement_count": sum(
            _slot_has_initial_disagreement(slot) for slot in initial_slots
        ),
        "aligned_slot_count": len(aligned_slots),
        "consensus_dimension_count": len(dimensions),
        "consensus_throughput_vs_max_sample": (
            len(dimensions) / max(len(sample.dimensions) for sample in samples)
        ),
        "third_sample_majority_resolved_count": majority_resolved,
        "downgraded_other_count": downgraded_other,
        "dropped_no_presence_majority_count": dropped_no_presence_majority,
        "role_vote_disagreement_count": role_vote_disagreements,
        "belongs_to_disagreement_count": belongs_to_disagreements,
        "is_derived_disagreement_count": is_derived_disagreements,
    }
    return (
        Transcription(
            dimensions=tuple(dimensions),
            pin_count=pin_count,
            recommended_land_pattern=recommended_land_pattern,
            paste_evidence_visible=any(sample.paste_evidence_visible for sample in samples),
            unmarked_roles=tuple(
                dict.fromkeys(
                    role for sample in samples for role in sample.unmarked_roles
                )
            ),
            family_proposal=family_proposal,
            family_proposal_reason=family_reason,
            discarded_dimensions=tuple(
                {
                    **dict(discarded),
                    "sample_index": sample_index,
                }
                for sample_index, sample in enumerate(samples, start=1)
                for discarded in sample.discarded_dimensions
            ),
        ),
        stats,
    )


_FAMILY_ROLE_NORMALIZATION_FAMILIES = frozenset({"CHIP", "SMX", "SOT3"})
_FAMILY_ROLE_NORMALIZATION_MAP = {
    "tab_width": "pad_width",
    "tab_height": "pad_height",
}


def normalize_roles_for_family(
    transcription: Transcription,
    family: str,
) -> tuple[Transcription, dict[str, Any]]:
    normalized_family = str(family or "").strip().upper()
    pad_roles = tuple(
        item.role
        for item in transcription.dimensions
        if item.role.startswith("pad_")
    )
    tab_items = tuple(
        (index, item)
        for index, item in enumerate(transcription.dimensions)
        if item.role in _FAMILY_ROLE_NORMALIZATION_MAP
    )
    eligible_family = normalized_family in _FAMILY_ROLE_NORMALIZATION_FAMILIES
    missing_target_roles = {
        target_role
        for target_role in _FAMILY_ROLE_NORMALIZATION_MAP.values()
        if target_role not in pad_roles
    }
    changes: list[dict[str, Any]] = []
    dimensions: list[Dimension] = []
    for index, item in enumerate(transcription.dimensions):
        mapped_role = _FAMILY_ROLE_NORMALIZATION_MAP.get(item.role)
        if not eligible_family or mapped_role not in missing_target_roles:
            mapped_role = None
        if mapped_role is None:
            dimensions.append(item)
            continue
        original_role = item.role_original or item.role
        dimensions.append(
            replace(
                item,
                role=mapped_role,
                role_original=original_role,
            )
        )
        changes.append(
            {
                "dimension_index": index,
                "symbol": item.symbol,
                "value": item.value,
                "unit": item.unit,
                "belongs_to": item.belongs_to,
                "role_original": original_role,
                "role": mapped_role,
            }
        )

    if normalized_family == "ASYM3":
        reason = "ASYM3 保留真实 tab，禁止角色归一。"
    elif normalized_family not in _FAMILY_ROLE_NORMALIZATION_FAMILIES:
        reason = "封装族不在 CHIP/SMX/SOT3 授权范围。"
    elif not tab_items:
        reason = "共识中没有可归一的 tab_width/tab_height。"
    elif not changes:
        reason = "tab_* 对应轴已有 pad_*，逐轴归一无需改动。"
    else:
        reason = "授权族内按缺失轴将 tab_width/tab_height 归一为 pad_width/pad_height。"

    stats = {
        "schema": "e1j9_family_role_normalization_v1",
        "family": normalized_family,
        "authorized_families": sorted(_FAMILY_ROLE_NORMALIZATION_FAMILIES),
        "trigger_mode": "per_axis_missing_pad_role",
        "trigger_requires_no_pad_roles": False,
        "pad_role_count_before": len(pad_roles),
        "pad_roles_before": list(pad_roles),
        "missing_target_roles_before": sorted(missing_target_roles),
        "tab_role_count_before": len(tab_items),
        "eligible": eligible_family and bool(changes),
        "applied": bool(changes),
        "normalized_dimension_count": len(changes),
        "changes": changes,
        "reason": reason,
    }
    return (
        Transcription(
            dimensions=tuple(dimensions),
            pin_count=transcription.pin_count,
            recommended_land_pattern=transcription.recommended_land_pattern,
            paste_evidence_visible=transcription.paste_evidence_visible,
            unmarked_roles=transcription.unmarked_roles,
            family_proposal=transcription.family_proposal,
            family_proposal_reason=transcription.family_proposal_reason,
            discarded_dimensions=transcription.discarded_dimensions,
        ),
        stats,
    )


_FAMILY_SIZE_TOLERANCE_MM = 0.02
_FAMILY_LARGE_MARKERS = (
    "大焊盘",
    "大端子",
    "散热焊盘",
    "散热片",
    "thermal",
    "heat sink",
    "tab",
)
_FAMILY_GENERIC_PAD_IDS = {
    "pad",
    "land",
    "solderland",
    "solderpad",
    "焊盘",
    "大焊盘",
    "小焊盘",
    "端子",
    "tab",
}
_FAMILY_GROUP_PAD_MARKERS = (
    "上排焊盘",
    "下排焊盘",
    "左排焊盘",
    "右排焊盘",
    "rowpad",
)
_FAMILY_PAD_ID_PATTERNS = (
    re.compile(
        r"((?:(?:左上|右上|左下|右下|上方|下方|上排|下排|左侧|右侧|左排|右排|"
        r"左|右|上|下|"
        r"第\s*\d+\s*号|大|小|散热|热)\s*)*焊盘)"
    ),
    re.compile(
        r"((?:(?:left|right|top|bottom|upper|lower|large|small|thermal|heat\s*sink)\s+)?"
        r"(?:solder\s+land|solder\s+pad|land|pad|tab)(?:\s*#?\s*[a-z0-9]+)?)",
        re.IGNORECASE,
    ),
)
_FAMILY_PAD_MULTIPLICITY_RE = re.compile(
    r"(?:\(|\b)(\d+)\s*[x×](?:\)|\b)|左右\s*(?:两处|各一处)|两(?:个|处)"
)


def _family_metric_land_dimensions(transcription: Transcription) -> tuple[Dimension, ...]:
    """Return only direct, metric solder-land shape dimensions for the family gate."""

    items = tuple(
        item
        for item in transcription.dimensions
        if item.belongs_to == "solder_land"
        and not item.is_derived
        and item.role
        in {
            "pad_width",
            "pad_height",
            "pad_gap",
            "gap_between_pads",
            "overall_span",
            "tab_width",
            "tab_height",
            "pad_center_distance",
            "pitch",
            "half_pitch",
            "half_pitch_from_centerline",
        }
    )
    metric = tuple(
        item
        for item in items
        if unicodedata.normalize("NFKC", item.unit).strip().lower().replace(" ", "")
        in {"mm", "millimeter", "millimeters", "毫米"}
    )
    return metric or items


def _family_is_large_pad(item: Dimension) -> bool:
    text = unicodedata.normalize("NFKC", f"{item.role} {item.endpoints} {item.raw}").lower()
    return item.role in {"tab_width", "tab_height"} or any(
        marker.lower() in text for marker in _FAMILY_LARGE_MARKERS
    )


def _family_close(left: float, right: float) -> bool:
    return abs(left - right) <= _FAMILY_SIZE_TOLERANCE_MM


def _family_consistent(values: list[float]) -> bool:
    return bool(values) and max(values) - min(values) <= _FAMILY_SIZE_TOLERANCE_MM


def _family_values(items: tuple[Dimension, ...], roles: set[str]) -> list[float]:
    values: list[float] = []
    for item in items:
        if item.role not in roles:
            continue
        try:
            values.append(dimension_value_mm(item))
        except E1aError:
            # An invalid numeric witness is evidence failure, not a model crash.
            continue
    return values


def _family_clean_half_candidates(items: tuple[Dimension, ...]) -> tuple[Dimension, ...]:
    """Do not let a half pitch masquerading as pad width split one equal-pad family."""

    center_items = tuple(
        item
        for item in items
        if item.role
        in {
            "pad_center_distance",
            "pitch",
            "half_pitch",
            "half_pitch_from_centerline",
        }
    )
    if not center_items:
        return items
    excluded: set[int] = set()
    for index, item in enumerate(items):
        if item.role not in {"pad_width", "pad_height"}:
            continue
        try:
            item_value = dimension_value_mm(item)
        except E1aError:
            continue
        for center in center_items:
            try:
                center_value = dimension_value_mm(center)
            except E1aError:
                continue
            if _family_close(item_value * 2.0, center_value):
                excluded.add(index)
                break
    return tuple(item for index, item in enumerate(items) if index not in excluded)


def _family_pad_identity(segment: str) -> str | None:
    """Extract a stable pad identity from one endpoint side, if visible."""

    text = unicodedata.normalize("NFKC", segment or "").strip().lower()
    if not text:
        return None
    for pattern in _FAMILY_PAD_ID_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        identity = re.sub(r"[\s_\-]+", "", match.group(1))
        return identity
    return None


def _family_solder_land_pad_count(items: tuple[Dimension, ...]) -> int | None:
    """Count distinct visible solder-land pads, independent of pin_count."""

    specific: set[str] = set()
    generic: set[str] = set()
    multiplicity = 1
    for item in items:
        if item.belongs_to != "solder_land" or item.is_derived:
            continue
        endpoint_text = unicodedata.normalize(
            "NFKC", f"{item.endpoints} {item.raw}"
        ).lower()
        for segment in re.split(r"\s*(?:->|→|到|至)\s*", item.endpoints or ""):
            identity = _family_pad_identity(segment)
            if identity is None:
                continue
            if identity in _FAMILY_GENERIC_PAD_IDS:
                generic.add(identity)
            else:
                specific.add(identity)
        for match in _FAMILY_PAD_MULTIPLICITY_RE.finditer(endpoint_text):
            token = match.group(1)
            multiplicity = max(multiplicity, int(token) if token else 2)

    # A generic "solder land (2x)" is evidence for two pads, but must not
    # be counted as a third pad when another dimension names left/right pads.
    grouped = {
        identity
        for identity in specific
        if any(marker in identity for marker in _FAMILY_GROUP_PAD_MARKERS)
    }
    for identity in specific:
        if not any(marker in identity for marker in ("侧", "方")):
            continue
        size_marker = next(
            (marker for marker in ("大焊盘", "小焊盘", "焊盘") if marker in identity),
            None,
        )
        if size_marker is None:
            continue
        expected_children = {
            "上方": ("左上", "右上"),
            "下方": ("左下", "右下"),
            "左侧": ("左上", "左下"),
            "右侧": ("右上", "右下"),
        }
        position_marker = next(
            (marker for marker in expected_children if marker in identity), None
        )
        if position_marker is None:
            continue
        children = {
            other
            for other in specific
            if other != identity
            and size_marker in other
            and any(marker in other for marker in expected_children[position_marker])
        }
        if len(children) >= 2:
            grouped.add(identity)
    individual = specific - grouped
    # A row label such as "下排焊盘" names a group, not a fourth pad.  Once
    # its individual pads are visible elsewhere, count only those individuals.
    visible_count = len(individual) if individual else len(specific)
    if not visible_count:
        visible_count = len(generic)
    if multiplicity > visible_count:
        visible_count = multiplicity
    return visible_count or None


def _family_tolerance_only(item: Dimension) -> bool:
    raw = unicodedata.normalize("NFKC", item.raw or "").strip().lower()
    symbol = unicodedata.normalize("NFKC", item.symbol or "").strip().lower()
    endpoints = unicodedata.normalize("NFKC", item.endpoints or "").strip().lower()
    return (
        raw.startswith(("±", "+/-", "+-"))
        or symbol.startswith(("±", "+/-", "+-"))
        or "公差" in endpoints
        or "tolerance" in endpoints
    )


def _family_has_thermal_evidence(transcription: Transcription) -> bool:
    markers = (
        "thermal pad",
        "exposed pad",
        "exposed",
        "heat sink",
        "heatsink",
        "exposed die pad",
        "powerpad",
        "epad",
        "散热焊盘",
        "散热片",
        "中央焊盘",
        "中心焊盘",
    )
    for item in transcription.dimensions:
        text = unicodedata.normalize(
            "NFKC", f"{item.role} {item.raw} {item.endpoints}"
        ).lower()
        if any(marker in text for marker in markers):
            return True
    return False


_EP_POSITIVE_PAGE_PHRASES = (
    "exposed thermal pad",
    "exposed die pad",
    "exposed pad",
    "thermal pad",
    "e-pad",
)
_EP_POSITIVE_ROLE_NAMES = frozenset(
    {
        "center_pad_width",
        "center_pad_length",
        "center_pad_height",
        "optional_center_pad_width",
        "optional_center_pad_length",
        "optional_center_pad_height",
        "thermal_pad_width",
        "thermal_pad_length",
        "thermal_pad_height",
        "exposed_pad_width",
        "exposed_pad_length",
        "exposed_pad_height",
    }
)


def _ep_positive_evidence(
    transcription: Transcription,
    page_text: str | None,
) -> str | None:
    """Return selected-page EP evidence without interpreting free-form prose."""

    normalized_page = unicodedata.normalize("NFKC", page_text or "").lower()
    for phrase in _EP_POSITIVE_PAGE_PHRASES:
        if phrase in normalized_page:
            return f'页面文字“{phrase}”'

    for item in transcription.dimensions:
        for role_value in (item.role, item.role_original):
            normalized_role = re.sub(
                r"[^a-z0-9]+",
                "_",
                unicodedata.normalize("NFKC", role_value or "").lower(),
            ).strip("_")
            if normalized_role in _EP_POSITIVE_ROLE_NAMES:
                return f"角色 {normalized_role}"
        symbol = unicodedata.normalize("NFKC", item.symbol or "").strip().lower()
        if (
            item.belongs_to == "solder_land"
            and item.role in {"tab_width", "tab_height"}
            and symbol in {"x2", "y2"}
        ):
            return f"角色 {symbol.upper()} optional center pad"
    return None


def _ep_rejection_reason(evidence: str) -> str:
    return f"检出裸露焊盘正证据：{evidence}；QUAD/EP 证据不足，转人工"


def _quad_ep_item_text(item: Dimension) -> str:
    return unicodedata.normalize(
        "NFKC", f"{item.symbol} {item.role} {item.raw} {item.endpoints}"
    ).lower()


def _quad_ep_is_thermal_via(item: Dimension) -> bool:
    text = _quad_ep_item_text(item)
    symbol = _normalize_symbol(item.symbol)
    return (
        symbol in {"v", "ev"}
        and any(marker in text for marker in ("via", "过孔", "thermal"))
    ) or any(marker in text for marker in ("thermal via", "散热过孔", "热过孔"))


def _quad_ep_is_center_pad(item: Dimension) -> bool:
    if item.belongs_to != "solder_land" or _quad_ep_is_thermal_via(item):
        return False
    symbol = _normalize_symbol(item.symbol)
    text = _quad_ep_item_text(item)
    normalized_roles = {
        re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        for value in (item.role, item.role_original)
        if value
    }
    if (
        item.role in {"tab_width", "tab_height"}
        or symbol in {"x2", "y2"}
        or normalized_roles.intersection(_EP_POSITIVE_ROLE_NAMES)
    ):
        return True

    # A relation that merely terminates at the EP (for example G: contact-pad
    # edge -> center-pad edge) is not an EP size.  Likewise CH and other
    # auxiliary dimensions must not enter the X2/Y2 candidate set.
    if item.role not in {"pad_width", "pad_height"}:
        return False
    peripheral_marked = any(
        marker in text
        for marker in (
            "small",
            "contact pad",
            "terminal",
            "小焊盘",
            "端子焊盘",
        )
    )
    return not peripheral_marked and any(
        marker in text
        for marker in (
            "center pad",
            "centre pad",
            "exposed pad",
            "thermal pad",
            "e-pad",
            "中央焊盘",
            "中心焊盘",
            "大焊盘",
        )
    )


def _quad_ep_axis(item: Dimension) -> str | None:
    return _relation_axis(item) or _family_grid_relation_kind(item)


def _quad_ep_relation_nominal(item: Dimension) -> bool:
    """Keep direct land-pattern values, never a tolerance or one-sided bound."""

    if item.is_derived or _family_tolerance_only(item):
        return False
    if _raw_has_tolerance(item.raw) and not _first_raw_number_matches_value(item):
        return False
    text = unicodedata.normalize(
        "NFKC", f"{item.symbol} {item.raw}"
    ).lower()
    if item.role in {
        "pad_width",
        "pad_height",
        "pitch",
        "tab_width",
        "tab_height",
        "pad_center_distance",
    }:
        # Recommended-land tables often put the exact construction value in a
        # MIN/MAX column (for example X1/Y1/X2/Y2).  The frozen M3 locks use
        # those values directly.  A standalone overall/gap bound is different.
        return True
    return not bool(re.search(r"\b(?:max|min|ref)\b|最大|最小|参考", text))


def _quad_ep_chain_operand(item: Dimension) -> bool:
    """Allow only exact solder-land values in arithmetic relation chains."""

    if item.belongs_to != "solder_land":
        return False
    text = unicodedata.normalize("NFKC", f"{item.symbol} {item.raw}").lower()
    if re.search(
        r"(?:\d+(?:\.\d+)?|\.\d+)\s*(?:-|–|—|~|～|\bto\b|至|到)\s*"
        r"(?:\d+(?:\.\d+)?|\.\d+)",
        text,
    ):
        return False
    return not bool(
        re.search(
            r"\b(?:min(?:imum)?|max(?:imum)?|ref(?:erence)?)\b|最大|最小|参考",
            text,
        )
    )


def _quad_ep_square_marked(item: Dimension) -> bool:
    text = unicodedata.normalize("NFKC", f"{item.symbol} {item.raw}")
    return any(marker in text for marker in ("□", "\u25a1", "square", "方形"))


def _quad_ep_opposing_axis(item: Dimension) -> str | None:
    """Return an axis only when endpoints name two opposing peripheral rows."""

    parts = _relation_endpoint_parts(item)
    if parts is None:
        return None

    marker_groups = {
        "left": (
            "左侧", "左列", "左排", "最左", "leftrow", "leftcolumn",
            "leftside", "leftmost", "leftsmall",
        ),
        "right": (
            "右侧", "右列", "右排", "最右", "rightrow", "rightcolumn",
            "rightside", "rightmost", "rightsmall",
        ),
        "top": (
            "上排", "上方", "上侧", "顶部", "最上", "toprow", "upperrow",
            "topside", "upperside", "topmost", "topsmall",
        ),
        "bottom": (
            "下排", "下方", "下侧", "底部", "最下", "bottomrow", "lowerrow",
            "bottomside", "lowerside", "bottommost", "bottomsmall",
        ),
    }

    sides: set[str] = set()
    for part in parts:
        text = re.sub(
            r"[\s_\-]+", "", unicodedata.normalize("NFKC", part).lower()
        )
        matches = {
            side
            for side, markers in marker_groups.items()
            if any(marker in text for marker in markers)
        }
        if len(matches) == 1:
            sides.update(matches)
    if sides == {"left", "right"}:
        return "width"
    if sides == {"top", "bottom"}:
        return "height"
    return None


def _quad_ep_is_tangential_center_span(item: Dimension) -> bool:
    """Reject a within-row/column span from the opposing-row center evidence."""

    axis = _quad_ep_opposing_axis(item)
    if axis is None:
        return False
    text = re.sub(
        r"[\s_\-]+", "", unicodedata.normalize("NFKC", item.endpoints or "").lower()
    )
    same_line_markers = {
        "width": ("同排", "同一排", "每排", "samerow", "eachrow"),
        "height": ("同列", "同一列", "每列", "samecolumn", "eachcolumn"),
    }
    return any(marker in text for marker in same_line_markers[axis])


def _quad_ep_dimension_multiplicity(item: Dimension) -> int | None:
    text = unicodedata.normalize(
        "NFKC", f"{item.symbol} {item.raw} {item.endpoints}"
    ).lower()
    values = [
        int(match.group(1))
        for match in _FAMILY_PAD_MULTIPLICITY_RE.finditer(text)
        if match.group(1)
    ]
    return max(values) if values else None


def _quad_ep_unique_value(
    items: Iterable[Dimension],
) -> tuple[float | None, tuple[Dimension, ...], str | None]:
    selected: list[Dimension] = []
    values: list[float] = []
    seen: set[tuple[float, str, str]] = set()
    for item in items:
        try:
            value = dimension_value_mm(item)
        except E1aError:
            continue
        key = (round(value, 9), item.raw, item.endpoints)
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        values.append(value)
    if not values:
        return None, (), "missing"
    distinct = sorted({round(value, 6) for value in values})
    if len(distinct) != 1:
        return None, tuple(selected), "multiple_direct_values"
    return values[0], tuple(selected), None


def _quad_ep_peripheral_identity_count(
    items: tuple[Dimension, ...],
) -> tuple[int | None, int | None, str]:
    """Return exact count only when the drawing explicitly numbers/multiplies pads."""

    raw_count = _family_solder_land_pad_count(items)
    text = unicodedata.normalize(
        "NFKC",
        " ".join(f"{item.symbol} {item.raw} {item.endpoints}" for item in items),
    ).lower()
    multiplicities = [
        value
        for item in items
        if (value := _quad_ep_dimension_multiplicity(item)) is not None
    ]
    if multiplicities:
        return max(multiplicities), raw_count, "explicit_multiplicity"

    explicit_ids = set(
        re.findall(
            r"(?:pad|land|pin|焊盘|引脚)\s*#?\s*(\d+)\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    if explicit_ids:
        return len(explicit_ids), raw_count, "explicit_identifiers"
    if raw_count is None:
        return None, None, "not_visible"
    # Positional phrases such as "top small pad" and "one small pad" are
    # category/group evidence, not distinct pin identities.
    return 2, raw_count, "generic_peripheral_groups"


def _quad_ep_topology_evidence(
    transcription: Transcription,
    page_text: str | None,
) -> dict[str, Any]:
    """Prove QUAD/EP geometry by closed relations; never rank numeric candidates."""

    ep_evidence = _ep_positive_evidence(transcription, page_text)
    base: dict[str, Any] = {
        "schema": "m3_quad_ep_topology_gate_v1",
        "status": "not_applicable" if ep_evidence is None else "insufficient",
        "reason": "没有裸露焊盘正证据。" if ep_evidence is None else "QUAD/EP 关系链不完整。",
        "ep_evidence": ep_evidence,
        "form": None,
        "separation_axis": None,
        "peripheral_count": transcription.pin_count,
        "row_counts": None,
        "solved": {},
        "sources": {},
        "operands": {},
        "checks": [],
        "thermal_vias_excluded": [],
    }
    if ep_evidence is None:
        return base

    direct: list[Dimension] = []
    vias: list[Dimension] = []
    for item in transcription.dimensions:
        if not _quad_ep_relation_nominal(item):
            continue
        try:
            dimension_value_mm(item)
        except E1aError:
            continue
        if _quad_ep_is_thermal_via(item):
            vias.append(item)
            continue
        direct.append(item)
    base["thermal_vias_excluded"] = [asdict(item) for item in vias]

    land = tuple(item for item in direct if item.belongs_to == "solder_land")
    ep_items = tuple(item for item in land if _quad_ep_is_center_pad(item))
    peripheral = tuple(item for item in land if item not in ep_items)
    if not ep_items:
        return {
            **base,
            "reason": "检出 EP 文字，但 solder_land 分区没有可证明双轴的中央焊盘尺寸。",
        }

    observed_count, observed_count_raw, observed_count_source = (
        _quad_ep_peripheral_identity_count(peripheral)
    )
    if observed_count is not None and observed_count > 2:
        peripheral_count = observed_count
        if (
            transcription.pin_count is not None
            and transcription.pin_count not in {peripheral_count, peripheral_count + 1}
        ):
            return {
                **base,
                "observed_peripheral_identity_count": observed_count,
                "observed_peripheral_identity_count_raw": observed_count_raw,
                "observed_peripheral_identity_count_source": observed_count_source,
                "reason": (
                    f"图中外围焊盘数 {peripheral_count} 与 pin_count="
                    f"{transcription.pin_count} 不闭合（仅允许 EP 令总数多 1）。"
                ),
            }
    elif transcription.pin_count is not None:
        peripheral_count = transcription.pin_count
    else:
        return {
            **base,
            "observed_peripheral_identity_count": observed_count,
            "observed_peripheral_identity_count_raw": observed_count_raw,
            "observed_peripheral_identity_count_source": observed_count_source,
            "reason": "QUAD/EP 缺少可证明的外围焊盘总数。",
        }
    if not 4 <= peripheral_count <= 48:
        return {
            **base,
            "peripheral_count": peripheral_count,
            "reason": "QUAD/EP 外围焊盘总数不在 4..48 范围内。",
        }
    base["peripheral_count"] = peripheral_count

    def mixed_peripheral_ep(item: Dimension) -> bool:
        text = _quad_ep_item_text(item)
        peripheral_marked = any(
            marker in text for marker in ("small", "contact pad", "terminal", "小焊盘", "端子焊盘")
        )
        ep_marked = any(
            marker in text for marker in ("center pad", "exposed", "thermal pad", "大焊盘", "中央焊盘")
        )
        return peripheral_marked and ep_marked

    def peripheral_inner_span(item: Dimension) -> bool:
        parts = _relation_endpoint_parts(item)
        if parts is None:
            return False

        def peripheral_endpoint(part: str) -> bool:
            normalized = unicodedata.normalize("NFKC", part).lower()
            return any(
                marker in normalized
                for marker in (
                    "small",
                    "contact pad",
                    "terminal pad",
                    "小焊盘",
                    "端子焊盘",
                )
            )

        return all(peripheral_endpoint(part) for part in parts)

    shape_items = tuple(
        item
        for item in peripheral
        if item.role in {"pad_width", "pad_height"} and not mixed_peripheral_ep(item)
    )
    shape_by_axis = {
        axis: tuple(item for item in shape_items if _quad_ep_axis(item) == axis)
        for axis in ("width", "height")
    }
    ep_by_axis = {
        axis: tuple(
            item
            for item in ep_items
            if _quad_ep_axis(item) == axis or _quad_ep_square_marked(item)
        )
        for axis in ("width", "height")
    }
    topology_centers_by_axis = {
        axis: tuple(
            item
            for item in peripheral
            if item.role == "pad_center_distance"
            and _quad_ep_opposing_axis(item) == axis
            and not _quad_ep_is_tangential_center_span(item)
            and _relation_center_marked(item)
        )
        for axis in ("width", "height")
    }
    topology_inner_gaps_by_axis = {
        axis: tuple(
            item
            for item in peripheral
            if item.role in {"other", "pad_gap", "gap_between_pads"}
            and not mixed_peripheral_ep(item)
            and peripheral_inner_span(item)
            and _quad_ep_opposing_axis(item) == axis
            and not _relation_center_marked(item)
        )
        for axis in ("width", "height")
    }
    topology_outer_spans_by_axis = {
        axis: tuple(
            item
            for item in land
            if item.role == "overall_span"
            and (_quad_ep_opposing_axis(item) or _quad_ep_axis(item)) == axis
        )
        for axis in ("width", "height")
    }
    centers_by_axis = {
        axis: tuple(
            item
            for item in topology_centers_by_axis[axis]
            if _quad_ep_chain_operand(item)
        )
        for axis in ("width", "height")
    }
    inner_gaps_by_axis = {
        axis: tuple(
            item
            for item in topology_inner_gaps_by_axis[axis]
            if _quad_ep_chain_operand(item)
        )
        for axis in ("width", "height")
    }
    outer_spans_by_axis = {
        axis: tuple(
            item
            for item in topology_outer_spans_by_axis[axis]
            if _quad_ep_chain_operand(item)
        )
        for axis in ("width", "height")
    }
    ep_gaps_by_axis = {
        axis: tuple(
            item
            for item in peripheral
            if item.role in {"pad_gap", "gap_between_pads"}
            and mixed_peripheral_ep(item)
            and (_quad_ep_axis(item) in {axis, None})
            and _quad_ep_chain_operand(item)
        )
        for axis in ("width", "height")
    }

    relation_axes = {
        axis
        for axis in ("width", "height")
        if topology_centers_by_axis[axis]
        or topology_inner_gaps_by_axis[axis]
        or topology_outer_spans_by_axis[axis]
    }

    checks: list[dict[str, Any]] = []

    def unique(
        label: str, candidates: Iterable[Dimension]
    ) -> tuple[float | None, tuple[Dimension, ...]]:
        value, selected, error = _quad_ep_unique_value(candidates)
        checks.append(
            {
                "label": label,
                "status": "pass" if error is None else error,
                "value_mm": value,
                "evidence": [asdict(item) for item in selected],
            }
        )
        return value, selected

    def closed_value(
        label: str,
        candidates: list[tuple[float, str, tuple[Dimension, ...]]],
    ) -> tuple[float | None, str | None, tuple[Dimension, ...]]:
        if not candidates:
            checks.append({"label": label, "status": "missing", "candidates": []})
            return None, None, ()
        reference = candidates[0][0]
        closed = all(
            math.isclose(reference, value, rel_tol=0.0, abs_tol=_FAMILY_SIZE_TOLERANCE_MM)
            for value, _source, _items in candidates[1:]
        )
        checks.append(
            {
                "label": label,
                "status": "pass" if closed else "relation_conflict",
                "candidates": [
                    {"value_mm": value, "source": source}
                    for value, source, _items in candidates
                ],
            }
        )
        if not closed:
            return None, None, tuple(
                item for _value, _source, items in candidates for item in items
            )
        return reference, candidates[0][1], candidates[0][2]

    # Some vendor tables contain a mislabeled auxiliary row value.  It may be
    # promoted to center distance only when one axis closes exactly as
    # EP + 2*gap + pad, and the other axis does not.
    closure_centers_by_axis: dict[
        str, tuple[float, tuple[Dimension, ...]]
    ] = {}
    for axis in ("width", "height"):
        pad_value, pad_items, pad_error = _quad_ep_unique_value(shape_by_axis[axis])
        ep_value, axis_ep_items, ep_error = _quad_ep_unique_value(ep_by_axis[axis])
        gap_value, gap_items, gap_error = _quad_ep_unique_value(ep_gaps_by_axis[axis])
        if any(error is not None for error in (pad_error, ep_error, gap_error)):
            continue
        expected_center = float(pad_value + ep_value + 2.0 * gap_value)
        excluded_ids = {
            id(item)
            for item in pad_items + axis_ep_items + gap_items
        }
        matches = tuple(
            item
            for item in peripheral
            if id(item) not in excluded_ids
            and item.role in {"other", "pad_center_distance", "overall_span"}
            and _quad_ep_chain_operand(item)
            and math.isclose(
                dimension_value_mm(item),
                expected_center,
                rel_tol=0.0,
                abs_tol=_FAMILY_SIZE_TOLERANCE_MM,
            )
        )
        distinct_matches = {
            round(dimension_value_mm(item), 6) for item in matches
        }
        if len(distinct_matches) == 1:
            closure_centers_by_axis[axis] = (
                expected_center,
                pad_items + axis_ep_items + gap_items + matches,
            )
            relation_axes.add(axis)

    if not relation_axes:
        return {
            **base,
            "checks": checks,
            "reason": "外围焊盘没有可证明的相对排轴或唯一闭合中心距。",
        }
    if relation_axes == {"width", "height"}:
        form = "four_sided"
    elif len(relation_axes) == 1:
        form = "two_row"
    else:
        return {**base, "checks": checks, "reason": "QUAD/EP 排间图轴矛盾。"}

    pitch_items = tuple(
        item
        for item in peripheral
        if item.role == "pitch"
    )
    common_pitch_items = tuple(item for item in pitch_items if _quad_ep_axis(item) is None)

    if form == "two_row":
        separation_axis = next(iter(relation_axes))
        cross_axis = "height" if separation_axis == "width" else "width"
        if peripheral_count % 2:
            return {**base, "form": form, "separation_axis": separation_axis,
                    "reason": "两排 QUAD/EP 的外围 pin_count 不是偶数。"}
        row_count = peripheral_count // 2

        direct_pad, direct_pad_items = unique(
            f"pad_normal_{separation_axis}", shape_by_axis[separation_axis]
        )
        inner, inner_items = unique(
            f"inner_gap_{separation_axis}", inner_gaps_by_axis[separation_axis]
        )
        outer, outer_items = unique(
            f"outer_span_{separation_axis}", outer_spans_by_axis[separation_axis]
        )
        direct_center, direct_center_items = unique(
            f"center_{separation_axis}", centers_by_axis[separation_axis]
        )
        ep_gap, ep_gap_items = unique(
            f"ep_gap_{separation_axis}", ep_gaps_by_axis[separation_axis]
        )
        ep_normal, ep_normal_items = unique(
            f"ep_{separation_axis}", ep_by_axis[separation_axis]
        )
        ep_cross, ep_cross_items = unique(f"ep_{cross_axis}", ep_by_axis[cross_axis])
        pad_cross, pad_cross_items = unique(
            f"pad_tangent_{cross_axis}", shape_by_axis[cross_axis]
        )
        pitch_axis_items = tuple(
            item for item in pitch_items if _quad_ep_axis(item) in {None, cross_axis}
        )
        pitch, pitch_sources = unique("row_pitch", pitch_axis_items)

        pad_candidates: list[tuple[float, str, tuple[Dimension, ...]]] = []
        if direct_pad is not None:
            pad_candidates.append((direct_pad, "直接单焊盘法向尺寸", direct_pad_items))
        if outer is not None and inner is not None and outer > inner:
            pad_candidates.append(
                ((outer - inner) / 2.0, "(外围总跨-内缘间隙)/2", outer_items + inner_items)
            )
        if direct_center is not None and inner is not None and direct_center > inner:
            pad_candidates.append(
                (direct_center - inner, "中心距-内缘间隙", direct_center_items + inner_items)
            )
        if outer is not None and direct_center is not None and outer > direct_center:
            pad_candidates.append(
                (outer - direct_center, "外围总跨-中心距", outer_items + direct_center_items)
            )
        if (
            direct_center is not None
            and ep_normal is not None
            and ep_gap is not None
            and direct_center > ep_normal + 2.0 * ep_gap
        ):
            pad_candidates.append(
                (
                    direct_center - ep_normal - 2.0 * ep_gap,
                    "中心距-EP尺寸-2×EP间隙",
                    direct_center_items + ep_normal_items + ep_gap_items,
                )
            )
        pad_normal, pad_source, pad_operands = closed_value(
            "pad_normal_closed_chain", pad_candidates
        )

        center_candidates: list[tuple[float, str, tuple[Dimension, ...]]] = []
        if direct_center is not None:
            center_candidates.append((direct_center, "直接排中心距", direct_center_items))
        closure_center = closure_centers_by_axis.get(separation_axis)
        if closure_center is not None:
            center_candidates.append(
                (
                    closure_center[0],
                    "辅助值与 EP+2×间隙+焊盘长度精确闭合",
                    closure_center[1],
                )
            )
        if pad_normal is not None and inner is not None:
            center_candidates.append(
                (inner + pad_normal, "内缘间隙+单焊盘法向尺寸", inner_items + pad_operands)
            )
        if pad_normal is not None and outer is not None:
            center_candidates.append(
                (outer - pad_normal, "外围总跨-单焊盘法向尺寸", outer_items + pad_operands)
            )
        if pad_normal is not None and ep_normal is not None and ep_gap is not None:
            center_candidates.append(
                (
                    ep_normal + 2.0 * ep_gap + pad_normal,
                    "EP尺寸+2×EP间隙+单焊盘法向尺寸",
                    ep_normal_items + ep_gap_items + pad_operands,
                )
            )
        center, center_source, center_operands = closed_value(
            "center_closed_chain", center_candidates
        )
        required_without_center = (pad_normal, pad_cross, pitch, ep_normal, ep_cross)
        if any(value is None for value in required_without_center):
            return {
                **base,
                "form": form,
                "separation_axis": separation_axis,
                "row_counts": [row_count, row_count, 0, 0],
                "checks": checks,
                "reason": "两排 QUAD/EP 的焊盘双轴、pitch、中心距或 EP 双轴未形成唯一闭合解。",
            }
        partial_center = center is None
        if center is not None and (
            center <= pad_normal or (row_count > 1 and pitch <= pad_cross)
        ):
            return {
                **base,
                "form": form,
                "separation_axis": separation_axis,
                "checks": checks,
                "reason": "两排 QUAD/EP 的中心距或行内 pitch 会导致外围焊盘重叠。",
            }
        solved = {
            "pad_x": float(pad_normal),
            "pad_y": float(pad_cross),
            "center_y": 0.0,
            "pitch_y": float(pitch),
            "pitch_x": 0.0,
            "tab_x": float(ep_normal),
            "tab_y": float(ep_cross),
            "quad_left_count": float(row_count),
            "quad_right_count": float(row_count),
            "quad_top_count": 0.0,
            "quad_bottom_count": 0.0,
        }
        if center is not None:
            solved["center_x"] = float(center)
        sources = {
            "pad_x": str(pad_source),
            "pad_y": f"直接单焊盘切向尺寸（原图 {cross_axis}）",
            "center_x": (
                str(center_source)
                if center_source is not None
                else "未形成仅由 solder_land 未限定值组成的唯一中心距链，需人工填写"
            ),
            "pitch_y": "相邻外围焊盘中心距",
            "tab_x": f"EP 原图 {separation_axis} 轴直接尺寸",
            "tab_y": f"EP 原图 {cross_axis} 轴直接尺寸",
            "quad_left_count": f"外围焊盘总数 {peripheral_count}/2",
            "quad_right_count": f"外围焊盘总数 {peripheral_count}/2",
        }
        operands = {
            "pad_x": pad_operands,
            "pad_y": pad_cross_items,
            "center_x": center_operands,
            "pitch_y": pitch_sources,
            "tab_x": ep_normal_items,
            "tab_y": ep_cross_items,
        }
        combination = (
            f"REL_QUAD_EP_{separation_axis}_TWO_ROW_PARTIAL_CENTER"
            if partial_center
            else f"REL_QUAD_EP_{separation_axis}_TWO_ROW"
        )
        row_counts = [row_count, row_count, 0, 0]
        result_status = "partial" if partial_center else "auto"
    else:
        if peripheral_count % 4:
            return {**base, "form": form, "reason": "四边 QUAD/EP 的外围 pin_count 不能四等分。"}
        row_count = peripheral_count // 4

        shape_groups: dict[float, list[Dimension]] = {}
        for item in shape_items:
            shape_groups.setdefault(round(dimension_value_mm(item), 6), []).append(item)
        checks.append(
            {
                "label": "peripheral_shape_pair",
                "status": "pass" if len(shape_groups) == 2 else "not_unique_pair",
                "values_mm": sorted(shape_groups),
                "evidence": [asdict(item) for item in shape_items],
            }
        )
        if len(shape_groups) != 2:
            return {
                **base,
                "form": form,
                "row_counts": [row_count] * 4,
                "checks": checks,
                "reason": "四边 QUAD/EP 的外围单焊盘双轴不是唯一一对。",
            }

        global_pitch_items = tuple(
            item
            for item in pitch_items
            if _quad_ep_axis(item) is None
            or _quad_ep_dimension_multiplicity(item) == peripheral_count - 4
        )
        if global_pitch_items:
            pitch_common, pitch_common_sources = unique(
                "pitch_common_four_sides", global_pitch_items
            )
            pitch_width = pitch_height = pitch_common
            pitch_width_sources = pitch_height_sources = pitch_common_sources
        else:
            pitch_width, pitch_width_sources = unique(
                "pitch_width",
                tuple(item for item in pitch_items if _quad_ep_axis(item) == "width"),
            )
            pitch_height, pitch_height_sources = unique(
                "pitch_height",
                tuple(item for item in pitch_items if _quad_ep_axis(item) == "height"),
            )

        direct_center_width, direct_center_width_sources = unique(
            "center_width", centers_by_axis["width"]
        )
        direct_center_height, direct_center_height_sources = unique(
            "center_height", centers_by_axis["height"]
        )
        outer_width, outer_width_sources = unique(
            "outer_span_width", outer_spans_by_axis["width"]
        )
        outer_height, outer_height_sources = unique(
            "outer_span_height", outer_spans_by_axis["height"]
        )
        ep_width, ep_width_sources = unique("ep_width", ep_by_axis["width"])
        ep_height, ep_height_sources = unique("ep_height", ep_by_axis["height"])

        if any(
            value is None
            for value in (pitch_width, pitch_height, ep_width, ep_height)
        ):
            return {
                **base,
                "form": form,
                "row_counts": [row_count] * 4,
                "checks": checks,
                "reason": "四边 QUAD/EP 缺少两轴 pitch 或 EP 双轴。",
            }

        def axis_center(
            axis: str,
            pad_length_value: float,
            direct_center: float | None,
            direct_sources: tuple[Dimension, ...],
            outer_span: float | None,
            outer_sources: tuple[Dimension, ...],
        ) -> tuple[float | None, str | None, tuple[Dimension, ...]]:
            candidates: list[tuple[float, str, tuple[Dimension, ...]]] = []
            if direct_center is not None:
                candidates.append((direct_center, f"原图 {axis} 轴直接排中心距", direct_sources))
            if outer_span is not None and outer_span > pad_length_value:
                candidates.append(
                    (
                        outer_span - pad_length_value,
                        f"原图 {axis} 轴外围总跨-单焊盘法向长度",
                        outer_sources,
                    )
                )
            if not candidates:
                return None, None, ()
            reference = candidates[0][0]
            if any(
                not math.isclose(
                    reference,
                    value,
                    rel_tol=0.0,
                    abs_tol=_FAMILY_SIZE_TOLERANCE_MM,
                )
                for value, _source, _items in candidates[1:]
            ):
                return None, None, tuple(
                    item for _value, _source, items in candidates for item in items
                )
            return (
                reference,
                " + ".join(source for _value, source, _items in candidates),
                tuple(item for _value, _source, items in candidates for item in items),
            )

        shape_values = sorted(shape_groups)
        orientation_candidates: list[dict[str, Any]] = []
        for pad_length_value, pad_width_value in (
            (shape_values[0], shape_values[1]),
            (shape_values[1], shape_values[0]),
        ):
            center_width, center_width_source, center_width_sources = axis_center(
                "width",
                pad_length_value,
                direct_center_width,
                direct_center_width_sources,
                outer_width,
                outer_width_sources,
            )
            center_height, center_height_source, center_height_sources = axis_center(
                "height",
                pad_length_value,
                direct_center_height,
                direct_center_height_sources,
                outer_height,
                outer_height_sources,
            )
            valid = (
                center_width is not None
                and center_height is not None
                and center_width > pad_length_value
                and center_height > pad_length_value
                and (
                    row_count <= 1
                    or (
                        pitch_width > pad_width_value
                        and pitch_height > pad_width_value
                    )
                )
            )
            orientation_candidates.append(
                {
                    "pad_length": pad_length_value,
                    "pad_width": pad_width_value,
                    "center_width": center_width,
                    "center_height": center_height,
                    "center_width_source": center_width_source,
                    "center_height_source": center_height_source,
                    "center_width_sources": center_width_sources,
                    "center_height_sources": center_height_sources,
                    "valid": valid,
                }
            )
        checks.append(
            {
                "label": "four_side_orientation_closure",
                "status": (
                    "pass"
                    if sum(bool(candidate["valid"]) for candidate in orientation_candidates) == 1
                    else "not_unique"
                ),
                "candidates": [
                    {
                        key: value
                        for key, value in candidate.items()
                        if not key.endswith("_sources")
                    }
                    for candidate in orientation_candidates
                ],
            }
        )
        valid_orientations = [
            candidate for candidate in orientation_candidates if candidate["valid"]
        ]
        if len(valid_orientations) != 1:
            return {
                **base,
                "form": form,
                "row_counts": [row_count] * 4,
                "checks": checks,
                "reason": "四边 QUAD/EP 的焊盘法向/切向与两轴中心距没有唯一自洽指派。",
            }
        orientation = valid_orientations[0]
        pad_length = float(orientation["pad_length"])
        pad_width = float(orientation["pad_width"])
        center_width = float(orientation["center_width"])
        center_height = float(orientation["center_height"])
        center_width_sources = orientation["center_width_sources"]
        center_height_sources = orientation["center_height_sources"]
        pad_length_sources = tuple(shape_groups[round(pad_length, 6)])
        pad_width_sources = tuple(shape_groups[round(pad_width, 6)])
        solved = {
            "pad_x": float(pad_length),
            "pad_y": float(pad_width),
            "center_x": float(center_width),
            "center_y": float(center_height),
            "pitch_y": float(pitch_height),
            "pitch_x": float(pitch_width),
            "tab_x": float(ep_width),
            "tab_y": float(ep_height),
            "quad_left_count": float(row_count),
            "quad_right_count": float(row_count),
            "quad_top_count": float(row_count),
            "quad_bottom_count": float(row_count),
        }
        sources = {
            "pad_x": "双轴候选中唯一通过中心距/pitch 自洽闸的法向长度",
            "pad_y": "双轴候选中唯一通过中心距/pitch 自洽闸的切向宽度",
            "center_x": str(orientation["center_width_source"]),
            "center_y": str(orientation["center_height_source"]),
            "pitch_y": "原图 height 轴行内 pitch",
            "pitch_x": "原图 width 轴行内 pitch",
            "tab_x": "EP 原图 width 轴直接尺寸",
            "tab_y": "EP 原图 height 轴直接尺寸",
            "quad_left_count": f"外围焊盘总数 {peripheral_count}/4",
            "quad_right_count": f"外围焊盘总数 {peripheral_count}/4",
            "quad_top_count": f"外围焊盘总数 {peripheral_count}/4",
            "quad_bottom_count": f"外围焊盘总数 {peripheral_count}/4",
        }
        operands = {
            "pad_x": pad_length_sources,
            "pad_y": pad_width_sources,
            "center_x": center_width_sources,
            "center_y": center_height_sources,
            "pitch_y": pitch_height_sources,
            "pitch_x": pitch_width_sources,
            "tab_x": ep_width_sources,
            "tab_y": ep_height_sources,
        }
        combination = "REL_QUAD_EP_TWO_AXES"
        row_counts = [row_count] * 4
        separation_axis = None
        result_status = "auto"

    non_land_operands = [
        (field, item)
        for field, field_operands in operands.items()
        if field
        in {
            "pad_x",
            "pad_y",
            "pitch_y",
            "pitch_x",
            "center_x",
            "center_y",
            "tab_x",
            "tab_y",
        }
        for item in field_operands
        if item.belongs_to != "solder_land"
    ]
    if non_land_operands:
        return {
            **base,
            "form": form,
            "separation_axis": separation_axis,
            "row_counts": row_counts,
            "checks": checks,
            "reason": "QUAD/EP 焊盘关系链含非 solder_land 操作数，已按分区隔离守卫拒绝。",
        }

    return {
        **base,
        "status": result_status,
        "reason": (
            "外围排数、焊盘双轴、pitch 与 EP 双轴已由 solder_land 直接值证明；"
            "中心距没有仅由未限定 solder_land 值组成的唯一闭合链，保留部分自动并转人工填写。"
            if result_status == "partial"
            else "外围排数、焊盘双轴、pitch、中心距与 EP 双轴均由直接值/闭合关系唯一证明。"
        ),
        "form": form,
        "separation_axis": separation_axis,
        "row_counts": row_counts,
        "combination": combination,
        "solved": solved,
        "sources": sources,
        "operands": operands,
        "checks": checks,
        "observed_peripheral_identity_count": observed_count,
        "observed_peripheral_identity_count_raw": observed_count_raw,
        "observed_peripheral_identity_count_source": observed_count_source,
    }


def _dual_row_side(segment: str, *, expanded_aliases: bool = True) -> str | None:
    text = re.sub(
        r"[\s_\-]+", "", unicodedata.normalize("NFKC", segment or "").lower()
    )
    marker_groups = {
        "top": ("上排", "上方一排", "toprow", "upperrow"),
        "bottom": ("下排", "下方一排", "bottomrow", "lowerrow"),
        "left": ("左排", "左侧一排", "leftrow"),
        "right": ("右排", "右侧一排", "rightrow"),
    }
    if expanded_aliases:
        marker_groups = {
            "top": marker_groups["top"]
            + ("顶部小焊盘", "顶部端子焊盘", "topcolumn"),
            "bottom": marker_groups["bottom"]
            + ("底部小焊盘", "底部端子焊盘", "bottomcolumn"),
            "left": marker_groups["left"]
            + ("左列", "左侧小焊盘", "左侧端子焊盘", "leftcolumn"),
            "right": marker_groups["right"]
            + ("右列", "右侧小焊盘", "右侧端子焊盘", "rightcolumn"),
        }
    matches = [
        side
        for side, markers in marker_groups.items()
        if any(marker in text for marker in markers)
    ]
    return matches[0] if len(matches) == 1 else None


def _dual_row_relation_axis(
    item: Dimension,
    *,
    expanded_aliases: bool = True,
) -> str | None:
    parts = _relation_endpoint_parts(item)
    if parts is None:
        return None
    sides = frozenset(
        side
        for part in parts
        if (side := _dual_row_side(part, expanded_aliases=expanded_aliases))
        is not None
    )
    if sides == {"top", "bottom"}:
        return "height"
    if sides == {"left", "right"}:
        return "width"
    return None


def _dual_page_has_ep_evidence(
    transcription: Transcription,
    page_text: str | None,
) -> bool:
    if _family_has_thermal_evidence(transcription):
        return True
    normalized = unicodedata.normalize("NFKC", page_text or "").lower()
    return bool(
        re.search(
            r"\b(?:ep|epad)\b|exposed\s+(?:die\s+)?pad|thermal\s+pad|powerpad|"
            r"散热焊盘|中央焊盘|中心焊盘",
            normalized,
        )
    )


def _dual_topology_evidence(
    transcription: Transcription,
    items: tuple[Dimension, ...],
    page_text: str | None = None,
) -> dict[str, Any]:
    """Prove a two-row gull-wing topology; reject or decline without guessing."""

    nominal = tuple(item for item in items if not _family_tolerance_only(item))
    pin_count = transcription.pin_count
    expanded_aliases = pin_count != 4

    def row_axis(item: Dimension) -> str | None:
        return _dual_row_relation_axis(item, expanded_aliases=expanded_aliases)

    row_relations = tuple(
        item
        for item in nominal
        if row_axis(item) in {"width", "height"}
    )
    row_axes = {row_axis(item) for item in row_relations}
    shape_by_axis = {
        axis: tuple(
            item
            for item in nominal
            if item.role in {"pad_width", "pad_height"}
            and _family_topology_axis(item) == axis
        )
        for axis in ("width", "height")
    }
    shape_present = all(shape_by_axis.values())
    pitch_items = tuple(
        item
        for item in nominal
        if item.role in {"pitch", "pad_center_distance"}
    )
    layout_signal = bool(
        (pin_count is None or pin_count >= 4) and row_relations
    ) or bool(pin_count is not None and pin_count >= 4 and shape_present and pitch_items)
    base = {
        "schema": "m2_dual_topology_gate_v1",
        "status": "not_applicable",
        "reason": "没有形成两排 DUAL 拓扑证据。",
        "separation_axis": None,
        "pad_count": None,
        "row_counts": None,
        "shape_values": {},
        "pitch_y": None,
        "evidence": [asdict(item) for item in row_relations],
    }
    if not layout_signal:
        return base
    if _dual_page_has_ep_evidence(transcription, page_text):
        return {
            **base,
            "status": "reject_ep",
            "reason": "待 M3 QFN/DFN 族",
        }

    normalized_page = unicodedata.normalize("NFKC", page_text or "").lower()
    has_reflow = bool(re.search(r"re[\s-]*flow|回流", normalized_page))
    has_wave = bool(re.search(r"wave\s*(?:solder(?:ing)?)?|波峰", normalized_page))
    if has_reflow and has_wave:
        return {
            **base,
            "status": "manual_required",
            "reason": "同页同时出现 reflow 与 wave soldering；禁止跨工艺混合尺寸。",
        }
    if len(row_axes) != 1:
        return {
            **base,
            "status": "insufficient",
            "reason": "两排分离轴没有形成唯一图轴。",
        }
    separation_axis = next(iter(row_axes))
    cross_axis = "height" if separation_axis == "width" else "width"

    shape_values: dict[str, float] = {}
    shape_sources: dict[str, tuple[Dimension, ...]] = {}
    for axis, axis_items in shape_by_axis.items():
        value, sources = _family_unique_axis_value(axis_items)
        if value is None:
            status = "manual_required" if axis_items else "insufficient"
            reason = (
                f"{axis} 轴出现非均匀单焊盘尺寸；禁止均匀化建模。"
                if axis_items
                else f"缺少 {axis} 轴单焊盘尺寸。"
            )
            return {
                **base,
                "status": status,
                "reason": reason,
                "separation_axis": separation_axis,
            }
        shape_values[axis] = float(value)
        shape_sources[axis] = tuple(sources)

    if shape_values[separation_axis] <= shape_values[cross_axis]:
        return {
            **base,
            "status": "not_applicable",
            "reason": (
                "单焊盘未沿两排分离轴向外伸长，未形成可证的鸥翼 DUAL 形制；"
                "交回既有族闸。"
            ),
            "separation_axis": separation_axis,
            "shape_values": shape_values,
        }

    in_row_pitch_items = tuple(
        item
        for item in pitch_items
        if _family_topology_axis(item) in {None, cross_axis}
        and row_axis(item) is None
    )
    pitch_value, pitch_sources = _family_unique_axis_value(in_row_pitch_items)
    if pitch_value is None or pitch_value <= shape_values[cross_axis]:
        return {
            **base,
            "status": "insufficient",
            "reason": "行内 pitch 缺失、多值或不大于行内焊盘宽度。",
            "separation_axis": separation_axis,
            "shape_values": shape_values,
        }
    if pin_count is None or pin_count < 4:
        return {
            **base,
            "status": "insufficient",
            "reason": "DUAL 拓扑缺少至少四脚的 pin_count 证据。",
            "separation_axis": separation_axis,
            "shape_values": shape_values,
            "pitch_y": float(pitch_value),
        }
    if pin_count == 5:
        row_counts = (3, 2)
    elif pin_count % 2 == 0:
        row_counts = (pin_count // 2, pin_count // 2)
    else:
        return {
            **base,
            "status": "manual_required",
            "reason": "两排行数不等且不是已知 3+2 形态，转人工。",
            "separation_axis": separation_axis,
            "shape_values": shape_values,
            "pitch_y": float(pitch_value),
        }

    observed_count = _family_solder_land_pad_count(items)
    if observed_count not in {None, 2, pin_count}:
        return {
            **base,
            "status": "manual_required",
            "reason": (
                f"可见 solder_land 身份数 {observed_count} 与 pin_count={pin_count} "
                "不闭合，禁止自动建模。"
            ),
            "separation_axis": separation_axis,
            "shape_values": shape_values,
            "pitch_y": float(pitch_value),
        }

    status = "multi_family_conflict" if pin_count == 4 else "auto"
    reason = (
        "2+2 两排与 GRID-4 几何等价，必须人工裁族。"
        if status == "multi_family_conflict"
        else "两排分离轴、统一焊盘双轴、行内 pitch 与排数均闭合。"
    )
    return {
        **base,
        "status": status,
        "reason": reason,
        "separation_axis": separation_axis,
        "pad_count": pin_count,
        "row_counts": list(row_counts),
        "shape_values": shape_values,
        "shape_sources": {
            axis: [asdict(item) for item in sources]
            for axis, sources in shape_sources.items()
        },
        "pitch_y": float(pitch_value),
        "pitch_sources": [asdict(item) for item in pitch_sources],
        "evidence": [asdict(item) for item in row_relations],
    }


def _family_unique_axis_value(
    items: Iterable[Dimension],
) -> tuple[float | None, tuple[Dimension, ...]]:
    selected = tuple(items)
    if not selected:
        return None, ()
    values = [dimension_value_mm(item) for item in selected]
    if max(values) - min(values) > _FAMILY_SIZE_TOLERANCE_MM:
        return None, selected
    return sum(values) / len(values), selected


def _family_inline_endpoint_node(segment: str) -> str | None:
    text = re.sub(
        r"[\s_\-]+", "", unicodedata.normalize("NFKC", segment or "").lower()
    )
    marker_groups = {
        "left": ("左侧", "左边", "左端", "left"),
        "middle": ("中间", "中央", "居中", "middle", "center", "centre"),
        "right": ("右侧", "右边", "右端", "right"),
    }
    matches = [
        node
        for node, markers in marker_groups.items()
        if any(marker in text for marker in markers)
    ]
    return matches[0] if len(matches) == 1 else None


def _family_grid_relation_kind(item: Dimension) -> str | None:
    parts = _relation_endpoint_parts(item)
    if parts is None:
        return None
    left, right = (
        re.sub(r"[\s_\-]+", "", unicodedata.normalize("NFKC", part).lower())
        for part in parts
    )

    def has(text: str, markers: tuple[str, ...]) -> bool:
        return any(marker in text for marker in markers)

    horizontal = (
        has(left, ("左列", "左排", "左侧", "leftcolumn", "leftrow", "left"))
        and has(right, ("右列", "右排", "右侧", "rightcolumn", "rightrow", "right"))
    ) or (
        has(right, ("左列", "左排", "左侧", "leftcolumn", "leftrow", "left"))
        and has(left, ("右列", "右排", "右侧", "rightcolumn", "rightrow", "right"))
    )
    vertical = (
        has(left, ("上排", "上侧", "上方", "upperrow", "toprow", "upper", "top"))
        and has(right, ("下排", "下侧", "下方", "lowerrow", "bottomrow", "lower", "bottom"))
    ) or (
        has(right, ("上排", "上侧", "上方", "upperrow", "toprow", "upper", "top"))
        and has(left, ("下排", "下侧", "下方", "lowerrow", "bottomrow", "lower", "bottom"))
    )
    if horizontal == vertical:
        return None
    return "width" if horizontal else "height"


def _family_topology_axis(item: Dimension) -> str | None:
    return _relation_axis(item) or _family_grid_relation_kind(item)


def _family_new_topology_evidence(
    transcription: Transcription,
    items: tuple[Dimension, ...],
) -> tuple[str | None, str, int | None, tuple[str, ...]]:
    """Prove IN-LINE-3 or GRID-4 from pad topology; otherwise decline to choose."""

    if _family_has_thermal_evidence(transcription):
        return None, "检测到 exposed/thermal pad 迹象，新族闸拒绝。", None, ()
    nominal = tuple(item for item in items if not _family_tolerance_only(item))
    shape_by_axis = {
        axis: tuple(
            item
            for item in nominal
            if item.role in {"pad_width", "pad_height"}
            and _family_topology_axis(item) == axis
        )
        for axis in ("width", "height")
    }
    shape_values: dict[str, float] = {}
    for axis, axis_items in shape_by_axis.items():
        value, _ = _family_unique_axis_value(axis_items)
        if value is not None:
            shape_values[axis] = value
    if set(shape_values) != {"width", "height"}:
        return None, "新族闸缺少唯一的 width/height 单焊盘尺寸证据。", None, ()

    center_items = tuple(
        item
        for item in nominal
        if item.role in {"pad_center_distance", "pitch"}
        and _relation_center_marked(item)
        and _family_topology_axis(item) in {"width", "height"}
    )
    centers_by_axis = {
        axis: tuple(item for item in center_items if _family_topology_axis(item) == axis)
        for axis in ("width", "height")
    }
    center_values: dict[str, float] = {}
    for axis, axis_items in centers_by_axis.items():
        value, _ = _family_unique_axis_value(axis_items)
        if value is not None:
            center_values[axis] = value

    if transcription.pin_count == 3 and len(center_values) == 1:
        separation_axis, pitch = next(iter(center_values.items()))
        edges: set[frozenset[str]] = set()
        for item in centers_by_axis[separation_axis]:
            parts = _relation_endpoint_parts(item)
            if parts is None:
                continue
            nodes = tuple(_family_inline_endpoint_node(part) for part in parts)
            if None not in nodes and nodes[0] != nodes[1]:
                edges.add(frozenset((str(nodes[0]), str(nodes[1]))))
        expected_edges = {
            frozenset(("left", "middle")),
            frozenset(("middle", "right")),
        }
        if edges == expected_edges and pitch > shape_values[separation_axis]:
            cross_axis = "height" if separation_axis == "width" else "width"
            return (
                "INLINE3",
                "三焊盘中心关系形成 left-middle-right 同轴等距链；无跨轴中心关系。",
                3,
                (
                    f"pad={shape_values[separation_axis]:.3f}x{shape_values[cross_axis]:.3f}mm",
                    f"pitch={pitch:.3f}mm",
                    f"source_axis={separation_axis}->footprint_x",
                ),
            )

    if transcription.pin_count == 4 and set(center_values) == {"width", "height"}:
        relation_kinds = {
            axis: {
                kind
                for item in centers_by_axis[axis]
                if (kind := _family_grid_relation_kind(item)) is not None
            }
            for axis in ("width", "height")
        }
        if (
            relation_kinds == {"width": {"width"}, "height": {"height"}}
            and center_values["width"] > shape_values["width"]
            and center_values["height"] > shape_values["height"]
        ):
            return (
                "GRID4",
                "四焊盘证据形成左右两列×上下两排的正交 2×2 等尺寸栅格。",
                4,
                (
                    f"pad={shape_values['width']:.3f}x{shape_values['height']:.3f}mm",
                    f"pitch_x={center_values['width']:.3f}mm",
                    f"pitch_y={center_values['height']:.3f}mm",
                ),
            )
    return None, "新族拓扑关系不足或存在多解，未自动选族。", None, ()


def new_family_pin_mapping_evidence(
    transcription: Transcription,
    family: str,
) -> dict[str, Any]:
    """Prove the preview numbering from endpoint text or require user review."""

    if family == "QUAD_EP":
        return {
            "status": "needs_user_review",
            "family": family,
            "mapping": {"EP": "placeholder"},
            "expected_preview_mapping": (
                "peripheral counter-clockwise from upper-left; EP logical number must "
                "be assigned from the symbol/datasheet"
            ),
            "reason": (
                "外围焊盘按 QFN 逆时针预览编号；中央 EP 暂用明确占位符 EP。"
                "正式入库前必须由用户按符号/资料确认中央焊盘电气编号。"
            ),
        }

    if family == "DUAL":
        pin_count = transcription.pin_count
        row_pin_numbers: dict[str, set[int]] = {}
        for item in transcription.dimensions:
            if item.belongs_to != "solder_land" or item.is_derived:
                continue
            side = _dual_row_side(item.endpoints)
            numbers = {
                int(match.group(1) or match.group(2))
                for match in re.finditer(
                    r"(?:焊盘|端子|pad|pin)\s*#?\s*(\d+)|"
                    r"第\s*(\d+)\s*(?:号)?(?:焊盘|端子)",
                    unicodedata.normalize("NFKC", item.endpoints or "").lower(),
                )
            }
            if side is not None and numbers:
                row_pin_numbers.setdefault(side, set()).update(numbers)

        observed_mapping = {
            side: sorted(numbers) for side, numbers in sorted(row_pin_numbers.items())
        }
        if pin_count == 5:
            row_sizes = sorted(len(numbers) for numbers in row_pin_numbers.values())
            all_numbers = set().union(*row_pin_numbers.values()) if row_pin_numbers else set()
            proven = row_sizes == [2, 3] and all_numbers == set(range(1, 6))
            return {
                "status": "document_proven" if proven else "needs_user_review",
                "family": family,
                "mapping": observed_mapping,
                "expected_preview_mapping": "3+2 rows; pin side assignment must come from drawing",
                "reason": (
                    "原图端点文字完整证明了 3+2 两排的焊盘编号与所在排。"
                    if proven
                    else "3+2 不等排缺少完整原图 pin 映射证据；不得猜编号，须人工核对。"
                ),
            }
        return {
            "status": "standard_equal_row_contract",
            "family": family,
            "mapping": observed_mapping,
            "expected_preview_mapping": "counter-clockwise equal-row DUAL numbering",
            "reason": "等排 DUAL 沿用逆时针标准编号；仍须用户在预览中确认后入库。",
        }

    if family not in {"INLINE3", "GRID4"}:
        return {
            "status": "legacy_family_contract",
            "family": family,
            "mapping": {},
            "reason": "既有族沿用已冻结的编号契约。",
        }
    observed: dict[str, int] = {}
    for item in transcription.dimensions:
        if item.belongs_to != "solder_land" or item.is_derived:
            continue
        for segment in re.split(r"\s*(?:->|→|到|至)\s*", item.endpoints or ""):
            normalized = unicodedata.normalize("NFKC", segment).lower()
            number_match = re.search(
                r"(?:焊盘|端子|pad|pin)\s*#?\s*(\d+)|第\s*(\d+)\s*(?:号)?(?:焊盘|端子)",
                normalized,
            )
            if number_match is None:
                continue
            number = int(number_match.group(1) or number_match.group(2))
            if family == "INLINE3":
                position = _family_inline_endpoint_node(normalized)
            else:
                compact = re.sub(r"[\s_\-]+", "", normalized)
                horizontal = "left" if any(
                    marker in compact for marker in ("左", "left")
                ) else ("right" if any(marker in compact for marker in ("右", "right")) else None)
                vertical = "top" if any(
                    marker in compact for marker in ("上", "top", "upper")
                ) else ("bottom" if any(marker in compact for marker in ("下", "bottom", "lower")) else None)
                position = f"{vertical}_{horizontal}" if horizontal and vertical else None
            if position is not None:
                observed[position] = number
    expected = (
        {"left": 1, "middle": 2, "right": 3}
        if family == "INLINE3"
        else {"top_left": 1, "top_right": 2, "bottom_right": 3, "bottom_left": 4}
    )
    proven = observed == expected
    return {
        "status": "document_proven" if proven else "needs_user_review",
        "family": family,
        "mapping": observed,
        "expected_preview_mapping": expected,
        "reason": (
            "原图端点文字逐焊盘证明了预览编号。"
            if proven
            else "原图端点文字未完整证明焊盘编号；预览编号仅为待确认占位，不得自动写入正式库。"
        ),
    }


def decide_family(
    transcription: Transcription,
    page_text: str | None = None,
) -> FamilyDecision:
    """Determine a family from solder-land geometry without model authority."""

    proposal = transcription.family_proposal
    reason = transcription.family_proposal_reason
    raw_items = _family_metric_land_dimensions(transcription)
    items = _family_clean_half_candidates(raw_items)
    pad_count = _family_solder_land_pad_count(items)
    ep_evidence = _ep_positive_evidence(transcription, page_text)
    if ep_evidence is not None:
        quad = _quad_ep_topology_evidence(transcription, page_text)
        if quad.get("status") in {"auto", "partial"}:
            solved = quad.get("solved") or {}
            rows = quad.get("row_counts") or []
            summary = (
                f"form={quad.get('form')}",
                f"rows={'+'.join(str(value) for value in rows)}",
                f"pad={solved.get('pad_x')}x{solved.get('pad_y')}",
                f"center={solved.get('center_x')}x{solved.get('center_y')}",
                f"pitch={solved.get('pitch_y')}x{solved.get('pitch_x')}",
                f"ep={solved.get('tab_x')}x{solved.get('tab_y')}",
                f"thermal_vias_excluded={len(quad.get('thermal_vias_excluded') or [])}",
            )
            return FamilyDecision(
                model_proposal=proposal,
                model_reason=reason,
                evidence_family="QUAD/EP",
                auto_family="QUAD_EP",
                status="auto",
                reason=(
                    f"{quad['reason']} 确定性拓扑闸自动选中 QUAD_EP；"
                    "模型 family_proposal 仅留痕，不参与判定。"
                ),
                solder_land_pad_count=transcription.pin_count,
                land_size_summary=summary,
            )
        return FamilyDecision(
            model_proposal=proposal,
            model_reason=reason,
            evidence_family="QFN/DFN/EP",
            auto_family=None,
            status="reject_ep",
            reason=f"{_ep_rejection_reason(ep_evidence)}；{quad.get('reason')}",
            solder_land_pad_count=pad_count,
            land_size_summary=(f"ep_positive_evidence={ep_evidence}",),
        )
    dual = _dual_topology_evidence(transcription, items, page_text)
    dual_status = str(dual.get("status") or "")
    if dual_status != "not_applicable":
        row_counts = dual.get("row_counts")
        summary = [
            f"pad_count={dual.get('pad_count')}",
            f"separation_axis={dual.get('separation_axis')}",
            f"pitch_y={dual.get('pitch_y')}",
        ]
        if row_counts:
            summary.append(f"rows={row_counts[0]}+{row_counts[1]}")
        shape_values = dual.get("shape_values") or {}
        if shape_values:
            summary.append(
                "source_pad="
                + "x".join(
                    str(round(float(shape_values.get(axis)), 4))
                    for axis in ("width", "height")
                    if shape_values.get(axis) is not None
                )
            )
        if dual_status == "auto":
            return FamilyDecision(
                model_proposal=proposal,
                model_reason=reason,
                evidence_family="DUAL",
                auto_family="DUAL",
                status="auto",
                reason=(
                    f"{dual['reason']} 确定性几何闸自动选中 DUAL；"
                    "模型 family_proposal 仅留痕，不参与判定。"
                ),
                solder_land_pad_count=int(dual["pad_count"]),
                land_size_summary=tuple(summary),
            )
        return FamilyDecision(
            model_proposal=proposal,
            model_reason=reason,
            evidence_family=(
                "DUAL/GRID4" if dual_status == "multi_family_conflict" else "DUAL"
            ),
            auto_family=None,
            status=dual_status,
            reason=str(dual.get("reason") or "DUAL 拓扑闸未通过。"),
            solder_land_pad_count=(
                int(dual["pad_count"]) if dual.get("pad_count") is not None else pad_count
            ),
            land_size_summary=tuple(summary),
        )
    new_family, new_reason, promoted_count, new_summary = _family_new_topology_evidence(
        transcription, items
    )
    if new_family is not None:
        return FamilyDecision(
            model_proposal=proposal,
            model_reason=reason,
            evidence_family=new_family,
            auto_family=new_family,
            status="auto",
            reason=(
                f"{new_reason} 确定性几何闸自动选中 {new_family}；"
                "模型 family_proposal 仅留痕，不参与判定。"
            ),
            solder_land_pad_count=promoted_count,
            land_size_summary=new_summary,
        )
    large_items = tuple(item for item in items if _family_is_large_pad(item))
    small_items = tuple(item for item in items if not _family_is_large_pad(item))
    large_widths = _family_values(large_items, {"pad_width", "tab_width"})
    large_heights = _family_values(large_items, {"pad_height", "tab_height"})
    small_widths = _family_values(small_items, {"pad_width"})
    small_heights = _family_values(small_items, {"pad_height"})
    pin_count = transcription.pin_count
    evidence_family: str | None = None
    evidence_reason = ""
    summary: list[str] = []

    if pad_count is not None:
        summary.append(f"pad_count={pad_count}")
    if pad_count is None or pad_count not in {2, 3}:
        return FamilyDecision(
            model_proposal=proposal,
            model_reason=reason,
            evidence_family=None,
            auto_family=None,
            status="insufficient",
            reason=(
                "solder_land 分区没有足够的可见焊盘身份，或焊盘数不是 2/3；"
                "未自动选族，保留下拉供人选择。"
            ),
            solder_land_pad_count=pad_count,
            land_size_summary=tuple(summary),
        )
    if pin_count is not None and pin_count != pad_count:
        return FamilyDecision(
            model_proposal=proposal,
            model_reason=reason,
            evidence_family=None,
            auto_family=None,
            status="insufficient",
            reason=(
                f"solder_land 实际可见焊盘数为 {pad_count}，但 pin_count={pin_count}；"
                "证据矛盾，未自动选族，请人工确认。"
            ),
            solder_land_pad_count=pad_count,
            land_size_summary=tuple(summary),
        )

    if large_items:
        large_area = max(large_widths, default=0.0) * max(large_heights, default=0.0)
        small_area = max(small_widths, default=0.0) * max(small_heights, default=0.0)
        if (
            pad_count in {2, 3}
            and _family_consistent(large_widths)
            and _family_consistent(large_heights)
            and _family_consistent(small_widths)
            and _family_consistent(small_heights)
            and large_area > small_area * 1.15
        ):
            evidence_family = "ASYM3"
            evidence_reason = (
                f"solder_land 有 {pad_count} 个焊盘证据，且 1 个大焊盘 "
                f"({max(large_widths):.3f}×{max(large_heights):.3f} mm) "
                f"与 {pin_count - 1} 个小焊盘尺寸分组可区分。"
            )
            summary.append(f"large={max(large_widths):.3f}x{max(large_heights):.3f}mm")
            summary.append(f"small={max(small_widths):.3f}x{max(small_heights):.3f}mm")
    elif pad_count == 3 and _family_consistent(small_widths) and _family_consistent(small_heights):
        evidence_family = "SOT3"
        evidence_reason = (
            f"solder_land 的 {pad_count} 个焊盘为同一宽高分组 "
            f"({max(small_widths):.3f}×{max(small_heights):.3f} mm)，未发现大焊盘。"
        )
        summary.append(f"equal={max(small_widths):.3f}x{max(small_heights):.3f}mm")
    elif pad_count == 2 and (small_widths or small_heights):
        evidence_family = "TWO_END"
        evidence_reason = (
            f"solder_land 仅见 {pad_count} 个同类焊盘，未发现大焊盘；"
            "CHIP/SMX 的生成参数相同，确定性规范化为 SMX。"
        )
        if small_widths and small_heights:
            summary.append(f"equal={max(small_widths):.3f}x{max(small_heights):.3f}mm")
        elif small_widths:
            summary.append(f"width={max(small_widths):.3f}mm")
        else:
            summary.append(f"height={max(small_heights):.3f}mm")

    if evidence_family is None:
        return FamilyDecision(
            model_proposal=proposal,
            model_reason=reason,
            evidence_family=None,
            auto_family=None,
            status="insufficient",
            reason=(
                "solder_land 的焊盘数或尺寸关系不足以确定族；"
                "未自动选族，保留下拉供人选择。"
            ),
            solder_land_pad_count=pad_count,
            land_size_summary=tuple(summary),
        )

    auto_family = "SMX" if evidence_family == "TWO_END" else evidence_family
    return FamilyDecision(
        model_proposal=proposal,
        model_reason=reason,
        evidence_family="CHIP/SMX" if evidence_family == "TWO_END" else evidence_family,
        auto_family=auto_family,
        status="auto",
        reason=(
            f"{evidence_reason} 确定性几何闸自动选中 {auto_family}；"
            "模型 family_proposal 仅留痕，不参与判定。"
        ),
        solder_land_pad_count=pad_count,
        land_size_summary=tuple(summary),
    )


def resolve_prefill_family(
    decision: FamilyDecision,
    selected_family: str,
) -> tuple[str, str]:
    """Use automatic family evidence when available; otherwise keep the dropdown."""

    if selected_family not in SUPPORTED_FAMILIES:
        raise E1aError(f"不支持的封装族：{selected_family}")
    if decision.auto_family is not None:
        return decision.auto_family, "auto"
    return selected_family, "dropdown"


def _numeric_exact_match(value: str, text: str) -> bool:
    if not text:
        return False
    escaped = re.escape(value)
    if value.startswith(("+", "-")):
        return re.search(rf"{escaped}(?![\d.,])", text) is not None
    return re.search(rf"(?<![\d.,]){escaped}(?![\d.,])", text) is not None


def anchor_transcription(
    transcription: Transcription,
    *,
    source_kind: str,
    page_text: str,
) -> AnchorResult:
    if source_kind == "image":
        checks = tuple(
            {
                "symbol": item.symbol,
                "value": item.value,
                "anchored": None,
                "reason": "上传截图没有 PDF 文本层，保留给人工目视确认。",
            }
            for item in transcription.dimensions
        )
        return AnchorResult(
            source_kind="image",
            checks=checks,
            can_generate=True,
            message="截图路径无文本层；锚定仅作提示，请由用户目视确认。",
        )

    checks_list: list[dict[str, Any]] = []
    for item in transcription.dimensions:
        anchored = _numeric_exact_match(item.value, page_text)
        checks_list.append(
            {
                "symbol": item.symbol,
                "value": item.value,
                "anchored": anchored,
                "reason": "页文本层精确命中" if anchored else "页文本层未精确命中",
            }
        )
    unmatched = sum(item["anchored"] is False for item in checks_list)
    return AnchorResult(
        source_kind="pdf",
        checks=tuple(checks_list),
        can_generate=True,
        message=(
            "转录尺寸均在所选页文本层精确命中；锚定为正面证据。"
            if checks_list and unmatched == 0
            else f"有 {unmatched} 条转录尺寸未在文本层精确命中；仅提示人工看图，不阻断。"
        ),
    )


def anchor_used_geometry_values(
    transcription: Transcription,
    used_values: dict[str, float | None],
    *,
    source_kind: str,
    page_text: str,
) -> AnchorResult:
    """Advisory text-layer check limited to values written into a footprint."""

    checks: list[dict[str, Any]] = []
    for field_name in MANUAL_GEOMETRY_FIELDS:
        raw_value = used_values.get(field_name)
        if raw_value is None:
            continue
        try:
            value_mm = float(raw_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value_mm) or value_mm <= 0.0:
            continue
        source_candidates = tuple(
            item
            for item in transcription.dimensions
            if math.isclose(
                dimension_value_mm(item),
                value_mm,
                rel_tol=0.0,
                abs_tol=0.011,
            )
        )
        if source_kind == "image":
            anchored: bool | None = None
            reason = "上传截图没有 PDF 文本层；请人工目视确认该写入值。"
        else:
            anchored = any(
                _numeric_exact_match(item.value, page_text)
                for item in source_candidates
            )
            if anchored:
                reason = "实际写入值的转录来源已在页文本层精确命中"
            elif source_candidates:
                reason = "实际写入值有转录来源，但页文本层未精确命中；请人工看图确认"
            else:
                reason = "实际写入值为确定性推导或无同值转录项；请人工看图确认"
        checks.append(
            {
                "field": field_name,
                "value_mm": value_mm,
                "anchored": anchored,
                "used_for_footprint": True,
                "source_candidates": [
                    {
                        "symbol": item.symbol,
                        "value": item.value,
                        "unit": item.unit,
                        "raw": item.raw,
                    }
                    for item in source_candidates
                ],
                "reason": reason,
            }
        )

    unmatched = sum(item["anchored"] is False for item in checks)
    if source_kind == "image":
        message = (
            f"实际写入 footprint 的 {len(checks)} 个几何值无文本层；"
            "锚定仅提示，请人工目视确认。"
        )
    elif not checks:
        message = "没有实际写入 footprint 的几何值需要锚定；锚定不参与阻断。"
    elif unmatched:
        message = (
            f"实际写入 footprint 的 {len(checks)} 个值中 {unmatched} 个未能在文本层复核；"
            "仅提示人工看图，不阻断。"
        )
    else:
        message = f"实际写入 footprint 的 {len(checks)} 个值均有文本层正面锚定证据。"
    return AnchorResult(
        source_kind=source_kind,
        checks=tuple(checks),
        can_generate=True,
        message=message,
    )


def _normalize_symbol(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().lower()
    text = text.replace("×", "x")
    return re.sub(r"[^a-z0-9]+", "", text)


_PASTE_TEXT_MARKERS = (
    "solderpaste",
    "pasteopening",
    "stencil",
    "cream",
    "钢网",
    "焊膏",
    "锡膏",
    "はんだ",
)
_LANDS_TEXT_MARKERS = (
    "solderland",
    "solderlands",
    "copperland",
    "copperpad",
    "landcenter",
    "铜箔",
    "铜焊盘",
)
_CENTER_TEXT_MARKERS = (
    "centerx",
    "centerpitch",
    "centerdistance",
    "centertocenter",
    "centretocentre",
    "landcenterdistance",
    "pitchx",
    "中心距",
)
_TOTAL_SPAN_TEXT_MARKERS = (
    "overall",
    "overalllandspan",
    "totalwidth",
    "totalspan",
    "occupiedwidth",
    "occupiedarea",
    "zmax",
    "总宽",
    "总跨",
)


def _dimension_text_key(item: Dimension) -> str:
    text = unicodedata.normalize("NFKC", f"{item.symbol} {item.raw}").lower()
    compact = re.sub(r"[\s_\-:/()]+", "", text)
    return compact


def dimension_pattern_role(item: Dimension) -> str:
    if item.belongs_to == "solder_paste" or item.role in {"paste_width", "paste_height"}:
        return "paste"
    if item.belongs_to == "solder_land":
        return "lands"
    text = _dimension_text_key(item)
    if any(marker in text for marker in _PASTE_TEXT_MARKERS):
        return "paste"
    if any(marker in text for marker in _LANDS_TEXT_MARKERS):
        return "lands"
    if item.belongs_to in {"package_outline", "occupied_area"}:
        return "other"
    return "unknown"


def dimension_role_label(item: Dimension) -> str:
    return {
        "lands": "铜箔焊盘 / lands",
        "paste": "焊膏开口 / paste",
        "other": "其他",
        "unknown": "未判定",
    }[dimension_pattern_role(item)]


def geometry_role_label(item: Dimension) -> str:
    labels = {
        "pad_width": "焊盘宽",
        "pad_height": "焊盘高",
        "pad_gap": "焊盘边缘间距",
        "pad_center_distance": "焊盘中心距",
        "overall_span": "总跨",
        "body_length": "本体长",
        "body_width": "本体宽",
        "body_height": "本体高/厚",
        "tab_width": "大焊盘宽",
        "tab_height": "大焊盘高",
        "half_pitch": "半间距",
        "half_pitch_from_centerline": "中心线到焊盘中心（半间距）",
        "center_to_edge": "中心线到边缘",
        "gap_between_pads": "焊盘边缘间距",
        "pitch": "间距",
        "paste_width": "焊膏宽",
        "paste_height": "焊膏高",
        "lead_width": "引脚宽",
        "other": "其他",
    }
    label = labels[item.role]
    if item.role_original and item.role_original != item.role:
        label += f"（原{labels[item.role_original]}）"
    return label


def page_mentions_lands_and_paste(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    compact = re.sub(r"[\s_\-:/()]+", "", normalized)
    return any(marker in compact for marker in _LANDS_TEXT_MARKERS) and any(
        marker in compact for marker in _PASTE_TEXT_MARKERS
    )


_TEXT_BOUND_NUMBER_RE = re.compile(
    r"(?<![\w.])[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?![\w.])"
)
_TEXT_BOUND_ALLOWED_MARKERS_RE = re.compile(
    r"\b(?:min(?:imum)?|max(?:imum)?|nom(?:inal)?|ref(?:erence)?|typ(?:ical)?|bsc|mm|"
    r"millimeters?|millimetres?|inches?|inch|mils?)\b",
    re.IGNORECASE,
)
_NOTATION_NUMBER_PATTERN = r"(?:\d+(?:[.,]\d+)?|[.,]\d+)"
_NOTATION_VALUE_PAREN_RE = re.compile(
    rf"^\s*(?:(?P<typ>typ(?:ical)?)[\s.:]*)?"
    rf"(?P<outer>{_NOTATION_NUMBER_PATTERN})\s*"
    rf"\(\s*(?P<paren>{_NOTATION_NUMBER_PATTERN})\s*"
    r"(?P<paren_unit>mm|millimeters?|millimetres?|in(?:ch(?:es)?)?|[\"'])?\s*\)"
    r"\s*(?P<qualifier>max(?:imum)?|min(?:imum)?|ref(?:erence)?|typ(?:ical)?)?[\s.]*$",
    re.IGNORECASE,
)
_NOTATION_PAREN_ONLY_RE = re.compile(
    rf"^\s*\(\s*(?P<value>{_NOTATION_NUMBER_PATTERN})\s*"
    r"(?P<unit>mm|millimeters?|millimetres?|in(?:ch(?:es)?)?|[\"'])?\s*\)\s*$",
    re.IGNORECASE,
)
_NOTATION_PM_RE = re.compile(
    rf"^\s*(?P<nominal>{_NOTATION_NUMBER_PATTERN})\s*"
    rf"(?:±|\+\s*/\s*[-−]|\+\s*/\s*)\s*(?P<tolerance>{_NOTATION_NUMBER_PATTERN})\s*$",
    re.IGNORECASE,
)
_NOTATION_PLUS_RE = re.compile(
    rf"^\s*(?P<nominal>{_NOTATION_NUMBER_PATTERN})\s*\+\s*"
    rf"(?P<plus>{_NOTATION_NUMBER_PATTERN})\s*$",
    re.IGNORECASE,
)
_NOTATION_MINUS_ONLY_RE = re.compile(
    rf"^\s*[-−]\s*(?P<minus>{_NOTATION_NUMBER_PATTERN})\s*$",
    re.IGNORECASE,
)
_NOTATION_PLUS_MINUS_RE = re.compile(
    rf"^\s*(?P<nominal>{_NOTATION_NUMBER_PATTERN})\s*\+\s*"
    rf"(?P<plus>{_NOTATION_NUMBER_PATTERN})\s*/\s*[-−]\s*"
    rf"(?P<minus>{_NOTATION_NUMBER_PATTERN})\s*$",
    re.IGNORECASE,
)
_NOTATION_QUALIFIED_RE = re.compile(
    rf"^\s*(?P<value>{_NOTATION_NUMBER_PATTERN})\s*"
    r"(?P<qualifier>max(?:imum)?|min(?:imum)?|ref(?:erence)?|typ(?:ical)?)[\s.]*$",
    re.IGNORECASE,
)
_NOTATION_QUALIFIER_ONLY_RE = re.compile(
    r"^\s*(?P<qualifier>max(?:imum)?|min(?:imum)?|ref(?:erence)?|typ(?:ical)?)[\s.]*$",
    re.IGNORECASE,
)
_NOTATION_BARE_RE = re.compile(
    rf"^\s*(?P<value>{_NOTATION_NUMBER_PATTERN})"
    r"(?:\s+(?:[A-Z](?:\s+[A-Z])*))?\s*$"
)


def _notation_decimal(raw_value: str) -> Decimal | None:
    try:
        value = Decimal(raw_value.replace(",", "."))
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _declared_page_unit_layout(page_text: str) -> dict[str, Any]:
    normalized = unicodedata.normalize("NFKC", page_text or "").lower()
    declarations = (
        (
            r"(?:dimensions?[^\n]{0,24})?inches?\s*\(\s*millimeters?\s*\)",
            "in",
            "mm",
        ),
        (
            r"(?:dimensions?[^\n]{0,24})?(?:mm|millimeters?|millimetres?)\s*"
            r"\(\s*(?:inches?|in\.?)[\s.]*\)",
            "mm",
            "in",
        ),
    )
    for pattern, outer_unit, paren_unit in declarations:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            return {
                "outer_unit": outer_unit,
                "parenthetical_unit": paren_unit,
                "declaration": match.group(0).strip(),
                "declares_both_units": True,
            }
    single_mm_markers = (
        "dimensions in mm",
        "dimension in mm",
        "all dimensions are in millimeters",
        "unit : mm",
        "unit: mm",
        "dimensions in millimeters",
    )
    marker = next((item for item in single_mm_markers if item in normalized), None)
    if marker is not None:
        return {
            "outer_unit": "mm",
            "parenthetical_unit": None,
            "declaration": marker,
            "declares_both_units": False,
        }
    single_in_match = re.search(r"dimensions?\s+in\s+inches?", normalized)
    if single_in_match:
        return {
            "outer_unit": "in",
            "parenthetical_unit": None,
            "declaration": single_in_match.group(0),
            "declares_both_units": False,
        }
    return {
        "outer_unit": None,
        "parenthetical_unit": None,
        "declaration": "",
        "declares_both_units": False,
    }


def _line_primary_notation_value(line: str) -> Decimal | None:
    normalized = unicodedata.normalize("NFKC", line or "").strip()
    for pattern in (
        _NOTATION_PLUS_MINUS_RE,
        _NOTATION_PM_RE,
        _NOTATION_PLUS_RE,
        _NOTATION_QUALIFIED_RE,
        _NOTATION_BARE_RE,
    ):
        match = pattern.match(normalized)
        if match:
            group = "nominal" if "nominal" in match.groupdict() else "value"
            return _notation_decimal(match.group(group))
    paired = _NOTATION_VALUE_PAREN_RE.match(normalized)
    return _notation_decimal(paired.group("outer")) if paired else None


def _parenthetical_ratio_evidence(lines: list[str]) -> tuple[dict[str, Any], ...]:
    evidence: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, int]] = set()

    def append_pair(
        *,
        outer_line: int,
        paren_line: int,
        outer: Decimal | None,
        paren: Decimal | None,
        pairing_mode: str,
    ) -> None:
        if outer is None or paren is None:
            return
        pair_key = (outer_line, paren_line)
        if pair_key in seen_pairs:
            return
        seen_pairs.add(pair_key)
        ratio_outer_over_paren = float(outer / paren)
        ratio_paren_over_outer = float(paren / outer)
        direction: str | None = None
        relative_error: float | None = None
        if abs(ratio_outer_over_paren - 25.4) / 25.4 <= 0.01:
            direction = "outer_mm_paren_in"
            relative_error = abs(ratio_outer_over_paren - 25.4) / 25.4
        elif abs(ratio_paren_over_outer - 25.4) / 25.4 <= 0.01:
            direction = "outer_in_paren_mm"
            relative_error = abs(ratio_paren_over_outer - 25.4) / 25.4
        evidence.append(
            {
                "outer_line_1based": outer_line + 1,
                "paren_line_1based": paren_line + 1,
                "outer_value": float(outer),
                "paren_value": float(paren),
                "direction": direction,
                "relative_error": relative_error,
                "pairing_mode": pairing_mode,
            }
        )

    normalized_lines = [
        unicodedata.normalize("NFKC", line or "").strip() for line in lines
    ]
    for index, line in enumerate(lines):
        normalized = normalized_lines[index]
        paired = _NOTATION_VALUE_PAREN_RE.match(normalized)
        if paired:
            outer = _notation_decimal(paired.group("outer"))
            paren = _notation_decimal(paired.group("paren"))
            paren_line = index
        else:
            outer = _line_primary_notation_value(normalized)
            if outer is None or index + 1 >= len(lines):
                continue
            paren_match = _NOTATION_PAREN_ONLY_RE.match(
                normalized_lines[index + 1]
            )
            if paren_match is None:
                continue
            paren = _notation_decimal(paren_match.group("value"))
            paren_line = index + 1
        append_pair(
            outer_line=index,
            paren_line=paren_line,
            outer=outer,
            paren=paren,
            pairing_mode="inline" if paren_line == index else "interleaved",
        )

    def grouped_values(
        start: int,
        pattern: re.Pattern[str],
        value_group: str,
    ) -> tuple[list[tuple[int, Decimal | None]], int]:
        values: list[tuple[int, Decimal | None]] = []
        cursor = start
        while cursor < len(normalized_lines):
            match = pattern.match(normalized_lines[cursor])
            if match is None:
                break
            values.append((cursor, _notation_decimal(match.group(value_group))))
            cursor += 1
        return values, cursor

    cursor = 0
    while cursor < len(normalized_lines):
        paren_group, after_paren = grouped_values(
            cursor,
            _NOTATION_PAREN_ONLY_RE,
            "value",
        )
        if len(paren_group) >= 2:
            outer_group, after_outer = grouped_values(
                after_paren,
                _NOTATION_BARE_RE,
                "value",
            )
            if len(outer_group) == len(paren_group):
                for (paren_line, paren), (outer_line, outer) in zip(
                    paren_group,
                    outer_group,
                ):
                    append_pair(
                        outer_line=outer_line,
                        paren_line=paren_line,
                        outer=outer,
                        paren=paren,
                        pairing_mode="grouped_by_ordinal",
                    )
                cursor = after_outer
                continue

        outer_group, after_outer = grouped_values(
            cursor,
            _NOTATION_BARE_RE,
            "value",
        )
        if len(outer_group) >= 2:
            paren_group, after_paren = grouped_values(
                after_outer,
                _NOTATION_PAREN_ONLY_RE,
                "value",
            )
            if len(paren_group) == len(outer_group):
                for (outer_line, outer), (paren_line, paren) in zip(
                    outer_group,
                    paren_group,
                ):
                    append_pair(
                        outer_line=outer_line,
                        paren_line=paren_line,
                        outer=outer,
                        paren=paren,
                        pairing_mode="grouped_by_ordinal",
                    )
                cursor = after_paren
                continue
        cursor += 1
    return tuple(evidence)


def _resolve_page_unit_direction(page_text: str, lines: list[str]) -> dict[str, Any]:
    declaration = _declared_page_unit_layout(page_text)
    evidence = _parenthetical_ratio_evidence(lines)
    valid_directions = {
        item["direction"] for item in evidence if item["direction"] is not None
    }
    parenthetical_token_count = sum(
        len(re.findall(r"\([^)]*\d[^)]*\)", line)) for line in lines
    )
    expected_direction = None
    if declaration["outer_unit"] == "mm" and declaration["parenthetical_unit"] == "in":
        expected_direction = "outer_mm_paren_in"
    elif declaration["outer_unit"] == "in" and declaration["parenthetical_unit"] == "mm":
        expected_direction = "outer_in_paren_mm"

    status = "unresolved"
    reason = ""
    if parenthetical_token_count == 0:
        status = "not_applicable"
        reason = "页面没有括号数字，单位方向无需判定。"
    elif not declaration["declares_both_units"]:
        reason = "表头未同时声明括号内外单位；paren 一律不作标称。"
    elif not valid_directions:
        reason = "未找到括号内外比值约为 25.4 的双重验证；paren 一律不作标称。"
    elif len(valid_directions) != 1 or expected_direction not in valid_directions:
        reason = "表头声明与 25.4 比值方向不一致或存在双向证据；paren 一律不作标称。"
    else:
        status = "resolved"
        reason = "表头单位声明与括号内外 25.4 比值方向一致。"

    return {
        "status": status,
        "reason": reason,
        "header": declaration,
        "expected_direction": expected_direction,
        "ratio_tolerance": 0.01,
        "ratio_evidence": [dict(item) for item in evidence],
        "valid_directions": sorted(item for item in valid_directions if item),
        "parenthetical_token_count": parenthetical_token_count,
    }


def _unit_value_mm(value: Decimal, unit: str | None) -> float | None:
    if unit == "mm":
        return float(value)
    if unit == "in":
        return float(value * Decimal("25.4"))
    return None


def _classify_page_dimension_notation(
    page_text: str,
) -> tuple[dict[str, Any], tuple[tuple[dict[str, Any], ...], ...]]:
    lines = unicodedata.normalize("NFKC", page_text or "").splitlines()
    contexts = _text_bound_section_contexts(lines)
    unit_direction = _resolve_page_unit_direction(page_text, lines)
    header = unit_direction["header"]
    outer_unit = header["outer_unit"]
    paren_unit = header["parenthetical_unit"] if unit_direction["status"] == "resolved" else None
    ratio_by_lines = {
        (item["outer_line_1based"], item["paren_line_1based"]): item
        for item in unit_direction["ratio_evidence"]
    }
    tokens: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    line_atoms: list[tuple[dict[str, Any], ...]] = [() for _ in lines]
    consumed: set[int] = set()

    def token(
        *,
        category: str,
        line_start: int,
        line_end: int,
        raw: str,
        value: Decimal | None,
        unit: str | None,
        accepted: bool,
        reason: str,
        qualifier: str = "",
    ) -> dict[str, Any]:
        return {
            "category": category,
            "line_start_1based": line_start + 1,
            "line_end_1based": line_end + 1,
            "context": contexts[line_start],
            "raw": raw,
            "value": float(value) if value is not None else None,
            "unit": unit,
            "value_mm": _unit_value_mm(value, unit) if value is not None else None,
            "accepted_as_nominal": accepted,
            "boundary_eligible": category == "bare" and accepted,
            "qualifier": qualifier,
            "reason": reason,
        }

    for index, line in enumerate(lines):
        if index in consumed:
            continue
        normalized = unicodedata.normalize("NFKC", line or "").strip()
        if not normalized:
            continue
        if "×" in normalized or re.search(r"(?:\d\s*[xX]|[xX]\s*\d)", normalized):
            excluded.append(
                {
                    "line_1based": index + 1,
                    "raw": normalized,
                    "reason": "multiplicity_marker",
                }
            )
            continue

        plus_minus = _NOTATION_PLUS_MINUS_RE.match(normalized)
        symmetric = _NOTATION_PM_RE.match(normalized)
        split_plus = _NOTATION_PLUS_RE.match(normalized)
        split_minus = (
            _NOTATION_MINUS_ONLY_RE.match(
                unicodedata.normalize("NFKC", lines[index + 1]).strip()
            )
            if split_plus and index + 1 < len(lines)
            else None
        )
        if plus_minus or symmetric or (split_plus and split_minus):
            match = plus_minus or symmetric or split_plus
            nominal = _notation_decimal(match.group("nominal"))
            line_end = index + 1 if split_plus and split_minus else index
            raw = " / ".join(lines[index : line_end + 1]).strip()
            tokens.append(
                token(
                    category="nominal_pm",
                    line_start=index,
                    line_end=line_end,
                    raw=raw,
                    value=nominal,
                    unit=outer_unit,
                    accepted=nominal is not None,
                    reason="公差记号只采纳标称 A，不把公差界当独立尺寸。",
                )
            )
            if line_end > index:
                consumed.add(line_end)
            continue

        qualified = _NOTATION_QUALIFIED_RE.match(normalized)
        if qualified:
            value = _notation_decimal(qualified.group("value"))
            qualifier = qualified.group("qualifier").upper()
            tokens.append(
                token(
                    category="qualifier",
                    line_start=index,
                    line_end=index,
                    raw=normalized,
                    value=value,
                    unit=outer_unit,
                    accepted=False,
                    reason="MAX/MIN/REF/TYP 界限或参考词永不作标称。",
                    qualifier=qualifier,
                )
            )
            continue

        paired = _NOTATION_VALUE_PAREN_RE.match(normalized)
        if paired:
            outer = _notation_decimal(paired.group("outer"))
            paren = _notation_decimal(paired.group("paren"))
            next_qualifier = (
                _NOTATION_QUALIFIER_ONLY_RE.match(
                    unicodedata.normalize("NFKC", lines[index + 1]).strip()
                )
                if index + 1 < len(lines)
                else None
            )
            qualifier = paired.group("qualifier") or paired.group("typ")
            if next_qualifier is not None:
                qualifier = next_qualifier.group("qualifier")
                consumed.add(index + 1)
            category = "qualifier" if qualifier else "bare"
            accepted_outer = outer is not None and category == "bare"
            outer_record = token(
                category=category,
                line_start=index,
                line_end=index + 1 if next_qualifier is not None else index,
                raw=normalized,
                value=outer,
                unit=outer_unit,
                accepted=accepted_outer,
                reason=(
                    "括号外主值可作裸值候选。"
                    if accepted_outer
                    else "MAX/MIN/REF/TYP 界限或参考词永不作标称。"
                ),
                qualifier=(qualifier or "").upper(),
            )
            tokens.append(outer_record)
            ratio = ratio_by_lines.get((index + 1, index + 1))
            paren_accepted = (
                unit_direction["status"] == "resolved"
                and ratio is not None
                and ratio["direction"] == unit_direction["expected_direction"]
                and category == "bare"
            )
            tokens.append(
                token(
                    category="paren",
                    line_start=index,
                    line_end=index,
                    raw=f"({paired.group('paren')})",
                    value=paren,
                    unit=paren_unit,
                    accepted=paren_accepted,
                    reason=(
                        "括号单位方向经表头与 25.4 比值双证据确认，作为同一主值的换算。"
                        if paren_accepted
                        else unit_direction["reason"]
                    ),
                )
            )
            if accepted_outer and outer is not None:
                value_mm = _unit_value_mm(outer, outer_unit)
                if paren_accepted and paren is not None and paren_unit == "mm":
                    value_mm = float(paren)
                if value_mm is not None:
                    line_atoms[index] = (
                        {
                            "raw_value": normalized,
                            "unit": "mm",
                            "value_mm": value_mm,
                            "inside_parentheses": False,
                            "notation_category": "bare",
                        },
                    )
            continue

        paren_only = _NOTATION_PAREN_ONLY_RE.match(normalized)
        if paren_only:
            value = _notation_decimal(paren_only.group("value"))
            ratio = next(
                (
                    item
                    for (outer_line, paren_line), item in ratio_by_lines.items()
                    if paren_line == index + 1 and outer_line != paren_line
                ),
                None,
            )
            accepted = (
                unit_direction["status"] == "resolved"
                and ratio is not None
                and ratio["direction"] == unit_direction["expected_direction"]
            )
            tokens.append(
                token(
                    category="paren",
                    line_start=index,
                    line_end=index,
                    raw=normalized,
                    value=value,
                    unit=paren_unit,
                    accepted=accepted,
                    reason=(
                        "括号单位方向经表头与 25.4 比值双证据确认，作为相邻主值的换算。"
                        if accepted
                        else unit_direction["reason"]
                    ),
                )
            )
            continue

        bare = _NOTATION_BARE_RE.match(normalized)
        if bare:
            value = _notation_decimal(bare.group("value"))
            next_qualifier = (
                _NOTATION_QUALIFIER_ONLY_RE.match(
                    unicodedata.normalize("NFKC", lines[index + 1]).strip()
                )
                if index + 1 < len(lines)
                else None
            )
            category = "qualifier" if next_qualifier is not None else "bare"
            if next_qualifier is not None:
                consumed.add(index + 1)
            accepted = value is not None and category == "bare"
            tokens.append(
                token(
                    category=category,
                    line_start=index,
                    line_end=index + 1 if next_qualifier is not None else index,
                    raw=normalized,
                    value=value,
                    unit=outer_unit,
                    accepted=accepted,
                    reason=(
                        "裸值可参与相邻降序 min/max 定界。"
                        if accepted
                        else "下一 token 为 MAX/MIN/REF/TYP，永不作标称。"
                    ),
                    qualifier=(
                        next_qualifier.group("qualifier").upper()
                        if next_qualifier is not None
                        else ""
                    ),
                )
            )
            if accepted and value is not None:
                value_mm = _unit_value_mm(value, outer_unit)
                if value_mm is not None:
                    line_atoms[index] = (
                        {
                            "raw_value": normalized,
                            "unit": "mm",
                            "value_mm": value_mm,
                            "inside_parentheses": False,
                            "notation_category": "bare",
                        },
                    )

    category_counts = Counter(item["category"] for item in tokens)
    accepted_counts = Counter(
        item["category"] for item in tokens if item["accepted_as_nominal"]
    )
    ledger = {
        "schema": "e1j10v2_page_notation_classifier_v1",
        "model_calls": 0,
        "unit_direction": unit_direction,
        "token_count": len(tokens),
        "category_counts": dict(sorted(category_counts.items())),
        "accepted_nominal_counts": dict(sorted(accepted_counts.items())),
        "tokens": tokens,
        "excluded_non_dimension_tokens": excluded,
        "boundary_eligible_line_count": sum(bool(items) for items in line_atoms),
    }
    return ledger, tuple(line_atoms)


def classify_page_dimension_notation(page_text: str) -> dict[str, Any]:
    ledger, _ = _classify_page_dimension_notation(page_text)
    return ledger


def _page_text_unit_layout(page_text: str) -> tuple[str | None, str | None]:
    normalized = unicodedata.normalize("NFKC", page_text or "").lower()
    if re.search(r"inches?\s*\(\s*millimeters?", normalized):
        return "in", "mm"
    if re.search(r"(?:dimensions?\s*:?\s*)?mm\s*\(\s*inches?", normalized):
        return "mm", "in"
    if any(
        marker in normalized
        for marker in (
            "dimensions in mm",
            "dimension in mm",
            "all dimensions are in millimeters",
            "unit : mm",
            "unit: mm",
            "dimensions in millimeters",
        )
    ):
        return "mm", "mm"
    return None, None


def _text_bound_line_atoms(
    line: str,
    *,
    default_unit: str | None,
    parenthetical_unit: str | None,
) -> tuple[dict[str, Any], ...]:
    normalized = unicodedata.normalize("NFKC", line or "").strip()
    if not normalized or default_unit is None:
        return ()
    lowered = normalized.lower()
    if "×" in normalized or re.search(r"(?:\d\s*[xX]|[xX]\s*\d)", normalized):
        return ()
    if any(marker in lowered for marker in ("±", "+/-", "+/−")):
        return ()
    if re.search(r"\d\s*/\s*\d", normalized):
        return ()
    matches = tuple(_TEXT_BOUND_NUMBER_RE.finditer(normalized))
    if not matches:
        return ()
    remainder = _TEXT_BOUND_NUMBER_RE.sub("", normalized)
    remainder = _TEXT_BOUND_ALLOWED_MARKERS_RE.sub("", remainder)
    remainder = re.sub(r"[\s()\[\]{},.;:'\"+\-×xX]+", "", remainder)
    if remainder:
        return ()

    atoms: list[dict[str, Any]] = []
    for match in matches:
        raw_value = match.group(0).replace(",", ".")
        try:
            number = Decimal(raw_value)
        except InvalidOperation:
            return ()
        if number <= 0:
            return ()
        left_parenthesis = normalized.rfind("(", 0, match.start())
        right_parenthesis = normalized.find(")", match.end())
        inside_parentheses = (
            left_parenthesis >= 0
            and right_parenthesis >= 0
            and normalized.rfind(")", 0, match.start()) < left_parenthesis
        )
        unit = (
            parenthetical_unit
            if inside_parentheses and parenthetical_unit is not None
            else default_unit
        )
        scale = Decimal("25.4") if unit == "in" else Decimal("1")
        atoms.append(
            {
                "raw_value": raw_value,
                "unit": unit,
                "value_mm": float(number * scale),
                "inside_parentheses": inside_parentheses,
            }
        )
    return tuple(atoms)


def _text_bound_section_contexts(lines: list[str]) -> tuple[str, ...]:
    """Track whether each line belongs to package-outline or land-pattern text."""

    context = "unknown"
    contexts: list[str] = []
    package_heading = re.compile(
        r"(?:package|case)\s+outline|physical\s+dimensions?|mechanical\s+dimensions?",
        re.IGNORECASE,
    )
    land_heading = re.compile(
        r"land\s*pattern|pad\s*layout|solder(?:ing|\s+lands?)|footprint|reflow|"
        r"焊盘|焊膏|钢网",
        re.IGNORECASE,
    )
    for line in lines:
        if land_heading.search(line):
            context = "solder_land"
        elif package_heading.search(line):
            context = "package_outline"
        contexts.append(context)
    return tuple(contexts)


def _descending_text_bound_pair(
    first: tuple[dict[str, Any], ...],
    second: tuple[dict[str, Any], ...],
) -> tuple[tuple[dict[str, Any], dict[str, Any]], ...]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for unit in ("mm", "in"):
        first_unit = [item for item in first if item["unit"] == unit]
        second_unit = [item for item in second if item["unit"] == unit]
        if not first_unit or len(first_unit) != len(second_unit):
            continue
        unit_pairs = tuple(zip(first_unit, second_unit))
        if any(
            maximum["value_mm"] <= minimum["value_mm"] + 1e-12
            for maximum, minimum in unit_pairs
        ):
            continue
        pairs.extend(unit_pairs)
    return tuple(pairs)


def _extract_page_text_min_max_bounds_with_notation(
    page_text: str,
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    """Extract stable descending min/max blocks from package-outline page text."""

    lines = unicodedata.normalize("NFKC", page_text or "").splitlines()
    contexts = _text_bound_section_contexts(lines)
    notation, parsed = _classify_page_dimension_notation(page_text)
    bounds: list[dict[str, Any]] = []
    run_start = 0
    while run_start < len(lines):
        if not parsed[run_start] or contexts[run_start] != "package_outline":
            run_start += 1
            continue
        run_end = run_start
        while (
            run_end + 1 < len(lines)
            and parsed[run_end + 1]
            and contexts[run_end + 1] == "package_outline"
        ):
            run_end += 1

        parity_candidates: list[tuple[int, list[tuple[int, tuple[Any, ...]]]]] = []
        for parity in (0, 1):
            chain: list[tuple[int, tuple[Any, ...]]] = []
            best_chain: list[tuple[int, tuple[Any, ...]]] = []
            index = run_start + parity
            while index + 1 <= run_end:
                pairs = _descending_text_bound_pair(parsed[index], parsed[index + 1])
                if pairs:
                    chain.append((index, pairs))
                    if len(chain) > len(best_chain):
                        best_chain = list(chain)
                else:
                    chain = []
                index += 2
            parity_candidates.append((parity, best_chain))

        _, selected_chain = max(
            parity_candidates,
            key=lambda candidate: (len(candidate[1]), -candidate[0]),
        )
        if len(selected_chain) >= 2:
            for line_index, pairs in selected_chain:
                for maximum, minimum in pairs:
                    bounds.append(
                        {
                            "max_mm": maximum["value_mm"],
                            "min_mm": minimum["value_mm"],
                            "source_unit": maximum["unit"],
                            "max_raw": maximum["raw_value"],
                            "min_raw": minimum["raw_value"],
                            "max_line_1based": line_index + 1,
                            "min_line_1based": line_index + 2,
                            "max_line_text": lines[line_index].strip(),
                            "min_line_text": lines[line_index + 1].strip(),
                            "section_context": "package_outline",
                            "block_pair_count": len(selected_chain),
                        }
                    )
        run_start = run_end + 1

    deduplicated: list[dict[str, Any]] = []
    for bound in bounds:
        duplicate = next(
            (
                existing
                for existing in deduplicated
                if existing["max_line_1based"] == bound["max_line_1based"]
                and existing["min_line_1based"] == bound["min_line_1based"]
                and math.isclose(
                    existing["max_mm"], bound["max_mm"], rel_tol=0.0, abs_tol=0.02
                )
                and math.isclose(
                    existing["min_mm"], bound["min_mm"], rel_tol=0.0, abs_tol=0.02
                )
            ),
            None,
        )
        if duplicate is None:
            deduplicated.append(bound)
        elif duplicate["source_unit"] != "mm" and bound["source_unit"] == "mm":
            deduplicated[deduplicated.index(duplicate)] = bound
    return tuple(deduplicated), notation


def extract_page_text_min_max_bounds(page_text: str) -> tuple[dict[str, Any], ...]:
    bounds, _ = _extract_page_text_min_max_bounds_with_notation(page_text)
    return bounds


def fold_page_text_min_max_bounds(
    transcription: Transcription,
    page_text: str,
) -> tuple[Transcription, dict[str, Any]]:
    """Fold text-proven min/max pairs; split-role pairs degrade as one unit."""

    bounds, notation = _extract_page_text_min_max_bounds_with_notation(page_text)
    dimensions = list(transcription.dimensions)
    consumed: set[int] = set()
    replacements: dict[int, Dimension] = {}
    operations: list[dict[str, Any]] = []

    for bound_index, bound in enumerate(bounds):
        by_belongs: dict[str, list[int]] = {}
        for index, item in enumerate(dimensions):
            if index in consumed or item.is_derived:
                continue
            try:
                value_mm = dimension_value_mm(item)
            except E1aError:
                continue
            if not (
                math.isclose(value_mm, bound["max_mm"], rel_tol=0.0, abs_tol=0.011)
                or math.isclose(value_mm, bound["min_mm"], rel_tol=0.0, abs_tol=0.011)
            ):
                continue
            by_belongs.setdefault(item.belongs_to, []).append(index)

        for belongs_to, indexes in sorted(by_belongs.items()):
            indexes.sort()
            matched: tuple[int, int] | None = None
            for left, right in zip(indexes, indexes[1:]):
                if right != left + 1:
                    continue
                left_value = dimension_value_mm(dimensions[left])
                right_value = dimension_value_mm(dimensions[right])
                if math.isclose(
                    left_value, bound["max_mm"], rel_tol=0.0, abs_tol=0.011
                ) and math.isclose(
                    right_value, bound["min_mm"], rel_tol=0.0, abs_tol=0.011
                ):
                    matched = (left, right)
                    break
            if matched is None:
                continue

            pair_items = [dimensions[index] for index in matched]
            role_votes = Counter(item.role for item in pair_items)
            role = _strict_majority(
                (item.role for item in pair_items), len(pair_items), "other"
            )
            representative = pair_items[0]
            max_text = format(Decimal(str(bound["max_mm"])).normalize(), "f")
            original_role = representative.role_original
            if role == "other" and representative.role != "other":
                original_role = representative.role
            folded = Dimension(
                symbol=representative.symbol,
                value=max_text,
                unit="mm",
                raw=(
                    f"文本层 min/max {bound['max_raw']}/{bound['min_raw']} "
                    f"{bound['source_unit']}，确定性取 max"
                ),
                role=role,
                belongs_to=belongs_to,
                endpoints=representative.endpoints,
                is_derived=any(item.is_derived for item in pair_items),
                role_original=original_role,
            )
            replacements[matched[0]] = folded
            consumed.update(matched)
            operations.append(
                {
                    "bound_index": bound_index,
                    "replacement_index": matched[0],
                    "belongs_to": belongs_to,
                    "dimension_indexes": list(matched),
                    "input_values_mm": [
                        dimension_value_mm(item) for item in pair_items
                    ],
                    "role_votes": dict(sorted(role_votes.items())),
                    "resolved_role": role,
                    "role_majority": role != "other" or role_votes.get("other", 0) > 1,
                    "output_value_mm": bound["max_mm"],
                    "split_role_prevented": len(role_votes) > 1,
                    "selected_within_role_axis": True,
                    "selection_reason": "single_pair_for_role_axis",
                }
            )

    multi_pair_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    body_role_axes = {
        "body_length": "length",
        "body_width": "width",
        "body_height": "height",
    }
    for operation in operations:
        role = operation["resolved_role"]
        if (
            operation["belongs_to"] != "package_outline"
            or role not in body_role_axes
        ):
            continue
        group_key = (
            operation["belongs_to"],
            role,
            body_role_axes[role],
        )
        multi_pair_groups.setdefault(group_key, []).append(operation)

    multi_pair_selections: list[dict[str, Any]] = []
    for group_key, group in sorted(multi_pair_groups.items()):
        if len(group) < 2:
            continue
        selected = min(
            group,
            key=lambda operation: (
                operation["output_value_mm"],
                operation["bound_index"],
            ),
        )
        for operation in group:
            is_selected = operation is selected
            operation["selected_within_role_axis"] = is_selected
            operation["selection_reason"] = (
                "same_axis_same_role_multiple_pairs_choose_smaller_pair"
                if is_selected
                else "larger_pair_demoted_as_body_outer_boundary"
            )
            if is_selected:
                continue
            replacement_index = operation["replacement_index"]
            folded = replacements[replacement_index]
            replacements[replacement_index] = replace(
                folded,
                role="other",
                role_original=folded.role,
            )
        multi_pair_selections.append(
            {
                "belongs_to": group_key[0],
                "role": group_key[1],
                "axis": group_key[2],
                "pair_count": len(group),
                "candidate_pair_max_mm": sorted(
                    operation["output_value_mm"] for operation in group
                ),
                "selected_pair_max_mm": selected["output_value_mm"],
                "policy": "body is contained by overall; choose the smaller text-proven pair",
            }
        )

    folded_dimensions: list[Dimension] = []
    for index, item in enumerate(dimensions):
        if index in replacements:
            folded_dimensions.append(replacements[index])
        elif index not in consumed:
            folded_dimensions.append(item)

    stats = {
        "schema": "e1j10v2_notation_aware_min_max_folding_v1",
        "model_calls": 0,
        "notation_classifier": notation,
        "extraction_method": "notation-classified bare adjacent descending lines",
        "role_policy": "strict majority for the whole pair; no majority degrades the folded pair to other",
        "multi_pair_policy": (
            "package_outline body role only; same-axis multiple text-proven pairs choose smaller pair"
        ),
        "input_dimension_count": len(dimensions),
        "extracted_bound_count": len(bounds),
        "bounds": [dict(bound) for bound in bounds],
        "fold_operation_count": len(operations),
        "operations": operations,
        "multi_pair_selection_count": len(multi_pair_selections),
        "multi_pair_selections": multi_pair_selections,
        "output_dimension_count": len(folded_dimensions),
    }
    return replace(transcription, dimensions=tuple(folded_dimensions)), stats


def apply_page_pattern_evidence(transcription: Transcription, page_text: str) -> Transcription:
    if transcription.paste_evidence_visible or not page_mentions_lands_and_paste(page_text):
        return transcription
    return Transcription(
        dimensions=transcription.dimensions,
        pin_count=transcription.pin_count,
        recommended_land_pattern=transcription.recommended_land_pattern,
        paste_evidence_visible=True,
        unmarked_roles=transcription.unmarked_roles,
        family_proposal=transcription.family_proposal,
        family_proposal_reason=transcription.family_proposal_reason,
        discarded_dimensions=transcription.discarded_dimensions,
    )


def _is_center_distance_dimension(item: Dimension) -> bool:
    if item.role == "pad_center_distance":
        return True
    key = _dimension_text_key(item)
    normalized_symbol = _normalize_symbol(item.symbol)
    return normalized_symbol in {
        "centerx",
        "centerpitch",
        "pitchx",
        "rowspan",
        "rowspanx",
    } or any(marker in key for marker in _CENTER_TEXT_MARKERS)


def _is_total_span_dimension(item: Dimension) -> bool:
    if item.role == "overall_span":
        return True
    key = _dimension_text_key(item)
    normalized_symbol = _normalize_symbol(item.symbol)
    return normalized_symbol in {
        "z",
        "zmax",
        "overall",
        "overalllandspan",
    } or any(marker in key for marker in _TOTAL_SPAN_TEXT_MARKERS)


def _pattern_axis(item: Dimension) -> str | None:
    if item.role in {"pad_width", "tab_width", "paste_width", "lead_width"}:
        return "width"
    if item.role in {"pad_height", "tab_height", "paste_height"}:
        return "height"
    key = _dimension_text_key(item)
    if _is_center_distance_dimension(item):
        return "center"
    if any(marker in key for marker in ("height", "lengthy", "pady", "纵向", "高度")):
        return "height"
    if any(marker in key for marker in ("width", "lengthx", "padx", "横向", "宽度")):
        return "width"
    return None


def _endpoints_axis(endpoints: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", endpoints or "").lower()
    compact = re.sub(r"[\s_\-:/()]+", "", normalized)
    vertical_markers = (
        "上边",
        "下边",
        "上缘",
        "下缘",
        "上方",
        "下方",
        "上排",
        "下排",
        "topedge",
        "bottomedge",
        "upper",
        "lower",
        "vertical",
        "纵向",
    )
    horizontal_markers = (
        "左边",
        "右边",
        "左缘",
        "右缘",
        "左上",
        "右上",
        "左下",
        "右下",
        "leftedge",
        "rightedge",
        "horizontal",
        "横向",
    )
    vertical = any(marker in compact for marker in vertical_markers)
    horizontal = any(marker in compact for marker in horizontal_markers)
    if vertical and not horizontal:
        return "vertical"
    if horizontal and not vertical:
        return "horizontal"
    return None


def _paste_is_smaller_than_lands(
    lands: tuple[Dimension, ...],
    paste: tuple[Dimension, ...],
) -> tuple[bool, str]:
    land_axes: dict[str, float] = {}
    paste_axes: dict[str, float] = {}
    for item in lands:
        axis = _pattern_axis(item)
        if axis is not None:
            land_axes.setdefault(axis, dimension_value_mm(item))
    for item in paste:
        axis = _pattern_axis(item)
        if axis is not None:
            paste_axes.setdefault(axis, dimension_value_mm(item))
    common_axes = sorted(set(land_axes) & set(paste_axes))
    if common_axes:
        failed = [
            axis
            for axis in common_axes
            if not paste_axes[axis] < land_axes[axis]
        ]
        if failed:
            return False, "、".join(failed) + " 方向的 paste 未小于 lands"
        return True, "、".join(common_axes) + " 方向均满足 paste < lands"
    if len(lands) == len(paste) and lands:
        land_values = sorted(dimension_value_mm(item) for item in lands)
        paste_values = sorted(dimension_value_mm(item) for item in paste)
        if all(paste_value < land_value for paste_value, land_value in zip(paste_values, land_values)):
            return True, "两组排序后逐项满足 paste < lands"
    return False, "无法按宽/高/中心距配对两组尺寸"


def validate_center_distance_guard(
    geometry: FootprintGeometry,
    transcription: Transcription,
) -> None:
    pattern_items = _metric_land_dimensions(transcription)
    _validate_two_pad_gap_center_consistency(
        geometry.family,
        geometry.center_x,
        pattern_items,
    )
    center_items = tuple(
        item
        for item in pattern_items
        if item.belongs_to == "solder_land" and _is_center_distance_dimension(item)
    )
    center_values = tuple(dimension_value_mm(item) for item in center_items)
    if center_values:
        if any(
            math.isclose(geometry.center_x, value, rel_tol=0.0, abs_tol=0.02)
            for value in center_values
        ):
            return
        if any(
            math.isclose(
                geometry.center_x,
                value - geometry.pad_x,
                rel_tol=0.0,
                abs_tol=0.02,
            )
            for value in center_values
        ):
            raise E1aError(
                "中心距不得二次相减：图中已给出中心距，禁止再减去焊盘宽。"
            )
        visible = ", ".join(f"{value:.4g}" for value in center_values)
        raise E1aError(f"图中已给出中心距 {visible} mm；center_x 必须直接采用已确认值。")

    total_values = tuple(
        dimension_value_mm(item)
        for item in pattern_items
        if item.belongs_to == "solder_land" and _is_total_span_dimension(item)
    )
    actual_span = (
        geometry.center_x + (geometry.pad_x + geometry.tab_x) / 2.0
        if geometry.family == "ASYM3"
        else geometry.center_x + geometry.pad_x
    )
    if total_values and not any(
        math.isclose(actual_span, total, rel_tol=0.0, abs_tol=0.02)
        for total in total_values
    ):
        totals = ", ".join(f"{value:.4g}" for value in total_values)
        raise E1aError(
            "中心距推导自校验失败：焊盘外缘总跨与图中总宽 "
            f"{totals} mm 不符。"
        )


def validate_lands_paste_guard(
    geometry: FootprintGeometry,
    transcription: Transcription,
    *,
    manual_assignments: dict[str, Dimension] | None = None,
) -> None:
    pattern_items = _metric_land_dimensions(transcription)
    lands = tuple(
        item for item in pattern_items if dimension_pattern_role(item) == "lands"
    )
    paste = tuple(
        item for item in pattern_items if dimension_pattern_role(item) == "paste"
    )
    paste_visible = transcription.paste_evidence_visible or bool(paste)
    if not paste_visible:
        validate_center_distance_guard(geometry, transcription)
        return

    paste_by_axis = {
        axis: dimension_value_mm(item)
        for item in paste
        if (axis := _pattern_axis(item)) in {"width", "height"}
    }
    for field, axis in (("pad_x", "width"), ("pad_y", "height")):
        if axis in paste_by_axis and math.isclose(
            getattr(geometry, field),
            paste_by_axis[axis],
            rel_tol=0.0,
            abs_tol=0.02,
        ):
            raise E1aError(
                f"{field} 命中焊膏开口 / paste 尺寸；必须改用 solder lands。"
            )

    lands_by_axis = {
        axis: dimension_value_mm(item)
        for item in lands
        if (axis := _pattern_axis(item)) in {"width", "height"}
    }
    paste_ok, _detail = _paste_is_smaller_than_lands(lands, paste)
    if lands and paste and paste_ok and {"width", "height"}.issubset(lands_by_axis):
        if not math.isclose(
            geometry.pad_x, lands_by_axis["width"], rel_tol=0.0, abs_tol=0.02
        ) or not math.isclose(
            geometry.pad_y, lands_by_axis["height"], rel_tol=0.0, abs_tol=0.02
        ):
            raise E1aError("铜箔焊盘尺寸必须采用已识别的 solder lands 宽和高。")
    else:
        selected = manual_assignments or {}
        for field in ("pad_x", "pad_y"):
            item = selected.get(field)
            if item is None:
                raise E1aError(
                    "疑似含钢网开口尺寸且无法自动区分；请从候选列表明确点选 lands 的宽和高。"
                )
            if dimension_pattern_role(item) == "paste":
                raise E1aError("人工点选仍指向 paste；禁止生成 footprint。")
            if not math.isclose(
                getattr(geometry, field),
                dimension_value_mm(item),
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise E1aError(f"{field} 已在点选后被修改；请重新点选确认。")

    validate_center_distance_guard(geometry, transcription)


def _unit_scale_to_mm(unit: str) -> Decimal:
    normalized = unicodedata.normalize("NFKC", unit).strip().lower().replace(" ", "")
    if normalized in {"mm", "millimeter", "millimeters", "毫米"}:
        return Decimal("1")
    if normalized in {"mil", "mils"}:
        return Decimal("0.0254")
    if normalized in {"in", "inch", "inches", '"'}:
        return Decimal("25.4")
    raise E1aError(f"E1a 仅支持 mm/mil/inch，当前单位为 {unit!r}。")


def dimension_value_mm(item: Dimension) -> float:
    try:
        number = Decimal(item.value.replace(",", "."))
    except InvalidOperation as exc:
        raise E1aError(f"尺寸值无法解析：{item.value!r}") from exc
    result = number * _unit_scale_to_mm(item.unit)
    if result <= 0:
        raise E1aError(f"尺寸值必须大于 0：{item.value!r}")
    return float(result)


def _endpoint_key(item: Dimension) -> str:
    normalized = unicodedata.normalize("NFKC", item.endpoints or "").lower()
    return re.sub(r"[\s_\-:/()→]+", "", normalized)


def _raw_annotation_key(item: Dimension) -> str:
    normalized = unicodedata.normalize("NFKC", item.raw or "").lower()
    return re.sub(r"\s+", "", normalized)


def _deduplicate_dual_unit_dimensions(
    dimensions: tuple[Dimension, ...],
) -> tuple[Dimension, ...]:
    """Drop imperial duplicates only when the physical annotation also matches."""
    metric_indexes = [
        index for index, item in enumerate(dimensions) if _is_metric_unit(item.unit)
    ]
    dropped: set[int] = set()
    for index, item in enumerate(dimensions):
        if index in metric_indexes:
            continue
        try:
            imperial_mm = dimension_value_mm(item)
        except E1aError:
            continue
        item_endpoint = _endpoint_key(item)
        item_raw = _raw_annotation_key(item)
        for metric_index in metric_indexes:
            metric = dimensions[metric_index]
            if metric.belongs_to != item.belongs_to:
                continue
            same_annotation = bool(item_endpoint) and item_endpoint == _endpoint_key(metric)
            same_annotation = same_annotation or (
                bool(item_raw) and item_raw == _raw_annotation_key(metric)
            )
            if not same_annotation:
                continue
            metric_mm = dimension_value_mm(metric)
            scale = max(metric_mm, imperial_mm)
            if scale > 0 and abs(metric_mm - imperial_mm) / scale <= 0.02:
                dropped.add(index)
                break
    return tuple(item for index, item in enumerate(dimensions) if index not in dropped)


def _dimension_map(
    transcription: Transcription | Iterable[Dimension],
) -> dict[str, Dimension]:
    result: dict[str, Dimension] = {}
    items = (
        transcription.dimensions
        if isinstance(transcription, Transcription)
        else transcription
    )
    for item in items:
        if item.is_derived:
            continue
        key = _normalize_symbol(item.symbol)
        result.setdefault(key, item)
    return result


def _find_dimension(mapping: dict[str, Dimension], aliases: Iterable[str]) -> Dimension | None:
    for alias in aliases:
        found = mapping.get(_normalize_symbol(alias))
        if found is not None:
            return found
    return None


_LAND_GROUP_MARKERS = (
    "landpattern",
    "recommendedland",
    "recommendedpadlayout",
    "padlayout",
)
_LAND_ROLE_ALIASES = {
    _normalize_symbol(alias)
    for alias in (
        "PAD_X",
        "PAD_LENGTH",
        "LAND_LENGTH",
        "Y",
        "PAD_Y",
        "PAD_WIDTH",
        "LAND_WIDTH",
        "X",
        "CENTER_X",
        "CENTER_PITCH",
        "PITCH_X",
        "ROW_SPAN",
        "ROW_SPAN_X",
        "PITCH_Y",
        "PIN_PITCH",
        "C",
        "C1",
        "C2",
        "E",
        "Z",
        "ZMAX",
        "OVERALL",
        "OVERALL_LAND_SPAN",
        "G",
        "GMIN",
        "GAP",
        "INNER_GAP",
    )
}
_BODY_GROUP_MARKERS = ("physicaldimensions", "productdimensions", "dimensions")


def _is_land_dimension(item: Dimension) -> bool:
    key = _normalize_symbol(item.symbol)
    text_key = _dimension_text_key(item)
    return (
        item.role
        in {
            "pad_width",
            "pad_height",
            "pad_gap",
            "pad_center_distance",
            "overall_span",
            "tab_width",
            "tab_height",
            "half_pitch",
            "half_pitch_from_centerline",
            "center_to_edge",
            "gap_between_pads",
            "pitch",
            "paste_width",
            "paste_height",
        }
        or item.belongs_to in {"solder_land", "solder_paste", "occupied_area"}
        or any(marker in key for marker in _LAND_GROUP_MARKERS)
        or key in _LAND_ROLE_ALIASES
        or dimension_pattern_role(item) in {"lands", "paste"}
        or _is_center_distance_dimension(item)
        or _is_total_span_dimension(item)
        or any(marker in text_key for marker in _PASTE_TEXT_MARKERS)
    )


def _is_metric_unit(unit: str) -> bool:
    normalized = unicodedata.normalize("NFKC", unit).strip().lower().replace(" ", "")
    return normalized in {"mm", "millimeter", "millimeters", "毫米"}


def _raw_has_tolerance(raw: str) -> bool:
    normalized = unicodedata.normalize("NFKC", raw)
    return bool(
        "±" in normalized
        or "+/-" in normalized
        or re.search(
            r"\+\s*(?:\d|[.,])[\s\S]*[-−]\s*(?:\d|[.,])",
            normalized,
        )
    )


_BODY_NOTATION_QUALIFIER_RE = re.compile(
    r"\b(?:max(?:imum)?|min(?:imum)?|ref(?:erence)?|typ(?:ical)?)\b",
    re.IGNORECASE,
)


def _body_notation_tokens_for_value(
    item: Dimension,
    notation_ledger: dict[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    """Return existing classifier tokens matching one body candidate in mm."""
    if not notation_ledger:
        return ()
    try:
        value_mm = dimension_value_mm(item)
    except E1aError:
        return ()
    tokens = tuple(
        token
        for token in notation_ledger.get("tokens", ())
        if token.get("value_mm") is not None
        and math.isclose(
            float(token["value_mm"]), value_mm, rel_tol=0.0, abs_tol=0.011
        )
    )
    package_tokens = tuple(
        token for token in tokens if token.get("context") == "package_outline"
    )
    return package_tokens or tokens


def _body_notation_candidate_decision(
    item: Dimension,
    notation_ledger: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Reuse the page notation classifier for drawing-orientation body roles."""
    raw = unicodedata.normalize("NFKC", item.raw or "").strip()
    if raw.startswith("文本层 min/max "):
        return True, "既有文本层 min/max 折叠结果"

    # The classifier already rejects these as nominal candidates.  This branch
    # only carries that decision to the body-role selector; it does not invent
    # a new interpretation of a notation token.
    if _BODY_NOTATION_QUALIFIER_RE.search(raw):
        return False, "MAX/MIN/REF/TYP 限定值不是标称"

    paired = _NOTATION_VALUE_PAREN_RE.match(raw)
    if paired:
        try:
            candidate_mm = dimension_value_mm(item)
        except E1aError:
            return False, "括号表达式无法换算为 mm"
        unit_direction = (notation_ledger or {}).get("unit_direction", {})
        if unit_direction.get("status") != "resolved":
            return False, "括号值未通过表头 + 25.4 双验证"
        matching_tokens = _body_notation_tokens_for_value(item, notation_ledger)
        if any(
            token.get("accepted_as_nominal")
            and token.get("value_mm") is not None
            and math.isclose(
                float(token["value_mm"]), candidate_mm, rel_tol=0.0, abs_tol=0.011
            )
            for token in matching_tokens
        ):
            return True, "括号值通过表头 + 25.4 双验证"
        return False, "括号值未通过表头 + 25.4 双验证"

    if _raw_has_tolerance(raw):
        # A±B and A+x−y use A only.  If the raw expression has no numeric A
        # (for example φD±0.5 or B±0.2), there is no nominal body value here.
        leading = re.match(rf"^\s*({_NOTATION_NUMBER_PATTERN})", raw)
        if leading is None:
            return False, "公差表达式没有可采纳的数值标称 A"
        if not _first_raw_number_matches_value(item):
            return False, "候选值落在公差项，不是表达式首个标称 A"
        return True, "公差表达式只采纳首个标称 A"

    # For a bare model value, use the same page token decision when the page
    # contains a matching qualified/parenthetical token.  Ambiguous evidence
    # is withheld rather than resolved by size or order.
    if _NOTATION_BARE_RE.match(raw):
        tokens = _body_notation_tokens_for_value(item, notation_ledger)
        accepted = tuple(token for token in tokens if token.get("accepted_as_nominal"))
        rejected = tuple(token for token in tokens if not token.get("accepted_as_nominal"))
        if rejected and not accepted:
            return False, "页面记号分类将匹配值判为界限/非标称"
        if accepted and rejected:
            return False, "页面同值同时存在可采纳与拒绝记号，证据有歧义"

    return True, "无新增解释；沿用既有标称候选"


def _drawing_orientation_role_items(
    transcription: Transcription,
    role: str,
    notation_ledger: dict[str, Any] | None,
) -> tuple[tuple[Dimension, ...], tuple[str, ...]]:
    """Select body-role candidates after applying the existing notation gate."""
    candidates = _role_items(
        transcription,
        role,
        belongs_to="package_outline",
    )
    allowed: list[Dimension] = []
    rejected: list[str] = []
    for item in candidates:
        accepted, reason = _body_notation_candidate_decision(item, notation_ledger)
        if not accepted:
            rejected.append(f"{item.raw}: {reason}")
            continue
        source_allowed, source_reason = _body_axis_candidate_decision(item)
        if source_allowed:
            allowed.append(item)
        else:
            rejected.append(f"{item.raw}: {source_reason}")
    return tuple(allowed), tuple(rejected)


def _is_generic_land_group_item(item: Dimension) -> bool:
    key = _normalize_symbol(item.symbol)
    return key in {"dimension", "dimensions"} or any(
        marker in key for marker in _LAND_GROUP_MARKERS
    )


def _metric_land_dimensions(transcription: Transcription) -> tuple[Dimension, ...]:
    explicit_pattern = tuple(
        item
        for item in transcription.dimensions
        if item.belongs_to in {"solder_land", "solder_paste"}
    )
    if explicit_pattern:
        metric = tuple(item for item in explicit_pattern if _is_metric_unit(item.unit))
        return metric or explicit_pattern

    grouped = tuple(
        item
        for item in transcription.dimensions
        if any(
            marker in _normalize_symbol(item.symbol)
            for marker in _LAND_GROUP_MARKERS
        )
    )
    role_items = tuple(
        item for item in transcription.dimensions if _is_land_dimension(item)
    )
    if grouped:
        items = tuple(
            item
            for item in transcription.dimensions
            if item in grouped
            or dimension_pattern_role(item) in {"lands", "paste"}
            or _is_center_distance_dimension(item)
            or _is_total_span_dimension(item)
        )
    else:
        items = role_items
    if not items and transcription.recommended_land_pattern:
        leading_unlabelled: list[Dimension] = []
        for item in transcription.dimensions:
            if not _is_metric_unit(item.unit):
                continue
            if _raw_has_tolerance(item.raw):
                break
            if _normalize_symbol(item.symbol) not in {"dimension", "dimensions"}:
                if leading_unlabelled:
                    break
                continue
            leading_unlabelled.append(item)
        items = tuple(leading_unlabelled)
    if not items:
        return ()
    metric = tuple(
        item
        for item in items
        if _is_metric_unit(item.unit)
    )
    return metric or items


def _first_raw_number_matches_value(item: Dimension) -> bool:
    match = re.search(r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)", item.raw)
    if match is None:
        return False
    try:
        first = Decimal(match.group(0).replace(",", "."))
        value = Decimal(item.value.replace(",", "."))
    except InvalidOperation:
        return False
    return first == value


def _assign_grouped_body_dimensions(
    transcription: Transcription,
    values: dict[str, float | None],
    sources: dict[str, str],
) -> None:
    if values["body_x"] is not None and values["body_y"] is not None:
        return
    grouped_candidates: list[Dimension] = []
    for item in transcription.dimensions:
        key = _normalize_symbol(item.symbol)
        if (
            item.is_derived
            or item.role in {"body_length", "body_width", "body_height"}
            or _is_land_dimension(item)
            or not any(
            marker in key for marker in _BODY_GROUP_MARKERS
            )
        ):
            continue
        if not _raw_has_tolerance(item.raw) or not _first_raw_number_matches_value(item):
            continue
        try:
            dimension_value_mm(item)
        except E1aError:
            continue
        grouped_candidates.append(item)
    metric_candidates = [
        item for item in grouped_candidates if _is_metric_unit(item.unit)
    ]
    pool = metric_candidates or grouped_candidates
    seen_values = [
        float(value)
        for field in ("body_x", "body_y")
        if (value := values[field]) is not None
    ]
    candidates: list[Dimension] = []
    for item in pool:
        value = dimension_value_mm(item)
        if any(math.isclose(value, seen, rel_tol=0.0, abs_tol=1e-9) for seen in seen_values):
            continue
        candidates.append(item)
        seen_values.append(value)
        if len(candidates) == sum(values[field] is None for field in ("body_x", "body_y")):
            break
    candidate_index = 0
    for field in ("body_x", "body_y"):
        if values[field] is not None:
            continue
        if candidate_index >= len(candidates):
            break
        item = candidates[candidate_index]
        candidate_index += 1
        values[field] = dimension_value_mm(item)
        sources[field] = f"{item.symbol} 分组第 {candidate_index} 个本体基准值"


def _assign_role_body_dimensions(
    transcription: Transcription,
    values: dict[str, float | None],
    sources: dict[str, str],
    warnings: list[str],
    family: str,
) -> None:
    if family == "ASYM3":
        layout_overall = _role_items(
            transcription,
            "overall_span",
            belongs_to="solder_land",
        )
        layout_rotated = (
            len(layout_overall) == 1
            and _endpoints_axis(layout_overall[0].endpoints) == "vertical"
        )
        candidates = tuple(
            item
            for item in transcription.dimensions
            if item.belongs_to == "package_outline"
            and item.role in {"body_length", "body_width"}
            and not item.is_derived
        )
        axis_fields = (
            (("width", "body_y"), ("height", "body_x"))
            if layout_rotated
            else (("width", "body_x"), ("height", "body_y"))
        )
        endpoint_axis_used = False
        for axis, field in axis_fields:
            axis_candidates = tuple(
                item for item in candidates if _endpoint_span_axis(item) == axis
            )
            if not axis_candidates:
                continue
            endpoint_axis_used = True
            outer = tuple(
                item
                for item in axis_candidates
                if not any(
                    marker in _endpoint_key(item)
                    for marker in ("内侧", "内部", "inner", "inside")
                )
            )
            pool = outer or axis_candidates
            item = max(pool, key=dimension_value_mm)
            values[field] = dimension_value_mm(item)
            sources[field] = f"端点轴向直取 ASYM3 外形最大边界：{item.raw}"
            if len(axis_candidates) > 1:
                warnings.append(
                    f"ASYM3 {axis} 轴有 {len(axis_candidates)} 个范围/视图值；"
                    f"已按外形最大边界 {item.value} {item.unit} 预填。"
                )
        if endpoint_axis_used:
            if layout_rotated:
                warnings.append("ASYM3 本体轴已随竖直 LAND 布局旋转到生成器坐标。")
            return

    role_fields = {
        "body_length": "body_x",
        "body_width": "body_y",
    }
    for role, field in role_fields.items():
        candidates = _role_items(
            transcription,
            role,
            belongs_to="package_outline",
        )
        if len(candidates) == 1:
            item = candidates[0]
            values[field] = dimension_value_mm(item)
            sources[field] = f"角色直取 {role}：{item.raw}"
        elif len(candidates) > 1:
            item, reason = _select_body_outer_max(candidates)
            if item is None:
                warnings.append(
                    f"角色 {role} 有 {len(candidates)} 个不同规格/边界值，未自动选择。"
                )
                continue
            values[field] = dimension_value_mm(item)
            sources[field] = f"角色直取 {role} 外形最大边界：{item.raw}"
            warnings.append(
                f"角色 {role} 有 {len(candidates)} 个{reason}；"
                f"已按外形最大边界 {item.value} {item.unit} 预填。"
            )


def _select_body_outer_max(
    candidates: tuple[Dimension, ...],
) -> tuple[Dimension | None, str | None]:
    """Select max only for one physical boundary or an explicit min/max pair."""
    if len(candidates) < 2:
        return (candidates[0], "直接标注值") if candidates else (None, None)

    outer = tuple(
        item
        for item in candidates
        if not any(
            marker in _endpoint_key(item)
            for marker in ("内侧", "内部", "inner", "inside")
        )
    )
    pool = outer or candidates
    endpoint_groups: dict[str, list[Dimension]] = {}
    for item in pool:
        endpoint_groups.setdefault(_canonical_body_endpoints(item), []).append(item)

    if len(endpoint_groups) == 1:
        group = next(iter(endpoint_groups.values()))
        values = [dimension_value_mm(item) for item in group]
        if len(values) == 2:
            lower, upper = sorted(values)
            if lower > 0.0 and (upper - lower) / lower < 0.20:
                return max(group, key=dimension_value_mm), "同一尺寸 min/max 值"
        if _all_body_values_are_min_max(group):
            return max(group, key=dimension_value_mm), "同一尺寸 min/max 值"
        if all(
            math.isclose(value, values[0], rel_tol=0.0, abs_tol=1e-9)
            for value in values[1:]
        ):
            return group[0], "多视图重复值"
        return max(group, key=dimension_value_mm), "同一外形边界的范围/视图值"

    # Different endpoint descriptions can be different physical boundaries.
    # Do not merge them merely because their values happen to be close.
    return None, None


def _canonical_body_endpoints(item: Dimension) -> str:
    key = _endpoint_key(item)
    for marker in (
        "正视图",
        "俯视图",
        "顶视图",
        "底视图",
        "侧视图",
        "frontview",
        "topview",
        "bottomview",
        "sideview",
    ):
        key = key.replace(marker, "")
    return key


def _all_body_values_are_min_max(candidates: list[Dimension]) -> bool:
    markers = tuple(_normalize_symbol(item.raw) for item in candidates)
    has_min = any("min" in marker or "minimum" in marker for marker in markers)
    has_max = any("max" in marker or "maximum" in marker for marker in markers)
    return has_min and has_max


def _role_items(
    transcription: Transcription,
    role: str,
    *,
    belongs_to: str | None = None,
) -> tuple[Dimension, ...]:
    candidates = tuple(
        item
        for item in transcription.dimensions
        if item.role == role
        and not item.is_derived
        and (belongs_to is None or item.belongs_to == belongs_to)
    )
    return _deduplicate_dimension_candidates(candidates)


def _deduplicate_dimension_candidates(
    candidates: tuple[Dimension, ...],
) -> tuple[Dimension, ...]:
    if not candidates:
        return ()
    non_tolerance = tuple(
        item
        for item in candidates
        if not any(
            marker in _endpoint_key(item)
            for marker in (
                "公差",
                "偏差",
                "tolerance",
                "deviation",
            )
        )
    )
    if non_tolerance:
        candidates = non_tolerance
    # One physical annotation is often emitted once for the primary unit and
    # again for its parenthesized conversion.  A nominal and each tolerance
    # can likewise arrive as separate records sharing the same raw string.
    # Prefer metric records, then records whose value is the first number in
    # the annotation, and finally collapse exact numeric duplicates.  Ranges
    # with genuinely different direct values remain ambiguous.
    metric = tuple(item for item in candidates if _is_metric_unit(item.unit))
    pool = metric or candidates
    primary = tuple(item for item in pool if _first_raw_number_matches_value(item))
    if primary:
        pool = primary
    deduplicated: list[Dimension] = []
    seen_values: list[float] = []
    for item in pool:
        value = dimension_value_mm(item)
        if any(
            math.isclose(value, seen, rel_tol=0.0, abs_tol=1e-9)
            for seen in seen_values
        ):
            continue
        deduplicated.append(item)
        seen_values.append(value)
    return tuple(deduplicated)


def _endpoint_span_axis(item: Dimension) -> str | None:
    key = _endpoint_key(item)
    left = any(
        marker in key
        for marker in (
            "左缘",
            "左边",
            "左侧",
            "左端",
            "左外缘",
            "左侧外缘",
            "leftedge",
            "leftouteredge",
        )
    ) or (
        "left" in key and "right" in key
    )
    right = any(
        marker in key
        for marker in (
            "右缘",
            "右边",
            "右侧",
            "右端",
            "右外缘",
            "右侧外缘",
            "rightedge",
            "rightouteredge",
        )
    ) or (
        "left" in key and "right" in key
    )
    top = any(
        marker in key
        for marker in (
            "上缘",
            "上边",
            "上侧",
            "上端",
            "上表面",
            "顶面",
            "顶部",
            "上外缘",
            "上侧外缘",
            "topedge",
            "upperedge",
            "topouteredge",
            "upperouteredge",
            "topsurface",
        )
    ) or (
        "top" in key and "bottom" in key
    )
    bottom = any(
        marker in key
        for marker in (
            "下缘",
            "下边",
            "下侧",
            "下端",
            "下表面",
            "底面",
            "底部",
            "下外缘",
            "下侧外缘",
            "bottomedge",
            "loweredge",
            "bottomouteredge",
            "lowerouteredge",
            "bottomsurface",
        )
    ) or (
        "top" in key and "bottom" in key
    )
    if left and right and not (top and bottom):
        return "width"
    if top and bottom and not (left and right):
        return "height"
    return None


def _body_planar_endpoint_axis(item: Dimension) -> str | None:
    """Recognize explicit outer-edge wording used by planar body drawings."""
    axis = _endpoint_span_axis(item)
    if axis is not None:
        return axis
    key = _endpoint_key(item)
    horizontal = any(
        marker in key
        for marker in ("左外缘", "右外缘", "左侧外缘", "右侧外缘", "左端面", "右端面")
    )
    vertical = any(
        marker in key
        for marker in ("上外缘", "下外缘", "上侧外缘", "下侧外缘", "上端面", "下端面")
    )
    if horizontal and not vertical:
        return "width"
    if vertical and not horizontal:
        return "height"
    return None


_BODY_DIAMETER_CONTEXT_MARKERS = (
    "diameter",
    "cylinder",
    "cylindrical",
    "直径",
    "圆柱",
    "外周",
    "穿过圆心",
)
_BODY_HEIGHT_CONTEXT_MARKERS = (
    "height",
    "thickness",
    "高度",
    "厚度",
    "顶面",
    "底面",
)
_BODY_VIEW_MARKERS = (
    "正视图",
    "俯视图",
    "顶视图",
    "底视图",
    "侧视图",
    "frontview",
    "topview",
    "bottomview",
    "sideview",
)


def _body_axis_context(item: Dimension) -> str:
    return unicodedata.normalize(
        "NFKC", f"{item.symbol} {item.raw} {item.endpoints}"
    ).lower()


def _body_axis_candidate_decision(item: Dimension) -> tuple[bool, str]:
    """Reject diameter/height annotations before selecting rectangular axes."""
    symbol = unicodedata.normalize("NFKC", item.symbol or "").strip().lower()
    context = _body_axis_context(item)
    symbol_key = _normalize_symbol(symbol)
    diameter_symbol = (
        any(marker in symbol for marker in ("φ", "ϕ", "phi"))
        or symbol_key in {"phid", "diameter"}
    )
    diameter_context = any(marker in context for marker in _BODY_DIAMETER_CONTEXT_MARKERS)
    indexed_diameter_symbol = bool(re.fullmatch(r"d\d*", symbol_key)) and diameter_context
    if diameter_symbol or indexed_diameter_symbol:
        return False, "直径/圆柱尺寸不得作为矩形本体轴"

    height_symbol = symbol_key in {"l", "h", "a"}
    endpoint_axis = _body_planar_endpoint_axis(item)
    height_context = (
        item.role == "body_height"
        or any(marker in context for marker in _BODY_HEIGHT_CONTEXT_MARKERS)
        or (height_symbol and endpoint_axis == "height")
    )
    if height_symbol and height_context:
        return False, "高度/厚度尺寸不得作为矩形本体轴"
    return True, "平面本体候选"


def _body_planar_pair_member_decision(item: Dimension) -> tuple[bool, str]:
    """A pair member must represent one planar dimension, not a value list."""
    raw = unicodedata.normalize("NFKC", item.raw or "").strip()
    if (
        raw.startswith("文本层 min/max ")
        or _raw_has_tolerance(raw)
        or _NOTATION_VALUE_PAREN_RE.match(raw)
    ):
        return True, "单一平面尺寸（含既有公差/括号形式）"
    raw_numbers = re.findall(
        rf"(?<![A-Za-z])({_NOTATION_NUMBER_PATTERN})(?![A-Za-z])", raw
    )
    if len(raw_numbers) > 1:
        return False, "同一原文串含多个未分组数值，不是单一平面尺寸符号"
    return True, "单一平面尺寸"


def _body_view_key(item: Dimension) -> str:
    key = _endpoint_key(item)
    for marker in _BODY_VIEW_MARKERS:
        compact = _normalize_symbol(marker)
        if marker in key or compact in key:
            return marker
    return ""


def _body_endpoint_context_key(item: Dimension) -> str:
    """Keep the physical-view prefix while dropping the two endpoint names."""
    key = _endpoint_key(item)
    for marker in _BODY_VIEW_MARKERS:
        key = key.replace(marker, "")
    edge_markers = (
        "左",
        "右",
        "上",
        "下",
        "前",
        "后",
        "left",
        "right",
        "top",
        "bottom",
        "front",
        "back",
    )
    positions = [key.find(marker) for marker in edge_markers if key.find(marker) >= 0]
    return key[: min(positions)] if positions else key


def _body_pair_group_key(left: dict[str, Any], right: dict[str, Any]) -> tuple[Any, ...]:
    left_item = left["item"]
    right_item = right["item"]
    views = tuple(sorted(filter(None, (_body_view_key(left_item), _body_view_key(right_item)))))
    symbols = tuple(sorted((_normalize_symbol(left_item.symbol), _normalize_symbol(right_item.symbol))))
    roles = tuple(sorted((left["role"], right["role"])))
    context = tuple(
        sorted(
            (
                _body_endpoint_context_key(left_item),
                _body_endpoint_context_key(right_item),
            )
        )
    )
    return (views[0] if len(views) == 1 else "", context, symbols, roles)


def _select_unique_planar_body_pair(
    records: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]] | None, str]:
    """Require one auditable horizontal/vertical planar-symbol pair."""
    width_records = [record for record in records if record["endpoint_axis"] == "width"]
    height_records = [record for record in records if record["endpoint_axis"] == "height"]
    groups: dict[tuple[Any, ...], dict[str, list[Dimension]]] = {}
    for width in width_records:
        for height in height_records:
            if not (
                {width["role"], height["role"]}
                == {"body_length", "body_width"}
            ):
                continue
            width_item = width["item"]
            height_item = height["item"]
            width_allowed, _ = _body_planar_pair_member_decision(width_item)
            height_allowed, _ = _body_planar_pair_member_decision(height_item)
            if not (width_allowed and height_allowed):
                continue
            width_view = _body_view_key(width_item)
            height_view = _body_view_key(height_item)
            same_view = bool(width_view and width_view == height_view)
            same_symbol = _normalize_symbol(width_item.symbol) == _normalize_symbol(
                height_item.symbol
            )
            shared_context = bool(
                _body_endpoint_context_key(width_item)
                and _body_endpoint_context_key(width_item)
                == _body_endpoint_context_key(height_item)
            )
            if not (same_view or same_symbol or shared_context):
                continue
            key = _body_pair_group_key(width, height)
            group = groups.setdefault(key, {"width": [], "height": []})
            if width_item not in group["width"]:
                group["width"].append(width_item)
            if height_item not in group["height"]:
                group["height"].append(height_item)

    valid: list[tuple[tuple[Any, ...], Dimension, Dimension, str, str]] = []
    for key, group in groups.items():
        width_item, width_reason = _select_body_outer_max(tuple(group["width"]))
        height_item, height_reason = _select_body_outer_max(tuple(group["height"]))
        if width_item is None or height_item is None:
            continue
        valid.append(
            (
                key,
                width_item,
                height_item,
                width_reason or "唯一横向候选",
                height_reason or "唯一纵向候选",
            )
        )

    if len(valid) != 1:
        if not valid:
            return None, "本体候选无法形成唯一横/竖成对平面尺寸符号"
        return None, f"本体存在 {len(valid)} 个不可排除的成对平面尺寸候选"

    key, width_item, height_item, width_reason, height_reason = valid[0]
    return (
        {
            "body_x": {
                "item": width_item,
                "endpoint_axis": "width",
                "selection_reason": width_reason,
            },
            "body_y": {
                "item": height_item,
                "endpoint_axis": "height",
                "selection_reason": height_reason,
            },
        },
        "唯一成对平面尺寸符号："
        f"{width_item.symbol}/{height_item.symbol}（组键={key!r}）",
    )


def _resolve_body_axis_provenance(
    transcription: Transcription,
    family: str,
    combination: str | None,
    values: dict[str, float | None],
    sources: dict[str, str],
    warnings: list[str],
    notation_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assign body axes only when source axis and footprint mapping are unique."""
    assignments: dict[str, dict[str, Any]] = {}
    unresolved_reasons: list[str] = []
    provenance: dict[str, Any] = {
        "schema": "e1j12_body_axis_provenance_v1",
        "status": "absent_requires_user_input",
        "family": family,
        "geometry_solution": combination,
        "mapping_source": (
            "land_solution" if combination is not None else "drawing_orientation"
        ),
        "mapping_basis": None,
        "assignments": assignments,
        "unresolved_reasons": unresolved_reasons,
    }

    values["body_x"] = None
    values["body_y"] = None
    sources.pop("body_x", None)
    sources.pop("body_y", None)

    raw_body_candidates = tuple(
        item
        for item in transcription.dimensions
        if item.belongs_to == "package_outline"
        and item.role in {"body_length", "body_width"}
        and not item.is_derived
    )
    body_source_rejections: list[str] = []
    filtered_body_candidates: list[Dimension] = []
    for item in raw_body_candidates:
        if family in {"INLINE3", "GRID4"}:
            normalized_raw = unicodedata.normalize("NFKC", item.raw or "").lower()
            limit_marked = bool(
                re.search(r"(?:max(?:imum)?|min(?:imum)?|ref(?:erence)?|typ(?:ical)?)", normalized_raw)
            )
            pair_member, pair_reason = _body_planar_pair_member_decision(item)
            accepted = (
                _body_planar_endpoint_axis(item) in {"width", "height"}
                and pair_member
                and not _family_tolerance_only(item)
                and not limit_marked
            )
            reason = (
                "新族平面本体端点与标称门通过"
                if accepted
                else "新族本体仅接受明确平面端点的非限定标称值：" + pair_reason
            )
        else:
            accepted, reason = _body_axis_candidate_decision(item)
        if accepted:
            filtered_body_candidates.append(item)
        else:
            body_source_rejections.append(f"{item.symbol} {item.raw}: {reason}")
    body_candidates = tuple(filtered_body_candidates)
    provenance["body_source_gate"] = {
        "status": "applied",
        "rejected": body_source_rejections,
    }
    if body_source_rejections:
        warnings.append(
            "矩形本体轴来源闸剔除非平面/非标称候选："
            + "；".join(body_source_rejections)
        )

    if combination is None:
        # LAND 没有唯一解时，body 不再沿用 LAND 的轴向链。先采用 body
        # 自身端点轴证据；只有该证据缺失时，才使用 body_length/body_width
        # 的记号分类作为原图朝向证据。整个分支不使用数值大小推断。
        provenance["mapping_basis"] = (
            "无 LAND 唯一解；按原图朝向固定映射 original_image:width -> "
            "footprint X、original_image:height -> footprint Y；"
            "仅接受 body 自身的端点轴或记号分类证据"
        )
        role_candidates: dict[str, tuple[Dimension, ...]] = {}
        notation_rejections: dict[str, tuple[str, ...]] = {}
        for role in ("body_length", "body_width"):
            selected, rejected = _drawing_orientation_role_items(
                transcription,
                role,
                notation_ledger,
            )
            role_candidates[role] = selected
            if rejected:
                notation_rejections[role] = rejected
        provenance["notation_guard"] = {
            "status": "applied" if notation_ledger is not None else "raw_only",
            "classifier_schema": (
                notation_ledger.get("schema") if notation_ledger is not None else None
            ),
            "rejected_by_role": {
                role: list(reasons) for role, reasons in notation_rejections.items()
            },
        }
        if notation_rejections:
            warnings.append(
                "drawing_orientation 已接入既有记号/标称闸；剔除候选："
                + "；".join(
                    f"{role}=" + ", ".join(reasons)
                    for role, reasons in notation_rejections.items()
                )
            )
        records = [
            {
                "item": item,
                "role": role,
                "endpoint_axis": _body_planar_endpoint_axis(item),
                "semantic_axis": "width" if role == "body_length" else "height",
            }
            for role, items in role_candidates.items()
            for item in items
        ]
        pair_records, pair_reason = _select_unique_planar_body_pair(records)
        provenance["planar_pair_gate"] = {
            "status": "passed" if pair_records is not None else "unresolved",
            "reason": pair_reason,
            "candidate_count": len(records),
        }
        if pair_records is None:
            for field_name, source_axis in (
                ("body_x", "width"),
                ("body_y", "height"),
            ):
                target_axis = "footprint_x" if field_name == "body_x" else "footprint_y"
                notation_detail = ""
                rejected = notation_rejections.get(
                    "body_length" if field_name == "body_x" else "body_width",
                    (),
                )
                if rejected:
                    notation_detail = "；既有记号/来源闸已剔除：" + "，".join(rejected)
                reason = (
                    f"{field_name} 未形成唯一成对平面本体来源：{pair_reason}。"
                    "LAND 无唯一解时不借用焊盘轴向。"
                    + notation_detail
                )
                unresolved_reasons.append(reason)
                assignments[field_name] = {
                    "status": "unresolved",
                    "target_axis": target_axis,
                    "source_axis": source_axis,
                    "mapping_chain": [],
                    "basis": [reason],
                }
        else:
            for field_name, source_axis in (
                ("body_x", "width"),
                ("body_y", "height"),
            ):
                target_axis = "footprint_x" if field_name == "body_x" else "footprint_y"
                record = pair_records[field_name]
                item = record["item"]
                value = dimension_value_mm(item)
                role_step = item.role
                assignments[field_name] = {
                    "status": "proven",
                    "value_mm": value,
                    "target_axis": target_axis,
                    "source_axis": source_axis,
                    "source_role": item.role,
                    "source_role_original": getattr(item, "role_original", ""),
                    "source_symbol": item.symbol,
                    "source_raw": item.raw,
                    "source_endpoints": item.endpoints,
                    "candidate_count": sum(
                        candidate["endpoint_axis"] == source_axis for candidate in records
                    ),
                    "mapping_chain": [
                        f"original_image:{source_axis}",
                        role_step,
                        f"drawing_orientation:{source_axis}->{target_axis}",
                        target_axis,
                    ],
                    "basis": [
                        f"成对平面符号证明 original_image:{source_axis}：{item.endpoints}",
                        f"候选裁定：{record['selection_reason']}",
                        pair_reason,
                        str(provenance["mapping_basis"]),
                    ],
                }

        complete = (
            set(assignments) == {"body_x", "body_y"}
            and all(record.get("status") == "proven" for record in assignments.values())
        )
        if complete:
            for field_name in ("body_x", "body_y"):
                record = assignments[field_name]
                values[field_name] = float(record["value_mm"])
                sources[field_name] = (
                    "body 按原图朝向证明："
                    f"{record['source_raw']} ({record['source_axis']} -> "
                    f"{record['target_axis']})"
                )
            provenance["status"] = "proven"
            return provenance

        for record in assignments.values():
            if record.get("status") == "proven":
                record["status"] = "withheld_pair_incomplete"
        values["body_x"] = None
        values["body_y"] = None
        warnings.append("body 原图朝向证据无法形成唯一双轴指派，已留空等待人工输入。")
        return provenance

    mapping_specs: tuple[tuple[str, str, str | None], ...]
    relation_source_axes: dict[str, str] | None = None
    if family == "INLINE3":
        match = re.fullmatch(r"REL_INLINE3_(width|height)", combination)
        if match is None:
            reason = "IN-LINE-3 的唯一 LAND 解未携带可审计的行轴。"
            unresolved_reasons.append(reason)
            mapping_specs = ()
        else:
            separation_axis = match.group(1)
            cross_axis = "height" if separation_axis == "width" else "width"
            mapping_specs = (
                ("body_x", separation_axis, None),
                ("body_y", cross_axis, None),
            )
            provenance["mapping_basis"] = (
                f"{combination}: IN-LINE-3 source {separation_axis} -> footprint X; "
                f"source {cross_axis} -> footprint Y"
            )
    elif family == "GRID4":
        if combination != "REL_GRID4_TWO_AXES":
            reason = "GRID-4 的唯一 LAND 解未携带可审计的正交两轴。"
            unresolved_reasons.append(reason)
            mapping_specs = ()
        else:
            mapping_specs = (
                ("body_x", "width", None),
                ("body_y", "height", None),
            )
            provenance["mapping_basis"] = (
                f"{combination}: original_image:width -> footprint X; "
                "original_image:height -> footprint Y"
            )
    elif family == "DUAL":
        match = re.fullmatch(r"REL_DUAL_(width|height)_.+", combination)
        if match is None:
            reason = (
                "DUAL 的唯一 LAND 解未携带可审计的两排分离轴，"
                "无法证明 body 轴向映射。"
            )
            unresolved_reasons.append(reason)
            mapping_specs = ()
        else:
            separation_axis = match.group(1)
            cross_axis = "height" if separation_axis == "width" else "width"
            mapping_specs = (
                ("body_x", separation_axis, None),
                ("body_y", cross_axis, None),
            )
            provenance["mapping_basis"] = (
                f"{combination}: DUAL source {separation_axis} -> footprint X; "
                f"source {cross_axis} -> footprint Y"
            )
    elif family == "QUAD_EP":
        two_row_match = re.fullmatch(
            r"REL_QUAD_EP_(width|height)_TWO_ROW", combination
        )
        if combination == "REL_QUAD_EP_TWO_AXES":
            mapping_specs = (
                ("body_x", "width", None),
                ("body_y", "height", None),
            )
            provenance["mapping_basis"] = (
                f"{combination}: four-sided QUAD/EP original_image:width -> "
                "footprint X; original_image:height -> footprint Y"
            )
        elif two_row_match is not None:
            separation_axis = two_row_match.group(1)
            cross_axis = "height" if separation_axis == "width" else "width"
            mapping_specs = (
                ("body_x", separation_axis, None),
                ("body_y", cross_axis, None),
            )
            provenance["mapping_basis"] = (
                f"{combination}: two-row QUAD/EP source {separation_axis} -> "
                f"footprint X; source {cross_axis} -> footprint Y"
            )
        else:
            reason = "QUAD/EP 的唯一 LAND 解未携带可审计的排间图轴。"
            unresolved_reasons.append(reason)
            mapping_specs = ()
    elif family == "ASYM3":
        match = re.fullmatch(r"REL_ASYM3_(width|height)_.+", combination)
        if match is None:
            reason = (
                "ASYM3 的唯一 LAND 解未携带可审计的分离轴，"
                "无法证明 body 轴向映射。"
            )
            unresolved_reasons.append(reason)
            mapping_specs = ()
        else:
            separation_axis = match.group(1)
            cross_axis = "height" if separation_axis == "width" else "width"
            mapping_specs = (
                ("body_x", separation_axis, None),
                ("body_y", cross_axis, None),
            )
            provenance["mapping_basis"] = (
                f"{combination}: source {separation_axis} -> footprint X; "
                f"source {cross_axis} -> footprint Y"
            )
    else:
        mapping_specs = (
            ("body_x", "role:body_length", "body_length"),
            ("body_y", "role:body_width", "body_width"),
        )
        relation_match = re.fullmatch(
            r"REL_(?:DIRECT_CENTER|GAP_PLUS_PAD|OVERALL_GAP)_(width|height)",
            combination,
        )
        if family in {"CHIP", "SMX"} and relation_match is not None:
            separation_axis = relation_match.group(1)
            cross_axis = "height" if separation_axis == "width" else "width"
            relation_source_axes = {
                "body_x": separation_axis,
                "body_y": cross_axis,
            }
            provenance["mapping_basis"] = (
                f"{combination}: LAND 关系式已证明 original_image:{separation_axis} "
                f"-> footprint X; original_image:{cross_axis} -> footprint Y；"
                f"{family} body_length/body_width 沿用既有族契约"
            )
        elif family == "SOT3" and combination == "REL_SOT3_TWO_AXES":
            relation_source_axes = {
                "body_x": "width",
                "body_y": "height",
            }
            provenance["mapping_basis"] = (
                f"{combination}: SOT3 焊盘尺寸使用局部长宽轴；本体仍按族契约 "
                "original_image:width -> footprint X、original_image:height "
                "-> footprint Y"
            )
        elif combination.startswith("REL_"):
            reason = (
                f"{combination} 未提供可审计的图轴到 footprint 轴映射，"
                "无法证明 body 轴向。"
            )
            unresolved_reasons.append(reason)
            mapping_specs = ()
        else:
            provenance["mapping_basis"] = (
                f"{combination}: {family} body_length/body_width 与既有 pad/tab "
                "逻辑轴契约分别映射到 footprint X/Y"
            )

    if not mapping_specs:
        reason = unresolved_reasons[-1]
        for field_name in ("body_x", "body_y"):
            assignments[field_name] = {
                "status": "unresolved",
                "target_axis": (
                    "footprint_x" if field_name == "body_x" else "footprint_y"
                ),
                "source_axis": None,
                "mapping_chain": [],
                "basis": [reason],
            }

    for field_name, source_selector, required_role in mapping_specs:
        target_axis = "footprint_x" if field_name == "body_x" else "footprint_y"
        if required_role is None:
            candidates = tuple(
                item
                for item in body_candidates
                if _endpoint_span_axis(item) == source_selector
            )
        else:
            candidates = _deduplicate_dimension_candidates(
                tuple(
                    item
                    for item in body_candidates
                    if item.role == required_role
                )
            )

        if not candidates:
            reason = f"{field_name} 没有满足 {source_selector} 的本体原图候选。"
            unresolved_reasons.append(reason)
            assignments[field_name] = {
                "status": "unresolved",
                "target_axis": target_axis,
                "source_axis": None,
                "mapping_chain": [],
                "basis": [reason],
            }
            continue

        item, selection_reason = _select_body_outer_max(candidates)
        if item is None:
            reason = (
                f"{field_name} 的 {len(candidates)} 个本体候选来自不同边界，"
                "存在一个以上不可排除的指派。"
            )
            unresolved_reasons.append(reason)
            assignments[field_name] = {
                "status": "unresolved",
                "target_axis": target_axis,
                "source_axis": None,
                "candidate_count": len(candidates),
                "mapping_chain": [],
                "basis": [reason],
            }
            continue

        endpoint_axis = _endpoint_span_axis(item)
        relation_axis = (
            relation_source_axes.get(field_name)
            if relation_source_axes is not None
            else None
        )
        if (
            relation_axis is not None
            and endpoint_axis is not None
            and endpoint_axis != relation_axis
        ):
            reason = (
                f"{field_name} 的本体端点轴 {endpoint_axis} 与 LAND 关系式要求的"
                f"原图轴 {relation_axis} 冲突；禁止按数值反推或二选一。"
            )
            unresolved_reasons.append(reason)
            assignments[field_name] = {
                "status": "unresolved_relation_axis_conflict",
                "target_axis": target_axis,
                "source_axis": endpoint_axis,
                "expected_source_axis": relation_axis,
                "source_role": item.role,
                "source_endpoints": item.endpoints,
                "source_raw": item.raw,
                "mapping_chain": [],
                "basis": [reason, str(provenance["mapping_basis"])],
            }
            continue
        source_axis = endpoint_axis or relation_axis
        if source_axis is None:
            reason = (
                f"{field_name} 候选 {item.raw} 的端点不能唯一证明原图轴向。"
            )
            unresolved_reasons.append(reason)
            assignments[field_name] = {
                "status": "unresolved",
                "target_axis": target_axis,
                "source_axis": None,
                "source_role": item.role,
                "source_endpoints": item.endpoints,
                "source_raw": item.raw,
                "mapping_chain": [],
                "basis": [reason],
            }
            continue

        value = dimension_value_mm(item)
        role_step = item.role if required_role is not None else f"endpoint:{source_axis}"
        axis_basis = (
            f"端点解析为 {source_axis} 轴：{item.endpoints}"
            if endpoint_axis is not None
            else f"{combination} 关系式解已证明 original_image:{source_axis} "
            f"-> {target_axis}"
        )
        assignments[field_name] = {
            "status": "proven",
            "value_mm": value,
            "target_axis": target_axis,
            "source_axis": source_axis,
            "source_role": item.role,
            "source_role_original": getattr(item, "role_original", ""),
            "source_symbol": item.symbol,
            "source_raw": item.raw,
            "source_endpoints": item.endpoints,
            "candidate_count": len(candidates),
            "mapping_chain": [
                f"original_image:{source_axis}",
                role_step,
                f"land_solution:{combination}",
                target_axis,
            ],
            "basis": [
                axis_basis,
                f"候选裁定：{selection_reason or '唯一直接标注'}",
                str(provenance["mapping_basis"]),
            ],
        }

    complete = (
        set(assignments) == {"body_x", "body_y"}
        and all(record.get("status") == "proven" for record in assignments.values())
    )
    if complete:
        for field_name in ("body_x", "body_y"):
            record = assignments[field_name]
            values[field_name] = float(record["value_mm"])
            sources[field_name] = (
                "body 轴向来源可证："
                f"{record['source_raw']} ({record['source_axis']} -> "
                f"{record['target_axis']})"
            )
        provenance["status"] = "proven"
        return provenance

    for record in assignments.values():
        if record.get("status") == "proven":
            record["status"] = "withheld_pair_incomplete"
    values["body_x"] = None
    values["body_y"] = None
    warnings.append("body 轴向来源无法形成唯一双轴指派，已留空等待人工输入。")
    return provenance


def _invalidate_body_axis_provenance(
    provenance: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    updated = dict(provenance)
    updated["status"] = "absent_requires_user_input"
    updated["unresolved_reasons"] = [
        *list(provenance.get("unresolved_reasons", [])),
        reason,
    ]
    assignments = {
        key: dict(value)
        for key, value in dict(provenance.get("assignments", {})).items()
    }
    for record in assignments.values():
        if record.get("status") == "proven":
            record["status"] = "rejected_by_existing_guard"
            record.setdefault("basis", []).append(reason)
    updated["assignments"] = assignments
    return updated


def _evaluate_asym3_axis_consistency(
    family: str,
    values: dict[str, float | None],
) -> dict[str, Any]:
    """Reject a selected ASYM3 assignment whose tab cannot cover both small pads."""
    record: dict[str, Any] = {
        "schema": "e1j16_asym3_axis_consistency_v1",
        "family": family,
        "status": "not_applicable",
        "rule": "tab_y >= pitch_y + pad_y",
        "reject_only": True,
        "inputs_mm": {
            "tab_spacing_axis": values.get("tab_y"),
            "pitch": values.get("pitch_y"),
            "pad_spacing_axis": values.get("pad_y"),
        },
        "required_span_mm": None,
        "passed": None,
    }
    if family != "ASYM3":
        return record

    tab_value = values.get("tab_y")
    pitch_value = values.get("pitch_y")
    pad_value = values.get("pad_y")
    if tab_value is None or pitch_value is None or pad_value is None:
        record["status"] = "insufficient_evidence"
        return record

    required_span = float(pitch_value) + float(pad_value)
    passed = float(tab_value) + 1e-9 >= required_span
    record["required_span_mm"] = required_span
    record["passed"] = passed
    record["status"] = "passed" if passed else "rejected"
    return record


def _endpoint_pad_kind(item: Dimension) -> str | None:
    key = _endpoint_key(item)
    tab = any(
        marker in key
        for marker in (
            "大焊盘",
            "散热焊盘",
            "散热片",
            "thermalpad",
            "heatsinkpad",
            "exposedpad",
            "largepad",
            "tab",
        )
    )
    small = any(
        marker in key
        for marker in ("小焊盘", "端子焊盘", "terminalpad", "smallpad")
    )
    if tab and not small:
        return "tab"
    if small and not tab:
        return "small"
    return None


def _effective_land_role(item: Dimension) -> str:
    """Resolve a land role from explicit role plus physical endpoint semantics."""
    if (
        item.role_original in _FAMILY_ROLE_NORMALIZATION_MAP
        and item.role == _FAMILY_ROLE_NORMALIZATION_MAP[item.role_original]
    ):
        return item.role
    role = {
        "gap_between_pads": "pad_gap",
        "half_pitch_from_centerline": "half_pitch",
    }.get(item.role, item.role)
    if role in {
        "pad_gap",
        "overall_span",
        "half_pitch",
        "center_to_edge",
        "pitch",
    }:
        return role
    kind = _endpoint_pad_kind(item)
    axis = _endpoint_span_axis(item)
    if kind == "tab" and axis in {"width", "height"}:
        return "tab_width" if axis == "width" else "tab_height"
    if kind == "small" and axis in {"width", "height"}:
        return "pad_width" if axis == "width" else "pad_height"

    key = _endpoint_key(item)
    center_marked = "中心线" in key or "centerline" in key or "centreline" in key
    if kind == "small" and center_marked:
        if any(
            marker in key
            for marker in (
                "图形中央",
                "图形中心",
                "封装中央",
                "封装中心",
                "layoutcenter",
                "packagecenter",
            )
        ):
            return "half_pitch"
        if any(
            marker in key
            for marker in (
                "左上小焊盘",
                "右上小焊盘",
                "左下小焊盘",
                "右下小焊盘",
                "uppersmallpad",
                "lowersmallpad",
                "leftsmallpad",
                "rightsmallpad",
            )
        ):
            return "pitch"
    return role


def _effective_role_items(
    dimensions: tuple[Dimension, ...], role: str
) -> tuple[Dimension, ...]:
    candidates = tuple(
        item
        for item in dimensions
        if not item.is_derived and _effective_land_role(item) == role
    )
    return _deduplicate_dimension_candidates(candidates)


def _two_pad_gap_center_expectations(
    dimensions: tuple[Dimension, ...],
) -> tuple[tuple[float, Dimension, Dimension], ...]:
    """Return direct gap + same-axis pad-size center-distance relationships."""
    expectations: list[tuple[float, Dimension, Dimension]] = []
    for gap_item in _effective_role_items(dimensions, "pad_gap"):
        axis = _endpoint_span_axis(gap_item)
        size_role = {"width": "pad_width", "height": "pad_height"}.get(axis)
        if size_role is None:
            continue
        size_items = tuple(
            item
            for item in _effective_role_items(dimensions, size_role)
            if _endpoint_span_axis(item) in {None, axis}
        )
        if len(size_items) != 1:
            continue
        size_item = size_items[0]
        expected = dimension_value_mm(gap_item) + dimension_value_mm(size_item)
        if not any(
            math.isclose(expected, existing[0], rel_tol=0.0, abs_tol=1e-9)
            for existing in expectations
        ):
            expectations.append((expected, gap_item, size_item))
    return tuple(expectations)


def _validate_two_pad_gap_center_consistency(
    family: str,
    center_distance: float,
    dimensions: tuple[Dimension, ...],
) -> None:
    if family not in {"CHIP", "SMX"}:
        return
    expectations = _two_pad_gap_center_expectations(dimensions)
    if not expectations:
        return
    if any(
        math.isclose(center_distance, expected, rel_tol=0.02, abs_tol=1e-9)
        for expected, _gap, _size in expectations
    ):
        return
    visible = ", ".join(
        f"{gap.raw}+{size.raw}={expected:.4g} mm"
        for expected, gap, size in expectations
    )
    raise E1aError(
        "中心距自洽闸失败：同轴 gap + pad_size 应为 "
        f"{visible}（相对容差 2%），实际 center_x={center_distance:.4g} mm。"
    )


def _full_outline_pitch_values(transcription: Transcription) -> tuple[float, ...]:
    values: list[float] = []
    for item in transcription.dimensions:
        if item.belongs_to != "package_outline" or item.is_derived:
            continue
        if item.role not in {
            "pitch",
            "half_pitch",
            "half_pitch_from_centerline",
            "pad_center_distance",
        }:
            continue
        value = dimension_value_mm(item)
        key = _endpoint_key(item)
        endpoint_is_half = any(
            marker in key
            for marker in (
                "中间端子",
                "中央端子",
                "中心端子",
                "middleterminal",
                "centerterminal",
                "centreterminal",
            )
        )
        if item.role in {"half_pitch", "half_pitch_from_centerline"} or endpoint_is_half:
            value *= 2.0
        if not any(math.isclose(value, seen, rel_tol=0.0, abs_tol=1e-9) for seen in values):
            values.append(value)
    return tuple(values)


def _within_outline_pitch_range(value: float, references: tuple[float, ...]) -> bool:
    if not references:
        return False
    if len(references) >= 2:
        return min(references) <= value <= max(references)
    reference = references[0]
    return abs(value - reference) / reference <= 0.15


def _sot3_center_roles(
    dimensions: tuple[Dimension, ...],
) -> tuple[Dimension | None, Dimension | None]:
    """Return generator center_x and pitch_y candidates for a SOT3 layout."""
    if not dimensions:
        return None, None
    dimensions = _deduplicate_dimension_candidates(dimensions)
    if len(dimensions) >= 3:
        values = [dimension_value_mm(item) for item in dimensions]
        half_indexes = {
            low_index
            for low_index, low in enumerate(values)
            for high_index, high in enumerate(values)
            if low_index != high_index
            and math.isclose(high, low * 2.0, rel_tol=0.0, abs_tol=1e-9)
        }
        remaining = tuple(
            item for index, item in enumerate(dimensions) if index not in half_indexes
        )
        if len(remaining) >= 2:
            dimensions = remaining
    row_markers = (
        "上排",
        "下排",
        "整排",
        "共同中心",
        "两个小焊盘",
        "row",
        "commoncenter",
        "pairofpads",
    )
    pair_markers = (
        "左上焊盘",
        "右上焊盘",
        "左下焊盘",
        "右下焊盘",
        "lowerleftpad",
        "lowerrightpad",
        "upperleftpad",
        "upperrightpad",
        "twopads",
        "pad1",
        "pad2",
    )

    def endpoint_key(item: Dimension) -> str:
        normalized = unicodedata.normalize("NFKC", item.endpoints or "").lower()
        return re.sub(r"[\s_\-:/()→]+", "", normalized)

    def row_relation(item: Dimension) -> bool:
        key = endpoint_key(item)
        upper = any(
            marker in key
            for marker in ("上方", "上排", "左上", "右上", "upper", "top")
        )
        lower = any(
            marker in key
            for marker in ("下方", "下排", "左下", "右下", "lower", "bottom")
        )
        return (upper and lower) or any(marker in key for marker in row_markers)

    def pair_relation(item: Dimension) -> bool:
        key = endpoint_key(item)
        left = any(marker in key for marker in ("左下", "左上", "left"))
        right = any(marker in key for marker in ("右下", "右上", "right"))
        same_lower_row = left and right and any(
            marker in key for marker in ("左下", "右下", "lower", "bottom")
        )
        same_upper_row = left and right and any(
            marker in key for marker in ("左上", "右上", "upper", "top")
        )
        return same_lower_row or same_upper_row or any(
            marker in key for marker in pair_markers
        )

    row = tuple(item for item in dimensions if row_relation(item))
    pair = tuple(item for item in dimensions if item not in row and pair_relation(item))
    if len(row) == 1 and len(pair) == 1:
        return row[0], pair[0]
    if len(dimensions) == 1:
        return dimensions[0], None
    return None, None


def _derived_role_items(transcription: Transcription) -> tuple[Dimension, ...]:
    return tuple(item for item in transcription.dimensions if item.is_derived)


def _half_value_filter(
    dimensions: tuple[Dimension, ...],
    *,
    minimum_remaining: int,
) -> tuple[tuple[Dimension, ...], tuple[Dimension, ...], bool]:
    numeric = [dimension_value_mm(item) for item in dimensions]
    excluded_indexes: set[int] = set()
    for left in range(len(numeric)):
        for right in range(left + 1, len(numeric)):
            left_item = dimensions[left]
            right_item = dimensions[right]
            legacy_pair = all(
                item.endpoints.startswith("legacy fixture:")
                for item in (left_item, right_item)
            )
            if not legacy_pair and left_item.belongs_to != right_item.belongs_to:
                continue
            low_index, high_index = (
                (left, right) if numeric[left] < numeric[right] else (right, left)
            )
            low_item = dimensions[low_index]
            high_item = dimensions[high_index]
            if not legacy_pair and not (
                low_item.role in {"pad_width", "pad_height"}
                and high_item.role in {"pad_center_distance", "pitch"}
            ):
                continue
            if math.isclose(
                numeric[high_index],
                numeric[low_index] * 2.0,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                excluded_indexes.add(low_index)
    if not excluded_indexes:
        return dimensions, (), False
    remaining = tuple(
        item for index, item in enumerate(dimensions) if index not in excluded_indexes
    )
    excluded = tuple(
        item for index, item in enumerate(dimensions) if index in excluded_indexes
    )
    if len(remaining) < minimum_remaining:
        return dimensions, excluded, True
    return remaining, excluded, False


def _solution_key(solution: dict[str, float]) -> tuple[tuple[str, float], ...]:
    return tuple(sorted((key, round(value, 6)) for key, value in solution.items()))


def _relation_axis(item: Dimension) -> str | None:
    """Return a physical width/height axis using endpoints only."""
    span_axis = _endpoint_span_axis(item)
    if span_axis is not None:
        return span_axis
    broad_axis = {
        "horizontal": "width",
        "vertical": "height",
    }.get(_endpoints_axis(item.endpoints))
    if broad_axis is not None:
        return broad_axis
    parts = _relation_endpoint_parts(item)
    if parts is not None:
        start_entities = _relation_endpoint_entities(parts[0])
        end_entities = _relation_endpoint_entities(parts[1])
        if (
            ("left" in start_entities and "right" in end_entities)
            or ("right" in start_entities and "left" in end_entities)
        ):
            return "width"
        if (
            ("upper" in start_entities and "lower" in end_entities)
            or ("lower" in start_entities and "upper" in end_entities)
        ):
            return "height"
    key = _endpoint_key(item)
    horizontal = any(
        marker in key
        for marker in ("左侧", "右侧", "左端", "右端", "left", "right")
    )
    vertical = any(
        marker in key
        for marker in ("上侧", "下侧", "上端", "下端", "top", "bottom")
    )
    if horizontal and not vertical:
        return "width"
    if vertical and not horizontal:
        return "height"
    return None


def _relation_item_label(index: int, item: Dimension) -> str:
    return (
        f"#{index + 1} {item.value} {item.unit}; role={_effective_land_role(item)}; "
        f"belongs_to={item.belongs_to}; endpoints={item.endpoints or '未标端点'}"
    )


def _relation_center_marked(item: Dimension) -> bool:
    key = _endpoint_key(item)
    return (
        key.count("中心线") >= 2
        or key.count("中心点") >= 2
        or key.count("centerline") >= 2
        or key.count("centreline") >= 2
        or key.count("centerpoint") >= 2
        or key.count("centrepoint") >= 2
    )


def _relation_endpoint_parts(item: Dimension) -> tuple[str, str] | None:
    normalized = unicodedata.normalize("NFKC", item.endpoints or "").lower()
    parts = re.split(r"\s*(?:->|→|至|到)\s*", normalized, maxsplit=1)
    if len(parts) != 2 or not all(part.strip() for part in parts):
        return None
    return parts[0].strip(), parts[1].strip()


def _relation_endpoint_entities(text: str) -> frozenset[str]:
    compact = re.sub(r"\s+", "", text)
    entities: set[str] = set()
    if "大焊盘" in compact or "tab" in compact or "largepad" in compact:
        entities.add("tab")
    if "小焊盘" in compact or "smallpad" in compact or "terminalpad" in compact:
        entities.add("small")
    if re.search(r"左(?:侧)?[^>]{0,12}焊盘", compact) or "leftpad" in compact:
        entities.add("left")
    if re.search(r"右(?:侧)?[^>]{0,12}焊盘", compact) or "rightpad" in compact:
        entities.add("right")
    if re.search(r"上(?:方|侧)?[^>]{0,12}焊盘", compact) or "upperpad" in compact:
        entities.add("upper")
    if re.search(r"下(?:方|侧)?[^>]{0,12}焊盘", compact) or "lowerpad" in compact:
        entities.add("lower")
    return frozenset(entities)


def _relation_distinct_endpoint_entities(item: Dimension) -> tuple[str, str] | None:
    parts = _relation_endpoint_parts(item)
    if parts is None:
        return None
    left_entities = _relation_endpoint_entities(parts[0])
    right_entities = _relation_endpoint_entities(parts[1])
    if not left_entities or not right_entities or left_entities == right_entities:
        return None
    return parts


def _relation_overall_marked(item: Dimension) -> bool:
    parts = _relation_distinct_endpoint_entities(item)
    if parts is None:
        return False
    start, end = parts
    horizontal = (
        any(marker in start for marker in ("左边缘", "左外缘", "左缘", "leftedge"))
        and any(marker in end for marker in ("右边缘", "右外缘", "右缘", "rightedge"))
    )
    vertical = (
        any(marker in start for marker in ("上边缘", "上外缘", "上缘", "topedge"))
        and any(marker in end for marker in ("下边缘", "下外缘", "下缘", "bottomedge"))
    )
    return horizontal or vertical


def _relation_gap_marked(item: Dimension) -> bool:
    parts = _relation_distinct_endpoint_entities(item)
    if parts is None:
        return False
    start, end = parts
    horizontal = (
        any(marker in start for marker in ("右边缘", "右内缘", "右缘", "rightedge"))
        and any(marker in end for marker in ("左边缘", "左内缘", "左缘", "leftedge"))
    )
    vertical = (
        any(marker in start for marker in ("下边缘", "下内缘", "下缘", "bottomedge"))
        and any(marker in end for marker in ("上边缘", "上内缘", "上缘", "topedge"))
    )
    return horizontal or vertical


def _relation_role_score(item: Dimension, expected: str) -> int:
    role = _effective_land_role(item)
    aliases = {
        "pad_width": {"pad_width"},
        "pad_height": {"pad_height"},
        "center": {"pad_center_distance", "pitch"},
        "gap": {"pad_gap", "gap_between_pads"},
        "overall": {"overall_span"},
        "half_pitch": {"half_pitch", "half_pitch_from_centerline"},
        "pitch": {"pitch", "pad_center_distance"},
        "tab_width": {"tab_width"},
        "tab_height": {"tab_height"},
    }
    return 2 if role in aliases.get(expected, set()) else 0


def _relation_pitch_matches(value: float, references: tuple[float, ...]) -> bool:
    if not references:
        return False
    unique = sorted(
        {
            round(reference, 6)
            for reference in references
            if math.isfinite(reference) and reference > 0
        }
    )
    if len(unique) == 1:
        return abs(value - unique[0]) / unique[0] <= 0.15
    low, high = unique[0], unique[-1]
    if abs(high - 2.0 * low) / high <= 0.03:
        return abs(value - high) / high <= 0.15
    return low - 0.02 <= value <= high + 0.02


def _relation_first_solution(
    transcription: Transcription,
    family: str,
    active_land: tuple[Dimension, ...],
    page_text: str | None = None,
) -> tuple[
    tuple[
        str,
        dict[str, float],
        dict[str, str],
        dict[str, tuple[Dimension, ...]],
    ]
    | None,
    tuple[dict[str, Any], ...],
]:
    """Enumerate endpoint-consistent numeric relations before consulting roles."""
    indexed: list[tuple[int, Dimension, float, str]] = []
    for index, item in enumerate(active_land):
        if _family_tolerance_only(item):
            continue
        if family == "GRID4":
            axis = _family_topology_axis(item)
        elif family == "DUAL":
            axis = _dual_row_relation_axis(item) or _relation_axis(item)
        else:
            axis = _relation_axis(item)
        if axis is None:
            continue
        indexed.append((index, item, dimension_value_mm(item), axis))

    relation_records: list[dict[str, Any]] = []
    candidates: list[
        tuple[
            str,
            dict[str, float],
            dict[str, str],
            dict[str, tuple[Dimension, ...]],
            int,
            int,
            tuple[str, ...],
        ]
    ] = []

    def record_rejection(formula: str, evidence: Iterable[str], reason: str) -> None:
        relation_records.append(
            {
                "formula": formula,
                "status": "rejected",
                "solution": None,
                "relation_score": 0,
                "role_score": 0,
                "evidence": list(evidence),
                "rejection_reasons": [reason],
                "selected": False,
            }
        )

    def consider(
        formula: str,
        solved: dict[str, float],
        solved_sources: dict[str, str],
        solved_operands: dict[str, tuple[Dimension, ...]],
        *,
        relation_score: int,
        role_score: int,
        evidence: Iterable[str],
    ) -> None:
        required = {"pad_x", "pad_y", "center_x"}
        if family in {"SOT3", "GRID4", "DUAL"}:
            required.add("pitch_y")
        elif family == "ASYM3":
            required.update({"pitch_y", "tab_x", "tab_y"})
        missing = sorted(required - set(solved))
        invalid = sorted(
            key
            for key, value in solved.items()
            if not math.isfinite(value) or value <= 0
        )
        if missing or invalid:
            reasons = []
            if missing:
                reasons.append("缺字段：" + "、".join(missing))
            if invalid:
                reasons.append("非正有限值：" + "、".join(invalid))
            record_rejection(formula, evidence, "；".join(reasons))
            return
        candidate = (
            formula,
            solved,
            solved_sources,
            solved_operands,
            relation_score,
            role_score,
            tuple(evidence),
        )
        candidates.append(candidate)
        relation_records.append(
            {
                "formula": formula,
                "status": "candidate",
                "solution": {key: round(value, 6) for key, value in solved.items()},
                "field_operands": {
                    key: [asdict(item) for item in solved_operands.get(key, ())]
                    for key in solved
                },
                "relation_score": relation_score,
                "role_score": role_score,
                "evidence": list(evidence),
                "rejection_reasons": [],
                "selected": False,
            }
        )

    axes = {
        axis: [item for item in indexed if item[3] == axis]
        for axis in ("width", "height")
    }
    if not axes["width"] or not axes["height"]:
        record_rejection(
            "axis_inventory",
            (_relation_item_label(index, item) for index, item, _value, _axis in indexed),
            "端点无法形成 width/height 两轴候选。",
        )
    elif family == "INLINE3":
        topology_family, topology_reason, _count, _summary = _family_new_topology_evidence(
            transcription, active_land
        )
        if topology_family != "INLINE3":
            record_rejection("REL_INLINE3", (), topology_reason)
        else:
            center_axes = {
                axis: tuple(
                    item
                    for item in axes[axis]
                    if item[1].role in {"pad_center_distance", "pitch"}
                    and _relation_center_marked(item[1])
                )
                for axis in ("width", "height")
            }
            separation_axis = next(axis for axis, axis_items in center_axes.items() if axis_items)
            cross_axis = "height" if separation_axis == "width" else "width"
            pitch_value, pitch_items = _family_unique_axis_value(
                item[1] for item in center_axes[separation_axis]
            )
            pad_sep_value, pad_sep_items = _family_unique_axis_value(
                item[1]
                for item in axes[separation_axis]
                if item[1].role in {"pad_width", "pad_height"}
            )
            pad_cross_value, pad_cross_items = _family_unique_axis_value(
                item[1]
                for item in axes[cross_axis]
                if item[1].role in {"pad_width", "pad_height"}
            )
            evidence_items = pitch_items + pad_sep_items + pad_cross_items
            evidence = tuple(
                _relation_item_label(active_land.index(item), item) for item in evidence_items
            )
            if None in {pitch_value, pad_sep_value, pad_cross_value}:
                record_rejection("REL_INLINE3", evidence, "三焊盘等距链尺寸无法形成唯一值。")
            else:
                consider(
                    f"REL_INLINE3_{separation_axis}",
                    {
                        "pad_x": float(pad_sep_value),
                        "pad_y": float(pad_cross_value),
                        "center_x": float(pitch_value),
                    },
                    {
                        "pad_x": "关系式优先：IN-LINE-3 行轴单焊盘尺寸",
                        "pad_y": "关系式优先：IN-LINE-3 交叉轴单焊盘尺寸",
                        "center_x": "关系式优先：left-middle-right 相邻中心距",
                    },
                    {
                        "pad_x": tuple(pad_sep_items),
                        "pad_y": tuple(pad_cross_items),
                        "center_x": tuple(pitch_items),
                    },
                    relation_score=8,
                    role_score=sum(
                        _relation_role_score(item, "center")
                        if item.role in {"pad_center_distance", "pitch"}
                        else _relation_role_score(
                            item,
                            "pad_width" if _relation_axis(item) == "width" else "pad_height",
                        )
                        for item in evidence_items
                    ),
                    evidence=evidence,
                )

    elif family == "GRID4":
        topology_family, topology_reason, _count, _summary = _family_new_topology_evidence(
            transcription, active_land
        )
        if topology_family != "GRID4":
            record_rejection("REL_GRID4_TWO_AXES", (), topology_reason)
        else:
            shape_values: dict[str, float] = {}
            center_values: dict[str, float] = {}
            shape_items_by_axis: dict[str, tuple[Dimension, ...]] = {}
            center_items_by_axis: dict[str, tuple[Dimension, ...]] = {}
            evidence_items: list[Dimension] = []
            for axis in ("width", "height"):
                shape_value, shape_items = _family_unique_axis_value(
                    item[1]
                    for item in axes[axis]
                    if item[1].role in {"pad_width", "pad_height"}
                )
                center_value, center_items = _family_unique_axis_value(
                    item[1]
                    for item in axes[axis]
                    if item[1].role in {"pad_center_distance", "pitch"}
                    and _relation_center_marked(item[1])
                )
                if shape_value is not None:
                    shape_values[axis] = shape_value
                    shape_items_by_axis[axis] = tuple(shape_items)
                if center_value is not None:
                    center_values[axis] = center_value
                    center_items_by_axis[axis] = tuple(center_items)
                evidence_items.extend(shape_items)
                evidence_items.extend(center_items)
            evidence = tuple(
                _relation_item_label(active_land.index(item), item) for item in evidence_items
            )
            if set(shape_values) != {"width", "height"} or set(center_values) != {
                "width",
                "height",
            }:
                record_rejection(
                    "REL_GRID4_TWO_AXES", evidence, "2×2 栅格两轴尺寸无法形成唯一值。"
                )
            else:
                consider(
                    "REL_GRID4_TWO_AXES",
                    {
                        "pad_x": shape_values["width"],
                        "pad_y": shape_values["height"],
                        "center_x": center_values["width"],
                        "pitch_y": center_values["height"],
                    },
                    {
                        "pad_x": "关系式优先：GRID-4 水平单焊盘尺寸",
                        "pad_y": "关系式优先：GRID-4 垂直单焊盘尺寸",
                        "center_x": "关系式优先：GRID-4 左右列中心距",
                        "pitch_y": "关系式优先：GRID-4 上下排中心距",
                    },
                    {
                        "pad_x": shape_items_by_axis["width"],
                        "pad_y": shape_items_by_axis["height"],
                        "center_x": center_items_by_axis["width"],
                        "pitch_y": center_items_by_axis["height"],
                    },
                    relation_score=8,
                    role_score=sum(
                        _relation_role_score(item, "center")
                        if item.role in {"pad_center_distance", "pitch"}
                        else _relation_role_score(
                            item,
                            "pad_width" if _relation_axis(item) == "width" else "pad_height",
                        )
                        for item in evidence_items
                    ),
                    evidence=evidence,
                )

    elif family == "DUAL":
        dual = _dual_topology_evidence(transcription, active_land, page_text)
        if dual.get("status") not in {"auto", "multi_family_conflict"}:
            record_rejection("REL_DUAL", (), str(dual.get("reason") or "DUAL 拓扑闸未通过。"))
        else:
            separation_axis = str(dual["separation_axis"])
            cross_axis = "height" if separation_axis == "width" else "width"
            pad_sep_value = float(dual["shape_values"][separation_axis])
            pad_cross_value = float(dual["shape_values"][cross_axis])
            pitch_value = float(dual["pitch_y"])
            pad_sep_items = tuple(
                item[1]
                for item in axes[separation_axis]
                if item[1].role in {"pad_width", "pad_height"}
            )
            pad_cross_items = tuple(
                item[1]
                for item in axes[cross_axis]
                if item[1].role in {"pad_width", "pad_height"}
            )
            pitch_items = tuple(
                item
                for item in active_land
                if item.role in {"pitch", "pad_center_distance"}
                and _family_topology_axis(item) in {None, cross_axis}
                and _dual_row_relation_axis(item) is None
            )
            row_axis_items = tuple(
                item
                for item in indexed
                if _dual_row_relation_axis(item[1]) == separation_axis
            )

            def unique_row_value(
                predicate: Any,
            ) -> tuple[float | None, tuple[Dimension, ...]]:
                selected = tuple(item[1] for item in row_axis_items if predicate(item[1]))
                return _family_unique_axis_value(selected)

            direct_value, direct_items = unique_row_value(
                lambda item: item.role in {"pad_center_distance", "pitch"}
                and _relation_center_marked(item)
            )
            gap_value, gap_items = unique_row_value(
                lambda item: item.role in {"pad_gap", "gap_between_pads"}
                or _relation_gap_marked(item)
            )
            overall_value, overall_items = unique_row_value(
                lambda item: item.role == "overall_span" or _relation_overall_marked(item)
            )
            evidence_items = (
                pad_sep_items
                + pad_cross_items
                + pitch_items
                + direct_items
                + gap_items
                + overall_items
            )
            evidence = tuple(
                _relation_item_label(active_land.index(item), item)
                for item in dict.fromkeys(evidence_items)
            )
            center_value: float | None = None
            center_formula = ""
            center_sources: tuple[Dimension, ...] = ()
            relation_score = 0
            if gap_value is not None and overall_value is not None:
                from_inner = gap_value + pad_sep_value
                from_outer = overall_value - pad_sep_value
                if abs(from_inner - from_outer) <= 0.05:
                    center_value = (gap_value + overall_value) / 2.0
                    center_formula = "CHAIN_INNER_OUTER"
                    center_sources = gap_items + overall_items + pad_sep_items
                    relation_score = 10
                else:
                    record_rejection(
                        f"REL_DUAL_{separation_axis}_CHAIN_INNER_OUTER",
                        evidence,
                        (
                            "DUAL 内外缘闭合失败："
                            f"inner+pad={from_inner:.4g}，outer-pad={from_outer:.4g}，"
                            "差值超过 0.05 mm；不挑不猜。"
                        ),
                    )
            elif direct_value is not None:
                center_value = direct_value
                center_formula = "DIRECT_CENTER"
                center_sources = direct_items
                relation_score = 9
            elif gap_value is not None:
                center_value = gap_value + pad_sep_value
                center_formula = "INNER_PLUS_PAD"
                center_sources = gap_items + pad_sep_items
                relation_score = 8
            elif overall_value is not None and overall_value > pad_sep_value:
                center_value = overall_value - pad_sep_value
                center_formula = "OUTER_MINUS_PAD"
                center_sources = overall_items + pad_sep_items
                relation_score = 7
            else:
                record_rejection(
                    f"REL_DUAL_{separation_axis}",
                    evidence,
                    "DUAL 排中心距缺少中心直标、内缘或外缘证据。",
                )
            if center_value is not None:
                formula = f"REL_DUAL_{separation_axis}_{center_formula}"
                consider(
                    formula,
                    {
                        "pad_x": pad_sep_value,
                        "pad_y": pad_cross_value,
                        "center_x": center_value,
                        "pitch_y": pitch_value,
                    },
                    {
                        "pad_x": "关系式优先：DUAL 分离轴单焊盘尺寸",
                        "pad_y": "关系式优先：DUAL 行内轴单焊盘尺寸",
                        "center_x": f"关系式优先：DUAL {center_formula}",
                        "pitch_y": "关系式优先：DUAL 行内相邻中心距",
                    },
                    {
                        "pad_x": pad_sep_items,
                        "pad_y": pad_cross_items,
                        "center_x": center_sources,
                        "pitch_y": pitch_items,
                    },
                    relation_score=relation_score,
                    role_score=sum(
                        _relation_role_score(item, "center")
                        if item.role in {"pad_center_distance", "pitch"}
                        else _relation_role_score(
                            item,
                            "pad_width"
                            if _family_topology_axis(item) == "width"
                            else "pad_height",
                        )
                        for item in evidence_items
                    ),
                    evidence=evidence,
                )

    elif family in {"CHIP", "SMX"}:
        for separation_axis in ("width", "height"):
            cross_axis = "height" if separation_axis == "width" else "width"
            for center in axes[separation_axis]:
                if not _relation_center_marked(center[1]):
                    continue
                for pad_sep in axes[separation_axis]:
                    if center[0] == pad_sep[0] or center[2] <= pad_sep[2]:
                        continue
                    for pad_cross in axes[cross_axis]:
                        physical = {
                            separation_axis: pad_sep[2],
                            cross_axis: pad_cross[2],
                        }
                        solved = {
                            "pad_x": physical[separation_axis],
                            "pad_y": physical[cross_axis],
                            "center_x": center[2],
                        }
                        score = (
                            _relation_role_score(center[1], "center")
                            + _relation_role_score(
                                pad_sep[1],
                                "pad_width" if separation_axis == "width" else "pad_height",
                            )
                            + _relation_role_score(
                                pad_cross[1],
                                "pad_width" if cross_axis == "width" else "pad_height",
                            )
                        )
                        evidence = (
                            _relation_item_label(center[0], center[1]),
                            _relation_item_label(pad_sep[0], pad_sep[1]),
                            _relation_item_label(pad_cross[0], pad_cross[1]),
                        )
                        consider(
                            f"REL_DIRECT_CENTER_{separation_axis}",
                            solved,
                            {
                                "center_x": "关系式优先：端点中心距直取；" + evidence[0],
                                "pad_x": "关系式优先：分离轴焊盘尺寸；" + evidence[1],
                                "pad_y": "关系式优先：交叉轴焊盘尺寸；" + evidence[2],
                            },
                            {
                                "center_x": (center[1],),
                                "pad_x": (pad_sep[1],),
                                "pad_y": (pad_cross[1],),
                            },
                            relation_score=1,
                            role_score=score,
                            evidence=evidence,
                        )

            for gap in axes[separation_axis]:
                if not _relation_gap_marked(gap[1]):
                    continue
                for pad_sep in axes[separation_axis]:
                    if gap[0] == pad_sep[0]:
                        continue
                    center_value = gap[2] + pad_sep[2]
                    for pad_cross in axes[cross_axis]:
                        matching_overall = [
                            item
                            for item in axes[separation_axis]
                            if item[0] not in {gap[0], pad_sep[0]}
                            and _relation_overall_marked(item[1])
                            and math.isclose(
                                item[2],
                                gap[2] + 2.0 * pad_sep[2],
                                rel_tol=0.0,
                                abs_tol=0.02,
                            )
                        ]
                        physical = {
                            separation_axis: pad_sep[2],
                            cross_axis: pad_cross[2],
                        }
                        evidence = [
                            _relation_item_label(gap[0], gap[1]),
                            _relation_item_label(pad_sep[0], pad_sep[1]),
                            _relation_item_label(pad_cross[0], pad_cross[1]),
                        ]
                        relation_score = 2
                        role_score = (
                            _relation_role_score(gap[1], "gap")
                            + _relation_role_score(
                                pad_sep[1],
                                "pad_width" if separation_axis == "width" else "pad_height",
                            )
                            + _relation_role_score(
                                pad_cross[1],
                                "pad_width" if cross_axis == "width" else "pad_height",
                            )
                        )
                        if matching_overall:
                            relation_score = 4
                            best_overall = max(
                                matching_overall,
                                key=lambda item: _relation_role_score(item[1], "overall"),
                            )
                            role_score += _relation_role_score(best_overall[1], "overall")
                            evidence.append(_relation_item_label(best_overall[0], best_overall[1]))
                        consider(
                            f"REL_GAP_PLUS_PAD_{separation_axis}",
                            {
                                "pad_x": physical[separation_axis],
                                "pad_y": physical[cross_axis],
                                "center_x": center_value,
                            },
                            {
                                "center_x": "关系式优先：同轴 gap+pad_size；" + evidence[0],
                                "pad_x": "关系式优先：端点轴向尺寸",
                                "pad_y": "关系式优先：端点轴向尺寸",
                            },
                            {
                                "center_x": (gap[1], pad_sep[1]),
                                "pad_x": (pad_sep[1],),
                                "pad_y": (pad_cross[1],),
                            },
                            relation_score=relation_score,
                            role_score=role_score,
                            evidence=evidence,
                        )

            for overall in axes[separation_axis]:
                if not _relation_overall_marked(overall[1]):
                    continue
                for gap in axes[separation_axis]:
                    if (
                        overall[0] == gap[0]
                        or overall[2] <= gap[2]
                        or not _relation_gap_marked(gap[1])
                    ):
                        continue
                    pad_value = (overall[2] - gap[2]) / 2.0
                    center_value = gap[2] + pad_value
                    matching_pad = [
                        item
                        for item in axes[separation_axis]
                        if item[0] not in {overall[0], gap[0]}
                        and math.isclose(item[2], pad_value, rel_tol=0.0, abs_tol=0.02)
                    ]
                    for pad_cross in axes[cross_axis]:
                        physical = {
                            separation_axis: pad_value,
                            cross_axis: pad_cross[2],
                        }
                        evidence = [
                            _relation_item_label(overall[0], overall[1]),
                            _relation_item_label(gap[0], gap[1]),
                            _relation_item_label(pad_cross[0], pad_cross[1]),
                        ]
                        relation_score = 3
                        role_score = (
                            _relation_role_score(overall[1], "overall")
                            + _relation_role_score(gap[1], "gap")
                            + _relation_role_score(
                                pad_cross[1],
                                "pad_width" if cross_axis == "width" else "pad_height",
                            )
                        )
                        if matching_pad:
                            relation_score = 4
                            best_pad = max(
                                matching_pad,
                                key=lambda item: _relation_role_score(
                                    item[1],
                                    "pad_width"
                                    if separation_axis == "width"
                                    else "pad_height",
                                ),
                            )
                            role_score += _relation_role_score(
                                best_pad[1],
                                "pad_width" if separation_axis == "width" else "pad_height",
                            )
                            evidence.append(_relation_item_label(best_pad[0], best_pad[1]))
                        consider(
                            f"REL_OVERALL_GAP_{separation_axis}",
                            {
                                "pad_x": physical[separation_axis],
                                "pad_y": physical[cross_axis],
                                "center_x": center_value,
                            },
                            {
                                "center_x": "关系式优先：overall-(overall-gap)/2",
                                "pad_x": "关系式优先：分离轴 (overall-gap)/2",
                                "pad_y": "关系式优先：交叉轴端点尺寸",
                            },
                            {
                                "center_x": (overall[1], gap[1]),
                                "pad_x": (overall[1], gap[1]),
                                "pad_y": (pad_cross[1],),
                            },
                            relation_score=relation_score,
                            role_score=role_score,
                            evidence=evidence,
                        )

    elif family == "SOT3":
        for pad_width in axes["width"]:
            for pitch in axes["width"]:
                if (
                    pad_width[0] == pitch[0]
                    or pitch[2] <= pad_width[2]
                    or not _relation_center_marked(pitch[1])
                ):
                    continue
                for pad_height in axes["height"]:
                    for center in axes["height"]:
                        if (
                            pad_height[0] == center[0]
                            or center[2] <= pad_height[2]
                            or not _relation_center_marked(center[1])
                        ):
                            continue
                        evidence = (
                            _relation_item_label(pad_width[0], pad_width[1]),
                            _relation_item_label(pitch[0], pitch[1]),
                            _relation_item_label(pad_height[0], pad_height[1]),
                            _relation_item_label(center[0], center[1]),
                        )
                        consider(
                            "REL_SOT3_TWO_AXES",
                            {
                                "pad_x": pad_height[2],
                                "pad_y": pad_width[2],
                                "center_x": center[2],
                                "pitch_y": pitch[2],
                            },
                            {
                                "pad_x": "关系式优先：SOT3 竖轴焊盘尺寸映射生成器 X",
                                "pad_y": "关系式优先：SOT3 横轴焊盘尺寸映射生成器 Y",
                                "center_x": "关系式优先：SOT3 竖轴中心距",
                                "pitch_y": "关系式优先：SOT3 横轴双焊盘中心距",
                            },
                            {
                                "pad_x": (pad_height[1],),
                                "pad_y": (pad_width[1],),
                                "center_x": (center[1],),
                                "pitch_y": (pitch[1],),
                            },
                            relation_score=2,
                            role_score=(
                                _relation_role_score(pad_width[1], "pad_width")
                                + _relation_role_score(pad_height[1], "pad_height")
                                + _relation_role_score(pitch[1], "center")
                                + _relation_role_score(center[1], "center")
                            ),
                            evidence=evidence,
                        )

    elif family == "ASYM3":
        overall_items = list(indexed)
        next_index = len(active_land)
        for item in transcription.dimensions:
            if (
                item.belongs_to == "occupied_area"
                and not item.is_derived
                and _is_total_span_dimension(item)
            ):
                axis = _relation_axis(item)
                if axis is not None:
                    overall_items.append((next_index, item, dimension_value_mm(item), axis))
                    next_index += 1
        pitch_references = _full_outline_pitch_values(transcription)
        for overall in overall_items:
            if not _relation_overall_marked(overall[1]):
                continue
            separation_axis = overall[3]
            cross_axis = "height" if separation_axis == "width" else "width"
            same_axis = [item for item in axes[separation_axis] if item[0] != overall[0]]
            for first, second in combinations(same_axis, 2):
                pad_sep, tab_sep = sorted((first, second), key=lambda item: item[2])
                if not overall[2] > tab_sep[2] > pad_sep[2]:
                    continue
                center_value = overall[2] - (pad_sep[2] + tab_sep[2]) / 2.0
                if center_value <= 0:
                    continue
                for cross_first, cross_second in combinations(axes[cross_axis], 2):
                    pad_cross, tab_cross = sorted(
                        (cross_first, cross_second),
                        key=lambda item: item[2],
                    )
                    if tab_cross[2] <= pad_cross[2]:
                        continue
                    used_indexes = {
                        overall[0],
                        pad_sep[0],
                        tab_sep[0],
                        pad_cross[0],
                        tab_cross[0],
                    }
                    pitch_inputs = [
                        item
                        for item in axes[cross_axis]
                        if item[0] not in used_indexes
                    ]
                    for pitch_input in pitch_inputs:
                        formulas = (
                            (
                                "half_pitch_x2",
                                2.0 * pitch_input[2],
                                _relation_role_score(pitch_input[1], "half_pitch"),
                            ),
                            (
                                "gap_plus_pad",
                                pitch_input[2] + pad_cross[2],
                                _relation_role_score(pitch_input[1], "gap"),
                            ),
                        )
                        for pitch_formula, pitch_value, formula_role_score in formulas:
                            evidence = (
                                _relation_item_label(overall[0], overall[1]),
                                _relation_item_label(pad_sep[0], pad_sep[1]),
                                _relation_item_label(tab_sep[0], tab_sep[1]),
                                _relation_item_label(pad_cross[0], pad_cross[1]),
                                _relation_item_label(tab_cross[0], tab_cross[1]),
                                _relation_item_label(pitch_input[0], pitch_input[1]),
                            )
                            formula_name = (
                                f"REL_ASYM3_{separation_axis}_{pitch_formula}"
                            )
                            if not _relation_pitch_matches(pitch_value, pitch_references):
                                record_rejection(
                                    formula_name,
                                    evidence,
                                    (
                                        f"pitch_y={pitch_value:.4g} mm 与封装外形 pitch "
                                        f"参考 {list(pitch_references)} 不自洽。"
                                    ),
                                )
                                continue
                            physical_pad = {
                                separation_axis: pad_sep[2],
                                cross_axis: pad_cross[2],
                            }
                            physical_tab = {
                                separation_axis: tab_sep[2],
                                cross_axis: tab_cross[2],
                            }
                            role_score = (
                                _relation_role_score(
                                    pad_sep[1],
                                    "pad_width"
                                    if separation_axis == "width"
                                    else "pad_height",
                                )
                                + _relation_role_score(
                                    tab_sep[1],
                                    "tab_width"
                                    if separation_axis == "width"
                                    else "tab_height",
                                )
                                + _relation_role_score(
                                    pad_cross[1],
                                    "pad_width" if cross_axis == "width" else "pad_height",
                                )
                                + _relation_role_score(
                                    tab_cross[1],
                                    "tab_width" if cross_axis == "width" else "tab_height",
                                )
                                + formula_role_score
                                + _relation_role_score(overall[1], "overall")
                            )
                            consider(
                                formula_name,
                                {
                                    "pad_x": physical_pad[separation_axis],
                                    "pad_y": physical_pad[cross_axis],
                                    "center_x": center_value,
                                    "pitch_y": pitch_value,
                                    "tab_x": physical_tab[separation_axis],
                                    "tab_y": physical_tab[cross_axis],
                                },
                                {
                                    "pad_x": "关系式优先：分离轴小焊盘尺寸",
                                    "pad_y": "关系式优先：交叉轴小焊盘尺寸",
                                    "center_x": "关系式优先：overall-(pad+tab)/2",
                                    "pitch_y": (
                                        "关系式优先：中心线半间距×2"
                                        if pitch_formula == "half_pitch_x2"
                                        else "关系式优先：同轴 gap+pad_size"
                                    ),
                                    "tab_x": "关系式优先：分离轴大焊盘尺寸",
                                    "tab_y": "关系式优先：交叉轴大焊盘尺寸",
                                },
                                {
                                    "pad_x": (pad_sep[1],),
                                    "pad_y": (pad_cross[1],),
                                    "center_x": (overall[1], pad_sep[1], tab_sep[1]),
                                    "pitch_y": (
                                        (pitch_input[1],)
                                        if pitch_formula == "half_pitch_x2"
                                        else (pitch_input[1], pad_cross[1])
                                    ),
                                    "tab_x": (tab_sep[1],),
                                    "tab_y": (tab_cross[1],),
                                },
                                relation_score=6,
                                role_score=role_score,
                                evidence=evidence,
                            )

    best_by_solution: dict[
        tuple[tuple[str, float], ...],
        tuple[
            str,
            dict[str, float],
            dict[str, str],
            dict[str, tuple[Dimension, ...]],
            int,
            int,
            tuple[str, ...],
        ],
    ] = {}
    for candidate in candidates:
        key = _solution_key(candidate[1])
        incumbent = best_by_solution.get(key)
        if incumbent is None or (candidate[4], candidate[5]) > (
            incumbent[4],
            incumbent[5],
        ):
            best_by_solution[key] = candidate
    unique_candidates = list(best_by_solution.values())
    selected = None
    if unique_candidates:
        best_relation_score = max(candidate[4] for candidate in unique_candidates)
        relation_winners = [
            candidate
            for candidate in unique_candidates
            if candidate[4] == best_relation_score
        ]
        best_role_score = max(candidate[5] for candidate in relation_winners)
        winners = [
            candidate
            for candidate in relation_winners
            if candidate[5] == best_role_score
        ]
        if len(winners) == 1:
            selected = winners[0]
            selected_key = _solution_key(selected[1])
            selected_record_marked = False
            for record in relation_records:
                solution = record.get("solution")
                if solution is None:
                    continue
                record_key = _solution_key(
                    {key: float(value) for key, value in solution.items()}
                )
                if record_key == selected_key and not selected_record_marked:
                    record["status"] = "selected"
                    record["selected"] = True
                    selected_record_marked = True
                else:
                    record["status"] = "not_selected"
                    record["rejection_reasons"] = [
                        "关系分低于唯一候选，或关系同分后角色次级分较低。"
                    ]
        else:
            tied_keys = {_solution_key(candidate[1]) for candidate in winners}
            for record in relation_records:
                solution = record.get("solution")
                if solution is None:
                    continue
                key = _solution_key({name: float(value) for name, value in solution.items()})
                if key in tied_keys:
                    record["status"] = "ambiguous"
                    record["rejection_reasons"] = [
                        "关系分与角色次级分均并列，禁止自动选择。"
                    ]
    if selected is None and not relation_records:
        record_rejection("no_candidate", (), "未形成任何关系式候选。")
    if selected is None:
        return None, tuple(relation_records)
    return (selected[0], selected[1], selected[2], selected[3]), tuple(relation_records)


_DECIMAL_EXTENSION_TOKEN_RE = re.compile(r"\d+[.,]\d+|\.\d+|\d+")


def _normalized_numeric_source(value: Any) -> str:
    return str(value).strip().replace(",", ".")


def decimal_extension_suspicion(
    source_value: Any,
    page_text: str | None,
) -> dict[str, Any]:
    """Reject only a positive text-layer contradiction that extends a decimal."""

    source = _normalized_numeric_source(source_value)
    result: dict[str, Any] = {
        "source": source,
        "status": "pass",
        "text_token": None,
        "reason": "",
    }
    if "." not in source:
        result["reason"] = "integer_or_non_decimal_source"
        return result
    try:
        source_number = Decimal(source)
    except InvalidOperation:
        result["reason"] = "source_not_decimal"
        return result

    tokens = [
        _normalized_numeric_source(token)
        for token in _DECIMAL_EXTENSION_TOKEN_RE.findall(page_text or "")
    ]
    numeric_tokens: list[tuple[str, Decimal]] = []
    for token in tokens:
        try:
            numeric_tokens.append((token, Decimal(token)))
        except InvalidOperation:
            continue
    if any(number == source_number for _token, number in numeric_tokens):
        result["reason"] = "numeric_equal_text_token_present"
        return result
    for token, _number in numeric_tokens:
        suffix = token[len(source) :] if token.startswith(source) else ""
        if token != source and suffix and suffix.isdigit():
            result.update(
                {
                    "status": "suspicious",
                    "text_token": token,
                    "reason": "decimal_prefix_extended_by_text_token",
                }
            )
            return result
    result["reason"] = "no_positive_extension_contradiction"
    return result


def _body_axis_source_dimensions(
    transcription: Transcription,
    provenance: dict[str, Any],
) -> dict[str, tuple[Dimension, ...]]:
    assignments = provenance.get("assignments") if isinstance(provenance, dict) else None
    if not isinstance(assignments, dict):
        return {}
    resolved: dict[str, tuple[Dimension, ...]] = {}
    for field_name in ("body_x", "body_y"):
        assignment = assignments.get(field_name)
        if not isinstance(assignment, dict) or assignment.get("status") != "proven":
            continue
        raw = str(assignment.get("source_raw") or "")
        symbol = str(assignment.get("source_symbol") or "")
        role = str(assignment.get("source_role") or "")
        matches = tuple(
            item
            for item in transcription.dimensions
            if not item.is_derived
            and item.raw == raw
            and item.symbol == symbol
            and item.role == role
        )
        if matches:
            resolved[field_name] = matches
    return resolved


def _apply_decimal_extension_guard(
    transcription: Transcription,
    values: dict[str, float | None],
    sources: dict[str, str],
    field_operands: dict[str, tuple[Dimension, ...]],
    body_axis_provenance: dict[str, Any],
    page_text: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Blank only fields whose direct transcription operands have a proven extension."""

    operands_by_field = dict(field_operands)
    operands_by_field.update(
        _body_axis_source_dimensions(transcription, body_axis_provenance)
    )
    ledger: dict[str, Any] = {
        "schema": "m1_1_decimal_extension_guard_v1",
        "rule": "reject_only_positive_decimal_extension_contradiction",
        "fields": {},
        "suspicious_fields": [],
    }
    for field_name, value in values.items():
        if value is None:
            continue
        unique_operands: list[Dimension] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for item in operands_by_field.get(field_name, ()):
            if item.is_derived:
                continue
            key = (item.symbol, item.value, item.unit, item.raw, item.endpoints)
            if key in seen:
                continue
            seen.add(key)
            unique_operands.append(item)
        checks = []
        for item in unique_operands:
            check = decimal_extension_suspicion(item.value, page_text)
            checks.append(
                {
                    "source_dimension": asdict(item),
                    **check,
                }
            )
        suspicious = [check for check in checks if check["status"] == "suspicious"]
        ledger["fields"][field_name] = {
            "value_mm_before_guard": value,
            "direct_operand_count": len(unique_operands),
            "checks": checks,
            "status": "suspicious" if suspicious else "pass",
        }
        if not suspicious:
            continue
        text_token = str(suspicious[0]["text_token"])
        notice = f"审核方闸：疑漏读末位（文本层另有 {text_token}）"
        original_source = sources.get(field_name, "模型候选")
        values[field_name] = None
        sources[field_name] = f"{notice}；原来源：{original_source}"
        warnings.append(notice)
        ledger["suspicious_fields"].append(field_name)
    ledger["suspicious_count"] = len(ledger["suspicious_fields"])
    return ledger


def _apply_body_reasonableness_guard(
    family: str,
    values: dict[str, float | None],
    sources: dict[str, str],
    warnings: list[str],
) -> None:
    """Reject a body axis that is smaller than the pads and gap on that axis."""

    def reject_if_too_small(
        field: str,
        axis_label: str,
        pad_value: float | None,
        gap_value: float | None,
    ) -> None:
        body_value = values.get(field)
        if body_value is None or pad_value is None:
            return
        requirements = [("单个焊盘尺寸", pad_value)]
        if gap_value is not None and gap_value > 0:
            requirements.append(("焊盘间隙", gap_value))
        failed = [
            (label, minimum)
            for label, minimum in requirements
            if body_value + 1e-9 < minimum
        ]
        if not failed:
            return
        detail = "、".join(
            f"{label} {minimum:.4g} mm" for label, minimum in failed
        )
        original_source = sources.get(field, "模型候选")
        values[field] = None
        sources[field] = (
            f"body 合理性闸拒绝 {body_value:.4g} mm：小于{detail}；"
            f"原来源：{original_source}"
        )
        warnings.append(
            f"body {axis_label} 候选 {body_value:.4g} mm 小于{detail}，"
            "该轴已转 unresolved，禁止进入 auto_unique。"
        )

    pad_x = values.get("pad_x")
    center_x = values.get("center_x")
    body_x = values.get("body_x")
    if (
        family == "CHIP"
        and body_x is not None
        and center_x is not None
        and body_x + 1e-9 < center_x
    ):
        original_source = sources.get("body_x", "模型候选")
        values["body_x"] = None
        sources["body_x"] = (
            f"CHIP body 横向闸拒绝 {body_x:.4g} mm："
            f"小于焊盘中心距 {center_x:.4g} mm；原来源：{original_source}"
        )
        warnings.append(
            f"CHIP body_x 候选 {body_x:.4g} mm 小于 center_x "
            f"{center_x:.4g} mm，该轴已转 unresolved，禁止进入 auto_unique。"
        )
        warnings.append(CHIP_EXTERNAL_TERMINAL_HINT)
    gap_x: float | None = None
    if pad_x is not None and center_x is not None:
        if family == "ASYM3" and values.get("tab_x") is not None:
            gap_x = center_x - (pad_x + float(values["tab_x"])) / 2.0
        else:
            gap_x = center_x - pad_x
    reject_if_too_small("body_x", "X", pad_x, gap_x)

    # A transverse land may legitimately overhang a two-terminal package.
    # Apply the Y-axis guard only when multiple pads form a measurable Y gap.
    pad_y = values.get("pad_y")
    pitch_y = values.get("pitch_y")
    if family in {"SOT3", "ASYM3", "GRID4"} and pad_y is not None and pitch_y is not None:
        reject_if_too_small("body_y", "Y", pad_y, pitch_y - pad_y)


def _apply_body_overall_cross_guard(
    transcription: Transcription,
    values: dict[str, float | None],
    sources: dict[str, str],
    warnings: list[str],
) -> None:
    """Reject, but never select, a body value exceeding a same-axis overall span."""

    overall_items = tuple(
        item
        for item in transcription.dimensions
        if item.role == "overall_span"
        and item.belongs_to == "package_outline"
        and not item.is_derived
    )
    if not overall_items:
        return
    for body_role, field, label in (
        ("body_length", "body_x", "X"),
        ("body_width", "body_y", "Y"),
    ):
        body_value = values.get(field)
        if body_value is None:
            continue
        matching_body_items = tuple(
            item
            for item in _role_items(
                transcription,
                body_role,
                belongs_to="package_outline",
            )
            if math.isclose(
                dimension_value_mm(item), body_value, rel_tol=0.0, abs_tol=0.011
            )
        )
        body_axes = {
            axis
            for item in matching_body_items
            if (axis := _endpoint_span_axis(item)) is not None
        }
        if len(body_axes) != 1:
            continue
        axis = next(iter(body_axes))
        same_axis_overall = tuple(
            item for item in overall_items if _endpoint_span_axis(item) == axis
        )
        if not same_axis_overall:
            continue
        overall_value = max(dimension_value_mm(item) for item in same_axis_overall)
        if body_value <= overall_value + 1e-9:
            continue
        original_source = sources.get(field, "模型候选")
        values[field] = None
        sources[field] = (
            f"body≤overall 同轴闸拒绝 {body_value:.4g} mm："
            f"大于 overall {overall_value:.4g} mm；原来源：{original_source}"
        )
        warnings.append(
            f"body {label} 候选 {body_value:.4g} mm 大于同轴 overall "
            f"{overall_value:.4g} mm，该轴已转 unresolved；本闸未另选候选。"
        )


def infer_geometry_prefill(
    transcription: Transcription,
    family: str,
    *,
    page_text: str | None = None,
) -> GeometryPrefillResult:
    if family not in SUPPORTED_FAMILIES:
        raise E1aError(f"不支持的封装族：{family}")
    mapping = _dimension_map(transcription)
    values: dict[str, float | None] = {
        "pad_x": None,
        "pad_y": None,
        "center_x": None,
        "center_y": None,
        "pitch_y": None,
        "pitch_x": None,
        "tab_x": None,
        "tab_y": None,
        "body_x": None,
        "body_y": None,
        "dual_left_count": None,
        "dual_right_count": None,
        "quad_left_count": None,
        "quad_right_count": None,
        "quad_top_count": None,
        "quad_bottom_count": None,
    }
    sources: dict[str, str] = {}
    warnings: list[str] = []
    body_axis_provenance: dict[str, Any] | None = None
    notation_ledger = (
        classify_page_dimension_notation(page_text)
        if page_text is not None
        else None
    )
    decimal_extension_guard: dict[str, Any] = {
        "schema": "m1_1_decimal_extension_guard_v1",
        "rule": "reject_only_positive_decimal_extension_contradiction",
        "fields": {},
        "suspicious_fields": [],
        "suspicious_count": 0,
    }
    asym3_axis_consistency = _evaluate_asym3_axis_consistency(family, values)
    ep_evidence = _ep_positive_evidence(transcription, page_text)
    quad_topology = (
        _quad_ep_topology_evidence(transcription, page_text)
        if ep_evidence is not None
        else None
    )
    if ep_evidence is not None and (
        family != "QUAD_EP"
        or quad_topology is None
        or quad_topology.get("status") not in {"auto", "partial"}
    ):
        reason = _ep_rejection_reason(ep_evidence)
        if quad_topology is not None and quad_topology.get("reason"):
            reason += "；" + str(quad_topology["reason"])
        return GeometryPrefillResult(
            values=values,
            sources=sources,
            warnings=(reason,),
            status="reject_ep",
            combination=None,
            message=reason,
            land_dimensions=(),
            excluded_half_dimensions=(),
            paste_dimensions=(),
            unknown_pattern_dimensions=(),
            paste_guard_status="not_applicable",
            body_source="unresolved",
            auto_pads=False,
            auto_full=False,
            relation_candidates=(),
            body_axis_provenance={
                "schema": "m2_1_ep_positive_evidence_gate_v1",
                "status": "rejected",
                "mapping_source": None,
                "reason": reason,
            },
            asym3_axis_consistency=asym3_axis_consistency,
            decimal_extension_guard=decimal_extension_guard,
        )
    pin_mapping_evidence = new_family_pin_mapping_evidence(transcription, family)
    if pin_mapping_evidence["status"] == "needs_user_review":
        warnings.append(
            "焊盘编号 needs_user_review：原图未完整证明编号映射；"
            "预览编号仅为占位，须人工核对后才能确认入库。"
        )

    derived_items = _derived_role_items(transcription)
    if derived_items:
        warnings.append(
            "模型返回了 is_derived=true 的尺寸，已禁止直接用于几何并标为人工确认项："
            + "、".join(f"{item.role}={item.value}" for item in derived_items)
        )

    land_dimensions = _metric_land_dimensions(transcription)
    paste_dimensions = tuple(
        item for item in land_dimensions if dimension_pattern_role(item) == "paste"
    )
    explicit_lands = tuple(
        item for item in land_dimensions if dimension_pattern_role(item) == "lands"
    )
    unknown_pattern_dimensions = tuple(
        item
        for item in land_dimensions
        if dimension_pattern_role(item) == "unknown"
    )
    paste_guard_status = "not_applicable"
    paste_evidence_visible = transcription.paste_evidence_visible or bool(
        paste_dimensions
    )
    if paste_evidence_visible:
        warnings.append("疑似含钢网开口尺寸：焊膏/paste 数值不得作为铜箔焊盘。")
        if explicit_lands and paste_dimensions:
            paste_ok, paste_detail = _paste_is_smaller_than_lands(
                explicit_lands, paste_dimensions
            )
            if paste_ok:
                paste_guard_status = "lands_selected_paste_smaller"
                warnings.append(f"焊膏判别闸通过：{paste_detail}；自动求解仅使用 lands。")
            else:
                paste_guard_status = "ambiguous"
                warnings.append(
                    "焊膏判别闸无法自校验："
                    f"{paste_detail}；已停止自动预填，请人工点选 lands。"
                )
        else:
            paste_guard_status = "ambiguous"
            warnings.append(
                "检测到 paste/stencil 证据，但 lands/paste 两组未完整区分；已停止自动预填。"
            )
    active_role_dimensions = tuple(
        item
        for item in land_dimensions
        if dimension_pattern_role(item) != "paste" and not item.is_derived
    )
    if explicit_lands:
        active_role_dimensions = tuple(
            item
            for item in active_role_dimensions
            if dimension_pattern_role(item) == "lands"
            or _is_center_distance_dimension(item)
            or _is_total_span_dimension(item)
        )
    minimum_remaining = (
        6
        if family in {"ASYM3", "QUAD_EP"}
        else (4 if family in {"SOT3", "GRID4", "DUAL"} else 3)
    )
    active_land, excluded_half, half_restored = _half_value_filter(
        active_role_dimensions,
        minimum_remaining=minimum_remaining,
    )
    if excluded_half:
        half_text = ", ".join(f"{item.value} {item.unit}" for item in excluded_half)
        if half_restored:
            warnings.append(
                f"半值候选 {half_text} 剔除后参数不足，已放回并要求人工确认。"
            )
        else:
            warnings.append(f"已先剔除半值候选：{half_text}。")

    land_mapping = _dimension_map(active_land)
    solutions: list[
        tuple[
            str,
            dict[str, float],
            dict[str, str],
            dict[str, tuple[Dimension, ...]],
        ]
    ] = []
    self_check_failed = paste_guard_status == "ambiguous"

    def land_item(aliases: Iterable[str]) -> Dimension | None:
        return _find_dimension(land_mapping, aliases)

    def add_solution(
        combination: str,
        solved: dict[str, float],
        solved_sources: dict[str, str],
        solved_operands: dict[str, tuple[Dimension, ...]],
    ) -> None:
        try:
            _validate_two_pad_gap_center_consistency(
                family,
                solved.get("center_x", float("nan")),
                active_land,
            )
        except E1aError as exc:
            warnings.append(f"LAND 组合{combination}被拒绝：{exc}")
            return
        dual_axis_match = (
            re.match(r"REL_DUAL_(width|height)_", combination)
            if family == "DUAL"
            else None
        )
        direct_centers = tuple(
            dimension_value_mm(item)
            for item in active_land
            if item.belongs_to == "solder_land"
            and _is_center_distance_dimension(item)
            and (
                dual_axis_match is None
                or _dual_row_relation_axis(item) == dual_axis_match.group(1)
            )
        )
        if direct_centers and not any(
            math.isclose(
                solved.get("center_x", float("nan")),
                center,
                rel_tol=0.0,
                abs_tol=0.02,
            )
            for center in direct_centers
        ):
            warnings.append(
                f"LAND 组合{combination}被拒绝：图中已有中心距，禁止二次相减或替换。"
            )
            return
        total_spans = tuple(
            dimension_value_mm(item)
            for item in active_land
            if item.belongs_to == "solder_land"
            and _is_total_span_dimension(item)
            and (
                dual_axis_match is None
                or _dual_row_relation_axis(item) == dual_axis_match.group(1)
            )
        )
        if not direct_centers and total_spans and {
            "center_x",
            "pad_x",
        }.issubset(solved):
            if family == "ASYM3":
                if "tab_x" not in solved:
                    warnings.append(
                        f"LAND 组合{combination}被拒绝：ASYM3 总跨校验缺少 tab_x。"
                    )
                    return
                solved_span = solved["center_x"] + (
                    solved["pad_x"] + solved["tab_x"]
                ) / 2.0
            else:
                solved_span = solved["center_x"] + solved["pad_x"]
            span_tolerance = 0.05 if family == "DUAL" else 0.02
            if any(
                math.isclose(
                    solved_span,
                    total,
                    rel_tol=0.0,
                    abs_tol=span_tolerance,
                )
                for total in total_spans
            ):
                pass
            else:
                warnings.append(
                    f"LAND 组合{combination}被拒绝：焊盘外缘总跨与图中总宽不符。"
                )
                return
        key = _solution_key(solved)
        if any(_solution_key(existing[1]) == key for existing in solutions):
            return
        solutions.append((combination, solved, solved_sources, solved_operands))

    relation_land = tuple(
        item for item in active_land if item.belongs_to == "solder_land"
    )
    if (
        family == "QUAD_EP"
        and quad_topology is not None
        and quad_topology.get("status") in {"auto", "partial"}
        and not self_check_failed
    ):
        solved = {
            key: float(value)
            for key, value in dict(quad_topology.get("solved") or {}).items()
        }
        solved_sources = {
            str(key): str(value)
            for key, value in dict(quad_topology.get("sources") or {}).items()
        }
        solved_operands = {
            str(key): tuple(value)
            for key, value in dict(quad_topology.get("operands") or {}).items()
        }
        solutions.append(
            (
                str(quad_topology.get("combination") or "REL_QUAD_EP"),
                solved,
                solved_sources,
                solved_operands,
            )
        )
        if quad_topology.get("status") == "partial":
            warnings.append(
                "QUAD/EP 拓扑已确定，焊盘双轴、pitch 与中央 EP 已自动预填；"
                "中心距没有未限定的 solder_land 唯一闭合链，保留为空待人工填写。"
            )
        else:
            warnings.append(
                "QUAD/EP 关系式形成唯一闭合解；V/EV 热过孔已排除，"
                "中央 EP 与外围焊盘分别建模。"
            )
        warnings.append(
            "EP 焊膏暂行规则：单块居中、同形状，双轴均乘 sqrt(0.70)，"
            "面积覆盖率 70%；审核卡与预览必须人工确认。"
        )
        warnings.append(
            "中央 EP 电气编号当前为明确占位符 EP；正式入库前须按符号/资料人工确认。"
        )
    if family == "DUAL":
        dual_topology = _dual_topology_evidence(
            transcription,
            relation_land,
            page_text,
        )
        row_counts = dual_topology.get("row_counts")
        if isinstance(row_counts, list) and len(row_counts) == 2:
            values["dual_left_count"] = int(row_counts[0])
            values["dual_right_count"] = int(row_counts[1])
            sources["dual_left_count"] = "确定性 DUAL 两排拓扑"
            sources["dual_right_count"] = "确定性 DUAL 两排拓扑"
    if family == "QUAD_EP":
        relation_choice = None
        relation_records = tuple(
            dict(item) for item in (quad_topology or {}).get("checks", [])
        )
    else:
        relation_choice, relation_records = _relation_first_solution(
            transcription,
            family,
            relation_land,
            page_text,
        )
    dual_chain_conflict = family == "DUAL" and any(
        record.get("status") == "rejected"
        and str(record.get("formula", "")).startswith("REL_DUAL_")
        and str(record.get("formula", "")).endswith("_CHAIN_INNER_OUTER")
        for record in relation_records
    )
    if dual_chain_conflict:
        self_check_failed = True
        warnings.append(
            "DUAL 内外缘过定链闭合失败；禁止角色直取或单边关系绕过，整件转人工。"
        )
    relation_solution_added = family == "QUAD_EP" and bool(solutions)

    def one_role(role: str, *, belongs_to: str | None = None) -> Dimension | None:
        if belongs_to == "solder_land":
            items = _effective_role_items(active_land, role)
        else:
            items = _role_items(transcription, role, belongs_to=belongs_to)
        if len(items) == 1:
            return items[0]
        if len(items) > 1:
            warnings.append(f"角色 {role} 有 {len(items)} 个直接标注值，角色直取停止。")
        return None

    role_pad_x = one_role("pad_width", belongs_to="solder_land")
    role_pad_y = one_role("pad_height", belongs_to="solder_land")
    sot3_role_pitch: Dimension | None = None
    if family == "SOT3":
        role_center_items = tuple(
            item
            for item in active_land
            if item.role in {"pad_center_distance", "pitch"} and not item.is_derived
        )
        role_center, sot3_role_pitch = _sot3_center_roles(role_center_items)
        if role_center is None and role_center_items:
            warnings.append(
                "SOT3 中心距角色无法按标注端点区分行距与双焊盘间距；角色直取停止。"
            )
    else:
        role_center_items = _effective_role_items(active_land, "pad_center_distance")
        role_center = one_role("pad_center_distance", belongs_to="solder_land")
    role_gap = one_role("pad_gap", belongs_to="solder_land")
    role_overall = one_role("overall_span", belongs_to="solder_land")
    role_pitch = (
        None
        if family == "SOT3" and sot3_role_pitch is not None
        else one_role("pitch", belongs_to="solder_land")
    )
    role_half_pitch = one_role("half_pitch", belongs_to="solder_land")
    role_tab_x = one_role("tab_width", belongs_to="solder_land")
    role_tab_y = one_role("tab_height", belongs_to="solder_land")

    role_solved: dict[str, float] = {}
    role_sources: dict[str, str] = {}
    role_operands: dict[str, tuple[Dimension, ...]] = {}

    def put_role(field: str, item: Dimension, value: float | None = None) -> None:
        role_solved[field] = dimension_value_mm(item) if value is None else value
        role_sources[field] = f"角色直取 {item.role}：{item.raw}"
        role_operands[field] = (item,)

    layout_rotated = family == "ASYM3" and role_overall is not None and _endpoints_axis(
        role_overall.endpoints
    ) == "vertical"
    if layout_rotated:
        role_pad_x, role_pad_y = role_pad_y, role_pad_x
        role_tab_x, role_tab_y = role_tab_y, role_tab_x
        warnings.append("ASYM3 角色图显示大小焊盘沿竖直方向分离；自动旋转 90° 映射到生成器。")
    elif family == "SOT3":
        # The established SOT3 generator is rotated 90 degrees relative to
        # the usual datasheet land-pattern view: the one-pad/two-pad row
        # separation is center_x, while the pair separation is pitch_y.
        role_pad_x, role_pad_y = role_pad_y, role_pad_x
        warnings.append("SOT3 按标注端点方向映射到既有生成器轴。")

    if role_pad_x is not None:
        put_role("pad_x", role_pad_x)
    if role_pad_y is not None:
        put_role("pad_y", role_pad_y)
    if role_center is not None:
        put_role("center_x", role_center)
    if role_pitch is not None:
        put_role("pitch_y", role_pitch)
    elif sot3_role_pitch is not None:
        put_role("pitch_y", sot3_role_pitch)
    elif role_half_pitch is not None:
        put_role("pitch_y", role_half_pitch, 2.0 * dimension_value_mm(role_half_pitch))
        role_sources["pitch_y"] = (
            f"代码换算 {_effective_land_role(role_half_pitch)}×2：{role_half_pitch.raw}"
        )
    if role_tab_x is not None:
        put_role("tab_x", role_tab_x)
    if role_tab_y is not None:
        put_role("tab_y", role_tab_y)

    if (
        family == "ASYM3"
        and role_gap is not None
        and role_pad_y is not None
        and role_pitch is None
        and role_half_pitch is None
    ):
        gap_pitch = dimension_value_mm(role_gap) + dimension_value_mm(role_pad_y)
        alternate_pitch = 2.0 * dimension_value_mm(role_gap)
        outline_pitches = _full_outline_pitch_values(transcription)
        matching = [
            value
            for value in (gap_pitch, alternate_pitch)
            if _within_outline_pitch_range(value, outline_pitches)
        ]
        if len(matching) == 1:
            role_solved["pitch_y"] = matching[0]
            role_sources["pitch_y"] = (
                "端点关系消歧：gap_between_pads+pad_height"
                if math.isclose(matching[0], gap_pitch, rel_tol=0.0, abs_tol=1e-9)
                else "端点关系消歧：中心线半间距×2"
            )
            role_operands["pitch_y"] = (
                (role_gap, role_pad_y)
                if math.isclose(matching[0], gap_pitch, rel_tol=0.0, abs_tol=1e-9)
                else (role_gap,)
            )
        else:
            warnings.append(
                "gap_between_pads 无法依据封装外形表 e 唯一消歧为边缘间距或半间距。"
            )

    if (
        "center_x" not in role_solved
        and family == "ASYM3"
        and role_overall is not None
        and role_pad_x is not None
        and role_tab_x is not None
    ):
        role_solved["center_x"] = (
            dimension_value_mm(role_overall)
            - dimension_value_mm(role_pad_x) / 2.0
            - dimension_value_mm(role_tab_x) / 2.0
        )
        role_sources["center_x"] = "代码推导 overall_span-小焊盘半宽-大焊盘半宽"
        role_operands["center_x"] = (role_overall, role_pad_x, role_tab_x)
    gap_axis = _endpoint_span_axis(role_gap) if role_gap is not None else None
    gap_axis_pad = role_pad_x
    if family in {"CHIP", "SMX"}:
        gap_axis_pad = {
            "width": role_pad_x,
            "height": role_pad_y,
        }.get(gap_axis)
        if gap_axis is None:
            gap_axis_pad = role_pad_x

    if (
        "center_x" not in role_solved
        and role_gap is not None
        and family == "ASYM3"
        and role_pad_x is not None
        and role_tab_x is not None
    ):
        role_solved["center_x"] = (
            dimension_value_mm(role_gap)
            + dimension_value_mm(role_pad_x) / 2.0
            + dimension_value_mm(role_tab_x) / 2.0
        )
        role_sources["center_x"] = "代码推导 pad_gap+小焊盘半宽+大焊盘半宽"
        role_operands["center_x"] = (role_gap, role_pad_x, role_tab_x)
    elif (
        "center_x" not in role_solved
        and role_gap is not None
        and family in {"CHIP", "SMX"}
        and gap_axis_pad is not None
    ):
        role_solved["center_x"] = (
            dimension_value_mm(role_gap) + dimension_value_mm(gap_axis_pad)
        )
        axis_label = "pad_height" if gap_axis == "height" else "pad_width"
        role_sources["center_x"] = (
            f"代码推导同轴 pad_gap+{axis_label}：{role_gap.raw}+{gap_axis_pad.raw}"
        )
        role_operands["center_x"] = (role_gap, gap_axis_pad)
    elif "center_x" not in role_solved and role_overall is not None and role_pad_x is not None:
        role_solved["center_x"] = dimension_value_mm(role_overall) - dimension_value_mm(role_pad_x)
        role_sources["center_x"] = (
            f"代码推导 overall_span-pad_width：{role_overall.raw}-{role_pad_x.raw}"
        )
        role_operands["center_x"] = (role_overall, role_pad_x)
    elif role_overall is not None and role_pad_x is None and role_gap is not None:
        pad_x_value = (dimension_value_mm(role_overall) - dimension_value_mm(role_gap)) / 2.0
        if pad_x_value > 0:
            role_solved["pad_x"] = pad_x_value
            role_solved["center_x"] = dimension_value_mm(role_overall) - pad_x_value
            role_sources["pad_x"] = (
                f"代码推导 (overall_span-pad_gap)/2：{role_overall.raw}-{role_gap.raw}"
            )
            role_sources["center_x"] = "代码推导 overall_span-pad_x"
            role_operands["pad_x"] = (role_overall, role_gap)
            role_operands["center_x"] = (role_overall, role_gap)

    role_required = {"pad_x", "pad_y", "center_x"}
    if family in {"SOT3", "GRID4", "DUAL"}:
        role_required.add("pitch_y")
    elif family == "ASYM3":
        role_required.update({"pitch_y", "tab_x", "tab_y"})
    role_available = bool(role_solved)
    role_conflicts: list[str] = []
    if "center_x" in role_solved:
        try:
            _validate_two_pad_gap_center_consistency(
                family,
                role_solved["center_x"],
                active_land,
            )
        except E1aError as exc:
            role_conflicts.append(str(exc))
    if role_center is not None and role_pad_y is not None:
        center = role_solved["center_x"]
        pad_height = role_solved["pad_y"]
        ratio = max(center, pad_height) / min(center, pad_height)
        if ratio <= 1.5:
            role_conflicts.append(
                "pad_center_distance 与 pad_height 数量级过近，疑似角色互换"
            )
    if "pitch_y" in role_solved:
        outline_pitch_values = _full_outline_pitch_values(transcription)
        if outline_pitch_values:
            pitch = role_solved["pitch_y"]
            relative_errors = [
                abs(pitch - outline) / outline for outline in outline_pitch_values
            ]
            if min(relative_errors) > 0.15:
                visible = ", ".join(f"{value:.4g}" for value in outline_pitch_values)
                role_conflicts.append(
                    f"解出的 pitch_y={pitch:.4g} mm 与封装外形表 e={visible} mm 相差超过 15%"
                )

    if relation_choice is not None and not self_check_failed:
        relation_combination, relation_solved, _, _ = relation_choice
        probe_values = dict(values)
        probe_values.update(relation_solved)
        probe_sources: dict[str, str] = {}
        probe_warnings: list[str] = []
        probe_provenance = _resolve_body_axis_provenance(
            transcription,
            family,
            relation_combination,
            probe_values,
            probe_sources,
            probe_warnings,
            notation_ledger,
        )
        relation_body_axis_conflict = any(
            assignment.get("status") == "unresolved_relation_axis_conflict"
            for assignment in probe_provenance.get("assignments", {}).values()
        )
        role_complete_and_clean = (
            role_required.issubset(role_solved) and not role_conflicts
        )
        role_matches_relation_geometry = role_complete_and_clean and all(
            field_name in relation_solved
            and math.isclose(
                role_solved[field_name],
                relation_solved[field_name],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            for field_name in role_required
        )
        if relation_body_axis_conflict and role_matches_relation_geometry:
            warnings.append(
                "关系式候选与已证本体轴冲突，且完整角色直取解无冲突；"
                "两者焊盘几何逐项等价，按拒绝式自洽闸保留角色解，"
                "未放宽本体轴证明。"
            )
            relation_records = tuple(
                {
                    **record,
                    "status": (
                        "rejected"
                        if record.get("selected")
                        else record.get("status")
                    ),
                    "selected": False,
                    "rejection_reasons": (
                        list(record.get("rejection_reasons") or [])
                        + ["与已证本体轴冲突；完整角色直取解无冲突"]
                        if record.get("selected")
                        else list(record.get("rejection_reasons") or [])
                    ),
                }
                for record in relation_records
            )
        else:
            before_count = len(solutions)
            add_solution(*relation_choice)
            relation_solution_added = len(solutions) > before_count
            if relation_solution_added:
                warnings.append(
                    "关系式优先枚举形成唯一解；模型 role 仅用于几何同分候选的次级裁决。"
                )
    if role_conflicts:
        if relation_solution_added:
            warnings.extend(
                "角色冲突（不推翻关系式唯一解，仅留痕）：" + item
                for item in role_conflicts
            )
        else:
            self_check_failed = True
            warnings.extend("角色冲突（标黄待确认）：" + item for item in role_conflicts)

    if relation_solution_added:
        pass
    elif role_required.issubset(role_solved) and not role_conflicts:
        add_solution("ROLE", role_solved, role_sources, role_operands)
    elif role_available and not role_conflicts:
        missing = sorted(role_required - set(role_solved))
        warnings.append("角色直取尚缺字段：" + "、".join(missing) + "；将回退到既有关系式。")

    if relation_solution_added:
        warnings.append("关系式唯一候选已锁定；未启用角色直取或旧组合猜测。")
    elif solutions:
        warnings.append("角色字段已形成唯一候选；未启用旧组合猜测。")
    elif family == "ASYM3":
        warnings.append(
            "ASYM3 本轮不自动映射：请按推荐焊盘图人工核对并填写大小焊盘几何。"
        )
    elif family in {"INLINE3", "GRID4", "DUAL", "QUAD_EP"}:
        warnings.append(
            f"{family} 拓扑关系未形成唯一解；禁止回退到旧族的符号顺序或数值排序组合。"
        )
    elif family == "SOT3":
        pad_x_item = land_item(("PAD_X", "PAD_LENGTH", "LAND_LENGTH", "Y"))
        pad_y_item = land_item(("PAD_Y", "PAD_WIDTH", "LAND_WIDTH", "X"))
        center_item = land_item(("CENTER_X", "ROW_SPAN", "ROW_SPAN_X", "C", "C1"))
        pitch_item = land_item(("PITCH_Y", "PIN_PITCH", "E", "C2"))
        if all((pad_x_item, pad_y_item, center_item, pitch_item)):
            direct_items = {
                "pad_x": pad_x_item,
                "pad_y": pad_y_item,
                "center_x": center_item,
                "pitch_y": pitch_item,
            }
            add_solution(
                "A",
                {field: dimension_value_mm(item) for field, item in direct_items.items()},
                {
                    field: f"LAND 组合A：{item.symbol}={item.value} {item.unit}"
                    for field, item in direct_items.items()
                },
                {field: (item,) for field, item in direct_items.items()},
            )
        # Unlabelled LAND groups use the model contract's spatial scan order.
        generic_group = all(_is_generic_land_group_item(item) for item in active_land)
        if generic_group and len(active_land) == 4:
            pad_x_item, center_item, pad_y_item, pitch_item = active_land
            add_solution(
                "A",
                {
                    "pad_x": dimension_value_mm(pad_x_item),
                    "pad_y": dimension_value_mm(pad_y_item),
                    "center_x": dimension_value_mm(center_item),
                    "pitch_y": dimension_value_mm(pitch_item),
                },
                {
                    "pad_x": f"LAND 组合A：第1项 {pad_x_item.raw}",
                    "center_x": f"LAND 组合A：第2项 {center_item.raw}",
                    "pad_y": f"LAND 组合A：第3项 {pad_y_item.raw}",
                    "pitch_y": f"LAND 组合A：第4项 {pitch_item.raw}",
                },
                {
                    "pad_x": (pad_x_item,),
                    "center_x": (center_item,),
                    "pad_y": (pad_y_item,),
                    "pitch_y": (pitch_item,),
                },
            )
    else:
        pad_x_item = land_item(
            ("PAD_X", "PAD_LENGTH", "LAND_LENGTH", "SOLDER LAND WIDTH", "Y")
        )
        pad_y_item = land_item(
            ("PAD_Y", "PAD_WIDTH", "LAND_WIDTH", "SOLDER LAND HEIGHT", "X")
        )
        center_item = land_item(
            (
                "CENTER_X",
                "CENTER_PITCH",
                "PITCH_X",
                "LAND CENTER DISTANCE",
                "CENTER TO CENTER",
                "C",
                "E",
            )
        )
        overall_item = land_item(
            (
                "Z",
                "ZMAX",
                "OVERALL",
                "OVERALL_LAND_SPAN",
                "OCCUPIED WIDTH",
            )
        )
        gap_item = land_item(("G", "GMIN", "GAP", "INNER_GAP"))
        if all((pad_x_item, pad_y_item, center_item)):
            direct_items = {
                "pad_x": pad_x_item,
                "pad_y": pad_y_item,
                "center_x": center_item,
            }
            add_solution(
                "A",
                {field: dimension_value_mm(item) for field, item in direct_items.items()},
                {
                    field: f"LAND 组合A：{item.symbol}={item.value} {item.unit}"
                    for field, item in direct_items.items()
                },
                {field: (item,) for field, item in direct_items.items()},
            )
        if overall_item is not None and gap_item is not None and pad_y_item is not None:
            overall = dimension_value_mm(overall_item)
            gap = dimension_value_mm(gap_item)
            pad_y = dimension_value_mm(pad_y_item)
            if pad_x_item is None:
                if overall > gap:
                    pad_x = (overall - gap) / 2.0
                    add_solution(
                        "B",
                        {
                            "pad_x": pad_x,
                            "pad_y": pad_y,
                            "center_x": overall - pad_x,
                        },
                        {
                            "pad_x": f"代码推导 LAND 组合B：({overall_item.raw}-{gap_item.raw})/2",
                            "pad_y": f"LAND 组合B：{pad_y_item.raw}",
                            "center_x": f"代码推导 LAND 组合B：{overall_item.raw}-pad_x",
                        },
                        {
                            "pad_x": (overall_item, gap_item),
                            "pad_y": (pad_y_item,),
                            "center_x": (overall_item, gap_item),
                        },
                    )
            else:
                pad_x = dimension_value_mm(pad_x_item)
                expected_overall = gap + 2.0 * pad_x
                if math.isclose(overall, expected_overall, rel_tol=0.0, abs_tol=0.02):
                    add_solution(
                        "C",
                        {
                            "pad_x": pad_x,
                            "pad_y": pad_y,
                            "center_x": gap + pad_x,
                        },
                        {
                            "pad_x": f"LAND 组合C：{pad_x_item.raw}",
                            "pad_y": f"LAND 组合C：{pad_y_item.raw}",
                            "center_x": f"LAND 组合C：{gap_item.raw}+{pad_x_item.raw}",
                        },
                        {
                            "pad_x": (pad_x_item,),
                            "pad_y": (pad_y_item,),
                            "center_x": (gap_item, pad_x_item),
                        },
                    )
                else:
                    self_check_failed = True
                    warnings.append(
                        "LAND 组合C 自校验失败："
                        f"总跨 {overall:.4g} != 间距 {gap:.4g} + 2×焊盘宽 {pad_x:.4g} "
                        "（容差 ±0.02 mm）。"
                    )

        # Unlabelled LAND groups use the model contract's spatial scan order.
        generic_group = all(_is_generic_land_group_item(item) for item in active_land)
        if generic_group and len(active_land) == 3:
            gap_item, pad_y_item, overall_item = active_land
            gap = dimension_value_mm(gap_item)
            pad_y = dimension_value_mm(pad_y_item)
            overall = dimension_value_mm(overall_item)
            if overall > gap:
                pad_x = (overall - gap) / 2.0
                add_solution(
                    "B",
                    {
                        "pad_x": pad_x,
                        "pad_y": pad_y,
                        "center_x": overall - pad_x,
                    },
                    {
                        "pad_x": f"代码推导 LAND 组合B：({overall_item.raw}-{gap_item.raw})/2",
                        "pad_y": f"LAND 组合B：第2项 {pad_y_item.raw}",
                        "center_x": f"代码推导 LAND 组合B：{overall_item.raw}-pad_x",
                    },
                    {
                        "pad_x": (overall_item, gap_item),
                        "pad_y": (pad_y_item,),
                        "center_x": (overall_item, gap_item),
                    },
                )
        elif generic_group and len(active_land) == 4:
            overall_item, pad_y_item, gap_item, pad_x_item = active_land
            overall = dimension_value_mm(overall_item)
            pad_y = dimension_value_mm(pad_y_item)
            gap = dimension_value_mm(gap_item)
            pad_x = dimension_value_mm(pad_x_item)
            expected_overall = gap + 2.0 * pad_x
            if math.isclose(overall, expected_overall, rel_tol=0.0, abs_tol=0.02):
                add_solution(
                    "C",
                    {
                        "pad_x": pad_x,
                        "pad_y": pad_y,
                        "center_x": gap + pad_x,
                    },
                    {
                        "pad_x": f"LAND 组合C：第4项 {pad_x_item.raw}",
                        "pad_y": f"LAND 组合C：第2项 {pad_y_item.raw}",
                        "center_x": f"LAND 组合C：{gap_item.raw}+{pad_x_item.raw}",
                    },
                    {
                        "pad_x": (pad_x_item,),
                        "pad_y": (pad_y_item,),
                        "center_x": (gap_item, pad_x_item),
                    },
                )
            else:
                self_check_failed = True
                warnings.append(
                    "LAND 组合C 自校验失败："
                    f"总跨 {overall:.4g} != 间距 {gap:.4g} + 2×焊盘宽 {pad_x:.4g} "
                    "（容差 ±0.02 mm）。"
                )

    status = "manual_select"
    combination: str | None = None
    message = "无法唯一确定，请指定；LAND 组原始数值已列出。"
    auto_pads = False
    auto_full = False
    body_source = "absent_requires_user_input"
    if paste_guard_status == "ambiguous":
        message = (
            "疑似含钢网开口尺寸：无法可靠区分 solder lands 与 paste，"
            "请从下列带归属标记的数值中人工点选。"
        )
    if not self_check_failed and len(solutions) == 1:
        combination, solved, solved_sources, solved_operands = solutions[0]
        values.update(solved)
        sources.update(solved_sources)
        body_axis_provenance = _resolve_body_axis_provenance(
            transcription,
            family,
            combination,
            values,
            sources,
            warnings,
            notation_ledger,
        )
        body_complete_before_decimal_guard = (
            values["body_x"] is not None and values["body_y"] is not None
        )
        decimal_extension_guard = _apply_decimal_extension_guard(
            transcription,
            values,
            sources,
            solved_operands,
            body_axis_provenance,
            page_text,
            warnings,
        )
        if body_complete_before_decimal_guard and (
            values["body_x"] is None or values["body_y"] is None
        ):
            body_axis_provenance = _invalidate_body_axis_provenance(
                body_axis_provenance,
                "轴向来源虽可证，但被小数末位延伸疑点闸拒绝。",
            )
        body_complete_before_existing_guards = (
            values["body_x"] is not None and values["body_y"] is not None
        )
        _apply_body_overall_cross_guard(transcription, values, sources, warnings)
        _apply_body_reasonableness_guard(family, values, sources, warnings)
        if body_complete_before_existing_guards and (
            values["body_x"] is None or values["body_y"] is None
        ):
            body_axis_provenance = _invalidate_body_axis_provenance(
                body_axis_provenance,
                "轴向来源虽可证，但被既有 body 合理性/overall 闸拒绝。",
            )
        asym3_axis_consistency = _evaluate_asym3_axis_consistency(family, values)
        asym3_axis_rejected = asym3_axis_consistency["status"] == "rejected"
        if asym3_axis_rejected:
            guard_inputs = asym3_axis_consistency["inputs_mm"]
            warnings.append(
                "ASYM3 轴向自洽闸拒绝当前指派："
                f"tab_y={guard_inputs['tab_spacing_axis']:.4g} mm < "
                f"pitch_y {guard_inputs['pitch']:.4g} mm + "
                f"pad_y {guard_inputs['pad_spacing_axis']:.4g} mm = "
                f"{asym3_axis_consistency['required_span_mm']:.4g} mm；"
                "整件降为人工核对，未尝试选择其他轴向。"
            )
        required_pads = ["pad_x", "pad_y", "center_x"]
        if family == "ASYM3":
            required_pads.extend(("pitch_y", "tab_x", "tab_y"))
        elif family in {"SOT3", "GRID4", "DUAL"}:
            required_pads.append("pitch_y")
        elif family == "QUAD_EP":
            required_pads.extend(
                (
                    "pitch_y",
                    "tab_x",
                    "tab_y",
                    "quad_left_count",
                    "quad_right_count",
                )
            )
            if values.get("quad_top_count"):
                required_pads.extend(
                    ("center_y", "pitch_x", "quad_top_count", "quad_bottom_count")
                )
        auto_pads = (
            all(values[field] is not None for field in required_pads)
            and not asym3_axis_rejected
        )
        document_body_complete = (
            values["body_x"] is not None and values["body_y"] is not None
        )
        if asym3_axis_rejected:
            body_source = (
                "document"
                if document_body_complete
                else "absent_requires_user_input"
            )
            status = "manual_select"
            message = (
                "ASYM3 当前轴向指派未通过确定性自洽闸，已整件降为人工核对；"
                "未自动改选其他轴向。"
            )
        elif auto_pads and document_body_complete:
            auto_full = True
            body_source = "document"
            status = "auto_unique"
            message = f"已按 LAND 组合{combination}自动预填；请核对后生成预览。"
        elif auto_pads:
            if values["body_x"] is None:
                sources["body_x"] = "本体尺寸未定，需人工填写"
            if values["body_y"] is None:
                sources["body_y"] = "本体尺寸未定，需人工填写"
            body_source = "absent_requires_user_input"
            status = "auto_pads"
            message = (
                f"已按 LAND 组合{combination}自动预填焊盘；"
                "本体尺寸未定，需人工填写；未绘制 F.Fab/F.SilkS 本体框。"
            )
            warnings.append("本体尺寸未定，需人工填写")
        elif family == "QUAD_EP" and quad_topology is not None and (
            quad_topology.get("status") == "partial"
        ):
            body_source = (
                "document"
                if document_body_complete
                else "absent_requires_user_input"
            )
            status = "partial_auto"
            message = (
                "QUAD/EP 已部分自动预填；中心距缺少仅由未限定 solder_land 值组成的"
                "唯一闭合链，须人工填写并核对后才能生成预览。"
            )
        else:
            body_source = (
                "document"
                if document_body_complete
                else "absent_requires_user_input"
            )
            message = "LAND 几何未完整求解；请人工补齐后核对。"
    elif len(solutions) > 1:
        warnings.append("LAND 数值产生多个不同解，已停止自动预填。")
    elif not land_dimensions:
        warnings.append("模型结果中未识别到 LAND PATTERN 分组。")

    if body_axis_provenance is None:
        body_axis_provenance = _resolve_body_axis_provenance(
            transcription,
            family,
            None,
            values,
            sources,
            warnings,
            notation_ledger,
        )
        body_complete_before_decimal_guard = (
            values["body_x"] is not None and values["body_y"] is not None
        )
        decimal_extension_guard = _apply_decimal_extension_guard(
            transcription,
            values,
            sources,
            {},
            body_axis_provenance,
            page_text,
            warnings,
        )
        if body_complete_before_decimal_guard and (
            values["body_x"] is None or values["body_y"] is None
        ):
            body_axis_provenance = _invalidate_body_axis_provenance(
                body_axis_provenance,
                "轴向来源虽可证，但被小数末位延伸疑点闸拒绝。",
            )

    if transcription.pin_count is not None:
        if family in {"DUAL", "QUAD_EP"}:
            expected_counts = {transcription.pin_count}
            if family == "DUAL":
                count_matches = (
                    values.get("dual_left_count") is not None
                    and values.get("dual_right_count") is not None
                    and int(values["dual_left_count"])
                    + int(values["dual_right_count"])
                    == transcription.pin_count
                )
            else:
                count_matches = sum(
                    int(values.get(field) or 0)
                    for field in (
                        "quad_left_count",
                        "quad_right_count",
                        "quad_top_count",
                        "quad_bottom_count",
                    )
                ) == transcription.pin_count
        else:
            expected_counts = (
                {2, 3}
                if family == "ASYM3"
                else (
                    {3}
                    if family in {"SOT3", "INLINE3"}
                    else ({4} if family == "GRID4" else {2})
                )
            )
            count_matches = transcription.pin_count in expected_counts
        if not count_matches:
            expected_text = "/".join(str(value) for value in sorted(expected_counts))
            warnings.append(
                f"模型 pin_count={transcription.pin_count}，与所选族要求 {expected_text} 不一致。"
            )
    if values["body_x"] is None or values["body_y"] is None:
        values["body_x"] = None
        values["body_y"] = None
        sources["body_x"] = "本体尺寸未定，需人工填写"
        sources["body_y"] = "本体尺寸未定，需人工填写"
        body_source = "absent_requires_user_input"
        auto_full = False
    elif body_axis_provenance.get("status") == "proven":
        # Body evidence is independent of LAND.  Preserve that provenance in
        # the source label even when pads remain incomplete and the form must
        # stay editable for manual pad completion.
        body_source = "document"
    return GeometryPrefillResult(
        values=values,
        sources=sources,
        warnings=tuple(warnings),
        status=status,
        combination=combination,
        message=message,
        land_dimensions=land_dimensions,
        excluded_half_dimensions=excluded_half,
        paste_dimensions=paste_dimensions,
        unknown_pattern_dimensions=unknown_pattern_dimensions,
        paste_guard_status=paste_guard_status,
        body_source=body_source,
        auto_pads=auto_pads,
        auto_full=auto_full,
        relation_candidates=relation_records,
        body_axis_provenance=body_axis_provenance,
        asym3_axis_consistency=asym3_axis_consistency,
        decimal_extension_guard=decimal_extension_guard,
    )


def infer_geometry_fields(
    transcription: Transcription,
    family: str,
) -> tuple[dict[str, float | None], dict[str, str], list[str]]:
    result = infer_geometry_prefill(transcription, family)
    return result.values, result.sources, list(result.warnings)


def geometry_from_fields(
    family: str,
    fields: dict[str, Any],
    *,
    require_all: bool = False,
    body_source: str | None = None,
) -> FootprintGeometry:
    parsed: dict[str, float] = {}
    small_pad_count = 2
    tab_pad_number = 2
    dual_left_count = 0
    dual_right_count = 0
    quad_left_count = 0
    quad_right_count = 0
    quad_top_count = 0
    quad_bottom_count = 0
    if family == "ASYM3":
        small_pad_count_raw = str(fields.get("small_pad_count", "2")).strip()
        tab_pad_number_raw = str(fields.get("tab_pad_number", "2")).strip()
        try:
            small_pad_count = int(small_pad_count_raw)
            tab_pad_number = int(tab_pad_number_raw)
        except ValueError as exc:
            raise E1aError("ASYM3 焊盘数量与大焊盘编号必须是整数。") from exc
    elif family == "DUAL":
        try:
            dual_left_count = int(str(fields.get("dual_left_count", "")).strip())
            dual_right_count = int(str(fields.get("dual_right_count", "")).strip())
        except ValueError as exc:
            raise E1aError("DUAL 左右排焊盘数必须是整数。") from exc
    elif family == "QUAD_EP":
        try:
            quad_left_count = int(str(fields.get("quad_left_count", "")).strip())
            quad_right_count = int(str(fields.get("quad_right_count", "")).strip())
            quad_top_count = int(str(fields.get("quad_top_count", "0")).strip())
            quad_bottom_count = int(str(fields.get("quad_bottom_count", "0")).strip())
        except ValueError as exc:
            raise E1aError("QUAD/EP 四边焊盘数必须是整数。") from exc

    def required(name: str) -> float:
        if name in parsed:
            return parsed[name]
        raw = str(fields.get(name, "")).strip().replace(",", ".")
        if not raw:
            raise E1aError(f"缺少几何字段：{name}")
        try:
            value = float(raw)
        except ValueError as exc:
            raise E1aError(f"{name} 不是有效数值：{raw!r}") from exc
        if not math.isfinite(value) or value <= 0:
            raise E1aError(f"{name} 必须大于 0。")
        parsed[name] = value
        return value

    if require_all:
        required_fields = ["pad_x", "pad_y", "center_x"]
        if family == "ASYM3":
            required_fields.extend(("tab_x", "tab_y"))
            if small_pad_count == 2:
                required_fields.append("pitch_y")
        elif family in {"SOT3", "GRID4", "DUAL"}:
            required_fields.append("pitch_y")
        elif family == "QUAD_EP":
            required_fields.extend(("pitch_y", "tab_x", "tab_y"))
            if quad_top_count or quad_bottom_count:
                required_fields.extend(("center_y", "pitch_x"))
        for field in required_fields:
            required(field)

    pad_x = required("pad_x")
    pad_y = required("pad_y")
    center_x = required("center_x")
    center_y = (
        required("center_y")
        if family == "QUAD_EP" and (quad_top_count or quad_bottom_count)
        else 0.0
    )
    pitch_y = (
        required("pitch_y")
        if family in {"SOT3", "GRID4", "DUAL", "QUAD_EP"}
        or (family == "ASYM3" and small_pad_count == 2)
        else 0.0
    )
    pitch_x = (
        required("pitch_x")
        if family == "QUAD_EP" and (quad_top_count or quad_bottom_count)
        else 0.0
    )
    tab_x = required("tab_x") if family in {"ASYM3", "QUAD_EP"} else 0.0
    tab_y = required("tab_y") if family in {"ASYM3", "QUAD_EP"} else 0.0

    body_x_raw = str(fields.get("body_x", "")).strip().replace(",", ".")
    body_y_raw = str(fields.get("body_y", "")).strip().replace(",", ".")
    body_x = required("body_x") if body_x_raw else None
    body_y = required("body_y") if body_y_raw else None
    body_complete = body_x is not None and body_y is not None
    effective_body_source = (
        body_source
        if body_complete
        else "absent_requires_user_input"
    )

    geometry = FootprintGeometry(
        family=family,
        pad_x=pad_x,
        pad_y=pad_y,
        center_x=center_x,
        pitch_y=pitch_y,
        body_x=body_x,
        body_y=body_y,
        body_source=effective_body_source
        or ("manual" if require_all else "document"),
        tab_x=tab_x,
        tab_y=tab_y,
        small_pad_count=small_pad_count,
        tab_pad_number=tab_pad_number,
        dual_left_count=dual_left_count,
        dual_right_count=dual_right_count,
        center_y=center_y,
        pitch_x=pitch_x,
        quad_left_count=quad_left_count,
        quad_right_count=quad_right_count,
        quad_top_count=quad_top_count,
        quad_bottom_count=quad_bottom_count,
    )
    geometry.validate()
    return geometry


def half_double_pairs(fields: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, Decimal]] = []
    for name in MANUAL_GEOMETRY_FIELDS:
        raw = str(fields.get(name, "")).strip().replace(",", ".")
        if not raw:
            continue
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        if not value.is_finite() or value <= 0:
            continue
        values.append((name, value))

    pairs: list[tuple[str, str]] = []
    for index, (left_name, left_value) in enumerate(values):
        for right_name, right_value in values[index + 1 :]:
            if left_value == right_value * 2 or right_value == left_value * 2:
                pairs.append((left_name, right_name))
    return tuple(pairs)


def _provenance_source_key(
    field_name: str,
    raw_value: Any,
    body_axis_provenance: dict[str, Any] | None,
) -> tuple[str, str] | None:
    if not isinstance(body_axis_provenance, dict):
        return None
    assignments = body_axis_provenance.get("assignments")
    if not isinstance(assignments, dict):
        return None
    record = assignments.get(field_name)
    if not isinstance(record, dict) or record.get("status") != "proven":
        return None

    source_axis = as_text(record.get("source_axis")).strip().casefold()
    source_symbol = as_text(record.get("source_symbol")).strip().casefold()
    if not source_axis or not source_symbol:
        return None

    try:
        current_value = Decimal(str(raw_value).strip().replace(",", "."))
        proven_value = Decimal(str(record.get("value_mm")))
    except InvalidOperation:
        return None
    if current_value != proven_value:
        return None
    return source_axis, source_symbol


def half_double_warning_pairs(
    fields: dict[str, Any],
    body_axis_provenance: dict[str, Any] | None,
) -> tuple[tuple[str, str], ...]:
    warning_pairs: list[tuple[str, str]] = []
    for left_name, right_name in half_double_pairs(fields):
        left_source = _provenance_source_key(
            left_name, fields.get(left_name, ""), body_axis_provenance
        )
        right_source = _provenance_source_key(
            right_name, fields.get(right_name, ""), body_axis_provenance
        )
        if left_source is not None and right_source is not None:
            if left_source != right_source:
                continue
        warning_pairs.append((left_name, right_name))
    return tuple(warning_pairs)


def validate_footprint_name(name: str) -> str:
    candidate = name.strip()
    if not _FOOTPRINT_NAME_RE.fullmatch(candidate):
        raise E1aError(
            "Footprint 名称必须以字母或数字开头，且只含字母、数字、点、下划线、加号或连字符。"
        )
    return candidate


def _fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _thermal_paste_size(pad: Pad) -> tuple[float, float]:
    """Scale both EP axes equally so one centered aperture covers exactly 70%."""
    if not pad.is_thermal:
        raise E1aError("只有中央热焊盘可以使用热焊盘焊膏缩小规则。")
    width = float(pad.size_x)
    height = float(pad.size_y)
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise E1aError("热焊盘尺寸必须是大于 0 的有限数值。")
    ratio = THERMAL_PASTE_AREA_RATIO
    scale = math.sqrt(ratio)
    paste_width = width * scale
    paste_height = height * scale
    if paste_width <= 0 or paste_height <= 0:
        raise E1aError("热焊盘 70% 焊膏开口计算结果非正。")
    actual_ratio = paste_width * paste_height / (width * height)
    if not math.isclose(actual_ratio, ratio, rel_tol=0.0, abs_tol=1e-9):
        raise E1aError("热焊盘焊膏面积未达到 70%。")
    if not math.isclose(
        paste_width / paste_height,
        width / height,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise E1aError("热焊盘焊膏开口未保持 EP 长宽比。")
    return paste_width, paste_height


def _validated_output_pads(
    geometry: FootprintGeometry,
    thermal_pad: Pad | None,
) -> tuple[Pad, ...]:
    pads = geometry.pads()
    if geometry.family == "QUAD_EP":
        generated_thermal = Pad(
            "EP", 0.0, 0.0, geometry.tab_x, geometry.tab_y, is_thermal=True
        )
        if thermal_pad is None:
            thermal_pad = generated_thermal
        elif thermal_pad != generated_thermal:
            raise E1aError("QUAD/EP 中央焊盘必须与已确认 tab_x/tab_y 完全一致。")
    if thermal_pad is None:
        return pads
    if not thermal_pad.is_thermal:
        raise E1aError("附加焊盘必须明确标记为中央热焊盘。")
    if any(pad.number == thermal_pad.number for pad in pads):
        raise E1aError("中央热焊盘编号不得与端子焊盘重复。")
    _thermal_paste_size(thermal_pad)
    return pads + (thermal_pad,)


def _pad_bounds(
    geometry: FootprintGeometry,
    thermal_pad: Pad | None = None,
) -> tuple[float, float, float, float]:
    pads = _validated_output_pads(geometry, thermal_pad)
    min_x = min(pad.x - pad.size_x / 2 for pad in pads)
    max_x = max(pad.x + pad.size_x / 2 for pad in pads)
    min_y = min(pad.y - pad.size_y / 2 for pad in pads)
    max_y = max(pad.y + pad.size_y / 2 for pad in pads)
    return min_x, min_y, max_x, max_y


def _fab_bounds(
    geometry: FootprintGeometry,
) -> tuple[float, float, float, float] | None:
    if geometry.body_x is None or geometry.body_y is None:
        return None
    return (
        -geometry.body_x / 2,
        -geometry.body_y / 2,
        geometry.body_x / 2,
        geometry.body_y / 2,
    )


def _bounds(
    geometry: FootprintGeometry,
    thermal_pad: Pad | None = None,
) -> tuple[float, float, float, float]:
    pad_min_x, pad_min_y, pad_max_x, pad_max_y = _pad_bounds(geometry, thermal_pad)
    fab = _fab_bounds(geometry)
    if fab is None:
        return pad_min_x, pad_min_y, pad_max_x, pad_max_y
    fab_min_x, fab_min_y, fab_max_x, fab_max_y = fab
    min_x = min(pad_min_x, fab_min_x)
    max_x = max(pad_max_x, fab_max_x)
    min_y = min(pad_min_y, fab_min_y)
    max_y = max(pad_max_y, fab_max_y)
    return min_x, min_y, max_x, max_y


def build_kicad_mod(
    name: str,
    geometry: FootprintGeometry,
    *,
    source_sha256: str = "",
    anchor_status: str = "",
    thermal_pad: Pad | None = None,
) -> str:
    name = validate_footprint_name(name)
    geometry.validate()
    pads = _validated_output_pads(geometry, thermal_pad)
    min_x, min_y, max_x, max_y = _bounds(geometry, thermal_pad)
    courtyard = (min_x - 0.25, min_y - 0.25, max_x + 0.25, max_y + 0.25)
    fab = _fab_bounds(geometry)
    silk_margin = 0.15
    silk = (
        (
            fab[0] - silk_margin,
            fab[1] - silk_margin,
            fab[2] + silk_margin,
            fab[3] + silk_margin,
        )
        if fab is not None
        else None
    )
    source_note = source_sha256[:16] if source_sha256 else "unknown"
    anchor_note = re.sub(r"[^a-z0-9_.-]+", "_", anchor_status.strip().lower())[:40]
    anchor_descr = f"; anchor_status={anchor_note}" if anchor_note else ""
    body_source_note = re.sub(
        r"[^a-z0-9_.-]+", "_", geometry.body_source.strip().lower()
    )[:40]
    ep_paste_descr = (
        "; ep_paste=single_centered_70pct_provisional"
        if geometry.family == "QUAD_EP"
        else ""
    )
    reference_y = courtyard[1] - 1.0
    value_y = courtyard[3] + 1.0
    reference_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"e1a:{name}:Reference")
    value_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"e1a:{name}:Value")
    lines = [
        f'(footprint "{name}"',
        "  (version 20240108)",
        '  (generator "e1a_minimal_loop")',
        '  (layer "F.Cu")',
        f'  (descr "E1a user-confirmed preview; family={geometry.family}; source_sha256={source_note}{anchor_descr}; body_source={body_source_note}{ep_paste_descr}")',
        "  (attr smd)",
        '  (property "Reference" "REF**"',
        f'    (at 0 {_fmt(reference_y)} 0)',
        '    (layer "F.SilkS")',
        f'    (uuid "{reference_uuid}")',
        '    (effects (font (size 1 1) (thickness 0.15))))',
        f'  (property "Value" "{name}"',
        f'    (at 0 {_fmt(value_y)} 0)',
        '    (layer "F.Fab")',
        f'    (uuid "{value_uuid}")',
        '    (effects (font (size 1 1) (thickness 0.15))))',
    ]
    if fab is not None:
        lines.extend(
            [
                f'  (fp_rect (start {_fmt(fab[0])} {_fmt(fab[1])}) (end {_fmt(fab[2])} {_fmt(fab[3])})',
                '    (stroke (width 0.1) (type default)) (fill none) (layer "F.Fab"))',
            ]
        )
    if silk is not None and geometry.family == "ASYM3":
        clearance = 0.2

        def visible_ranges(
            start: float,
            end: float,
            blocked: list[tuple[float, float]],
        ) -> list[tuple[float, float]]:
            ranges = [(start, end)]
            for blocked_start, blocked_end in sorted(blocked):
                next_ranges: list[tuple[float, float]] = []
                for range_start, range_end in ranges:
                    if blocked_end <= range_start or blocked_start >= range_end:
                        next_ranges.append((range_start, range_end))
                        continue
                    if blocked_start > range_start:
                        next_ranges.append((range_start, blocked_start))
                    if blocked_end < range_end:
                        next_ranges.append((blocked_end, range_end))
                ranges = next_ranges
            return [item for item in ranges if item[1] - item[0] >= 0.25]

        for y in (silk[1], silk[3]):
            blocked = [
                (
                    pad.x - pad.size_x / 2 - clearance,
                    pad.x + pad.size_x / 2 + clearance,
                )
                for pad in pads
                if pad.y - pad.size_y / 2 - clearance <= y
                <= pad.y + pad.size_y / 2 + clearance
            ]
            for x1, x2 in visible_ranges(silk[0], silk[2], blocked):
                lines.extend(
                    [
                        f'  (fp_line (start {_fmt(x1)} {_fmt(y)}) (end {_fmt(x2)} {_fmt(y)})',
                        '    (stroke (width 0.12) (type default)) (layer "F.SilkS"))',
                    ]
                )
        for x in (silk[0], silk[2]):
            blocked = [
                (
                    pad.y - pad.size_y / 2 - clearance,
                    pad.y + pad.size_y / 2 + clearance,
                )
                for pad in pads
                if pad.x - pad.size_x / 2 - clearance <= x
                <= pad.x + pad.size_x / 2 + clearance
            ]
            for y1, y2 in visible_ranges(silk[1], silk[3], blocked):
                lines.extend(
                    [
                        f'  (fp_line (start {_fmt(x)} {_fmt(y1)}) (end {_fmt(x)} {_fmt(y2)})',
                        '    (stroke (width 0.12) (type default)) (layer "F.SilkS"))',
                    ]
                )
    elif silk is not None:
        lines.extend(
            [
                f'  (fp_rect (start {_fmt(silk[0])} {_fmt(silk[1])}) (end {_fmt(silk[2])} {_fmt(silk[3])})',
                '    (stroke (width 0.12) (type default)) (fill none) (layer "F.SilkS"))',
            ]
        )
    lines.extend(
        [
            f'  (fp_rect (start {_fmt(courtyard[0])} {_fmt(courtyard[1])}) (end {_fmt(courtyard[2])} {_fmt(courtyard[3])})',
            '    (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))',
        ]
    )
    pin1 = next((pad for pad in pads if pad.number == "1"), pads[0])
    marker_x = pin1.x - pin1.size_x / 2 - 0.22
    marker_y = pin1.y - pin1.size_y / 2 - 0.22
    lines.extend(
        [
            f'  (fp_circle (center {_fmt(marker_x)} {_fmt(marker_y)}) (end {_fmt(marker_x + 0.12)} {_fmt(marker_y)})',
            '    (stroke (width 0.1) (type default)) (fill solid) (layer "F.Fab"))',
        ]
    )
    for pad in pads:
        if pad.is_thermal:
            paste_width, paste_height = _thermal_paste_size(pad)
            lines.extend(
                [
                    f'  (pad "{pad.number}" smd roundrect',
                    f'    (at {_fmt(pad.x)} {_fmt(pad.y)})',
                    f'    (size {_fmt(pad.size_x)} {_fmt(pad.size_y)})',
                    '    (layers "F.Cu" "F.Mask")',
                    f'    (solder_mask_margin {_fmt(ENTERPRISE_SOLDER_MASK_MARGIN)})',
                    "    (roundrect_rratio 0.15))",
                    '  (pad "" smd roundrect',
                    f'    (at {_fmt(pad.x)} {_fmt(pad.y)})',
                    f'    (size {_fmt(paste_width)} {_fmt(paste_height)})',
                    '    (layers "F.Paste")',
                    "    (roundrect_rratio 0.15))",
                ]
            )
        else:
            lines.extend(
                [
                    f'  (pad "{pad.number}" smd roundrect',
                    f'    (at {_fmt(pad.x)} {_fmt(pad.y)})',
                    f'    (size {_fmt(pad.size_x)} {_fmt(pad.size_y)})',
                    '    (layers "F.Cu" "F.Paste" "F.Mask")',
                    f'    (solder_mask_margin {_fmt(ENTERPRISE_SOLDER_MASK_MARGIN)})',
                    f'    (solder_paste_margin {_fmt(ENTERPRISE_SOLDER_PASTE_MARGIN)})',
                    "    (roundrect_rratio 0.15))",
                ]
            )
    lines.append(")")
    return "\n".join(lines) + "\n"


def write_candidate(
    path: Path,
    name: str,
    geometry: FootprintGeometry,
    *,
    source_sha256: str = "",
    anchor_status: str = "",
    thermal_pad: Pad | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = build_kicad_mod(
        name,
        geometry,
        source_sha256=source_sha256,
        anchor_status=anchor_status,
        thermal_pad=thermal_pad,
    )
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def commit_candidate(
    candidate_path: Path,
    library_dir: Path,
    name: str,
    *,
    confirmed: bool,
    overwrite: bool = False,
) -> Path:
    if not confirmed:
        raise E1aError("未确认，禁止写入 footprint 库。")
    name = validate_footprint_name(name)
    if candidate_path.suffix.lower() != ".kicad_mod" or not candidate_path.is_file():
        raise E1aError("候选 footprint 文件不存在或扩展名不正确。")
    library_dir = library_dir.expanduser().resolve()
    if library_dir.suffix.lower() != ".pretty":
        raise E1aError("目标库目录必须以 .pretty 结尾。")
    library_dir.mkdir(parents=True, exist_ok=True)
    target = (library_dir / f"{name}.kicad_mod").resolve()
    if target.parent != library_dir:
        raise E1aError("目标路径越过所选 footprint 库。")
    if target.exists() and not overwrite:
        raise E1aError("目标 footprint 已存在；需要明确选择覆盖。")
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{name}.", suffix=".tmp", dir=library_dir, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(candidate_path.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)
    return target


def render_footprint_png(
    geometry: FootprintGeometry,
    output_path: Path,
    *,
    name: str,
    width: int = 1100,
    height: int = 720,
    thermal_pad: Pad | None = None,
) -> Path:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise E1aError("缺少 Pillow，无法渲染 footprint 预览。") from exc

    geometry.validate()
    min_x, min_y, max_x, max_y = _bounds(geometry, thermal_pad)
    margin_mm = max(0.8, (max(max_x - min_x, max_y - min_y)) * 0.18)
    min_x -= margin_mm
    max_x += margin_mm
    min_y -= margin_mm
    max_y += margin_mm
    usable_width = width - 90
    usable_height = height - 130
    scale = min(usable_width / (max_x - min_x), usable_height / (max_y - min_y))

    def point(x: float, y: float) -> tuple[int, int]:
        px = 45 + int(round((x - min_x) * scale))
        py = 75 + int(round((y - min_y) * scale))
        return px, py

    image = Image.new("RGB", (width, height), "#F7F8FA")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("arial.ttf", 24)
        label_font = ImageFont.truetype("arial.ttf", 18)
        small_font = ImageFont.truetype("arial.ttf", 15)
    except OSError:
        title_font = label_font = small_font = ImageFont.load_default()

    grid_step = 0.5
    x_start = math.floor(min_x / grid_step) * grid_step
    y_start = math.floor(min_y / grid_step) * grid_step
    x = x_start
    while x <= max_x + 1e-9:
        x1, y1 = point(x, min_y)
        x2, y2 = point(x, max_y)
        draw.line((x1, y1, x2, y2), fill="#DDE2E7", width=1)
        x += grid_step
    y = y_start
    while y <= max_y + 1e-9:
        x1, y1 = point(min_x, y)
        x2, y2 = point(max_x, y)
        draw.line((x1, y1, x2, y2), fill="#DDE2E7", width=1)
        y += grid_step

    axis_left, axis_y = point(min_x, 0)
    axis_right, _ = point(max_x, 0)
    axis_x, axis_bottom = point(0, min_y)
    _, axis_top = point(0, max_y)
    draw.line((axis_left, axis_y, axis_right, axis_y), fill="#AAB3BC", width=2)
    draw.line((axis_x, axis_bottom, axis_x, axis_top), fill="#AAB3BC", width=2)

    fab = _fab_bounds(geometry)
    if fab is not None:
        fab_min_x, fab_min_y, fab_max_x, fab_max_y = fab
        body_tl = point(fab_min_x, fab_min_y)
        body_br = point(fab_max_x, fab_max_y)
        draw.rectangle((*body_tl, *body_br), outline="#2563A6", width=4)

    pads = _validated_output_pads(geometry, thermal_pad)
    for pad in pads:
        tl = point(pad.x - pad.size_x / 2, pad.y - pad.size_y / 2)
        br = point(pad.x + pad.size_x / 2, pad.y + pad.size_y / 2)
        radius = max(2, int(min(br[0] - tl[0], br[1] - tl[1]) * 0.12))
        fill = "#92400E" if pad.is_thermal else "#D97706"
        outline = "#451A03" if pad.is_thermal else "#7C2D12"
        draw.rounded_rectangle((*tl, *br), radius=radius, fill=fill, outline=outline, width=3)
        if pad.is_thermal:
            paste_width, paste_height = _thermal_paste_size(pad)
            paste_tl = point(
                pad.x - paste_width / 2.0, pad.y - paste_height / 2.0
            )
            paste_br = point(
                pad.x + paste_width / 2.0, pad.y + paste_height / 2.0
            )
            paste_radius = max(
                2,
                int(
                    min(
                        paste_br[0] - paste_tl[0],
                        paste_br[1] - paste_tl[1],
                    )
                    * 0.12
                ),
            )
            draw.rounded_rectangle(
                (*paste_tl, *paste_br),
                radius=paste_radius,
                fill="#F59E0B",
                outline="#FFF7ED",
                width=3,
            )
        bbox = draw.textbbox((0, 0), pad.number, font=label_font)
        tx = (tl[0] + br[0] - (bbox[2] - bbox[0])) // 2
        ty = (tl[1] + br[1] - (bbox[3] - bbox[1])) // 2
        draw.text((tx, ty), pad.number, fill="#FFFFFF", font=label_font)

    pin1 = next((pad for pad in pads if pad.number == "1"), pads[0])
    marker = point(pin1.x - pin1.size_x / 2 - 0.18, pin1.y - pin1.size_y / 2 - 0.18)
    draw.ellipse((marker[0] - 6, marker[1] - 6, marker[0] + 6, marker[1] + 6), fill="#111827")
    draw.text((30, 20), f"{name}  |  {geometry.family}", fill="#111827", font=title_font)
    details = (
        f"pad={_fmt(geometry.pad_x)} x {_fmt(geometry.pad_y)} mm   "
        f"center_x={_fmt(geometry.center_x)} mm"
    )
    if geometry.family in {"SOT3", "GRID4", "DUAL", "QUAD_EP"} or (
        geometry.family == "ASYM3" and geometry.small_pad_count == 2
    ):
        details += f"   pitch_y={_fmt(geometry.pitch_y)} mm"
    if geometry.family == "ASYM3":
        details += (
            f"   tab={_fmt(geometry.tab_x)} x {_fmt(geometry.tab_y)} mm"
            f"   small_pads={geometry.small_pad_count}"
        )
    if geometry.family == "DUAL":
        details += (
            f"   rows={geometry.dual_left_count}+{geometry.dual_right_count}"
        )
    if geometry.family == "QUAD_EP":
        details += (
            f"   rows={geometry.quad_left_count}+{geometry.quad_right_count}+"
            f"{geometry.quad_top_count}+{geometry.quad_bottom_count}"
            f"   EP={_fmt(geometry.tab_x)} x {_fmt(geometry.tab_y)}"
            "   EP paste=70% single (provisional)"
        )
    draw.text((30, height - 42), details, fill="#374151", font=small_font)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return output_path


class RunArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def start(
        self,
        *,
        source_path: Path,
        source_image: Path,
        page: int,
        family: str,
        mode: str,
        model_name: str = MODEL_NAME,
    ) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = self.root / f"{stamp}_{uuid.uuid4().hex[:8]}"
        run_dir.mkdir(parents=True, exist_ok=False)
        copied_image = run_dir / "source_page.png"
        shutil.copy2(source_image, copied_image)
        self.write_json(
            run_dir / "request_meta.json",
            {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "model": model_name,
                "prompt_version": PROMPT_VERSION,
                "mode": mode,
                "source_path": str(source_path),
                "source_sha256": sha256_file(source_path),
                "page": page,
                "family": family,
                "source_image_sha256": sha256_file(copied_image),
            },
        )
        self.write_json(run_dir / "status.json", {"stage": "started", "ok": None})
        return run_dir

    @staticmethod
    def write_json(path: Path, payload: Any) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def update_status(self, run_dir: Path, **changes: Any) -> None:
        path = run_dir / "status.json"
        current: dict[str, Any] = {}
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current.update(loaded)
        current.update(changes)
        current["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.write_json(path, current)

SYSTEM_PROMPT = """你读取用户指定的封装外形图或推荐焊盘图，逐项转录图上尺寸并说明几何角色。
你不是 footprint 生成器。禁止计算、换算、补值、取均值、从总跨反推焊盘、合并 min/max，
禁止生成 footprint、文件、代码或任何图上没有的数字。

每条 dimensions 只对应图上一个直接可见的单个数字：
- value 必须是图片上原样连续出现的单个数字字符串，保留尾随零；raw 必须逐字包含 value。
- A±B 或 A+x−y 只输出一条尺寸，value 取标称 A，raw 保留完整表达式；
  禁止把公差 B、x、y 再单独输出成尺寸。
- 同一物理标注同时给 mm 与 inch 时只输出 mm；不得把换算单位重复成两个候选。
- symbol 照抄尺寸符号；没有局部符号时照抄所属图题或数字旁短标签。
- role 说明该标注线的几何角色；belongs_to 说明它属于铜箔焊盘、焊膏、本体外形或占用区。
- endpoints 用自然语言具体说明标注线/箭头两端分别落在哪里，例如“左焊盘右缘 -> 右焊盘左缘”。
- is_derived 对图上直接标注的数字必须为 false。不要输出推导数字；若确有非图示推断，
  is_derived=true 且不得伪装为图示尺寸。

role 只能从以下枚举选择：pad_width, pad_height, pad_gap, pad_center_distance,
overall_span, body_length, body_width, body_height, tab_width, tab_height, half_pitch,
half_pitch_from_centerline, center_to_edge, gap_between_pads, pitch, paste_width,
paste_height, lead_width, other。
belongs_to 只能从 solder_land, solder_paste, package_outline, occupied_area, other 选择。
铜箔焊盘/solder land 与 solder paste/stencil/paste opening 必须按图例、纹理、引线或文字区分，
禁止按数值大小猜；paste 的宽高分别用 paste_width/paste_height。
half_pitch/half_pitch_from_centerline 只有标注明确从封装或图形中心线到单个焊盘中心线时才用；
中心线到焊盘边缘用 center_to_edge；两焊盘中心线之间用 pitch 或 pad_center_distance；
边缘到边缘用 pad_gap 或 gap_between_pads；总外缘到总外缘用 overall_span。
若现有枚举无法准确表达，必须用 other 并在 endpoints 中完整说明两端，不得塞入近似角色。
大焊盘/tab/散热焊盘的宽高用 tab_width/tab_height；小焊盘/端子焊盘用
pad_width/pad_height。endpoints 必须明确写“大焊盘”或“小焊盘/端子焊盘”。

判定 endpoints 时必须沿该数字对应的延长线和箭头一直追到实际端点，不得按数字靠近哪个图形猜。
虚线/点划线的对称中心线本身可以是端点：从布局中心线到一个小焊盘中心线必须用
half_pitch_from_centerline，不得写成 gap_between_pads。gap_between_pads 仅限两个焊盘相向边缘之间。
推荐焊盘图中每一条直接可见的尺寸都必须转录，包括 MIN./NOM./REF. 的总跨、中心距、半间距；
返回前逐条清点焊盘图的尺寸箭头，不能只抄焊盘宽高而漏掉跨距。

图上没有直接标注、但对建封装有意义的角色，放入 unmarked_roles；不得为它编造 dimension。
pin_count 仅在图上明确标注或焊盘/引脚可清楚逐个数出时填写，否则 null。
recommended_land_pattern 仅按标题或图注判断。LAND 图内保持空间阅读顺序，不按大小重排。
family_proposal 只能从 CHIP、SMX、SOT3、ASYM3、other 中提名；它只是候选，不能替代
焊盘几何裁决。family_proposal_reason 用一句话说明你看到的布局（例如“两个相近尺寸的
solder land，左右对称”）；不要写模型自信度，不要把提名当作确定结论。
看不清的内容不输出。只返回符合 JSON Schema 的对象，不返回 Markdown。"""

USER_PROMPT = """读取这张图。逐项返回图上直接可见的尺寸数字、几何角色、归属和标注线两端。
特别区分：焊盘宽/高、焊盘边缘间距、中心距、总跨、半间距、本体长宽高、大焊盘尺寸，
以及 solder lands 与 solder paste。图上未标的角色写入 unmarked_roles，不得计算补出。
另外只提名一个 family_proposal（CHIP/SMX/SOT3/ASYM3/other）并用一句话描述焊盘布局；
族最终由程序根据 solder_land 几何裁决，你不得凭自信度定案。
不得做算术，不得把 min/max/公差合并，不得生成 footprint。"""

FORMAT_REPAIR_PROMPT = """上一次 JSON 未通过本地字段契约校验：{error}
只修正 JSON 字段自洽，不得重新识图、计算、换算、补值、增删或合并尺寸。
每条 value 必须是对应 raw 中连续出现且完全相同的单个数字子串。
如果正负号与数字被版面空白隔开，value 只取 raw 中连续可见的数字部分。
family_proposal 必须是 CHIP/SMX/SOT3/ASYM3/other，且 family_proposal_reason 必须是一句
可见焊盘布局判据；这两个字段只作模型提名，不得改动尺寸数字。
仍然只返回符合既定 JSON Schema 的 JSON 对象。"""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "dimensions",
        "pin_count",
        "recommended_land_pattern",
        "paste_evidence_visible",
        "unmarked_roles",
        "family_proposal",
        "family_proposal_reason",
    ],
    "properties": {
        "dimensions": {
            "type": "array",
            "minItems": 1,
            "items": {
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
                        "enum": sorted(GEOMETRY_ROLES),
                    },
                    "belongs_to": {
                        "type": "string",
                        "enum": sorted(DIMENSION_BELONGS_TO),
                    },
                    "endpoints": {"type": "string", "minLength": 1},
                    "is_derived": {"type": "boolean"},
                },
            },
        },
        "pin_count": {"type": ["integer", "null"], "minimum": 1},
        "recommended_land_pattern": {"type": "boolean"},
        "paste_evidence_visible": {"type": "boolean"},
        "unmarked_roles": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(GEOMETRY_ROLES)},
        },
        "family_proposal": {
            "type": "string",
            "enum": sorted(FAMILY_PROPOSALS),
        },
        "family_proposal_reason": {
            "type": "string",
            "minLength": 1,
        },
    },
}


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    api_key: str = ""
    timeout_seconds: float = 150.0
    allow_insecure_http: bool = False
    model_name: str = MODEL_NAME

    def validate(self) -> None:
        if not self.model_name.strip():
            raise E1aError("模型名称不能为空。")
        value = self.base_url.strip()
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise E1aError("模型 Base URL 必须是完整的 http(s) URL。")
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        if (
            self.api_key.strip()
            and parsed.scheme != "https"
            and (parsed.hostname or "").lower() not in local_hosts
            and not self.allow_insecure_http
        ):
            raise E1aError(
                "携带 API Key 的非本机模型连接必须使用 HTTPS；"
                "如已接受风险，请在连接设置中明确允许远程 HTTP。"
            )


@dataclass(frozen=True)
class ModelResult:
    transcription: Transcription
    raw_response: dict[str, Any]
    request_id: str
    mode: str
    sample_transcriptions: tuple[Transcription, ...] = ()
    sample_raw_responses: tuple[dict[str, Any], ...] = ()
    sample_request_ids: tuple[str, ...] = ()
    consensus_stats: dict[str, Any] | None = None


def _endpoint(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    suffix = "/v1/chat/completions"
    if base.endswith(suffix):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    parsed = urllib.parse.urlparse(base)
    if parsed.path in {"", "/"}:
        return base + suffix
    return base + "/chat/completions"


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.I | re.S)
    return fenced.group(1).strip() if fenced else text


def _message_content(raw_response: dict[str, Any]) -> str:
    try:
        content = raw_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise E1aError("模型响应缺少 choices[0].message.content。") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        return "\n".join(texts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    raise E1aError("模型 message.content 类型无法解析。")


def _post_json(config: ModelConfig, payload: dict[str, Any]) -> dict[str, Any]:
    config.validate()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "e1a-minimal-footprint/1.0",
    }
    if config.api_key.strip():
        headers["Authorization"] = f"Bearer {config.api_key.strip()}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error = ""
    max_attempts = 3
    for attempt in range(max_attempts):
        request = urllib.request.Request(
            _endpoint(config.base_url), data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
                response_body = response.read()
            parsed = json.loads(response_body.decode("utf-8", errors="replace"))
            if not isinstance(parsed, dict):
                raise E1aError("模型 HTTP 响应顶层不是 JSON 对象。")
            return parsed
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            last_error = f"HTTP {exc.code}: {detail}"
            if exc.code not in {429, 500, 502, 503, 504} or attempt == max_attempts - 1:
                raise E1aError(f"模型请求失败：{last_error}") from exc
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == max_attempts - 1:
                raise E1aError(f"模型请求失败：{last_error}") from exc
        time.sleep(1.5 * (2**attempt))
    raise E1aError(f"模型请求失败：{last_error}")


def _transcribe_image_once(image_path: Path, config: ModelConfig) -> ModelResult:
    if not image_path.is_file():
        raise E1aError(f"源图片不存在：{image_path}")
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    schema_contract = {
        "type": "json_schema",
        "json_schema": {
            "name": "e1a_dimension_transcription",
            "strict": True,
            "schema": OUTPUT_SCHEMA,
        },
    }
    payload: dict[str, Any] = {
        "model": config.model_name.strip(),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": USER_PROMPT
                        + "\nPROMPT_VERSION="
                        + PROMPT_VERSION
                        + "\nOUTPUT_SCHEMA="
                        + json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, separators=(",", ":")),
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
        "response_format": schema_contract,
    }
    raw_response: dict[str, Any] = {}
    transcription: Transcription | None = None
    for contract_attempt in range(2):
        try:
            raw_response = _post_json(config, payload)
        except E1aError as exc:
            if (
                "response_format" not in str(exc).lower()
                and "json_schema" not in str(exc).lower()
            ):
                raise
            payload["response_format"] = {"type": "json_object"}
            raw_response = _post_json(config, payload)

        content = _strip_json_fence(_message_content(raw_response))
        try:
            parsed = json.loads(content)
            transcription = parse_transcription(parsed)
            break
        except (json.JSONDecodeError, E1aError) as exc:
            if contract_attempt == 1:
                if isinstance(exc, json.JSONDecodeError):
                    raise E1aError(f"模型返回内容不是有效 JSON：{exc.msg}") from exc
                raise
            error_text = (
                f"模型返回内容不是有效 JSON：{exc.msg}"
                if isinstance(exc, json.JSONDecodeError)
                else str(exc)
            )
            payload["messages"].extend(
                [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": FORMAT_REPAIR_PROMPT.format(error=error_text),
                    },
                ]
            )

    if transcription is None:
        raise E1aError("模型转录未通过字段契约校验。")
    request_id = str(raw_response.get("id") or "")
    return ModelResult(transcription, raw_response, request_id, "live_model")


def transcribe_image(image_path: Path, config: ModelConfig) -> ModelResult:
    """Run three requests concurrently while preserving 2+conditional-third consensus."""

    indexed_results: list[ModelResult | None] = [None] * MODEL_SAMPLE_REQUESTS
    indexed_errors: list[str] = [""] * MODEL_SAMPLE_REQUESTS
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MODEL_SAMPLE_CONCURRENCY,
        thread_name_prefix="footprint-vlm",
    ) as executor:
        futures = [
            executor.submit(_transcribe_image_once, image_path, config)
            for _index in range(MODEL_SAMPLE_REQUESTS)
        ]
        for index, future in enumerate(futures):
            try:
                indexed_results[index] = future.result()
            except E1aError as exc:
                indexed_errors[index] = type(exc).__name__

    successful_indices = [
        index for index, result in enumerate(indexed_results) if result is not None
    ]
    if len(successful_indices) < 2:
        error_types = ",".join(value for value in indexed_errors if value) or "unknown"
        raise E1aError(
            f"模型并发采样不足：{len(successful_indices)}/{MODEL_SAMPLE_REQUESTS} 成功；"
            f"错误类型：{error_types}。"
        )

    base_indices = successful_indices[:2]
    sample_results = [indexed_results[index] for index in base_indices]
    if any(result is None for result in sample_results):
        raise E1aError("模型并发采样结果不完整。")
    third_required = consensus_requires_third_sample(
        tuple(result.transcription for result in sample_results)
    )
    third_error = ""
    third_used = False
    if third_required:
        third_indices = [
            index for index in successful_indices if index not in base_indices
        ]
        if third_indices:
            third = indexed_results[third_indices[0]]
            if third is not None:
                sample_results.append(third)
                third_used = True
        else:
            third_error = next((value for value in indexed_errors if value), "E1aError")
    transcription, consensus_stats = merge_transcription_consensus(
        tuple(result.transcription for result in sample_results),
        third_sample_attempted=third_required,
        third_sample_error=third_error,
    )
    consensus_stats.update(
        {
            "network_sample_concurrency": MODEL_SAMPLE_CONCURRENCY,
            "network_sample_attempt_count": MODEL_SAMPLE_REQUESTS,
            "network_sample_success_count": len(successful_indices),
            "network_sample_failure_count": MODEL_SAMPLE_REQUESTS - len(successful_indices),
            "network_sample_error_types": [
                value for value in indexed_errors if value
            ],
            "speculative_third_started": True,
            "speculative_third_used": third_used,
            "consensus_sample_indices": base_indices
            + ([third_indices[0]] if third_required and third_used else []),
        }
    )
    return ModelResult(
        transcription=transcription,
        raw_response=sample_results[0].raw_response,
        request_id=sample_results[0].request_id,
        mode="live_model",
        sample_transcriptions=tuple(result.transcription for result in sample_results),
        sample_raw_responses=tuple(result.raw_response for result in sample_results),
        sample_request_ids=tuple(result.request_id for result in sample_results),
        consensus_stats=consensus_stats,
    )


def load_fixture(path: Path) -> ModelResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise E1aError(f"测试回放 JSON 无法读取：{exc}") from exc
    if (
        isinstance(payload, dict)
        and {"dimensions", "pin_count", "recommended_land_pattern"}.issubset(payload)
        and set(payload).issubset(
            {
                "dimensions",
                "pin_count",
                "recommended_land_pattern",
                "paste_evidence_visible",
                "unmarked_roles",
                "family_proposal",
                "family_proposal_reason",
            }
        )
    ):
        transcription_payload = payload
    elif isinstance(payload, dict) and isinstance(payload.get("transcription"), dict):
        transcription_payload = payload["transcription"]
    else:
        raise E1aError("测试回放 JSON 缺少 transcription 契约对象。")
    return ModelResult(
        transcription=parse_transcription(transcription_payload),
        raw_response={"fixture": path.name},
        request_id="fixture",
        mode="fixture_replay_no_model",
    )

E1A_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


@dataclass(frozen=True)
class SourcePage:
    source_path: Path
    source_kind: str
    page: int
    page_count: int
    image_path: Path
    page_text: str
    image_width: int
    image_height: int
    text_extractor: str = ""


def prepare_source_page(source_path: Path, page: int, workspace: Path) -> SourcePage:
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file():
        raise E1aError(f"输入文件不存在：{source_path}")
    workspace.mkdir(parents=True, exist_ok=True)
    image_path = workspace / "selected_source.png"
    suffix = source_path.suffix.lower()
    if suffix in E1A_IMAGE_SUFFIXES:
        try:
            from PIL import Image

            with Image.open(source_path) as image:
                converted = image.convert("RGB")
                converted.save(image_path, format="PNG")
                width, height = converted.size
        except OSError as exc:
            raise E1aError(f"截图无法读取：{exc}") from exc
        return SourcePage(
            source_path,
            "image",
            1,
            1,
            image_path,
            "",
            width,
            height,
            "not_applicable",
        )
    if suffix != ".pdf":
        raise E1aError("只支持 PDF、PNG、JPG、BMP、TIFF 或 WebP。")

    try:
        import pypdfium2 as pdfium
    except Exception as exc:
        raise E1aError("插件内置 PDF 渲染器无法加载。") from exc
    try:
        from pypdf import PdfReader
    except Exception:
        PdfReader = None

    document = pdfium.PdfDocument(str(source_path))
    try:
        page_count = len(document)
        if page < 1 or page > page_count:
            raise E1aError(f"页码 {page} 超出 1..{page_count}。")
        pdf_page = document[page - 1]
        try:
            image = pdf_page.render(scale=2.2).to_pil().convert("RGB")
            image.save(image_path, format="PNG")
            width, height = image.size
            text_extractor = "pypdf"
            try:
                if PdfReader is None:
                    raise RuntimeError("pypdf is unavailable")
                page_text = PdfReader(str(source_path)).pages[page - 1].extract_text() or ""
            except Exception:
                text_page = pdf_page.get_textpage()
                try:
                    page_text = text_page.get_text_range() or ""
                finally:
                    text_page.close()
                text_extractor = "pypdfium2_fallback"
        finally:
            pdf_page.close()
    finally:
        document.close()

    return SourcePage(
        source_path=source_path,
        source_kind="pdf",
        page=page,
        page_count=page_count,
        image_path=image_path,
        page_text=page_text,
        image_width=width,
        image_height=height,
        text_extractor=text_extractor,
    )


E1A_APP_TITLE = "Datasheet 封装建库（读图）"


class ScaledImagePanel(wx.Panel):
    def __init__(self, parent: wx.Window, empty_label: str):
        super().__init__(parent, style=wx.BORDER_SUNKEN)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.empty_label = empty_label
        self.image: wx.Image | None = None
        self.cache_key: tuple[int, int, int, int] | None = None
        self.cache_bitmap: wx.Bitmap | None = None
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, self.on_size)

    def load(self, path: Path | None) -> None:
        self.image = None
        if path is not None and path.is_file():
            loaded = wx.Image(str(path), wx.BITMAP_TYPE_ANY)
            if loaded.IsOk():
                self.image = loaded
        self.cache_key = None
        self.cache_bitmap = None
        self.Refresh()

    def load_image(self, image: wx.Image | None) -> None:
        """Load an already decoded image, used by the enlarged preview dialog."""
        self.image = image.Copy() if image is not None and image.IsOk() else None
        self.cache_key = None
        self.cache_bitmap = None
        self.Refresh()

    def on_size(self, event: wx.SizeEvent) -> None:
        self.cache_key = None
        event.Skip()
        self.Refresh()

    def on_paint(self, _event: wx.PaintEvent) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.Colour("#F7F8FA")))
        dc.Clear()
        width, height = self.GetClientSize()
        if self.image is None or width < 20 or height < 20:
            dc.SetTextForeground(wx.Colour("#6B7280"))
            text_width, text_height = dc.GetTextExtent(self.empty_label)
            dc.DrawText(self.empty_label, max(8, (width - text_width) // 2), max(8, (height - text_height) // 2))
            return
        source_width, source_height = self.image.GetWidth(), self.image.GetHeight()
        key = (width, height, source_width, source_height)
        if key != self.cache_key or self.cache_bitmap is None:
            scale = min((width - 16) / source_width, (height - 16) / source_height)
            target_width = max(1, int(source_width * scale))
            target_height = max(1, int(source_height * scale))
            resized = self.image.Scale(target_width, target_height, wx.IMAGE_QUALITY_HIGH)
            self.cache_bitmap = wx.Bitmap(resized)
            self.cache_key = key
        bitmap = self.cache_bitmap
        x = (width - bitmap.GetWidth()) // 2
        y = (height - bitmap.GetHeight()) // 2
        dc.DrawBitmap(bitmap, x, y, True)


class ImageZoomDialog(wx.Dialog):
    """Show the original package image in a larger, fit-to-window view."""

    def __init__(self, parent: wx.Window, image: wx.Image):
        display_size = wx.GetDisplaySize()
        display_width = display_size.GetWidth()
        display_height = display_size.GetHeight()
        width = min(1600, max(720, display_width - 100))
        height = min(1000, max(520, display_height - 140))
        super().__init__(
            parent,
            title="原封装图（放大）",
            size=(width, height),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        preview = ScaledImagePanel(self, "无法显示原封装图")
        preview.load_image(image)
        preview.SetMinSize((480, 340))
        caption = wx.StaticText(
            self,
            label="原图放大预览；按 Esc 或关闭窗口返回",
        )
        caption.SetForegroundColour(wx.Colour("#4B5563"))
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(preview, 1, wx.EXPAND | wx.ALL, 12)
        root.Add(caption, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        if buttons:
            root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.SetSizer(root)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.CentreOnParent()

    def _on_char_hook(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        event.Skip()


class ScreenshotDropTarget(wx.FileDropTarget):
    """Accept image files dropped onto the original-image area."""

    def __init__(self, owner: "E1aDialog"):
        super().__init__()
        self.owner = owner

    def OnDropFiles(self, _x: int, _y: int, filenames: list[str]) -> bool:
        for filename in filenames:
            path = Path(filename)
            if path.is_file() and path.suffix.lower() in E1A_IMAGE_SUFFIXES:
                wx.CallAfter(self.owner.on_screenshot_dropped, path)
                return True
        wx.CallAfter(
            self.owner.show_error,
            "请拖入 PNG、JPG、BMP、TIFF 或 WebP 截图。",
        )
        return False


class PageSuggestionDialog(wx.Dialog):
    """Let the user choose or override one of the deterministic page suggestions."""

    def __init__(self, parent: wx.Window, pdf_path: Path, result: dict[str, Any]):
        super().__init__(
            parent,
            title="自动建议封装页",
            size=(940, 500),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.suggestions = list(result.get("suggestions") or [])
        page_count = max(1, int(result.get("page_count") or 1))
        is_fallback = bool(self.suggestions) and all(
            bool(item.get("fallback")) for item in self.suggestions
        )

        banner_text = (
            "引擎未找到焊盘页，以下仅为兜底"
            if is_fallback
            else "以下为页选引擎建议；选择后只载入页面，不会自动提交"
        )
        banner = wx.StaticText(self, label=banner_text)
        if is_fallback:
            banner.SetBackgroundColour(wx.Colour("#FFF3BF"))
            banner.SetForegroundColour(wx.Colour("#7C4A03"))
        else:
            banner.SetForegroundColour(wx.Colour("#166534"))

        self.list = wx.ListCtrl(
            self,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        for index, (label, width) in enumerate(
            (("页码", 80), ("分数", 80), ("类别", 110), ("命中词", 600))
        ):
            self.list.InsertColumn(index, label, width=width)
        for index, item in enumerate(self.suggestions):
            fallback = bool(item.get("fallback"))
            page_class = "兜底" if fallback else str(item.get("page_class") or "none")
            terms = tuple(
                dict.fromkeys(
                    [
                        *[str(value) for value in item.get("matched_terms") or ()],
                        *[str(value) for value in item.get("matched_stem_terms") or ()],
                    ]
                )
            )
            row = self.list.InsertItem(index, str(item.get("page") or 1))
            self.list.SetItem(row, 1, str(item.get("score") or 0))
            self.list.SetItem(row, 2, page_class)
            self.list.SetItem(row, 3, ", ".join(terms) if terms else "-")
            if fallback:
                self.list.SetItemBackgroundColour(row, wx.Colour("#FFF3BF"))

        initial_page = int(self.suggestions[0].get("page") or 1) if self.suggestions else 1
        self.page = wx.SpinCtrl(self, min=1, max=page_count, initial=initial_page)
        if self.suggestions:
            self.list.Select(0)

        page_row = wx.BoxSizer(wx.HORIZONTAL)
        page_row.Add(wx.StaticText(self, label="使用页码"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        page_row.Add(self.page, 0)
        page_row.Add(
            wx.StaticText(self, label="可直接改成任意页"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.LEFT,
            8,
        )
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(wx.StaticText(self, label=str(pdf_path)), 0, wx.EXPAND | wx.ALL, 12)
        root.Add(banner, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        root.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        root.Add(page_row, 0, wx.EXPAND | wx.ALL, 12)
        if buttons:
            root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizer(root)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_page_selected)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_page_activated)
        self.CentreOnParent()

    def _on_page_selected(self, event: wx.ListEvent) -> None:
        index = event.GetIndex()
        if 0 <= index < len(self.suggestions):
            self.page.SetValue(int(self.suggestions[index].get("page") or 1))

    def _on_page_activated(self, event: wx.ListEvent) -> None:
        self._on_page_selected(event)
        self.EndModal(wx.ID_OK)

    def selected_page(self) -> int:
        return self.page.GetValue()


class ModelSettingsDialog(wx.Dialog):
    def __init__(
        self,
        parent: wx.Window,
        base_url: str,
        model_name: str,
        api_key: str,
        allow_insecure_http: bool,
    ):
        super().__init__(parent, title="模型连接设置", size=(680, 330))
        self.base_url = wx.TextCtrl(self, value=base_url)
        self.model = wx.ComboBox(
            self,
            value=model_name,
            choices=[
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
                "gpt-5.5",
                "gpt-5.4",
                "gpt-5.4-mini",
            ],
            style=wx.CB_DROPDOWN,
        )
        self.model.SetToolTip("可从列表选择，也可直接输入网关支持的其他模型 ID")
        self.api_key = wx.TextCtrl(self, value=api_key, style=wx.TE_PASSWORD)
        self.allow_insecure_http = wx.CheckBox(
            self,
            label="我确认允许远程 HTTP 明文传输密钥与封装图（仅当前窗口）",
        )
        self.allow_insecure_http.SetValue(allow_insecure_http)

        form = wx.FlexGridSizer(cols=2, hgap=10, vgap=10)
        form.AddGrowableCol(1, 1)
        for label, control in (
            ("Base URL", self.base_url),
            ("模型", self.model),
            ("API Key", self.api_key),
        ):
            form.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(control, 1, wx.EXPAND)
        note = wx.StaticText(
            self,
            label="连接信息只保留在当前窗口内；远程 HTTP 放行默认关闭。",
        )
        insecure_note = wx.StaticText(
            self,
            label="警告：启用后网络中间节点可能读取密钥、图片和模型响应。",
        )
        insecure_note.SetForegroundColour(wx.Colour("#9A3412"))
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(form, 1, wx.EXPAND | wx.ALL, 14)
        root.Add(self.allow_insecure_http, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        root.Add(insecure_note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        if buttons:
            root.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizer(root)

    def values(self) -> tuple[str, str, str, bool]:
        return (
            self.base_url.GetValue().strip(),
            self.model.GetValue().strip(),
            self.api_key.GetValue().strip(),
            self.allow_insecure_http.GetValue(),
        )


class BatchConfirmationDialog(wx.Dialog):
    def __init__(
        self,
        parent: wx.Window,
        queue_path: Path,
        library_dir: Path,
    ):
        super().__init__(
            parent,
            title="审核待确认 footprint",
            size=(1050, 720),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        self.queue_path = queue_path.expanduser().resolve()
        self.rows = list_confirmation_records(self.queue_path)
        payload = json.loads(self.queue_path.read_text(encoding="utf-8"))
        self.raw_by_id = {
            str(row.get("record_id")): row
            for row in payload.get("records") or []
            if isinstance(row, dict) and row.get("record_id")
        }
        self.committed_ids: set[str] = set()
        self.last_transaction_path: Path | None = None

        labels = [self._row_label(row) for row in self.rows]
        self.checklist = wx.CheckListBox(self, choices=labels)
        self.details = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL,
        )
        self.reviewer = wx.TextCtrl(
            self,
            value=(os.environ.get("USERNAME") or os.environ.get("USER") or "user"),
        )
        self.library_picker = wx.DirPickerCtrl(
            self,
            path=str(library_dir),
            message="选择或创建 .pretty footprint 库",
        )
        self.open_source_button = wx.Button(self, label="打开原图")
        self.open_preview_button = wx.Button(self, label="打开预览")
        self.check_all_button = wx.Button(self, label="全选待确认")
        self.clear_button = wx.Button(self, label="清空选择")
        self.confirm_button = wx.Button(self, label="确认所选并入库")
        self.rollback_button = wx.Button(self, label="回退本次提交")
        self.rollback_button.Disable()

        header = wx.StaticText(
            self,
            label=(
                "逐件查看原图、预览和数值出处后勾选。只有点击“确认所选并入库”并再次确认，"
                "所选件才会写入；未勾选件保持不变。"
            ),
        )
        header.Wrap(980)
        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        list_host = wx.Panel(splitter)
        detail_host = wx.Panel(splitter)
        list_sizer = wx.BoxSizer(wx.VERTICAL)
        list_sizer.Add(self.checklist, 1, wx.EXPAND)
        list_host.SetSizer(list_sizer)
        detail_sizer = wx.BoxSizer(wx.VERTICAL)
        detail_sizer.Add(self.details, 1, wx.EXPAND)
        asset_row = wx.BoxSizer(wx.HORIZONTAL)
        asset_row.Add(self.open_source_button, 0, wx.RIGHT, 8)
        asset_row.Add(self.open_preview_button, 0)
        detail_sizer.Add(asset_row, 0, wx.TOP, 8)
        detail_host.SetSizer(detail_sizer)
        splitter.SplitVertically(list_host, detail_host, 430)
        splitter.SetMinimumPaneSize(320)

        select_row = wx.BoxSizer(wx.HORIZONTAL)
        select_row.Add(self.check_all_button, 0, wx.RIGHT, 8)
        select_row.Add(self.clear_button, 0)
        form = wx.FlexGridSizer(cols=2, hgap=10, vgap=8)
        form.AddGrowableCol(1, 1)
        form.Add(wx.StaticText(self, label="审核人"), 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.reviewer, 1, wx.EXPAND)
        form.Add(wx.StaticText(self, label="目标 .pretty 库"), 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.library_picker, 1, wx.EXPAND)
        action_row = wx.BoxSizer(wx.HORIZONTAL)
        action_row.AddStretchSpacer(1)
        action_row.Add(self.rollback_button, 0, wx.RIGHT, 8)
        action_row.Add(self.confirm_button, 0, wx.RIGHT, 8)
        action_row.Add(wx.Button(self, wx.ID_CLOSE, "关闭"), 0)

        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(header, 0, wx.EXPAND | wx.ALL, 12)
        root.Add(splitter, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        root.Add(select_row, 0, wx.EXPAND | wx.ALL, 12)
        root.Add(form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        root.Add(action_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizer(root)

        self.checklist.Bind(wx.EVT_LISTBOX, self._on_row_selected)
        self.open_source_button.Bind(wx.EVT_BUTTON, self._on_open_source)
        self.open_preview_button.Bind(wx.EVT_BUTTON, self._on_open_preview)
        self.check_all_button.Bind(wx.EVT_BUTTON, self._on_check_all)
        self.clear_button.Bind(wx.EVT_BUTTON, self._on_clear)
        self.confirm_button.Bind(wx.EVT_BUTTON, self._on_confirm)
        self.rollback_button.Bind(wx.EVT_BUTTON, self._on_rollback)
        self.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
        if self.rows:
            self.checklist.SetSelection(0)
            self._show_row(0)
        self.CentreOnParent()

    @staticmethod
    def _row_label(row: dict[str, Any]) -> str:
        marker = "待确认" if row.get("pending") else str(row.get("status") or "不可提交")
        return (
            f"[{marker}] {row.get('record_id')} | {row.get('family') or '-'} | "
            f"p{row.get('page') or '-'} | {row.get('candidate_name') or '-'}"
        )

    def _selected_index(self) -> int:
        return self.checklist.GetSelection()

    def _show_row(self, index: int) -> None:
        if not 0 <= index < len(self.rows):
            self.details.SetValue("")
            return
        row = self.rows[index]
        raw = self.raw_by_id.get(str(row["record_id"]), {})
        provenance = raw.get("value_provenance") or []
        lines = [
            f"记录：{row['record_id']}",
            f"状态：{row.get('status')}",
            f"族：{row.get('family')}",
            f"资料：{row.get('pdf_name')}  第 {row.get('page')} 页",
            f"候选：{row.get('candidate_name')}",
            "",
            "几何值：",
        ]
        for key, value in sorted(dict(row.get("values") or {}).items()):
            if value is not None:
                lines.append(f"  {key} = {value}")
        lines.extend(("", "逐值出处："))
        for item in provenance:
            if not isinstance(item, dict):
                continue
            raw_values = item.get("raw_source_candidates") or []
            raw_text = " | ".join(
                str(value.get("raw") or value.get("value") or "")
                for value in raw_values
                if isinstance(value, dict)
            )
            lines.append(
                f"  {item.get('field')} = {item.get('value_mm')} | "
                f"p{item.get('source_page_1based')} | {item.get('solver_source')} | {raw_text}"
            )
        if row["record_id"] in self.committed_ids:
            lines.extend(("", "本窗口内已提交；可使用“回退本次提交”。"))
        self.details.SetValue("\n".join(lines))

    def _on_row_selected(self, _event: wx.CommandEvent) -> None:
        self._show_row(self._selected_index())

    def _record_asset(self, key: str) -> Path | None:
        index = self._selected_index()
        if not 0 <= index < len(self.rows):
            return None
        raw = self.raw_by_id.get(str(self.rows[index]["record_id"]), {})
        text = str(raw.get(key) or "").strip()
        if not text and key == "source_image_path":
            text = str(raw.get("pdf_path") or "").strip()
        if not text:
            return None
        path = Path(text)
        if not path.is_absolute():
            path = self.queue_path.parent / path
        path = path.resolve()
        return path if path.is_file() else None

    def _open_asset(self, key: str) -> None:
        path = self._record_asset(key)
        if path is None:
            wx.MessageBox("该记录没有可打开的文件。", E1A_APP_TITLE, wx.OK | wx.ICON_INFORMATION)
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            wx.MessageBox(str(exc), E1A_APP_TITLE, wx.OK | wx.ICON_ERROR)

    def _on_open_source(self, _event: wx.CommandEvent) -> None:
        self._open_asset("source_image_path")

    def _on_open_preview(self, _event: wx.CommandEvent) -> None:
        self._open_asset("preview_path")

    def _on_check_all(self, _event: wx.CommandEvent) -> None:
        for index, row in enumerate(self.rows):
            should_check = bool(row.get("pending")) and row["record_id"] not in self.committed_ids
            self.checklist.Check(index, should_check)

    def _on_clear(self, _event: wx.CommandEvent) -> None:
        for index in range(len(self.rows)):
            self.checklist.Check(index, False)

    def _checked_record_ids(self) -> list[str]:
        return [
            str(row["record_id"])
            for index, row in enumerate(self.rows)
            if self.checklist.IsChecked(index)
            and bool(row.get("pending"))
            and row["record_id"] not in self.committed_ids
        ]

    def _on_confirm(self, _event: wx.CommandEvent) -> None:
        record_ids = self._checked_record_ids()
        if not record_ids:
            wx.MessageBox("请先勾选至少一条待确认记录。", E1A_APP_TITLE, wx.OK | wx.ICON_INFORMATION)
            return
        library = Path(self.library_picker.GetPath())
        message = (
            f"已逐件核对所选 {len(record_ids)} 条记录，并确认写入：\n{library}\n\n"
            "现有同名 footprint 会先备份再替换；未勾选件不会变化。"
        )
        if wx.MessageBox(message, E1A_APP_TITLE, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) != wx.YES:
            return
        try:
            transaction = apply_confirmations(
                self.queue_path,
                library,
                record_ids,
                reviewer=self.reviewer.GetValue(),
                confirmed=True,
            )
        except Exception as exc:
            wx.MessageBox(str(exc), E1A_APP_TITLE, wx.OK | wx.ICON_ERROR)
            return
        self.committed_ids.update(record_ids)
        self.last_transaction_path = Path(transaction["transaction_path"])
        self.rollback_button.Enable()
        for index, row in enumerate(self.rows):
            if row["record_id"] in record_ids:
                self.checklist.Check(index, False)
                self.checklist.SetString(index, "[本次已提交] " + self._row_label(row))
        self._show_row(self._selected_index())
        wx.MessageBox(
            f"已写入 {len(record_ids)} 件。\n审计记录：\n{self.last_transaction_path}",
            E1A_APP_TITLE,
            wx.OK | wx.ICON_INFORMATION,
        )

    def _on_rollback(self, _event: wx.CommandEvent) -> None:
        if self.last_transaction_path is None:
            return
        if wx.MessageBox(
            "确认回退本窗口最近一次提交？提交后的文件若已变化将拒绝回退。",
            E1A_APP_TITLE,
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        ) != wx.YES:
            return
        try:
            transaction = rollback_transaction(
                self.last_transaction_path,
                reviewer=self.reviewer.GetValue(),
                confirmed=True,
            )
        except Exception as exc:
            wx.MessageBox(str(exc), E1A_APP_TITLE, wx.OK | wx.ICON_ERROR)
            return
        rolled_back = set(transaction.get("confirmed_record_ids") or [])
        self.committed_ids.difference_update(rolled_back)
        for index, row in enumerate(self.rows):
            if row["record_id"] in rolled_back:
                self.checklist.SetString(index, self._row_label(row))
        self.last_transaction_path = None
        self.rollback_button.Disable()
        self._show_row(self._selected_index())
        wx.MessageBox("最近一次提交已回退。", E1A_APP_TITLE, wx.OK | wx.ICON_INFORMATION)


class E1aDialog(wx.Dialog):
    FIELD_LABELS = (
        ("pad_x", "小焊盘 X / mm"),
        ("pad_y", "小焊盘 Y / mm"),
        ("center_x", "左右中心距 / mm"),
        ("center_y", "上下中心距 / mm"),
        ("pitch_y", "左右排行内 pitch / mm"),
        ("pitch_x", "上下排行内 pitch / mm"),
        ("tab_x", "大焊盘 X / mm"),
        ("tab_y", "大焊盘 Y / mm"),
        ("body_x", "本体 X / mm"),
        ("body_y", "本体 Y / mm"),
    )
    LAND_TARGET_FIELDS = (
        "pad_x", "pad_y", "center_x", "center_y", "pitch_y", "pitch_x",
        "tab_x", "tab_y",
    )

    def __init__(
        self,
        parent: wx.Window | None = None,
        *,
        initial_source: Path | None = None,
        initial_library: Path | None = None,
        runs_root: Path | None = None,
    ):
        fixture_text = os.environ.get("E1A_TRANSCRIPTION_FIXTURE", "").strip()
        title = E1A_APP_TITLE + (" [测试回放]" if fixture_text else "")
        super().__init__(
            parent,
            title=title,
            size=(1400, 880),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        self.SetMinSize((1080, 760))
        self.fixture_path = Path(fixture_text).resolve() if fixture_text else None
        self.model_base_url = (
            os.environ.get("VLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
        ).strip()
        self.model_name = (
            os.environ.get("VLM_MODEL") or os.environ.get("OPENAI_MODEL") or MODEL_NAME
        ).strip()
        self.model_api_key = (
            os.environ.get("VLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        ).strip()
        self.model_allow_insecure_http = (
            os.environ.get("E1A_ALLOW_INSECURE_HTTP", "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.temp_dir = tempfile.TemporaryDirectory(prefix="e1a_ui_")
        self.source_workspace = Path(self.temp_dir.name)
        default_runs = Path(tempfile.gettempdir()) / "kicad_footprint_builder_runs"
        configured_runs = os.environ.get("E1A_RUNS_DIR", "").strip()
        effective_runs = Path(configured_runs) if configured_runs else (runs_root or default_runs)
        self.store = RunArtifactStore(effective_runs)
        self.source_page: SourcePage | None = None
        self.pasted_source_path: Path | None = None
        self.transcription: Transcription | None = None
        self.anchor_result: AnchorResult | None = None
        self.family_decision: FamilyDecision | None = None
        self.geometry_prefill: GeometryPrefillResult | None = None
        self.land_candidate_items: tuple[Dimension, ...] = ()
        self.land_target_fields: list[str] = []
        self.manual_land_assignments: dict[str, Dimension] = {}
        self.current_run_dir: Path | None = None
        self.current_candidate: Path | None = None
        self.current_candidate_name = ""
        self.current_mode = ""
        self.running = False
        self._family_programmatic_change = False
        self._last_original_click_at = 0.0
        self._last_original_click_pos: wx.Point | None = None
        self._zoom_opening = False

        self.source_path = wx.TextCtrl(self, style=wx.TE_READONLY)
        self.choose_source_button = wx.Button(self, label="换用 PDF / 截图")
        self.suggest_pdf_button = wx.Button(self, label="打开 PDF → 自动建议页")
        self.suggest_pdf_button.SetToolTip("列出 top-3 建议页，由你选择；不会自动提交")
        self.page = wx.SpinCtrl(self, min=1, max=1, initial=1, size=(90, -1))
        self.load_page_button = wx.Button(self, label="载入页")
        self.family = wx.Choice(
            self,
            choices=[
                "CHIP 两端",
                "SMx / SOD 两端",
                "SOT 三端",
                "ASYM3 一大两小 / 一大一小",
                "IN-LINE-3 一排三等距",
                "GRID-4 2×2 栅格",
                "DUAL 两排等距鸥翼",
                "QUAD/EP QFN / DFN 裸露焊盘",
            ],
        )
        self.family.SetSelection(0)
        self.family_auto_label = wx.StaticText(self, label="")
        self.family_auto_label.SetForegroundColour(wx.Colour("#166534"))
        self.asym_variant = wx.Choice(self, choices=["2 个小焊盘", "1 个小焊盘"])
        self.asym_variant.SetSelection(0)
        self.asym_variant.Hide()
        self.tab_pad_number = wx.Choice(self, choices=["大焊盘编号 1", "大焊盘编号 2", "大焊盘编号 3"])
        self.tab_pad_number.SetSelection(1)
        self.tab_pad_number.Hide()
        self.model_label = wx.StaticText(self, label=self.model_name)
        self.model_settings_button = wx.Button(self, label="连接设置")
        self.transcribe_button = wx.Button(self, label="读取尺寸并预填")
        self.transcribe_button.SetToolTip("模型转录 → 确定性裁族 → 自动预填 → 生成预览；仍须人工确认入库")
        self.status = wx.StaticText(self, label="就绪")
        self.family_notice = wx.StaticText(self, label="")
        self.family_notice.SetBackgroundColour(wx.Colour("#FFF3BF"))
        self.family_notice.SetForegroundColour(wx.Colour("#7C4A03"))
        self.family_notice.Hide()
        self.anchor_notice = wx.StaticText(
            self,
            label="本次几何为人工输入，未做原文锚定",
        )
        self.anchor_notice.SetBackgroundColour(wx.Colour("#FFF3BF"))
        self.anchor_notice.SetForegroundColour(wx.Colour("#7C4A03"))
        self.anchor_notice.Hide()
        self.ratio_warning = wx.StaticText(self, label="")
        self.ratio_warning.SetBackgroundColour(wx.Colour("#FFF3BF"))
        self.ratio_warning.SetForegroundColour(wx.Colour("#7C4A03"))
        self.ratio_warning.Hide()

        self.preview_splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        self.original_host = wx.Panel(self.preview_splitter)
        self.generated_host = wx.Panel(self.preview_splitter)
        self.original_image = ScaledImagePanel(self.original_host, "未载入封装图")
        self.generated_image = ScaledImagePanel(self.generated_host, "未生成 footprint")
        self.original_drop_hint = wx.StaticText(
            self.original_host,
            label="双击放大；可拖入或 Ctrl+V 粘贴截图",
        )
        self.original_drop_hint.SetForegroundColour(wx.Colour("#4B5563"))
        self.original_image.SetToolTip("双击查看放大原图；也可直接拖入截图文件")
        self._layout_preview_host(
            self.original_host,
            "原封装图",
            self.original_image,
            footer=self.original_drop_hint,
        )
        self._layout_preview_host(self.generated_host, "生成的 footprint", self.generated_image)
        self.preview_splitter.SplitVertically(self.original_host, self.generated_host, 735)
        self.preview_splitter.SetMinimumPaneSize(340)

        self._drop_targets: list[ScreenshotDropTarget] = []
        for drop_window in (self, self.original_host, self.original_image):
            drop_target = ScreenshotDropTarget(self)
            drop_window.SetDropTarget(drop_target)
            self._drop_targets.append(drop_target)

        self.details_splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        self.dimension_host = wx.Panel(self.details_splitter)
        self.geometry_host = wx.Panel(self.details_splitter)
        self.dimension_list = wx.ListCtrl(
            self.dimension_host,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        for index, (label, width) in enumerate(
            (
                ("符号", 90),
                ("值", 65),
                ("单位", 55),
                ("几何角色", 120),
                ("归属", 110),
                ("标注线两端", 230),
                ("图示/推导", 80),
                ("原文", 210),
                ("锚定", 75),
            )
        ):
            self.dimension_list.InsertColumn(index, label, width=width)
        dimension_sizer = wx.BoxSizer(wx.VERTICAL)
        dimension_sizer.Add(wx.StaticText(self.dimension_host, label="模型转录"), 0, wx.BOTTOM, 5)
        dimension_sizer.Add(self.dimension_list, 1, wx.EXPAND)
        self.dimension_host.SetSizer(dimension_sizer)

        self.geometry_inputs: dict[str, wx.TextCtrl] = {}
        self.geometry_sources: dict[str, wx.StaticText] = {}
        self.geometry_labels: dict[str, wx.StaticText] = {}
        self._layout_geometry_host()
        self.details_splitter.SplitVertically(self.dimension_host, self.geometry_host, 760)
        self.details_splitter.SetMinimumPaneSize(420)

        default_library = initial_library or (
            Path(r"D:\codex_workspace\KiCad_workspace\KiCad_formal_lib\E1a_Confirmed.pretty")
        )
        self.library_picker = wx.DirPickerCtrl(
            self,
            path=str(default_library),
            message="选择或创建 .pretty footprint 库",
        )
        self.review_queue_button = wx.Button(self, label="审核批量候选…")
        self.review_queue_button.SetToolTip(
            "打开审核队列；逐件或批量确认后才写入，并自动保留可回退备份与出处链"
        )
        self.footprint_name = wx.TextCtrl(self, value="E1A_Preview")
        self.generate_button = wx.Button(self, label="生成预览")
        self.confirm_button = wx.Button(self, label="确认并入库")
        self.confirm_button.Disable()
        self.transcribe_button.Disable()

        self._layout()
        self._bind_events()
        self.update_family_controls()
        self.CentreOnParent()
        if initial_source is not None and initial_source.is_file():
            self.source_path.SetValue(str(initial_source.resolve()))
            wx.CallAfter(self.on_load_page, None)
        if self.fixture_path:
            self.set_status("测试回放模式：读取尺寸不会调用模型。")

    @staticmethod
    def _layout_preview_host(
        host: wx.Panel,
        label: str,
        panel: ScaledImagePanel,
        *,
        footer: wx.Window | None = None,
    ) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(host, label=label), 0, wx.BOTTOM, 5)
        sizer.Add(panel, 1, wx.EXPAND)
        if footer is not None:
            sizer.Add(footer, 0, wx.EXPAND | wx.TOP, 4)
        host.SetSizer(sizer)

    def _layout_geometry_host(self) -> None:
        form = wx.FlexGridSizer(cols=3, hgap=8, vgap=6)
        form.AddGrowableCol(1, 1)
        form.AddGrowableCol(2, 1)
        for key, label in self.FIELD_LABELS:
            control = wx.TextCtrl(self.geometry_host)
            source = wx.StaticText(self.geometry_host, label="-")
            field_label = wx.StaticText(self.geometry_host, label=label)
            self.geometry_inputs[key] = control
            self.geometry_sources[key] = source
            self.geometry_labels[key] = field_label
            form.Add(field_label, 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(control, 1, wx.EXPAND)
            form.Add(source, 1, wx.ALIGN_CENTER_VERTICAL | wx.EXPAND)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self.geometry_host, label="确定性几何输入"), 0, wx.BOTTOM, 5)
        sizer.Add(form, 1, wx.EXPAND)
        self.dual_row_label = wx.StaticText(self.geometry_host, label="DUAL 每排焊盘数")
        self.dual_left_count = wx.SpinCtrl(
            self.geometry_host, min=1, max=64, initial=4, size=(72, -1)
        )
        self.dual_right_count = wx.SpinCtrl(
            self.geometry_host, min=1, max=64, initial=4, size=(72, -1)
        )
        self.dual_left_label = wx.StaticText(self.geometry_host, label="左排")
        self.dual_right_label = wx.StaticText(self.geometry_host, label="右排")
        dual_row = wx.BoxSizer(wx.HORIZONTAL)
        dual_row.Add(self.dual_row_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        dual_row.Add(self.dual_left_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        dual_row.Add(self.dual_left_count, 0, wx.RIGHT, 10)
        dual_row.Add(self.dual_right_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        dual_row.Add(self.dual_right_count, 0)
        for control in (
            self.dual_row_label,
            self.dual_left_label,
            self.dual_left_count,
            self.dual_right_label,
            self.dual_right_count,
        ):
            control.Hide()
        sizer.Add(dual_row, 0, wx.EXPAND | wx.TOP, 8)
        self.quad_row_label = wx.StaticText(
            self.geometry_host, label="QUAD/EP 四边外围焊盘数"
        )
        self.quad_left_count = wx.SpinCtrl(
            self.geometry_host, min=1, max=24, initial=4, size=(64, -1)
        )
        self.quad_right_count = wx.SpinCtrl(
            self.geometry_host, min=1, max=24, initial=4, size=(64, -1)
        )
        self.quad_top_count = wx.SpinCtrl(
            self.geometry_host, min=0, max=12, initial=0, size=(64, -1)
        )
        self.quad_bottom_count = wx.SpinCtrl(
            self.geometry_host, min=0, max=12, initial=0, size=(64, -1)
        )
        quad_row = wx.BoxSizer(wx.HORIZONTAL)
        quad_row.Add(self.quad_row_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        for label, control in (
            ("左", self.quad_left_count),
            ("右", self.quad_right_count),
            ("上", self.quad_top_count),
            ("下", self.quad_bottom_count),
        ):
            label_control = wx.StaticText(self.geometry_host, label=label)
            setattr(self, f"quad_{'left' if label == '左' else 'right' if label == '右' else 'top' if label == '上' else 'bottom'}_label", label_control)
            quad_row.Add(label_control, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
            quad_row.Add(control, 0, wx.RIGHT, 8)
        self.quad_controls = (
            self.quad_row_label,
            self.quad_left_label,
            self.quad_left_count,
            self.quad_right_label,
            self.quad_right_count,
            self.quad_top_label,
            self.quad_top_count,
            self.quad_bottom_label,
            self.quad_bottom_count,
        )
        for control in self.quad_controls:
            control.Hide()
        sizer.Add(quad_row, 0, wx.EXPAND | wx.TOP, 8)
        self.geometry_resolution_notice = wx.StaticText(self.geometry_host, label="")
        self.geometry_resolution_notice.Hide()
        sizer.Add(self.geometry_resolution_notice, 0, wx.EXPAND | wx.TOP, 8)

        self.land_resolution_panel = wx.Panel(self.geometry_host)
        self.land_candidate_list = wx.ListBox(
            self.land_resolution_panel,
            style=wx.LB_SINGLE | wx.BORDER_SUNKEN,
            size=(-1, 78),
        )
        self.land_target_choice = wx.Choice(self.land_resolution_panel)
        self.assign_land_button = wx.Button(self.land_resolution_panel, label="填入所选字段")
        manual_row = wx.BoxSizer(wx.HORIZONTAL)
        manual_row.Add(self.land_candidate_list, 1, wx.EXPAND | wx.RIGHT, 8)
        manual_actions = wx.BoxSizer(wx.VERTICAL)
        manual_actions.Add(self.land_target_choice, 0, wx.EXPAND | wx.BOTTOM, 6)
        manual_actions.Add(self.assign_land_button, 0, wx.EXPAND)
        manual_row.Add(manual_actions, 0, wx.EXPAND)
        self.land_resolution_panel.SetSizer(manual_row)
        self.land_resolution_panel.Hide()
        sizer.Add(self.land_resolution_panel, 0, wx.EXPAND | wx.TOP, 6)
        self.geometry_host.SetSizer(sizer)

    def _layout(self) -> None:
        root = wx.BoxSizer(wx.VERTICAL)

        source_row = wx.BoxSizer(wx.HORIZONTAL)
        source_row.Add(wx.StaticText(self, label="封装图"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        source_row.Add(self.source_path, 1, wx.EXPAND | wx.RIGHT, 8)
        source_row.Add(self.choose_source_button, 0, wx.RIGHT, 12)
        source_row.Add(self.suggest_pdf_button, 0, wx.RIGHT, 12)
        source_row.Add(wx.StaticText(self, label="页码"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        source_row.Add(self.page, 0, wx.RIGHT, 6)
        source_row.Add(self.load_page_button, 0, wx.RIGHT, 12)
        source_row.Add(wx.StaticText(self, label="封装族"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        source_row.Add(self.family, 0)
        source_row.Add(self.family_auto_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        source_row.Add(self.asym_variant, 0, wx.LEFT, 6)
        source_row.Add(self.tab_pad_number, 0, wx.LEFT, 6)
        root.Add(source_row, 0, wx.EXPAND | wx.ALL, 10)

        action_row = wx.BoxSizer(wx.HORIZONTAL)
        action_row.Add(wx.StaticText(self, label="模型"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        action_row.Add(self.model_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        action_row.Add(self.model_settings_button, 0, wx.RIGHT, 8)
        action_row.Add(self.transcribe_button, 0, wx.RIGHT, 14)
        action_row.Add(self.status, 1, wx.ALIGN_CENTER_VERTICAL)
        root.Add(action_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        root.Add(self.family_notice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        root.Add(self.anchor_notice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        root.Add(self.ratio_warning, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        root.Add(self.preview_splitter, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        root.Add(self.details_splitter, 0, wx.EXPAND | wx.ALL, 10)
        self.details_splitter.SetMinSize((-1, 300))

        library_row = wx.BoxSizer(wx.HORIZONTAL)
        library_row.Add(
            wx.StaticText(self, label="目标 .pretty 库"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            8,
        )
        library_row.Add(self.library_picker, 1, wx.EXPAND | wx.RIGHT, 8)
        library_row.Add(self.review_queue_button, 0)
        root.Add(library_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        commit_row = wx.BoxSizer(wx.HORIZONTAL)
        commit_row.Add(
            wx.StaticText(self, label="Footprint 名称"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            8,
        )
        commit_row.Add(self.footprint_name, 1, wx.RIGHT, 12)
        commit_row.Add(self.generate_button, 0, wx.RIGHT, 8)
        commit_row.Add(self.confirm_button, 0)
        root.Add(commit_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.SetSizer(root)

    def _bind_events(self) -> None:
        self.choose_source_button.Bind(wx.EVT_BUTTON, self.on_choose_source)
        self.suggest_pdf_button.Bind(wx.EVT_BUTTON, self.on_suggest_pdf_pages)
        self.load_page_button.Bind(wx.EVT_BUTTON, self.on_load_page)
        self.family.Bind(wx.EVT_CHOICE, self.on_family_changed)
        self.asym_variant.Bind(wx.EVT_CHOICE, self.on_family_changed)
        self.tab_pad_number.Bind(wx.EVT_CHOICE, self.on_family_changed)
        self.dual_left_count.Bind(wx.EVT_SPINCTRL, self.on_geometry_changed)
        self.dual_right_count.Bind(wx.EVT_SPINCTRL, self.on_geometry_changed)
        self.quad_left_count.Bind(wx.EVT_SPINCTRL, self.on_geometry_changed)
        self.quad_right_count.Bind(wx.EVT_SPINCTRL, self.on_geometry_changed)
        self.quad_top_count.Bind(wx.EVT_SPINCTRL, self.on_geometry_changed)
        self.quad_bottom_count.Bind(wx.EVT_SPINCTRL, self.on_geometry_changed)
        self.model_settings_button.Bind(wx.EVT_BUTTON, self.on_model_settings)
        self.transcribe_button.Bind(wx.EVT_BUTTON, self.on_read_and_prefill)
        self.generate_button.Bind(wx.EVT_BUTTON, self.on_generate)
        self.confirm_button.Bind(wx.EVT_BUTTON, self.on_confirm)
        self.review_queue_button.Bind(wx.EVT_BUTTON, self.on_open_review_queue)
        self.assign_land_button.Bind(wx.EVT_BUTTON, self.on_assign_land_candidate)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.original_image.Bind(wx.EVT_LEFT_DOWN, self.on_original_left_down)
        self.original_image.Bind(wx.EVT_LEFT_DCLICK, self.on_original_double_click)
        self.original_host.Bind(wx.EVT_LEFT_DCLICK, self.on_original_double_click)
        for control in self.geometry_inputs.values():
            control.Bind(wx.EVT_TEXT, self.on_geometry_changed)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def on_close(self, event: wx.CloseEvent) -> None:
        if self.running:
            wx.MessageBox("模型请求仍在运行，请等待完成。", E1A_APP_TITLE, wx.OK | wx.ICON_INFORMATION)
            event.Veto()
            return
        self.temp_dir.cleanup()
        event.Skip()

    def family_code(self) -> str:
        return SUPPORTED_FAMILIES[max(0, self.family.GetSelection())]

    def apply_family_decision(self, decision: FamilyDecision) -> None:
        self.family_decision = decision
        if decision.auto_family is not None:
            family_index = {name: index for index, name in enumerate(SUPPORTED_FAMILIES)}[
                decision.auto_family
            ]
            self._family_programmatic_change = True
            try:
                self.family.SetSelection(family_index)
            finally:
                self._family_programmatic_change = False
            self.family_auto_label.SetLabel("(自动)")
            self.family_auto_label.SetForegroundColour(wx.Colour("#166534"))
            self.family_notice.Hide()
        elif decision.status == "reject_ep":
            self.family_auto_label.SetLabel("(已拒绝)")
            self.family_auto_label.SetForegroundColour(wx.Colour("#B91C1C"))
            self.family_notice.SetLabel(decision.reason)
            self.family_notice.Show()
        else:
            selected_family = self.family_code()
            self.family_auto_label.SetLabel("(按下拉预填)")
            self.family_auto_label.SetForegroundColour(wx.Colour("#7C4A03"))
            self.family_notice.SetLabel(
                f"族自动未定，按下拉所选 {selected_family} 预填：{decision.reason}"
            )
            self.family_notice.Show()
        self.update_family_controls()
        self.Layout()

    def clear_geometry_for_family_review(self) -> None:
        self.geometry_prefill = None
        self.manual_land_assignments = {}
        for key, _label in self.FIELD_LABELS:
            self.geometry_inputs[key].SetValue("")
            self.geometry_sources[key].SetLabel("等待封装族裁决")
        self.geometry_resolution_notice.Hide()
        self.land_resolution_panel.Hide()
        self.land_candidate_list.Clear()
        self.land_target_choice.Clear()
        self.update_family_controls()
        self.update_ratio_warning()
        self.geometry_host.Layout()

    def update_family_controls(self) -> None:
        family = self.family_code()
        asym = family == "ASYM3"
        dual = family == "DUAL"
        quad = family == "QUAD_EP"
        asym_two_small = asym and self.asym_variant.GetSelection() == 0
        current_tab_number = self.tab_pad_number.GetSelection() + 1
        tab_choices = ["大焊盘编号 1", "大焊盘编号 2"]
        if not asym or asym_two_small:
            tab_choices.append("大焊盘编号 3")
        if self.tab_pad_number.GetCount() != len(tab_choices):
            self.tab_pad_number.Clear()
            for choice in tab_choices:
                self.tab_pad_number.Append(choice)
            self.tab_pad_number.SetSelection(min(current_tab_number, len(tab_choices)) - 1)
        self.asym_variant.Show(asym)
        self.tab_pad_number.Show(asym)
        for control in (
            self.dual_row_label,
            self.dual_left_label,
            self.dual_left_count,
            self.dual_right_label,
            self.dual_right_count,
        ):
            control.Show(dual)
        for control in self.quad_controls:
            control.Show(quad)
        for field in ("center_y", "pitch_y", "pitch_x", "tab_x", "tab_y"):
            visible = (
                (
                    field == "pitch_y"
                    and (
                        family in {"SOT3", "GRID4", "DUAL", "QUAD_EP"}
                        or asym_two_small
                    )
                )
                or (field in {"center_y", "pitch_x"} and quad)
                or (field in {"tab_x", "tab_y"} and (asym or quad))
            )
            self.geometry_labels[field].Show(visible)
            self.geometry_inputs[field].Show(visible)
            self.geometry_sources[field].Show(visible)
            if not visible and self.geometry_inputs[field].GetValue():
                self.geometry_inputs[field].ChangeValue("")
                self.geometry_sources[field].SetLabel("-")
        self.geometry_host.Layout()
        self.Layout()

    def set_status(self, text: str) -> None:
        self.status.SetLabel(text)
        self.Layout()

    def set_running(self, value: bool) -> None:
        self.running = value
        for control in (
            self.choose_source_button,
            self.suggest_pdf_button,
            self.load_page_button,
            self.family,
            self.model_settings_button,
            self.transcribe_button,
            self.generate_button,
            self.confirm_button,
            self.assign_land_button,
        ):
            control.Enable(not value)
        self.asym_variant.Enable(not value)
        self.tab_pad_number.Enable(not value)
        self.dual_left_count.Enable(not value)
        self.dual_right_count.Enable(not value)
        self.quad_left_count.Enable(not value)
        self.quad_right_count.Enable(not value)
        self.quad_top_count.Enable(not value)
        self.quad_bottom_count.Enable(not value)
        if not value:
            self.transcribe_button.Enable(self.source_page is not None)
            self.confirm_button.Enable(self.current_candidate is not None)

    def show_error(self, error: Exception | str) -> None:
        message = str(error)
        self.set_status(message)
        wx.MessageBox(message, E1A_APP_TITLE, wx.OK | wx.ICON_ERROR)

    def reset_after_source_change(self) -> None:
        self.transcription = None
        self.anchor_result = None
        self.family_decision = None
        self.geometry_prefill = None
        self.land_candidate_items = ()
        self.land_target_fields = []
        self.manual_land_assignments = {}
        self.current_run_dir = None
        self.current_candidate = None
        self.current_candidate_name = ""
        self.current_mode = ""
        self.dimension_list.DeleteAllItems()
        self.generated_image.load(None)
        self.confirm_button.Disable()
        self.anchor_notice.Hide()
        self.family_notice.Hide()
        self.family_auto_label.SetLabel("")
        self.geometry_resolution_notice.Hide()
        self.land_resolution_panel.Hide()
        self.land_candidate_list.Clear()
        self.land_target_choice.Clear()
        for control in self.geometry_inputs.values():
            control.SetValue("")
        self.update_family_controls()
        for label in self.geometry_sources.values():
            label.SetLabel("-")
        self.update_ratio_warning()
        self.Layout()

    def on_choose_source(self, _event: wx.CommandEvent) -> None:
        wildcard = "PDF 或图片 (*.pdf;*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff;*.webp)|*.pdf;*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff;*.webp"
        dialog = wx.FileDialog(self, "选择封装图", wildcard=wildcard, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            self.source_path.SetValue(dialog.GetPath())
            self.page.SetValue(1)
        finally:
            dialog.Destroy()
        self.pasted_source_path = None
        self.on_load_page(None)

    def on_suggest_pdf_pages(self, _event: wx.CommandEvent) -> None:
        dialog = wx.FileDialog(
            self,
            "选择 Datasheet PDF",
            wildcard="PDF 文件 (*.pdf)|*.pdf",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            pdf_path = Path(dialog.GetPath()).expanduser().resolve()
        finally:
            dialog.Destroy()

        self.set_status("正在分析 PDF 并生成页建议…")
        try:
            with wx.BusyCursor():
                result = suggest_pdf_pages(pdf_path, limit=3)
        except Exception as exc:
            self.show_error(f"自动建议页失败：{exc}")
            return

        suggestion_dialog = PageSuggestionDialog(self, pdf_path, result)
        try:
            if suggestion_dialog.ShowModal() != wx.ID_OK:
                self.set_status("已取消页建议选择。")
                return
            selected_page = suggestion_dialog.selected_page()
        finally:
            suggestion_dialog.Destroy()

        self.source_path.SetValue(str(pdf_path))
        self.page.SetRange(1, max(1, int(result.get("page_count") or 1)))
        self.page.SetValue(selected_page)
        self.pasted_source_path = None
        self.on_load_page(None)

    def _next_pasted_source_path(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = self.store.root / f"pasted_{stamp}.png"
        counter = 2
        while candidate.exists():
            candidate = self.store.root / f"pasted_{stamp}_{counter}.png"
            counter += 1
        return candidate

    def _save_pasted_bitmap(self, bitmap: wx.Bitmap) -> Path:
        if bitmap is None or not bitmap.IsOk():
            raise E1aError("剪贴板中没有图片")
        path = self._next_pasted_source_path()
        if not bitmap.SaveFile(str(path), wx.BITMAP_TYPE_PNG):
            raise E1aError("无法保存剪贴板图片")
        if not path.is_file() or not sha256_file(path):
            raise E1aError("剪贴板图片未能生成有效证据文件")
        return path.resolve()

    def _clipboard_image_file(self) -> Path | None:
        data = wx.FileDataObject()
        if not wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_FILENAME)):
            return None
        if not wx.TheClipboard.GetData(data):
            return None
        for filename in data.GetFilenames():
            path = Path(filename).expanduser().resolve()
            if path.is_file() and path.suffix.lower() in E1A_IMAGE_SUFFIXES:
                return path
        return None

    def on_char_hook(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key not in (ord("V"), ord("v")) or not event.ControlDown():
            event.Skip()
            return
        focus = wx.Window.FindFocus()
        if isinstance(focus, wx.TextCtrl) and focus is not self.source_path:
            event.Skip()
            return
        self.on_paste(event)

    def on_paste(self, _event: wx.KeyEvent | None = None) -> None:
        if self.running:
            self.set_status("模型请求进行中，请等待完成。")
            return
        clipboard = wx.TheClipboard
        if not clipboard.Open():
            self.set_status("剪贴板中没有图片")
            return
        try:
            file_path = self._clipboard_image_file()
            if file_path is not None:
                self.pasted_source_path = None
                self.on_screenshot_dropped(file_path)
                return

            bitmap_data = wx.BitmapDataObject()
            if clipboard.IsSupported(wx.DataFormat(wx.DF_BITMAP)) and clipboard.GetData(bitmap_data):
                pasted_path = self._save_pasted_bitmap(bitmap_data.GetBitmap())
                self.pasted_source_path = pasted_path
                self.on_screenshot_dropped(pasted_path)
                return
        except Exception:
            self.set_status("剪贴板中没有图片")
            return
        finally:
            clipboard.Close()
        self.set_status("剪贴板中没有图片")

    def on_screenshot_dropped(self, path: Path) -> None:
        if self.running:
            self.set_status("模型请求进行中，请等待完成。")
            return
        resolved = path.expanduser().resolve()
        if self.pasted_source_path is not None and resolved != self.pasted_source_path:
            self.pasted_source_path = None
        self.source_path.SetValue(str(resolved))
        self.page.SetValue(1)
        self.set_status("正在载入拖放截图…")
        self.on_load_page(None)

    def on_original_double_click(self, event: wx.MouseEvent) -> None:
        if self._zoom_opening:
            return
        image = self.original_image.image
        if image is None or not image.IsOk():
            self.set_status("当前没有可放大的原封装图。")
            event.Skip()
            return
        self._zoom_opening = True
        dialog = ImageZoomDialog(self, image)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
            self._zoom_opening = False

    def on_original_left_down(self, event: wx.MouseEvent) -> None:
        """Fallback for hosts that do not synthesize EVT_LEFT_DCLICK reliably."""
        now = time.monotonic()
        position = event.GetPosition()
        double_click_ms = wx.SystemSettings.GetMetric(wx.SYS_DCLICK_MSEC)
        if double_click_ms <= 0:
            double_click_ms = 500
        close_enough = (
            self._last_original_click_pos is not None
            and abs(position.x - self._last_original_click_pos.x) <= 8
            and abs(position.y - self._last_original_click_pos.y) <= 8
        )
        if (
            self._last_original_click_at
            and (now - self._last_original_click_at) * 1000 <= double_click_ms
            and close_enough
        ):
            self._last_original_click_at = 0.0
            self._last_original_click_pos = None
            self.on_original_double_click(event)
            return
        self._last_original_click_at = now
        self._last_original_click_pos = position
        event.Skip()

    def on_load_page(self, _event: wx.CommandEvent | None) -> None:
        value = self.source_path.GetValue().strip()
        if not value:
            self.show_error("请先选择 PDF 或截图。")
            return
        if self.pasted_source_path is not None:
            try:
                if Path(value).expanduser().resolve() != self.pasted_source_path:
                    self.pasted_source_path = None
            except OSError:
                self.pasted_source_path = None
        try:
            with wx.BusyCursor():
                source = prepare_source_page(Path(value), self.page.GetValue(), self.source_workspace)
        except Exception as exc:
            self.show_error(exc)
            return
        self.source_page = source
        self.page.SetRange(1, source.page_count)
        self.page.SetValue(source.page)
        self.original_image.load(source.image_path)
        self.reset_after_source_change()
        self.transcribe_button.Enable()
        detail = f"第 {source.page}/{source.page_count} 页" if source.source_kind == "pdf" else "截图"
        self.set_status(f"已载入 {detail}。")

    def on_family_changed(self, _event: wx.CommandEvent) -> None:
        self.current_candidate = None
        self.current_candidate_name = ""
        self.generated_image.load(None)
        self.confirm_button.Disable()
        if self.family_decision is not None and not self._family_programmatic_change:
            self.family_auto_label.SetLabel("(人工改选)")
            self.family_auto_label.SetForegroundColour(wx.Colour("#7C4A03"))
        if self.transcription is not None:
            self.populate_geometry(self.transcription)
        else:
            self.update_family_controls()
            if self.current_mode == "manual_only":
                self.current_run_dir = None
                self.current_mode = ""
            self.anchor_notice.Hide()
            self.Layout()

    def on_model_settings(self, _event: wx.CommandEvent | None) -> bool:
        dialog = ModelSettingsDialog(
            self,
            self.model_base_url,
            self.model_name,
            self.model_api_key,
            self.model_allow_insecure_http,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return False
            base_url, model_name, api_key, allow_insecure_http = dialog.values()
        finally:
            dialog.Destroy()
        if not base_url:
            self.show_error("Base URL 不能为空。")
            return False
        if not model_name:
            self.show_error("模型名称不能为空。")
            return False
        try:
            ModelConfig(
                base_url,
                api_key,
                model_name=model_name,
                allow_insecure_http=allow_insecure_http,
            ).validate()
        except E1aError as exc:
            self.show_error(exc)
            return False
        self.model_base_url = base_url
        self.model_name = model_name
        self.model_api_key = api_key
        self.model_allow_insecure_http = allow_insecure_http
        self.model_label.SetLabel(model_name)
        self.Layout()
        return True

    def on_open_review_queue(self, _event: wx.CommandEvent) -> None:
        dialog = wx.FileDialog(
            self,
            message="选择 review_queue.json",
            wildcard="审核队列 (*.json)|*.json",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            queue_path = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        try:
            review = BatchConfirmationDialog(
                self,
                queue_path,
                Path(self.library_picker.GetPath()),
            )
        except (ConfirmationError, OSError, ValueError, json.JSONDecodeError) as exc:
            self.show_error(exc)
            return
        try:
            review.ShowModal()
        finally:
            review.Destroy()

    def on_read_and_prefill(self, event: wx.CommandEvent | None) -> None:
        """One-click path: model transcription, family gate, prefill, then preview when safe."""

        self.on_transcribe(event)

    def on_transcribe(self, _event: wx.CommandEvent | None) -> None:
        if self.source_page is None:
            self.show_error("请先载入封装图。")
            return
        if self.fixture_path is None and not self.model_base_url:
            if not self.on_model_settings(None):
                return
        mode = "fixture_replay_no_model" if self.fixture_path else "live_model"
        try:
            run_dir = self.store.start(
                source_path=self.source_page.source_path,
                source_image=self.source_page.image_path,
                page=self.source_page.page,
                family=self.family_code(),
                mode=mode,
                model_name=self.model_name,
            )
            (run_dir / "page_text.txt").write_text(
                self.source_page.page_text, encoding="utf-8", newline="\n"
            )
        except Exception as exc:
            self.show_error(exc)
            return
        self.current_run_dir = run_dir
        self.current_mode = mode
        if mode == "live_model":
            self.store.update_status(
                run_dir,
                insecure_http_allowed=self.model_allow_insecure_http,
            )
        self.current_candidate = None
        self.generated_image.load(None)
        self.confirm_button.Disable()
        self.anchor_notice.Hide()
        self.set_running(True)
        if mode == "live_model":
            suffix = "（已明确允许远程 HTTP）" if self.model_allow_insecure_http else ""
            self.set_status("读取尺寸中…" + suffix)
        else:
            self.set_status("载入测试转录中…")
        thread = threading.Thread(target=self._transcribe_worker, args=(run_dir,), daemon=True)
        thread.start()

    def _transcribe_worker(self, run_dir: Path) -> None:
        try:
            if self.fixture_path is not None:
                result = load_fixture(self.fixture_path)
            else:
                result = transcribe_image(
                    self.source_page.image_path,
                    ModelConfig(
                        self.model_base_url,
                        self.model_api_key,
                        model_name=self.model_name,
                        allow_insecure_http=self.model_allow_insecure_http,
                    ),
                )
        except Exception as exc:
            self.store.update_status(run_dir, stage="model_failed", ok=False, error=str(exc))
            wx.CallAfter(self._on_transcribe_failed, exc)
            return
        wx.CallAfter(self._on_transcribe_success, run_dir, result)

    def _on_transcribe_failed(self, exc: Exception) -> None:
        self.set_running(False)
        self.show_error(exc)

    def _on_transcribe_success(self, run_dir: Path, result: ModelResult) -> None:
        prefill: GeometryPrefillResult | None = None
        decision: FamilyDecision | None = None
        consensus_stats: dict[str, Any] | None = None
        role_normalization: dict[str, Any] | None = None
        text_bound_folding: dict[str, Any] | None = None
        prefill_family = self.family_code()
        prefill_family_source = "dropdown"
        auto_preview = False
        try:
            transcription = apply_page_pattern_evidence(
                result.transcription,
                self.source_page.page_text,
            )
            transcription, text_bound_folding = fold_page_text_min_max_bounds(
                transcription,
                self.source_page.page_text,
            )
            display_anchor = anchor_transcription(
                transcription,
                source_kind=self.source_page.source_kind,
                page_text=self.source_page.page_text,
            )
            anchor = display_anchor
            self.store.write_json(run_dir / "model_raw_response.json", result.raw_response)
            for index, raw_response in enumerate(result.sample_raw_responses, start=1):
                self.store.write_json(
                    run_dir / f"model_sample_{index:02d}_raw_response.json",
                    raw_response,
                )
            for index, sample in enumerate(result.sample_transcriptions, start=1):
                self.store.write_json(
                    run_dir / f"model_sample_{index:02d}_transcription.json",
                    sample.to_dict(),
                )
            self.store.write_json(run_dir / "transcription.json", transcription.to_dict())
            self.store.write_json(
                run_dir / "text_bound_folding.json",
                text_bound_folding,
            )
            self.store.write_json(
                run_dir / "transcription_anchor_display.json",
                display_anchor.to_dict(),
            )
            decision = decide_family(transcription, self.source_page.page_text)
            self.store.write_json(run_dir / "family_decision.json", decision.to_dict())
            selected_family = self.family_code()
            prefill_family, prefill_family_source = resolve_prefill_family(
                decision,
                selected_family,
            )
            normalized_transcription, role_normalization = normalize_roles_for_family(
                transcription,
                prefill_family,
            )
            self.store.write_json(
                run_dir / "normalized_transcription.json",
                normalized_transcription.to_dict(),
            )
            self.store.write_json(
                run_dir / "role_normalization.json",
                role_normalization,
            )
            if result.consensus_stats is not None:
                consensus_stats = dict(result.consensus_stats)
                consensus_stats["text_bound_folding"] = text_bound_folding
                consensus_stats["family_role_normalization"] = role_normalization
                self.store.write_json(
                    run_dir / "consensus_ledger.json",
                    consensus_stats,
                )
            request_meta_path = run_dir / "request_meta.json"
            request_meta = json.loads(request_meta_path.read_text(encoding="utf-8"))
            request_meta.update(
                {
                    "family_proposal": decision.model_proposal,
                    "family_proposal_reason": decision.model_reason,
                    "family_evidence": decision.evidence_family,
                    "family_auto": decision.auto_family,
                    "consensus": consensus_stats,
                    "text_bound_folding": text_bound_folding,
                    "family_role_normalization": role_normalization,
                    "sample_request_ids": list(result.sample_request_ids),
                }
            )
            self.store.write_json(request_meta_path, request_meta)
            page_has_both = page_mentions_lands_and_paste(self.source_page.page_text)
            self.store.update_status(
                run_dir,
                stage="transcribed",
                ok=True,
                model_mode=result.mode,
                request_id=result.request_id,
                anchor_can_generate=display_anchor.can_generate,
                anchor_hint_only=True,
                page_mentions_lands_and_paste=page_has_both,
                family_model_proposal=decision.model_proposal,
                family_evidence=decision.evidence_family,
                family_auto=decision.auto_family,
                family_gate_status=decision.status,
                consensus=consensus_stats,
                text_bound_folding=text_bound_folding,
                family_role_normalization=role_normalization,
            )
            self.transcription = normalized_transcription
            self.anchor_result = display_anchor
            self.current_mode = result.mode
            self.apply_family_decision(decision)
            self.populate_dimensions(normalized_transcription, display_anchor)
            if self.family_code() != prefill_family:
                raise E1aError("预填族与当前下拉族不一致")
            prefill = self.populate_geometry(normalized_transcription)
            anchor = anchor_used_geometry_values(
                normalized_transcription,
                prefill.values,
                source_kind=self.source_page.source_kind,
                page_text=self.source_page.page_text,
            )
            self.anchor_result = anchor
            self.store.write_json(run_dir / "anchor_check.json", anchor.to_dict())
            prefill_payload = prefill.to_dict()
            prefill_payload["family"] = prefill_family
            prefill_payload["family_source"] = prefill_family_source
            prefill_payload["family_decision"] = decision.to_dict()
            self.store.write_json(run_dir / "geometry_prefill.json", prefill_payload)
            geometry_metrics = {
                "auto_pads": prefill.auto_pads,
                "auto_full": prefill.auto_full,
                "body_source": prefill.body_source,
                "prefill_status": prefill.status,
                "mapping_source": prefill.body_axis_provenance.get("mapping_source"),
            }
            if consensus_stats is not None:
                consensus_stats["body_source"] = prefill.body_source
                consensus_stats["mapping_source"] = geometry_metrics["mapping_source"]
                consensus_stats["geometry_metrics"] = geometry_metrics
                self.store.write_json(
                    run_dir / "consensus_ledger.json",
                    consensus_stats,
                )
            request_meta["body_source"] = prefill.body_source
            request_meta["mapping_source"] = geometry_metrics["mapping_source"]
            request_meta["geometry_metrics"] = geometry_metrics
            self.store.write_json(request_meta_path, request_meta)
            self.store.update_status(
                run_dir,
                family_prefill=prefill_family,
                family_prefill_source=prefill_family_source,
                body_source=prefill.body_source,
                mapping_source=geometry_metrics["mapping_source"],
                auto_pads=prefill.auto_pads,
                auto_full=prefill.auto_full,
                anchor_can_generate=True,
                anchor_hint_only=True,
                anchor_used_value_count=len(anchor.checks),
            )
        except Exception as exc:
            self.store.update_status(run_dir, stage="postprocess_failed", ok=False, error=str(exc))
            self.set_running(False)
            self.show_error(exc)
            return
        self.set_running(False)
        if prefill is not None and prefill.auto_pads:
            self.on_generate(None)
            auto_preview = self.current_candidate is not None
        mode_label = "测试回放，未调用模型；" if result.mode != "live_model" else ""
        status_parts = [mode_label + anchor.message]
        if consensus_stats is not None:
            stats = consensus_stats
            status_parts.append(
                "共识采样 "
                f"{stats['sample_success_count']}/{stats['sample_attempt_count']} 次成功，"
                f"一致 {stats['initial_role_agreement_count']} 条，"
                f"分歧 {stats['initial_disagreement_count']} 条"
            )
        if role_normalization is not None and role_normalization["applied"]:
            status_parts.append(
                "族感知角色归一 "
                f"{role_normalization['normalized_dimension_count']} 条；"
                "尺寸表已保留原角色"
            )
        if decision is not None:
            status_parts.append("族裁决：" + decision.reason)
            if decision.auto_family is None and decision.status != "reject_ep":
                status_parts.append(
                    f"族自动未定，按下拉所选 {prefill_family} 预填"
                )
        if prefill is not None:
            status_parts.append(prefill.message)
            status_parts.extend(prefill.warnings)
            status_parts.append(
                f"指标 auto_pads={str(prefill.auto_pads).lower()} / "
                f"auto_full={str(prefill.auto_full).lower()} / "
                f"body_source={prefill.body_source} / "
                f"mapping_source={prefill.body_axis_provenance.get('mapping_source')}"
            )
        else:
            status_parts.append("尚未预填几何；请先人工选择封装族")
        if auto_preview:
            status_parts.append("已自动生成 footprint 预览；确认前尚未写入目标库")
        if page_mentions_lands_and_paste(self.source_page.page_text):
            status_parts.append("所选页文字层同时出现 lands 与 paste；请核对归属列")
        self.set_status("；".join(part.rstrip("。") for part in status_parts if part) + "。")

    def populate_dimensions(self, transcription: Transcription, anchor: AnchorResult) -> None:
        self.dimension_list.DeleteAllItems()
        checks = list(anchor.checks)
        for row, item in enumerate(transcription.dimensions):
            index = self.dimension_list.InsertItem(row, item.symbol)
            self.dimension_list.SetItem(index, 1, item.value)
            self.dimension_list.SetItem(index, 2, item.unit)
            self.dimension_list.SetItem(index, 3, geometry_role_label(item))
            self.dimension_list.SetItem(index, 4, dimension_role_label(item))
            self.dimension_list.SetItem(index, 5, item.endpoints)
            self.dimension_list.SetItem(
                index, 6, "推导-需确认" if item.is_derived else "图上直标"
            )
            self.dimension_list.SetItem(index, 7, item.raw)
            anchored = checks[row].get("anchored")
            anchor_text = "人工目视" if anchored is None else ("命中" if anchored else "未命中")
            self.dimension_list.SetItem(index, 8, anchor_text)

    def populate_geometry(self, transcription: Transcription) -> GeometryPrefillResult:
        try:
            result = infer_geometry_prefill(
                transcription,
                self.family_code(),
                page_text=self.source_page.page_text if self.source_page else None,
            )
        except E1aError as exc:
            self.show_error(exc)
            raise
        self.geometry_prefill = result
        self.manual_land_assignments = {}
        for key, _label in self.FIELD_LABELS:
            value = result.values.get(key)
            self.geometry_inputs[key].SetValue("" if value is None else f"{value:.4f}".rstrip("0").rstrip("."))
            missing_source = (
                "本体尺寸未定，需人工填写"
                if key in {"body_x", "body_y"} and value is None
                else "人工填写"
            )
            self.geometry_sources[key].SetLabel(result.sources.get(key, missing_source))
        if result.values.get("dual_left_count") is not None:
            self.dual_left_count.SetValue(int(result.values["dual_left_count"]))
        if result.values.get("dual_right_count") is not None:
            self.dual_right_count.SetValue(int(result.values["dual_right_count"]))
        for field, control in (
            ("quad_left_count", self.quad_left_count),
            ("quad_right_count", self.quad_right_count),
            ("quad_top_count", self.quad_top_count),
            ("quad_bottom_count", self.quad_bottom_count),
        ):
            if result.values.get(field) is not None:
                control.SetValue(int(result.values[field]))
        self.update_family_controls()
        self.show_geometry_prefill(result)
        self.update_ratio_warning()
        self.geometry_host.Layout()
        return result

    def show_geometry_prefill(self, result: GeometryPrefillResult) -> None:
        self.geometry_resolution_notice.SetLabel(geometry_prefill_notice_text(result))
        needs_attention = not result.auto_full or any(
            "已放回" in warning for warning in result.warnings
        )
        if needs_attention:
            self.geometry_resolution_notice.SetBackgroundColour(wx.Colour("#FFF3BF"))
            self.geometry_resolution_notice.SetForegroundColour(wx.Colour("#7C4A03"))
        else:
            self.geometry_resolution_notice.SetBackgroundColour(wx.Colour("#E8F5E9"))
            self.geometry_resolution_notice.SetForegroundColour(wx.Colour("#166534"))
        self.geometry_resolution_notice.Show()

        self.land_candidate_items = result.land_dimensions
        self.land_candidate_list.Clear()
        excluded = set(result.excluded_half_dimensions)
        for item in result.land_dimensions:
            suffix = " [半值候选]" if item in excluded else ""
            if item.is_derived:
                suffix += " [推导值-禁止自动使用]"
            self.land_candidate_list.Append(
                f"[{geometry_role_label(item)} / {dimension_role_label(item)}] "
                f"{item.value} {item.unit} | {item.endpoints} | {item.raw}{suffix}"
            )
        self.land_target_choice.Clear()
        labels = dict(self.FIELD_LABELS)
        self.land_target_fields = [
            field
            for field in self.LAND_TARGET_FIELDS
            if (
                (field in {"tab_x", "tab_y"} and self.family_code() == "ASYM3")
                or (
                    field == "pitch_y"
                    and (
                        self.family_code() in {"SOT3", "GRID4", "DUAL"}
                        or (
                            self.family_code() == "ASYM3"
                            and self.asym_variant.GetSelection() == 0
                        )
                    )
                )
                or field not in {"pitch_y", "tab_x", "tab_y"}
            )
        ]
        for field in self.land_target_fields:
            self.land_target_choice.Append(labels[field])
        if self.land_candidate_items:
            self.land_candidate_list.SetSelection(0)
        if self.land_target_fields:
            self.land_target_choice.SetSelection(0)
        self.land_resolution_panel.Show(not result.auto_pads)
        self.geometry_host.Layout()

    def on_assign_land_candidate(self, _event: wx.CommandEvent) -> None:
        item_index = self.land_candidate_list.GetSelection()
        field_index = self.land_target_choice.GetSelection()
        if item_index == wx.NOT_FOUND or field_index == wx.NOT_FOUND:
            self.show_error("请先选择 LAND 数值和目标字段。")
            return
        item = self.land_candidate_items[item_index]
        field = self.land_target_fields[field_index]
        if dimension_pattern_role(item) == "paste":
            self.show_error("焊膏开口 / paste 数值禁止填入铜箔焊盘几何。")
            return
        if item.is_derived:
            self.show_error("is_derived=true 的模型推导值不得直接用于几何；请回原图人工确认。")
            return
        value = dimension_value_mm(item)
        self.geometry_inputs[field].SetValue(f"{value:.4f}".rstrip("0").rstrip("."))
        self.geometry_sources[field].SetLabel(f"人工点选 LAND：{item.raw}")
        self.manual_land_assignments[field] = item
        self.update_ratio_warning()
        self.geometry_host.Layout()

    def geometry_fields(self) -> dict[str, str]:
        fields = {
            key: control.GetValue().strip() for key, control in self.geometry_inputs.items()
        }
        fields["small_pad_count"] = "1" if self.asym_variant.GetSelection() == 1 else "2"
        fields["tab_pad_number"] = str(self.tab_pad_number.GetSelection() + 1)
        fields["dual_left_count"] = str(self.dual_left_count.GetValue())
        fields["dual_right_count"] = str(self.dual_right_count.GetValue())
        fields["quad_left_count"] = str(self.quad_left_count.GetValue())
        fields["quad_right_count"] = str(self.quad_right_count.GetValue())
        fields["quad_top_count"] = str(self.quad_top_count.GetValue())
        fields["quad_bottom_count"] = str(self.quad_bottom_count.GetValue())
        return fields

    def on_geometry_changed(self, _event: wx.CommandEvent) -> None:
        self.update_ratio_warning()

    def update_ratio_warning(self) -> None:
        provenance = (
            self.geometry_prefill.body_axis_provenance
            if self.geometry_prefill is not None
            else None
        )
        pairs = half_double_warning_pairs(self.geometry_fields(), provenance)
        if not pairs:
            self.ratio_warning.Hide()
            self.Layout()
            return
        labels = dict(self.FIELD_LABELS)
        pair_text = "；".join(
            f"{labels.get(left, left)} ↔ {labels.get(right, right)}"
            for left, right in pairs[:3]
        )
        suffix = "；另有更多组合" if len(pairs) > 3 else ""
        self.ratio_warning.SetLabel(
            f"疑似把半尺寸当独立尺寸，请确认：{pair_text}{suffix}"
        )
        self.ratio_warning.Show()
        self.Layout()

    def ensure_manual_run(self) -> Path:
        if self.source_page is None:
            raise E1aError("请先载入封装图。")
        if self.current_mode == "manual_only" and self.current_run_dir is not None:
            return self.current_run_dir
        run_dir = self.store.start(
            source_path=self.source_page.source_path,
            source_image=self.source_page.image_path,
            page=self.source_page.page,
            family=self.family_code(),
            mode="manual_only",
        )
        if self.pasted_source_path is not None:
            pasted_source = self.pasted_source_path.resolve()
            pasted_copy = run_dir / pasted_source.name
            shutil.copy2(pasted_source, pasted_copy)
            pasted_sha256 = sha256_file(pasted_copy)
            request_meta_path = run_dir / "request_meta.json"
            request_meta = json.loads(request_meta_path.read_text(encoding="utf-8"))
            request_meta.update(
                {
                    "pasted_source_file": pasted_copy.name,
                    "pasted_source_sha256": pasted_sha256,
                }
            )
            self.store.write_json(request_meta_path, request_meta)
            self.store.write_json(
                run_dir / "pasted_source.json",
                {
                    "source_file": pasted_copy.name,
                    "source_sha256": pasted_sha256,
                    "staged_path": str(pasted_source),
                },
            )
            self.store.update_status(
                run_dir,
                pasted_source_file=pasted_copy.name,
                pasted_source_sha256=pasted_sha256,
            )
        (run_dir / "page_text.txt").write_text(
            self.source_page.page_text,
            encoding="utf-8",
            newline="\n",
        )
        self.store.write_json(
            run_dir / "anchor_check.json",
            {
                "source_kind": self.source_page.source_kind,
                "checks": [],
                "can_generate": True,
                "anchor_status": "manual_only",
                "message": "本次几何为人工输入，未做原文锚定",
            },
        )
        self.store.update_status(
            run_dir,
            stage="manual_input_ready",
            ok=True,
            model_mode="manual_only",
            anchor_status="manual_only",
        )
        self.current_run_dir = run_dir
        self.current_mode = "manual_only"
        return run_dir

    def on_generate(self, _event: wx.CommandEvent) -> None:
        if self.source_page is None:
            self.show_error("请先载入封装图。")
            return
        manual_only = self.transcription is None
        if not manual_only and (self.anchor_result is None or self.current_run_dir is None):
            self.show_error("模型转录状态不完整，请重新读取尺寸。")
            return
        if not manual_only and not self.anchor_result.can_generate:
            self.show_error(self.anchor_result.message)
            return
        run_dir: Path | None = None
        try:
            run_dir = self.ensure_manual_run() if manual_only else self.current_run_dir
            if run_dir is None:
                raise E1aError("运行目录尚未建立。")
            name = validate_footprint_name(self.footprint_name.GetValue())
            geometry_fields = self.geometry_fields()
            prefill_body_source = (
                self.geometry_prefill.body_source
                if self.geometry_prefill is not None
                else "unresolved"
            )
            geometry_body_source = (
                "manual"
                if manual_only
                else (
                    prefill_body_source
                    if prefill_body_source
                    in {"document", "absent_requires_user_input"}
                    else "user_manual"
                )
            )
            geometry = geometry_from_fields(
                self.family_code(),
                geometry_fields,
                require_all=manual_only,
                body_source=geometry_body_source,
            )
            if not manual_only and self.geometry_prefill is not None:
                prefill_body = (
                    self.geometry_prefill.values.get("body_x"),
                    self.geometry_prefill.values.get("body_y"),
                )
                geometry_body = (geometry.body_x, geometry.body_y)
                prefill_complete = all(value is not None for value in prefill_body)
                geometry_complete = all(value is not None for value in geometry_body)
                if prefill_complete and geometry_complete:
                    body_matches_prefill = math.isclose(
                        float(geometry.body_x),
                        float(prefill_body[0]),
                        abs_tol=1e-9,
                    ) and math.isclose(
                        float(geometry.body_y),
                        float(prefill_body[1]),
                        abs_tol=1e-9,
                    )
                    if not body_matches_prefill:
                        geometry = replace(geometry, body_source="user_manual")
                elif geometry_complete:
                    geometry = replace(geometry, body_source="user_manual")
                else:
                    geometry = replace(
                        geometry,
                        body_source="absent_requires_user_input",
                    )
            if self.transcription is not None:
                validate_lands_paste_guard(
                    geometry,
                    self.transcription,
                    manual_assignments=self.manual_land_assignments,
                )
            expected_pins = geometry.pin_count
            if (
                self.transcription is not None
                and self.transcription.pin_count is not None
                and self.transcription.pin_count != expected_pins
            ):
                raise E1aError(
                    f"模型 pin_count={self.transcription.pin_count}，与所选族的 {expected_pins} 个焊盘不一致。"
                )
            anchor_status = (
                "manual_only"
                if manual_only
                else (
                    "pdf_text_exact"
                    if self.anchor_result.source_kind == "pdf"
                    else "image_human_review"
                )
            )
            candidate = run_dir / f"{name}.kicad_mod"
            preview = run_dir / "footprint_preview.png"
            write_candidate(
                candidate,
                name,
                geometry,
                source_sha256=sha256_file(self.source_page.source_path),
                anchor_status=anchor_status,
            )
            render_footprint_png(geometry, preview, name=name)
            self.store.write_json(
                run_dir / "geometry.json",
                {
                    "family": geometry.family,
                    "pad_x": geometry.pad_x,
                    "pad_y": geometry.pad_y,
                    "center_x": geometry.center_x,
                    "center_y": geometry.center_y,
                    "pitch_y": geometry.pitch_y,
                    "pitch_x": geometry.pitch_x,
                    "tab_x": geometry.tab_x,
                    "tab_y": geometry.tab_y,
                    "small_pad_count": geometry.small_pad_count,
                    "tab_pad_number": geometry.tab_pad_number,
                    "quad_left_count": geometry.quad_left_count,
                    "quad_right_count": geometry.quad_right_count,
                    "quad_top_count": geometry.quad_top_count,
                    "quad_bottom_count": geometry.quad_bottom_count,
                    "body_x": geometry.body_x,
                    "body_y": geometry.body_y,
                    "body_source": geometry.body_source,
                    "mapping_source": (
                        self.geometry_prefill.body_axis_provenance.get("mapping_source")
                        if self.geometry_prefill is not None
                        else None
                    ),
                    "auto_pads": (
                        self.geometry_prefill.auto_pads
                        if self.geometry_prefill is not None
                        else False
                    ),
                    "auto_full": (
                        self.geometry_prefill.auto_full
                        if self.geometry_prefill is not None
                        else False
                    ),
                    "pin_count": geometry.pin_count,
                    "anchor_status": anchor_status,
                },
            )
            self.store.update_status(
                run_dir,
                stage="preview_ready",
                ok=True,
                error="",
                candidate_sha256=sha256_file(candidate),
                anchor_status=anchor_status,
                body_source=geometry.body_source,
                mapping_source=(
                    self.geometry_prefill.body_axis_provenance.get("mapping_source")
                    if self.geometry_prefill is not None
                    else None
                ),
            )
        except Exception as exc:
            if run_dir is not None:
                self.store.update_status(
                    run_dir,
                    stage="preview_failed",
                    ok=False,
                    error=str(exc),
                    anchor_status="manual_only" if manual_only else None,
                )
            self.show_error(exc)
            return
        self.current_candidate = candidate
        self.current_candidate_name = name
        self.generated_image.load(preview)
        self.confirm_button.Enable()
        self.anchor_notice.Show(manual_only)
        self.Layout()
        if manual_only:
            self.set_status("本次几何为人工输入，未做原文锚定；确认前尚未写入目标库。")
        else:
            self.set_status("footprint 已渲染；确认前尚未写入目标库。")

    def on_confirm(self, _event: wx.CommandEvent) -> None:
        if self.current_candidate is None or self.current_run_dir is None:
            self.show_error("请先生成 footprint 预览。")
            return
        try:
            name = validate_footprint_name(self.footprint_name.GetValue())
        except E1aError as exc:
            self.show_error(exc)
            return
        if name != self.current_candidate_name:
            self.show_error("Footprint 名称已变化，请重新生成预览后再确认。")
            return
        library = Path(self.library_picker.GetPath())
        target = library / f"{name}.kicad_mod"
        message = f"确认左右图已经核对，并将 footprint 写入：\n{target}"
        if wx.MessageBox(message, E1A_APP_TITLE, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) != wx.YES:
            self.store.update_status(self.current_run_dir, stage="preview_rejected", ok=True, confirmed=False)
            self.set_status("未确认；目标库没有写入。")
            return
        overwrite = False
        if target.exists():
            overwrite = (
                wx.MessageBox(
                    "目标 footprint 已存在，确认覆盖？",
                    E1A_APP_TITLE,
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
                )
                == wx.YES
            )
            if not overwrite:
                self.set_status("取消覆盖；目标库没有变化。")
                return
        try:
            committed = commit_candidate(
                self.current_candidate,
                library,
                name,
                confirmed=True,
                overwrite=overwrite,
            )
            self.store.update_status(
                self.current_run_dir,
                stage="committed",
                ok=True,
                error="",
                confirmed=True,
                committed_path=str(committed),
                committed_sha256=sha256_file(committed),
            )
        except Exception as exc:
            self.store.update_status(
                self.current_run_dir, stage="commit_failed", ok=False, error=str(exc)
            )
            self.show_error(exc)
            return
        self.set_status(f"已写入 {committed.name}")
        wx.MessageBox(f"已写入：\n{committed}", E1A_APP_TITLE, wx.OK | wx.ICON_INFORMATION)

class KiCadFootprintBuilderPlugin(pcbnew.ActionPlugin):
    def defaults(self) -> None:
        self.name = "封装建库（读图）"
        self.category = "Library"
        self.description = "直接选择 PDF 或封装截图，预览并确认后写入 KiCad footprint 库。"
        self.show_toolbar_button = True
        self.icon_file_name = ""

    def Run(self) -> None:
        dialog = E1aDialog(None)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
