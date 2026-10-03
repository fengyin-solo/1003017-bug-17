<template>
  <section class="page" data-module="battery">
    <header class="page-head">
      <div>
        <h2>蓄电池组管理</h2>
        <p class="page-desc">维护蓄电池组，围绕电池组编号、电池类型、额定容量、内阻值做登记、实测留底与单向状态流转。</p>
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
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <template v-if="actionFor(row.status)">
              <button
                class="link"
                type="button"
                @click="runAction(actionFor(row.status) as string, row)"
              >
                {{ actionFor(row.status) }}
              </button>
              <button class="link" type="button" @click="recordMeasurement(row)">登记实测</button>
            </template>
            <span v-else class="muted">已更换，流程终结</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无蓄电池组数据，可先登记蓄电池组</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条蓄电池组记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

interface ActionPayload {
  ok: boolean
  message: string
  entry?: Row
}

const ENDPOINT = '/api/battery'
const columns = ["电池组编号", "电池类型", "额定容量", "所属站点", "放电时长", "内阻值", "最近实测日期", "电池状态"]
// 状态只能 容量合格→容量下降→需更换→已更换 单向推进，每个状态只有一个可执行动作。
const nextAction: Record<string, string> = {
  '容量合格': '记录下降',
  '容量下降': '安排更换',
  '需更换': '完成更换',
}
const statsConfig = [
  { label: '合格电池组', status: '容量合格' },
  { label: '下降电池组', status: '容量下降' },
  { label: '需更换电池组', status: '需更换' },
  { label: '已更换电池组', status: '已更换' },
]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = ["电池组编号", "电池类型", "额定容量"]

const stats = computed(() =>
  statsConfig.map((item) => ({
    label: item.label,
    value: rows.value.filter((row) => row['电池状态'] === item.status || row.status === item.status).length,
  })),
)

function actionFor(status: unknown): string | null {
  return typeof status === 'string' ? nextAction[status] ?? null : null
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '蓄电池组登记入口尚未接入审批流'
}

async function postPayload(path: string, body: Record<string, unknown>): Promise<ActionPayload> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return (await response.json()) as ActionPayload
}

async function recordMeasurement(row: Row) {
  errorMessage.value = ''
  const resistance = window.prompt('请输入本次实测内阻值（mΩ，许可范围 0~100）')
  if (resistance === null) {
    return
  }
  const ratioInput = window.prompt('可选：输入实测容量占额定容量的比例（0~1，如 0.72），留空跳过')
  const values: Record<string, string> = { 内阻值: resistance }
  if (ratioInput && ratioInput.trim()) {
    values['容量比'] = ratioInput.trim()
  }
  const payload = await postPayload(`${ENDPOINT}/${row.id}/measurements`, { values })
  if (!payload.ok) {
    errorMessage.value = payload.message
    return
  }
  await reload()
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const payload = await postPayload(`${ENDPOINT}/${row.id}/actions`, { action })
    if (!payload.ok) {
      // 后端会点名为拦下（倒回/跳级/已终结），直接呈现，不做静默刷新。
      errorMessage.value = payload.message
      return
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '蓄电池组操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('蓄电池组列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '蓄电池组列表读取失败'
  }
}

onMounted(reload)
</script>
