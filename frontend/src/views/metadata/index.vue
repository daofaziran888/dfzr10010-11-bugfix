<template>
  <section class="page" data-module="metadata">
    <header class="page-head">
      <div>
        <h2>元数据服务管理</h2>
        <p class="page-desc">
          维护元数据表的字段清单、统计信息与分区元数据；字段版本可追溯，同一张表只保留一个当前版本，
          历史版本与注销前最后有效清单继续供血缘引用。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记元数据表</button>
        <button class="btn" type="button" @click="exportRows">导出元数据服务清单</button>
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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <button
              v-if="column === '字段清单'"
              class="link"
              type="button"
              @click="openDetail(row)"
            >
              {{ row[column] ?? '—' }}
            </button>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无元数据服务数据，可先登记元数据表</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条元数据服务记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-if="successMessage" class="ok-text">{{ successMessage }}</span>
    </footer>

    <section v-if="detail" class="detail-panel">
      <header class="detail-head">
        <div>
          <h3>{{ detail['表名称'] }}（{{ detail['表编号'] }}）</h3>
          <p class="page-desc">
            当前字段版本 {{ detail['当前字段版本'] ?? '—' }} · 最后有效字段版本
            {{ detail['最后有效字段版本'] ?? '—' }} · 注销状态 {{ detail['注销状态'] ?? '未注销' }}
          </p>
        </div>
        <button class="btn ghost" type="button" @click="closeDetail">收起</button>
      </header>

      <div class="filter-bar">
        <label class="filter-item">
          <span>查看字段版本</span>
          <select v-model="selectedVersion" @change="loadDetail">
            <option value="">当前版本</option>
            <option v-for="version in detailVersions" :key="version.version" :value="version.version">
              {{ version.version }}（{{ version['状态'] }}{{ version['注销状态'] === '已注销' ? ' / 已注销' : '' }}）
            </option>
          </select>
        </label>
        <button
          v-if="detail['当前字段版本'] && detail['注销状态'] !== '已注销' && selectedVersion !== detail['当前字段版本']"
          class="btn"
          type="button"
          @click="activateVersion(detail['当前字段版本'])"
        >
          恢复当前版本
        </button>
      </div>

      <form
        v-if="detail['注销状态'] !== '已注销'"
        class="filter-bar"
        @submit.prevent="submitRefresh"
      >
        <label class="filter-item">
          <span>刷新字段清单（生成新版本，旧版本保留）</span>
          <input
            v-model="refreshFieldsText"
            placeholder="order_id:bigint:false:订单ID; dt:date:true:日期"
          />
        </label>
        <button class="btn primary" type="submit">刷新元数据</button>
      </form>

      <table class="data-table">
        <thead>
          <tr><th>字段名</th><th>类型</th><th>可空</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="field in detailFields" :key="field.name">
            <td>{{ field.name }}</td>
            <td>{{ field.type }}</td>
            <td>{{ field.nullable ? '是' : '否' }}</td>
            <td>{{ field.comment || '—' }}</td>
          </tr>
          <tr v-if="!detailFields.length">
            <td colspan="4" class="empty-state">该字段版本没有可追溯的字段清单</td>
          </tr>
        </tbody>
      </table>
      <p class="page-desc">
        正在查看 {{ detail['查看字段版本'] ?? '—' }}（{{ detail['查看字段版本状态'] ?? '—' }}）·
        字段指纹 {{ detail['查看字段指纹'] ?? '—' }} · 字段数 {{ detailFields.length }}
      </p>

      <h4>字段版本流转</h4>
      <table class="data-table">
        <thead>
          <tr><th>字段版本</th><th>状态</th><th>当前版本</th><th>注销状态</th><th>字段数</th><th>创建时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="version in detailVersions" :key="version.version">
            <td>{{ version.version }}</td>
            <td>{{ version['状态'] }}</td>
            <td>{{ version.current ? '是' : '' }}</td>
            <td>{{ version['注销状态'] }}</td>
            <td>{{ version['字段数'] }}</td>
            <td>{{ version['创建时间'] }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="viewVersion(version.version)">查看</button>
              <button
                class="link"
                type="button"
                :disabled="version.current || version['注销状态'] === '已注销' || detail?.['注销状态'] === '已注销'"
                @click="activateVersion(version.version)"
              >
                设为当前
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <h4>字段审计台账</h4>
      <table class="data-table">
        <thead>
          <tr><th>台账编号</th><th>数据来源</th><th>字段版本</th><th>校验结果</th><th>注销状态</th><th>字段摘要</th><th>校验说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="record in detailAudit" :key="record['台账编号']">
            <td>{{ record['台账编号'] }}</td>
            <td>{{ record['数据来源'] }}</td>
            <td>{{ record['字段版本'] }}</td>
            <td>{{ record['校验结果'] }}</td>
            <td>{{ record['注销状态'] }}</td>
            <td>{{ record['字段摘要'] || '—' }}</td>
            <td>{{ record['校验说明'] }}</td>
          </tr>
          <tr v-if="!detailAudit.length">
            <td colspan="7" class="empty-state">尚无字段审计记录</td>
          </tr>
        </tbody>
      </table>
      <p v-if="consistency" class="page-desc">
        元数据与血缘核对：
        <strong :class="consistency.consistent ? 'ok-text' : 'error-text'">{{ consistency.result }}</strong>
        —— {{ consistency.detail }}
      </p>
    </section>

    <section v-if="createOpen" class="detail-panel">
      <header class="detail-head">
        <h3>登记元数据表</h3>
        <button class="btn ghost" type="button" @click="createOpen = false">取消</button>
      </header>
      <form class="filter-bar" @submit.prevent="submitCreate">
        <label class="filter-item">
          <span>表编号</span>
          <input v-model="createForm['表编号']" placeholder="如 META-2026-0010" required />
        </label>
        <label class="filter-item">
          <span>表名称</span>
          <input v-model="createForm['表名称']" placeholder="如 lake.orders" required />
        </label>
        <label class="filter-item">
          <span>字段清单</span>
          <input
            v-model="createForm.fieldsText"
            placeholder="order_id:bigint:false:订单ID, dt:date:true:日期"
          />
        </label>
        <button class="btn primary" type="submit">提交登记</button>
      </form>
      <p class="page-desc">登记后表为草稿状态；注册后才进入已注册，刷新元数据会生成新的字段版本。</p>
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

const ENDPOINT = '/api/metadata'
const columns = ['表编号', '表名称', '字段清单', '分区信息', '统计信息', '最后更新', '数据大小', '表状态']
const actions = ['注册表', '刷新元数据', '注销表']
const statuses = ['草稿', '已注册', '已过期', '已注销']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const successMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const detail = ref<Row | null>(null)
const selectedVersion = ref('')
const consistency = ref<Row | null>(null)
const createOpen = ref(false)
const createForm = ref<Record<string, string>>({ '表编号': '', '表名称': '', fieldsText: '' })
const refreshFieldsText = ref('')

const stats = computed(() => [
  { label: '元数据表总数', value: total.value },
  { label: '已注册表', value: rows.value.filter((row) => row.status === '已注册').length },
  { label: '已注销表', value: rows.value.filter((row) => row.status === '已注销').length },
])
const detailVersions = computed<Row[]>(() => detail.value?.['字段版本清单'] ?? [])
const detailFields = computed<FieldDef[]>(() => detail.value?.['查看字段清单'] ?? [])
const detailAudit = computed<Row[]>(() => detail.value?.['字段审计台账'] ?? [])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  createForm.value = { '表编号': '', '表名称': '', fieldsText: '' }
  createOpen.value = true
}

