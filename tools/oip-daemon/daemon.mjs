#!/usr/bin/env node
// OIP 画廊控制台：一个常驻的本地小服务，只做四件事——
// 读画廊状态、启停画廊、开关开机自启、读日志。
// 它不拥有画廊进程：画廊由 gallery-service.mjs 以独立进程拉起，
// 控制台随时退出都不影响画廊运行。
import http from 'node:http'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import fs from 'node:fs'
import net from 'node:net'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const execFileAsync = promisify(execFile)

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '..', '..')
const HOST = '127.0.0.1'
const GALLERY_PORT = Number(process.env.OIP_GALLERY_PORT || 5173)
const CONSOLE_PORT_START = Number(process.env.OIP_CONSOLE_PORT || 47825)
const CONSOLE_PORT_END = CONSOLE_PORT_START + 9

const DAEMON_DIR = path.join(REPO, '.oip', 'daemon')
const STATE_PATH = path.join(DAEMON_DIR, 'gallery.json')
const PORT_FILE = path.join(DAEMON_DIR, 'console-port.txt')
const CONSOLE_LOG = path.join(DAEMON_DIR, 'console.log')
const SERVICE_LOG = path.join(DAEMON_DIR, 'service.log')
const LAUNCHER = path.join(HERE, 'gallery-service.mjs')
const PANEL = path.join(HERE, 'console.html')

const STARTUP_DIR = path.join(
  process.env.APPDATA || '',
  'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup',
)
const AUTOSTART_VBS = path.join(STARTUP_DIR, 'OIP 画廊服务.vbs')

fs.mkdirSync(DAEMON_DIR, { recursive: true })

// ---------- 小工具 ----------

function log(message) {
  const line = `[${new Date().toISOString()}] ${message}\n`
  try {
    fs.appendFileSync(CONSOLE_LOG, line, 'utf8')
  } catch {
    // 写日志失败不影响主流程
  }
}

function readJson(file, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return fallback
  }
}

function readGalleryState() {
  return readJson(STATE_PATH)
}

function writeGalleryState(patch) {
  const next = { ...(readGalleryState() || {}), ...patch }
  fs.writeFileSync(STATE_PATH, `${JSON.stringify(next, null, 2)}\n`, 'utf8')
  return next
}

function probeHealth(port = GALLERY_PORT, timeoutMs = 1200) {
  return new Promise((resolvePromise) => {
    const request = http.get({ host: HOST, port, path: '/health', timeout: timeoutMs }, (response) => {
      let body = ''
      response.setEncoding('utf8')
      response.on('data', (chunk) => { body += chunk })
      response.on('end', () => {
        try {
          const payload = JSON.parse(body)
          resolvePromise({
            ok: response.statusCode === 200 && payload?.ok === true && 'query_concurrency' in payload,
            payload,
          })
        } catch {
          resolvePromise({ ok: false, payload: null })
        }
      })
    })
    request.on('timeout', () => { request.destroy() })
    request.on('error', () => resolvePromise({ ok: false, payload: null }))
  })
}

function isPortListening(port) {
  return new Promise((resolvePromise) => {
    const socket = net.createConnection({ host: HOST, port })
    const finish = (value) => { socket.destroy(); resolvePromise(value) }
    socket.setTimeout(700, () => finish(false))
    socket.once('connect', () => finish(true))
    socket.once('error', () => finish(false))
  })
}

function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

async function processCommandLine(pid) {
  try {
    const { stdout } = await execFileAsync(
      'powershell',
      ['-NoLogo', '-NoProfile', '-Command', `(Get-CimInstance Win32_Process -Filter "ProcessId=${pid}").CommandLine`],
      { windowsHide: true, timeout: 5000 },
    )
    return stdout.trim()
  } catch {
    return ''
  }
}

async function pidOnPort(port) {
  try {
    const { stdout } = await execFileAsync('netstat', ['-ano'], { windowsHide: true, timeout: 5000 })
    const match = new RegExp(`TCP\\s+127\\.0\\.0\\.1:${port}\\s+\\S+\\s+LISTENING\\s+(\\d+)`).exec(stdout)
    return match ? Number(match[1]) : null
  } catch {
    return null
  }
}

