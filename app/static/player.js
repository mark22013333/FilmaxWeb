/* FilmaxWeb 自製播放器
   不用瀏覽器原生控制列，字幕自己渲染 —— 才能完整控制大小 / 顏色 / 位置 / 延遲。 */
const $ = s => document.querySelector(s);
const params = new URLSearchParams(location.search);
const fileId = params.get('file');
const v = $('#v'), pl = $('#pl');

let info = null, hlsObj = null, mode = null, seekingByUs = false;

/* ---------------- 播放執行期狀態（唯一的事實來源）----------------

   **原本的問題**：「現在在播什麼」這件事散在四個地方 —— cfg.quality（使用者
   選的）、info.height（片源的）、curLevel（hls.js 的階）、v.videoWidth（元素的），
   而 renderTags() 主要讀 info.height。於是選了 1080p 上限、ABR 實際降到 480p 時，
   右上角照樣寫著片源的 720p。**介面在說謊。**

   現在只有一份 playbackState，所有 UI 都只能從它取值，而它只由
   updatePlaybackState() 更新。推導邏輯（要顯示什麼字）在 playback-state.js，
   那是純函式所以測得到。 */
const PS = window.PlaybackState;
let playbackState = PS.emptyState();

// 看門狗要用的量測。跟 playbackState 分開放：那個是「要顯示什麼」，
// 這個是「怎麼判斷網路撐不撐得住」，兩者的生命週期不同（切畫質時前者
// 要整個清掉，後者的 stall 歷史反而要留一部分才判斷得出「一直在卡」）。
let metrics = emptyMetrics();
function emptyMetrics() {
  return {
    stallTimes: [], currentStallSince: 0, currentStallMs: 0,
    lowBufferSince: 0, lastActionAt: 0, cappedAt: 0,
    // 最近一段的下載耗時與段長（看門狗用來判斷「下載追不追得上播放」）
    lastFragLoadMs: 0, segmentSeconds: 0, fragLoadingSince: 0, switchingTo: null,
    fellBack: false,
    // 實際「呈現出來」的畫格（見 startFrameMonitor）。**不是 currentTime** ——
    // currentTime 沒往回，不代表螢幕上那張畫面沒有回頭。
    lastPresentedMediaTime: -1, frameRegressions: 0, lastRegression: null,
    presentedFrames: 0,
    // 每分鐘切了幾次階：ABR 震盪只有用數字看得出來，肉眼看不出「切太多」。
    switchTimes: [], switchedTimes: [], stepDownTimes: [], releaseTimes: [],
    // 最近一段的 fragment 身分（切階時要對得出「舊的哪一段、新的哪一段」）
    lastFrag: null,
  };
}

/* ---------------- 呈現畫格的監控（驗收條件的量測工具）----------------

   驗收規則是「**沒有使用者 Seek 時，實際呈現的 frame mediaTime 不得突然
   回退數秒**」。那件事 `video.currentTime` 量不到：它是播放頭的位置，
   而畫面倒退是 decode／render 那一層的事 —— SourceBuffer 被 append 進
   重疊或錯位的內容時，currentTime 可以一路往前，螢幕上卻閃回舊畫面。

   `requestVideoFrameCallback` 給的 `metadata.mediaTime` 才是「這一張正在
   顯示的畫面屬於影片的哪一秒」。所以倒退要用它來判定。

   容許的抖動只有一格（decoder 對 B-frame 的呈現順序本來就會有微小誤差）；
   數秒等級的回退一律記成 FRAME REGRESSION。                              */

// 一格的容許量。24fps 時約 0.042 秒，抓寬一點到 0.25 秒 ——
// 要抓的是「數秒」等級的回退，不是逐格的抖動。
const FRAME_REGRESSION_TOLERANCE = 0.25;
let frameMonitorGen = -1;

function startFrameMonitor(gen) {
  if (typeof v.requestVideoFrameCallback !== 'function') return;
  if (frameMonitorGen === gen) return;        // 同一代不要掛兩份
  frameMonitorGen = gen;
  const step = (_now, meta) => {
    if (isStale(gen)) return;                 // 換片／換模式，這一份監控退休
    const t = meta?.mediaTime;
    if (typeof t === 'number') {
      metrics.presentedFrames = meta.presentedFrames || metrics.presentedFrames + 1;
      const prev = metrics.lastPresentedMediaTime;
      // 使用者自己 seek 的時候本來就會回退，那不是 bug —— 要排除掉。
      const userSeeking = seekingByUs || seekingDrag || pendingSeekTarget != null;
      if (prev >= 0 && !userSeeking && t < prev - FRAME_REGRESSION_TOLERANCE) {
        metrics.frameRegressions++;
        const rec = {
          at: Date.now(), from: prev, to: t, delta: t - prev,
          level: playbackState.actualLevelIndex,
          levelLabel: PS.resolutionText(playbackState.actualResolution) || '?',
          switchingTo: metrics.switchingTo,
          frag: metrics.lastFrag, buffered: rangesText(v.buffered),
          currentTime: v.currentTime,
        };
        metrics.lastRegression = rec;
        // **一定要印出來**：這一類 bug 只在真的播了幾十分鐘之後才出現一次，
        // 沒有這行 log 就只能靠使用者形容「好像跳了一下」。
        console.warn('[FRAME REGRESSION] presented %.2f → %.2f (%.2fs) level=%s'
          + ' switchingTo=%s frag=%o buffered=%s',
          prev, t, rec.delta, rec.levelLabel, rec.switchingTo || '—',
          rec.frag, rec.buffered);
        renderDebugOverlay();
      }
      // 回退之後也要更新，不然一次倒退會讓後面每一格都被記成倒退
      metrics.lastPresentedMediaTime = t;
    }
    try { v.requestVideoFrameCallback(step); } catch {}
  };
  try { v.requestVideoFrameCallback(step); } catch {}
}

/** 最近一分鐘發生幾次。ABR 震盪、看門狗過度插手都只有用頻率看得出來。 */
function perMinute(times, now) {
  const cutoff = (now || Date.now()) - 60000;
  // 就地剪裁，不然一部兩小時的片會讓這幾個陣列無限長
  while (times.length && times[0] < cutoff) times.shift();
  return times.length;
}

// 每次換片源／換模式／換畫質就 +1。所有 hls.js 與 <video> 的非同步 callback
// 都要帶著自己那一代的號碼回來比對 —— 對不上就直接丟掉。
// **這是「切換之後舊事件還在飄」那一整類 bug 的統一解法**（規格的 H、I 案）。
let generation = 0;
function bumpGeneration() { generation++; return generation; }
function isStale(gen) { return gen !== generation; }

// ?debugPlayer=1 才顯示的診斷疊圖。正式模式下整排資訊不塞在畫面上。
const debugPlayer = params.get('debugPlayer') === '1';

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

/* ------------------------------------------------ 播放狀態的更新與呈現 ------------------------------------------------ */

// 目前 currentTime 往後還有多少秒 buffer。**要找「涵蓋 currentTime 的那一段」**，
// 不能直接拿 buffered.end(0) —— 拖過進度條之後 buffered 會有好幾段不連續的區間，
// 拿第一段等於在講一個播放頭根本不在的地方還有多少緩衝。
function bufferAhead() {
  try {
    const t = v.currentTime;
    for (let i = 0; i < v.buffered.length; i++)
      if (v.buffered.start(i) <= t + 0.25 && t < v.buffered.end(i))
        return Math.max(0, v.buffered.end(i) - t);
  } catch {}
  return 0;
}

/** 把 hls.js 與 <video> 現在的樣子收進 playbackState，然後重畫。
 *  `patch` 是這一次事件帶來的增量（例如 LEVEL_SWITCHED 帶 level）。
 *  **所有 UI 都只走這一支** —— 各自去讀 hlsObj 正是原本會不一致的原因。 */
function updatePlaybackState(patch) {
  Object.assign(playbackState, patch || {});
  playbackState.bufferSeconds = bufferAhead();
  if (hlsObj) {
    try {
      playbackState.bandwidthEstimate = Math.round(hlsObj.bandwidthEstimate || 0);
      playbackState.levelCount = (hlsObj.levels || []).length;
      playbackState.capping = hlsObj.autoLevelCapping;
    } catch {}
  }
  renderPlaybackStatus();
}

/** 把 playbackState 畫到畫面上：右上角那一行、debug 疊圖、開著的面板。 */
function renderPlaybackStatus() {
  renderTags();
  renderDebugOverlay();
  // 面板開著的時候裡面也有同一份資訊，不同步的話就又出現兩個說法
  const box = $('#playbackRows');
  if (box) box.innerHTML = PS.detailRows(playbackState).map(([k, val]) =>
    `<div class="ir"><span>${esc(k)}</span><b>${esc(val)}</b></div>`).join('');
}

/** 切片、切模式、切畫質時把狀態清乾淨。**上一部片／上一階不能留** ——
 *  殘留正是「選了新畫質、右上角還寫著舊的」那個症狀。 */
function resetPlaybackState(which) {
  const gen = bumpGeneration();
  const src = info?.quality?.source
    || (info?.width ? { width: info.width, height: info.height,
                        label: PS.qualityClass(info.width, info.height) } : null);
  playbackState = PS.emptyState();
  playbackState.playMode = which;
  playbackState.sourceQuality = src;
  playbackState.selectedQuality = cfg.quality;
  playbackState.remote = !!info?.remote;
  playbackState.mobile = isMobile();
  // stall 歷史刻意**不**跨模式保留：direct 卡了三次而切到 HLS 之後，
  // 那三次不該立刻又讓 HLS 的看門狗降階（它還沒有機會證明自己）。
  const keptFellBack = metrics.fellBack;
  metrics = emptyMetrics();
  metrics.fellBack = keptFellBack;
  // 換模式／換畫質會重建 MediaSource，之前那一次 seek 的等待狀態不能留 ——
  // 留著的話 syncBar() 會一直照舊的 pendingSeekTarget 畫，進度條就卡在
  // 上一個位置不動了。拖曳狀態本身不清（使用者手指可能還按著）。
  pendingSeekTarget = null;
  clearTimeout(seekTimeoutTimer);
  durationMismatchLogged = false;
  renderPlaybackStatus();
  return gen;
}

/* ---------------- debug 疊圖 ---------------- */
/** TimeRanges → "0:00-12:34, 40:00-42:10"。
 *  **seekable 只是「現在跳得到哪」，不是片長** —— 兩者並排顯示正是為了
 *  讓人一眼看出不要把 seekable.end() 誤當成總長度。 */
