<script setup lang="ts">
/** 内部问答页：左会话列表 + 右对话流（SSE 流式）。管理员与 employee 复用。 */

import { ElMessage } from 'element-plus'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import * as chatApi from '@/api/chat'
import CitationCard from '@/components/CitationCard.vue'
import SseText from '@/components/SseText.vue'
import type {
  ChatMessageView,
  ChatSessionView,
  CitationView,
  SseErrorData,
  SseFinalData,
} from '@/types/api'

interface LocalMessage {
  turn_id: string
  role: 'user' | 'assistant'
  content: string
  status: 'streaming' | 'completed' | 'failed'
  citations: CitationView[]
  answer_source: 'faq_cache' | 'rag' | 'none' | null
  error_code: string | null
}

const sessions = ref<ChatSessionView[]>([])
const currentSessionId = ref<string | null>(null)
const messages = ref<LocalMessage[]>([])
const input = ref('')
const streaming = ref(false)
const loadingSessions = ref(false)
const loadingMessages = ref(false)
const errorMessage = ref('')

const messageListRef = ref<HTMLElement | null>(null)
let abortController: AbortController | null = null

const currentSession = (): ChatSessionView | null =>
  sessions.value.find((s) => s.id === currentSessionId.value) ?? null

const sendDisabled = (): boolean => {
  const session = currentSession()
  return (
    !session || session.status !== 'active' || streaming.value || input.value.trim().length === 0
  )
}

async function loadSessions(): Promise<void> {
  loadingSessions.value = true
  try {
    const resp = await chatApi.listSessions(1, 100)
    sessions.value = resp.data.items
  } catch (err) {
    ElMessage.error((err as Error).message || '会话列表加载失败')
  } finally {
    loadingSessions.value = false
  }
}

async function selectSession(sessionId: string): Promise<void> {
  abortStream()
  currentSessionId.value = sessionId
  messages.value = []
  errorMessage.value = ''
  loadingMessages.value = true
  try {
    const resp = await chatApi.getSessionDetail(sessionId)
    messages.value = resp.data.messages.map((m: ChatMessageView) => ({
      turn_id: m.turn_id,
      role: m.role,
      content: m.content,
      status: m.status === 'completed' ? 'completed' : 'failed',
      citations: m.citations ?? [],
      answer_source: m.answer_source,
      error_code: m.error_code,
    }))
    scrollToBottom()
  } catch (err) {
    ElMessage.error((err as Error).message || '会话加载失败')
  } finally {
    loadingMessages.value = false
  }
}

async function newSession(): Promise<void> {
  try {
    const resp = await chatApi.createSession()
    const created = resp.data
    sessions.value.unshift(created)
    await selectSession(created.id)
  } catch (err) {
    ElMessage.error((err as Error).message || '新建会话失败')
  }
}

async function send(): Promise<void> {
  const session = currentSession()
  const question = input.value.trim()
  if (!session || streaming.value || !question) return
  if (session.status !== 'active') {
    ElMessage.warning('会话已归档，无法继续提问')
    return
  }
  if (session.title === '新会话') {
    session.title = question.length > 30 ? `${question.slice(0, 30)}…` : question
  }

  const turnId = `turn-${Date.now()}`
  messages.value.push({ turn_id: turnId, role: 'user', content: question, status: 'completed', citations: [], answer_source: null, error_code: null })
  const assistant: LocalMessage = { turn_id: turnId, role: 'assistant', content: '', status: 'streaming', citations: [], answer_source: null, error_code: null }
  messages.value.push(assistant)

  input.value = ''
  streaming.value = true
  errorMessage.value = ''
  abortController = new AbortController()
  scrollToBottom()

  try {
    const result = await chatApi.streamQuestion(
      session.id,
      question,
      {
        signal: abortController.signal,
        onDelta: (delta) => {
          assistant.content += delta.text
          assistant.status = 'streaming'
          scrollToBottom()
        },
        onFinal: (final: SseFinalData) => {
          assistant.content = final.answer
          assistant.citations = final.citations ?? []
          assistant.answer_source = final.answer_source
          assistant.status = 'completed'
          scrollToBottom()
        },
        onError: (error: SseErrorData) => {
          assistant.status = 'failed'
          assistant.error_code = error.code
          errorMessage.value = error.message
        },
      },
    )
    if (!result.ok && result.error) {
      assistant.status = 'failed'
      assistant.error_code = result.error.code
      errorMessage.value = result.error.message
    }
    await loadSessions()
  } catch (err) {
    // 用户主动中断（切换会话/组件卸载）：不标记失败、不弹错误
    if ((err as Error).name === 'AbortError') return
    // 非流式错误（409 并发 / 404 / 422 等）
    assistant.status = 'failed'
    assistant.error_code = 'HTTP_ERROR'
    errorMessage.value = (err as Error).message || '发送失败'
    ElMessage.error(errorMessage.value)
  } finally {
    streaming.value = false
    abortController = null
  }
}

function abortStream(): void {
  if (abortController) {
    abortController.abort()
    abortController = null
  }
}

function scrollToBottom(): void {
  void nextTick(() => {
    if (messageListRef.value) {
      messageListRef.value.scrollTop = messageListRef.value.scrollHeight
    }
  })
}

function isAssistantStreaming(message: LocalMessage): boolean {
  return message.role === 'assistant' && message.status === 'streaming'
}

