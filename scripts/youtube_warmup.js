/**
 * youtube_warmup.js
 *
 * Robust YouTube account warmup script using Puppeteer (CDP connection).
 * Mimics real user behavior: homepage scroll, Shorts viewing, search & watch,
 * video watching with seek/pause/play, random mouse movements, natural delays.
 *
 * Env vars (optional):
 *   CLOUDSURF_CDP_PORT     CDP port (default 9222)
 *   CLOUDSURF_PROFILE_ID   For logging
 */

const puppeteer = require('puppeteer-core');

const CDP_PORT = process.env.CLOUDSURF_CDP_PORT || '9222';
const PROFILE_ID = process.env.CLOUDSURF_PROFILE_ID || 'youtube-warmup';

const log = (...a) => console.log(`[yt-warmup | ${PROFILE_ID}]`, ...a);

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const randomInt = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;

const humanDelay = async (base = 800, variance = 600) => {
  await sleep(base + randomInt(0, variance));
};

// ── Deep DOM Helpers (YouTube uses heavy Shadow DOM) ─────────────────────
const FN_CLICK_DEEP_BY_TEXT = `
function clickDeepByText(word, root = document, clickAll = false) {
  const wordLower = word.toLowerCase().trim();
  const found = [];

  function search(node) {
    if (!node) return;
    if (node.shadowRoot) search(node.shadowRoot);

    const text = (node.textContent || '').toLowerCase();
    const aria = node.getAttribute?.('aria-label')?.toLowerCase() || '';

    if ((text.includes(wordLower) || aria.includes(wordLower))) {
      // Prefer leaf nodes
      const hasDirectChildMatch = Array.from(node.children || []).some(c =>
        (c.textContent || '').toLowerCase().includes(wordLower)
      );
      if (!hasDirectChildMatch) found.push(node);
    }

    if (node.children) {
      for (const child of node.children) search(child);
    }
  }

  search(root);

  if (found.length > 0) {
    if (clickAll) {
      found.forEach(el => { try { el.click(); } catch(e){} });
    } else {
      try { found[0].click(); } catch(e){}
    }
    return true;
  }
  return false;
}
`;

// FIX: FN_FIND_DEEP is retained for potential reuse but explicitly called where needed.
const FN_FIND_DEEP = `
function findDeep(root, selectorOrText) {
  if (!root) return null;
  if (root.shadowRoot) {
    const inShadow = findDeep(root.shadowRoot, selectorOrText);
    if (inShadow) return inShadow;
  }
  if (typeof selectorOrText === 'string') {
    if (root.matches && root.matches(selectorOrText)) return root;
    if (root.querySelector) {
      const found = root.querySelector(selectorOrText);
      if (found) return found;
    }
  }
  const children = root.children || [];
  for (const child of children) {
    const found = findDeep(child, selectorOrText);
    if (found) return found;
  }
  return null;
}
`;

// ── Mouse movement simulation ─────────────────────────────────────────────
// FIX: Moved humanMouseMove into Node.js scope so it can actually be called.
const humanMouseMove = async (page, targetX, targetY, steps = 25) => {
  const startX = randomInt(100, 1100);
  const startY = randomInt(100, 600);
  for (let i = 0; i <= steps; i++) {
    const x = startX + (targetX - startX) * (i / steps) + (Math.random() * 12 - 6);
    const y = startY + (targetY - startY) * (i / steps) + (Math.random() * 12 - 6);
    await page.mouse.move(Math.floor(x), Math.floor(y));
    await sleep(8 + Math.random() * 12);
  }
};

// ── Safe back-navigation helper ───────────────────────────────────────────
// FIX: Awaits the fallback goto and surfaces errors cleanly instead of swallowing them.
const safeGoBack = async (page) => {
  try {
    await page.goBack({ waitUntil: 'networkidle2', timeout: 15000 });
  } catch (_) {
    try {
      await page.goto('https://www.youtube.com', { waitUntil: 'networkidle2', timeout: 30000 });
    } catch (e) {
      log(`safeGoBack: fallback goto also failed — ${e.message}`);
    }
  }
};

// ── Login state check ─────────────────────────────────────────────────────
// FIX: New helper — exits early with a clear message if the account is not logged in.
const assertLoggedIn = async (page) => {
  const loggedIn = await page.evaluate(() => {
    // Avatar button is only present for signed-in users
    return !!(
      document.querySelector('button#avatar-btn') ||
      document.querySelector('yt-img-shadow#avatar')
    );
  });
  if (!loggedIn) {
    throw new Error(
      'YouTube account does not appear to be logged in. ' +
      'Sign in first, then re-run the warmup.'
    );
  }
  log('Login state verified ✓');
};

