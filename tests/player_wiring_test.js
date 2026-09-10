/* 播放器的「接線」測試：證明 hls.js 的事件真的會把 UI 改掉。

     node tests/player_wiring_test.js

   playback_state_test.js 測的是推導（給定狀態該顯示什麼），這一支測的是
   **接線**（事件進來之後狀態有沒有真的被改、UI 有沒有真的被重畫）。
   純函式全對但沒有人呼叫它們，是這次修正最容易留下的漏洞。

   做法：用最小的 DOM stub ＋ 假的 hls.js 把 player.js 真的載進來跑。
   不開瀏覽器、不裝框架 —— player.js 只用到少數幾個 DOM API，
   stub 出來比拉一整套 jsdom 便宜得多，而且看得出它到底依賴了什麼。 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let OK = 0, FAIL = 0;
function check(name, cond, extra) {
  if (cond) { OK++; console.log('  PASS  ' + name); }
  else { FAIL++; console.log('  FAIL  ' + name + '  → ' + JSON.stringify(extra)); }
}
function head(t) { console.log('\n' + t); }

const ROOT = path.join(__dirname, '..');

// 這些 id 在 player.html 裡不存在，是 player.js 自己建的
const DYNAMIC = new Set(['dbg']);

/* ---------------------------------------------------------------- DOM stub */

function makeEl(id) {
  const el = {
    id, _text: '', innerHTML: '', className: '', title: '',
    style: { setProperty() {}, display: '' },
    dataset: {}, children: [],
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { on === undefined ? (this._s.has(c) ? this._s.delete(c) : this._s.add(c))
                                       : (on ? this._s.add(c) : this._s.delete(c)); },
      contains(c) { return this._s.has(c); },
    },
    _handlers: {},
    addEventListener(ev, fn) { (this._handlers[ev] ||= []).push(fn); },
    removeEventListener() {},
    dispatch(ev, d) { (this._handlers[ev] || []).forEach(fn => fn(d || {})); },
    appendChild(c) { this.children.push(c); return c; },
    // buildPanel() 會對面板裡的元素掛 onclick。回一個空殼比回 null 好 ——
    // 回 null 的話測試會炸在「掛事件」而不是它真正要驗的那件事上。
    querySelector() { return makeEl('stub'); }, querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, width: 100, top: 0, bottom: 0 }; },
    setPointerCapture() {}, releasePointerCapture() {},
    focus() {}, click() {},
  };
  Object.defineProperty(el, 'textContent', {
    get() { return this._text; }, set(v) { this._text = String(v); },
  });
  return el;
}

function buildEnv() {
  // els 只放「HTML 裡本來就有的」那些。**沒有預先放的 id 要回 null** ——
  // player.js 用 `if (!el) 建一個` 這種寫法來決定要不要建 debug 疊圖，
  // 什麼 id 都回一個空殼的話那條路永遠不會被走到（測不到它）。
  const els = {};
  const get = id => (els[id] ||= makeEl(id));
  const find = id => els[id] || null;

  // <video> stub：只實作 player.js 真的會碰到的那些
  const video = makeEl('v');
  Object.assign(video, {
    currentTime: 0, duration: 3600, paused: false, volume: 1, muted: false,
    playbackRate: 1, videoWidth: 0, videoHeight: 0,
    buffered: { length: 0, start: () => 0, end: () => 0 },
    _src: null,
    play: () => Promise.resolve(),
    pause() { this.paused = true; },
    load() {}, removeAttribute() { this._src = null; },
    canPlayType: () => '',
    requestPictureInPicture: () => Promise.resolve(),
  });
  Object.defineProperty(video, 'src', {
    get() { return this._src; }, set(v) { this._src = v; },
  });
  els['v'] = video;

  /** buffer 水位：讓測試可以直接指定「currentTime 往後還有幾秒」。 */
  video.setBuffer = (ahead) => {
    const t = video.currentTime;
    video.buffered = { length: 1, start: () => Math.max(0, t - 1), end: () => t + ahead };
  };

  const document = {
    _handlers: {},
    _els: els,
    querySelector(sel) {
      if (!sel.startsWith('#')) return makeEl(sel);
      const id = sel.slice(1);
      // 動態建立的元素（目前只有 debug 疊圖）不預先給，讓 player.js 自己建
      return DYNAMIC.has(id) ? find(id) : get(id);
    },
    querySelectorAll() { return []; },
    // player.js 建好之後會 appendChild 到 #stage 並設 id —— 那時候才登記進 els，
    // 下一次 $('#dbg') 才找得到它（真的 DOM 就是這個行為）。
    createElement(tag) {
      const el = makeEl(tag);
      Object.defineProperty(el, 'id', {
        get() { return el._id; },
        set(v) { el._id = v; if (v) els[v] = el; },
      });
      return el;
    },
    addEventListener(ev, fn) { (this._handlers[ev] ||= []).push(fn); },
    dispatch(ev, d) { (this._handlers[ev] || []).forEach(fn => fn(d || {})); },
    body: makeEl('body'),
    documentElement: makeEl('html'),
    fullscreenElement: null, webkitFullscreenElement: null,
    visibilityState: 'visible',
    exitPictureInPicture() {}, pictureInPictureElement: null,
    exitFullscreen() {},
  };

  const window = {
    document, location: { search: '?file=1&debugPlayer=1', href: '' },
    localStorage: { _d: {}, getItem(k) { return this._d[k] ?? null; },
                    setItem(k, v) { this._d[k] = v; }, removeItem(k) { delete this._d[k]; } },
    navigator: { connection: undefined, sendBeacon: () => true },
    screen: { width: 1280, height: 800, orientation: { lock: () => Promise.resolve() } },
    matchMedia: () => ({ matches: false }),
    _handlers: {},
    addEventListener(ev, fn) { (this._handlers[ev] ||= []).push(fn); },
    dispatch(ev, d) { (this._handlers[ev] || []).forEach(fn => fn(d || {})); },
    setTimeout, clearTimeout, setInterval, clearInterval,
    fetch: () => Promise.reject(new Error('測試不打網路')),
    URLSearchParams,
  };
  window.window = window;

  return { window, document, video, els, get };
}

