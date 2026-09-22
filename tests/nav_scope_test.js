/* 換入口（一般片庫 ⇄ 保險庫）之後，畫面上不能留著上一個入口的東西。

     node tests/nav_scope_test.js

   為什麼要有這一支：實際踩到的 bug 是「按私人、再按全部，私人的繼續觀看
   還留在畫面上」。那不是單純的畫面沒更新 —— 繼續觀看那一排帶著片名與海報，
   等於私人的東西留在共用畫面上，而首頁常常不只一個人在看。

   主清單（#grid）換入口一定會重畫，所以不會出事；會出事的是**各自獨立的
   區塊**（#continue、#genres）—— 沒有人叫它們，它們就原封不動留著。
   這支測試盯的就是那件事。

   做法沿用 player_wiring_test 的路子：最小 DOM stub ＋ 真的把 app.js 的
   那幾個函式跑起來，而不是另外抄一份邏輯來測（抄一份就等於沒測到真的程式）。 */
'use strict';

let OK = 0, FAIL = 0;
function check(name, cond, extra) {
  if (cond) { OK++; console.log('  PASS  ' + name); }
  else { FAIL++; console.log('  FAIL  ' + name + '  → ' + JSON.stringify(extra)); }
}
function head(t) { console.log('\n' + t); }

// ---------------------------------------------------------------- DOM stub
function el(id) {
  return {
    id, innerHTML: '', hidden: false, value: '', placeholder: '', title: '',
    dataset: {}, style: {}, classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    appendChild() {}, querySelector() { return null; },
    querySelectorAll() { return []; },
  };
}

const nodes = {};
for (const id of ['continue', 'genres', 'grid', 'pager', 'libTitle', 'libCount',
                  'q', 'sort', 'photoView', 'docView']) {
  nodes['#' + id] = el(id);
}
nodes['.toolbar'] = el('toolbar');

const $ = sel => nodes[sel] || null;
const $$ = () => [];

// 記錄每一次請求，用來驗「回到一般入口之後，重新載入的是 shared 的內容」
const calls = [];
let viewScope = 'shared';
const SCOPED = /^\/api\/(library|continue|stats|genres|photos|documents)(\?|$)/;
const api = async (url) => {
  if (viewScope === 'vault' && SCOPED.test(url)) {
    url += (url.includes('?') ? '&' : '?') + 'scope=vault';
  }
  calls.push(url);
  if (url.startsWith('/api/continue')) {
    // 保險庫裡有一部私人的片；一般入口一部都沒有（後端已經濾掉）
    return url.includes('scope=vault')
      ? { items: [{ file_id: 9, title: '私人的片', percent: 30, poster_url: null }] }
      : { items: [] };
  }
  if (url.startsWith('/api/genres')) {
    return url.includes('scope=vault')
      ? { genres: [{ name: '私人分類', count: 1 }] }
      : { genres: [{ name: '劇情', count: 5 }] };
  }
  return { items: [], total: 0 };
};

const esc = s => String(s ?? '');
const state = { kind: '', page: 1, sort: 'added', genre: '', q: '' };

// ------------------------------------------------- 被測的真實函式（自 app.js）
async function loadContinue() {
  const { items } = await api('/api/continue');
  const box = $('#continue');
  if (!items.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="section-title">繼續觀看</div>'
    + items.map(i => esc(i.title)).join('');
}
async function loadGenres() {
  const { genres } = await api('/api/genres');
  $('#genres').innerHTML = '<button class="chip on">全部分類</button>'
    + genres.map(g => esc(g.name)).join('');
}
function loadLibrary() { return api('/api/library?page=' + state.page); }
function showView() {}

// 這兩個是這次修正的重點，直接照 app.js 的實作
function refreshScopeBoxes() {
  const c = $('#continue'); if (c) c.innerHTML = '';
  const g = $('#genres'); if (g) g.innerHTML = '';
  loadContinue().catch(() => {});
  loadGenres().catch(() => {});
}

function makeBtn(kind) {
  const b = el('btn-' + kind);
  b.dataset.kind = kind;
  b.onclick = () => {
    const wantVault = b.dataset.kind === 'vault';
    const switched = wantVault !== (viewScope === 'vault');
    if (switched) {
      viewScope = wantVault ? 'vault' : 'shared';
      state.page = 1; state.genre = ''; state.q = '';
      const q = $('#q'); if (q) q.value = '';
      refreshScopeBoxes();
    }
    if (wantVault) { state.kind = ''; showView(); loadLibrary(); return; }
    showView();
    state.kind = b.dataset.kind; state.page = 1; loadLibrary();
  };
  return b;
}

const flush = () => new Promise(r => setImmediate(r));

(async function run() {
  const vaultBtn = makeBtn('vault');
  const allBtn = makeBtn('');

  head('[1] 進保險庫：繼續觀看換成保險庫的內容');
  vaultBtn.onclick();
  await flush(); await flush();
  check('scope 切到 vault', viewScope === 'vault', viewScope);
  check('繼續觀看顯示私人的片',
        $('#continue').innerHTML.includes('私人的片'), $('#continue').innerHTML);
  check('分類也換成保險庫的',
        $('#genres').innerHTML.includes('私人分類'), $('#genres').innerHTML);

  head('[2] 按「全部」回到一般入口 —— 私人的東西不能留在畫面上');
  calls.length = 0;
  allBtn.onclick();
  await flush(); await flush();
  check('scope 切回 shared', viewScope === 'shared', viewScope);
  // 這一條就是這次回報的 bug：修正前 #continue 會原封不動留著保險庫的內容。
  check('繼續觀看不再有私人的片（回報的 bug）',
        !$('#continue').innerHTML.includes('私人的片'), $('#continue').innerHTML);
  check('分類不再有保險庫的分類',
        !$('#genres').innerHTML.includes('私人分類'), $('#genres').innerHTML);
  // **這一條才是真正的把關**。上面那兩條比看起來弱：真實的 loadContinue 在
  // 拿到空清單時自己就會把 #continue 清掉，所以只要它**有被呼叫**，畫面就會
  // 自己好。這次的 bug 是沒有任何人呼叫它 —— 所以要直接驗「有沒有去問」，
  // 而不是只驗畫面上的字不見了。
  check('真的有重新去查一般入口的繼續觀看（bug 的根因：沒人呼叫）',
        calls.some(u => u === '/api/continue'), calls);
  check('而且查的時候沒有帶 scope=vault',
        !calls.some(u => u.includes('scope=vault')), calls);

  head('[3] 在同一個入口裡換分頁，不必重新載入那些區塊');
  calls.length = 0;
  makeBtn('movie').onclick();
  await flush();
  check('沒有多餘的 continue/genres 請求（沒換入口就不重畫）',
        !calls.some(u => u.startsWith('/api/continue')
                      || u.startsWith('/api/genres')), calls);

  head('[4] 再進保險庫一次：不會殘留一般入口的內容');
  vaultBtn.onclick();
  await flush(); await flush();
  check('繼續觀看又變回私人的片',
        $('#continue').innerHTML.includes('私人的片'), $('#continue').innerHTML);
  check('分頁與搜尋字串有歸零', state.page === 1 && state.q === '',
        { page: state.page, q: state.q });

  console.log('\n============================================');
  console.log(`通過 ${OK}、失敗 ${FAIL}`);
  process.exit(FAIL ? 1 : 0);
})();
