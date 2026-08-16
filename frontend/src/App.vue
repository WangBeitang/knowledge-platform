<script setup lang="ts">
// 平台根组件：仅承载路由出口与全局浅色蓝灰视觉基线。
</script>

<template>
  <div class="app-root">
    <router-view />
  </div>
</template>

<style>
:root {
  /* 低饱和蓝灰体系（G1 已确认：企业后台浅色、专业克制） */
  --kp-primary: #3d5a80;
  --kp-primary-light: #eef2f7;
  --kp-primary-hover: #33506f;
  --kp-bg: #f5f7fa;
  --kp-surface: #ffffff;
  --kp-border: #e2e8f0;
  --kp-text: #1f2d3d;
  --kp-text-secondary: #5c6b7a;
  --kp-danger: #c0504d;
  --kp-success: #2e7d5b;
  --el-color-primary: #3d5a80;
}

* {
  box-sizing: border-box;
}

html,
body,
#app {
  margin: 0;
  padding: 0;
  height: 100%;
  font-family:
    -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Microsoft YaHei', 'Helvetica Neue', Arial, sans-serif;
  background: var(--kp-bg);
  color: var(--kp-text);
}

.app-root {
  height: 100%;
}
</style>
