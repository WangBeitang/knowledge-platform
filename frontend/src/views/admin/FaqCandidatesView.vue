<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError } from '@/api/client'
import * as faqsApi from '@/api/faqs'
import ScopeTag from '@/components/ScopeTag.vue'
import type { FaqCandidateView } from '@/types/api'
import { formatDateTime } from '@/utils/format'

const loading = ref(false)
const items = ref<FaqCandidateView[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = ref({ knowledge_scope: '', status: '' })

const dialogVisible = ref(false)
const publishing = ref(false)
const current = ref<FaqCandidateView | null>(null)
const publishForm = ref({ knowledge_scope: '', question: '', answer: '' })

const scopes = [
  { value: 'external_public', label: '外部公开' },
  { value: 'internal_shared', label: '内部共享' },
  { value: 'admin_private', label: '管理员专属' },
]

function statusText(status: string): string {
  const map: Record<string, string> = { pending_review: '待审核', published: '已发布', rejected: '已拒绝' }
  return map[status] ?? status
}

function statusType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'published') return 'success'
  if (status === 'pending_review') return 'warning'
  if (status === 'rejected') return 'danger'
  return 'info'
}

async function fetchCandidates(): Promise<void> {
  loading.value = true
  try {
    const resp = await faqsApi.listFaqCandidates({
      page: page.value,
      page_size: pageSize.value,
      knowledge_scope: filters.value.knowledge_scope || undefined,
      status: filters.value.status || undefined,
    })
    items.value = resp.data.items
    total.value = resp.data.total
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载候选列表失败')
  } finally {
    loading.value = false
  }
}

async function handleAnalyze(): Promise<void> {
  try {
    await ElMessageBox.confirm('将按归一化问题聚合全部问答日志生成 FAQ 候选（重复分析不会重复建行），继续吗？', '手动分析日志', {
      confirmButtonText: '开始分析',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch {
    return
  }
  try {
    const resp = await faqsApi.analyzeFaqCandidates()
    const d = resp.data
    ElMessage.success(`分析完成：新建 ${d.created} 条，更新 ${d.updated} 条，已发布跳过 ${d.skipped_published} 条`)
    await fetchCandidates()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '分析失败')
  }
}

function openPublish(row: FaqCandidateView): void {
  current.value = row
  publishForm.value = {
    knowledge_scope: row.knowledge_scope,
    question: row.normalized_question,
    answer: row.suggested_answer ?? '',
  }
  dialogVisible.value = true
}

async function submitPublish(): Promise<void> {
  if (!current.value) return
  if (!publishForm.value.question.trim() || !publishForm.value.answer.trim()) {
    ElMessage.warning('请填写标准问题与标准答案')
    return
  }
  publishing.value = true
  try {
    await faqsApi.publishFaqCandidate(current.value.id, publishForm.value)
    ElMessage.success('审核发布成功，已触发该范围 FAQ 文档同步')
    dialogVisible.value = false
    await fetchCandidates()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '发布失败')
  } finally {
    publishing.value = false
  }
}

async function handleReject(row: FaqCandidateView): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定拒绝候选「${row.normalized_question}」吗？`, '拒绝候选', {
      confirmButtonText: '拒绝',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await faqsApi.rejectFaqCandidate(row.id)
    ElMessage.success('候选已拒绝')
    await fetchCandidates()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '拒绝失败')
  }
}

onMounted(fetchCandidates)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h2>FAQ 候选</h2>
      <el-button
        type="primary"
        :loading="loading"
        @click="handleAnalyze"
      >
        手动分析日志
      </el-button>
    </div>

    <el-card shadow="never">
      <div class="filters">
        <el-select
          v-model="filters.knowledge_scope"
          placeholder="全部范围"
          clearable
          style="width: 180px"
          @change="fetchCandidates"
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
          @change="fetchCandidates"
        >
          <el-option
            label="待审核"
            value="pending_review"
          />
          <el-option
            label="已发布"
            value="published"
          />
          <el-option
            label="已拒绝"
            value="rejected"
          />
        </el-select>
        <el-button @click="fetchCandidates">
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
          prop="normalized_question"
          label="归一化问题"
          min-width="220"
          show-overflow-tooltip
        />
        <el-table-column
          label="样例问题"
          min-width="200"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            <div
              v-for="(q, i) in row.sample_questions.slice(0, 2)"
              :key="i"
              class="sample"
            >
              · {{ q }}
            </div>
          </template>
        </el-table-column>
        <el-table-column
          prop="ask_count"
          label="累计频次"
          width="100"
          sortable
        />
        <el-table-column
          label="状态"
          width="100"
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
          label="最近生成"
          width="160"
        >
          <template #default="{ row }">
            {{ formatDateTime(row.generated_at) }}
          </template>
        </el-table-column>
        <el-table-column
          label="操作"
          width="180"
          fixed="right"
        >
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'pending_review'"
              size="small"
              type="primary"
              @click="openPublish(row)"
            >
              审核发布
            </el-button>
            <el-button
              v-if="row.status === 'pending_review'"
              size="small"
              type="danger"
              @click="handleReject(row)"
            >
              拒绝
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
          @current-change="fetchCandidates"
        />
      </div>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      title="审核并发布 FAQ"
      width="560px"
    >
      <el-form label-width="90px">
        <el-form-item label="知识范围">
          <el-select
            v-model="publishForm.knowledge_scope"
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
            v-model="publishForm.question"
            type="textarea"
            :rows="2"
          />
        </el-form-item>
        <el-form-item label="标准答案">
          <el-input
            v-model="publishForm.answer"
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
          :loading="publishing"
          @click="submitPublish"
        >
          发布
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
.sample {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
