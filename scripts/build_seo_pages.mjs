#!/usr/bin/env node
// Regenerate the crawlable SEO pages as part of the web build.
//
// The production host serves a pre-rendered page tree from disk, so the pages have
// to be rebuilt whenever the dataset changes. Hooking this into `npm run build`
// (see web/package.json) means the existing deploy pipeline produces them without
// touching the root-owned updater script.
//
// It is deliberately forgiving:
//   * no dataset database  -> skip (local checkouts, CI, Windows) and still exit 0
//   * generator failure    -> log loudly and exit 0, so a page-generation problem
//                             never blocks shipping the app itself
//   * broken output        -> the generator keeps the previous page tree intact
import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const generator = resolve(repositoryRoot, 'scripts', 'gen_seo_pages.py')
const outDir = process.env.OIP_SEO_OUT || '/var/www/oip-pages'
const site = (process.env.OIP_SEO_SITE || 'https://openimages.relakkesyang.org').replace(/\/$/, '')

// The IndexNow key is whatever `<hex>.txt` file the build published, so the
// key lives in git next to robots.txt and nothing has to configure it.
// IndexNow accepts 8-128 hex characters, so do not assume a 32-char UUID: a
// stricter pattern silently skipped every submission for a 40-char key.
function indexNowKey() {
  if (process.env.OIP_INDEXNOW_KEY) return process.env.OIP_INDEXNOW_KEY
  const publicDir = resolve(repositoryRoot, 'web', 'public')
  try {
    const candidate = readdirSync(publicDir).find((name) => /^[0-9a-f]{8,128}\.txt$/.test(name))
    return candidate ? candidate.replace(/\.txt$/, '') : null
  } catch {
    return null
  }
}

async function submitToIndexNow(directory) {
  try {
    if (process.env.OIP_INDEXNOW === '0') {
      console.log('seo-pages: IndexNow disabled by OIP_INDEXNOW=0')
      return
    }
    const key = indexNowKey()
    if (!key) {
      console.warn('seo-pages: no IndexNow key file in web/public; skipping submission')
      return
    }
    let changed = []
    try {
      changed = JSON.parse(readFileSync(resolve(directory, '.seo-changed.json'), 'utf8')).urls || []
    } catch {
      console.log('seo-pages: no .seo-changed.json sidecar; nothing to submit')
      return
    }
    if (!changed.length) {
      console.log('seo-pages: no changed URLs this run; nothing to submit')
      return
    }
    const host = new URL(site).host
    const body = JSON.stringify({
      host,
      key,
      keyLocation: `${site}/${key}.txt`,
      urlList: changed.slice(0, 10000),
    })
    const response = await fetch('https://api.indexnow.org/indexnow', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body,
    })
    console.log(`seo-pages: submitted ${changed.length} URL(s) to IndexNow (HTTP ${response.status})`)
  } catch (error) {
    console.warn(`seo-pages: IndexNow submission failed: ${error.message}`)
  }
}

// --- static content for the SPA shell ---------------------------------------
//
// `/` is client-rendered, so its HTML carried no text and no links at all: Google
// crawled it three times and reported "crawled — currently not indexed", and a
// crawler had no way to discover anything except the sitemap. The generator emits
// this block; here it is spliced into the built shell so the root page has real
// content and real links before (and without) JavaScript. React replaces it on
// mount, so the gallery is unaffected.
const HOME_START = '<!--oip-static-home-->'
const HOME_END = '<!--/oip-static-home-->'
const HEAD_START = '<!--oip-static-head-->'
const HEAD_END = '<!--/oip-static-head-->'

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function staticHead() {
  const structured = JSON.stringify({
    '@context': 'https://schema.org',
    '@type': 'WebSite',
    name: 'Open Image Prompts',
    alternateName: 'AI 提示词档案',
    url: `${site}/`,
    description: '浏览好看的 AI 图片，查看并复制生成它们的提示词。',
    inLanguage: ['zh-Hans', 'en'],
  })
  return [
    HEAD_START,
    `<link rel="canonical" href="${site}/">`,
    '<meta property="og:type" content="website">',
    `<meta property="og:url" content="${site}/">`,
    '<meta property="og:title" content="Open Image Prompts — 看好图，复制好提示词">',
    `<script type="application/ld+json">${structured}</script>`,
    HEAD_END,
  ].join('\n')
}

