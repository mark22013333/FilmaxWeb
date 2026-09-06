/* FilmaxWeb 自製播放器
   不用瀏覽器原生控制列，字幕自己渲染 —— 才能完整控制大小 / 顏色 / 位置 / 延遲。 */
const $ = s => document.querySelector(s);
const params = new URLSearchParams(location.search);
const fileId = params.get('file');
const v = $('#v'), pl = $('#pl');

let info = null, hlsObj = null, mode = null, saveTimer = null, seekingByUs = false;
// ABR 目前選中的那一階（hls.js 的 level 物件）。是當下狀態，不進 cfg。
let curLevel = null;

/* ------------------------------------------------ 小工具 ------------------------------------------------ */
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
const clamp = (n, a, b) => Math.min(b, Math.max(a, n));
const fmt = s => {
  if (!isFinite(s) || s < 0) s = 0;
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(x).padStart(2,'0')}`
           : `${m}:${String(x).padStart(2,'0')}`;
};
let tt;
function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(tt); tt = setTimeout(() => t.classList.remove('show'), 2000);
}
// 緩衝/轉碼中用角落的小標示，不要拿大黑框蓋住畫面
function busy(text) {
  const el = $('#busy');
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('show', !!text);
}

// 這是不是行動裝置。**用來調 ABR 的保守程度，不是用來決定畫質** ——
// 畫質上限由伺服器按 IP 判（客戶端自報的東西只能收緊、不能放寬）。
// coarse pointer ＋ 沒有 hover 是目前最可靠的判準：只看 userAgent 會被
// 桌機的「要求電腦版網站」騙過，只看寬度會把小視窗的桌機誤判成手機。
function isMobile() {
  try {
    if (matchMedia('(pointer: coarse)').matches && matchMedia('(hover: none)').matches) return true;
  } catch {}
  return Math.min(screen.width, screen.height) <= 480;
}

function center(text, spinning = true) {
  if (text) {
    $('#centerText').textContent = text;
    $('#spin').style.display = spinning ? '' : 'none';
    $('#center').classList.add('show');
  } else $('#center').classList.remove('show');
}

/* ------------------------------------------------ 設定保存 ------------------------------------------------ */
const DEFAULTS = {
  subSize: 32, subColor: '#ffffff', subBg: 'none', subEdge: 'shadow', subPos: 'bottom',
  subOffset: 10, subFont: 'sans', subWeight: 600, subDelay: 0,
  volume: 1, muted: false, rate: 1, quality: 0,
  skip: 10,
  // 每個檔案記住自己選過的字幕：-1 = 這個檔案關字幕，>=0 = 指定軌道，沒有這筆 = 自動挑繁中。
  // key 前面加 f 是刻意的：純數字 key 會被 JS 依大小重排，之後就沒辦法照「最近用過」剪裁。
  subPick: {},
  audioPick: {},          // 同上，記住每部片選過的音軌
};
// 目前螢幕上是哪一軌 —— 這是當下狀態，不該跟著設定存起來（存了會被套到別的檔案上）
let curSub = -1;
// 舊版把「目前第幾軌」當設定存起來，會被套到別的檔案上。讀到就丟掉。
function dropLegacy(c) { delete c.subTrack; delete c.subManual; delete c.subMode; }

// prefsAt 是伺服器蓋的章，不是本機時鐘 —— 本機只負責記「有沒有還沒推上去的修改」。
// 這樣就算這台電腦時間不準，也不會把別台的設定判成舊的。
let prefsAt = 0;
let prefsDirty = false;
let cfg = { ...DEFAULTS };
try {
  const raw = JSON.parse(localStorage.getItem('filmax.player') || '{}');
  prefsAt = raw.__at || 0; prefsDirty = !!raw.__dirty;
  delete raw.__at; delete raw.__dirty;
  cfg = { ...DEFAULTS, ...raw };
  dropLegacy(cfg);
} catch {}

let syncTimer = null;
function writeLocal() {
  try {
    localStorage.setItem('filmax.player',
      JSON.stringify({ ...cfg, __at: prefsAt, __dirty: prefsDirty }));
  } catch {}
}

function saveCfg() {
  prefsDirty = true;
  writeLocal();
  // 先寫瀏覽器（立即生效、離線也不會掉），過幾秒才同步到伺服器
  clearTimeout(syncTimer);
  syncTimer = setTimeout(pushPrefs, 3000);
}

async function pushPrefs() {
  clearTimeout(syncTimer); syncTimer = null;
  try {
    const r = await fetch('/api/prefs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: cfg }),
    });
    if (!r.ok) return;
    const rec = await r.json();
    if (rec?.ok) { prefsAt = rec.updated_at || prefsAt; prefsDirty = false; writeLocal(); }
  } catch {}   // 推不上去就維持 dirty，下次存檔或下次開頁再試
}

async function pullPrefs() {
  try {
    const r = await fetch('/api/prefs', { cache: 'no-store' });
    if (!r.ok) return;
    const rec = await r.json();
    // 本機還有沒推上去的修改 → 本機優先，先推上去
    if (prefsDirty) { pushPrefs(); return false; }
    // 否則只要伺服器那份跟本機記到的章不一樣，就以伺服器為準（別台改過了）
    if (rec && rec.data && (rec.updated_at || 0) !== prefsAt) {
      cfg = { ...DEFAULTS, ...rec.data };
      dropLegacy(cfg);
      prefsAt = rec.updated_at || 0;
      writeLocal();
      return true;
    }
  } catch {}
  return false;
}
window.addEventListener('beforeunload', () => { if (prefsDirty) pushPrefs(); });
// 切到別的分頁 / 鎖螢幕時也推一次，手機上 beforeunload 常常不會觸發
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden' && prefsDirty) pushPrefs();
});

const FONTS = {
  sans: '"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif',
  serif: '"Noto Serif TC","Songti TC","PMingLiU",serif',
  mono: 'ui-monospace,"Cascadia Mono",Consolas,monospace',
};
const EDGES = {
  none: 'none',
  shadow: '0 2px 4px rgba(0,0,0,.9)',
  outline: '-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000,1px 1px 0 #000,0 0 6px rgba(0,0,0,.9)',
  heavy: '-2px -2px 0 #000,2px -2px 0 #000,-2px 2px 0 #000,2px 2px 0 #000,0 0 10px #000',
};
const BGS = { none: 'transparent', dim: 'rgba(0,0,0,.5)', solid: 'rgba(0,0,0,.85)' };

function applySubStyle() {
  const box = $('#subs'), st = box.style;
  st.setProperty('--sub-size', cfg.subSize + 'px');
  st.setProperty('--sub-color', cfg.subColor);
  st.setProperty('--sub-bg', BGS[cfg.subBg] || 'transparent');
  st.setProperty('--sub-shadow', EDGES[cfg.subEdge] || EDGES.shadow);
  st.setProperty('--sub-font', FONTS[cfg.subFont] || FONTS.sans);
  st.setProperty('--sub-weight', cfg.subWeight);
  st.setProperty('--sub-offset', cfg.subOffset + '%');
  st.setProperty('--sub-pad', cfg.subBg === 'none' ? '.1em .45em' : '.15em .6em');
  box.className = 'subs ' + (cfg.subPos === 'top' ? 'top' : 'bottom');
}

/* ------------------------------------------------ 每個檔案記住自己的字幕選擇 ------------------------------------------------ */
const PICK_KEY = 'f' + fileId;
const PICK_MAX = 300;

function savedPick() {
  const x = cfg.subPick?.[PICK_KEY];
  return typeof x === 'number' ? x : undefined;
}
function rememberPick(idx) {
  if (!cfg.subPick) cfg.subPick = {};
  delete cfg.subPick[PICK_KEY];           // 先刪再塞 = 移到最後，剪裁時才知道誰最久沒用
  cfg.subPick[PICK_KEY] = idx;
  const keys = Object.keys(cfg.subPick);
  if (keys.length > PICK_MAX)
    for (const k of keys.slice(0, keys.length - PICK_MAX)) delete cfg.subPick[k];
  saveCfg();
}
function forgetPick() {
  if (cfg.subPick) delete cfg.subPick[PICK_KEY];
  saveCfg();
}

function savedAudio() {
  const x = cfg.audioPick?.[PICK_KEY];
  return typeof x === 'number' ? x : undefined;
}
function rememberAudio(index) {
  if (!cfg.audioPick) cfg.audioPick = {};
  delete cfg.audioPick[PICK_KEY];
  cfg.audioPick[PICK_KEY] = index;
  const keys = Object.keys(cfg.audioPick);
  if (keys.length > PICK_MAX)
    for (const k of keys.slice(0, keys.length - PICK_MAX)) delete cfg.audioPick[k];
  saveCfg();
}

/* ------------------------------------------------ 字幕挑選 ------------------------------------------------ */
// 只在繁體出現、簡體不會出現的字（以及反過來）。用來在語言標籤分不出繁簡時判斷內容。
const HANT_ONLY = '實體灣國電關與這來時會學習點對開發們個為說話語邊還沒華萬價買賣車東門問間題經濟認識讓變數樣機經過從業務員長遠親愛醫藥聽覺讀寫應該當現實際';
const HANS_ONLY = '实体湾国电关与这来时会学习点对开发们个为说话语边还没华万价买卖车东门问间题经济认识让变数样机经过从业务员长远亲爱医药听觉读写应该当现实际';

function hantScore(text) {
  let hant = 0, hans = 0;
  for (const ch of text) {
    if (HANT_ONLY.includes(ch)) hant++;
    else if (HANS_ONLY.includes(ch)) hans++;
  }
  if (hant + hans < 4) return 0;              // 樣本太少不下判斷
  return (hant - hans) / (hant + hans);       // -1 = 簡體, +1 = 繁體
}

const RE_HANT = /(zh[-_]?(tw|hant|hk|mo)|cht|big5|繁體|繁体|正體|正体|繁中|台繁|港繁|traditional)/i;
const RE_HANS = /(zh[-_]?(cn|hans|sg)|chs|gb2312|gbk|简体|簡體|简中|simplified)/i;
const RE_ZH   = /(^zh|chi|zho|中文|中字|漢語|汉语|chinese)/i;
// \beng?\b 是刻意的：直接寫 eng 會把 Bengali、Bengalí 這種也當成英文
const RE_ENG  = /(\beng?\b|\benglish\b|英文|英語)/i;

// 設定面板只直接列繁中和英文，其餘收進「其他字幕」。
// 有些片十幾條字幕軌，全部攤開的話整個面板都是字幕，其他設定要滑很久才看得到。
function trackGroup(t) {
  if (t.graphic) return 'other';   // 選不了的東西不該佔著主要位置
  const hay = `${t.lang || ''} ${t.label || ''}`;
  if (RE_HANS.test(hay) && !RE_HANT.test(hay)) return 'other';   // 明講簡體的收起來
  if (RE_HANT.test(hay) || RE_ZH.test(hay)) return 'main';       // 繁體、或沒標繁簡的中文
  if (RE_ENG.test(hay)) return 'main';
  return 'other';
}

function scoreTrack(t) {
  if (t.graphic) return -999;      // PGS/VobSub 轉不出 WebVTT，絕不自動選
  const hay = `${t.lang || ''} ${t.label || ''}`;
  let n = 0;
  if (RE_HANT.test(hay)) n += 100;
  else if (RE_ZH.test(hay)) n += 60;
  if (RE_HANS.test(hay)) n -= 50;
  if (t.kind === 'external') n += 5;           // 外掛字幕通常是特地找的，優先一點
  if (/(sdh|hearing|forced|強制|强制)/i.test(hay)) n -= 15;
  return n;
}

function pickAutoTrack(exclude = []) {
  const subs = info?.subtitles || [];
  let best = -1, bestScore = 0;
  subs.forEach((t, i) => {
    if (exclude.includes(i) || t.graphic) return;
    const n = scoreTrack(t);
    if (n > bestScore) { bestScore = n; best = i; }
  });
  return best;
}

/* ------------------------------------------------ 字幕 ------------------------------------------------ */
let cues = [], cueIdx = -1;

/* 原生字幕軌。
   進 iOS 原生全螢幕時畫面交給系統播放器接管，自繪的那一層會整個不見。
   但已經解析好的 cue 可以同時餵給一個原生 TextTrack —— 這樣
   「邊抽邊送」的漸進式載入完全保留（大檔第一句 0.11 秒），
   只有外觀樣式在原生模式下改由系統決定。 */
let nativeTrack = null;

function resetNativeTrack(label, lang) {
  if (!nativeTrack) {
    try { nativeTrack = v.addTextTrack('subtitles', label || '字幕', lang || 'und'); }
    catch { nativeTrack = null; }
  }
  if (!nativeTrack) return;
  // 「關著」要用 hidden 不是 disabled。
  // disabled 狀態下 track.cues 是 null（規格如此），所以下面那個清空迴圈
  // 會安靜地什麼都不做 —— 換字幕軌時舊的句子會留著，中英文疊在一起。
  // hidden 是「有在跑但不顯示」，cues 讀得到、清得掉，正是我們要的。
  nativeTrack.mode = 'hidden';
  try {
    while (nativeTrack.cues && nativeTrack.cues.length) {
      nativeTrack.removeCue(nativeTrack.cues[0]);
    }
  } catch {}
}

function pushNativeCue(c) {
  if (!nativeTrack || !window.VTTCue) return;
  try {
    // 用純文字：系統播放器不吃我們那套 <b>/<i> 的處理，而且
    // 塞 HTML 進去在某些版本會整句不顯示
    const txt = String(c.html || '').replace(/<[^>]*>/g, '');
    if (txt) nativeTrack.addCue(new VTTCue(c.start, c.end, txt));
  } catch {}
}

function showNativeTrack(on) {
  if (nativeTrack) { try { nativeTrack.mode = on ? 'showing' : 'hidden'; } catch {} }
  // 自繪那層跟原生軌不能同時開，不然會看到兩份字幕
  $('#subs').style.visibility = on ? 'hidden' : '';
}
let subGen = 0;          // 換軌時讓還在跑的舊串流自己收工
// 唯讀角色沒有下載權限，就別顯示那個按鈕（後端一樣會擋）
let canDownload = true;

// 解析單一段 VTT 區塊。拆出來是為了能邊下載邊解析 —— 大檔案抽字幕要跑很久，
// 等整份下載完才顯示的話，使用者按下字幕後要乾等好幾十秒。
function parseCueBlock(block) {
  const lines = block.split('\n').filter(l => l.trim() !== '');
  if (!lines.length) return null;
  if (/^NOTE\b/.test(lines[0]) || /^STYLE\b/.test(lines[0]) || /^REGION\b/.test(lines[0])) return null;
  const i = lines.findIndex(l => l.includes('-->'));
  if (i < 0) return null;
  const m = lines[i].match(/((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})/);
  if (!m) return null;
  const t = x => {
    const p = x.replace(',', '.').split(':').map(Number);
    return p.length === 3 ? p[0]*3600 + p[1]*60 + p[2] : p[0]*60 + p[1];
  };
  // 字幕內容是外來的（片源內嵌，或 FTP 上撿到的 .srt），最後會被塞進 innerHTML，
  // 所以這一段就是消毒器，必須當成不可信輸入處理。兩個要點：
  //   1. 標籤樣式要求 < 後面接字母，而且結尾的 > 是「可有可無」的。
  //      原本寫 [^>]*> 要求一定有收尾的 >，於是 `<img src=x onerror=alert(1)`
  //      這種殘缺標籤會原樣通過，瀏覽器解析時再自己幫它補完 —— 就成了 XSS。
  //   2. 沒被當成標籤的裸 < （例如字幕裡的「a < b」）留到最後才轉義，
  //      不能一律砍掉，否則會把正常台詞吃掉。
  const html = lines.slice(i + 1).join('\n')
    .replace(/<\/?[a-zA-Z][^>]*>?/g, m => /^<\/?[bi]>$/.test(m) ? m : '')
    .replace(/&(?!(amp|lt|gt|quot|#\d+);)/g, '&amp;')
    .replace(/<(?!\/?[bi]>)/g, '&lt;');
  if (!html.trim()) return null;
  return { start: t(m[1]), end: t(m[2]), html };
}

function parseVTT(text) {
  const out = [];
  for (const block of text.replace(/\r\n?/g, '\n').split(/\n{2,}/)) {
    const c = parseCueBlock(block);
    if (c) out.push(c);
  }
  return out.sort((a, b) => a.start - b.start);
}

/* ------------------------------------------------ 開播後才抽字幕 ------------------------------------------------ */
/* 內嵌字幕要把整部片 demux 一遍才收得齊。原本在 boot() 裡是「開播的同一瞬間」
 * 就開始抽 —— 於是抽字幕的 ffmpeg 跟正在轉碼的 ffmpeg 同時對同一個來源檔全速讀，
 * 兩邊搶同一條 FTP。影片的起播因此變慢，而使用者會覺得「整個都在等」。
 *
 * 所以分兩種情況：
 *   已經有快取（info.subtitles[i].cached，由 /api/play 回報）
 *       → 那是本機檔案，讀它沒有頻寬問題，立刻載入。
 *   還沒有快取
 *       → 等影片緩衝穩定幾秒再開始。
 */
const SUB_WARMUP_MS = 5000;
let warmTimer = null;

function subCached(idx) {
  return !!(info?.subtitles?.[idx]?.cached);
}

function startSubtitles(idx, opts = {}) {
  if (idx < 0) return;
  if (subCached(idx)) return loadSubtitle(idx, opts);
  clearTimeout(warmTimer);
  warmTimer = setTimeout(() => loadSubtitle(idx, opts), SUB_WARMUP_MS);
}

/* 字幕是關著的（使用者上次刻意關掉），但還是先把它抽進快取 ——
 * 這是唯一「顯示路徑不會順便產生快取」的情況：真的想看字幕時，
 * 不做預抽就得從零等一次整片 demux。 */
function prefetchSubtitles() {
  if (!info?.subtitle_prefetch) return;
  const idx = pickAutoTrack();
  if (idx < 0 || subCached(idx)) return;
  const url = info.subtitles[idx]?.url;
  if (!url) return;
  clearTimeout(warmTimer);
  warmTimer = setTimeout(async () => {
    try {
      const r = await fetch(url);
      if (!r.ok) return;
      // **一定要把 body 讀完。** 伺服器端 aiter_subtitle_vtt 是非同步產生器，
      // Starlette 靠「取消回應」收尾 —— 前端不讀 body 就等於取消，ffmpeg 會被
      // kill，而半截的快取會被丟掉（media.py 那邊是對的），等於白跑一趟。
      // 內容本身不要，直接排掉。
      if (r.body?.pipeTo) await r.body.pipeTo(new WritableStream({ write() {} }));
      else await r.text();                     // 舊瀏覽器沒有 pipeTo
      const t = info.subtitles[idx];
      if (t) t.cached = true;
    } catch {
      /* 預抽失敗不影響播放：使用者按下字幕時會走正常的載入路徑 */
    }
  }, SUB_WARMUP_MS);
}

// remember：把這次選擇記在這個檔案上（使用者自己點的才要）
// auto    ：這一軌是程式挑的，所以載入後可以看內容決定要不要改挑別軌。
//           使用者指定的軌永遠不會被換掉，即使內容是簡體 —— 那是他要的。
async function loadSubtitle(idx, opts = {}) {
  clearTimeout(warmTimer);        // 使用者自己動作了，取消排程中的暖機
  const { remember = true, auto = false, tried = [] } = opts;
  const track = idx >= 0 ? info?.subtitles?.[idx] : null;
  if (track?.graphic) {
    // 圖形字幕是一張張圖片，要嘛燒進畫面、要嘛 OCR，兩條路都不便宜。
    // 與其點下去沒反應，不如直接說清楚。
    toast(`${track.label} 是圖形字幕（${(track.codec || '').toUpperCase()}），無法顯示`);
    return;
  }
  if (remember) rememberPick(idx);
  curSub = idx;
  const gen = ++subGen;
  cues = []; cueIdx = -1; $('#subs').innerHTML = '';
  resetNativeTrack(track?.label, track?.lang);
  $('#btnSubs').classList.toggle('on', idx >= 0);
  if (idx < 0 || !info.subtitles[idx]) return;

  const s = info.subtitles[idx];
  busy('載入字幕');
  let reader = null;
  try {
    const r = await fetch(s.url);
    if (!r.ok) throw new Error(await r.text());
    if (gen !== subGen) return;

    // 伺服器是邊抽邊送的，這裡也要邊收邊解析，不能等 r.text() 整份到齊。
    // 內嵌字幕要把整部片 demux 過一遍才收得齊，大檔案動輒好幾十秒。
    const dec = new TextDecoder('utf-8');
    let buf = '';
    const feed = (text, final) => {
      buf += text;
      const parts = buf.split(/\n{2,}/);
      buf = final ? '' : (parts.pop() ?? '');   // 最後一塊可能被切一半，留到下次
      if (final && buf.trim()) parts.push(buf);
      for (const b of parts) {
        const c = parseCueBlock(b);
        if (c) { cues.push(c); pushNativeCue(c); }
      }
    };

    // 語言標籤分不出繁簡時看實際內容。
    //
    // **顯示不等判斷。** 原本是「收滿 20 句 → 判斷 → 才畫第一句」，於是即使字幕
    // 已經在串流進來，畫面上還是要空等 20 句。而把門檻降到 10 句並不是好解法：
    // hantScore 在 10 句上明顯更吵（樣本不足時它回 0，就會挑錯軌）。
    //
    // 所以改成：**一有句子就畫**（renderCues 在下面的迴圈裡無條件呼叫），
    // 判斷照舊等 60 句才做、才夠準。判定簡體且還有別軌時那時再換，並提示使用者。
    // 代價是「拿到的是簡體軌」這個少數情況會先看到簡體再換掉 ——
    // 比起所有情況都先等 20 句，這個交換是划算的。
    // 而且有了預抽快取之後，第二次播放整份是瞬間到齊，判斷根本不會被感覺到。
    const CHECK_AT = 60;
    let checked = !auto, simplified = false;
    const check = done => {
      if (checked || (cues.length < CHECK_AT && !done)) return;
      checked = true;
      if (!cues.length) return;
      if (hantScore(cues.slice(0, CHECK_AT).map(c => c.html).join('')) >= -0.3) return;
      // 判定是簡體。但**只有真的有別軌可換時才中止這一軌** ——
      // 沒有替代品還把它砍掉，結果是「整部片只剩前 60 句字幕」：
      // 迴圈 break、reader.cancel()、ffmpeg 被收掉，而後面的句子永遠不會到。
      // 實測確認過這個行為（只有一條簡體軌的片，60 句之後就沒字幕了）。
      if (pickAutoTrack([...tried, idx]) >= 0) simplified = true;
      else toast('這部片只有簡體字幕');
    };

    reader = r.body?.getReader?.();
    if (!reader) {
      feed(await r.text(), true); check(true);          // 不支援串流就退回一次讀完
    } else {
      while (true) {
        const { done, value } = await reader.read();
        if (gen !== subGen) { reader.cancel().catch(() => {}); return; }
        if (value) feed(dec.decode(value, { stream: true }), false);
        if (done) feed(dec.decode(), true);
        check(done);
        if (simplified) break;
        // 有句子就畫，不等繁簡判斷（見上面 CHECK_AT 那段註解）
        if (cues.length) renderCues(true);
        busy(done ? '' : `載入字幕 ${cues.length} 句`);
        if (done) break;
      }
    }

    if (simplified) {
      reader?.cancel().catch(() => {});
      const next = pickAutoTrack([...tried, idx]);
      if (next >= 0) {          // check() 只在有替代軌時才會設 simplified，這裡必成立
        busy('');
        // 已經畫了幾十句簡體出去，換軌時要講一聲 —— 不然畫面自己變了很像壞掉
        if (cues.length) toast('這一軌是簡體，已改用繁體字幕');
        return loadSubtitle(next, { remember: false, auto: true, tried: [...tried, idx] });
      }
      toast(`找不到繁體字幕，已選：${s.label}`);
    } else if (cues.length) {
      // 完整抽完了 → 伺服器已經把 .vtt 寫進 SUB_DIR。標記起來，
      // 之後在這個頁面裡重新選同一軌就會走「立刻載入」而不是再等暖機。
      if (info.subtitles[idx]) info.subtitles[idx].cached = true;
      toast(`字幕：${s.label}`);
    } else {
      toast(`這一軌沒有可顯示的字幕：${s.label}`);
    }
  } catch (e) {
    if (gen !== subGen) return;              // 換軌造成的中斷不算錯誤
    reader?.cancel().catch(() => {});
    toast('字幕載入失敗');
    curSub = -1;
    $('#btnSubs').classList.remove('on');
  }
  if (gen !== subGen) return;
  busy('');
  renderCues(true);
}

function renderCues(force = false) {
  if (!cues.length) return;
  const t = v.currentTime - cfg.subDelay;
  let i = -1;
  for (let k = 0; k < cues.length; k++) {
    if (cues[k].start <= t && t <= cues[k].end) { i = k; break; }
    if (cues[k].start > t) break;
  }
  if (i === cueIdx && !force) return;
  cueIdx = i;
  const box = $('#subs');
  if (i < 0) { box.innerHTML = ''; return; }
  // 同一時間可能有多句重疊，一起顯示
  const now = cues.filter(c => c.start <= t && t <= c.end).slice(0, 3);
  box.innerHTML = now.map(c => `<div class="cue">${c.html}</div>`).join('');
}

/* ------------------------------------------------ 播放 ------------------------------------------------ */
function play(which, startAt) {
  mode = which;
  const t = startAt ?? (v.currentTime > 1 ? v.currentTime : (info.resume || 0));
  $('#modePill').textContent = which === 'direct' ? '直接串流' : '即時轉碼';
  $('#modePill').className = 'badge-mode ' + (which === 'direct' ? 'direct' : '');
  renderTags();

  teardownHls(hlsObj); hlsObj = null;
  curLevel = null;              // 上一次的階別不能留到這一次（會標錯畫質）
  v.removeAttribute('src'); v.load();
  center('緩衝中…');

  if (which === 'direct') {
    v.src = info.direct_url;
    v.addEventListener('loadedmetadata', () => { if (t > 1) v.currentTime = t; }, { once: true });
    v.play().catch(() => {});
    return;
  }

  const url = info.hls_url;
  if (!window.Hls || !Hls.isSupported()) {
    if (v.canPlayType('application/vnd.apple.mpegurl')) {
      v.src = url;
      v.addEventListener('loadedmetadata', () => { if (t > 1) v.currentTime = t; }, { once: true });
      v.play().catch(() => {});
    } else center('這個瀏覽器不支援 HLS 播放', false);
    return;
  }

  let netRetry = 0, mediaRetry = 0, retryTimer = null, dead = false;
  const kill = () => { dead = true; clearTimeout(retryTimer);
    teardownHls(hlsObj); hlsObj = null; };
  // 行動網路的緩衝要深一點：訊號在隧道、電梯、移動中會整段掉，
  // 緩衝越深越撐得過去。桌機在區網不需要，深緩衝反而是多轉一堆用不到的段。
  const mob = isMobile();
  hlsObj = new Hls({
    maxBufferLength: mob ? 60 : 40, maxMaxBufferLength: mob ? 120 : 90,
    // **這個 timeout 不能照行動網路的直覺改小。**下階是隨選轉碼的，
    // 一段第一次被要的時候 ffmpeg 可能真的要跑十幾秒（4K 片源更久）。
    // 改小的話會在「伺服器正常、只是還在轉」的時候誤判成網路失敗。
    fragLoadingTimeOut: 180000, manifestLoadingTimeOut: 60000, levelLoadingTimeOut: 60000,
    startPosition: t > 1 ? t : -1, appendErrorMaxRetry: 5,
    // ABR：讓它自己挑起始階（-1），但**第一次的頻寬估計要保守**。
    // hls.js 的預設是 5 Mbps —— 手機在 4G 邊緣開播會直接選到選不動的那一階，
    // 然後卡住等它自己降下來。給一個行動網路撐得住的值，寧可開播後往上爬。
    startLevel: -1,
    abrEwmaDefaultEstimate: mob ? 1_200_000 : 5_000_000,
    // 往上切保守、往下切果斷：往上切錯的代價是卡頓（要重新緩衝），
    // 往下切錯的代價只是這幾秒畫質差一點。兩者不對稱，參數就不該對稱。
    abrBandWidthUpFactor: mob ? 0.5 : 0.7,
    abrBandWidthFactor: 0.9,
  });
  hlsObj.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch(() => {}));
  hlsObj.on(Hls.Events.FRAG_LOADED, () => { netRetry = 0; mediaRetry = 0; });
  // 自動模式下畫質是浮動的，選單上只寫「自動」不夠 —— 要看得到現在實際在哪一階，
  // 不然使用者只會覺得「畫質怎麼忽好忽壞」而不知道是 ABR 在work。
  hlsObj.on(Hls.Events.LEVEL_SWITCHED, (_, d) => {
    const lv = hlsObj?.levels?.[d.level];
    if (!lv) return;
    curLevel = lv;
    renderTags();
  });
  hlsObj.on(Hls.Events.ERROR, (_, d) => {
    // destroy 之後 hls.js 仍可能送事件進來，這時候再去碰它就會炸
    if (dead || !hlsObj || !d.fatal) return;
    clearTimeout(retryTimer);
    if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
      if (++netRetry > 6) {
        center('取不到轉碼分段，請看伺服器主控台的 ffmpeg 錯誤訊息', false); kill(); return;
      }
      busy(`轉碼中（重試 ${netRetry}）`);
      retryTimer = setTimeout(() => {
        if (dead || !hlsObj) return;
        try { hlsObj.startLoad(); } catch {}
      }, Math.min(1000 * 2 ** (netRetry - 1), 15000));
    } else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) {
      if (++mediaRetry > 3) {
        center('瀏覽器無法解碼這個串流，請改用 Chrome / Edge 或下載觀看', false); kill(); return;
      }
      busy('修復中');
      try {
        if (mediaRetry === 1) hlsObj.recoverMediaError();
        else { hlsObj.swapAudioCodec(); hlsObj.recoverMediaError(); }
      } catch { kill(); }
    } else { center('播放失敗：' + (d.details || ''), false); kill(); }
  });
  hlsObj.loadSource(url);
  hlsObj.attachMedia(v);
}

// hls.js 的 destroy() 會把內部參考清成 null，但它自己排隊中的 callback 還會再跑一次，
// 於是主控台就噴 "Cannot read properties of null (reading 'trigger')"。
// 先 stopLoad + detachMedia 讓那些 callback 這一輪收乾淨，下一個 tick 再 destroy。
function teardownHls(h) {
  if (!h) return;
  try { h.stopLoad(); } catch {}
  try { h.detachMedia(); } catch {}
  setTimeout(() => { try { h.destroy(); } catch {} }, 0);
}

// 畫質與音軌都要重新跟伺服器要一次播放資訊，兩個參數得一起帶，
// 不然切畫質會把選好的音軌洗掉，反過來也一樣。
function playUrl({ height, audio }) {
  const q = new URLSearchParams();
  // 0 = 自動（依區網/遠端判定）、-1 = 原畫質不縮放、其餘 = 指定高度
  if (height > 0) q.set('h', height);
  else if (height === -1) q.set('h', 0);
  if (audio != null) q.set('a', audio);
  const qs = q.toString();
  return `/api/play/${fileId}${qs ? '?' + qs : ''}`;
}

async function switchAudio(index) {
  if (index === info.audio_index) return;
  const at = v.currentTime;
  busy('切換音軌');
  try {
    info = await (await fetch(playUrl({ height: cfg.quality, audio: index }))).json();
  } catch { busy(''); toast('切換音軌失敗'); return; }
  rememberAudio(index);
  // 直接播放是把原始檔整個丟給瀏覽器，選哪一軌由瀏覽器決定；只有轉碼選得了
  play('hls', at);
  const t = (info.audio_tracks || []).find(x => x.index === index);
  toast(`音軌：${t ? audioLabel(t, 0) : index}`);
  buildPanel();
}

// 切換之後的提示要跟面板上寫的一致：級別 ＋ 實際解析度。
function qualityToast(height) {
  const q = info?.quality || {};
  const src = q.source;
  if (height === 0) return '畫質：自動';
  if (height === -1) {
    return src ? `畫質：原畫質 ${src.label}（${src.width}×${src.height}）` : '畫質：原畫質';
  }
  const lv = (q.levels || []).find(l => l.h === height);
  return lv ? `畫質：${lv.label}（${lv.width}×${lv.height}）` : `畫質：${height}p`;
}


async function switchQuality(height) {
  cfg.quality = height; saveCfg();
  const at = v.currentTime;
  busy('切換畫質');
  try {
    info = await (await fetch(playUrl({ height, audio: info.audio_index }))).json();
  } catch { busy(''); toast('切換畫質失敗'); return; }
  play(info.mode, at);
  toast(qualityToast(height));
  buildPanel();
}

/* ------------------------------------------------ 控制列 ------------------------------------------------ */
const ICON_PLAY = '<path d="M7 4l13 8-13 8z"/>';
const ICON_PAUSE = '<path d="M6 4h4v16H6zM14 4h4v16h-4z"/>';

function syncPlayBtn() { $('#icPlay').innerHTML = v.paused ? ICON_PLAY : ICON_PAUSE; }
const togglePlay = () => {
  if (v.paused) { userPaused = false; v.play().catch(() => {}); }
  else { userPaused = true; v.pause(); }
};

$('#btnPlay').onclick = togglePlay;
// 觸控裝置：控制列藏起來時，第一下只負責叫醒控制列，不要誤按成暫停
$('#tap').addEventListener('pointerup', e => {
  if (e.pointerType === 'touch' && pl.classList.contains('idle')) { wake(); return; }
  if (e.pointerType !== 'touch') return;   // 滑鼠交給 click 處理，避免觸發兩次
  togglePlay();
});
$('#tap').onclick = e => { if (e.pointerType === 'touch') return; togglePlay(); };
$('#tap').ondblclick = () => toggleFull();
$('#btnBack').onclick = () => { v.currentTime -= cfg.skip; toast(`◀ ${cfg.skip} 秒`); };
$('#btnFwd').onclick = () => { v.currentTime += cfg.skip; toast(`${cfg.skip} 秒 ▶`); };
function syncSkipLabels() {
  $('#skipBack').textContent = cfg.skip;
  $('#skipFwd').textContent = cfg.skip;
  $('#btnBack').title = `倒退 ${cfg.skip} 秒 (←)`;
  $('#btnFwd').title = `快轉 ${cfg.skip} 秒 (→)`;
}

$('#vol').oninput = e => { v.volume = +e.target.value; v.muted = false; cfg.volume = v.volume; cfg.muted = false; saveCfg(); };
$('#btnMute').onclick = () => { v.muted = !v.muted; cfg.muted = v.muted; saveCfg(); syncVol(); };
function syncVol() {
  $('#vol').value = v.muted ? 0 : v.volume;
  $('#icVol').innerHTML = v.muted || !v.volume
    ? '<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M17 9l4 6M21 9l-4 6"/>'
    : '<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16.5 8.5a5 5 0 010 7"/>';
}

const barWrap = $('#barWrap');
const posOf = e => {
  const r = barWrap.getBoundingClientRect();
  return clamp(((e.touches ? e.touches[0].clientX : e.clientX) - r.left) / r.width, 0, 1);
};
let dragging = false;
barWrap.addEventListener('pointerdown', e => {
  dragging = true; barWrap.setPointerCapture(e.pointerId);
  if (v.duration) v.currentTime = posOf(e) * v.duration;
});
barWrap.addEventListener('pointermove', e => {
  const p = posOf(e);
  const tip = $('#barTip');
  tip.style.left = (p * 100) + '%';
  tip.textContent = fmt(p * (v.duration || 0));
  if (dragging && v.duration) v.currentTime = p * v.duration;
});
barWrap.addEventListener('pointerup', e => { dragging = false; try { barWrap.releasePointerCapture(e.pointerId); } catch {} });

function syncBar() {
  const d = v.duration || info?.duration || 0;
  const p = d ? (v.currentTime / d) * 100 : 0;
  $('#barPlay').style.width = p + '%';
  $('#barKnob').style.left = p + '%';
  $('#tCur').textContent = fmt(v.currentTime);
  $('#tDur').textContent = fmt(d);
  if (v.buffered.length && d) {
    let end = 0;
    for (let i = 0; i < v.buffered.length; i++)
      if (v.buffered.start(i) <= v.currentTime + 0.5) end = Math.max(end, v.buffered.end(i));
    $('#barBuf').style.width = clamp((end / d) * 100, 0, 100) + '%';
  }
}

/* ---------------- 全螢幕 ----------------
   iOS Safari 上 div.requestFullscreen 這個方法**根本不存在** —— 只有
   <video> 能進原生全螢幕（webkitEnterFullscreen）。原本寫成
   pl.requestFullscreen?.() ，那個 ?. 讓它安靜地什麼都不做，
   所以 iPhone 上按全螢幕毫無反應、連錯誤都不會噴。

   三種模式：
     auto    桌機／Android 用 Fullscreen API，iPhone 用網頁全螢幕
     page    網頁全螢幕：CSS 撐滿，保留自訂控制列與自繪字幕
     native  原生全螢幕：畫面乾淨，但字幕改交給系統渲染
*/
const canElementFullscreen = !!(pl.requestFullscreen || pl.webkitRequestFullscreen);
const canVideoNative = typeof v.webkitEnterFullscreen === 'function';

function fsMode() {
  const m = cfg.fullscreen || 'auto';
  // 使用者選了這個瀏覽器做不到的模式時，要回報**實際會發生的事**。
  // 直接把選項原樣回傳的話，設定面板會顯示「原生」是啟用中的，
  // 但按下去其實走的是網頁全螢幕 —— 介面在說謊。
  if (m === 'native') return canVideoNative ? 'native' : 'page';
  if (m === 'page') return 'page';
  if (m === 'element') return canElementFullscreen ? 'element' : 'page';
  return canElementFullscreen ? 'element' : (canVideoNative ? 'native' : 'page');
}

function inPageFullscreen() { return document.body.classList.contains('page-fs'); }

function setPageFullscreen(on) {
  document.body.classList.toggle('page-fs', on);
  $('#btnFull').classList.toggle('on', on);
  // 鎖住背景捲動，不然 iOS 上兩指一滑整頁會跑掉
  document.documentElement.style.overflow = on ? 'hidden' : '';
  if (on) lockLandscape();
}

function lockLandscape() {
  // iOS Safari 不支援 screen.orientation.lock，而且它會 reject。
  // 失敗只是「沒轉成橫的」，不該讓全螢幕整個中止。
  try { screen.orientation?.lock?.('landscape').catch(() => {}); } catch {}
}

function toggleFull() {
  const mode = fsMode();
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen)?.call(document);
    return;
  }
  if (inPageFullscreen()) { setPageFullscreen(false); return; }

  if (mode === 'element' && canElementFullscreen) {
    const req = pl.requestFullscreen || pl.webkitRequestFullscreen;
    Promise.resolve(req.call(pl)).then(lockLandscape).catch(() => setPageFullscreen(true));
  } else if (mode === 'native' && canVideoNative) {
    enterNativeFullscreen();
  } else {
    setPageFullscreen(true);
  }
}

function enterNativeFullscreen() {
  // 進系統播放器之前把字幕交給原生軌，不然全螢幕下就完全沒有字幕了
  showNativeTrack(true);
  try {
    v.webkitEnterFullscreen();
  } catch {
    showNativeTrack(false);
    setPageFullscreen(true);
  }
}

v.addEventListener('webkitendfullscreen', () => {
  // 使用者從系統播放器退出 —— 把字幕交還給自繪那一層
  showNativeTrack(false);
  renderCues(true);
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && inPageFullscreen()) setPageFullscreen(false);
});
$('#btnFull').onclick = toggleFull;
$('#btnPip').onclick = () => {
  if (document.pictureInPictureElement) document.exitPictureInPicture();
  else v.requestPictureInPicture?.().catch(() => toast('這個瀏覽器不支援子母畫面'));
};

/* 閒置隱藏 */
let idleTimer, pointerOverCtl = false;
function wake() {
  pl.classList.remove('idle');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    // 只要不是「使用者主動暫停」或滑鼠停在控制列上，就收起來。
    // 之前的條件是 !v.paused，結果緩衝/轉碼中 video 是 waiting 狀態，
    // 控制列那條漸層就一直蓋在畫面下緣不走。
    if (userPaused || pointerOverCtl || $('#panel').classList.contains('show')) return;
    pl.classList.add('idle');
  }, 2800);
}
let userPaused = false;
$('#ctl').addEventListener('pointerenter', () => { pointerOverCtl = true; });
$('#ctl').addEventListener('pointerleave', () => { pointerOverCtl = false; wake(); });
['pointermove', 'pointerdown', 'keydown', 'wheel'].forEach(ev => document.addEventListener(ev, wake, { passive: true }));

/* ------------------------------------------------ 設定面板 ------------------------------------------------ */
let panelTab = 'subtitle';
const COLORS = ['#ffffff', '#ffe680', '#ffd54a', '#8ef7c5', '#8ecbff', '#ff9ec4', '#000000'];

const CH_NAME = { 1: '單聲道', 2: '立體聲', 6: '5.1', 8: '7.1' };
// 常見的語言代碼對照。查不到就直接顯示原本的代碼，不要硬翻。
const LANG_NAME = {
  chi: '中文', zho: '中文', cmn: '中文', yue: '粵語',
  eng: '英文', jpn: '日文', kor: '韓文', spa: '西班牙文', fre: '法文', fra: '法文',
  ger: '德文', deu: '德文', ita: '義大利文', rus: '俄文', por: '葡萄牙文',
  tha: '泰文', vie: '越南文', ind: '印尼文', und: '未標示',
};
const langName = c => LANG_NAME[String(c || '').toLowerCase()] || c || '';

function audioLabel(t, i) {
  const bits = [];
  if (t.title) bits.push(t.title);
  const ln = langName(t.lang);
  if (ln && ln !== t.title) bits.push(ln);
  return bits.join(' · ') || ('音軌 ' + (i + 1));
}

function audioDetail(t) {
  const bits = [];
  if (t.codec) bits.push(t.codec.toUpperCase());
  const ch = CH_NAME[t.channels] || (t.channels ? t.channels + ' 聲道' : '');
  if (ch) bits.push(ch);
  if (t.bitrate) bits.push(Math.round(t.bitrate / 1000) + ' kbps');
  return bits.join(' / ');
}

// 轉碼會把聲道降混。標示上寫著 5.1 但實際聽到立體聲的話，那不是壞掉，
// 是設定如此 —— 講清楚，不要讓標籤說謊。
function downmixNote(t) {
  const out = info?.audio_channels_out;
  if (!out || mode === 'direct') return '';
  if (t && t.channels > out) return `（轉碼後降混成 ${CH_NAME[out] || out + ' 聲道'}）`;
  return '';
}

const fmtSize = b => !b ? '未知'
  : b >= 1073741824 ? (b / 1073741824).toFixed(2) + ' GB' : (b / 1048576).toFixed(0) + ' MB';
const fmtRate = kbps => !kbps ? '' : kbps >= 1000
  ? (kbps / 1000).toFixed(1) + ' Mbps' : Math.round(kbps) + ' kbps';

// 面板底部的片源資訊。這是「這個檔案本身長什麼樣」，
// 跟上面的畫質選項（要轉成什麼樣）是兩回事，所以分開列。
function mediaInfoRows() {
  if (!info) return [];
  const rows = [];
  // 級別放前面、實際解析度放後面：「1080p · 1920 × 804」一眼就看得懂
  // 「這是 1080p 的片，畫面比較扁」，而不是誤以為畫質被降到 804。
  const res = info.width && info.height ? `${info.width} × ${info.height}` : '未知';
  const cls = info?.quality?.source?.label;
  rows.push(['解析度', (cls ? cls + ' · ' : '') + res + (info.fps ? ` · ${info.fps} fps` : '')]);

  const vbits = [(info.video_codec || '').toUpperCase()];
  if (info.bit_depth) vbits.push(info.bit_depth + '-bit');
  if (info.is_hdr) vbits.push('HDR');
  else if (info.color_transfer && info.color_transfer !== 'bt709') vbits.push(info.color_transfer);
  rows.push(['影像', vbits.filter(Boolean).join(' · ') || '未知']);

  const cur = (info.audio_tracks || []).find(t => t.index === info.audio_index)
           || (info.audio_tracks || [])[0];
  if (cur) {
    const d = audioDetail(cur), ln = langName(cur.lang);
    rows.push(['音訊', [d, ln].filter(Boolean).join(' · ')
      + ((info.audio_tracks || []).length > 1 ? `（共 ${info.audio_tracks.length} 軌）` : '')
      + downmixNote(cur)]);
  }

  const subs = info.subtitles || [];
  if (subs.length) {
    const g = subs.filter(x => x.graphic).length;
    rows.push(['字幕', `${subs.length} 軌` + (g ? `（其中 ${g} 軌是圖形字幕，無法顯示）` : '')]);
  }

  rows.push(['容器', [(info.container || info.ext || '').toUpperCase(),
                      fmtSize(info.size)].filter(Boolean).join(' · ')]);
  const overall = info.bitrate ? fmtRate(info.bitrate / 1000) : '';
  if (overall) rows.push(['整體位元率', overall]);

  // 現在正在送給瀏覽器的東西，跟原始檔可能不一樣
  const q = info.quality || {};
  rows.push(['目前播放', mode === 'direct' ? '直送原始檔，未轉碼'
    : `轉碼 ${q.height || '?'}p` + (q.bitrate_kbps ? ` · 上限 ${fmtRate(q.bitrate_kbps)}` : ' · 位元率不限')]);
  if (info.filename) rows.push(['檔名', info.filename]);
  return rows;
}

function renderTags() {
  const box = $('#tags');
  if (!box || !info) return;
  const t = [];
  if (info.height) t.push(info.height >= 2000 ? '4K' : info.height + 'p');
  if (info.is_hdr) {
    const m = info.hdr_mode;
    t.push(!info.tonemap_available
      ? `<span class="tg warn" title="這個 ffmpeg 沒有 zscale/tonemap 濾鏡，HDR 會發灰">HDR 未處理</span>`
      : m === 'off' ? `<span class="tg warn">HDR 未轉換</span>`
      : `<span class="tg hdr" title="${m === 'fast' ? '快速色域轉換：色彩正確，高光會削掉' : '標準 tonemap：畫質最好'}">HDR → SDR${m === 'fast' ? '（快速）' : ''}</span>`);
  }
  if (info.bit_depth && info.bit_depth > 8) t.push(info.bit_depth + '-bit');
  // 自動模式下 ABR 會自己上下切，標出「現在實際在哪一階」。只在自動、
  // 而且真的有第二階可切的時候顯示 —— 使用者鎖死某一階時它是固定的，
  // 再標一次只是雜訊。
  if (mode === 'hls' && cfg.quality === 0 && curLevel && (hlsObj?.levels?.length || 0) > 1) {
    const lab = curLevel.height ? `${curLevel.height}p` : '';
    if (lab) t.push(`<span class="tg" title="自動調節：依目前網路速度選的畫質">自動 · ${esc(lab)}</span>`);
  }
  box.innerHTML = t.map(x => x.startsWith('<') ? x : `<span class="tg">${esc(x)}</span>`).join('');
}

let showOtherSubs = null;

function buildPanel() {
  const p = $('#panel');
  const subs = info?.subtitles || [];

  const mainSubs = [], otherSubs = [];
  subs.forEach((t, i) => (trackGroup(t) === 'main' ? mainSubs : otherSubs).push(i));
  // 還沒手動展開／收合過的話：沒有繁中英文可選、或目前正在放的就是「其他」裡的軌，就先攤開
  const expandOther = showOtherSubs !== null ? showOtherSubs
    : (mainSubs.length === 0 || otherSubs.includes(curSub));
  const subRow = (i, nested = false) => {
    const s = subs[i];
    const tail = s.graphic ? `圖形字幕${s.codec ? '（' + esc(s.codec.toUpperCase()) + '）' : ''}・無法顯示`
               : (s.kind === 'external' ? '外掛' : '內嵌') + (s.forced ? '・強制' : '');
    return `<div class="opt ${nested ? 'nested ' : ''}${s.graphic ? 'dim ' : ''}${curSub===i?'on':''}" data-sub="${i}">
      <span class="tick">${curSub===i?'✓':''}</span>${esc(s.label || ('字幕 ' + (i+1)))}
      <small>${tail}</small></div>`;
  };
  const tracks = info?.audio_tracks || [];
  const ladder = info?.quality?.ladder || [];
  // 級別（1080p）與實際解析度（1920×804）兩個都要顯示。只給高度的話，
  // 寬螢幕片會被標成「804p」看起來像降級 —— 那其實就是原檔的畫面高度。
  const levels = info?.quality?.levels || ladder.map(h => ({ h, label: h + 'p' }));
  const srcQ = info?.quality?.source;
  const dim = q => q && q.width ? `${q.width}×${q.height}` : '';
  const srcLabel = srcQ ? `原畫質 ${srcQ.label}` : '原畫質';

  const seg = (id, opts, cur) => `<div class="seg" data-seg="${id}">` +
    opts.map(([val, lab]) => `<button data-v="${val}" class="${String(cur)===String(val)?'on':''}">${lab}</button>`).join('') + '</div>';
  const slider = (id, label, min, max, step, val, unit='') =>
    `<div class="pi"><label>${label}</label>
       <input type="range" data-slider="${id}" min="${min}" max="${max}" step="${step}" value="${val}">
       <span class="val" data-val="${id}">${val}${unit}</span></div>`;

  p.innerHTML = `
    <h4>字幕</h4>
    <div class="opt ${curSub < 0 ? 'on' : ''}" data-sub="-1"><span class="tick">${curSub<0?'✓':''}</span>關閉</div>
    ${subs.length ? `<div class="opt ${savedPick()===undefined?'on':''}" data-sub="auto">
      <span class="tick">${savedPick()===undefined?'✓':''}</span>
      自動選繁體中文 <small>${savedPick()!==undefined?'這部片已手動指定':'依語言標籤與內容判斷'}</small></div>` : ''}
    ${mainSubs.map(i => subRow(i)).join('')}
    ${otherSubs.length ? `<div class="opt more" data-moresubs="1">
        <span class="tick">${expandOther ? '▾' : '▸'}</span>其他字幕
        <small>${otherSubs.length} 軌${expandOther ? '' : '（含簡體、其他語言）'}</small></div>
      ${expandOther ? otherSubs.map(i => subRow(i, true)).join('') : ''}` : ''}
    ${subs.length ? '' : '<div class="opt" style="color:var(--dim);cursor:default">這個檔案沒有字幕軌</div>'}

    <h4>字幕外觀</h4>
    ${slider('subSize', '大小', 14, 80, 1, cfg.subSize, 'px')}
    <div class="pi"><label>顏色</label></div>
    <div class="swatches">${COLORS.map(c => `<div class="sw ${cfg.subColor===c?'on':''}" data-color="${c}" style="background:${c}"></div>`).join('')}</div>
    <div class="pi"><label>位置</label></div>
    ${seg('subPos', [['bottom','靠下'],['top','靠上']], cfg.subPos)}
    ${slider('subOffset', '距離邊緣', 0, 45, 1, cfg.subOffset, '%')}
    <div class="pi"><label>背景</label></div>
    ${seg('subBg', [['none','無'],['dim','半透明'],['solid','黑底']], cfg.subBg)}
    <div class="pi"><label>邊緣</label></div>
    ${seg('subEdge', [['none','無'],['shadow','陰影'],['outline','描邊'],['heavy','粗描邊']], cfg.subEdge)}
    <div class="pi"><label>字體</label></div>
    ${seg('subFont', [['sans','黑體'],['serif','明體'],['mono','等寬']], cfg.subFont)}
    ${seg('subWeight', [['400','細'],['600','中'],['800','粗']], cfg.subWeight)}
    ${slider('subDelay', '字幕延遲', -10, 10, 0.1, cfg.subDelay, 's')}

    <h4>播放</h4>
    <div class="pi"><label>快轉／倒轉秒數</label></div>
    ${seg('skip', [['3','3 秒'],['5','5 秒'],['10','10 秒'],['30','30 秒']], cfg.skip)}
    <div class="pi"><label>全螢幕方式</label></div>
    ${seg('fullscreen', [['auto','自動'],['page','網頁'],['native','原生']], cfg.fullscreen || 'auto')}
    <div class="hint">原生全螢幕畫面比較乾淨，但字幕會改由系統顯示，
      上面那些字幕外觀設定在原生模式下不會生效。iPhone 只有這兩種可選。</div>
    <div class="pi"><label>速度</label></div>
    ${seg('rate', [['0.75','0.75x'],['1','1x'],['1.25','1.25x'],['1.5','1.5x'],['2','2x']], cfg.rate)}
    ${ladder.length ? `<div class="pi"><label>畫質${info.quality.auto_reason ? ' <small style="color:var(--dim)">'+esc(info.quality.auto_reason)+'</small>' : ''}</label></div>
      ${srcQ ? `<div class="pi"><small style="color:var(--dim)">片源 ${esc(srcQ.label)}・${esc(dim(srcQ))}</small></div>` : ''}
      ${seg('quality', [['0','自動'], ['-1', srcLabel],
                        ...levels.map(l => [String(l.h), l.label + (dim(l) ? ` <small>${dim(l)}</small>` : '')])], cfg.quality)}
      ${info.is_hdr ? `<div class="pi"><label style="font-size:12px;color:var(--dim)">
        HDR 片源：${info.tonemap_available ? (info.hdr_mode === 'fast'
          ? '快速轉換中（色彩正確，高光削掉）。要最佳畫質可在 .env 設 HDR_TONEMAP=quality，但 4K 可能會卡'
          : '標準 tonemap') : '這個 ffmpeg 缺 zscale/tonemap 濾鏡，畫面會發灰'}</label></div>` : ''}` : ''}
    ${tracks.length > 1 ? `<h4>音軌</h4>` + tracks.map((t, i) =>
      `<div class="opt ${t.index === info.audio_index ? 'on' : ''}" data-audio="${t.index}">
        <span class="tick">${t.index === info.audio_index ? '✓' : ''}</span>${esc(audioLabel(t, i))}
        <small>${esc(audioDetail(t))}${esc(downmixNote(t))}</small></div>`).join('')
      + (mode === 'direct' ? `<div class="pi"><label style="font-size:12px;color:var(--dim)">
          直接播放時由瀏覽器決定音軌，切換會自動改用轉碼播放</label></div>` : '') : ''}
    ${info?.mode ? `<h4>來源</h4>
      <div class="opt" data-switchmode="1"><span class="tick"></span>
        ${mode === 'direct' ? '改用轉碼播放' : '試試直接播放'}
        <small>${mode === 'direct' ? '目前直送原始檔' : '目前即時轉碼'}</small></div>
      ${canDownload ? `<div class="opt" onclick="location.href='${info.download_url}'">
        <span class="tick"></span>下載原始檔 <small>${fmtSize(info.size)}</small></div>` : ''}` : ''}

    <h4>片源資訊</h4>
    <div class="info">${mediaInfoRows().map(([k, val]) =>
      `<div class="ir"><span>${esc(k)}</span><b>${esc(val)}</b></div>`).join('')}</div>
    <div class="foot"><button data-reset="1">恢復字幕預設值</button></div>`;

  p.querySelectorAll('[data-sub]').forEach(el => el.onclick = () => {
    if (el.dataset.sub === 'auto') {
      forgetPick();
      const best = pickAutoTrack();
      best >= 0 ? loadSubtitle(best, { remember: false, auto: true })
                : toast('這個檔案沒有中文字幕');
    } else loadSubtitle(+el.dataset.sub);
    buildPanel();
  });
  p.querySelector('[data-moresubs]')?.addEventListener('click', () => {
    showOtherSubs = !expandOther; buildPanel();
  });
  p.querySelectorAll('[data-color]').forEach(el => el.onclick = () => {
    cfg.subColor = el.dataset.color; saveCfg(); applySubStyle(); buildPanel(); });
  p.querySelectorAll('[data-slider]').forEach(el => el.oninput = () => {
    const k = el.dataset.slider, val = parseFloat(el.value);
    cfg[k] = val; saveCfg();
    const unit = k === 'subSize' ? 'px' : k === 'subOffset' ? '%' : k === 'subDelay' ? 's' : '';
    p.querySelector(`[data-val="${k}"]`).textContent = (k === 'subDelay' ? val.toFixed(1) : val) + unit;
    applySubStyle();
    if (k === 'subDelay') renderCues(true);
  });
  p.querySelectorAll('[data-seg]').forEach(box => box.querySelectorAll('button').forEach(b => b.onclick = () => {
    const key = box.dataset.seg, val = b.dataset.v;
    if (key === 'rate') { cfg.rate = parseFloat(val); v.playbackRate = cfg.rate; }
    else if (key === 'skip') { cfg.skip = parseInt(val); syncSkipLabels(); }
    else if (key === 'quality') { switchQuality(parseInt(val)); return; }
    else if (key === 'subWeight') cfg.subWeight = parseInt(val);
    else cfg[key] = val;
    saveCfg(); applySubStyle(); buildPanel();
  }));
  p.querySelectorAll('[data-audio]').forEach(el => el.onclick = () => {
    switchAudio(+el.dataset.audio);
  });
  p.querySelector('[data-switchmode]')?.addEventListener('click', () => {
    play(mode === 'direct' ? 'hls' : 'direct'); $('#panel').classList.remove('show'); });
  p.querySelector('[data-reset]').onclick = () => {
    Object.assign(cfg, { subSize: DEFAULTS.subSize, subColor: DEFAULTS.subColor, subBg: DEFAULTS.subBg,
      subEdge: DEFAULTS.subEdge, subPos: DEFAULTS.subPos, subOffset: DEFAULTS.subOffset,
      subFont: DEFAULTS.subFont, subWeight: DEFAULTS.subWeight, subDelay: DEFAULTS.subDelay });
    saveCfg(); applySubStyle(); renderCues(true); buildPanel(); toast('已恢復字幕預設值');
  };
}

function togglePanel(scrollTo) {
  const p = $('#panel');
  const show = !p.classList.contains('show');
  p.classList.toggle('show', show);
  if (show) { buildPanel(); if (scrollTo) p.scrollTop = 0; wake(); }
}
$('#btnSettings').onclick = () => togglePanel();
$('#btnSubs').onclick = () => {
  // 有字幕就直接切換開/關，長按或沒字幕時開面板
  if (!info?.subtitles?.length) { togglePanel(); return; }
  if (curSub >= 0) { loadSubtitle(-1); }          // 關掉要記住，下次開這部片就別自己跳出來
  else {
    const saved = savedPick();
    if (saved >= 0) loadSubtitle(saved, { remember: false });
    else {
      forgetPick();                                // 清掉「這部片關字幕」，不然重整又變關的
      const best = pickAutoTrack();
      loadSubtitle(best >= 0 ? best : 0, { remember: false, auto: best >= 0 });
    }
  }
  buildPanel();
};
document.addEventListener('pointerdown', e => {
  const p = $('#panel');
  if (p.classList.contains('show') && !p.contains(e.target) &&
      !$('#btnSettings').contains(e.target) && !$('#btnSubs').contains(e.target))
    p.classList.remove('show');
});

/* ------------------------------------------------ 事件 ------------------------------------------------ */
v.addEventListener('play', syncPlayBtn);
v.addEventListener('pause', () => { syncPlayBtn(); wake(); });
v.addEventListener('play', () => { userPaused = false; });
v.addEventListener('playing', () => { center(''); busy(''); });
// 卡住的原因有兩種，講錯會把人引導到錯的方向：伺服器還在轉碼（等一下就好），
// 還是網路餵不動（那要等 ABR 降階）。已經在最低階還在等 = 網路問題。
v.addEventListener('waiting', () => {
  if (mode !== 'hls') return busy('緩衝中');
  // **不能用 `currentLevel === 0` 判斷「在最低階」** —— level 的索引順序
  // 由 hls.js 內部決定，不保證跟 master 裡的順序或碼率高低一致。
  // 直接比碼率，才不會因為換一版 hls.js 就標反。
  const lv = hlsObj?.levels || [];
  const cur = lv[hlsObj?.currentLevel];
  const lowest = lv.length > 1 && cur
    && cur.bitrate <= Math.min(...lv.map(x => x.bitrate));
  busy(lowest ? '網路較慢，已降到最低畫質' : '轉碼中');
});
v.addEventListener('canplay', () => { center(''); busy(''); });
v.addEventListener('volumechange', syncVol);
v.addEventListener('timeupdate', () => {
  syncBar(); renderCues();
  clearTimeout(saveTimer); saveTimer = setTimeout(saveProgress, 5000);
});
v.addEventListener('progress', syncBar);
v.addEventListener('seeking', () => renderCues(true));
v.addEventListener('loadedmetadata', () => { v.playbackRate = cfg.rate; syncBar(); });

let fellBack = false;
v.addEventListener('error', () => {
  if (mode === 'direct' && !fellBack) { fellBack = true; toast('直接播放失敗，改用轉碼'); play('hls'); }
  else if (mode === 'direct') center('這個檔案無法在瀏覽器播放，請下載後用本機播放器開啟', false);
});

function saveProgress(finished = false) {
  if (!info || !v.currentTime) return;
  const body = JSON.stringify({ file_id: +fileId, position: v.currentTime,
    duration: v.duration || info.duration || 0, finished });
  navigator.sendBeacon?.('/api/progress', new Blob([body], { type: 'application/json' }));
}
v.addEventListener('ended', () => saveProgress(true));
window.addEventListener('beforeunload', () => saveProgress());

document.addEventListener('keydown', e => {
  if (['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)) return;
  const k = e.key.toLowerCase();
  if (k === ' ' || k === 'k') { e.preventDefault(); togglePlay(); }
  else if (e.key === 'ArrowRight') { const n = e.shiftKey ? 60 : cfg.skip; v.currentTime += n; toast(n + ' 秒 ▶'); }
  else if (e.key === 'ArrowLeft') { const n = e.shiftKey ? 60 : cfg.skip; v.currentTime -= n; toast('◀ ' + n + ' 秒'); }
  else if (e.key === 'ArrowUp') { v.volume = clamp(v.volume + .1, 0, 1); toast('音量 ' + Math.round(v.volume*100) + '%'); }
  else if (e.key === 'ArrowDown') { v.volume = clamp(v.volume - .1, 0, 1); toast('音量 ' + Math.round(v.volume*100) + '%'); }
  else if (k === 'f') toggleFull();
  else if (k === 'm') { v.muted = !v.muted; }
  else if (k === 'c') $('#btnSubs').click();
  else if (k === 'escape') $('#panel').classList.remove('show');
  else if (k === 'g') { cfg.subDelay = +(cfg.subDelay - 0.1).toFixed(1); saveCfg(); renderCues(true); toast('字幕延遲 ' + cfg.subDelay.toFixed(1) + 's'); }
  else if (k === 'h') { cfg.subDelay = +(cfg.subDelay + 0.1).toFixed(1); saveCfg(); renderCues(true); toast('字幕延遲 ' + cfg.subDelay.toFixed(1) + 's'); }
});

/* ------------------------------------------------ 啟動 ------------------------------------------------ */
async function boot() {
  try {
    const who = await (await fetch('/api/me')).json();
    canDownload = !!who.is_admin;
  } catch {}
  await pullPrefs();
  applySubStyle();
  syncSkipLabels();
  v.volume = cfg.volume; v.muted = cfg.muted; syncVol(); syncPlayBtn();
  if (!fileId) return center('缺少 file 參數', false);

  center('讀取影片資訊…');
  try {
    const q = cfg.quality > 0 ? `?h=${cfg.quality}` : (cfg.quality === -1 ? '?h=0' : '');
    const r = await fetch(`/api/play/${fileId}${q}`);
    if (r.status === 401) { location.href = '/login'; return; }
    info = await r.json();
  } catch (e) { return center('讀取失敗：' + e.message, false); }
  if (info.detail) return center(info.detail, false);

  document.title = info.title + ' — FilmaxWeb';
  $('#ti').textContent = info.title;
  $('#sub').textContent = [info.subtitle_label, info.filename].filter(Boolean).join(' · ');

  if (info.probe_state !== 'ok') {
    center('這個檔案還沒分析過格式，正在分析…');
    try {
      const r = await (await fetch('/api/probe/' + fileId, { method: 'POST' })).json();
      if (r.probe_state !== 'ok') return center('分析失敗：' + (r.probe_error || '未知錯誤'), false);
      info = await (await fetch(`/api/play/${fileId}`)).json();
    } catch (e) { return center('分析失敗：' + e.message, false); }
  }

  if (info.quality?.auto_reason) toast(info.quality.auto_reason);

  // 這部片選過音軌就照舊。要重新跟伺服器要一次，轉碼的 profile 才會帶上那一軌。
  // 只有真的跟預設不同才需要處理 —— 選的就是預設那軌的話，直接播放依然可用。
  const wantAudio = savedAudio();
  let forceHls = false;
  if (wantAudio != null && wantAudio !== info.audio_index &&
      (info.audio_tracks || []).some(t => t.index === wantAudio)) {
    try {
      info = await (await fetch(playUrl({ height: cfg.quality, audio: wantAudio }))).json();
      forceHls = true;               // 指定音軌只有轉碼做得到
    } catch {}
  }

  buildPanel();
  play(forceHls ? 'hls' : info.mode);
  const saved = savedPick();
  if (saved !== undefined && (saved < 0 || info.subtitles[saved])) {
    // 這部片手動選過（含刻意關掉）—— 照舊，不要自作聰明
    if (saved >= 0) startSubtitles(saved, { remember: false });
    else { $('#btnSubs').classList.remove('on'); prefetchSubtitles(); }
  } else {
    const best = pickAutoTrack();
    if (best >= 0) startSubtitles(best, { remember: false, auto: true });
    else $('#btnSubs').classList.remove('on');
  }
  wake();
}
boot();
