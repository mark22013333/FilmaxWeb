/* PDF 閱讀器。vendored PDF.js（Apache-2.0），不走 CDN —— CSP 是
 * script-src 'self'，而且這個服務要能在沒有外網的區網開起來。
 *
 * 三個刻意的決定：
 *
 * 1. **每一頁先放一個等比例的佔位框，看到才畫。**一份 300 頁的 PDF 如果一開始
 *    就全部 render，記憶體是幾百 MB 起跳、而且要等很久。用 IntersectionObserver
 *    只畫看得到的（前後各留一頁），離開視野就把 canvas 釋放掉。
 *
 * 2. **PDF 裡的 JavaScript 一律不執行**（isEvalSupported:false、不啟用 XFA、
 *    不啟用 scripting）。這些 PDF 是從 FTP 來的，內容不受控。
 *
 * 3. 手勢交給瀏覽器。這一頁沒有 user-scalable=no，雙指縮放是原生行為 ——
 *    自己實作一套 pinch 只會比原生的難用。
 */
import * as pdfjs from './vendor/pdfjs/pdf.min.mjs';

pdfjs.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs';

const VENDOR = '/static/vendor/pdfjs/';
const MAX_DPR = 2;             // 再高就是拿記憶體換看不出來的銳利度
const NEAR = 1;                // 視野外前後各預先畫幾頁

const $ = s => document.querySelector(s);
const docEl = $('#rdDoc');
const statusEl = $('#rdStatus');

const state = {
  pdf: null,
  pages: [],            // { num, wrap, canvas, viewport1, rendering, task }
  mode: 'width',        // 'width' | 'page' | 'scale'
  scale: 1,
  current: 1,
};

function fail(msg) {
  statusEl.textContent = msg;
  statusEl.hidden = false;
}

const docId = new URLSearchParams(location.search).get('doc');

async function api(path) {
  const r = await fetch(path, { headers: { 'Accept': 'application/json' } });
  if (r.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search); return null; }
  if (!r.ok) throw new Error((await r.text().catch(() => '')) || r.statusText);
  return r.json();
}

async function boot() {
  if (!docId) return fail('網址少了 ?doc=<id>');
  let info;
  try { info = await api('/api/documents/' + encodeURIComponent(docId)); }
  catch (e) { return fail('讀不到這份文件：' + e.message); }
  if (!info) return;

  document.title = info.filename + ' — FilmaxWeb';
  $('#rdTitle').textContent = info.filename;
  $('#rdTitle').title = info.folder || '';
  $('#rdRaw').href = info.file_url;

  statusEl.textContent = '開啟中…（第一次開啟要先把整份抓到本機）';
  try {
    state.pdf = await pdfjs.getDocument({
      url: info.file_url,
      // 內嵌字型缺失時的替代字型。cmaps 沒有一起帶 —— 需要它的是
      // 「用了 CJK 字型但沒有內嵌」的 PDF，真的遇到再補那 169 個檔案。
      standardFontDataUrl: VENDOR + 'standard_fonts/',
      isEvalSupported: false,
      enableXfa: false,
      disableAutoFetch: false,
    }).promise;
  } catch (e) {
    return fail('這份 PDF 打不開：' + (e && e.message ? e.message : e));
  }

  $('#rdTotal').textContent = state.pdf.numPages;
  statusEl.hidden = true;
  await buildPages();
  wire();
}

/** 先把每一頁的框架做出來（只問尺寸，不畫內容）。 */
async function buildPages() {
  const frag = document.createDocumentFragment();
  for (let n = 1; n <= state.pdf.numPages; n++) {
    const wrap = document.createElement('div');
    wrap.className = 'rd-page';
    wrap.dataset.page = String(n);
    const num = document.createElement('div');
    num.className = 'rd-num';
    num.textContent = n;
    wrap.appendChild(num);
    frag.appendChild(wrap);
    state.pages.push({ num: n, wrap, canvas: null, viewport1: null, task: null });
  }
  docEl.appendChild(frag);

  // 尺寸一頁一頁問（getPage 很便宜，真正貴的是 render）
  for (const p of state.pages) {
    const page = await state.pdf.getPage(p.num);
    p.viewport1 = page.getViewport({ scale: 1 });
    p.pageObj = page;
  }
  layout();
  observe();
}

function viewportWidth() {
  return Math.max(320, docEl.clientWidth - 32);
}

function viewportHeight() {
  return Math.max(320, window.innerHeight - docEl.getBoundingClientRect().top - 32);
}

function scaleFor(p) {
  if (state.mode === 'width') return viewportWidth() / p.viewport1.width;
  if (state.mode === 'page') {
    return Math.min(viewportWidth() / p.viewport1.width,
                    viewportHeight() / p.viewport1.height);
  }
  return state.scale;
}

/** 依目前的縮放模式排好每一頁的框（不畫內容）。 */
function layout() {
  for (const p of state.pages) {
    if (!p.viewport1) continue;
    const s = scaleFor(p);
    p.wrap.style.width = Math.round(p.viewport1.width * s) + 'px';
    p.wrap.style.height = Math.round(p.viewport1.height * s) + 'px';
    // 縮放變了，已經畫好的 canvas 解析度就不對了 —— 丟掉重畫
    drop(p);
  }
  renderVisible();
}

