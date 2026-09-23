/* 播放器操作手感：中央回饋、觸控手勢、累加快轉、長按 2×、衝突、重播、Media Session。

     node tests/player_gesture_test.js

   這一支要釘住的是**「使用者操作 → 立即回饋 → 最少的播放器狀態變更」**：

     · 回饋看的是 <video> 的 play/pause 事件，但播放器自己造成的（換來源、
       播完、切下一集）不能閃圖示
     · 手勢的 seek 一律走 seekBy() → seekTo()，而連續雙擊只能 seek 一次
     · 單擊不可以跳秒（手機誤觸），長按放開要回到**原本的**速度

   所以跟 player_seek_test.js 一樣，幾乎每個案例都直接數「currentTime 被寫了
   幾次、寫了什麼」與「play()/pause() 被叫了幾次」，而不是只看最後的畫面。

   時間全部用假時鐘：手勢判定靠 pointer 事件的 timeStamp 與 setTimeout，
   用真的時間跑的話每個案例都要等好幾百毫秒，而且在慢機器上會不穩。 */
'use strict';
const { load, start } = require('./player_harness');

let OK = 0, FAIL = 0;
function check(name, cond, extra) {
  if (cond) { OK++; console.log('  PASS  ' + name); }
  else { FAIL++; console.log('  FAIL  ' + name + '  → ' + JSON.stringify(extra)); }
}
function head(t) { console.log('\n' + t); }

/* ---------------------------------------------------------------- 假時鐘 */

/** 換掉 sandbox 的 setTimeout／clearTimeout／Date.now。
 *  player.js 在呼叫當下才去全域找 setTimeout（[9b] 也是靠這一點攔計時器）。 */
function fakeClock(sb) {
  let now = 0, seq = 0;
  const q = new Map();
  sb.setTimeout = (fn, ms) => { const id = ++seq; q.set(id, { at: now + (ms || 0), fn }); return id; };
  sb.clearTimeout = id => { q.delete(id); };
  const base = Date.now();
  const RealDate = Date;
  sb.Date = class extends RealDate { static now() { return base + now; } };
  return {
    get now() { return now; },
    advance(ms) {
      const end = now + ms;
      for (;;) {
        let next = null;
        for (const [id, x] of q) if (x.at <= end && (!next || x.at < next[1].at)) next = [id, x];
        if (!next) break;
        q.delete(next[0]);
        now = next[1].at;
        next[1].fn();
      }
      now = end;
    },
  };
}

/* ---------------------------------------------------------------- 場景 */

/** 開一個「已經在播」的播放器。
 *  video stub 的 play()/pause() 改成會發事件、會記次數 —— 回饋是看事件的，
 *  harness 預設那一份只改屬性不發事件，測不到這一段。 */
