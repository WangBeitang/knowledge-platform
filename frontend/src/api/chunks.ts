/** Chunk 管理 API 封装（仅管理员）：分页列表/详情/启停。无正文编辑接口。 */

import { http } from '@/api/client'
import type {
  ApiResponse,
  ChunkListData,
  ChunkSetEnabledData,
  ChunkView,
} from '@/types/api'

export function listChunks(
  documentId: string,
  page: number,
  pageSize: number,
): Promise<ApiResponse<ChunkListData>> {
  return http.get<ChunkListData>(
    `/admin/documents/${documentId}/chunks?page=${page}&page_size=${pageSize}`,
  )
}

export function getChunk(documentId: string, chunkId: string): Promise<ApiResponse<ChunkView>> {
  return http.get<ChunkView>(`/admin/documents/${documentId}/chunks/${chunkId}`)
}

export interface ChunkSetEnabledParams {
  enabled: boolean
  reason_code: string
  reason_text: string
  expected_index_version: number
}

export function setChunkEnabled(
  documentId: string,
  chunkId: string,
  params: ChunkSetEnabledParams,
): Promise<ApiResponse<ChunkSetEnabledData>> {
  return http.patch<ChunkSetEnabledData>(
    `/admin/documents/${documentId}/chunks/${chunkId}/enabled`,
    params,
  )
}
