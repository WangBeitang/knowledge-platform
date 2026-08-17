/** 与后端 DTO 对齐的类型定义（snake_case 单一命名事实）。 */

export interface ApiErrorBody {
  code: string
  message: string
  retryable: boolean
  details?: Record<string, unknown>
}

export interface ApiResponse<T> {
  request_id: string
  data: T
}

export interface ApiErrorResponse {
  request_id: string
  error: ApiErrorBody
}

export interface PageData<T> {
  items: T[]
  page: number
  page_size: number
  total: number
}

export interface UserView {
  id: string
  username: string
  display_name: string
  role: 'admin' | 'employee'
  status: 'active' | 'disabled'
  last_login_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface LoginUser {
  id: string
  username: string
  display_name: string
  role: 'admin' | 'employee'
  status: 'active' | 'disabled'
}

export interface LoginData {
  access_token: string
  token_type: string
  expires_in: number
  user: LoginUser
}

export interface MeView {
  id: string
  username: string
  display_name: string
  role: 'admin' | 'employee'
  status: 'active' | 'disabled'
  created_at: string | null
  last_login_at: string | null
}

export interface RagDatasetStatusItem {
  scope: string
  dataset_id: string
  status: string
  member_status: string
  document_count: number | null
  message: string
}

export interface RagStatusData {
  import_base_url_configured: boolean
  datasets: RagDatasetStatusItem[]
  overall: string
}

export interface BootstrapData {
  verify_only: boolean
  datasets: RagDatasetStatusItem[]
  overall: string
}

// ========== Stage 3：知识管理 ==========

export type KnowledgeScope = 'external_public' | 'internal_shared' | 'admin_private'

export type PlatformStatus = 'importing' | 'active' | 'import_failed' | 'replaced' | 'deleted'

export interface ManagedDocumentView {
  id: string
  rag_document_id: string
  rag_dataset_id: string
  knowledge_scope: KnowledgeScope
  file_name: string
  source_kind: string
  index_version: number
  rag_status: string
  rag_parse_status: string | null
  rag_index_status: string | null
  platform_status: PlatformStatus
  chunk_count: number
  latest_rag_task_id: string | null
  error_code: string | null
  error_message: string | null
  created_at: string | null
  updated_at: string | null
}

export interface ImportErrorBody {
  code: string
  message: string
  retryable: boolean
}

export interface DocumentImportItem {
  file_name: string
  document_id: string | null
  task_id: string | null
  status: 'pending' | 'rejected'
  error: ImportErrorBody | null
}

export interface DocumentImportData {
  knowledge_scope: string
  submitted_count: number
  rejected_count: number
  items: DocumentImportItem[]
}

export interface IntegrationTaskView {
  id: string
  operation: string
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  document_id: string | null
  rag_status: string | null
  done_nodes: Record<string, unknown>[]
  running_nodes: Record<string, unknown>[]
  failed_node: string | null
  error_code: string | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  updated_at: string | null
}

export interface RebuildData {
  task_id: string
  document_id: string
  operation: string
  status: string
}

export interface ReplaceData {
  task_id: string
  new_document_id: string
  replacement_id: string
  status: string
}

export interface DeleteData {
  id: string
  platform_status: string
}

export interface ChunkView {
  chunk_id: string
  document_id: string
  index_version: number
  position: number
  text: string
  enabled: boolean
  disabled_reason_code: string | null
  disabled_reason_text: string | null
  metadata: Record<string, unknown>
}

export interface ChunkListData {
  items: ChunkView[]
  total: number
  page: number
  page_size: number
}

export interface ChunkSetEnabledData {
  document_id: string
  chunk_id: string
  index_version: number
  enabled: boolean
}

// ---- Stage 4 内部问答 ----

export interface CitationView {
  document_id: string | null
  chunk_id: string | null
  document_name: string | null
  content_preview: string | null
  score: number | null
  source_url: string | null
  index_version: number | null
  raw: Record<string, unknown>
}

export interface ChatSessionView {
  id: string
  channel: string
  user_id: string | null
  title: string
  status: 'active' | 'archived' | 'deleted'
  last_message_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface ChatMessageView {
  id: string
  session_id: string
  turn_id: string
  seq_no: number
  role: 'user' | 'assistant'
  content: string
  status: 'pending' | 'streaming' | 'completed' | 'failed'
  answer_source: 'faq_cache' | 'rag' | 'none' | null
  rag_trace_id: string | null
  terminal_reason_code: string | null
  citations: CitationView[]
  error_code: string | null
  created_at: string | null
  completed_at: string | null
}

export interface ChatSessionDetailData {
  session: ChatSessionView
  messages: ChatMessageView[]
}

export interface SseProgressData {
  request_id: string
  turn_id: string
  stage: 'faq_lookup' | 'rag_submit' | 'rag_progress' | 'finalizing'
  message: string
}

export interface SseDeltaData {
  request_id: string
  turn_id: string
  text: string
}

export interface SseReadyData {
  request_id: string
  turn_id: string
  session_id: string
}

export interface SseFinalData {
  request_id: string
  turn_id: string
  answer: string
  answer_source: 'faq_cache' | 'rag'
  trace_id: string | null
  citations: CitationView[]
  terminal_reason_code: string | null
}

export interface SseErrorData {
  request_id: string
  turn_id: string
  code: string
  message: string
  retryable: boolean
}

// ========== Stage 5：FAQ 闭环 ==========

export type FaqCandidateStatus = 'pending_review' | 'published' | 'rejected'

export type FaqStatus = 'published' | 'unpublished'

export type FaqSyncStatus = 'pending' | 'syncing' | 'succeeded' | 'failed'

export interface FaqCandidateView {
  id: string
  knowledge_scope: KnowledgeScope
  normalized_question: string
  normalized_question_hash: string
  sample_questions: string[]
  ask_count: number
  suggested_answer: string | null
  status: FaqCandidateStatus
  published_faq_id: string | null
  generated_at: string | null
  reviewed_by_user_id: string | null
  reviewed_at: string | null
}

export interface FaqView {
  id: string
  knowledge_scope: KnowledgeScope
  question: string
  normalized_question: string
  normalized_question_hash: string
  answer: string
  status: FaqStatus
  source_candidate_id: string | null
  hit_count: number
  rag_sync_status: FaqSyncStatus
  rag_sync_error: string | null
  created_by_user_id: string
  reviewed_by_user_id: string
  published_at: string | null
  updated_at: string | null
  unpublished_at: string | null
}

export interface FaqSyncRunView {
  id: string
  knowledge_scope: KnowledgeScope
  content_hash: string
  generated_file_name: string
  status: FaqSyncStatus
  rag_task_id: string | null
  rag_document_id: string | null
  previous_rag_document_id: string | null
  error_code: string | null
  error_message: string | null
  requested_by_user_id: string
  created_at: string | null
  finished_at: string | null
}

export interface FaqAnalyzeData {
  created: number
  updated: number
  skipped_published: number
}

// ========== Stage 5 Batch 2：知识缺口 ==========

export type GapStatus = 'pending_review' | 'ignored' | 'resolved'

export type GapReasonCode = 'no_citation' | 'insufficient_evidence'

export interface GapView {
  id: string
  knowledge_scope: KnowledgeScope
  normalized_question: string
  normalized_question_hash: string
  sample_questions: string[]
  ask_count: number
  reason_code: GapReasonCode
  status: GapStatus
  resolution_note: string | null
  resolved_document_id: string | null
  reviewed_by_user_id: string | null
  created_at: string | null
  last_seen_at: string | null
  reviewed_at: string | null
}

export interface GapAnalyzeData {
  created: number
  updated: number
}

// ========== Stage 5 Batch 3：运营看板 + 审计查询 ==========

export interface DashboardSummary {
  pv_count: number
  uv_count: number
  question_count: number
  success_rate: number | null
  avg_latency_ms: number | null
  token_input_total: number | null
  token_output_total: number | null
  token_total: number | null
  token_coverage_rate: number | null
}

export interface DashboardTrendItem {
  bucket: string
  pv_count: number
  uv_count: number
  question_count: number
  success_rate: number | null
  avg_latency_ms: number | null
  token_total: number | null
  token_coverage_rate: number | null
}

export interface DashboardTrendsData {
  granularity: 'day' | 'hour'
  items: DashboardTrendItem[]
}

export interface TopQuestionItem {
  normalized_question: string
  sample_question: string | null
  ask_count: number
}

export interface TopDocumentItem {
  document_id: string
  file_name: string | null
  citation_count: number
}

export type AuditResult = 'succeeded' | 'failed'

export interface AuditLogView {
  id: string
  request_id: string
  operator_user_id: string
  operator_username: string | null
  action: string
  resource_type: string
  resource_id: string | null
  result: AuditResult
  error_code: string | null
  client_ip: string | null
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  created_at: string | null
}
