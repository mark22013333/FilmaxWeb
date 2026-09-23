/* 範圍（共用 ⇄ 私人）與媒體類型（影片／相片／文件）是兩個獨立的維度。

     node tests/nav_scope_test.js

   這支測試盯三件事：

   1. **換媒體不會離開保險庫。** 舊版把「私人」做成跟「電影」「相片」並列的一個
      分類，進保險庫只能看影片，從保險庫點「相片」就被切回一般片庫 ——
      後端支援保險庫裡的相片與 PDF，前端卻沒有路走過去。
   2. **換 scope 時，上一個 scope 的東西立刻從畫面上消失**，包括目前沒在看的
      那幾個檢視（相片牆、文件牆的 DOM 一直都在）。回報過的 bug 是「按私人、
      再按全部，私人的繼續觀看還留在畫面上」—— 片名與海報留在共用畫面上。
   3. **換 scope 之前發出、之後才回來的回應要丟掉**，否則它會把私人內容畫進
      已經是共用的畫面（清空只擋得住已經畫上去的東西）。

   做法：**把真的 app.js 整支載進 vm 跑**，配一個最小的 DOM／fetch stub。
   舊版是把 app.js 的幾個函式抄一份過來測 —— 抄的那份跟真的那份一旦分岔，
   測試就綠著、程式卻壞著，而這次的重構正好會讓它們分岔。 */
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

