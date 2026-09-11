/* 進度條：Scrubbing、Seek、片長、觸控體驗的回歸測試。

     node tests/player_seek_test.js

   對應規格的 A～R 案。這一支要釘住的是**三個症狀的根因**：

     · 拖曳中連續寫 v.currentTime（HLS 的 seek storm ＋ 拖起來很卡）
     · 拿會縮水的 v.duration 當比例尺（總時間變短、拖到中間卻跳片尾）
     · 圓點靠 syncBar() 從 currentTime 反推（跟不上手指、來回抖）

   所以下面幾乎每個案例都直接數「currentTime 被指派了幾次」，
   而不是只看最後的畫面對不對 —— 畫面對但中間發了三十次 seek，
   正是修這個 bug 之前的狀態。 */
'use strict';
const { load, start, Events } = require('./player_harness');

let OK = 0, FAIL = 0;
function check(name, cond, extra) {
  if (cond) { OK++; console.log('  PASS  ' + name); }
  else { FAIL++; console.log('  FAIL  ' + name + '  → ' + JSON.stringify(extra)); }
}
function head(t) { console.log('\n' + t); }

const MIN120 = 7200;      // 2 小時

/** 監看 v.currentTime 的每一次「指派」。
 *
 *  **這是這一整支測試的核心工具。**規格的硬性要求是「一次拖曳只能產生
 *  一次真正的 Seek」，而那件事只有在指派層面看得到 —— 從畫面或從最後的
 *  currentTime 值都看不出中間被寫了幾次。 */
function watchSeeks(video) {
  const writes = [];
  let val = video.currentTime;
  Object.defineProperty(video, 'currentTime', {
    configurable: true,
    get() { return val; },
    set(x) { val = x; writes.push(x); },
  });
  return writes;
}

/** 進度條的三個元素現在畫在哪（百分比字串 → 數字）。 */
function barState(ctx) {
  const pct = s => parseFloat(String(s || '0').replace('%', ''));
  return {
    play: pct(ctx.get('barPlay').style.width),
    knob: pct(ctx.get('barKnob').style.left),
    tip: pct(ctx.get('barTip').style.left),
    tipText: ctx.get('barTip').textContent,
    cur: ctx.get('tCur').textContent,
    dur: ctx.get('tDur').textContent,
  };
}

/** 造一個 pointer 事件。bar 的 getBoundingClientRect 是 left:0 width:100，
 *  所以 clientX 直接就是百分比。 */
const pev = (x, id = 1) => ({ clientX: x, pointerId: id, button: 0,
                              preventDefault() {}, pointerType: 'touch' });

function drag(ctx, xs) {
  const bar = ctx.get('barWrap');
  bar.dispatch('pointerdown', pev(xs[0]));
  for (const x of xs.slice(1)) bar.dispatch('pointermove', pev(x));
  return bar;
}


// ============================================================ A
head('[A] 120 分鐘的片拖到 50%，preview 要是約 60 分鐘');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  drag(ctx, [10, 30, 50]);
  const t = ctx.hooks.seekPreviewTime;
  check('preview 時間 = 3600 秒（60 分）', Math.abs(t - 3600) < 1, t);
  check('tooltip 寫的是 1:00:00', barState(ctx).tipText === '1:00:00', barState(ctx).tipText);
  check('總時間欄寫的是 2:00:00', barState(ctx).dur === '2:00:00', barState(ctx).dur);
}


// ============================================================ B / C
head('[B][C] 拖曳全程不可以動 currentTime，只有 pointerup 才 commit 一次');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const writes = watchSeeks(ctx.video);
  const bar = drag(ctx, [0, 10, 20, 30, 40, 50]);
  check('[B] 0%→10%→20%→50% 的移動過程中，currentTime 一次都沒被寫',
        writes.length === 0, writes);
  check('[B] 但畫面已經跟著走到 50%', barState(ctx).knob === 50, barState(ctx));

  bar.dispatch('pointerup', pev(50));
  check('[C] 整次拖曳只 commit 一次 seek', writes.length === 1, writes);
  check('[C] 而且跳到的是 50% 對應的 3600 秒',
        Math.abs(writes[0] - 3600) < 1, writes[0]);
}