function setup(opts = {}) {
  const ctx = load({ beforeRun: opts.beforeRun });
  const clock = fakeClock(ctx.sandbox);
  start(ctx, 'hls', Object.assign({ duration: 3600 }, opts.info || {}));
  const v = ctx.video;
  const calls = { play: 0, pause: 0 };
  let paused = true;
  Object.defineProperty(v, 'paused', { configurable: true, get: () => paused, set: x => { paused = x; } });
  v.play = () => { calls.play++; if (paused) { paused = false; v.dispatch('play'); } return Promise.resolve(); };
  v.pause = () => { calls.pause++; if (!paused) { paused = true; v.dispatch('pause'); } };
  v.readyState = 4;

  // currentTime 的每一次指派都記下來。**seek 次數是這一支的核心量測。**
  const seeks = [];
  let ct = 600;
  Object.defineProperty(v, 'currentTime', {
    configurable: true, get: () => ct,
    // 真的 <video> 被 seek 之後 ended 立刻變 false —— 重播那段靠這個
    set: x => { ct = x; seeks.push(x); v.ended = false; },
  });
  // 起播：hls.js 的 MANIFEST_PARSED 叫 play() → play（播放器自己的，不閃）→ playing
  v.play(); v.dispatch('playing');
  calls.play = 0;

  // 手機橫向大約的寬度。35%／30%／35% → 左 <140、中 140～260、右 >260
  const tap = ctx.get('tap');
  tap.getBoundingClientRect = () => ({ left: 0, top: 0, width: 400, height: 225 });
  clock.advance(1000);

  const P = (x, o = {}) => ({
    clientX: x, clientY: o.y ?? 100, pointerId: o.id ?? 1, pointerType: o.type ?? 'touch',
    timeStamp: clock.now, button: 0, target: tap, preventDefault() {},
  });
  const api = {
    ctx, clock, v, calls, seeks, tap, hooks: ctx.hooks, P,
    down(x, o) { tap.dispatch('pointerdown', P(x, o)); },
    up(x, o) { tap.dispatch('pointerup', P(x, o)); },
    /** 點一下：按下 → 手指停 hold 毫秒 → 放開 */
    tapAt(x, o = {}) { api.down(x, o); clock.advance(o.hold ?? 50); api.up(x, o); },
    /** 雙擊：兩下之間隔 gap 毫秒（放開到按下） */
    doubleTap(x, gap = 120, o = {}) { api.tapAt(x, o); clock.advance(gap); api.tapAt(o.x2 ?? x, o); },
    seeked() { v.dispatch('seeked'); },
    flashEl: ctx.get('flash'),
    flashing(kind) {
      const f = ctx.get('flash');
      return f.classList.contains('show') && (!kind || f.dataset.kind === kind);
    },
    idle: () => ctx.get('pl').classList.contains('idle'),
    settle() { clock.advance(2000); },
  };
  return api;
}

const G = s => s.hooks.GESTURE;
const LEFT = 60, CENTER = 200, RIGHT = 340;


// ============================================================ 1. 中央回饋
head('[1] 播放／暫停的中央回饋：看 <video> 的事件，不看是誰呼叫的');

{
  const s = setup();
  check('起播那一次 play（播放器自己叫的）不閃圖示', !s.flashing());
  s.hooks.togglePlay();
  check('暫停 → 中央閃 ❚❚', s.flashing('pause'), s.flashEl.dataset);
  s.clock.advance(800);
  check('約 0.7 秒後收掉', !s.flashing());
  s.hooks.togglePlay();
  check('播放 → 中央閃 ▶', s.flashing('play'), s.flashEl.dataset);

  // 快捷鍵也是同一份回饋 —— 不是只有 togglePlay() 那條路
  s.settle();
  s.ctx.env.document.dispatch('keydown', { key: ' ', target: { tagName: 'DIV' }, preventDefault() {} });
  check('空白鍵暫停 → 同樣閃 ❚❚', s.flashing('pause'));
}

{
  const s = setup();
  const doc = s.ctx.env.document;
  let created = 0;
  const realCreate = doc.createElement.bind(doc);
  doc.createElement = tag => { created++; return realCreate(tag); };
  const stageKids = s.ctx.get('stage').children.length;
  for (let i = 0; i < 12; i++) { s.hooks.togglePlay(); s.clock.advance(30); }
  check('連按 12 次：沒有新建任何元素', created === 0, created);
  check('#stage 底下的元素數量不變（沒有疊一堆圓圈）',
        s.ctx.get('stage').children.length === stageKids);
  check('而且是同一個 #flash 在重播動畫', s.flashing() && s.ctx.get('flash') === s.flashEl);
  s.clock.advance(800);
  check('最後一下之後照常收掉（計時器有被重排，不是第一下的那個）', !s.flashing());
}