// ── Shorts watcher ────────────────────────────────────────────────────────
const watchShorts = async (page) => {
  // FIX: Navigate directly instead of relying on sidebar text match (too fragile).
  log('Navigating to Shorts via direct URL...');
  await page.goto('https://www.youtube.com/shorts', {
    waitUntil: 'networkidle2',
    timeout: 30000,
  });
  await humanDelay(2500, 1500);

  log('Watching Shorts...');
  const SHORTS_COUNT = randomInt(6, 8); // FIX: Explicit range, matches comment intent.
  for (let s = 0; s < SHORTS_COUNT; s++) {
    log(`Short ${s + 1}/${SHORTS_COUNT}`);

    if (Math.random() > 0.4) {
      await page.keyboard.press('k').catch(() => {}); // toggle play/pause
      await humanDelay(300, 200);
      await page.keyboard.press('k').catch(() => {}); // resume
    }

    if (Math.random() > 0.65) {
      await page.keyboard.press('ArrowRight').catch(() => {});
      await humanDelay(300);
    }

    await humanDelay(2800, 3200);

    // Advance to next Short
    await page.keyboard.press('ArrowDown').catch(() => {});
    await page.evaluate(() => window.scrollBy(0, randomInt ? randomInt(280, 380) : 320));
    await humanDelay(800, 1200);
  }
};

// ── Regular video watcher ─────────────────────────────────────────────────
const watchVideos = async (page) => {
  const VIDEO_COUNT = 3;

  for (let v = 0; v < VIDEO_COUNT; v++) {
    log(`Watching video ${v + 1}/${VIDEO_COUNT}...`);
    try {
      // FIX: scrollIntoView + click are split — click happens in Node after a proper await.
      const clicked = await page.evaluate(() => {
        const videos = document.querySelectorAll(
          'ytd-rich-item-renderer a#thumbnail, ytd-video-renderer a#thumbnail'
        );
        if (videos.length <= 2) return false;
        const randomVid = videos[Math.floor(Math.random() * (videos.length - 2)) + 2];
        randomVid.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return true;
      });

      if (!clicked) {
        log(`Video ${v + 1}: no thumbnails found, skipping`);
        continue;
      }

      // Let scroll animation settle, then click
      await humanDelay(900, 400);
      await page.evaluate(() => {
        const videos = document.querySelectorAll(
          'ytd-rich-item-renderer a#thumbnail, ytd-video-renderer a#thumbnail'
        );
        if (videos.length > 2) {
          videos[Math.floor(Math.random() * (videos.length - 2)) + 2].click();
        }
      });

      await humanDelay(3000, 2000);

      // FIX: Increased timeout from 8s → 20s for reliability on slower connections.
      await page.waitForSelector('video', { timeout: 20000 }).catch(() => {
        log(`Video ${v + 1}: player did not appear within timeout`);
      });

      // FIX: Use Date.now() deadline instead of accumulating a step variable.
      const watchTime = randomInt(25000, 65000);
      log(`  Watching for ~${Math.round(watchTime / 1000)}s`);
      const deadline = Date.now() + watchTime;

      while (Date.now() < deadline) {
        const step = randomInt(4000, 9000);
        await humanDelay(step, 0);

        if (Math.random() > 0.7) await page.keyboard.press('k').catch(() => {});
        if (Math.random() > 0.8) await page.keyboard.press('ArrowRight').catch(() => {});

        // FIX: Use the real humanMouseMove helper now that it's in Node scope.
        await humanMouseMove(page, randomInt(300, 900), randomInt(200, 500));
      }

      await safeGoBack(page);
      await humanDelay(1500);
    } catch (e) {
      log(`Video ${v + 1} error: ${e.message}`);
      await safeGoBack(page);
    }
  }
};

// ── Search query pool ─────────────────────────────────────────────────────
// Broad, evergreen topics — nothing niche that looks bot-like on a fresh account.
const SEARCH_QUERIES = [
  'how to make pasta carbonara',
  'best hiking trails for beginners',
  'lo fi hip hop study music',
  'how does a car engine work',
  'beginner guitar lessons',
  'home workout no equipment',
  'documentary nature 2024',
  'funny cat compilation',
  'how to learn a new language fast',
  'space exploration news',
  'easy meal prep ideas',
  'mindfulness meditation for sleep',
  'history of ancient rome',
  'how to fix a leaky tap',
  'street food around the world',
  'chess for beginners tutorial',
  'diy home decoration ideas',
  'best road trips in europe',
  'how to start investing',
  'wildlife photography tips',
];

