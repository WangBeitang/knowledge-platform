<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadFile } from 'element-plus'

import { ApiError } from '@/api/client'
import * as documentsApi from '@/api/documents'
import * as chunksApi from '@/api/chunks'
import { getTask } from '@/api/integration'
import ScopeTag from '@/components/ScopeTag.vue'
import type { ChunkView, ManagedDocumentView } from '@/types/api'
import { formatDateTime } from '@/utils/format'

const route = useRoute()
const router = useRouter()

const documentId = String(route.params.id)
const loading = ref(false)
const doc = ref<ManagedDocumentView | null>(null)
const notFound = ref(false)

// ===== Chunk 分页 =====
const chunkLoading = ref(false)
const chunks = ref<ChunkView[]>([])
const chunkTotal = ref(0)
const chunkPage = ref(1)
const chunkPageSize = ref(20)

// ===== Chunk 完整正文（只读 Drawer）=====
const chunkDrawer = reactive({
  visible: false,
  loading: false,
  chunk: null as ChunkView | null,
})

async function openChunkDetail(chunk: ChunkView): Promise<void> {
  chunkDrawer.visible = true
  chunkDrawer.loading = true
  chunkDrawer.chunk = null
  try {
    const resp = await chunksApi.getChunk(documentId, chunk.chunk_id)
    chunkDrawer.chunk = resp.data
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载 Chunk 正文失败')
  } finally {
    chunkDrawer.loading = false
  }
}

// ===== 启停对话框 =====
const enabledDialog = reactive({
  visible: false,
  chunk: null as ChunkView | null,
  targetEnabled: true,
  reasonCode: '',
  reasonText: '',
})
const savingEnabled = ref(false)

const disableReasons = [
  { value: 'parse_error', label: '解析错误' },
  { value: 'header_footer', label: '页眉页脚' },
  { value: 'garbled_text', label: '乱码文本' },
  { value: 'outdated_content', label: '内容过期' },
  { value: 'other', label: '其他' },
]

const restoreReasons = [
  { value: 'human_misjudgment', label: '人工误判' },
  { value: 'manual_restore', label: '人工恢复' },
  { value: 'other', label: '其他' },
]

// ===== 替换 =====
const replaceDialog = reactive({
  visible: false,
  file: null as File | null,
  submitting: false,
})
const taskView = ref<{ taskId: string; status: string; message: string } | null>(null)

const statusMap: Record<string, { text: string; type: 'success' | 'warning' | 'danger' | 'info' }> = {
  importing: { text: '导入中', type: 'warning' },
  active: { text: '正常', type: 'success' },
  import_failed: { text: '导入失败', type: 'danger' },
  replaced: { text: '已替换', type: 'info' },
  deleted: { text: '已删除', type: 'info' },
}

const docStatus = computed(() => statusMap[doc.value?.platform_status ?? ''] ?? { text: '--', type: 'info' as const })

async function fetchDocument(): Promise<void> {
  loading.value = true
  try {
    const resp = await documentsApi.getDocument(documentId)
    doc.value = resp.data
  } catch (err) {
    if (err instanceof ApiError && err.code === 'RESOURCE_NOT_FOUND') {
      notFound.value = true
      return
    }
    ElMessage.error(err instanceof ApiError ? err.message : '加载文档详情失败')
  } finally {
    loading.value = false
  }
}

async function fetchChunks(): Promise<void> {
  chunkLoading.value = true
  try {
    const resp = await chunksApi.listChunks(documentId, chunkPage.value, chunkPageSize.value)
    chunks.value = resp.data.items
    chunkTotal.value = resp.data.total
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载 Chunk 列表失败')
  } finally {
    chunkLoading.value = false
  }
}

function back(): void {
  void router.push('/admin/documents')
}

// ===== 启停 =====
function openEnableDialog(chunk: ChunkView): void {
  enabledDialog.chunk = chunk
  enabledDialog.targetEnabled = !chunk.enabled
  enabledDialog.reasonCode = ''
  enabledDialog.reasonText = ''
  enabledDialog.visible = true
}

const reasonOptions = computed(() => (enabledDialog.targetEnabled ? restoreReasons : disableReasons))

