/** 认证状态（Pinia）：token、user、role、logout。 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import * as authApi from '@/api/auth'
import { getStoredToken, setUnauthorizedHandler, storeToken } from '@/api/client'
import type { LoginData, MeView } from '@/types/api'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(getStoredToken())
  const user = ref<MeView | null>(null)
  const ready = ref(false) // 是否已完成登录态恢复

  function applyLogin(data: LoginData): void {
    token.value = data.access_token
    storeToken(data.access_token)
    localStorage.setItem('kp_role', data.user.role)
    user.value = {
      id: data.user.id,
      username: data.user.username,
      display_name: data.user.display_name,
      role: data.user.role,
      status: data.user.status,
      created_at: null,
      last_login_at: null,
    }
  }

  async function login(username: string, password: string): Promise<void> {
    const resp = await authApi.login({ username, password })
    applyLogin(resp.data)
  }

  async function restore(): Promise<void> {
    // 应用启动/刷新后：有 token 则拉取当前用户，失败视为未登录
    if (!token.value) {
      ready.value = true
      return
    }
    try {
      const resp = await authApi.fetchMe()
      user.value = resp.data
      localStorage.setItem('kp_role', resp.data.role)
    } catch {
      token.value = null
      storeToken(null)
      localStorage.removeItem('kp_role')
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
    token.value = null
    storeToken(null)
    localStorage.removeItem('kp_role')
    user.value = null
  }

  const isAdmin = () => user.value?.role === 'admin'

  // 全局 401 处理：令牌失效时清空登录态
  setUnauthorizedHandler(() => {
    token.value = null
    storeToken(null)
    localStorage.removeItem('kp_role')
    user.value = null
  })

  return { token, user, ready, login, restore, logout, isAdmin }
})
