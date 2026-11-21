"""混合日志文件接收入口测试：分段识别、未知段留存、接收清单回写、
中断恢复和补登记适配后的重传。"""
from __future__ import annotations

import json

import pytest

from app.store import store
from app.services.intake import FILES, SEGMENTS, intake
from app.services.pipeline import pipeline


MIXED_CONTENT = "\n".join([
    "2026-11-21T08:00:01|INFO|service started",       # 1 pipe-delim
    "2026-11-21T08:00:02|WARN|disk usage high",        # 2 pipe-delim
    "[2026-11-21 08:01:00] ERROR upstream timeout",    # 3 bracket-syslog
    "[2026-11-21 08:01:01] INFO retry scheduled",      # 4 bracket-syslog
    "2026-11-21 08:02:00 TRACE trace probe",           # 5 未知
    "2026-11-21 08:02:01 TRACE trace probe 2",         # 6 未知
]) + "\n"


def _raw(file_key: str):
    return [r for r in store.rows("raw_records") if r.get("intake_file_key") == file_key]


def _entities(file_key: str):
    return [p for p in store.rows("parsed_entity") if p.get("intake_file_key") == file_key]


def _register_space_adapter(enable: bool = True) -> None:
    """值班员补登记新格式：空间分隔的 trace 日志。"""
    config = {
        "match": (
            r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
            r"(?P<level>\w+)\s+(?P<message>.+)$"
        ),
        "fields": ["time", "level", "message"],
    }
    entry = {
        "id": max((int(r.get("id", 0)) for r in store.rows("format_adapter")), default=0) + 1,
        "输入格式名": "space-trace",
        "适配器类型": "正则",
        "目标格式": "unified-log-v1",
        "转换配置": json.dumps(config, ensure_ascii=False),
        "版本": "v3",
        "status": "已启用" if enable else "草稿",
        "pending": not enable,
        "abnormal": False,
    }
    store.rows("format_adapter").append(entry)
    rule = {
        "id": max((int(r.get("id", 0)) for r in store.rows("field_parser")), default=0) + 1,
        "规则名称": "space-trace-rule",
        "适用数据源": "混合文件接收",
        "输入格式": "space-trace",
        "输出字段列表": "trace_time,trace_level,trace_message",
        "解析引擎": "正则",
        "status": "已发布",
        "pending": False,
        "abnormal": False,
    }
    store.rows("field_parser").append(rule)


class TestMixedSegmentation:
    def test_mixed_file_splits_into_three_segments(self):
        result = intake.receive("mixed.log", MIXED_CONTENT)
        assert result["ok"] is True
        receipt = result["receipt"]
        assert receipt["segment_count"] == 3
        assert receipt["available_segment_count"] == 2
        assert receipt["pending_segment_count"] == 1
        assert [(s["start_line"], s["end_line"], s["format"]) for s in receipt["segments"]] == [
            (1, 2, "pipe-delim"),
            (3, 4, "bracket-syslog"),
            (5, 6, None),
        ]

    def test_raw_records_only_for_matched_segments(self):
        result = intake.receive("mixed.log", MIXED_CONTENT)
        key = result["file_key"]
        raw = _raw(key)
        assert len(raw) == 4
        assert sorted(int(r["原始行号"]) for r in raw) == [1, 2, 3, 4]
        # 未知段不产生任何接收记录，也不会进入合格明细
        assert all(int(r["原始行号"]) not in (5, 6) for r in raw)
        assert len(_entities(key)) == 4

    def test_unknown_segment_is_pending_adapter_with_raw_reference(self):
        result = intake.receive("mixed.log", MIXED_CONTENT)
        receipt = result["receipt"]
        pending = receipt["pending_sections"][0]
        assert pending["status"] == "待适配"
        assert pending["line_range"] == "L5-L6"
        assert pending["raw_ref"] == f"{result['file_key']}#L5-L6"
        assert pending["counted_lines"] == 0
        assert pending["mapping"] == []
        segment = intake.get_segment(pending["id"])
        assert "TRACE trace probe" in segment["raw_text"]
        assert intake.candidates(result["file_key"])[0]["id"] == pending["id"]

    def test_receipt_shows_available_and_pending_line_counts(self):
        receipt = intake.receive("mixed.log", MIXED_CONTENT)["receipt"]
        assert receipt["total_lines"] == 6
        assert receipt["counted_lines"] == 4
        assert receipt["accepted_lines"] == 4
        assert receipt["pending_lines"] == 2
        assert receipt["qualified_lines"] == 4
        assert receipt["unqualified_lines"] == 2
        assert receipt["status"] == "部分接收"

    def test_blank_lines_do_not_break_segment(self):
        content = "\n".join([
            "2026-11-21T08:00:01|INFO|a",
            "",
            "2026-11-21T08:00:02|INFO|b",
        ])
        receipt = intake.receive("blank.log", content)["receipt"]
        assert receipt["segment_count"] == 1
        assert receipt["segments"][0]["line_range"] == "L1-L3"
        assert receipt["segments"][0]["line_count"] == 2
        assert receipt["counted_lines"] == 2


