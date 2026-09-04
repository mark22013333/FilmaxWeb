/* 相片檢視器（PhotoSwipe 5 的接線層）。
 *
 * 為什麼不是在原本的 #lightbox 上自己加縮放：
 * 原本的燈箱只有「換張」與「關閉」。要做到點圖放大、雙指捏合、拖曳平移、
 * 邊界回彈、雙擊在 fit 與 1:1 之間切換，這些手勢的慣性與邊界處理是
 * 整個功能真正的成本所在，而且在行動裝置上最容易做不順。
 * PhotoSwipe 5 是 MIT、無依賴、54KB（min），這幾件事都已經做對了。
 *
 * 這個檔案只做三件事：
 *   1. 把 /api/photos 的資料列轉成 PhotoSwipe 的 dataSource
 *   2. 加一個「拍攝資訊」側欄（PhotoSwipe 本身沒有，而 EXIF 是相片庫的重點）
 *   3. 縮放超過 preview 的實際像素時，把圖換成原圖
 *
 * 刻意用動態 import 從 app.js 載進來，app.js 才不必變成 module
 * （變成 module 的話 onclick="..." 那些內聯呼叫會全部失效）。
 */
import PhotoSwipe from './vendor/photoswipe/photoswipe.esm.min.js';

const INFO_KEY = 'fx.photoInfoPane';
const INFO_WIDTH = 300;          // 桌機側欄寬度，要與 style.css 的 .pswp__info 一致
const INFO_HEIGHT = 0.4;         // 手機底部面板高度（視窗高度比例）
const MOBILE = 820;

let pswp = null;

/** 側欄要不要展開 —— 記住使用者上次的選擇。無痛失敗：讀不到就當展開。 */
function infoPref() {
  try {
    return localStorage.getItem(INFO_KEY) !== '0';
  } catch { return true; }
}

function setInfoPref(on) {
  try { localStorage.setItem(INFO_KEY, on ? '1' : '0'); } catch { /* 隱私模式 */ }
}

const isNarrow = () => window.innerWidth <= MOBILE;

/**
 * 開啟相片檢視器。
 *
 * @param {object} o
 * @param {Array}  o.items      /api/photos 回傳的資料列（要有 id/width/height/filename）
 * @param {number} o.index      要先看哪一張
 * @param {number} o.previewPx  preview 端點的長邊像素，用來判斷何時該換原圖
 * @param {(item:object)=>Promise<string>} o.loadInfo  回傳側欄的 HTML
 * @param {(i:number)=>Element|null} o.thumbEl  第 i 張在相片牆上的 <img>，用來做放大動畫
 */
