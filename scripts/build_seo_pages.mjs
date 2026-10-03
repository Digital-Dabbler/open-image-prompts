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
import { existsSync, readdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const generator = resolve(repositoryRoot, 'scripts', 'gen_seo_pages.py')
const outDir = process.env.OIP_SEO_OUT || '/var/www/oip-pages'

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
process.exit(0)
