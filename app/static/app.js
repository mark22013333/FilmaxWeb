const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (url, opt = {}) => {
  // 有 body 就一定要帶 Content-Type: application/json —— FastAPI 的 Pydantic
  // 是看這個標頭決定要不要解析 body 的，少了它會回 422 而不是「欄位錯」，
  // 而 422 的訊息看起來完全不像「你忘了設標頭」。
  const opts = opt.body && !opt.headers
    ? { ...opt, headers: { 'Content-Type': 'application/json' } } : opt;
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = (await r.text()).slice(0, 300);
    try { const j = JSON.parse(msg); if (j.detail) msg = typeof j.detail === 'string'
      ? j.detail : JSON.stringify(j.detail); } catch (e) { /* 不是 JSON 就用原文 */ }
    throw new Error(msg || r.status);
  }
  return r.json();
};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const gb = b => !b ? '-' : (b >= 1024 ** 3 ? (b / 1024 ** 3).toFixed(2) + ' GB' : (b / 1024 ** 2).toFixed(0) + ' MB');
const hhmm = s => {
  if (!s) return '';
  s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return `${h} 小時 ${m} 分`;
  return m ? `${m} 分` : `${s} 秒`;
};
// 目前登入者的身分。唯讀角色不該看到下載與管理功能 —— 後端已經擋掉了，
// 前端隱藏是為了不要給出按了會失敗的按鈕。
let me = { role: 'admin', is_admin: true };

async function loadMe() {
  try { me = await api('/api/me'); } catch {}
  document.body.classList.toggle('viewer', !me.is_admin);
  // 後台入口只給管理員看。真正的保護在 /admin 與每個 API 端點上，
  // 這裡只是不要在唯讀使用者面前放一個按了會被拒絕的連結。
  const adminLink = $('#btnAdmin');
  if (adminLink) adminLink.hidden = !me.is_admin;
  paintAccount();
}

function paintAccount() {
  const btn = $('#btnAccount');
  if (!btn) return;
  // 沒開驗證時整顆按鈕都沒意義（區網直接進來，沒有「誰」可言）
  btn.hidden = !me.auth_enabled;
  $('#acctName').textContent = me.display_name || me.email || (me.is_admin ? '管理員' : '唯讀');
  const n = me.pending_users || 0;
  const b = $('#acctBadge');
  b.hidden = !n;
  b.textContent = n;
  btn.title = me.email ? `${me.email}（${me.is_admin ? '管理員' : '唯讀'}）` : '帳號';
}

/* 換頁時捲到「格線的頂端」而不是文件頂端。
   原本是 window.scrollTo({top:0})，會捲到標頭、搜尋列、資料夾標籤之上，
   離第一列還有一大段，等於每次換頁都要再往下捲一次。 */
function scrollToGrid(sel) {
  const grid = $(sel);
  if (!grid) return;
  const header = document.querySelector('header');
  // 標頭是 sticky 的，捲過去之後它會蓋住格線頂端，所以要扣掉它的高度。
  // 動態量而不是寫死 —— 手機上標頭會換行，高度跟桌機不一樣。
  const offset = (header ? header.getBoundingClientRect().height : 0) + 8;
  const target = Math.max(0, grid.getBoundingClientRect().top + window.scrollY - offset);
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  window.scrollTo({ top: target, behavior: reduce ? 'auto' : 'smooth' });
}

/* 「要不要捲」必須在換內容**之前**判斷。
   換頁時格線會先被清成「載入中」，頁面高度瞬間塌掉，瀏覽器把捲動位置
   夾到 0 —— 這時候再問「使用者是不是已經在上面了」，答案永遠是「是」，
   於是永遠不捲。所以先記下來，渲染完再決定。 */
async function pageTo(loader, sel) {
  const wasScrolled = window.scrollY > 4;
  await loader();
  if (wasScrolled) scrollToGrid(sel);
}

/* ------------------------- 分頁器（媒體庫／相片／文件共用） -------------------------

   原本三個列表各寫一份上一頁／下一頁，而且行為已經開始分岔（媒體庫走全域的
   window.go，另外兩個各自綁 data-pp）。併成一份不是為了少幾行 ——
   而是「加跳頁按鈕」如果要改三次，第四個列表出現時就變成改四次。

   版面：首末頁固定顯示 ＋ 目前頁附近展開 ＋ 中間省略。
     第 1 頁：   [上一頁] (1) 2 3 4 5 … 335 [下一頁]
     第 8 頁：   [上一頁] 1 … 6 7 (8) 9 10 … 335 [下一頁]
   省略號是純文字不可點：有些實作讓它跳 ±10 頁，但那個行為沒有任何視覺
   提示，點下去的結果無法預期。                                            */
const PAGER_JUMP_MIN = 20;      // 超過這麼多頁才給「跳到第幾頁」的輸入框

// 目前頁前後各留幾頁。手機螢幕塞不下，收成 1。
const pagerWindow = () => (window.innerWidth < 560 ? 1 : 2);

// 要顯示哪些頁碼。回傳陣列，'…' 代表省略號。
function pagerPages(page, pages, win) {
  const out = [];
  const lo = Math.max(2, page - win), hi = Math.min(pages - 1, page + win);
  out.push(1);
  // 只跳過一頁的話就直接列出來 —— 省略號跟數字一樣寬，藏一個 2 沒有意義
  if (lo > 2) out.push(lo === 3 ? 2 : '…');
  for (let i = lo; i <= hi; i++) out.push(i);
  if (hi < pages - 1) out.push(hi === pages - 2 ? pages - 1 : '…');
  if (pages > 1) out.push(pages);
  return out;
}

// 每個分頁器最後一次畫的參數。resize 要重畫時得知道畫什麼。
// 宣告放在 paintPager 前面：const 不像 function 會提升，雖然實際呼叫都在
// 載入之後（不會踩到 TDZ），但讓「宣告在使用之前」是看得出來的。
const _pagerLast = new Map();

/** 畫出分頁器並塞進 sel。記住參數，resize 時才重畫得出來。 */
function paintPager(sel, { page, total, pageSize }) {
  _pagerLast.set(sel, { page, total, pageSize });
  const box = $(sel);
  if (box) box.innerHTML = renderPagerHTML({ page, total, pageSize });
}

function renderPagerHTML({ page, total, pageSize }) {
  const pages = Math.ceil(total / pageSize);
  if (pages <= 1) return '';
  // 夾住頁碼再畫。超出範圍是真的會發生的：停在最後一頁時把相片刪掉，
  // 總數變少而 page 沒變 —— 那時整排頁碼會一個都沒有「目前頁」的樣式。
  page = Math.min(Math.max(1, page), pages);
  const win = pagerWindow();
  const btn = n => n === '…'
    ? `<span class="gap">…</span>`
    : `<button class="pg${n === page ? ' on' : ''}" data-pg="${n}"
         ${n === page ? 'aria-current="page"' : ''}>${n}</button>`;
  const jump = pages >= PAGER_JUMP_MIN
    ? `<span class="jump">跳到
         <input type="number" min="1" max="${pages}" value="${page}"
                inputmode="numeric" aria-label="跳到第幾頁"> 頁</span>` : '';
  return `<div class="pager">
    <button class="btn" data-pg="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一頁</button>
    <span class="nums">${pagerPages(page, pages, win).map(btn).join('')}</span>
    <button class="btn" data-pg="${page + 1}" ${page >= pages ? 'disabled' : ''}>下一頁</button>
    ${jump}</div>`;
}

// 轉螢幕方向或改視窗寬度時，window 從 2 變 1（或反過來）要重畫一次，
// 否則手機橫轉直之後那排頁碼會超出畫面。重畫用最後一次的參數，
// 所以每個分頁器要記住自己畫的是什麼。
let _pagerResizeTimer;
addEventListener('resize', () => {
  clearTimeout(_pagerResizeTimer);
  _pagerResizeTimer = setTimeout(() => {
    for (const [sel, args] of _pagerLast) {
      const box = $(sel);
      if (box && box.innerHTML) box.innerHTML = renderPagerHTML(args);
    }
  }, 150);
});

/** 綁一次就好：事件委派在容器上，換頁重畫內容不會失效。 */
function bindPager(sel, onGo) {
  const box = $(sel);
  if (!box || box.dataset.bound) return;
  box.dataset.bound = '1';
  box.addEventListener('click', e => {
    const b = e.target.closest('[data-pg]');
    if (b && !b.disabled) onGo(+b.dataset.pg);
  });
  const jump = e => {
    const el = e.target.closest('.jump input');
    if (!el) return;
    // 超出範圍就夾到邊界，不要當成錯誤 —— 打 999 的人要的顯然是最後一頁
    const n = Math.min(Math.max(1, parseInt(el.value, 10) || 1), +el.max);
    el.value = n;
    onGo(n);
  };
  box.addEventListener('keydown', e => { if (e.key === 'Enter') jump(e); });
  box.addEventListener('change', jump);
}