function rangesText(tr) {
  try {
    if (!tr || !tr.length) return '—';
    const out = [];
    for (let i = 0; i < tr.length && i < 4; i++)
      out.push(`${fmt(tr.start(i))}-${fmt(tr.end(i))}`);
    if (tr.length > 4) out.push(`…+${tr.length - 4}`);
    return out.join(', ');
  } catch { return '—'; }
}
function renderDebugOverlay() {
  if (!debugPlayer) return;
  let el = $('#dbg');
  if (!el) {
    el = document.createElement('div');
    el.id = 'dbg'; el.className = 'dbg';
    $('#stage').appendChild(el);
  }
  const st = playbackState, m = metrics;
  const rows = [
    ['Mode', st.playMode || '—'],
    ['Selected', PS.selectedLabel(st.selectedQuality, st.sourceQuality)],
    ['Actual level', st.actualLevelIndex >= 0 ? `${st.actualLevelIndex + 1}/${st.levelCount}` : '—'],
    ['Actual res', PS.resolutionText(st.actualResolution) || '—'],
    ['Variant bitrate', st.actualBitrate ? PS.fmtKbps(st.actualBitrate / 1000) : '—'],
    ['Bandwidth est', st.bandwidthEstimate ? PS.fmtKbps(st.bandwidthEstimate / 1000) : '—'],
    ['Buffer', st.bufferSeconds.toFixed(1) + 's'],
    ['Stalls', String(st.stallCount)],
    ['Last frag', st.lastFragSize ? (st.lastFragSize / 1024).toFixed(0) + ' KiB' : '—'],
    ['Frag load', st.lastFragLoadMs ? st.lastFragLoadMs + ' ms' : '—'],
    ['Frag thruput', st.lastFragThroughputKbps ? PS.fmtKbps(st.lastFragThroughputKbps) : '—'],
    ['Last switch', st.lastSwitchReason ? PS.switchReasonText(st.lastSwitchReason) : '—'],
    ['Switching to', m.switchingTo || '—'],
    ['Capping', st.capping >= 0 ? `level ≤ ${st.capping}` : 'off'],
    ['Link', st.remote ? 'remote' : 'LAN'],
    ['Mobile', st.mobile ? 'yes' : 'no'],
    ['Fell back', m.fellBack ? 'yes' : 'no'],
  ];
  /* 這次修正加的那一組：**「總時間突然縮短」要看得出是哪一個 duration 在亂。**
     canonical 是 UI 真正在用的那個，api 與 media 並排就看得出誰跟誰不合。 */
  rows.push(
    ['Duration canon', fmt(getCanonicalDuration())],
    ['Duration api', isValidDuration(info?.duration) ? fmt(info.duration) : '—'],
    ['Duration media', isValidDuration(v.duration) ? fmt(v.duration) : String(v.duration)],
    ['Current', fmt(v.currentTime)],
    ['Seek preview', seekPreviewTime != null ? fmt(seekPreviewTime) : '—'],
    ['Dragging', seekingDrag ? 'true' : 'false'],
    ['Seek pending', pendingSeekTarget != null ? fmt(pendingSeekTarget) : '—'],
    ['Last seek', lastSeek
      ? `${fmt(lastSeek.want)} → ${fmt(lastSeek.got)} (${lastSeek.delta >= 0 ? '+' : ''}${lastSeek.delta.toFixed(1)}s)`
      : '—'],
    ['HLS level', st.actualLevelIndex >= 0 ? String(st.actualLevelIndex) : '—'],
    ['Buffered', rangesText(v.buffered)],
    ['Seekable', rangesText(v.seekable)],
  );
  /* 「播放會 lag」要能分成四類，否則只能瞎調參數（規格第十八節）：
       A 網路餵不動 → Frag load > 段長
       B 轉碼餵不動 → Frag load 大但 throughput 也不低（伺服器還在轉）
       C 解碼/算繪跟不上 → Dropped 一直漲，buffer 卻是滿的
       D 時間軸被修正 → buffer 與網路都正常，但 Frame regress 有數字
     這四種的解法完全不同，混在一起看就會把 D 當成 A 去加 buffer。 */
  const q = (() => {
    try { return v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : null; }
    catch { return null; }
  })();
  const total = q?.totalVideoFrames || 0, dropped = q?.droppedVideoFrames || 0;
  const now = Date.now();
  rows.push(
    ['Decoded frames', total ? String(total) : '—'],
    ['Dropped frames', total ? `${dropped} (${(dropped / total * 100).toFixed(2)}%)` : '—'],
    ['Frame mediaTime', m.lastPresentedMediaTime >= 0
      ? m.lastPresentedMediaTime.toFixed(2) + 's' : '—'],
    ['Frame regress', m.lastRegression
      ? `${m.frameRegressions}× 最近 ${m.lastRegression.delta.toFixed(2)}s @ ${fmt(m.lastRegression.from)}`
      : String(m.frameRegressions)],
    ['Switches/min', `${perMinute(m.switchTimes, now)} 決定 / ${perMinute(m.switchedTimes, now)} 完成`],
    ['Watchdog/min', `↓${perMinute(m.stepDownTimes, now)} ↑${perMinute(m.releaseTimes, now)}`],
    ['Frag SN', m.lastFrag
      ? `${m.lastFrag.sn} @ [${m.lastFrag.start.toFixed(1)}, ${m.lastFrag.end.toFixed(1)}]`
      : '—'],
  );
  el.innerHTML = rows.map(([k, val]) =>
    `<div><span>${esc(k)}</span><b>${esc(val)}</b></div>`).join('');
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

  teardownHls(hlsObj); hlsObj = null;
  // 換來源會讓 <video> 自己發 play/pause —— 那不是使用者按的，不能閃中央的圖示。
  // 真的開始播（playing）之後才恢復（見 feedbackMuted）。
  feedbackMuted = true;
  // 手上還沒落地的手勢（累加中的雙擊快轉、2× 長按）是對著上一個來源的，一起收掉
  resetGestures();
  // 這一支的所有非同步 callback 都要帶著 gen 回來比對。舊的那一代
  // 一律丟掉 —— 上一部片、上一階、上一個模式的事件都走這道閘。
  const gen = resetPlaybackState(which);
  v.removeAttribute('src'); v.load();
  center('緩衝中…');
  // 呈現畫格的監控跟著這一代走。**direct 也要掛** —— 「畫面倒退」要能分辨
  // 是 HLS 的時間軸問題還是連 direct 都會發生（那就是別的原因）。
  startFrameMonitor(gen);

  if (which === 'direct') {
    v.src = info.direct_url;
    v.addEventListener('loadedmetadata', () => {
      if (isStale(gen)) return;
      if (t > 1) v.currentTime = t;
      // direct 沒有 level 可問，實際解析度只能問 <video> 自己。
      // 這是唯一一個「元素尺寸就是實際播放尺寸」成立的模式。
      updatePlaybackState({
        actualResolution: v.videoWidth ? { width: v.videoWidth, height: v.videoHeight } : null,
        lastSwitchReason: 'initial',
      });
    }, { once: true });
    v.play().catch(() => {});
    return;
  }

  const url = info.hls_url;
  if (!window.Hls || !Hls.isSupported()) {
    if (v.canPlayType('application/vnd.apple.mpegurl')) {
      // 原生 HLS（iOS Safari）：ABR 由系統管，我們拿不到 level 事件。
      // resize 是唯一問得到「現在實際多大」的訊號。
      v.src = url;
      v.addEventListener('loadedmetadata', () => {
        if (isStale(gen)) return;
        if (t > 1) v.currentTime = t;
        syncNativeResolution();
      }, { once: true });
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
    // 螢幕放不下的階不要選。手機 390px 寬去拉 4K 是純浪費頻寬 ——
    // 而浪費頻寬在行動網路上就等於卡頓。hls.js 1.5 內建這一項，
    // 它會跟著元素尺寸重新評估，所以全螢幕／轉向之後會自己放寬（見下面的
    // resize handler）。**不是永久鎖死低畫質。**
    capLevelToPlayerSize: true,
    // 高 DPI 手機的實體像素要算進去，不然 390 邏輯寬會把 720p 也擋掉
    ignoreDevicePixelRatio: false,
  });

  attachHlsHandlers(gen, mob, { resetRetry: () => { netRetry = 0; mediaRetry = 0; } });

  hlsObj.on(Hls.Events.ERROR, (_, d) => {
    // destroy 之後 hls.js 仍可能送事件進來，這時候再去碰它就會炸。
    // gen 這道閘另外擋掉「已經切到別的模式，但舊實例的事件還在飄」。
    if (dead || isStale(gen) || !hlsObj || !d.fatal) return;
    clearTimeout(retryTimer);
    if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
      if (++netRetry > 6) {
        center('取不到轉碼分段，請看伺服器主控台的 ffmpeg 錯誤訊息', false); kill(); return;
      }
      busy(`轉碼中（重試 ${netRetry}）`);
      retryTimer = setTimeout(() => {
        if (dead || isStale(gen) || !hlsObj) return;
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

/* 全螢幕、旋轉手機、改視窗大小之後要重新評估 —— **不能永久鎖死低畫質**。
   capLevelToPlayerSize 是跟著元素尺寸算的，但 hls.js 只在自己的計時器上重算；
   這裡在尺寸真的變了之後戳它一下，讓「橫過來變全螢幕」立刻能拿到高一階。
   （注意：這裡動的是 capLevelToPlayerSize 那條路，跟看門狗壓的
   autoLevelCapping 是兩個獨立的上限 —— 看門狗壓的那個仍然由 shouldRelease 管，
   不會因為轉了個方向就被放掉。） */
let resizeTimer = null;
function onViewportChange() {
  clearTimeout(resizeTimer);
  // debounce：轉向的過程中會連續送好幾個 resize，每一個都重算是白做工
  resizeTimer = setTimeout(() => {
    if (!hlsObj) { syncNativeResolution(); return; }
    try {
      // 重新指派一次會讓 hls.js 重跑它的尺寸上限計算
      hlsObj.capLevelToPlayerSize = true;
    } catch {}
    updatePlaybackState({});
  }, 350);
}
window.addEventListener('resize', onViewportChange, { passive: true });
window.addEventListener('orientationchange', onViewportChange, { passive: true });
document.addEventListener('fullscreenchange', onViewportChange);
document.addEventListener('webkitfullscreenchange', onViewportChange);

/** 原生 HLS（iOS Safari）沒有 level 事件，只能從元素尺寸推。 */
function syncNativeResolution() {
  if (mode === 'direct' || !v.videoWidth) return;
  updatePlaybackState({
    actualResolution: { width: v.videoWidth, height: v.videoHeight },
  });
}

/** 掛上所有會影響 playbackState 的 hls.js 事件。
 *  **每一個 handler 都先過 isStale(gen)** —— 切畫質／切模式之後舊實例
 *  排隊中的 callback 還會再跑，沒有這道閘就會把上一階的資訊寫進新狀態。 */
function attachHlsHandlers(gen, mob, ctl) {
  const H = hlsObj;
  const on = (ev, fn) => H.on(ev, (_, d) => { if (!isStale(gen) && hlsObj === H) fn(d); });

  on(Hls.Events.MANIFEST_PARSED, d => {
    const levels = d?.levels || H.levels || [];
    // 開播策略：手機遠端先壓一階，寧可畫質差一階也要快點開始播。
    // 賭最高畫質、然後卡十幾秒是更差的體驗（規格第 3 節）。
    const pol = PS.startupPolicy({
      remote: !!info?.remote, mobile: mob,
      effectiveType: navigator.connection?.effectiveType,
      downlink: navigator.connection?.downlink,
      saveData: !!navigator.connection?.saveData,
    });
    if (pol.capIndex >= 0 && levels.length > 1) {
      // levels 是**碼率由低到高**排的（hls.js 保證），所以 index 就是階。
      // 從最低往上數 capIndex 階：0 = 最低、1 = 倒數第二低。
      const cap = Math.min(pol.capIndex, levels.length - 1);
      try {
        H.autoLevelCapping = cap;
        metrics.cappedAt = Date.now();
      } catch {}
      if (debugPlayer) toast(`開播保守：${pol.reason}`);
    }
    updatePlaybackState({ levelCount: levels.length, lastSwitchReason: 'initial' });
    v.play().catch(() => {});
  });

  // LEVEL_SWITCHING 是「決定要切了」，LEVEL_SWITCHED 是「切完了」。
  // **右上角那一行只能吃 SWITCHED** —— SWITCHING 的時候畫面上放的還是舊那一階，
  // 這時候就改文字等於又說了一次謊（只是方向相反）。
  // 這裡只記「正在切」給 debug 疊圖看：手機卡頓時「決定切了但一直切不過去」
  // 與「根本沒決定要切」是兩種不同的病，截圖時分得出來才有用。
  on(Hls.Events.LEVEL_SWITCHING, d => {
    const lv = H.levels?.[d?.level];
    metrics.switchingTo = lv?.height ? lv.height + 'p' : null;
    metrics.switchTimes.push(Date.now());
    /* **切階的當下要把兩邊的時間軸都記下來。**「畫面倒退」如果是 rendition
       對不齊造成的，證據就在這裡：舊 fragment 的 SN 與 PTS 區間、新 level、
       currentTime、以及 buffered。三者兜起來就看得出 append 進去的那一段
       是不是落在已經播過的位置上。 */
    if (debugPlayer) {
      console.info('[LEVEL_SWITCHING] t=%.2f %s → level %s  lastFrag=%o buffered=%s',
        v.currentTime, PS.resolutionText(playbackState.actualResolution) || '?',
        d?.level, metrics.lastFrag, rangesText(v.buffered));
    }
    renderDebugOverlay();
  });

  on(Hls.Events.LEVEL_SWITCHED, d => {
    const lv = H.levels?.[d.level];
    metrics.switchingTo = null;          // 切完了，「正在切」要收掉
    metrics.switchedTimes.push(Date.now());
    if (debugPlayer) {
      console.info('[LEVEL_SWITCHED] t=%.2f → level %s (%sp) buffered=%s',
        v.currentTime, d.level, lv?.height ?? '?', rangesText(v.buffered));
    }
    if (!lv) return;
    const prev = playbackState.actualBitrate;
    // 手動 = 我們或使用者指定了 currentLevel（不是 -1）。
    const manual = H.autoLevelEnabled === false;
    updatePlaybackState({
      ...PS.levelToState(lv, d.level, H.levels.length),
      lastSwitchReason: PS.switchReason(prev, lv.bitrate || 0, manual),
    });
  });

  // 這一段開始下載了。**下載中的那一段也要算進「追不追得上」** ——
  // 只看 FRAG_LOADED 的話，一段卡在下載中永遠不會回來時 lastFragLoadMs
  // 停在上一段的好成績上，看門狗會以為一切正常（那正是最該降階的時候）。
  on(Hls.Events.FRAG_LOADING, () => { metrics.fragLoadingSince = Date.now(); });

  on(Hls.Events.FRAG_LOADED, d => {
    ctl.resetRetry();
    const st = d?.frag?.stats || d?.stats;
    if (!st) { updatePlaybackState({}); return; }
    const ms = Math.max(1, Math.round((st.loading?.end || 0) - (st.loading?.first || st.loading?.start || 0)));
    const size = st.total || st.loaded || 0;
    updatePlaybackState({
      lastFragSize: size, lastFragLoadMs: ms,
      lastFragThroughputKbps: size ? Math.round(size * 8 / ms) : 0,
    });
    metrics.lastFragLoadMs = ms;
    metrics.fragLoadingSince = 0;        // 這一段回來了，不再算「還在等」
    metrics.segmentSeconds = d?.frag?.duration || metrics.segmentSeconds || 6;
    // 這一段的身分：倒退發生時要回答「當時在播哪一段、它宣稱涵蓋哪個區間」。
    const f = d?.frag;
    if (f) metrics.lastFrag = {
      sn: f.sn, level: f.level, start: +(f.start || 0).toFixed(3),
      duration: +(f.duration || 0).toFixed(3),
      end: +((f.start || 0) + (f.duration || 0)).toFixed(3),
    };
  });

  /* FRAG_CHANGED = 「播放頭現在真的在這一段裡了」。跟 FRAG_LOADED 分開記：
     載入順序不等於播放順序，而**倒退要對的是正在播的那一段**。 */
  on(Hls.Events.FRAG_CHANGED, d => {
    const f = d?.frag;
    if (!f) return;
    metrics.lastFrag = {
      sn: f.sn, level: f.level, start: +(f.start || 0).toFixed(3),
      duration: +(f.duration || 0).toFixed(3),
      end: +((f.start || 0) + (f.duration || 0)).toFixed(3),
    };
    if (debugPlayer) {
      // **播放頭與 fragment 宣稱的區間對不上就是 rendition 錯位的直接證據。**
      const t = v.currentTime, s = f.start || 0, e = s + (f.duration || 0);
      if (t < s - 1 || t > e + 1)
        console.warn('[FRAG MISMATCH] currentTime=%.2f 不在 SN=%s 宣稱的 [%.2f, %.2f] 內',
          t, f.sn, s, e);
    }
    renderDebugOverlay();
  });

  // watchdogTick() 自己會先刷新狀態，不必在這裡再刷一次
  on(Hls.Events.FRAG_BUFFERED, () => watchdogTick());
}

/* ------------------------------------------------ 卡頓看門狗 ------------------------------------------------

   **這一段最容易做壞的地方是「跟 hls.js 的 ABR 打架」。**
   hls.js 本身就有 ABR，每一個 waiting 都手動切一次 level 的話，兩套會
   互相覆蓋：它剛往上切、我們就往下壓，下一秒它又往上，畫質每幾秒震盪一次。

   所以這裡的角色是**例外處理，不是第二套 ABR**：
     * 只在 Auto 模式動（手動挑了上限的話，上限底下的階交給 hls.js）
     * 要好幾個訊號同時指向「真的撐不住」才插手一次（shouldStepDown）
     * 插手的方式是壓 autoLevelCapping，不是鎖 currentLevel ——
       壓上限之後 ABR 仍然在上限底下正常運作
     * 恢復穩定就把上限拿掉，交還控制權（shouldRelease），不永久鎖死  */

function watchdogTick() {
  if (mode !== 'hls' || !hlsObj) return;
  const now = Date.now();
  // **先把狀態刷新再判斷。**看門狗讀的 buffer／頻寬如果是上一個事件留下的
  // 快照，判斷就會慢一拍 —— 而它插手的時機本來就只有幾秒的餘裕。
  // updatePlaybackState({}) 會重新問 <video> 與 hls.js 拿現在的值。
  updatePlaybackState({});
  const st = playbackState;

  // buffer 危險水位：要**持續**低才算，瞬間掉下去不算（下載中本來就會掉）
  if (st.bufferSeconds < PS.WATCHDOG.lowBufferSec) {
    if (!metrics.lowBufferSince) metrics.lowBufferSince = now;
  } else metrics.lowBufferSince = 0;

  const levels = hlsObj.levels || [];
  const curIdx = hlsObj.currentLevel;
  // **不要用 index === 0 判斷「在最低階」** —— 索引順序由 hls.js 內部決定。
  // 直接比碼率（player.js 原本的 waiting handler 已經為這件事留過註解）。
  const cur = levels[curIdx];
  const atLowest = levels.length > 1 && cur
    && cur.bitrate <= Math.min(...levels.map(x => x.bitrate));

  // 「最近一段花了多久」取兩者的大值：已經下載完的那一段，以及**還在下載中**
  // 的那一段到現在為止已經等了多久。少了後者的話，一段永遠回不來時
  // lastFragLoadMs 會停在上一段的好成績上，看門狗以為一切正常。
  const inflight = metrics.fragLoadingSince ? now - metrics.fragLoadingSince : 0;
  const down = PS.shouldStepDown({
    autoMode: cfg.quality === PS.QUALITY_AUTO,
    levelCount: levels.length, atLowestLevel: !!atLowest,
    stallTimes: metrics.stallTimes, lowBufferSince: metrics.lowBufferSince,
    lastFragLoadMs: Math.max(metrics.lastFragLoadMs || 0, inflight),
    segmentSeconds: metrics.segmentSeconds,
    lastActionAt: metrics.lastActionAt,
  }, now);

  if (down.act) { stepDown(down.reason, now); return; }

  const up = PS.shouldRelease({
    capping: hlsObj.autoLevelCapping, cappedAt: metrics.cappedAt,
    stallTimes: metrics.stallTimes, bufferSeconds: st.bufferSeconds,
    bandwidthEstimate: st.bandwidthEstimate,
    nextLevelBitrate: levels[Math.min(hlsObj.autoLevelCapping + 1, levels.length - 1)]?.bitrate || 0,
  }, now);
  if (up.act) releaseCap(up.reason, now);
}

/** 主動往下壓一階。壓的是 autoLevelCapping（上限），不是 currentLevel ——
 *  鎖死 currentLevel 等於把 ABR 整個關掉，之後網路恢復也不會自己回來。 */
function stepDown(reason, now) {
  const levels = hlsObj.levels || [];
  const curIdx = hlsObj.currentLevel >= 0 ? hlsObj.currentLevel : levels.length - 1;
  const target = Math.max(0, Math.min(curIdx - 1,
    hlsObj.autoLevelCapping >= 0 ? hlsObj.autoLevelCapping - 1 : curIdx - 1));
  if (target === hlsObj.autoLevelCapping) return;
  try { hlsObj.autoLevelCapping = target; } catch { return; }
  metrics.cappedAt = now; metrics.lastActionAt = now;
  metrics.lowBufferSince = 0;
  metrics.stepDownTimes.push(now);
  updatePlaybackState({ lastSwitchReason: 'stall-down', capping: target });
  const lab = levels[target]?.height ? levels[target].height + 'p' : '較低畫質';
  toast(`網路不穩，已降到 ${lab}`);
  if (debugPlayer) console.info('[watchdog] step down:', reason, '→ cap', target);
}

/** 把上限拿掉，讓 hls.js 的 Auto ABR 重新全權負責。 */
function releaseCap(reason, now) {
  try { hlsObj.autoLevelCapping = -1; } catch { return; }
  metrics.cappedAt = 0; metrics.lastActionAt = now;
  metrics.releaseTimes.push(now);
  updatePlaybackState({ lastSwitchReason: 'recover', capping: -1 });
  if (debugPlayer) console.info('[watchdog] release:', reason);
}

/** 記一次卡頓。direct 與 hls 共用同一個計數 —— 兩邊的處置不同，
 *  但「卡了幾次」這件事本身是同一件事。 */
function noteStall() {
  const now = Date.now();
  metrics.stallTimes.push(now);
  // 只留最近 60 秒，不然清單會無限長（一部片兩小時）
  metrics.stallTimes = metrics.stallTimes.filter(x => now - x <= 60000);
  metrics.currentStallSince = now;
  updatePlaybackState({ stallCount: playbackState.stallCount + 1 });
  if (mode === 'direct') maybeFallbackFromDirect();
  else watchdogTick();
}

function noteStallEnd() {
  metrics.currentStallSince = 0;
  metrics.currentStallMs = 0;
}

/* ------------------------------------------------ Direct → HLS 自動 fallback ------------------------------------------------

   **direct 本質上是原始檔的 HTTP Range 串流，沒有第二階可選。**
   所以「在 direct 裡面降到 480p」這件事不存在 —— 唯一正確的處置是換一條
   有階梯的路。換過去時要**保留播放位置**：在 00:42:15 卡住就從 00:42:15
   繼續，跳回開頭是比卡頓更糟的失效。                                        */

function maybeFallbackFromDirect() {
  const now = Date.now();
  metrics.currentStallMs = metrics.currentStallSince ? now - metrics.currentStallSince : 0;
  const r = PS.shouldFallbackFromDirect({
    playMode: mode, fellBack: metrics.fellBack,
    stallTimes: metrics.stallTimes, currentStallMs: metrics.currentStallMs,
    directAdvised: info?.direct_advised,
  }, now);
  if (!r.act) return;
  fallbackToHls(r.reason);
}

function fallbackToHls(reason) {
  // **這道閘擋掉「舊的 direct 事件在切換之後又觸發第二次」**（規格 H 案）。
  // fellBack 在 resetPlaybackState 之間是刻意保留的。
  if (metrics.fellBack || mode !== 'direct') return;
  metrics.fellBack = true;
  const at = v.currentTime;          // 位置要留住，不能跳回開頭
  if (debugPlayer) console.info('[direct] fallback:', reason, '@', at);
  toast('網路速度不足，已從直接串流改用自動畫質');
  play('hls', at);
  updatePlaybackState({ lastSwitchReason: 'direct-fallback' });
  buildPanel();
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
function playUrl({ height, audio, forceDirect }) {
  const q = new URLSearchParams();
  // 0 = 自動（依區網/遠端判定）、-1 = 原畫質不縮放、其餘 = 指定高度
  if (height > 0) q.set('h', height);
  else if (height === -1) q.set('h', 0);
  if (audio != null) q.set('a', audio);
  // 使用者明確按了「試試直接播放」。**這個旗標只放寬伺服器的建議，
  // 不關掉 fallback** —— 真的持續卡頓還是會自己切回 HLS（規格第 5 節）。
  if (forceDirect) q.set('force_direct', '1');
  const qs = q.toString();
  return `/api/play/${fileId}${qs ? '?' + qs : ''}`;
}

/** 手動切換播放來源。跟自動 fallback 走不同的路：
 *  使用者明確要求時要**尊重他的選擇**，所以要重新跟伺服器要一份帶
 *  force_direct 的播放資訊（不然遠端大檔會被政策擋回 HLS，按了沒反應）。 */
/* **播放位置一定要在 `await` 之後才讀。**寫成 `const at = v.currentTime` 放在
   函式開頭看起來無害，但 `await fetch(...)` 期間影片沒有暫停、還在往前播 ——
   手機上這一趟 RTT 是 0.2 秒到數秒，於是 `play()` 會把位置設回一個**比實際
   播放位置更舊**的時間點，使用者看到的就是「切個畫質，畫面倒退幾秒」。
   行動網路 RTT 大，所以手機比桌機明顯得多。 */
async function switchMode(which) {
  if (which === 'direct') {
    busy('切換來源');
    try {
      info = await (await fetch(playUrl({ height: cfg.quality, audio: info.audio_index,
                                          forceDirect: true }))).json();
    } catch { busy(''); toast('切換來源失敗'); return; }
    // 手動要求試 direct = 給它一次新的機會。之前自動切回來過也重新計次，
    // 不然按了「試試直接播放」會因為 fellBack 還是 true 而立刻被切回去。
    metrics.fellBack = false;
    if (info.remote && info.direct_advised === false) {
      toast('這個片源位元率偏高，遠端可能會卡；卡住會自動切回自動畫質');
    }
  }
  play(which, v.currentTime);
  buildPanel();
}

async function switchAudio(index) {
  if (index === info.audio_index) return;
  busy('切換音軌');
  try {
    info = await (await fetch(playUrl({ height: cfg.quality, audio: index }))).json();
  } catch { busy(''); toast('切換音軌失敗'); return; }
  rememberAudio(index);
  // 直接播放是把原始檔整個丟給瀏覽器，選哪一軌由瀏覽器決定；只有轉碼選得了
  // 位置在這裡才讀，理由見 switchMode() 上面那段註解。
  play('hls', v.currentTime);
  const t = (info.audio_tracks || []).find(x => x.index === index);
  toast(`音軌：${t ? audioLabel(t, 0) : index}`);
  buildPanel();
}

// 切換之後的提示要跟面板上寫的一致：級別 ＋ 實際解析度。
// **指定高度時要講「上限」** —— 後端仍然會發比它低的階，講成固定值就是說謊。
function qualityToast(height) {
  const q = info?.quality || {};
  const src = q.source;
  if (height === 0) return '畫質：自動';
  if (height === -1) {
    return src ? `畫質：原畫質 ${src.label}（${src.width}×${src.height}）` : '畫質：原畫質';
  }
  const lv = (q.levels || []).find(l => l.h === height);
  return lv ? `畫質上限：${lv.label}（${lv.width}×${lv.height}），網路不足仍會降階`
            : `畫質上限：${height}p，網路不足仍會降階`;
}


async function switchQuality(height) {
  cfg.quality = height; saveCfg();
  busy('切換畫質');
  try {
    info = await (await fetch(playUrl({ height, audio: info.audio_index }))).json();
  } catch { busy(''); toast('切換畫質失敗'); return; }
  // play() 會 resetPlaybackState()，把上一階的 curLevel／解析度整個清掉。
  // **這正是「切了畫質，右上角還寫著舊的 720p」那個症狀的修法**：
  // 新狀態一開始是空的，等 LEVEL_SWITCHED 進來才會有實際畫質。
  // 位置在這裡才讀，理由見 switchMode() 上面那段註解。
  play(info.mode, v.currentTime);
  toast(qualityToast(height));
  buildPanel();
}

/* ------------------------------------------------ 控制列 ------------------------------------------------ */
const ICON_PLAY = '<path d="M7 4l13 8-13 8z"/>';
const ICON_PAUSE = '<path d="M6 4h4v16H6zM14 4h4v16h-4z"/>';
// 播完之後播放鍵改成「重播」。它是 stroke 圖示（跟其他 .ib 一樣），
// 所以要把 .solid 的 fill 關掉，不然會畫成一坨實心。
const ICON_REPLAY = '<path d="M3 12a9 9 0 1 0 2.64-6.36" fill="none" stroke="currentColor" stroke-width="2.2"'
  + ' stroke-linecap="round"/><path d="M3 4v5h5" fill="none" stroke="currentColor" stroke-width="2.2"'
  + ' stroke-linecap="round" stroke-linejoin="round"/>';

function syncPlayBtn() {
  $('#icPlay').innerHTML = v.ended ? ICON_REPLAY : v.paused ? ICON_PLAY : ICON_PAUSE;
  $('#btnPlay').title = v.ended ? '重播 (空白鍵)' : '播放 / 暫停 (空白鍵)';
}

/* 使用者要播／要停。**所有來源都走這兩支**（播放鍵、畫面、快捷鍵、Media Session）——
   userPaused 決定控制列要不要一直留著，feedbackMuted 決定中央要不要閃圖示，
   兩者都只該被「人」改到，所以集中在這裡而不是散在每個 handler 裡。 */
function userPlay() { userPaused = false; feedbackMuted = false; v.play().catch(() => {}); }
function userPause() { userPaused = true; feedbackMuted = false; v.pause(); }
const togglePlay = () => {
  // 播完了再按「播放」語意是重播 —— 走 replay() 才會經過 Seek 與進度的正確流程
  if (v.ended) replay();
  else if (v.paused) userPlay();
  else userPause();
};
$('#btnPlay').onclick = togglePlay;

/* ---------------- Seek：唯一一條路 ----------------

   **按鈕、鍵盤、觸控手勢、進度條、重播、Media Session 全部從這裡出去。**
   原本進度條（commitScrub）與跳秒（skipBy）各寫一份 currentTime 指派，
   兩邊對「seek 中」的定義不一樣：跳秒不設 pendingSeekTarget，於是跳秒期間
   圓點會先回到舊位置、進度照樣被寫進資料庫。收成一支之後，
   seekingByUs／pendingSeekTarget／逾時保險對每一種 seek 都成立。

     seekTo(t, source)      絕對位置 —— 真正寫 currentTime 的只有這裡
     skipBy(n)              相對位置
     seekBy(delta, source)  相對位置 ＋ 依來源給回饋（按鈕／鍵盤用 toast，
                            手勢有自己的空間回饋，所以不 toast） */
function seekTo(t, source) {
  const target = clampSeekTarget(t);
  // **我們自己發動的 seek 要標記起來。**不標的話「倒退 10 秒」會被
  // startFrameMonitor() 記成一次 FRAME REGRESSION（它只看 mediaTime 有沒有變小），
  // 於是驗收用的 `Frame regress` 數字會被自己的操作灌水，看起來比實際嚴重。
  seekingByUs = true;
  // 還沒有 metadata（readyState 0）時指派 currentTime 只是設「起播位置」，
  // 不會有 seeked —— 那時候掛 pending 會讓進度條與進度寫入白白卡 30 秒。
  if (v.readyState !== 0) {
    pendingSeekTarget = target;
    // **一定要有逾時放行。**seeked 是唯一會把 pendingSeekTarget 清掉的事件，
    // 而它不保證一定來（fragment 抓不到、MediaSource 被重建、瀏覽器把這次
    // seek 丟掉）。沒有這道保險的話，進度條會永遠停在使用者選的位置不動，
    // 看起來像整個播放器當掉 —— 比 seek 失敗本身更糟。
    clearTimeout(seekTimeoutTimer);
    seekTimeoutTimer = setTimeout(() => {
      if (pendingSeekTarget == null) return;
      if (debugPlayer)
        console.warn('[seek] 逾時未收到 seeked，放行 UI（目標 ' + target.toFixed(2) + '）');
      pendingSeekTarget = null;
      busy('');
      syncBar();
    }, SEEK_TIMEOUT_MS);
  }
  if (debugPlayer) console.info('[seek] requested:', target.toFixed(2), source || '');
  try { v.currentTime = target; } catch {}
  syncBar();
  return target;
}
// 跳秒數也要走 clamp（在 seekTo 裡）—— 在片尾按「快轉 10 秒」不該把
// currentTime 頂到 duration 而直接觸發 ended。
const skipBy = n => seekTo(v.currentTime + n, 'skip');
function seekBy(delta, source = 'button') {
  if (!delta || !isFinite(delta)) return;
  skipBy(delta);
  if (source === 'button' || source === 'keyboard') {
    const n = Math.abs(delta);
    toast(delta > 0 ? `${n} 秒 ▶` : `◀ ${n} 秒`);
  }
}
// seeked 一定會來（同一個位置的 seek 也會），所以旗標不會卡住。
v.addEventListener('seeked', () => { seekingByUs = false; });
$('#btnBack').onclick = () => seekBy(-cfg.skip, 'button');
$('#btnFwd').onclick = () => seekBy(cfg.skip, 'button');
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

/* ---------------- 進度條：Scrubbing 與 Seek ----------------

   **原本的寫法是壞的**，而且三個症狀其實是同一個根因的三種表現：

     1. 拖到一半突然跳到片尾
     2. 右下角的總時間從 2:30:00 變成 35:00
     3. 圓點不跟手指走

   根因是「拖曳過程中直接、連續地寫 v.currentTime，而且用 v.duration 當尺」。

   · **連續寫 currentTime**：pointermove 一秒可以來幾十次，每一次對 HLS
     來說都是一次真正的 seek —— 取消目前的 fragment、重選階、重建 buffer、
     發新的 HTTP request，而下階是**隨選轉碼**的，等於叫 ffmpeg 去轉一堆
     使用者根本不會看的段。手指還在動，播放器已經在追幾十個互相取消的
     seek，這就是「拖起來很卡」。

   · **拿 v.duration 當尺**：MediaSource 的 duration 是「目前 buffer 覆蓋到
     哪裡」推出來的，在 seek／換階之後會暫時縮水。於是同一個手指位置，
     50% 這一刻是 4500 秒、下一刻 v.duration 掉到 1800 就變成 900 秒 ——
     UI 的總時間跟著縮短（症狀 2），而算出來的 target 也整個歪掉（症狀 1）。
     拖到最右邊時還會剛好 currentTime === duration 而直接觸發 ended。

   · **圓點靠 syncBar() 更新**：syncBar() 讀的是 v.currentTime，而拖曳期間
     真正的 currentTime 不該動 —— timeupdate 一進來就把圓點拉回播放頭，
     下一個 pointermove 又拉回手指，於是圓點在兩處之間抖（症狀 3）。

   **新的架構**分成兩段，這是硬性要求（不是 debounce）：

     pointermove  →  只更新畫面（seekPreviewRatio 這一個來源）
     pointerup    →  才寫一次 v.currentTime

   一整次拖曳**只會產生一次** currentTime 指派，不管手指移動了幾百次。 */

const barWrap = $('#barWrap');

/** 指標在進度條上的位置 → 0～1。
 *  只吃 Pointer Events（不再混 touches）—— 兩套事件並存會讓手機上每個
 *  動作跑兩次，那正是「明明只拖一次卻發出兩次 seek」的來源。 */
const posOf = e => {
  const r = barWrap.getBoundingClientRect();
  if (!r.width) return 0;
  return clamp((e.clientX - r.left) / r.width, 0, 1);
};

/* scrubbing 狀態。**這三個是拖曳期間 UI 的唯一事實來源** ——
   barPlay／barKnob／barTip 都只能從 seekPreviewRatio 取值，各自算一份
   正是原本會不同步的原因。 */
let seekingDrag = false;        // 手指/滑鼠正按著進度條
let seekPreviewRatio = null;    // 0～1，手指現在在哪
let seekPreviewTime = null;     // 換算成秒（用 canonical duration 換的）
let seekPointerId = null;
let wasPlayingBeforeSeek = false;
// 已經 commit 出去、但播放器還沒真的跳過去的目標。**在 seeked 之前不能讓
// 圓點回到 currentTime** —— HLS 要等 fragment，那段期間 currentTime 還是舊值，
// 圓點會先跳回原處再跳到新位置，看起來就像「放開之後自己亂跑」。
let pendingSeekTarget = null;
// 最後一次 seek 的「要的 / 拿到的 / 差多少」，只給 debug 疊圖看。
let lastSeek = null;
// seeked 沒來時的保險（見 commitScrub）。30 秒是刻意寬的 —— 下階是隨選
// 轉碼，一段第一次被要的時候 ffmpeg 真的可能跑十幾秒，太短會在「其實還在
// 轉」的時候就把 UI 放掉。
const SEEK_TIMEOUT_MS = 30000;
let seekTimeoutTimer = null;

/** 影片的「真正總長度」。
 *
 *  **不能無條件相信 v.duration。**它在 HLS/MediaSource 底下是隨 buffer 變動的
 *  推估值，seek 或換階之後會暫時變成幾百秒 —— 讓它當 canonical 的話，
 *  UI 的總時間會從 2:32:03 跳成 37:21，而進度條的比例尺整個歪掉。
 *
 *  /api/play 給的 info.duration 是後端 ffprobe 出來的完整片長，對 VOD 而言
 *  這才是穩定的答案，所以**它優先**。v.duration 只在沒有 API 值時才用
 *  （例如 info 還沒回來的那幾百毫秒）。 */
function isValidDuration(d) {
  return typeof d === 'number' && isFinite(d) && !Number.isNaN(d) && d > 0;
}

// 兩邊差太多時只警告、不換尺。差異容忍：5 秒或 2%（取大的）——
// 容器標示的片長跟實際解出來的長度本來就會差個一兩秒，那不算異常。
const DURATION_TOLERANCE_S = 5;
const DURATION_TOLERANCE_RATIO = 0.02;
let durationMismatchLogged = false;

function getCanonicalDuration() {
  const api = info?.duration;
  const media = v.duration;
  if (isValidDuration(api)) {
    // 對得起來就什麼都不用說；對不起來要留紀錄，但**還是用 API 那個**。
    // 「最後收到的 duration 就是真的」是錯的策略：MediaSource 的值本來就
    // 會在播放期間上下跳，跟著它走等於讓 UI 跟著抖。
    if (isValidDuration(media)) {
      const tol = Math.max(DURATION_TOLERANCE_S, api * DURATION_TOLERANCE_RATIO);
      if (Math.abs(media - api) > tol && !durationMismatchLogged) {
        durationMismatchLogged = true;
        if (debugPlayer)
          console.warn(`duration mismatch\nAPI: ${api}\nmedia: ${media}`);
      }
    }
    return api;
  }
  return isValidDuration(media) ? media : 0;
}

/** Seek 目標一律 clamp 在 [0, duration - EPS]。
 *  **不能讓 currentTime 剛好等於 duration** —— 那在多數瀏覽器會直接觸發
 *  ended，使用者把手指拖到最右邊就變成「影片突然結束」。
 *
 *  **epsilon 要以「幾個 frame」為尺度，不是隨手取個小數。**先前用 0.05 秒，
 *  而 24fps 的一格是 0.042 秒 —— 退不到兩格，播下去立刻又撞到結尾，實測
 *  拖到 100% 仍然 ended。0.5 秒在最慢的 24fps 也有 12 格，足夠讓使用者
 *  看到「跳到接近片尾」而不是「影片結束了」，而對兩小時的片來說，
 *  0.5 秒在進度條上連半個像素都不到，看不出被截短。 */
const SEEK_END_EPS = 0.5;
function clampSeekTarget(t, d) {
  const dur = isValidDuration(d) ? d : getCanonicalDuration();
  if (!isValidDuration(dur)) return Math.max(0, t || 0);
  return clamp(t, 0, Math.max(0, dur - SEEK_END_EPS));
}

/** 拖曳中的畫面：三個元素共用同一個 ratio。 */
function renderSeekPreview(ratio) {
  const d = getCanonicalDuration();
  const p = clamp(ratio, 0, 1);
  const t = p * d;
  seekPreviewRatio = p;
  seekPreviewTime = t;
  const pct = (p * 100) + '%';
  $('#barPlay').style.width = pct;
  $('#barKnob').style.left = pct;
  const tip = $('#barTip');
  tip.style.left = pct;
  tip.textContent = fmt(t);
  // 目前時間也跟著走，使用者才知道「放開會跳到哪」而不必只盯 tooltip
  $('#tCur').textContent = fmt(t);
  $('#tDur').textContent = fmt(d);
}

function beginScrub(e) {
  seekingDrag = true;
  seekPointerId = e.pointerId;
  // 暫停與否要原樣還回去 —— 拖進度條不該順手把播放狀態改掉
  wasPlayingBeforeSeek = !v.paused;
  barWrap.classList.add('dragging');
  try { barWrap.setPointerCapture(e.pointerId); } catch {}
  wake();
  renderSeekPreview(posOf(e));
}

/** 放開：整次拖曳唯一一次真正的 Seek。
 *
 *  `e` 給的話**以它的座標為準**。pointerup 自己帶著一個位置，而它不保證
 *  等於最後一次 pointermove —— 快速拖曳時瀏覽器可能合併掉中間幾個 move，
 *  最後一個 move 與放開的位置就會差上一小段。以 move 為準的話，使用者
 *  眼睛看到手指停在哪、實際跳到的卻是稍早的那個點。 */
function commitScrub(e) {
  const ratio = e ? posOf(e) : seekPreviewRatio;
  if (e) renderSeekPreview(ratio);      // 畫面先對齊到真正要跳的位置
  endScrub();
  if (ratio == null) return;
  const d = getCanonicalDuration();
  if (!isValidDuration(d)) return;
  // pendingSeekTarget、逾時保險、seekingByUs 都在 seekTo 裡 —— 跟跳秒同一條路
  seekTo(clampSeekTarget(ratio * d, d), 'scrub');
  // 原本在播就繼續播、原本暫停就維持暫停
  if (wasPlayingBeforeSeek) v.play().catch(() => {});
}

/** 清掉拖曳狀態。pointerup／pointercancel／lostpointercapture 都要走這裡 ——
 *  少一條路就會留下一個永遠為 true 的 dragging，之後所有 timeupdate 都被
 *  當成「使用者還在拖」而不再更新畫面。 */
function endScrub() {
  if (!seekingDrag) return;
  seekingDrag = false;
  barWrap.classList.remove('dragging');
  if (seekPointerId != null) {
    try { barWrap.releasePointerCapture(seekPointerId); } catch {}
  }
  seekPointerId = null;
  wake();
}

barWrap.addEventListener('pointerdown', e => {
  // 只接主鍵/觸控，右鍵不要進入拖曳
  if (e.button != null && e.button !== 0) return;
  e.preventDefault();
  beginScrub(e);
});

barWrap.addEventListener('pointermove', e => {
  if (seekingDrag) {
    if (seekPointerId != null && e.pointerId !== seekPointerId) return;
    renderSeekPreview(posOf(e));
    return;
  }
  // 沒在拖：滑鼠 hover 時仍然顯示「這裡是幾分幾秒」，但不碰圓點與播放色條
  const p = posOf(e);
  const tip = $('#barTip');
  tip.style.left = (p * 100) + '%';
  tip.textContent = fmt(p * getCanonicalDuration());
});

barWrap.addEventListener('pointerup', e => {
  if (!seekingDrag) return;
  if (seekPointerId != null && e.pointerId !== seekPointerId) return;
  commitScrub(e);
});
// 手勢被瀏覽器中斷（滑出、轉向、來電、多指）—— 不 commit，狀態要清乾淨
barWrap.addEventListener('pointercancel', () => endScrub());
barWrap.addEventListener('lostpointercapture', () => endScrub());

function syncBar() {
  // 拖曳中：畫面完全由 seekPreviewRatio 決定，不准 currentTime 搶回去。
  // **這就是「手指 60% → timeupdate → 圓點跳回 20%」那個抖動的修法。**
  if (seekingDrag) {
    if (seekPreviewRatio != null) renderSeekPreview(seekPreviewRatio);
    return;
  }
  const d = getCanonicalDuration();
  // 已 commit 但還沒 seeked（HLS 在等 fragment）：圓點停在使用者選的位置。
  // 讓它照 currentTime 畫的話會先跳回原處，看起來像「放開之後自己亂跑」。
  const at = pendingSeekTarget != null ? pendingSeekTarget : v.currentTime;
  const p = d ? clamp((at / d) * 100, 0, 100) : 0;
  $('#barPlay').style.width = p + '%';
  $('#barKnob').style.left = p + '%';
  $('#tCur').textContent = fmt(at);
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
let idleTimer, pointerOverCtl = false, lastInputTouch = false;
// 觸控給久一點：手指離開螢幕之後要「看一眼、再伸手」，滑鼠游標一直在畫面上。
const IDLE_MS = 2800, IDLE_TOUCH_MS = 3500;

/** 現在控制列能不能收起來。**每一條都是「收起來就把使用者手上的東西藏掉」：**
 *  · userPaused  使用者主動暫停 —— 這時候他要看的就是控制列。
 *                （不用 v.paused：緩衝/轉碼中 video 是 waiting，用 paused 判斷的話
 *                 那條漸層會一直蓋在畫面下緣不走。）
 *  · ended       播完了，下一步一定是按某個東西（重播／下一集）
 *  · seekingDrag／pendingSeekTarget  拖曳中、或 seek 還沒落地
 *  · 面板開著    設定與字幕都在這一片裡 */
function controlsMustStay() {
  return userPaused || v.ended || pointerOverCtl || seekingDrag || pendingSeekTarget != null
    || $('#panel').classList.contains('show');
}
function armIdle() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    // 條件還在就**重排**而不是放棄 —— 放棄的話條件解除之後（seek 落地、面板關掉）
    // 沒有人會再把它收起來，控制列就一直掛著直到下一次碰螢幕。
    if (controlsMustStay()) { armIdle(); return; }
    pl.classList.add('idle');
  }, lastInputTouch ? IDLE_TOUCH_MS : IDLE_MS);
}
function wake() {
  pl.classList.remove('idle');
  armIdle();
}
/** 手勢「點一下收起控制列」。收不得的時候（暫停中、播完）就維持原樣。 */
function sleepControls() {
  if (controlsMustStay()) return;
  clearTimeout(idleTimer);
  pl.classList.add('idle');
}
let userPaused = false;
$('#ctl').addEventListener('pointerenter', () => { pointerOverCtl = true; });
$('#ctl').addEventListener('pointerleave', () => { pointerOverCtl = false; wake(); });
['pointermove', 'pointerdown', 'keydown', 'wheel'].forEach(ev => document.addEventListener(ev, e => {
  if (e.pointerType) lastInputTouch = e.pointerType === 'touch';
  // **#tap 上的觸控由手勢控制器自己決定要不要叫出控制列。**這裡也 wake 的話，
  // 每一次雙擊快轉都會把整條控制列叫出來 —— 要的是「畫面乾淨地雙擊快轉」。
  if (e.pointerType === 'touch' && e.target === tapEl) return;
  wake();
}, { passive: true }));

/* ------------------------------------------------ 視覺回饋 ------------------------------------------------

   **所有回饋都是 player.html 裡固定的一個元素，重複使用，不新建 DOM。**
   快速連按十次只是把同一個元素的動畫重新開始十次 —— 每次 createElement
   一個再等它淡出移除的話，連按會疊一堆半透明圓圈，而且要處理「移除之前
   又按了一下」的競態。全部 pointer-events:none：它們浮在 #tap 上面，
   擋到點擊的話手勢就收不到第二下了。 */

/** 讓同一個元素的 CSS 動畫從頭再跑一次。拿掉 class → 強迫 reflow → 加回去。 */
function restartAnim(el, cls) {
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
}

/* ---- 播放／暫停：中央的 ▶ ❚❚ ↻ ----

   **以 <video> 的 play/pause 事件為準，不是在 togglePlay() 裡顯示。**
   這樣快捷鍵、畫面、控制列、Media Session、耳機線控全部同一種回饋。
   但事件不分「人按的」與「播放器自己做的」，所以要另外擋：
     · feedbackMuted  換來源（play()）、HLS teardown、切下一集之前的 pause。
                      開頁到第一次 playing 之前也算（自動起播不是使用者按的）。
                      第一次 playing 或使用者操作（userPlay/userPause）才解除。
     · v.ended        播完時瀏覽器會先發 pause 再發 ended —— 那不是「暫停」。 */
let feedbackMuted = true;
let nextPlayFlash = null;          // 下一次 play 要閃的圖示（重播時是 ↻ 不是 ▶）
const FLASH_MS = 700;
let flashTimer = null;
function flash(kind) {
  const el = $('#flash');
  if (!el) return;
  el.dataset.kind = kind;
  restartAnim(el, 'show');
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => el.classList.remove('show'), FLASH_MS);
}
v.addEventListener('play', () => {
  const kind = nextPlayFlash || 'play';
  nextPlayFlash = null;
  if (!feedbackMuted) flash(kind);
});
v.addEventListener('pause', () => {
  if (!feedbackMuted && !v.ended) flash('pause');
});
v.addEventListener('playing', () => { feedbackMuted = false; });

/* ---- 左右快轉：按哪邊，提示就出現在哪邊 ---- */
// seek 真的送出去之後再留這麼久才淡出 —— 從最後一下算起約 750ms
const SEEK_FB_TAIL_MS = 300;
const seekFb = { timer: null, visible: false };
function showSeekFeedback(dir, secs) {
  const on = $(dir < 0 ? '#seekL' : '#seekR'), off = $(dir < 0 ? '#seekR' : '#seekL');
  off.classList.remove('show', 'pulse');
  $(dir < 0 ? '#seekLText' : '#seekRText').textContent = `${secs} 秒`;
  on.classList.add('show');
  restartAnim(on, 'pulse');          // 每加一次跳一下，使用者看得出「有算到這一下」
  seekFb.visible = true;
  clearTimeout(seekFb.timer);
  seekFb.timer = setTimeout(hideSeekFeedback, GESTURE.seekCommitMs + SEEK_FB_TAIL_MS);
}
function hideSeekFeedback() {
  clearTimeout(seekFb.timer);
  seekFb.visible = false;
  for (const id of ['#seekL', '#seekR']) $(id).classList.remove('show', 'pulse');
}

/* ------------------------------------------------ 觸控手勢（#tap）------------------------------------------------

   **所有觸控手勢只在這裡判定**，而且只掛在 #tap（影片畫面那一層）上。
   控制列、進度條、下一集、設定面板都是 #tap 的**兄弟**而不是子元素 ——
   事件根本不會傳進來，所以不需要到處 stopPropagation 補洞。唯一跨區的是
   document 層的 wake()，那裡已經把「#tap 上的觸控」排除（見上面）。

   滑鼠完全不走這裡：click → 播放/暫停、dblclick → 全螢幕，照舊。
   判斷依據是 pointerdown 的 pointerType，而**不是 click 事件自己的
   pointerType** —— 舊版 iOS Safari 的 click 不是 PointerEvent，拿不到
   pointerType，原本的寫法在那裡會「pointerup 切一次、click 又切一次」。

   畫面切三區（左 35%／中 30%／右 35%）：

     單擊   控制列藏著 → 叫出控制列（**不會**跳秒、不會暫停 —— 手機誤觸太多）
            控制列在   → 中央：播放/暫停；左右：收起控制列
            播完了     → 中央：重播
     雙擊   左 → 倒退 cfg.skip、右 → 快轉 cfg.skip、中 → 播放/暫停
            連續雙擊會**累加**（+10 → +20 → +30），最後只 seek 一次
     長按   右側：暫時 2×，放開恢復原本的速度

   **單擊一定要等雙擊窗口過了才動作** —— 不然雙擊快轉的第一下會先把
   控制列叫出來（或先暫停），那正是要避免的。代價是單擊慢 300ms 才反應。 */
const GESTURE = {
  doubleMs: 300,        // 第一下放開 → 第二下按下，超過就不算雙擊
  doubleDist: 40,       // 兩下之間的距離上限（CSS px）—— 左邊一下、右邊一下不算雙擊
  slop: 12,             // 一下之內手指飄多遠還算「點」（超過就是滑動，不處理）
  holdMs: 450,          // 按住多久進入 2×；**超過這個時間放開也不算單擊**
  holdRate: 2,
  seekCommitMs: 450,    // 最後一次雙擊之後多久才真的 seek（等使用者可能的下一下）
  zones: [0.35, 0.65],
};
const tapEl = $('#tap');
let tapPointerType = 'mouse';      // 最近一次在 #tap 上按下的是什麼 —— click／dblclick 靠它分流
const gesture = {
  down: null,        // 這一下 { id, x, y, t, zone, moved, candidate, consumed }
  touches: new Set(),// 目前按著的手指。兩根以上 = 捏合縮放，整組作廢
  lastTap: null,     // 等第二下的那一下 { x, y, t(放開的時間), zone }
  singleTimer: null,
  holdTimer: null,
  hold: null,        // 2× 進行中 { previousPlaybackRate }
};
// 累加中的雙擊快轉。**UI 每一下立刻更新，真正的 seek 只在最後做一次** ——
// 對 HLS 而言每一次 seek 都是「取消 fragment → 重新定位 → 重抓」，
// 連點三次若各自 seek，就是三輪互相取消的下載（而下階是隨選轉碼的）。
const gSeek = { delta: 0, dir: 0, timer: null };

const isTouchPointer = e => e.pointerType === 'touch';
function zoneAt(x) {
  const r = tapEl.getBoundingClientRect();
  const p = r.width ? (x - r.left) / r.width : 0.5;
  return p < GESTURE.zones[0] ? 'left' : p > GESTURE.zones[1] ? 'right' : 'center';
}
const distTo = (a, x, y) => Math.hypot(a.x - x, (a.y || 0) - (y || 0));

tapEl.addEventListener('pointerdown', e => {
  tapPointerType = e.pointerType || 'mouse';
  if (!isTouchPointer(e)) return;
  const g = gesture;
  g.touches.add(e.pointerId);
  if (g.touches.size > 1) { abortGesture(); return; }

  const t = e.timeStamp, x = e.clientX, y = e.clientY;
  const last = g.lastTap;
  const candidate = !!last && t - last.t <= GESTURE.doubleMs
    && distTo(last, x, y) <= GESTURE.doubleDist;
  if (candidate) clearTimeout(g.singleTimer);   // 可能是雙擊的第二下：前一下的單擊先別做
  else if (last) flushSingleTap();              // 太晚／太遠：前一下確定是單擊

  g.down = {
    id: e.pointerId, x, y, t, zone: zoneAt(x), moved: false, candidate,
    // 面板開著時點畫面 = 關面板（document 層那支會關），這一下不再兼做別的事
    consumed: $('#panel').classList.contains('show'),
  };
  try { tapEl.setPointerCapture(e.pointerId); } catch {}
  if (g.down.zone === 'right' && !candidate && !g.down.consumed && canFastHold())
    g.holdTimer = setTimeout(beginFastHold, GESTURE.holdMs);
});

tapEl.addEventListener('pointermove', e => {
  const d = gesture.down;
  if (!d || e.pointerId !== d.id || d.moved) return;
  if (distTo(d, e.clientX, e.clientY) > GESTURE.slop) {
    d.moved = true;
    // 還沒進 2× 就開始滑 → 不是長按。已經在 2× 的話手指飄一點不算放開。
    if (!gesture.hold) clearTimeout(gesture.holdTimer);
  }
});

tapEl.addEventListener('pointerup', e => {
  if (!isTouchPointer(e)) return;
  const g = gesture, d = g.down;
  g.touches.delete(e.pointerId);
  if (!d || d.id !== e.pointerId) return;
  g.down = null;
  clearTimeout(g.holdTimer);
  if (g.hold) { endFastHold(); return; }            // 長按放開，不是點擊
  // 滑動、按太久、關面板的那一下：都不是點擊。等著配對的前一下也一起作廢 ——
  // 「點一下再按住」不該在放開時被當成單擊補做。
  const tooLong = e.timeStamp - d.t >= GESTURE.holdMs;
  if (d.moved || d.consumed || tooLong) { if (d.candidate) g.lastTap = null; return; }
  if (d.candidate) {
    const first = g.lastTap;
    g.lastTap = null;
    onDoubleTap(first.zone);
    return;
  }
  g.lastTap = { x: d.x, y: d.y, t: e.timeStamp, zone: d.zone };
  g.singleTimer = setTimeout(flushSingleTap, GESTURE.doubleMs);
});

// 手勢被瀏覽器中斷（捲動接手、來電、轉向、多指）：什麼都不做，但狀態要清乾淨 ——
// 尤其 2× 一定要恢復，不然使用者手指早就離開了，影片還在用兩倍速跑。
function onTapAbort(e) {
  gesture.touches.delete(e.pointerId);
  const d = gesture.down;
  if (!d || d.id !== e.pointerId) return;
  gesture.down = null;
  // 被打斷的是雙擊的第二下：等著配對的那一下也作廢，不然它之後會被當成單擊補做
  if (d.candidate) gesture.lastTap = null;
  clearTimeout(gesture.holdTimer);
  endFastHold();
}
tapEl.addEventListener('pointercancel', onTapAbort);
tapEl.addEventListener('lostpointercapture', onTapAbort);
// Android 長按會跳系統選單。只擋觸控；滑鼠右鍵照舊。
tapEl.addEventListener('contextmenu', e => { if (tapPointerType === 'touch') e.preventDefault(); });

// 滑鼠：沿用原本的 click → 播放/暫停、dblclick → 全螢幕
tapEl.onclick = () => { if (tapPointerType !== 'touch') togglePlay(); };
// 手機瀏覽器有時會把兩下觸控合成 dblclick —— 觸控的雙擊是快轉，不是全螢幕
tapEl.ondblclick = () => { if (tapPointerType !== 'touch') toggleFull(); };

function flushSingleTap() {
  clearTimeout(gesture.singleTimer);
  const tap = gesture.lastTap;
  gesture.lastTap = null;
  if (tap) onSingleTap(tap.zone);
}

/** 手勢全部作廢（多指、離開頁面、全螢幕切換）。還沒落地的累加快轉**不**丟 ——
 *  那是使用者已經看到「+30 秒」的操作，它有自己的計時器會照常落地。 */
function abortGesture() {
  clearTimeout(gesture.singleTimer);
  clearTimeout(gesture.holdTimer);
  gesture.lastTap = null;
  gesture.down = null;
  endFastHold();
}
/** 換來源時用：連累加中的快轉也一起丟（它是對著上一個來源算的）。 */
function resetGestures() {
  abortGesture();
  gesture.touches.clear();
  clearTimeout(gSeek.timer);
  gSeek.timer = null; gSeek.delta = 0; gSeek.dir = 0;
  hideSeekFeedback();
}

function onSingleTap(zone) {
  // 連續雙擊快轉之間多出來的一下（沒配成對）：吞掉，不要叫出控制列打斷使用者
  if (gSeek.timer || seekFb.visible) return;
  if (v.ended) { zone === 'center' ? replay() : wake(); return; }
  if (pl.classList.contains('idle')) { wake(); return; }
  if (zone === 'center') { togglePlay(); wake(); }
  else sleepControls();
}

function onDoubleTap(zone) {
  if (zone === 'center') { togglePlay(); return; }
  gestureSeekAdd(zone === 'left' ? -1 : 1);
}

function gestureSeekAdd(dir) {
  // 換邊：前一段先落地再開新的一段。不做淨額相加 —— 使用者已經看到「+20 秒」，
  // 接著按左邊時他預期的是「從 +20 那裡再倒退」，不是兩者抵銷。
  if (gSeek.dir && gSeek.dir !== dir) commitGestureSeek();
  gSeek.dir = dir;
  gSeek.delta += dir * cfg.skip;
  showSeekFeedback(dir, Math.abs(gSeek.delta));
  clearTimeout(gSeek.timer);
  gSeek.timer = setTimeout(commitGestureSeek, GESTURE.seekCommitMs);
}
function commitGestureSeek() {
  clearTimeout(gSeek.timer);
  const d = gSeek.delta;
  gSeek.timer = null; gSeek.delta = 0; gSeek.dir = 0;
  if (d) seekBy(d, 'gesture');
}

/* ---- 長按暫時 2× ----
   進入時記下**當下的**速度，放開時還原成它 —— 使用者原本是 1.25× 就回 1.25×，
   不是回 1×。暫停中不啟動：長按不該變成「開始播放」。 */
function canFastHold() { return !v.paused && !v.ended; }
function beginFastHold() {
  gesture.holdTimer = null;
  const d = gesture.down;
  if (!d || d.moved || gesture.hold || !canFastHold()) return;
  gesture.hold = { previousPlaybackRate: v.playbackRate };
  v.playbackRate = GESTURE.holdRate;
  $('#holdFb').classList.add('show');
}
function endFastHold() {
  const h = gesture.hold;
  if (!h) return;
  gesture.hold = null;
  v.playbackRate = h.previousPlaybackRate;
  $('#holdFb').classList.remove('show');
}
// 手指還按著但畫面已經不是這個畫面了：切走 App、鎖螢幕、進出全螢幕。
// 這些時候 pointerup 不保證會來，不收的話 2× 會一直留著。
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') abortGesture();
});
window.addEventListener('pagehide', abortGesture);
document.addEventListener('fullscreenchange', abortGesture);
document.addEventListener('webkitfullscreenchange', abortGesture);
v.addEventListener('webkitbeginfullscreen', abortGesture);
v.addEventListener('webkitendfullscreen', abortGesture);

/* ---------------- 播完：重播 ----------------

   **Replay 要走 seekTo()，不是直接 currentTime = 0。**走同一條路才會有
   pendingSeekTarget，而 saveProgress 在 seek 落地之前不寫 —— 否則按下重播
   的那一瞬間，currentTime 還是片尾、狀態卻已經不是 ended，會寫出一筆
   「片尾、未完成」。

   另外一個洞是**重播之後的第一筆進度**：這一集剛剛才被標成 finished，
   重播幾秒又關掉的話，節流寫入會把它改回「看到 0:08、未完成」，
   它就重新出現在「繼續觀看」上。所以重播後要真的看過 REPLAY_KEEP_FINISHED_S
   秒才開始寫一般進度（那時候才算「在重看」而不是「手滑按到」）。 */
const REPLAY_KEEP_FINISHED_S = 30;
let replayGuard = false;
function replay() {
  if (v.ended) replayGuard = true;
  nextPlayFlash = 'replay';
  seekTo(0, 'replay');
  userPlay();
}
function syncEndedUi() {
  pl.classList.toggle('ended', !!v.ended);
  syncPlayBtn();
}
v.addEventListener('ended', () => { syncEndedUi(); wake(); });
['play', 'seeking', 'loadstart', 'emptied'].forEach(ev => v.addEventListener(ev, syncEndedUi));

/* ---------------- 控制元件的按壓回饋 ----------------
   手機沒有 hover，按下去沒有任何變化的話使用者分不出「沒按到」與「還在處理」。
   :active 在 iOS 上不可靠（要有 touchstart listener 才會套），而且快速點一下
   根本來不及畫出來，所以用一個固定 ~140ms 的 class。 */
pl.addEventListener('pointerdown', e => {
  const b = e.target?.closest?.('.ib, .next-ep');
  if (!b) return;
  restartAnim(b, 'pressed');
  clearTimeout(b._pressTimer);
  b._pressTimer = setTimeout(() => b.classList.remove('pressed'), 160);
}, { passive: true });

/* ---------------- Media Session（鎖定畫面、耳機、藍牙鍵）----------------
   **跟畫面上的按鈕共用同一套**：play/pause 走 userPlay/userPause，
   跳秒走 seekBy，指定位置走 seekTo —— 沒有第二套 seek。 */
function setupMediaSession() {
  const ms = navigator.mediaSession;
  if (!ms || typeof ms.setActionHandler !== 'function') return;
  // 不支援的 action 會丟例外（Safari 舊版沒有 seekto），一個失敗不能讓其他的也掛不上
  const set = (action, fn) => { try { ms.setActionHandler(action, fn); } catch {} };
  set('play', () => { v.ended ? replay() : userPlay(); });
  set('pause', () => userPause());
  set('seekbackward', d => seekBy(-(d?.seekOffset || cfg.skip), 'media-session'));
  set('seekforward', d => seekBy(d?.seekOffset || cfg.skip, 'media-session'));
  set('seekto', d => { if (d && isFinite(d.seekTime)) seekTo(d.seekTime, 'media-session'); });
}
function syncMediaMetadata() {
  const ms = navigator.mediaSession;
  if (!ms || !info || typeof window.MediaMetadata !== 'function') return;
  try {
    ms.metadata = new MediaMetadata({
      title: info.title || '', artist: info.subtitle_label || '', album: 'FilmaxWeb',
    });
  } catch {}
}
setupMediaSession();

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

  // **「現在在播什麼」不在這一區。**這一區從頭到尾講的是片源（這個檔案本身
  // 長什麼樣），而正在播的那一份由 playbackState 提供、列在上面的「播放狀態」
  // 區塊。原本把兩者混在一起，正是「顯示的畫質不是實際播放畫質」的來源之一。
  //
  // 這裡只留伺服器端的**上限設定**（它是「我們打算送什麼」，仍然屬於設定
  // 而不是實測），並且明講它是上限。
  const q = info.quality || {};
  if (mode !== 'direct') {
    rows.push(['伺服器上限', `${q.height ? q.height + 'p' : '不縮放'}`
      + (q.bitrate_kbps ? ` · ${fmtRate(q.bitrate_kbps)}` : ' · 位元率不限')]);
  }
  if (info.filename) rows.push(['檔名', info.filename]);
  return rows;
}