{
  // 換畫質／換模式：play() 會重建來源，<video> 會再發一次 play —— 那不是使用者按的
  const s = setup();
  s.hooks.play('hls', 600);
  check('換來源之後回饋先關著', s.hooks.feedbackMuted === true);
  s.v.paused = true;
  s.v.play();
  check('換來源造成的 play 不閃 ▶', !s.flashing());
  s.v.pause();
  check('換來源期間的 pause（例如自動播放被擋）也不閃', !s.flashing());
  s.v.dispatch('playing');
  s.hooks.togglePlay();
  check('真的開始播之後，使用者的操作照常有回饋',
        s.flashing('pause') || s.flashing('play'));
}

{
  const s = setup();
  s.v.ended = true;
  s.v.pause();
  check('播完時瀏覽器發的 pause 不閃 ❚❚', !s.flashing());
}

{
  const s = setup({ info: { next_episode: { file_id: 2, url: '/player?file=2', label: 'E02' } } });
  s.hooks.goNextEpisode();
  check('切下一集之前的 pause 不閃 ❚❚', s.calls.pause === 1 && !s.flashing(), s.calls);
}


// ============================================================ 2~5. 單擊 / 雙擊
head('[3] 手機左右雙擊：-skip／+skip，走 seekBy()');

{
  const s = setup();
  s.doubleTap(RIGHT);
  check('右側雙擊：回饋立刻出現在右邊', s.ctx.get('seekR').classList.contains('show'));
  check('寫的是「10 秒」', s.ctx.get('seekRText').textContent === '10 秒',
        s.ctx.get('seekRText').textContent);
  check('左邊的回饋沒有出現', !s.ctx.get('seekL').classList.contains('show'));
  check('seek 還沒送出去（等看看有沒有下一下）', s.seeks.length === 0, s.seeks);
  s.clock.advance(G(s).seekCommitMs + 10);
  check('然後只 seek 一次：600 → 610', s.seeks.length === 1 && s.seeks[0] === 610, s.seeks);
  check('走的是同一套 seek：pendingSeekTarget 有掛上', s.hooks.pendingSeekTarget === 610);
  check('雙擊不是單擊：沒有播放／暫停', s.calls.play === 0 && s.calls.pause === 0, s.calls);
}

{
  const s = setup();
  s.doubleTap(LEFT);
  s.clock.advance(G(s).seekCommitMs + 10);
  check('左側雙擊 → 600 → 590', s.seeks.length === 1 && s.seeks[0] === 590, s.seeks);
  check('回饋在左邊，寫「10 秒」',
        s.ctx.get('seekL').classList.contains('show') && s.ctx.get('seekLText').textContent === '10 秒');
  s.clock.advance(1000);
  check('約 0.75 秒後淡出', !s.ctx.get('seekL').classList.contains('show'));
}

{
  const s = setup();
  s.hooks.cfg.skip = 5;
  s.doubleTap(RIGHT);
  s.clock.advance(G(s).seekCommitMs + 10);
  check('跳秒數跟著 cfg.skip（設成 5 秒 → +5）', s.seeks[0] === 605, s.seeks);
}

{
  const s = setup();
  s.doubleTap(CENTER);
  s.settle();
  check('中央雙擊不 seek', s.seeks.length === 0, s.seeks);
  check('中央雙擊 = 播放/暫停一次', s.calls.pause === 1 && s.calls.play === 0, s.calls);
}

head('[4] 單擊不會跳秒');

{
  const s = setup();
  s.tapAt(RIGHT);
  s.settle();
  check('右側單擊：一次 seek 都沒有', s.seeks.length === 0, s.seeks);
  check('控制列原本在 → 右側單擊收起控制列', s.idle());
  check('也沒有暫停', s.calls.pause === 0);
}

{
  const s = setup();
  s.ctx.get('pl').classList.add('idle');
  s.tapAt(CENTER);
  check('控制列藏著時的單擊：雙擊窗口內先不動（可能是雙擊）', s.idle());
  s.clock.advance(G(s).doubleMs + 10);
  check('窗口過了 → 叫出控制列', !s.idle());
  check('但不會順手暫停', s.calls.pause === 0 && s.calls.play === 0, s.calls);
  s.tapAt(CENTER);
  s.clock.advance(G(s).doubleMs + 10);
  check('控制列在的時候點中央 → 暫停', s.calls.pause === 1, s.calls);
}

