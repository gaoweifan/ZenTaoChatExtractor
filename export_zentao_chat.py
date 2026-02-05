#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys
import types
from typing import Any, Dict, Iterable, List, Optional, Tuple


def log_status(message: str) -> None:
    """Print a timestamped status line for long-running exports."""
    timestamp = dt.datetime.now().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def _ensure_bytes(data: Any) -> bytes:
    """Coerce input into bytes for Snappy decompression.

    Accepts bytes-like objects or file-like objects with a .read() method.
    Raises TypeError for unsupported inputs to avoid silently corrupting data.
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if hasattr(data, "read"):
        return data.read()
    raise TypeError(f"Unsupported input type for snappy: {type(data)}")


def snappy_decompress(data: Any) -> bytes:
    """Decompress a Snappy (unframed) block used by LevelDB.

    This implements the minimal subset of Snappy used in Chromium LevelDB:
    - Reads the varint uncompressed length prefix.
    - Processes literal and copy tags (1/2/4-byte offsets).
    - Returns the reconstructed payload as bytes.
    The function intentionally skips checksum validation and is tolerant of
    length mismatches for forensic use.
    """
    data = _ensure_bytes(data)
    i = 0
    length = 0
    shift = 0
    while True:
        if i >= len(data):
            raise ValueError("snappy: unexpected end of data while reading length")
        b = data[i]
        i += 1
        length |= (b & 0x7F) << shift
        if b < 0x80:
            break
        shift += 7

    out = bytearray()
    while i < len(data):
        tag = data[i]
        i += 1
        tag_type = tag & 0x03
        if tag_type == 0:  # literal
            lit_len = tag >> 2
            if lit_len < 60:
                lit_len += 1
            else:
                extra = lit_len - 59
                lit_len = 0
                for j in range(extra):
                    if i >= len(data):
                        raise ValueError("snappy: literal length exceeds input")
                    lit_len |= data[i] << (8 * j)
                    i += 1
                lit_len += 1
            if i + lit_len > len(data):
                raise ValueError("snappy: literal exceeds input")
            out.extend(data[i : i + lit_len])
            i += lit_len
            continue

        if tag_type == 1:  # copy with 1-byte offset
            length_copy = ((tag >> 2) & 0x7) + 4
            if i >= len(data):
                raise ValueError("snappy: copy1 missing offset")
            offset = ((tag >> 5) << 8) | data[i]
            i += 1
        elif tag_type == 2:  # copy with 2-byte offset
            length_copy = (tag >> 2) + 1
            if i + 1 >= len(data):
                raise ValueError("snappy: copy2 missing offset")
            offset = data[i] | (data[i + 1] << 8)
            i += 2
        else:  # tag_type == 3, copy with 4-byte offset
            length_copy = (tag >> 2) + 1
            if i + 3 >= len(data):
                raise ValueError("snappy: copy4 missing offset")
            offset = (
                data[i]
                | (data[i + 1] << 8)
                | (data[i + 2] << 16)
                | (data[i + 3] << 24)
            )
            i += 4

        if offset == 0:
            raise ValueError("snappy: zero offset")
        start = len(out) - offset
        if start < 0:
            raise ValueError("snappy: offset beyond output")
        for _ in range(length_copy):
            out.append(out[start])
            start += 1

    # Length mismatch can occur in malformed blocks; we still return data for forensics.
    return bytes(out)


def install_shims() -> None:
    """Install stub modules expected by ccl_chromium_reader.

    ccl_chromium_reader imports brotli and ccl_simplesnappy unconditionally.
    For this exporter we only need a minimal Snappy implementation, so this
    function injects lightweight shims into sys.modules before imports occur.
    """
    sys.modules.setdefault("brotli", types.SimpleNamespace(decompress=lambda x: x))
    sys.modules["ccl_simplesnappy"] = types.SimpleNamespace(decompress=snappy_decompress)


def add_ccl_reader_path(base_dir: pathlib.Path) -> None:
    """Prepend the local ccl_chromium_reader package to sys.path.

    This allows importing the bundled reader without installing it globally.
    """
    reader_path = base_dir / "ccl_chromium_reader"
    if not reader_path.exists():
        raise FileNotFoundError(f"Missing ccl_chromium_reader at {reader_path}")
    sys.path.insert(0, str(reader_path))


def normalize_json(value: Any) -> Any:
    """Convert complex objects into JSON-serializable structures.

    - dict keys are stringified.
    - sets are converted to lists.
    - unsupported objects are coerced to string.
    """
    if isinstance(value, dict):
        return {str(k): normalize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_json(v) for v in value]
    if isinstance(value, set):
        return [normalize_json(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def parse_content(content: Any) -> Tuple[str, Optional[Any]]:
    """Return the raw content string plus a parsed JSON object if applicable.

    If content is a string that starts with '{' or '[', attempt json.loads.
    The raw string is always returned even if JSON parsing fails.
    """
    if not isinstance(content, str):
        return str(content), None
    raw = content
    stripped = raw.strip()
    if stripped and stripped[0] in ("{", "["):
        try:
            return raw, json.loads(stripped)
        except json.JSONDecodeError:
            return raw, None
    return raw, None


def pick_member(existing: Optional[Dict[str, Any]], new: Dict[str, Any]) -> Dict[str, Any]:
    """Choose the best Member record when duplicates exist.

    Preference order:
    - non-deleted record over deleted record
    - record that includes realname
    - otherwise keep the existing record
    """
    if existing is None:
        return new
    if existing.get("deleted") and not new.get("deleted"):
        return new
    if not existing.get("realname") and new.get("realname"):
        return new
    return existing


def chat_sort_key(chat: Dict[str, Any]) -> float:
    """Pick a stable timestamp for Chat records to resolve duplicates."""
    for key in ("editedDate", "lastActiveTime", "lastAccessTime", "createdDate"):
        value = chat.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def pick_chat(existing: Optional[Dict[str, Any]], new: Dict[str, Any]) -> Dict[str, Any]:
    """Choose the most recent Chat record based on chat_sort_key."""
    if existing is None:
        return new
    return new if chat_sort_key(new) >= chat_sort_key(existing) else existing


def simplify_chat(chat: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce Chat record to a JSON-friendly subset used in exports."""
    members = chat.get("members")
    admins = chat.get("admins")
    return {
        "gid": chat.get("gid"),
        "id": chat.get("id"),
        "type": chat.get("type"),
        "name": chat.get("name"),
        "members": sorted(list(members)) if isinstance(members, set) else normalize_json(members),
        "admins": sorted(list(admins)) if isinstance(admins, set) else normalize_json(admins),
        "createdDate": chat.get("createdDate"),
        "editedDate": chat.get("editedDate"),
        "lastActiveTime": chat.get("lastActiveTime"),
        "lastAccessTime": chat.get("lastAccessTime"),
        "theOtherMemberID": chat.get("theOtherMemberID"),
    }