function renderTags() {
  const box = $('#tags');
  if (!box || !info) return;
  const t = [];
  // **這一顆是「現在真正在播的」，不是片源。**
  // 原本這裡是 info.height（片源高度）—— 選了 1080p 上限、ABR 實際降到 480p 時
  // 它照樣寫著片源的 720p，而使用者看到的就是那個數字。
  // 片源資訊改放在面板的「片源」那一列，兩者刻意不混在一起。
  const line = PS.statusLine(playbackState);
  if (line) {
    const tip = playbackState.playMode === 'direct'
      ? '直接串流：原始檔直送，沒有畫質階梯'
      : (cfg.quality === PS.QUALITY_AUTO
         ? '自動調節：依目前網路速度選的畫質，會隨網路上下切'
         : '你選的是「上限」，網路不足時仍會降到較低的階');
    t.push(`<span class="tg live" title="${esc(tip)}">${esc(line)}</span>`);
  }
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
                        ...levels.map(l => [String(l.h),
                          // **「720p」與「720p 上限」是兩件不同的事。**
                          // 後端 abr_ladder(auto=false) 仍然會發比它低的階，
                          // 所以選它的語意是「最高 720p」而不是「固定 720p」。
                          // 只寫「720p」的話，實際降到 360p 時使用者會以為介面壞了。
                          l.label + ' 上限' + (dim(l) ? ` <small>${dim(l)}</small>` : '')])],
           cfg.quality)}
      <div class="hint">選一個數字是設「上限」不是鎖死 —— 網路不足時仍會自動降到更低的階，
        右上角與上面的「播放狀態」會顯示目前實際在播的那一階。</div>
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

    <h4>播放狀態</h4>
    <div class="hint">「片源」是這個檔案本身；「目前實際畫質」是瀏覽器這一秒真的在播的東西。
      選了上限不代表每一秒都播得到那個上限 —— 網路不足時會自動降階。</div>
    <div class="info" id="playbackRows">${PS.detailRows(playbackState).map(([k, val]) =>
      `<div class="ir"><span>${esc(k)}</span><b>${esc(val)}</b></div>`).join('')}</div>

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
    switchMode(mode === 'direct' ? 'hls' : 'direct');
    $('#panel').classList.remove('show'); });
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
v.addEventListener('playing', () => {
  center(''); busy(''); noteStallEnd(); updatePlaybackState({});
});
// 卡住的原因有兩種，講錯會把人引導到錯的方向：伺服器還在轉碼（等一下就好），
// 還是網路餵不動（那要等 ABR 降階）。已經在最低階還在等 = 網路問題。
v.addEventListener('waiting', () => {
  noteStall();
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
v.addEventListener('canplay', () => {
  center(''); busy(''); noteStallEnd(); updatePlaybackState({});
});
// direct 與原生 HLS 沒有 level 事件，畫面尺寸變了就是「實際畫質變了」。
// hls.js 那條路也掛著沒關係 —— 它只會確認一次已經從 level 拿到的答案。
v.addEventListener('resize', () => {
  if (mode === 'direct' || !hlsObj) syncNativeResolution();
});
v.addEventListener('loadeddata', () => {
  if (mode === 'direct' || !hlsObj) syncNativeResolution();
});
v.addEventListener('volumechange', syncVol);
v.addEventListener('timeupdate', () => {
  syncBar(); renderCues(); syncNextEp();
  // 節流（不是 debounce）—— 原本的 clearTimeout + setTimeout 寫法在連續
  // 播放時永遠不會到期，見 saveProgress 上方的說明。
  saveProgress();
});
v.addEventListener('progress', () => {
  syncBar();
  // buffer 水位是 direct 那條路唯一的健康訊號（它沒有 fragment 事件），
  // 而 progress 是它唯一會固定送出來的事件。
  updatePlaybackState({});
  if (mode === 'direct') maybeFallbackFromDirect();
});
v.addEventListener('seeking', () => {
  renderCues(true);
  // HLS 還在抓 fragment 的期間給一個明確的提示，但**圓點不動** ——
  // 位置仍然停在使用者選的地方（見 syncBar 的 pendingSeekTarget）。
  if (pendingSeekTarget != null) busy('跳轉中…');
});

/* Seek 落地：驗證「要的」跟「拿到的」差多少。
   誤差大到離譜（例如選 4500 卻跑到 8990）代表 playlist 的時間軸與
   MediaSource 對不起來，那是後端的問題，不是 UI 的 —— 所以要留紀錄，
   不能默默接受。 */
const SEEK_DELTA_WARN_S = 5;
v.addEventListener('seeked', () => {
  if (pendingSeekTarget != null) {
    const want = pendingSeekTarget, got = v.currentTime;
    const delta = got - want;
    if (debugPlayer) {
      const line = `[seek] requested: ${want.toFixed(2)}  actual: ${got.toFixed(2)}  ` +
                   `delta: ${delta >= 0 ? '+' : ''}${delta.toFixed(2)}`;
      if (Math.abs(delta) > SEEK_DELTA_WARN_S) console.warn(line + '  ← 誤差異常');
      else console.info(line);
    }
    lastSeek = { want, got, delta };
    pendingSeekTarget = null;
    clearTimeout(seekTimeoutTimer);
    busy('');
    // **`seeked` 不等於「新畫面已經出現在螢幕上」。**它只說播放頭移好了；
    // decoder 還可能再顯示幾張舊位置的畫格。真正的完成訊號是「第一張
    // mediaTime 落在目標附近的畫格」—— 這裡量它，因為「seek 完看到舊畫面」
    // 與「ABR 切階看到舊畫面」是兩種不同的病，要分得出來。
    // **只量測、不重設 currentTime**：為了畫面好看而反覆 seek 會製造更多卡頓。
    awaitSeekedFrame(want);
  }
  syncBar();
});

/* Seek 之後第一張「真的呈現出來」的畫格。只在 debugPlayer 記錄 ——
   正式模式下不需要為它多掛一條 callback。 */
function awaitSeekedFrame(want) {
  if (!debugPlayer || typeof v.requestVideoFrameCallback !== 'function') return;
  const t0 = performance.now();
  const gen = generation;
  const step = (_now, meta) => {
    if (isStale(gen)) return;
    const t = meta?.mediaTime;
    if (typeof t !== 'number') return;
    if (Math.abs(t - want) <= 1.0) {
      console.info('[seek] 第一張新畫格 mediaTime=%.2f（目標 %.2f，落地耗時 %d ms）',
        t, want, Math.round(performance.now() - t0));
      return;                              // 到位了，這條 callback 收工
    }
    // 還在放舊位置的畫格 —— 這正是「seek 完短暫看到之前畫面」的證據
    console.warn('[seek] 仍在呈現舊畫格 mediaTime=%.2f（目標 %.2f，差 %.2fs）',
      t, want, t - want);
    try { v.requestVideoFrameCallback(step); } catch {}
  };
  try { v.requestVideoFrameCallback(step); } catch {}
}
v.addEventListener('loadedmetadata', () => { v.playbackRate = cfg.rate; syncBar(); syncNextEp(); });

v.addEventListener('error', () => {
  if (mode !== 'direct') return;
  // **fellBack 統一由 metrics 管**（不再另外一個區域變數）—— 兩個地方各記
  // 一份「切過了沒」的話，錯誤與卡頓兩條路徑會各切一次。
  if (metrics.fellBack) {
    center('這個檔案無法在瀏覽器播放，請下載後用本機播放器開啟', false);
    return;
  }
  metrics.fellBack = true;
  const at = v.currentTime;          // 位置一樣要留住
  toast('直接播放失敗，改用轉碼');
  play('hls', at);
  updatePlaybackState({ lastSwitchReason: 'direct-fallback' });
});

/* ---------------- 進度儲存 ----------------

   **原本的寫法是壞的**：每次 timeupdate 都 `clearTimeout` 再
   `setTimeout(saveProgress, 5000)`。timeupdate 在正常播放時每 250ms 左右
   就發一次，所以那個 timer **永遠在被重設，永遠不會到期** —— 連續播放
   一小時，一筆進度都沒有寫進去。它只在「暫停或卡住超過 5 秒」時才會真的
   存，而那剛好是最不需要它的時候（那些時機另外有事件可以掛）。

   這是 debounce 與 throttle 用錯的典型：debounce 的語意是「等事件停下來
   再做」，而播放期間事件本來就不會停。

   改成真正的節流：距離上次寫入超過 SAVE_EVERY_MS 才寫，其餘直接忽略。
   一小時的片子從「0 次」變成「每 8 秒一次」，DB 寫入量仍然很小
   （一筆 upsert，WAL 底下一次 fsync）。

   另外在關鍵時機補存，因為節流一定會漏掉最後那幾秒：
     pause / seeked      使用者主動停下來或跳位置
     visibilitychange    切 App、鎖螢幕 —— 手機上最重要的一個
     pagehide            iOS Safari 的 beforeunload 常常不觸發
     beforeunload        桌機關分頁
     ended               真的播完
   visibilitychange 與 pagehide 都要掛：iOS 上切 App 只會觸發前者，
   而回到 Safari 再關分頁只會觸發後者。 */
const SAVE_EVERY_MS = 8000;
let lastSaveAt = 0, lastSavedPos = -1;

/** 寫進度。force = 不管節流一定寫（離開頁面、暫停、播完）。
 *
 *  sendBeacon 才是離開頁面時唯一可靠的送法（fetch 會被取消），
 *  但它在部分瀏覽器有佇列上限、也可能回 false —— 回 false 時退回
 *  keepalive fetch，不要靜靜地掉掉。 */
function saveProgress(finished = false, { force = false } = {}) {
  if (!info) return;
  // **拖曳期間絕對不能寫進度。**preview 只是「打算跳到哪」，真正的
  // currentTime 還停在原處；而 seek 已經送出、還沒落地時 currentTime 也
  // 仍是舊值。這兩種情形寫進去都是把錯的位置蓋到資料庫上 ——
  // 使用者下次回來會從一個他沒看到的地方續播。
  if (seekingDrag || pendingSeekTarget != null) return;
  const pos = v.currentTime;
  if (!pos && !finished) return;
  // 剛播完又按了重播：真的重看一段之前不要把 finished 改回未完成（見 replay()）
  if (replayGuard && !finished) {
    if (pos < REPLAY_KEEP_FINISHED_S) return;
    replayGuard = false;
  }
  const now = Date.now();
  if (!force && !finished) {
    if (now - lastSaveAt < SAVE_EVERY_MS) return;
    // 位置沒動就不必再寫一次（暫停後 timeupdate 仍可能零星進來）
    if (Math.abs(pos - lastSavedPos) < 1) return;
  }
  lastSaveAt = now; lastSavedPos = pos;
  const body = JSON.stringify({ file_id: +fileId, position: pos,
    duration: getCanonicalDuration(), finished });
  const blob = new Blob([body], { type: 'application/json' });
  const sent = navigator.sendBeacon?.('/api/progress', blob);
  if (!sent) {
    // keepalive 讓請求在頁面關掉之後仍然送得完
    fetch('/api/progress', { method: 'POST', body,
      headers: { 'Content-Type': 'application/json' }, keepalive: true }).catch(() => {});
  }
}

/** 確實把目前這一集標記完成，並等到真的送出去為止。
 *
 *  **為什麼需要一個會 await 的版本**：切下一集時如果只發 sendBeacon 再
 *  立刻 `location.href = ...`，導覽會把還沒送出的請求砍掉 ——
 *  於是這一集留在 finished=0，下一秒又出現在「繼續觀看」上。
 *  這正是「按了下一集，結果舊的那一集自己跑回來」的根因。
 *
 *  所以這裡用會回 Promise 的 fetch + keepalive，並在跳頁前 await 它。
 *  逾時也要放行 —— 網路卡住不該把使用者困在這一頁。 */
async function finishCurrentEpisode() {
  if (!info) return;
  const body = JSON.stringify({ file_id: +fileId, position: v.currentTime || 0,
    duration: getCanonicalDuration(), finished: true });
  lastSaveAt = Date.now(); lastSavedPos = v.currentTime;
  try {
    await Promise.race([
      fetch('/api/progress', { method: 'POST', body,
        headers: { 'Content-Type': 'application/json' }, keepalive: true }),
      new Promise(r => setTimeout(r, 1500)),
    ]);
  } catch {
    // 送不出去至少留一個 beacon，總比完全沒有好
    navigator.sendBeacon?.('/api/progress', new Blob([body], { type: 'application/json' }));
  }
}

v.addEventListener('ended', () => { saveProgress(true, { force: true }); syncNextEp(); });
v.addEventListener('pause', () => saveProgress(false, { force: true }));
v.addEventListener('seeked', () => saveProgress(false, { force: true }));
window.addEventListener('beforeunload', () => saveProgress(false, { force: true }));
// 手機切 App / 鎖螢幕：beforeunload 不會來，這兩個才是可靠的時機。
window.addEventListener('pagehide', () => saveProgress(false, { force: true }));
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') saveProgress(false, { force: true });
});

