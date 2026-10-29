// node render.js <outdir> <fps> [t1,t2,...]   (no times = the whole video)
const { chromium } = require('playwright');
const fs = require('fs'), path = require('path');
const DIR = __dirname, OUT = process.argv[2], FPS = +process.argv[3] || 30, ONLY = process.argv[4];
(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
  page.on('console', m => console.log('page:', m.text())); page.on('pageerror', e => console.log('page error:', e.message));
  await page.route('http://anim.local/**', r => {
    const f = path.join(DIR, decodeURIComponent(new URL(r.request().url()).pathname));
    const type = f.endsWith('.html') ? 'text/html' : f.endsWith('.json') ? 'application/json' : 'application/octet-stream';
    r.fulfill({ status: 200, contentType: type, body: fs.readFileSync(f) });
  });
  await page.goto('http://anim.local/index.html');
  const info = await page.evaluate(() => window.ready);
  console.log('total seconds', info.total.toFixed(1), 'steps', info.steps);
  const times = ONLY ? ONLY.split(',').map(Number) : Array.from({ length: Math.ceil(info.total * FPS) }, (_, k) => k / FPS);
  const t0 = Date.now();
  for (let k = 0; k < times.length; k++) {
    await page.evaluate(t => window.render(t), times[k]);
    await page.screenshot({ path: path.join(OUT, `f${String(k).padStart(5, '0')}.jpg`), type: 'jpeg', quality: 92 });
    if (k % 300 === 0) console.log(`frame ${k}/${times.length}  ${((Date.now() - t0) / 1000).toFixed(0)} s`);
  }
  console.log('done', times.length, 'frames in', ((Date.now() - t0) / 1000).toFixed(0), 's');
  await browser.close();
})();
