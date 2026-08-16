import { createPinia } from 'pinia'
import { createApp } from 'vue'

import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import App from './App.vue'
import { setUnauthorizedHandler } from './api/client'
import router from './router'
import { useAuthStore } from './stores/auth'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')

// API 返回 401：清登录态后尽快回到登录页（不只删除 token 停留在当前管理页）
setUnauthorizedHandler(() => {
  const auth = useAuthStore()
  auth.clearSession()
  if (router.currentRoute.value.path !== '/login') {
    void router.replace('/login')
  }
})

// 恢复登录态（刷新后拉取当前用户；路由守卫会 await，这里提前触发避免白屏等待）
void useAuthStore().restore()
