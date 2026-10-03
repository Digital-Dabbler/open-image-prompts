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
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const generator = resolve(repositoryRoot, 'scripts', 'gen_seo_pages.py')
const outDir = process.env.OIP_SEO_OUT || '/var/www/oip-pages'
const site = (process.env.OIP_SEO_SITE || 'https://openimages.relakkesyang.org').replace(/\/$/, '')

// The IndexNow key is whatever `<32 hex>.txt` file the build published, so the
// key lives in git next to robots.txt and nothing has to configure it.
function indexNowKey() {
  if (process.env.OIP_INDEXNOW_KEY) return process.env.OIP_INDEXNOW_KEY
  const publicDir = resolve(repositoryRoot, 'web', 'public')
  try {
    const candidate = readdirSync(publicDir).find((name) => /^[0-9a-f]{32}\.txt$/.test(name))
    return candidate ? candidate.replace(/\.txt$/, '') : null
  } catch {
    return null
  }
}

async function submitToIndexNow(directory) {
  if (process.env.OIP_INDEXNOW === '0') return
  const key = indexNowKey()
  if (!key) return
  let changed = []
  try {
    changed = JSON.parse(readFileSync(resolve(directory, '.seo-changed.json'), 'utf8')).urls || []
  } catch {
    return
  }
  if (!changed.length) return
  const host = new URL(site).host
  const body = JSON.stringify({
    host,
    key,
    keyLocation: `${site}/${key}.txt`,
    urlList: changed.slice(0, 10000),
  })
  try {
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
const args = [generator, '--db', database, '--out', outDir]
if (process.env.OIP_SEO_INDEX_LIMIT) args.push('--index-limit', process.env.OIP_SEO_INDEX_LIMIT)
if (process.env.OIP_SEO_LIMIT) args.push('--limit', process.env.OIP_SEO_LIMIT)

const result = spawnSync(python, args, { stdio: 'inherit' })
if (result.error) {
  console.warn(`seo-pages: could not run ${python}: ${result.error.message}`)
  process.exit(0)
}
if (result.status !== 0) {
  console.warn(`seo-pages: generation failed (exit ${result.status}); keeping the previous page tree`)
  process.exit(process.env.OIP_SEO_STRICT === '1' ? result.status : 0)
}

// Ping IndexNow for the pages this run actually wrote. Bing/Yandex/Seznam consume
// it; Google does not, and instead picks pages up through the sitemap. The key is
// public by design: it is the file we serve at /<key>.txt.
await submitToIndexNow(outDir)
process.exit(0)
