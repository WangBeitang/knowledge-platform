<script setup lang="ts">
/** 历史会话页：只查询当前用户自己的平台会话与消息（MySQL 是事实来源）。 */

import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, ref } from 'vue'

import * as chatApi from '@/api/chat'
import CitationCard from '@/components/CitationCard.vue'
import type { ChatMessageView, ChatSessionView, CitationView } from '@/types/api'

const sessions = ref<ChatSessionView[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

const currentSession = ref<ChatSessionView | null>(null)
const messages = ref<ChatMessageView[]>([])
const detailLoading = ref(false)

async function loadSessions(targetPage = page.value): Promise<void> {
  loading.value = true
  try {
    const resp = await chatApi.listSessions(targetPage, pageSize.value)
    sessions.value = resp.data.items
    total.value = resp.data.total
    page.value = targetPage
  } catch (err) {
    ElMessage.error((err as Error).message || '会话列表加载失败')
  } finally {
    loading.value = false
  }
}

async function openSession(session: ChatSessionView): Promise<void> {
  currentSession.value = session
  messages.value = []
  detailLoading.value = true
  try {
    const resp = await chatApi.getSessionDetail(session.id)
    currentSession.value = resp.data.session
    messages.value = resp.data.messages
  } catch (err) {
    ElMessage.error((err as Error).message || '会话详情加载失败')
  } finally {
    detailLoading.value = false
  }
}

async function renameSession(): Promise<void> {
  if (!currentSession.value) return
  try {
    const { value } = await ElMessageBox.prompt('请输入新的会话标题', '修改标题', {
      inputValue: currentSession.value.title,
      inputValidator: (v: string) => (v && v.trim().length > 0 ? true : '标题不能为空'),
    })
    const resp = await chatApi.updateSession(currentSession.value.id, { title: value.trim() })
    currentSession.value = resp.data
    await loadSessions()
    ElMessage.success('标题已更新')
  } catch (err) {
    if ((err as Error).name !== 'Cancel') {
      ElMessage.error((err as Error).message || '修改失败')
    }
  }
}

async function toggleArchive(): Promise<void> {
  if (!currentSession.value) return
  const next = currentSession.value.status === 'archived' ? 'active' : 'archived'
  try {
    const resp = await chatApi.updateSession(currentSession.value.id, { status: next })
    currentSession.value = resp.data
    await loadSessions()
    ElMessage.success(next === 'archived' ? '会话已归档' : '会话已恢复')
  } catch (err) {
    ElMessage.error((err as Error).message || '操作失败')
  }
}

async function removeSession(): Promise<void> {
  if (!currentSession.value) return
  try {
    await ElMessageBox.confirm('删除后会话不再展示（软删除），确定继续？', '删除会话', {
      type: 'warning',
    })
    await chatApi.deleteSession(currentSession.value.id)
    ElMessage.success('会话已删除')
    currentSession.value = null
    messages.value = []
    await loadSessions()
  } catch (err) {
    if ((err as Error).name !== 'Cancel') {
      ElMessage.error((err as Error).message || '删除失败')
    }
  }
}

function statusType(m: ChatMessageView): 'success' | 'danger' | 'info' | 'primary' {
  if (m.status === 'failed') return 'danger'
  if (m.answer_source === 'faq_cache') return 'primary'
  if (m.answer_source === 'rag') return 'success'
  return 'info'
}

function statusLabel(m: ChatMessageView): string {
  if (m.status === 'failed') return '失败'
  if (m.answer_source === 'faq_cache') return 'FAQ'
  if (m.answer_source === 'rag') return 'RAG'
  return '完成'
}

function citationList(m: ChatMessageView): CitationView[] {
  return m.citations ?? []
}

onMounted(() => loadSessions())
</script>

<template>
  <div class="history-view">
    <el-card
      shadow="never"
      class="history-panel"
    >
      <template #header>
        <div class="panel-header">
          <span class="panel-title">我的会话</span>
          <el-button
            size="small"
            @click="loadSessions()"
          >
            刷新
          </el-button>
        </div>
      </template>
      <el-table
        v-loading="loading"
        :data="sessions"
        highlight-current-row
        @row-click="openSession"
      >
        <el-table-column
          prop="title"
          label="标题"
          min-width="180"
          show-overflow-tooltip
        />
        <el-table-column
          label="状态"
          width="90"
        >
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.status === 'archived' ? 'info' : 'success'"
              effect="plain"
            >
              {{ row.status === 'archived' ? '已归档' : '正常' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column
          label="最近消息"
          width="160"
        >
          <template #default="{ row }">
            {{ row.last_message_at ? row.last_message_at.replace('T', ' ').slice(0, 16) : '—' }}
          </template>
        </el-table-column>
        <el-table-column
          label="创建时间"
          width="160"
        >
          <template #default="{ row }">
            {{ row.created_at ? row.created_at.replace('T', ' ').slice(0, 16) : '—' }}
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="prev, pager, next"
          @current-change="loadSessions"
        />
      </div>
    </el-card>

    <el-card
      v-if="currentSession"
      shadow="never"
      class="detail-panel"
    >
      <template #header>
        <div class="panel-header">
          <span class="panel-title">{{ currentSession.title }}</span>
          <div class="actions">
            <el-button
              size="small"
              @click="renameSession"
            >
              修改标题
            </el-button>
            <el-button
              size="small"
              @click="toggleArchive"
            >
              {{ currentSession.status === 'archived' ? '恢复' : '归档' }}
            </el-button>
            <el-button
              size="small"
              type="danger"
              plain
              @click="removeSession"
            >
              删除
            </el-button>
          </div>
        </div>
      </template>
      <div
        v-loading="detailLoading"
        class="message-list"
      >
        <div
          v-for="(message, index) in messages"
          :key="`${message.id}-${index}`"
          class="message-row"
          :class="message.role"
        >
          <div class="avatar">
            {{ message.role === 'user' ? '我' : 'AI' }}
          </div>
          <div class="bubble-wrap">
            <div class="bubble">
              <template v-if="message.role === 'assistant'">
                <div class="assistant-head">
                  <el-tag
                    v-if="message.status === 'completed'"
                    size="small"
                    :type="statusType(message)"
                    effect="plain"
                  >
                    {{ statusLabel(message) }}
                  </el-tag>
                  <el-tag
                    v-else
                    size="small"
                    type="danger"
                    effect="plain"
                  >
                    失败（{{ message.error_code ?? 'ERROR' }}）
                  </el-tag>
                </div>
              </template>
              <div class="msg-content">
                {{ message.content }}
              </div>
              <div
                v-if="message.role === 'assistant' && citationList(message).length > 0"
                class="citations"
              >
                <CitationCard
                  v-for="(citation, ci) in citationList(message)"
                  :key="`${citation.document_id ?? citation.source_url}-${ci}`"
                  :citation="citation"
                />
              </div>
            </div>
          </div>
        </div>
        <el-empty
          v-if="messages.length === 0 && !detailLoading"
          description="该会话暂无消息"
          :image-size="60"
        />
      </div>
    </el-card>

    <el-empty
      v-else
      description="选择左侧会话查看历史消息"
      class="placeholder"
    />
  </div>
</template>

<style scoped>
.history-view {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.history-panel {
  width: 620px;
  flex-shrink: 0;
}

.detail-panel {
  flex: 1;
  min-width: 0;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.panel-title {
  font-size: 14px;
  font-weight: 600;
}

.pagination {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}

.message-list {
  max-height: calc(100vh - 240px);
  overflow-y: auto;
  padding: 8px 4px;
}

.message-row {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}

.message-row.user {
  flex-direction: row-reverse;
}

.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  background: var(--kp-primary, #2f6bff);
}

.message-row.user .avatar {
  background: var(--el-color-success, #67c23a);
}

.bubble-wrap {
  max-width: 78%;
  min-width: 0;
}

.bubble {
  padding: 10px 14px;
  border-radius: 10px;
  background: var(--el-fill-color-light, #f5f7fa);
  font-size: 14px;
  line-height: 1.7;
  word-break: break-word;
}

.message-row.user .bubble {
  background: var(--kp-primary-light, #e8f0ff);
}

.assistant-head {
  margin-bottom: 6px;
}

.msg-content {
  white-space: pre-wrap;
  word-break: break-word;
}

.citations {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.placeholder {
  flex: 1;
}
</style>
