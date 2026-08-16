/** 文档管理 API 封装（仅管理员）：导入/列表/详情/重建/替换/删除。 */

import { http } from '@/api/client'
import type {
  ApiResponse,
  DeleteData,
  DocumentImportData,
  DocumentListResponse,
  ManagedDocumentView,
  PageData,
  RebuildData,
  ReplaceData,
} from '@/types/api'

export interface DocumentListParams {
  page?: number
  page_size?: number
  knowledge_scope?: string
  platform_status?: string
  file_name?: string
  source_kind?: string
  sort_by?: string
  sort_order?: 'asc' | 'desc'
}

export function importDocuments(
  knowledgeScope: string,
  files: File[],
): Promise<ApiResponse<DocumentImportData>> {
  const form = new FormData()
  form.set('knowledge_scope', knowledgeScope)
  files.forEach((file) => form.append('files', file))
  return http.post<DocumentImportData>('/admin/documents/import', form, {
    'Content-Type': 'multipart/form-data',
  })
}

export function listDocuments(
  params: DocumentListParams = {},
): Promise<ApiResponse<PageData<ManagedDocumentView>>> {
  const query = new URLSearchParams()
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  if (params.knowledge_scope) query.set('knowledge_scope', params.knowledge_scope)
  if (params.platform_status) query.set('platform_status', params.platform_status)
  if (params.file_name) query.set('file_name', params.file_name)
  if (params.source_kind) query.set('source_kind', params.source_kind)
  if (params.sort_by) query.set('sort_by', params.sort_by)
  if (params.sort_order) query.set('sort_order', params.sort_order)
  return http.get<PageData<ManagedDocumentView>>(`/admin/documents?${query.toString()}`)
}

export function getDocument(documentId: string): Promise<ApiResponse<ManagedDocumentView>> {
  return http.get<ManagedDocumentView>(`/admin/documents/${documentId}`)
}

export function rebuildDocument(documentId: string): Promise<ApiResponse<RebuildData>> {
  return http.post<RebuildData>(`/admin/documents/${documentId}/rebuild`)
}

export function deleteDocument(documentId: string): Promise<ApiResponse<DeleteData>> {
  return http.delete<DeleteData>(`/admin/documents/${documentId}`)
}

export function replaceDocument(
  documentId: string,
  knowledgeScope: string,
  file: File,
): Promise<ApiResponse<ReplaceData>> {
  const form = new FormData()
  form.set('knowledge_scope', knowledgeScope)
  form.set('file', file)
  return http.post<ReplaceData>(`/admin/documents/${documentId}/replace`, form, {
    'Content-Type': 'multipart/form-data',
  })
}
