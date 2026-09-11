/* 播放器測試共用的 DOM / hls.js stub。

   player_wiring_test.js 與 player_seek_test.js 都把 player.js 整個載進來跑，
   而「把 player.js 載得起來」需要的那一套 stub 有兩百多行 —— 複製一份到
   第二個測試檔的話，兩邊會慢慢分岔（改了一邊忘了另一邊），而分岔的症狀是
   「同一段程式在 A 檔測得到、在 B 檔測不到」，非常難查。

   刻意維持最小的 stub，不引入 jsdom：player.js 只用到少數幾個 DOM API，
   stub 出來比拉一整套框架便宜，而且看得出它到底依賴了什麼。 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

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
    // 屬性與 hidden：player.js 用 el.hidden 切換「下一集」的顯示，
    // 用 setAttribute('aria-label') 讓報讀器念得出是哪一集。
    // stub 少實作這兩個的話，那段接線會在測試裡直接炸掉（而不是回報失敗）。
    _attrs: {},
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return this._attrs[k] ?? null; },
    hidden: false, disabled: false,
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
    // seekable 跟 buffered 是兩回事：前者是「跳得到哪」，後者是「已經下載到哪」。
    // 測試要驗「不可以把 seekable.end() 當成片長」，所以兩個都要有。
    seekable: { length: 0, start: () => 0, end: () => 0 },
    ended: false,
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
  // 「下一集」的按鈕與標籤在 player.html 裡本來就有（hidden 只是初始狀態，
  // 不是「不存在」）。不預先登記的話 find() 會回 null，而 player.js 用的是
  // `$('#nextEp')?.addEventListener(...)` —— 那條線會靜靜地接不上，測試卻全綠。
  els['nextEp'] = Object.assign(makeEl('nextEp'), { hidden: true });
  els['nextEpLabel'] = makeEl('nextEpLabel');

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
    // Date 要明確放進 sandbox：vm context 預設有自己的一份內建物件，
    // 不放的話測試改不到 player.js 看到的時鐘，而進度節流正是靠時鐘判斷的。
    Date,
    // sendBeacon 要包一個 Blob。node 的 vm sandbox 沒有 Blob，
    // 而 saveProgress 每次都會用到 —— 少了它整個進度儲存的接線測不到。
    Blob: class { constructor(parts, opt) { this._text = String(parts[0]);
                                            this.type = (opt || {}).type; }
                  text() { return Promise.resolve(this._text); } },
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


module.exports = { makeEl, buildEnv, makeFakeHls, load, start, Events, ROOT };