class TestReceiptMapping:
    def test_each_segment_records_its_actual_mapping(self):
        receipt = intake.receive("mixed.log", MIXED_CONTENT)["receipt"]
        first, second = receipt["available_sections"]
        assert first["mapping"] == ["log_time", "log_level", "log_message"]
        assert first["mapping_source"] == "field_parser:pipe-delim-rule"
        assert first["adapter_version"] == "v1"
        assert second["mapping"] == ["event_time", "severity", "detail"]
        assert second["mapping_source"] == "field_parser:bracket-syslog-rule"
        assert second["adapter_version"] == "v2"

    def test_entities_carry_rule_version_and_file_line(self):
        result = intake.receive("mixed.log", MIXED_CONTENT)
        entities = _entities(result["file_key"])
        first = [e for e in entities if e["原始行号"] == "1"][0]
        assert first["解析规则版本"] == "pipe-delim-rule@v1"
        assert first["intake_file_key"] == result["file_key"]
        fields = json.loads(first["标准化字段集"])
        assert fields == {
            "log_time": "2026-11-21T08:00:01",
            "log_level": "INFO",
            "log_message": "service started",
        }

    def test_unknown_segment_never_enters_qualified_details(self):
        result = intake.receive("mixed.log", MIXED_CONTENT)
        for entity in _entities(result["file_key"]):
            assert int(entity["原始行号"]) <= 4
        assert all(row["status"] == "校验通过" for row in _entities(result["file_key"]))


class TestRecovery:
    def test_interrupted_upload_keeps_confirmed_segment(self):
        result = intake.receive("stop.log", MIXED_CONTENT, file_key="CASE-1", stop_after_segments=1)
        receipt = result["receipt"]
        assert receipt["status"] == "接收中断"
        assert receipt["counted_lines"] == 2
        statuses = [s["status"] for s in receipt["segments"]]
        assert statuses == ["已解析", "待接收", "待接收"]

    def test_recover_counts_only_missing_segments(self):
        intake.receive("stop.log", MIXED_CONTENT, file_key="CASE-2", stop_after_segments=1)
        result = intake.recover("CASE-2")
        assert result["ok"] is True
        assert result["counted_before"] == 2
        assert result["counted_after"] == 4
        assert result["counted_delta"] == 2
        assert result["raw_before"] == 2
        assert result["raw_after"] == 4
        # 已确认小节整段复用，记录数没有翻倍
        assert len(_raw("CASE-2")) == 4
        assert len(_entities("CASE-2")) == 4
        by_range = {s["line_range"]: s for s in result["receipt"]["segments"]}
        assert by_range["L1-L2"]["reused_line_count"] == 2
        assert by_range["L1-L2"]["new_line_count"] == 0

    def test_recover_rejects_digest_mismatch(self):
        result = intake.receive("stop.log", MIXED_CONTENT, file_key="CASE-3", stop_after_segments=1)
        bad = intake.recover("CASE-3", content_digest="deadbeef")
        assert bad["ok"] is False
        assert "摘要" in bad["message"]
        assert result["receipt"]["counted_lines"] == 2

    def test_reupload_same_key_does_not_double_count(self):
        first = intake.receive("mixed.log", MIXED_CONTENT, file_key="CASE-4")
        second = intake.receive("mixed.log", MIXED_CONTENT, file_key="CASE-4")
        assert second["resumed"] is True
        assert second["stats"]["counted_delta"] == 0
        assert second["receipt"]["counted_lines"] == first["receipt"]["counted_lines"]
        assert len(_raw("CASE-4")) == 4
        assert len(_entities("CASE-4")) == 4
        assert second["receipt"]["upload_count"] == 2

    def test_auto_recovery_after_interruption(self):
        intake.receive("stop.log", MIXED_CONTENT, file_key="CASE-5", stop_after_segments=2)
        result = intake.receive("stop.log", MIXED_CONTENT, file_key="CASE-5")
        assert result["resumed"] is True
        assert result["receipt"]["counted_lines"] == 4
        assert len(_raw("CASE-5")) == 4


