<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps({ selected: Object, runs: Array, selectedRun: Object, request: Function })
const emit = defineEmits(['select'])

const filter = ref('all')
const loading = ref(false)
const message = ref('')
const preview = ref(null)
const API = new URL(import.meta.env.VITE_API_BASE_URL || '/api/v1', location.href).href.replace(/\/+$/, '')

const typeLabels = { api: '接口', functional: '功能', web: 'Web', app: 'App', performance: '性能' }
const filteredRuns = computed(() => (props.runs || []).filter(
  (run) => filter.value === 'all' || (run.test_type || 'api') === filter.value,
))
const filteredCount = computed(() => filteredRuns.value.length)

function typeLabel(type) { return typeLabels[type] || type || '接口' }
function summary(run) { return run?.summary || {} }
function statusLabel(status) {
  return { completed: '已完成', running: '执行中', failed: '失败', manual_pending: '待人工执行', manual_in_progress: '人工执行中' }[status] || status || '未知'
}
function statusClass(status) {
  if (status === 'completed') return 'status-ok'
  if (status === 'failed') return 'status-fail'
  if (status === 'running' || status === 'manual_in_progress') return 'status-running'
  return 'status-pending'
}
function resultStatus(status) {
  return { passed: '通过', failed: '失败', error: '异常', blocked: '阻塞' }[status] || status || '未知'
}
function resultClass(status) {
  return status === 'passed' ? 'pass' : status === 'failed' ? 'fail' : 'error-text'
}
function evidence(response) {
  const item = response || {}
  const screenshots = item.screenshots || (item.screenshot ? [item.screenshot] : [])
  const logs = item.device_logs || item.console_logs || []
  return { screenshots, logs }
}

