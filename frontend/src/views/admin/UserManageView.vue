<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError } from '@/api/client'
import * as usersApi from '@/api/users'
import type { UserView } from '@/types/api'
import { formatDateTime } from '@/utils/format'

const loading = ref(false)
const items = ref<UserView[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const errorMessage = ref('')

const createDialogVisible = ref(false)
const createSaving = ref(false)
const createForm = reactive({
  username: '',
  display_name: '',
  role: 'employee' as 'admin' | 'employee',
  initial_password: '',
})

const editDialogVisible = ref(false)
const editSaving = ref(false)
const editingId = ref('')
const editForm = reactive({
  display_name: '',
  role: 'employee' as 'admin' | 'employee',
  status: 'active' as 'active' | 'disabled',
})

async function fetchUsers(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    const resp = await usersApi.listUsers({ page: page.value, page_size: pageSize.value })
    items.value = resp.data.items
    total.value = resp.data.total
  } catch (err) {
    errorMessage.value = err instanceof ApiError ? err.message : '加载用户列表失败'
  } finally {
    loading.value = false
  }
}

function openCreate(): void {
  createForm.username = ''
  createForm.display_name = ''
  createForm.role = 'employee'
  createForm.initial_password = ''
  createDialogVisible.value = true
}

async function handleCreate(): Promise<void> {
  if (!createForm.username.trim() || !createForm.display_name.trim() || !createForm.initial_password) {
    ElMessage.warning('请完整填写用户名、展示名与初始密码')
    return
  }
  createSaving.value = true
  try {
    await usersApi.createUser({
      username: createForm.username.trim(),
      display_name: createForm.display_name.trim(),
      role: createForm.role,
      initial_password: createForm.initial_password,
    })
    ElMessage.success('用户创建成功')
    createDialogVisible.value = false
    await fetchUsers()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '创建失败')
  } finally {
    createSaving.value = false
  }
}

function openEdit(row: UserView): void {
  editingId.value = row.id
  editForm.display_name = row.display_name
  editForm.role = row.role
  editForm.status = row.status
  editDialogVisible.value = true
}

async function handleEdit(): Promise<void> {
  editSaving.value = true
  try {
    await usersApi.updateUser(editingId.value, {
      display_name: editForm.display_name,
      role: editForm.role,
      status: editForm.status,
    })
    ElMessage.success('用户信息已更新')
    editDialogVisible.value = false
    await fetchUsers()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '更新失败')
  } finally {
    editSaving.value = false
  }
}

async function handleToggleStatus(row: UserView): Promise<void> {
  const disabling = row.status === 'active'
  const action = disabling ? '停用' : '启用'
  try {
    await ElMessageBox.confirm(
      disabling
        ? `确定停用用户「${row.display_name}」吗？停用后该用户所有登录状态将立即失效。`
        : `确定启用用户「${row.display_name}」吗？`,
      `${action}用户`,
      { confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return // 用户取消
  }
  try {
    await usersApi.updateUser(row.id, { status: disabling ? 'disabled' : 'active' })
    ElMessage.success(`用户已${action}`)
    await fetchUsers()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : `${action}失败`)
  }
}

async function handleResetPassword(row: UserView): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt(
      `为用户「${row.display_name}」设置新密码。重置后其当前所有登录状态将立即失效。`,
      '重置密码',
      {
        confirmButtonText: '重置',
        cancelButtonText: '取消',
        inputType: 'password',
        inputPlaceholder: '请输入新密码',
        inputValidator: (v: string) => (v && v.length > 0 ? true : '密码不能为空'),
      },
    )
    await usersApi.resetUserPassword(row.id, value)
    ElMessage.success('密码已重置')
  } catch (err) {
    if (err instanceof ApiError) {
      ElMessage.error(err.message)
    }
    // 用户取消或校验失败不提示
  }
}

onMounted(fetchUsers)
</script>

