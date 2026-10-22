'use strict';
const { browserOptions } = require('./browser_config.cjs');
// Ordinary visible navigation only: no personal profiles, login, cookie reads or API replay.
const path = require('node:path');
const fs = require('node:fs');
const RULES = require('./platform_rules.json');
const DENIED = new Set([401, 403, 412, 429, 451]);
const MAX_BYTES = 512 * 1024 * 1024;

function sameAsset(platform, a, b) {
  if (!allowedMedia(platform, a) || !allowedMedia(platform, b)) return false;
  if (a === b) return true;
  if (platform !== 'youtube') return false;
  const x = new URL(a), y = new URL(b);
  return Boolean(x.searchParams.get('id') && x.searchParams.get('itag') &&
    x.searchParams.get('id') === y.searchParams.get('id') &&
    x.searchParams.get('itag') === y.searchParams.get('itag'));
}

function locationKind(platform, value, expectedId) {
  if (identity(platform, value) === expectedId) return 'target_video';
  try {
    const u = new URL(value), hosts = {
      youtube: ['www.youtube.com', 'youtube.com', 'm.youtube.com'],
      instagram: ['www.instagram.com', 'instagram.com'],
      tiktok: ['www.tiktok.com', 'tiktok.com', 'm.tiktok.com'],
      bilibili: ['www.bilibili.com', 'bilibili.com', 'm.bilibili.com'],
    };
    if (!hosts[platform]?.includes(u.hostname)) return 'external_site';
    if (/\/404(?:\/|$)|\/error(?:\/|$)/.test(u.pathname)) return 'error_page';
    if (u.pathname === '/') return 'site_home';
    return 'other_platform_page';
  } catch { return 'invalid_url'; }
}

function completeResponse(status, headers) {
  if (![200, 206].includes(status)) return false;
  if (status === 206) {
    const match = /^bytes 0-(\d+)\/(\d+)$/.exec(headers['content-range'] || '');
    if (!match || Number(match[1]) + 1 !== Number(match[2])) return false;
  }
  const size = headers['content-length'];
  return size === undefined || /^\d+$/.test(size) && Number(size) > 0 && Number(size) <= MAX_BYTES;
}

function identity(platform, value) {
  try {
    const u = new URL(value);
    if (u.protocol !== 'https:' || u.username || u.password || u.port) return '';
    if (platform === 'youtube' && ['www.youtube.com', 'youtube.com', 'm.youtube.com'].includes(u.hostname)) {
      const values = u.searchParams.getAll('v');
      return u.pathname === '/watch' ? (values.length === 1 && /^[A-Za-z0-9_-]{11}$/.test(values[0]) ? values[0] : '') :
        u.pathname.match(/^\/(?:shorts|embed)\/([A-Za-z0-9_-]{11})\/?$/)?.[1] || '';
    }
    if (platform === 'instagram' && ['www.instagram.com', 'instagram.com'].includes(u.hostname)) {
      return u.pathname.match(/^\/(?:reel|p|tv)\/([A-Za-z0-9_-]{5,40})\/?$/)?.[1] || '';
    }
    if (platform === 'tiktok' && ['www.tiktok.com', 'tiktok.com', 'm.tiktok.com'].includes(u.hostname)) {
      return u.pathname.match(/^\/@[A-Za-z0-9_.-]+\/video\/(\d{10,25})\/?$/)?.[1] || '';
    }
    if (platform === 'bilibili' && ['www.bilibili.com', 'bilibili.com', 'm.bilibili.com'].includes(u.hostname)) {
      return u.pathname.match(/^\/video\/(BV[A-Za-z0-9]{10})\/?$/)?.[1] || '';
    }
  } catch {}
  return '';
}

function allowedMedia(platform, value) {
  try {
    const u = new URL(value), rule = RULES[platform];
    return u.protocol === 'https:' && !u.username && !u.password && !u.port && rule &&
      (rule.media_hosts.includes(u.hostname) || rule.media_suffixes.some(h =>
        u.hostname === h || u.hostname.endsWith('.' + h)));
  } catch { return false; }
}

function pageBlock(value) {
  try {
    const u = new URL(value);
    if (u.hostname === 'consent.youtube.com') return 'consent_required';
    if (/\/(?:accounts\/login|login|challenge|checkpoint)(?:\/|$)/.test(u.pathname)) return 'login_or_challenge_required';
  } catch {}
  return '';
}