{
  // 大量 pointermove（相當於手機上手指移動一秒）也只能有一次 seek
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const writes = watchSeeks(ctx.video);
  const xs = [];
  for (let i = 0; i <= 80; i++) xs.push(i);
  const bar = drag(ctx, xs);
  check('[C] 81 次 pointermove → 0 次 currentTime 指派（原本會是 81 次）',
        writes.length === 0, writes.length);
  bar.dispatch('pointerup', pev(80));
  check('[C] 放開之後總共只有 1 次', writes.length === 1, writes.length);
}

{
  // 只點一下不拖：一樣要 seek 過去
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const writes = watchSeeks(ctx.video);
  const bar = ctx.get('barWrap');
  bar.dispatch('pointerdown', pev(25));
  bar.dispatch('pointerup', pev(25));
  check('[C] 只點一下（沒有 pointermove）也會 seek 到那個位置',
        writes.length === 1 && Math.abs(writes[0] - 1800) < 1, writes);
}


// ============================================================ D
head('[D] 拖曳中 knob / play bar / tooltip 必須同一個 ratio');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  for (const x of [13, 47, 91]) {
    drag(ctx, [x]);
    const b = barState(ctx);
    check(`拖到 ${x}%：三個元素位置一致`,
          b.play === x && b.knob === x && b.tip === x, b);
  }
}


// ============================================================ E
head('[E] 拖曳期間收到 timeupdate，不可以把 knob 拉回實際 currentTime');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  ctx.video.currentTime = 1440;        // 實際播放頭在 20%
  const bar = drag(ctx, [60]);
  check('先確認手指在 60%', barState(ctx).knob === 60, barState(ctx));

  ctx.video.dispatch('timeupdate');
  check('timeupdate 進來之後 knob 仍然在 60%（原本會被拉回 20%）',
        barState(ctx).knob === 60, barState(ctx));
  ctx.video.dispatch('progress');
  check('progress 進來也不會把它拉走', barState(ctx).knob === 60, barState(ctx));

  bar.dispatch('pointermove', pev(61));
  check('再移動一格是 61%，不是從 20% 重新跳（不會抖）',
        barState(ctx).knob === 61, barState(ctx));
}


// ============================================================ F / H
head('[F][H] API 說 9000 秒、media 暫時變 1800 秒 → UI 仍然是 9000 秒');

{
  const ctx = load();
  start(ctx, 'hls', { duration: 9000 });
  ctx.video.duration = 9000;
  ctx.video.dispatch('timeupdate');
  check('一開始總時間是 2:30:00', barState(ctx).dur === '2:30:00', barState(ctx).dur);

  // HLS 在 seek／換階之後 MediaSource 的 duration 縮水
  ctx.video.duration = 1800;
  ctx.video.dispatch('timeupdate');
  check('[F] media 掉到 1800 之後，總時間**不變**（原本會變成 30:00）',
        barState(ctx).dur === '2:30:00', barState(ctx).dur);
  check('[F] canonical duration 仍然是 9000',
        ctx.hooks.getCanonicalDuration() === 9000, ctx.hooks.getCanonicalDuration());

  // 這時候拖到 50%
  const writes = watchSeeks(ctx.video);
  const bar = drag(ctx, [50]);
  check('[H] 拖到 50% 的 preview 是 4500 秒，不是 900',
        Math.abs(ctx.hooks.seekPreviewTime - 4500) < 1, ctx.hooks.seekPreviewTime);
  bar.dispatch('pointerup', pev(50));
  check('[H] 真正 seek 到 4500（不會因為 media duration 縮水而跳到片尾）',
        Math.abs(writes[0] - 4500) < 1, writes[0]);
}


// ============================================================ G
head('[G] video.duration 是 NaN / Infinity 不可以影響進度條');

{
  for (const bad of [NaN, Infinity, 0, -1, undefined]) {
    const ctx = load();
    start(ctx, 'hls', { duration: 7200 });
    ctx.video.duration = bad;
    ctx.video.dispatch('timeupdate');
    check(`media duration = ${String(bad)} 時，canonical 仍是 7200`,
          ctx.hooks.getCanonicalDuration() === 7200, ctx.hooks.getCanonicalDuration());
    check(`media duration = ${String(bad)} 時，總時間欄仍是 2:00:00`,
          barState(ctx).dur === '2:00:00', barState(ctx).dur);
  }
  const ctx = load();
  check('isValidDuration 擋掉 NaN / Infinity / 0 / 負數',
        !ctx.hooks.isValidDuration(NaN) && !ctx.hooks.isValidDuration(Infinity)
        && !ctx.hooks.isValidDuration(0) && !ctx.hooks.isValidDuration(-5)
        && ctx.hooks.isValidDuration(1));
}

