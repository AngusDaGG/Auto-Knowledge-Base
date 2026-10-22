'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const { inspect } = require('./visible_browser_probe.cjs');

const PAGE = 'https://www.douyin.com/video/12345';
const AUDIO = 'https://v3.douyinvod.com/original/media-audio-abc123/?signed=PRIVATE';
const PROFILE = path.join(os.tmpdir(), 'probe-tests', 'state', 'visible-browser-profile');
const readyPlayer = (overrides = {}) => ({ duration: 350, readyState: 4, paused: false,
  muted: false, ended: false, currentSrc: 'blob:https://www.douyin.com/original-player', ...overrides });

function environment(options = {}) {
  const callbacks = {};
  const calls = { navigations: 0, waits: 0, snapshots: 0, contextClosed: 0, browserClosed: 0 };
  const frame = {};
  const response = (url, status = 206, contentType = 'audio/mp4', ownFrame = true) => ({
    url: () => url, status: () => status, headers: () => ({ 'content-type': contentType }),
    frame: () => ownFrame ? frame : {},
  });
  let currentUrl = PAGE;
  const page = {
    on: (event, cb) => { callbacks[event] = cb; },
    mainFrame: () => frame,
    goto: async () => {
      calls.navigations++;
      if (options.gotoError) throw options.gotoError;
      currentUrl = options.finalUrl || PAGE;
      for (const item of options.responses ?? [{ url: AUDIO }]) {
        callbacks.response(response(item.url, item.status, item.contentType, item.ownFrame));
      }
      return { status: () => options.navigationStatus ?? 200 };
    },
    url: () => currentUrl,
    evaluate: async fn => {
      calls.snapshots++;
      for (const item of options.evaluationResponses ?? []) {
        callbacks.response(response(item.url, item.status, item.contentType, item.ownFrame));
      }
      if (options.snapshotUrls?.[calls.snapshots - 1]) currentUrl = options.snapshotUrls[calls.snapshots - 1];
      const snapshotError = options.snapshotErrors?.[calls.snapshots - 1] ?? options.snapshotError;
      if (snapshotError) throw snapshotError;
      if (options.evaluate) return options.evaluate(fn);
      return { blocker: options.blocker || '', title: '可見標題', uploader: options.uploader,
        players: options.players ?? [readyPlayer()] };
    },
    waitForTimeout: async ms => { calls.waits++; await new Promise(resolve => setTimeout(resolve, ms)); },
  };
  const context = {
    browser: () => ({ close: async () => {
      calls.browserClosed++;
      if (options.browserCloseError) throw new Error('PRIVATE browser close error');
    } }),
    pages: () => [page],
    close: async () => { calls.contextClosed++; if (options.closeError) throw new Error('PRIVATE close error'); },
  };
  const playwright = { chromium: { launchPersistentContext: async (profilePath, launchOptions) => {
    assert.equal(profilePath, PROFILE);
    assert.equal(launchOptions.channel, 'msedge');
    assert.equal(launchOptions.headless, false);
    assert.equal(launchOptions.storageState, undefined);
    assert.equal(launchOptions.args, undefined);
    if (options.launchError) throw options.launchError;
    return context;
  } } };
  return { playwright, calls };
}

function run(env, options = {}) {
  return inspect(env.playwright, PAGE, '12345', PROFILE, { observationMs: 15, pollMs: 3, ...options });
}

test('observes original audio in visible persistent Edge and closes its own context', async () => {
  const env = environment();
  const result = await run(env);
  assert.equal(result.ok, true);
  assert.deepEqual(result.info, { id: '12345', title: '可見標題', uploader: '未知',
    video: { audio: { play_url: { url_list: [AUDIO] } } }, download_method: 'visible-browser', expected_duration: 350 });
  assert.equal(result.diagnostics.browser_closed, true);
  assert.equal(env.calls.navigations, 1);
  assert.equal(env.calls.contextClosed, 1);
  assert.equal(env.calls.waits, 0);
  assert.doesNotMatch(JSON.stringify(result.diagnostics), /PRIVATE|https:|signed=/);
});