async function bounded(promise, ms) {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(Object.assign(new Error(), { name: 'ProbeTimeout' })), ms);
  })]); } finally { clearTimeout(timer); }
}

function readPage({ platform, expectedId }) {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && r.bottom > 0 && r.right > 0 &&
      r.top < innerHeight && r.left < innerWidth && s.display !== 'none' &&
      s.visibility !== 'hidden' && Number(s.opacity) !== 0;
  };
  const body = (document.body?.innerText || '').slice(0, 100000);
  let blocker = '';
  if (/verify you are human|verify to continue|sign in to confirm you.re not a bot|请完成验证|安全验证|拖动滑块/i.test(body) ||
      [...document.querySelectorAll('iframe[src*="captcha"], [id*="captcha"], [class*="captcha_verify"]')].some(visible)) {
    blocker = 'captcha_required';
  } else if (/log in to (?:continue|watch|see)|sign in to confirm your age|you must be logged in|登录后观看|请登录后|登入後觀看|登入後才能|请先登录|登录后即可观看完整/i.test(body)) {
    blocker = 'login_required';
  } else if (/检测到(?:您|你)?(?:的)?(?:访问|有)?异常请求|请求被拦截|访问受限|access denied|too many requests/i.test(body)) {
    blocker = 'request_blocked';
  } else if (/this video is (?:private|unavailable)|video has been removed|this account is private|视频已删除|视频不存在|稿件不存在|视频不见了|视频去哪了/i.test(body)) {
    blocker = 'video_unavailable';
  }
  const videos = [...document.querySelectorAll('video')].filter(visible);
  const players = videos.map(v => ({ currentSrc: v.currentSrc || '', duration: Number.isFinite(v.duration) ? v.duration : null,
    readyState: v.readyState, paused: v.paused, ended: v.ended, protected: Boolean(v.mediaKeys) }));
  const metadata = name => document.querySelector(`meta[property="${name}"]`)?.content || '';
  let attestedId = '', original = '', adActive = false, protectedMedia = false, targetSources = [];
  let title = metadata('og:title') || document.title || '';
  try {
    if (platform === 'youtube') {
      const player = document.getElementById('movie_player');
      adActive = Boolean(player?.classList.contains('ad-showing'));
      const data = player?.getPlayerResponse?.();
      attestedId = data?.videoDetails?.videoId || player?.getVideoData?.()?.video_id || '';
      const state = data?.playabilityStatus?.status;
      if (state === 'LOGIN_REQUIRED') blocker = 'login_required';
      if (state === 'UNPLAYABLE' || state === 'ERROR') blocker = 'video_unavailable';
      if (attestedId === expectedId && !adActive) {
        targetSources = [...(data?.streamingData?.formats || []), ...(data?.streamingData?.adaptiveFormats || [])]
          .map(f => f.url || new URLSearchParams(f.signatureCipher || f.cipher || '').get('url')).filter(Boolean);
        const audio = data?.streamingData?.adaptiveFormats?.filter(f => /^audio\//.test(f.mimeType || '') && f.url) || [];
        original = audio.sort((a, b) => (a.bitrate || 0) - (b.bitrate || 0))[0]?.url || '';
        title = data?.videoDetails?.title || title;
        protectedMedia = Boolean(data?.streamingData?.licenseInfos?.length);
      }
    } else if (platform === 'bilibili') {
      attestedId = window.__INITIAL_STATE__?.bvid || window.__INITIAL_STATE__?.videoData?.bvid || '';
      if (attestedId === expectedId) {
        const audio = window.__playinfo__?.data?.dash?.audio || [];
        original = [...audio].sort((a, b) => (a.bandwidth || 0) - (b.bandwidth || 0))[0]?.baseUrl ||
          [...audio][0]?.base_url || '';
        targetSources = audio.flatMap(a => [a.baseUrl, a.base_url]).filter(Boolean);
      }
    } else if (platform === 'tiktok') {
      const text = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')?.textContent || '';
      if (text.length && text.length < 5000000) {
        const data = JSON.parse(text)?.__DEFAULT_SCOPE__?.['webapp.video-detail']?.itemInfo?.itemStruct;
        attestedId = data?.id || '';
        if (attestedId === expectedId) {
          original = typeof data?.video?.playAddr === 'string' ? data.video.playAddr : '';
          title = data?.desc || title;
        }
      }
    } else if (platform === 'instagram') {
      original = metadata('og:video') || metadata('og:video:url');
      // Read only already loaded public data, scoped to this post's shortcode.
      let visited = 0;
      const walk = (node, depth = 0) => {
        if (!node || typeof node !== 'object' || depth > 40 || ++visited > 100000) return;
        if (node.shortcode === expectedId || node.code === expectedId) {
          attestedId = expectedId;
          const urls = [node.video_url, node.playable_url, node.playable_url_quality_hd,
            ...(Array.isArray(node.video_versions) ? node.video_versions.map(v => v.url) : [])].filter(v => typeof v === 'string');
          targetSources.push(...urls);
          original ||= urls[0] || '';
        }
        for (const value of Object.values(node)) walk(value, depth + 1);
      };
      let total = 0;
      for (const script of document.querySelectorAll('script[type="application/json"]')) {
        const text = script.textContent || '';
        total += text.length;
        if (total > 5000000) break;
        try { walk(JSON.parse(text)); } catch {}
      }
    }
  } catch { /* Optional public player state is not required for DOM sources. */ }
  return { blocker, players, attestedId, original, targetSources, adActive, protectedMedia,
    documentReady: document.readyState,
    canonical: document.querySelector('link[rel="canonical"]')?.href || '',
    title: title.trim().slice(0, 1000), user_agent: navigator.userAgent };
}

