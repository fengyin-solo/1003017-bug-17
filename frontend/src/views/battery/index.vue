<template>
  <section class="page" data-module="battery">
    <header class="page-head">
      <div>
        <h2>蓄电池组管理</h2>
        <p class="page-desc">维护蓄电池组，围绕电池组编号、电池类型、额定容量、所属站点做登记、筛选与状态流转。更换结论按内阻值与实测容量统一判定，列表与详情同一口径。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记蓄电池组</button>
        <button class="btn" type="button" @click="exportRows">导出蓄电池组清单</button>
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
          <td v-for="column in columns" :key="column">{{ display(row, column) }}</td>
          <td class="row-actions">
            <button
              v-if="nextAction(rowStatus(row))"
              class="link"
              type="button"
              @click="runAction(String(nextAction(rowStatus(row))), row)"
            >
              {{ nextAction(rowStatus(row)) }}
            </button>
            <button
              v-if="rowStatus(row) !== '已更换'"
              class="link"
              type="button"
              @click="openMeasurement(row)"
            >
              录入实测
            </button>
            <span v-if="!nextAction(rowStatus(row)) && rowStatus(row) === '已更换'" class="muted">已闭环</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无蓄电池组数据，可先登记蓄电池组</td>
        </tr>
      </tbody>
    </table>

    <div v-if="formMode" class="modal-mask" @click.self="closeForm">
      <form class="modal-card" @submit.prevent="submitForm">
        <h3>{{ formMode === 'create' ? '登记蓄电池组' : `录入实测 · ${String(formTarget?.['电池组编号'] ?? '')}` }}</h3>
        <label v-for="field in formFields" :key="field.key" class="form-item">
          <span>{{ field.label }}{{ field.required ? ' *' : '' }}</span>
          <input
            v-model="formValues[field.key]"
            :type="field.type"
            :placeholder="field.hint"
          />
        </label>
        <p class="form-tip">
          额定容量许可范围 1~5000 Ah；内阻值须大于 0 且不超过 50 mΩ。
          结论口径：内阻 ≥10mΩ 或实测容量不足额定 80% 记容量下降，≥20mΩ 或不足 60% 需更换。
        </p>
        <div class="form-actions">
          <button class="btn primary" type="submit">提交</button>
          <button class="btn ghost" type="button" @click="closeForm">取消</button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条蓄电池组记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, unknown>

const ENDPOINT = '/api/battery'
const columns = ["电池组编号", "电池类型", "额定容量", "所属站点", "放电时长", "内阻值", "投用日期", "电池状态"]
const statuses = ["容量合格", "容量下降", "需更换", "已更换"]
const actionByStep: Record<string, string> = {
  '容量合格': '记录下降',
  '容量下降': '安排更换',
  '需更换': '完成更换',
}

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const statDefs = [
  { label: '合格电池组', status: '容量合格' },
  { label: '下降电池组', status: '容量下降' },
  { label: '需更换电池组', status: '需更换' },
  { label: '已更换电池组', status: '已更换' },
]
const stats = ref(statDefs.map((item) => ({ label: item.label, value: 0 })))

// 登记 / 实测录入共用一个弹层
const formMode = ref<'' | 'create' | 'measurement'>('')
const formTarget = ref<Row | null>(null)
const formValues = ref<Record<string, string>>({})

const createFields = [
  { key: '电池组编号', label: '电池组编号', required: true, type: 'text', hint: '如 BATT-0005' },
  { key: '电池类型', label: '电池类型', required: true, type: 'text', hint: '如 阀控式铅酸蓄电池' },
  { key: '额定容量', label: '额定容量(Ah)', required: true, type: 'number', hint: '1~5000' },
  { key: '内阻值', label: '内阻值(mΩ)', required: true, type: 'number', hint: '大于 0，不超过 50' },
  { key: '实测容量', label: '实测容量(Ah)', required: false, type: 'number', hint: '可选，不超过额定容量' },
  { key: '所属站点', label: '所属站点', required: false, type: 'text', hint: '' },
  { key: '放电时长', label: '放电时长(h)', required: false, type: 'number', hint: '' },
]
const measurementFields = [
  { key: '内阻值', label: '本次实测内阻(mΩ)', required: true, type: 'number', hint: '大于 0，不超过 50' },
  { key: '实测容量', label: '本次实测容量(Ah)', required: false, type: 'number', hint: '可选' },
  { key: '实测日期', label: '实测日期', required: false, type: 'date', hint: '默认今天' },
]
const formFields = computed(() => (formMode.value === 'create' ? createFields : measurementFields))

function display(row: Row, column: string): string {
  const value = row[column]
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function rowStatus(row: Row): string {
  return String(row['status'] ?? row['电池状态'] ?? '')
}

function nextAction(status: string): string | undefined {
  return actionByStep[status]
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  formMode.value = 'create'
  formTarget.value = null
  formValues.value = {}
}

function openMeasurement(row: Row) {
  formMode.value = 'measurement'
  formTarget.value = row
  formValues.value = {}
}

function closeForm() {
  formMode.value = ''
  formTarget.value = null
}

async function submitForm() {
  errorMessage.value = ''
  const payload: Record<string, unknown> = { values: { ...formValues.value } }
  const url = formMode.value === 'create'
    ? ENDPOINT
    : `${ENDPOINT}/${String(formTarget.value?.id ?? '')}/measurements`
  try {
    const response = await request(url, { method: 'POST', body: JSON.stringify(payload) })
    const result = (await response.json()) as { ok: boolean; message: string }
    if (!result.ok) {
      errorMessage.value = result.message
      return
    }
    closeForm()
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${String(row.id)}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const result = (await response.json()) as { ok: boolean; message: string }
    if (!result.ok) {
      errorMessage.value = result.message
      return
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '蓄电池组操作失败'
  }
}

async function loadStats() {
  try {
    const response = await request(`${ENDPOINT}/export`)
    if (!response.ok) return
    const payload = (await response.json()) as { items?: Row[] }
    const items = payload.items ?? []
    stats.value = statDefs.map((def) => ({
      label: def.label,
      value: items.filter((row) => rowStatus(row) === def.status).length,
    }))
  } catch {
    // 统计不阻塞主列表
  }
}

async function reload() {
  errorMessage.value = ''
  // 列表只按电池组编号做关键字过滤，其余输入框保持界面占位
  const keyword = filters.value['电池组编号']
  const query = new URLSearchParams(keyword ? { keyword } : {}).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('蓄电池组列表读取失败')
    }
    const payload = await response.json() as { items?: Row[]; total?: number }
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await loadStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '蓄电池组列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}

.modal-card {
  width: min(560px, 92vw);
  max-height: 86vh;
  overflow: auto;
  background: #fff;
  border-radius: 8px;
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.form-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}

.form-item input {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
}

.form-tip {
  font-size: 12px;
  color: #64748b;
  line-height: 1.6;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.muted {
  color: #94a3b8;
}
</style>