{
  const s = setup();
  s.ctx.get('pl').classList.add('idle');
  s.doubleTap(RIGHT);
  s.settle();
  check('畫面乾淨地雙擊快轉：控制列不會被叫出來', s.idle());
}

head('[5] 雙擊判定：時間、距離、pointerType');

{
  const s = setup();
  s.doubleTap(RIGHT, G(s).doubleMs + 60);
  s.settle();
  check('兩下間隔超過窗口 → 不算雙擊、不 seek', s.seeks.length === 0, s.seeks);
}

{
  const s = setup();
  s.doubleTap(20, 120, { x2: 125 });     // 兩下都在左區，但相距 105px
  s.settle();
  check('同一區但距離太遠 → 不算雙擊', s.seeks.length === 0, s.seeks);
}

{
  const s = setup();
  s.doubleTap(LEFT, 120, { x2: RIGHT });  // 左邊一下、右邊一下
  s.settle();
  check('左邊一下、右邊一下 → 不算雙擊', s.seeks.length === 0, s.seeks);
}

{
  const s = setup();
  s.tapAt(RIGHT);
  s.clock.advance(100);
  s.down(RIGHT, { y: 100 });
  s.tap.dispatch('pointercancel', s.P(RIGHT));
  s.settle();
  check('第二下被瀏覽器中斷（pointercancel）→ 兩下都作廢，不補做單擊',
        s.seeks.length === 0 && !s.idle(), { seeks: s.seeks, idle: s.idle() });
}

{
  const s = setup();
  s.down(RIGHT, { type: 'mouse' }); s.clock.advance(40); s.up(RIGHT, { type: 'mouse' });
  s.clock.advance(80);
  s.down(RIGHT, { type: 'mouse' }); s.clock.advance(40); s.up(RIGHT, { type: 'mouse' });
  s.settle();
  check('滑鼠不走觸控手勢：快速點兩下右側不會 seek', s.seeks.length === 0, s.seeks);
  s.tap.onclick({});
  check('滑鼠 click → 照舊播放/暫停', s.calls.pause === 1, s.calls);
  s.tap.ondblclick({});
  check('滑鼠 dblclick → 照舊全螢幕',
        s.ctx.env.document.body.classList.contains('page-fs'));
}

{
  // 觸控之後瀏覽器補發的 click／dblclick 不能再切一次。舊版 iOS 的 click
  // 沒有 pointerType，所以要看 pointerdown 記下來的那一個。
  const s = setup();
  s.tapAt(CENTER);
  s.tap.onclick({});                     // 沒有 pointerType 的合成 click
  s.tap.ondblclick({});
  s.clock.advance(G(s).doubleMs + 10);
  check('觸控後的合成 click 不會多切一次播放', s.calls.pause === 1, s.calls);
  check('觸控後的合成 dblclick 不會進全螢幕',
        !s.ctx.env.document.body.classList.contains('page-fs'));
}


// ============================================================ 7. 累加
head('[7] 連續雙擊累加：UI 立刻 +10/+20/+30，seek 只有最後一次');

{
  const s = setup();
  s.doubleTap(RIGHT);
  check('第一次：10 秒', s.ctx.get('seekRText').textContent === '10 秒');
  s.clock.advance(150);
  s.doubleTap(RIGHT);
  check('第二次：20 秒', s.ctx.get('seekRText').textContent === '20 秒');
  s.clock.advance(150);
  s.doubleTap(RIGHT);
  check('第三次：30 秒', s.ctx.get('seekRText').textContent === '30 秒');
  check('三次雙擊期間 currentTime 一次都沒被寫', s.seeks.length === 0, s.seeks);
  s.clock.advance(G(s).seekCommitMs + 10);
  check('最後只有一次 seek，而且是 +30（不是 seek、seek、seek）',
        s.seeks.length === 1 && s.seeks[0] === 630, s.seeks);
  s.seeked();
  s.settle();
  check('之後沒有多出來的 seek', s.seeks.length === 1, s.seeks);
  check('沒配成對的那幾下沒有被當成單擊（控制列沒被收／叫）',
        s.calls.play === 0 && s.calls.pause === 0, s.calls);
}

