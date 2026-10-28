/**
 * 聚合入口浏览器验收（可选开发工具，只读）。
 *
 * 前置：
 *   1) 飞控模拟器：PYTHONPATH=src python3 tools/cockpit_server.py --port 8794
 *   2) 任务下发 Agent：按其文档启动，监听 127.0.0.1:5055
 *   3) 聚合入口：PYTHONPATH=src python3 tools/agent_hub_server.py --port 8800 \
 *        --flight-ui-dir ../lingxi-flight-agent-ui/dist \
 *        --flight-api http://127.0.0.1:8794 --task-ui http://127.0.0.1:5055
 *
 * 用法：
 *   node tools/bench/agent_hub_e2e.mjs
 *   HUB_URL=http://127.0.0.1:8800/agent-hub/ SHOT_DIR=/tmp/hub-shots \
 *   CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
 *   node tools/bench/agent_hub_e2e.mjs
 *
 * 只验证页面聚合、懒加载、切换、深链和 HTTP 状态；不点击任何业务确认，
 * 不代表模型、任务下发或设备执行验收。
 */

import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const HUB_URL = process.env.HUB_URL || 'http://127.0.0.1:8800/agent-hub/'
const CDP_PORT = Number(process.env.CDP_PORT || 9333)
const SHOT_DIR = process.env.SHOT_DIR || join(tmpdir(), 'lingxi-agent-hub-e2e')
const TIMEOUT_MS = Number(process.env.TIMEOUT_MS || 30000)
const hub = new URL(HUB_URL)
const ORIGIN = hub.origin
const TASK_TITLE = process.env.TASK_TITLE || '灵犀任务下发助手 - 无人机巡检任务规划与下发'
const FLIGHT_TITLE = process.env.FLIGHT_TITLE || '灵犀飞控助手'

mkdirSync(SHOT_DIR, { recursive: true })