async function killTree(pid) {
  await execFileAsync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, timeout: 10000 })
}

async function waitPortClosed(port, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (!(await isPortListening(port))) return true
    await new Promise((done) => setTimeout(done, 200))
  }
  return false
}

function tailLines(file, count) {
  try {
    const stat = fs.statSync(file)
    const windowBytes = Math.min(stat.size, 96 * 1024)
    const buffer = Buffer.alloc(windowBytes)
    const fd = fs.openSync(file, 'r')
    fs.readSync(fd, buffer, 0, windowBytes, stat.size - windowBytes)
    fs.closeSync(fd)
    const text = buffer.toString('utf8')
    return text.split(/\r?\n/).slice(-count)
  } catch {
    return []
  }
}

// ---------- 画廊状态 ----------

async function galleryStatus() {
  const state = readGalleryState() || {}
  const health = await probeHealth()
  const listening = await isPortListening(GALLERY_PORT)
  const startedAt = state.startedAt || null
  const uptimeSec = health.ok && startedAt
    ? Math.max(0, Math.round((Date.now() - Date.parse(startedAt)) / 1000))
    : 0

  let phase = 'stopped'
  if (health.ok) phase = 'running'
  else if (listening) phase = 'blocked'
  else if (state.lastStart?.ok === false) phase = 'error'
  else if (state.lastStart?.ok === null) phase = 'starting'

  return {
    phase,
    url: `http://${HOST}:${GALLERY_PORT}/`,
    port: GALLERY_PORT,
    pid: health.ok ? (state.pid ?? null) : null,
    external: Boolean(state.external) || (health.ok && !state.pid),
    startedAt,
    uptimeSec,
    health: health.ok ? { ok: true, ...health.payload } : { ok: false },
    lastStart: state.lastStart || null,
    logPath: SERVICE_LOG,
  }
}

async function startGallery() {
  try {
    const { stdout } = await execFileAsync(process.execPath, [LAUNCHER], {
      cwd: REPO,
      windowsHide: true,
      timeout: 120_000,
      maxBuffer: 1024 * 1024,
    })
    const line = stdout.trim().split(/\r?\n/).filter(Boolean).at(-1) || '{}'
    log(`启动画廊 → ${line}`)
    return JSON.parse(line)
  } catch (error) {
    const stdout = String(error?.stdout || '').trim().split(/\r?\n/).filter(Boolean).at(-1)
    if (stdout) {
      try {
        return JSON.parse(stdout)
      } catch {
        // 落到下面的通用错误
      }
    }
    log(`启动画廊失败 → ${error?.message || error}`)
    return { ok: false, error: error?.message || '启动失败' }
  }
}

async function stopGallery() {
  const state = readGalleryState() || {}
  const health = await probeHealth()

  let pid = null
  let source = ''
  if (Number.isInteger(state.pid) && processAlive(state.pid)) {
    const commandLine = await processCommandLine(state.pid)
    if (commandLine.includes('with_api.mjs')) {
      pid = state.pid
      source = '记录'
    }
  }

  if (!pid && health.ok) {
    const portPid = await pidOnPort(GALLERY_PORT)
    if (portPid) {
      const commandLine = await processCommandLine(portPid)
      if (commandLine.includes('with_api.mjs')) {
        pid = portPid
        source = '端口'
      }
    }
  }

  if (!pid) {
    if (!health.ok && !(await isPortListening(GALLERY_PORT))) {
      writeGalleryState({ running: false, pid: null, startedAt: null, lastStop: { at: new Date().toISOString(), message: '服务本来就是停止状态' } })
      return { ok: true, alreadyStopped: true }
    }
    return {
      ok: false,
      error: `端口 ${GALLERY_PORT} 上的进程不是本项目服务，出于安全没有终止它。请手动处理占用端口的程序。`,
    }
  }

  try {
    await killTree(pid)
    log(`停止画廊（pid=${pid}，来源=${source}）`)
  } catch (error) {
    const message = `终止进程失败：${error?.message || error}`
    log(message)
    return { ok: false, error: message }
  }

  const closed = await waitPortClosed(GALLERY_PORT)
  writeGalleryState({
    running: false,
    pid: null,
    startedAt: null,
    lastStop: { at: new Date().toISOString(), message: closed ? `已停止（pid ${pid}）` : '进程已终止但端口仍被占用' },
  })
  if (!closed) return { ok: false, error: `进程已终止，但 ${GALLERY_PORT} 端口仍被占用` }
  return { ok: true, stopped: true, pid }
}