let toastTimer;
function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
}

const state = { kind: '', genre: '', q: '', sort: 'added', page: 1 };

/* ------------------------- 媒體庫 ------------------------- */
async function loadLibrary() {
  const grid = $('#grid');
  grid.innerHTML = '<div class="loading">載入中…</div>';
  const p = new URLSearchParams({ sort: state.sort, page: state.page, page_size: 60 });
  if (state.kind) p.set('kind', state.kind);
  if (state.genre) p.set('genre', state.genre);
  if (state.q) p.set('q', state.q);
  let data;
  try { data = await api('/api/library?' + p); }
  catch (e) { grid.innerHTML = `<div class="empty"><h3>載入失敗</h3><p>${esc(e.message)}</p></div>`; return; }

  $('#libCount').textContent = data.total ? `共 ${data.total} 部` : '';
  if (!data.items.length) {
    grid.innerHTML = `<div class="empty" style="grid-column:1/-1">
      <h3>${state.q || state.genre ? '沒有符合的結果' : '媒體庫是空的'}</h3>
      <p>${state.q || state.genre ? '換個關鍵字或分類試試' : '按右上角「掃描媒體庫」開始從 FTP 建立索引'}</p>
      ${state.q || state.genre || !me.is_admin ? '' : '<button class="btn primary" onclick="startScan()">開始掃描</button>'}
    </div>`;
    $('#pager').innerHTML = ''; return;
  }
  grid.innerHTML = data.items.map(cardHtml).join('');
  $$('.card', grid).forEach(c => c.onclick = () => openItem(c.dataset.id));
  renderPager(data);
}

function cardHtml(it) {
  const img = it.poster_url || it.thumb_url;
  const poster = img
    ? `<img loading="lazy" src="${img}" alt="" onerror="this.remove()">`
    : `<div class="ph">${esc(it.title)}</div>`;
  const rate = it.rating ? `<div class="badge rate">★ ${it.rating.toFixed(1)}</div>` : '';
  const kind = it.kind === 'tv'
    ? `<div class="badge ep">${it.season_count > 1 ? it.season_count + ' 季' : ''} ${it.file_count} 集</div>` : '';
  const unscraped = it.scrape_state !== 'ok' && it.scrape_state !== 'manual'
    ? `<div class="badge" style="background:rgba(255,92,92,.8)">未刮削</div>` : '';
  return `<div class="card" data-id="${it.id}">
    <div class="poster">${poster}${rate}${kind}${unscraped}</div>
    <div class="meta"><div class="t">${esc(it.title)}</div>
      <div class="s">${it.year || ''}${it.year && it.kind === 'tv' ? ' · ' : ''}${it.kind === 'tv' ? '影集' : ''}</div>
    </div></div>`;
}

function renderPager(d) {
  paintPager('#pager',
    { page: d.page, total: d.total, pageSize: d.page_size });
  // 要等新內容渲染完才捲。先捲的話，loadLibrary 會把格線換成「載入中」，
  // 頁面高度瞬間塌掉，瀏覽器把捲動位置夾到 0，接著算出來的目標也是錯的。
  bindPager('#pager', p => { state.page = p; return pageTo(loadLibrary, '#grid'); });
}

async function loadGenres() {
  const { genres } = await api('/api/genres');
  $('#genres').innerHTML = `<button class="chip on" data-g="">全部分類</button>` +
    genres.slice(0, 22).map(g => `<button class="chip" data-g="${esc(g.name)}">${esc(g.name)} <small style="opacity:.6">${g.count}</small></button>`).join('');
  $$('#genres .chip').forEach(c => c.onclick = () => {
    $$('#genres .chip').forEach(x => x.classList.remove('on'));
    c.classList.add('on'); state.genre = c.dataset.g; state.page = 1; loadLibrary();
  });
}

