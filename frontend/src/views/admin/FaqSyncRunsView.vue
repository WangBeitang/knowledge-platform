<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { ApiError } from '@/api/client'
import * as faqsApi from '@/api/faqs'
import ScopeTag from '@/components/ScopeTag.vue'
import type { FaqSyncRunView } from '@/types/api'
import { formatDateTime } from '@/utils/format'

const loading = ref(false)
const items = ref<FaqSyncRunView[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = ref({ knowledge_scope: '', status: '' })
let timer: number | undefined

const scopes = [
  { value: 'external_public', label: '外部公开' },
  { value: 'internal_shared', label: '内部共享' },
  { value: 'admin_private', label: '管理员专属' },
]

function statusText(status: string): string {
  const map: Record<string, string> = { pending: '待同步', syncing: '同步中', succeeded: '已同步', failed: '同步失败' }
  return map[status] ?? status
}

function statusType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'succeeded') return 'success'
  if (status === 'syncing') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
}

async function fetchRuns(showLoading = true): Promise<void> {
  if (showLoading) loading.value = true
  try {
    const resp = await faqsApi.listFaqSyncRuns({
      page: page.value,
      page_size: pageSize.value,
      knowledge_scope: filters.value.knowledge_scope || undefined,
      status: filters.value.status || undefined,
    })
    items.value = resp.data.items
    total.value = resp.data.total
  } catch (err) {
    if (showLoading) ElMessage.error(err instanceof ApiError ? err.message : '加载同步记录失败')
  } finally {
    loading.value = false
  }
}

async function handleRetry(row: FaqSyncRunView): Promise<void> {
  try {
    // sync:retry 按 FAQ 定位；同步记录视图通过列表刷新获取最新状态
    const run = await retryByScope(row.knowledge_scope)
    ElMessage.success(`同步重试已提交（状态：${statusText(run.status)}）`)
    await fetchRuns(false)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '同步重试失败')
  }
}

async function retryByScope(scope: string): Promise<{ status: string }> {
  // 通过 FAQ 列表找到该范围任意一条 FAQ 触发 sync:retry
  const resp = await faqsApi.listFaqs({ page: 1, page_size: 1, knowledge_scope: scope })
  const faq = resp.data.items[0]
  if (!faq) throw new Error('该范围暂无正式 FAQ')
  const runResp = await faqsApi.retryFaqSync(faq.id)
  return { status: runResp.data.status }
}

onMounted(() => {
  void fetchRuns()
  timer = window.setInterval(() => void fetchRuns(false), 5000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h2>FAQ 同步状态</h2>
      <span class="hint">每 5 秒自动刷新上游任务状态；同步失败可点击重试（优先继续旧文档清理，不重复上传）</span>
    </div>

    <el-card shadow="never">
      <div class="filters">
        <el-select
          v-model="filters.knowledge_scope"
          placeholder="全部范围"
          clearable
          style="width: 180px"
          @change="() => fetchRuns()"
        >
          <el-option
            v-for="s in scopes"
            :key="s.value"
            :label="s.label"
            :value="s.value"
          />
        </el-select>
        <el-select
          v-model="filters.status"
          placeholder="全部状态"
          clearable
          style="width: 150px"
          @change="() => fetchRuns()"
        >
          <el-option
            label="待同步"
            value="pending"
          />
          <el-option
            label="同步中"
            value="syncing"
          />
          <el-option
            label="已同步"
            value="succeeded"
          />
          <el-option
            label="同步失败"
            value="failed"
          />
        </el-select>
        <el-button @click="() => fetchRuns()">
          查询
        </el-button>
      </div>

      <el-table
        v-loading="loading"
        :data="items"
        border
        stripe
      >
        <el-table-column
          label="范围"
          width="130"
        >
          <template #default="{ row }">
            <ScopeTag :scope="row.knowledge_scope" />
          </template>
        </el-table-column>
        <el-table-column
          prop="generated_file_name"
          label="文档文件名"
          width="180"
        />
        <el-table-column
          label="状态"
          width="110"
        >
          <template #default="{ row }">
            <el-tag
              :type="statusType(row.status)"
              size="small"
            >
              {{ statusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          prop="rag_task_id"
          label="上游任务 ID"
          min-width="150"
          show-overflow-tooltip
        />
        <el-table-column
          prop="rag_document_id"
          label="当前文档 ID"
          min-width="150"
          show-overflow-tooltip
        />
        <el-table-column
          label="被替换文档"
          min-width="150"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            {{ row.previous_rag_document_id ?? '—' }}
          </template>
        </el-table-column>
        <el-table-column
          label="失败原因"
          min-width="180"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            <span
              v-if="row.error_message"
              class="error-text"
            >{{ row.error_message }}</span>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column
          label="创建时间"
          width="160"
        >
          <template #default="{ row }">
            {{ formatDateTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column
          label="完成时间"
          width="160"
        >
          <template #default="{ row }">
            {{ formatDateTime(row.finished_at) }}
          </template>
        </el-table-column>
        <el-table-column
          label="操作"
          width="110"
          fixed="right"
        >
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'failed'"
              size="small"
              type="danger"
              @click="handleRetry(row)"
            >
              重试
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="() => fetchRuns()"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.page {
  padding: 16px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.page-header h2 {
  margin: 0;
  font-size: 18px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.filters {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.error-text {
  color: var(--el-color-danger);
  font-size: 12px;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
