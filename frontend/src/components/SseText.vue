<script setup lang="ts">
/** 流式文本展示：streaming 时显示打字光标。 */

withDefaults(
  defineProps<{
    text: string
    streaming?: boolean
  }>(),
  { streaming: false },
)
</script>

<template>
  <div class="sse-text">
    <span class="sse-content">{{ text }}</span>
    <span
      v-if="streaming"
      class="sse-cursor"
      aria-hidden="true"
    />
  </div>
</template>

<style scoped>
.sse-text {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.7;
  font-size: 14px;
  color: var(--kp-text, #303133);
}

.sse-content {
  display: inline;
}

.sse-cursor {
  display: inline-block;
  width: 2px;
  height: 16px;
  margin-left: 2px;
  vertical-align: -2px;
  background: var(--kp-primary, #2f6bff);
  animation: sse-blink 1s steps(2, start) infinite;
}

@keyframes sse-blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0;
  }
}
</style>
