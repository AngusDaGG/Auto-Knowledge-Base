/* Observe original media loaded by ordinary navigation in a dedicated visible Edge profile.
 * Never attaches to a personal browser, reads cookies, calls metadata APIs or replays requests.
 */
'use strict';
const { browserOptions } = require('./browser_config.cjs');
const path = require('node:path');

const CDN_SUFFIXES = [
  'douyinvod.com', 'douyinstatic.com', 'bytecdn.cn', 'bytecdntp.com',
  'ibytedtos.com', 'bytefcdn.com', 'byteimg.com',
];
const CDN_HOSTS = new Set(['v5-dy-ov-experiment.zjcdn.com', 'v5-hl-mly-ov.zjcdn.com']);
const DENIED = new Set([401, 403, 429]);
const ERROR_NAMES = new Set(['Error', 'TypeError', 'ReferenceError', 'SyntaxError', 'RangeError',
  'TimeoutError', 'TargetClosedError', 'ProbeTimeout']);

function failureDiagnostics(error, phase) {
  const message = typeof error?.message === 'string' ? error.message : '';
  const safe = { failure_phase: phase, error_type: ERROR_NAMES.has(error?.name) ? error.name : 'OtherError' };
  const network = message.match(/\bnet::ERR_[A-Z_]{1,80}\b/);
  if (network) safe.network_error = network[0];
  if (/Target (?:page|context|browser).*closed|(?:page|context|browser) has been closed/i.test(message)) {
    safe.reason_code = 'page_closed';
  } else if (/Execution context was destroyed|Cannot find context|Inspected target navigated/i.test(message)) {
    safe.reason_code = 'page_context_changed';
  } else if (/ProcessSingleton|SingletonLock|profile.*(?:in use|locked)|user data directory is already in use/i.test(message)) {
    safe.reason_code = 'profile_locked';
  } else if (/executable.*(?:does not exist|doesn't exist|not found)|(?:msedge|chromium).*distribution.*not found/i.test(message)) {
    safe.reason_code = 'browser_missing';
  } else if (/\bEACCES\b|\bEPERM\b|permission denied|access is denied/i.test(message)) {
    safe.reason_code = 'permission_denied';
  }
  return safe;
}

function mediaUrl(value) {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
        !['', '80', '443'].includes(url.port)) return null;
    if (!CDN_HOSTS.has(url.hostname) && !CDN_SUFFIXES.some(host =>
      url.hostname === host || url.hostname.endsWith('.' + host))) return null;
    return url;
  } catch { return null; }
}

function currentSrcKind(value) {
  if (!value) return 'none';
  if (typeof value === 'string' && value.startsWith('blob:')) return 'blob';
  return mediaUrl(value) ? 'allowlisted_direct' : 'disallowed_other';
}

function targetId(value) {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
        !['', '80', '443'].includes(url.port) ||
        !['douyin.com', 'www.douyin.com'].includes(url.hostname)) return '';
    return url.pathname.match(/^\/video\/(\d+)\/?$/)?.[1] || '';
  } catch { return ''; }
}

function targetPageKind(value, expectedId) {
  const id = targetId(value);
  if (id) return id === expectedId ? 'canonical_target' : 'canonical_other_video';
  try {
    const url = new URL(value);
    if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password &&
        ['', '80', '443'].includes(url.port) &&
        (url.hostname === 'douyin.com' || url.hostname.endsWith('.douyin.com'))) return 'other_douyin_page';
  } catch {}
  return 'external_page';
}

async function bounded(promise, milliseconds) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(Object.assign(new Error(), { name: 'ProbeTimeout' })), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}

// Runs in the page. Only visible DOM and the actual media elements are inspected.
function readVisiblePage() {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity) !== 0 && rect.width > 1 && rect.height > 1 &&
      rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
  };
  const body = (document.body?.innerText || '').slice(0, 100000);
  let blocker = '';
  if (/请完成验证|請完成驗證|拖动滑块|拖動滑塊|安全验证|安全驗證/.test(body) ||
      [...document.querySelectorAll('iframe[src*="captcha"], iframe[src*="verifycenter"], [id*="captcha"], [class*="captcha_verify"]')].some(visible)) {
    blocker = 'captcha_required';
  } else if (/登录后观看|登录后才能|请登录后观看|需登录才能|登入後觀看|登入後才能/.test(body)) {
    blocker = 'login_required';
  } else if (/视频不存在|视频已删除|视频暂不可用|该视频已下架|影片不存在|影片已刪除/.test(body)) {
    blocker = 'video_unavailable';
  }
  const titles = [...document.querySelectorAll('h1')].filter(visible);
  const authors = [...document.querySelectorAll('[data-e2e="user-title"], [data-e2e="video-author-name"], [data-e2e="feed-video-author"]')].filter(visible);
  return {
    blocker,
    title: titles.length === 1 ? titles[0].innerText.trim().slice(0, 1000) : '',
    uploader: authors.length === 1 ? authors[0].innerText.trim().slice(0, 200) : '',
    players: [...document.querySelectorAll('video')].filter(visible).map(video => ({
      duration: Number.isFinite(video.duration) ? video.duration : null,
      readyState: video.readyState,
      paused: video.paused,
      muted: video.muted,
      ended: video.ended,
      currentSrc: video.currentSrc || '',
    })),
  };
}