async function loadContinue() {
  const { items } = await api('/api/continue');
  const box = $('#continue');
  if (!items.length) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="section-title">繼續觀看 <span>${items.length} 部</span></div>
    <div class="grid">${items.map(i => `
      <div class="card" onclick="location.href='/player?file=${i.file_id}'">
        <div class="poster">${i.poster_url ? `<img loading="lazy" src="${i.poster_url}" onerror="this.remove()">` : `<div class="ph">${esc(i.title || i.filename)}</div>`}
          ${i.season ? `<div class="badge ep">S${String(i.season).padStart(2, '0')}E${String(i.episode).padStart(2, '0')}</div>` : ''}
          <div class="progress"><i style="width:${i.percent}%"></i></div></div>
        <div class="meta"><div class="t">${esc(i.title || i.filename)}</div>
        <div class="s">已看 ${i.percent}%</div></div></div>`).join('')}</div>`;
}

/* ------------------------- 詳情 ------------------------- */
async function openItem(id) {
  // 記住現在開著哪一筆：重新分析完成之後要把彈窗刷新，才看得到新的時長
  window.__openItemId = id;
  showOverlay();
  $('#modal').innerHTML = '<div class="loading">載入中…</div>';
  const it = await api('/api/items/' + id);
  const files = it.kind === 'tv'
    ? (it.seasons || []).map(s => `<div class="season-h">第 ${s.season} 季</div>` +
        s.episodes.map(e => e.files.map(f => fileRow(f, `E${String(e.episode).padStart(2, '0')} ${esc(e.title || '')}`)).join('')).join('')
      ).join('') + (it.files || []).map(f => fileRow(f)).join('')
    : (it.files || []).map(f => fileRow(f)).join('');

  $('#modal').innerHTML = `
    <div class="hero">
      <button class="close" onclick="closeModal()">×</button>
      ${it.backdrop_url ? `<img src="${it.backdrop_url}" onerror="this.remove()">` : ''}
      <div class="hero-in">
        <div class="p">${it.poster_url || it.thumb_url ? `<img src="${it.poster_url || it.thumb_url}" onerror="this.remove()">` : ''}</div>
        <div style="min-width:0;flex:1">
          <h2>${esc(it.title)}</h2>
          <div class="sub">${[it.year, it.original_title && it.original_title !== it.title ? esc(it.original_title) : '',
            it.runtime ? it.runtime + ' 分鐘' : '', it.rating ? '★ ' + it.rating.toFixed(1) : ''].filter(Boolean).join(' · ')}</div>
          <div class="tags">${(it.genres || []).map(g => `<span>${esc(g)}</span>`).join('')}</div>
          <div class="plot">${esc(it.overview || '（無簡介，可能是 TMDB 沒有資料或尚未刮削）')}</div>
          ${(it.cast || []).length ? `<div class="sub" style="margin-top:10px">演員：${it.cast.slice(0, 6).map(c => esc(c.name)).join('、')}</div>` : ''}
        </div>
      </div>
    </div>
    <div class="modal-body">
      <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap">
        ${me.is_admin ? `<button class="btn" onclick="rescrape(${it.id})">重新刮削</button>` : ''}
        ${me.is_admin ? `<button class="btn" onclick="manualMatch(${it.id},'${it.kind}','${esc(it.title).replace(/'/g, "\\'")}')">手動指定 TMDB</button>` : ''}
        ${me.is_admin ? `<button class="btn" onclick="editItem(${it.id})">改片名／年份</button>` : ''}
        ${me.is_admin ? `<button class="btn" onclick="switchKind(${it.id},'${it.kind}')">改成${it.kind === 'tv' ? '電影' : '影集'}</button>` : ''}
      </div>
      ${me.is_admin && it.scrape_state === 'manual'
        ? '<div class="sub" style="margin-bottom:12px;color:var(--accent)">這一筆是手動修正過的，自動刮削不會再蓋掉它。</div>' : ''}
      ${files || '<div class="empty">沒有檔案</div>'}
    </div>`;
}
// 開關集中在這兩個函式：body.modal-open 要跟 .show 同進同出，
// 分散在三個開啟點各寫一次的話，遲早有一個忘了加而讓背景捲動鎖不掉。
function showOverlay() {
  $('#overlay').classList.add('show');
  document.body.classList.add('modal-open');
}
window.closeModal = () => {
  $('#overlay').classList.remove('show');
  document.body.classList.remove('modal-open');
};
$('#overlay').onclick = e => { if (e.target.id === 'overlay') closeModal(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

function fileRow(f, label = '') {
  const mode = f.play_mode || (f.probe_state === 'ok' ? 'hls' : '');
  const pill = f.probe_state !== 'ok'
    ? `<span class="pill warn" title="${esc(f.probe_error || '')}">未探測</span>`
    : `<span class="pill ${mode === 'direct' ? 'direct' : ''}">${mode === 'direct' ? '直接播放' : '轉碼播放'}</span>`;
  const info = [f.height ? f.height + 'p' : '', (f.video_codec || '').toUpperCase(),
    (f.audio_codec || '').toUpperCase(), gb(f.size), hhmm(f.duration)].filter(Boolean).join(' · ');
  const pct = f.position && f.watched_duration ? Math.round(100 * f.position / f.watched_duration) : 0;
  return `<div class="file-row">
    <div class="n"><b>${label ? esc(label) + ' — ' : ''}${esc(f.filename)}</b>
      <small>${info}${pct ? ` · 已看 ${pct}%` : ''}</small></div>
    ${pill}
    <button class="btn primary" onclick="location.href='/player?file=${f.id}'">播放</button>
    ${me.is_admin ? `<a class="btn" href="/api/download/${f.id}" download>下載</a>` : ''}
    ${me.is_admin ? `<button class="btn" onclick="reprobe(${f.id})" title="時長、字幕軌、解析度不對的時候用">重新分析</button>` : ''}
  </div>`;
}

window.rescrape = async id => {
  toast('刮削中…');
  try { await api('/api/rescrape/' + id, { method: 'POST' }); toast('完成'); openItem(id); loadLibrary(); }
  catch (e) { toast('失敗：' + e.message); }
};

window.manualMatch = async (id, kind, title) => {
  const q = prompt('輸入正確片名以在 TMDB 搜尋：', title);
  if (!q) return;
  let res;
  try { res = await api(`/api/tmdb/search?kind=${kind}&q=${encodeURIComponent(q)}`); }
  catch (e) { return toast('搜尋失敗：' + e.message); }
  if (!res.results.length) return toast('TMDB 查無結果');
  $('#modal').innerHTML = `<div class="modal-body"><h2 style="margin-top:0">選擇正確的作品</h2>
    ${res.results.map(r => `<div class="file-row" style="cursor:pointer" onclick="applyMatch(${id},${r.id})">
      ${r.poster ? `<img src="${r.poster}" style="width:46px;border-radius:6px">` : ''}
      <div class="n"><b>${esc(r.title)} ${r.year ? '(' + r.year + ')' : ''}</b>
      <small>${esc((r.overview || '').slice(0, 90))}</small></div></div>`).join('')}
    <button class="btn" onclick="openItem(${id})">返回</button></div>`;
};
/* 只改片名／年份，不接 TMDB。
 * TMDB 上的中文片名有時候就是不好，或者根本沒有中譯 —— 那種情況下
 * 「整筆重刮」解不了問題，使用者要的只是改一個欄位。 */
window.editItem = async id => {
  const it = await api('/api/items/' + id).catch(() => null);
  if (!it) return toast('讀不到這個條目');
  const title = prompt('片名：', it.title || '');
  if (title === null) return;
  const yearRaw = prompt('年份（留空表示清掉）：', it.year || '');
  if (yearRaw === null) return;
  const body = {};
  if (title.trim() && title !== it.title) body.title = title.trim();
  if (String(yearRaw).trim() !== String(it.year || '')) body.year = +yearRaw || null;
  if (!Object.keys(body).length) return toast('沒有變更');
  try {
    const r = await api('/api/items/' + id, { method: 'PATCH', body: JSON.stringify(body) });
    toast('已改：' + (r.changed || []).join('、'));
    openItem(id); loadLibrary();
  } catch (e) { toast('失敗：' + e.message); }
};

/* 類型認錯（電影 ↔ 影集）。
 * 這是「刮到錯的資料」裡最惡劣的一種：TMDB 的搜尋是分 movie / tv 兩個端點的，
 * 類型錯了的話手動指定也永遠搜不到，而畫面上不會告訴你為什麼。 */
window.switchKind = async (id, cur) => {
  const to = cur === 'tv' ? 'movie' : 'tv';
  const label = to === 'tv' ? '影集' : '電影';
  const extra = to === 'tv'
    ? '底下的檔案會依檔名重新指派集數（解析不出集數的就按檔名順序給 S01E01、S01E02…）。'
    : '底下的集數資料會被刪掉（含劇照）。';
  if (!confirm(`把這一筆改成${label}？\n\n${extra}\n\n改完會自動重新刮削一次，因為類型錯的時候 TMDB 資料整份都是錯的。`))
    return;
  toast('處理中…');
  try {
    const r = await api('/api/items/' + id, { method: 'PATCH', body: JSON.stringify({ kind: to }) });
    toast(r.rescraped === false ? `已改成${label}，但重新刮削失敗：${r.error || ''}`
          : `已改成${label}${r.episodes ? `，指派了 ${r.episodes} 集` : ''}`);
    openItem(id); loadLibrary();
  } catch (e) { toast('失敗：' + e.message); }
};

/* 重新跑 ffprobe。時長、字幕軌、解析度、HDR 判斷錯的時候用 ——
 * 端點本來就有，只是之前只有「分析失敗」的情況會呼叫它。 */
window.reprobe = async fileId => {
  toast('重新分析中…');
  try {
    const r = await api('/api/probe/' + fileId, { method: 'POST' });
    toast(r.probe_state === 'ok'
      ? `完成：${r.play_mode === 'direct' ? '直接播放' : '轉碼播放'}${r.duration ? '，' + hhmm(r.duration) : ''}`
      : `分析失敗：${r.probe_error || ''}`);
    const open = $('#overlay').classList.contains('show');
    if (open && window.__openItemId) openItem(window.__openItemId);
  } catch (e) { toast('失敗：' + e.message); }
};

window.applyMatch = async (id, tmdbId) => {
  await api(`/api/rescrape/${id}?tmdb_id=${tmdbId}`, { method: 'POST' });
  toast('已套用'); openItem(id); loadLibrary();
};

/* ------------------------- 掃描 ------------------------- */
let scanTimer;
window.startScan = async () => {
  try { await api('/api/scan', { method: 'POST' }); } catch (e) { toast('掃描已在進行中'); }
  $('#scanPanel').classList.add('show'); pollScan();
};
$('#btnScan').onclick = startScan;
$('#btnCancelScan').onclick = () => api('/api/scan/cancel', { method: 'POST' });
$('#btnHideScan').onclick = () => $('#scanPanel').classList.remove('show');

async function pollScan() {
  clearTimeout(scanTimer);
  let s;
  try { s = await api('/api/scan/status'); } catch { return; }
  const phase = { listing: '掃描 FTP 目錄', indexing: '建立索引', scraping: 'TMDB 刮削',
    probing: '分析影片格式', photos: '讀取相片', done: '掃描完成', error: '發生錯誤',
    cancelled: '已停止', idle: '待機' }[s.phase] || s.phase;
  $('#scanPhase').textContent = phase;
  $('#scanDot').style.background = s.running ? 'var(--green)' : (s.phase === 'error' ? 'var(--red)' : 'var(--dim)');
  $('#scanDot').style.animation = s.running ? '' : 'none';

  let pct = 0, txt = '';
  if (s.phase === 'listing') { txt = `已看 ${s.dirs_seen} 個資料夾，找到 ${s.files_found} 個影片（新增 ${s.files_new}）`; pct = Math.min(90, s.dirs_seen * 2); }
  else if (s.phase === 'scraping') { pct = s.items_total ? 100 * s.scraped / s.items_total : 0; txt = `刮削 ${s.scraped}/${s.items_total}`; }
  else if (s.phase === 'probing') { pct = s.probe_total ? 100 * s.probed / s.probe_total : 0; txt = `分析 ${s.probed}/${s.probe_total}`; }
  // 相片階段原本沒有對照，會直接印出英文 phase，進度條也跳到 100%
  else if (s.phase === 'photos') { pct = s.photo_total ? 100 * s.photos_read / s.photo_total : 0; txt = `讀取相片 ${s.photos_read}/${s.photo_total}`; }
  else {
    pct = 100;
    const bits = [`${s.files_found} 個影片檔`];
    if (s.items_total) bits.push(`${s.items_total} 個條目`);
    if (s.photos_found) bits.push(`${s.photos_found} 張相片`);
    txt = s.error || bits.join(' · ');
  }
  $('#scanText').textContent = txt + (s.current ? ` — ${s.current}` : '');
  $('#scanBar').style.width = pct + '%';
  $('#scanLog').innerHTML = (s.log || []).slice(-40).reverse().map(l => esc(l)).join('<br>');

  if (s.running) scanTimer = setTimeout(pollScan, 1200);
  else { loadLibrary(); loadGenres(); loadContinue(); }
}

// 沒吃到硬體加速時，把「為什麼」講清楚，而不是只顯示 CPU
function hwRow(s) {
  const hw = s.hwaccel, probe = s.hwaccel_probe;
  if (hw && hw !== 'none') {
    const args = probe?.chosen_args?.[`h264_${hw}`];
    return `<div class="file-row"><div class="n"><b>轉碼器</b>
      <small>設定值 ${esc(s.hwaccel_setting)}${args ? ' · 參數 ' + esc(args) : ''}</small></div>
      <span class="pill direct">${esc(hw).toUpperCase()}</span></div>`;
  }
  const failed = (probe?.candidates || []).filter(c => c.listed_by_ffmpeg && !c.ok);
  const reason = failed.map(c => {
    const err = (c.tried?.[0]?.error || c.error || '').trim();
    return err ? `<div style="margin-top:6px"><b style="color:var(--muted)">${esc(c.name)}</b><br>
      <span style="font:11.5px/1.6 ui-monospace,Consolas,monospace;color:var(--dim);word-break:break-word">${esc(err.slice(0, 260))}</span></div>` : '';
  }).join('');
  return `<div class="file-row" style="align-items:flex-start"><div class="n">
      <b>轉碼器</b><small>設定值 ${esc(s.hwaccel_setting)} · 目前用 CPU (libx264)，轉碼較慢</small>
      ${reason}
      ${failed.length ? `<div style="margin-top:8px"><button class="btn" style="padding:4px 10px;font-size:12px"
        onclick="recheckHw()">修好驅動後按這裡重測</button></div>` : ''}
    </div><span class="pill warn">CPU</span></div>`;
}

window.recheckHw = async () => {
  toast('重新實測中…');
  try {
    const r = await api('/api/diagnostics/recheck', { method: 'POST' });
    toast(r.chosen && r.chosen !== 'none' ? `成功，改用 ${r.chosen.toUpperCase()}` : '還是不行，硬體加速仍未啟用');
    $('#btnStats').click(); $('#btnStats').click();
  } catch (e) { toast('重測失敗：' + e.message); }
};

/* ------------------------- 帳號與使用者管理 ------------------------- */
const ST_TEXT = { pending: '待審核', approved: '已核准', rejected: '已拒絕', disabled: '已停權' };

function userRow(u, lastOwner) {
  const initial = esc((u.display_name || u.email || '?').trim()[0].toUpperCase());
  const av = u.picture_url
    ? `<img src="${esc(u.picture_url)}" alt="" referrerpolicy="no-referrer">` : initial;
  const where = [u.last_login_city, u.last_login_country].filter(Boolean).join(' / ');
  const meta = [u.last_login_at ? '上次登入 ' + new Date(u.last_login_at * 1000)
      .toLocaleString('zh-TW', { hour12: false }) : '從未登入',
    u.last_login_ip ? u.last_login_ip + (where ? `（${where}）` : '') : ''
  ].filter(Boolean).join(' · ');
  const acts = [];
  if (u.status !== 'approved') acts.push(`<button class="btn primary" onclick="patchUser(${u.id},{status:'approved'})">核准</button>`);
  if (u.status === 'pending') acts.push(`<button class="btn" onclick="patchUser(${u.id},{status:'rejected'})">拒絕</button>`);
  // 最後一個管理員不給停權也不給降級 —— 後端會擋（400），這裡不要放
  // 一顆按了必定失敗的按鈕出來。
  if (u.status === 'approved' && !lastOwner) acts.push(`<button class="btn" onclick="patchUser(${u.id},{status:'disabled'})">停權</button>`);
  if (!lastOwner) acts.push(u.role === 'owner'
    ? `<button class="btn" onclick="patchUser(${u.id},{role:'viewer'})">改唯讀</button>`
    : `<button class="btn" onclick="patchUser(${u.id},{role:'owner'})">升管理員</button>`);
  if (lastOwner) acts.push('<span style="font-size:11.5px;color:var(--dim)">唯一的管理員</span>');
  return `<div class="urow">
    <div class="uav">${av}</div>
    <div class="uinfo">
      <b>${esc(u.display_name || u.email)}${u.id === me.uid ? '（你）' : ''}</b>
      <small>${esc(u.email)}</small>
      <small>${esc(meta)}</small>
    </div>
    <span class="st ${u.status}">${ST_TEXT[u.status] || esc(u.status)}</span>
    ${u.role === 'owner' ? '<span class="st owner">管理員</span>' : ''}
    <div class="uacts">${acts.join('')}</div>
  </div>`;
}

window.patchUser = async (id, body) => {
  try {
    await api('/api/users/' + id, {
      method: 'PATCH', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    toast('已更新');
    await loadMe();
    // 改到自己身上時（例如把自己降成唯讀）整頁重讀，
    // 不然畫面上還留著已經沒有權限的按鈕
    if (id === me.uid) return location.reload();
    openAccount();
  } catch (e) { toast('更新失敗：' + e.message); }
};

window.openAccount = async () => {
  showOverlay();
  $('#modal').innerHTML = '<div class="loading">載入中…</div>';
  let users = null;
  if (me.is_admin && me.google_login) {
    try { users = await api('/api/users'); } catch {}
  }
  const who = `<div class="urow">
      <div class="uav">${me.picture_url
        ? `<img src="${esc(me.picture_url)}" alt="" referrerpolicy="no-referrer">`
        : esc((me.display_name || me.email || (me.is_admin ? 'A' : 'V'))[0].toUpperCase())}</div>
      <div class="uinfo"><b>${esc(me.display_name || me.email || (me.is_admin ? '管理員（密碼登入）' : '唯讀（密碼登入）'))}</b>
        <small>${esc(me.email || '這個 session 是用密碼登入的，沒有對應的帳號')}</small></div>
      <span class="st ${me.is_admin ? 'owner' : 'approved'}">${me.is_admin ? '管理員' : '唯讀'}</span>
      <div class="uacts"><a class="btn" href="/logout">登出</a></div>
    </div>`;

  let list = '';
  if (users) {
    const c = users.counts || {};
    const pend = (users.items || []).filter(u => u.status === 'pending');
    const owners = (users.items || []).filter(u => u.role === 'owner' && u.status === 'approved').length;
    list = `<h3 style="margin:24px 0 6px;font-size:15px">使用者
        <span style="color:var(--dim);font-weight:500;font-size:12.5px">
          共 ${c.total || 0} 人${c.pending ? ` · ${c.pending} 人待審核` : ''}</span></h3>
      ${pend.length ? '' : '<div style="font-size:12.5px;color:var(--dim);margin-bottom:8px">目前沒有待審核的申請。</div>'}
      ${(users.items || []).map(u => userRow(u, owners === 1 && u.role === 'owner' && u.status === 'approved')).join('') || '<div class="loading">還沒有人用 Google 登入過</div>'}`;
  } else if (me.is_admin && !me.google_login) {
    list = `<div style="font-size:12.5px;color:var(--dim);margin-top:20px;line-height:1.7">
      尚未啟用 Google 登入。在 <code>.env</code> 設定 <code>GOOGLE_CLIENT_ID</code>、
      <code>GOOGLE_CLIENT_SECRET</code> 與 <code>PUBLIC_BASE_URL</code> 後重新啟動，
      這裡就會出現帳號審核清單。</div>`;
  }
  $('#modal').innerHTML = `<div class="modal-body"><button class="close" onclick="closeModal()">×</button>
    <h2 style="margin-top:0">帳號</h2>${who}${list}</div>`;
};

$('#btnAccount').onclick = openAccount;

$('#btnStats').onclick = async () => {
  showOverlay();
  $('#modal').innerHTML = '<div class="loading">檢查中…</div>';
  const [s, d] = await Promise.all([api('/api/stats'), api('/api/diagnostics').catch(() => null)]);
  const problems = (d && d.problems || []).length ? `
    <div style="border:1px solid #5c2b2b;background:#241618;border-radius:10px;padding:14px 16px;margin-bottom:16px">
      <div style="font-weight:800;color:var(--red);margin-bottom:8px">發現 ${d.problems.length} 個問題</div>
      ${d.problems.map(x => `<div style="font-size:13px;color:#e6c9c9;margin-bottom:6px;line-height:1.6">• ${esc(x)}</div>`).join('')}
      ${d.probe_failed ? `<div style="font-size:12.5px;color:var(--dim);margin-top:10px">目前有 ${d.probe_failed} 個檔案分析失敗。修好上面的問題後，按「掃描媒體庫」會自動重試。</div>` : ''}
    </div>` : (d ? `<div style="border:1px solid #22402f;background:#14211a;border-radius:10px;padding:12px 16px;margin-bottom:16px;color:var(--green);font-weight:700">所有相依都正常</div>` : '');
  const tools = d && d.tools ? Object.entries(d.tools).map(([k, t]) => `
    <div class="file-row"><div class="n"><b>${k}</b><small>${esc(t.resolved || t.configured || '')}${t.version ? ' · ' + esc(t.version) : ''}</small></div>
    <span class="pill ${t.ok ? 'direct' : 'warn'}">${t.ok ? 'OK' : '找不到'}</span></div>`).join('') : '';
  $('#modal').innerHTML = `<div class="modal-body"><button class="close" onclick="closeModal()">×</button>
    <h2 style="margin-top:0">媒體庫狀態</h2>
    ${problems}
    ${tools}
    <div class="file-row"><div class="n"><b>電影 / 影集</b><small>條目數</small></div><span class="pill">${s.movies} / ${s.shows}</span></div>
    <div class="file-row"><div class="n"><b>影片檔</b><small>總容量 ${gb(s.total_size)}</small></div><span class="pill">${s.files}</span></div>
    <div class="file-row"><div class="n"><b>未刮削條目</b><small>TMDB ${s.tmdb_enabled ? '已啟用' : '未設定金鑰'}</small></div><span class="pill ${s.unscraped ? 'warn' : 'direct'}">${s.unscraped}</span></div>
    <div class="file-row"><div class="n"><b>未分析檔案</b><small>沒有格式資訊就無法轉碼播放</small></div><span class="pill ${s.unprobed ? 'warn' : 'direct'}">${s.unprobed}</span></div>
    ${hwRow(s)}
    <div class="file-row"><div class="n"><b>FTP</b><small>${esc(s.ftp.roots.join(', '))}</small></div><span class="pill">${esc(s.ftp.host)}:${s.ftp.port}</span></div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="btn" onclick="testFtp()">測試 FTP 連線</button>
      <button class="btn" onclick="api('/api/cache/clear',{method:'POST'}).then(()=>toast('已清除轉碼快取'))">清除轉碼快取</button>
      <button class="btn" onclick="reparseLibrary()" title="重新解析所有檔名並重新分組。不會重新下載中繼資料。">重新分組</button>
      <button class="btn primary" onclick="closeModal();api('/api/scan?full=true',{method:'POST'});$('#scanPanel').classList.add('show');pollScan()">完整重新掃描</button>
    </div></div>`;
};
/* 重新分組：改了檔名解析規則之後，既有檔案不會自己重新分組 ——
   平常的掃描看到「檔案大小沒變」就早退了。 */
window.reparseLibrary = async () => {
  if (!confirm('重新分組會重新解析所有檔名並重建條目。\n\n'
    + '播放進度會保留（它是綁在檔案上的），但手動指定過的 TMDB 配對可能要重做。\n\n'
    + '要繼續嗎？')) return;
  closeModal();
  await api('/api/scan?reparse=true', { method: 'POST' }).catch(e => toast('失敗：' + e.message));
  $('#scanPanel').classList.add('show');
  pollScan();
};

window.testFtp = async () => {
  const r = await api('/api/ftp/test');
  toast(r.ok ? `連線成功：${(r.welcome || '').slice(0, 60)}` : '連線失敗：' + r.error);
};
window.api = api; window.toast = toast; window.pollScan = pollScan; window.openItem = openItem;

/* ------------------------- 相片牆 ------------------------- */
const pstate = { folder: '', page: 1, sort: 'taken', items: [], total: 0, previewPx: 1920,
                 // 'grid' = 方格＋分頁（預設，既有行為）／'flow' = 瀑布流＋無限捲動
                 mode: 'grid', loading: false, done: false,
                 // 這一輪瀏覽的載入上限。按「繼續載入」會往上推一批。
                 flowCap: 0 };

// 瀑布流一次滑到底要載幾張就停。**上限的存在理由是記憶體**：20,033 張全部
// 攤在 DOM 裡，就算有 loading="lazy"，節點本身也要記憶體。
// 沒做虛擬捲動（windowing）是刻意的 —— 它要處理捲動位置還原與 PhotoSwipe 的
// 索引對應，複雜度不成比例；真要看第 2,000 張之後的人，用相簿或排序找快得多。
const FLOW_MAX = 2000;
const PHOTO_PAGE = 60;

try {
  const m = localStorage.getItem('photoMode');
  if (m === 'flow' || m === 'grid') pstate.mode = m;
} catch { /* 隱私模式讀不到 localStorage，用預設值就好 */ }
const dstate = { folder: '', page: 1, sort: 'name', items: [], total: 0 };

const fmtBytes = b => !b ? '—'
  : b >= 1073741824 ? (b / 1073741824).toFixed(2) + ' GB'
  : b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB';

// 資料夾名稱只顯示最後一層，前面的路徑當提示就好
const shortFolder = f => (f || '').split('/').filter(Boolean).pop() || '/';

/* 相簿的顯示名稱。單看最後一層會撞名 —— 實測庫裡就有「巨乳外拍/巨乳外拍」
   與「咬一口兔娘ovo…/咬一口兔娘ovo…」這種父子同名的資料夾，兩個標籤長得
   一模一樣，點下去才知道是哪個。撞名的時候往上找第一個不一樣的層來補。 */
function albumNames(folders) {
  const last = folders.map(shortFolder);
  const dup = new Set(last.filter((n, i) => last.indexOf(n) !== i));
  return folders.map((f, i) => {
    if (!dup.has(last[i])) return last[i];
    const segs = f.split('/').filter(Boolean);
    // 往上找第一個跟末層不同的祖先。父子同名（…/外拍/外拍）時直接取
    // segs[-2] 會補出「外拍 / 外拍」，那沒有解決任何問題。
    for (let j = segs.length - 2; j >= 0; j--) {
      if (segs[j] !== last[i]) return segs[j] + ' / ' + last[i];
    }
    return last[i];
  });
}

const pfilter = { q: '', items: [] };

async function loadFolders() {
  let d;
  try { d = await api('/api/photos/folders'); } catch { return; }
  pfilter.items = d.items;
  paintFolderBar();
}

function paintFolderBar() {
  const bar = $('#folderBar');
  const needle = pfilter.q.toLowerCase();
  const all = pfilter.items;
  const names = albumNames(all.map(f => f.folder));
  const shown = all
    .map((f, i) => ({ ...f, label: names[i] }))
    .filter(f => !needle || f.label.toLowerCase().includes(needle)
                 || f.folder.toLowerCase().includes(needle));
  const total = all.reduce((s, f) => s + (f.c || 0), 0);
  // 搜尋中的話選中的相簿是被忽略的（搜全庫），所以標籤也不該還亮著 ——
  // 亮著等於告訴使用者「你正在看這個相簿」，但畫面上不是。
  const sel = state.q ? null : pstate.folder;
  const head = needle ? '' : `<div class="fchip ${sel ? '' : 'on'}" data-folder=""
      title="所有相簿"><span class="fc-t"><span class="fc-n">全部</span>
      <span class="fc-c"> ${total}</span></span></div>`;
  bar.innerHTML = head + (shown.length ? shown.map(f => `
    <div class="fchip ${sel === f.folder ? 'on' : ''}" data-folder="${esc(f.folder)}"
         title="${esc(f.folder)}">
      ${f.cover ? `<img loading="lazy" src="/api/image/${esc(f.cover)}" alt="">` : ''}
      <span class="fc-t"><span class="fc-n">${esc(f.label)}</span>
      <span class="fc-c"> ${f.c}</span></span>
    </div>`).join('') : '<div class="fc-none">找不到符合的相簿</div>');
  bar.querySelectorAll('[data-folder]').forEach(el => el.onclick = () => {
    pstate.folder = el.dataset.folder; pstate.page = 1; loadPhotos();
  });
}

let pfTimer;
$('#photoFolderQ').oninput = e => {
  clearTimeout(pfTimer);
  pfTimer = setTimeout(() => { pfilter.q = e.target.value.trim(); paintFolderBar(); }, 200);
};

// 相片的排序後端一直都吃 sort，只是前端沒有地方選 —— 影片的那顆排序在
// 切到相片牆時整條 toolbar 被藏掉了，所以相片牆得有自己的。
$('#photoSort').onchange = e => {
  pstate.sort = e.target.value; pstate.page = 1; loadPhotos();
};

// 方格 ↔ 瀑布流。選擇存 localStorage（跟 photoview.js 的 infoPref 同一套做法）。
function paintPhotoMode() {
  $$('.pmode [data-pmode]').forEach(b => {
    const on = b.dataset.pmode === pstate.mode;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}
$$('.pmode [data-pmode]').forEach(b => b.onclick = () => {
  if (b.dataset.pmode === pstate.mode) return;
  pstate.mode = b.dataset.pmode;
  try { localStorage.setItem('photoMode', pstate.mode); } catch { /* 隱私模式 */ }
  paintPhotoMode();
  pstate.page = 1;
  loadPhotos();
});
paintPhotoMode();

/** 一張相片的 HTML。`i` 一定要是**累計索引** —— openPhoto 與 PhotoSwipe 的
 *  thumbEl 都靠 data-i 對回 pstate.items，瀑布流下用頁內索引會開錯張。 */
function photoTile(x, i) {
  // 瀑布流要把長寬寫進 <img>：瀏覽器在圖載入前就知道要留多高，版面不會跳動。
  // 這也是這裡不需要 masonry 函式庫的原因 —— 它們解決的正是「要等圖載完
  // 才量得到高度」，而這座片庫 100% 的相片在掃描時就存好尺寸了。
  const dim = (x.width > 0 && x.height > 0)
    ? ` width="${x.width}" height="${x.height}"` : '';
  return `
    <div class="ph" data-i="${i}" role="button" tabindex="0"
         aria-label="看大圖：${esc(x.filename)}" title="${esc(x.filename)}">
      <img loading="lazy" src="/api/photo/${x.id}/thumb.jpg" alt="${esc(x.filename)}"${dim}
           onerror="this.style.display='none'">
      <div class="ph-meta">${esc(x.filename)}</div>
    </div>`;
}

function bindTiles(root) {
  root.querySelectorAll('.ph:not([data-bound])').forEach(el => {
    el.dataset.bound = '1';
    el.onclick = () => openPhoto(+el.dataset.i);
    el.onkeydown = e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPhoto(+el.dataset.i); }
    };
  });
}

/** 從伺服器要一頁。回傳 null 表示失敗（呼叫端自己決定要不要顯示錯誤）。 */
async function fetchPhotoPage(page) {
  const p = new URLSearchParams({ page, page_size: PHOTO_PAGE, sort: pstate.sort });
  // 跟文件牆一樣：搜尋時忽略選中的相簿，直接搜全庫。原本兩者是 AND，
  // 於是「選了一個相簿之後就再也搜不到別處」，而畫面上沒有線索說明為什麼。
  const searching = !!state.q;
  if (pstate.folder && !searching) p.set('folder', pstate.folder);
  if (searching) p.set('q', state.q);
  try { return await api('/api/photos?' + p); }
  catch (e) { return { _err: e.message }; }
}

async function loadPhotos() {
  const grid = $('#pgrid');
  const flow = pstate.mode === 'flow';
  grid.classList.toggle('flow', flow);
  grid.innerHTML = '<div class="loading">載入中…</div>';
  // 換相簿／換排序／換模式都是重新開始 —— 累積下來的東西全部清掉，
  // 否則上一個相簿的相片會留在 items 裡，PhotoSwipe 就會翻到不存在的圖。
  pstate.items = []; pstate.done = false; pstate.loading = false;
  pstate.flowCap = FLOW_MAX;          // 每次重新開始都把上限歸位
  $('#pmore').hidden = true;

  const d = await fetchPhotoPage(flow ? 1 : pstate.page);
  if (d._err) {
    grid.innerHTML = `<div class="loading">載入失敗：${esc(d._err)}</div>`;
    $('#ppager').innerHTML = ''; return;
  }
  pstate.total = d.total;
  pstate.previewPx = d.preview_px || 1920;
  const searching = !!state.q;
  $('#photoCount').textContent = d.total
    ? `共 ${d.total} 張${searching ? '（全庫搜尋）' : ''}` : '';

  if (!d.items.length) {
    grid.innerHTML = searching
      ? `<div class="empty"><h3>找不到「${esc(state.q)}」</h3>
         <p>換個關鍵字試試，或清空搜尋框回到相簿瀏覽。</p></div>`
      : `<div class="empty"><h3>沒有相片</h3>
         <p>把圖片放進 .env 的 LIBRARY_ROOTS 底下，再按「掃描媒體庫」即可。</p></div>`;
    $('#ppager').innerHTML = ''; return;
  }

  pstate.items = d.items.slice();
  grid.innerHTML = pstate.items.map(photoTile).join('');
  bindTiles(grid);

  if (flow) {
    // 瀑布流沒有「第幾頁」的概念，分頁器要收起來
    $('#ppager').innerHTML = '';
    pstate.page = 1;
    pstate.done = pstate.items.length >= d.total;
    updateFlowFooter();
    observeFlow();
  } else {
    paintPager('#ppager',
      { page: d.page, total: d.total, pageSize: d.page_size });
    bindPager('#ppager', p => { pstate.page = p; return pageTo(loadPhotos, '#pgrid'); });
  }
}

/* ---------------- 瀑布流：往下滑自動載入 ---------------- */

/** 接著載下一頁並 append。同一時間只會有一個在跑。 */
async function loadMorePhotos() {
  if (pstate.loading || pstate.done || pstate.mode !== 'flow') return;
  pstate.loading = true;
  updateFlowFooter();
  const d = await fetchPhotoPage(pstate.page + 1);
  pstate.loading = false;
  if (d._err) {
    // 載失敗不要把已經看到的清掉，也不要無限重試 —— 停下來讓使用者自己按
    pstate.done = true;
    updateFlowFooter(d._err);
    return;
  }
  pstate.page += 1;
  // **append 而不是覆蓋**：openPhoto(i) 與 thumbEl(n) 都是拿 data-i 對回
  // 這個陣列，覆蓋的話索引就對不上，PhotoSwipe 會開到別張。
  const base = pstate.items.length;
  pstate.items.push(...d.items);
  const grid = $('#pgrid');
  grid.insertAdjacentHTML('beforeend',
    d.items.map((x, k) => photoTile(x, base + k)).join(''));
  bindTiles(grid);
  pstate.done = !d.items.length
    || pstate.items.length >= d.total
    || pstate.items.length >= pstate.flowCap;
  updateFlowFooter();
}

/** 瀑布流底下那一行的狀態：載入中／載入更多／到底了。 */
function updateFlowFooter(err) {
  const box = $('#pmore');
  if (!box) return;
  if (pstate.mode !== 'flow') { box.hidden = true; return; }
  box.hidden = false;
  if (pstate.loading) { box.innerHTML = '<div class="loading">載入中…</div>'; return; }
  if (err) {
    box.innerHTML = `<div class="flow-end">載入失敗：${esc(err)}
      <button class="btn" data-more="1">重試</button></div>`;
  } else if (!pstate.done) {
    box.innerHTML = '<div class="flow-end"><button class="btn" data-more="1">載入更多</button></div>';
  } else if (pstate.items.length >= pstate.flowCap && pstate.items.length < pstate.total) {
    // 撞到上限而不是真的到底 —— 要講清楚，否則使用者會以為相片只有這些
    box.innerHTML = `<div class="flow-end">已載入 ${pstate.items.length} 張
      （共 ${pstate.total} 張）。再往下請用相簿或排序縮小範圍。
      <button class="btn" data-more="more">繼續載入</button></div>`;
  } else {
    box.innerHTML = `<div class="flow-end">已經到底了 · 共 ${pstate.items.length} 張</div>`;
  }
}

$('#pmore').addEventListener('click', e => {
  if (!e.target.closest('[data-more]')) return;
  // 使用者明確按了才繼續。撞到 FLOW_MAX 時把上限往上推一批 ——
  // 上限是防呆不是禁令，按下去就代表他知道自己在做什麼。
  if (pstate.items.length >= pstate.flowCap) pstate.flowCap += FLOW_MAX;
  pstate.done = false;
  loadMorePhotos();
});

let flowObserver;
function observeFlow() {
  const sentinel = $('#pmore');
  if (!sentinel) return;
  if (!flowObserver) {
    // rootMargin 讓它提早 600px 就開始載，滑到底時通常已經接上了
    flowObserver = new IntersectionObserver(entries => {
      if (entries.some(x => x.isIntersecting)) loadMorePhotos();
    }, { rootMargin: '600px 0px' });
  }
  flowObserver.disconnect();
  if (pstate.mode === 'flow') flowObserver.observe(sentinel);
}

/* ------------------------- 文件牆（PDF） -------------------------

   導覽是一棵懶載入的資料夾樹。原本是把 452 個資料夾一次平鋪成標籤，
   而且標籤只顯示路徑的最後一層 —— 這個庫 97% 的路徑深達 7～8 層，
   於是畫面上是一排分不出來的「PDF」「教用」「學用」。

   後端一次只回一層，並且會把「只有一條路可走」的連續層併成一個節點
   （顯示成「A / B / C」），所以這裡不必自己處理深度。 */
const dtree = { open: new Set(), kids: new Map(), root: '', q: '' };

// querySelector 的屬性選擇器要吃得下路徑裡的引號與反斜線
const cssEsc = s => (window.CSS && CSS.escape) ? CSS.escape(s)
  : String(s).replace(/["\\]/g, '\\$&');

// 展開／收合只動這一層的子節點，不重畫整棵樹 —— 重畫會讓其他已展開的
// 分支跟著閃一下，而且捲動位置會跳掉。
async function docKids(prefix) {
  if (dtree.kids.has(prefix)) return dtree.kids.get(prefix);
  const p = new URLSearchParams();
  if (prefix) p.set('prefix', prefix);
  const d = await api('/api/documents/folders' + (p.toString() ? '?' + p : ''));
  if (!prefix) dtree.root = d.prefix || '';
  dtree.kids.set(prefix, d.items);
  return d.items;
}

function docNodeHTML(f) {
  const on = !state.q && dstate.folder === f.folder;
  const open = dtree.open.has(f.folder);
  return `<div class="tnode-wrap" data-wrap="${esc(f.folder)}">
    <div class="tnode ${on ? 'on' : ''}" data-node="${esc(f.folder)}"
         role="treeitem" tabindex="0" title="${esc(f.folder)}"
         aria-selected="${on}" aria-expanded="${f.has_children ? open : ''}">
      <span class="tw ${f.has_children ? '' : 'leaf'} ${open ? 'open' : ''}"
            data-tw="${esc(f.folder)}" role="presentation">▶</span>
      <span class="tn">${esc(f.name)}</span>
      <span class="tc">${f.c}</span>
    </div>
    <div class="tkids" data-kids="${esc(f.folder)}" ${open ? '' : 'hidden'}></div>
  </div>`;
}

async function paintDocKids(prefix, box) {
  box.innerHTML = '<div class="loading">載入中…</div>';
  let items;
  try { items = await docKids(prefix); }
  catch (e) { box.innerHTML = `<div class="empty">載入失敗：${esc(e.message)}</div>`; return; }
  box.innerHTML = items.length ? items.map(docNodeHTML).join('')
    : '<div class="empty">沒有子資料夾</div>';
  // 已經展開過的分支要跟著補回來（收合再展開時不必重打 API，kids 有快取）
  for (const f of items) {
    if (dtree.open.has(f.folder)) {
      const kb = box.querySelector(`[data-kids="${cssEsc(f.folder)}"]`);
      if (kb) paintDocKids(f.folder, kb);
    }
  }
}

// 窄螢幕上側欄是抽屜，預設收起來 —— 不收的話一進文件牆就是滿螢幕的資料夾，
// 要捲很久才看得到檔案。寬螢幕則永遠並排顯示（hidden 一定要清掉，不然
// 從窄轉寬時側欄會整個消失）。
let wasNarrow = null;

function syncDocTreePane() {
  const narrow = window.matchMedia('(max-width:900px)').matches;
  wasNarrow = narrow;
  $('#docTree').hidden = narrow;
  $('#docTreeToggle').setAttribute('aria-expanded', String(!narrow));
}
// 只在真的跨越斷點時才動它。每次 resize 都同步的話，使用者在窄螢幕上
// 開著抽屜、手指稍微碰到縮放就被關掉。
window.addEventListener('resize', () => {
  const narrow = window.matchMedia('(max-width:900px)').matches;
  if (narrow === wasNarrow) return;
  wasNarrow = narrow;
  if (!$('#docView').hidden) syncDocTreePane();
});

async function loadDocFolders() {
  const body = $('#docTreeBody');
  dtree.kids.clear();
  syncDocTreePane();
  body.innerHTML = '<div class="loading">載入中…</div>';
  await paintDocKids('', body);
  paintCrumbs();
}

// 點名字＝換內容；點箭頭＝只展開，不換內容。分開是刻意的：想確認
// 「這底下有什麼」不該把右邊整頁換掉。
$('#docTreeBody').onclick = async e => {
  const tw = e.target.closest('[data-tw]');
  if (tw) {
    e.stopPropagation();
    const path = tw.dataset.tw;
    const box = $('#docTreeBody').querySelector(`[data-kids="${cssEsc(path)}"]`);
    if (!box) return;
    if (dtree.open.has(path)) {
      dtree.open.delete(path); box.hidden = true; tw.classList.remove('open');
    } else {
      dtree.open.add(path); box.hidden = false; tw.classList.add('open');
      if (!box.innerHTML) await paintDocKids(path, box);
    }
    const node = tw.closest('.tnode');
    if (node) node.setAttribute('aria-expanded', dtree.open.has(path));
    return;
  }
  const node = e.target.closest('[data-node]');
  if (node) selectDocFolder(node.dataset.node);
};

$('#docTreeBody').onkeydown = e => {
  const node = e.target.closest('[data-node]');
  if (!node) return;
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault(); selectDocFolder(node.dataset.node);
  } else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
    const tw = node.querySelector('[data-tw]');
    const open = dtree.open.has(node.dataset.node);
    if (tw && !tw.classList.contains('leaf') && (e.key === 'ArrowRight') !== open) {
      e.preventDefault(); tw.click();
    }
  }
};

function selectDocFolder(path) {
  dstate.folder = path; dstate.page = 1;
  if (window.matchMedia('(max-width:900px)').matches) closeDocTree();
  paintCrumbs();          // 選取標示也在這裡面一起畫
  loadDocs();
}

/* 麵包屑。路徑有 7～8 層，全部畫出來會佔掉兩三行，所以中段折成「…」，
   只留頭一層與最後兩層 —— 頭一層是脈絡，最後兩層是「我現在在哪」。 */
function paintCrumbs() {
  const bar = $('#docCrumbs');
  if (!bar) return;
  const root = dtree.root || '';
  const rest = dstate.folder && dstate.folder.startsWith(root)
    ? dstate.folder.slice(root.length) : dstate.folder || '';
  const segs = rest.split('/').filter(Boolean);
  const crumb = (label, path, cur) => cur
    ? `<span class="cur">${esc(label)}</span>`
    : `<button data-crumb="${esc(path)}">${esc(label)}</button>`;
  const parts = [crumb('全部', '', !segs.length)];
  let acc = root;
  const keep = segs.length > 3 ? [0, segs.length - 2, segs.length - 1] : segs.map((_, i) => i);
  segs.forEach((s, i) => {
    acc += '/' + s;
    if (!keep.includes(i)) {
      if (keep.includes(i + 1) && i > 0) parts.push('<span class="sep">…</span>');
      return;
    }
    parts.push('<span class="sep">›</span>', crumb(s, acc, i === segs.length - 1));
  });
  bar.innerHTML = parts.join('');
  bar.querySelectorAll('[data-crumb]').forEach(b => b.onclick = () => {
    selectDocFolder(b.dataset.crumb);
  });
  // 搜尋是跨全庫的，這時樹上不該還有一個節點亮著說「你在這裡」
  const sel = state.q ? null : dstate.folder;
  $$('#docTreeBody .tnode').forEach(n => {
    const on = n.dataset.node === sel;
    n.classList.toggle('on', on);
    n.setAttribute('aria-selected', on);
  });
}

/* 行動版的抽屜 */
function closeDocTree() {
  $('#docTree').hidden = true;
  $('#docTreeToggle').setAttribute('aria-expanded', 'false');
}
$('#docTreeToggle').onclick = () => {
  const pane = $('#docTree');
  pane.hidden = !pane.hidden;
  $('#docTreeToggle').setAttribute('aria-expanded', String(!pane.hidden));
};

/* 篩選資料夾。輸入時改成打平的搜尋結果（跨層），清空就回到樹。
   在 452 個資料夾裡逐層點下去找一個名字是很痛的，所以留一個直接跳的入口。 */
let dfTimer;
$('#docFolderQ').oninput = e => {
  clearTimeout(dfTimer);
  dfTimer = setTimeout(() => filterDocFolders(e.target.value.trim()), 260);
};

async function filterDocFolders(q) {
  dtree.q = q;
  const body = $('#docTreeBody');
  if (!q) { dtree.kids.clear(); await paintDocKids('', body); return; }
  body.innerHTML = '<div class="loading">搜尋中…</div>';
  let d;
  try { d = await api('/api/documents/folders?flat=true'); }
  catch (e) { body.innerHTML = `<div class="empty">載入失敗：${esc(e.message)}</div>`; return; }
  const needle = q.toLowerCase();
  const hit = d.items.filter(f => f.folder.toLowerCase().includes(needle)).slice(0, 200);
  if (!hit.length) { body.innerHTML = '<div class="empty">找不到符合的資料夾</div>'; return; }
  // 搜尋結果沒有層級可言，所以顯示「最後兩層」讓同名資料夾分得出來
  body.innerHTML = hit.map(f => {
    const segs = f.folder.split('/').filter(Boolean);
    const label = segs.slice(-2).join(' / ');
    return `<div class="tnode ${dstate.folder === f.folder ? 'on' : ''}"
      data-node="${esc(f.folder)}" role="treeitem" tabindex="0" title="${esc(f.folder)}">
      <span class="tw leaf">▶</span>
      <span class="tn">${esc(label)}</span><span class="tc">${f.c}</span></div>`;
  }).join('');
}

/* 每一列要顯示的路徑：相對於目前選中的節點。

   原本一律顯示 shortFolder()（路徑最後一層），在這個庫裡等於整牆都是
   「教用(PDF)」「學用(PDF)」「PDF」，分不出誰是誰。改成：
     - 選了資料夾 → 顯示它底下的剩餘路徑；剛好就在選中那層則不顯示（不冗餘）
     - 沒選（搜尋全庫）→ 顯示最後兩層，前面折成「…」 */
function docSubPath(folder) {
  // 搜尋是跨全庫的，結果跟選中的節點沒有關係 —— 這時一律給可辨識的尾段，
  // 不能拿 dstate.folder 去切（會把不相干的路徑截成看起來像子路徑的東西）。
  const base = state.q ? '' : dstate.folder;
  if (base && folder === base) return '';
  if (base && folder.startsWith(base + '/')) {
    return folder.slice(base.length + 1).split('/').join(' › ');
  }
  const segs = folder.split('/').filter(Boolean);
  return (segs.length > 2 ? '… › ' : '') + segs.slice(-2).join(' › ');
}

async function loadDocs() {
  const grid = $('#dgrid');
  grid.innerHTML = '<div class="loading">載入中…</div>';
  const p = new URLSearchParams({ page: dstate.page, page_size: 60, sort: dstate.sort });
  // 搜尋時忽略選中的資料夾，直接搜全庫。原本兩者是 AND，於是「選了一個
  // 資料夾之後就再也搜不到別處的東西」，而畫面上沒有任何線索說明為什麼。
  const searching = !!state.q;
  // 選中的是樹上的一個節點，要看的是「它與它底下的一切」。精確比對的話，
  // 選中任何一個中間層都會是 0 筆 —— 這個庫的檔案全都在葉節點上。
  if (dstate.folder && !searching) { p.set('folder', dstate.folder); p.set('subtree', 'true'); }
  if (searching) p.set('q', state.q);
  let d;
  try { d = await api('/api/documents?' + p); }
  catch (e) { grid.innerHTML = `<div class="loading">載入失敗：${esc(e.message)}</div>`; return; }
  dstate.items = d.items; dstate.total = d.total;
  $('#docCount').textContent = d.total
    ? `共 ${d.total} 份${searching ? '（全庫搜尋）' : ''}` : '';
  if (!d.items.length) {
    grid.innerHTML = searching
      ? `<div class="empty"><h3>找不到「${esc(state.q)}」</h3>
         <p>換個關鍵字試試，或清空搜尋框回到資料夾瀏覽。</p></div>`
      : `<div class="empty"><h3>沒有文件</h3>
         <p>把 PDF 放進 .env 的 LIBRARY_ROOTS 底下，再按「掃描媒體庫」即可。</p></div>`;
    $('#dpager').innerHTML = ''; return;
  }
  // 用 <button> 而不是 <div>：鍵盤 Tab 與 Enter 直接就能用，不必自己補
  // role 與 keydown（相片牆那邊是後來才補上的）。
  grid.innerHTML = d.items.map(x => `
    <button class="doc" data-doc="${x.id}" title="${esc(x.folder + '/' + x.filename)}">
      <span class="di">PDF</span>
      <span class="dn"><b>${esc(x.filename)}</b><span>${esc(docSubPath(x.folder))}</span></span>
      <span class="ds">${fmtBytes(x.size)}</span>
    </button>`).join('');
  grid.querySelectorAll('[data-doc]').forEach(el => el.onclick = () => {
    location.href = '/reader?doc=' + el.dataset.doc;
  });

  paintPager('#dpager',
    { page: d.page, total: d.total, pageSize: d.page_size });
  bindPager('#dpager', p => { dstate.page = p; return pageTo(loadDocs, '#dgrid'); });
}

/* --------------------- 相片檢視器（PhotoSwipe） --------------------- */
// 檢視器是動態 import 進來的：沒點進相片牆就完全不會下載那 54KB。
// app.js 本身刻意維持成普通 script —— 改成 module 的話頁面上那些
// onclick="..." 內聯呼叫會全部找不到函式。
let _viewer = null;
async function photoViewer() {
  if (!_viewer) _viewer = await import('/static/photoview.js');
  return _viewer;
}

function photoInfoHtml(d) {
  const row = (k, v) => v ? `<div class="row"><span>${k}</span><b>${esc(v)}</b></div>` : '';
  const shot = [row('拍攝時間', d.taken_at), row('相機', d.camera), row('鏡頭', d.lens),
                row('快門', d.exposure), row('光圈', d.aperture),
                row('ISO', d.iso), row('焦距', d.focal_len)].join('');
  return `
    <h3>${esc(d.filename)}</h3>
    <div class="sub">${esc(d.folder)}</div>
    <h4>檔案</h4>
    ${row('尺寸', d.width && d.height ? `${d.width} × ${d.height}` : '')}
    ${row('格式', [d.format, d.mode].filter(Boolean).join(' · '))}
    ${row('大小', fmtBytes(d.size))}
    ${row('修改時間', d.mtime)}
    ${shot ? '<h4>拍攝資訊</h4>' + shot : '<h4>拍攝資訊</h4><div class="row"><b style="color:var(--dim)">這張圖沒有 EXIF</b></div>'}
    ${me.is_admin ? `<div style="margin-top:20px">
      <a class="btn" href="${d.download_url}" download>下載原圖</a>
    </div>` : ''}
    <div style="margin-top:14px;font-size:11.5px;color:var(--dim);line-height:1.6">
      不讀取也不顯示 GPS 座標。原始檔案沒有被修改。
    </div>`;
}

async function openPhoto(i) {
  if (i < 0 || i >= pstate.items.length) return;
  let v;
  try { v = await photoViewer(); }
  catch { toast('檢視器載入失敗'); return; }
  v.openPhotos({
    items: pstate.items,
    index: i,
    previewPx: pstate.previewPx || 1920,
    loadInfo: item => api('/api/photos/' + item.id).then(photoInfoHtml),
    thumbEl: n => document.querySelector(`#pgrid .ph[data-i="${n}"] img`),
  });
}

