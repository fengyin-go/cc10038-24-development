"""混合日志文件接收入口：分段识别、原文留存、字段映射回写与中断恢复。

处理链路：
1. 上传内容按行识别格式，连续同格式的行组成一个小节；连续无法识别的行组成待适配小节。
2. 可识别小节逐行落成 raw_records 接收记录，并把文件行号、适配配置和适配版本写在记录上。
3. 解析时按接收记录选中的格式查找 field_parser 规则，实际输出映射与规则版本回写到小节（接收清单）。
4. 无法识别的小节只保存原文和原文引用（文件标识 + 行号范围），不生成 raw_records /
   parsed_entity，因此不会进入合格明细。
5. 同一文件标识再次上传按恢复处理：内容摘要一致才允许恢复，已确认行按行号去重，
   已确认小节整段复用，不重复计入。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from app.store import store

FILES = "_intake_files"
SEGMENTS = "_intake_segments"

STATUS_PENDING_RECEIVE = "待接收"
STATUS_PENDING_ADAPTER = "待适配"
STATUS_ACCEPTED = "已接收"
STATUS_PARSED = "已解析"
ACCEPTED_STATUSES = (STATUS_ACCEPTED, STATUS_PARSED)

# 平台自带两种已支持的格式；其余格式由值班员在格式适配层登记后生效。
_BUILTIN_ADAPTERS: list[dict[str, Any]] = [
    {
        "输入格式名": "pipe-delim",
        "适配器类型": "分隔符",
        "目标格式": "unified-log-v1",
        "转换配置": json.dumps(
            {
                "match": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\|",
                "delimiter": "|",
                "fields": ["time", "level", "message"],
            },
            ensure_ascii=False,
        ),
        "版本": "v1",
    },
    {
        "输入格式名": "bracket-syslog",
        "适配器类型": "正则",
        "目标格式": "unified-log-v1",
        "转换配置": json.dumps(
            {
                "match": r"^\[(?P<time>[^\]]+)\]\s+(?P<level>\w+)\s+(?P<message>.+)$",
                "fields": ["time", "level", "message"],
            },
            ensure_ascii=False,
        ),
        "版本": "v2",
    },
]

_BUILTIN_RULES: list[dict[str, Any]] = [
    {
        "规则名称": "pipe-delim-rule",
        "适用数据源": "混合文件接收",
        "输入格式": "pipe-delim",
        "输出字段列表": "log_time,log_level,log_message",
        "解析引擎": "分隔符",
    },
    {
        "规则名称": "bracket-syslog-rule",
        "适用数据源": "混合文件接收",
        "输入格式": "bracket-syslog",
        "输出字段列表": "event_time,severity,detail",
        "解析引擎": "正则",
    },
]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _next_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _ensure_catalog() -> None:
    """幂等补齐内置适配与映射规则；store 被重置后仍可继续接收入口。"""
    adapters = store.rows("format_adapter")
    known_formats = {row.get("输入格式名") for row in adapters}
    for spec in _BUILTIN_ADAPTERS:
        if spec["输入格式名"] in known_formats:
            continue
        entry: dict[str, Any] = {
            "id": _next_id("format_adapter"),
            "status": "已启用",
            "pending": False,
            "abnormal": False,
            "builtin": True,
        }
        entry.update(spec)
        adapters.append(entry)
        known_formats.add(spec["输入格式名"])

    parsers = store.rows("field_parser")
    known_rules = {row.get("规则名称") for row in parsers}
    for spec in _BUILTIN_RULES:
        if spec["规则名称"] in known_rules:
            continue
        entry = {
            "id": _next_id("field_parser"),
            "status": "已发布",
            "pending": False,
            "abnormal": False,
            "builtin": True,
        }
        entry.update(spec)
        parsers.append(entry)
        known_rules.add(spec["规则名称"])


def _adapter_version(adapter: dict[str, Any]) -> str:
    return str(adapter.get("版本") or "v1")


def _adapter_config(adapter: dict[str, Any]) -> dict[str, Any] | None:
    raw = adapter.get("转换配置")
    if isinstance(raw, dict):
        config = raw
    else:
        try:
            config = json.loads(str(raw or ""))
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(config, dict):
        return None
    if not config.get("match") and not config.get("json"):
        return None
    return config


def _is_json_adapter(config: dict[str, Any]) -> bool:
    return bool(config.get("json")) or str(config.get("mode") or "").lower() == "json"


def _extract_values(line: str, config: dict[str, Any]) -> dict[str, Any] | None:
    fields = [str(name) for name in (config.get("fields") or [])]
    if not fields:
        return None
    if _is_json_adapter(config):
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        return {name: payload.get(name, "") for name in fields}

    delimiter = config.get("delimiter")
    if delimiter:
        parts = line.split(str(delimiter))
        if len(parts) < 2:
            return None
        return {
            name: (parts[idx].strip() if idx < len(parts) else "")
            for idx, name in enumerate(fields)
        }

    pattern = str(config.get("match") or "")
    match = re.search(pattern, line) if pattern else None
    if match is None:
        return None
    groups = match.groupdict()
    if groups and all(name in groups for name in fields):
        return {name: groups.get(name, "") for name in fields}
    positional = match.groups()
    if not positional:
        # 只有整体匹配、没有可分配的捕获组：配置不足以抽字段，视为不匹配。
        return None
    return {
        name: (positional[idx] if idx < len(positional) else "")
        for idx, name in enumerate(fields)
    }


def _eligible_adapters() -> list[dict[str, Any]]:
    """可参与识别的适配配置；已弃用的不参与，草稿可先用于试接收。"""
    eligible: list[dict[str, Any]] = []
    for row in store.rows("format_adapter"):
        if row.get("status") == "已弃用":
            continue
        config = _adapter_config(row)
        if config is None:
            continue
        eligible.append({**row, "_config": config})
    # 已启用的优先；同状态时新登记版本优先，补登记后可以覆盖旧配置。
    eligible.sort(key=lambda row: (0 if row.get("status") == "已启用" else 1, -int(row.get("id", 0))))
    return eligible


def _match_line(
    line: str, adapters: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    for adapter in adapters:
        config = adapter["_config"]
        pattern = str(config.get("match") or "")
        if pattern and re.search(pattern, line) is None:
            continue
        values = _extract_values(line, config)
        if values is not None:
            return adapter, values
    return None, None


def _split_segments(
    content: str, adapters: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """把文件切成连续小节；空行不算日志行，也不切断同格式小节。"""
    raw_lines = content.splitlines()
    segments: list[dict[str, Any]] = []
    total_lines = 0
    current: dict[str, Any] | None = None
    for line_no, text in enumerate(raw_lines, start=1):
        if not text.strip():
            continue
        total_lines += 1
        adapter, values = _match_line(text, adapters)
        key = int(adapter["id"]) if adapter is not None else None
        if current is None or current["adapter_id"] != key:
            current = {
                "adapter_id": key,
                "adapter": adapter,
                "line_numbers": [],
                "lines": [],
                "values": [],
                "start_line": line_no,
                "end_line": line_no,
            }
            segments.append(current)
        current["line_numbers"].append(line_no)
        current["lines"].append(text)
        current["values"].append(values)
        current["end_line"] = line_no
    return segments, total_lines


def _rule_for_format(format_name: str) -> dict[str, Any] | None:
    candidates = [
        row for row in store.rows("field_parser")
        if row.get("status") == "已发布" and row.get("输入格式") == format_name
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: int(row.get("id", 0)))


def _rule_fields(rule: dict[str, Any] | None) -> list[str]:
    if not rule:
        return []
    return [
        part.strip()
        for part in re.split(r"[,，;；]", str(rule.get("输出字段列表") or ""))
        if part.strip()
    ]


def _resolve_mapping(
    adapter: dict[str, Any], adapter_fields: list[str]
) -> tuple[list[str], str, str | None]:
    """输出映射由该接收记录所选格式对应的解析规则决定。"""
    rule = _rule_for_format(str(adapter.get("输入格式名") or ""))
    names = _rule_fields(rule)
    if names:
        mapping = [
            names[idx] if idx < len(names) else adapter_fields[idx]
            for idx in range(max(len(names), len(adapter_fields)))
        ]
        return mapping, f"field_parser:{rule.get('规则名称')}", str(rule.get("规则名称"))
    return list(adapter_fields), f"adapter:{adapter.get('输入格式名')}", None


def _find_file(file_key: str) -> dict[str, Any] | None:
    for row in store.rows(FILES):
        if row.get("file_key") == file_key:
            return row
    return None


def _file_segments(file_key: str) -> list[dict[str, Any]]:
    rows = [row for row in store.rows(SEGMENTS) if row.get("file_key") == file_key]
    return sorted(rows, key=lambda row: (int(row.get("seq", 0)), int(row.get("id", 0))))


def _file_raw_records(file_key: str) -> list[dict[str, Any]]:
    return [row for row in store.rows("raw_records") if row.get("intake_file_key") == file_key]


def _batch_for_file(file_entry: dict[str, Any]) -> dict[str, Any]:
    batch_no = file_entry.get("batch_no")
    batches = store.rows("ingest_batch")
    if batch_no:
        for row in batches:
            if row.get("批次编号") == batch_no:
                return row
    batch_no = f"INTK-{_next_id('ingest_batch'):04d}"
    batch = {
        "id": _next_id("ingest_batch"),
        "批次编号": batch_no,
        "所属数据源": f"upload:{file_entry.get('file_name')}",
        "采集时间窗口": _now_iso(),
        "记录总数": "0",
        "批次状态": "混合文件接收",
        "status": "采集中",
        "pending": True,
        "abnormal": False,
        "intake_file_key": file_entry.get("file_key"),
        "started_at": _now_iso(),
    }
    batches.append(batch)
    file_entry["batch_no"] = batch_no
    return batch


def _refresh_batch_counts(file_entry: dict[str, Any], parse_failed: bool = False) -> dict[str, Any]:
    batch = _batch_for_file(file_entry)
    records = _file_raw_records(str(file_entry.get("file_key")))
    batch["记录总数"] = str(len(records))
    segments = _file_segments(str(file_entry.get("file_key")))
    blocked = any(seg.get("status") == STATUS_PENDING_RECEIVE for seg in segments)
    if records and all(row.get("status") == "已解析" for row in records) and not blocked and not parse_failed:
        batch["status"] = "已完成"
        batch["批次状态"] = "已完成"
        batch["pending"] = False
        batch["abnormal"] = False
    elif records:
        batch["status"] = "采集中"
        batch["批次状态"] = "混合文件接收"
        batch["pending"] = True
        batch["abnormal"] = parse_failed
    else:
        batch["status"] = "采集中"
        batch["批次状态"] = "混合文件接收"
        batch["pending"] = True
        batch["abnormal"] = False
    return batch


def _create_raw_record(
    file_entry: dict[str, Any],
    segment: dict[str, Any],
    line_no: int,
    text: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    batch = _batch_for_file(file_entry)
    record = {
        "id": _next_id("raw_records"),
        "批次编号": batch["批次编号"],
        "原始行号": str(line_no),
        "采集时间戳": _now_iso(),
        "原始正文": text,
        "解析状态": STATUS_PENDING_RECEIVE,
        "status": "待解析",
        "pending": True,
        "abnormal": False,
        "intake_file_key": file_entry["file_key"],
        "segment_id": segment["id"],
        "segment_seq": segment["seq"],
        "输入格式": segment.get("format"),
        "适配版本": segment.get("adapter_version"),
        "适配配置ID": segment.get("adapter_id"),
        "适配字段映射": json.dumps(values, ensure_ascii=False),
        "原始引用": segment.get("raw_ref"),
        "ingested_at": _now_iso(),
    }
    store.rows("raw_records").append(record)
    return record


def _segment_summary(segment: dict[str, Any]) -> dict[str, Any]:
    accepted = segment.get("status") in ACCEPTED_STATUSES
    line_numbers = segment.get("line_numbers") or []
    return {
        "id": segment.get("id"),
        "seq": segment.get("seq"),
        "start_line": segment.get("start_line"),
        "end_line": segment.get("end_line"),
        "line_range": segment.get("line_range"),
        "line_count": segment.get("line_count", len(line_numbers)),
        "format": segment.get("format"),
        "adapter_id": segment.get("adapter_id"),
        "adapter_version": segment.get("adapter_version"),
        "adapter_status": segment.get("adapter_status"),
        "status": segment.get("status"),
        "mapping": segment.get("mapping") or [],
        "mapping_source": segment.get("mapping_source"),
        "parser_rule": segment.get("parser_rule"),
        "counted_lines": len(line_numbers) if accepted else 0,
        "parsed_count": int(segment.get("parsed_count", 0)),
        "new_line_count": int(segment.get("new_line_count", 0)),
        "reused_line_count": int(segment.get("reused_line_count", 0)),
        "raw_text": segment.get("raw_text"),
        "raw_ref": segment.get("raw_ref"),
        "reason": segment.get("reason"),
        "download_url": f"/api/intake/segments/{segment.get('id')}/download",
    }


def _receipt(file_entry: dict[str, Any]) -> dict[str, Any]:
    segments = _file_segments(file_entry["file_key"])
    available = [seg for seg in segments if seg.get("status") in ACCEPTED_STATUSES]
    pending = [seg for seg in segments if seg.get("status") not in ACCEPTED_STATUSES]
    accepted_lines = sum(int(seg.get("line_count", 0)) for seg in available)
    pending_lines = sum(int(seg.get("line_count", 0)) for seg in pending)
    parsed_lines = sum(int(seg.get("parsed_count", 0)) for seg in available)
    return {
        "file_key": file_entry.get("file_key"),
        "file_name": file_entry.get("file_name"),
        "content_digest": file_entry.get("content_digest"),
        "status": file_entry.get("status"),
        "batch_no": file_entry.get("batch_no"),
        "total_lines": int(file_entry.get("total_lines", 0)),
        "counted_lines": int(file_entry.get("counted_lines", 0)),
        "accepted_lines": accepted_lines,
        "pending_lines": pending_lines,
        "parsed_lines": parsed_lines,
        "qualified_lines": parsed_lines,
        "unqualified_lines": pending_lines,
        "segment_count": len(segments),
        "available_segment_count": len(available),
        "pending_segment_count": len(pending),
        "upload_count": int(file_entry.get("upload_count", 0)),
        "created_at": file_entry.get("created_at"),
        "updated_at": file_entry.get("updated_at"),
        "available_sections": [_segment_summary(seg) for seg in available],
        "pending_sections": [_segment_summary(seg) for seg in pending],
        "segments": [_segment_summary(seg) for seg in segments],
    }


def _recompute_file_status(file_entry: dict[str, Any]) -> None:
    segments = _file_segments(file_entry["file_key"])
    if any(seg.get("status") == STATUS_PENDING_RECEIVE for seg in segments):
        file_entry["status"] = "接收中断"
        return
    if any(seg.get("status") == STATUS_PENDING_ADAPTER for seg in segments):
        file_entry["status"] = "部分接收"
        return
    if segments and all(seg.get("status") == STATUS_PARSED for seg in segments):
        file_entry["status"] = STATUS_PARSED
    else:
        file_entry["status"] = STATUS_ACCEPTED


class IntakeService:
    """混合日志文件接收与分段解析。"""

    def list_files(self) -> list[dict[str, Any]]:
        rows = sorted(
            store.rows(FILES),
            key=lambda row: (str(row.get("updated_at", "")), int(row.get("id", 0))),
            reverse=True,
        )
        return [_receipt(row) for row in rows]

    def get_receipt(self, file_key: str) -> dict[str, Any] | None:
        file_entry = _find_file(file_key)
        return _receipt(file_entry) if file_entry else None

    def receive(
        self,
        file_name: str,
        content: str,
        file_key: str | None = None,
        stop_after_segments: int | None = None,
        auto_parse: bool = True,
    ) -> dict[str, Any]:
        _ensure_catalog()
        if not str(file_name or "").strip():
            return {"ok": False, "message": "缺少文件名"}
        if stop_after_segments is not None and stop_after_segments < 0:
            return {"ok": False, "message": "stop_after_segments 不能为负数"}

        digest = _digest(content or "")
        key = (file_key or "").strip() or f"INTK-{digest[:12].upper()}"
        existing = _find_file(key)
        if existing is not None:
            if existing.get("content_digest") and existing.get("content_digest") != digest:
                return {
                    "ok": False,
                    "message": f"文件标识 {key} 已绑定另一份内容，按原文件标识核对失败",
                    "file_key": key,
                }
            file_entry = existing
            resumed = True
        else:
            file_entry = {
                "id": _next_id(FILES),
                "file_key": key,
                "file_name": str(file_name).strip(),
                "content_digest": digest,
                "content": content or "",
                "confirmed_lines": [],
                "status": STATUS_ACCEPTED,
                "total_lines": 0,
                "counted_lines": 0,
                "upload_count": 0,
                "batch_no": None,
                "created_at": _now_iso(),
            }
            store.rows(FILES).append(file_entry)
            resumed = False

        file_entry["file_name"] = str(file_name).strip()
        file_entry["content"] = content or ""
        file_entry["upload_count"] = int(file_entry.get("upload_count", 0)) + 1
        stats = self._receive_segments(file_entry, stop_after_segments)
        parse_stats = self.parse_file(key) if auto_parse else None
        _recompute_file_status(file_entry)
        file_entry["updated_at"] = _now_iso()
        receipt = _receipt(file_entry)
        if file_entry["status"] == "接收中断":
            message = "接收中断，已确认小节保留，可按原文件标识恢复"
        elif resumed:
            message = "按原文件标识恢复接收，已确认小节未重复计入"
        else:
            message = "文件已接收"
        return {
            "ok": True,
            "message": message,
            "file_key": key,
            "resumed": resumed,
            "stats": stats,
            "parse": parse_stats,
            "receipt": receipt,
        }

    def _receive_segments(
        self,
        file_entry: dict[str, Any],
        stop_after_segments: int | None,
    ) -> dict[str, Any]:
        key = file_entry["file_key"]
        content = file_entry.get("content", "")
        raw_lines = content.splitlines()
        detected, total_lines = _split_segments(content, _eligible_adapters())

        old_segments = _file_segments(key)
        old_by_range = {(seg.get("start_line"), seg.get("end_line")): seg for seg in old_segments}
        old_accepted_by_line: dict[int, dict[str, Any]] = {}
        for seg in old_segments:
            if seg.get("status") in ACCEPTED_STATUSES:
                for line_no in seg.get("line_numbers") or []:
                    old_accepted_by_line[int(line_no)] = seg
        confirmed = {int(value) for value in file_entry.get("confirmed_lines") or []}
        next_segment_id = max(
            (int(row.get("id", 0)) for row in store.rows(SEGMENTS)), default=0
        ) + 1

        new_segments: list[dict[str, Any]] = []
        stats = {
            "segments": len(detected),
            "new_segments": 0,
            "reused_segments": 0,
            "new_lines": 0,
            "reused_lines": 0,
            "counted_delta": 0,
        }

        def _blank(prior: dict[str, Any] | None) -> dict[str, Any]:
            nonlocal next_segment_id
            if prior is not None:
                return dict(prior)
            segment = {"id": next_segment_id, "created_at": _now_iso()}
            next_segment_id += 1
            return segment

        for idx, item in enumerate(detected):
            seq = idx + 1
            line_numbers = [int(value) for value in item["line_numbers"]]
            start_line, end_line = item["start_line"], item["end_line"]
            raw_ref = f"{key}#L{start_line}-L{end_line}"
            prior = old_by_range.get((start_line, end_line))
            adapter = item["adapter"]
            base = {
                "file_key": key,
                "seq": seq,
                "start_line": start_line,
                "end_line": end_line,
                "line_range": f"L{start_line}-L{end_line}",
                "line_count": len(line_numbers),
                "line_numbers": line_numbers,
                "raw_text": "\n".join(item["lines"]),
                "raw_ref": raw_ref,
                "updated_at": _now_iso(),
            }

            interrupted = stop_after_segments is not None and idx >= stop_after_segments
            if interrupted:
                segment = _blank(prior)
                segment.update(base)
                segment.update({
                    "format": adapter.get("输入格式名") if adapter else None,
                    "adapter_id": adapter.get("id") if adapter else None,
                    "adapter_version": _adapter_version(adapter) if adapter else None,
                    "adapter_status": adapter.get("status") if adapter else None,
                    "status": STATUS_PENDING_RECEIVE,
                    "reason": "接收中断，等待按原文件标识恢复",
                    "mapping": [],
                    "mapping_source": None,
                    "parser_rule": None,
                    "record_ids": [],
                    "parsed_count": 0,
                    "new_line_count": 0,
                    "reused_line_count": 0,
                })
                stats["new_segments" if prior is None else "reused_segments"] += 1
                new_segments.append(segment)
                continue

            if adapter is None:
                # 未知格式：只保留原文和原文引用，不建接收记录。
                if all(line_no in old_accepted_by_line for line_no in line_numbers):
                    segment = dict(old_accepted_by_line[line_numbers[0]])
                    segment.update(base)
                    segment["reused_line_count"] = len(line_numbers)
                    segment["new_line_count"] = 0
                    stats["reused_segments"] += 1
                    stats["reused_lines"] += len(line_numbers)
                    new_segments.append(segment)
                    continue
                segment = _blank(prior)
                segment.update(base)
                segment.update({
                    "format": None,
                    "adapter_id": None,
                    "adapter_version": None,
                    "adapter_status": None,
                    "status": STATUS_PENDING_ADAPTER,
                    "reason": "无匹配的格式适配配置，原文已留存待适配",
                    "mapping": [],
                    "mapping_source": None,
                    "parser_rule": None,
                    "record_ids": [],
                    "parsed_count": 0,
                    "new_line_count": 0,
                    "reused_line_count": 0,
                })
                stats["new_segments" if prior is None else "reused_segments"] += 1
                new_segments.append(segment)
                continue

            fresh_lines = [line_no for line_no in line_numbers if line_no not in confirmed]
            adapter_fields = list((item["values"][0] or {}).keys())
            # 小节的“适配版本 / 字段映射”记录首次识别时的结果；旧记录重传时
            # 小节可能复用了整段，这里沿用原有版本信息，避免被新配置覆盖。
            if prior is not None and prior.get("status") in ACCEPTED_STATUSES:
                adapter = {
                    **adapter,
                    "版本": prior.get("adapter_version") or _adapter_version(adapter),
                    "id": prior.get("adapter_id") or adapter.get("id"),
                }
                adapter_fields = list(prior.get("adapter_fields") or adapter_fields)
            mapping, mapping_source, parser_rule = _resolve_mapping(adapter, adapter_fields)

            if not fresh_lines:
                # 已确认小节整段复用，不新建接收记录、不重复计数。
                prior_segment = None
                if prior and prior.get("status") in ACCEPTED_STATUSES:
                    prior_segment = prior
                if prior_segment is None:
                    prior_segment = old_accepted_by_line.get(line_numbers[0])
                segment = dict(prior_segment) if prior_segment else _blank(None)
                segment.update(base)
                segment["reused_line_count"] = len(line_numbers)
                segment["new_line_count"] = 0
                segment["mapping"] = segment.get("mapping") or mapping
                segment["mapping_source"] = segment.get("mapping_source") or mapping_source
                stats["reused_segments"] += 1
                stats["reused_lines"] += len(line_numbers)
                new_segments.append(segment)
                continue

            reuse_prior = prior is not None and prior.get("status") not in ACCEPTED_STATUSES
            segment = _blank(prior if reuse_prior else None)
            segment.update(base)
            segment.update({
                "format": adapter.get("输入格式名"),
                "adapter_id": adapter.get("id"),
                "adapter_version": _adapter_version(adapter),
                "adapter_status": adapter.get("status"),
                "adapter_fields": adapter_fields,
                "mapping": mapping,
                "mapping_source": mapping_source,
                "parser_rule": parser_rule,
                "status": STATUS_ACCEPTED,
                "reason": None,
                "parsed_count": int(segment.get("parsed_count", 0)),
                "record_ids": list(segment.get("record_ids", [])),
                "new_line_count": len(fresh_lines),
                "reused_line_count": len(line_numbers) - len(fresh_lines),
            })
            values_by_line = {
                int(item["line_numbers"][pos]): item["values"][pos]
                for pos in range(len(item["line_numbers"]))
            }
            for line_no in fresh_lines:
                record = _create_raw_record(
                    file_entry, segment, line_no,
                    raw_lines[line_no - 1], values_by_line.get(line_no) or {},
                )
                segment["record_ids"].append(record["id"])
            confirmed.update(fresh_lines)
            stats["new_lines"] += len(fresh_lines)
            stats["counted_delta"] += len(fresh_lines)
            if len(fresh_lines) == len(line_numbers):
                stats["new_segments"] += 1
            else:
                stats["reused_segments"] += 1
            new_segments.append(segment)

        # 替换该文件的小节；复用的小节保留原 id（新小节在 _blank 里已经分配）。
        table = store.rows(SEGMENTS)
        table[:] = [row for row in table if row.get("file_key") != key] + new_segments

        file_entry["confirmed_lines"] = sorted(confirmed)
        file_entry["total_lines"] = total_lines
        file_entry["counted_lines"] = len(confirmed)
        _refresh_batch_counts(file_entry)
        _recompute_file_status(file_entry)
        return stats

    def parse_file(self, file_key: str) -> dict[str, Any]:
        _ensure_catalog()
        file_entry = _find_file(file_key)
        if file_entry is None:
            return {"ok": False, "message": f"接收文件 {file_key} 不存在"}
        batch = _batch_for_file(file_entry)
        records = _file_raw_records(file_key)
        segments = _file_segments(file_key)
        segment_by_id = {int(seg["id"]): seg for seg in segments}
        parsed_now = 0
        parse_failed = False

        for raw in records:
            if raw.get("status") == "已解析" and raw.get("parsed_entity_id"):
                continue
            adapter = store.find("format_adapter", int(raw.get("适配配置ID") or 0))
            if adapter is None:
                raw["status"] = "解析失败"
                raw["解析状态"] = "解析失败"
                raw["abnormal"] = True
                parse_failed = True
                continue
            rule = _rule_for_format(str(adapter.get("输入格式名") or ""))
            output_names = _rule_fields(rule)
            try:
                values = json.loads(raw.get("适配字段映射") or "{}")
            except (json.JSONDecodeError, TypeError):
                values = {}
            if not isinstance(values, dict):
                values = {}
            adapter_fields = list(values.keys())
            if not output_names:
                output_names = adapter_fields
            mapped = {
                name: (values.get(adapter_fields[idx], "") if idx < len(adapter_fields) else "")
                for idx, name in enumerate(output_names)
            }
            if not mapped or not all(str(value).strip() for value in mapped.values()):
                # 缺字段的记录不生成可校验明细，原文仍在接收清单里可追溯。
                raw["status"] = "解析失败"
                raw["解析状态"] = "解析失败"
                raw["abnormal"] = True
                parse_failed = True
                continue
            rule_name = str(rule.get("规则名称")) if rule else f"adapter:{adapter.get('输入格式名')}"
            version = str(raw.get("适配版本") or _adapter_version(adapter))
            entity_id = _next_id("parsed_entity")
            entity = {
                "id": entity_id,
                "来源批次": batch["批次编号"],
                "原始记录ID": str(raw["id"]),
                "解析规则版本": f"{rule_name}@{version}",
                "标准化字段集": json.dumps(mapped, ensure_ascii=False),
                "校验状态": "",
                "status": "待校验",
                "pending": True,
                "abnormal": False,
                "parser_rule": rule_name,
                "adapter_version": version,
                "intake_file_key": file_key,
                "原始行号": str(raw.get("原始行号")),
                "segment_seq": raw.get("segment_seq"),
                "parsed_at": _now_iso(),
            }
            store.rows("parsed_entity").append(entity)
            raw["status"] = "已解析"
            raw["解析状态"] = "已解析"
            raw["pending"] = False
            raw["解析规则"] = rule_name
            raw["解析映射"] = json.dumps(output_names, ensure_ascii=False)
            raw["解析规则版本"] = f"{rule_name}@{version}"
            raw["parsed_entity_id"] = entity_id
            parsed_now += 1
            segment = segment_by_id.get(int(raw.get("segment_id") or 0))
            if segment is not None:
                segment["mapping"] = output_names
                segment["mapping_source"] = (
                    f"field_parser:{rule.get('规则名称')}" if rule
                    else f"adapter:{adapter.get('输入格式名')}"
                )
                segment["parser_rule"] = rule_name
                segment["parsed_count"] = int(segment.get("parsed_count", 0)) + 1

        for segment in segments:
            if (
                segment.get("status") == STATUS_ACCEPTED
                and int(segment.get("parsed_count", 0)) >= int(segment.get("line_count", 0))
            ):
                segment["status"] = STATUS_PARSED

        validate_result = None
        if parsed_now:
            # 复用既有质量校验链路：只有可解析接收记录会进入合格明细。
            from app.services.pipeline import pipeline
            validate_result = pipeline.validate_entities(batch["批次编号"])
        _refresh_batch_counts(file_entry, parse_failed=parse_failed)
        if parsed_now or parse_failed:
            pending_lines = int(file_entry.get("total_lines", 0)) - len(file_entry.get("confirmed_lines") or [])
            success = parsed_now
            journal = store.rows("parse_journal")
            journal.append({
                "id": _next_id("parse_journal"),
                "操作类型": "接收解析",
                "批次编号": batch["批次编号"],
                "执行时间": _now_iso(),
                "影响的记录数": str(success),
                "执行结果": "失败" if parse_failed and not parsed_now else ("部分成功" if pending_lines or parse_failed else "成功"),
                "status": "失败" if parse_failed and not parsed_now else ("部分成功" if pending_lines or parse_failed else "成功"),
                "pending": bool(pending_lines),
                "abnormal": parse_failed,
                "intake_file_key": file_key,
                "success_count": str(success),
                "failure_count": "1" if parse_failed else "0",
                "pending_lines": str(max(pending_lines, 0)),
            })
        _recompute_file_status(file_entry)
        file_entry["updated_at"] = _now_iso()
        return {
            "ok": True,
            "message": f"接收解析完成，新增 {parsed_now} 条标准化明细",
            "file_key": file_key,
            "batch_no": batch["批次编号"],
            "parsed_now": parsed_now,
            "parse_failed": parse_failed,
            "validate": validate_result,
            "receipt": _receipt(file_entry),
        }

    def recover(self, file_key: str, content_digest: str | None = None) -> dict[str, Any]:
        file_entry = _find_file(file_key)
        if file_entry is None:
            return {"ok": False, "message": f"接收文件 {file_key} 不存在"}
        if content_digest and content_digest != file_entry.get("content_digest"):
            return {"ok": False, "message": "文件内容摘要与原文件标识不一致，恢复已拒绝"}
        before_counted = int(file_entry.get("counted_lines", 0))
        raw_before = len(_file_raw_records(file_key))
        entity_before = len([
            row for row in store.rows("parsed_entity") if row.get("intake_file_key") == file_key
        ])
        stats = self._receive_segments(file_entry, stop_after_segments=None)
        parse_stats = self.parse_file(file_key)
        _recompute_file_status(file_entry)
        file_entry["updated_at"] = _now_iso()
        return {
            "ok": True,
            "message": "已按原文件标识恢复接收，此前确认的小节未重复计入",
            "file_key": file_key,
            "verified_digest": file_entry.get("content_digest"),
            "counted_before": before_counted,
            "counted_after": int(file_entry.get("counted_lines", 0)),
            "counted_delta": int(file_entry.get("counted_lines", 0)) - before_counted,
            "raw_before": raw_before,
            "raw_after": len(_file_raw_records(file_key)),
            "entities_before": entity_before,
            "entities_after": len([
                row for row in store.rows("parsed_entity") if row.get("intake_file_key") == file_key
            ]),
            "stats": stats,
            "parse": parse_stats,
            "receipt": _receipt(file_entry),
        }

    def get_segment(self, segment_id: int) -> dict[str, Any] | None:
        for segment in store.rows(SEGMENTS):
            if int(segment.get("id", 0)) == segment_id:
                detail = dict(segment)
                detail["download_url"] = f"/api/intake/segments/{segment_id}/download"
                return detail
        return None

    def candidates(self, file_key: str | None = None) -> list[dict[str, Any]]:
        rows = [
            segment for segment in store.rows(SEGMENTS)
            if segment.get("status") in (STATUS_PENDING_ADAPTER, STATUS_PENDING_RECEIVE)
            and (file_key is None or segment.get("file_key") == file_key)
        ]
        rows.sort(key=lambda row: (str(row.get("file_key", "")), int(row.get("seq", 0))))
        return [_segment_summary(segment) for segment in rows]


intake = IntakeService()