function startPlayback() {
  const videos = [...document.querySelectorAll('video')].filter(v => {
    const r = v.getBoundingClientRect();
    return r.width > 1 && r.height > 1 && r.bottom > 0 && r.top < innerHeight;
  });
  if (videos.length === 1 && videos[0].paused) {
    videos[0].muted = true;
    videos[0].play().catch(() => {});
  }
}

function choose(platform, expectedId, snapshot, resources) {
  if (snapshot.blocker) return { category: 'BLOCKED_PLATFORM', reason: snapshot.blocker };
  if (snapshot.adActive) return null;
  if (snapshot.attestedId && snapshot.attestedId !== expectedId) return { category: 'INVALID_TARGET', reason: 'player_id_mismatch' };
  const canonical = snapshot.canonical && identity(platform, snapshot.canonical);
  if (canonical && canonical !== expectedId) return { category: 'INVALID_TARGET', reason: 'canonical_id_mismatch' };
  const ready = snapshot.players.filter(p => p.readyState >= 2 && Number.isFinite(p.duration) && p.duration > 0 && !p.ended);
  const active = ready.filter(p => !p.paused);
  if (active.length > 1 || ready.length > 1) return { category: 'INVALID_TARGET', reason: 'multiple_visible_players' };
  if (ready.length !== 1) return null;
  if (snapshot.protectedMedia || ready[0].protected) return { category: 'PROTECTED_MEDIA', reason: 'protected_media' };
  // YouTube can display an ad in the same video element: require public player identity.
  if (['youtube', 'bilibili'].includes(platform) && snapshot.attestedId !== expectedId) return null;
  const sources = platform === 'tiktok' ? [ready[0].currentSrc, snapshot.original] : [snapshot.original, ready[0].currentSrc];
  const explicit = sources.find(url => allowedMedia(platform, url) &&
    !/\.(?:m3u8|mpd)(?:$|\?)/i.test(url));
  if (explicit) return { url: explicit, duration: ready[0].duration };
  // Network candidates need a target source match; a unique ad/recommendation is not sufficient.
  const audio = [...resources.values()].filter(r => r.audio &&
    (snapshot.targetSources || []).some(source => sameAsset(platform, source, r.url)));
  if (audio.length > 1) return { category: 'NO_SUPPORTED_MEDIA', reason: 'ambiguous_original_audio' };
  if (audio.length === 1) return { url: audio[0].url, duration: ready[0].duration };
  return null;
}

function deniedForTarget(platform, snapshot, denied) {
  const sources = [snapshot.original, ...(snapshot.targetSources || []), ...snapshot.players.map(p => p.currentSrc)].filter(Boolean);
  return denied.find(r => sources.some(source => sameAsset(platform, source, r.url)));
}