{
  const s = setup();
  s.doubleTap(RIGHT); s.clock.advance(150); s.doubleTap(RIGHT);
  s.clock.advance(150);
  s.doubleTap(LEFT);
  check('換邊：右邊的 +20 先落地', s.seeks.length === 1 && s.seeks[0] === 620, s.seeks);
  check('左邊的回饋從 10 秒重新算', s.ctx.get('seekLText').textContent === '10 秒');
  s.clock.advance(G(s).seekCommitMs + 10);
  check('然後左邊的 -10 從 +20 那裡倒退（620 → 610）',
        s.seeks.length === 2 && s.seeks[1] === 610, s.seeks);
}

{
  const s = setup();
  s.doubleTap(RIGHT);
  s.hooks.play('hls', 600);                 // 還沒落地就換來源（例如自動 fallback）
  s.settle();
  check('換來源時，累加中的快轉一起丟掉（它是對著舊來源算的）', s.seeks.length === 0, s.seeks);
  check('回饋也收掉', !s.ctx.get('seekR').classList.contains('show'));
}


// ============================================================ 8. 長按
head('[8] 長按右側暫時 2×，放開恢復「原本的」速度');

function holdRight(s, ms = 500) { s.down(RIGHT); s.clock.advance(ms); }

{
  const s = setup();
  holdRight(s);
  check('按住 0.5 秒 → 2×', s.v.playbackRate === 2, s.v.playbackRate);
  check('畫面上出現 2× 提示', s.ctx.get('holdFb').classList.contains('show'));
  s.up(RIGHT);
  check('放開 → 回到 1×', s.v.playbackRate === 1, s.v.playbackRate);
  check('提示收掉', !s.ctx.get('holdFb').classList.contains('show'));
  s.settle();
  check('長按不是點擊：沒有暫停、沒有 seek、控制列沒被收',
        s.calls.pause === 0 && s.seeks.length === 0 && !s.idle(),
        { calls: s.calls, seeks: s.seeks });
}

{
  const s = setup();
  s.v.playbackRate = 1.25;
  holdRight(s);
  check('原本 1.25× → 長按變 2×', s.v.playbackRate === 2);
  s.up(RIGHT);
  check('放開回到 1.25×，不是 1×', s.v.playbackRate === 1.25, s.v.playbackRate);
}

for (const [label, end] of [
  ['pointercancel', s => s.tap.dispatch('pointercancel', s.P(RIGHT))],
  ['lostpointercapture', s => s.tap.dispatch('lostpointercapture', s.P(RIGHT))],
  ['visibilitychange（切走 App）', s => {
    s.ctx.env.document.visibilityState = 'hidden';
    s.ctx.env.document.dispatch('visibilitychange');
  }],
  ['fullscreenchange', s => s.ctx.env.document.dispatch('fullscreenchange')],
  ['webkitendfullscreen（iOS 原生全螢幕）', s => s.v.dispatch('webkitendfullscreen')],
]) {
  const s = setup();
  s.v.playbackRate = 1.25;
  holdRight(s);
  end(s);
  check(`${label} → 回到 1.25×`, s.v.playbackRate === 1.25 && !s.hooks.fastHold, s.v.playbackRate);
}