// ---------------------------------------------------------------- DOM stub
function classList() {
  const s = new Set();
  return {
    _s: s,
    add(...c) { c.forEach(x => s.add(x)); }, remove(...c) { c.forEach(x => s.delete(x)); },
    toggle(c, on) { if (on === undefined) on = !s.has(c); on ? s.add(c) : s.delete(c); return on; },
    contains(c) { return s.has(c); },
  };
}
function el(tag = 'div') {
  const attrs = {};
  const e = {
    tagName: tag.toUpperCase(), innerHTML: '', textContent: '', hidden: false, value: '',
    placeholder: '', title: '', type: '', className: '', disabled: false,
    dataset: {}, style: {}, classList: classList(), children: [],
    setAttribute(k, v) { attrs[k] = String(v); }, getAttribute(k) { return attrs[k] ?? null; },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { e.children.push(c); return c; },
    insertBefore(c) { e.children.unshift(c); return c; },
    insertAdjacentHTML(_pos, html) { e.innerHTML += html; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, focus() {},
    getBoundingClientRect() { return { top: 0, height: 0, width: 0 }; },
  };
  return e;
}

// 依選擇器建立（並記住）節點。app.js 用到的 id 都會落在這裡。
const nodes = {};
const $node = sel => (nodes[sel] || (nodes[sel] = el()));
// 標記裡本來就是 hidden 的兩塊
$node('#photoView').hidden = true;
$node('#docView').hidden = true;

// nav 裡的分類按鈕，跟 index.html 同一組屬性
const NAV = [['video', ''], ['video', 'movie'], ['video', 'tv'], ['video', 'jav'],
             ['video', 'home'], ['photo', 'photo'], ['doc', 'doc']].map(([media, kind]) => {
  const b = el('button');
  b.dataset.media = media; b.dataset.kind = kind;
  return b;
});
const navBtn = (media, kind = media === 'video' ? '' : media) =>
  NAV.find(b => b.dataset.media === media && b.dataset.kind === kind);

const document = {
  body: el('body'),
  documentElement: { scrollTop: 0 },
  querySelector: sel => $node(sel),
  querySelectorAll: sel => (sel === 'header nav [data-media]' ? NAV : []),
  createElement: tag => el(tag),
  addEventListener() {},
};

// ---------------------------------------------------------------- fetch stub
// 保險庫與一般入口各有一份「會出現在畫面上的名字」。測試的判斷就是：
// 在共用畫面上找不到任何一個 PRIVATE 裡的字。
const PRIVATE = ['私人的片', '私人分類', '私人相片.jpg', '私人相簿', '私人文件.pdf', '私人資料夾'];
const calls = [];
const held = [];             // 被刻意卡住的回應：{url, release}
let holdPattern = null;      // 符合的請求先不回，模擬慢速網路

function body(url) {
  const u = new URL(url, 'http://x');
  const vault = u.searchParams.get('scope') === 'vault';
  const p = u.pathname;
  if (p === '/api/me') return { role: 'viewer', is_admin: false, auth_enabled: true, uid: 7 };
  if (p === '/api/vault') return { has_vault: true, counts: { videos: 1, photos: 1, documents: 1 },
                                   total: 3, items: 1, folders: ['私人'] };
  if (p === '/api/scan/status') return { running: false };
  if (p === '/api/continue') return { items: vault ? [{ file_id: 9, title: '私人的片', percent: 30 }] : [] };
  if (p === '/api/genres') return { genres: [{ name: vault ? '私人分類' : '劇情', count: 1 }] };
  if (p === '/api/library') return { total: 1, page: 1, page_size: 60,
    items: [{ id: vault ? 2 : 1, title: vault ? '私人的片' : '公開的片', kind: 'movie' }] };
  if (p === '/api/photos/folders') return { items: [{ folder: vault ? '/媒體/私人相簿' : '/媒體/公開相簿', c: 1 }] };
  if (p === '/api/photos') return { total: 1, page: 1, page_size: 60, preview_px: 1920,
    items: [{ id: vault ? 2 : 1, filename: vault ? '私人相片.jpg' : '公開相片.jpg', width: 1, height: 1 }] };
  if (p === '/api/documents/folders') return { prefix: '', items: [{ folder: vault ? '/媒體/私人資料夾' : '/媒體/公開資料夾',
    name: vault ? '私人資料夾' : '公開資料夾', c: 1, has_children: false }] };
  if (p === '/api/documents') return { total: 1, page: 1, page_size: 60,
    items: [{ id: vault ? 2 : 1, folder: '/媒體/x', filename: vault ? '私人文件.pdf' : '公開文件.pdf', size: 1 }] };
  return { items: [], total: 0 };
}

async function fetch(url, opts = {}) {
  calls.push({ url, method: (opts.method || 'GET').toUpperCase() });
  const resp = { ok: true, status: 200, json: async () => body(url), text: async () => '' };
  if (holdPattern && holdPattern.test(url)) {
    return new Promise(res => held.push({ url, release: () => res(resp) }));
  }
  return resp;
}

// ---------------------------------------------------------------- 載入真的 app.js
const sandbox = {
  document, fetch, console, URL, URLSearchParams, Promise, Math, JSON, String, Number,
  Object, Array, Set, Map, Error, RegExp, parseInt,
  setTimeout, clearTimeout,
  setInterval: () => 0, clearInterval() {},        // 回頂端的輪詢：測試不需要，也不能讓 node 停不下來
  addEventListener() {}, removeEventListener() {},
  matchMedia: () => ({ matches: false }),
  localStorage: { getItem: () => null, setItem() {} },
  IntersectionObserver: class { observe() {} disconnect() {} },
  CSS: { escape: s => String(s) },
  location: { href: '', reload() {} },
  innerWidth: 1280, scrollY: 0, scrollTo() {},
  prompt: () => null, confirm: () => false,
};
sandbox.window = sandbox;
vm.createContext(sandbox);

const src = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'app.js'), 'utf8');
// 頂層的 const/let 不會掛到 global 上，所以在**同一支 script 的尾巴**把要看的東西
// 交出來 —— 這樣拿到的是真的那一份，不是測試自己複製的。
vm.runInContext(src + `
;globalThis.__t = { viewState, state, pstate, dstate, dtree, pfilter,
                    setScope, setMedia, scopeButtons, api };`, sandbox, { filename: 'app.js' });
const T = sandbox.__t;

const flush = async (n = 6) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };
const VIEW_DOM = ['#grid', '#continue', '#genres', '#libCount', '#pgrid', '#folderBar',
                  '#photoCount', '#albumToggleLabel', '#docTreeBody', '#dgrid', '#docCount',
                  '#docCrumbs', '#modal'];
const screenText = () => VIEW_DOM.map(s => `${nodes[s] ? nodes[s].innerHTML + nodes[s].textContent : ''}`).join('\n');
const leaked = () => PRIVATE.filter(w => screenText().includes(w));
const since = n => calls.slice(n).filter(c => c.method === 'GET').map(c => c.url);
const scopeBtn = s => T.scopeButtons.find(b => b.dataset.scope === s);

