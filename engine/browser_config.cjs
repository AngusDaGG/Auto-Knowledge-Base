"use strict";
const fs = require('node:fs');
const path = require('node:path');

function browserOptions(env = process.env) {
  if (env.AKB_BROWSER_EXECUTABLE) {
    const executablePath = path.resolve(env.AKB_BROWSER_EXECUTABLE);
    if (!fs.statSync(executablePath).isFile()) throw new Error('browser_missing');
    return { executablePath };
  }
  if (env.AKB_BROWSER_CHANNEL) {
    if (!['msedge', 'chrome'].includes(env.AKB_BROWSER_CHANNEL)) throw new Error('unsupported_browser_channel');
    return { channel: env.AKB_BROWSER_CHANNEL };
  }
  // Only normal machine installations; never inspect a personal browser profile.
  for (const channel of ['msedge', 'chrome']) {
    const relative = channel === 'msedge' ? 'Microsoft/Edge/Application/msedge.exe' : 'Google/Chrome/Application/chrome.exe';
    if ([env.ProgramFiles, env['ProgramFiles(x86)']].filter(Boolean).some(base => fs.existsSync(path.join(base, relative)))) {
      return { channel };
    }
  }
  return { channel: 'msedge' }; // Missing browsers fail explicitly; no download.
}
module.exports = { browserOptions };
