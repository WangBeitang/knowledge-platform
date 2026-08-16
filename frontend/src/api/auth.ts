/** 认证 API 封装。 */

import { http } from '@/api/client'
import type { ApiResponse, LoginData, MeView } from '@/types/api'

export interface LoginParams {
  username: string
  password: string
}

export function login(params: LoginParams): Promise<ApiResponse<LoginData>> {
  return http.post<LoginData>('/auth/login', params)
}

export function logout(): Promise<ApiResponse<{ ok: boolean }>> {
  return http.post<{ ok: boolean }>('/auth/logout')
}

export function fetchMe(): Promise<ApiResponse<MeView>> {
  return http.get<MeView>('/auth/me')
}
