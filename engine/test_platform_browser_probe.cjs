'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { inspect, identity, allowedMedia, choose, sameAsset, deniedForTarget, locationKind,
  completeResponse, saveInBrowser, readPage } = require('./platform_browser_probe.cjs');
const ID = 'abcdefghijk', URL = `https://www.youtube.com/watch?v=${ID}`;
const AUDIO = 'https://rr1.googlevideo.com/videoplayback?mime=audio%2Fmp4&sig=PRIVATE';
const PROFILE = require('node:path').resolve('state/youtube/visible-browser-profile');
const snapshot = overrides => ({ players: [{ currentSrc: 'blob:local', duration: 20, readyState: 4,
  paused: false, ended: false }], attestedId: ID, original: AUDIO, title: 'Title', user_agent: 'Browser', ...overrides });

function environment(options = {}) {
  const calls = { goto: 0, evaluate: 0, closed: 0 };
  let response;
  const frame = {};
  const page = {
    on: (event, cb) => { if (event === 'response') response = cb; },
    mainFrame: () => frame,
    url: () => options.url || URL,
    goto: async () => { calls.goto++; return { status: () => options.status || 200 }; },
    evaluate: async () => {
      calls.evaluate++;
      if (options.mediaDenied) response({ url: () => options.deniedUrl || AUDIO, status: () => 403, frame: () => frame,
        headers: () => ({ 'content-type': 'audio/mp4' }) });
      if (options.hang) return new Promise(() => {});
      if (options.error) throw new Error('secret signed=PRIVATE https://private.invalid/token');
      return options.snapshot || snapshot({});
    },
    waitForTimeout: async () => {},
  };
  const context = { pages: () => [page], browser: () => null,
    close: async () => { calls.closed++; if (options.closeFail) throw new Error('PRIVATE'); } };
  return { calls, page, emitResponse: value => response(value), frame, chromium: { launchPersistentContext: async (_profile, config) => {
    assert.equal(config.headless, false); assert.equal(config.channel, 'msedge'); return context;
  } } };
}

test('platform identities reject host spoofing, unrelated pages and credentials', () => {
  assert.equal(identity('youtube', URL), ID);
  assert.equal(identity('instagram', 'https://www.instagram.com/reel/ExampleClip01/'), 'ExampleClip01');
  assert.equal(identity('tiktok', 'https://www.tiktok.com/@fixture_user/video/1234567890123456789'), '1234567890123456789');
  assert.equal(identity('bilibili', 'https://www.bilibili.com/video/BV1234567890/'), 'BV1234567890');
  for (const value of [URL.replace('youtube.com', 'youtube.com.evil.test'), URL.replace('https://', 'https://user@'), 'https://www.youtube.com/playlist?list=123']) {
    assert.equal(identity('youtube', value), '');
  }
});

test('CDN allowlists are platform-specific and do not allow local or credential URLs', () => {
  assert.equal(allowedMedia('youtube', AUDIO), true);
  for (const u of ['https://googlevideo.com.evil.test/x', 'https://127.0.0.1/x',
    'https://u:p@rr1.googlevideo.com/x', 'http://rr1.googlevideo.com/x', 'https://rr1.googlevideo.com:444/x']) {
    assert.equal(allowedMedia('youtube', u), false);
  }
  assert.equal(allowedMedia('instagram', AUDIO), false);
});

test('target player audio is preferred; ads and unrelated player identities are rejected', () => {
  assert.equal(choose('youtube', ID, snapshot({}), new Map()).url, AUDIO);
  assert.equal(choose('youtube', ID, snapshot({ adActive: true }), new Map()), null);
  assert.equal(choose('youtube', ID, snapshot({ attestedId: 'OTHER_VIDEO' }), new Map()).category, 'INVALID_TARGET');
  assert.equal(choose('youtube', ID, snapshot({ attestedId: '' }), new Map()), null);
});

test('DRM, mandatory login and multiple visible players cannot become successful media', () => {
  assert.equal(choose('youtube', ID, snapshot({ protectedMedia: true }), new Map()).category, 'PROTECTED_MEDIA');
  assert.equal(choose('youtube', ID, snapshot({ blocker: 'login_required' }), new Map()).category, 'BLOCKED_PLATFORM');
  assert.equal(choose('youtube', ID, snapshot({ players: [...snapshot({}).players, ...snapshot({}).players] }), new Map()).category, 'INVALID_TARGET');
});

