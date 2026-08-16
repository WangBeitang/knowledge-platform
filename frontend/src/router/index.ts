/** 路由：登录页 + 平台布局；未登录 → 登录页；员工隐藏管理菜单（前端隐藏，后端仍强制校验）。 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { getStoredToken } from '@/api/client'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: () => import('@/components/AppLayout.vue'),
    children: [
      {
        path: '',
        redirect: '/admin/users',
      },
      {
        path: 'admin/users',
        name: 'admin-users',
        component: () => import('@/views/admin/UserManageView.vue'),
        meta: { title: '员工账号', adminOnly: true },
      },
      {
        path: 'admin/integration',
        name: 'admin-integration',
        component: () => import('@/views/admin/IntegrationView.vue'),
        meta: { title: '系统集成', adminOnly: true },
      },
      {
        path: 'home',
        name: 'home',
        component: () => import('@/views/employee/HomeView.vue'),
        meta: { title: '工作台' },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const token = getStoredToken()
  if (to.meta.public) {
    return token ? { path: '/' } : true
  }
  if (!token) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  // 员工访问管理路由：前端重定向（后端鉴权仍是事实来源）
  if (to.meta.adminOnly) {
    const role = localStorage.getItem('kp_role')
    if (role !== 'admin') {
      return { path: '/home' }
    }
  }
  return true
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? '券商财富业务知识管理平台'
  document.title = title
})

export default router
