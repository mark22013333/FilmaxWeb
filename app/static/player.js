/* FilmaxWeb 自製播放器
   不用瀏覽器原生控制列，字幕自己渲染 —— 才能完整控制大小 / 顏色 / 位置 / 延遲。 */
const $ = s => document.querySelector(s);
const params = new URLSearchParams(location.search);
const fileId = params.get('file');
const v = $('#v'), pl = $('#pl');

let info = null, hlsObj = null, mode = null, saveTimer = null, seekingByUs = false;

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

// remember：把這次選擇記在這個檔案上（使用者自己點的才要）
// auto    ：這一軌是程式挑的，所以載入後可以看內容決定要不要改挑別軌。
//           使用者指定的軌永遠不會被換掉，即使內容是簡體 —— 那是他要的。
async function loadSubtitle(idx, opts = {}) {
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
      for (const b of parts) { const c = parseCueBlock(b); if (c) cues.push(c); }
    };

    // 語言標籤分不出繁簡時看實際內容。收到夠多句就判斷，不必等整份下載完。
    let checked = !auto, simplified = false;
    const check = done => {
      if (checked || (cues.length < 20 && !done)) return;
      checked = true;
      if (cues.length) simplified = hantScore(cues.slice(0, 60).map(c => c.html).join('')) < -0.3;
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
        // 判斷完才畫，免得簡體字幕先閃一下才換掉
        if (checked && cues.length) renderCues(true);
        busy(done ? '' : `載入字幕 ${cues.length} 句`);
        if (done) break;
      }
    }

    if (simplified) {
      reader?.cancel().catch(() => {});
      const next = pickAutoTrack([...tried, idx]);
      if (next >= 0) {
        busy('');
        return loadSubtitle(next, { remember: false, auto: true, tried: [...tried, idx] });
      }
      toast(`找不到繁體字幕，已選：${s.label}`);
    } else if (cues.length) {
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
  hlsObj = new Hls({
    maxBufferLength: 40, maxMaxBufferLength: 90,
    fragLoadingTimeOut: 180000, manifestLoadingTimeOut: 60000, levelLoadingTimeOut: 60000,
    startPosition: t > 1 ? t : -1, appendErrorMaxRetry: 5,
  });
  hlsObj.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch(() => {}));
  hlsObj.on(Hls.Events.FRAG_LOADED, () => { netRetry = 0; mediaRetry = 0; });
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

async function switchQuality(height) {
  cfg.quality = height; saveCfg();
  const at = v.currentTime;
  busy('切換畫質');
  try {
    info = await (await fetch(playUrl({ height, audio: info.audio_index }))).json();
  } catch { busy(''); toast('切換畫質失敗'); return; }
  play(info.mode, at);
  toast(height > 0 ? `畫質：${height}p` : height === -1 ? '畫質：原畫質' : '畫質：自動');
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

function toggleFull() {
  if (document.fullscreenElement) document.exitFullscreen();
  else pl.requestFullscreen?.().catch(() => {});
}
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
  const res = info.width && info.height ? `${info.width} × ${info.height}` : '未知';
  rows.push(['解析度', res + (info.fps ? ` · ${info.fps} fps` : '')]);

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
    <div class="pi"><label>速度</label></div>
    ${seg('rate', [['0.75','0.75x'],['1','1x'],['1.25','1.25x'],['1.5','1.5x'],['2','2x']], cfg.rate)}
    ${ladder.length ? `<div class="pi"><label>畫質${info.quality.auto_reason ? ' <small style="color:var(--dim)">'+esc(info.quality.auto_reason)+'</small>' : ''}</label></div>
      ${seg('quality', [['0','自動'], ['-1','原畫質'], ...ladder.map(h => [String(h), h + 'p'])], cfg.quality)}
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
v.addEventListener('waiting', () => busy(mode === 'hls' ? '轉碼中' : '緩衝中'));
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
    if (saved >= 0) loadSubtitle(saved, { remember: false });
    else $('#btnSubs').classList.remove('on');
  } else {
    const best = pickAutoTrack();
    if (best >= 0) loadSubtitle(best, { remember: false, auto: true });
    else $('#btnSubs').classList.remove('on');
  }
  wake();
}
boot();