def format_timestamp(ms: Optional[float], use_utc: bool) -> Tuple[Optional[int], Optional[str]]:
    """Convert millisecond timestamps to integer ms and ISO-8601 string.

    Returns (None, None) if the input is missing or invalid.
    """
    if ms is None:
        return None, None
    try:
        ts = int(ms)
    except (TypeError, ValueError):
        return None, None
    dt_obj = dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc if use_utc else None)
    return ts, dt_obj.isoformat()


def find_image_paths(
    images_dir: pathlib.Path, gid: str, mime_type: Optional[str]
) -> Tuple[Optional[pathlib.Path], Optional[pathlib.Path]]:
    """Find image and thumbnail paths by gid with extension heuristics.

    The lookup prefers mime-based extensions, then common image suffixes,
    and falls back to a glob match if necessary.
    """
    if not images_dir.exists():
        return None, None
    candidates: List[str] = []
    if mime_type and "/" in mime_type:
        candidates.append("." + mime_type.split("/")[-1].lower())
    candidates += [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]
    seen = set()
    exts = [ext for ext in candidates if not (ext in seen or seen.add(ext))]

    image_path = None
    thumb_path = None
    for ext in exts:
        path = images_dir / f"{gid}{ext}"
        if path.exists():
            image_path = path
            thumb = images_dir / f"{gid}_thumb{ext}"
            if thumb.exists():
                thumb_path = thumb
            break

    if image_path is None:
        matches = list(images_dir.glob(f"{gid}*"))
        for match in matches:
            if match.name.endswith("_thumb.png") or match.name.endswith("_thumb.jpg"):
                thumb_path = match
            elif match.is_file():
                image_path = match
        if image_path and not thumb_path:
            for match in matches:
                if "_thumb" in match.name:
                    thumb_path = match
                    break

    return image_path, thumb_path