/* ---------------- 下一集（Netflix 式）----------------

   只有影集、而且後端真的算出 next_episode 時才會有這顆按鈕 ——
   「有沒有下一集」「是哪一集」都由後端決定（app/episodes.py），
   前端不自己猜，否則兩邊的排序規則遲早會分岔。

   出現時機的門檻在 playback-state.js 的 nextEpisodeVisible()（純函式，
   測得到）。刻意**不做倒數自動跳台** —— 這個專案原本沒有自動播放設定，
   突然讓它自己跳到下一集是會嚇到人的行為改變。 */
function nextEpInfo() { return info?.next_episode || null; }

function syncNextEp() {
  const box = $('#nextEp');
  if (!box) return;
  const n = nextEpInfo();
  const show = PS.nextEpisodeVisible({
    hasNext: !!n,
    duration: getCanonicalDuration(),
    currentTime: v.currentTime,
    ended: v.ended,
  });
  box.hidden = !show;
  if (show && box.dataset.for !== String(n.file_id)) {
    box.dataset.for = String(n.file_id);
    const label = [n.label, n.title].filter(Boolean).join(' ');
    $('#nextEpLabel').textContent = '下一集' + (label ? ' ' + label : '');
    box.setAttribute('aria-label', '播放下一集' + (label ? ' ' + label : ''));
  }
}