{
  // API 也沒有 duration 時才退回 media（例如 info 還沒回來）
  const ctx = load();
  start(ctx, 'hls', { duration: null });
  ctx.video.duration = 1234;
  check('API 沒有 duration → 退回 media 的 1234',
        ctx.hooks.getCanonicalDuration() === 1234, ctx.hooks.getCanonicalDuration());
}


// ============================================================ I
head('[I] 拖到 100% 要 clamp，不可以讓 currentTime === duration 而直接 ended');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const writes = watchSeeks(ctx.video);
  const bar = drag(ctx, [100]);
  bar.dispatch('pointerup', pev(100));
  check('seek 目標嚴格小於 duration', writes[0] < MIN120, writes[0]);
  // **退的量要以 frame 為尺度**：24fps 的一格是 0.042 秒，只退 0.05 秒等於
  // 不到兩格，播下去立刻又撞到結尾（實測在真瀏覽器上仍然 ended）。
  // 這裡釘住「至少退 0.25 秒」，同時也不能退太多（兩小時的片退 2 秒就過頭了）。
  check('退的量夠大，不會一播就撞到結尾（>= 0.25 秒 ≈ 6 格 @24fps）',
        MIN120 - writes[0] >= 0.25, MIN120 - writes[0]);
  check('但也不能退太多（進度條上看不出被截短）',
        MIN120 - writes[0] <= 2, MIN120 - writes[0]);
  check('clampSeekTarget 直接餵 duration 也會退一個 epsilon',
        ctx.hooks.clampSeekTarget(MIN120, MIN120) < MIN120);
  check('clampSeekTarget 負數會夾到 0',
        ctx.hooks.clampSeekTarget(-50, MIN120) === 0);
  check('遠超出右邊界的值仍然 clamp 在片長以內',
        ctx.hooks.clampSeekTarget(9e9, MIN120) < MIN120);
}

{
  // 快轉鍵在片尾也不可以把 currentTime 頂到 duration
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  ctx.video.currentTime = MIN120 - 2;
  const writes = watchSeeks(ctx.video);
  ctx.get('btnFwd').onclick();
  check('片尾按「快轉 10 秒」不會頂到 duration（不會誤觸發 ended）',
        writes[0] < MIN120, writes[0]);
}


// ============================================================ J / K
head('[J][K] pointercancel / lostpointercapture 之後 dragging 要清乾淨');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const bar = drag(ctx, [40]);
  check('[J] 拖曳中 seekingDrag = true', ctx.hooks.seekingDrag === true);
  const writes = watchSeeks(ctx.video);
  bar.dispatch('pointercancel', pev(40));
  check('[J] pointercancel 之後 seekingDrag 回到 false',
        ctx.hooks.seekingDrag === false);
  check('[J] 而且**不會** commit seek（手勢被中斷不是使用者的選擇）',
        writes.length === 0, writes);
  check('[J] dragging class 拿掉了',
        !ctx.get('barWrap').classList.contains('dragging'));

  // 中斷之後 syncBar 要恢復正常（跟著 currentTime 走）
  ctx.video.currentTime = 1440;
  ctx.video.dispatch('timeupdate');
  check('[J] 中斷後 timeupdate 恢復控制畫面（20%）',
        barState(ctx).knob === 20, barState(ctx));
}

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const bar = drag(ctx, [70]);
  bar.dispatch('lostpointercapture', pev(70));
  check('[K] lostpointercapture 之後 seekingDrag 回到 false',
        ctx.hooks.seekingDrag === false);
  check('[K] dragging class 拿掉了',
        !ctx.get('barWrap').classList.contains('dragging'));
  ctx.video.currentTime = 720;
  ctx.video.dispatch('timeupdate');
  check('[K] 畫面恢復由 currentTime 控制（10%）',
        barState(ctx).knob === 10, barState(ctx));
}


