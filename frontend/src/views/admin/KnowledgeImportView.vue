<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import type { UploadFile, UploadRawFile } from 'element-plus'

import { ApiError } from '@/api/client'
import * as documentsApi from '@/api/documents'
import { getTask } from '@/api/integration'
import ImportTaskStatus from '@/components/ImportTaskStatus.vue'
import type { DocumentImportItem, IntegrationTaskView } from '@/types/api'

const scope = ref<'external_public' | 'internal_shared' | 'admin_private'>('internal_shared')
const selectedFiles = ref<File[]>([])
const submitting = ref(false)
const tasks = ref<Array<{ item: DocumentImportItem; view: IntegrationTaskView | null }>>([])
let timer: ReturnType<typeof setInterval> | null = null
const pollingTaskIds = new Set<string>()

function onFileChange(uploadFiles: UploadFile[]): void {
  selectedFiles.value = uploadFiles
    .map((f) => f.raw)
    .filter((raw): raw is UploadRawFile => raw !== undefined)
}

async function handleImport(): Promise<void> {
  if (!selectedFiles.value.length) {
    ElMessage.warning('请选择 PDF 文件')
    return
  }
  submitting.value = true
  try {
    const resp = await documentsApi.importDocuments(scope.value, selectedFiles.value)
    tasks.value = resp.data.items.map((item) => ({ item, view: null }))
    selectedFiles.value = []
    startPolling()
    const rejected = resp.data.items.filter((i) => i.status === 'rejected').length
    if (rejected > 0) {
      ElMessage.warning(`${rejected} 个文件被拒绝（仅支持 PDF）`)
    }
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '导入失败')
  } finally {
    submitting.value = false
  }
}

function startPolling(): void {
  stopPolling()
  timer = setInterval(() => {
    void pollOnce()
  }, 2000)
  void pollOnce()
}

function stopPolling(): void {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  pollingTaskIds.clear()
}

async function pollOnce(): Promise<void> {
  let active = false
  for (const entry of tasks.value) {
    const taskId = entry.item.task_id
    if (!taskId || entry.item.status !== 'pending') continue
    if (pollingTaskIds.has(taskId)) continue
    pollingTaskIds.add(taskId)
    active = true
    try {
      const resp = await getTask(taskId)
      entry.view = resp.data
      if (resp.data.status === 'succeeded' || resp.data.status === 'failed' || resp.data.status === 'cancelled') {
        pollingTaskIds.delete(taskId)
      }
    } catch {
      pollingTaskIds.delete(taskId) // 下一次继续尝试
    }
  }
  if (!active && timer) {
    stopPolling()
  }
}

onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="knowledge-import">
    <el-card
      shadow="never"
      class="import-card"
    >
      <template #header>
        <span class="card-title">知识导入</span>
      </template>

      <el-form label-position="top">
        <el-form-item label="知识范围">
          <el-radio-group v-model="scope">
            <el-radio value="external_public">
              外部公开
            </el-radio>
            <el-radio value="internal_shared">
              内部共享
            </el-radio>
            <el-radio value="admin_private">
              管理员专属
            </el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="PDF 文件（可多选）">
          <el-upload
            drag
            multiple
            accept=".pdf,application/pdf"
            :auto-upload="false"
            :on-change="onFileChange"
            :file-list="[]"
            :limit="20"
          >
            <el-icon class="el-icon--upload">
              <UploadFilled />
            </el-icon>
            <div class="el-upload__text">
              拖拽 PDF 到此处，或<em>点击选择</em>
            </div>
          </el-upload>
        </el-form-item>
      </el-form>

      <div
        v-if="selectedFiles.length"
        class="selected-files"
      >
        <el-tag
          v-for="(file, idx) in selectedFiles"
          :key="`${file.name}-${idx}`"
          closable
          size="small"
          @close="selectedFiles.splice(idx, 1)"
        >
          {{ file.name }}
        </el-tag>
      </div>

      <el-button
        type="primary"
        :loading="submitting"
        :disabled="!selectedFiles.length"
        @click="handleImport"
      >
        开始导入
      </el-button>
    </el-card>

    <div
      v-if="tasks.length"
      class="task-list"
    >
      <div
        v-for="(entry, idx) in tasks"
        :key="idx"
        class="task-item"
      >
        <div class="task-file">
          {{ entry.item.file_name }}
        </div>
        <template v-if="entry.item.status === 'rejected'">
          <el-alert
            :title="entry.item.error?.message ?? '文件被拒绝'"
            type="error"
            :closable="false"
            show-icon
          />
        </template>
        <ImportTaskStatus
          v-else-if="entry.view"
          :task="entry.view"
        />
        <div
          v-else
          class="task-waiting"
        >
          等待提交结果…
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.card-title {
  font-size: 14px;
  font-weight: 600;
}

.selected-files {
  margin-bottom: 16px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.task-list {
  margin-top: 20px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 12px;
}

.task-item {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.task-file {
  font-weight: 600;
  font-size: 13px;
  word-break: break-all;
}

.task-waiting {
  border: 1px dashed var(--kp-border);
  border-radius: 6px;
  padding: 10px 12px;
  color: var(--kp-text-secondary);
  font-size: 12px;
}
</style>
