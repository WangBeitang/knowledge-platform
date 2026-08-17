<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError } from '@/api/client'
import * as faqsApi from '@/api/faqs'
import ScopeTag from '@/components/ScopeTag.vue'
import type { FaqView } from '@/types/api'
import { formatDateTime } from '@/utils/format'

const loading = ref(false)
const items = ref<FaqView[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = ref({ knowledge_scope: '', status: '' })

const dialogVisible = ref(false)
const saving = ref(false)
const isEdit = ref(false)
const currentId = ref('')
const form = ref({ knowledge_scope: 'internal_shared', question: '', answer: '' })

const scopes = [
  { value: 'external_public', label: '外部公开' },
  { value: 'internal_shared', label: '内部共享' },
  { value: 'admin_private', label: '管理员专属' },
]

function statusText(status: string): string {
  return status === 'published' ? '已发布' : '已下线'
}

function statusType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'published') return 'success'
  return 'info'
}

function syncStatusText(status: string): string {
  const map: Record<string, string> = { pending: '待同步', syncing: '同步中', succeeded: '已同步', failed: '同步失败' }
  return map[status] ?? status
}

function syncStatusType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'succeeded') return 'success'
  if (status === 'syncing') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
}

async function fetchFaqs(): Promise<void> {
  loading.value = true
  try {
    const resp = await faqsApi.listFaqs({
      page: page.value,
      page_size: pageSize.value,
      knowledge_scope: filters.value.knowledge_scope || undefined,
      status: filters.value.status || undefined,
    })
    items.value = resp.data.items
    total.value = resp.data.total
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载 FAQ 列表失败')
  } finally {
    loading.value = false
  }
}

function openCreate(): void {
  isEdit.value = false
  currentId.value = ''
  form.value = { knowledge_scope: 'internal_shared', question: '', answer: '' }
  dialogVisible.value = true
}

function openEdit(row: FaqView): void {
  isEdit.value = true
  currentId.value = row.id
  form.value = { knowledge_scope: row.knowledge_scope, question: row.question, answer: row.answer }
  dialogVisible.value = true
}

async function submit(): Promise<void> {
  if (!form.value.question.trim() || !form.value.answer.trim()) {
    ElMessage.warning('请填写问题与答案')
    return
  }
  saving.value = true
  try {
    if (isEdit.value) {
      await faqsApi.updateFaq(currentId.value, { question: form.value.question, answer: form.value.answer })
      ElMessage.success('FAQ 已更新，正在重建该范围 FAQ 文档')
    } else {
      await faqsApi.createFaq(form.value)
      ElMessage.success('FAQ 已创建并发布，正在同步 FAQ 文档')
    }
    dialogVisible.value = false
    await fetchFaqs()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function handleUnpublish(row: FaqView): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定下线「${row.question}」吗？下线后精确匹配不再命中。`, '下线 FAQ', {
      confirmButtonText: '下线',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await faqsApi.unpublishFaq(row.id)
    ElMessage.success('FAQ 已下线，正在重建该范围 FAQ 文档')
    await fetchFaqs()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '下线失败')
  }
}

async function handleRepublish(row: FaqView): Promise<void> {
  try {
    await faqsApi.republishFaq(row.id)
    ElMessage.success('FAQ 已重新发布，正在重建该范围 FAQ 文档')
    await fetchFaqs()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '重新发布失败')
  }
}

async function handleRetrySync(row: FaqView): Promise<void> {
  try {
    const resp = await faqsApi.retryFaqSync(row.id)
    ElMessage.success(`同步重试已提交（状态：${syncStatusText(resp.data.status)}）`)
    await fetchFaqs()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '同步重试失败')
  }
}

onMounted(fetchFaqs)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h2>FAQ 管理</h2>
      <el-button
        type="primary"
        @click="openCreate"
      >
        新建 FAQ
      </el-button>
    </div>

    <el-card shadow="never">
      <div class="filters">
        <el-select
          v-model="filters.knowledge_scope"
          placeholder="全部范围"
          clearable
          style="width: 180px"
          @change="fetchFaqs"
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
          @change="fetchFaqs"
        >
          <el-option
            label="已发布"
            value="published"
          />
          <el-option
            label="已下线"
            value="unpublished"
          />
        </el-select>
        <el-button @click="fetchFaqs">
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
          prop="question"
          label="标准问题"
          min-width="200"
          show-overflow-tooltip
        />
        <el-table-column
          prop="answer"
          label="标准答案"
          min-width="240"
          show-overflow-tooltip
        />
        <el-table-column
          label="状态"
          width="90"
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
          prop="hit_count"
          label="命中数"
          width="90"
          sortable
        />
        <el-table-column
          label="RAG 同步"
          width="120"
        >
          <template #default="{ row }">
            <el-tooltip
              :content="row.rag_sync_error ?? ''"
              :disabled="!row.rag_sync_error"
            >
              <el-tag
                :type="syncStatusType(row.rag_sync_status)"
                size="small"
              >
                {{ syncStatusText(row.rag_sync_status) }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column
          label="更新时间"
          width="160"
        >
          <template #default="{ row }">
            {{ formatDateTime(row.updated_at) }}
          </template>
        </el-table-column>
        <el-table-column
          label="操作"
          width="240"
          fixed="right"
        >
          <template #default="{ row }">
            <el-button
              size="small"
              @click="openEdit(row)"
            >
              编辑
            </el-button>
            <el-button
              v-if="row.status === 'published'"
              size="small"
              type="warning"
              @click="handleUnpublish(row)"
            >
              下线
            </el-button>
            <el-button
              v-else
              size="small"
              type="success"
              @click="handleRepublish(row)"
            >
              重发
            </el-button>
            <el-button
              v-if="row.rag_sync_status === 'failed'"
              size="small"
              type="danger"
              @click="handleRetrySync(row)"
            >
              同步重试
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
          @current-change="fetchFaqs"
        />
      </div>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑 FAQ' : '新建 FAQ' "
      width="560px"
    >
      <el-form label-width="90px">
        <el-form-item label="知识范围">
          <el-select
            v-model="form.knowledge_scope"
            :disabled="isEdit"
            style="width: 100%"
          >
            <el-option
              v-for="s in scopes"
              :key="s.value"
              :label="s.label"
              :value="s.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="标准问题">
          <el-input
            v-model="form.question"
            type="textarea"
            :rows="2"
          />
        </el-form-item>
        <el-form-item label="标准答案">
          <el-input
            v-model="form.answer"
            type="textarea"
            :rows="5"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">
          取消
        </el-button>
        <el-button
          type="primary"
          :loading="saving"
          @click="submit"
        >
          {{ isEdit ? '保存' : '创建并发布' }}
        </el-button>
      </template>
    </el-dialog>
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
.filters {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
