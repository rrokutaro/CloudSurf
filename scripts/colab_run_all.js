/**
 * colab_run_all.js
 *
 * Connects to a running Chrome instance via CDP and triggers
 * "Run all" in Google Colab by injecting JS directly into the page.
 *
 * The findDeep helper walks both Light DOM and Shadow DOM trees,
 * which is necessary for Colab's web-component-heavy toolbar.
 *
 * Env vars injected by CloudSurf:
 *   CLOUDSURF_CDP_PORT     raw port number (e.g. 9222)
 *   CLOUDSURF_PROFILE_ID   profile id string (for logging)
 *
 * Manual run:
 *   CLOUDSURF_CDP_PORT=9222 node scripts/colab_run_all.js
 */

const puppeteer = require('puppeteer-core');

const CDP_PORT   = process.env.CLOUDSURF_CDP_PORT || '9222';
const PROFILE_ID = process.env.CLOUDSURF_PROFILE_ID || '(unknown)';

const log = (...a) => console.log(`[colab_run_all | ${PROFILE_ID}]`, ...a);

// ── In-browser script (attempt 1: immediate) ─────────────────────────────────
// Injected into the Colab tab via page.evaluate().
// Returns "clicked" | "not_found".
const IN_PAGE_SCRIPT = `
(function () {
  function findDeep(root, targetText) {
    if (root.textContent && root.textContent.trim().toLowerCase() === targetText.toLowerCase()) {
      return root;
    }
    if (root.getAttribute && root.getAttribute('aria-label')?.toLowerCase().includes(targetText.toLowerCase())) {
      return root;
    }
    const children = root.children || [];
    for (const child of children) {
      const found = findDeep(child, targetText);
      if (found) return found;
    }
    if (root.shadowRoot) {
      const found = findDeep(root.shadowRoot, targetText);
      if (found) return found;
    }
    return null;
  }

  // Try the toolbar first (faster), then fall back to full document scan
  const toolbar = document.querySelector('#top-toolbar') || document.querySelector('*');
  const btn = findDeep(toolbar, 'Run all');
  if (btn) {
    btn.click();
    return 'clicked';
  }
  return 'not_found';
})();
`;

// ── In-browser script (attempt 2: poll) ──────────────────────────────────────
// If the toolbar isn't ready yet, polls every 500 ms for up to 30 s.
const IN_PAGE_POLL_SCRIPT = `
new Promise((resolve) => {
  function findDeep(root, targetText) {
    if (root.textContent && root.textContent.trim().toLowerCase() === targetText.toLowerCase()) {
      return root;
    }
    if (root.getAttribute && root.getAttribute('aria-label')?.toLowerCase().includes(targetText.toLowerCase())) {
      return root;
    }
    const children = root.children || [];
    for (const child of children) {
      const found = findDeep(child, targetText);
      if (found) return found;
    }
    if (root.shadowRoot) {
      const found = findDeep(root.shadowRoot, targetText);
      if (found) return found;
    }
    return null;
  }

  let attempts = 0;
  const interval = setInterval(() => {
    attempts++;
    const btn = findDeep(document.querySelector('*'), 'Run all');
    if (btn) {
      btn.click();
      clearInterval(interval);
      resolve('clicked_after_' + attempts + '_polls');
    } else if (attempts >= 60) {  // 60 × 500 ms = 30 s timeout
      clearInterval(interval);
      resolve('timeout');
    }
  }, 500);
});
`;

// ─────────────────────────────────────────────────────────────────────────────

(async () => {
  let browser;
  try {
    log(`Connecting to Chrome on port ${CDP_PORT} ...`);
    browser = await puppeteer.connect({
      browserURL: `http://127.0.0.1:${CDP_PORT}`,
      defaultViewport: null,
    });

    const pages = await browser.pages();
    log(`${pages.length} tab(s) open`);

    // Prefer a Colab tab; fall back to first tab
    let page = pages.find(p => p.url().includes('colab.research.google.com'));
    if (!page) {
      log('No Colab tab found -- using first tab');
      page = pages[0];
    }
    if (!page) {
      log('No tabs at all -- cannot proceed');
      process.exit(1);
    }

    log(`Target tab: ${page.url()}`);

    // Navigate to Colab if not already there
    if (!page.url().includes('colab.research.google.com')) {
      log('Navigating to colab.research.google.com ...');
      await page.goto('https://colab.research.google.com/', {
        waitUntil: 'networkidle2',
        timeout: 60000,
      });
    }

    // Attempt 1: immediate click
    log('Attempting immediate "Run all" click ...');
    const immediate = await page.evaluate(IN_PAGE_SCRIPT);
    log(`Immediate result: ${immediate}`);

    if (immediate === 'clicked') {
      log('Done.');
    } else {
      // Attempt 2: poll until toolbar is ready
      log('"Run all" not found yet -- polling (up to 30s) ...');
      const polled = await page.evaluate(IN_PAGE_POLL_SCRIPT);
      log(`Poll result: ${polled}`);
      if (polled.startsWith('clicked')) {
        log('Done.');
      } else {
        log('Could not find "Run all" button after 30s');
        process.exit(1);
      }
    }

  } catch (err) {
    console.error(`[colab_run_all | ${PROFILE_ID}] Fatal: ${err.message}`);
    process.exit(1);
  } finally {
    if (browser) {
      try { await browser.disconnect(); } catch (_) {}
    }
  }
})();
