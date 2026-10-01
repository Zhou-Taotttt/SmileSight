<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({ selected: Object, request: Function })
const emit = defineEmits(['navigate'])

const defaults = () => ({
  execution: { default_timeout_seconds: 30, retry_count: 0, max_parallel_tasks: 2, capture_on_failure: true },
  web: { remote_url: null, browser: 'chrome', headless: true, live_view_url: 'http://localhost:7900' },
  app: { server_url: null, platform_name: 'Android', automation_name: 'UiAutomator2', device_name: '', launch_wait_seconds: 2.5 },
  performance: { engine: 'internal', jmeter_path: 'jmeter', default_concurrency: 5, default_requests: 20, max_p95_ms: null, max_error_rate: null, min_throughput_rps: null },
  ai: { enabled: false, provider: '', model: '', base_url: null },
})

const settings = ref(defaults())
const loading = ref(false)
const saving = ref(false)
const message = ref('')
const updatedAt = ref(null)
const secretPolicy = ref('')

const summary = computed(() => [
  { label: '默认超时', value: `${settings.value.execution.default_timeout_seconds}s` },
  { label: 'Web 驱动', value: settings.value.web.browser === 'chrome' ? 'Chrome' : settings.value.web.browser },
  { label: 'App 平台', value: settings.value.app.platform_name },
  { label: '性能引擎', value: settings.value.performance.engine === 'jmeter' ? 'JMeter' : '内置 HTTP' },
])

function mergeSettings(data = {}) {
  const base = defaults()
  for (const section of Object.keys(base)) Object.assign(base[section], data[section] || {})
  return base
}

function nullableNumber(value) {
  return value === '' || value === undefined ? null : value
}

function nullableText(value) {
  const text = String(value || '').trim()
  return text || null
}