{
  const s = setup();
  s.hooks.togglePlay();                    // 先暫停
  s.calls.play = 0;
  s.down(RIGHT); s.clock.advance(700);
  check('暫停中長按：不進 2×', s.v.playbackRate === 1 && !s.hooks.fastHold);
  s.up(RIGHT);
  s.settle();
  check('放開也不會自己開始播（按太久不算單擊）', s.calls.play === 0 && s.v.paused, s.calls);
}

{
  const s = setup();
  s.down(LEFT); s.clock.advance(600); s.up(LEFT);
  check('左側長按沒有 2×', s.v.playbackRate === 1);
  s.down(RIGHT);
  s.tap.dispatch('pointermove', Object.assign(s.P(RIGHT + 30), {}));
  s.clock.advance(600);
  check('按下去就開始滑（不是長按）→ 不進 2×', s.v.playbackRate === 1);
  s.up(RIGHT + 30);
}

{
  const s = setup();
  s.down(RIGHT);
  s.tap.dispatch('pointerdown', s.P(CENTER, { id: 2 }));   // 第二根手指：捏合
  s.clock.advance(600);
  check('多指（捏合縮放）→ 整組作廢，不進 2×', s.v.playbackRate === 1);
}


// ============================================================ 9. 衝突
head('[9] 控制元件上的操作不會被畫面手勢接管');

/* 真的 DOM 上，控制列與下一集是 #tap 的兄弟節點，事件本來就傳不進 #tap
   （tests/player_gesture_page_test.py 用真的瀏覽器驗 hit-testing）。
   這裡驗的是**邏輯層**：這些操作不會在手勢控制器裡留下狀態，
   所以緊接著點一下畫面也不會被配成「雙擊」。 */
{
  const s = setup();
  const doc = s.ctx.env.document;
  const bar = s.ctx.get('barWrap');
  const P = s.P;
  // 點進度條（觸控）：那是進度條自己的 seek
  bar.dispatch('pointerdown', Object.assign(P(50), { target: bar }));
  doc.dispatch('pointerdown', Object.assign(P(50), { target: bar }));
  bar.dispatch('pointerup', Object.assign(P(50), { target: bar }));
  s.seeked();
  s.settle();
  check('進度條：只有進度條自己那一次 seek（50% = 1800）',
        s.seeks.length === 1 && s.seeks[0] === 1800, s.seeks);
  // commitScrub 會 v.play()（「原本在播就繼續播」）—— 那是 no-op，所以看狀態不看呼叫次數
  check('進度條：播放狀態沒被切換、沒有中央回饋、沒有 2×',
        !s.v.paused && s.calls.pause === 0 && !s.flashing() && s.v.playbackRate === 1, s.calls);
}

for (const id of ['btnSettings', 'btnSubs', 'btnFull', 'btnPip', 'nextEp', 'btnFwd']) {
  const s = setup();
  const el = s.ctx.get(id);
  const doc = s.ctx.env.document;
  const ev = Object.assign(s.P(RIGHT), { target: el });
  el.dispatch('pointerdown', ev); doc.dispatch('pointerdown', ev);
  s.clock.advance(50);
  el.dispatch('pointerup', ev);
  // 緊接著點一下畫面右側：不能跟上一下（按鈕）配成雙擊
  s.clock.advance(80);
  const before = s.seeks.length;
  s.tapAt(RIGHT);
  s.settle();
  check(`${id}：按鈕之後緊接著點畫面，不會被當成雙擊快轉`,
        s.seeks.length === before, s.seeks);
  check(`${id}：沒有觸發畫面的播放/暫停`, s.calls.pause === 0 && s.calls.play === 0, s.calls);
}

{
  const s = setup({ info: { next_episode: { file_id: 2, url: '/player?file=2' } } });
  s.ctx.get('panel').classList.add('show');
  s.tapAt(CENTER);
  s.clock.advance(G(s).doubleMs + 10);
  check('面板開著時點畫面 = 關面板，不兼做播放/暫停', s.calls.pause === 0, s.calls);
}


// ============================================================ 10. Seek 共用
head('[10] 按鈕、鍵盤、手勢走同一條 seek');