test('independent visible browser succeeds and closes only its own context', async () => {
  const env = environment(); const r = await inspect(env, 'youtube', URL, ID, PROFILE);
  assert.equal(r.ok, true); assert.equal(r.info.raw_id, ID); assert.equal(r.info.expected_duration, 20);
  assert.deepEqual(env.calls, { goto: 1, evaluate: 1, closed: 1 });
});

test('HTTP 412 and 403 are terminal before player observation', async () => {
  for (const status of [401, 403, 412, 429, 451]) {
    const env = environment({ status }); const r = await inspect(env, 'youtube', URL, ID, PROFILE);
    assert.equal(r.category, 'BLOCKED_PLATFORM'); assert.equal(env.calls.evaluate, 0); assert.equal(env.calls.closed, 1);
  }
});

test('media denial is preserved if the pending snapshot times out', async () => {
  const env = environment({ mediaDenied: true, hang: true });
  const r = await inspect(env, 'youtube', URL, ID, PROFILE, { observationMs: 20 });
  assert.equal(r.category, 'BLOCKED_PLATFORM'); assert.equal(r.reason, 'unattributed_media_access_denied');
  assert.equal(env.calls.evaluate, 1); assert.equal(env.calls.closed, 1);
  assert.doesNotMatch(JSON.stringify(r), /PRIVATE|signed=/);
});

test('login redirect and consent are terminal and never navigated again', async () => {
  for (const url of ['https://www.youtube.com/login', 'https://consent.youtube.com/m']) {
    const env = environment({ url }); const r = await inspect(env, 'youtube', URL, ID, PROFILE);
    assert.equal(r.category, 'BLOCKED_PLATFORM'); assert.equal(env.calls.goto, 1); assert.equal(env.calls.evaluate, 0);
  }
});

test('raw exception data and media signatures are absent from failed diagnostics', async () => {
  const env = environment({ error: true }); const r = await inspect(env, 'youtube', URL, ID, PROFILE);
  assert.equal(r.category, 'LOCAL_BROWSER_ERROR'); assert.doesNotMatch(JSON.stringify(r), /PRIVATE|private.invalid/);
});

test('a close failure does not overwrite a platform denial', async () => {
  const env = environment({ status: 403, closeFail: true });
  const r = await inspect(env, 'youtube', URL, ID, PROFILE);
  assert.equal(r.category, 'BLOCKED_PLATFORM'); assert.equal(r.diagnostics.browser_closed, false);
});

test('a personal profile path is never launched', async () => {
  let launched = false;
  const r = await inspect({ chromium: { launchPersistentContext: async () => { launched = true; } } },
    'youtube', URL, ID, require('node:path').resolve('User Data'));
  assert.equal(r.reason, 'invalid_probe_input'); assert.equal(launched, false);
});

test('denial correlation separates target audio from an unrelated ad without changing signatures', () => {
  const target = 'https://rr1.googlevideo.com/videoplayback?id=TARGET&itag=140&sig=one';
  const range = target + '&range=0-100';
  const ad = target.replace('id=TARGET', 'id=AD');
  assert.equal(sameAsset('youtube', target, range), true);
  assert.equal(sameAsset('youtube', target, ad), false);
  const s = snapshot({ original: target, targetSources: [target] });
  assert.equal(deniedForTarget('youtube', s, [{ url: ad, status: 403 }]), undefined);
  assert.equal(deniedForTarget('youtube', s, [{ url: range, status: 403 }]).status, 403);
  assert.equal(choose('instagram', 'ExampleClip01', snapshot({ original: '', attestedId: '' }),
    new Map([['unrelated', { url: 'https://cdn.cdninstagram.com/other.mp4', audio: true }]])), null);
});

test('redirect diagnostics do not expose queries and Bilibili still requires player identity', () => {
  assert.equal(locationKind('bilibili', 'https://www.bilibili.com/?token=PRIVATE', 'BV1234567890'), 'site_home');
  assert.equal(locationKind('bilibili', 'https://www.bilibili.com/404?token=PRIVATE', 'BV1234567890'), 'error_page');
  assert.equal(locationKind('bilibili', 'https://evil.test/video/BV1234567890/', 'BV1234567890'), 'external_site');
  assert.equal(choose('bilibili', 'BV1234567890', snapshot({ attestedId: '' }), new Map()), null);
});

test('partial ranges and oversized responses cannot be treated as complete playback', () => {
  assert.equal(completeResponse(206, { 'content-range': 'bytes 0-4/5', 'content-length': '5' }), true);
  assert.equal(completeResponse(206, { 'content-range': 'bytes 0-4/100' }), false);
  assert.equal(completeResponse(200, { 'content-length': String(513 * 1024 * 1024) }), false);
  assert.equal(completeResponse(403, {}), false);
});

