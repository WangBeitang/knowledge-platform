/** 审计日志查询 API 封装（仅管理员）（《API 接口设计》§13.3）。 */

import { http } from '@/api/client'
import type { ApiResponse, AuditLogView, PageData } from '@/types/api'

export interface AuditListParams {
  page?: number
  page_size?: number
  action?: string
  operator_user_id?: string
  resource_type?: string
  result?: 'succeeded' | 'failed'
  date_from?: string
  date_to?: string
  sort_by?: string
  sort_order?: 'asc' | 'desc'
}

export function listAuditLogs(
  params: AuditListParams = {},
): Promise<ApiResponse<PageData<AuditLogView>>> {
  const query = new URLSearchParams()
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  if (params.action) query.set('action', params.action)
  if (params.operator_user_id) query.set('operator_user_id', params.operator_user_id)
  if (params.resource_type) query.set('resource_type', params.resource_type)
  if (params.result) query.set('result', params.result)
  if (params.date_from) query.set('date_from', params.date_from)
  if (params.date_to) query.set('date_to', params.date_to)
  if (params.sort_by) query.set('sort_by', params.sort_by)
  if (params.sort_order) query.set('sort_order', params.sort_order)
  return http.get<PageData<AuditLogView>>(`/admin/audit-logs?${query.toString()}`)
}
