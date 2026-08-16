<script setup lang="ts">
// 员工工作台：阶段 4 内部问答尚未实现，此处仅为登录后的落地页说明，不做假功能。
</script>

<template>
  <div class="home-view">
    <el-card shadow="never">
      <template #header>
        <span class="card-title">工作台</span>
      </template>
      <el-empty description="内部问答与历史会话功能将在后续阶段开放">
        <template #image>
          <div class="empty-mark">
            ?
          </div>
        </template>
      </el-empty>
    </el-card>
  </div>
</template>

<style scoped>
.card-title {
  font-size: 14px;
  font-weight: 600;
}

.empty-mark {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  background: var(--kp-primary-light);
  color: var(--kp-primary);
  font-size: 22px;
  font-weight: 600;
  line-height: 48px;
  text-align: center;
}
</style>
