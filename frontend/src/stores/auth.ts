/** 认证状态（Pinia）：token、user、role、restore、logout。

登录态唯一事实来源：路由守卫 await `auth.restore()` 后以 `auth.user.role` 判断，
不再使用 localStorage 中的独立 `kp_role`。
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import * as authApi from '@/api/auth'
import { getStoredToken, setUnauthorizedHandler, storeToken } from '@/api/client'
import type { LoginData, MeView } from '@/types/api'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(getStoredToken())
  const user = ref<MeView | null>(null)
  const ready = ref(false) // restore 是否完成（无论成功失败）
  let restoring: Promise<void> | null = null

  function applyLogin(data: LoginData): void {
    token.value = data.access_token
    storeToken(data.access_token)
    user.value = {
      id: data.user.id,
      username: data.user.username,
      display_name: data.user.display_name,
      role: data.user.role,
      status: data.user.status,
      created_at: null,
      last_login_at: null,
    }
    ready.value = true
  }

  async function login(username: string, password: string): Promise<void> {
    const resp = await authApi.login({ username, password })
    applyLogin(resp.data)
  }

  function restore(): Promise<void> {
    // 幂等：并发调用只执行一次
    if (restoring) return restoring
    restoring = doRestore().finally(() => {
      restoring = null
    })
    return restoring
  }

  async function doRestore(): Promise<void> {
    // 应用启动/刷新后：有 token 则拉取当前用户，失败视为未登录
    if (!token.value) {
      ready.value = true
      return
    }
    try {
      const resp = await authApi.fetchMe()
      user.value = resp.data
    } catch {
      token.value = null
      storeToken(null)
      user.value = null
    } finally {
      ready.value = true
    }
  }

  async function logout(): Promise<void> {
    try {
      if (token.value) await authApi.logout()
    } catch {
      // 登出失败（如 token 已失效）也继续清空本地状态
    }
    clearSession()
  }

  function clearSession(): void {
    token.value = null
    storeToken(null)
    user.value = null
    ready.value = true
  }

  const isAdmin = () => user.value?.role === 'admin'

  // 全局 401：清空登录态（跳转登录页由 main.ts 注册的处理器完成）
  setUnauthorizedHandler(() => {
    token.value = null
    storeToken(null)
    user.value = null
  })

  return { token, user, ready, login, restore, logout, clearSession, isAdmin }
})
