<template>
  <section class="page" data-module="format_adapter">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <section class="intake-panel" data-testid="intake-panel">
      <h3 class="intake-title">混合日志文件接收</h3>
      <p class="page-desc">
        上传后按行识别格式并分段：匹配到适配配置的小节生成接收记录，未知格式小节只留存原文待适配。
      </p>
      <form class="intake-form" @submit.prevent="submitUpload">
        <label class="filter-item">
          <span>文件名</span>
          <input v-model="uploadName" data-testid="intake-file-name" placeholder="mixed-2026-11-21.log" required />
        </label>
        <label class="filter-item">
          <span>文件标识（恢复时保持同一个）</span>
          <input v-model="uploadKey" data-testid="intake-file-key" placeholder="留空则按内容摘要生成" />
        </label>
        <label class="filter-item">
          <span>中断前只接收前 N 个小节（留空表示整份接收）</span>
          <input v-model.number="stopAfter" data-testid="intake-stop-after" type="number" min="0" placeholder="可选" />
        </label>
        <label class="filter-item intake-file">
          <span>日志文件</span>
          <input data-testid="intake-file-input" type="file" accept=".log,.txt,text/plain" @change="pickFile" />
        </label>
        <button
          class="btn primary"
          data-testid="intake-submit"
          type="submit"
          :disabled="uploading || !uploadContent"
        >
          {{ uploading ? '接收中…' : '上传并接收' }}
        </button>
      </form>
      <p v-if="intakeMessage" class="intake-message" data-testid="intake-message">{{ intakeMessage }}</p>

      <article v-if="receipt" class="intake-receipt" data-testid="intake-receipt">
        <div class="intake-receipt-head">
          <strong>{{ receipt.file_name }}</strong>
          <span class="intake-status" :data-status="receipt.status" data-testid="intake-status">
            {{ receipt.status }}
          </span>
          <span class="intake-meta" data-testid="intake-file-key-view">标识：{{ receipt.file_key }}</span>
          <span class="intake-meta">批次：{{ receipt.batch_no ?? '—' }}</span>
          <span class="intake-meta">上传次数：{{ receipt.upload_count }}</span>
        </div>

        <div class="stat-row intake-stats">
          <article class="stat-card">
            <span class="stat-label">文件行数</span>
            <strong class="stat-value" data-testid="intake-total-lines">{{ receipt.total_lines }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">已计入行</span>
            <strong class="stat-value" data-testid="intake-counted-lines">{{ receipt.counted_lines }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">合格明细行</span>
            <strong class="stat-value" data-testid="intake-qualified-lines">{{ receipt.qualified_lines }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">待适配行</span>
            <strong class="stat-value" data-testid="intake-pending-lines">{{ receipt.pending_lines }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">可用/待适配小节</span>
            <strong class="stat-value">
              {{ receipt.available_segment_count }}/{{ receipt.pending_segment_count }}
            </strong>
          </article>
        </div>

        <h4 class="intake-subtitle">可用小节（已匹配适配配置）</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>行范围</th><th>行数</th><th>格式</th><th>适配版本</th>
              <th>字段映射</th><th>映射来源</th><th>计入行</th><th>已解析</th><th>本次新增/复用</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="section in receipt.available_sections"
              :key="section.id"
              :data-testid="`intake-section-${section.id}`"
            >
              <td>{{ section.line_range }}</td>
              <td>{{ section.line_count }}</td>
              <td>{{ section.format }}</td>
              <td>{{ section.adapter_version }}</td>
              <td>{{ section.mapping.join(' → ') || '—' }}</td>
              <td>{{ section.mapping_source ?? '—' }}</td>
              <td>{{ section.counted_lines }}</td>
              <td>{{ section.parsed_count }}</td>
              <td>{{ section.new_line_count }} / {{ section.reused_line_count }}</td>
            </tr>
            <tr v-if="!receipt.available_sections.length">
              <td colspan="9" class="empty-state">没有可解析的小节</td>
            </tr>
          </tbody>
        </table>

        <h4 class="intake-subtitle">待适配小节（原文已留存，不计入合格明细）</h4>
        <table class="data-table">
          <thead>
            <tr><th>行范围</th><th>行数</th><th>状态</th><th>原因</th><th>原文引用</th><th>候选片段</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="section in receipt.pending_sections"
              :key="section.id"
              :data-testid="`intake-pending-${section.id}`"
            >
              <td>{{ section.line_range }}</td>
              <td>{{ section.line_count }}</td>
              <td>{{ section.status }}</td>
              <td>{{ section.reason ?? '—' }}</td>
              <td>{{ section.raw_ref }}</td>
              <td>
                <details>
                  <summary class="link" :data-testid="`intake-preview-${section.id}`">查看原文</summary>
                  <pre class="intake-preview">{{ section.raw_text }}</pre>
                </details>
                <a class="link" :href="downloadUrl(section)" :data-testid="`intake-download-${section.id}`">
                  下载原文
                </a>
              </td>
            </tr>
            <tr v-if="!receipt.pending_sections.length">
              <td colspan="6" class="empty-state">所有小节均已匹配适配配置</td>
            </tr>
          </tbody>
        </table>

        <div class="page-actions intake-actions">
          <button
            v-if="receipt.status === '接收中断'"
            class="btn"
            data-testid="intake-recover"
            type="button"
            :disabled="recovering"
            @click="recover"
          >
            {{ recovering ? '恢复中…' : '按原文件标识恢复' }}
          </button>
          <span class="intake-meta">内容摘要：{{ receipt.content_digest.slice(0, 16) }}…</span>
        </div>
      </article>
    </section>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <p class="page-desc">
          新格式适配登记后，再按原文件标识重传即可让待适配小节进入接收记录。
        </p>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import {
  getIntakeReceipt,
  intakeSegmentDownloadUrl,
  recoverIntakeFile,
  uploadIntakeFile,
} from '@/api/intake-service'
import type { IntakeReceipt, IntakeSegment } from '@/api/intake-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const uploadName = ref('')
const uploadKey = ref('')
const uploadContent = ref('')
const stopAfter = ref<number | null>(null)
const uploading = ref(false)
const recovering = ref(false)
const intakeMessage = ref('')
const receipt = ref<IntakeReceipt | null>(null)

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

function downloadUrl(section: IntakeSegment): string {
  return intakeSegmentDownloadUrl(section.id)
}

function pickFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (!uploadName.value) uploadName.value = file.name
  const reader = new FileReader()
  reader.onload = () => {
    uploadContent.value = String(reader.result ?? '')
  }
  reader.readAsText(file)
}

async function submitUpload() {
  if (!uploadContent.value) {
    errorMessage.value = '请先选择要接收的日志文件'
    return
  }
  uploading.value = true
  intakeMessage.value = ''
  errorMessage.value = ''
  try {
    const result = await uploadIntakeFile({
      file_name: uploadName.value || 'upload.log',
      content: uploadContent.value,
      file_key: uploadKey.value.trim() || undefined,
      stop_after_segments: stopAfter.value === null ? undefined : stopAfter.value,
    })
    if (!result.ok) throw new Error(result.message)
    intakeMessage.value = result.message
    receipt.value = result.receipt ?? null
    if (receipt.value) {
      uploadKey.value = receipt.value.file_key
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '接收失败'
  } finally {
    uploading.value = false
  }
}

async function recover() {
  if (!receipt.value) return
  recovering.value = true
  errorMessage.value = ''
  try {
    const result = await recoverIntakeFile(receipt.value.file_key)
    if (!result.ok) throw new Error(result.message)
    intakeMessage.value = result.message
    receipt.value = result.receipt ?? (await getIntakeReceipt(receipt.value.file_key))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复失败'
  } finally {
    recovering.value = false
  }
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