// ============================================================ L
head('[L] 觸控拖曳期間 knob 必須強制可見（手機沒有 hover）');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const bar = ctx.get('barWrap');
  check('一開始沒有 dragging class', !bar.classList.contains('dragging'));
  bar.dispatch('pointerdown', pev(30));
  check('pointerdown 之後掛上 dragging（CSS 用它強制顯示 knob 與 tooltip）',
        bar.classList.contains('dragging'));
  bar.dispatch('pointermove', pev(35));
  check('移動過程中一直掛著', bar.classList.contains('dragging'));
  bar.dispatch('pointerup', pev(35));
  check('放開之後拿掉', !bar.classList.contains('dragging'));
}

{
  // CSS 那一半也要驗：規則真的存在，而且不是只寫在 :hover 底下
  const fs = require('fs'), path = require('path');
  const css = fs.readFileSync(
    path.join(__dirname, '..', 'app/static/player.css'), 'utf8');
  check('[L] CSS 有 .bar-wrap.dragging .bar-knob 的 scale(1)',
        /\.bar-wrap\.dragging\s+\.bar-knob\s*\{[^}]*scale\(1\)/.test(css));
  check('[L] CSS 有 .bar-wrap.dragging .bar-tip 的 opacity:1',
        /\.bar-wrap\.dragging\s+\.bar-tip\s*\{[^}]*opacity:\s*1/.test(css));
  check('[L] .bar-wrap 有 touch-action:none（不然手機拖曳會被當成頁面手勢）',
        /\.bar-wrap\s*\{[^}]*touch-action:\s*none/.test(css));
}


// ============================================================ M / N
head('[M][N] 拖曳不可以改變播放 / 暫停狀態');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  ctx.video.paused = true;
  let played = 0;
  ctx.video.play = () => { played++; ctx.video.paused = false; return Promise.resolve(); };
  const bar = drag(ctx, [20, 40]);
  bar.dispatch('pointerup', pev(40));
  check('[M] 原本暫停 → 拖完仍然暫停（沒有偷偷 play）', played === 0, played);
}

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  ctx.video.paused = false;
  let played = 0;
  ctx.video.play = () => { played++; return Promise.resolve(); };
  const bar = drag(ctx, [20, 40]);
  check('[N] 拖曳中記下了「原本在播」', ctx.hooks.wasPlayingBeforeSeek === true);
  bar.dispatch('pointerup', pev(40));
  check('[N] 原本在播 → 放開之後恢復播放', played === 1, played);
}


// ============================================================ O / P
head('[O][P] Direct 與 HLS Auto 都要能正常 Seek');

{
  const ctx = load();
  start(ctx, 'direct', { duration: MIN120 });
  const writes = watchSeeks(ctx.video);
  const bar = drag(ctx, [35]);
  bar.dispatch('pointerup', pev(35));
  check('[O] Direct：一次拖曳一次 seek，位置正確',
        writes.length === 1 && Math.abs(writes[0] - 2520) < 1, writes);
}

{
  const ctx = load();
  const h = start(ctx, 'hls', { duration: MIN120 });
  const LEVELS = [
    { width: 640, height: 268, bitrate: 700000 },
    { width: 1280, height: 536, bitrate: 2800000 },
  ];
  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  h.currentLevel = 1;
  h.emit(Events.LEVEL_SWITCHED, { level: 1 });
  const writes = watchSeeks(ctx.video);
  const bar = drag(ctx, [65]);
  bar.dispatch('pointerup', pev(65));
  check('[P] HLS Auto：一次拖曳一次 seek，位置正確',
        writes.length === 1 && Math.abs(writes[0] - 4680) < 1, writes);
}


// ============================================================ Q
head('[Q] HLS 切 level 前後，完整影片 duration 不可以改變');

{
  const ctx = load();
  const h = start(ctx, 'hls', { duration: 9123.4 });
  const LEVELS = [
    { width: 640, height: 268, bitrate: 700000 },
    { width: 1280, height: 536, bitrate: 2800000 },
  ];
  h.levels = LEVELS;
  h.emit(Events.MANIFEST_PARSED, { levels: LEVELS });
  ctx.video.duration = 9123.4;
  ctx.video.dispatch('timeupdate');
  const before = barState(ctx).dur;
  check('切階前總時間是 2:32:03', before === '2:32:03', before);

  // 換階：MediaSource 重建，duration 暫時只剩已緩衝的那一段
  h.currentLevel = 0;
  h.emit(Events.LEVEL_SWITCHED, { level: 0 });
  ctx.video.duration = 2123.8;
  ctx.video.dispatch('timeupdate');
  check('[Q] 切階之後總時間不變（原本會變成 35:23）',
        barState(ctx).dur === before, barState(ctx).dur);

  h.currentLevel = 1;
  h.emit(Events.LEVEL_SWITCHED, { level: 1 });
  ctx.video.duration = 9123.4;
  ctx.video.dispatch('timeupdate');
  check('[Q] 切回去也還是同一個總時間', barState(ctx).dur === before);
}