def csv_safe_value(value: Any) -> Any:
    """Return a CSV-safe value by escaping embedded newlines.

    Some CSV consumers break on multi-line fields even if quoted. This
    function replaces CR/LF with literal '\\n' to keep one record per line.
    Non-string values are returned as-is.
    """
    if not isinstance(value, str):
        return value
    if "\n" not in value and "\r" not in value:
        return value
    return value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def export_db(
    db,
    db_name: str,
    out_dir: pathlib.Path,
    images_dir: pathlib.Path,
    include_deleted: bool,
    include_duplicates: bool,
    fmt: str,
    use_utc: bool,
    root_dir: pathlib.Path,
) -> None:
    """Export chat data for a single IndexedDB database.

    This function:
    - loads Member and Chat stores and resolves duplicates
    - iterates ChatMessage records, optionally deduping old versions
    - parses content JSON and links image file paths
    - writes JSON and/or CSV exports under the output directory
    """
    log_status(f"{db_name}: loading members")
    members: Dict[int, Dict[str, Any]] = {}
    for rec in db["Member"].iterate_records():
        value = rec.value
        if not isinstance(value, dict):
            continue
        member_id = value.get("id")
        if isinstance(member_id, int):
            members[member_id] = pick_member(members.get(member_id), value)

    log_status(f"{db_name}: loading chats")
    chats: Dict[str, Dict[str, Any]] = {}
    for rec in db["Chat"].iterate_records():
        value = rec.value
        if not isinstance(value, dict):
            continue
        gid = value.get("gid")
        if not gid:
            continue
        chats[gid] = pick_chat(chats.get(gid), value)

    chats_simple = {gid: simplify_chat(chat) for gid, chat in chats.items()}

    messages_by_key: Dict[str, Dict[str, Any]] = {}
    messages: List[Dict[str, Any]] = []

    log_status(f"{db_name}: loading messages")
    for rec in db["ChatMessage"].iterate_records():
        value = rec.value
        if not isinstance(value, dict):
            continue
        if value.get("deleted") and not include_deleted:
            continue

        key = value.get("unionId") or value.get("id") or value.get("gid")
        if key is None:
            continue

        def message_score(v: Dict[str, Any]) -> Tuple[int, float]:
            not_deleted = 0 if v.get("deleted") else 1
            date = float(v.get("date") or 0.0)
            return (not_deleted, date)

        if not include_duplicates:
            existing = messages_by_key.get(str(key))
            if existing is not None:
                if message_score(value) > message_score(existing):
                    messages_by_key[str(key)] = value
                continue
            messages_by_key[str(key)] = value
            continue

        messages.append(value)

    if not include_duplicates:
        messages = list(messages_by_key.values())
    log_status(f"{db_name}: messages selected {len(messages)}")

    out_records: List[Dict[str, Any]] = []
    for value in messages:
        content_raw, content_obj = parse_content(value.get("content", ""))
        ctype = value.get("contentType")

        image_path = None
        image_thumb = None
        file_name = None
        file_size = None
        file_type = None
        url = None
        emoji = None

        if isinstance(content_obj, dict):
            file_name = content_obj.get("name")
            file_size = content_obj.get("size")
            file_type = content_obj.get("type")
            url = content_obj.get("url")
            if content_obj.get("type") == "emoji":
                emoji = content_obj.get("content")

            if ctype == "image":
                gid = content_obj.get("gid") or value.get("gid")
                if gid:
                    image_path, image_thumb = find_image_paths(images_dir, gid, file_type)

        member = members.get(value.get("user"))
        chat = chats_simple.get(value.get("cgid"))
        ts_ms, ts_iso = format_timestamp(value.get("date"), use_utc)

        record = {
            "db_name": db_name,
            "chat_id": value.get("cgid"),
            "chat_name": chat.get("name") if chat else None,
            "chat_type": chat.get("type") if chat else None,
            "chat_members": chat.get("members") if chat else None,
            "message_id": value.get("id"),
            "message_gid": value.get("gid"),
            "message_index": value.get("index"),
            "union_id": value.get("unionId"),
            "sender_id": value.get("user"),
            "sender_account": member.get("account") if member else None,
            "sender_realname": member.get("realname") if member else None,
            "timestamp_ms": ts_ms,
            "timestamp_iso": ts_iso,
            "content_type": ctype,
            "content": content_raw,
            "content_json": normalize_json(content_obj) if content_obj is not None else None,
            "data_json": normalize_json(value.get("data")) if value.get("data") else None,
            "keys": value.get("keys"),
            "deleted": value.get("deleted"),
            "file_name": file_name,
            "file_size": file_size,
            "file_type": file_type,
            "url": url,
            "emoji": emoji,
            "image_path": str(image_path.relative_to(root_dir)) if image_path else None,
            "image_thumb_path": str(image_thumb.relative_to(root_dir)) if image_thumb else None,
        }
        out_records.append(record)

    out_records.sort(key=lambda r: (r["timestamp_ms"] or 0, r["message_index"] or 0, r["message_id"] or 0))

    out_dir.mkdir(parents=True, exist_ok=True)

    if fmt in ("json", "both"):
        log_status(f"{db_name}: writing JSON")
        payload = {
            "db_name": db_name,
            "exported_at": dt.datetime.now().isoformat(),
            "message_count": len(out_records),
            "messages": out_records,
            "members": normalize_json(members),
            "chats": chats_simple,
        }
        json_path = out_dir / "messages.json"
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    if fmt in ("csv", "both"):
        log_status(f"{db_name}: writing CSV")
        csv_path = out_dir / "messages.csv"
        fieldnames = [
            "db_name",
            "chat_id",
            "chat_name",
            "chat_type",
            "chat_members",
            "message_id",
            "message_gid",
            "message_index",
            "union_id",
            "sender_id",
            "sender_account",
            "sender_realname",
            "timestamp_ms",
            "timestamp_iso",
            "content_type",
            "content",
            "content_json",
            "data_json",
            "keys",
            "deleted",
            "file_name",
            "file_size",
            "file_type",
            "url",
            "emoji",
            "image_path",
            "image_thumb_path",
        ]
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for rec in out_records:
                row = dict(rec)
                row["chat_members"] = json.dumps(row["chat_members"], ensure_ascii=False) if row["chat_members"] else None
                row["content_json"] = json.dumps(row["content_json"], ensure_ascii=False) if row["content_json"] else None
                row["data_json"] = json.dumps(row["data_json"], ensure_ascii=False) if row["data_json"] else None
                for field in fieldnames:
                    row[field] = csv_safe_value(row.get(field))
                writer.writerow(row)