{
  const s = setup();
  s.ctx.get('btnFwd').onclick();
  check('按鈕：+10 → 610，掛上 pendingSeekTarget',
        s.seeks[0] === 610 && s.hooks.pendingSeekTarget === 610, s.seeks);
  s.seeked();
  s.ctx.env.document.dispatch('keydown', { key: 'ArrowLeft', target: { tagName: 'DIV' },
                                           preventDefault() {} });
  check('鍵盤：-10 → 600，同樣掛 pendingSeekTarget',
        s.seeks[1] === 600 && s.hooks.pendingSeekTarget === 600, s.seeks);
  s.seeked();
  check('seeked 之後 pending 清掉、seekingByUs 放掉', s.hooks.pendingSeekTarget === null);
  s.hooks.seekBy(9999, 'gesture');
  check('手勢一樣過 clamp：不會頂到 duration（不誤觸 ended）',
        s.seeks[2] < 3600 && s.seeks[2] > 3598, s.seeks);
}


// ============================================================ 12. 控制列
head('[12] 控制列：播放中才會自己收，暫停／seek／面板／播完都要留著');

{
  const s = setup();
  s.ctx.get('pl').classList.remove('idle');
  s.hooks.togglePlay();                    // 暫停 → pause 事件會 wake()
  s.clock.advance(10000);
  check('暫停中：10 秒後控制列還在', !s.idle());
  s.hooks.togglePlay();
  s.ctx.env.document.dispatch('pointermove', { pointerType: 'mouse', target: {} });
  s.clock.advance(3000);
  check('播放中：一段時間沒操作就收起來', s.idle());
}

{
  const s = setup();
  s.ctx.get('panel').classList.add('show');
  s.ctx.env.document.dispatch('pointermove', { pointerType: 'mouse', target: {} });
  s.clock.advance(6000);
  check('設定／字幕面板開著：不收', !s.idle());
  s.ctx.get('panel').classList.remove('show');
  s.clock.advance(3000);
  check('面板關掉之後會自己收（計時器是重排不是放棄）', s.idle());
}

{
  const s = setup();
  s.hooks.seekBy(30, 'button');
  s.ctx.env.document.dispatch('pointermove', { pointerType: 'mouse', target: {} });
  s.clock.advance(6000);
  check('seek 還沒落地：不收', !s.idle());
  s.seeked();
  s.clock.advance(3000);
  check('落地之後照常收', s.idle());
}


// ============================================================ 13~14. 播完與重播
head('[13][14] 播完：控制列留著、中央變重播、重播不會把「已看完」改壞');

{
  const s = setup();
  const beacons = [];
  s.ctx.sandbox.navigator.sendBeacon = (_u, b) => { beacons.push(JSON.parse(b._text)); return true; };
  // 播到片尾
  s.v.currentTime = 3600; s.seeks.length = 0;
  s.v.ended = true; s.v.pause(); s.v.dispatch('ended');
  check('播完：.pl 掛上 ended（中央常駐重播圖示、下一集變顯眼）',
        s.ctx.get('pl').classList.contains('ended'));
  check('播放鍵變成重播圖示', s.ctx.get('icPlay').innerHTML.includes('2.64'),
        s.ctx.get('icPlay').innerHTML.slice(0, 40));
  check('播完不閃一般的 ❚❚', !s.flashing());
  check('寫了一筆 finished=true', beacons.some(b => b.finished === true), beacons);
  s.clock.advance(10000);
  check('播完 10 秒後控制列還在', !s.idle());

  // 中央單擊 = 重播
  s.tapAt(CENTER);
  s.clock.advance(G(s).doubleMs + 10);
  check('點中央 → 回到 0 並開始播', s.seeks[0] === 0 && s.calls.play === 1,
        { seeks: s.seeks, calls: s.calls });
  check('閃的是 ↻ 不是 ▶', s.flashing('replay'), s.flashEl.dataset);
  check('ended 狀態解除', !s.ctx.get('pl').classList.contains('ended'));
  check('重播走 seekTo：落地之前 pendingSeekTarget = 0', s.hooks.pendingSeekTarget === 0);

  const n = beacons.length;
  s.seeked();
  s.v.currentTime = 9; s.clock.advance(9000); s.v.dispatch('timeupdate');
  s.v.pause();
  check('重播幾秒又停掉：不寫「看到 0:09、未完成」蓋掉 finished',
        !beacons.slice(n).some(b => b.finished === false), beacons.slice(n));
  s.v.play();
  s.v.currentTime = 31; s.clock.advance(9000); s.v.dispatch('timeupdate');
  check('真的重看超過 30 秒 → 才開始寫一般進度',
        beacons.slice(n).some(b => b.finished === false && b.position === 31), beacons.slice(n));
}