const pickSearchQueries = (count = 2) => {
  const shuffled = [...SEARCH_QUERIES].sort(() => Math.random() - 0.5);
  return shuffled.slice(0, count);
};

// ── Human-like typing ─────────────────────────────────────────────────────
// Types character-by-character with realistic inter-key delays and occasional typo+backspace.
const humanType = async (page, text) => {
  for (let i = 0; i < text.length; i++) {
    // Occasional typo: insert a wrong char then backspace (~4% chance)
    if (Math.random() < 0.04 && i < text.length - 1) {
      const wrongChars = 'abcdefghijklmnopqrstuvwxyz';
      const typo = wrongChars[randomInt(0, wrongChars.length - 1)];
      await page.keyboard.type(typo);
      await sleep(randomInt(80, 180));
      await page.keyboard.press('Backspace');
      await sleep(randomInt(60, 140));
    }
    await page.keyboard.type(text[i]);
    await sleep(randomInt(60, 180)); // 60–180ms per keystroke
  }
};

// ── Search and watch ──────────────────────────────────────────────────────
// Performs a search, scrolls results, clicks a video, watches a percentage of it.
const searchAndWatch = async (page, query) => {
  log(`Searching for: "${query}"`);

  // Navigate to YouTube home first if not already there
  if (!page.url().includes('youtube.com')) {
    await page.goto('https://www.youtube.com', { waitUntil: 'networkidle2', timeout: 30000 });
    await humanDelay(1200);
  }

  // Click the search box
  try {
    await page.waitForSelector('input#search', { timeout: 8000 });
  } catch (_) {
    log('Search input not found, skipping this query');
    return;
  }

  await humanMouseMove(page, randomInt(400, 700), randomInt(50, 90));
  await page.click('input#search');
  await humanDelay(400, 300);

  // Clear any existing text
  await page.evaluate(() => {
    const input = document.querySelector('input#search');
    if (input) input.value = '';
  });

  // Type query naturally
  await humanType(page, query);
  await humanDelay(500, 400);

  // Submit
  await page.keyboard.press('Enter');
  await humanDelay(2500, 1500);

  // Scroll results page to look like the user is scanning
  log('Scanning search results...');
  for (let i = 0; i < randomInt(2, 4); i++) {
    await page.evaluate((amount) => window.scrollBy(0, amount), randomInt(250, 450));
    await humanDelay(700, 800);
  }

  // Pick a result — skip index 0 (often an ad or promoted), pick from 1–5
  const clicked = await page.evaluate(() => {
    const results = document.querySelectorAll('ytd-video-renderer a#thumbnail');
    if (results.length < 2) return false;
    const pick = results[randomInt ? randomInt(1, Math.min(4, results.length - 1)) : 1];
    pick.scrollIntoView({ behavior: 'smooth', block: 'center' });
    return true;
  });

  if (!clicked) {
    log('No search results found to click, skipping');
    return;
  }

  await humanDelay(900, 500);

  // Click the chosen result
  await page.evaluate(() => {
    const results = document.querySelectorAll('ytd-video-renderer a#thumbnail');
    if (results.length >= 2) {
      const pick = results[Math.floor(Math.random() * Math.min(4, results.length - 1)) + 1];
      pick.click();
    }
  });

  await humanDelay(3000, 2000);
  await page.waitForSelector('video', { timeout: 20000 }).catch(() => {
    log('Video player did not appear for search result');
  });

  // Watch 30–60% of the video's actual duration for natural drop-off behaviour
  const watchTime = await page.evaluate(() => {
    const video = document.querySelector('video');
    if (!video || !video.duration || isNaN(video.duration)) return null;
    const pct = 0.30 + Math.random() * 0.30; // 30–60%
    return Math.floor(video.duration * pct * 1000); // ms
  });

  // Fallback to a fixed range if duration isn't readable yet
  const actualWatchTime = watchTime || randomInt(30000, 70000);
  log(`  Watching search result for ~${Math.round(actualWatchTime / 1000)}s`);

  const deadline = Date.now() + actualWatchTime;
  while (Date.now() < deadline) {
    const step = randomInt(4000, 9000);
    await humanDelay(step, 0);
    if (Math.random() > 0.7) await page.keyboard.press('k').catch(() => {});
    if (Math.random() > 0.8) await page.keyboard.press('ArrowRight').catch(() => {});
    await humanMouseMove(page, randomInt(300, 900), randomInt(200, 500));
  }

  await safeGoBack(page);
  await humanDelay(1200, 800);
};

