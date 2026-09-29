/** Selection-based copy: only needs the click's user gesture, not a permission. */
function copyBySelection(text) {
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '0'
  textarea.style.left = '0'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  textarea.setSelectionRange(0, text.length)
  let copied = false
  try {
    copied = document.execCommand('copy')
  } finally {
    textarea.remove()
  }
  return copied
}

export async function writeClipboard(text) {
  const value = text == null ? '' : String(text)

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value)
      return
    } catch {
      // A framed document (for example a host application's embedded browser)
      // rejects the async Clipboard API unless the host delegates
      // `clipboard-write`, so fall back to the selection-based copy.
    }
  }

  if (!copyBySelection(value)) throw new Error('copy failed')
}
