#!/usr/bin/env node
// 画廊启动器：把画廊服务静默拉起来，记下 pid 和状态，等它就绪后自己退出。
// 画廊服务与本进程解耦（detached + 独立日志 fd），所以本进程退出、控制台退出、
// 浏览器关掉都不影响画廊。供「开机自启」的 VBS 和控制台面板共用。
import { spawn } from 'node:child_process'
import { createConnection } from 'node:net'
import {
  closeSync, existsSync, mkdirSync, openSync, readFileSync, renameSync, statSync, writeFileSync,
} from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import http from 'node:http'

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const HOST = '127.0.0.1'
const PORT = Number(process.env.OIP_GALLERY_PORT || 5173)
const DAEMON_DIR = resolve(REPO, '.oip', 'daemon')
const LOG_PATH = resolve(DAEMON_DIR, 'service.log')
const STATE_PATH = resolve(DAEMON_DIR, 'gallery.json')
const LOG_LIMIT_BYTES = 20 * 1024 * 1024
const READY_TIMEOUT_MS = Number(process.env.OIP_GALLERY_READY_TIMEOUT || 90_000)
const URL = `http://${HOST}:${PORT}/`

function ensureDir() {
  mkdirSync(DAEMON_DIR, { recursive: true })
}

function readState() {
  try {
    return JSON.parse(readFileSync(STATE_PATH, 'utf8'))
  } catch {
    return null
  }
}

function writeState(patch) {
  ensureDir()
  const next = { ...(readState() || {}), ...patch }
  writeFileSync(STATE_PATH, `${JSON.stringify(next, null, 2)}\n`, 'utf8')
  return next
}

// 只认本项目的健康响应：别的程序也可能在 /health 返回 {ok:true}。
function probeHealth(timeoutMs = 1500) {
  return new Promise((resolvePromise) => {
    const request = http.get({ host: HOST, port: PORT, path: '/health', timeout: timeoutMs }, (response) => {
      let body = ''
      response.setEncoding('utf8')
      response.on('data', (chunk) => { body += chunk })
      response.on('end', () => {
        try {
          const payload = JSON.parse(body)
          resolvePromise(response.statusCode === 200 && payload?.ok === true && 'query_concurrency' in payload)
        } catch {
          resolvePromise(false)
        }
      })
    })
    request.on('timeout', () => request.destroy())
    request.on('error', () => resolvePromise(false))
  })
}

function portIsListening() {
  return new Promise((resolvePromise) => {
    const socket = createConnection({ host: HOST, port: PORT })
    const finish = (value) => { socket.destroy(); resolvePromise(value) }
    socket.setTimeout(800, () => finish(false))
    socket.once('connect', () => finish(true))
    socket.once('error', () => finish(false))
  })
}

function rotateLog() {
  try {
    if (existsSync(LOG_PATH) && statSync(LOG_PATH).size > LOG_LIMIT_BYTES) {
      renameSync(LOG_PATH, `${LOG_PATH}.1`)
    }
  } catch {
    // 轮转失败不阻塞启动
  }
}

function tailLog(lines = 12) {
  try {
    const raw = readFileSync(LOG_PATH, 'utf8')
    return raw.split(/\r?\n/).filter(Boolean).slice(-lines)
  } catch {
    return []
  }
}

function errorLineFromLog() {
  const interesting = tailLog(60).filter((line) => /error|EADDRINUSE|in use|failed|Traceback/i.test(line))
  return interesting.at(-1)?.trim().slice(0, 300) || null
}

async function waitUntilReady(child) {
  const deadline = Date.now() + READY_TIMEOUT_MS
  let exited = null
  child.once('exit', (code, signal) => { exited = { code, signal } })
  while (Date.now() < deadline) {
    if (await probeHealth()) return { ok: true }
    if (exited) {
      return { ok: false, error: `服务进程启动后立即退出（code=${exited.code}）`, detail: errorLineFromLog() }
    }
    await new Promise((done) => setTimeout(done, 250))
  }
  return { ok: false, error: `等待服务就绪超时（${Math.round(READY_TIMEOUT_MS / 1000)} 秒）`, detail: errorLineFromLog() }
}

async function main() {
  ensureDir()

  if (await probeHealth()) {
    const previous = readState()
    // 已经在跑（可能是手动 start.bat 起的实例）：不重复启动，也不覆盖启动时间。
    const state = writeState({
      port: PORT,
      running: true,
      lastStart: { ok: true, at: new Date().toISOString(), message: '服务已在运行，未重复启动' },
      ...(previous?.pid ? {} : { pid: null, external: true }),
    })
    return { ok: true, alreadyRunning: true, pid: state.pid ?? null, url: URL }
  }

  if (await portIsListening()) {
    const detail = errorLineFromLog()
    writeState({
      port: PORT,
      running: false,
      pid: null,
      lastStart: { ok: false, at: new Date().toISOString(), message: `端口 ${PORT} 被其他程序占用` },
    })
    return { ok: false, error: `端口 ${PORT} 被其他程序占用`, detail }
  }

  rotateLog()
  const logFd = openSync(LOG_PATH, 'a')
  writeFileSync(
    LOG_PATH,
    `\n===== ${new Date().toISOString()} 启动画廊（端口 ${PORT}）=====\n`,
    { flag: 'a' },
  )

  const child = spawn(process.execPath, [resolve(REPO, 'web', 'scripts', 'with_api.mjs'), 'dev'], {
    cwd: REPO,
    env: {
      ...process.env,
      OIP_WEB_HOST: HOST,
      OIP_WEB_PORT: String(PORT),
      // 子进程若再拉起别的 node，不要继承我们的启动器上下文
      OIP_GALLERY_LAUNCHED_BY: 'oip-daemon',
    },
    detached: true,
    windowsHide: true,
    stdio: ['ignore', logFd, logFd],
  })
  closeSync(logFd)
  child.unref()

  const startedAt = new Date().toISOString()
  writeState({
    port: PORT,
    pid: child.pid ?? null,
    startedAt,
    running: true,
    external: false,
    logPath: LOG_PATH,
    lastStart: { ok: null, at: startedAt, message: '正在启动…' },
  })

  const ready = await waitUntilReady(child)
  if (!ready.ok) {
    writeState({
      running: false,
      pid: null,
      lastStart: { ok: false, at: new Date().toISOString(), message: ready.error, detail: ready.detail },
    })
    return { ok: false, error: ready.error, detail: ready.detail, logPath: LOG_PATH }
  }

  writeState({
    pid: child.pid ?? null,
    running: true,
    lastStart: { ok: true, at: new Date().toISOString(), message: '启动完成' },
  })
  return { ok: true, pid: child.pid ?? null, url: URL }
}

main()
  .then((result) => {
    process.stdout.write(`${JSON.stringify(result)}\n`)
    process.exitCode = result.ok ? 0 : 1
  })
  .catch((error) => {
    process.stdout.write(`${JSON.stringify({ ok: false, error: error?.message || String(error) })}\n`)
    process.exitCode = 1
  })