function closeDetail() {
  detail.value = null
  selectedVersion.value = ''
  consistency.value = null
}

function openDetail(row: Row) {
  detail.value = row
  selectedVersion.value = ''
  refreshFieldsText.value = ''
  void loadDetail()
}

function viewVersion(versionId: string) {
  selectedVersion.value = versionId
  void loadDetail()
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const values = {
      '表编号': createForm.value['表编号'],
      '表名称': createForm.value['表名称'],
      '字段清单': createForm.value.fieldsText,
    }
    const response = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values }) })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '元数据表登记失败')
    }
    successMessage.value = payload.message
    createOpen.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '元数据表登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '元数据服务动作未生效，请稍后重试')
    }
    successMessage.value = payload.message
    if (detail.value?.id === row.id) {
      detail.value = payload.entry
      void loadDetail()
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '元数据服务操作失败'
  }
}

async function submitRefresh() {
  const target = detail.value
  if (!target) return
  if (!refreshFieldsText.value.trim()) {
    errorMessage.value = '请填写字段清单后再刷新'
    return
  }
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${target.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          action: '刷新元数据',
          字段列表: refreshFieldsText.value,
          基线版本: target['当前字段版本'] ?? undefined,
        },
      }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '刷新元数据失败')
    }
    successMessage.value = payload.message
    refreshFieldsText.value = ''
    detail.value = payload.entry
    selectedVersion.value = ''
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '刷新元数据失败'
  }
}

async function activateVersion(versionId: string) {
  if (!detail.value) return
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${detail.value.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action: '切换字段版本', 字段版本: versionId } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '字段版本切换失败')
    }
    successMessage.value = payload.message
    detail.value = payload.entry
    selectedVersion.value = ''
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段版本切换失败'
  }
}

async function loadDetail() {
  const target = detail.value
  if (!target) return
  const query = selectedVersion.value ? `?version=${encodeURIComponent(selectedVersion.value)}` : ''
  try {
    const response = await request(`${ENDPOINT}/${target.id}${query}`)
    if (!response.ok) {
      throw new Error('元数据表明细读取失败')
    }
    detail.value = await response.json()
    const consistencyResponse = await request(`${ENDPOINT}/audit/consistency/${target.id}`)
    consistency.value = consistencyResponse.ok ? await consistencyResponse.json() : null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '元数据表明细读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('元数据表列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '元数据服务列表读取失败'
  }
}

onMounted(reload)
</script>
