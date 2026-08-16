<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const form = reactive({
  username: '',
  password: '',
})

const loading = ref(false)
const errorMessage = ref('')

async function handleSubmit(): Promise<void> {
  // loading guard：提交中不重复提交（防连点/重复 Enter 产生多个 auth_sessions）
  if (loading.value) return
  if (!form.username.trim() || !form.password) {
    errorMessage.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  errorMessage.value = ''
  try {
    await auth.login(form.username.trim(), form.password)
    const redirect = (route.query.redirect as string | undefined) ?? '/'
    await router.replace(redirect)
  } catch (err) {
    errorMessage.value = err instanceof ApiError ? err.message : '登录失败，请稍后重试'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="brand">
        <div class="brand-mark">
          K
        </div>
        <h1 class="brand-title">
          券商财富业务知识管理平台
        </h1>
        <p class="brand-sub">
          知识管理 · 内部问答 · 运营分析
        </p>
      </div>

      <!-- 只保留一条提交链：form submit + button native-type=submit -->
      <el-form
        label-position="top"
        @submit.prevent="handleSubmit"
      >
        <el-form-item label="用户名">
          <el-input
            v-model="form.username"
            placeholder="请输入用户名"
            autocomplete="username"
            size="large"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            autocomplete="current-password"
            show-password
            size="large"
          />
        </el-form-item>

        <el-alert
          v-if="errorMessage"
          :title="errorMessage"
          type="error"
          :closable="false"
          show-icon
          class="login-error"
        />

        <el-button
          type="primary"
          size="large"
          class="login-button"
          :loading="loading"
          native-type="submit"
        >
          登 录
        </el-button>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--kp-bg);
}

.login-card {
  width: 400px;
  background: var(--kp-surface);
  border: 1px solid var(--kp-border);
  border-radius: 8px;
  padding: 40px 36px 32px;
  box-shadow: 0 2px 12px rgba(31, 45, 61, 0.06);
}

.brand {
  margin-bottom: 28px;
  text-align: center;
}

.brand-mark {
  width: 48px;
  height: 48px;
  margin: 0 auto 12px;
  border-radius: 8px;
  background: var(--kp-primary);
  color: #fff;
  font-size: 24px;
  font-weight: 600;
  line-height: 48px;
  text-align: center;
}

.brand-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: var(--kp-text);
}

.brand-sub {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--kp-text-secondary);
}

.login-error {
  margin-bottom: 16px;
}

.login-button {
  width: 100%;
}
</style>