{
  const s = setup();
  s.v.currentTime = 3600; s.seeks.length = 0;
  s.v.ended = true; s.v.pause(); s.v.dispatch('ended');
  s.ctx.get('btnPlay').onclick();
  check('播完按播放鍵 = 重播（走同一支 replay）', s.seeks[0] === 0 && s.flashing('replay'), s.seeks);
}


// ============================================================ 15. Media Session
head('[15] Media Session 的每個 action 都接到同一套');

{
  const ms = { h: {}, setActionHandler(a, f) { if (a === 'seekto' && this.noSeekTo) throw new Error('x'); this.h[a] = f; } };
  const s = setup({ beforeRun: sb => { sb.navigator.mediaSession = ms; } });
  check('play／pause／seekbackward／seekforward／seekto 都有掛',
        ['play', 'pause', 'seekbackward', 'seekforward', 'seekto'].every(a => typeof ms.h[a] === 'function'),
        Object.keys(ms.h));
  ms.h.seekforward({});
  check('seekforward → +cfg.skip，走 seekTo（有 pending）',
        s.seeks[0] === 610 && s.hooks.pendingSeekTarget === 610, s.seeks);
  s.seeked();
  ms.h.seekbackward({ seekOffset: 5 });
  check('seekbackward 用系統給的 seekOffset（5 秒）', s.seeks[1] === 605, s.seeks);
  s.seeked();
  ms.h.seekto({ seekTime: 100 });
  check('seekto → 絕對位置 100', s.seeks[2] === 100, s.seeks);
  ms.h.pause({});
  check('pause → 暫停，而且有中央回饋', s.calls.pause === 1 && s.flashing('pause'));
  ms.h.play({});
  check('play → 播放，而且有中央回饋', s.calls.play === 1 && s.flashing('play'));
}

{
  const ms = { h: {}, noSeekTo: true,
               setActionHandler(a, f) { if (a === 'seekto' && this.noSeekTo) throw new Error('x'); this.h[a] = f; } };
  setup({ beforeRun: sb => { sb.navigator.mediaSession = ms; } });
  check('某個 action 不支援（丟例外）不會讓其他的掛不上',
        typeof ms.h.play === 'function' && typeof ms.h.seekforward === 'function');
}


// ============================================================ 11. 按壓回饋
head('[11] 控制元件的按壓回饋');

{
  const s = setup();
  const btn = s.ctx.get('btnPlay');
  s.ctx.get('pl').dispatch('pointerdown', { target: { closest: () => btn } });
  check('按下去立刻掛上 .pressed', btn.classList.contains('pressed'));
  s.clock.advance(200);
  check('約 150ms 後拿掉', !btn.classList.contains('pressed'));
  s.ctx.get('pl').dispatch('pointerdown', { target: { closest: () => null } });
  check('點在按鈕以外的地方沒事', true);
}


console.log('\n' + '='.repeat(50));
console.log(`通過 ${OK}，失敗 ${FAIL}`);
process.exit(FAIL ? 1 : 0);
