<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({selected:Object, cases:Array, environments:Array, request:Function})
const emit = defineEmits(['refresh'])
const API = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8001/api/v1'

const environmentId = ref('')
const channel = ref('browser')
const mode = ref('strict')
const session = ref(null)
const activeCaseId = ref(null)
const activeStepNo = ref(1)
const notes = ref({})
const uploadFiles = ref({})
const message = ref('')
const busy = ref(false)

const functionalCases = computed(() => (props.cases || []).filter(item => item.test_type === 'functional'))
const enabledCases = computed(() => functionalCases.value.filter(item => item.enabled))
const activeCase = computed(() => functionalCases.value.find(item => item.id === activeCaseId.value) || functionalCases.value[0] || null)
const activeStep = computed(() => (activeCase.value?.steps || []).find((item, index) => Number(item.step_no || index + 1) === Number(activeStepNo.value)) || null)
const events = computed(() => session.value?.events || [])
const latestCheckpoint = computed(() => [...events.value].reverse().find(item => item.event_type === 'checkpoint' && (!activeCase.value || item.case_id === activeCase.value.id)) || null)
const assertions = computed(() => latestCheckpoint.value?.payload?.assertions || [])
const network = computed(() => latestCheckpoint.value?.payload?.network || [])
const logs = computed(() => [
  ...(latestCheckpoint.value?.payload?.console_logs || []).map(item => ({...item, source:'浏览器'})),
  ...(latestCheckpoint.value?.payload?.device_logs || []).map(item => ({...item, source:'设备'})),
])
const latestScreenshot = computed(() => latestCheckpoint.value?.evidence?.screenshot?.relative_path || '')
const sessionActive = computed(() => ['manual_pending','manual_in_progress','interactive_starting','interactive_running'].includes(session.value?.status))

watch(() => props.environments, value => {
  if (!environmentId.value && value?.length) environmentId.value = String(value[0].id)
}, {immediate:true, deep:true})
watch(functionalCases, value => {
  if (!value.some(item => item.id === activeCaseId.value)) activeCaseId.value = value[0]?.id || null
}, {immediate:true})
watch(activeCaseId, () => { activeStepNo.value = Number(activeCase.value?.steps?.[0]?.step_no || 1) })
watch(() => props.selected?.project_id, () => {
  session.value = null
  message.value = ''
})