<template>
  <div class="user-manage">
    <div class="toolbar">
      <el-button
        type="primary"
        @click="openCreate"
      >
        创建用户
      </el-button>
    </div>

    <el-alert
      v-if="errorMessage"
      :title="errorMessage"
      type="error"
      :closable="false"
      show-icon
      class="page-error"
    />

    <el-table
      v-loading="loading"
      :data="items"
      border
      stripe
    >
      <el-table-column
        prop="username"
        label="用户名"
        min-width="140"
      />
      <el-table-column
        prop="display_name"
        label="展示名"
        min-width="120"
      />
      <el-table-column
        label="角色"
        width="100"
      >
        <template #default="{ row }">
          <el-tag
            :type="row.role === 'admin' ? 'primary' : 'info'"
            effect="plain"
            size="small"
          >
            {{ row.role === 'admin' ? '管理员' : '员工' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        label="状态"
        width="100"
      >
        <template #default="{ row }">
          <el-tag
            :type="row.status === 'active' ? 'success' : 'danger'"
            effect="plain"
            size="small"
          >
            {{ row.status === 'active' ? '正常' : '已停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column
        label="最近登录"
        width="170"
      >
        <template #default="{ row }">
          {{ formatDateTime(row.last_login_at) }}
        </template>
      </el-table-column>
      <el-table-column
        label="创建时间"
        width="170"
      >
        <template #default="{ row }">
          {{ formatDateTime(row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column
        label="操作"
        width="220"
        fixed="right"
      >
        <template #default="{ row }">
          <el-button
            link
            type="primary"
            size="small"
            @click="openEdit(row)"
          >
            编辑
          </el-button>
          <el-button
            link
            :type="row.status === 'active' ? 'danger' : 'success'"
            size="small"
            @click="handleToggleStatus(row)"
          >
            {{ row.status === 'active' ? '停用' : '启用' }}
          </el-button>
          <el-button
            link
            type="warning"
            size="small"
            @click="handleResetPassword(row)"
          >
            重置密码
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pagination-bar">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        layout="total, prev, pager, next, sizes"
        :page-sizes="[10, 20, 50]"
        @current-change="fetchUsers"
        @size-change="fetchUsers"
      />
    </div>

    <!-- 创建用户 -->
    <el-dialog
      v-model="createDialogVisible"
      title="创建用户"
      width="480px"
      :close-on-click-modal="false"
    >
      <el-form label-position="top">
        <el-form-item
          label="用户名（登录名，统一转小写，全局唯一）"
          required
        >
          <el-input
            v-model="createForm.username"
            placeholder="如 zhangsan"
            maxlength="64"
          />
        </el-form-item>
        <el-form-item
          label="展示名"
          required
        >
          <el-input
            v-model="createForm.display_name"
            placeholder="如 张三"
            maxlength="100"
          />
        </el-form-item>
        <el-form-item
          label="角色"
          required
        >
          <el-radio-group v-model="createForm.role">
            <el-radio value="employee">
              员工
            </el-radio>
            <el-radio value="admin">
              管理员
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item
          label="初始密码"
          required
        >
          <el-input
            v-model="createForm.initial_password"
            type="password"
            show-password
            maxlength="256"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">
          取消
        </el-button>
        <el-button
          type="primary"
          :loading="createSaving"
          @click="handleCreate"
        >
          创建
        </el-button>
      </template>
    </el-dialog>

    <!-- 编辑用户 -->
    <el-dialog
      v-model="editDialogVisible"
      title="编辑用户"
      width="480px"
      :close-on-click-modal="false"
    >
      <el-form label-position="top">
        <el-form-item
          label="展示名"
          required
        >
          <el-input
            v-model="editForm.display_name"
            maxlength="100"
          />
        </el-form-item>
        <el-form-item
          label="角色"
          required
        >
          <el-radio-group v-model="editForm.role">
            <el-radio value="employee">
              员工
            </el-radio>
            <el-radio value="admin">
              管理员
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item
          label="状态"
          required
        >
          <el-radio-group v-model="editForm.status">
            <el-radio value="active">
              正常
            </el-radio>
            <el-radio value="disabled">
              停用
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="系统始终至少保留一个有效管理员；管理员不能停用自己。"
        />
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">
          取消
        </el-button>
        <el-button
          type="primary"
          :loading="editSaving"
          @click="handleEdit"
        >
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  margin-bottom: 16px;
}

.page-error {
  margin-bottom: 16px;
}

.pagination-bar {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