def main() -> None:
    """CLI entrypoint for exporting ZenTao client chats."""
    parser = argparse.ArgumentParser(description="Export ZenTao client chat records from IndexedDB.")
    parser.add_argument("--root", default="zentaoclient", help="Path to zentaoclient data root")
    parser.add_argument(
        "--db-name",
        action="append",
        help="Target DB name (e.g., gaoweifan@192.168.131.211__11443). Can be repeated.",
    )
    parser.add_argument("--out", default="output", help="Output directory")
    parser.add_argument("--include-deleted", action="store_true", help="Include deleted messages")
    parser.add_argument("--include-duplicates", action="store_true", help="Include duplicate records")
    parser.add_argument("--format", choices=["json", "csv", "both"], default="both")
    parser.add_argument("--timezone", choices=["local", "utc"], default="local")
    args = parser.parse_args()

    root_dir = pathlib.Path(args.root).resolve()
    ldb_path = root_dir / "IndexedDB" / "file__0.indexeddb.leveldb"
    if not ldb_path.exists():
        raise FileNotFoundError(f"IndexedDB not found: {ldb_path}")

    install_shims()
    add_ccl_reader_path(pathlib.Path(__file__).resolve().parent)

    from ccl_chromium_reader import ccl_chromium_indexeddb  # type: ignore

    wrapper = ccl_chromium_indexeddb.WrappedIndexDB(ldb_path)
    dbs = []
    for info in wrapper.database_ids:
        db = wrapper[info.dbid_no]
        dbs.append(db)
    available_names = sorted({db.name for db in dbs if db.name})

    target_names = set(args.db_name or [])
    if target_names:
        dbs = [db for db in dbs if db.name in target_names]
        if not dbs:
            available = ", ".join(available_names)
            raise SystemExit(f"No DB matched. Available: {available}")

    out_root = pathlib.Path(args.out).resolve()
    use_utc = args.timezone == "utc"

    for db in dbs:
        db_name = db.name or "unknown"
        images_dir = root_dir / "users" / db_name / "images"
        export_db(
            db=db,
            db_name=db_name,
            out_dir=out_root / db_name,
            images_dir=images_dir,
            include_deleted=args.include_deleted,
            include_duplicates=args.include_duplicates,
            fmt=args.format,
            use_utc=use_utc,
            root_dir=root_dir,
        )


if __name__ == "__main__":
    main()