onMounted(async () => {
  await loadSessions()
  if (sessions.value.length > 0) {
    await selectSession(sessions.value[0].id)
  }
})

onBeforeUnmount(() => {
  abortStream()
})

watch(streaming, () => scrollToBottom())
</script>

<template>
  <div class="chat-view">
    <el-card
      shadow="never"
      class="session-panel"
    >
      <template #header>
        <div class="panel-header">
          <span class="panel-title">会话</span>
          <el-button
            type="primary"
            size="small"
            :icon="'Plus'"
            @click="newSession"
          >
            新建会话
          </el-button>
        </div>
      </template>
      <div
        v-loading="loadingSessions"
        class="session-list"
      >
        <div
          v-for="session in sessions"
          :key="session.id"
          class="session-item"
          :class="{ active: session.id === currentSessionId }"
          @click="selectSession(session.id)"
        >
          <div class="session-title">
            {{ session.title }}
          </div>
          <div class="session-meta">
            <el-tag
              v-if="session.status === 'archived'"
              size="small"
              type="info"
              effect="plain"
            >
              已归档
            </el-tag>
            <span
              v-else-if="session.last_message_at"
              class="session-time"
            >{{ session.last_message_at?.slice(5, 16) }}</span>
            <span
              v-else
              class="session-time"
            >暂无消息</span>
          </div>
        </div>
        <el-empty
          v-if="sessions.length === 0 && !loadingSessions"
          description="还没有会话"
          :image-size="60"
        />
      </div>
    </el-card>

    <el-card
      shadow="never"
      class="chat-panel"
    >
      <template #header>
        <div class="panel-header">
          <span class="panel-title">{{ currentSession()?.title ?? '内部问答' }}</span>
          <el-tag
            v-if="currentSession()?.status === 'archived'"
            size="small"
            type="info"
          >
            已归档（只读）
          </el-tag>
        </div>
      </template>

      <div
        ref="messageListRef"
        v-loading="loadingMessages"
        class="message-list"
      >
        <template v-if="messages.length === 0 && !loadingMessages">
          <el-empty
            description="向知识库提问，试试「客户如何办理风险测评？」"
            :image-size="80"
          />
        </template>
        <div
          v-for="(message, index) in messages"
          :key="`${message.turn_id}-${message.role}-${index}`"
          class="message-row"
          :class="message.role"
        >
          <div class="avatar">
            {{ message.role === 'user' ? '我' : 'AI' }}
          </div>
          <div class="bubble-wrap">
            <div class="bubble">
              <template v-if="message.role === 'user'">
                <span class="plain-text">{{ message.content }}</span>
              </template>
              <template v-else>
                <SseText
                  :text="message.content"
                  :streaming="isAssistantStreaming(message)"
                />
                <div
                  v-if="message.citations.length > 0"
                  class="citations"
                >
                  <CitationCard
                    v-for="(citation, ci) in message.citations"
                    :key="`${citation.document_id ?? citation.source_url}-${ci}`"
                    :citation="citation"
                  />
                </div>
                <div
                  v-if="message.status === 'failed'"
                  class="failed-tip"
                >
                  <el-tag
                    size="small"
                    type="danger"
                    effect="plain"
                  >
                    {{ message.error_code ?? 'ERROR' }}
                  </el-tag>
                </div>
              </template>
            </div>
          </div>
        </div>
      </div>

      <div class="input-area">
        <el-input
          v-model="input"
          type="textarea"
          :rows="2"
          resize="none"
          :placeholder="currentSession()?.status === 'archived' ? '会话已归档，无法继续提问' : '请输入问题，Enter 发送，Shift+Enter 换行'"
          :disabled="streaming || currentSession()?.status !== 'active'"
          @keydown.enter.exact.prevent="send"
        />
        <div class="input-footer">
          <span
            v-if="errorMessage"
            class="error-text"
          >
            {{ errorMessage }}
          </span>
          <el-button
            type="primary"
            :loading="streaming"
            :disabled="sendDisabled()"
            @click="send"
          >
            {{ streaming ? '回答中…' : '发送' }}
          </el-button>
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  gap: 16px;
  height: calc(100vh - 140px);
  min-height: 480px;
}

.session-panel {
  width: 260px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
}

.session-panel :deep(.el-card__body) {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  padding: 8px;
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

.session-list {
  flex: 1;
  overflow-y: auto;
}

.session-item {
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  margin-bottom: 4px;
  border: 1px solid transparent;
}

.session-item:hover {
  background: var(--el-fill-color-light, #f5f7fa);
}

.session-item.active {
  background: var(--kp-primary-light, #e8f0ff);
  border-color: var(--kp-primary, #2f6bff);
}

.session-title {
  font-size: 13px;
  color: var(--el-text-color-primary, #303133);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-meta {
  margin-top: 4px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.session-time {
  font-size: 11px;
  color: var(--el-text-color-secondary, #909399);
}

.chat-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.chat-panel :deep(.el-card__body) {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 12px;
}

.message-list {
  flex: 1;
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
  max-width: 76%;
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

.plain-text {
  white-space: pre-wrap;
  word-break: break-word;
}

.citations {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.failed-tip {
  margin-top: 8px;
}

.input-area {
  border-top: 1px solid var(--el-border-color-light, #e4e7ed);
  padding-top: 10px;
}

.input-footer {
  margin-top: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.error-text {
  font-size: 12px;
  color: var(--el-color-danger, #f56c6c);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 70%;
}
</style>