(async function run() {
  await flush(10);

  head('[0] 初始：共用 · 影片');
  check('一開始是 shared / video', T.viewState.scope === 'shared' && T.viewState.media === 'video',
        T.viewState);
  check('有授權 → 長出範圍開關（共用／私人兩顆）', T.scopeButtons.length === 2,
        T.scopeButtons.map(b => b.dataset.scope));
  check('私人的數量只在提示裡，不印在按鈕上',
        !/\d/.test(scopeBtn('vault').innerHTML.replace(/<svg[\s\S]*?<\/svg>/, ''))
        && scopeBtn('vault').title.includes('相片 1'), scopeBtn('vault'));
  check('共用畫面上沒有任何私人的字', leaked().length === 0, leaked());
  check('一般入口的請求都沒帶 scope', !calls.some(c => c.url.includes('scope=')),
        calls.map(c => c.url));

  head('[1] Shared Video → Vault Video');
  let n = calls.length;
  scopeBtn('vault').onclick();
  await flush();
  check('scope 變成 vault、media 還是 video',
        T.viewState.scope === 'vault' && T.viewState.media === 'video', T.viewState);
  check('影片清單換成私人的', nodes['#grid'].innerHTML.includes('私人的片'), nodes['#grid'].innerHTML);
  check('繼續觀看換成私人的', nodes['#continue'].innerHTML.includes('私人的片'));
  check('分類換成私人的', nodes['#genres'].innerHTML.includes('私人分類'));
  check('這一輪的每一個 GET 都帶 scope=vault', since(n).length > 0
        && since(n).every(u => u.includes('scope=vault')), since(n));
  check('body 有 in-vault（header 的細線提示）', document.body.classList.contains('in-vault'));
  check('範圍開關標到「私人」', scopeBtn('vault').classList.contains('on')
        && scopeBtn('vault').getAttribute('aria-pressed') === 'true'
        && !scopeBtn('shared').classList.contains('on'));

  head('[2] Vault Video → Vault Photo（換媒體不能偷偷回 Shared）');
  n = calls.length;
  navBtn('photo').onclick();
  await flush();
  check('scope 還是 vault', T.viewState.scope === 'vault', T.viewState);
  check('media 變成 photo', T.viewState.media === 'photo', T.viewState);
  check('相片的請求帶 scope=vault',
        since(n).some(u => u.startsWith('/api/photos?') && u.includes('scope=vault')), since(n));
  check('相簿的請求帶 scope=vault',
        since(n).some(u => u.startsWith('/api/photos/folders') && u.includes('scope=vault')), since(n));
  check('沒有任何一個請求落到 shared', since(n).every(u => u.includes('scope=vault')), since(n));
  check('相片牆顯示私人的相片', nodes['#pgrid'].innerHTML.includes('私人相片.jpg'), nodes['#pgrid'].innerHTML);
  check('相片檢視打開、影片格線藏起來', !nodes['#photoView'].hidden && nodes['#grid'].hidden);
  check('分類按鈕標到「相片」', navBtn('photo').classList.contains('on')
        && !navBtn('video').classList.contains('on'));

  head('[3] Vault Photo → Vault Document');
  n = calls.length;
  navBtn('doc').onclick();
  await flush();
  check('scope 還是 vault、media 變成 doc',
        T.viewState.scope === 'vault' && T.viewState.media === 'doc', T.viewState);
  check('文件清單與資料夾樹的請求都帶 scope=vault',
        since(n).some(u => u.startsWith('/api/documents?') && u.includes('scope=vault'))
        && since(n).some(u => u.startsWith('/api/documents/folders') && u.includes('scope=vault')),
        since(n));
  check('沒有任何一個請求落到 shared', since(n).every(u => u.includes('scope=vault')), since(n));
  check('文件牆顯示私人的文件', nodes['#dgrid'].innerHTML.includes('私人文件.pdf'));

  head('[4] Vault Document → Shared Document（換 scope：私人的東西立刻消失）');
  // 先在保險庫裡留下一些「選取」與「搜尋」，確認它們不會跟著帶到共用去
  T.dstate.folder = '/媒體/私人資料夾'; T.pstate.folder = '/媒體/私人相簿';
  T.state.q = '私人'; nodes['#q'].value = '私人'; T.state.genre = '私人分類'; T.state.page = 3;
  T.dtree.open.add('/媒體/私人資料夾');
  // 慢速網路：共用那邊的回應先卡住，才看得到「換過去的那一瞬間」畫面上有什麼
  holdPattern = /./;
  n = calls.length;
  scopeBtn('shared').onclick();
  check('scope 變成 shared、media 還是 doc',
        T.viewState.scope === 'shared' && T.viewState.media === 'doc', T.viewState);
  // **還沒有任何新回應** —— 這一刻畫面上就不能有私人的東西
  check('新內容還沒回來之前，私人的字已經全部不見（含藏著的相片牆與繼續觀看）',
        leaked().length === 0, leaked());
  check('搜尋、分頁、分類、相簿、資料夾選取都歸零',
        T.state.q === '' && nodes['#q'].value === '' && T.state.genre === '' && T.state.page === 1
        && T.dstate.folder === '' && T.pstate.folder === '' && T.dtree.open.size === 0
        && T.dtree.kids.size === 0 && T.pfilter.items.length === 0,
        { q: T.state.q, genre: T.state.genre, page: T.state.page, d: T.dstate.folder, p: T.pstate.folder });
  holdPattern = null;
  held.splice(0).forEach(h => h.release());
  await flush();
  check('共用的文件載回來了', nodes['#dgrid'].innerHTML.includes('公開文件.pdf'), nodes['#dgrid'].innerHTML);
  check('這一輪的請求都沒帶 scope=vault', since(n).every(u => !u.includes('scope=vault')), since(n));
  check('真的有重新去查一般入口的繼續觀看（不是只把畫面清空）',
        since(n).some(u => u === '/api/continue'), since(n));
  check('共用畫面上沒有任何私人的字', leaked().length === 0, leaked());
  check('body 的 in-vault 拿掉了', !document.body.classList.contains('in-vault'));

  head('[5] 換 scope 之前發出、之後才回來的回應要丟掉');
  scopeBtn('vault').onclick();
  await flush();
  navBtn('photo').onclick();
  await flush();
  // 保險庫裡再點一次相片，但這次回應卡住；在它回來之前切回共用
  holdPattern = /scope=vault/;
  navBtn('photo').onclick();
  await flush(2);
  const pending = held.length;
  scopeBtn('shared').onclick();
  holdPattern = null;
  await flush();
  held.splice(0).forEach(h => h.release());     // 私人的回應這時才到
  await flush();
  check('有攔到晚到的私人回應（測試本身有效）', pending > 0, pending);
  check('晚到的私人回應沒有被畫進共用畫面', leaked().length === 0, leaked());
  check('相片牆是共用的內容', nodes['#pgrid'].innerHTML.includes('公開相片.jpg'), nodes['#pgrid'].innerHTML);

  head('[6] 在同一個 scope 裡換分類，不必重新載入繼續觀看與分類');
  navBtn('video').onclick();
  await flush();
  n = calls.length;
  navBtn('video', 'movie').onclick();
  await flush();
  check('換到「電影」只重查清單', since(n).every(u => u.startsWith('/api/library')), since(n));
  check('而且帶著 kind=movie', since(n).some(u => u.includes('kind=movie')), since(n));
  check('videoKind 記在 viewState', T.viewState.videoKind === 'movie', T.viewState);

  head('[7] 保險庫裡的影片類型也照樣可以選');
  n = calls.length;
  scopeBtn('vault').onclick();
  await flush();
  check('換 scope 不會把影片類型洗掉（media/kind 是另一個維度）',
        T.viewState.media === 'video' && T.viewState.videoKind === 'movie', T.viewState);
  check('保險庫的清單帶著 kind=movie 與 scope=vault',
        since(n).some(u => u.startsWith('/api/library') && u.includes('kind=movie')
                           && u.includes('scope=vault')), since(n));

  head('[8] api() 的 scope 注入：只看 viewState，不看端點名稱');
  n = calls.length;
  await T.api('/api/some-future-list');
  check('保險庫裡，連一支不認識的新端點也會帶 scope=vault（沒有 allow-list 可以漏）',
        since(n).some(u => u === '/api/some-future-list?scope=vault'), since(n));
  n = calls.length;
  await T.api('/api/prefs', { method: 'POST', body: '{}' });
  check('非 GET 不帶 scope', calls.slice(n).every(c => !c.url.includes('scope=')), calls.slice(n));
  n = calls.length;
  await T.api('/api/library?scope=shared');
  check('已經明確帶了 scope 的網址不重複加', since(n)[0] === '/api/library?scope=shared', since(n));
  check('app.js 裡沒有端點名稱的 scope 正則了', !/SCOPED\s*=/.test(src));
  check('app.js 沒有用 cookie 記 scope', !/document\.cookie/.test(src));

  console.log('\n============================================');
  console.log(`通過 ${OK}、失敗 ${FAIL}`);
  process.exit(FAIL ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