export function openPhotos({ items, index, previewPx = 1920, loadInfo, thumbEl }) {
  if (pswp) pswp.destroy();

  let infoOpen = infoPref();

  const dataSource = items.map(it => {
    // PhotoSwipe 需要事先知道長寬。資料庫裡本來就有（掃描時 PIL 讀的，
    // 而且已經照 EXIF orientation 轉正過），所以正常情況不必等圖載完。
    const known = it.width > 0 && it.height > 0;
    return {
      src: `/api/photo/${it.id}/preview.jpg`,
      msrc: it.thumb ? `/api/photo/${it.id}/thumb.jpg` : undefined,
      // 長寬給的是**原圖**尺寸，不是 preview 的。這樣 1:1 就真的是原圖的
      // 1:1，縮放級距也照原圖算；顯示的則是 preview 被瀏覽器放大的樣子，
      // 超過 preview 實際像素時再換成原圖（見下面的 zoomPanUpdate）。
      width: known ? it.width : 1600,
      height: known ? it.height : 1200,
      alt: it.filename || '',
      _item: it,
      _guessed: !known,
      _full: `/api/photo/${it.id}/full`,
      // preview 的實際像素：長邊被縮到 previewPx，短邊等比例。原圖比 previewPx
      // 小的話 make_thumb 不會放大，所以取兩者較小值。
      _previewPx: known ? Math.min(previewPx, Math.max(it.width, it.height)) : previewPx,
    };
  });

  pswp = new PhotoSwipe({
    dataSource,
    index,
    bgOpacity: 0.94,
    // 相片庫裡滾輪的預期行為就是縮放，不是換張
    wheelToZoom: true,
    initialZoomLevel: 'fit',
    secondaryZoomLevel: 1,          // 雙擊 / 點放大鏡 = 原圖 1:1
    maxZoomLevel: 4,
    preload: [1, 2],
    errorMsg: '這張圖讀不到（可能是 FTP 連線問題）',
    closeTitle: '關閉 (Esc)',
    zoomTitle: '縮放 (雙擊圖片也可以)',
    arrowPrevTitle: '上一張 (←)',
    arrowNextTitle: '下一張 (→)',
    // 側欄展開時把可視區域讓出來，圖不會被壓在面板底下。
    // PhotoSwipe 支援動態 padding，切換後叫 updateSize() 重算即可。
    paddingFn: () => {
      if (!infoOpen) return { top: 0, bottom: 0, left: 0, right: 0 };
      return isNarrow()
        ? { top: 0, bottom: Math.round(window.innerHeight * INFO_HEIGHT), left: 0, right: 0 }
        : { top: 0, bottom: 0, left: 0, right: INFO_WIDTH };
    },
  });

  // 放大動畫要從相片牆上那張縮圖長出來，關閉時縮回去。
  if (thumbEl) pswp.addFilter('thumbEl', (el, _data, i) => thumbEl(i) || el);

  /* ---------------- 拍攝資訊側欄 ---------------- */
  let infoEl = null;
  const shown = new Map();          // photo id -> 已載好的 HTML，翻回來不必再要一次

  function applyInfoClass() {
    pswp.element.classList.toggle('pswp--info-open', infoOpen);
  }

  async function fillInfo(slide) {
    if (!infoEl || !slide) return;
    const it = slide.data?._item;
    if (!it) return;
    if (shown.has(it.id)) { infoEl.innerHTML = shown.get(it.id); return; }
    infoEl.innerHTML = `<h3>${escapeHtml(it.filename || '')}</h3>
      <div class="sub">載入中…</div>`;
    let html;
    try { html = await loadInfo(it); } catch { html = '<div class="sub">資訊讀不到</div>'; }
    shown.set(it.id, html);
    // 使用者可能已經翻到別張了
    if (pswp && pswp.currSlide?.data?._item?.id === it.id) infoEl.innerHTML = html;
  }

  pswp.on('uiRegister', () => {
    pswp.ui.registerElement({
      name: 'info-toggle',
      order: 8,
      isButton: true,
      html: '<span class="pswp__info-i" aria-hidden="true">i</span>',
      title: '拍攝資訊 (I)',
      appendTo: 'bar',
      onClick: () => toggleInfo(),
    });
    pswp.ui.registerElement({
      name: 'info',
      appendTo: 'root',
      onInit: el => {
        // class 由 PhotoSwipe 自己給（pswp__info + pswp__hide-on-close），
        // 覆寫掉的話面板不會跟著 UI 一起淡入淡出，也拿不到 pointer-events。
        infoEl = el;
        applyInfoClass();
        fillInfo(pswp.currSlide);
      },
    });
  });

  function toggleInfo(force) {
    infoOpen = force === undefined ? !infoOpen : !!force;
    setInfoPref(infoOpen);
    applyInfoClass();
    pswp.updateSize(true);          // padding 變了要重算，不然圖的位置會怪
    if (infoOpen) fillInfo(pswp.currSlide);
  }

  pswp.on('change', () => fillInfo(pswp.currSlide));

  /* ---------------- 尺寸未知的那幾張 ---------------- */
  // 正常照片掃描時就有長寬。少數讀不到（壞掉的 EXIF、奇怪的格式）先用 4:3
  // 佔位，圖載好之後用真實像素修正，否則那張的縮放級距會是錯的。
  pswp.on('loadComplete', ({ content, slide }) => {
    const img = content?.element;
    if (!slide?.data?._guessed || !img?.naturalWidth) return;
    slide.data.width = slide.width = img.naturalWidth;
    slide.data.height = slide.height = img.naturalHeight;
    slide.data._guessed = false;
    // 只改長寬不夠：縮放級距（fit 是幾倍）是用舊的佔位尺寸算的，
    // 不重算的話這張一開就是放大而且比例是錯的。這四步是 PhotoSwipe
    // 自己 resize() 裡的那一串，直接照做 —— 不呼叫 resize() 是因為它
    // 會先比對 currZoomLevel 是否還在初始值，而這裡兩邊都正在改，
    // 那個比對在這個時機點不成立（實測會停在 0.11 倍）。
    try {
      slide.calculateSize();
      slide.currentResolution = 0;
      slide.zoomAndPanToInitial();
      slide.applyCurrentZoomPan();
      slide.updateContentSize(true);
    } catch { /* 只是級距不完美，不要因此炸掉整個檢視器 */ }
  });

  /* ---------------- 放大超過 preview 就換原圖 ---------------- */
  // preview 是 1920 長邊的衍生圖，fit 顯示時綽綽有餘。但「點圖放大」的重點
  // 就是看細節，放到 1:1 還餵 preview 等於看放大的模糊。
  // 只在真的需要時才拉原圖（中位數 2MB、最大 30MB，都要走 FTP）。
  pswp.on('zoomPanUpdate', ({ slide }) => {
    const d = slide?.data;
    if (!d || d._upgraded || d._guessed) return;
    const displayed = slide.width * (slide.currZoomLevel || 0);
    if (displayed <= d._previewPx * 1.15) return;
    d._upgraded = true;             // 先設旗標，事件會連續觸發很多次
    const probe = new Image();
    probe.onload = () => {
      const el = slide.content?.element;
      // 換 src 就好。長寬本來就是原圖的，版面不會跳。
      if (el && el.tagName === 'IMG') el.src = d._full;
    };
    probe.onerror = () => { d._upgraded = false; };
    probe.src = d._full;
  });

  /* ---------------- 鍵盤 ---------------- */
  // 上下張、Esc、縮放 PhotoSwipe 自己處理，這裡只加 I。
  const onKey = e => {
    if (e.key === 'i' || e.key === 'I') { e.preventDefault(); toggleInfo(); }
  };
  document.addEventListener('keydown', onKey);
  pswp.on('destroy', () => {
    document.removeEventListener('keydown', onKey);
    pswp = null;
  });

  pswp.init();
  return pswp;
}

export function closePhotos() {
  if (pswp) pswp.close();
}

export const isOpen = () => !!pswp;

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