async function saveInBrowser(page, platform, expectedId, selection, resources, outputPath, diagnostics) {
  // Reuse a complete response already obtained by ordinary playback when available.
  const observed = [...resources.values()].find(r => sameAsset(platform, r.url, selection.url) &&
    completeResponse(r.status, r.headers));
  let fd, handle, written = 0;
  const deadline = Date.now() + 25000;
  try {
    fd = fs.openSync(outputPath, 'wx');
    diagnostics.capture_started = true;
    if (observed) {
      const body = await bounded(observed.response.body(), Math.max(1, deadline - Date.now()));
      const declared = observed.headers['content-length'];
      if (!body.length || body.length > MAX_BYTES || declared && body.length !== Number(declared)) {
        return { category: 'NO_SUPPORTED_MEDIA', reason: 'incomplete_browser_response' };
      }
      fs.writeFileSync(fd, body);
      written = body.length;
      diagnostics.media_status = observed.status;
      diagnostics.capture_kind = 'playback_response';
    } else {
      // Native browser fetch, unchanged signed URL and ambient anonymous session.
      // No Cookie/header extraction, signature manipulation, proxy or retry.
      handle = await bounded(page.evaluateHandle(async url => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 24000);
        try {
          const response = await fetch(url, { redirect: 'error', signal: controller.signal });
          return { reader: response.body?.getReader(), status: response.status,
            headers: Object.fromEntries(['content-length', 'content-range', 'content-type'].map(k => [k, response.headers.get(k)]).filter(([,v]) => v !== null)),
            controller, timer };
        } catch (error) { clearTimeout(timer); throw error; }
      }, selection.url), Math.max(1, deadline - Date.now()));
      const metadata = await handle.evaluate(state => ({ status: state.status, headers: state.headers }));
      diagnostics.media_status = metadata.status;
      if (DENIED.has(metadata.status)) return { category: 'BLOCKED_PLATFORM', reason: 'media_access_denied' };
      if (!completeResponse(metadata.status, metadata.headers)) return { category: 'NO_SUPPORTED_MEDIA', reason: 'partial_browser_response' };
      while (true) {
        if (identity(platform, page.url()) !== expectedId) return { category: 'INVALID_TARGET', reason: 'target_changed_during_download' };
        const chunk = await bounded(handle.evaluate(async state => {
          const { done, value } = await state.reader.read();
          if (done) return null;
          let binary = '';
          for (let i = 0; i < value.length; i += 32768) binary += String.fromCharCode(...value.subarray(i, i + 32768));
          return btoa(binary);
        }), Math.max(1, deadline - Date.now()));
        if (chunk === null) break;
        const bytes = Buffer.from(chunk, 'base64');
        written += bytes.length;
        if (written > MAX_BYTES) return { category: 'MEDIA_LIMIT', reason: 'media_too_large' };
        fs.writeSync(fd, bytes);
      }
      if (!written || metadata.headers['content-length'] && written !== Number(metadata.headers['content-length'])) {
        return { category: 'NO_SUPPORTED_MEDIA', reason: 'incomplete_browser_response' };
      }
      diagnostics.capture_kind = 'browser_fetch';
    }
    if (identity(platform, page.url()) !== expectedId) return { category: 'INVALID_TARGET', reason: 'target_changed_during_download' };
    diagnostics.byte_count = written;
    return { media_path: outputPath };
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
    if (handle) {
      try { await bounded(handle.evaluate(state => { clearTimeout(state.timer); state.controller.abort(); }), 1000); } catch {}
      await handle.dispose().catch(() => {});
    }
  }
}

