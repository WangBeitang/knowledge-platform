<script setup lang="ts">
/** 引用卡片：只展示真实存在字段（content_preview null 不展示空框）。 */

import { computed } from 'vue'

import type { CitationView } from '@/types/api'

const props = defineProps<{
  citation: CitationView
}>()

const hasName = computed(() => !!props.citation.document_name)
const hasScore = computed(() => props.citation.score !== null && props.citation.score !== undefined)
const hasSourceUrl = computed(() => !!props.citation.source_url)
const hasLocalIds = computed(() => !!props.citation.document_id || !!props.citation.chunk_id)

const scoreText = computed(() => {
  if (!hasScore.value) return null
  const value = Number(props.citation.score)
  return Number.isFinite(value) ? value.toFixed(2) : null
})
</script>

<template>
  <div class="citation-card">
    <div class="citation-head">
      <span class="citation-mark">引用</span>
      <span
        v-if="hasName"
        class="citation-name"
        :title="citation.document_name ?? undefined"
      >
        {{ citation.document_name }}
      </span>
      <span
        v-if="hasScore"
        class="citation-score"
      >
        {{ scoreText }}
      </span>
    </div>
    <div
      v-if="hasSourceUrl"
      class="citation-link"
    >
      <el-link
        :href="citation.source_url ?? undefined"
        type="primary"
        target="_blank"
        rel="noopener noreferrer"
      >
        {{ citation.source_url }}
      </el-link>
    </div>
    <div
      v-if="hasLocalIds"
      class="citation-ids"
    >
      <template v-if="citation.document_id">
        <span>文档 {{ citation.document_id }}</span>
      </template>
      <template v-if="citation.chunk_id">
        <span>分块 {{ citation.chunk_id }}</span>
      </template>
    </div>
  </div>
</template>

<style scoped>
.citation-card {
  border: 1px solid var(--el-border-color-light, #e4e7ed);
  border-radius: 6px;
  padding: 8px 12px;
  background: var(--el-fill-color-light, #f5f7fa);
  font-size: 12px;
  max-width: 100%;
}

.citation-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.citation-mark {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--kp-primary, #2f6bff);
  background: var(--kp-primary-light, #e8f0ff);
  border-radius: 3px;
  padding: 1px 6px;
  font-weight: 600;
}

.citation-name {
  color: var(--el-text-color-primary, #303133);
  font-weight: 500;
  word-break: break-all;
}

.citation-score {
  color: var(--el-text-color-secondary, #909399);
}

.citation-link {
  margin-top: 4px;
  overflow: hidden;
}

.citation-link :deep(.el-link) {
  font-size: 12px;
  word-break: break-all;
}

.citation-ids {
  display: flex;
  gap: 12px;
  margin-top: 4px;
  color: var(--el-text-color-secondary, #909399);
  word-break: break-all;
}
</style>