/** 按下「下一集」：先確實完成這一集，再跳頁。
 *
 *  順序不能顛倒（見 finishCurrentEpisode 的說明）—— 先跳頁的話，
 *  這一集的 finished 寫不進去，它會重新出現在「繼續觀看」上。 */
async function goNextEpisode() {
  const n = nextEpInfo();
  if (!n) return;
  const btn = $('#nextEp');
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }
  // 這個 pause 是「要換頁了」，不是使用者按暫停 —— 不閃 ❚❚
  feedbackMuted = true;
  try { v.pause(); } catch {}
  await finishCurrentEpisode();
  location.href = n.url || ('/player?file=' + n.file_id);
}

// 點按與鍵盤都要能用。button 元素本來就吃 Enter/Space 的原生 click，
// 所以這裡只掛 click 一個就夠 —— 另外自己攔 keydown 反而會在
// Space 時觸發兩次（一次原生 click、一次自己的）。
// 按鈕本身不能吃掉 .tapzone 的點擊：它是 .stage 的子元素而且在上層，
// 所以要擋住冒泡，否則按下去會同時暫停影片。
$('#nextEp')?.addEventListener('click', e => { e.stopPropagation(); goNextEpisode(); });

document.addEventListener('keydown', e => {
  if (['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)) return;
  // 焦點在按鈕上時，Space / Enter 是「按下這顆按鈕」而不是全域快捷鍵。
  // 不讓開的話「下一集」用鍵盤永遠按不到 —— Space 會被這裡攔去暫停影片。
  if (e.target.tagName === 'BUTTON' && (e.key === ' ' || e.key === 'Enter')) return;
  const k = e.key.toLowerCase();
  if (k === ' ' || k === 'k') { e.preventDefault(); togglePlay(); }
  else if (e.key === 'ArrowRight') seekBy(e.shiftKey ? 60 : cfg.skip, 'keyboard');
  else if (e.key === 'ArrowLeft') seekBy(-(e.shiftKey ? 60 : cfg.skip), 'keyboard');
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
  syncMediaMetadata();

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
  syncNextEp();
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

/* 測試掛勾（tests/player_wiring_test.js）。

   **為什麼需要它**：這個檔案是一個大 IIFE 式的 script，`info`／`mode`／
   `playbackState` 都是 `let` 宣告的 module 區域變數，從外面碰不到。
   而這次修正的重點正是「事件進來之後狀態有沒有真的被改」—— 只驗純函式
   證明不了那件事（純函式全對但沒有人呼叫它們，是最容易留下的漏洞）。

   這裡只掛讀寫幾個內部變數的存取器，不改任何行為，正式頁面上沒有人會用到它。
   刻意不掛整個內部狀態 —— 掛得越多，測試就越容易在重構時無謂地壞掉。 */
window.__playerTestHooks = {
  setInfo(x) { info = x; },
  get info() { return info; },
  get mode() { return mode; },
  set mode(x) { mode = x; },
  get playbackState() { return playbackState; },
  get metrics() { return metrics; },
  get cfg() { return cfg; },
  play, updatePlaybackState, watchdogTick, renderPlaybackStatus,
  saveProgress, syncNextEp, goNextEpisode,
  // 進度條／Seek（這次修正）。測試要能看見「現在在拖嗎、preview 到哪、
  // canonical 是多少」，否則只能從 DOM 反推，那會連「三個元素同一個來源」
  // 都驗不出來。
  syncBar, getCanonicalDuration, isValidDuration, clampSeekTarget, renderSeekPreview,
  // ABR 切階的回歸測試要能拿到 hls.js 實例：它得自己強制切階（不能等 ABR
  // 剛好決定要切），並且掛自己那一份 requestVideoFrameCallback 監控 ——
  // **驗「畫面有沒有倒退」不能只相信被測程式自己的計數。**
  hls() { return hlsObj; },
  get seekingDrag() { return seekingDrag; },
  get seekPreviewRatio() { return seekPreviewRatio; },
  get seekPreviewTime() { return seekPreviewTime; },
  get pendingSeekTarget() { return pendingSeekTarget; },
  get wasPlayingBeforeSeek() { return wasPlayingBeforeSeek; },
  // 觸控手勢與回饋（tests/player_gesture_test.js）。seekTo／seekBy 要掛出來，
  // 測試才驗得出「按鈕、鍵盤、手勢走的是同一支」，而不是只看最後的 currentTime。
  seekTo, seekBy, skipBy, togglePlay, replay, GESTURE,
  get fastHold() { return gesture.hold; },
  get feedbackMuted() { return feedbackMuted; },
  get replayGuard() { return replayGuard; },
};

boot();