async function inspect(playwright, pageUrl, expectedId, profilePath, options = {}) {
  const started = performance.now();
  const diagnostics = { navigation_status: null, browser_closed: false, timings_ms: {},
    audio_resources: 0, active_players: 0, ready_players: 0, ready_player_observations: [],
    response_excluded: { frame: 0, target: 0, path: 0, mime: 0, status: 0 } };
  const fail = (category, reason) => ({ ok: false, category, reason, diagnostics });
  const observationMs = Math.min(25000, Math.max(1, options.observationMs ?? 25000));
  const pollMs = Math.min(500, Math.max(1, options.pollMs ?? 250));
  let context;
  let browser;
  let result;
  let blocked = '';
  let phase = 'launch';
  let observationStarted;
  if (!/^\d+$/.test(expectedId) || targetId(pageUrl) !== expectedId ||
      typeof profilePath !== 'string' || !path.isAbsolute(profilePath) ||
      path.basename(profilePath) !== 'visible-browser-profile' ||
      path.basename(path.dirname(profilePath)) !== 'state') {
    return fail('LOCAL_BROWSER_ERROR', 'invalid_probe_input');
  }
  try {
    context = await playwright.chromium.launchPersistentContext(profilePath, {
      ...browserOptions(), headless: false, timeout: 15000,
      locale: 'zh-CN', viewport: { width: 1280, height: 800 },
    });
    phase = 'page_setup';
    browser = context.browser();
    diagnostics.timings_ms.launch = Math.round(performance.now() - started);
    const page = context.pages()[0] || await context.newPage();
    const audio = new Map();
    const deniedMedia = new Set();
    page.on('response', response => {
      try {
        const url = mediaUrl(response.url());
        if (!url) return;
        if (response.frame() !== page.mainFrame()) { diagnostics.response_excluded.frame++; return; }
        if (targetId(page.url()) !== expectedId) { diagnostics.response_excluded.target++; return; }
        const status = response.status();
        if (DENIED.has(status)) deniedMedia.add(url.href);
        if (!/media-audio-[a-z0-9_-]+/i.test(url.pathname) ||
            /(?:^|[\/_-])(?:music|bgm|background)(?:[\/_-]|$)/i.test(url.pathname)) {
          diagnostics.response_excluded.path++; return;
        }
        if (DENIED.has(status)) {
          diagnostics.response_excluded.status++; blocked = 'media_access_denied'; return;
        }
        if (status < 200 || status >= 300) { diagnostics.response_excluded.status++; return; }
        const contentType = String(response.headers()['content-type'] || '').split(';')[0].trim();
        if (!/^(?:audio\/|video\/mp4$|application\/octet-stream$)/i.test(contentType)) {
          diagnostics.response_excluded.mime++; return;
        }
        // Byte-range requests and refreshed signed URLs for one asset count once.
        audio.set(url.hostname + url.pathname, response.url());
        diagnostics.audio_resources = audio.size;
      } catch { diagnostics.ignored_response_error = true; }
    });
    page.on('pageerror', () => { diagnostics.page_error_observed = true; });
    phase = 'navigation';
    const navigationStarted = performance.now();
    const navigation = await page.goto(pageUrl, { waitUntil: 'commit', timeout: 20000 });
    diagnostics.navigation_status = navigation?.status() ?? null;
    diagnostics.timings_ms.navigation = Math.round(performance.now() - navigationStarted);
    diagnostics.target_page_kind = targetPageKind(page.url(), expectedId);
    if (DENIED.has(diagnostics.navigation_status)) {
      result = fail('BLOCKED_PLATFORM', 'navigation_access_denied');
    } else {
      phase = 'observation';
      observationStarted = performance.now();
      const deadline = observationStarted + observationMs;
      let snapshot;
      let lastContextError;
      do {
        if (blocked) { result = fail('BLOCKED_PLATFORM', blocked); break; }
        diagnostics.target_page_kind = targetPageKind(page.url(), expectedId);
        if (['canonical_other_video', 'external_page'].includes(diagnostics.target_page_kind)) {
          result = fail('INVALID_TARGET', 'target_page_mismatch'); break;
        }
        try {
          // A busy renderer may need more than 1.2 seconds during bootstrap.
          // Await one snapshot within the existing overall deadline; never stack evaluations.
          snapshot = await bounded(page.evaluate(readVisiblePage),
            Math.max(1, deadline - performance.now()));
          lastContextError = null;
        } catch (error) {
          const failure = failureDiagnostics(error, phase);
          if (failure.reason_code !== 'page_context_changed') throw error;
          lastContextError = error;
          diagnostics.context_changes = (diagnostics.context_changes || 0) + 1;
          const remaining = deadline - performance.now();
          if (remaining > 0) await page.waitForTimeout(Math.min(pollMs, remaining));
          continue;
        }
        if (snapshot.blocker) { result = fail('BLOCKED_PLATFORM', snapshot.blocker); break; }
        diagnostics.target_page_kind = targetPageKind(page.url(), expectedId);
        if (['canonical_other_video', 'external_page'].includes(diagnostics.target_page_kind)) {
          result = fail('INVALID_TARGET', 'target_page_mismatch'); break;
        }
        if (diagnostics.target_page_kind === 'other_douyin_page') {
          const remaining = deadline - performance.now();
          if (remaining > 0) await page.waitForTimeout(Math.min(pollMs, remaining));
          continue;
        }
        const active = snapshot.players.filter(player => !player.paused && !player.ended);
        const ready = snapshot.players.filter(player => Number.isFinite(player.duration) &&
          player.duration > 0 && player.readyState >= 2 && !player.ended);
        diagnostics.active_players = active.length;
        diagnostics.ready_players = ready.length;
        diagnostics.ready_player_observations = ready.map((player, player_index) => ({
          player_index, current_src_kind: currentSrcKind(player.currentSrc),
          paused: Boolean(player.paused), muted: Boolean(player.muted), duration_seconds: player.duration,
        }));
        if (active.length > 1 || ready.length > 1) {
          result = fail('INVALID_TARGET', 'multiple_active_players'); break;
        }
        if (ready.length === 1) {
          const source = mediaUrl(ready[0].currentSrc);
          if (source && deniedMedia.has(source.href)) {
            result = fail('BLOCKED_PLATFORM', 'media_access_denied'); break;
          }
          // The target player's own direct source is stronger evidence than a
          // network audio response, which could belong to recommendation preloads.
          if (!source && audio.size > 1) { result = fail('INVALID_TARGET', 'ambiguous_original_audio'); break; }
          const selected = source?.href || (audio.size === 1 ? [...audio.values()][0] : null);
          if (selected) {
            if (blocked) { result = fail('BLOCKED_PLATFORM', blocked); break; }
            diagnostics.media_source = source ? 'player_current_src' : 'original_audio_response';
            result = { ok: true, info: {
              id: expectedId, title: snapshot.title || `抖音影片 ${expectedId}`,
              uploader: snapshot.uploader || '未知',
              video: { audio: { play_url: { url_list: [selected] } } },
              download_method: 'visible-browser', expected_duration: ready[0].duration,
            }, diagnostics };
            break;
          }
        }
        const remaining = deadline - performance.now();
        if (remaining > 0) await page.waitForTimeout(Math.min(pollMs, remaining));
      } while (performance.now() < deadline);
      diagnostics.timings_ms.observation = Math.round(performance.now() - observationStarted);
      if (!result && blocked) result = fail('BLOCKED_PLATFORM', blocked);
      if (!result) {
        diagnostics.target_page_kind = targetPageKind(page.url(), expectedId);
        if (diagnostics.target_page_kind !== 'canonical_target') result = fail('INVALID_TARGET', 'target_page_mismatch');
      }
      if (!result && lastContextError) {
        Object.assign(diagnostics, failureDiagnostics(lastContextError, phase));
        result = fail('LOCAL_BROWSER_ERROR', 'launch_or_runtime_error');
      }
      if (!result) result = fail('NO_MEDIA', diagnostics.ready_players ?
        'original_audio_not_observed' : 'ready_player_not_observed');
    }
  } catch (error) {
    if (phase === 'observation' && observationStarted !== undefined) {
      diagnostics.timings_ms.observation = Math.round(performance.now() - observationStarted);
    }
    Object.assign(diagnostics, failureDiagnostics(error, phase));
    result = blocked ? fail('BLOCKED_PLATFORM', blocked) :
      fail('LOCAL_BROWSER_ERROR', phase === 'observation' && error.name === 'ProbeTimeout' ?
        'observation_deadline_exceeded' : phase === 'navigation' && error.name === 'TimeoutError' ?
          'navigation_timeout' : 'launch_or_runtime_error');
  } finally {
    if (context) {
      try {
        await bounded(context.close(), 5000);
        diagnostics.browser_closed = true;
      } catch {
        // Only this probe's browser may be closed; never attach to another browser.
        if (browser) {
          try { await bounded(browser.close(), 2000); diagnostics.browser_closed = true; } catch {}
        }
        if (!diagnostics.browser_closed) {
          diagnostics.close_failed = true;
          if (!result || result.ok) result = fail('LOCAL_BROWSER_ERROR', 'browser_close_failed');
        }
      }
    }
    diagnostics.timings_ms.total = Math.round(performance.now() - started);
  }
  return result;
}

module.exports = { inspect };
if (require.main === module) {
  const [modulePath, pageUrl, expectedId, profilePath] = process.argv.slice(2);
  Promise.resolve().then(() => inspect(require(modulePath), pageUrl, expectedId, profilePath)).then(result => {
    process.stdout.write(JSON.stringify(result));
    if (!result.ok) process.exitCode = 1;
  }).catch(() => {
    process.stdout.write(JSON.stringify({ ok: false, category: 'LOCAL_BROWSER_ERROR', reason: 'launch_or_runtime_error' }));
    process.exitCode = 1;
  });
}