/* ---------------------------------------------------------------- 假的 hls.js */

// 只要有 player.js 用到的那幾個事件名與屬性。刻意不模擬 hls.js 的內部行為 ——
// 這一支要測的是「事件進來之後我們怎麼反應」，不是 hls.js 對不對。
const Events = {
  MANIFEST_PARSED: 'hlsManifestParsed', LEVEL_SWITCHING: 'hlsLevelSwitching',
  LEVEL_SWITCHED: 'hlsLevelSwitched', FRAG_LOADING: 'hlsFragLoading',
  FRAG_LOADED: 'hlsFragLoaded', FRAG_BUFFERED: 'hlsFragBuffered', ERROR: 'hlsError',
};

function makeFakeHls() {
  const instances = [];
  class FakeHls {
    constructor(cfg) {
      this.config = cfg;
      this._h = {};
      this.levels = [];
      this.currentLevel = -1;
      this.autoLevelCapping = -1;
      this.autoLevelEnabled = true;
      this.bandwidthEstimate = 0;
      this.capLevelToPlayerSize = cfg.capLevelToPlayerSize;
      this.destroyed = false;
      instances.push(this);
    }
    on(ev, fn) { (this._h[ev] ||= []).push(fn); }
    emit(ev, d) { (this._h[ev] || []).forEach(fn => fn(ev, d)); }
    loadSource() {} attachMedia() {} startLoad() {} stopLoad() {} detachMedia() {}
    destroy() { this.destroyed = true; }
    recoverMediaError() {} swapAudioCodec() {}
  }
  FakeHls.isSupported = () => true;
  FakeHls.Events = Events;
  FakeHls.ErrorTypes = { NETWORK_ERROR: 'networkError', MEDIA_ERROR: 'mediaError' };
  return { FakeHls, instances };
}

/* ---------------------------------------------------------------- 載入 player.js */

function load(opts) {
  const env = buildEnv();
  if (opts && opts.search) env.window.location.search = opts.search;
  const { FakeHls, instances } = makeFakeHls();
  env.window.Hls = FakeHls;

  const sandbox = Object.assign(env.window, {
    console: { log() {}, info() {}, warn() {}, error() {} },
    globalThis: env.window,
  });
  vm.createContext(sandbox);

  const psSrc = fs.readFileSync(path.join(ROOT, 'app/static/playback-state.js'), 'utf8');
  vm.runInContext(psSrc, sandbox);

  let pjSrc = fs.readFileSync(path.join(ROOT, 'app/static/player.js'), 'utf8');
  // boot() 會打網路。測試要的是同步的接線，不是啟動流程 —— 把最後那一行拿掉，
  // 需要的初始狀態由測試自己餵（比讓它去 fetch 一個假伺服器可靠得多）。
  pjSrc = pjSrc.replace(/\nboot\(\);\s*$/, '\n');
  vm.runInContext(pjSrc, sandbox);

  const hooks = sandbox.__playerTestHooks;
  if (!hooks) throw new Error('player.js 沒有掛上 __playerTestHooks');
  return { sandbox, hooks, env, instances, video: env.video, get: env.get };
}

