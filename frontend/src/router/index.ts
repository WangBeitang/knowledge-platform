/** 路由：登录页 + 平台布局；守卫以 Pinia auth store 为登录态事实来源。

守卫规则：
1. 若 auth 尚未完成 restore，先 await auth.restore()（避免旧 token/旧 role 竞态）；
2. 无 token 访问保护页 → /login；
3. admin 路由判断用 auth.user.role，不再读取独立的 kp_role；
4. 前端隐藏/跳转只是 UX，后端鉴权仍是最终事实来源。
*/

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

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
        path: 'admin/knowledge-import',
        name: 'admin-knowledge-import',
        component: () => import('@/views/admin/KnowledgeImportView.vue'),
        meta: { title: '知识导入', adminOnly: true },
      },
      {
        path: 'admin/documents',
        name: 'admin-documents',
        component: () => import('@/views/admin/DocumentListView.vue'),
        meta: { title: '文档管理', adminOnly: true },
      },
      {
        path: 'admin/documents/:id',
        name: 'admin-document-detail',
        component: () => import('@/views/admin/DocumentDetailView.vue'),
        meta: { title: '文档详情', adminOnly: true },
      },
      {
        path: 'admin/faq-candidates',
        name: 'admin-faq-candidates',
        component: () => import('@/views/admin/FaqCandidatesView.vue'),
        meta: { title: 'FAQ 候选', adminOnly: true },
      },
      {
        path: 'admin/faqs',
        name: 'admin-faqs',
        component: () => import('@/views/admin/FaqLibraryView.vue'),
        meta: { title: 'FAQ 管理', adminOnly: true },
      },
      {
        path: 'admin/faq-sync-runs',
        name: 'admin-faq-sync-runs',
        component: () => import('@/views/admin/FaqSyncRunsView.vue'),
        meta: { title: 'FAQ 同步状态', adminOnly: true },
      },
      {
        path: 'admin/knowledge-gaps',
        name: 'admin-knowledge-gaps',
        component: () => import('@/views/admin/KnowledgeGapsView.vue'),
        meta: { title: '知识缺口', adminOnly: true },
      },
      {
        path: 'home',
        name: 'home',
        component: () => import('@/views/employee/HomeView.vue'),
        meta: { title: '工作台' },
      },
      {
        path: 'chat',
        name: 'chat',
        component: () => import('@/views/employee/ChatView.vue'),
        meta: { title: '内部问答' },
      },
      {
        path: 'chat/history',
        name: 'chat-history',
        component: () => import('@/views/employee/SessionHistoryView.vue'),
        meta: { title: '历史记录' },
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

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  // 登录态尚未恢复：先 restore（/auth/me 失败会清登录态），再决定放行
  if (!auth.ready) {
    await auth.restore()
  }
  if (to.meta.public) {
    return auth.token ? { path: '/' } : true
  }
  if (!auth.token) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  // 员工访问管理路由：前端跳转非管理落地页（后端鉴权仍是事实来源）
  if (to.meta.adminOnly && auth.user?.role !== 'admin') {
    return { path: '/home' }
  }
  return true
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? '券商财富业务知识管理平台'
  document.title = title
})

export default router