/* ------------------------- 事件 ------------------------- */
// 三個檢視（影片／相片／文件）共用同一塊區域，一次只顯示一個。
// 每個檢視有自己的排序選項。後端三邊都吃 sort，但可選的鍵不一樣 ——
// 影片有年份與評分，文件沒有；文件的預設是檔名（一套講義的順序是編號）。
const SORTS = {
  video: [['added', '最近加入'], ['title', '片名'], ['year', '年份'], ['rating', '評分']],
  doc: [['name', '檔名'], ['time', '檔案時間'], ['size', '大小'], ['added', '最近加入']],
};

function showView(kind) {
  const photo = kind === 'photo', doc = kind === 'doc';
  const other = photo || doc;
  $('#photoView').hidden = !photo;
  $('#docView').hidden = !doc;
  for (const id of ['#grid', '#pager', '#libTitle', '#continue']) {
    const el = $(id); if (el) el.hidden = other;
  }
  // 整條 toolbar 原本是一起藏的，於是文件牆的排序後端支援了卻沒有 UI 可以選。
  // 類型 chips 確實只有影片用得到，但排序三邊都要。
  $('.toolbar').hidden = photo;
  $('#genres').hidden = other;
  if (!photo) {
    const opts = SORTS[doc ? 'doc' : 'video'];
    const cur = doc ? dstate.sort : state.sort;
    $('#sort').innerHTML = opts.map(([v, label]) =>
      `<option value="${v}" ${v === cur ? 'selected' : ''}>${label}</option>`).join('');
  }
  $('#q').placeholder = other ? '搜尋檔名或資料夾…' : '搜尋片名或檔名…';
}

