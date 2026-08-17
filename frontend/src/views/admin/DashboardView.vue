<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'

import { ApiError } from '@/api/client'
import * as dashboardApi from '@/api/dashboard'
import type {
  DashboardSummary,
  DashboardTrendsData,
  TopDocumentItem,
  TopQuestionItem,
} from '@/types/api'

const loading = ref(false)
const dateRange = ref<[string, string] | null>(null)
const channel = ref('')
const granularity = ref<'day' | 'hour'>('day')

const summary = ref<DashboardSummary | null>(null)
const trends = ref<DashboardTrendsData | null>(null)
const topQuestions = ref<TopQuestionItem[]>([])
const topDocuments = ref<TopDocumentItem[]>([])

const chartEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

function filters(): dashboardApi.DashboardQueryParams {
  return {
    date_from: dateRange.value?.[0] ?? undefined,
    date_to: dateRange.value?.[1] ?? undefined,
    channel: channel.value || undefined,
  }
}

function formatPercent(value: number | null): string {
  if (value === null) return '暂无数据'
  return `${(value * 100).toFixed(1)}%`
}

function formatNumber(value: number | null): string {
  if (value === null) return '暂无数据'
  return value.toLocaleString('zh-CN')
}

function coverageText(value: number | null): string {
  if (value === null) return '暂无数据'
  return `${(value * 100).toFixed(1)}%`
}

function renderTrendChart(): void {
  if (!chartEl.value) return
  if (!chart) {
    chart = echarts.init(chartEl.value)
  }
  const items = trends.value?.items ?? []
  chart.setOption(
    {
      tooltip: { trigger: 'axis' },
      legend: { data: ['访问次数 (PV)', '问答量'], top: 0 },
      grid: { left: 40, right: 16, top: 32, bottom: 28 },
      xAxis: {
        type: 'category',
        data: items.map((i) => i.bucket),
        axisLabel: { fontSize: 11 },
      },
      yAxis: { type: 'value', minInterval: 1 },
      series: [
        {
          name: '访问次数 (PV)',
          type: 'line',
          smooth: true,
          data: items.map((i) => i.pv_count),
          itemStyle: { color: '#4e7fff' },
        },
        {
          name: '问答量',
          type: 'line',
          smooth: true,
          data: items.map((i) => i.question_count),
          itemStyle: { color: '#19be6b' },
        },
      ],
    },
    true,
  )
}

async function fetchAll(): Promise<void> {
  loading.value = true
  try {
    const f = filters()
    const [s, t, q, d] = await Promise.all([
      dashboardApi.getDashboardSummary(f),
      dashboardApi.getDashboardTrends({ ...f, granularity: granularity.value }),
      dashboardApi.getTopQuestions({ ...f, limit: 10 }),
      dashboardApi.getTopDocuments({ ...f, limit: 10 }),
    ])
    summary.value = s.data
    trends.value = t.data
    topQuestions.value = q.data
    topDocuments.value = d.data
    await nextTick()
    renderTrendChart()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载看板数据失败')
  } finally {
    loading.value = false
  }
}

function handleGranularityChange(): void {
  fetchAll()
}

function onResize(): void {  chart?.resize()
}

onMounted(() => {
  window.addEventListener('resize', onResize)
  fetchAll()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h2>运营看板</h2>
      <el-button
        type="primary"
        :loading="loading"
        @click="fetchAll"
      >
        刷新
      </el-button>
    </div>

    <el-card shadow="never">
      <div class="filters">
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          value-format="YYYY-MM-DD"
          style="width: 260px"
        />
        <el-select
          v-model="channel"
          placeholder="全部渠道"
          clearable
          style="width: 150px"
        >
          <el-option
            label="内部 Web"
            value="internal_web"
          />
          <el-option
            label="外部 API"
            value="external_api"
          />
        </el-select>
        <el-radio-group
          v-model="granularity"
          @change="handleGranularityChange"
        >
          <el-radio-button value="day">
            按日
          </el-radio-button>
          <el-radio-button value="hour">
            按小时
          </el-radio-button>
        </el-radio-group>
        <el-button
          type="primary"
          @click="fetchAll"
        >
          查询
        </el-button>
      </div>
    </el-card>

    <el-row
      :gutter="12"
      class="cards"
    >
      <el-col :span="4">
        <el-card shadow="never">
          <div class="metric-label">
            访问次数 (PV)
          </div>
          <div class="metric-value">
            {{ summary ? formatNumber(summary.pv_count) : '--' }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card shadow="never">
          <div class="metric-label">
            独立用户 (UV)
          </div>
          <div class="metric-value">
            {{ summary ? formatNumber(summary.uv_count) : '--' }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card shadow="never">
          <div class="metric-label">
            问答量
          </div>
          <div class="metric-value">
            {{ summary ? formatNumber(summary.question_count) : '--' }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card shadow="never">
          <div class="metric-label">
            成功率
          </div>
          <div class="metric-value">
            {{ summary ? formatPercent(summary.success_rate) : '--' }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card shadow="never">
          <div class="metric-label">
            平均延迟 (ms)
          </div>
          <div class="metric-value">
            {{ summary ? (summary.avg_latency_ms === null ? '暂无数据' : Math.round(summary.avg_latency_ms)) : '--' }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card shadow="never">
          <div class="metric-label">
            Token 总量
          </div>
          <div class="metric-value">
            {{ summary ? formatNumber(summary.token_total) : '--' }}
          </div>
          <div class="metric-sub">
            覆盖率 {{ summary ? coverageText(summary.token_coverage_rate) : '--' }}
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card
      shadow="never"
      class="chart-card"
    >
      <template #header>
        <span>访问趋势（UTC 时间）</span>
      </template>
      <div
        ref="chartEl"
        class="trend-chart"
      />
    </el-card>

    <el-row :gutter="12">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <span>高频问题 Top 10</span>
          </template>
          <el-table
            v-loading="loading"
            :data="topQuestions"
            border
            stripe
            size="small"
          >
            <el-table-column
              type="index"
              label="#"
              width="48"
            />
            <el-table-column
              prop="normalized_question"
              label="归一化问题"
              min-width="200"
              show-overflow-tooltip
            />
            <el-table-column
              label="最近问法"
              min-width="140"
              show-overflow-tooltip
            >
              <template #default="{ row }">
                {{ row.sample_question ?? '--' }}
              </template>
            </el-table-column>
            <el-table-column
              prop="ask_count"
              label="频次"
              width="80"
            />
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <span>高频引用文档 Top 10</span>
          </template>
          <el-table
            v-loading="loading"
            :data="topDocuments"
            border
            stripe
            size="small"
          >
            <el-table-column
              type="index"
              label="#"
              width="48"
            />
            <el-table-column
              label="文档"
              min-width="180"
              show-overflow-tooltip
            >
              <template #default="{ row }">
                {{ row.file_name ?? row.document_id }}
              </template>
            </el-table-column>
            <el-table-column
              prop="document_id"
              label="文档 ID"
              min-width="160"
              show-overflow-tooltip
            />
            <el-table-column
              prop="citation_count"
              label="引用次数"
              width="90"
            />
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.page {
  padding: 16px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.page-header h2 {
  margin: 0;
  font-size: 18px;
}
.filters {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.cards {
  margin-top: 12px;
}
.metric-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.metric-value {
  font-size: 22px;
  font-weight: 600;
  margin-top: 6px;
  color: var(--el-text-color-primary);
}
.metric-sub {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
}
.chart-card {
  margin-top: 12px;
}
.trend-chart {
  height: 300px;
}
</style>