async function submitEnabled(): Promise<void> {
  const chunk = enabledDialog.chunk
  if (!chunk) return
  if (!enabledDialog.reasonCode) {
    ElMessage.warning('请选择原因')
    return
  }
  if (enabledDialog.reasonCode === 'other' && !enabledDialog.reasonText.trim()) {
    ElMessage.warning('请填写具体原因')
    return
  }
  savingEnabled.value = true
  try {
    const resp = await chunksApi.setChunkEnabled(documentId, chunk.chunk_id, {
      enabled: enabledDialog.targetEnabled,
      reason_code: enabledDialog.reasonCode,
      reason_text: enabledDialog.reasonText.trim(),
      expected_index_version: chunk.index_version,
    })
    ElMessage.success(resp.data.enabled ? 'Chunk 已启用' : 'Chunk 已停用')
    enabledDialog.visible = false
    await fetchChunks()
  } catch (err) {
    if (err instanceof ApiError && err.code === 'INDEX_VERSION_CONFLICT') {
      // 文档已重新索引：提示并刷新，禁止自动重新 PATCH
      enabledDialog.visible = false
      ElMessage.warning('文档已重新索引，请刷新后再操作')
      await Promise.all([fetchDocument(), fetchChunks()])
      return
    }
    ElMessage.error(err instanceof ApiError ? err.message : '操作失败')
  } finally {
    savingEnabled.value = false
  }
}

// ===== 重建 =====
async function handleRebuild(): Promise<void> {
  if (!doc.value) return
  try {
    await ElMessageBox.confirm(`确定重建「${doc.value.file_name}」的索引吗？`, '重建索引', {
      confirmButtonText: '重建',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    const resp = await documentsApi.rebuildDocument(documentId)
    ElMessage.success('重建任务已创建，正在轮询进度…')
    await pollTaskUntilDone(resp.data.task_id)
    await Promise.all([fetchDocument(), fetchChunks()])
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '重建失败')
  }
}

function pollTaskUntilDone(taskId: string): Promise<void> {
  return new Promise((resolve) => {
    const timer = setInterval(() => {
      void getTask(taskId)
        .then((resp) => {
          const status = resp.data.status
          if (status === 'succeeded') ElMessage.success('任务完成')
          if (status === 'succeeded' || status === 'failed' || status === 'cancelled') {
            clearInterval(timer)
            if (status === 'failed') ElMessage.error(resp.data.error_message ?? '任务失败')
            resolve(undefined)
          }
        })
        .catch(() => {
          clearInterval(timer)
          resolve(undefined)
        })
    }, 2000)
  })
}

// ===== 替换 =====
function onReplaceFileChange(uploadFiles: UploadFile[]): void {
  const raw = uploadFiles[0]?.raw
  replaceDialog.file = raw ?? null
}

async function submitReplace(): Promise<void> {
  if (!doc.value) return
  if (!replaceDialog.file) {
    ElMessage.warning('请选择要替换的 PDF 文件')
    return
  }
  replaceDialog.submitting = true
  taskView.value = null
  try {
    const resp = await documentsApi.replaceDocument(documentId, doc.value.knowledge_scope, replaceDialog.file)
    replaceDialog.visible = false
    ElMessage.success('替换任务已创建，正在轮询进度…')
    const taskId = resp.data.task_id
    taskView.value = { taskId, status: 'running', message: '替换进行中…' }
    await pollReplaceTask(taskId)
    await Promise.all([fetchDocument(), fetchChunks()])
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '替换失败')
  } finally {
    replaceDialog.submitting = false
  }
}

function pollReplaceTask(taskId: string): Promise<void> {
  return new Promise((resolve) => {
    const timer = setInterval(() => {
      void getTask(taskId)
        .then((resp) => {
          const status = resp.data.status
          if (status === 'succeeded' || status === 'failed' || status === 'cancelled') {
            clearInterval(timer)
            taskView.value = {
              taskId,
              status,
              message:
                status === 'succeeded'
                  ? '替换完成，旧文档已清理'
                  : resp.data.error_message ?? (status === 'failed' ? '替换失败，旧文档保留' : '替换已取消'),
            }
            resolve(undefined)
          }
        })
        .catch(() => {
          clearInterval(timer)
          taskView.value = { taskId, status: 'failed', message: '任务查询失败，请在文档列表查看状态' }
          resolve(undefined)
        })
    }, 2000)
  })
}

// ===== 删除 =====
async function handleDelete(): Promise<void> {
  if (!doc.value) return
  try {
    await ElMessageBox.confirm(
      `确定删除「${doc.value.file_name}」吗？将同时删除原 RAG 中的文档。`,
      '删除文档',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'error' },
    )
  } catch {
    return
  }
  try {
    await documentsApi.deleteDocument(documentId)
    ElMessage.success('文档已删除')
    await router.replace('/admin/documents')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '删除失败')
  }
}

onMounted(() => {
  void fetchDocument()
  void fetchChunks()
})
</script>