// ============================================================ R
head('[R] 2～3 小時長片拖到 25% / 50% / 75% / 90% 都要落在正確時間');

{
  const ctx = load();
  start(ctx, 'hls', { duration: 9123.4 });      // 2:32:03
  for (const pct of [25, 50, 75, 90]) {
    const want = 9123.4 * pct / 100;
    const writes = watchSeeks(ctx.video);
    const bar = drag(ctx, [pct]);
    bar.dispatch('pointerup', pev(pct));
    check(`拖到 ${pct}% → ${want.toFixed(0)} 秒（誤差 < 1 秒）`,
          writes.length === 1 && Math.abs(writes[0] - want) < 1,
          { got: writes[0], want });
  }
}

{
  // 而且中途 media duration 一直在變也不影響
  const ctx = load();
  start(ctx, 'hls', { duration: 9123.4 });
  const shrink = [9123.4, 1200, 4000, 600];
  let i = 0;
  for (const pct of [25, 50, 75, 90]) {
    ctx.video.duration = shrink[i++ % shrink.length];
    const writes = watchSeeks(ctx.video);
    const bar = drag(ctx, [pct]);
    bar.dispatch('pointerup', pev(pct));
    const want = 9123.4 * pct / 100;
    check(`media duration 亂跳時，拖到 ${pct}% 仍然是 ${want.toFixed(0)} 秒`,
          Math.abs(writes[0] - want) < 1, { got: writes[0], want });
  }
}


// ============================================================ 進度儲存
head('[12] scrubbing 的 preview 不可以污染觀看進度');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const sent = [];
  ctx.sandbox.window.navigator.sendBeacon = (url, blob) => { sent.push(blob._text); return true; };
  ctx.video.currentTime = 600;
  ctx.hooks.saveProgress(false, { force: true });
  const base = sent.length;
  check('先確認正常狀態存得進去', base > 0);

  const bar = drag(ctx, [90]);
  ctx.video.dispatch('timeupdate');
  ctx.hooks.saveProgress(false, { force: true });
  check('拖曳期間不寫進度（preview 不是已播進度）', sent.length === base, sent.length);

  bar.dispatch('pointerup', pev(90));
  // seek 已送出但還沒 seeked：currentTime 仍是舊值，一樣不能寫
  ctx.hooks.saveProgress(false, { force: true });
  check('seek 還沒落地時也不寫（不然會把舊位置蓋上去）',
        sent.length === base, sent.length);

  ctx.video.currentTime = 6480;
  ctx.video.dispatch('seeked');
  ctx.hooks.saveProgress(false, { force: true });
  check('seeked 之後恢復正常寫入', sent.length > base, sent.length);
  check('存進去的是新位置 6480',
        Math.abs(JSON.parse(sent[sent.length - 1]).position - 6480) < 1,
        sent[sent.length - 1]);
  check('而且 duration 存的是 canonical 的 7200',
        JSON.parse(sent[sent.length - 1]).duration === 7200,
        sent[sent.length - 1]);
}


// ============================================================ 9 / 15
head('[9][15] seek 等待期間圓點不跳片尾；seekable 不可以被當成片長');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const bar = drag(ctx, [60]);
  bar.dispatch('pointerup', pev(60));
  check('放開之後 pendingSeekTarget 記著使用者選的位置',
        Math.abs(ctx.hooks.pendingSeekTarget - 4320) < 1, ctx.hooks.pendingSeekTarget);

  // HLS 還在抓 fragment：currentTime 還停在舊的地方
  ctx.video.currentTime = 10;
  ctx.video.dispatch('seeking');
  ctx.video.dispatch('timeupdate');
  check('[9] 等待期間 knob 仍停在 60%，不會跳回開頭也不會跳片尾',
        barState(ctx).knob === 60, barState(ctx));
  check('[9] 有顯示「跳轉中…」', ctx.get('busy').textContent === '跳轉中…',
        ctx.get('busy').textContent);

  ctx.video.currentTime = 4320;
  ctx.video.dispatch('seeked');
  check('[9] seeked 之後提示收掉', ctx.get('busy').textContent === '');
  check('[9] 並恢復由 currentTime 控制', barState(ctx).knob === 60, barState(ctx));
}