$$('nav button').forEach(b => b.onclick = () => {
  $$('nav button').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  if (b.dataset.kind === 'photo') {
    showView('photo');
    loadFolders(); loadPhotos();
    return;
  }
  if (b.dataset.kind === 'doc') {
    showView('doc');
    loadDocFolders(); loadDocs();
    return;
  }
  showView('video');
  state.kind = b.dataset.kind; state.page = 1; loadLibrary();
});
let qTimer;
$('#q').oninput = e => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => {
    state.q = e.target.value.trim();
    // 搜尋會蓋過「選中的相簿／資料夾」，所以那兩塊導覽的選取標示要跟著重畫，
    // 不然畫面上會同時說「你在看這個相簿」與「這是全庫搜尋結果」。
    if (!$('#photoView').hidden) { pstate.page = 1; paintFolderBar(); loadPhotos(); }
    else if (!$('#docView').hidden) { dstate.page = 1; paintCrumbs(); loadDocs(); }
    else { state.page = 1; loadLibrary(); }
  }, 320);
};
$('#sort').onchange = e => {
  if (!$('#docView').hidden) { dstate.sort = e.target.value; dstate.page = 1; loadDocs(); }
  else { state.sort = e.target.value; state.page = 1; loadLibrary(); }
};

(async function init() {
  await loadMe();               // 先知道身分，才知道要不要畫出下載與管理按鈕
  await loadLibrary();
  loadGenres().catch(() => {});
  loadContinue().catch(() => {});
  const s = await api('/api/scan/status').catch(() => null);
  if (s && s.running) { $('#scanPanel').classList.add('show'); pollScan(); }
})();