// ---------- 开机自启 ----------

function autostartInfo() {
  const exists = fs.existsSync(AUTOSTART_VBS)
  return { enabled: exists, path: AUTOSTART_VBS, folder: STARTUP_DIR }
}

function autostartScript() {
  // VBScript 字符串里的引号只能靠 Chr(34) 拼，直接写 """ 会编译不过
  const literal = (value) => `"${value}"`
  return [
    "' OIP 画廊服务 —— 登录 Windows 后静默启动，不弹窗口。",
    "' 取消自启：删除本文件，或在控制台里关掉「开机自动启动」开关。",
    'Option Explicit',
    'Dim sh, q',
    'Set sh = CreateObject("WScript.Shell")',
    'q = Chr(34)',
    'WScript.Sleep 15000',
    `sh.CurrentDirectory = ${literal(REPO)}`,
    `sh.Run q & ${literal(process.execPath)} & q & " " & q & ${literal(LAUNCHER)} & q, 0, False`,
    '',
  ].join('\r\n')
}

function setAutostart(enabled) {
  if (enabled) {
    fs.mkdirSync(STARTUP_DIR, { recursive: true })
    // 启动项必须是 UTF-16LE + BOM，wscript 才能正确读取中文注释
    fs.writeFileSync(AUTOSTART_VBS, `\ufeff${autostartScript()}`, { encoding: 'utf16le' })
    log(`已开启开机自启 → ${AUTOSTART_VBS}`)
  } else if (fs.existsSync(AUTOSTART_VBS)) {
    fs.unlinkSync(AUTOSTART_VBS)
    log(`已关闭开机自启 → 删除 ${AUTOSTART_VBS}`)
  }
  return autostartInfo()
}

// ---------- HTTP ----------

function sendJson(response, status, value) {
  const body = Buffer.from(JSON.stringify(value), 'utf8')
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
  })
  response.end(body)
}

function readBody(request) {
  return new Promise((resolvePromise, reject) => {
    let size = 0
    const chunks = []
    request.on('data', (chunk) => {
      size += chunk.length
      if (size > 64 * 1024) {
        reject(new Error('请求体过大'))
        request.destroy()
        return
      }
      chunks.push(chunk)
    })
    request.on('end', () => {
      try {
        resolvePromise(chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {})
      } catch {
        reject(new Error('请求体不是合法 JSON'))
      }
    })
    request.on('error', reject)
  })
}

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url || '/', `http://${HOST}`)
  const pathname = url.pathname

  try {
    if (pathname === '/' || pathname === '/index.html') {
      const html = fs.readFileSync(PANEL)
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' })
      response.end(html)
      return
    }

    if (pathname === '/api/health') {
      sendJson(response, 200, { ok: true, console: true, port: currentPort })
      return
    }

    if (pathname === '/api/status' && request.method === 'GET') {
      sendJson(response, 200, {
        gallery: await galleryStatus(),
        autostart: autostartInfo(),
        console: {
          port: currentPort,
          pid: process.pid,
          startedAt: consoleStartedAt,
          repo: REPO,
          node: process.execPath,
        },
      })
      return
    }

    if (pathname === '/api/logs' && request.method === 'GET') {
      const count = Math.min(1000, Math.max(10, Number(url.searchParams.get('tail')) || 200))
      const lines = tailLines(SERVICE_LOG, count)
      let size = 0
      try { size = fs.statSync(SERVICE_LOG).size } catch { size = 0 }
      sendJson(response, 200, { ok: true, lines, path: SERVICE_LOG, size })
      return
    }

    if (request.method === 'POST') {
      // 只接受本页面发来的请求：自定义头会触发跨源预检，而本服务不回应 CORS，
      // 所以别的网页无法悄悄调用这些接口。
      if (request.headers['x-oip-console'] !== '1') {
        sendJson(response, 403, { ok: false, error: '缺少本地控制台请求标识' })
        return
      }
      const body = await readBody(request)

      if (pathname === '/api/gallery/start') {
        sendJson(response, 200, await startGallery())
        return
      }

      if (pathname === '/api/gallery/stop') {
        sendJson(response, 200, await stopGallery())
        return
      }

      if (pathname === '/api/gallery/restart') {
        const stopped = await stopGallery()
        if (!stopped.ok) {
          sendJson(response, 200, stopped)
          return
        }
        await new Promise((done) => setTimeout(done, 600))
        sendJson(response, 200, await startGallery())
        return
      }

      if (pathname === '/api/autostart') {
        const enabled = body?.enabled === true
        sendJson(response, 200, { ok: true, ...setAutostart(enabled) })
        return
      }

      if (pathname === '/api/console/quit') {
        sendJson(response, 200, { ok: true })
        log('控制台退出（画廊不受影响）')
        setTimeout(() => {
          server.close(() => process.exit(0))
          setTimeout(() => process.exit(0), 1500).unref()
        }, 120)
        return
      }

      sendJson(response, 404, { ok: false, error: '未知接口' })
      return
    }

    sendJson(response, 404, { ok: false, error: '未知路径' })
  } catch (error) {
    log(`请求出错 ${pathname} → ${error?.message || error}`)
    sendJson(response, 500, { ok: false, error: error?.message || '内部错误' })
  }
})