function output() {
  const fs = require('node:fs'), path = require('node:path');
  const root = path.join(require('node:os').tmpdir(), 'akb-browser-capture-tests');
  fs.mkdirSync(root, { recursive: true });
  return path.join(fs.mkdtempSync(path.join(root, 'case-')), 'visible-browser_media.bin');
}

test('a complete ordinary playback response is saved without another network request', async () => {
  const target = output(), fs = require('node:fs'), d = {};
  const page = { url: () => URL, evaluateHandle: () => assert.fail('must not request again') };
  const r = await saveInBrowser(page, 'youtube', ID, { url: AUDIO }, new Map([['audio', {
    url: AUDIO, status: 200, headers: { 'content-length': '5' }, response: { body: async () => Buffer.from('audio') },
  }]]), target, d);
  assert.equal(r.media_path, target); assert.equal(fs.readFileSync(target, 'utf8'), 'audio');
  assert.equal(d.capture_kind, 'playback_response');
});

test('native browser fetch streams locally once and never retries a 403', async () => {
  for (const status of [200, 403]) {
    const target = output(), d = {}, state = { status, headers: { 'content-length': '5' },
      reader: { read: async () => state.sent++ ? { done: true } : { done: false, value: Buffer.from('audio') } },
      sent: 0, controller: { abort() {} }, timer: null };
    let requested = 0;
    const handle = { evaluate: async fn => fn(state), dispose: async () => {} };
    const page = { url: () => URL, evaluateHandle: async (_fn, url) => { requested++; assert.equal(url, AUDIO); return handle; } };
    const r = await saveInBrowser(page, 'youtube', ID, { url: AUDIO }, new Map(), target, d);
    assert.equal(requested, 1);
    if (status === 403) { assert.equal(r.category, 'BLOCKED_PLATFORM'); assert.equal(state.sent, 0); }
    else { assert.equal(r.media_path, target); assert.equal(require('node:fs').readFileSync(target, 'utf8'), 'audio'); }
  }
});

test('pre-existing output is never overwritten or marked for cleanup', async () => {
  const target = output(), fs = require('node:fs'), d = {};
  fs.writeFileSync(target, 'original');
  await assert.rejects(saveInBrowser({}, 'youtube', ID, { url: AUDIO }, new Map(), target, d), { code: 'EEXIST' });
  assert.equal(fs.readFileSync(target, 'utf8'), 'original'); assert.equal(d.capture_started, undefined);
});

test('Instagram public data is scoped to matching shortcode, not a recommended post', () => {
  const script = { textContent: JSON.stringify({ posts: [
    { code: 'OTHER', video_url: 'https://cdn.cdninstagram.com/ad.mp4' },
    { shortcode: 'ExampleClip01', video_versions: [{ url: 'https://cdn.cdninstagram.com/target.mp4' }] },
  ] }) };
  const document = { body: { innerText: '' }, title: 'Title', querySelector: () => null,
    querySelectorAll: selector => selector === 'script[type="application/json"]' ? [script] : [] };
  const result = require('node:vm').runInNewContext(`(${readPage.toString()})({platform:'instagram',expectedId:'ExampleClip01'})`,
    { document, window: {}, navigator: { userAgent: 'Browser' } });
  assert.equal(result.original, 'https://cdn.cdninstagram.com/target.mp4');
  assert.equal(result.attestedId, 'ExampleClip01');
});

test('an unrelated denied ad does not reject a verified target source', async () => {
  const env = environment({ mediaDenied: true, deniedUrl: AUDIO + '&id=AD&itag=140' });
  const r = await inspect(env, 'youtube', URL, ID, PROFILE);
  assert.equal(r.ok, true); assert.equal(env.calls.goto, 1);
});

test('a CORS fetch error cannot hide an observed target HTTP denial or leave a partial file', async () => {
  const env = environment(), target = output();
  env.page.evaluateHandle = async () => {
    env.emitResponse({ url: () => AUDIO, status: () => 403, frame: () => env.frame,
      headers: () => ({'content-type':'text/plain'}) });
    throw new Error('CORS fetch failed PRIVATE');
  };
  const r = await inspect(env, 'youtube', URL, ID, PROFILE, {outputPath: target});
  assert.equal(r.category, 'BLOCKED_PLATFORM'); assert.equal(r.diagnostics.media_status, 403);
  assert.equal(require('node:fs').existsSync(target), false);
  assert.doesNotMatch(JSON.stringify(r), /PRIVATE/);
});
