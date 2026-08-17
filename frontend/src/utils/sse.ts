/** SSE 流解析器（fetch + ReadableStream + TextDecoder）。

正确处理：
- 一个 TCP chunk 包多个 SSE event；
- 一个 SSE event 被拆到多个 chunk；
- JSON 跨 chunk；
- UTF-8 中文跨字节（TextDecoder stream 模式自动拼接）；
- 空行作为 event 边界；
- event/data 字段解析（data 多行合并）；
- final/error 后由调用方结束消费（本工具不主动停止）。

调用方通过 AbortController 中止读取（页面切换 / 用户主动断开），
前端 Abort 不应主动调用“取消 RAG”接口（本期无此契约）。
 */

export interface SseEvent {
  event: string
  data: Record<string, unknown>
}

export class SseParseError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SseParseError'
  }
}

export async function consumeSse(
  response: Response,
  onEvent: (event: SseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (!response.body) {
    throw new SseParseError('响应没有可读流')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let eventName = ''
  const dataLines: string[] = []

  const flushEvent = (): void => {
    if (!eventName && dataLines.length === 0) return
    let data: Record<string, unknown> = {}
    const raw = dataLines.join('\n')
    if (raw) {
      try {
        const parsed = JSON.parse(raw) as unknown
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          data = parsed as Record<string, unknown>
        }
      } catch {
        // 单个事件 data 理论上必须可解析；解析失败时交出空对象，由上层防御
        data = {}
      }
    }
    onEvent({ event: eventName || 'message', data })
    eventName = ''
    dataLines.length = 0
  }

  const processLine = (line: string): void => {
    const trimmed = line.endsWith('\r') ? line.slice(0, -1) : line
    if (trimmed === '') {
      flushEvent()
      return
    }
    if (trimmed.startsWith('event:')) {
      eventName = trimmed.slice('event:'.length).trim()
    } else if (trimmed.startsWith('data:')) {
      dataLines.push(trimmed.slice('data:'.length).trim())
    }
    // 忽略注释行（以 : 开头）和未知字段
  }

  try {
    for (;;) {
      if (signal?.aborted) {
        throw new DOMException('The operation was aborted.', 'AbortError')
      }
      const { done, value } = await reader.read()
      if (done) break
      // stream: true 让 TextDecoder 保留跨 chunk 的未完成 UTF-8 字节
      buffer += decoder.decode(value, { stream: true })
      let newlineIndex = buffer.indexOf('\n')
      while (newlineIndex >= 0) {
        const line = buffer.slice(0, newlineIndex)
        buffer = buffer.slice(newlineIndex + 1)
        processLine(line)
        newlineIndex = buffer.indexOf('\n')
      }
    }
    // 流结束：flush decoder 残留字节 + 处理残留行
    buffer += decoder.decode()
    let newlineIndex = buffer.indexOf('\n')
    while (newlineIndex >= 0) {
      const line = buffer.slice(0, newlineIndex)
      buffer = buffer.slice(newlineIndex + 1)
      processLine(line)
      newlineIndex = buffer.indexOf('\n')
    }
    if (buffer) processLine(buffer)
    flushEvent()
  } finally {
    reader.releaseLock()
  }
}