function resultFor(caseId) {
  return [...events.value].reverse().find(item => item.case_id === caseId && item.event_type === 'case_result')
}
function stepNumber(step, index) { return Number(step.step_no || index + 1) }
function artifactUrl(relativePath) {
  if (!relativePath || !session.value || !props.selected) return ''
  const encoded = relativePath.split('/').map(encodeURIComponent).join('/')
  return `${API}/projects/${encodeURIComponent(props.selected.project_id)}/functional-runs/${session.value.run_id}/artifacts/${encoded}`
}
async function refreshSession() {
  if (!session.value || !props.selected) return
  session.value = await props.request(`/projects/${props.selected.project_id}/functional-runs/${session.value.run_id}`)
}
async function start() {
  if (!props.selected) { message.value = '请先选择项目'; return }
  if (!environmentId.value) { message.value = '请先配置并选择测试环境'; return }
  if (!enabledCases.value.length) { message.value = '请先准备并启用功能测试用例'; return }
  busy.value = true; message.value = ''
  try {
    session.value = await props.request(`/projects/${props.selected.project_id}/functional-runs`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({environment_id:Number(environmentId.value), case_ids:enabledCases.value.map(item => item.id), channel:channel.value, mode:mode.value}),
    })
    activeCaseId.value = enabledCases.value[0]?.id || null
    message.value = channel.value === 'browser' ? '浏览器人工会话已连接，可在中间画面操作' : channel.value === 'app' ? 'App 人工会话已连接' : '人工记录会话已开始'
  } catch (error) { message.value = error.message } finally { busy.value = false }
}
async function captureCheckpoint() {
  if (!session.value || !activeCase.value) return
  busy.value = true; message.value = ''
  try {
    await props.request(`/projects/${props.selected.project_id}/functional-runs/${session.value.run_id}/checkpoint`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({case_id:activeCase.value.id, step_no:Number(activeStepNo.value), note:notes.value[activeCase.value.id] || ''}),
    })
    await refreshSession()
    const failed = assertions.value.filter(item => item.passed === false).length
    message.value = failed ? `已采集检查点，${failed} 条断言失败` : '已采集页面、接口、日志、截图并完成断言'
  } catch (error) { message.value = error.message } finally { busy.value = false }
}
async function addObservation() {
  if (!session.value || !activeCase.value) return
  const text = (notes.value[activeCase.value.id] || '').trim()
  if (!text) { message.value = '请先填写观察说明'; return }
  busy.value = true
  try {
    await props.request(`/projects/${props.selected.project_id}/functional-runs/${session.value.run_id}/events`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({case_id:activeCase.value.id, step_no:Number(activeStepNo.value), source:'user', event_type:'manual_note', status:'recorded', summary:text}),
    })
    await refreshSession(); message.value = '人工观察已加入事件时间线'
  } catch (error) { message.value = error.message } finally { busy.value = false }
}
async function uploadEvidence(caseItem) {
  const file = uploadFiles.value[caseItem.id]
  if (!session.value || !file) { message.value = '请先选择证据文件'; return }
  busy.value = true
  try {
    const form = new FormData(); form.append('file', file)
    await props.request(`/projects/${props.selected.project_id}/functional-runs/${session.value.run_id}/evidence?case_id=${caseItem.id}&step_no=${Number(activeStepNo.value)}`, {method:'POST', body:form})
    await refreshSession(); message.value = `证据 ${file.name} 已上传`
  } catch (error) { message.value = error.message } finally { busy.value = false }
}
async function record(caseItem, status) {
  if (!session.value) return
  busy.value = true
  const text = (notes.value[caseItem.id] || '').trim()
  try {
    await props.request(`/projects/${props.selected.project_id}/functional-runs/${session.value.run_id}/results`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({case_id:caseItem.id, step_no:Number(activeStepNo.value), status, summary:text || ({passed:'人工确认业务结果通过', failed:'人工观察到功能异常', blocked:'当前条件阻塞，暂无法继续'}[status]), failure:status === 'failed' ? {category:'manual_observation'} : {}}),
    })
    await refreshSession(); emit('refresh')
    message.value = `已记录：${caseItem.title} · ${status}`
  } catch (error) { message.value = error.message } finally { busy.value = false }
}
async function finish(status = 'completed') {
  if (!session.value) return
  busy.value = true
  try {
    session.value = await props.request(`/projects/${props.selected.project_id}/functional-runs/${session.value.run_id}/finish`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({status, summary:status === 'cancelled' ? '用户取消人工测试会话' : '用户结束人工测试会话'}),
    })
    emit('refresh'); message.value = session.value.status === 'completed' ? '功能测试会话已完成' : '会话已结束，未记录用例保留为待处理'
  } catch (error) { message.value = error.message } finally { busy.value = false }
}
</script>