const chrome = spawn(CHROME, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${CDP_PORT}`,
  `--user-data-dir=${join(SHOT_DIR, 'chrome-profile')}`,
  '--window-size=1440,900', 'about:blank',
], { stdio: 'ignore' })

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function waitFor(fn, label, timeoutMs = TIMEOUT_MS) {
  const started = Date.now()
  let last
  while (Date.now() - started < timeoutMs) {
    try {
      const value = await fn()
      if (value) return value
    } catch (error) { last = error }
    await sleep(150)
  }
  throw new Error(`${label} 超时${last ? `：${last.message}` : ''}`)
}

let ws
try {
  const created = await waitFor(async () => {
    const response = await fetch(
      `http://127.0.0.1:${CDP_PORT}/json/new?${encodeURIComponent(HUB_URL)}`, { method: 'PUT' })
    return response.ok ? response.json() : null
  }, 'Chrome CDP 启动')

  ws = new WebSocket(created.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject })

  let nextId = 1
  const pending = new Map()
  const responses = []
  const consoleErrors = []
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data)
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id)
      pending.delete(message.id)
      message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result)
      return
    }
    if (message.method === 'Network.responseReceived') {
      const { url, status } = message.params.response
      if (url.startsWith(`${ORIGIN}/`)) responses.push({ url, status })
    }
    if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
      consoleErrors.push(message.params.args.map((arg) => arg.value ?? arg.description).join(' '))
    }
    if (message.method === 'Runtime.exceptionThrown') {
      const details = message.params.exceptionDetails
      consoleErrors.push(`${details.text} ${details.exception?.description || ''}`)
    }
  }

  function send(method, params = {}) {
    const id = nextId++
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject })
      ws.send(JSON.stringify({ id, method, params }))
    })
  }

  async function evaluate(expression) {
    const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text)
    return result.result.value
  }

  await send('Runtime.enable')
  await send('Page.enable')
  await send('Network.enable')
  await send('Page.navigate', { url: HUB_URL })

  const taskReady = await waitFor(async () => evaluate(`(() => {
    const frame = document.getElementById('view-task')
    if (!frame || !frame.classList.contains('is-active')) return null
    const doc = frame.contentDocument
    if (!doc || doc.readyState !== 'complete' || !doc.body) return null
    if (!doc.location.href.startsWith(${JSON.stringify(`${ORIGIN}/task-dispatch/`)})) return null
    if (!doc.title) return null
    return { title: doc.title, text: doc.body.innerText.replace(/\\s+/g, ' ').slice(0, 160), src: frame.src }
  })()`), '任务下发 iframe 加载')
  assert.equal(taskReady.title, TASK_TITLE)

  const flightBefore = await evaluate(`document.getElementById('view-flight').getAttribute('src')`)
  assert.equal(flightBefore, null, '飞控 iframe 不应在首次切换前请求')

  const tabState = await evaluate(`({
    task: document.getElementById('tab-task').getAttribute('aria-selected'),
    flight: document.getElementById('tab-flight').getAttribute('aria-selected')
  })`)
  assert.equal(tabState.task, 'true')
  assert.equal(tabState.flight, 'false')

  await evaluate(`document.getElementById('tab-flight').click()`)

  const flightReady = await waitFor(async () => evaluate(`(() => {
    const frame = document.getElementById('view-flight')
    if (!frame || !frame.classList.contains('is-active')) return null
    const doc = frame.contentDocument
    if (!doc || doc.readyState !== 'complete' || !doc.body) return null
    if (!doc.location.href.startsWith(${JSON.stringify(`${ORIGIN}/flight-agent-ui/`)})) return null
    if (!doc.title) return null
    return {
      title: doc.title,
      src: frame.src,
      hasLauncher: !!doc.querySelector('[data-testid="flight-chat-launcher"]'),
    }
  })()`), '飞控 iframe 加载')
  assert.equal(flightReady.title, FLIGHT_TITLE)
  assert.equal(flightReady.hasLauncher, true)

  // 飞控助手默认收起气泡；点开后才会建立会话并请求 /context。
  await evaluate(`(() => {
    const doc = document.getElementById('view-flight').contentDocument
    const launcher = doc.querySelector('[data-testid="flight-chat-launcher"]')
    if (!launcher) throw new Error('找不到飞控气泡按钮')
    launcher.click()
    return true
  })()`)
  await waitFor(async () => evaluate(`(() => {
    const doc = document.getElementById('view-flight').contentDocument
    return doc.querySelector('[data-testid="flight-chat-dialog"]') ? true : null
  })()`), '飞控对话窗口打开')
  await sleep(1200)

  const flightState = await evaluate(`({
    taskSelected: document.getElementById('tab-task').getAttribute('aria-selected'),
    flightSelected: document.getElementById('tab-flight').getAttribute('aria-selected'),
    view: new URL(location.href).searchParams.get('view'),
    taskStillLoaded: document.getElementById('view-task').dataset.loaded,
    external: document.getElementById('hub-external').href
  })`)
  assert.equal(flightState.taskSelected, 'false')
  assert.equal(flightState.flightSelected, 'true')
  assert.equal(flightState.view, 'flight')
  assert.equal(flightState.taskStillLoaded, '1')
  assert.ok(flightState.external.includes('/flight-agent-ui/'))

  const apiFailures = responses.filter(({ url, status }) => url.includes('/api/') && status >= 400)
  const flightContext = responses.find(({ url }) => url.includes('/api/flight-control/context'))
  const taskCalls = responses.filter(({ url, status }) =>
    url.includes('/api/') && !url.includes('/api/flight-control') && status === 200)
  assert.ok(flightContext, '飞控 iframe 应请求 /api/flight-control/context')
  assert.equal(flightContext.status, 200)
  assert.ok(taskCalls.length > 0, `任务下发 iframe 应有成功的 /api 请求；实际 ${JSON.stringify(responses)}`)
  assert.deepEqual(apiFailures, [], '不应有 4xx/5xx API 响应')

  const flightShot = await send('Page.captureScreenshot', { format: 'png' })
  const flightShotPath = join(SHOT_DIR, 'hub-flight.png')
  writeFileSync(flightShotPath, Buffer.from(flightShot.data, 'base64'))

  // 切回任务下发：隐藏的 iframe 不重载，会话保留。
  await evaluate(`document.getElementById('tab-task').click()`)
  await sleep(800)
  const backState = await evaluate(`({
    taskSelected: document.getElementById('tab-task').getAttribute('aria-selected'),
    taskDocTitle: document.getElementById('view-task').contentDocument.title,
    flightStillLoaded: document.getElementById('view-flight').dataset.loaded,
    view: new URL(location.href).searchParams.get('view')
  })`)
  assert.equal(backState.taskSelected, 'true')
  assert.equal(backState.taskDocTitle, TASK_TITLE)
  assert.equal(backState.flightStillLoaded, '1')
  assert.equal(backState.view, 'task')
  const taskShot = await send('Page.captureScreenshot', { format: 'png' })
  const taskShotPath = join(SHOT_DIR, 'hub-task.png')
  writeFileSync(taskShotPath, Buffer.from(taskShot.data, 'base64'))

  // 深链 ?view=flight 直接激活飞控，任务页仍按需加载。
  await send('Page.navigate', { url: `${HUB_URL}?view=flight` })
  await sleep(2500)
  const deepLink = await evaluate(`({
    flightSelected: document.getElementById('tab-flight').getAttribute('aria-selected'),
    flightLoaded: document.getElementById('view-flight').dataset.loaded || null,
    taskLoaded: document.getElementById('view-task').dataset.loaded || null,
    flightActive: document.getElementById('view-flight').classList.contains('is-active')
  })`)
  assert.equal(deepLink.flightSelected, 'true')
  assert.equal(deepLink.flightActive, true)
  assert.equal(deepLink.flightLoaded, '1')
  assert.equal(deepLink.taskLoaded, null)

  console.log(JSON.stringify({
    hub: HUB_URL,
    task: taskReady,
    flight: flightReady,
    flightState,
    backState,
    deepLink,
    apiCalls: responses.filter(({ url }) => url.includes('/api/')),
    consoleErrors,
    screenshots: { flight: flightShotPath, task: taskShotPath },
  }, null, 2))
} finally {
  try { ws?.close() } catch { /* 已关闭 */ }
  chrome.kill('SIGTERM')
}
