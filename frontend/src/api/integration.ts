/** RAG 集成 API 封装（仅管理员）：三档 Dataset 状态与初始化。 */

import { http } from '@/api/client'
import type { ApiResponse, BootstrapData, RagStatusData } from '@/types/api'

export function fetchRagStatus(): Promise<ApiResponse<RagStatusData>> {
  return http.get<RagStatusData>('/admin/integration/rag/status')
}

export function bootstrapRag(verifyOnly: boolean): Promise<ApiResponse<BootstrapData>> {
  return http.post<BootstrapData>('/admin/integration/rag/bootstrap', { verify_only: verifyOnly })
}