/** 餵一份 info 進去並開始播（相當於 boot() 拿到 /api/play 之後那一段）。 */
function start(ctx, which, infoOver) {
  ctx.hooks.setInfo(Object.assign({
    file_id: 1, title: 't', mode: which, duration: 3600, resume: 0,
    width: 1920, height: 804,
    direct_url: '/api/stream/1', hls_url: '/api/hls/1/master.m3u8',
    remote: true, direct_advised: true,
    quality: { height: 720, bitrate_kbps: 2800,
               source: { width: 1920, height: 804, label: '1080p' } },
    audio_tracks: [], subtitles: [],
  }, infoOver || {}));
  ctx.hooks.play(which);
  return ctx.instances[ctx.instances.length - 1];
}

const LEVELS = [
  { width: 640, height: 268, bitrate: 700_000 },
  { width: 854, height: 358, bitrate: 1_260_000 },
  { width: 1280, height: 536, bitrate: 2_800_000 },
];

const tagText = ctx => ctx.get('tags').innerHTML;


// ============================================================ A / D / E
head('[A][D][E] LEVEL_SWITCHED 進來之後，右上角要立刻變成新的那一階');

{
  const ctx = load();
  const h = start(ctx, 'hls');
  check('開播當下還沒有實際畫質 → 只講「自動」，不要拿片源的 1080p 頂替',
        tagText(ctx).includes('自動') && !tagText(ctx).includes('1080p'), tagText(ctx));

  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  h.currentLevel = 2;
  h.emit(Events.LEVEL_SWITCHED, { level: 2 });
  check('[D] 切到 720p 之後右上角是「自動 · 720p · 1280×536」',
        tagText(ctx).includes('自動 · 720p · 1280×536'), tagText(ctx));

  h.currentLevel = 1;
  h.emit(Events.LEVEL_SWITCHED, { level: 1 });
  check('[E] ABR 降到 480p → 右上角立刻變 480p',
        tagText(ctx).includes('480p · 854×358'), tagText(ctx));
  check('[E] 而且舊的 720p 不留在畫面上', !tagText(ctx).includes('720p'), tagText(ctx));
  check('[E] 切換原因記成 abr-down',
        ctx.hooks.playbackState.lastSwitchReason === 'abr-down',
        ctx.hooks.playbackState.lastSwitchReason);

  h.currentLevel = 2;
  h.emit(Events.LEVEL_SWITCHED, { level: 2 });
  check('[E] 網路恢復升回 720p，UI 同步更新',
        tagText(ctx).includes('720p · 1280×536'), tagText(ctx));
  check('[E] 升階記成 abr-up',
        ctx.hooks.playbackState.lastSwitchReason === 'abr-up');
}


// ============================================================ F
head('[F] 手動選 1080p 上限、實際只播得動 720p');

{
  const ctx = load();
  ctx.hooks.cfg.quality = 1080;
  const h = start(ctx, 'hls');
  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  h.currentLevel = 2;
  h.emit(Events.LEVEL_SWITCHED, { level: 2 });
  const txt = tagText(ctx);
  check('右上角同時講得出「上限 1080p」與「目前 720p」',
        txt.includes('上限 1080p') && txt.includes('目前 720p'), txt);
}


// ============================================================ C
head('[C] 切換畫質之後不可以把舊的階留在 UI 上');

{
  const ctx = load();
  const h = start(ctx, 'hls');
  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  h.currentLevel = 2;
  h.emit(Events.LEVEL_SWITCHED, { level: 2 });
  check('先確認畫面上有 720p', tagText(ctx).includes('720p'), tagText(ctx));

  // 相當於 switchQuality() 拿到新的 info 之後那一步
  ctx.hooks.cfg.quality = 480;
  ctx.hooks.play('hls', 100);
  check('重新 play 之後舊的 720p 立刻不見（新狀態是空的）',
        !tagText(ctx).includes('720p'), tagText(ctx));
  check('而且改成講新的選擇（480p 上限）',
        tagText(ctx).includes('480p 上限'), tagText(ctx));
  check('actualResolution 被清乾淨',
        ctx.hooks.playbackState.actualResolution === null);
  check('level 索引回到 -1（0 是一個真的存在的階，不能拿來當「沒有」）',
        ctx.hooks.playbackState.actualLevelIndex === -1);
}


