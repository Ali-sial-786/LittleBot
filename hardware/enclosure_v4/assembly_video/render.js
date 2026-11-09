// node render.js <outdir> <fps> [workers] [t1,t2,...]   (no times = the whole video; each worker is its own browser)
const { chromium } = require('playwright');
const fs = require('fs'), path = require('path');
const DIR = __dirname, OUT = process.argv[2], FPS = +process.argv[3] || 30, WORKERS = +process.argv[4] || 1, ONLY = process.argv[5];
async function open() {
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
  return { browser, page, info };
}
(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const first = await open();
  console.log('total seconds', first.info.total.toFixed(1), 'steps', first.info.steps);
  if (process.env.TIMELINE) console.log(JSON.stringify(await first.page.evaluate(() => window.timeline())));
  const times = ONLY ? ONLY.split(',').map(Number) : Array.from({ length: Math.ceil(first.info.total * FPS) }, (_, k) => k / FPS);
  const t0 = Date.now(); let done = 0;
  const work = async (w, ctx) => {
    for (let k = w; k < times.length; k += WORKERS) {
      await ctx.page.evaluate(t => window.render(t), times[k]);
      await ctx.page.screenshot({ path: path.join(OUT, `f${String(k).padStart(5, '0')}.jpg`), type: 'jpeg', quality: 92 });
      if (++done % 300 === 0) console.log(`frame ${done}/${times.length}  ${((Date.now() - t0) / 1000).toFixed(0)} s`);
    }
    await ctx.browser.close();
  };
  const ctxs = [first]; for (let w = 1; w < WORKERS; w++) ctxs.push(await open());
  await Promise.all(ctxs.map((c, w) => work(w, c)));
  console.log('done', times.length, 'frames in', ((Date.now() - t0) / 1000).toFixed(0), 's');
})();