// ── Main warmup orchestrator ──────────────────────────────────────────────
const warmupScript = async (page) => {
  log('Starting YouTube warmup sequence...');

  page.on('dialog', async (dialog) => {
    log(`Dialog: ${dialog.message()} — accepting`);
    await dialog.accept().catch(() => {});
  });

  // Navigate to homepage
  log('Navigating to YouTube...');
  await page.goto('https://www.youtube.com', {
    waitUntil: 'networkidle2',
    timeout: 45000,
  });

  await humanDelay(1500, 1000);

  // Accept cookies if present
  log('Handling consent...');
  try {
    const consentClicked = await page.evaluate(`
      (${FN_CLICK_DEEP_BY_TEXT})
      clickDeepByText('accept all') || clickDeepByText('i agree') || clickDeepByText('agree');
    `);
    if (consentClicked) {
      log('Consent accepted');
      await humanDelay(1200);
    }
  } catch (_) {
    log('No consent dialog or already handled');
  }

  // FIX: Assert login before doing anything meaningful.
  await assertLoggedIn(page);

  // Realistic mouse movements across the page
  log('Simulating mouse activity...');
  for (let i = 0; i < 6; i++) {
    const tx = randomInt(100, 1200);
    const ty = randomInt(100, 700);
    // FIX: Removed unused (tx, ty) args that were passed into evaluate but ignored.
    await page.evaluate(() => window.scrollBy(0, randomInt ? 0 : 0)); // no-op, just context check
    await humanMouseMove(page, tx, ty);
    await humanDelay(400, 600);
  }

  // Scroll homepage feed naturally
  log('Scrolling homepage feed...');
  for (let i = 0; i < 8; i++) {
    // FIX: Use randomInt consistently instead of raw Math.random expressions.
    await page.evaluate((amount) => window.scrollBy(0, amount), randomInt(300, 700));
    await humanDelay(900, 1100);

    if (i % 3 === 0) {
      await page.evaluate(() => window.scrollBy(0, -180));
      await humanDelay(600);
    }
  }

  // Watch Shorts
  try {
    await watchShorts(page);
  } catch (e) {
    log(`Shorts section failed, continuing — ${e.message}`);
    // Return to homepage before video section
    await page.goto('https://www.youtube.com', { waitUntil: 'networkidle2', timeout: 30000 })
      .catch(() => {});
  }

  // ── Idle gap (simulates user getting distracted between sections) ─────────
  const idleTime = randomInt(30000, 60000);
  log(`Idle pause for ~${Math.round(idleTime / 1000)}s...`);
  await sleep(idleTime);

  // ── Search section ────────────────────────────────────────────────────────
  log('Starting search section...');
  const queries = pickSearchQueries(randomInt(2, 3));
  for (const query of queries) {
    try {
      await searchAndWatch(page, query);
      // Brief idle between searches
      await sleep(randomInt(8000, 18000));
    } catch (e) {
      log(`Search error for "${query}": ${e.message}`);
      await page.goto('https://www.youtube.com', { waitUntil: 'networkidle2', timeout: 30000 })
        .catch(() => {});
    }
  }

  // ── Watch regular videos from feed ────────────────────────────────────────
  log('Watching regular videos...');
  await watchVideos(page);

  // Final scroll
  log('Final feed interactions...');
  await page.evaluate(() => window.scrollTo(0, 400));
  await humanDelay(1200);

  log('YouTube warmup completed successfully.');
};

// ── Entry point ───────────────────────────────────────────────────────────
(async () => {
  let browser;
  try {
    log(`Connecting to Chrome on port ${CDP_PORT}...`);
    browser = await puppeteer.connect({
      browserURL: `http://127.0.0.1:${CDP_PORT}`,
      defaultViewport: { width: 1280, height: 800 },
    });

    const pages = await browser.pages();
    let page = pages.find(p => p.url().includes('youtube.com')) || pages[0];

    if (!page) {
      page = await browser.newPage();
    }

    await page.setViewport({ width: 1280, height: 800 });
    await page.setUserAgent(
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
      '(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36'
    );

    await warmupScript(page);
  } catch (err) {
    console.error(`[yt-warmup | ${PROFILE_ID}] Fatal:`, err.message);
    process.exit(1);
  } finally {
    if (browser) {
      try { await browser.disconnect(); } catch (_) {}
    }
  }
})();
