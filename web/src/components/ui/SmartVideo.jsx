import { ImageBroken } from '@phosphor-icons/react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useLang } from '../../i18n'

/**
 * Video twin of SmartImage: same ordered-source fallback (local pack first, then
 * the original remote URL), plus honest failure handling when nothing plays.
 *
 * `playing` drives inline previews (hover cards): the element stays mounted with
 * preload="none" and only fetches when the preview actually starts.
 */
export default function SmartVideo({
  sources,
  poster = null,
  alt = '',
  className = '',
  playing = false,
  controls = false,
  loop = false,
  muted = true,
  eager = false,
  fit = 'contain',
  onFailed,
}) {
  const { t } = useLang()
  const availableSources = useMemo(() => [...new Set((sources || []).filter(Boolean))], [sources])
  const sourceKey = availableSources.join('|')
  const [sourceIndex, setSourceIndex] = useState(0)
  const videoRef = useRef(null)

  useEffect(() => {
    setSourceIndex(0)
  }, [sourceKey])

  const source = availableSources[sourceIndex]

  useEffect(() => {
    const element = videoRef.current
    if (!element) return
    if (playing) {
      const attempt = element.play()
      if (attempt?.catch) attempt.catch(() => {})
    } else {
      element.pause()
      if (!controls) element.currentTime = 0
    }
  }, [playing, source, controls])

  if (!source) {
    return (
      <div className={`grid place-items-center bg-surface text-faint ${className}`} aria-label={alt}>
        <div className="flex flex-col items-center gap-2 text-xs">
          <ImageBroken size={24} />
          <span>{t('image.unavailable')}</span>
        </div>
      </div>
    )
  }

  return (
    <video
      key={source}
      ref={videoRef}
      src={source}
      poster={poster || undefined}
      controls={controls}
      loop={loop}
      muted={muted}
      playsInline
      preload={eager || controls ? 'metadata' : 'none'}
      onError={() => {
        if (sourceIndex < availableSources.length - 1) {
          setSourceIndex((index) => index + 1)
        } else {
          onFailed?.()
        }
      }}
      className={`${className} ${fit === 'contain' ? 'object-contain' : 'object-cover'}`}
    />
  )
}
