// Sharing inside the JavaScript gallery.
//
// The prompt dialog is a modal: it never changes the address bar, so until now
// there was nothing to hand to someone else — the only shareable URL was the
// homepage. Every prompt already has a pre-rendered, crawlable page at
// `/p/<tweet_id>/` (the same one Google indexes, with the media, both languages,
// canonical tags and JSON-LD), so that is what "share" produces.
import { writeClipboard } from './clipboard.js'

export const SHARE_ORIGIN = 'https://openimages.relakkesyang.org'

const NUMERIC_ID = /^[0-9]{6,25}$/

export function promptShareUrl(tweetId, origin = SHARE_ORIGIN) {
  const id = String(tweetId ?? '').trim()
  if (!NUMERIC_ID.test(id)) return ''
  return `${String(origin).replace(/\/+$/, '')}/p/${id}/`
}

/** The gallery URL that opens this prompt in the app (`/?p=<id>`). */
export function galleryDeepLink(tweetId, origin = SHARE_ORIGIN) {
  const id = String(tweetId ?? '').trim()
  if (!NUMERIC_ID.test(id)) return ''
  return `${String(origin).replace(/\/+$/, '')}/?p=${id}`
}

/** True on touch-first devices, where the OS share sheet beats a clipboard copy. */
export function prefersNativeShare() {
  if (typeof navigator === 'undefined' || typeof navigator.share !== 'function') return false
  try {
    return window.matchMedia('(pointer: coarse)').matches
  } catch {
    return false
  }
}

/**
 * Share one prompt's pre-rendered page. Touch devices get the native sheet;
 * pointer devices copy the link (a system share sheet on a desktop is a surprise,
 * and pasting the URL is what people actually do in a chat window).
 * Returns 'shared' | 'copied' | ''.
 */
export async function sharePrompt(tweetId, { title = '', text = '' } = {}) {
  const url = promptShareUrl(tweetId)
  if (!url) throw new Error('no shareable url for this record')
  if (prefersNativeShare()) {
    try {
      await navigator.share({ title, text, url })
      return 'shared'
    } catch (error) {
      // A cancelled share sheet is not an error the user needs to see; anything
      // else falls through to the clipboard so the action still does something.
      if (error && error.name === 'AbortError') return ''
    }
  }
  await writeClipboard(url)
  return 'copied'
}
