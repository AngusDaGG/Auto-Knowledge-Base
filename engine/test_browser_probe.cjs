'use strict';
// Keep the established CI entry point running both browser strategies.
require('./test_visible_browser_probe.cjs');
require('./test_platform_browser_probe.cjs');
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { inspect } = require('./browser_probe.cjs');

function environment({ apiStatus = 200, body = '', navigationError = '', apiBody = null, detached = false } = {}) {
  let callback;
  const calls = { navigation: 0, closed: false, contexts: 0 };
  const page = {
    on: (event, cb) => { if (event === 'response') callback = cb; },
    goto: async () => {
      calls.navigation++;
      const task = callback({
        url: () => 'https://www.douyin.com/aweme/v1/web/aweme/detail/',
        status: () => apiStatus,
        json: async () => ({ aweme_detail: { aweme_id: '12345', desc: 'title', video: {} } }),
        text: async () => {
          if (detached) await new Promise(resolve => setTimeout(resolve, 5));
          return apiBody ?? JSON.stringify({ aweme_detail: { aweme_id: '12345', desc: 'title', video: {} } });
        },
      });
      if (!detached) await task;
      if (navigationError) throw new Error(navigationError);
      return { status: () => 200 };
    },
    locator: () => ({ innerText: async () => body, first: () => ({ isVisible: async () => false }) }),
    url: () => 'https://www.douyin.com/video/12345',
    waitForTimeout: async () => new Promise(resolve => setImmediate(resolve)),
  };
  const playwright = { chromium: { launch: async options => {
    assert.equal(options.channel, 'msedge');
    assert.equal(options.headless, true);
    assert.equal(options.userDataDir, undefined);
    return {
      version: () => 'test',
      newContext: async () => { calls.contexts++; return { newPage: async () => page }; },
      close: async () => { calls.closed = true; },
    };
  } } };
  return { playwright, calls };
}

test('normal navigation observes the matching response and closes only its browser', async () => {
  const env = environment();
  const result = await inspect(env.playwright, 'https://www.douyin.com/video/12345', '12345');
  assert.equal(result.ok, true);
  assert.equal(result.aweme.aweme_id, '12345');
  assert.deepEqual(env.calls, { navigation: 1, closed: true, contexts: 1 });
});

test('API 403 stops without replay or another navigation', async () => {
  const env = environment({ apiStatus: 403 });
  const result = await inspect(env.playwright, 'https://www.douyin.com/video/12345', '12345');
  assert.equal(result.ok, false);
  assert.match(result.reason, /403/);
  assert.equal(env.calls.navigation, 1);
  assert.equal(env.calls.closed, true);
});

test('visible captcha stops even when a detail response was available', async () => {
  const env = environment({ body: '请完成验证' });
  const result = await inspect(env.playwright, 'https://www.douyin.com/video/12345', '12345');
  assert.equal(result.ok, false);
  assert.match(result.reason, /驗證/);
  assert.equal(env.calls.closed, true);
});

test('diagnostics never retain a navigation URL query or authorization value', async () => {
  const env = environment({ navigationError: 'request https://www.douyin.com/video/12345?signature=SECRET_VALUE failed' });
  const result = await inspect(env.playwright, 'https://www.douyin.com/video/12345', '12345');
  assert.doesNotMatch(JSON.stringify(result.diagnostics), /SECRET_VALUE|signature=/);
});

test('Argus 403 is classified without retaining the response body', async () => {
  const env = environment({ apiStatus: 403, apiBody: 'Blocked by ArgusSecurityPlugin Uifid Not Found; signature=DO_NOT_STORE' });
  const result = await inspect(env.playwright, 'https://www.douyin.com/video/12345', '12345');
  assert.equal(result.category, 'BLOCKED_PLATFORM');
  assert.equal(result.diagnostics.detail_responses[0].argus_kind, 'uifid_missing');
  assert.equal(result.diagnostics.detail_responses[0].body_type, 'text');
  assert.equal(result.diagnostics.browser_closed, true);
  assert.doesNotMatch(JSON.stringify(result), /DO_NOT_STORE|signature=/);
});

test('empty and HTML denial bodies remain distinct and do not trigger a retry', async () => {
  for (const [apiBody, expected] of [['', 'empty'], ['<html>Forbidden</html>', 'html']]) {
    const env = environment({ apiStatus: 403, apiBody });
    const result = await inspect(env.playwright, 'https://www.douyin.com/video/12345', '12345');
    assert.equal(result.diagnostics.detail_responses[0].body_type, expected);
    assert.equal(result.diagnostics.detail_responses[0].argus_kind, 'not_observed');
    assert.equal(env.calls.navigation, 1);
  }
});

test('a blocked job does not contaminate the next isolated job', async () => {
  const denied = environment({ apiStatus: 403 });
  const allowed = environment();
  assert.equal((await inspect(denied.playwright, 'https://www.douyin.com/video/12345', '12345')).ok, false);
  assert.equal((await inspect(allowed.playwright, 'https://www.douyin.com/video/12345', '12345')).ok, true);
  assert.equal(denied.calls.closed && allowed.calls.closed, true);
  assert.equal(denied.calls.contexts, 1);
  assert.equal(allowed.calls.contexts, 1);
});

test('response parsing can finish after goto without being lost or closed early', async () => {
  const env = environment({ detached: true });
  const result = await inspect(env.playwright, 'https://www.douyin.com/video/12345', '12345');
  assert.equal(result.ok, true);
  assert.equal(result.diagnostics.detail_responses[0].body_type, 'json');
  assert.equal(result.diagnostics.browser_closed, true);
});
