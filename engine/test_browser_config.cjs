"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const { browserOptions } = require('./browser_config.cjs');
test('explicit installed browser path is selected without profile access', () => {
  assert.deepEqual(browserOptions({ AKB_BROWSER_EXECUTABLE: __filename }), { executablePath: __filename });
});
test('only normal Chrome and Edge channels are accepted', () => {
  assert.deepEqual(browserOptions({ AKB_BROWSER_CHANNEL: 'chrome' }), { channel: 'chrome' });
  assert.deepEqual(browserOptions({ AKB_BROWSER_CHANNEL: 'msedge' }), { channel: 'msedge' });
  assert.throws(() => browserOptions({ AKB_BROWSER_CHANNEL: 'arbitrary-profile' }), /unsupported_browser_channel/);
});
test('missing executable fails without installation or fallback', () => {
  assert.throws(() => browserOptions({ AKB_BROWSER_EXECUTABLE: __filename + '.absent' }));
});
