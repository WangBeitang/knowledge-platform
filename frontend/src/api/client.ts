/** 统一 API Client：BaseURL、request_id、错误码、401 登出、TS 类型校验。 */

import type {
  ApiErrorBody,
  ApiErrorResponse,
  ApiResponse,
} from '@/types/api'

const BASE_URL = '/api/v1'

export class ApiError extends Error {
  code: string
  retryable: boolean
  requestId: string
  details: Record<string, unknown>

  constructor(body: ApiErrorBody, requestId: string) {
    super(body.message)
    this.name = 'ApiError'
    this.code = body.code
    this.retryable = body.retryable
    this.requestId = requestId
    this.details = body.details ?? {}
  }
}

const unauthorizedHandlers = new Set<() => void>()

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  if (handler) {
    unauthorizedHandlers.add(handler)
  } else {
    unauthorizedHandlers.clear()
  }
}

export function getStoredToken(): string | null {
  return localStorage.getItem('kp_access_token')
}

export function storeToken(token: string | null): void {
  if (token) {
    localStorage.setItem('kp_access_token', token)
  } else {
    localStorage.removeItem('kp_access_token')
  }
}

export interface RequestOptions {
  method?: string
  body?: unknown
  headers?: Record<string, string>
  signal?: AbortSignal
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
  const { method = 'GET', body, headers = {}, signal } = options
  const token = getStoredToken()
  const resp = await fetch(`${BASE_URL}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })

  const text = await resp.text()
  const payload = text ? (JSON.parse(text) as ApiResponse<T> | ApiErrorResponse) : null

  if (!resp.ok) {
    const errorBody: ApiErrorBody =
      payload && 'error' in payload
        ? payload.error
        : { code: 'INTERNAL_ERROR', message: '服务内部错误', retryable: false }
    const requestId = (payload && 'request_id' in payload ? payload.request_id : '') as string
    if (resp.status === 401 && errorBody.code === 'AUTH_REQUIRED') {
      storeToken(null)
      // 触发全部 401 处理器（清登录态 + 跳转登录页）
      unauthorizedHandlers.forEach((handler) => handler())
    }
    throw new ApiError(errorBody, requestId)
  }

  if (!payload || !('data' in payload)) {
    throw new ApiError(
      { code: 'INTERNAL_ERROR', message: '响应格式错误', retryable: false },
      '',
    )
  }
  return payload as ApiResponse<T>
}

export const http = {
  get<T>(path: string, headers?: Record<string, string>): Promise<ApiResponse<T>> {
    return request<T>(path, { method: 'GET', headers })
  },
  post<T>(path: string, body?: unknown, headers?: Record<string, string>): Promise<ApiResponse<T>> {
    return request<T>(path, { method: 'POST', body, headers })
  },
  patch<T>(path: string, body?: unknown, headers?: Record<string, string>): Promise<ApiResponse<T>> {
    return request<T>(path, { method: 'PATCH', body, headers })
  },
  delete<T>(path: string, headers?: Record<string, string>): Promise<ApiResponse<T>> {
    return request<T>(path, { method: 'DELETE', headers })
  },
}
