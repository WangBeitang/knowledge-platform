<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { ApiError } from '@/api/client'
import * as auditApi from '@/api/audit'
import type { AuditLogView } from '@/types/api'
import { formatDateTime } from '@/utils/format'

const loading = ref(false)
const items = ref<AuditLogView[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = ref({
  action: '',
  resource_type: '',
  result: '' as '' | 'succeeded' | 'failed',
  dateRange: null as [string, string] | null,
})

const actionOptions = [
  { value: 'user_created', label: '创建用户' },
  { value: 'user_updated', label: '修改用户' },
  { value: 'user_password_reset', label: '重置密码' },
  { value: 'dataset_bootstrap', label: 'Dataset 初始化' },
  { value: 'document_import', label: '文档导入' },
  { value: 'document_rebuild', label: '文档重建' },
  { value: 'document_replace', label: '文档替换' },
  { value: 'document_delete', label: '文档删除' },
  { value: 'chunk_status_changed', label: 'Chunk 启停' },
  { value: 'faq_candidate_published', label: '发布 FAQ 候选' },
  { value: 'faq_candidate_rejected', label: '拒绝 FAQ 候选' },
  { value: 'faq_created', label: '新建 FAQ' },
  { value: 'faq_updated', label: '修改 FAQ' },
  { value: 'faq_published', label: 'FAQ 发布' },
  { value: 'faq_unpublished', label: 'FAQ 下线' },
  { value: 'faq_republished', label: 'FAQ 重新发布' },
  { value: 'faq_sync_retried', label: 'FAQ 同步重试' },
  { value: 'gap_ignored', label: '缺口忽略' },
  { value: 'gap_resolved', label: '缺口解决' },
]

const resourceOptions = [
  { value: 'user', label: '用户' },
  { value: 'dataset', label: 'Dataset' },
  { value: 'document', label: '文档' },
  { value: 'chunk', label: 'Chunk' },
  { value: 'faq_candidate', label: 'FAQ 候选' },
  { value: 'faq', label: 'FAQ' },
  { value: 'faq_sync_run', label: 'FAQ 同步' },
  { value: 'knowledge_gap', label: '知识缺口' },
]

function actionText(action: string): string {
  const hit = actionOptions.find((o) => o.value === action)
  return hit ? hit.label : action
}

function resourceText(type: string): string {
  const hit = resourceOptions.find((o) => o.value === type)
  return hit ? hit.label : type
}

async function fetchLogs(): Promise<void> {
  loading.value = true
  try {
    const resp = await auditApi.listAuditLogs({
      page: page.value,
      page_size: pageSize.value,
      action: filters.value.action || undefined,
      resource_type: filters.value.resource_type || undefined,
      result: filters.value.result || undefined,
      date_from: filters.value.dateRange?.[0] ?? undefined,
      date_to: filters.value.dateRange?.[1] ?? undefined,
    })
    items.value = resp.data.items
    total.value = resp.data.total
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载审计日志失败')
  } finally {
    loading.value = false
  }
}

function resetAndFetch(): void {
  page.value = 1
  fetchLogs()
}

function snapshotText(snapshot: Record<string, unknown> | null): string {
  if (!snapshot || Object.keys(snapshot).length === 0) return '--'
  return JSON.stringify(snapshot)
}

onMounted(fetchLogs)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h2>审计日志</h2>
      <el-button
        type="primary"
        :loading="loading"
        @click="fetchLogs"
      >
        刷新
      </el-button>
    </div>

    <el-card shadow="never">
      <div class="filters">
        <el-select
          v-model="filters.action"
          placeholder="全部动作"
          clearable
          filterable
          style="width: 190px"
        >
          <el-option
            v-for="o in actionOptions"
            :key="o.value"
            :label="o.label"
            :value="o.value"
          />
        </el-select>
        <el-select
          v-model="filters.resource_type"
          placeholder="全部资源"
          clearable
          style="width: 150px"
        >
          <el-option
            v-for="o in resourceOptions"
            :key="o.value"
            :label="o.label"
            :value="o.value"
          />
        </el-select>
        <el-select
          v-model="filters.result"
          placeholder="全部结果"
          clearable
          style="width: 130px"
        >
          <el-option
            label="成功"
            value="succeeded"
          />
          <el-option
            label="失败"
            value="failed"
          />
        </el-select>
        <el-date-picker
          v-model="filters.dateRange"
          type="daterange"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          value-format="YYYY-MM-DD"
          style="width: 260px"
        />
        <el-button
          type="primary"
          @click="resetAndFetch"
        >
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
          label="时间"
          width="170"
        >
          <template #default="{ row }">
            {{ formatDateTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column
          label="操作人"
          width="130"
        >
          <template #default="{ row }">
            {{ row.operator_username ?? row.operator_user_id }}
          </template>
        </el-table-column>
        <el-table-column
          label="动作"
          width="140"
        >
          <template #default="{ row }">
            {{ actionText(row.action) }}
          </template>
        </el-table-column>
        <el-table-column
          label="资源"
          width="110"
        >
          <template #default="{ row }">
            {{ resourceText(row.resource_type) }}
          </template>
        </el-table-column>
        <el-table-column
          label="资源 ID"
          min-width="150"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            {{ row.resource_id ?? '--' }}
          </template>
        </el-table-column>
        <el-table-column
          label="结果"
          width="80"
        >
          <template #default="{ row }">
            <el-tag
              :type="row.result === 'succeeded' ? 'success' : 'danger'"
              size="small"
            >
              {{ row.result === 'succeeded' ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          label="错误码"
          width="140"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            {{ row.error_code ?? '--' }}
          </template>
        </el-table-column>
        <el-table-column
          prop="client_ip"
          label="IP"
          width="120"
        />
        <el-table-column
          label="变更内容"
          min-width="200"
        >
          <template #default="{ row }">
            <el-popover
              placement="left"
              width="360"
              trigger="hover"
            >
              <template #reference>
                <span class="snapshot-link">查看快照</span>
              </template>
              <div class="snapshot">
                <div class="snapshot-title">
                  变更前
                </div>
                <pre>{{ snapshotText(row.before) }}</pre>
                <div class="snapshot-title">
                  变更后
                </div>
                <pre>{{ snapshotText(row.after) }}</pre>
              </div>
            </el-popover>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="fetchLogs"
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
.filters {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
  align-items: center;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.snapshot-link {
  color: var(--el-color-primary);
  cursor: pointer;
  font-size: 12px;
}
.snapshot pre {
  margin: 4px 0 10px;
  padding: 6px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 160px;
  overflow: auto;
}
.snapshot-title {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
