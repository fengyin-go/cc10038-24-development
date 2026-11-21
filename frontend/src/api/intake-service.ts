/** 混合日志文件接收接口：上传、回执、小节下载和中断恢复。 */

export type IntakeSegment = {
  id: number
  seq: number
  start_line: number
  end_line: number
  line_range: string
  line_count: number
  format: string | null
  adapter_id: number | null
  adapter_version: string | null
  adapter_status: string | null
  status: string
  mapping: string[]
  mapping_source: string | null
  parser_rule: string | null
  counted_lines: number
  parsed_count: number
  new_line_count: number
  reused_line_count: number
  raw_ref: string | null
  raw_text: string | null
  reason: string | null
  download_url: string
}

export type IntakeReceipt = {
  file_key: string
  file_name: string
  content_digest: string
  status: string
  batch_no: string | null
  total_lines: number
  counted_lines: number
  accepted_lines: number
  pending_lines: number
  parsed_lines: number
  qualified_lines: number
  unqualified_lines: number
  segment_count: number
  available_segment_count: number
  pending_segment_count: number
  upload_count: number
  created_at: string | null
  updated_at: string | null
  available_sections: IntakeSegment[]
  pending_sections: IntakeSegment[]
  segments: IntakeSegment[]
}

export type IntakeUploadResult = {
  ok: boolean
  message: string
  file_key?: string
  resumed?: boolean
  stats?: {
    segments: number
    new_segments: number
    reused_segments: number
    new_lines: number
    reused_lines: number
    counted_delta: number
  }
  parse?: { parsed_now: number; batch_no: string } | null
  receipt?: IntakeReceipt
}

export type IntakeRecoveryResult = {
  ok: boolean
  message: string
  file_key?: string
  counted_before?: number
  counted_after?: number
  counted_delta?: number
  raw_before?: number
  raw_after?: number
  entities_before?: number
  entities_after?: number
  receipt?: IntakeReceipt
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export async function uploadIntakeFile(payload: {
  file_name: string
  content: string
  file_key?: string
  stop_after_segments?: number | null
  auto_parse?: boolean
}): Promise<IntakeUploadResult> {
  const response = await fetch('/api/intake/files', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return readJson<IntakeUploadResult>(response)
}

export async function getIntakeReceipt(fileKey: string): Promise<IntakeReceipt> {
  return readJson<IntakeReceipt>(await fetch(`/api/intake/files/${encodeURIComponent(fileKey)}`))
}

export async function listIntakeFiles(): Promise<{ items: IntakeReceipt[]; total: number }> {
  return readJson(await fetch('/api/intake/files'))
}

export async function recoverIntakeFile(fileKey: string): Promise<IntakeRecoveryResult> {
  return readJson<IntakeRecoveryResult>(
    await fetch(`/api/intake/files/${encodeURIComponent(fileKey)}/recover`, { method: 'POST' }),
  )
}

export function intakeSegmentDownloadUrl(segmentId: number): string {
  return `/api/intake/segments/${segmentId}/download`
}