{
  // seekable 只涵蓋一小段時，總時間仍然是完整片長
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  ctx.video.seekable = { length: 1, start: () => 0, end: () => 1800 };
  ctx.video.duration = 1800;
  ctx.video.dispatch('timeupdate');
  check('[15] seekable 只到 30 分，總時間仍是 2:00:00（沒把 seekable.end 當片長）',
        barState(ctx).dur === '2:00:00', barState(ctx).dur);
  const writes = watchSeeks(ctx.video);
  const bar = drag(ctx, [80]);
  bar.dispatch('pointerup', pev(80));
  check('[15] 目標在 seekable 之外也照樣送出（等 HLS 載 fragment），不改成 seekable.end',
        Math.abs(writes[0] - 5760) < 1, writes[0]);
}


// ============================================================ 18
head('[18] 拖曳期間控制列不可以被 idle timer 藏掉');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const pl = ctx.get('pl');
  const bar = drag(ctx, [50]);
  check('拖曳中 seekingDrag 擋住 idle 隱藏', ctx.hooks.seekingDrag === true);
  check('控制列沒有 idle class', !pl.classList.contains('idle'));
  bar.dispatch('pointerup', pev(50));
  check('放開後恢復正常（可以再被隱藏）', ctx.hooks.seekingDrag === false);
}


// ============================================================ seeked 沒來
head('[9b] seeked 一直不來時要逾時放行，不可以讓進度條永遠卡住');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const realSetTimeout = ctx.sandbox.window.setTimeout;
  let seekTimer = null;
  // 攔下 commitScrub 排的那個保險計時器，直接把它叫起來（不用真的等 30 秒）
  ctx.sandbox.window.setTimeout = (fn, ms) => {
    if (ms === 30000) { seekTimer = fn; return 999; }
    return realSetTimeout(fn, ms);
  };
  const bar = drag(ctx, [70]);
  bar.dispatch('pointerup', pev(70));
  check('先確認有排一個逾時保險', typeof seekTimer === 'function');
  check('而且 pendingSeekTarget 還在（seeked 尚未到）',
        ctx.hooks.pendingSeekTarget != null);

  // seeked 始終沒來 → 逾時觸發
  seekTimer();
  check('逾時之後 pendingSeekTarget 被清掉',
        ctx.hooks.pendingSeekTarget === null, ctx.hooks.pendingSeekTarget);
  ctx.video.currentTime = 1440;
  ctx.video.dispatch('timeupdate');
  check('進度條恢復跟著 currentTime 走（20%），不會永遠卡在 70%',
        barState(ctx).knob === 20, barState(ctx));
  ctx.sandbox.window.setTimeout = realSetTimeout;
}


// ============================================================ 換模式 / 換畫質
head('[8] 換模式或換畫質之後，上一次 seek 的等待狀態不可以留著');

{
  const ctx = load();
  start(ctx, 'hls', { duration: MIN120 });
  const bar = drag(ctx, [60]);
  bar.dispatch('pointerup', pev(60));
  check('先確認有一個等待中的 seek',
        ctx.hooks.pendingSeekTarget != null, ctx.hooks.pendingSeekTarget);

  // 相當於 switchQuality() / switchMode() 裡的那一步
  ctx.hooks.play('direct', 100);
  check('換模式之後 pendingSeekTarget 被清掉（不然進度條會卡在舊位置）',
        ctx.hooks.pendingSeekTarget === null, ctx.hooks.pendingSeekTarget);

  // 而且進度條恢復跟著 currentTime 走
  ctx.video.currentTime = 3600;
  ctx.video.dispatch('timeupdate');
  check('進度條恢復正常（50%）', barState(ctx).knob === 50, barState(ctx));
}


console.log('\n' + '='.repeat(50));
console.log(`通過 ${OK}，失敗 ${FAIL}`);
process.exit(FAIL ? 1 : 0);