class TestRegisterAdapterAndReupload:
    def test_new_adapter_promotes_pending_segment_on_reupload(self):
        first = intake.receive("mixed.log", MIXED_CONTENT, file_key="CASE-6")
        assert first["receipt"]["pending_lines"] == 2
        assert len(_entities("CASE-6")) == 4

        _register_space_adapter()
        second = intake.receive("mixed.log", MIXED_CONTENT, file_key="CASE-6")
        receipt = second["receipt"]

        assert receipt["status"] == "已解析"
        assert receipt["counted_lines"] == 6
        assert receipt["pending_lines"] == 0
        assert receipt["available_segment_count"] == 3
        assert receipt["pending_segment_count"] == 0

        # 前两段复用，不重复建记录
        assert second["stats"]["new_lines"] == 2
        assert second["stats"]["reused_lines"] == 4
        assert second["stats"]["counted_delta"] == 2
        assert len(_raw("CASE-6")) == 6
        assert len(_entities("CASE-6")) == 6

        trace = receipt["segments"][2]
        assert trace["format"] == "space-trace"
        assert trace["mapping"] == ["trace_time", "trace_level", "trace_message"]
        assert trace["mapping_source"] == "field_parser:space-trace-rule"
        assert trace["adapter_version"] == "v3"
        assert trace["parsed_count"] == 2

        # 每段计入的行与文件行号一一对应
        counted = sum(s["counted_lines"] for s in receipt["segments"])
        line_numbers = sorted(
            int(r["原始行号"]) for r in _raw("CASE-6")
        )
        assert counted == 6
        assert line_numbers == [1, 2, 3, 4, 5, 6]

    def test_new_adapters_do_not_break_existing_segments(self):
        _register_space_adapter()
        result = intake.receive("mixed.log", MIXED_CONTENT)
        receipt = result["receipt"]
        assert [s["format"] for s in receipt["segments"]] == [
            "pipe-delim", "bracket-syslog", "space-trace",
        ]
        assert receipt["status"] == "已解析"
        assert receipt["counted_lines"] == 6

    def test_deprecated_adapter_is_ignored(self):
        _register_space_adapter()
        for row in store.rows("format_adapter"):
            if row.get("输入格式名") == "space-trace":
                row["status"] = "已弃用"
        receipt = intake.receive("mixed.log", MIXED_CONTENT)["receipt"]
        assert receipt["segments"][2]["status"] == "待适配"
        assert receipt["counted_lines"] == 4


class TestLegacyPipelineUnaffected:
    def test_pipeline_parse_still_works(self, sample_raw_logs):
        pipeline.register_source("intake-regression", "http", "default")
        pipeline.validate_source("intake-regression")
        batch = pipeline.start_ingest_batch("intake-regression", "2026Q1", sample_raw_logs)
        result = pipeline.parse_batch(batch["batch"]["批次编号"])
        assert result["ok"] is True
        assert result["success"] == len(sample_raw_logs)

    def test_intake_tables_do_not_pollute_module_counts(self):
        intake.receive("mixed.log", MIXED_CONTENT)
        assert FILES != SEGMENTS
        from app.services import SERVICES
        snapshot = SERVICES["format_adapter"].domain_snapshot()
        assert snapshot["count"] >= 3