async function load() {
  if (!props.selected) {
    settings.value = defaults()
    updatedAt.value = null
    return
  }
  loading.value = true
  message.value = ''
  try {
    const data = await props.request(`/projects/${props.selected.project_id}/settings`)
    settings.value = mergeSettings(data)
    updatedAt.value = data.updated_at
    secretPolicy.value = data.secret_policy || ''
  } catch (error) {
    message.value = error.message
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!props.selected) {
    message.value = '请先选择或创建项目'
    return
  }
  saving.value = true
  message.value = ''
  const payload = JSON.parse(JSON.stringify(settings.value))
  payload.web.remote_url = nullableText(payload.web.remote_url)
  payload.web.live_view_url = nullableText(payload.web.live_view_url)
  payload.app.server_url = nullableText(payload.app.server_url)
  payload.ai.base_url = nullableText(payload.ai.base_url)
  for (const key of ['max_p95_ms', 'max_error_rate', 'min_throughput_rps']) {
    payload.performance[key] = nullableNumber(payload.performance[key])
  }
  try {
    const data = await props.request(`/projects/${props.selected.project_id}/settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    settings.value = mergeSettings(data)
    updatedAt.value = data.updated_at
    secretPolicy.value = data.secret_policy || secretPolicy.value
    message.value = '配置已保存，并将在下一次测试执行时生效'
  } catch (error) {
    message.value = error.message
  } finally {
    saving.value = false
  }
}

function resetLocal() {
  settings.value = defaults()
  message.value = '已恢复默认值，点击“保存配置”后生效'
}

watch(() => props.selected?.project_id, load, { immediate: true })
</script>

<template>
  <div class="settings-page">
    <section class="panel settings-hero">
      <div>
        <p class="eyebrow">PROJECT DEFAULTS</p>
        <h2>项目配置中心</h2>
        <p class="muted">统一维护执行器默认参数。测试环境中的同名配置优先级更高，可针对 QA、预发和生产分别覆盖。</p>
      </div>
      <div class="settings-actions">
        <button class="secondary" type="button" :disabled="loading || saving" @click="resetLocal">恢复默认值</button>
        <button type="button" :disabled="loading || saving || !selected" @click="save">{{ saving ? '保存中…' : '保存配置' }}</button>
      </div>
      <div class="settings-summary">
        <div v-for="item in summary" :key="item.label"><small>{{ item.label }}</small><strong>{{ item.value }}</strong></div>
      </div>
      <p v-if="updatedAt" class="updated-at">上次保存：{{ new Date(updatedAt).toLocaleString() }}</p>
      <p v-if="message" class="inline-message">{{ message }}</p>
    </section>

    <div v-if="!selected" class="panel empty large-empty">请先选择或创建项目，再维护项目配置。</div>
    <div v-else-if="loading" class="panel empty large-empty">正在加载项目配置…</div>
    <div v-else class="settings-grid">
      <section class="panel settings-section">
        <div class="section-title"><div><h3>通用执行</h3><p class="muted">接口、Web、App 任务共享的安全执行边界。</p></div><span class="status-pill">Execution</span></div>
        <div class="form-grid">
          <label>默认超时（秒）<input v-model.number="settings.execution.default_timeout_seconds" type="number" min="1" max="300"></label>
          <label>失败重试次数<input v-model.number="settings.execution.retry_count" type="number" min="0" max="5"></label>
          <label>最大并行任务<input v-model.number="settings.execution.max_parallel_tasks" type="number" min="1" max="20"></label>
          <label class="check-field"><input v-model="settings.execution.capture_on_failure" type="checkbox">失败时采集证据</label>
        </div>
      </section>

      <section class="panel settings-section">
        <div class="section-title"><div><h3>Web 自动化</h3><p class="muted">配置 Selenium 服务和实时浏览器画面。</p></div><span class="status-pill">Selenium</span></div>
        <div class="form-grid">
          <label>Selenium Remote URL<input v-model="settings.web.remote_url" placeholder="留空使用 Docker 默认地址"></label>
          <label>浏览器<select v-model="settings.web.browser"><option value="chrome">Chrome</option></select></label>
          <label>实时画面地址<input v-model="settings.web.live_view_url" placeholder="http://localhost:7900"></label>
          <label class="check-field"><input v-model="settings.web.headless" type="checkbox">使用无头模式</label>
        </div>
      </section>

      <section class="panel settings-section">
        <div class="section-title"><div><h3>App 自动化</h3><p class="muted">项目级 Appium 默认值，环境配置可覆盖。</p></div><span class="status-pill">Appium</span></div>
        <div class="form-grid">
          <label>Appium Server URL<input v-model="settings.app.server_url" placeholder="http://host.docker.internal:4723/wd/hub"></label>
          <label>平台<select v-model="settings.app.platform_name"><option>Android</option><option>iOS</option></select></label>
          <label>自动化引擎<input v-model="settings.app.automation_name" placeholder="UiAutomator2"></label>
          <label>设备名称<input v-model="settings.app.device_name" placeholder="emulator-5554"></label>
          <label>启动等待（秒）<input v-model.number="settings.app.launch_wait_seconds" type="number" min="0" max="30" step="0.1"></label>
        </div>
      </section>

      <section class="panel settings-section">
        <div class="section-title"><div><h3>性能测试</h3><p class="muted">设置默认负载和质量门禁。</p></div><span class="status-pill">Performance</span></div>
        <div class="form-grid">
          <label>默认执行引擎<select v-model="settings.performance.engine"><option value="internal">内置 HTTP</option></select></label>
          <label>JMeter 命令<input v-model="settings.performance.jmeter_path" placeholder="jmeter"></label>
          <label>默认并发数<input v-model.number="settings.performance.default_concurrency" type="number" min="1" max="500"></label>
          <label>默认请求数<input v-model.number="settings.performance.default_requests" type="number" min="1" max="100000"></label>
          <label>P95 上限（ms）<input v-model.number="settings.performance.max_p95_ms" type="number" min="1" placeholder="不限制"></label>
          <label>错误率上限（%）<input v-model.number="settings.performance.max_error_rate" type="number" min="0" max="100" placeholder="不限制"></label>
          <label>吞吐量下限（req/s）<input v-model.number="settings.performance.min_throughput_rps" type="number" min="0" placeholder="不限制"></label>
        </div>
      </section>

      <section class="panel settings-section ai-section">
        <div class="section-title"><div><h3>AI 辅助</h3><p class="muted">预留用例生成和结果分析模型配置，不保存密钥。</p></div><span class="status-pill">AI</span></div>
        <div class="form-grid">
          <label class="check-field"><input v-model="settings.ai.enabled" type="checkbox">启用 AI 辅助</label>
          <label>服务商<input v-model="settings.ai.provider" placeholder="例如 OpenAI Compatible"></label>
          <label>模型<input v-model="settings.ai.model" placeholder="模型名称"></label>
          <label>Base URL<input v-model="settings.ai.base_url" placeholder="可选的兼容接口地址"></label>
        </div>
        <div class="secret-policy"><strong>密钥安全策略</strong><p>{{ secretPolicy || 'API 密钥和访问令牌只通过部署环境变量注入，平台页面不保存也不回显。' }}</p></div>
      </section>

      <section class="panel settings-section override-section">
        <div><h3>环境级覆盖</h3><p class="muted">需要为某个环境单独设置 Base URL、Appium capabilities 或接口变量时，请到项目资料管理编辑环境。环境值会覆盖本页的项目默认值。</p></div>
        <button class="secondary" type="button" @click="emit('navigate', 'materials')">前往项目资料管理</button>
      </section>
    </div>
  </div>
</template>

<style scoped>
.settings-page{display:grid;gap:18px}.settings-hero{position:relative}.settings-hero h2{margin:4px 0 7px}.settings-actions{position:absolute;right:22px;top:22px;display:flex;gap:9px}.settings-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:20px}.settings-summary div{padding:12px 14px;border:1px solid #e4eaf3;border-radius:10px;background:#f8faff}.settings-summary small{display:block;color:#8290a7}.settings-summary strong{display:block;margin-top:5px;color:#1b2b45}.updated-at{margin:12px 0 0;color:#8a98ac;font-size:11px}.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.settings-section{min-width:0}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px;margin-top:18px}.form-grid label{display:grid;gap:7px;color:#52647f;font-size:12px}.form-grid input,.form-grid select{width:100%;box-sizing:border-box}.check-field{display:flex!important;align-items:center;gap:8px!important;align-self:end;min-height:39px}.check-field input{width:auto}.ai-section,.override-section{grid-column:1/-1}.ai-section .form-grid{grid-template-columns:repeat(4,minmax(0,1fr))}.secret-policy{margin-top:16px;padding:12px 14px;border-radius:9px;background:#fff9e8;border:1px solid #f1dfaa;color:#725b1f}.secret-policy p{margin:5px 0 0;font-size:12px;line-height:1.5}.override-section{display:flex;align-items:center;justify-content:space-between;gap:18px}.override-section h3{margin:0 0 7px}.override-section p{margin:0;max-width:850px;line-height:1.6}@media(max-width:900px){.settings-actions{position:static;margin-top:15px}.settings-grid{grid-template-columns:1fr}.settings-summary{grid-template-columns:repeat(2,1fr)}.ai-section,.override-section{grid-column:auto}.ai-section .form-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:600px){.form-grid,.ai-section .form-grid{grid-template-columns:1fr}.settings-summary{grid-template-columns:1fr}.override-section{display:grid}}
</style>
