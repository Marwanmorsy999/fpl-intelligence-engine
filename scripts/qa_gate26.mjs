import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const BASE = process.env.QA_BASE || 'https://fpl-intelligence-engine-foundation.vercel.app';
const OUT = 'qa_gate26';
if (!fs.existsSync(OUT)) fs.mkdirSync(OUT, { recursive: true });

const viewports = [
  { w: 390, h: 844, name: '390' },
  { w: 768, h: 1024, name: '768' },
  { w: 1440, h: 900, name: '1440' },
];

const pages = [
  { path: '/', label: 'Decisions' },
  { path: '/my_team', label: 'MyTeam' },
  { path: '/live', label: 'Live' },
  { path: '/league', label: 'League' },
  { path: '/track_record', label: 'TrackRecord' },
  { path: '/targets', label: 'Targets' },
];

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ ignoreHTTPSErrors: true });

let consoleErrors = [];
let failed = [];

for (const pg of pages) {
  for (const vp of viewports) {
    const page = await ctx.newPage();
    page.on('console', m => {
      if (m.type() === 'error') consoleErrors.push(`${pg.label} ${vp.name}: ${m.text()}`);
    });
    page.on('pageerror', e => consoleErrors.push(`${pg.label} ${vp.name} pageerror: ${e.message}`));
    await page.setViewportSize({ width: vp.w, height: vp.h });
    const url = BASE + pg.path;
    try {
      await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
      await page.waitForTimeout(1500);
      const file = path.join(OUT, `${pg.label}_${vp.name}.png`);
      await page.screenshot({ path: file, fullPage: true });
      console.log(`ok ${pg.label} ${vp.name} -> ${file}`);
      // basic check: no horizontal page scroll
      const hasHScroll = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 2);
      if (hasHScroll) {
        failed.push(`${pg.label} ${vp.name} has horizontal scroll (scrollWidth > clientWidth)`);
      }
      // check 48px targets on phone
      if (vp.w === 390) {
        const smallTargets = await page.evaluate(() => {
          const els = [...document.querySelectorAll('button, a, [role="button"]')].filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && (r.width < 44 || r.height < 44) && getComputedStyle(el).display !== 'none';
          });
          return els.slice(0, 3).map(e => `${e.tagName}.${e.className} ${Math.round(e.getBoundingClientRect().width)}x${Math.round(e.getBoundingClientRect().height)}`);
        });
        if (smallTargets.length) console.log(`warn small targets ${pg.label} ${vp.name}: ${smallTargets.join('; ')}`);
      }
    } catch (e) {
      console.log(`FAIL ${pg.label} ${vp.name}: ${e.message}`);
      failed.push(`${pg.label} ${vp.name}: ${e.message}`);
    }
    await page.close();
  }
}
await browser.close();

console.log('\n=== Console errors (must be 0) ===');
if (consoleErrors.length === 0) console.log('console 0: PASS');
else { consoleErrors.forEach(e => console.log(e)); console.log(`console errors: ${consoleErrors.length} FAIL`); failed.push(`console errors ${consoleErrors.length}`); }

console.log('\n=== Horizontal scroll ===');
if (failed.filter(f => f.includes('horizontal')).length === 0) console.log('no h-scroll: PASS'); else console.log(failed.filter(f=>f.includes('horizontal')).join('\n'));

const s = JSON.stringify({ consoleErrors, failed, viewports, pages, base: BASE, at: new Date().toISOString() }, null, 2);
fs.writeFileSync(path.join(OUT, 'qa.json'), s);
console.log(`\nQA done. Screenshots in ${OUT}/`);
if (failed.length) { console.log(`\nFAILED checks:\n${failed.join('\n')}`); process.exitCode = 1; }
