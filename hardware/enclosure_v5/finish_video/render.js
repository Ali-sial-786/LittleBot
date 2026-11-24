// node render.js <outdir> <fps> [workers] [t1,t2,...]   (no times = the whole video; each worker is its own browser)
// Every frame is finish.html's (1920 x 1080). For a '3d' step, the assembly video's page (ASM, ../assembly_video/build)
// renders that step's state first (renderAsm, at 1.5x its 1280 x 720), and its picture and the labels' positions go in.
const { chromium } = require('playwright');
const fs = require('fs'), path = require('path');
const DIR = __dirname, ASM = process.env.ASM || path.join(DIR, '..', '..', 'assembly_video', 'build');
const OUT = process.argv[2], FPS = +process.argv[3] || 30, WORKERS = +process.argv[4] || 1, ONLY = process.argv[5];
const STEPS = JSON.parse(fs.readFileSync(path.join(DIR, 'steps.json'))), TL = JSON.parse(fs.readFileSync(path.join(DIR, 'timeline.json')));
const TOTAL = TL[TL.length - 1].t0 + TL[TL.length - 1].dur;
const clamp01 = t => Math.max(0, Math.min(1, t)), ease = t => { t = clamp01(t); return t * t * (3 - 2 * t); }, lerp = (a, b, t) => a + (b - a) * t;
const stepAt = t => { let i = 0; while (i + 1 < TL.length && TL[i + 1].t0 <= t) i++; return i; };
const types = f => f.endsWith('.html') ? 'text/html' : f.endsWith('.json') ? 'application/json' : f.endsWith('.png') ? 'image/png' : 'application/octet-stream';
async function page(browser, root, file, w, h, scale) {
  const p = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: scale });
  p.on('console', m => { if (!m.text().includes('willReadFrequently')) console.log('page:', m.text()); }); p.on('pageerror', e => console.log('page error:', e.message));
  await p.route('http://anim.local/**', r => { const f = path.join(root, decodeURIComponent(new URL(r.request().url()).pathname)); r.fulfill({ status: 200, contentType: types(f), body: fs.readFileSync(f) }); });
  await p.goto('http://anim.local/' + file); await p.evaluate(() => window.ready); return p;
}
async function open() {          // the 2D page in a plain browser; the 3D one (WebGL) in its own, on SwiftShader
  const browser = await chromium.launch();
  const flat = await page(browser, DIR, 'finish.html', 1920, 1080, 1);
  const need3d = STEPS.some(s => s.scene === '3d');
  const b3 = need3d ? await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] }) : null;
  const asm = need3d ? await page(b3, ASM, 'index.html', 1280, 720, 1.5) : null;
  return { browser, b3, flat, asm };
}
function asmArgs(s, lt, dur) {       // the 3D page's step, its fraction and the camera at this moment
  const a = s.asm, k = clamp01((lt - (a.lead ?? 0.4)) / Math.max(0.5, dur - (a.lead ?? 0.4) - (a.tail ?? 1.2)));
  const frac = lerp(a.from ?? 0, a.to ?? 1, a.ease === false ? k : ease(k));
  let view = a.view || null;
  if (a.view && a.view2) { const e = ease(clamp01((lt - (a.v2at ?? 0.5)) / (a.v2dur ?? (dur - 1.5)))); view = { t: [0, 1, 2].map(j => lerp(a.view.t[j], a.view2.t[j], e)), az: lerp(a.view.az, a.view2.az, e), el: lerp(a.view.el, a.view2.el, e), d: lerp(a.view.d, a.view2.d, e) }; }
  return [a.id, frac, { view, noLabels: !!a.noLabels }];
}
(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  console.log('total seconds', TOTAL.toFixed(1), 'steps', STEPS.length);
  const times = ONLY ? ONLY.split(',').map(Number) : Array.from({ length: Math.ceil(TOTAL * FPS) }, (_, k) => k / FPS);
  const spans = (process.env.RANGE || '').split(',').filter(Boolean).map(r => r.split('-').map(Number));
  const keep = k => !spans.length || spans.some(([a, b]) => times[k] >= a && times[k] <= b);
  const t0 = Date.now(); let done = 0;
  const work = async (w, ctx) => {
    for (let k = w; k < times.length; k += WORKERS) {
      if (!keep(k)) continue;
      const t = Math.min(times[k], TOTAL - 0.001), i = stepAt(t), s = STEPS[i];
      let d3 = null;
      if (s.scene === '3d') {
        const [id, frac, o] = asmArgs(s, t - TL[i].t0, TL[i].dur), key = JSON.stringify([id, frac, o, s.pins]);
        if (ctx.key === key) d3 = ctx.d3; else {
        const pins = await ctx.asm.evaluate(([id, frac, o, pins]) => { window.renderAsm(id, frac, o); return pins.map(p => window.project(p)); }, [id, frac, o, (s.pins || []).map(p => p[0])]);
        const buf = await ctx.asm.screenshot({ type: 'jpeg', quality: 93 });
        d3 = { img: 'data:image/jpeg;base64,' + buf.toString('base64'), pins }; ctx.key = key; ctx.d3 = d3;      // a still shot: render it once
      } }
      await ctx.flat.evaluate(([t, d3]) => window.render(t, d3), [times[k], d3]);
      await ctx.flat.screenshot({ path: path.join(OUT, `f${String(k).padStart(5, '0')}.jpg`), type: 'jpeg', quality: 92 });
      if (++done % 300 === 0) console.log(`frame ${done}/${times.length}  ${((Date.now() - t0) / 1000).toFixed(0)} s`);
    }
    await ctx.browser.close(); if (ctx.b3) await ctx.b3.close();
  };
  const ctxs = []; for (let w = 0; w < WORKERS; w++) ctxs.push(await open());
  await Promise.all(ctxs.map((c, w) => work(w, c)));
  console.log('done', times.length, 'frames in', ((Date.now() - t0) / 1000).toFixed(0), 's');
})();
