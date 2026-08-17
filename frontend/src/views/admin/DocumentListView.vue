<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError } from '@/api/client'
import * as documentsApi from '@/api/documents'
import { getTask } from '@/api/integration'
import ScopeTag from '@/components/ScopeTag.vue'
import type { ManagedDocumentView } from '@/types/api'
import { formatDateTime } from '@/utils/format'

const router = useRouter()
const loading = ref(false)
const items = ref<ManagedDocumentView[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = ref({
  knowledge_scope: '',
  platform_status: '',
  file_name: '',
  source_kind: '',
})

async function fetchDocuments(): Promise<void> {
  loading.value = true
  try {
    const resp = await documentsApi.listDocuments({
      page: page.value,
      page_size: pageSize.value,
      knowledge_scope: filters.value.knowledge_scope || undefined,
      platform_status: filters.value.platform_status || undefined,
      file_name: filters.value.file_name || undefined,
      source_kind: filters.value.source_kind || undefined,
    })
    items.value = resp.data.items
    total.value = resp.data.total
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载文档列表失败')
  } finally {
    loading.value = false
  }
}

function statusText(status: string): string {
  const map: Record<string, string> = {
    importing: '导入中',
    active: '正常',
    import_failed: '导入失败',
    replaced: '已替换',
    deleted: '已删除',
  }
  return map[status] ?? status
}

function statusType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'active') return 'success'
  if (status === 'importing') return 'warning'
  if (status === 'import_failed') return 'danger'
  return 'info'
}

async function handleRebuild(row: ManagedDocumentView): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定重建「${row.file_name}」的索引吗？`, '重建索引', {
      confirmButtonText: '重建',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    const resp = await documentsApi.rebuildDocument(row.id)
    ElMessage.success('重建任务已创建，正在轮询进度…')
    const taskId = resp.data.task_id
    await pollTaskUntilDone(taskId)
    await fetchDocuments()
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
          if (status === 'succeeded') ElMessage.success('重建完成')
          if (status === 'succeeded' || status === 'failed' || status === 'cancelled') {
            clearInterval(timer)
            if (status === 'failed') ElMessage.error(resp.data.error_message ?? '重建失败')
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

async function handleDelete(row: ManagedDocumentView): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定删除「${row.file_name}」吗？将同时删除原 RAG 中的文档。`,
      '删除文档',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'error' },
    )
  } catch {
    return
  }
  try {
    await documentsApi.deleteDocument(row.id)
    ElMessage.success('文档已删除')
    await fetchDocuments()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '删除失败')
  }
}

function openDetail(row: ManagedDocumentView): void {
  void router.push(`/admin/documents/${row.id}`)
}

function openReplace(row: ManagedDocumentView): void {
  void router.push({ path: `/admin/documents/${row.id}`, query: { action: 'replace' } })
}

onMounted(fetchDocuments)
</script>

<template>
  <div class="document-list">
    <el-card shadow="never">
      <template #header>
        <div class="toolbar">
          <span class="card-title">文档管理</span>
          <div class="filters">
            <el-select
              v-model="filters.knowledge_scope"
              placeholder="知识范围"
              clearable
              style="width: 140px"
              @change="fetchDocuments"
            >
              <el-option
                label="外部公开"
                value="external_public"
              />
              <el-option
                label="内部共享"
                value="internal_shared"
              />
              <el-option
                label="管理员专属"
                value="admin_private"
              />
            </el-select>
            <el-select
              v-model="filters.platform_status"
              placeholder="平台状态"
              clearable
              style="width: 130px"
              @change="fetchDocuments"
            >
              <el-option
                label="导入中"
                value="importing"
              />
              <el-option
                label="正常"
                value="active"
              />
              <el-option
                label="导入失败"
                value="import_failed"
              />
              <el-option
                label="已替换"
                value="replaced"
              />
              <el-option
                label="已删除"
                value="deleted"
              />
            </el-select>
            <el-input
              v-model="filters.file_name"
              placeholder="文件名"
              clearable
              style="width: 160px"
              @keyup.enter="fetchDocuments"
              @clear="fetchDocuments"
            />
            <el-button
              type="primary"
              @click="fetchDocuments"
            >
              查询
            </el-button>
          </div>
        </div>
      </template>

      <el-table
        v-loading="loading"
        :data="items"
        border
        stripe
      >
        <el-table-column
          prop="file_name"
          label="文件名"
          min-width="180"
          show-overflow-tooltip
        />
        <el-table-column
          label="知识范围"
          width="110"
        >
          <template #default="{ row }">
            <ScopeTag :scope="row.knowledge_scope" />
          </template>
        </el-table-column>
        <el-table-column
          label="来源"
          width="110"
        >
          <template #default="{ row }">
            {{ row.source_kind === 'manual_upload' ? '手动上传' : row.source_kind }}
          </template>
        </el-table-column>
        <el-table-column
          label="平台状态"
          width="100"
        >
          <template #default="{ row }">
            <el-tag
              :type="statusType(row.platform_status)"
              size="small"
              effect="plain"
            >
              {{ statusText(row.platform_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="rag_status"
          label="RAG 状态"
          width="110"
        />
        <el-table-column
          prop="chunk_count"
          label="Chunk 数"
          width="90"
        />
        <el-table-column
          prop="index_version"
          label="索引版本"
          width="90"
        />
        <el-table-column
          label="更新时间"
          width="170"
        >
          <template #default="{ row }">
            {{ formatDateTime(row.updated_at) }}
          </template>
        </el-table-column>
        <el-table-column
          label="操作"
          width="220"
          fixed="right"
        >
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              size="small"
              @click="openDetail(row)"
            >
              详情
            </el-button>
            <el-button
              v-if="['active', 'import_failed'].includes(row.platform_status)"
              link
              type="warning"
              size="small"
              @click="handleRebuild(row)"
            >
              重建
            </el-button>
            <el-button
              v-if="row.platform_status === 'active'"
              link
              type="success"
              size="small"
              @click="openReplace(row)"
            >
              替换
            </el-button>
            <el-button
              v-if="['active', 'import_failed'].includes(row.platform_status)"
              link
              type="danger"
              size="small"
              @click="handleDelete(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-bar">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next, sizes"
          :page-sizes="[10, 20, 50]"
          @current-change="fetchDocuments"
          @size-change="fetchDocuments"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}

.card-title {
  font-size: 14px;
  font-weight: 600;
}

.filters {
  display: flex;
  gap: 8px;
}

.pagination-bar {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