// ============================================================ I
head('[I] 切換之後舊實例的事件不可以再改到 UI');

{
  const ctx = load();
  const old = start(ctx, 'hls');
  old.levels = LEVELS;
  old.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  old.currentLevel = 2;
  old.emit(Events.LEVEL_SWITCHED, { level: 2 });

  ctx.hooks.cfg.quality = 480;
  ctx.hooks.play('hls', 100);          // 換一代
  const before = tagText(ctx);

  // 舊實例排隊中的 callback 現在才跑到（真的會發生 —— hls.js destroy 之後
  // 仍會送事件進來，player.js 原本就為這件事留過註解）
  old.emit(Events.LEVEL_SWITCHED, { level: 2 });
  old.emit(Events.FRAG_LOADED, { frag: { duration: 6 },
                                 stats: { loading: { start: 0, first: 0, end: 9000 },
                                          total: 3_000_000 } });
  check('舊實例的 LEVEL_SWITCHED 被 generation 閘擋掉，畫面沒有被改回去',
        tagText(ctx) === before, [before, tagText(ctx)]);
  check('舊實例的 FRAG_LOADED 也不會污染新的量測',
        ctx.hooks.playbackState.lastFragSize === 0,
        ctx.hooks.playbackState.lastFragSize);

  // 換到 direct 之後，舊的 hls 事件一樣不能動它
  ctx.hooks.play('direct');
  const dtxt = tagText(ctx);
  old.emit(Events.LEVEL_SWITCHED, { level: 0 });
  check('切到 direct 之後舊的 hls 事件也擋得住',
        tagText(ctx) === dtxt, [dtxt, tagText(ctx)]);
}


// ============================================================ G / H
head('[G][H] Direct → HLS：保留播放位置，而且只切一次');

{
  const ctx = load();
  start(ctx, 'direct');
  check('direct 的右上角講「直接串流 · 原檔 1080p」',
        tagText(ctx).includes('直接串流') && tagText(ctx).includes('原檔 1080p'),
        tagText(ctx));

  ctx.video.currentTime = 2535;          // 00:42:15
  ctx.video.setBuffer(0.2);
  // 反覆緩衝：40 秒內三次
  for (let i = 0; i < 3; i++) ctx.video.dispatch('waiting');

  check('[G] 已經切到 hls', ctx.hooks.mode === 'hls', ctx.hooks.mode);
  const inst = ctx.instances[ctx.instances.length - 1];
  check('[G] **播放位置保留**：startPosition 就是卡住的那個時間，不是 0',
        Math.abs(inst.config.startPosition - 2535) < 1, inst.config.startPosition);
  check('[G] 切換原因記成 direct-fallback',
        ctx.hooks.playbackState.lastSwitchReason === 'direct-fallback',
        ctx.hooks.playbackState.lastSwitchReason);

  // H：舊的 direct 事件在切換之後才飄進來
  const n = ctx.instances.length;
  ctx.hooks.mode = 'direct';           // 假裝舊事件以為自己還在 direct
  for (let i = 0; i < 5; i++) ctx.video.dispatch('waiting');
  check('[H] 不會被舊的 direct 事件觸發第二次切換',
        ctx.instances.length === n, [n, ctx.instances.length]);
}

{
  // direct 的 error 路徑也要保留位置、也只能切一次
  const ctx = load();
  start(ctx, 'direct');
  ctx.video.currentTime = 1234;
  ctx.video.dispatch('error');
  check('[G] direct 播放失敗改用轉碼時一樣保留位置',
        Math.abs(ctx.instances[ctx.instances.length - 1].config.startPosition - 1234) < 1,
        ctx.instances[ctx.instances.length - 1].config.startPosition);
  const n = ctx.instances.length;
  ctx.hooks.mode = 'direct';
  ctx.video.dispatch('error');
  check('[H] 第二次 error 不會再切一次（改成講「請下載觀看」）',
        ctx.instances.length === n, [n, ctx.instances.length]);
}