// ---------- 启动 ----------

const consoleStartedAt = new Date().toISOString()
let currentPort = 0

async function bindPort(port) {
  return new Promise((resolvePromise) => {
    const onError = (error) => {
      server.removeListener('listening', onListening)
      resolvePromise(error?.code || 'error')
    }
    const onListening = () => {
      server.removeListener('error', onError)
      resolvePromise(null)
    }
    server.once('error', onError)
    server.once('listening', onListening)
    server.listen(port, HOST)
  })
}

async function main() {
  for (let port = CONSOLE_PORT_START; port <= CONSOLE_PORT_END; port += 1) {
    const occupied = await new Promise((resolvePromise) => {
      const probe = net.createConnection({ host: HOST, port })
      probe.setTimeout(400, () => { probe.destroy(); resolvePromise(false) })
      probe.once('connect', () => { probe.destroy(); resolvePromise(true) })
      probe.once('error', () => resolvePromise(false))
    })
    if (occupied) {
      // 如果占用者就是另一个控制台，本次不重复启动
      const health = await new Promise((resolvePromise) => {
        const request = http.get({ host: HOST, port, path: '/api/health', timeout: 800 }, (res) => {
          let body = ''
          res.setEncoding('utf8')
          res.on('data', (chunk) => { body += chunk })
          res.on('end', () => {
            try { resolvePromise(JSON.parse(body)?.console === true) } catch { resolvePromise(false) }
          })
        })
        request.on('timeout', () => request.destroy())
        request.on('error', () => resolvePromise(false))
      })
      if (health) {
        process.stdout.write(`${JSON.stringify({ ok: true, alreadyRunning: true, port })}\n`)
        return
      }
      continue
    }
    const failure = await bindPort(port)
    if (!failure) {
      currentPort = port
      break
    }
    if (failure !== 'EADDRINUSE') {
      process.stdout.write(`${JSON.stringify({ ok: false, error: `控制台端口 ${port} 绑定失败：${failure}` })}\n`)
      process.exitCode = 1
      return
    }
  }

  if (!currentPort) {
    process.stdout.write(`${JSON.stringify({ ok: false, error: '找不到可用端口（47825-47834）' })}\n`)
    process.exitCode = 1
    return
  }

  fs.writeFileSync(PORT_FILE, String(currentPort), 'utf8')
  log(`控制台启动：${HOST}:${currentPort}（pid ${process.pid}）`)
  process.stdout.write(`${JSON.stringify({ ok: true, port: currentPort })}\n`)
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    log('收到退出信号')
    process.exit(0)
  })
}

main().catch((error) => {
  log(`控制台启动失败：${error?.stack || error}`)
  process.stdout.write(`${JSON.stringify({ ok: false, error: error?.message || String(error) })}\n`)
  process.exitCode = 1
})