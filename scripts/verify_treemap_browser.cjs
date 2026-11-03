#!/usr/bin/env node
/*
 * Optional real-browser qualification for OCD's offline HTML treemap.
 *
 * No browser download, package installation, network fetch, host scan or
 * cleanup operation is performed. Exit 2 means UNQUALIFIED, not PASS.
 *
 *   node scripts/verify_treemap_browser.cjs
 *   OCD_PYTHON=python node scripts/verify_treemap_browser.cjs
 */
'use strict';

const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const repository = path.resolve(__dirname, '..');

function unqualified(reason) {
  process.stderr.write(JSON.stringify({ status: 'UNQUALIFIED', reason }) + '\n');
  process.exitCode = 2;
}

function runOcd(args) {
  const python = process.env.OCD_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  const source = path.join(repository, 'src');
  const env = {
    ...process.env,
    PYTHONPATH: [source, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
  };
  return spawnSync(python, ['-m', 'ocd', 'usage', ...args], {
    cwd: repository,
    env,
    encoding: 'utf8',
    timeout: 60000,
    maxBuffer: 1024 * 1024,
  });
}

function createFixture(workspace) {
  const source = path.join(workspace, 'source');
  const output = path.join(workspace, 'output');
  fs.mkdirSync(path.join(source, 'nested', 'inner'), { recursive: true });
  fs.mkdirSync(path.join(source, 'empty'));
  fs.mkdirSync(output);
  fs.writeFileSync(path.join(source, 'nested', 'inner', 'actual.bin'), Buffer.alloc(191, 0x61));
  fs.writeFileSync(path.join(source, 'root.bin'), Buffer.alloc(23, 0x62));
  const hostileName = process.platform === 'win32'
    ? 'ampersand & quoted-name'
    : '<img src=x onerror=alert(1)> & name';
  fs.mkdirSync(path.join(source, hostileName));
  fs.writeFileSync(path.join(source, hostileName, 'tiny.bin'), Buffer.from('q'));
  const report = path.join(output, 'usage.html');
  const generated = runOcd(['--path', source, '--html', report, '--max-entries', '10000']);
  assert.equal(generated.status, 0, `HTML creation failed: ${generated.stderr || generated.error}`);
  assert.equal(fs.existsSync(report), true);

  const original = fs.readFileSync(report);
  const refused = runOcd(['--path', source, '--html', report, '--max-entries', '10000']);
  assert.notEqual(refused.status, 0, 'HTML generation must refuse an existing report');
  assert.deepEqual(fs.readFileSync(report), original, 'failed no-overwrite attempt changed the report');
  return {
    report,
    hostileName,
    htmlDigest: createHash('sha256').update(original).digest('hex'),
  };
}

async function verifyPage(page, hostileName, failures) {
  await page.goto(pathToFileURL(page.__ocdReport).href, { waitUntil: 'load' });
  const stage = page.locator('#ocd-directory-drilldown');
  await stage.waitFor({ state: 'visible', timeout: 10000 });
  assert.equal(await page.locator('#ocd-static-treemap').isVisible(), false);
  assert.equal(await page.locator('#ocd-drill-list button').filter({ hasText: hostileName }).count(), 1);
  assert.equal(await page.locator('#ocd-drill-list button').filter({ hasText: 'empty' }).count(), 1);

  const nested = page.locator('#ocd-drill-list button').filter({ hasText: 'nested' });
  await nested.click();
  assert.match(await page.locator('#ocd-drill-status').innerText(), /Directory nested:/);
  await page.keyboard.press('Escape');
  assert.equal(await nested.evaluate(el => document.activeElement === el), true,
    'Escape must restore focus to the originating HTML list button');

  const tile = page.locator('#ocd-drill-svg [role="button"]').first();
  await tile.focus();
  await page.keyboard.press('Enter');
  assert.match(await page.locator('#ocd-drill-status').innerText(), /Directory nested:/);
  await page.keyboard.press('Backspace');
  assert.equal(await tile.evaluate(el => document.activeElement === el), true,
    'Backspace must restore focus to the originating SVG tile');

  await nested.click();
  await page.locator('#ocd-drill-home').click();
  assert.equal(await page.locator('#ocd-drill-up').isDisabled(), true,
    'Home must restore root navigation state');
  await page.locator('#ocd-drill-list button').filter({ hasText: hostileName }).click();
  assert.match(await page.locator('#ocd-drill-status').innerText(), /observed logical bytes/);
  assert.equal(await page.locator('img').count(), 0,
    'hostile directory name must remain text, never an injected image element');
  assert.deepEqual(failures, [], 'browser reported JavaScript/CSP failures');
}

async function main() {
  if (process.argv.includes('--help') || process.argv.includes('-h')) {
    process.stdout.write(
      'Runs an opt-in OCD offline treemap browser qualification. Requires an already-installed Playwright Chromium browser.\n' +
      'Exit 0 = browser probe passed; exit 1 = tested behavior failed; exit 2 = browser unavailable/unqualified.\n'
    );
    return;
  }

  // Run fixture and source-policy checks even when no browser is installed.
  // This deliberately does not emit PASS_BROWSER_PROBE.
  if (process.argv.includes('--static-check')) {
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'ocd-browser-static-check-'));
    try {
      const { report, htmlDigest } = createFixture(temp);
      const html = fs.readFileSync(report, 'utf8');
      assert.match(html, /id="ocd-static-treemap"/);
      assert.match(html, /id="ocd-directory-drilldown"/);
      assert.match(html, /script-src 'sha256-/);
      assert.match(html, /connect-src 'none'/);
      assert.doesNotMatch(html, /script-src 'unsafe-inline'/);
      process.stdout.write(JSON.stringify({
        status: 'PASS_STATIC_FIXTURE_ONLY',
        html_sha256: htmlDigest,
        browser_qualified: false,
      }) + '\n');
    } finally {
      fs.rmSync(temp, { recursive: true, force: true });
    }
    return;
  }

  let chromium;
  try {
    ({ chromium } = require('playwright'));
  } catch (error) {
    return unqualified(`Playwright package unavailable: ${error.message}`);
  }

  const executable = chromium.executablePath();
  if (!fs.existsSync(executable)) {
    return unqualified(`Chromium executable unavailable: ${executable}`);
  }

  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (error) {
    return unqualified(`Installed Chromium failed to launch: ${error.message}`);
  }

  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'ocd-browser-qualification-'));
  try {
    const { report, hostileName, htmlDigest } = createFixture(temp);
    const context = await browser.newContext({ javaScriptEnabled: true });
    const attemptedExternalRequests = [];
    await context.route(/^https?:\/\//, route => {
      attemptedExternalRequests.push(route.request().url());
      return route.abort();
    });
    const page = await context.newPage();
    page.__ocdReport = report;
    const failures = [];
    page.on('pageerror', error => failures.push(`pageerror: ${error.message}`));
    page.on('console', message => {
      if (message.type() === 'error') failures.push(`console: ${message.text()}`);
    });
    await page.addInitScript(() => {
      window.__ocdCspViolations = [];
      document.addEventListener('securitypolicyviolation', event => {
        window.__ocdCspViolations.push(event.violatedDirective);
      });
    });
    await verifyPage(page, hostileName, failures);
    const violations = await page.evaluate(() => window.__ocdCspViolations || []);
    assert.deepEqual(violations, [], 'CSP violations occurred');
    assert.deepEqual(attemptedExternalRequests, [], 'the report attempted outbound HTTP(S) requests');
    await context.close();

    const noJs = await browser.newContext({ javaScriptEnabled: false });
    const fallbackPage = await noJs.newPage();
    await fallbackPage.goto(pathToFileURL(report).href, { waitUntil: 'load' });
    assert.equal(await fallbackPage.locator('#ocd-static-treemap').isVisible(), true,
      'static SVG fallback must work with scripts disabled');
    assert.equal(await fallbackPage.locator('#ocd-directory-drilldown').isVisible(), false,
      'interactive section must remain hidden without scripts');
    await noJs.close();
    process.stdout.write(JSON.stringify({
      status: 'PASS_BROWSER_PROBE',
      chromium: browser.version(),
      html_sha256: htmlDigest,
      external_requests: attemptedExternalRequests.length,
      csp_violations: violations.length,
      source: 'temporary local fixture only',
    }) + '\n');
  } finally {
    await browser.close();
    // Only this unique temporary directory is removed.
    fs.rmSync(temp, { recursive: true, force: true });
  }
}

main().catch(error => {
  process.stderr.write(JSON.stringify({
    status: 'FAIL_BROWSER_PROBE',
    error: error.stack || error.message,
  }) + '\n');
  process.exitCode = 1;
});
