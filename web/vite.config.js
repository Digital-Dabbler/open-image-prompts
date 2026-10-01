import { createReadStream, readFileSync, statSync } from 'node:fs'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const WEB_DIR = fileURLToPath(new URL('.', import.meta.url))
const IMAGE_DIR = resolve(WEB_DIR, '../images')
const DIMS_FILE = resolve(WEB_DIR, 'dims.json')
const API_PORT = globalThis.process?.env.OIP_API_PORT || '8787'
const GALLERY_HMR_PORT = Number(globalThis.process?.env.OIP_GALLERY_HMR_PORT || 0)
const CONTENT_TYPES = {
  '.avif': 'image/avif',
  '.gif': 'image/gif',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.mp4': 'video/mp4',
  '.m4v': 'video/mp4',
  '.mov': 'video/quicktime',
  '.webm': 'video/webm',
}

function blockPrivateArchive(middlewares) {
  middlewares.use((request, response, next) => {
    const pathname = new URL(request.url || '/', 'http://open-image-prompts.local').pathname
    if (!pathname.startsWith('/db/')) return next()
    response.statusCode = 404
    response.end('Not found')
  })
}

function serveArchiveImages(middlewares) {
  middlewares.use((request, response, next) => {
    const pathname = new URL(request.url || '/', 'http://open-image-prompts.local').pathname
    if (!pathname.startsWith('/images/')) return next()

    let relativePath
    try {
      relativePath = decodeURIComponent(pathname.slice('/images/'.length))
    } catch {
      response.statusCode = 400
      response.end('Bad image path')
      return
    }

    const filePath = resolve(IMAGE_DIR, relativePath)
    if (!filePath.startsWith(`${IMAGE_DIR}${sep}`)) {
      response.statusCode = 403
      response.end('Forbidden')
      return
    }

    try {
      const file = statSync(filePath)
      if (!file.isFile()) return next()
      const contentType = CONTENT_TYPES[extname(filePath).toLowerCase()] || 'application/octet-stream'
      response.setHeader('Content-Type', contentType)
      response.setHeader('Cache-Control', 'public, max-age=3600')
      // Video needs byte ranges: without 206 responses the browser cannot seek
      // (and Safari refuses to play at all).
      const range = request.headers.range
      const rangeMatch = typeof range === 'string' ? /^bytes=(\d*)-(\d*)$/.exec(range.trim()) : null
      if (rangeMatch) {
        const [, rawStart, rawEnd] = rangeMatch
        let start = rawStart === '' ? null : Number(rawStart)
        let end = rawEnd === '' ? null : Number(rawEnd)
        if (start === null && end !== null) {
          start = Math.max(file.size - end, 0)
          end = file.size - 1
        } else {
          start = start ?? 0
          end = end === null ? file.size - 1 : Math.min(end, file.size - 1)
        }
        if (Number.isNaN(start) || Number.isNaN(end) || start > end || start >= file.size) {
          response.statusCode = 416
          response.setHeader('Content-Range', `bytes */${file.size}`)
          response.end()
          return
        }
        response.statusCode = 206
        response.setHeader('Accept-Ranges', 'bytes')
        response.setHeader('Content-Range', `bytes ${start}-${end}/${file.size}`)
        response.setHeader('Content-Length', end - start + 1)
        if (request.method === 'HEAD') {
          response.end()
          return
        }
        createReadStream(filePath, { start, end }).pipe(response)
        return
      }
      response.setHeader('Accept-Ranges', 'bytes')
      response.setHeader('Content-Length', file.size)
      if (request.method === 'HEAD') {
        response.end()
        return
      }
      createReadStream(filePath).pipe(response)
    } catch {
      next()
    }
  })
}

function archiveImagesPlugin() {
  return {
    name: 'open-image-prompts-images',
    configureServer(server) {
      serveArchiveImages(server.middlewares)
    },
    configurePreviewServer(server) {
      serveArchiveImages(server.middlewares)
    },
  }
}

function archiveAssetsPlugin() {
  return {
    name: 'open-image-prompts-assets',
    configureServer(server) {
      blockPrivateArchive(server.middlewares)
    },
    configurePreviewServer(server) {
      blockPrivateArchive(server.middlewares)
    },
    generateBundle() {
      try {
        this.emitFile({
          type: 'asset',
          fileName: 'dims.json',
          source: readFileSync(DIMS_FILE),
        })
      } catch {
        // dims.json is optional (run scripts/export_dims.py to regenerate)
      }
    },
  }
}

export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss(), archiveImagesPlugin(), archiveAssetsPlugin()],
  server: {
    // Never inherit Vite's dual-stack `localhost` default: it lets the dev
    // server bind [::1] while an unrelated process owns IPv4 on the same port,
    // and the printed localhost URL then opens that other application.
    // scripts/with_api.mjs overrides this from OIP_WEB_HOST when set.
    host: '127.0.0.1',
    hmr: GALLERY_HMR_PORT
      ? { host: '127.0.0.1', clientPort: GALLERY_HMR_PORT }
      : undefined,
    proxy: {
      '/api': `http://127.0.0.1:${API_PORT}`,
      '/health': `http://127.0.0.1:${API_PORT}`,
    },
  },
  preview: {
    host: '127.0.0.1',
    proxy: {
      '/api': `http://127.0.0.1:${API_PORT}`,
      '/health': `http://127.0.0.1:${API_PORT}`,
    },
  },
  build: {
    outDir: 'dist',
  },
})
