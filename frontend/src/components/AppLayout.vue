<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const isAdmin = computed(() => auth.user?.role === 'admin')
const displayName = computed(() => auth.user?.display_name || auth.user?.username || '--')

const adminMenu = [
  { path: '/admin/users', label: '员工账号', icon: 'User' },
  { path: '/admin/integration', label: '系统集成', icon: 'Connection' },
]

async function handleLogout(): Promise<void> {
  await auth.logout()
  await router.replace('/login')
}
</script>

<template>
  <el-container class="layout">
    <el-aside
      width="208px"
      class="aside"
    >
      <div class="logo-area">
        <div class="logo-mark">
          K
        </div>
        <div class="logo-text">
          <div class="logo-title">
            知识管理平台
          </div>
          <div class="logo-sub">
            券商财富业务
          </div>
        </div>
      </div>
      <el-menu
        :default-active="route.path"
        class="nav-menu"
        router
        background-color="transparent"
      >
        <template v-if="isAdmin">
          <el-menu-item
            v-for="item in adminMenu"
            :key="item.path"
            :index="item.path"
          >
            <el-icon><component :is="item.icon" /></el-icon>
            <span>{{ item.label }}</span>
          </el-menu-item>
        </template>
        <el-menu-item index="/home">
          <el-icon><HomeFilled /></el-icon>
          <span>工作台</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container class="main-wrap">
      <el-header class="header">
        <div class="header-title">
          {{ route.meta.title ?? '券商财富业务知识管理平台' }}
        </div>
        <div class="header-right">
          <el-tag
            size="small"
            :type="isAdmin ? 'primary' : 'info'"
            effect="plain"
          >
            {{ isAdmin ? '管理员' : '员工' }}
          </el-tag>
          <span class="user-name">{{ displayName }}</span>
          <el-button
            link
            type="primary"
            @click="handleLogout"
          >
            退出登录
          </el-button>
        </div>
      </el-header>

      <el-main class="content">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100%;
}

.aside {
  background: #ffffff;
  border-right: 1px solid var(--kp-border);
  display: flex;
  flex-direction: column;
}

.logo-area {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px;
  border-bottom: 1px solid var(--kp-border);
}

.logo-mark {
  width: 34px;
  height: 34px;
  border-radius: 6px;
  background: var(--kp-primary);
  color: #fff;
  font-size: 17px;
  font-weight: 600;
  line-height: 34px;
  text-align: center;
  flex-shrink: 0;
}

.logo-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--kp-text);
  line-height: 1.3;
}

.logo-sub {
  font-size: 11px;
  color: var(--kp-text-secondary);
}

.nav-menu {
  flex: 1;
  border-right: none;
  padding-top: 8px;
}

.main-wrap {
  min-width: 0;
}

.header {
  height: 56px;
  background: #ffffff;
  border-bottom: 1px solid var(--kp-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--kp-text);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.user-name {
  font-size: 13px;
  color: var(--kp-text-secondary);
}

.content {
  padding: 20px;
  overflow: auto;
}
</style>
