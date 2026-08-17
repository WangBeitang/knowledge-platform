/** 知识缺口管理 API 封装（仅管理员）（《API 接口设计》§13.1）。 */

import { http } from '@/api/client'
import type {
  ApiResponse,
  GapAnalyzeData,
  GapView,
  PageData,
} from '@/types/api'

export interface GapListParams {
  page?: number
  page_size?: number
  knowledge_scope?: string
  status?: string
  sort_by?: string
  sort_order?: 'asc' | 'desc'
}

export interface GapResolveBody {
  resolution_note?: string
  resolved_document_id?: string
}

export function analyzeKnowledgeGaps(): Promise<ApiResponse<GapAnalyzeData>> {
  return http.post<GapAnalyzeData>('/admin/knowledge-gaps/analyze', {})
}

export function listKnowledgeGaps(
  params: GapListParams = {},
): Promise<ApiResponse<PageData<GapView>>> {
  const query = new URLSearchParams()
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  if (params.knowledge_scope) query.set('knowledge_scope', params.knowledge_scope)
  if (params.status) query.set('status', params.status)
  if (params.sort_by) query.set('sort_by', params.sort_by)
  if (params.sort_order) query.set('sort_order', params.sort_order)
  return http.get<PageData<GapView>>(`/admin/knowledge-gaps?${query.toString()}`)
}

export function ignoreKnowledgeGap(gapId: string): Promise<ApiResponse<GapView>> {
  return http.post<GapView>(`/admin/knowledge-gaps/${gapId}/ignore`)
}

export function resolveKnowledgeGap(
  gapId: string,
  body: GapResolveBody = {},
): Promise<ApiResponse<GapView>> {
  return http.post<GapView>(`/admin/knowledge-gaps/${gapId}/resolve`, body)
}
