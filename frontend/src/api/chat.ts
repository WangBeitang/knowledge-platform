/** 内部问答 API 封装（会话 CRUD + SSE 流式问答）。 */

import { getStoredToken } from '@/api/client'
import { http } from '@/api/client'
import type {
  ApiResponse,
  ChatSessionDetailData,
  ChatSessionView,
  PageData,
  SseDeltaData,
  SseErrorData,
  SseFinalData,
  SseProgressData,
  SseReadyData,
} from '@/types/api'
import { consumeSse, type SseEvent } from '@/utils/sse'

const BASE = '/api/v1/chat'

export function createSession(): Promise<ApiResponse<ChatSessionView>> {
  return http.post<ChatSessionView>(`${BASE}/sessions`, {})
}

export function listSessions(
  page = 1,
  pageSize = 50,
): Promise<ApiResponse<PageData<ChatSessionView>>> {
  return http.get<PageData<ChatSessionView>>(`${BASE}/sessions?page=${page}&page_size=${pageSize}`)
}

export function getSessionDetail(sessionId: string): Promise<ApiResponse<ChatSessionDetailData>> {
  return http.get<ChatSessionDetailData>(`${BASE}/sessions/${sessionId}`)
}

export function updateSession(
  sessionId: string,
  patch: { title?: string; status?: string },
): Promise<ApiResponse<ChatSessionView>> {
  return http.patch<ChatSessionView>(`${BASE}/sessions/${sessionId}`, patch)
}

export function deleteSession(sessionId: string): Promise<ApiResponse<{ id: string; message: string }>> {
  return http.delete<{ id: string; message: string }>(`${BASE}/sessions/${sessionId}`)
}

/** SSE 事件回调（平台契约：ready/progress/delta/final/error）。 */
export interface StreamHandlers {
  onReady?: (data: SseReadyData) => void
  onProgress?: (data: SseProgressData) => void
  onDelta?: (data: SseDeltaData) => void
  onFinal?: (data: SseFinalData) => void
  onError?: (data: SseErrorData) => void
  signal?: AbortSignal
}

export interface StreamResult {
  ok: boolean
  error: SseErrorData | null
}

/** POST SSE 流式问答：只能用 fetch（原生 EventSource 不支持 POST）。 */
export async function streamQuestion(
  sessionId: string,
  question: string,
  handlers: StreamHandlers,
): Promise<StreamResult> {
  const token = getStoredToken()
  const response = await fetch(`${BASE}/sessions/${sessionId}/messages:stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ question }),
    signal: handlers.signal,
  })

  if (!response.ok) {
    // 非流式错误（404/409/422/400 等普通 JSON 错误）
    let message = `请求失败（${response.status}）`
    try {
      const payload = (await response.json()) as { error?: { code?: string; message?: string } }
      if (payload.error?.message) message = payload.error.message
    } catch {
      // 非 JSON 错误体：保留默认文案
    }
    throw new Error(message)
  }

  let lastError: SseErrorData | null = null
  await consumeSse(
    response,
    (event: SseEvent) => {
      switch (event.event) {
        case 'ready':
          handlers.onReady?.(event.data as unknown as SseReadyData)
          break
        case 'progress':
          handlers.onProgress?.(event.data as unknown as SseProgressData)
          break
        case 'delta':
          handlers.onDelta?.(event.data as unknown as SseDeltaData)
          break
        case 'final':
          handlers.onFinal?.(event.data as unknown as SseFinalData)
          break
        case 'error':
          lastError = event.data as unknown as SseErrorData
          handlers.onError?.(lastError)
          break
        default:
          break
      }
    },
    handlers.signal,
  )
  return { ok: lastError === null, error: lastError }
}
