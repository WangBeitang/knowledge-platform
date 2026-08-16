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
