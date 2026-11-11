// Render deck slides to PNG files and one PDF.
// Usage: node export.js <deckProjectDir> <outImgDir> <outPdfPath>
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const [deckDir, imgDir, pdfPath] = process.argv.slice(2);
const deck = JSON.parse(fs.readFileSync(path.join(deckDir, 'deck.json'), 'utf8'));
const order = deck.order;

const HEAD = `<meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;700&display=swap">
<style>
  @page { size: 1920px 1080px; margin: 0; }
  * { margin: 0; box-sizing: border-box; }
  html, body { background: #ffffff; }
  section { position: relative; width: 1920px; height: 1080px; overflow: hidden; break-after: page; }
  h1 { font-size: 96px; font-weight: 600; line-height: 1.1; }
  h2 { font-size: 64px; font-weight: 600; line-height: 1.15; }
  h3 { font-size: 44px; font-weight: 600; line-height: 1.2; }
  p { font-size: 32px; line-height: 1.4; }
  p, h1, h2, h3, td, th { word-break: keep-all; overflow-wrap: break-word; }
  table { border-collapse: collapse; width: 100%; }
  td, th { border: 1px solid #c9c4b8; padding: .35em .6em; font-weight: 400; text-align: left; }
  th { font-weight: 600; }
  aside { display: none; }
  x-connector { display: none; }
  x-shape { display: block; }
</style>`;

// Runs inside the page: draws connectors and shapes, applies column alignment.
function shim() {
  const NS = 'http://www.w3.org/2000/svg';
  for (const sec of document.querySelectorAll('section')) {
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', 1920); svg.setAttribute('height', 1080);
    svg.style.cssText = 'position:absolute;left:0;top:0;pointer-events:none';
    for (const c of sec.querySelectorAll('x-connector')) {
      const x1 = +c.getAttribute('x1'), y1 = +c.getAttribute('y1'), x2 = +c.getAttribute('x2'), y2 = +c.getAttribute('y2');
      if (isNaN(x1)) continue;
      const st = c.style, col = st.color || '#000', w = parseFloat(st.borderWidth) || 2, dash = st.borderStyle === 'dashed';
      const route = c.getAttribute('route') || 'straight';
      let pts = [[x1, y1], [x2, y2]];
      if (route === 'hv') pts = [[x1, y1], [x2, y1], [x2, y2]];
      if (route === 'vh') pts = [[x1, y1], [x1, y2], [x2, y2]];
      const pl = document.createElementNS(NS, 'polyline');
      pl.setAttribute('points', pts.map(q => q.join(',')).join(' '));
      pl.setAttribute('fill', 'none'); pl.setAttribute('stroke', col); pl.setAttribute('stroke-width', w);
      pl.setAttribute('stroke-linecap', 'round'); pl.setAttribute('stroke-linejoin', 'round');
      if (dash) pl.setAttribute('stroke-dasharray', `${w * 3} ${w * 2.5}`);
      svg.appendChild(pl);
      const head = (ax, ay, bx, by) => {
        const a = Math.atan2(by - ay, bx - ax), s = w * 4;
        const pt = (d, o) => [bx - d * Math.cos(a) + o * Math.sin(a), by - d * Math.sin(a) - o * Math.cos(a)];
        const tri = document.createElementNS(NS, 'polygon');
        tri.setAttribute('points', `${bx},${by} ${pt(s, s / 2)} ${pt(s, -s / 2)}`);
        tri.setAttribute('fill', col); svg.appendChild(tri);
      };
      const h = c.getAttribute('head') || 'end';
      if (h === 'end' || h === 'both') head(...pts[pts.length - 2], ...pts[pts.length - 1]);
      if (h === 'both') head(...pts[1], ...pts[0]);
    }
    sec.appendChild(svg);
    for (const sh of sec.querySelectorAll('x-shape')) {
      const kind = sh.getAttribute('kind');
      const cs = getComputedStyle(sh);
      const w = parseFloat(cs.width), h = parseFloat(cs.height), fill = cs.backgroundColor;
      if (!kind || !kind.startsWith('arrow')) continue;
      sh.style.background = 'transparent';
      const s = document.createElementNS(NS, 'svg');
      s.setAttribute('width', w); s.setAttribute('height', h); s.setAttribute('viewBox', `0 0 ${w} ${h}`);
      const p = document.createElementNS(NS, 'polygon');
      const hx = w * 0.6, sy1 = h * 0.3, sy2 = h * 0.7;
      p.setAttribute('points', `0,${sy1} ${hx},${sy1} ${hx},0 ${w},${h / 2} ${hx},${h} ${hx},${sy2} 0,${sy2}`);
      p.setAttribute('fill', fill); s.appendChild(p); sh.appendChild(s);
    }
    for (const t of sec.querySelectorAll('table')) {
      const first = t.rows[0]; if (!first) continue;
      [...first.cells].forEach((c, i) => {
        const al = c.style.textAlign;
        if (al) for (const r of t.rows) if (r.cells[i]) r.cells[i].style.textAlign = al;
      });
    }
  }
}

(async () => {
  fs.mkdirSync(imgDir, { recursive: true });
  const b = await chromium.launch();
  const slides = order.map(id => fs.readFileSync(path.join(deckDir, 'slides', id + '.html'), 'utf8'));
  // PNG per slide
  const p = await b.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  const files = [];
  for (let i = 0; i < order.length; i++) {
    await p.setContent(`<!doctype html><html><head>${HEAD}</head><body>${slides[i]}</body></html>`);
    await p.evaluate(() => document.fonts.ready); await p.waitForTimeout(300);
    const ok = await p.evaluate(() => document.fonts.check("24px 'Noto Sans KR'"));
    await p.evaluate(shim);
    const name = `${String(i + 1).padStart(2, '0')}-${order[i]}.png`;
    await p.screenshot({ path: path.join(imgDir, name) });
    files.push(name);
    if (!ok) console.log('font missing on', order[i]);
  }
  // One PDF
  const q = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  await q.setContent(`<!doctype html><html><head>${HEAD}</head><body>${slides.join('\n')}</body></html>`);
  await q.evaluate(() => document.fonts.ready); await q.waitForTimeout(800);
  await q.evaluate(shim);
  await q.pdf({ path: pdfPath, width: '1920px', height: '1080px', printBackground: true, margin: { top: 0, right: 0, bottom: 0, left: 0 } });
  await b.close();
  console.log(JSON.stringify(files));
})();