test('metadata API 403 does not override successfully loaded original audio', async () => {
  const env = environment({ responses: [
    { url: 'https://www.douyin.com/aweme/v1/web/aweme/detail/?secret=PRIVATE', status: 403 },
    { url: AUDIO },
  ] });
  const result = await run(env);
  assert.equal(result.ok, true);
  assert.equal(result.diagnostics.audio_resources, 1);
  assert.doesNotMatch(JSON.stringify(result.diagnostics), /PRIVATE|https:|secret=/);
});

test('navigation denial stops before DOM observation and still closes', async () => {
  for (const status of [401, 403, 429]) {
    const env = environment({ navigationStatus: status });
    const result = await run(env);
    assert.equal(result.category, 'BLOCKED_PLATFORM');
    assert.equal(result.reason, 'navigation_access_denied');
    assert.equal(env.calls.snapshots, 0);
    assert.equal(env.calls.contextClosed, 1);
  }
});

test('visible captcha, required login, and unavailable video stop', async () => {
  for (const blocker of ['captcha_required', 'login_required', 'video_unavailable']) {
    const env = environment({ blocker });
    const result = await run(env);
    assert.equal(result.category, 'BLOCKED_PLATFORM');
    assert.equal(result.reason, blocker);
    assert.equal(env.calls.waits, 0);
    assert.equal(result.diagnostics.browser_closed, true);
  }
});

test('visible DOM inspection detects blockers and ignores an ordinary login button or hidden captcha', async () => {
  const node = (text, hidden = false) => ({ innerText: text, hidden,
    getBoundingClientRect: () => ({ width: 640, height: 400, top: 0, left: 0, right: 640, bottom: 400 }) });
  for (const [body, captcha, expected] of [
    ['请完成验证', null, 'captcha_required'], ['请登录后观看', null, 'login_required'],
    ['视频已删除', null, 'video_unavailable'], ['登录', node('', false), 'captcha_required'],
    ['登录', node('', true), ''],
  ]) {
    const video = Object.assign(node(''), readyPlayer());
    const env = environment({ evaluate: fn => vm.runInNewContext(`(${fn.toString()})()`, {
      document: { body: { innerText: body }, querySelectorAll: selector => {
        if (selector === 'h1') return [node('可見標題')];
        if (selector === 'video') return [video];
        if (selector.includes('iframe')) return captcha ? [captcha] : [];
        return [];
      } },
      getComputedStyle: element => ({ display: element.hidden ? 'none' : 'block', visibility: 'visible', opacity: '1' }),
      innerHeight: 800, innerWidth: 1280,
    }) });
    const result = await run(env);
    assert.equal(result.ok, expected === '');
    if (expected) assert.equal(result.reason, expected);
  }
});

test('rejects navigation to another video or non-video page', async () => {
  for (const [finalUrl, kind] of [['https://www.douyin.com/video/99999', 'canonical_other_video'],
    ['https://www.douyin.com/', 'other_douyin_page'], ['https://private.example/?secret=PRIVATE', 'external_page']]) {
    const env = environment({ finalUrl });
    const result = await run(env);
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'target_page_mismatch');
    assert.equal(result.diagnostics.target_page_kind, kind);
    if (kind !== 'other_douyin_page') assert.equal(env.calls.waits, 0);
    assert.doesNotMatch(JSON.stringify(result), /99999|private.example|secret=/);
  }
});

test('a transient Douyin loading page can settle on the original target within the same deadline', async () => {
  const source = 'https://v3.douyinvod.com/original/video.mp4';
  const env = environment({ finalUrl: 'https://www.douyin.com/loading?private=PRIVATE', responses: [],
    snapshotUrls: ['https://www.douyin.com/loading?private=PRIVATE', PAGE],
    players: [readyPlayer({ currentSrc: source })] });
  const result = await run(env, { observationMs: 100 });
  assert.equal(result.ok, true);
  assert.equal(result.diagnostics.target_page_kind, 'canonical_target');
  assert.equal(env.calls.snapshots, 2);
  assert.equal(env.calls.navigations, 1);
  assert.doesNotMatch(JSON.stringify(result.diagnostics), /PRIVATE|private=/);
});