function formatTime(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—' }
function formatJson(value) { return JSON.stringify(value ?? {}, null, 2) }

function artifactPath(value) { if (!value) return ''; if (typeof value === 'object') return value.relative_path || value.path || value.filename || ''; return String(value) }
function artifactUrl(value) { const path = artifactPath(value); if (!path) return ''; if (path.startsWith('data:') || path.startsWith('http://') || path.startsWith('https://')) return path; if (!props.selectedRun?.id || !props.selected?.project_id) return ''; const encoded = path.split('/').filter(Boolean).map(encodeURIComponent).join('/'); return `${API}/projects/${encodeURIComponent(props.selected.project_id)}/runs/${props.selectedRun.id}/artifacts/${encoded}` }
function eventScreenshot(event) { return event?.evidence?.screenshot || event?.evidence?.file || null }
function openPreview(value) { const path = artifactPath(value); const url = artifactUrl(value); if (url) preview.value = { url, path, failed: false } }
function closePreview() { preview.value = null }
function onPreviewKeydown(event) { if (preview.value && event.key === 'Escape') closePreview() }

watch(() => [props.selected?.project_id, props.selectedRun?.id], closePreview)
onMounted(() => window.addEventListener('keydown', onPreviewKeydown))
onUnmounted(() => window.removeEventListener('keydown', onPreviewKeydown))

async function view(run) {
  if (!props.selected || !props.request) return
  loading.value = true
  message.value = ''
  try {
    emit('select', await props.request(`/projects/${props.selected.project_id}/runs/${run.id}`))
  } catch (error) {
    message.value = `加载执行详情失败：${error.message}`
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page-grid runs-page">
    <section class="panel">
      <div class="section-title runs-heading">
        <div><h3>测试执行记录</h3><p class="muted">按测试类型查看历史执行、断言结果和失败证据。</p></div>
        <div class="runs-toolbar"><select v-model="filter" aria-label="按测试类型筛选"><option value="all">全部类型</option><option value="api">接口</option><option value="functional">功能</option><option value="web">Web</option><option value="app">App</option><option value="performance">性能</option></select><span class="count">{{ filteredCount }} 次执行</span></div>
      </div>
      <div v-if="filteredRuns.length" class="run-list">
        <button v-for="run in filteredRuns" :key="run.id" type="button" class="run-card" :class="{ selected: selectedRun?.id === run.id }" @click="view(run)">
          <div class="run-main"><div class="run-key"><strong>{{ run.run_key }}</strong><span class="type-tag">{{ typeLabel(run.test_type) }}</span></div><small>{{ statusLabel(run.status) }} · {{ summary(run).total || 0 }} 条用例</small></div>
          <div class="run-stats"><span class="pass">通过 {{ summary(run).passed || 0 }}</span><span class="fail">失败 {{ summary(run).failed || 0 }}</span><span class="error-text">异常 {{ summary(run).error || 0 }}</span></div>
          <span class="run-status" :class="statusClass(run.status)">{{ statusLabel(run.status) }}</span>
        </button>
      </div>
      <p v-else class="empty">暂无匹配的执行记录。</p>
      <p v-if="loading" class="inline-message">正在加载执行详情…</p>
      <p v-if="message" class="inline-message error-message">{{ message }}</p>
    </section>

    <section v-if="selectedRun" class="panel run-detail">
      <div class="section-title">
        <div><h3>执行详情</h3><p class="muted">{{ typeLabel(selectedRun.test_type) }} · {{ statusLabel(selectedRun.status) }}</p></div>
        <span class="type-tag">{{ selectedRun.run_key }}</span>
      </div>
      <div class="detail-summary">
        <div><small>总计</small><strong>{{ summary(selectedRun).total || 0 }}</strong></div><div><small>通过</small><strong class="pass">{{ summary(selectedRun).passed || 0 }}</strong></div><div><small>失败</small><strong class="fail">{{ summary(selectedRun).failed || 0 }}</strong></div><div><small>异常</small><strong class="error-text">{{ summary(selectedRun).error || 0 }}</strong></div>
      </div>
      <div v-if="selectedRun.summary?.gate?.enabled" class="gate-banner" :class="selectedRun.summary.gate.passed ? 'gate-pass' : 'gate-fail'">性能门禁：<strong>{{ selectedRun.summary.gate.passed ? '通过' : '未通过' }}</strong><span v-for="check in selectedRun.summary.gate.checks || []" :key="check.metric">{{ check.metric }} {{ check.actual }} / {{ check.limit }}</span></div>
      <section v-if="selectedRun.events?.length" class="timeline-section">
        <div class="subsection-title"><div><h4>事件时间线</h4><p>按发生顺序展示用户、页面、接口、设备和系统事件。</p></div><span class="count">{{ selectedRun.events.length }} 条</span></div>
        <div class="timeline"><article v-for="event in selectedRun.events" :key="event.id" class="timeline-event" :class="`event-${event.status}`"><div class="timeline-dot"></div><div class="timeline-body"><div class="row-between"><strong>#{{ event.sequence }} · {{ event.event_type }}</strong><span>{{ event.source }} · {{ event.status }}</span></div><p>{{ event.summary || '未提供事件说明' }}</p><small>{{ formatTime(event.created_at) }}<template v-if="event.step_no"> · 步骤 {{ event.step_no }}</template></small><button v-if="eventScreenshot(event)" type="button" class="evidence-link evidence-button" @click="openPreview(eventScreenshot(event))">查看事件截图</button><details v-if="Object.keys(event.payload || {}).length"><summary>查看事件数据</summary><pre>{{ formatJson(event.payload) }}</pre></details></div></article></div>
      </section>
      <div v-if="selectedRun.results?.length" class="result-list">
        <article v-for="item in selectedRun.results" :key="item.id" class="result-card">
          <div class="result-head"><strong :class="resultClass(item.status)">{{ resultStatus(item.status) }}</strong><span>{{ item.duration_ms || 0 }} ms</span></div>
          <p>{{ item.summary || '未提供执行摘要' }}</p>
          <div v-if="item.request?.url || item.request?.server_url" class="result-request"><span v-if="item.request?.method">{{ item.request.method }}</span> {{ item.request?.url || item.request?.server_url }}</div>
          <div v-if="item.failure && Object.keys(item.failure).length" class="failure-box"><strong>失败信息</strong><p>{{ item.failure.summary || item.failure.message || '执行失败' }}</p><small v-if="item.failure.category">分类：{{ item.failure.category }}</small></div>
          <div v-if="item.assertions?.length" class="assertions"><div v-for="assertion in item.assertions" :key="assertion.type + JSON.stringify(assertion.expected)" class="assertion"><span :class="assertion.passed ? 'pass' : 'fail'">{{ assertion.passed ? '通过' : '失败' }}</span>{{ assertion.type }}<small v-if="assertion.actual !== undefined">实际：{{ JSON.stringify(assertion.actual) }}</small></div></div>
          <div v-if="evidence(item.response).screenshots.length" class="evidence-gallery"><h5>截图证据</h5><div><button v-for="shot in evidence(item.response).screenshots" :key="artifactPath(shot)" type="button" class="evidence-thumb" @click="openPreview(shot)"><img :src="artifactUrl(shot)" :alt="artifactPath(shot)" loading="lazy"><small>{{ artifactPath(shot) }}</small></button></div></div>
          <div v-if="evidence(item.response).screenshots.length || evidence(item.response).logs.length" class="evidence-line"><span>截图 {{ evidence(item.response).screenshots.length }}</span><span>日志 {{ evidence(item.response).logs.length }}</span><details v-if="evidence(item.response).logs.length"><summary>查看日志</summary><pre>{{ JSON.stringify(evidence(item.response).logs, null, 2) }}</pre></details></div>
        </article>
      </div>
      <p v-else class="empty">该执行暂无逐条结果。</p>
      <p v-if="selectedRun.report" class="report-path">报告：{{ selectedRun.report }}</p>
    </section>
    <section v-else class="panel detail-placeholder"><div class="empty large-empty">选择一条执行记录查看断言、失败信息和截图 / 日志证据。</div></section>
    <div v-if="preview" class="evidence-modal" role="dialog" aria-modal="true" aria-label="截图证据预览" @click.self="closePreview">
      <div class="evidence-modal-card">
        <div class="evidence-modal-head"><strong>截图证据预览</strong><button type="button" class="text-link" aria-label="关闭截图预览" @click="closePreview">关闭</button></div>
        <div v-if="preview.failed" class="empty evidence-load-error" role="alert">截图加载失败，请检查执行证据是否仍可访问。</div>
        <img v-else :src="preview.url" :alt="preview.path || '截图证据'" @error="preview.failed = true">
        <small>{{ preview.path || '截图证据' }}</small>
      </div>
    </div>
  </div>
</template>

<style scoped>
.runs-heading{align-items:flex-start}.runs-toolbar{display:flex;align-items:center;gap:10px}.run-list{display:grid;gap:8px;margin-top:18px}.run-card{display:grid;grid-template-columns:minmax(220px,1fr) auto auto;align-items:center;gap:14px;width:100%;padding:14px 12px;text-align:left;background:#fff;color:#243653;border:1px solid #e5ebf4;border-radius:10px}.run-card:hover,.run-card.selected{border-color:#8db1ff;background:#f5f8ff}.run-main small{display:block;color:#8290a7;margin-top:5px}.run-key{display:flex;align-items:center;gap:8px}.run-stats{display:flex;gap:12px;font-size:12px;white-space:nowrap}.run-status{font-size:12px;white-space:nowrap}.status-ok,.pass{color:#168457}.status-fail,.fail{color:#ce4957}.status-running{color:#b7791f}.status-pending,.error-text{color:#b7791f}.run-detail{min-width:0}.detail-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px}.detail-summary div{padding:13px;background:#f8faff;border:1px solid #e5ebf4;border-radius:9px}.detail-summary small{display:block;color:#8290a7}.detail-summary strong{display:block;margin-top:5px;font-size:19px;color:#172b4d}.gate-banner{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:14px;padding:10px 12px;border-radius:8px;font-size:12px}.gate-pass{color:#146c52;background:#effaf4;border:1px solid #b7e2ce}.gate-fail{color:#a33d48;background:#fff5f5;border:1px solid #f0c4c4}.gate-banner span{font-family:ui-monospace,monospace;color:#71819a}.result-list{display:grid;gap:10px;margin-top:18px}.result-card{padding:14px;border:1px solid #e5ebf4;border-radius:10px;background:#fbfcfe}.result-head{display:flex;justify-content:space-between;align-items:center;color:#8290a7;font-size:11px}.result-card>p{margin:8px 0;color:#52647f;line-height:1.5;font-size:12px}.result-request{font-family:ui-monospace,monospace;color:#5573a0;font-size:11px;word-break:break-all}.failure-box{margin-top:10px;padding:10px;border-radius:8px;background:#fff5f5;color:#8e2933}.failure-box p{margin:5px 0;color:#a33d48;font-size:12px;word-break:break-word}.failure-box small{color:#b35d66}.assertions{display:grid;gap:5px;margin-top:10px}.assertion{display:flex;align-items:baseline;gap:7px;color:#52647f;font-size:12px}.assertion small{color:#8290a7;word-break:break-all}.evidence-line{display:flex;align-items:center;gap:12px;margin-top:11px;color:#71819a;font-size:11px}.evidence-line details{margin-left:auto}.evidence-line summary{cursor:pointer;color:#5573a0}.evidence-line pre{max-width:100%;max-height:180px;overflow:auto;padding:8px;background:#f3f6fb;white-space:pre-wrap;word-break:break-word}.report-path{margin-top:14px;color:#71819a;font:11px ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-all}.error-message{color:#b42318}.detail-placeholder{min-height:260px}@media(max-width:850px){.runs-heading{display:block}.runs-toolbar{justify-content:flex-start;margin-top:12px}.run-card{grid-template-columns:1fr}.run-stats{flex-wrap:wrap}.detail-summary{grid-template-columns:repeat(2,1fr)}}
.subsection-title{display:flex;justify-content:space-between;align-items:flex-start;margin:22px 0 12px}.subsection-title h4,.subsection-title p{margin:0}.subsection-title p{margin-top:4px;color:#8290a7;font-size:12px}.timeline{display:grid}.timeline-event{display:grid;grid-template-columns:18px 1fr;gap:10px;position:relative}.timeline-event:not(:last-child):before{content:'';position:absolute;left:5px;top:17px;bottom:-8px;width:2px;background:#dfe6f0}.timeline-dot{width:12px;height:12px;border:3px solid #fff;border-radius:50%;background:#8da1bd;box-shadow:0 0 0 1px #cbd5e1;margin-top:5px;z-index:1}.event-passed .timeline-dot{background:#27a46f}.event-failed .timeline-dot,.event-blocked .timeline-dot{background:#d94d5c}.timeline-body{padding:0 0 16px}.timeline-body .row-between{align-items:center}.timeline-body p{margin:6px 0;color:#52647f;font-size:12px}.timeline-body small{color:#8290a7}.timeline-body details{margin-top:8px}.timeline-body summary{cursor:pointer;color:#5573a0;font-size:12px}.timeline-body pre{max-height:300px;overflow:auto;padding:10px;border-radius:8px;background:#f3f6fb;white-space:pre-wrap;word-break:break-word;font-size:11px}
.evidence-gallery{margin-top:14px}.evidence-gallery h5{margin:0 0 8px;color:#52647f}.evidence-gallery>div{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}.evidence-gallery img{width:100%;height:110px;object-fit:contain;border:1px solid #dce5f1;border-radius:8px;background:#eef2f7}.evidence-gallery small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.evidence-link{display:inline-block;margin:8px 8px 0 0;color:#155eef;font-size:12px}
.evidence-button{background:none;border:0;padding:0;cursor:pointer}.evidence-thumb{display:grid;gap:6px;min-width:0;padding:0;border:0;background:none;text-align:left;color:#5573a0;cursor:pointer}.evidence-modal{position:fixed;inset:0;z-index:30;display:grid;place-items:center;padding:24px;background:rgba(15,29,52,.72)}.evidence-modal-card{display:grid;gap:10px;width:min(960px,100%);max-height:calc(100vh - 48px);padding:16px;border-radius:14px;background:#fff;box-shadow:0 18px 60px rgba(0,0,0,.28)}.evidence-modal-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.evidence-modal-card>img{width:100%;max-height:calc(100vh - 150px);object-fit:contain;background:#f3f6fb;border:1px solid #dce5f1;border-radius:8px}.evidence-modal-card>small{overflow-wrap:anywhere;color:#71819a}.evidence-load-error{min-height:180px;display:grid;place-items:center;background:#fff7f7;border:1px solid #f0c4c4;border-radius:8px;color:#a33d48}
</style>
