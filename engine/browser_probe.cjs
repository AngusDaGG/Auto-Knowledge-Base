/* Observe ordinary page responses in a fresh, nonpersistent Edge context.
 * No CDP attachment, profile loading, cookie export, stealth or API replay.
 */
'use strict';
const { browserOptions } = require('./browser_config.cjs');

async function bounded(promise, milliseconds) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(Object.assign(new Error(), { name: 'BodyReadTimeout' })), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}

function argusKind(text) {
  if (!/ArgusSecurityPlugin/i.test(text)) return 'not_observed';
  if (/uifid\s+not\s+found/i.test(text)) return 'uifid_missing';
  if (/signature\s+not\s+found/i.test(text)) return 'signature_missing';
  if (/sign(?:ature)?\s+invalid/i.test(text)) return 'signature_invalid';
  return 'other_argus_rejection';
}

async function inspect(playwright, pageUrl, expectedId) {
  const started = performance.now();
  const diagnostics = { detail_responses: [], page_errors: [], browser_closed: false, timings_ms: {} };
  const browser = await playwright.chromium.launch({ ...browserOptions(), headless: true, timeout: 15000 });
  diagnostics.timings_ms.launch = Math.round(performance.now() - started);
  diagnostics.browser_version = browser.version();
  let aweme = null;
  let blocked = '';
  try {
    const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1280, height: 720 } });
    const page = await context.newPage();
    const visibleBlocker = async () => {
      const body = await page.locator('body').innerText({ timeout: 1000 }).catch(() => '');
      const reason = body.match(/请完成验证|請完成驗證|拖动滑块|拖動滑塊|安全验证|安全驗證|登录后观看|登录后才能|登入後觀看|视频不存在|视频已删除|视频暂不可用/);
      const captcha = await page.locator('iframe[src*="captcha"], iframe[src*="verifycenter"]').first().isVisible().catch(() => false);
      return reason || captcha ? `頁面要求驗證／登入或影片不可用：${reason?.[0] || '可見驗證框'}` : '';
    };
    const pending = new Set();
    const handleResponse = async response => {
      const url = new URL(response.url());
      if (!(url.hostname === 'douyin.com' || url.hostname.endsWith('.douyin.com')) ||
          !url.pathname.includes('/aweme/detail')) return;
      const received = performance.now();
      const entry = { api_type: 'aweme_detail', status: response.status(), body_type: 'unavailable', argus_kind: 'not_observed', target_found: false };
      diagnostics.detail_responses.push(entry);
      if ([401, 403, 429].includes(entry.status)) {
        entry.access_denied = true;
        blocked = `公開影片資訊 API HTTP ${entry.status}，停止此路徑`;
      }
      try {
        const raw = await bounded(response.text(), 1000);
        entry.byte_count = Buffer.byteLength(raw);
        entry.argus_kind = argusKind(raw.slice(0, 4096));
        entry.body_type = raw.trim() ? (/^\s*</.test(raw) ? 'html' : 'text') : 'empty';
        if (entry.argus_kind !== 'not_observed') {
          entry.access_denied = true;
          blocked = `公開 API Argus 驗證拒絕：${entry.argus_kind}`;
        }
        let payload;
        try { payload = JSON.parse(raw); entry.body_type = 'json'; } catch { return; }
        if (!payload || typeof payload !== 'object') return;
        if (Number.isInteger(payload.status_code)) entry.status_code = payload.status_code;
        const message = String(payload.status_msg || payload.message || '');
        if (payload.status_code && /登录|登入|验证|驗證|captcha|unauthorized/i.test(message)) {
          entry.access_denied = true;
          blocked = '公開 API 要求登入或驗證，停止此路徑';
          return;
        }
        if (String(payload.aweme_detail?.aweme_id || '') === expectedId) {
          entry.target_found = true;
          const detail = payload.aweme_detail;
          aweme = { aweme_id: detail.aweme_id, desc: detail.desc,
                    author: { nickname: detail.author?.nickname }, video: detail.video };
        }
      } catch (error) {
        entry.parse_error = error.name;
      } finally { entry.elapsed_ms = Math.round(performance.now() - received); }
    };
    page.on('response', response => {
      const task = handleResponse(response).catch(() => { diagnostics.response_error = 'ResponseHandlerError'; });
      pending.add(task);
      task.finally(() => pending.delete(task));
      return task;
    });
    page.on('pageerror', error => diagnostics.page_errors.push(error.name === 'TypeError' ? 'TypeError' : 'PageError'));
    page.on('requestfailed', request => {
      const url = new URL(request.url());
      if (url.pathname.includes('/aweme/detail')) diagnostics.detail_request_failed = true;
    });
    const navigationStarted = performance.now();
    try {
      const response = await page.goto(pageUrl, { waitUntil: 'commit', timeout: 20000 });
      diagnostics.navigation_status = response?.status();
      if ([401, 403, 429].includes(diagnostics.navigation_status)) {
        blocked = `公開頁面 HTTP ${diagnostics.navigation_status}，停止此路徑`;
      }
    } catch (error) {
      diagnostics.navigation_error = error.name === 'TimeoutError' ? 'TimeoutError' : 'NavigationError';
    }
    diagnostics.timings_ms.navigation = Math.round(performance.now() - navigationStarted);
    const metadataStarted = performance.now();
    const deadline = Date.now() + 18000;
    let nextVisibleCheck = 0;
    while (!blocked && !aweme && Date.now() < deadline) {
      if (Date.now() >= nextVisibleCheck) {
        nextVisibleCheck = Date.now() + 1000;
        blocked = await visibleBlocker();
      }
      if (!blocked && !aweme) await page.waitForTimeout(250);
    }
    // A detail response alone must not override a visible access challenge.
    if (!blocked) blocked = await visibleBlocker();
    await bounded(Promise.all([...pending]), 1200).catch(() => {});
    diagnostics.timings_ms.metadata = Math.round(performance.now() - metadataStarted);
    diagnostics.page_type = /^\/video\/\d+\/?$/.test(new URL(page.url()).pathname) ? 'video' : 'other';
    if (blocked) return { ok: false, category: 'BLOCKED_PLATFORM', reason: blocked, diagnostics };
    if (!aweme) return { ok: false, reason: '未登入公開頁面未傳回目標 aweme/detail 資料，已停止', diagnostics };
    return { ok: true, aweme, diagnostics };
  } finally {
    await browser.close();
    diagnostics.browser_closed = true;
    diagnostics.timings_ms.total = Math.round(performance.now() - started);
  }
}

module.exports = { inspect };
if (require.main === module) {
  const [modulePath, pageUrl, expectedId] = process.argv.slice(2);
  inspect(require(modulePath), pageUrl, expectedId).then(result => {
    process.stdout.write(JSON.stringify(result));
    if (!result.ok) process.exitCode = 1;
  }).catch(error => {
    process.stdout.write(JSON.stringify({ ok: false, category: 'LOCAL_BROWSER_ERROR', reason: error.name === 'TimeoutError' ? 'Playwright TimeoutError' : 'Playwright launch or runtime error' }));
    process.exitCode = 1;
  });
}