test('requires finite duration and a ready target player within a bounded wait', async () => {
  for (const player of [readyPlayer({ duration: Infinity }), readyPlayer({ duration: 0 }), readyPlayer({ readyState: 1 })]) {
    const env = environment({ players: [player] });
    const result = await run(env);
    assert.equal(result.reason, 'ready_player_not_observed');
    assert.ok(env.calls.waits > 0 && env.calls.waits < 15);
    assert.ok(result.diagnostics.timings_ms.total < 500);
    assert.equal(env.calls.navigations, 1);
    assert.equal(env.calls.contextClosed, 1);
  }
});

test('does not select music, arbitrary media, unapproved CDN, or another frame', async () => {
  for (const item of [
    { url: 'https://v3.douyinvod.com/music/media-audio-bgm/' },
    { url: 'https://v3.douyinvod.com/original/any-audio.mp4' },
    { url: 'https://unknown.example/media-audio-original/' },
    { url: AUDIO, ownFrame: false },
    { url: AUDIO, contentType: 'text/html' },
  ]) {
    const env = environment({ responses: [item] });
    const result = await run(env);
    assert.equal(result.reason, 'original_audio_not_observed');
    assert.equal(result.diagnostics.audio_resources, 0);
  }
});

test('multiple active players and multiple original audio assets are rejected', async () => {
  const result = await run(environment({ players: [readyPlayer(), readyPlayer()] }));
  assert.equal(result.reason, 'multiple_active_players');
  const ambiguous = await run(environment({ responses: [
    { url: AUDIO }, { url: 'https://v3.douyinvod.com/recommendation/media-audio-other/' },
  ] }));
  assert.equal(ambiguous.reason, 'ambiguous_original_audio');
});

test('ready player diagnostics use fixed source kinds without retaining URLs', async () => {
  for (const [currentSrc, current_src_kind] of [
    ['', 'none'], ['blob:https://www.douyin.com/PRIVATE', 'blob'],
    ['https://v3.douyinvod.com/original/video.mp4?signature=PRIVATE', 'allowlisted_direct'],
    ['https://private.example/PRIVATE?token=PRIVATE', 'disallowed_other'],
  ]) {
    const result = await run(environment({ responses: [],
      players: [readyPlayer({ currentSrc, paused: true, muted: true, duration: 644.726 })] }));
    assert.equal(result.ok, current_src_kind === 'allowlisted_direct');
    assert.equal(result.diagnostics.active_players, 0);
    assert.equal(result.diagnostics.ready_players, 1);
    assert.deepEqual(result.diagnostics.ready_player_observations, [{
      player_index: 0, current_src_kind, paused: true, muted: true, duration_seconds: 644.726,
    }]);
    assert.ok(Number.isFinite(result.diagnostics.timings_ms.observation));
    assert.doesNotMatch(JSON.stringify(result.diagnostics), /PRIVATE|https:|signature=|token=/);
  }
});

test('zero and multiple ready player diagnostics preserve existing selection rules', async () => {
  const empty = await run(environment({ players: [] }));
  assert.equal(empty.reason, 'ready_player_not_observed');
  assert.deepEqual(empty.diagnostics.ready_player_observations, []);
  for (const paused of [false, true]) {
    const result = await run(environment({ players: [readyPlayer({ paused }),
      readyPlayer({ paused, muted: true, duration: 20 })] }));
    assert.equal(result.category, 'INVALID_TARGET');
    assert.equal(result.reason, 'multiple_active_players');
    assert.equal(result.diagnostics.active_players, paused ? 0 : 2);
    assert.deepEqual(result.diagnostics.ready_player_observations, [
      { player_index: 0, current_src_kind: 'blob', paused, muted: false, duration_seconds: 350 },
      { player_index: 1, current_src_kind: 'blob', paused, muted: true, duration_seconds: 20 },
    ]);
  }
  const unready = await run(environment({ players: [readyPlayer({ duration: Infinity })] }));
  assert.deepEqual(unready.diagnostics.ready_player_observations, []);
});

