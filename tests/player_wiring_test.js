/* 播放器的「接線」測試：證明 hls.js 的事件真的會把 UI 改掉。

     node tests/player_wiring_test.js

   playback_state_test.js 測的是推導（給定狀態該顯示什麼），這一支測的是
   **接線**（事件進來之後狀態有沒有真的被改、UI 有沒有真的被重畫）。
   純函式全對但沒有人呼叫它們，是這次修正最容易留下的漏洞。

   做法：用最小的 DOM stub ＋ 假的 hls.js 把 player.js 真的載進來跑。
   不開瀏覽器、不裝框架 —— player.js 只用到少數幾個 DOM API，
   stub 出來比拉一整套 jsdom 便宜得多，而且看得出它到底依賴了什麼。 */
'use strict';
const { load, start, Events } = require('./player_harness');

let OK = 0, FAIL = 0;
function check(name, cond, extra) {
  if (cond) { OK++; console.log('  PASS  ' + name); }
  else { FAIL++; console.log('  FAIL  ' + name + '  → ' + JSON.stringify(extra)); }
}
function head(t) { console.log('\n' + t); }

// 非同步的驗證掛在這裡（goNextEpisode 要 await 進度寫入才跳頁）。
// 檔案最後的 summary 會等它們跑完再印結果 —— 不等的話那些 check 會在
// process.exit 之後才執行，永遠不會被算進去。
const pending = [];

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


head('[下一集] 顯示／隱藏的接線，以及「切集前先把這一集標記完成」');
{
  const ctx = load();
  start(ctx, 'direct', {
    duration: 3600,
    next_episode: { file_id: 42, item_id: 7, season: 2, episode: 3,
                    title: '第三集', label: 'S02E03', url: '/player?file=42' },
  });
  const btn = ctx.get('nextEp');

  ctx.video.currentTime = 100;
  ctx.video.dispatch('timeupdate');
  check('片中不顯示', btn.hidden === true, btn.hidden);

  // 3600 秒的片，門檻夾到上限 90 秒
  ctx.video.currentTime = 3600 - 40;
  ctx.video.dispatch('timeupdate');
  check('進入片尾門檻 → 顯示', btn.hidden === false, btn.hidden);
  check('標籤寫得出是哪一集', /S02E03/.test(ctx.get('nextEpLabel').textContent),
        ctx.get('nextEpLabel').textContent);
  check('有 aria-label（報讀器念得出來）',
        /S02E03/.test(btn._attrs['aria-label'] || ''), btn._attrs);

  ctx.video.currentTime = 1000;
  ctx.video.dispatch('timeupdate');
  check('把進度拉回門檻以前 → 再次隱藏', btn.hidden === true, btn.hidden);

  ctx.video.currentTime = 3595;
  ctx.video.dispatch('timeupdate');
  check('再拉到片尾 → 又出現', btn.hidden === false, btn.hidden);

  ctx.video.ended = true;
  ctx.video.dispatch('ended');
  check('影片真的播完之後按鈕仍然可以按', btn.hidden === false, btn.hidden);
}
{
  const ctx = load();
  start(ctx, 'direct', { duration: 3600, next_episode: null });
  ctx.video.currentTime = 3595;
  ctx.video.dispatch('timeupdate');
  check('電影／最後一集：片尾也不會冒出按鈕',
        ctx.get('nextEp').hidden === true, ctx.get('nextEp').hidden);
}
{
  // **這一項是第 5 點的核心**：按下「下一集」必須先把目前這一集寫成
  // finished 再跳頁。順序顛倒的話導覽會把請求砍掉，這一集就以
  // finished=false 留著，下一秒又出現在「繼續觀看」上。
  const ctx = load();
  const calls = [];
  ctx.sandbox.fetch = (url, opt) => {
    // 記下「呼叫的當下有沒有已經跳頁」—— 跳頁在前就是那個 race condition
    calls.push({ url, body: opt && opt.body, keepalive: !!(opt && opt.keepalive),
                 hrefWhenCalled: ctx.sandbox.location.href });
    return Promise.resolve({ ok: true });
  };
  start(ctx, 'direct', {
    duration: 3600,
    next_episode: { file_id: 42, item_id: 7, season: 2, episode: 3,
                    label: 'S02E03', url: '/player?file=42' },
  });
  ctx.video.currentTime = 3580;
  ctx.video.dispatch('timeupdate');

  // goNextEpisode 是 async（它要 await 進度寫入），所以這一段的驗證
  // 掛在 pending 上，由檔案最後的 summary 等它跑完再印結果。
  pending.push(ctx.hooks.goNextEpisode().then(() => {
    const prog = calls.filter(c => c.url === '/api/progress');
    check('有送出進度', prog.length >= 1, calls.map(c => c.url));
    const body = JSON.parse(prog[0].body);
    check('而且是 finished=true（不是只存位置）', body.finished === true, body);
    check('帶的是目前這一集的 file_id', body.file_id === 1, body);
    check('用 keepalive，跳頁之後請求才送得完', prog[0].keepalive === true, prog[0]);
    check('**寫入發生在跳頁之前**（順序顛倒就是那個 race condition）',
          prog[0].hrefWhenCalled === '', prog[0].hrefWhenCalled);
    check('最後才跳到下一集', ctx.sandbox.location.href === '/player?file=42',
          ctx.sandbox.location.href);
  }));
}