<template>
  <section class="functional-workspace">
    <div class="functional-toolbar panel">
      <div><p class="eyebrow">FUNCTIONAL SESSION</p><h3>功能测试交互工作台</h3><p class="muted">按步骤人工操作，平台同步关联页面、接口、设备日志、断言和证据。</p></div>
      <div class="functional-session-config">
        <label>测试环境<select v-model="environmentId" :disabled="sessionActive"><option value="" disabled>请选择环境</option><option v-for="env in environments" :key="env.id" :value="String(env.id)">{{env.name}} · {{env.base_url}}</option></select></label>
        <label>连接通道<select v-model="channel" :disabled="sessionActive"><option value="browser">浏览器 · Selenium/noVNC</option><option value="app">App · Appium/设备</option><option value="manual">仅人工记录</option></select></label>
        <label>执行模式<select v-model="mode" :disabled="sessionActive"><option value="strict">严格执行模式</option><option value="exploratory">探索测试模式</option></select></label>
        <button v-if="!sessionActive" type="button" :disabled="busy || !enabledCases.length" @click="start">{{busy ? '连接中…' : '开始测试会话'}}</button>
        <button v-else type="button" class="secondary" :disabled="busy" @click="finish('completed')">结束会话</button>
      </div>
    </div>

    <div v-if="session" class="session-strip"><span class="status-pill">{{session.channel}} · {{session.mode}}</span><strong>{{session.run_key}}</strong><span>{{session.summary?.passed || 0}} 通过 / {{session.summary?.failed || 0}} 失败 / {{session.summary?.blocked || 0}} 阻塞 / {{session.summary?.pending || 0}} 待处理</span><button v-if="sessionActive" type="button" class="link-button danger-text" @click="finish('cancelled')">取消会话</button></div>

    <div class="functional-three-col">
      <aside class="panel functional-scope">
        <div class="section-title"><div><h3>用例与步骤</h3><p class="muted">{{functionalCases.length}} 条功能用例</p></div></div>
        <button v-for="item in functionalCases" :key="item.id" type="button" class="functional-case-button" :class="{active:item.id===activeCase?.id}" @click="activeCaseId=item.id"><span><b>{{item.title}}</b><small>{{item.case_key}} · {{item.priority}}</small></span><i :class="resultFor(item.id)?.status || (item.enabled?'pending':'disabled')">{{resultFor(item.id)?.status || (item.enabled?'待执行':'已禁用')}}</i></button>
        <div v-if="activeCase" class="functional-steps"><p class="eyebrow">当前步骤</p><button v-for="(step,index) in activeCase.steps || []" :key="stepNumber(step,index)" type="button" :class="{active:stepNumber(step,index)===Number(activeStepNo)}" @click="activeStepNo=stepNumber(step,index)"><span>{{stepNumber(step,index)}}</span><div><b>{{step.action || step.title || '人工操作'}}</b><small>预期：{{step.expected || '由测试人员确认'}}</small></div></button><p v-if="!(activeCase.steps || []).length" class="empty compact">该用例暂无结构化步骤，可在探索模式记录。</p></div>
      </aside>

      <main class="panel functional-stage">
        <div class="section-title"><div><h3>被测对象主画面</h3><p class="muted">{{activeStep?.action || '选择步骤后开始操作'}}</p></div><a v-if="session?.live_view_url" :href="session.live_view_url" target="_blank" rel="noreferrer" class="button-link">新窗口打开</a></div>
        <div v-if="session?.channel==='browser' && session.live_view_url" class="live-frame-wrap"><iframe :key="session.run_id" :src="session.live_view_url" title="Selenium 浏览器实时画面"></iframe></div>
        <div v-else-if="session?.channel==='app'" class="device-stage"><span>▦</span><h4>请在已连接的模拟器或真机上操作</h4><p>点击“采集并断言”后，平台会抓取设备截图、Activity、Logcat 和断言结果。</p><a v-if="session.live_view_url" :href="session.live_view_url" target="_blank" rel="noreferrer">打开设备画面</a></div>
        <div v-else class="manual-stage"><span>◉</span><h4>{{session ? '仅人工记录模式' : '尚未开始测试会话'}}</h4><p>{{session ? '按实际操作填写观察说明并上传截图、日志或录像证据。' : '选择测试环境、连接通道和执行模式后开始。'}}</p></div>
        <div v-if="latestScreenshot" class="checkpoint-preview"><p class="eyebrow">最近检查点截图</p><img :src="artifactUrl(latestScreenshot)" alt="功能测试检查点截图"></div>
        <div v-if="activeCase" class="manual-controls"><textarea v-model="notes[activeCase.id]" placeholder="记录当前页面结果、异常描述、复现条件或阻塞原因"></textarea><div class="action-row"><button v-if="session?.channel!=='manual'" type="button" :disabled="!sessionActive || busy" @click="captureCheckpoint">采集并断言当前步骤</button><button type="button" class="secondary" :disabled="!sessionActive || busy" @click="addObservation">记录人工观察</button></div><div class="evidence-upload"><input type="file" @change="uploadFiles[activeCase.id]=$event.target.files[0]"><button type="button" class="secondary" :disabled="!sessionActive || busy || !uploadFiles[activeCase.id]" @click="uploadEvidence(activeCase)">上传证据</button></div><div class="action-row result-actions"><button class="pass-button" :disabled="!sessionActive || busy" @click="record(activeCase,'passed')">通过</button><button class="fail-button" :disabled="!sessionActive || busy" @click="record(activeCase,'failed')">失败</button><button class="block-button" :disabled="!sessionActive || busy" @click="record(activeCase,'blocked')">阻塞</button></div></div>
      </main>

      <aside class="panel functional-observe">
        <div class="section-title"><div><h3>实时观察与证据</h3><p class="muted">页面 / 接口 / 设备统一时间线</p></div><span class="count">{{events.length}}</span></div>
        <div v-if="latestCheckpoint" class="observation-summary"><div><b>页面</b><span>{{latestCheckpoint.payload?.page?.title || latestCheckpoint.payload?.device?.activity || '已采集'}}</span><small>{{latestCheckpoint.payload?.page?.url || latestCheckpoint.payload?.device?.package}}</small></div><div><b>断言</b><span :class="assertions.some(item=>item.passed===false)?'fail':'pass'">{{assertions.filter(item=>item.passed!==false).length}} / {{assertions.length}} 通过</span></div></div>
        <div class="observe-section"><h4>断言结果</h4><div v-for="(item,index) in assertions" :key="index" class="observe-item"><span :class="item.passed?'event-pass':'event-fail'">{{item.passed?'✓':'!'}}</span><div><b>{{item.type}}</b><small>期望 {{item.expected}} · 实际 {{item.actual ?? '未获取'}}</small><em v-if="item.error">{{item.error}}</em></div></div><p v-if="!assertions.length" class="empty compact">尚无断言检查点</p></div>
        <div class="observe-section"><h4>接口请求与响应</h4><div v-for="(item,index) in network.slice(-8).reverse()" :key="index" class="network-item"><span :class="Number(item.status_code)>=400?'event-fail':'event-pass'">{{item.status_code || 'REQ'}}</span><div><b>{{item.method}} {{item.url}}</b><pre v-if="item.response_body">{{item.response_body}}</pre></div></div><p v-if="!network.length" class="empty compact">当前检查点未采集到接口事件</p></div>
        <div class="observe-section"><h4>浏览器 / 设备日志</h4><div v-for="(item,index) in logs.slice(-8).reverse()" :key="index" class="log-item"><b>{{item.source}} · {{item.level}}</b><span>{{item.message}}</span></div><p v-if="!logs.length" class="empty compact">暂无错误日志</p></div>
        <div class="observe-section event-timeline"><h4>事件时间线</h4><div v-for="event in [...events].reverse().slice(0,12)" :key="event.id" class="timeline-item"><i :class="event.status"></i><div><b>#{{event.sequence}} {{event.summary}}</b><small>{{event.source}} · {{event.event_type}}<template v-if="event.step_no"> · 步骤 {{event.step_no}}</template></small></div></div><p v-if="!events.length" class="empty compact">会话开始后显示事件</p></div>
      </aside>
    </div>
    <p v-if="message" class="inline-message functional-message">{{message}}</p>
    <p v-if="!functionalCases.length" class="empty panel">请先到测试用例中心生成功能用例。</p>
  </section>
</template>