test('allowlisted response exclusions retain only first-failure counts', async () => {
  for (const [kind, item, extra] of [
    ['frame', { url: AUDIO, ownFrame: false }, {}],
    ['target', { url: AUDIO }, { finalUrl: 'https://www.douyin.com/loading?token=PRIVATE', snapshotUrls: [PAGE] }],
    ['path', { url: 'https://v3.douyinvod.com/PRIVATE?token=PRIVATE' }, {}],
    ['mime', { url: AUDIO, contentType: 'text/PRIVATE' }, {}],
    ['status', { url: AUDIO, status: 500 }, {}],
  ]) {
    const result = await run(environment({ responses: [item], ...extra }));
    const expected = { frame: 0, target: 0, path: 0, mime: 0, status: 0 };
    expected[kind] = 1;
    assert.equal(result.reason, 'original_audio_not_observed');
    assert.deepEqual(result.diagnostics.response_excluded, expected);
    assert.doesNotMatch(JSON.stringify(result.diagnostics), /PRIVATE|https:|token=/);
  }
  const outside = await run(environment({ responses: [{ url: 'https://private.example/media-audio-PRIVATE/' }] }));
  assert.deepEqual(outside.diagnostics.response_excluded, { frame: 0, target: 0, path: 0, mime: 0, status: 0 });
});

test('CDN denial of original audio or the actual direct player source stops', async () => {
  const deniedAudio = await run(environment({ responses: [{ url: AUDIO, status: 403 }] }));
  assert.equal(deniedAudio.category, 'BLOCKED_PLATFORM');
  assert.equal(deniedAudio.reason, 'media_access_denied');
  assert.equal(deniedAudio.diagnostics.response_excluded.status, 1);
  const source = 'https://v3.douyinvod.com/original/video.mp4';
  const deniedVideo = await run(environment({ responses: [{ url: source, status: 429 }],
    players: [readyPlayer({ currentSrc: source })] }));
  assert.equal(deniedVideo.reason, 'media_access_denied');
});

test('media denial arriving during a pending snapshot is not overwritten by its timeout', async () => {
  const env = environment({ responses: [], evaluationResponses: [{ url: AUDIO, status: 403 }],
    evaluate: () => new Promise(() => {}) });
  const result = await run(env, { observationMs: 40 });
  assert.equal(result.ok, false);
  assert.equal(result.category, 'BLOCKED_PLATFORM');
  assert.equal(result.reason, 'media_access_denied');
  assert.equal(result.diagnostics.error_type, 'ProbeTimeout');
  assert.ok(Number.isFinite(result.diagnostics.timings_ms.observation));
  assert.ok(result.diagnostics.timings_ms.observation >= 30);
  assert.equal(env.calls.snapshots, 1);
  assert.equal(env.calls.navigations, 1);
  assert.equal(env.calls.contextClosed, 1);
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE|signed=/);
});

test('range requests for one original asset do not look like recommendation audio', async () => {
  const result = await run(environment({ responses: [{ url: AUDIO },
    { url: AUDIO.replace('PRIVATE', 'REFRESHED') }] }));
  assert.equal(result.ok, true);
  assert.equal(result.diagnostics.audio_resources, 1);
});

test('uses a direct currentSrc from the single target player when no original audio was observed', async () => {
  const source = 'https://v3.douyinvod.com/original/direct-video.mp4?token=PRIVATE';
  const result = await run(environment({ responses: [], players: [readyPlayer({ currentSrc: source })] }));
  assert.equal(result.ok, true);
  assert.equal(result.info.video.audio.play_url.url_list[0], source);
  assert.equal(result.diagnostics.media_source, 'player_current_src');
});

test('the target direct currentSrc wins over recommendation audio preloads', async () => {
  const source = 'https://v3.douyinvod.com/original/target-video.mp4';
  for (const responses of [[{ url: 'https://v3.douyinvod.com/recommendation/media-audio-other/' }],
    [{ url: AUDIO }, { url: 'https://v3.douyinvod.com/recommendation/media-audio-other/' }]]) {
    const result = await run(environment({ responses, players: [readyPlayer({ currentSrc: source })] }));
    assert.equal(result.ok, true);
    assert.equal(result.info.video.audio.play_url.url_list[0], source);
    assert.equal(result.diagnostics.media_source, 'player_current_src');
  }
});

