/** 用户管理 API 封装（仅管理员）。 */

import { http } from '@/api/client'
import type { ApiResponse, PageData, UserView } from '@/types/api'

export interface UserCreateParams {
  username: string
  display_name: string
  role: 'admin' | 'employee'
  initial_password: string
}

export interface UserUpdateParams {
  display_name?: string
  role?: 'admin' | 'employee'
  status?: 'active' | 'disabled'
}

export interface UserListParams {
  page?: number
  page_size?: number
  sort_by?: string
  sort_order?: 'asc' | 'desc'
}

export function listUsers(params: UserListParams = {}): Promise<ApiResponse<PageData<UserView>>> {
  const query = new URLSearchParams()
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  if (params.sort_by) query.set('sort_by', params.sort_by)
  if (params.sort_order) query.set('sort_order', params.sort_order)
  return http.get<PageData<UserView>>(`/admin/users?${query.toString()}`)
}

export function createUser(params: UserCreateParams): Promise<ApiResponse<UserView>> {
  return http.post<UserView>('/admin/users', params)
}

export function updateUser(userId: string, params: UserUpdateParams): Promise<ApiResponse<UserView>> {
  return http.patch<UserView>(`/admin/users/${userId}`, params)
}

export function resetUserPassword(userId: string, newPassword: string): Promise<ApiResponse<{ id: string; message: string }>> {
  return http.post<{ id: string; message: string }>(`/admin/users/${userId}/reset-password`, {
    new_password: newPassword,
  })
}
