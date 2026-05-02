/**
 * colab_run_all.js
 *
 * 1. Connects to a running Chrome instance via CDP
 * 2. Finds and clicks a specific notebook by name (CLOUDSURF_NOTEBOOK)
 * 3. Handles any "leave page" browser dialog
 * 4. Clicks "Run all" using findDeep (Shadow DOM aware)
 *
 * Env vars:
 *   CLOUDSURF_CDP_PORT     CDP port (injected by CloudSurf)
 *   CLOUDSURF_PROFILE_ID   profile id (injected by CloudSurf)
 *   CLOUDSURF_NOTEBOOK     notebook name to open, e.g. "myproject.ipynb"
 *                          (set as a Codespace secret)
 */

const puppeteer = require('puppeteer-core');

const CDP_PORT    = process.env.CLOUDSURF_CDP_PORT || '9222';
const PROFILE_ID  = process.env.CLOUDSURF_PROFILE_ID || '(unknown)';
const NOTEBOOK    = (process.env.CLOUDSURF_NOTEBOOK || '').trim();

const log = (...a) => console.log(`[colab_run_all | ${PROFILE_ID}]`, ...a);

if (!NOTEBOOK) {
  log('WARNING: CLOUDSURF_NOTEBOOK is not set — will skip notebook selection and go straight to Run all');
}

// ── clickDeepByText ───────────────────────────────────────────────────────────
// Walks Light DOM + Shadow DOM, finds the innermost element whose
// textContent or aria-label contains the target word, clicks it.
// Returns true if something was clicked.
const FN_CLICK_DEEP_BY_TEXT = `
function clickDeepByText(word, root = document, clickAll = false) {
  const wordLower = word.toLowerCase();
  const foundElements = [];

  function search(node) {
    if (node.shadowRoot) search(node.shadowRoot);
    if (node.children) {
      for (const child of node.children) search(child);
    }
    const hasText = node.textContent?.toLowerCase().includes(wordLower);
    const hasAria = node.getAttribute?.('aria-label')?.toLowerCase().includes(wordLower);
    if (hasText || hasAria) {
      const childMatch = Array.from(node.children || []).some(child =>
        child.textContent?.toLowerCase().includes(wordLower)
      );
      if (!childMatch) foundElements.push(node);
    }
  }

  search(root);

  if (foundElements.length > 0) {
    if (clickAll) {
      foundElements.forEach(el => el.click());
    } else {
      foundElements[0].click();
    }
    return true;
  }
  return false;
}
`;

// ── findDeep ──────────────────────────────────────────────────────────────────
// Exact-match version used for "Run all" button.
const FN_FIND_DEEP = `
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
`;

// ── Step 1: click notebook in picker ─────────────────────────────────────────
// Returns "clicked" | "not_found" | "skipped"
const scriptClickNotebook = (notebook) => `
(function() {
  ${FN_CLICK_DEEP_BY_TEXT}
  if (!${JSON.stringify(notebook)}) return 'skipped';
  const clicked = clickDeepByText(${JSON.stringify(notebook)});
  return clicked ? 'clicked' : 'not_found';
})();
`;

// Poll version — picker may not have rendered yet
const scriptPollNotebook = (notebook) => `
new Promise((resolve) => {
  ${FN_CLICK_DEEP_BY_TEXT}
  let attempts = 0;
  const iv = setInterval(() => {
    attempts++;
    const clicked = clickDeepByText(${JSON.stringify(notebook)});
    if (clicked) {
      clearInterval(iv);
      resolve('clicked_after_' + attempts + '_polls');
    } else if (attempts >= 60) {  // 30s
      clearInterval(iv);
      resolve('timeout');
    }
  }, 500);
});
`;

// ── Step 2: Run all ───────────────────────────────────────────────────────────
const scriptRunAll = `
(function() {
  ${FN_FIND_DEEP}
  const toolbar = document.querySelector('#top-toolbar') || document.querySelector('*');
  const btn = findDeep(toolbar, 'Run all');
  if (btn) { btn.click(); return 'clicked'; }
  return 'not_found';
})();
`;

const scriptPollRunAll = `
new Promise((resolve) => {
  ${FN_FIND_DEEP}
  let attempts = 0;
  const iv = setInterval(() => {
    attempts++;
    const btn = findDeep(document.querySelector('*'), 'Run all');
    if (btn) {
      btn.click();
      clearInterval(iv);
      resolve('clicked_after_' + attempts + '_polls');
    } else if (attempts >= 120) {  // 60s — notebook load can be slow
      clearInterval(iv);
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

    let page = pages.find(p => p.url().includes('colab.research.google.com'));
    if (!page) {
      log('No Colab tab — using first tab');
      page = pages[0];
    }
    if (!page) {
      log('No tabs open — cannot proceed');
      process.exit(1);
    }

    // Navigate to Colab if needed
    if (!page.url().includes('colab.research.google.com')) {
      log('Navigating to Colab ...');
      await page.goto('https://colab.research.google.com/', {
        waitUntil: 'networkidle2',
        timeout: 60000,
      });

      await page.reload({ waitUntil: "networkidle2" });
    }

    log(`Tab: ${page.url()}`);

    // ── Handle "leave page" dialog BEFORE any clicks ──────────────────────
    // Set up the handler early so it fires if the dialog appears at any point
    page.on('dialog', async (dialog) => {
      log(`Browser dialog: "${dialog.message()}" — accepting`);
      try { await dialog.accept(); } catch (_) {}
    });

    // ── Step 1: Open the notebook ─────────────────────────────────────────
    if (NOTEBOOK) {
      log(`Looking for notebook: "${NOTEBOOK}" ...`);

      let result = await page.evaluate(scriptClickNotebook(NOTEBOOK));
      log(`Immediate notebook click: ${result}`);

      if (result === 'not_found') {
        log('Not found immediately — polling for picker to appear ...');
        result = await page.evaluate(scriptPollNotebook(NOTEBOOK));
        log(`Poll result: ${result}`);
      }

      if (result === 'timeout') {
        log(`Could not find notebook "${NOTEBOOK}" after 30s`);
        process.exit(1);
      }

      // Wait for navigation after clicking the notebook.
      // The page may reload or navigate — wait for it to settle.
      log('Waiting for page to load after notebook click ...');
      try {
        await page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 30000 });
      } catch (_) {
        // waitForNavigation times out if the page was already on the notebook URL
        // (e.g. opened in same tab without a full nav). That's fine — continue.
        log('No navigation detected — assuming already on notebook page');
      }

      log(`Page after notebook open: ${page.url()}`);
    } else {
      log('No CLOUDSURF_NOTEBOOK set — skipping notebook selection');
    }

    // ── Step 2: Run all ───────────────────────────────────────────────────
    log('Attempting "Run all" click ...');
    let runResult = await page.evaluate(scriptRunAll);
    log(`Immediate Run all: ${runResult}`);

    if (runResult === 'not_found') {
      log('Polling for Run all button (up to 60s) ...');
      runResult = await page.evaluate(scriptPollRunAll);
      log(`Poll result: ${runResult}`);
    }

    if (runResult === 'timeout') {
      log('Could not find "Run all" button after 60s');
      process.exit(1);
    }

    log('Done.');

  } catch (err) {
    console.error(`[colab_run_all | ${PROFILE_ID}] Fatal: ${err.message}`);
    process.exit(1);
  } finally {
    if (browser) {
      try { await browser.disconnect(); } catch (_) {}
    }
  }
})();
