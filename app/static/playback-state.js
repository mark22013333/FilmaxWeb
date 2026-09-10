/* 播放狀態的推導：純函式，沒有 DOM、沒有 hls.js、沒有全域變數。

   **為什麼要抽出來。**原本「右上角顯示什麼」這件事散在 renderTags() 裡，
   而它讀的是 info.height —— 那是**片源**資訊，不是播放器現在正在播的東西。
   選了 1080p 上限、ABR 實際降到 480p 的時候，右上角照樣寫著片源的 720p。
   介面在說謊，而說謊的地方沒有辦法被測試蓋住，因為它跟 DOM 綁在一起。

   抽成純函式之後，「選了什麼 ＋ 現在在哪一階 → 該顯示什麼」變成可以直接
   餵資料進去比對的東西（tests/playback_state_test.js）。這個檔案刻意不引進
   任何前端框架 —— 它只是幾個 function 跟一個常數表。

   同時被瀏覽器（<script> 全域）與 node（module.exports）載入。 */
(function (root) {
  'use strict';

  // 畫質選擇的三種語意。cfg.quality 存的就是這三種：
  //   0  = 自動（伺服器按區網／遠端給預設，ABR 在整條階梯上自己選）
  //  -1  = 原畫質（不縮放）
  //  >0  = 指定高度，語意是**上限**不是鎖定 —— 後端 abr_ladder(auto=false)
  //        仍然會發比它低的階，網路不夠時降得下去（規格 J 第 1 層）。
  const QUALITY_AUTO = 0;
  const QUALITY_SOURCE = -1;

  /** 一個乾淨的初始狀態。切片、切畫質、切 direct/hls 都要從這裡重來 ——
   *  殘留上一部片的 level 正是「顯示跟實際不一致」的其中一個來源。 */
  function emptyState() {
    return {
      playMode: null,            // 'direct' | 'hls'
      sourceQuality: null,       // { width, height, label } 片源，不是正在播的
      selectedQuality: QUALITY_AUTO,
      actualLevelIndex: -1,      // hls.js 的 level 索引；direct 或還沒切好時 -1
      actualResolution: null,    // { width, height } 現在真的在播的
      actualBitrate: 0,          // 這一階 master 宣告的 BANDWIDTH
      levelCount: 0,
      bandwidthEstimate: 0,      // hls.js 的 EWMA 估計
      bufferSeconds: 0,
      lastSwitchReason: null,    // initial | abr-down | abr-up | manual | stall-down | direct-fallback | recover
      stallCount: 0,
      lastFragSize: 0,
      lastFragLoadMs: 0,
      lastFragThroughputKbps: 0,
      remote: false,
      mobile: false,
      capping: -1,               // 自己壓的 autoLevelCapping（-1 = 沒壓）
    };
  }

  /** 「級別」：依**寬度**判，不依高度 —— 1920×804 的寬螢幕片是 1080p 不是 804p。
   *
   *  **這是 media.quality_class() 的逐行翻譯，門檻必須一模一樣。**
   *  兩邊分岔的話同一階在標籤上寫 720p、在面板上寫 480p，
   *  而那種不一致正是這次要修的東西。改一邊就要改另一邊
   *  （tests/playback_state_test.js 有一段專門比對兩邊的答案）。 */
  function qualityClass(w, h) {
    w = w || 0; h = h || 0;
    if (w >= 3840 || h >= 2000) return '4K';
    if (w >= 2560 || h >= 1400) return '1440p';
    if (w >= 1900 || h >= 1000) return '1080p';
    if (w >= 1280 || h >= 700) return '720p';
    if (w >= 854 || h >= 460) return '480p';
    return h ? h + 'p' : '—';
  }

  /** 使用者選的那一項要怎麼講。**「720p」與「720p 上限」是兩件不同的事** ——
   *  後端給的是上限（底下還有 480/360 可降），寫成「720p」會讓人以為每一秒
   *  都保證是 720p，然後在實際降到 360p 時覺得介面壞了。 */
  function selectedLabel(selected, sourceQuality) {
    if (selected === QUALITY_AUTO) return '自動';
    if (selected === QUALITY_SOURCE) {
      return sourceQuality && sourceQuality.label ? `原畫質 ${sourceQuality.label}` : '原畫質';
    }
    return `${selected}p 上限`;
  }

  /** 現在真的在播的那一階要怎麼講。拿不到 level 就回空字串 ——
   *  **不要拿片源的高度來頂替**，那正是原本那個 bug。 */
  function actualLabel(st) {
    const r = st.actualResolution;
    if (!r || !r.height) return '';
    const lab = qualityClass(r.width, r.height);
    return lab === '—' ? '' : lab;
  }

  function resolutionText(r) {
    return r && r.width && r.height ? `${r.width}×${r.height}` : '';
  }

  /** 右上角那一行。這是整個修正的重點：**只從 runtime state 取值。**
   *
   *  形狀（規格要求的那幾種）：
   *    自動、拿得到實際階     → 「自動 · 720p · 1280×536」
   *    手動上限、實際比它低   → 「上限 1080p · 目前 720p」
   *    手動上限、實際就是它   → 「1080p · 1920×804」
   *    原畫質                 → 「原畫質 · 1920×804」
   *    direct                 → 「直接串流 · 原檔 1080p · 1920×804」
   *  拿不到實際階（剛開播、還沒 LEVEL_SWITCHED）就只講選擇，不要瞎猜。 */
  function statusLine(st) {
    const sel = st.selectedQuality;
    const act = actualLabel(st);
    const dim = resolutionText(st.actualResolution);

    if (st.playMode === 'direct') {
      // direct 是原始檔直送，沒有第二階 —— 它的「實際」永遠等於片源。
      const s = st.sourceQuality || {};
      const bits = ['直接串流'];
      if (s.label) bits.push(`原檔 ${s.label}`);
      const d = resolutionText(s);
      if (d) bits.push(d);
      return bits.join(' · ');
    }

    if (sel === QUALITY_SOURCE) {
      return ['原畫質', dim].filter(Boolean).join(' · ');
    }

    if (sel === QUALITY_AUTO) {
      if (!act) return '自動';
      return ['自動', act, dim].filter(Boolean).join(' · ');
    }

    // 手動挑了一個上限。
    if (!act) return `${sel}p 上限`;
    // 實際就是選的那一階 → 不必囉嗦地寫兩次
    const actH = st.actualResolution ? st.actualResolution.height : 0;
    if (Math.abs(actH - sel) <= 2) return [`${sel}p`, dim].filter(Boolean).join(' · ');
    return `上限 ${sel}p · 目前 ${act}`;
  }

  /** 資訊面板那一疊。回 [[標籤, 值], ...]，值一律是字串。
   *  片源與「正在播的」刻意分成兩塊 —— 混在一起就是原本那個問題。 */
  function detailRows(st) {
    const rows = [];
    const src = st.sourceQuality || {};
    rows.push(['片源', [src.label, resolutionText(src)].filter(Boolean).join(' · ') || '未知']);
    rows.push(['選擇的畫質', selectedLabel(st.selectedQuality, src)]);
    rows.push(['播放模式', st.playMode === 'direct' ? '直接串流（原檔，無畫質階梯）'
                                                   : '自適應串流 HLS']);
    if (st.playMode === 'direct') {
      // direct 沒有 level、沒有 variant bitrate。**列一個「—」比不列好**：
      // 使用者才知道那不是壞掉，是這個模式本來就沒有這個東西。
      rows.push(['目前實際畫質', src.label || '原檔']);
      rows.push(['實際解析度', resolutionText(src) || '未知']);
      rows.push(['HLS level', '—（直接串流沒有階梯）']);
      rows.push(['Variant bitrate', '—']);
    } else {
      rows.push(['目前實際畫質', actualLabel(st) || '尚未取得']);
      rows.push(['實際解析度', resolutionText(st.actualResolution) || '尚未取得']);
      rows.push(['HLS level', st.actualLevelIndex >= 0
        ? `${st.actualLevelIndex + 1} / ${st.levelCount}` : '尚未取得']);
      rows.push(['Variant bitrate', st.actualBitrate ? fmtKbps(st.actualBitrate / 1000) : '未知']);
    }
    rows.push(['估計頻寬', st.bandwidthEstimate ? fmtKbps(st.bandwidthEstimate / 1000) : '尚未估計']);
    rows.push(['Buffer 秒數', st.bufferSeconds ? st.bufferSeconds.toFixed(1) + ' 秒' : '0 秒']);
    if (st.lastSwitchReason) rows.push(['最近一次切換', switchReasonText(st.lastSwitchReason)]);
    return rows;
  }

  const SWITCH_TEXT = {
    'initial': '開播選階',
    'abr-down': '網路變慢，自動降階',
    'abr-up': '網路變好，自動升階',
    'stall-down': '偵測到卡頓，強制降階',
    'manual': '手動選擇',
    'direct-fallback': '直接串流不順，改用自動畫質',
    'recover': '恢復穩定，交還自動調節',
  };
  function switchReasonText(r) { return SWITCH_TEXT[r] || r; }

  function fmtKbps(kbps) {
    if (!kbps) return '0 kbps';
    return kbps >= 1000 ? (kbps / 1000).toFixed(1) + ' Mbps' : Math.round(kbps) + ' kbps';
  }

  /** hls.js 的 level 物件 → 我們要的那幾個欄位。**level 可能沒有 width/height**
   *  （master 裡沒填 RESOLUTION 時），那時候只有 bitrate 是可信的。 */
  function levelToState(level, index, levelCount) {
    if (!level) return { actualLevelIndex: -1, actualResolution: null, actualBitrate: 0 };
    return {
      actualLevelIndex: index,
      actualResolution: (level.width && level.height)
        ? { width: level.width, height: level.height } : null,
      actualBitrate: level.bitrate || 0,
      levelCount: levelCount || 0,
    };
  }

  /** 這一次 level 變動要記成什麼原因。比的是**碼率**不是索引 ——
   *  hls.js 的 level 索引順序由它內部決定，不保證跟碼率高低一致
   *  （player.js 的 waiting handler 已經為這件事留過註解）。 */
  function switchReason(prevBitrate, nextBitrate, manual) {
    if (manual) return 'manual';
    if (!prevBitrate) return 'initial';
    if (nextBitrate < prevBitrate) return 'abr-down';
    if (nextBitrate > prevBitrate) return 'abr-up';
    return 'initial';
  }

  /* ---------------------------------------------------------------- 卡頓看門狗 */

  // 判斷「網路真的撐不住」的門檻。**不要每一個 waiting 就手動切 level** ——
  // hls.js 本身就有 ABR，兩套一起動只會互相打架（一個往下切、一個往上切，
  // 結果是每幾秒震盪一次）。這裡只在 ABR 明顯來不及時插手一次。
  const WATCHDOG = {
    windowMs: 30000,      // 只看最近 30 秒的 stall
    stallsToAct: 2,       // 30 秒內卡兩次才算真的撐不住（一次可能只是伺服器在轉碼）
    lowBufferSec: 2.0,    // buffer 低於這個值就算危險水位
    lowBufferMs: 6000,    // 而且要連續低於這麼久（瞬間掉下去不算）
    // fragment 下載時間超過段長的這個倍數 = 下載追不上播放。
    // 1.0 就已經是「剛好追不上」，留一點餘裕避免抖動誤判。
    slowFragRatio: 1.2,
    holdMs: 45000,        // 降階後至少穩定這麼久才考慮放手（hysteresis）
    upBandwidthFactor: 1.4,  // 而且估計頻寬要明顯高於上一階需求才放手
    upBufferSec: 8.0,     // 並且 buffer 要回到安全水位
  };

  /** 該不該主動降一階。回 { act, reason }。
   *
   *  三個獨立的訊號，任一個成立就算：
   *    1. 短時間內卡了好幾次
   *    2. buffer 持續在危險水位（不是瞬間掉下去，是一直起不來）
   *    3. fragment 下載時間明顯超過段長 —— 下載追不上播放，再等只會更糟
   *
   *  **只在 Auto 才動。**使用者手動挑了上限的話，那個上限底下的階由
   *  hls.js 的 ABR 管就好，我們再插手等於把兩套 ABR 疊起來。 */
  function shouldStepDown(m, now) {
    if (!m || m.autoMode === false) return { act: false, reason: null };
    if (m.levelCount <= 1) return { act: false, reason: null };
    // 已經在最低階了 —— 再降無階可降，插手只會噴事件
    if (m.atLowestLevel) return { act: false, reason: null };
    // 剛降過就先觀察，不要連續往下踩
    if (m.lastActionAt && now - m.lastActionAt < WATCHDOG.holdMs / 3) {
      return { act: false, reason: null };
    }
    const recent = (m.stallTimes || []).filter(t => now - t <= WATCHDOG.windowMs);
    if (recent.length >= WATCHDOG.stallsToAct) {
      return { act: true, reason: `最近 30 秒卡頓 ${recent.length} 次` };
    }
    if (m.lowBufferSince && now - m.lowBufferSince >= WATCHDOG.lowBufferMs) {
      return { act: true, reason: `buffer 已經連續 ${Math.round((now - m.lowBufferSince) / 1000)} 秒不足` };
    }
    if (m.lastFragLoadMs && m.segmentSeconds &&
        m.lastFragLoadMs > m.segmentSeconds * 1000 * WATCHDOG.slowFragRatio) {
      return { act: true, reason: '分段下載追不上播放' };
    }
    return { act: false, reason: null };
  }

  /** 降階之後該不該把控制權交還給 hls.js 的 Auto ABR。
   *
   *  **這裡就是防震盪的那道閘。**沒有它的話形狀會是
   *  720 → 480 →（頻寬看起來夠了）→ 720 → 卡 → 480 → …每幾秒一次。
   *  所以要同時滿足三件事，缺一不可：
   *    1. 已經穩定夠久（holdMs），而且這段期間沒有再卡過
   *    2. buffer 回到安全水位
   *    3. 估計頻寬明顯高於上一階的需求（不是「剛好等於」—— 剛好等於就是
   *       上次卡掉的那個點，回去只會再卡一次） */
  function shouldRelease(m, now) {
    if (!m || m.capping < 0) return { act: false, reason: null };
    if (!m.cappedAt || now - m.cappedAt < WATCHDOG.holdMs) return { act: false, reason: null };
    const recent = (m.stallTimes || []).filter(t => now - t <= WATCHDOG.holdMs);
    if (recent.length) return { act: false, reason: null };
    if ((m.bufferSeconds || 0) < WATCHDOG.upBufferSec) return { act: false, reason: null };
    const need = m.nextLevelBitrate || 0;
    if (need && (m.bandwidthEstimate || 0) < need * WATCHDOG.upBandwidthFactor) {
      return { act: false, reason: null };
    }
    return { act: true, reason: '已穩定，交還自動調節' };
  }

  /* ---------------------------------------------------------------- direct fallback */

  // direct 沒有畫質階梯，**所以它不可能自己變成 480p**。撐不住時唯一正確的
  // 動作是換一條有階梯的路（HLS），而不是假裝可以在 direct 裡面降畫質。
  const DIRECT = {
    windowMs: 40000,
    stallsToFallback: 3,       // 一般情況：40 秒內卡三次
    stallsWhenAdvisedAgainst: 2,  // 伺服器本來就說這條鏈路餵不動 → 門檻收緊
    longStallMs: 12000,        // 或是單一次就卡超過 12 秒（那不是在緩衝，是餵不動）
  };

  /** direct 該不該切去 HLS。回 { act, reason }。 */
  function shouldFallbackFromDirect(m, now) {
    if (!m || m.playMode !== 'direct') return { act: false, reason: null };
    // **已經切過一次就不要再切。**舊的 direct error/waiting 事件在切換之後
    // 還會再飄進來，沒有這道閘就會切第二次（規格的 H 案）。
    if (m.fellBack) return { act: false, reason: null };
    if (m.currentStallMs >= DIRECT.longStallMs) {
      return { act: true, reason: '直接串流卡住超過 12 秒' };
    }
    const need = m.directAdvised === false
      ? DIRECT.stallsWhenAdvisedAgainst : DIRECT.stallsToFallback;
    const recent = (m.stallTimes || []).filter(t => now - t <= DIRECT.windowMs);
    if (recent.length >= need) {
      return { act: true, reason: `直接串流反覆緩衝（${recent.length} 次）` };
    }
    return { act: false, reason: null };
  }

  /* ---------------------------------------------------------------- 手機 / 開播策略 */

  /** 開播要不要先壓一階。**手機遠端寧可先低一階、快點開始播，再慢慢升**——
   *  一開始就賭最高畫質，賭輸的代價是開播卡十幾秒，那比畫質差一階難受得多。
   *
   *  Network Information API 只能當**輔助**：Safari / iPhone 根本沒有它，
   *  所以它有值時可以用來收緊，沒有值時絕對不能因此放寬。 */
  function startupPolicy(env) {
    const e = env || {};
    // saveData 是使用者明確表達過的意思，優先於任何推測
    if (e.saveData) return { capIndex: 0, reason: '使用者開了節省流量' };
    if (!e.remote) return { capIndex: -1, reason: '區網，不必壓' };
    if (!e.mobile) return { capIndex: -1, reason: '遠端桌機，交給 ABR' };
    const et = e.effectiveType || '';
    if (et === 'slow-2g' || et === '2g') return { capIndex: 0, reason: `連線類型 ${et}` };
    if (et === '3g') return { capIndex: 1, reason: '連線類型 3g' };
    if (typeof e.downlink === 'number' && e.downlink > 0 && e.downlink < 1.5) {
      return { capIndex: 1, reason: `估計下行只有 ${e.downlink} Mbps` };
    }
    // 沒有 Network Information API（iPhone 就是這一類）：**仍然要壓一階**。
    // 這是這個函式最重要的一條 —— 手機遠端的預設就是保守開播，
    // 拿不到網路資訊不是「假設網路很好」的理由。
    return { capIndex: 1, reason: '手機遠端，保守開播' };
  }

  /** 螢幕真的放得下的最大高度。手機 390px 寬去選 4K 是純浪費 ——
   *  hls.js 的 capLevelToPlayerSize 會處理這件事，這個函式是給
   *  「它不支援 / 被關掉」時的說明文字與測試用。
   *  devicePixelRatio 要算進去（3x 的手機 390px 邏輯寬 = 1170 實體像素）。 */
  function screenCapHeight(env) {
    const e = env || {};
    const dpr = Math.min(e.devicePixelRatio || 1, 3);   // 4x 以上沒有實益，還會選過高
    const h = Math.max(e.viewportWidth || 0, e.viewportHeight || 0) * dpr;
    return Math.round(h) || 0;
  }

  const api = {
    QUALITY_AUTO, QUALITY_SOURCE, WATCHDOG, DIRECT,
    emptyState, qualityClass, selectedLabel, actualLabel, resolutionText,
    statusLine, detailRows, switchReason, switchReasonText, fmtKbps, levelToState,
    shouldStepDown, shouldRelease, shouldFallbackFromDirect,
    startupPolicy, screenCapHeight,
  };

  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PlaybackState = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