{
  // 伺服器說這條鏈路餵不動時門檻要收緊
  const ctx = load();
  start(ctx, 'direct', { direct_advised: false });
  ctx.video.currentTime = 100;
  ctx.video.setBuffer(0.1);
  ctx.video.dispatch('waiting');
  ctx.video.dispatch('waiting');
  check('伺服器判定不適合 direct 時，兩次卡頓就切（一般是三次）',
        ctx.hooks.mode === 'hls', ctx.hooks.mode);
}


// ============================================================ J / K 看門狗
head('[J][K] 卡頓降階與交還控制權');

{
  const ctx = load();
  const h = start(ctx, 'hls');
  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  h.currentLevel = 2;
  h.emit(Events.LEVEL_SWITCHED, { level: 2 });

  // 開播策略：遠端 ＋ 手機（matchMedia 回 false，但 screen 小）
  // 這一組是桌機，所以不該被壓
  check('遠端桌機開播不壓階（交給 ABR）', h.autoLevelCapping === -1, h.autoLevelCapping);

  ctx.video.currentTime = 60;
  ctx.video.setBuffer(0.5);
  ctx.video.dispatch('waiting');
  ctx.video.dispatch('waiting');
  check('[J] 30 秒內卡兩次 → 壓上限（壓的是 autoLevelCapping，不是鎖 currentLevel）',
        h.autoLevelCapping >= 0 && h.autoLevelCapping < 2, h.autoLevelCapping);
  check('[J] currentLevel 沒有被鎖死（鎖了的話網路恢復也回不來）',
        h.currentLevel === 2, h.currentLevel);
  check('[J] 切換原因記成 stall-down',
        ctx.hooks.playbackState.lastSwitchReason === 'stall-down');

  // K：立刻喊恢復不該被理會（hysteresis）
  ctx.video.setBuffer(20);
  h.bandwidthEstimate = 20_000_000;
  ctx.hooks.watchdogTick();
  check('[K] 才剛降階就說網路好了 → 不理它（不然就是每幾秒震盪一次）',
        h.autoLevelCapping >= 0, h.autoLevelCapping);

  // 把時間往回撥，模擬「已經穩定 60 秒」
  ctx.hooks.metrics.cappedAt = Date.now() - 60_000;
  ctx.hooks.metrics.stallTimes = [];
  ctx.hooks.watchdogTick();
  check('[K] 穩定 60 秒 ＋ buffer 夠 ＋ 頻寬有餘裕 → 交還 Auto ABR',
        h.autoLevelCapping === -1, h.autoLevelCapping);
  check('[K] 交還時記成 recover',
        ctx.hooks.playbackState.lastSwitchReason === 'recover');
}

{
  // **一段卡在下載中永遠不回來**：這是最該降階、卻最容易被漏掉的情境 ——
  // 只看 FRAG_LOADED 的話 lastFragLoadMs 會停在上一段的好成績上。
  const ctx = load();
  const h = start(ctx, 'hls');
  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  h.currentLevel = 2;
  h.emit(Events.LEVEL_SWITCHED, { level: 2 });
  // 先來一段很順的（6 秒的段只花 1 秒）
  h.emit(Events.FRAG_LOADED, { frag: { duration: 6 },
    stats: { loading: { start: 0, first: 0, end: 1000 }, total: 1_000_000 } });
  ctx.video.setBuffer(20);
  ctx.hooks.watchdogTick();
  check('[J] 上一段很順 → 不動', h.autoLevelCapping === -1, h.autoLevelCapping);

  // 下一段開始下載，然後就再也沒有回來
  h.emit(Events.FRAG_LOADING, {});
  ctx.hooks.metrics.fragLoadingSince = Date.now() - 9000;   // 已經等了 9 秒
  ctx.hooks.watchdogTick();
  check('[J] **下載中的那一段等了 9 秒（段長 6 秒）→ 要降階**，'
        + '不能因為上一段很順就以為一切正常',
        h.autoLevelCapping >= 0 && h.autoLevelCapping < 2, h.autoLevelCapping);
}

{
  // 手動挑了上限的話看門狗不插手（避免跟 hls.js 的 ABR 疊起來）
  const ctx = load();
  ctx.hooks.cfg.quality = 720;
  const h = start(ctx, 'hls');
  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  h.currentLevel = 2;
  h.emit(Events.LEVEL_SWITCHED, { level: 2 });
  ctx.video.setBuffer(0.2);
  for (let i = 0; i < 4; i++) ctx.video.dispatch('waiting');
  check('[J] 手動模式下不主動壓階（上限底下的階交給 hls.js）',
        h.autoLevelCapping === -1, h.autoLevelCapping);
}


