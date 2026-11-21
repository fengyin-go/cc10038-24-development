"""混合日志文件接收入口：上传、接收回执、小节原文下载和中断恢复。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.services.intake import intake

router = APIRouter(prefix="/api/intake", tags=["混合文件接收"])


class UploadPayload(BaseModel):
    file_name: str = Field(..., min_length=1)
    content: str = ""
    file_key: str | None = None
    stop_after_segments: int | None = Field(default=None, ge=0)
    auto_parse: bool = True


@router.get("/files")
def intake_list_files() -> dict[str, Any]:
    items = intake.list_files()
    return {"items": items, "total": len(items)}


@router.post("/files")
def intake_upload(payload: UploadPayload) -> dict[str, Any]:
    return intake.receive(
        payload.file_name,
        payload.content,
        file_key=payload.file_key,
        stop_after_segments=payload.stop_after_segments,
        auto_parse=payload.auto_parse,
    )


@router.get("/files/{file_key}")
def intake_get_receipt(file_key: str) -> dict[str, Any]:
    receipt = intake.get_receipt(file_key)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"接收文件 {file_key} 不存在")
    return receipt


@router.post("/files/{file_key}/recover")
def intake_recover(
    file_key: str,
    content_digest: str | None = Query(default=None),
) -> dict[str, Any]:
    result = intake.recover(file_key, content_digest)
    if result.get("ok") is False and "不存在" in str(result.get("message")):
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.post("/files/{file_key}/parse")
def intake_parse(file_key: str) -> dict[str, Any]:
    return intake.parse_file(file_key)


@router.get("/candidates")
def intake_candidates(file_key: str | None = Query(default=None)) -> dict[str, Any]:
    items = intake.candidates(file_key)
    return {"items": items, "total": len(items)}


@router.get("/segments/{segment_id}")
def intake_segment(segment_id: int) -> dict[str, Any]:
    segment = intake.get_segment(segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail=f"接收小节 {segment_id} 不存在")
    return segment


@router.get("/segments/{segment_id}/download")
def intake_segment_download(segment_id: int) -> PlainTextResponse:
    segment = intake.get_segment(segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail=f"接收小节 {segment_id} 不存在")
    file_name = f"segment-{segment_id}-{segment.get('line_range', '')}.log"
    return PlainTextResponse(
        str(segment.get("raw_text") or ""),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )
