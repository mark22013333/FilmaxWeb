/* 播放狀態的推導：右上角顯示什麼、看門狗什麼時候插手、direct 什麼時候 fallback。

     node tests/playback_state_test.js

   規格：J 章「播放狀態顯示」。這一支不開瀏覽器、不引進任何前端框架 ——
   要測的東西已經被抽成 app/static/playback-state.js 裡的純函式，
   為了測幾個 function 拉一整套 jest/vitest 進來是不划算的交換。

   跟 Python 那幾支測試同一個形狀（check/head/結尾統計），輸出對得起來。 */
'use strict';
const path = require('path');
const PS = require(path.join(__dirname, '..', 'app', 'static', 'playback-state.js'));

let OK = 0, FAIL = 0;
function check(name, cond, extra) {
  if (cond) { OK++; console.log('  PASS  ' + name); }
  else { FAIL++; console.log('  FAIL  ' + name + '  → ' + JSON.stringify(extra)); }
}
function head(t) { console.log('\n' + t); }

/** 造一個播放中的狀態。預設是「自動、HLS、正在播 720p」。 */
function st(over) {
  return Object.assign(PS.emptyState(), {
    playMode: 'hls',
    sourceQuality: { width: 1920, height: 804, label: '1080p' },
    selectedQuality: PS.QUALITY_AUTO,
    actualLevelIndex: 1, levelCount: 3,
    actualResolution: { width: 1280, height: 536 },
    actualBitrate: 2_800_000,
  }, over || {});
}
const R = { p1080: { width: 1920, height: 804 }, p720: { width: 1280, height: 536 },
            p480: { width: 854, height: 358 }, p360: { width: 640, height: 268 } };


// ============================================================ 級別判定
head('[J] 級別依寬度判，而且要跟後端的 media.quality_class() 完全一致');

// 這幾組就是 tests/two_rung_test.py 裡釘住後端的那幾組。**兩邊的答案必須一樣** ——
// 分岔的話同一階會在標籤上寫 720p、在面板上寫 480p。
check('1920×804 的寬螢幕片是 1080p，不是 804p', PS.qualityClass(1920, 804) === '1080p',
      PS.qualityClass(1920, 804));
check('1920×1080 也是 1080p', PS.qualityClass(1920, 1080) === '1080p');
check('1920×960（2:1）也是 1080p', PS.qualityClass(1920, 960) === '1080p');
check('3840×1608 的 4K 寬螢幕是 4K', PS.qualityClass(3840, 1608) === '4K');
check('1280×720 是 720p', PS.qualityClass(1280, 720) === '720p');
check('854×480 是 480p', PS.qualityClass(854, 480) === '480p');
check('真的很小的片源就照高度講', PS.qualityClass(720, 404) === '404p', PS.qualityClass(720, 404));
check('沒有資料不要亂猜', PS.qualityClass(null, null) === '—');


// ============================================================ A / 自動模式
head('[A] 自動模式：右上角要講「現在真的在播的那一階」');

check('自動 ＋ 正在播 720p → 「自動 · 720p · 1280×536」',
      PS.statusLine(st()) === '自動 · 720p · 1280×536', PS.statusLine(st()));
check('還沒切好（沒有 actualResolution）就只講「自動」，不要拿片源頂替',
      PS.statusLine(st({ actualResolution: null })) === '自動',
      PS.statusLine(st({ actualResolution: null })));
// **這一項就是原本那個 bug**：片源是 1080p，實際在播 480p，右上角寫的必須是 480p。
check('片源 1080p、實際 480p → 顯示 480p（不是片源的 1080p）',
      PS.statusLine(st({ actualResolution: R.p480 })).includes('480p') &&
      !PS.statusLine(st({ actualResolution: R.p480 })).includes('1080p'),
      PS.statusLine(st({ actualResolution: R.p480 })));


// ============================================================ E / ABR 降階後立即反映
head('[E] Auto 從 720 降到 480，UI 立即從「自動 · 720p」變「自動 · 480p」');

const s720 = st({ actualResolution: R.p720 });
check('降階前是 720p', PS.statusLine(s720) === '自動 · 720p · 1280×536', PS.statusLine(s720));
// LEVEL_SWITCHED 進來 → levelToState 產生新的 actual*，UI 只讀這一份
const after = Object.assign({}, s720, PS.levelToState(
  { width: 854, height: 358, bitrate: 1_260_000 }, 2, 3));
check('降階後立刻變 480p', PS.statusLine(after) === '自動 · 480p · 854×358', PS.statusLine(after));
check('bitrate 也跟著換成新那一階的', after.actualBitrate === 1_260_000, after.actualBitrate);
const back = Object.assign({}, after, PS.levelToState(
  { width: 1280, height: 536, bitrate: 2_800_000 }, 1, 3));
check('網路恢復升回 720p，UI 一樣立刻跟上',
      PS.statusLine(back) === '自動 · 720p · 1280×536', PS.statusLine(back));


// ============================================================ F / 手動上限 vs 目前實際
head('[F] 手動選 1080p 上限、實際只播得動 720p → 兩個狀態都要講清楚');

const manual = st({ selectedQuality: 1080, actualResolution: R.p720 });
const line = PS.statusLine(manual);
check('同一行要同時看得到「上限 1080p」與「目前 720p」',
      line.includes('上限 1080p') && line.includes('目前 720p'), line);
check('**不能讓使用者以為選了 1080p 就每一秒都是 1080p**',
      line !== '1080p' && !/^1080p ·/.test(line), line);
// 實際就等於選的那一階時不必囉嗦地寫兩次
const exact = st({ selectedQuality: 1080, actualResolution: { width: 1920, height: 1080 } });
check('實際就是選的那一階 → 只寫一次「1080p · 1920×1080」',
      PS.statusLine(exact) === '1080p · 1920×1080', PS.statusLine(exact));
// 寬螢幕片：選 1080p、實際高度 804（縮放後就是這樣），差 2px 以內才算「就是它」
const wide = st({ selectedQuality: 720, actualResolution: { width: 1280, height: 720 } });
check('選 720p、實際 1280×720 → 不寫成「上限 720p · 目前 720p」',
      PS.statusLine(wide) === '720p · 1280×720', PS.statusLine(wide));
check('還沒切好時手動模式只講上限', PS.statusLine(
      st({ selectedQuality: 1080, actualResolution: null })) === '1080p 上限');

check('選單標籤要講「上限」而不是「720p」',
      PS.selectedLabel(720, null) === '720p 上限', PS.selectedLabel(720, null));
check('自動就是「自動」', PS.selectedLabel(PS.QUALITY_AUTO, null) === '自動');
check('原畫質帶上片源級別',
      PS.selectedLabel(PS.QUALITY_SOURCE, { label: '1080p' }) === '原畫質 1080p');


// ============================================================ 原畫質 / direct
head('[J] 原畫質與直接串流的講法');

check('原畫質 → 「原畫質 · 1920×804」',
      PS.statusLine(st({ selectedQuality: PS.QUALITY_SOURCE,
                         actualResolution: R.p1080 })) === '原畫質 · 1920×804');
const dir = st({ playMode: 'direct', selectedQuality: PS.QUALITY_SOURCE,
                 actualResolution: R.p1080 });
check('direct → 「直接串流 · 原檔 1080p · 1920×804」',
      PS.statusLine(dir) === '直接串流 · 原檔 1080p · 1920×804', PS.statusLine(dir));


// ============================================================ C / 清狀態
head('[C] 切換之後不可以把舊的階留在 UI 上');

const fresh = PS.emptyState();
check('新狀態沒有 actualResolution', fresh.actualResolution === null);
check('新狀態的 level 索引是 -1（不是 0 —— 0 是一個真的存在的階）',
      fresh.actualLevelIndex === -1, fresh.actualLevelIndex);
check('新狀態沒有 bitrate', fresh.actualBitrate === 0);
check('新狀態什麼都還沒切過', fresh.lastSwitchReason === null);
fresh.playMode = 'hls'; fresh.selectedQuality = 1080;
check('清乾淨之後只講得出「選了什麼」，講不出「在播什麼」',
      PS.statusLine(fresh) === '1080p 上限', PS.statusLine(fresh));
check('levelToState(null) 也回一個乾淨的形狀（不是 undefined）',
      PS.levelToState(null, 0, 0).actualLevelIndex === -1);


// ============================================================ D / LEVEL_SWITCHED 的推導
head('[D] LEVEL_SWITCHED 之後 actualQuality 立即更新，切換原因也對');

const lv = PS.levelToState({ width: 1280, height: 536, bitrate: 2_800_000 }, 1, 3);
check('level 的欄位原樣搬進 state', lv.actualLevelIndex === 1 &&
      lv.actualResolution.width === 1280 && lv.actualBitrate === 2_800_000, lv);
check('master 沒填 RESOLUTION 時 actualResolution 是 null（不要編一個出來）',
      PS.levelToState({ bitrate: 900_000 }, 0, 2).actualResolution === null);
check('第一次切（沒有前一階）= initial', PS.switchReason(0, 2_800_000, false) === 'initial');
check('碼率變低 = abr-down', PS.switchReason(2_800_000, 1_260_000, false) === 'abr-down');
check('碼率變高 = abr-up', PS.switchReason(1_260_000, 2_800_000, false) === 'abr-up');
check('**比的是碼率不是索引**（索引順序由 hls.js 內部決定，不保證跟畫質一致）',
      PS.switchReason(2_800_000, 1_260_000, false) === 'abr-down');
check('手動指定就是 manual，不管碼率往哪邊走',
      PS.switchReason(1_260_000, 2_800_000, true) === 'manual');


// ============================================================ 面板列
head('[J] 資訊面板：片源與「正在播的」要分成兩列，不能混在一起');

const rows = PS.detailRows(manual);
const map = Object.fromEntries(rows);
for (const k of ['片源', '選擇的畫質', '目前實際畫質', '實際解析度', 'HLS level',
                 'Variant bitrate', '估計頻寬', 'Buffer 秒數', '播放模式']) {
  check(`面板有「${k}」這一列`, k in map, Object.keys(map));
}
check('「片源」講的是片源（1080p · 1920×804）', map['片源'] === '1080p · 1920×804', map['片源']);
check('「目前實際畫質」講的是正在播的（720p）', map['目前實際畫質'] === '720p', map['目前實際畫質']);
check('**兩者不一樣時要真的不一樣**（這正是原本混在一起的那個 bug）',
      map['片源'] !== map['目前實際畫質']);
check('「選擇的畫質」講上限', map['選擇的畫質'] === '1080p 上限', map['選擇的畫質']);
const drows = Object.fromEntries(PS.detailRows(dir));
check('direct 的 HLS level 要明講「沒有階梯」，不要留空讓人以為壞了',
      drows['HLS level'].includes('沒有階梯'), drows['HLS level']);


// ============================================================ J / K 卡頓降階與防震盪
head('[J] 遠端低頻寬會降階，但不會跟 hls.js 的 ABR 打架');

const NOW = 1_000_000;
const base = { autoMode: true, levelCount: 3, atLowestLevel: false, stallTimes: [],
               lowBufferSince: 0, lastFragLoadMs: 0, segmentSeconds: 6, lastActionAt: 0 };
const with_ = o => Object.assign({}, base, o);

check('沒事就不要動（這是最重要的一項 —— 動了就是在跟 ABR 打架）',
      PS.shouldStepDown(base, NOW).act === false);
check('只卡一次不算（可能只是伺服器在轉那一段）',
      PS.shouldStepDown(with_({ stallTimes: [NOW - 1000] }), NOW).act === false);
check('30 秒內卡兩次 → 降階',
      PS.shouldStepDown(with_({ stallTimes: [NOW - 20000, NOW - 1000] }), NOW).act === true);
check('卡過但已經是很久以前（超出 30 秒視窗）→ 不算',
      PS.shouldStepDown(with_({ stallTimes: [NOW - 60000, NOW - 45000] }), NOW).act === false);
check('buffer 只是瞬間掉下去（2 秒）→ 不動',
      PS.shouldStepDown(with_({ lowBufferSince: NOW - 2000 }), NOW).act === false);
check('buffer 連續 7 秒都起不來 → 降階',
      PS.shouldStepDown(with_({ lowBufferSince: NOW - 7000 }), NOW).act === true);
check('一段 6 秒卻要下載 8 秒 → 追不上播放，降階',
      PS.shouldStepDown(with_({ lastFragLoadMs: 8000 }), NOW).act === true);
check('一段 6 秒下載 4 秒 → 正常，不動',
      PS.shouldStepDown(with_({ lastFragLoadMs: 4000 }), NOW).act === false);
check('**手動挑了上限就不插手**（上限底下的階交給 hls.js 的 ABR）',
      PS.shouldStepDown(with_({ autoMode: false, stallTimes: [NOW - 2000, NOW - 1000] }),
                        NOW).act === false);
check('已經在最低階 → 無階可降，不要白按',
      PS.shouldStepDown(with_({ atLowestLevel: true, stallTimes: [NOW - 2000, NOW - 1000] }),
                        NOW).act === false);
check('只有一階 → 不動', PS.shouldStepDown(with_({ levelCount: 1,
      stallTimes: [NOW - 2000, NOW - 1000] }), NOW).act === false);
check('剛降過就再喊一次 → 先觀察，不要連續往下踩',
      PS.shouldStepDown(with_({ lastActionAt: NOW - 2000,
                                stallTimes: [NOW - 2000, NOW - 1000] }), NOW).act === false);


head('[K] 頻寬恢復可以升階，但不會在臨界值上下震盪');

const up = { capping: 1, cappedAt: NOW - 60000, stallTimes: [], bufferSeconds: 12,
             bandwidthEstimate: 6_000_000, nextLevelBitrate: 2_800_000 };
const upWith = o => Object.assign({}, up, o);

check('穩定夠久 ＋ buffer 夠 ＋ 頻寬明顯有餘裕 → 交還 ABR',
      PS.shouldRelease(up, NOW).act === true, PS.shouldRelease(up, NOW));
check('沒有壓過階就沒有什麼好放的', PS.shouldRelease(upWith({ capping: -1 }), NOW).act === false);
check('才剛降階 10 秒 → 還不能放（hysteresis）',
      PS.shouldRelease(upWith({ cappedAt: NOW - 10000 }), NOW).act === false);
check('這段期間又卡過 → 不能放',
      PS.shouldRelease(upWith({ stallTimes: [NOW - 5000] }), NOW).act === false);
check('buffer 只有 3 秒 → 不能放（升上去馬上又會卡）',
      PS.shouldRelease(upWith({ bufferSeconds: 3 }), NOW).act === false);
// **這一項就是防震盪的關鍵**：頻寬「剛好等於」上一階需求時不能升 ——
// 剛好等於就是上次卡掉的那個點，回去只會再卡一次。
check('頻寬剛好等於上一階需求（2.8M vs 2.8M）→ 不能升，不然就是 720→480→720 震盪',
      PS.shouldRelease(upWith({ bandwidthEstimate: 2_800_000 }), NOW).act === false);
check('頻寬只多一點點（×1.2）也還不夠，要 ×1.4 以上',
      PS.shouldRelease(upWith({ bandwidthEstimate: 3_360_000 }), NOW).act === false);
check('頻寬到了 ×1.5 才放行',
      PS.shouldRelease(upWith({ bandwidthEstimate: 4_200_000 }), NOW).act === true);

// 模擬一次完整的震盪測試：降階 → 網路在臨界值上下抖 → 不可以來回切
(function noFlapping() {
  let released = 0;
  let m = { capping: 1, cappedAt: NOW, stallTimes: [], bufferSeconds: 10,
            bandwidthEstimate: 2_800_000, nextLevelBitrate: 2_800_000 };
  for (let i = 1; i <= 60; i++) {           // 每秒 tick 一次，跑 60 秒
    const t = NOW + i * 1000;
    // 頻寬在臨界值上下抖動（±10%）—— 沒有 hysteresis 的話這裡會一直切
    m.bandwidthEstimate = 2_800_000 * (i % 2 ? 1.1 : 0.9);
    if (PS.shouldRelease(m, t).act) { released++; m.capping = -1; }
  }
  check('頻寬在臨界值上下抖 60 秒，一次都不該放手（不然就是每幾秒震盪一次）',
        released === 0, released);
})();


// ============================================================ G / H direct fallback
head('[G][H] Direct → HLS：什麼時候切、而且只能切一次');

const d = { playMode: 'direct', fellBack: false, stallTimes: [], currentStallMs: 0,
            directAdvised: true };
const dWith = o => Object.assign({}, d, o);

check('播得順就不要動它（direct 畫質最好、成本最低）',
      PS.shouldFallbackFromDirect(d, NOW).act === false);
check('卡一次不算', PS.shouldFallbackFromDirect(dWith({ stallTimes: [NOW - 1000] }), NOW).act === false);
check('40 秒內卡三次 → 切',
      PS.shouldFallbackFromDirect(dWith({
        stallTimes: [NOW - 30000, NOW - 15000, NOW - 1000] }), NOW).act === true);
check('單一次就卡超過 12 秒 → 切（那不是在緩衝，是餵不動）',
      PS.shouldFallbackFromDirect(dWith({ currentStallMs: 13000 }), NOW).act === true);
check('伺服器本來就說這條鏈路餵不動 → 門檻收緊到兩次',
      PS.shouldFallbackFromDirect(dWith({ directAdvised: false,
        stallTimes: [NOW - 10000, NOW - 1000] }), NOW).act === true);
check('伺服器說沒問題時兩次還不切（尊重 direct 的優勢，別太急）',
      PS.shouldFallbackFromDirect(dWith({ directAdvised: true,
        stallTimes: [NOW - 10000, NOW - 1000] }), NOW).act === false);
// H 案：切過之後舊的 direct 事件還會飄進來，不能再切第二次
check('**已經切過就不准再切**（舊的 direct error 事件會在切換之後才到）',
      PS.shouldFallbackFromDirect(dWith({ fellBack: true, currentStallMs: 30000,
        stallTimes: [NOW - 3000, NOW - 2000, NOW - 1000] }), NOW).act === false);
check('已經在 HLS 了就不關這支的事',
      PS.shouldFallbackFromDirect(dWith({ playMode: 'hls', currentStallMs: 30000 }),
                                  NOW).act === false);


// ============================================================ 手機開播策略
head('[J 3] 手機遠端寧可先低一階快點開播，不要賭最高畫質然後卡十幾秒');

check('區網不必壓', PS.startupPolicy({ remote: false, mobile: true }).capIndex === -1);
check('遠端桌機交給 ABR', PS.startupPolicy({ remote: true, mobile: false }).capIndex === -1);
check('手機遠端要壓一階', PS.startupPolicy({ remote: true, mobile: true }).capIndex === 1,
      PS.startupPolicy({ remote: true, mobile: true }));
// **這一項最重要**：iPhone 沒有 Network Information API，拿不到資訊時
// 絕對不能因此假設網路很好。
check('拿不到 Network Information（iPhone 就是這一類）仍然要保守，不能因此放寬',
      PS.startupPolicy({ remote: true, mobile: true, effectiveType: undefined,
                         downlink: undefined }).capIndex === 1);
check('2g 直接壓到最低階',
      PS.startupPolicy({ remote: true, mobile: true, effectiveType: '2g' }).capIndex === 0);
check('3g 壓一階',
      PS.startupPolicy({ remote: true, mobile: true, effectiveType: '3g' }).capIndex === 1);
check('下行只有 1 Mbps → 壓一階',
      PS.startupPolicy({ remote: true, mobile: true, downlink: 1 }).capIndex === 1);
check('4g ＋ 下行夠 → 還是壓一階（開播保守，之後由 ABR 往上爬）',
      PS.startupPolicy({ remote: true, mobile: true, effectiveType: '4g',
                         downlink: 10 }).capIndex === 1);
check('使用者開了節省流量 → 直接最低階，這是他明確表達過的意思',
      PS.startupPolicy({ remote: true, mobile: true, effectiveType: '4g',
                         downlink: 20, saveData: true }).capIndex === 0);
check('區網 ＋ saveData → 仍然尊重使用者（省流量不分區網遠端）',
      PS.startupPolicy({ remote: false, mobile: true, saveData: true }).capIndex === 0);

check('螢幕上限要把 devicePixelRatio 算進去（3x 的 390px 手機 = 1170）',
      PS.screenCapHeight({ viewportWidth: 390, viewportHeight: 844,
                           devicePixelRatio: 3 }) === 2532,
      PS.screenCapHeight({ viewportWidth: 390, viewportHeight: 844, devicePixelRatio: 3 }));
check('dpr 超過 3 就當 3（再高沒有實益，只會選過高的階）',
      PS.screenCapHeight({ viewportWidth: 100, viewportHeight: 100,
                           devicePixelRatio: 6 }) === 300);


console.log('\n' + '='.repeat(50));
console.log(`通過 ${OK}，失敗 ${FAIL}`);
process.exit(FAIL ? 1 : 0);
