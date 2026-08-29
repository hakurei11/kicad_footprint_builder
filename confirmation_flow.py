from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TRANSACTION_SCHEMA = "m5_confirmation_transaction_v1"
MAPPING_TRANSACTION_SCHEMA = "m9_canonical_mapping_transaction_v1"
MAPPING_RECORD_SCHEMA = "m9_component_to_canonical_mapping_v1"
CANONICAL_PROPOSAL_SCHEMA = "m9_canonical_first_instance_proposal_v1"


class ConfirmationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _append_chain_event(
    events: list[dict[str, Any]], event_type: str, details: dict[str, Any]
) -> None:
    event = {
        "sequence": len(events) + 1,
        "event_type": event_type,
        "at": _utc_now(),
        "previous_event_sha256": events[-1]["event_sha256"] if events else None,
        "details": details,
    }
    event["event_sha256"] = _canonical_sha256(event)
    events.append(event)


def _load_queue(queue_path: Path) -> tuple[Path, dict[str, Any]]:
    queue_path = queue_path.expanduser().resolve()
    if not queue_path.is_file():
        raise ConfirmationError(f"审核队列不存在：{queue_path}")
    try:
        payload = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfirmationError(f"审核队列无法读取：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ConfirmationError("审核队列缺少 records 列表。")
    if payload.get("confirmation_required_before_library_commit") is not True:
        raise ConfirmationError("审核队列未声明确认是写库前置条件。")
    return queue_path, payload


def _queue_child(queue_root: Path, raw_path: Any, label: str) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise ConfirmationError(f"{label} 路径为空。")
    raw = Path(text)
    candidate = raw.resolve() if raw.is_absolute() else (queue_root / raw).resolve()
    try:
        candidate.relative_to(queue_root)
    except ValueError as exc:
        raise ConfirmationError(f"{label} 越过审核队列目录：{candidate}") from exc
    if not candidate.is_file():
        raise ConfirmationError(f"{label} 不存在：{candidate}")
    return candidate


def _optional_queue_asset(
    queue_root: Path,
    row: dict[str, Any],
    path_key: str,
    sha_key: str,
) -> dict[str, Any] | None:
    if not row.get(path_key):
        return None
    path = _queue_child(queue_root, row[path_key], path_key)
    actual_sha = sha256_file(path)
    expected_sha = str(row.get(sha_key) or "").strip().upper()
    if expected_sha and expected_sha != actual_sha:
        raise ConfirmationError(
            f"{row.get('record_id')}: {path_key} SHA256 不匹配。"
        )
    return {
        "path": str(path),
        "sha256": actual_sha,
    }


def _record_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in payload["records"]:
        if not isinstance(raw, dict):
            raise ConfirmationError("审核队列包含非对象记录。")
        record_id = str(raw.get("record_id") or "").strip()
        if not record_id:
            raise ConfirmationError("审核队列包含空 record_id。")
        if record_id in result:
            raise ConfirmationError(f"审核队列 record_id 重复：{record_id}")
        result[record_id] = raw
    return result


def list_confirmation_records(queue_path: Path) -> list[dict[str, Any]]:
    queue_path, payload = _load_queue(queue_path)
    rows: list[dict[str, Any]] = []
    for record_id, row in _record_map(payload).items():
        candidate = str(row.get("candidate_path") or "").strip()
        pending = (
            row.get("status") == "pending_user_confirmation"
            and row.get("confirmation_required") is True
            and bool(candidate)
        )
        rows.append(
            {
                "record_id": record_id,
                "pending": pending,
                "status": row.get("status"),
                "family": row.get("family"),
                "pdf_name": row.get("pdf_name") or Path(
                    str(row.get("pdf_path") or "")
                ).name,
                "page": row.get("selected_page")
                or row.get("land_page_1based"),
                "candidate_name": Path(candidate).name if candidate else "",
                "values": dict(row.get("values") or {}),
                "queue_path": str(queue_path),
            }
        )
    return rows


def _snapshot_record(
    queue_root: Path,
    row: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    record_id = str(row["record_id"])
    if row.get("status") != "pending_user_confirmation":
        raise ConfirmationError(f"{record_id}: 状态不是 pending_user_confirmation。")
    if row.get("confirmation_required") is not True:
        raise ConfirmationError(f"{record_id}: 未声明逐件确认要求。")
    candidate = _queue_child(queue_root, row.get("candidate_path"), "candidate_path")
    if candidate.suffix.lower() != ".kicad_mod":
        raise ConfirmationError(f"{record_id}: 候选不是 .kicad_mod 文件。")
    actual_candidate_sha = sha256_file(candidate)
    expected_candidate_sha = str(row.get("candidate_sha256") or "").strip().upper()
    if not expected_candidate_sha:
        raise ConfirmationError(f"{record_id}: 缺少 candidate_sha256。")
    if expected_candidate_sha != actual_candidate_sha:
        raise ConfirmationError(f"{record_id}: 候选 SHA256 不匹配。")

    document_sha = str(
        row.get("document_sha256") or row.get("pdf_sha256") or ""
    ).strip().upper()
    page = row.get("selected_page") or row.get("land_page_1based")
    values = dict(row.get("values") or {})
    value_provenance = row.get("value_provenance")
    if not document_sha:
        raise ConfirmationError(f"{record_id}: 缺少来源文档 SHA256。")
    if not isinstance(page, int) or page < 1:
        raise ConfirmationError(f"{record_id}: 缺少合法的来源页码。")
    if not values:
        raise ConfirmationError(f"{record_id}: 缺少几何 values。")
    if not isinstance(value_provenance, list) or not value_provenance:
        raise ConfirmationError(f"{record_id}: 缺少逐值 provenance。")

    source_image = _optional_queue_asset(
        queue_root, row, "source_image_path", "source_image_sha256"
    )
    preview = _optional_queue_asset(
        queue_root, row, "preview_path", "preview_sha256"
    )
    return candidate, {
        "record_id": record_id,
        "family": row.get("family"),
        "source_document": {
            "name": row.get("pdf_name")
            or Path(str(row.get("pdf_path") or "")).name,
            "sha256": document_sha,
        },
        "source_page_1based": page,
        "candidate": {
            "path": str(candidate),
            "name": candidate.name,
            "sha256": actual_candidate_sha,
        },
        "source_image": source_image,
        "preview": preview,
        "values": values,
        "value_provenance": value_provenance,
        "body_axis_provenance": dict(row.get("body_axis_provenance") or {}),
        "queue_record_status": row.get("status"),
    }


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(source.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _audit_artifact_name(position: int, record_id: str, suffix: str) -> str:
    short_hash = hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:12]
    return f"{position:03d}_{short_hash}{suffix}"


def _load_user_approved_registry(path: Path) -> tuple[Path, dict[str, Any]]:
    resolved = path.expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfirmationError(f"正典注册表无法读取：{exc}") from exc
    if payload.get("schema") != "kicad_canonical_footprint_registry_v1":
        raise ConfirmationError("正典注册表 schema 不受支持。")
    if payload.get("authority") != "user_approved":
        raise ConfirmationError("正典注册表尚未获用户批准。")
    tolerance = float(payload.get("tolerance_mm", 0.001))
    if not 0.0 <= tolerance <= 0.001:
        raise ConfirmationError("正典注册表容差超过 0.001 mm。")
    return resolved, payload


def _safe_library_member(library_dir: Path, name: Any, label: str) -> Path:
    text = str(name or "").strip()
    if not text or Path(text).name != text or Path(text).suffix.lower() != ".kicad_mod":
        raise ConfirmationError(f"{label} 不是安全的 .kicad_mod 文件名。")
    result = (library_dir / text).resolve()
    if result.parent != library_dir:
        raise ConfirmationError(f"{label} 越过正式库目录。")
    return result


def apply_canonical_mappings(
    registry_path: Path,
    library_dir: Path,
    mappings: Iterable[dict[str, Any]],
    *,
    reviewer: str,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ConfirmationError("未收到明确确认，禁止把库成员转为正典映射。")
    reviewer = reviewer.strip()
    if not reviewer:
        raise ConfirmationError("审核人不能为空。")
    requested = [dict(item) for item in mappings]
    if not requested:
        raise ConfirmationError("至少需要一条正典映射。")
    registry_path, registry = _load_user_approved_registry(registry_path)
    library_dir = library_dir.expanduser().resolve()
    if library_dir.suffix.lower() != ".pretty" or not library_dir.is_dir():
        raise ConfirmationError("目标正式库必须是现有 .pretty 目录。")

    from canonical_footprint_matcher import (  # noqa: PLC0415
        compare_pad_signatures,
        pad_signature_from_file,
    )

    entries = {
        str(entry.get("entry_id")): entry
        for entry in registry.get("entries") or []
        if isinstance(entry, dict) and entry.get("entry_id")
    }
    groups = {
        str(group.get("group_id")): group
        for group in registry.get("canonical_groups") or []
        if isinstance(group, dict) and group.get("group_id")
    }
    mapping_root = library_dir.parent / f"{library_dir.stem}.canonical_mappings"
    audit_root = library_dir.parent / f"{library_dir.stem}.confirmation_audit"
    transaction_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_canonical_"
        + uuid.uuid4().hex[:8]
    )
    transaction_dir = audit_root / transaction_id
    backup_dir = transaction_dir / "backup"
    staged_dir = transaction_dir / "staged"
    transaction_path = transaction_dir / "transaction.json"
    prepared: list[dict[str, Any]] = []
    seen_members: set[str] = set()

    for position, spec in enumerate(requested, start=1):
        group_id = str(spec.get("group_id") or "").strip()
        group = groups.get(group_id)
        if group is None or group.get("approval_status") != "user_approved":
            raise ConfirmationError(f"映射 {position}: 正典组未获用户批准。")
        canonical_entry_id = str(spec.get("canonical_entry_id") or "").strip()
        member_entry_id = str(spec.get("member_entry_id") or "").strip()
        if canonical_entry_id != str(group.get("proposed_canonical_entry_id") or ""):
            raise ConfirmationError(f"映射 {position}: 正典 ID 与注册表不一致。")
        if member_entry_id not in set(group.get("member_entry_ids") or []):
            raise ConfirmationError(f"映射 {position}: 成员不属于获批正典组。")
        if member_entry_id == canonical_entry_id:
            raise ConfirmationError(f"映射 {position}: 正典自身不能转为映射记录。")
        canonical_entry = entries.get(canonical_entry_id)
        member_entry = entries.get(member_entry_id)
        if canonical_entry is None or member_entry is None:
            raise ConfirmationError(f"映射 {position}: 注册表成员记录不完整。")
        canonical = _safe_library_member(
            library_dir, canonical_entry.get("formal_name"), "canonical_name"
        )
        member = _safe_library_member(
            library_dir, member_entry.get("formal_name"), "member_name"
        )
        if member.name in seen_members:
            raise ConfirmationError(f"映射成员重复：{member.name}")
        seen_members.add(member.name)
        if not canonical.is_file() or not member.is_file():
            raise ConfirmationError(f"映射 {position}: 正典或成员文件不存在。")
        canonical_sha = sha256_file(canonical)
        member_sha = sha256_file(member)
        if canonical_sha != str(canonical_entry.get("formal_sha256") or "").upper():
            raise ConfirmationError(f"映射 {position}: 正典 SHA256 与注册表不一致。")
        if member_sha != str(member_entry.get("formal_sha256") or "").upper():
            raise ConfirmationError(f"映射 {position}: 成员 SHA256 与注册表不一致。")
        expected_canonical = str(spec.get("expected_canonical_sha256") or "").upper()
        expected_member = str(spec.get("expected_member_sha256") or "").upper()
        if expected_canonical and expected_canonical != canonical_sha:
            raise ConfirmationError(f"映射 {position}: 正典 SHA256 与任务输入不一致。")
        if expected_member and expected_member != member_sha:
            raise ConfirmationError(f"映射 {position}: 成员 SHA256 与任务输入不一致。")
        comparison = compare_pad_signatures(
            pad_signature_from_file(member),
            pad_signature_from_file(canonical),
            tolerance_mm=float(registry.get("tolerance_mm", 0.001)),
        )
        if not comparison["exact_within_tolerance"]:
            raise ConfirmationError(f"映射 {position}: 成员与正典焊盘不完全一致。")

        mapping_target = mapping_root / f"{member.stem}.canonical_mapping.json"
        if mapping_target.exists():
            raise ConfirmationError(f"映射记录已存在：{mapping_target}")
        mapping_record = {
            "schema": MAPPING_RECORD_SCHEMA,
            "status": "mapped_to_user_approved_canonical",
            "package_code": group.get("package_code"),
            "package_aliases": list(group.get("package_aliases") or []),
            "registry": {
                "path": str(registry_path),
                "sha256": sha256_file(registry_path),
                "group_id": group_id,
                "approval_status": group.get("approval_status"),
                "approval_reference": group.get("approval_reference"),
            },
            "member": {
                "entry_id": member_entry_id,
                "name": member.name,
                "former_library_path": str(member),
                "sha256": member_sha,
            },
            "canonical": {
                "entry_id": canonical_entry_id,
                "name": canonical.name,
                "library_path": str(canonical),
                "sha256": canonical_sha,
            },
            "pad_comparison": comparison,
            "confirmation": {
                "required": True,
                "confirmed": True,
                "reviewer": reviewer,
                "confirmed_at": _utc_now(),
                "mode": "user_authorized_proxy",
            },
        }
        staged = staged_dir / _audit_artifact_name(
            position, member_entry_id, ".canonical_mapping.json"
        )
        _write_json_atomic(staged, mapping_record)
        backup = backup_dir / _audit_artifact_name(position, member_entry_id, member.suffix)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(member, backup)
        prepared.append(
            {
                "group": group,
                "member_entry": member_entry,
                "canonical_entry": canonical_entry,
                "member": member,
                "canonical": canonical,
                "member_sha": member_sha,
                "canonical_sha": canonical_sha,
                "mapping_target": mapping_target,
                "staged": staged,
                "backup": backup,
                "mapping_record": mapping_record,
            }
        )

    events: list[dict[str, Any]] = []
    _append_chain_event(
        events,
        "user_confirmed_canonical_mapping",
        {"reviewer": reviewer, "record_count": len(prepared)},
    )
    transaction: dict[str, Any] = {
        "schema": MAPPING_TRANSACTION_SCHEMA,
        "transaction_id": transaction_id,
        "status": "prepared",
        "created_at": _utc_now(),
        "reviewer": reviewer,
        "mode": "user_authorized_proxy",
        "registry": {
            "path": str(registry_path),
            "sha256": sha256_file(registry_path),
            "authority": registry.get("authority"),
        },
        "library": str(library_dir),
        "mapping_root": str(mapping_root),
        "records": [],
        "review_chain": events,
        "rollback_available": True,
    }
    for item in prepared:
        transaction["records"].append(
            {
                "group_id": item["group"].get("group_id"),
                "package_code": item["group"].get("package_code"),
                "member_entry_id": item["member_entry"].get("entry_id"),
                "canonical_entry_id": item["canonical_entry"].get("entry_id"),
                "member_path": str(item["member"]),
                "member_sha256_before": item["member_sha"],
                "member_backup_path": str(item["backup"]),
                "member_backup_sha256": sha256_file(item["backup"]),
                "member_exists_after": None,
                "canonical_path": str(item["canonical"]),
                "canonical_sha256": item["canonical_sha"],
                "mapping_path": str(item["mapping_target"]),
                "mapping_sha256_after": None,
            }
        )
    _write_json_atomic(transaction_path, transaction)

    committed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    try:
        for item, record in zip(prepared, transaction["records"], strict=True):
            _atomic_copy(item["staged"], item["mapping_target"])
            mapping_sha = sha256_file(item["mapping_target"])
            if mapping_sha != sha256_file(item["staged"]):
                raise ConfirmationError("映射记录写后 SHA256 不匹配。")
            committed.append((item, record))
            item["member"].unlink()
            if item["member"].exists():
                raise ConfirmationError("成员文件删除失败。")
            if sha256_file(item["canonical"]) != item["canonical_sha"]:
                raise ConfirmationError("正典文件在映射事务中发生变化。")
            record["member_exists_after"] = False
            record["mapping_sha256_after"] = mapping_sha
    except Exception as exc:
        restored_count = 0
        for item in reversed(prepared):
            if not item["member"].is_file():
                _atomic_copy(item["backup"], item["member"])
                restored_count += 1
            item["mapping_target"].unlink(missing_ok=True)
        transaction["status"] = "failed_rolled_back"
        transaction["error"] = f"{type(exc).__name__}: {exc}"
        transaction["completed_at"] = _utc_now()
        _append_chain_event(
            events,
            "canonical_mapping_failed_rolled_back",
            {"error": transaction["error"], "restored_count": restored_count},
        )
        _write_json_atomic(transaction_path, transaction)
        raise

    transaction["status"] = "committed"
    transaction["completed_at"] = _utc_now()
    _append_chain_event(
        events,
        "canonical_mappings_committed",
        {
            "record_count": len(committed),
            "member_files_removed": len(committed),
            "mapping_records_written": len(committed),
        },
    )
    _write_json_atomic(transaction_path, transaction)
    transaction["transaction_path"] = str(transaction_path)
    transaction["transaction_sha256"] = sha256_file(transaction_path)
    return transaction


def apply_confirmations(
    queue_path: Path,
    library_dir: Path,
    record_ids: Iterable[str],
    *,
    reviewer: str,
    confirmed: bool,
    canonical_registry_path: Path | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise ConfirmationError("未收到明确确认，禁止写入正式库。")
    reviewer = reviewer.strip()
    if not reviewer:
        raise ConfirmationError("审核人不能为空。")
    selected = tuple(dict.fromkeys(str(value).strip() for value in record_ids))
    if not selected or any(not value for value in selected):
        raise ConfirmationError("至少选择一条待确认记录。")

    queue_path, payload = _load_queue(queue_path)
    queue_root = queue_path.parent.resolve()
    records = _record_map(payload)
    missing = [record_id for record_id in selected if record_id not in records]
    if missing:
        raise ConfirmationError(f"审核队列没有这些记录：{', '.join(missing)}")

    library_dir = library_dir.expanduser().resolve()
    if library_dir.suffix.lower() != ".pretty":
        raise ConfirmationError("目标库目录必须以 .pretty 结尾。")
    library_dir.mkdir(parents=True, exist_ok=True)
    audit_root = library_dir.parent / f"{library_dir.stem}.confirmation_audit"
    transaction_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid.uuid4().hex[:8]
    )
    transaction_dir = audit_root / transaction_id
    backup_dir = transaction_dir / "backup"
    staged_dir = transaction_dir / "staged"
    transaction_path = transaction_dir / "transaction.json"

    registry_path: Path | None = None
    registry: dict[str, Any] | None = None
    if canonical_registry_path is None:
        bundled_registry = Path(__file__).resolve().parent / "canonical_footprint_registry.json"
        if bundled_registry.is_file():
            canonical_registry_path = bundled_registry
    if canonical_registry_path is not None:
        registry_path, registry = _load_user_approved_registry(canonical_registry_path)

    prepared: list[dict[str, Any]] = []
    target_names: set[str] = set()
    for position, record_id in enumerate(selected, start=1):
        queue_record = records[record_id]
        candidate, snapshot = _snapshot_record(queue_root, queue_record)
        target_name = candidate.name
        if target_name in target_names:
            raise ConfirmationError(f"多条记录写入同名目标：{target_name}")
        target_names.add(target_name)
        target = (library_dir / target_name).resolve()
        if target.parent != library_dir:
            raise ConfirmationError(f"{record_id}: 目标路径越过所选库。")
        staged = staged_dir / _audit_artifact_name(
            position, record_id, candidate.suffix
        )
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, staged)
        prepared.append(
            {
                "candidate_path": candidate,
                "target_path": target,
                "staged_path": staged,
                "snapshot": snapshot,
                "queue_record": queue_record,
            }
        )

    events: list[dict[str, Any]] = []
    _append_chain_event(
        events,
        "review_queue_loaded",
        {
            "queue_path": str(queue_path),
            "queue_sha256": sha256_file(queue_path),
            "selected_record_ids": list(selected),
        },
    )
    _append_chain_event(
        events,
        "user_confirmed",
        {
            "reviewer": reviewer,
            "mode": "batch" if len(selected) > 1 else "single",
            "record_count": len(selected),
        },
    )

    transaction: dict[str, Any] = {
        "schema": TRANSACTION_SCHEMA,
        "transaction_id": transaction_id,
        "status": "prepared",
        "created_at": _utc_now(),
        "reviewer": reviewer,
        "mode": "batch" if len(selected) > 1 else "single",
        "queue": {
            "path": str(queue_path),
            "sha256": sha256_file(queue_path),
            "schema": payload.get("schema"),
        },
        "library": str(library_dir),
        "confirmed_record_ids": list(selected),
        "records": [],
        "canonical_proposals": [],
        "review_chain": events,
        "rollback_available": True,
    }

    for position, item in enumerate(prepared, start=1):
        target = item["target_path"]
        existed = target.is_file()
        backup = (
            backup_dir
            / _audit_artifact_name(
                position,
                str(item["snapshot"]["record_id"]),
                target.suffix,
            )
            if existed
            else None
        )
        before_sha = sha256_file(target) if existed else None
        if backup is not None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        transaction["records"].append(
            {
                **item["snapshot"],
                "target": {
                    "path": str(target),
                    "existed_before": existed,
                    "sha256_before": before_sha,
                    "backup_path": str(backup) if backup else None,
                    "backup_sha256": sha256_file(backup) if backup else None,
                    "sha256_after": None,
                },
            }
        )
    _write_json_atomic(transaction_path, transaction)

    proposal_root = library_dir.parent / f"{library_dir.stem}.canonical_proposals"
    existing_package_codes: set[str] = set()
    if registry is not None:
        from canonical_footprint_matcher import normalize_package_code  # noqa: PLC0415

        for group in registry.get("canonical_groups") or []:
            if not isinstance(group, dict):
                continue
            for value in (group.get("package_code"), *(group.get("package_aliases") or [])):
                normalized = normalize_package_code(value)
                if normalized:
                    existing_package_codes.add(normalized)

    written: list[dict[str, Any]] = []
    written_proposals: list[Path] = []
    try:
        for item, record in zip(prepared, transaction["records"], strict=True):
            written.append(record)
            _atomic_copy(item["staged_path"], item["target_path"])
            after_sha = sha256_file(item["target_path"])
            if after_sha != item["snapshot"]["candidate"]["sha256"]:
                raise ConfirmationError(
                    f"{item['snapshot']['record_id']}: 写后 SHA256 不匹配。"
                )
            record["target"]["sha256_after"] = after_sha

        if registry is not None:
            from canonical_footprint_matcher import (  # noqa: PLC0415
                normalize_package_code,
                package_token_evidence,
                pad_signature_from_file,
            )

            proposed_codes: set[str] = set()
            for item, record in zip(prepared, transaction["records"], strict=True):
                package_evidence = package_token_evidence(item["queue_record"])
                if package_evidence.get("status") != "exact_unique":
                    continue
                package_code = str(package_evidence.get("package_code") or "").strip()
                normalized = normalize_package_code(package_code)
                if not normalized or normalized in existing_package_codes or normalized in proposed_codes:
                    continue
                proposal_path = proposal_root / f"{normalized}.canonical_proposal.json"
                if proposal_path.exists():
                    continue
                proposal = {
                    "schema": CANONICAL_PROPOSAL_SCHEMA,
                    "status": "proposal_only",
                    "approval_status": "batch_user_approval_required",
                    "automatic_reuse_allowed": False,
                    "package_code": package_code,
                    "normalized_package_code": normalized,
                    "package_code_evidence": package_evidence,
                    "first_confirmed_instance": {
                        "record_id": record.get("record_id"),
                        "formal_path": record["target"]["path"],
                        "formal_sha256": record["target"]["sha256_after"],
                        "pad_signature": pad_signature_from_file(Path(record["target"]["path"])),
                        "values": record.get("values"),
                        "value_provenance": record.get("value_provenance"),
                    },
                    "confirmation": {
                        "transaction_id": transaction_id,
                        "reviewer": reviewer,
                        "confirmed_at": transaction.get("created_at"),
                    },
                    "policy": (
                        "Newly confirmed first instances enter a proposal-only queue; "
                        "canonical status requires separate batch user approval."
                    ),
                }
                _write_json_atomic(proposal_path, proposal)
                written_proposals.append(proposal_path)
                proposed_codes.add(normalized)
                transaction["canonical_proposals"].append(
                    {
                        "path": str(proposal_path),
                        "sha256": sha256_file(proposal_path),
                        "package_code": package_code,
                        "status": "proposal_only",
                    }
                )
    except Exception as exc:
        for proposal_path in reversed(written_proposals):
            proposal_path.unlink(missing_ok=True)
        for record in reversed(written):
            target = Path(record["target"]["path"])
            backup_text = record["target"].get("backup_path")
            if backup_text:
                _atomic_copy(Path(backup_text), target)
            else:
                target.unlink(missing_ok=True)
        transaction["status"] = "failed_rolled_back"
        transaction["error"] = f"{type(exc).__name__}: {exc}"
        transaction["completed_at"] = _utc_now()
        _append_chain_event(
            events,
            "commit_failed_rolled_back",
            {"error": transaction["error"], "restored_count": len(written)},
        )
        _write_json_atomic(transaction_path, transaction)
        raise

    transaction["status"] = "committed"
    transaction["completed_at"] = _utc_now()
    _append_chain_event(
        events,
        "library_committed",
        {
            "record_count": len(written),
            "targets": [record["target"]["path"] for record in written],
            "canonical_proposal_count": len(written_proposals),
        },
    )
    _write_json_atomic(transaction_path, transaction)
    transaction["transaction_path"] = str(transaction_path)
    transaction["transaction_sha256"] = sha256_file(transaction_path)
    return transaction


def _rollback_canonical_mapping_transaction(
    transaction_path: Path,
    transaction: dict[str, Any],
    *,
    reviewer: str,
) -> dict[str, Any]:
    if transaction.get("status") != "committed":
        raise ConfirmationError("只有 committed 正典映射事务可以回退。")
    records = transaction.get("records") or []
    for record in records:
        canonical = Path(record["canonical_path"]).resolve()
        if not canonical.is_file() or sha256_file(canonical) != record.get("canonical_sha256"):
            raise ConfirmationError("正典文件已在映射提交后变化，拒绝回退。")
        member = Path(record["member_path"]).resolve()
        if member.exists():
            raise ConfirmationError("被映射成员已重新出现，拒绝覆盖。")
        mapping = Path(record["mapping_path"]).resolve()
        if not mapping.is_file() or sha256_file(mapping) != record.get("mapping_sha256_after"):
            raise ConfirmationError("正典映射记录已变化或缺失，拒绝回退。")
        backup = Path(record["member_backup_path"]).resolve()
        if not backup.is_file() or sha256_file(backup) != record.get("member_backup_sha256"):
            raise ConfirmationError("正典映射回退备份已变化或缺失。")

    for record in reversed(records):
        member = Path(record["member_path"])
        _atomic_copy(Path(record["member_backup_path"]), member)
        Path(record["mapping_path"]).unlink(missing_ok=True)

    transaction["status"] = "rolled_back"
    transaction["rolled_back_at"] = _utc_now()
    transaction["rolled_back_by"] = reviewer
    events = transaction.setdefault("review_chain", [])
    _append_chain_event(
        events,
        "canonical_mappings_rolled_back",
        {"reviewer": reviewer, "record_count": len(records)},
    )
    _write_json_atomic(transaction_path, transaction)
    transaction["transaction_path"] = str(transaction_path)
    transaction["transaction_sha256"] = sha256_file(transaction_path)
    return transaction


def rollback_transaction(
    transaction_path: Path,
    *,
    reviewer: str,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ConfirmationError("未收到明确回退确认，禁止改动正式库。")
    reviewer = reviewer.strip()
    if not reviewer:
        raise ConfirmationError("回退审核人不能为空。")
    transaction_path = transaction_path.expanduser().resolve()
    try:
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfirmationError(f"事务记录无法读取：{exc}") from exc
    schema = transaction.get("schema")
    if schema == MAPPING_TRANSACTION_SCHEMA:
        return _rollback_canonical_mapping_transaction(
            transaction_path,
            transaction,
            reviewer=reviewer,
        )
    if schema != TRANSACTION_SCHEMA:
        raise ConfirmationError("事务记录 schema 不受支持。")
    if transaction.get("status") != "committed":
        raise ConfirmationError("只有 committed 事务可以回退。")

    records = transaction.get("records") or []
    proposals = transaction.get("canonical_proposals") or []
    for proposal in proposals:
        path = Path(str(proposal.get("path") or "")).resolve()
        expected = str(proposal.get("sha256") or "").upper()
        if not path.is_file() or sha256_file(path) != expected:
            raise ConfirmationError("首件正典提案已在提交后变化，拒绝回退。")
    for record in records:
        target = Path(record["target"]["path"]).resolve()
        expected = record["target"].get("sha256_after")
        if not target.is_file() or sha256_file(target) != expected:
            raise ConfirmationError(
                f"{record.get('record_id')}: 正式库文件已在提交后变化，拒绝覆盖。"
            )
        backup_text = record["target"].get("backup_path")
        if record["target"].get("existed_before"):
            backup = Path(str(backup_text or ""))
            if not backup.is_file():
                raise ConfirmationError(
                    f"{record.get('record_id')}: 回退备份缺失。"
                )
            if sha256_file(backup) != record["target"].get("backup_sha256"):
                raise ConfirmationError(
                    f"{record.get('record_id')}: 回退备份 SHA256 不匹配。"
                )

    for record in reversed(records):
        target = Path(record["target"]["path"])
        if record["target"].get("existed_before"):
            _atomic_copy(Path(record["target"]["backup_path"]), target)
        else:
            target.unlink(missing_ok=True)
    for proposal in proposals:
        Path(proposal["path"]).unlink(missing_ok=True)

    transaction["status"] = "rolled_back"
    transaction["rolled_back_at"] = _utc_now()
    transaction["rolled_back_by"] = reviewer
    events = transaction.setdefault("review_chain", [])
    _append_chain_event(
        events,
        "library_rolled_back",
        {
            "reviewer": reviewer,
            "record_count": len(records),
            "canonical_proposal_count": len(proposals),
        },
    )
    _write_json_atomic(transaction_path, transaction)
    transaction["transaction_path"] = str(transaction_path)
    transaction["transaction_sha256"] = sha256_file(transaction_path)
    return transaction