async function inspect(playwright, platform, pageUrl, expectedId, profilePath, options = {}) {
  const diagnostics = { navigation_status: null, browser_closed: false, media_candidates: 0 };
  const fail = (category, reason) => ({ ok: false, category, reason, diagnostics });
  if (!RULES[platform] || !expectedId || identity(platform, pageUrl) !== expectedId ||
      !path.isAbsolute(profilePath) || path.basename(profilePath) !== 'visible-browser-profile' ||
      path.basename(path.dirname(profilePath)) !== platform || path.basename(path.dirname(path.dirname(profilePath))) !== 'state' ||
      options.outputPath && (!path.isAbsolute(options.outputPath) || path.basename(options.outputPath) !== 'visible-browser_media.bin')) {
    return fail('LOCAL_BROWSER_ERROR', 'invalid_probe_input');
  }
  let context, browser, result, blocked = '', phase = 'launch', lastSnapshot;
  const resources = new Map();
  const denied = [];
  try {
    context = await playwright.chromium.launchPersistentContext(profilePath, {
      ...browserOptions(), headless: false, timeout: 15000,
      viewport: { width: 1280, height: 800 }, locale: 'en-US',
    });
    browser = context.browser();
    const page = context.pages()[0] || await context.newPage();
    page.on('response', response => {
      try {
        if (response.frame() !== page.mainFrame() || identity(platform, page.url()) !== expectedId || !allowedMedia(platform, response.url())) return;
        const u = new URL(response.url());
        const type = String(response.headers()['content-type'] || '').split(';')[0];
        const mime = u.searchParams.get('mime') || '';
        const looksMedia = /^(?:audio|video)\//.test(type) || /^(?:audio|video)\//.test(mime) || /\.(?:mp4|m4s|webm|m4a)(?:$|\?)/.test(u.pathname);
        const targetMedia = lastSnapshot && deniedForTarget(platform, lastSnapshot, [{ url: response.url() }]);
        if (!looksMedia && !targetMedia) return;
        if (DENIED.has(response.status())) {
          denied.push({ url: response.url(), status: response.status() });
          if (lastSnapshot && deniedForTarget(platform, lastSnapshot, denied)) blocked = 'media_access_denied';
          return;
        }
        if (response.status() < 200 || response.status() >= 300) return;
        const key = u.origin + u.pathname + ':' + (u.searchParams.get('itag') || '') + ':' + (u.searchParams.get('id') || '');
        resources.set(key, { url: response.url(), response, status: response.status(), headers: response.headers(),
          audio: /^audio\//.test(type) || /^audio\//.test(mime) || /media-audio|[\/_-]302\d\d[\/_-]/i.test(u.pathname) });
        diagnostics.media_candidates = resources.size;
      } catch { diagnostics.response_error = true; }
    });
    phase = 'navigation';
    const navigation = await page.goto(pageUrl, { waitUntil: 'commit', timeout: 20000 });
    diagnostics.navigation_status = navigation?.status() ?? null;
    if (DENIED.has(diagnostics.navigation_status)) result = fail('BLOCKED_PLATFORM', 'navigation_access_denied');
    const deadline = Date.now() + Math.min(25000, Math.max(1, options.observationMs ?? 25000));
    const redirectDeadline = Date.now() + Math.min(3000, Math.max(1, options.observationMs ?? 3000));
    let played = false;
    while (!result && Date.now() < deadline) {
      phase = 'observation';
      if (blocked) { result = fail('BLOCKED_PLATFORM', blocked); break; }
      const navigationBlock = pageBlock(page.url());
      if (navigationBlock) { result = fail('BLOCKED_PLATFORM', navigationBlock); break; }
      diagnostics.location_kind = locationKind(platform, page.url(), expectedId);
      let snapshot;
      try { snapshot = await bounded(page.evaluate(readPage, { platform, expectedId }), Math.max(1, deadline - Date.now())); }
      catch (error) {
        if (/Execution context was destroyed|Cannot find context/.test(error.message || '') && Date.now() < deadline) {
          await page.waitForTimeout(Math.min(250, Math.max(1, deadline - Date.now()))); continue;
        }
        throw error;
      }
      if (blocked) { result = fail('BLOCKED_PLATFORM', blocked); break; }
      lastSnapshot = snapshot;
      const denial = deniedForTarget(platform, snapshot, denied);
      if (denial) {
        diagnostics.media_status = denial.status;
        blocked = 'media_access_denied'; result = fail('BLOCKED_PLATFORM', blocked); break;
      }
      if (snapshot.blocker) { result = fail('BLOCKED_PLATFORM', snapshot.blocker); break; }
      if (identity(platform, page.url()) !== expectedId) {
        // A brief same-site transition can settle, but player identity alone never
        // authorizes a different final URL. Login/challenge is checked first.
        if (platform === 'bilibili' && (diagnostics.location_kind === 'site_home' ||
            diagnostics.location_kind === 'other_platform_page' && snapshot.documentReady === 'loading') && Date.now() < redirectDeadline) {
          await page.waitForTimeout(Math.min(250, Math.max(1, redirectDeadline - Date.now()))); continue;
        }
        result = fail(pageBlock(page.url()) ? 'BLOCKED_PLATFORM' : 'INVALID_TARGET', pageBlock(page.url()) || 'target_page_mismatch'); break;
      }
      const selection = choose(platform, expectedId, snapshot, resources);
      if (selection?.category) { result = fail(selection.category, selection.reason); break; }
      if (selection?.url) {
        if (options.outputPath) {
          phase = 'media_download';
          let saved;
          try { saved = await saveInBrowser(page, platform, expectedId, selection, resources, options.outputPath, diagnostics); }
          catch (error) {
            if (blocked || denied.some(r => sameAsset(platform, selection.url, r.url))) {
              const deniedResponse = denied.find(r => sameAsset(platform, selection.url, r.url));
              diagnostics.media_status = deniedResponse?.status;
              result = fail('BLOCKED_PLATFORM', 'media_access_denied'); break;
            }
            diagnostics.error_type = error.name === 'ProbeTimeout' ? 'ProbeTimeout' : 'Error';
            result = fail(error.code ? 'LOCAL_BROWSER_ERROR' : 'NO_SUPPORTED_MEDIA', 'browser_capture_unavailable'); break;
          }
          if (saved.category || blocked) { result = fail(blocked ? 'BLOCKED_PLATFORM' : saved.category, blocked || saved.reason); break; }
        }
        result = { ok: true, info: { platform, raw_id: expectedId, title: snapshot.title || expectedId,
          uploader: '未知', media_url: selection.url, user_agent: snapshot.user_agent,
          expected_duration: selection.duration, ...(options.outputPath ? { media_path: options.outputPath } : {}) }, diagnostics }; break;
      }
      if (!played && !snapshot.blocker && !snapshot.adActive && snapshot.players.length === 1) {
        played = true;
        await bounded(page.evaluate(startPlayback), Math.max(1, deadline - Date.now()));
      }
      await page.waitForTimeout(Math.min(250, Math.max(1, deadline - Date.now())));
    }
    if (!result) result = denied.length ? fail('BLOCKED_PLATFORM', 'unattributed_media_access_denied') : fail('NO_SUPPORTED_MEDIA', 'complete_original_audio_not_observed');
  } catch (error) {
    diagnostics.failure_phase = phase;
    diagnostics.error_type = ['TimeoutError', 'ProbeTimeout', 'Error', 'TypeError'].includes(error.name) ? error.name : 'OtherError';
    const net = String(error.message || '').match(/\bnet::ERR_[A-Z_]{1,80}\b/);
    if (net) diagnostics.network_error = net[0];
    result = blocked || denied.length ? fail('BLOCKED_PLATFORM', blocked || 'unattributed_media_access_denied') : fail('LOCAL_BROWSER_ERROR', 'browser_navigation_or_runtime_error');
  } finally {
    if (context) {
      try { await bounded(context.close(), 5000); diagnostics.browser_closed = true; }
      catch {
        if (browser) try { await bounded(browser.close(), 2000); diagnostics.browser_closed = true; } catch {}
        if (!diagnostics.browser_closed && (!result || result.ok)) result = fail('LOCAL_BROWSER_ERROR', 'browser_close_failed');
      }
    }
    if (!result?.ok && options.outputPath && fs.existsSync(options.outputPath)) {
      // A single owned output only; never remove directories or overwrite an existing file.
      if (diagnostics.capture_started) fs.unlinkSync(options.outputPath);
    }
  }
  return result;
}

module.exports = { inspect, identity, allowedMedia, choose, pageBlock, sameAsset, deniedForTarget, locationKind, completeResponse, saveInBrowser, readPage };
if (require.main === module) {
  const [modulePath, platform, pageUrl, id, profile, outputPath] = process.argv.slice(2);
  inspect(require(modulePath), platform, pageUrl, id, profile, { outputPath }).then(result => {
    process.stdout.write(JSON.stringify(result)); if (!result.ok) process.exitCode = 1;
  }).catch(() => { process.stdout.write(JSON.stringify({ ok: false, category: 'LOCAL_BROWSER_ERROR', reason: 'probe_runtime_error' })); process.exitCode = 1; });
}