test('runtime errors close the context and never include exception text', async () => {
  const env = environment({ snapshotError: new Error('https://private.example/?cookie=PRIVATE') });
  const result = await run(env);
  assert.equal(result.category, 'LOCAL_BROWSER_ERROR');
  assert.equal(env.calls.contextClosed, 1);
  assert.equal(result.diagnostics.failure_phase, 'observation');
  assert.equal(result.diagnostics.error_type, 'Error');
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE|cookie=|private.example/);
});

test('a normal 1.4 second snapshot succeeds within the five second observation deadline', async () => {
  const env = environment({ evaluate: async () => {
    await new Promise(resolve => setTimeout(resolve, 1400));
    return { blocker: '', title: '可見標題', players: [readyPlayer()] };
  } });
  const result = await run(env, { observationMs: 5000 });
  assert.equal(result.ok, true);
  assert.equal(env.calls.snapshots, 1);
  assert.equal(env.calls.waits, 0);
  assert.equal(env.calls.contextClosed, 1);
  assert.ok(result.diagnostics.timings_ms.observation >= 1300);
  assert.ok(result.diagnostics.timings_ms.total < 5000);
});

test('a snapshot exceeding the overall deadline stops without stacking another evaluation', async () => {
  const env = environment({ evaluate: () => new Promise(() => {}) });
  const result = await run(env, { observationMs: 40 });
  assert.equal(result.ok, false);
  assert.equal(result.category, 'LOCAL_BROWSER_ERROR');
  assert.equal(result.reason, 'observation_deadline_exceeded');
  assert.equal(result.diagnostics.error_type, 'ProbeTimeout');
  assert.equal(result.diagnostics.failure_phase, 'observation');
  assert.ok(Number.isFinite(result.diagnostics.timings_ms.observation));
  assert.ok(result.diagnostics.timings_ms.observation >= 30);
  assert.ok(result.diagnostics.timings_ms.observation < 500);
  assert.equal(env.calls.snapshots, 1);
  assert.equal(env.calls.waits, 0);
  assert.equal(env.calls.contextClosed, 1);
  assert.ok(result.diagnostics.timings_ms.total >= 30);
  assert.ok(result.diagnostics.timings_ms.total < 500);
});

test('navigation diagnostics retain only a fixed network error and safe error name', async () => {
  const env = environment({ gotoError: Object.assign(new Error(
    'page.goto: net::ERR_CONNECTION_RESET at https://private.example/?cookie=PRIVATE'), { name: 'TypeError' }) });
  const result = await run(env);
  assert.equal(result.reason, 'launch_or_runtime_error');
  assert.equal(result.diagnostics.failure_phase, 'navigation');
  assert.equal(result.diagnostics.error_type, 'TypeError');
  assert.equal(result.diagnostics.network_error, 'net::ERR_CONNECTION_RESET');
  assert.equal(env.calls.contextClosed, 1);
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE|cookie=|private.example/);
});

test('a page context change is retried only inside the observation deadline', async () => {
  const env = environment({ snapshotErrors: [new Error(
    'Execution context was destroyed, most likely because of a navigation PRIVATE'), null] });
  const result = await run(env, { observationMs: 100 });
  assert.equal(result.ok, true);
  assert.equal(result.diagnostics.context_changes, 1);
  assert.equal(env.calls.snapshots, 2);
  assert.equal(env.calls.navigations, 1);
  assert.equal(env.calls.contextClosed, 1);
  assert.equal(result.diagnostics.failure_phase, undefined);
  assert.doesNotMatch(JSON.stringify(result.diagnostics), /PRIVATE/);
});

