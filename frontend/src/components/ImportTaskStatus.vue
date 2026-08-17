<script setup lang="ts">
import { computed } from 'vue'
import { ElIcon } from 'element-plus'
import { CircleCheckFilled, Loading, WarningFilled } from '@element-plus/icons-vue'

import type { IntegrationTaskView } from '@/types/api'

const props = defineProps<{
  task: IntegrationTaskView
}>()

const statusText = computed(() => {
  const map: Record<string, string> = {
    pending: '等待中',
    running: '处理中',
    succeeded: '成功',
    failed: '失败',
    cancelled: '已取消',
  }
  return map[props.task.status] ?? props.task.status
})

const operationText = computed(() => {
  const map: Record<string, string> = {
    document_import: '导入',
    document_rebuild: '重建索引',
    document_replace: '替换',
  }
  return map[props.task.operation] ?? props.task.operation
})
</script>

<template>
  <div class="import-task-status">
    <div class="task-head">
      <span class="task-op">{{ operationText }}</span>
      <el-tag
        :type="task.status === 'succeeded' ? 'success' : task.status === 'failed' ? 'danger' : 'primary'"
        size="small"
      >
        {{ statusText }}
      </el-tag>
    </div>

    <div
      v-if="task.running_nodes.length"
      class="task-nodes"
    >
      <el-icon class="node-icon running">
        <Loading />
      </el-icon>
      <span
        v-for="node in task.running_nodes"
        :key="String(node.name ?? node)"
        class="node-name"
      >
        {{ String(node.name ?? node) }}
      </span>
    </div>
    <div
      v-if="task.done_nodes.length"
      class="task-nodes"
    >
      <el-icon class="node-icon done">
        <CircleCheckFilled />
      </el-icon>
      <span
        v-for="node in task.done_nodes"
        :key="String(node.name ?? node)"
        class="node-name"
      >
        {{ String(node.name ?? node) }}
      </span>
    </div>
    <div
      v-if="task.failed_node"
      class="task-failed"
    >
      <el-icon class="node-icon failed">
        <WarningFilled />
      </el-icon>
      <span>失败节点：{{ task.failed_node }}</span>
    </div>
    <div
      v-if="task.error_message"
      class="task-error"
    >
      {{ task.error_message }}
    </div>
  </div>
</template>

<style scoped>
.import-task-status {
  border: 1px solid var(--kp-border);
  border-radius: 6px;
  padding: 10px 12px;
  background: var(--kp-surface);
}

.task-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}

.task-op {
  font-weight: 600;
  font-size: 13px;
}

.task-nodes {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--kp-text-secondary);
  margin-top: 4px;
}

.node-icon {
  font-size: 14px;
}

.node-icon.running {
  color: var(--kp-primary);
}

.node-icon.done {
  color: var(--kp-success);
}

.node-icon.failed {
  color: var(--kp-danger);
}

.node-name {
  background: var(--kp-primary-light);
  border-radius: 4px;
  padding: 1px 6px;
}

.task-failed {
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--kp-danger);
}

.task-error {
  margin-top: 4px;
  font-size: 12px;
  color: var(--kp-danger);
  word-break: break-all;
}
</style>
