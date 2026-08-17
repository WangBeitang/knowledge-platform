/** FAQ 管理 API 封装（仅管理员）：候选、正式 FAQ、同步记录（《API 接口设计》§12）。 */

import { http } from '@/api/client'
import type {
  ApiResponse,
  FaqAnalyzeData,
  FaqCandidateView,
  FaqSyncRunView,
  FaqView,
  PageData,
} from '@/types/api'

export interface FaqListParams {
  page?: number
  page_size?: number
  knowledge_scope?: string
  status?: string
  sort_by?: string
  sort_order?: 'asc' | 'desc'
}

export interface FaqPublishBody {
  knowledge_scope: string
  question: string
  answer: string
}

export function analyzeFaqCandidates(): Promise<ApiResponse<FaqAnalyzeData>> {
  return http.post<FaqAnalyzeData>('/admin/faq-candidates/analyze', {})
}

export function listFaqCandidates(
  params: FaqListParams = {},
): Promise<ApiResponse<PageData<FaqCandidateView>>> {
  const query = new URLSearchParams()
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  if (params.knowledge_scope) query.set('knowledge_scope', params.knowledge_scope)
  if (params.status) query.set('status', params.status)
  if (params.sort_by) query.set('sort_by', params.sort_by)
  if (params.sort_order) query.set('sort_order', params.sort_order)
  return http.get<PageData<FaqCandidateView>>(`/admin/faq-candidates?${query.toString()}`)
}

export function rejectFaqCandidate(candidateId: string): Promise<ApiResponse<FaqCandidateView>> {
  return http.post<FaqCandidateView>(`/admin/faq-candidates/${candidateId}/reject`)
}

export function publishFaqCandidate(
  candidateId: string,
  body: FaqPublishBody,
): Promise<ApiResponse<FaqView>> {
  return http.post<FaqView>(`/admin/faq-candidates/${candidateId}/publish`, body)
}

export function listFaqs(
  params: FaqListParams = {},
): Promise<ApiResponse<PageData<FaqView>>> {
  const query = new URLSearchParams()
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  if (params.knowledge_scope) query.set('knowledge_scope', params.knowledge_scope)
  if (params.status) query.set('status', params.status)
  if (params.sort_by) query.set('sort_by', params.sort_by)
  if (params.sort_order) query.set('sort_order', params.sort_order)
  return http.get<PageData<FaqView>>(`/admin/faqs?${query.toString()}`)
}

export function createFaq(body: FaqPublishBody): Promise<ApiResponse<FaqView>> {
  return http.post<FaqView>('/admin/faqs', body)
}

export function updateFaq(faqId: string, body: { question: string; answer: string }): Promise<ApiResponse<FaqView>> {
  return http.patch<FaqView>(`/admin/faqs/${faqId}`, body)
}

export function unpublishFaq(faqId: string): Promise<ApiResponse<FaqView>> {
  return http.post<FaqView>(`/admin/faqs/${faqId}/unpublish`)
}

export function republishFaq(faqId: string): Promise<ApiResponse<FaqView>> {
  return http.post<FaqView>(`/admin/faqs/${faqId}/publish`)
}

export function retryFaqSync(faqId: string): Promise<ApiResponse<FaqSyncRunView>> {
  return http.post<FaqSyncRunView>(`/admin/faqs/${faqId}/sync:retry`)
}

export function listFaqSyncRuns(
  params: FaqListParams = {},
): Promise<ApiResponse<PageData<FaqSyncRunView>>> {
  const query = new URLSearchParams()
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  if (params.knowledge_scope) query.set('knowledge_scope', params.knowledge_scope)
  if (params.status) query.set('status', params.status)
  if (params.sort_by) query.set('sort_by', params.sort_by)
  if (params.sort_order) query.set('sort_order', params.sort_order)
  return http.get<PageData<FaqSyncRunView>>(`/admin/faq-sync-runs?${query.toString()}`)
}