test('continuous page context changes stop at the deadline without navigating again', async () => {
  for (const message of ['Execution context was destroyed', 'Cannot find context with specified id', 'Inspected target navigated']) {
    const env = environment({ snapshotError: new Error(message + ' PRIVATE') });
    const result = await run(env, { observationMs: 60 });
    assert.equal(result.ok, false);
    assert.equal(result.category, 'LOCAL_BROWSER_ERROR');
    assert.equal(result.diagnostics.reason_code, 'page_context_changed');
    assert.equal(result.diagnostics.failure_phase, 'observation');
    assert.ok(env.calls.snapshots > 1 && env.calls.snapshots < 20);
    assert.ok(result.diagnostics.timings_ms.total < 500);
    assert.equal(env.calls.navigations, 1);
    assert.equal(env.calls.contextClosed, 1);
    assert.doesNotMatch(JSON.stringify(result), /PRIVATE/);
  }
});

test('a closed page fails immediately and visible blockers after a context change still stop', async () => {
  const closed = environment({ snapshotError: new Error('Target page, context or browser has been closed PRIVATE') });
  const closedResult = await run(closed);
  assert.equal(closedResult.diagnostics.reason_code, 'page_closed');
  assert.equal(closed.calls.snapshots, 1);
  assert.equal(closed.calls.waits, 0);
  assert.equal(closed.calls.contextClosed, 1);
  const blocked = environment({ snapshotErrors: [new Error('Execution context was destroyed'), null], blocker: 'captcha_required' });
  const blockedResult = await run(blocked, { observationMs: 100 });
  assert.equal(blockedResult.category, 'BLOCKED_PLATFORM');
  assert.equal(blockedResult.reason, 'captcha_required');
  assert.equal(blocked.calls.navigations, 1);
});

test('launch failures map known causes and never retain arbitrary names or messages', async () => {
  for (const [message, reasonCode] of [
    ['ProcessSingleton profile PRIVATE is in use', 'profile_locked'],
    ["Executable doesn't exist at C:\\PRIVATE\\edge.exe", 'browser_missing'],
    ['EPERM: permission denied C:\\PRIVATE\\profile', 'permission_denied'],
  ]) {
    const result = await run(environment({ launchError: Object.assign(new Error(message), { name: 'PRIVATE' }) }));
    assert.equal(result.reason, 'launch_or_runtime_error');
    assert.equal(result.diagnostics.failure_phase, 'launch');
    assert.equal(result.diagnostics.error_type, 'OtherError');
    assert.equal(result.diagnostics.reason_code, reasonCode);
    assert.doesNotMatch(JSON.stringify(result), /PRIVATE|edge.exe/);
  }
});

test('context close failure falls back only to the browser created by this probe', async () => {
  const env = environment({ closeError: true });
  const result = await run(env);
  assert.equal(result.diagnostics.browser_closed, true);
  assert.equal(env.calls.contextClosed, 1);
  assert.equal(env.calls.browserClosed, 1);
});

test('both close failures preserve an existing platform block or invalid target', async () => {
  for (const [options, category, reason] of [
    [{ navigationStatus: 403 }, 'BLOCKED_PLATFORM', 'navigation_access_denied'],
    [{ finalUrl: 'https://www.douyin.com/video/99999' }, 'INVALID_TARGET', 'target_page_mismatch'],
  ]) {
    const env = environment({ ...options, closeError: true, browserCloseError: true });
    const result = await run(env);
    assert.equal(result.ok, false);
    assert.equal(result.category, category);
    assert.equal(result.reason, reason);
    assert.equal(result.diagnostics.browser_closed, false);
    assert.equal(result.diagnostics.close_failed, true);
    assert.equal(env.calls.contextClosed, 1);
    assert.equal(env.calls.browserClosed, 1);
    assert.doesNotMatch(JSON.stringify(result), /PRIVATE/);
  }
});

test('rejects non-dedicated profile and invalid target before launching', async () => {
  const env = environment();
  for (const profilePath of [path.join(os.tmpdir(), 'User Data'), 'visible-browser-profile']) {
    const result = await inspect(env.playwright, PAGE, '12345', profilePath);
    assert.equal(result.reason, 'invalid_probe_input');
  }
  const invalid = await inspect(env.playwright, PAGE, '99999', PROFILE);
  assert.equal(invalid.reason, 'invalid_probe_input');
  assert.equal(env.calls.navigations, 0);
});
