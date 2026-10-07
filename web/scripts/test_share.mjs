import assert from 'node:assert/strict'
import test from 'node:test'
import { galleryRequestFromSearch } from '../src/gallerySession.js'
import { galleryDeepLink, prefersNativeShare, promptShareUrl } from '../src/share.js'

test('a shared prompt points at the pre-rendered page, not the gallery root', () => {
  // The SEO page is the share target: it is crawlable, canonical and unfurls
  // with the work's own media. Shipping the homepage link was the bug.
  assert.equal(
    promptShareUrl('2107722836668244236'),
    'https://openimages.relakkesyang.org/p/2107722836668244236/',
  )
})

test('a trailing slash on the origin never doubles up', () => {
  assert.equal(promptShareUrl(123456789, 'https://example.com/'), 'https://example.com/p/123456789/')
})

test('anything that is not a tweet id produces no link at all', () => {
  for (const value of ['', null, undefined, 'abc', '12', '1e5', '../etc/passwd', '<script>']) {
    assert.equal(promptShareUrl(value), '', `expected no link for ${String(value)}`)
  }
})

test('the gallery deep link keeps the id and stays a query string', () => {
  assert.equal(
    galleryDeepLink('2107722836668244236'),
    'https://openimages.relakkesyang.org/?p=2107722836668244236',
  )
  assert.equal(galleryDeepLink('nope'), '')
})

test('the gallery reads ?p= and rejects anything that is not an id', () => {
  assert.equal(galleryRequestFromSearch('?p=2107722836668244236').promptId, '2107722836668244236')
  assert.equal(galleryRequestFromSearch('?p=abc').promptId, '')
  assert.equal(galleryRequestFromSearch('?p=').promptId, '')
  assert.equal(galleryRequestFromSearch('').promptId, '')
})

test('a deep link does not disturb the existing session parameters', () => {
  const request = galleryRequestFromSearch(
    '?session=8f14e45f-ceea-4a1e-9f1a-3c0b1a5e2d11&focus=2107722836668244236&p=2107722836668244236',
  )
  assert.equal(request.sessionId, '8f14e45f-ceea-4a1e-9f1a-3c0b1a5e2d11')
  assert.equal(request.focusId, '2107722836668244236')
  assert.equal(request.promptId, '2107722836668244236')
})

test('the desktop keeps the clipboard, touch devices get the share sheet', () => {
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const restore = () => {
    for (const [key, descriptor] of [['navigator', originalNavigator], ['window', originalWindow]]) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
  try {
    // Node's navigator has no share(); a pointer device must fall back to copy.
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {} })
    assert.equal(prefersNativeShare(), false)

    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { share: async () => {} } })
    Object.defineProperty(globalThis, 'window', { configurable: true, value: {} })
    assert.equal(prefersNativeShare(), false, 'a browser without matchMedia still copies')

    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: { matchMedia: () => ({ matches: false }) },
    })
    assert.equal(prefersNativeShare(), false, 'a mouse pointer copies the link')

    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: { matchMedia: () => ({ matches: true }) },
    })
    assert.equal(prefersNativeShare(), true, 'a coarse pointer uses the native sheet')
  } finally {
    restore()
  }
})
