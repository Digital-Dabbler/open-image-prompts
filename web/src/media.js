// `import.meta.env` is a Vite-only global; the optional chain keeps this module
// importable from plain Node so the source-ordering rules can be unit tested.
const BASE_URL = import.meta.env?.BASE_URL || '/'

export function assetUrl(localPath) {
  if (!localPath) return null
  if (/^https?:\/\//i.test(localPath)) return localPath

  const cleanBase = BASE_URL.endsWith('/') ? BASE_URL : `${BASE_URL}/`
  const cleanPath = String(localPath).replace(/^\/+/, '')
  return `${cleanBase}${cleanPath}`
}

export function isImageLikeUrl(url) {
  return Boolean(url && /\.(jpe?g|png|webp|gif|avif)(\?|$)/i.test(url))
}

export function isVideoLikeUrl(url) {
  return Boolean(url && /\.(mp4|m4v|mov|webm)(\?|$)/i.test(url))
}

function uniqueSources(...sources) {
  return [...new Set(sources.filter(Boolean))]
}

export function imageSources(image) {
  return uniqueSources(assetUrl(image?.local), image?.url)
}

export function firstImageSources(item) {
  const first = item.images?.[0]
  if (first) return imageSources(first)

  const videoThumb = item.videos?.find((video) => isImageLikeUrl(video?.url))
  if (videoThumb) return uniqueSources(videoThumb.url)

  return uniqueSources(posterForVideo(item, item.videos?.[0]))
}

// A poster frame keeps a video card from rendering as an empty box before the
// binary is loaded: prefer our own extracted still, then a still from the same
// tweet, then the poster JPEG older rows kept in `videos.url`.
function posterForVideo(item, video) {
  const extracted = assetUrl(video?.poster)
  if (extracted) return extracted
  if (isImageLikeUrl(video?.url)) return video.url
  const first = item?.images?.[0]
  if (first) return assetUrl(first.local) || first.url || null
  return null
}

export function videoSources(video) {
  if (!video) return []
  // Local pack first (works offline and for viewers who cannot reach X), then
  // the original video.twimg.com URL as a fallback while packs are missing.
  return uniqueSources(assetUrl(video.local), isVideoLikeUrl(video.url) ? video.url : null)
}

export function mediaItems(item) {
  const videos = item.videos || []
  const playableVideos = videos.filter((video) => videoSources(video).length > 0)

  return [
    ...(item.images || []).map((image) => ({
      type: 'image',
      sources: imageSources(image),
      sourceUrl: image.url,
      label: `图片 ${image.index}`,
    })),
    ...playableVideos.map((video) => ({
      type: 'video',
      sources: videoSources(video),
      poster: posterForVideo(item, video),
      sourceUrl: isVideoLikeUrl(video.url) ? video.url : item.tweet_url,
      tweetUrl: item.tweet_url,
      label: `视频 ${video.index}`,
    })),
    // Video rows with nothing playable still deserve an honest entry: they open
    // the original post instead of pretending to be a player.
    ...videos
      .filter((video) => videoSources(video).length === 0)
      .map((video) => ({
        type: 'video-link',
        url: item.tweet_url,
        sources: uniqueSources(posterForVideo(item, video)),
        label: `视频 ${video.index}`,
      })),
  ]
}
