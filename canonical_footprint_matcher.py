from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


REGISTRY_SCHEMA = "kicad_canonical_footprint_registry_v1"
MATCH_RESULT_SCHEMA = "kicad_canonical_footprint_match_v1"
PAD_SIGNATURE_SCHEMA = "kicad_pad_signature_v1"
MAX_TOLERANCE_MM = 0.001


class SExpressionError(ValueError):
    pass


def normalize_package_code(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sha256_json(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _tokenize(text: str) -> Iterator[str]:
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character == ";":
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if character in "()":
            yield character
            index += 1
            continue
        if character == '"':
            index += 1
            value: list[str] = []
            while index < length:
                character = text[index]
                if character == '"':
                    index += 1
                    break
                if character == "\\":
                    index += 1
                    if index >= length:
                        raise SExpressionError("unterminated escape in quoted string")
                    escaped = text[index]
                    value.append({"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped))
                    index += 1
                    continue
                value.append(character)
                index += 1
            else:
                raise SExpressionError("unterminated quoted string")
            yield "".join(value)
            continue
        end = index
        while end < length and not text[end].isspace() and text[end] not in "()":
            end += 1
        yield text[index:end]
        index = end


def parse_sexpressions(text: str) -> list[Any]:
    roots: list[Any] = []
    stack: list[list[Any]] = []
    for token in _tokenize(text):
        if token == "(":
            node: list[Any] = []
            if stack:
                stack[-1].append(node)
            stack.append(node)
        elif token == ")":
            if not stack:
                raise SExpressionError("unexpected closing parenthesis")
            completed = stack.pop()
            if not stack:
                roots.append(completed)
        else:
            if not stack:
                raise SExpressionError("atom outside an expression")
            stack[-1].append(token)
    if stack:
        raise SExpressionError("unterminated expression")
    return roots


def _walk_nodes(value: Any, name: str) -> Iterator[list[Any]]:
    if not isinstance(value, list):
        return
    if value and value[0] == name:
        yield value
    for child in value:
        if isinstance(child, list):
            yield from _walk_nodes(child, name)


def _first_child(node: Sequence[Any], name: str) -> list[Any] | None:
    for child in node:
        if isinstance(child, list) and child and child[0] == name:
            return child
    return None


def _float_atom(value: Any, field: str) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError) as exc:
        raise SExpressionError(f"{field} is not numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise SExpressionError(f"{field} is not finite")
    return number


def _numeric_child(
    node: Sequence[Any],
    name: str,
    count: int,
    *,
    defaults: Sequence[float] | None = None,
) -> list[float]:
    child = _first_child(node, name)
    if child is None:
        if defaults is None:
            raise SExpressionError(f"pad lacks required ({name} ...) field")
        return [float(value) for value in defaults]
    if len(child) < count + 1:
        raise SExpressionError(f"pad ({name} ...) has too few values")
    return [_float_atom(child[index + 1], name) for index in range(count)]


def _canonical_extra(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonical_extra(item) for item in value]
    text = str(value)
    try:
        number = float(text)
    except ValueError:
        return text
    if not math.isfinite(number):
        return text
    return round(number, 12)


def _pad_from_node(node: Sequence[Any]) -> dict[str, Any]:
    if len(node) < 4:
        raise SExpressionError("pad node lacks number, type, or shape")
    at = _numeric_child(node, "at", 2)
    at_node = _first_child(node, "at") or []
    rotation = _float_atom(at_node[3], "at rotation") if len(at_node) >= 4 else 0.0
    size = _numeric_child(node, "size", 2)
    layers_node = _first_child(node, "layers")
    if layers_node is None or len(layers_node) < 2:
        raise SExpressionError("pad lacks layers")
    known_children = {
        "at",
        "size",
        "layers",
        "roundrect_rratio",
        "solder_mask_margin",
        "solder_paste_margin",
        "uuid",
        "tstamp",
    }
    extras = [
        _canonical_extra(child)
        for child in node[4:]
        if isinstance(child, list) and child and child[0] not in known_children
    ]
    return {
        "number": str(node[1]),
        "type": str(node[2]),
        "shape": str(node[3]),
        "at_x": at[0],
        "at_y": at[1],
        "rotation": rotation,
        "size_x": size[0],
        "size_y": size[1],
        "layers": sorted(str(value) for value in layers_node[1:]),
        "roundrect_rratio": _numeric_child(
            node,
            "roundrect_rratio",
            1,
            defaults=(0.0,),
        )[0],
        "solder_mask_margin": _numeric_child(
            node,
            "solder_mask_margin",
            1,
            defaults=(0.0,),
        )[0],
        "solder_paste_margin": _numeric_child(
            node,
            "solder_paste_margin",
            1,
            defaults=(0.0,),
        )[0],
        "extra_fields": extras,
    }


def _pad_sort_key(pad: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(pad["number"]),
        str(pad["type"]),
        str(pad["shape"]),
        tuple(pad["layers"]),
        round(float(pad["at_x"]), 9),
        round(float(pad["at_y"]), 9),
        round(float(pad["size_x"]), 9),
        round(float(pad["size_y"]), 9),
    )


def pad_signature_from_text(text: str) -> dict[str, Any]:
    roots = parse_sexpressions(text)
    pad_nodes: list[list[Any]] = []
    for root in roots:
        pad_nodes.extend(_walk_nodes(root, "pad"))
    if not pad_nodes:
        raise SExpressionError("footprint contains no pad nodes")
    pads = sorted((_pad_from_node(node) for node in pad_nodes), key=_pad_sort_key)
    payload = {
        "schema": PAD_SIGNATURE_SCHEMA,
        "pad_count": len(pads),
        "pads": pads,
    }
    payload["signature_sha256"] = sha256_json(payload)
    return payload


def pad_signature_from_file(path: Path) -> dict[str, Any]:
    return pad_signature_from_text(path.read_text(encoding="utf-8"))


_NUMERIC_PAD_FIELDS = (
    "at_x",
    "at_y",
    "rotation",
    "size_x",
    "size_y",
    "roundrect_rratio",
    "solder_mask_margin",
    "solder_paste_margin",
)
_TEXT_PAD_FIELDS = ("number", "type", "shape", "layers", "extra_fields")


def compare_pad_signatures(
    candidate: dict[str, Any],
    canonical: dict[str, Any],
    *,
    tolerance_mm: float = MAX_TOLERANCE_MM,
) -> dict[str, Any]:
    tolerance = float(tolerance_mm)
    if not 0.0 <= tolerance <= MAX_TOLERANCE_MM:
        raise ValueError(f"tolerance must be between 0 and {MAX_TOLERANCE_MM}")
    candidate_pads = list(candidate.get("pads") or [])
    canonical_pads = list(canonical.get("pads") or [])
    differences: list[dict[str, Any]] = []
    if len(candidate_pads) != len(canonical_pads):
        differences.append(
            {
                "field": "pad_count",
                "document_value": len(candidate_pads),
                "canonical_value": len(canonical_pads),
                "within_tolerance": False,
            }
        )
    for index, (document_pad, canonical_pad) in enumerate(
        zip(candidate_pads, canonical_pads),
        start=1,
    ):
        for field in _TEXT_PAD_FIELDS:
            document_value = document_pad.get(field)
            canonical_value = canonical_pad.get(field)
            if document_value != canonical_value:
                differences.append(
                    {
                        "field": f"pads[{index}].{field}",
                        "document_value": document_value,
                        "canonical_value": canonical_value,
                        "within_tolerance": False,
                    }
                )
        for field in _NUMERIC_PAD_FIELDS:
            document_value = float(document_pad.get(field) or 0.0)
            canonical_value = float(canonical_pad.get(field) or 0.0)
            delta = document_value - canonical_value
            if abs(delta) > tolerance:
                differences.append(
                    {
                        "field": f"pads[{index}].{field}",
                        "document_value": document_value,
                        "canonical_value": canonical_value,
                        "delta_mm": delta,
                        "within_tolerance": False,
                    }
                )
    return {
        "exact_within_tolerance": not differences,
        "tolerance_mm": tolerance,
        "document_pad_count": len(candidate_pads),
        "canonical_pad_count": len(canonical_pads),
        "difference_count": len(differences),
        "differences": differences,
    }


def _package_token_evidence(payload: Any) -> dict[str, Any]:
    occurrences: list[dict[str, str]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if key == "package_tokens" and isinstance(item, list):
                    for index, token in enumerate(item):
                        normalized = str(token).strip().upper()
                        if normalized:
                            occurrences.append(
                                {
                                    "token": normalized,
                                    "json_path": f"{child_path}[{index}]",
                                }
                            )
                walk(item, child_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(payload, "")
    tokens = sorted({item["token"] for item in occurrences})
    if len(tokens) == 1:
        status = "exact_unique"
        package_code: str | None = tokens[0]
    elif not tokens:
        status = "unresolved_no_exact_package_token"
        package_code = None
    else:
        status = "unresolved_multiple_exact_package_tokens"
        package_code = None
    return {
        "status": status,
        "package_code": package_code,
        "tokens": tokens,
        "occurrences": occurrences,
        "inference_used": False,
    }


def package_token_evidence(payload: Any) -> dict[str, Any]:
    return _package_token_evidence(payload)


def load_registry(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if payload.get("schema") != REGISTRY_SCHEMA:
        raise ValueError("unsupported canonical registry schema")
    tolerance = float(payload.get("tolerance_mm", MAX_TOLERANCE_MM))
    if not 0.0 <= tolerance <= MAX_TOLERANCE_MM:
        raise ValueError("canonical registry tolerance exceeds the frozen 0.001 mm gate")
    payload["_loaded_from"] = str(resolved)
    payload["_loaded_sha256"] = sha256_file(resolved)
    return payload


def approved_canonical_for_package(
    registry: dict[str, Any] | None,
    package_names: Iterable[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "registry_not_configured",
        "matched_package_names": [],
        "matched_group_ids": [],
        "canonical_entry": None,
        "canonical_group": None,
        "reason": "canonical registry was not configured",
    }
    if registry is None:
        return result
    if str(registry.get("authority") or "") != "user_approved":
        result.update(
            status="registry_not_user_approved",
            reason="canonical registry root is not user_approved",
        )
        return result

    requested = {
        normalize_package_code(value)
        for value in package_names
        if normalize_package_code(value)
    }
    result["matched_package_names"] = sorted(requested)
    entries = {
        str(entry.get("entry_id")): entry
        for entry in registry.get("entries") or []
        if isinstance(entry, dict) and entry.get("entry_id")
    }
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for group in registry.get("canonical_groups") or []:
        if not isinstance(group, dict):
            continue
        if str(group.get("approval_status") or "") != "user_approved":
            continue
        group_names = {
            normalize_package_code(value)
            for value in (
                group.get("package_code"),
                *(group.get("package_aliases") or []),
            )
            if normalize_package_code(value)
        }
        if not requested.intersection(group_names):
            continue
        entry = entries.get(str(group.get("proposed_canonical_entry_id") or ""))
        if entry is not None:
            matches.append((group, entry))

    result["matched_group_ids"] = sorted(
        str(group.get("group_id") or "") for group, _entry in matches
    )
    if not matches:
        result.update(
            status="approved_canonical_not_found",
            reason="no user-approved canonical group matches the recognized package",
        )
        return result
    if len(matches) != 1:
        result.update(
            status="approved_canonical_ambiguous",
            reason="multiple user-approved canonical groups match the recognized package",
        )
        return result
    group, entry = matches[0]
    result.update(
        status="approved_canonical_unique",
        canonical_group=group,
        canonical_entry=entry,
        reason="one user-approved canonical group matches the recognized package",
    )
    return result


def body_cross_check(
    document_values: dict[str, Any],
    canonical_entry: dict[str, Any],
    *,
    tolerance_mm: float = MAX_TOLERANCE_MM,
) -> dict[str, Any]:
    tolerance = float(tolerance_mm)
    if not 0.0 <= tolerance <= MAX_TOLERANCE_MM:
        raise ValueError(f"tolerance must be between 0 and {MAX_TOLERANCE_MM}")
    canonical_values = dict(canonical_entry.get("values") or {})
    document = (document_values.get("body_x"), document_values.get("body_y"))
    canonical = (canonical_values.get("body_x"), canonical_values.get("body_y"))
    base = {
        "schema": "kicad_canonical_body_cross_check_v1",
        "policy": "reject_only",
        "tolerance_mm": tolerance,
        "document_body_mm": list(document),
        "canonical_body_mm": list(canonical),
        "direct_axis": None,
        "swapped_axis": None,
        "accepted_axis_mapping": None,
    }
    if not all(isinstance(value, (int, float)) for value in document):
        return {
            **base,
            "status": "not_evaluated_document_body_missing",
            "mapping_allowed": True,
            "reason": "document body is incomplete; reject-only cross-check has no negative evidence",
        }
    if not all(isinstance(value, (int, float)) for value in canonical):
        return {
            **base,
            "status": "not_evaluated_canonical_body_missing",
            "mapping_allowed": True,
            "reason": "canonical body is incomplete; reject-only cross-check has no negative evidence",
        }

    document_x, document_y = (float(value) for value in document)
    canonical_x, canonical_y = (float(value) for value in canonical)
    direct = {
        "delta_x_mm": document_x - canonical_x,
        "delta_y_mm": document_y - canonical_y,
    }
    direct["within_tolerance"] = (
        abs(direct["delta_x_mm"]) <= tolerance
        and abs(direct["delta_y_mm"]) <= tolerance
    )
    swapped = {
        "delta_x_mm": document_x - canonical_y,
        "delta_y_mm": document_y - canonical_x,
    }
    swapped["within_tolerance"] = (
        abs(swapped["delta_x_mm"]) <= tolerance
        and abs(swapped["delta_y_mm"]) <= tolerance
    )
    base["direct_axis"] = direct
    base["swapped_axis"] = swapped
    if direct["within_tolerance"] or swapped["within_tolerance"]:
        axis = "direct" if direct["within_tolerance"] else "swapped"
        return {
            **base,
            "status": "passed",
            "mapping_allowed": True,
            "accepted_axis_mapping": axis,
            "reason": "document outline body agrees with the canonical body",
        }
    return {
        **base,
        "status": "rejected_body_conflict",
        "mapping_allowed": False,
        "reason": "document outline body conflicts with the canonical body beyond tolerance",
    }


def _provenance_by_field(values: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("field")): dict(item)
        for item in values
        if isinstance(item, dict) and item.get("field")
    }


def _geometry_provenance_comparison(
    record: dict[str, Any],
    canonical_entry: dict[str, Any],
    tolerance: float,
) -> list[dict[str, Any]]:
    document_values = dict(record.get("values") or {})
    canonical_values = dict(canonical_entry.get("values") or {})
    document_provenance = _provenance_by_field(record.get("value_provenance") or [])
    canonical_provenance = _provenance_by_field(
        canonical_entry.get("value_provenance") or []
    )
    comparisons: list[dict[str, Any]] = []
    for field in sorted(set(document_values) | set(canonical_values)):
        document_value = document_values.get(field)
        canonical_value = canonical_values.get(field)
        both_numeric = isinstance(document_value, (int, float)) and isinstance(
            canonical_value,
            (int, float),
        )
        delta = float(document_value) - float(canonical_value) if both_numeric else None
        comparisons.append(
            {
                "field": field,
                "document": {
                    "value": document_value,
                    "provenance": document_provenance.get(field),
                },
                "canonical": {
                    "value": canonical_value,
                    "provenance": canonical_provenance.get(field),
                },
                "delta_mm": delta,
                "equal_within_tolerance": (
                    abs(delta) <= tolerance
                    if delta is not None
                    else document_value == canonical_value
                ),
            }
        )
    return comparisons


def _base_result(registry: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "schema": MATCH_RESULT_SCHEMA,
        "status": "registry_not_configured",
        "tolerance_mm": MAX_TOLERANCE_MM,
        "registry_authority": None,
        "registry_path": None,
        "registry_sha256": None,
        "document_package_code": None,
        "document_package_code_evidence": None,
        "same_package_group_count": 0,
        "suppress_candidate_file": False,
        "mapping_review_required": False,
        "canonical_entry_id": None,
        "canonical_path": None,
        "pad_comparison": None,
        "geometry_provenance_comparison": [],
        "near_canonical": None,
        "reason": "canonical registry was not configured",
    }


def match_candidate(
    record: dict[str, Any],
    candidate_footprint_text: str,
    registry: dict[str, Any] | None,
) -> dict[str, Any]:
    result = _base_result(registry)
    if registry is None:
        return result
    tolerance = float(registry.get("tolerance_mm", MAX_TOLERANCE_MM))
    if not 0.0 <= tolerance <= MAX_TOLERANCE_MM:
        raise ValueError("canonical registry tolerance exceeds the frozen 0.001 mm gate")
    result.update(
        tolerance_mm=tolerance,
        registry_authority=str(registry.get("authority") or "proposal_only"),
        registry_path=registry.get("_loaded_from"),
        registry_sha256=registry.get("_loaded_sha256"),
    )
    package_evidence = package_token_evidence(record)
    result["document_package_code_evidence"] = package_evidence
    result["document_package_code"] = package_evidence["package_code"]
    if package_evidence["status"] != "exact_unique":
        result.update(
            status="package_code_unresolved",
            reason="exact package code is absent or ambiguous; canonical reuse is forbidden",
        )
        return result

    entries = {
        str(entry.get("entry_id")): entry
        for entry in registry.get("entries") or []
        if isinstance(entry, dict) and entry.get("entry_id")
    }
    same_package_groups = [
        group
        for group in registry.get("canonical_groups") or []
        if isinstance(group, dict)
        and str(group.get("package_code") or "").upper()
        == package_evidence["package_code"]
    ]
    result["same_package_group_count"] = len(same_package_groups)
    if not same_package_groups:
        result.update(
            status="no_same_package_canonical",
            reason="no canonical group has the exact package code; use normal new-build flow",
        )
        return result

    document_signature = pad_signature_from_text(candidate_footprint_text)
    comparisons: list[dict[str, Any]] = []
    for group in same_package_groups:
        entry_id = str(group.get("proposed_canonical_entry_id") or "")
        entry = entries.get(entry_id)
        if entry is None:
            continue
        comparison = compare_pad_signatures(
            document_signature,
            dict(entry.get("pad_signature") or {}),
            tolerance_mm=tolerance,
        )
        comparisons.append(
            {
                "group": group,
                "entry": entry,
                "comparison": comparison,
            }
        )
    exact = [item for item in comparisons if item["comparison"]["exact_within_tolerance"]]
    if len(exact) > 1:
        result.update(
            status="ambiguous_exact_canonical_matches",
            reason="multiple canonical groups match exactly; do not select or suppress a candidate",
        )
        return result
    if len(exact) == 1:
        matched = exact[0]
        group = matched["group"]
        entry = matched["entry"]
        approved = (
            result["registry_authority"] == "user_approved"
            and str(group.get("approval_status") or "") == "user_approved"
        )
        result.update(
            status=(
                "exact_canonical_match_pending_review"
                if approved
                else "exact_canonical_proposal_detected"
            ),
            suppress_candidate_file=approved,
            mapping_review_required=True,
            canonical_entry_id=entry.get("entry_id"),
            canonical_path=entry.get("formal_path"),
            pad_comparison=matched["comparison"],
            geometry_provenance_comparison=_geometry_provenance_comparison(
                record,
                entry,
                tolerance,
            ),
            canonical_package_code_evidence=entry.get("package_code_evidence"),
            reason=(
                "exact package code and every pad field match; send component-to-canonical mapping for human confirmation"
                if approved
                else "exact match found, but registry is proposal-only; keep normal candidate flow until user approval"
            ),
        )
        return result

    comparable = [item for item in comparisons if item.get("comparison")]
    if comparable:
        nearest = sorted(
            comparable,
            key=lambda item: (
                int(item["comparison"]["difference_count"]),
                str(item["entry"].get("entry_id") or ""),
            ),
        )[0]
        result.update(
            status="near_canonical_new_build_required",
            near_canonical={
                "canonical_entry_id": nearest["entry"].get("entry_id"),
                "canonical_path": nearest["entry"].get("formal_path"),
                "pad_comparison": nearest["comparison"],
            },
            reason="package code matches but at least one pad field exceeds tolerance; use normal new-build flow",
        )
    else:
        result.update(
            status="canonical_registry_incomplete",
            reason="same-package group lacks a resolvable canonical entry; use normal new-build flow",
        )
    return result
