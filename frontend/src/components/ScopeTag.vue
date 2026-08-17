<script setup lang="ts">
import { computed } from 'vue'

import type { KnowledgeScope } from '@/types/api'

const props = defineProps<{
  scope: string
}>()

const label = computed(() => {
  const map: Record<string, string> = {
    external_public: '外部公开',
    internal_shared: '内部共享',
    admin_private: '管理员专属',
  }
  return map[props.scope] ?? props.scope
})

const type = computed(() => {
  const map: Record<string, 'primary' | 'info' | 'warning'> = {
    external_public: 'info',
    internal_shared: 'primary',
    admin_private: 'warning',
  }
  return map[props.scope as KnowledgeScope] ?? 'info'
})
</script>

<template>
  <el-tag
    :type="type"
    effect="plain"
    size="small"
  >
    {{ label }}
  </el-tag>
</template>