// ============================================================ 手機開播
head('[J 3] 手機遠端開播要先壓一階');

{
  const ctx = load();
  // 讓 isMobile() 回 true：coarse pointer ＋ 沒有 hover
  ctx.sandbox.matchMedia = q => ({ matches: /coarse|hover: none/.test(q) });
  const h = start(ctx, 'hls');
  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  check('手機遠端開播壓到倒數第二階（不賭最高畫質然後卡十幾秒）',
        h.autoLevelCapping === 1, h.autoLevelCapping);
  check('hls.js 的設定也照手機調（起始頻寬估計保守）',
        h.config.abrEwmaDefaultEstimate === 1_200_000, h.config.abrEwmaDefaultEstimate);
  check('而且開了 capLevelToPlayerSize（390px 寬不該去拉 4K）',
        h.config.capLevelToPlayerSize === true);
}

{
  const ctx = load();
  ctx.sandbox.matchMedia = q => ({ matches: /coarse|hover: none/.test(q) });
  const h = start(ctx, 'hls', { remote: false });
  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  check('區網手機不壓（鏈路夠寬，壓了只是白白畫質差）',
        h.autoLevelCapping === -1, h.autoLevelCapping);
}


// ============================================================ 量測與 debug
head('[7][8] fragment 量測與 debug 疊圖');

{
  const ctx = load();
  const h = start(ctx, 'hls');
  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  h.emit(Events.FRAG_LOADED, {
    frag: { duration: 6 },
    stats: { loading: { start: 0, first: 100, end: 3100 }, total: 1_500_000 },
  });
  const st = ctx.hooks.playbackState;
  check('[7] fragment 大小記得下來', st.lastFragSize === 1_500_000, st.lastFragSize);
  check('[7] fragment 下載時間記得下來（3000ms）', st.lastFragLoadMs === 3000, st.lastFragLoadMs);
  check('[7] 吞吐算得出來（1.5MB / 3s = 4000 kbps）',
        st.lastFragThroughputKbps === 4000, st.lastFragThroughputKbps);

  h.bandwidthEstimate = 5_000_000;
  ctx.video.setBuffer(12);
  h.emit(Events.FRAG_BUFFERED, {});
  check('[7] 頻寬估計跟得上', ctx.hooks.playbackState.bandwidthEstimate === 5_000_000);
  check('[7] buffer 秒數跟得上', ctx.hooks.playbackState.bufferSeconds === 12);

  const dbg = ctx.get('stage').children.find(c => c.id === 'dbg');
  check('[8] ?debugPlayer=1 時疊圖會出現', !!dbg, ctx.get('stage').children.map(c => c.id));
  check('[8] 疊圖裡有 Bandwidth est 這一項', dbg && dbg.innerHTML.includes('Bandwidth est'));
}

{
  const ctx = load({ search: '?file=1' });      // 沒有 debugPlayer
  const h = start(ctx, 'hls');
  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  const dbg = ctx.get('stage').children.find(c => c.id === 'dbg');
  check('[8] 正式模式下整排 debug 資訊不塞在畫面上', !dbg,
        ctx.get('stage').children.map(c => c.id));
  check('[8] 但右上角那一行照常有（那是給一般使用者看的）',
        tagText(ctx).includes('自動'), tagText(ctx));
}


// ============================================================ buffer 計算
head('[J] buffer 水位要看「涵蓋 currentTime 的那一段」');

{
  const ctx = load();
  start(ctx, 'hls');
  // 拖過進度條之後 buffered 會有好幾段不連續的區間
  ctx.video.currentTime = 1000;
  ctx.video.buffered = {
    length: 2,
    start: i => [0, 995][i],
    end: i => [60, 1010][i],
  };
  ctx.hooks.updatePlaybackState({});
  check('**不能拿 buffered.end(0)**（那是播放頭根本不在的地方）',
        ctx.hooks.playbackState.bufferSeconds === 10,
        ctx.hooks.playbackState.bufferSeconds);

  ctx.video.currentTime = 500;           // 落在兩段之間，沒有任何緩衝
  ctx.hooks.updatePlaybackState({});
  check('播放頭落在沒有緩衝的地方 → 0 秒',
        ctx.hooks.playbackState.bufferSeconds === 0,
        ctx.hooks.playbackState.bufferSeconds);
}


console.log('\n' + '='.repeat(50));
console.log(`通過 ${OK}，失敗 ${FAIL}`);
process.exit(FAIL ? 1 : 0);
