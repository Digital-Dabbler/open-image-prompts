// Contract test for the gallery's video/media mapping: what the player receives
// for each collected shape, and in which order sources are tried.
import assert from 'node:assert/strict'
import test from 'node:test'

import { firstImageSources, mediaItems, videoSources } from '../src/media.js'

test('a locally archived video is played from the pack, with the remote URL as fallback', () => {
  const video = {
    id: '1',
    index: 1,
    local: 'images/2104662818742309357/video_1.mp4',
    url: 'https://video.twimg.com/amplify_video/2104662483130863616/vid/avc1/1280x720/x.mp4',
    poster: 'images/2104662818742309357/video_1.jpg',
  }
  assert.deepEqual(videoSources(video), [
    '/images/2104662818742309357/video_1.mp4',
    'https://video.twimg.com/amplify_video/2104662483130863616/vid/avc1/1280x720/x.mp4',
  ])

  const [entry] = mediaItems({ tweet_id: '1', tweet_url: 'https://x.com/a/status/1', videos: [video] })
  assert.equal(entry.type, 'video')
  assert.deepEqual(entry.sources, videoSources(video))
  assert.equal(entry.poster, '/images/2104662818742309357/video_1.jpg')
  assert.equal(entry.tweetUrl, 'https://x.com/a/status/1')
})

test('a remote-only video still gets a real player (no local pack required)', () => {
  const video = {
    id: '2',
    index: 1,
    local: null,
    url: 'https://video.twimg.com/amplify_video/2050062063360495616/pu/vid/avc1/1280x720/bD2rjRmpxdH-jYc9.mp4',
    poster: null,
  }
  assert.deepEqual(videoSources(video), [
    'https://video.twimg.com/amplify_video/2050062063360495616/pu/vid/avc1/1280x720/bD2rjRmpxdH-jYc9.mp4',
  ])
  const [entry] = mediaItems({ tweet_id: '2', videos: [video] })
  assert.equal(entry.type, 'video')
})

test('a video row carrying only a poster JPEG is never offered as a player', () => {
  const video = {
    id: '3',
    index: 1,
    local: null,
    url: 'https://pbs.twimg.com/ext_tw_video_thumb/2030623554551902209/pu/img/JOJhqff2f7Q4ETTp.jpg',
    poster: null,
  }
  assert.deepEqual(videoSources(video), [])
  const [entry] = mediaItems({
    tweet_id: '3',
    tweet_url: 'https://x.com/a/status/3',
    videos: [video],
  })
  assert.equal(entry.type, 'video-link')
  assert.equal(entry.url, 'https://x.com/a/status/3')
})

test('images and videos coexist, images first', () => {
  const entries = mediaItems({
    tweet_id: '4',
    images: [{ id: '1', index: 1, url: 'https://pbs.twimg.com/media/a.jpg', local: 'images/4/1.jpg' }],
    videos: [
      { id: '9', index: 1, local: 'images/4/video_1.mp4', url: null, poster: null },
    ],
  })
  assert.deepEqual(entries.map((entry) => entry.type), ['image', 'video'])
})

test('a card falls back to the extracted poster, then the tweet still', () => {
  assert.deepEqual(
    firstImageSources({
      tweet_id: '5',
      images: [],
      videos: [{ index: 1, local: 'images/5/video_1.mp4', url: null, poster: 'images/5/video_1.jpg' }],
    }),
    ['/images/5/video_1.jpg'],
  )
  assert.deepEqual(
    firstImageSources({
      tweet_id: '6',
      images: [{ index: 1, url: 'https://pbs.twimg.com/media/b.jpg', local: 'images/6/1.jpg' }],
      videos: [{ index: 1, local: 'images/6/video_1.mp4', url: null, poster: null }],
    }),
    ['/images/6/1.jpg', 'https://pbs.twimg.com/media/b.jpg'],
  )
})
