<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError } from '@/api/client'
import * as gapsApi from '@/api/gaps'
import ScopeTag from '@/components/ScopeTag.vue'
import type { GapView } from '@/types/api'
import { formatDateTime } from '@/utils/format'

const loading = ref(false)
const items = ref<GapView[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = ref({ knowledge_scope: '', status: '' })

const dialogVisible = ref(false)
const resolving = ref(false)
const current = ref<GapView | null>(null)
const resolveForm = ref({ resolution_note: '', resolved_document_id: '' })

const scopes = [
  { value: 'external_public', label: '外部公开' },
  { value: 'internal_shared', label: '内部共享' },
  { value: 'admin_private', label: '管理员专属' },
]

function statusText(status: string): string {
  const map: Record<string, string> = { pending_review: '待审核', ignored: '已忽略', resolved: '已解决' }
  return map[status] ?? status
}

function statusType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'resolved') return 'success'
  if (status === 'pending_review') return 'warning'
  if (status === 'ignored') return 'info'
  return 'info'
}

function reasonText(reason: string): string {
  const map: Record<string, string> = { no_citation: '正常回答无引用', insufficient_evidence: '证据不足' }
  return map[reason] ?? reason
}

async function fetchGaps(): Promise<void> {
  loading.value = true
  try {
    const resp = await gapsApi.listKnowledgeGaps({
      page: page.value,
      page_size: pageSize.value,
      knowledge_scope: filters.value.knowledge_scope || undefined,
      status: filters.value.status || undefined,
    })
    items.value = resp.data.items
    total.value = resp.data.total
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载知识缺口列表失败')
  } finally {
    loading.value = false
  }
}

async function handleAnalyze(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '将按归一化问题聚合全部问答日志生成知识缺口候选（仅 RAG 正常完成但无引用或证据不足的问答；重复分析不会重复建行），继续吗？',
      '手动分析知识缺口',
      {
        confirmButtonText: '开始分析',
        cancelButtonText: '取消',
        type: 'info',
      },
    )
  } catch {
    return
  }
  try {
    const resp = await gapsApi.analyzeKnowledgeGaps()
    const d = resp.data
    ElMessage.success(`分析完成：新建 ${d.created} 条，更新 ${d.updated} 条`)
    await fetchGaps()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '分析失败')
  }
}

function openResolve(row: GapView): void {
  current.value = row
  resolveForm.value = {
    resolution_note: row.resolution_note ?? '',
    resolved_document_id: row.resolved_document_id ?? '',
  }
  dialogVisible.value = true
}

async function submitResolve(): Promise<void> {
  if (!current.value) return
  resolving.value = true
  try {
    await gapsApi.resolveKnowledgeGap(current.value.id, {
      resolution_note: resolveForm.value.resolution_note.trim() || undefined,
      resolved_document_id: resolveForm.value.resolved_document_id.trim() || undefined,
    })
    ElMessage.success('已标记为已解决')
    dialogVisible.value = false
    await fetchGaps()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '标记失败')
  } finally {
    resolving.value = false
  }
}

async function handleIgnore(row: GapView): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定忽略缺口「${row.normalized_question}」吗？（忽略后不会被重复分析复活）`,
      '忽略知识缺口',
      {
        confirmButtonText: '忽略',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
  } catch {
    return
  }
  try {
    await gapsApi.ignoreKnowledgeGap(row.id)
    ElMessage.success('缺口已忽略')
    await fetchGaps()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '忽略失败')
  }
}

onMounted(fetchGaps)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h2>知识缺口</h2>
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
          @change="fetchGaps"
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
          @change="fetchGaps"
        >
          <el-option
            label="待审核"
            value="pending_review"
          />
          <el-option
            label="已解决"
            value="resolved"
          />
          <el-option
            label="已忽略"
            value="ignored"
          />
        </el-select>
        <el-button @click="fetchGaps">
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
          min-width="200"
          show-overflow-tooltip
        />
        <el-table-column
          label="样例问题"
          min-width="180"
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
          label="缺口原因"
          width="130"
        >
          <template #default="{ row }">
            <el-tag
              :type="row.reason_code === 'insufficient_evidence' ? 'danger' : 'primary'"
              size="small"
            >
              {{ reasonText(row.reason_code) }}
            </el-tag>
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
          label="最近发生"
          width="160"
        >
          <template #default="{ row }">
            {{ formatDateTime(row.last_seen_at) }}
          </template>
        </el-table-column>
        <el-table-column
          label="操作"
          width="160"
          fixed="right"
        >
          <template #default="{ row }">
            <template v-if="row.status === 'pending_review'">
              <el-button
                size="small"
                type="primary"
                @click="openResolve(row)"
              >
                标记解决
              </el-button>
              <el-button
                size="small"
                type="info"
                @click="handleIgnore(row)"
              >
                忽略
              </el-button>
            </template>
            <span
              v-else-if="row.resolution_note"
              class="note"
            >
              {{ row.resolution_note }}
            </span>
            <span v-else>--</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="fetchGaps"
        />
      </div>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      title="标记知识缺口已解决"
      width="560px"
    >
      <el-form label-width="110px">
        <el-form-item label="处理说明">
          <el-input
            v-model="resolveForm.resolution_note"
            type="textarea"
            :rows="3"
            maxlength="1000"
            show-word-limit
            placeholder="可选：说明如何补充知识，如已上传/同步了哪些文档"
          />
        </el-form-item>
        <el-form-item label="关联文档 ID">
          <el-input
            v-model="resolveForm.resolved_document_id"
            maxlength="36"
            placeholder="可选：关联的平台文档 ID"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">
          取消
        </el-button>
        <el-button
          type="primary"
          :loading="resolving"
          @click="submitResolve"
        >
          确认解决
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
.note {
  font-size: 12px;
  color: var(--el-text-color-secondary);
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
