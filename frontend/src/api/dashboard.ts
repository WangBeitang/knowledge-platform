/** 运营看板 API 封装（仅管理员）（《API 接口设计》§13.2）。 */

import { http } from '@/api/client'
import type {
  ApiResponse,
  DashboardSummary,
  DashboardTrendsData,
  TopDocumentItem,
  TopQuestionItem,
} from '@/types/api'

export interface DashboardQueryParams {
  date_from?: string
  date_to?: string
  channel?: string
}

function buildQuery(params: object): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') {
      query.set(key, String(value))
    }
  }
  const s = query.toString()
  return s ? `?${s}` : ''
}

export function getDashboardSummary(
  params: DashboardQueryParams = {},
): Promise<ApiResponse<DashboardSummary>> {
  return http.get<DashboardSummary>(`/admin/dashboard/summary${buildQuery(params)}`)
}

export function getDashboardTrends(
  params: DashboardQueryParams & { granularity?: 'day' | 'hour' } = {},
): Promise<ApiResponse<DashboardTrendsData>> {
  return http.get<DashboardTrendsData>(`/admin/dashboard/trends${buildQuery(params)}`)
}

export function getTopQuestions(
  params: DashboardQueryParams & { limit?: number } = {},
): Promise<ApiResponse<TopQuestionItem[]>> {
  return http.get<TopQuestionItem[]>(`/admin/dashboard/top-questions${buildQuery(params)}`)
}

export function getTopDocuments(
  params: DashboardQueryParams & { limit?: number } = {},
): Promise<ApiResponse<TopDocumentItem[]>> {
  return http.get<TopDocumentItem[]>(`/admin/dashboard/top-documents${buildQuery(params)}`)
}