function drop(p) {
  if (p.task) { try { p.task.cancel(); } catch { /* 已經結束了 */ } p.task = null; }
  if (p.canvas) { p.canvas.remove(); p.canvas = null; }
}

async function render(p) {
  if (p.canvas || p.task || !p.pageObj) return;
  const s = scaleFor(p);
  const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
  const vp = p.pageObj.getViewport({ scale: s * dpr });
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(vp.width);
  canvas.height = Math.round(vp.height);
  canvas.className = 'rd-canvas';
  p.wrap.appendChild(canvas);
  p.canvas = canvas;
  try {
    p.task = p.pageObj.render({ canvasContext: canvas.getContext('2d', { alpha: false }), viewport: vp });
    await p.task.promise;
    p.task = null;
  } catch (e) {
    p.task = null;
    if (e && e.name !== 'RenderingCancelledException') {
      canvas.remove();
      p.canvas = null;
    }
  }
}

let io = null;
function observe() {
  if (io) io.disconnect();
  io = new IntersectionObserver(entries => {
    for (const e of entries) {
      const n = +e.target.dataset.page;
      const p = state.pages[n - 1];
      if (!p) continue;
      if (e.isIntersecting) {
        render(p);
        for (let d = 1; d <= NEAR; d++) {
          if (state.pages[n - 1 + d]) render(state.pages[n - 1 + d]);
          if (state.pages[n - 1 - d]) render(state.pages[n - 1 - d]);
        }
      } else if (Math.abs(n - state.current) > NEAR + 1) {
        drop(p);              // 離得夠遠才釋放，不然來回捲動會一直重畫
      }
    }
  }, { root: null, rootMargin: '200px 0px' });
  for (const p of state.pages) io.observe(p.wrap);

  // 「現在在第幾頁」不能用 IntersectionObserver 的回呼決定 ——
  // 縮小的時候畫面上同時看得到好幾頁，最後進來的那個 entry 會勝出，
  // 於是頁碼會亂跳（實測按一次 → 從 2 跳到 4）。改用捲動位置算：
  // 取「頂端最接近視野上緣」的那一頁。
  docEl.addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; updateCurrent(); });
  }, { passive: true });
  updateCurrent();
}

let ticking = false;
function updateCurrent() {
  const top = docEl.getBoundingClientRect().top;
  let best = state.current, bestDist = Infinity;
  for (const p of state.pages) {
    const r = p.wrap.getBoundingClientRect();
    if (r.bottom < top) continue;                 // 已經滑過去了
    const dist = Math.abs(r.top - top);
    if (dist < bestDist) { bestDist = dist; best = p.num; }
    if (r.top - top > docEl.clientHeight) break;  // 後面的都在視野下方了
  }
  if (best !== state.current) {
    state.current = best;
    $('#rdPage').value = String(best);
  }
}

function renderVisible() {
  const p = state.pages[state.current - 1];
  if (p) render(p);
}

function goto(n) {
  n = Math.max(1, Math.min(state.pages.length, n));
  const p = state.pages[n - 1];
  if (!p) return;
  p.wrap.scrollIntoView({ behavior: 'auto', block: 'start' });
}

function setMode(mode, scale) {
  state.mode = mode;
  if (scale) state.scale = scale;
  $('#rdFitW').classList.toggle('on', mode === 'width');
  $('#rdFitP').classList.toggle('on', mode === 'page');
  layout();
}

function zoom(mult) {
  const p = state.pages[state.current - 1];
  const base = p ? scaleFor(p) : state.scale;
  setMode('scale', Math.max(0.2, Math.min(6, base * mult)));
}

function wire() {
  $('#rdPrev').onclick = () => goto(state.current - 1);
  $('#rdNext').onclick = () => goto(state.current + 1);
  $('#rdIn').onclick = () => zoom(1.25);
  $('#rdOut').onclick = () => zoom(1 / 1.25);
  $('#rdFitW').onclick = () => setMode('width');
  $('#rdFitP').onclick = () => setMode('page');
  $('#rdPage').onchange = e => {
    const n = parseInt(e.target.value, 10);
    if (!isNaN(n)) goto(n); else e.target.value = String(state.current);
  };

  document.addEventListener('keydown', e => {
    if (e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
    switch (e.key) {
      case 'ArrowRight': case 'PageDown': case ' ':
        e.preventDefault(); goto(state.current + 1); break;
      case 'ArrowLeft': case 'PageUp':
        e.preventDefault(); goto(state.current - 1); break;
      case 'Home': e.preventDefault(); goto(1); break;
      case 'End': e.preventDefault(); goto(state.pages.length); break;
      case '+': case '=': e.preventDefault(); zoom(1.25); break;
      case '-': e.preventDefault(); zoom(1 / 1.25); break;
    }
  });

  // 視窗大小變了，符合寬度／整頁的倍率就跟著變。debounce 是必要的：
  // 拖視窗邊緣會連續觸發幾十次，每次都重畫的話會卡住。
  let t;
  window.addEventListener('resize', () => {
    if (state.mode === 'scale') return;
    clearTimeout(t);
    t = setTimeout(layout, 150);
  });
}

setMode('width');
boot();
