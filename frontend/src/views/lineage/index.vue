<template>
  <section class="page" data-module="lineage">
    <header class="page-head">
      <div>
        <h2>湖仓血缘追踪管理</h2>
        <p class="page-desc">
          维护湖仓血缘的上游下游追溯、字段映射与批次记录；血缘只引用一份完整字段定义，
          先声明可追溯字段清单的兼容目标，再引用已有数据并保存字段版本与注销状态。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记血缘记录</button>
        <button class="btn" type="button" @click="exportRows">导出湖仓血缘追踪清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>引用字段版本</th>
          <th>注销状态</th>
          <th>引用状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row['引用的字段版本'] || '—' }}</td>
          <td>{{ row['引用注销状态'] || '未注销' }}</td>
          <td>{{ row['引用状态'] || '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openDetail(row)">查看明细</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无湖仓血缘追踪数据，可先登记血缘记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条湖仓血缘追踪记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-if="successMessage" class="ok-text">{{ successMessage }}</span>
    </footer>

    <section v-if="detail" class="detail-panel">
      <header class="detail-head">
        <div>
          <h3>{{ detail['记录编号'] }}：{{ detail['上游表名'] }} → {{ detail['下游表名'] }}</h3>
          <p class="page-desc">
            引用字段版本 {{ detail['引用的字段版本'] || '—' }} · 注销状态
            {{ detail['引用注销状态'] || '未注销' }} · 兼容目标 {{ detail['兼容目标'] || '可追溯字段清单' }}
          </p>
        </div>
        <button class="btn ghost" type="button" @click="closeDetail">收起</button>
      </header>

      <div class="filter-bar">
        <label class="filter-item">
          <span>回查字段版本</span>
          <select v-model="queryVersion" @change="loadDetail">
            <option value="">当前引用</option>
            <option
              v-for="version in detail['可回查字段版本'] ?? []"
              :key="version.version"
              :value="version.version"
            >
              {{ version.version }}（{{ version['状态'] }}{{ version['最后有效'] ? ' / 最后有效' : '' }}）
            </option>
          </select>
        </label>
        <button class="btn" type="button" @click="retryReference">重试可追溯字段清单</button>
      </div>

      <form class="filter-bar" @submit.prevent="relinkVersion">
        <label class="filter-item">
          <span>按字段版本重新引用</span>
          <select v-model="linkVersion">
            <option value="">上游表当前版本</option>
            <option
              v-for="version in detail['可回查字段版本'] ?? []"
              :key="version.version"
              :value="version.version"
            >
              {{ version.version }}（{{ version['状态'] }}{{ version['注销状态'] === '已注销' ? ' / 已注销' : '' }}）
            </option>
          </select>
        </label>
        <button class="btn" type="submit">重新引用</button>
      </form>

      <p v-if="detail['引用说明']" class="page-desc">引用说明：{{ detail['引用说明'] }}</p>
      <p class="page-desc">
        正在回查 {{ detail['回查字段版本'] ?? '—' }} · 字段指纹 {{ detail['回查字段指纹'] ?? '—' }} ·
        注销状态 {{ detail['回查注销状态'] ?? '未注销' }}
      </p>

      <table class="data-table">
        <thead>
          <tr><th>字段名</th><th>类型</th><th>可空</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="field in queryFields" :key="field.name">
            <td>{{ field.name }}</td>
            <td>{{ field.type }}</td>
            <td>{{ field.nullable ? '是' : '否' }}</td>
            <td>{{ field.comment || '—' }}</td>
          </tr>
          <tr v-if="!queryFields.length">
            <td colspan="4" class="empty-state">该字段版本没有可回查的字段清单</td>
          </tr>
        </tbody>
      </table>

      <p class="page-desc">
        引用快照 {{ (detail['引用字段清单'] ?? []).map((field: FieldDef) => field.name).join('、') || '—' }}
      </p>
    </section>

    <section v-if="createOpen" class="detail-panel">
      <header class="detail-head">
        <h3>登记血缘记录</h3>
        <button class="btn ghost" type="button" @click="createOpen = false">取消</button>
      </header>
      <form class="filter-bar" @submit.prevent="submitCreate">
        <label class="filter-item">
          <span>记录编号</span>
          <input v-model="createForm['记录编号']" placeholder="如 LINE-0010" required />
        </label>
        <label class="filter-item">
          <span>上游表名</span>
          <input v-model="createForm['上游表名']" placeholder="如 lake.orders" required />
        </label>
        <label class="filter-item">
          <span>下游表名</span>
          <input v-model="createForm['下游表名']" placeholder="如 dws_order_daily" required />
        </label>
        <label class="filter-item">
          <span>字段映射</span>
          <input v-model="createForm['字段映射']" placeholder="order_id -> order_id" />
        </label>
        <button class="btn primary" type="submit">提交登记</button>
      </form>
      <p class="page-desc">登记后自动声明兼容目标并引用上游表当前字段版本；失败项可单独重试。</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

interface FieldDef {
  name: string
  type: string
  nullable: boolean
  comment: string
}

const ENDPOINT = '/api/lineage'
const columns = ['记录编号', '上游表名', '下游表名', '转换类型', '字段映射', '操作时间', '操作批次', '记录状态']
const actions = ['关联血缘', '标记过期', '清理记录']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const successMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const detail = ref<Row | null>(null)
const queryVersion = ref('')
const linkVersion = ref('')
const createOpen = ref(false)
const createForm = ref<Record<string, string>>({
  '记录编号': '',
  '上游表名': '',
  '下游表名': '',
  '字段映射': '',
})

const stats = computed(() => [
  { label: '血缘记录总数', value: total.value },
  { label: '已关联记录', value: rows.value.filter((row) => row.status === '已关联').length },
  { label: '待重试记录', value: rows.value.filter((row) => row['引用状态'] === '待重试').length },
])
const queryFields = computed<FieldDef[]>(() => detail.value?.['回查字段清单'] ?? [])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  createForm.value = { '记录编号': '', '上游表名': '', '下游表名': '', '字段映射': '' }
  createOpen.value = true
}

function closeDetail() {
  detail.value = null
  queryVersion.value = ''
  linkVersion.value = ''
}

function openDetail(row: Row) {
  detail.value = row
  queryVersion.value = ''
  linkVersion.value = ''
  void loadDetail()
}

async function submitCreate() {
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: createForm.value }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '血缘记录登记失败')
    }
    successMessage.value = payload.message
    createOpen.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '血缘记录登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const body = { values: { action } }
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '湖仓血缘追踪动作未生效，请稍后重试')
    }
    successMessage.value = payload.message
    if (detail.value?.id === row.id) {
      detail.value = payload.entry
      void loadDetail()
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '湖仓血缘追踪操作失败'
  }
}

async function retryReference() {
  if (!detail.value) return
  await runAction('重试引用', detail.value)
}

async function relinkVersion() {
  const target = detail.value
  if (!target) return
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${target.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({
        values: { action: '关联血缘', 字段版本: linkVersion.value || undefined },
      }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '重新引用字段版本失败')
    }
    successMessage.value = payload.message
    detail.value = payload.entry
    queryVersion.value = ''
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重新引用字段版本失败'
  }
}

async function loadDetail() {
  if (!detail.value) return
  const query = queryVersion.value ? `?version=${encodeURIComponent(queryVersion.value)}` : ''
  try {
    const response = await request(`${ENDPOINT}/${detail.value.id}${query}`)
    if (!response.ok) {
      throw new Error('血缘记录明细读取失败')
    }
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '血缘记录明细读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('血缘记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '湖仓血缘追踪列表读取失败'
  }
}

onMounted(reload)
</script>