<template>
  <div class="document-detail">
    <div class="detail-header">
      <el-button
        link
        @click="back"
      >
        ← 返回文档列表
      </el-button>
    </div>

    <el-card
      v-if="notFound"
      shadow="never"
    >
      <el-empty description="文档不存在或已被删除">
        <el-button
          type="primary"
          @click="back"
        >
          返回文档列表
        </el-button>
      </el-empty>
    </el-card>

    <template v-else>
      <el-card
        v-loading="loading"
        shadow="never"
        class="doc-card"
      >
        <template #header>
          <div class="doc-head">
            <span class="card-title">{{ doc?.file_name ?? '文档详情' }}</span>
            <el-tag
              v-if="doc"
              :type="docStatus.type"
              size="small"
              effect="plain"
            >
              {{ docStatus.text }}
            </el-tag>
          </div>
        </template>

        <template v-if="doc">
          <el-descriptions
            :column="3"
            border
          >
            <el-descriptions-item label="知识范围">
              <ScopeTag :scope="doc.knowledge_scope" />
            </el-descriptions-item>
            <el-descriptions-item label="来源">
              {{ doc.source_kind === 'manual_upload' ? '手动上传' : doc.source_kind }}
            </el-descriptions-item>
            <el-descriptions-item label="RAG 状态">
              {{ doc.rag_status || '--' }}
            </el-descriptions-item>
            <el-descriptions-item label="解析状态">
              {{ doc.rag_parse_status || '--' }}
            </el-descriptions-item>
            <el-descriptions-item label="索引状态">
              {{ doc.rag_index_status || '--' }}
            </el-descriptions-item>
            <el-descriptions-item label="Chunk 数">
              {{ doc.chunk_count }}
            </el-descriptions-item>
            <el-descriptions-item label="索引版本">
              {{ doc.index_version }}
            </el-descriptions-item>
            <el-descriptions-item label="创建时间">
              {{ formatDateTime(doc.created_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="更新时间">
              {{ formatDateTime(doc.updated_at) }}
            </el-descriptions-item>
            <el-descriptions-item
              v-if="doc.error_code || doc.error_message"
              label="错误信息"
              :span="3"
            >
              <span class="error-text">{{ doc.error_code }}: {{ doc.error_message }}</span>
            </el-descriptions-item>
          </el-descriptions>

          <div class="doc-actions">
            <el-button
              v-if="['active', 'import_failed'].includes(doc.platform_status)"
              type="warning"
              plain
              @click="handleRebuild"
            >
              重建索引
            </el-button>
            <el-button
              v-if="doc.platform_status === 'active'"
              type="success"
              plain
              @click="replaceDialog.visible = true"
            >
              替换文档
            </el-button>
            <el-button
              v-if="['active', 'import_failed'].includes(doc.platform_status)"
              type="danger"
              plain
              @click="handleDelete"
            >
              删除文档
            </el-button>
          </div>

          <div
            v-if="taskView"
            class="task-result"
          >
            <el-alert
              :title="taskView.message"
              :type="taskView.status === 'succeeded' ? 'success' : taskView.status === 'running' ? 'info' : 'error'"
              :closable="false"
              show-icon
            />
          </div>
        </template>
      </el-card>

      <el-card
        shadow="never"
        class="chunk-card"
      >
        <template #header>
          <span class="card-title">Chunk 列表（正文只读）</span>
        </template>
        <el-table
          v-loading="chunkLoading"
          :data="chunks"
          border
          stripe
        >
          <el-table-column
            prop="position"
            label="# 序号"
            width="80"
          />
          <el-table-column
            label="状态"
            width="90"
          >
            <template #default="{ row }">
              <el-tag
                :type="row.enabled ? 'success' : 'info'"
                size="small"
                effect="plain"
              >
                {{ row.enabled ? '启用' : '停用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            prop="text"
            label="正文预览"
            min-width="260"
            show-overflow-tooltip
          />
          <el-table-column
            label="停用原因"
            width="160"
          >
            <template #default="{ row }">
              <span
                v-if="row.disabled_reason_code"
                class="reason-text"
              >
                {{ row.disabled_reason_code }}{{ row.disabled_reason_text ? `: ${row.disabled_reason_text}` : '' }}
              </span>
              <span v-else>--</span>
            </template>
          </el-table-column>
          <el-table-column
            label="操作"
            width="150"
            fixed="right"
          >
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                size="small"
                @click="openChunkDetail(row)"
              >
                查看
              </el-button>
              <el-button
                link
                type="primary"
                size="small"
                @click="openEnableDialog(row)"
              >
                {{ row.enabled ? '停用' : '启用' }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="pagination-bar">
          <el-pagination
            v-model:current-page="chunkPage"
            v-model:page-size="chunkPageSize"
            :total="chunkTotal"
            layout="total, prev, pager, next, sizes"
            :page-sizes="[10, 20, 50]"
            @current-change="fetchChunks"
            @size-change="fetchChunks"
          />
        </div>
      </el-card>
    </template>

    <!-- 启停对话框 -->
    <el-dialog
      v-model="enabledDialog.visible"
      :title="enabledDialog.targetEnabled ? '启用 Chunk' : '停用 Chunk'"
      width="480px"
    >
      <el-form label-position="top">
        <el-form-item label="原因类型">
          <el-radio-group v-model="enabledDialog.reasonCode">
            <el-radio
              v-for="opt in reasonOptions"
              :key="opt.value"
              :value="opt.value"
            >
              {{ opt.label }}
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item
          v-if="enabledDialog.reasonCode === 'other'"
          label="具体原因"
        >
          <el-input
            v-model="enabledDialog.reasonText"
            placeholder="请输入停用/恢复的具体原因"
            maxlength="200"
            show-word-limit
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="enabledDialog.visible = false">
          取消
        </el-button>
        <el-button
          type="primary"
          :loading="savingEnabled"
          @click="submitEnabled"
        >
          确认
        </el-button>
      </template>
    </el-dialog>

    <!-- 替换对话框 -->
    <el-dialog
      v-model="replaceDialog.visible"
      title="替换文档"
      width="480px"
    >
      <el-alert
        v-if="doc"
        title="替换将使用同知识范围的新文件重建文档；新文档成功前旧文档保持可检索。"
        type="info"
        :closable="false"
        show-icon
        class="replace-tip"
      />
      <el-upload
        drag
        :auto-upload="false"
        accept=".pdf,application/pdf"
        :limit="1"
        :on-change="onReplaceFileChange"
        :file-list="[]"
      >
        <div class="el-upload__text">
          拖拽 PDF 到此处，或<em>点击选择</em>
        </div>
      </el-upload>
      <template #footer>
        <el-button @click="replaceDialog.visible = false">
          取消
        </el-button>
        <el-button
          type="primary"
          :loading="replaceDialog.submitting"
          @click="submitReplace"
        >
          开始替换
        </el-button>
      </template>
    </el-dialog>

    <!-- Chunk 完整正文（只读） -->
    <el-drawer
      v-model="chunkDrawer.visible"
      title="Chunk 正文"
      size="480px"
    >
      <div
        v-loading="chunkDrawer.loading"
        class="chunk-drawer-body"
      >
        <template v-if="chunkDrawer.chunk">
          <el-descriptions
            :column="2"
            border
          >
            <el-descriptions-item label="序号">
              {{ chunkDrawer.chunk.position }}
            </el-descriptions-item>
            <el-descriptions-item label="状态">
              <el-tag
                :type="chunkDrawer.chunk.enabled ? 'success' : 'info'"
                size="small"
                effect="plain"
              >
                {{ chunkDrawer.chunk.enabled ? '启用' : '停用' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item
              v-if="chunkDrawer.chunk.disabled_reason_code"
              label="停用原因"
              :span="2"
            >
              {{ chunkDrawer.chunk.disabled_reason_code }}{{ chunkDrawer.chunk.disabled_reason_text ? `: ${chunkDrawer.chunk.disabled_reason_text}` : '' }}
            </el-descriptions-item>
          </el-descriptions>
          <div class="chunk-text-title">
            正文（只读）
          </div>
          <pre class="chunk-text">{{ chunkDrawer.chunk.text }}</pre>
        </template>
        <el-empty
          v-else-if="!chunkDrawer.loading"
          description="暂无内容"
        />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.detail-header {
  margin-bottom: 12px;
}

.doc-card {
  margin-bottom: 16px;
}

.doc-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.card-title {
  font-size: 14px;
  font-weight: 600;
}

.error-text {
  color: var(--kp-danger);
  word-break: break-all;
}

.doc-actions {
  margin-top: 16px;
  display: flex;
  gap: 10px;
}

.task-result {
  margin-top: 16px;
}

.chunk-card .pagination-bar {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}

.reason-text {
  font-size: 12px;
  color: var(--kp-text-secondary);
  word-break: break-all;
}

.replace-tip {
  margin-bottom: 14px;
}

.chunk-drawer-body {
  min-height: 200px;
}

.chunk-text-title {
  margin: 16px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--kp-text);
}

.chunk-text {
  margin: 0;
  padding: 12px;
  background: var(--kp-bg);
  border: 1px solid var(--kp-border);
  border-radius: 6px;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  color: var(--kp-text);
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
