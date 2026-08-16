<script setup lang="ts">
// 阶段 1 骨架冒烟：仅用于验证前后端连通（健康检查），非正式页面。
// 正式页面（登录/看板/问答等）须通过前端设计门禁后按阶段 2+ 开发。
import { onMounted, ref } from 'vue'

interface ReadyData {
  status: string
  components: Record<string, { status: string }>
}

const ready = ref<ReadyData | null>(null)
const error = ref<string>('')

async function checkHealth(): Promise<void> {
  try {
    const resp = await fetch('/api/v1/health/ready')
    const body = await resp.json()
    ready.value = body.data
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(checkHealth)
</script>

<template>
  <main style="max-width: 720px; margin: 80px auto; font-family: system-ui, sans-serif">
    <h1>券商财富业务知识管理平台</h1>
    <p>工程基线已就绪（阶段 1）。正式页面开发前需先确认视觉风格与原型。</p>
    <section
      v-if="error"
      style="color: #c0392b"
    >
      健康检查失败：{{ error }}
    </section>
    <section v-else-if="ready">
      <p>整体状态：{{ ready.status }}</p>
      <ul>
        <li
          v-for="(comp, name) in ready.components"
          :key="name"
        >
          {{ name }}：{{ comp.status }}
        </li>
      </ul>
    </section>
    <section v-else>
      正在检查服务状态…
    </section>
  </main>
</template>