head('[進度儲存] 連續播放期間真的會寫入（原本的 debounce 寫法永遠不會）');
{
  // **這一項是第 6 點的核心迴歸測試。**
  // 原本是 `clearTimeout(saveTimer); setTimeout(saveProgress, 5000)` ——
  // timeupdate 每 250ms 就來一次，所以那個 timer 永遠在被重設、永遠不會到期，
  // 連續播放一小時一筆都沒寫進去。改成節流之後這裡才會有東西。
  const ctx = load();
  const sent = [];
  ctx.sandbox.navigator.sendBeacon = (url, blob) => {
    sent.push({ url, body: blob._text }); return true;
  };
  start(ctx, 'direct', { duration: 3600 });

  // 模擬 60 秒的連續播放：每 250ms 一次 timeupdate（跟真的瀏覽器一樣）
  // **時鐘要換掉 sandbox 裡的那一個。**vm context 有自己的 Date 內建物件，
  // 改測試這一端的 Date.now 對 player.js 完全沒有作用 —— 那樣跑出來的結果
  // 會是「60 秒只寫 1 次」，看起來像程式壞了，其實是測試沒有推進時間。
  let clock = 1_000_000;
  const realNow = ctx.sandbox.Date.now;
  ctx.sandbox.Date.now = () => clock;
  try {
    for (let t = 0; t < 60; t += 0.25) {
      ctx.video.currentTime = t;
      clock += 250;
      ctx.video.dispatch('timeupdate');
    }
  } finally { ctx.sandbox.Date.now = realNow; }

  check('連續播放 60 秒有寫進度（原本的寫法是 0 次）', sent.length >= 1, sent.length);
  // 節流是 8 秒一次，60 秒大約 7～8 次。上限放寬一點，重點是「不是每次都寫」。
  check(`而且不是每個 timeupdate 都發 API（240 次事件 → ${sent.length} 次寫入）`,
        sent.length <= 12, sent.length);
  const last = JSON.parse(sent[sent.length - 1].body);
  check('寫進去的是當下的播放位置', last.position > 40, last);
  check('連續播放期間 finished 是 false', last.finished === false, last);
}
{
  // 關鍵時機要補存 —— 節流一定會漏掉最後那幾秒。
  const ctx = load();
  const sent = [];
  ctx.sandbox.navigator.sendBeacon = (url, blob) => { sent.push(blob._text); return true; };
  start(ctx, 'direct', { duration: 3600 });
  ctx.video.currentTime = 123;

  const before = sent.length;
  ctx.video.dispatch('pause');
  check('暫停時補存一次', sent.length > before, sent.length);

  ctx.video.currentTime = 456;
  const b2 = sent.length;
  ctx.sandbox.document.visibilityState = 'hidden';
  ctx.sandbox.document.dispatch('visibilitychange');
  check('切到背景（手機切 App／鎖螢幕）補存一次', sent.length > b2, sent.length);

  ctx.video.currentTime = 789;
  const b3 = sent.length;
  ctx.sandbox.window.dispatch('pagehide');
  check('pagehide 補存一次（iOS Safari 的 beforeunload 常常不觸發）',
        sent.length > b3, sent.length);

  ctx.video.currentTime = 1011;
  const b4 = sent.length;
  ctx.sandbox.window.dispatch('beforeunload');
  check('beforeunload 補存一次（桌機關分頁）', sent.length > b4, sent.length);

  const b5 = sent.length;
  ctx.video.dispatch('ended');
  check('播完存一次 finished=true', sent.length > b5
        && JSON.parse(sent[sent.length - 1]).finished === true,
        sent[sent.length - 1]);
}

Promise.all(pending).then(() => {
  console.log('\n' + '='.repeat(50));
  console.log(`通過 ${OK}，失敗 ${FAIL}`);
  process.exit(FAIL ? 1 : 0);
});