function injectStaticHome(fragmentPath) {
  if (process.env.OIP_SEO_HOME_INJECT === '0') {
    console.log('seo-pages: homepage injection disabled by OIP_SEO_HOME_INJECT=0')
    return
  }
  const indexFile = resolve(repositoryRoot, 'web', 'dist', 'index.html')
  if (!existsSync(indexFile)) {
    console.log('seo-pages: web/dist/index.html not found; skipping homepage injection')
    return
  }
  let fragment = ''
  try {
    fragment = readFileSync(fragmentPath, 'utf8').trim()
  } catch {
    fragment = ''
  }
  if (!fragment) {
    console.warn('seo-pages: generator produced no homepage fragment; leaving the shell untouched')
    return
  }
  let html = readFileSync(indexFile, 'utf8')
  // Idempotent: drop any copy of the markers from an earlier run before re-adding.
  for (const [start, end] of [
    [HOME_START, HOME_END],
    [HEAD_START, HEAD_END],
  ]) {
    html = html.replace(new RegExp(`${escapeRegExp(start)}[\\s\\S]*?${escapeRegExp(end)}`, 'g'), '')
  }
  html = html.replace('</head>', `${staticHead()}\n</head>`)
  const block = `${HOME_START}${fragment}${HOME_END}`
  if (html.includes('<div id="root"></div>')) {
    html = html.replace('<div id="root"></div>', `<div id="root">${block}</div>`)
  } else if (html.includes('<div id="root">')) {
    html = html.replace('<div id="root">', `<div id="root">${block}`)
  } else {
    console.warn('seo-pages: no <div id="root"> in web/dist/index.html; skipping homepage injection')
    return
  }
  writeFileSync(indexFile, html)
  console.log(
    `seo-pages: injected ${fragment.length} bytes of crawlable home content into web/dist/index.html`,
  )
}

function newestStagedDatabase() {
  const stateDir = '/var/lib/open-image-prompts'
  if (!existsSync(stateDir)) return null
  let staged = []
  try {
    staged = readdirSync(stateDir)
      .filter((name) => name.startsWith('.staging-'))
      .map((name) => resolve(stateDir, name, 'prompts.db'))
      .filter((candidate) => existsSync(candidate))
  } catch {
    staged = []
  }
  // A staged database is the dataset this deploy is about to publish.
  if (staged.length) return staged.sort().at(-1)
  const deployed = resolve(stateDir, 'prompts.db')
  return existsSync(deployed) ? deployed : null
}

function datasetPath() {
  const configured = [process.env.OIP_SEO_DB, process.env.OIP_DB_PATH]
  for (const candidate of configured) {
    if (candidate && existsSync(candidate)) return candidate
  }
  return (
    newestStagedDatabase() ||
    [resolve(repositoryRoot, 'db', 'prompts.db'), resolve(repositoryRoot, '.oip', 'runtime', 'prompts.db')].find(
      (candidate) => existsSync(candidate),
    ) ||
    null
  )
}

const database = datasetPath()
if (!database) {
  console.log('seo-pages: no dataset found; skipping pre-rendered page generation')
  process.exit(0)
}

const python = process.env.OIP_PYTHON || 'python3'
// The generator also produces the static content that keeps the domain root from
// being an empty SPA shell. It lands in a temp file and is injected below, because
// only the web build owns web/dist/index.html.
const fragmentDir = mkdtempSync(join(tmpdir(), 'oip-seo-'))
const fragmentPath = join(fragmentDir, 'home-fragment.html')
const args = [generator, '--db', database, '--out', outDir, '--home-fragment', fragmentPath]
if (process.env.OIP_SEO_INDEX_LIMIT) args.push('--index-limit', process.env.OIP_SEO_INDEX_LIMIT)
if (process.env.OIP_SEO_LIMIT) args.push('--limit', process.env.OIP_SEO_LIMIT)

const result = spawnSync(python, args, { stdio: 'inherit' })
if (result.error) {
  console.warn(`seo-pages: could not run ${python}: ${result.error.message}`)
  rmSync(fragmentDir, { force: true, recursive: true })
  process.exit(0)
}
if (result.status !== 0) {
  console.warn(`seo-pages: generation failed (exit ${result.status}); keeping the previous page tree`)
  rmSync(fragmentDir, { force: true, recursive: true })
  process.exit(process.env.OIP_SEO_STRICT === '1' ? result.status : 0)
}

injectStaticHome(fragmentPath)
rmSync(fragmentDir, { force: true, recursive: true })

// Ping IndexNow for the pages this run actually wrote. Bing/Yandex/Seznam consume
// it; Google does not, and instead picks pages up through the sitemap. The key is
// public by design: it is the file we serve at /<key>.txt.
await submitToIndexNow(outDir)
process.exit(0)
