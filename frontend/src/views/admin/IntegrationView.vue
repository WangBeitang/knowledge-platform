<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError } from '@/api/client'
import * as integrationApi from '@/api/integration'
import type { RagDatasetStatusItem } from '@/types/api'

const loading = ref(false)
const bootstrapping = ref(false)
const verifying = ref(false)
const datasets = ref<RagDatasetStatusItem[]>([])
const overall = ref('')
const errorMessage = ref('')

async function loadStatus(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    const resp = await integrationApi.fetchRagStatus()
    datasets.value = resp.data.datasets
    overall.value = resp.data.overall
  } catch (err) {
    errorMessage.value = err instanceof ApiError ? err.message : '获取 RAG 状态失败'
  } finally {
    loading.value = false
  }
}

async function handleBootstrap(verifyOnly: boolean): Promise<void> {
  const mode = verifyOnly ? '校验' : '初始化'
  if (!verifyOnly) {
    try {
      await ElMessageBox.confirm(
        '初始化将操作三档知识库 Dataset（外部公开 / 内部共享 / 管理员专属）：\n' +
          '· 已存在则校验并补齐固定服务身份成员\n' +
          '· 不存在则创建，重复执行幂等，不会重复创建\n\n' +
          '确认继续初始化吗？',
        '三档 Dataset 初始化',
        { confirmButtonText: '初始化', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      return
    }
  }
  const busy = verifyOnly ? verifying : bootstrapping
  busy.value = true
  errorMessage.value = ''
  try {
    const resp = await integrationApi.bootstrapRag(verifyOnly)
    datasets.value = resp.data.datasets
    overall.value = resp.data.overall
    showBootstrapResult(verifyOnly, resp.data.overall)
  } catch (err) {
    errorMessage.value = err instanceof ApiError ? err.message : `${mode}失败`
  } finally {
    busy.value = false
  }
}

/** 按真实结果分色提示：succeeded/ok → success；partial → warning；failed → error。 */
function showBootstrapResult(verifyOnly: boolean, overall: string): void {
  if (overall === 'succeeded' || overall === 'ok') {
    ElMessage.success(
      verifyOnly ? '校验完成，三档状态正常（本次只查询，未修改任何上游状态）' : '初始化完成',
    )
  } else if (overall === 'partial') {
    ElMessage.warning(
      verifyOnly
        ? '校验完成，但存在缺失项，未做任何修改'
        : '初始化完成，但存在部分缺失或角色不符',
    )
  } else {
    ElMessage.error(verifyOnly ? '校验失败，请检查上游服务' : '初始化失败，请检查上游服务')
  }
}

function statusText(status: string): string {
  const map: Record<string, string> = {
    exists: '已存在',
    missing: '缺失',
    created: '已创建',
    existed: '已存在',
    verified: '已校验',
    failed: '失败',
  }
  return map[status] ?? status
}

function memberText(status: string): string {
  const map: Record<string, string> = {
    verified: '成员就绪',
    ensured: '成员已补齐',
    missing: '成员缺失',
    skipped: '跳过',
    failed: '失败',
  }
  return map[status] ?? status
}

onMounted(loadStatus)
</script>

<template>
  <div class="integration-view">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">RAG / Dataset 初始化状态</span>
          <div class="card-actions">
            <el-button
              size="small"
              :loading="loading"
              @click="loadStatus"
            >
              刷新状态
            </el-button>
            <el-button
              size="small"
              type="info"
              plain
              :loading="verifying"
              @click="handleBootstrap(true)"
            >
              仅校验（verify-only）
            </el-button>
            <el-button
              size="small"
              type="primary"
              :loading="bootstrapping"
              @click="handleBootstrap(false)"
            >
              执行初始化
            </el-button>
          </div>
        </div>
      </template>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="「仅校验」只查询并汇报三档 Dataset 与固定服务身份成员状态，不会修改任何上游；「执行初始化」先查后建，重复执行幂等。"
        class="hint"
      />

      <el-alert
        v-if="errorMessage"
        :title="errorMessage"
        type="error"
        :closable="false"
        show-icon
        class="page-error"
      />

      <el-table
        v-loading="loading || bootstrapping || verifying"
        :data="datasets"
        border
      >
        <el-table-column
          prop="scope"
          label="知识范围"
          min-width="150"
        />
        <el-table-column
          prop="dataset_id"
          label="Dataset ID"
          min-width="200"
        />
        <el-table-column
          label="Dataset 状态"
          width="110"
        >
          <template #default="{ row }">
            <el-tag
              :type="row.status === 'failed' ? 'danger' : row.status === 'missing' ? 'warning' : 'success'"
              effect="plain"
              size="small"
            >
              {{ statusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          label="成员状态"
          width="110"
        >
          <template #default="{ row }">
            <el-tag
              :type="row.member_status === 'missing' || row.member_status === 'failed' ? 'warning' : 'success'"
              effect="plain"
              size="small"
            >
              {{ memberText(row.member_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          label="说明"
          min-width="220"
        >
          <template #default="{ row }">
            {{ row.message }}
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-title {
  font-size: 14px;
  font-weight: 600;
}

.card-actions {
  display: flex;
  gap: 8px;
}

.hint {
  margin-bottom: 16px;
}

.page-error {
  margin-bottom: 16px;
}
</style>
