/* FilmaxWeb 管理後台。
 *
 * 這個檔案在 /static/ 底下，**不需要登入就下載得到** —— 所以裡面不能有
 * 任何敏感資訊。所有實際資料都來自需要管理員權限的 API，權限判斷在後端。
 * 這裡的隱藏與停用只是介面提示，不是防線。
 */
'use strict';

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, opts) {
  const r = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' }, ...opts
  });
  if (!r.ok) {
    let msg = r.status === 403 ? '需要管理員權限' : `HTTP ${r.status}`;
    let detail = null;
    try { detail = (await r.json()).detail; if (detail) msg = detail; }
    catch (e) { /* 不是 JSON 就用預設訊息 */ }
    // detail 可能是物件（批次儲存會回 {key: 訊息}）。直接丟進 Error 的話
    // 訊息會變成 "[object Object]"，呼叫端也拿不到每一項的錯誤 —— 所以另外掛上去。
    const err = new Error(typeof msg === 'string' ? msg : '有幾項不能儲存');
    err.detail = detail;
    err.status = r.status;
    throw err;
  }
  return r.status === 204 ? null : r.json();
}

let toastTimer;
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.toggle('bad', !!bad);
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3000);
}

const bytes = n => {
  if (n == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
};
const when = ts => ts ? new Date(ts * 1000).toLocaleString('zh-TW', { hour12: false }) : '—';
const dur = s => {
  if (s == null) return '—';
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d} 天 ${h} 小時` : h ? `${h} 小時 ${m} 分` : `${m} 分`;
};

/* ---------------------------------------------------------------- 對話框 */
/* 危險操作的二次確認。文案一定要重述對象名稱 ——
   「確定要刪除嗎？」在連按兩下的時候等於沒問，使用者根本沒看清楚刪的是哪一個。 */
/* collect：在對話框被清掉**之前**讀裡面的欄位。
 * done() 會先 m.innerHTML = '' 再 resolve，所以 await 回來之後再
 * querySelector 對話框裡的勾選一定是 null —— 那個 bug 的症狀是
 * 「勾了也沒有生效」，而且完全沒有錯誤訊息。 */
function confirmBox({ title, body, ok = '確定', danger = false, collect = null }) {
  return new Promise(resolve => {
    const m = $('#modal');
    m.innerHTML = `<div class="sheet" role="dialog" aria-modal="true">
      <h3>${esc(title)}</h3><p>${body}</p>
      <div class="row"><button class="btn" data-no>取消</button>
      <button class="btn ${danger ? 'danger' : 'primary'}" data-yes>${esc(ok)}</button></div></div>`;
    m.hidden = false;
    const done = v => {
      if (v && collect) collect(m);
      m.hidden = true; m.innerHTML = '';
      document.removeEventListener('keydown', key);
      resolve(v);
    };
    const key = e => { if (e.key === 'Escape') done(false); };
    m.querySelector('[data-no]').onclick = () => done(false);
    m.querySelector('[data-yes]').onclick = () => done(true);
    m.onclick = e => { if (e.target === m) done(false); };
    document.addEventListener('keydown', key);
    m.querySelector('[data-yes]').focus();
  });
}

/* ---------------------------------------------------------------- 分頁 */
const TABS = {};
let current = 'overview';

function show(name) {
  current = name;
  $$('.sidenav button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  // 二層 nav 只在參數頁展開。放在這裡而不是各分頁自己管，
  // 是因為離開參數頁時也要收起來，而那件事分頁自己不會知道。
  const pnav = $('#paramNav');
  if (pnav) pnav.hidden = name !== 'params' || !pd;
  $$('.tab').forEach(t => t.classList.toggle('on', t.id === 'tab-' + name));
  closeDrawer();
  if (location.hash.slice(1) !== name) history.replaceState(null, '', '#' + name);
  render(name);
}

async function render(name) {
  const el = $('#tab-' + name);
  el.innerHTML = '<div class="empty">載入中⋯</div>';
  try {
    await TABS[name](el);
  } catch (e) {
    // 每張表都要有錯誤態。空白畫面會讓人以為是自己網路的問題。
    el.innerHTML = `<div class="banner bad"><b>載入失敗：</b>${esc(e.message)}
      <div style="margin-top:9px"><button class="btn" onclick="location.reload()">重新載入</button></div></div>`;
  }
}

/* 抽屜。點連結自動關閉；iOS 不做邊緣滑動開啟（會跟系統返回手勢打架）。 */
function openDrawer() {
  $('#side').classList.add('open');
  $('#scrim').hidden = false;
  $('#menuBtn').setAttribute('aria-expanded', 'true');
}
function closeDrawer() {
  $('#side').classList.remove('open');
  $('#scrim').hidden = true;
  $('#menuBtn').setAttribute('aria-expanded', 'false');
}

/* ================================================================ 總覽 */
TABS.overview = async el => {
  const d = await api('/admin/overview');
  const c = d.counts, disk = d.disk || {};
  const warn = [];
  if (d.orphans && d.orphans.total) warn.push(`有 ${d.orphans.total} 筆孤兒資料`);
  if (c.failed) warn.push(`${c.failed} 個檔案分析失敗`);
  if (c.unscraped) warn.push(`${c.unscraped} 個條目還沒刮到資料`);   // no_metadata 刻意不進 warn：
  // 手機錄影這類東西本來就不會有 metadata，把它算成「問題」等於天天報一個
  // 永遠修不好的假警報，久了整條橫幅就沒人看了。
  if (disk.percent != null && disk.percent >= 90) warn.push(`磁碟只剩 ${bytes(disk.free)}`);
  if (d.pendingUsers) warn.push(`${d.pendingUsers} 個帳號等待審核`);

  const diskCls = disk.percent >= 92 ? 'bad' : disk.percent >= 80 ? 'warn' : '';
  el.innerHTML = `
    <h2 class="sec">總覽</h2>
    <p class="secsub">執行了 ${dur(d.uptime)}　·　Python ${esc(d.python)}</p>

    ${d.configDrift.length ? `<div class="banner">
      <b>有 ${d.configDrift.length} 項後台設定被環境變數蓋掉</b>，所以不會生效：
      ${d.configDrift.map(k => `<code>${esc(k)}</code>`).join(' ')}
      　<button class="btn" onclick="show('system')">去看看</button></div>` : ''}
    ${d.needsRestart.length ? `<div class="banner">
      <b>有設定改過但要重開服務才生效</b>：
      ${d.needsRestart.map(k => `<code>${esc(k)}</code>`).join(' ')}</div>` : ''}
    ${warn.length ? `<div class="banner"><b>要注意：</b>${warn.map(esc).join('　·　')}</div>`
      : '<div class="box" style="color:var(--green)">目前沒有需要處理的問題。</div>'}

    <div class="grid-cards">
      <div class="stat"><b>${c.movies}</b><small>電影</small></div>
      <div class="stat"><b>${c.shows}</b><small>影集（${c.episodes} 集）</small></div>
      <div class="stat"><b>${c.files}</b><small>影片檔　${bytes(c.size)}</small></div>
      <div class="stat"><b>${c.photos}</b><small>相片</small></div>
      <div class="stat ${c.unscraped ? 'warn' : ''}"><b>${c.unscraped}</b><small>待處理</small></div>
      <div class="stat"><b>${c.no_metadata}</b><small>無 metadata</small></div>
      <div class="stat ${c.failed ? 'bad' : ''}"><b>${c.failed}</b><small>分析失敗</small></div>
      <div class="stat ${d.orphans && d.orphans.total ? 'warn' : ''}">
        <b>${d.orphans ? d.orphans.total : '—'}</b><small>孤兒資料</small></div>
      <div class="stat ${diskCls}">
        <b>${bytes(disk.free)}</b><small>磁碟剩餘（快取佔 ${bytes(d.cacheBytes)}）</small>
        ${disk.percent != null ? `<div class="bar ${diskCls}"><i style="width:${disk.percent}%"></i></div>` : ''}
      </div>
    </div>

    ${d.scan && d.scan.running ? `<h3 class="sub">掃描進行中</h3>
      <div class="box">${esc(d.scan.phase || '')}　${esc(d.scan.current || '')}</div>` : ''}

    <h3 class="sub">最近活動</h3>
    ${d.recent.length ? `<div class="scrollx"><table class="t">
      <thead><tr><th>時間</th><th>事件</th><th>身分</th><th>來源</th></tr></thead><tbody>
      ${d.recent.map(r => `<tr><td>${when(r.at)}</td><td>${esc(r.event)}</td>
        <td>${esc(r.email || r.role || '')}</td><td>${esc(r.ip || '')}</td></tr>`).join('')}
      </tbody></table></div>
      <div class="rowcards">${d.recent.map(r => `<div class="rowcard">
        <div class="top"><b>${esc(r.event)}</b><span class="st">${esc(r.role || '')}</span></div>
        <dl><dt>時間</dt><dd>${when(r.at)}</dd>
        <dt>身分</dt><dd>${esc(r.email || '—')}</dd>
        <dt>來源</dt><dd>${esc(r.ip || '—')}</dd></dl></div>`).join('')}</div>`
      : '<div class="empty">還沒有任何登入紀錄。</div>'}`;
};

/* ---------------------------------------------------------------- 受限資料夾（L） */
/* 只給特定帳號看的資料夾。三件事刻意這樣做：
 *
 * 1. **從掃到的資料夾清單挑，不讓人手打路徑。**手打就會拼錯，而拼錯的規則
 *    等於沒有保護 —— 畫面上還是會顯示「已設定」，沒有人會發現。
 *    所以這一區沒有任何可以打路徑的欄位；picker 裡的搜尋框只是篩選，
 *    打的字永遠不會變成 prefix（送出去的一定是 ac.folders 裡的某一筆）。
 * 2. **密碼登入沒有帳號身分**，所以無法被授權。這句話要印在畫面上，
 *    不然設定的人會以為「我設了但他還是看不到」是壞掉。
 * 3. 授權用 checkbox 一次送出整份名單（PUT grants），不是逐一 add/remove ——
 *    逐一送會有「加了三個、第四個失敗」的半套狀態。
 *
 * 版面的原則是「平常只看結果」：規則是摘要卡，資料夾樹只在挑的時候出現，
 * checkbox 只在按「管理授權」之後出現。原本三樣東西全部永遠攤開，
 * 一條規則就佔半個螢幕，而 320px 高的資料夾清單大部分時間根本沒人在用。
 *
 * 資料夾清單以前是 <select>，後來改自訂元件，理由到現在都還成立：
 * option 在 Chrome/Safari 套不了深色樣式、表達不了階層、也標不出
 * 「已受限／被涵蓋」；篩選靠的 option.hidden 只有 Firefox 認 ——
 * Chrome／Safari 完全忽略，篩選看起來是壞的（打字沒反應）。 */

/* ---- 資料夾樹：純函式 ----
   tests/acl_picker_test.js 會把 @@folder-tree-begin 到 @@folder-tree-end 之間
   這一段原封不動載進 node 跑 —— 所以這裡**不能碰 DOM**，也不能用到這段以外
   的東西。資料處理跟點擊處理拆開，才測得到「搜尋會不會改掉展開狀態」這種事。 */
// @@folder-tree-begin
const segsOf = p => String(p || '').split('/').filter(Boolean);
const leafOf = p => { const s = segsOf(p); return s.length ? s[s.length - 1] : '/'; };

/** 已受限優先於被涵蓋：巢狀規則（舊資料）兩個都成立，但「它自己就是一條規則」
 *  才是使用者要知道的那件事。 */
function folderState(f) {
  return f.restricted ? 'restricted' : f.covered_by ? 'covered' : 'free';
}

function folderStats(folders) {
  const c = { free: 0, restricted: 0, covered: 0 };
  for (const f of folders || []) c[folderState(f)]++;
  return c;
}

/** ac.folders（依路徑排好的平面清單）→ 樹。
 *
 *  父子關係在這裡一次算好，不要在 DOM 裡靠 path 去猜。
 *  **父層找「清單裡最近的祖先」，不是直屬父層**：folder_counts 只回前 4 層，
 *  但已受限的資料夾不管幾層都會回 —— 第 6 層的規則，它第 5 層的父層不在清單裡，
 *  只找直屬父層的話它會變成孤兒掉到最上層，看起來像一個頂層資料夾。 */
function buildFolderTree(folders) {
  const byPath = new Map();
  for (const f of folders || []) {
    if (!f || !f.folder || byPath.has(f.folder)) continue;
    byPath.set(f.folder, {
      folder: f.folder, name: leafOf(f.folder), depth: 0, parent: null, children: [],
      restricted: !!f.restricted, covered_by: f.covered_by || null, state: folderState(f),
      videos: f.videos || 0, photos: f.photos || 0, documents: f.documents || 0,
      dupName: false,
    });
  }
  const roots = [];
  for (const n of byPath.values()) {
    const s = segsOf(n.folder);
    let p = null, i = s.length - 1;
    for (; i > 0 && !p; i--) p = byPath.get('/' + s.slice(0, i).join('/')) || null;
    n.parent = p;
    // 跳層的節點名稱要帶著中間那幾段（d/e、Deep/a/b/c），只寫 leaf 會丟掉脈絡
    n.name = s.slice(p ? i + 1 : 0).join('/') || '/';
    (p ? p.children : roots).push(n);
  }
  const cmp = (a, b) => a.name.localeCompare(b.name, 'zh-Hant', { numeric: true, sensitivity: 'base' });
  const walk = (list, d) => { list.sort(cmp); for (const n of list) { n.depth = d; walk(n.children, d + 1); } };
  walk(roots, 0);
  // 同名資料夾（/Movies/2024 與 /HomeVideo/2024）：列表上只寫「2024」兩次等於沒寫，
  // 標出來讓畫面多給一行上層脈絡。
  const seen = new Map();
  for (const n of byPath.values()) {
    const k = n.name.toLowerCase();
    seen.set(k, (seen.get(k) || 0) + 1);
  }
  for (const n of byPath.values()) n.dupName = seen.get(n.name.toLowerCase()) > 1;
  return { roots, byPath };
}

/** 「HBO / Westworld」—— 最後兩層。同名消歧義與 trigger 的主行都用這個。 */
const folderContext = p => segsOf(p).slice(-2).join(' / ') || '/';

/** 數量只在有值時出現。長版給桌機，短版（合計）給手機。 */
function folderCountText(n) {
  const full = [n.videos && `影片 ${n.videos}`, n.photos && `相片 ${n.photos}`,
                n.documents && `文件 ${n.documents}`].filter(Boolean).join(' · ');
  const total = (n.videos || 0) + (n.photos || 0) + (n.documents || 0);
  return { full, short: total ? `${total} 個項目` : '' };
}

/** 現在畫面上該出現哪幾列（依顯示順序）。
 *
 *  - 沒有搜尋：從最上層開始，只往 expanded 裡有的節點展開。
 *  - 有搜尋：比對**完整路徑**（打 west 找得到 /HBO/Westworld，打 HBO 也找得到它
 *    底下的東西），命中的列與它的祖先都留著、祖先自動展開 ——
 *    只顯示命中的那一列，縮排就沒有參照物，看起來像浮在半空。
 *    **搜尋不寫回 expanded**：清掉搜尋就回到搜尋前的樣子。搜尋中手動收合的
 *    節點記在另一個 collapsed 裡，換一個關鍵字就作廢。
 *  - show 控制已受限／被涵蓋要不要出現。可選的永遠出現。被過濾掉的命中數
 *    另外回傳（hiddenHits）—— 「找不到」跟「被藏起來」要分得出來，不然管理員
 *    會以為那個資料夾不存在。 */
function visibleFolderRows(tree, { expanded, query = '', show = {}, collapsed = null }) {
  const q = String(query || '').trim().toLowerCase();
  const allowed = n => n.state === 'free' || !!show[n.state];
  const rows = [];
  if (!q) {
    const walk = list => {
      for (const n of list) {
        if (!allowed(n)) continue;
        const kids = n.children.filter(allowed);
        const open = kids.length > 0 && expanded.has(n.folder);
        rows.push({ node: n, level: n.depth, hasKids: kids.length > 0, open, hit: false });
        if (open) walk(kids);
      }
    };
    walk(tree.roots);
    return { rows, hits: 0, hiddenHits: 0 };
  }
  const keep = new Set(), matched = new Set();
  let hiddenHits = 0;
  for (const n of tree.byPath.values()) {
    if (!n.folder.toLowerCase().includes(q)) continue;
    if (!allowed(n)) { hiddenHits++; continue; }
    matched.add(n);
    for (let p = n; p; p = p.parent) keep.add(p);   // 祖先一定留著（當作脈絡）
  }
  const walk = list => {
    for (const n of list) {
      if (!keep.has(n)) continue;
      const kids = n.children.filter(k => keep.has(k));
      const open = kids.length > 0 && !(collapsed && collapsed.has(n.folder));
      rows.push({ node: n, level: n.depth, hasKids: kids.length > 0, open, hit: matched.has(n) });
      if (open) walk(kids);
    }
  };
  walk(tree.roots);
  return { rows, hits: matched.size, hiddenHits };
}

/** 名稱裡要標亮的那一段（[start, end)）。命中在祖先段時名稱裡沒有，回 null。 */
function hitRange(name, query) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return null;
  const i = String(name).toLowerCase().indexOf(q);
  return i < 0 ? null : [i, i + q.length];
}

/** PUT grants 要送的完整名單。
 *
 *  **管理員原本就有的授權要原封帶回去。**這個端點是「整份取代」，
 *  而管理員不在勾選清單裡 —— 不帶的話，一個「先被授權、後來升管理員」的人
 *  會在別人按一次儲存時被安靜地收回授權，然後在他降級的那天才發現。 */
function grantPayload(rule, users, checkedIds) {
  const adminIds = new Set((users || []).filter(u => u.is_admin).map(u => u.id));
  const keep = ((rule && rule.user_ids) || []).filter(i => adminIds.has(i));
  return [...new Set([...(checkedIds || []), ...keep])];
}

/** 規則卡的摘要。只算「現在真的看得到的一般帳號」：管理員靠角色，
 *  不算在「N 人可看」裡（不然授權了零個人的規則會顯示「2 人可看」）。 */
function aclRuleSummary(rule, users, folders) {
  const admins = (users || []).filter(u => u.is_admin);
  const granted = new Set(rule.user_ids || []);
  const viewers = (users || []).filter(u => !u.is_admin && granted.has(u.id)).map(u => u.name);
  const preview = viewers.length <= 2 ? viewers.join('、')
    : viewers.slice(0, 2).join('、') + ` +${viewers.length - 2}`;
  const base = rule.prefix.replace(/\/+$/, '') + '/';
  const kids = (folders || []).filter(f => f.covered_by === rule.prefix)
    .map(f => f.folder.startsWith(base) ? f.folder.slice(base.length) : f.folder);
  return {
    name: leafOf(rule.prefix), viewers: viewers.length, preview,
    admins: admins.length, keptAdmins: admins.filter(u => granted.has(u.id)).map(u => u.name),
    kids,
  };
}
// @@folder-tree-end

const ICON = {
  folder: '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M2.5 5.5A1.5 1.5 0 0 1 4 4h3.6c.4 0 .8.16 1.06.44L9.9 5.7c.1.1.23.16.36.16H16a1.5 1.5 0 0 1 1.5 1.5v7.14A1.5 1.5 0 0 1 16 16H4a1.5 1.5 0 0 1-1.5-1.5z"/></svg>',
  lock: '<svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M6 8V6.5a4 4 0 1 1 8 0V8h.5A1.5 1.5 0 0 1 16 9.5v6a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 4 15.5v-6A1.5 1.5 0 0 1 5.5 8zm1.8 0h4.4V6.5a2.2 2.2 0 1 0-4.4 0z"/></svg>',
  caret: '<svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M5.3 7.3a1 1 0 0 1 1.4 0L10 10.6l3.3-3.3a1 1 0 1 1 1.4 1.4l-4 4a1 1 0 0 1-1.4 0l-4-4a1 1 0 0 1 0-1.4z"/></svg>',
  search: '<svg viewBox="0 0 20 20" width="15" height="15" aria-hidden="true"><path fill="currentColor" d="M8.5 3a5.5 5.5 0 0 1 4.38 8.83l3.4 3.4a1 1 0 0 1-1.42 1.41l-3.39-3.4A5.5 5.5 0 1 1 8.5 3m0 2a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7"/></svg>',
};

/* ---- Folder Picker ----
   關著的時候只是一顆按鈕，點了才長出浮層（手機是 bottom sheet）。
   **選擇與套用是兩步**：樹裡點一列只是「暫選」，按浮層的「選擇」才帶回表單，
   真正建立規則還要再按「加入限制」並通過 confirmBox。點一下就生效的話，
   手滑點錯一列就是一條錯的規則。 */
function folderPickerHTML({ id, stats, labelId }) {
  return `
  <div class="folder-pick" id="${esc(id)}">
    <button type="button" class="folder-pick-trigger" id="${esc(id)}-btn"
            aria-haspopup="dialog" aria-expanded="false" aria-controls="${esc(id)}-pop"
            aria-labelledby="${esc(labelId)} ${esc(id)}-val">
      <span class="folder-pick-icon">${ICON.folder}</span>
      <span class="folder-pick-value" id="${esc(id)}-val">
        <span class="folder-pick-main is-ph">選擇要限制的資料夾</span></span>
      <span class="folder-pick-caret">${ICON.caret}</span>
    </button>
    <div class="folder-pick-backdrop" hidden></div>
    <div class="folder-pick-pop" id="${esc(id)}-pop" role="dialog" tabindex="-1"
         aria-label="選擇要限制的資料夾" hidden>
      <div class="fp-head">
        <div class="fp-title"><b>選擇資料夾</b>
          <button type="button" class="fp-x" data-fp-cancel aria-label="關閉">✕</button></div>
        <label class="fp-search-wrap">${ICON.search}
          <input class="fp-search" id="${esc(id)}-q" type="search" autocomplete="off"
                 spellcheck="false" placeholder="搜尋資料夾…" aria-label="搜尋資料夾（比對完整路徑）"
                 aria-controls="${esc(id)}-tree"></label>
        <div class="fp-chips" role="group" aria-label="要顯示哪些資料夾">
          <span class="fp-chip is-static" title="可以設為受限的資料夾">可選 <b>${stats.free}</b></span>
          <button type="button" class="fp-chip" data-show="restricted" aria-pressed="true"
                  title="已經是一條規則，不能重複建立">${ICON.lock} 已受限 <b>${stats.restricted}</b></button>
          <button type="button" class="fp-chip" data-show="covered" aria-pressed="false"
                  title="在某個受限資料夾底下，已經自動跟著受限">被涵蓋 <b>${stats.covered}</b></button>
        </div>
      </div>
      <div class="fp-body">
        <div class="ftree" id="${esc(id)}-tree" role="tree" aria-label="資料夾"></div>
        <div class="fp-msg" hidden></div>
      </div>
      <div class="fp-foot">
        <div class="fp-staged" aria-live="polite"></div>
        <div class="fp-acts">
          <button type="button" class="btn" data-fp-cancel>取消</button>
          <button type="button" class="btn primary" data-fp-ok disabled>選擇</button>
        </div>
      </div>
      <div class="fp-live" role="status" aria-live="polite"></div>
    </div>
  </div>`;
}

/** 接線。回傳 { get, open, close }；get() 回已確認的路徑（沒選是 ''）。 */
function wireFolderPicker(el, { id, tree, onChange = null }) {
  const root = el.querySelector('#' + id);
  if (!root) return { get: () => '', open() {}, close() {} };
  const trig = root.querySelector('.folder-pick-trigger');
  const val = root.querySelector('.folder-pick-value');
  const pop = root.querySelector('.folder-pick-pop');
  const back = root.querySelector('.folder-pick-backdrop');
  const q = root.querySelector('.fp-search');
  const treeEl = root.querySelector('.ftree');
  const msg = root.querySelector('.fp-msg');
  const stagedEl = root.querySelector('.fp-staged');
  const okBtn = root.querySelector('[data-fp-ok]');
  const live = root.querySelector('.fp-live');
  const mq = window.matchMedia ? window.matchMedia('(max-width:768px)') : { matches: false };
  const free = [...tree.byPath.values()].filter(n => n.state === 'free').length;

  const expanded = new Set();          // 使用者展開的（搜尋不會動它）
  let collapsed = new Set();           // 搜尋中手動收合的（換關鍵字就作廢）
  let lastQ = '';
  const show = { restricted: true, covered: false };
  let chosen = null;                   // 已確認（帶回表單的）
  let staged = null;                   // 浮層裡暫選的
  let active = null;                   // 鍵盤游標（roving tabindex）
  let rows = [];
  let isOpen = false;

  const say = t => { live.textContent = ''; live.textContent = t; };
  const stateText = n => n.state === 'restricted' ? '已受限，不能重複建立'
    : n.state === 'covered' ? `在 ${n.covered_by} 底下，已經跟著受限` : '';

  const rowHTML = (r, i, pos, size) => {
    const n = r.node;
    const hr = hitRange(n.name, q.value);
    const name = hr ? esc(n.name.slice(0, hr[0])) + '<mark>' + esc(n.name.slice(hr[0], hr[1]))
      + '</mark>' + esc(n.name.slice(hr[1])) : esc(n.name);
    const cnt = folderCountText(n);
    const off = n.state !== 'free';
    // 同名，或搜尋中（祖先可能被收合）→ 補一行上層脈絡
    const ctx = (n.dupName || (r.hit && n.depth > 0)) && n.depth > 0 ? folderContext(n.folder) : '';
    const label = [n.folder, stateText(n), cnt.full].filter(Boolean).join('，');
    return `<div class="ftree-row is-${n.state}${n.folder === (staged && staged.folder) ? ' is-staged' : ''}"
        role="treeitem" id="${esc(id)}-r${i}" data-i="${i}" data-folder="${esc(n.folder)}"
        tabindex="${n.folder === active ? 0 : -1}" aria-level="${n.depth + 1}"
        aria-setsize="${size}" aria-posinset="${pos}"
        ${r.hasKids ? `aria-expanded="${r.open}"` : ''}
        aria-selected="${n.folder === (staged && staged.folder)}"${off ? ' aria-disabled="true"' : ''}
        aria-label="${esc(label)}" title="${esc(n.folder)}" style="--lv:${n.depth}">
      <span class="ftree-twisty"${r.hasKids ? ' data-toggle' : ''}>${r.hasKids ? ICON.caret : ''}</span>
      <span class="ftree-ico">${off ? ICON.lock : ICON.folder}</span>
      <span class="ftree-text"><span class="ftree-name">${name}</span>${
        ctx ? `<span class="ftree-ctx">${esc(ctx)}</span>` : ''}</span>
      ${n.state === 'restricted' ? '<span class="ftree-tag lock">已受限</span>'
        : n.state === 'covered' ? `<span class="ftree-tag" title="${esc(n.covered_by)}">被 ${
          esc(n.covered_by)} 涵蓋</span>` : ''}
      ${cnt.full ? `<span class="ftree-meta"><span class="full">${esc(cnt.full)}</span><span class="short">${
        esc(cnt.short)}</span></span>` : ''}
    </div>`;
  };

  const rowEl = folder => {
    const i = rows.findIndex(r => r.node.folder === folder);
    return i < 0 ? null : treeEl.querySelector('#' + id + '-r' + i);
  };
  const focusRow = folder => {
    const r = rowEl(folder);
    if (!r) return;
    treeEl.querySelectorAll('[role=treeitem][tabindex="0"]').forEach(x => x.tabIndex = -1);
    r.tabIndex = 0;
    r.focus({ preventScroll: true });
    r.scrollIntoView({ block: 'nearest' });
  };

  const render = () => {
    const nq = q.value.trim().toLowerCase();
    if (nq !== lastQ) { lastQ = nq; collapsed = new Set(); }
    const v = visibleFolderRows(tree, { expanded, query: q.value, show, collapsed });
    rows = v.rows;
    if (!rows.some(r => r.node.folder === active)) {
      const s = staged && rows.find(r => r.node.folder === staged.folder);
      active = s ? s.node.folder : rows.length ? rows[0].node.folder : null;
    }
    const hadFocus = treeEl.contains(document.activeElement);
    // aria-setsize/posinset 以「畫面上同一個父層底下」為一組
    const groups = new Map();
    rows.forEach(r => {
      const k = r.node.parent ? r.node.parent.folder : '';
      groups.set(k, (groups.get(k) || 0) + 1);
    });
    const seen = new Map();
    treeEl.innerHTML = rows.map((r, i) => {
      const k = r.node.parent ? r.node.parent.folder : '';
      seen.set(k, (seen.get(k) || 0) + 1);
      return rowHTML(r, i, seen.get(k), groups.get(k));
    }).join('');
    const hiddenNote = v.hiddenHits ? `<div>另有 ${v.hiddenHits} 個符合的資料夾已受限或被涵蓋，目前沒顯示。
      <button type="button" class="linkbtn" data-fp-showall>全部顯示</button></div>` : '';
    if (!tree.byPath.size) {
      msg.innerHTML = '還沒有掃到任何資料夾。';
    } else if (lastQ && !rows.length) {
      msg.innerHTML = `沒有符合「<b>${esc(q.value.trim())}</b>」的資料夾。${hiddenNote}`;
    } else {
      msg.innerHTML = hiddenNote;
    }
    msg.hidden = !msg.innerHTML;
    if (lastQ) say(`找到 ${v.hits} 個資料夾`);
    if (hadFocus) focusRow(active);
  };

  const paintFoot = () => {
    okBtn.disabled = !staged;
    stagedEl.innerHTML = staged
      ? `<span class="fp-staged-k">已選</span><span class="fp-staged-v"><b>${esc(staged.name)}</b>
         <code>${esc(staged.folder)}</code></span>`
      : `<span class="fp-staged-k">尚未選擇</span><span class="fp-staged-v">可選 ${free} 個</span>`;
  };

  const paintTrigger = () => {
    if (!chosen) {
      val.innerHTML = '<span class="folder-pick-main is-ph">選擇要限制的資料夾</span>';
      trig.removeAttribute('title');
      return;
    }
    const parent = segsOf(chosen.folder).slice(-2, -1)[0];
    val.innerHTML = `<span class="folder-pick-main">${parent
      ? `<span class="folder-pick-parent">${esc(parent)} / </span>` : ''}<b>${esc(chosen.name)}</b></span>
      <span class="folder-pick-sub">${esc(chosen.folder)}</span>`;
    trig.title = chosen.folder;
  };

  const stage = n => {
    staged = n;
    paintFoot();
    render();
    say(`已暫選 ${n.folder}。按「選擇」帶回表單`);
  };

  const toggle = r => {
    const set = lastQ ? collapsed : expanded;
    // 搜尋中 collapsed 記的是「收起來的」；平常 expanded 記的是「展開的」
    const nowOpen = !r.open;
    if (lastQ) nowOpen ? set.delete(r.node.folder) : set.add(r.node.folder);
    else nowOpen ? set.add(r.node.folder) : set.delete(r.node.folder);
    render();
  };

  // 手機鍵盤彈出來時 visualViewport 會縮，fixed 的 sheet 不會自己跟著 ——
  // 不處理的話 sheet 的下半（正是「選擇」那顆鈕）被鍵盤蓋住。
  const vv = window.visualViewport;
  const fitViewport = () => {
    if (!vv) return;
    const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    pop.style.setProperty('--fp-kb', kb + 'px');
    pop.style.setProperty('--fp-vh', vv.height + 'px');
  };

  const outside = e => { if (!root.contains(e.target)) close(false); };

  const open = () => {
    if (isOpen) return;
    isOpen = true;
    staged = chosen;
    if (chosen) for (let p = chosen.parent; p; p = p.parent) expanded.add(p.folder);
    active = chosen ? chosen.folder : null;
    q.value = '';
    lastQ = '';
    pop.hidden = false;
    back.hidden = false;
    root.dataset.open = '1';
    trig.setAttribute('aria-expanded', 'true');
    const sheet = mq.matches;
    pop.setAttribute('aria-modal', sheet ? 'true' : 'false');
    render();
    paintFoot();
    if (sheet) {
      document.documentElement.classList.add('fp-lock');
      if (vv) { fitViewport(); vv.addEventListener('resize', fitViewport); vv.addEventListener('scroll', fitViewport); }
      // 不自動聚焦搜尋框：手機一聚焦就彈鍵盤，半個 sheet 被蓋掉，而多數時候只是要點一下
      pop.focus({ preventScroll: true });
    } else {
      q.focus({ preventScroll: true });
      pop.scrollIntoView({ block: 'nearest' });
    }
    const s = staged && rowEl(staged.folder);
    if (s) s.scrollIntoView({ block: 'nearest' });
    document.addEventListener('pointerdown', outside, true);
  };

  /** 關掉 = 取消暫選。已確認的 chosen 不動 —— Escape 的意思是「不挑了」，
   *  不是「我不要剛才選好的那個」。 */
  function close(restoreFocus = true) {
    if (!isOpen) return;
    isOpen = false;
    staged = null;
    pop.hidden = true;
    back.hidden = true;
    root.dataset.open = '0';
    trig.setAttribute('aria-expanded', 'false');
    document.documentElement.classList.remove('fp-lock');
    if (vv) { vv.removeEventListener('resize', fitViewport); vv.removeEventListener('scroll', fitViewport); }
    document.removeEventListener('pointerdown', outside, true);
    if (restoreFocus) trig.focus({ preventScroll: true });
  }

  const confirm = () => {
    if (!staged || staged.state !== 'free') return;
    chosen = staged;
    paintTrigger();
    close(true);
    if (onChange) onChange(chosen.folder);
  };

  trig.onclick = () => (isOpen ? close(true) : open());
  trig.onkeydown = e => { if (e.key === 'ArrowDown') { e.preventDefault(); open(); } };
  back.onclick = () => close(true);
  root.querySelectorAll('[data-fp-cancel]').forEach(b => b.onclick = () => close(true));
  okBtn.onclick = confirm;

  root.querySelectorAll('[data-show]').forEach(b => b.onclick = () => {
    const k = b.dataset.show;
    show[k] = !show[k];
    b.setAttribute('aria-pressed', String(show[k]));
    render();
  });
  msg.onclick = e => {
    if (!e.target.closest('[data-fp-showall]')) return;
    show.restricted = show.covered = true;
    root.querySelectorAll('[data-show]').forEach(b => b.setAttribute('aria-pressed', 'true'));
    render();
  };

  q.oninput = render;
  q.onkeydown = e => {
    if (e.key === 'ArrowDown' || e.key === 'Enter') {
      e.preventDefault();
      if (rows.length) focusRow(active || rows[0].node.folder);
    }
  };

  // Escape：搜尋框裡有字 → 先清字；否則關浮層。攔在浮層層級，
  // 焦點在樹裡、在 chip 上、在按鈕上都一樣。
  pop.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    e.preventDefault();
    e.stopPropagation();
    if (document.activeElement === q && q.value) { q.value = ''; render(); }
    else close(true);
  });
  // Tab 出浮層 = 不挑了
  root.addEventListener('focusout', e => {
    if (isOpen && e.relatedTarget && !root.contains(e.relatedTarget)) close(false);
  });

  treeEl.onclick = e => {
    const rowN = e.target.closest('[role=treeitem]');
    if (!rowN) return;
    const r = rows[+rowN.dataset.i];
    active = r.node.folder;
    if (e.target.closest('[data-toggle]')) return toggle(r);
    if (r.node.state === 'free') return stage(r.node);
    // 不能選的列：有子資料夾就當作展開／收合，沒有就只說明原因
    if (r.hasKids) return toggle(r);
    say(stateText(r.node));
    render();
  };
  treeEl.ondblclick = e => {
    const rowN = e.target.closest('[role=treeitem]');
    if (rowN && !e.target.closest('[data-toggle]') && staged
        && staged.folder === rowN.dataset.folder) confirm();
  };

  treeEl.onkeydown = e => {
    const i = rows.findIndex(r => r.node.folder === active);
    if (i < 0) return;
    const r = rows[i];
    const go = j => { if (rows[j]) { active = rows[j].node.folder; focusRow(active); } };
    switch (e.key) {
      case 'ArrowDown': go(i + 1); break;
      case 'ArrowUp': if (i === 0) q.focus(); else go(i - 1); break;
      case 'Home': go(0); break;
      case 'End': go(rows.length - 1); break;
      case 'ArrowRight':
        if (r.hasKids && !r.open) toggle(r);
        else if (r.open) go(i + 1);
        break;
      case 'ArrowLeft':
        if (r.open) toggle(r);
        else if (r.node.parent && rows.some(x => x.node === r.node.parent)) {
          active = r.node.parent.folder; focusRow(active);
        }
        break;
      case 'Enter': case ' ':
        if (r.node.state !== 'free') { if (r.hasKids) toggle(r); else say(stateText(r.node)); break; }
        // 已暫選的那一列再按一次 Enter = 確認，鍵盤不用再 Tab 到「選擇」
        if (e.key === 'Enter' && staged && staged.folder === r.node.folder) confirm();
        else stage(r.node);
        break;
      default: return;
    }
    e.preventDefault();
  };

  paintTrigger();
  return { get: () => (chosen ? chosen.folder : ''), open, close };
}

/* ---- 規則卡 ----
   平常只有摘要：哪個資料夾、幾個人看得到、涵蓋多少子資料夾。
   checkbox 收在「管理授權」後面 —— 那是要修改時才需要的東西。 */
function aclRuleCard(r, ac) {
  const s = aclRuleSummary(r, ac.users, ac.folders);
  const viewers = ac.users.filter(u => !u.is_admin);
  const admins = ac.users.filter(u => u.is_admin);
  const rid = esc(r.id);
  const who = s.viewers
    ? `<div class="acl-rule-line"><span class="acl-who-names">${esc(s.preview)}</span>${
        s.admins ? `<span class="acl-sep">·</span><span class="acl-dim">另有管理員 ${s.admins} 人</span>` : ''}</div>`
    // **沒人被授權要一眼看得到。**剛建好的規則就是這個狀態，而它的意思是
    // 「除了管理員誰都看不到」—— 那通常不是設定的人想要的結果。
    : `<div class="acl-warn" role="note"><b>⚠ 尚未授權任何一般帳號</b>
         <span>管理員仍可存取${s.admins ? `（${s.admins} 人）` : ''}</span></div>`;
  return `
  <div class="acl-rule${s.viewers ? '' : ' is-empty'}" data-rule="${rid}">
    <div class="acl-rule-top">
      <span class="acl-rule-ico">${ICON.lock}</span>
      <div class="acl-rule-id">
        <div class="acl-rule-name">${esc(s.name)}</div>
        <code class="acl-rule-path" title="${esc(r.prefix)}">${esc(r.prefix)}</code>
      </div>
      <span class="acl-who${s.viewers ? '' : ' none'}">${s.viewers ? `${s.viewers} 人可看` : '0 人可看'}</span>
    </div>
    ${who}
    ${r.note ? `<div class="acl-rule-line acl-note">${esc(r.note)}</div>` : ''}
    <div class="acl-rule-line acl-dim">${s.kids.length
      ? `涵蓋 ${s.kids.length} 個子資料夾
         <button type="button" class="linkbtn" data-aclcovers aria-expanded="false"
                 aria-controls="aclCov-${rid}">查看</button>`
      : '底下沒有子資料夾'}</div>
    ${s.kids.length ? `<div class="acl-covers" id="aclCov-${rid}" hidden>${s.kids.map(k =>
      `<code title="${esc(r.prefix.replace(/\/+$/, '') + '/' + k)}">${esc(k)}</code>`).join('')}</div>` : ''}
    <div class="acl-rule-acts">
      <span class="acl-dirty" hidden>● 尚未儲存</span>
      <button type="button" class="btn" data-acledit aria-expanded="false"
              aria-controls="aclEdit-${rid}">管理授權</button>
      <button type="button" class="btn subtle-danger" data-aclrm="${rid}">移除限制</button>
    </div>
    <div class="acl-edit" id="aclEdit-${rid}" hidden>
      <div class="acl-edit-h">一般帳號
        <small>勾選的意思是「即使不是管理員也看得到」</small></div>
      <div class="acl-users">${viewers.length ? viewers.map(u => `
        <label><input type="checkbox" data-aclu="${esc(u.id)}"
          ${(r.user_ids || []).includes(u.id) ? 'checked' : ''}><span>${esc(u.name)}</span></label>`).join('')
        : '<span class="acl-dim">沒有可以授權的一般帳號。</span>'}</div>
      ${admins.length ? `
      <div class="acl-edit-h">管理員 <small>${admins.length} 人</small></div>
      <div class="acl-admins">${admins.map(u => esc(u.name)).join('、')}
        <div class="acl-dim">管理員永遠可以存取 —— 靠的是角色，不是這裡的授權，所以不在上面的勾選清單裡。
        一個人降回一般帳號之後，沒勾到的受限資料夾他就看不到了；要讓他降級後仍然看得到，趁現在先勾起來。</div>
        ${s.keptAdmins.length ? `<div class="acl-dim">其中 ${s.keptAdmins.map(esc).join('、')}
          另外保有授權，降為一般帳號後仍看得到（儲存時會原封保留）。</div>` : ''}</div>` : ''}
      <div class="acl-edit-acts">
        <button type="button" class="btn" data-aclcancel>取消</button>
        <button type="button" class="btn primary" data-aclsave="${rid}" disabled>儲存授權</button>
      </div>
    </div>
  </div>`;
}

function wireAclRule(card, rule, ac) {
  const edit = card.querySelector('.acl-edit');
  const editBtn = card.querySelector('[data-acledit]');
  const save = card.querySelector('[data-aclsave]');
  const dirtyEl = card.querySelector('.acl-dirty');
  const boxes = [...card.querySelectorAll('[data-aclu]')];
  const orig = new Map(boxes.map(b => [b, b.checked]));

  const isDirty = () => boxes.some(b => b.checked !== orig.get(b));
  const paint = () => {
    const d = isDirty();
    card.classList.toggle('is-dirty', d);
    dirtyEl.hidden = !d;
    save.disabled = !d;
  };
  const setOpen = v => {
    edit.hidden = !v;
    editBtn.setAttribute('aria-expanded', String(v));
    editBtn.textContent = v ? '收起授權' : '管理授權';
  };

  editBtn.onclick = () => {
    const v = edit.hidden;
    setOpen(v);
    if (v) (boxes[0] || save).focus({ preventScroll: true });
  };
  // checkbox 只改畫面，不打 API —— 一次送整份名單，見這一節開頭的第 3 點
  boxes.forEach(b => b.onchange = paint);

  card.querySelector('[data-aclcancel]').onclick = () => {
    boxes.forEach(b => { b.checked = orig.get(b); });
    paint();
    setOpen(false);
    editBtn.focus();
  };

  const cov = card.querySelector('[data-aclcovers]');
  if (cov) cov.onclick = () => {
    const list = card.querySelector('.acl-covers');
    list.hidden = !list.hidden;
    cov.setAttribute('aria-expanded', String(!list.hidden));
    cov.textContent = list.hidden ? '查看' : '收起';
  };

  save.onclick = async () => {
    const ids = boxes.filter(b => b.checked).map(b => +b.dataset.aclu);
    const all = grantPayload(rule, ac.users, ids);
    save.disabled = true;
    try {
      await api(`/folders/acl/${rule.id}/grants`,
        { method: 'PUT', body: JSON.stringify({ user_ids: all }) });
      toast(ids.length ? `已授權 ${ids.length} 個帳號` : '已收回全部授權');
      render('library');
    } catch (e) { toast(e.message, true); paint(); }
  };

  card.querySelector('[data-aclrm]').onclick = async () => {
    if (!await confirmBox({ title: '移除限制', ok: '移除', danger: true,
      body: `<code>${esc(rule.prefix)}</code> 之後<b>所有登入者都看得到</b>。` })) return;
    try { await api('/folders/acl/' + rule.id, { method: 'DELETE' });
      toast('已移除限制'); render('library'); }
    catch (e) { toast(e.message, true); }
  };
}

function aclBlock(ac) {
  const stats = folderStats(ac.folders);
  return `
    <div class="box acl">
      <div class="acl-intro">
        <b>沒被列在這裡的資料夾，所有登入者都看得到。</b>
        列進來的只有被授權的一般帳號與管理員看得到，子資料夾自動跟著受限。
        <div class="acl-pwnote">${esc(ac.password_login_note)}</div>
      </div>

      <div class="acl-sec-h">已建立規則 <span>${ac.rules.length}</span></div>
      ${ac.rules.length ? ac.rules.map(r => aclRuleCard(r, ac)).join('')
        : '<div class="empty acl-none">目前沒有受限資料夾。</div>'}

      <div class="acl-sec-h">新增限制</div>
      <div class="acl-form">
        <div class="acl-field">
          <span class="acl-lbl" id="aclPickLbl">資料夾</span>
          ${folderPickerHTML({ id: 'aclPick', stats, labelId: 'aclPickLbl' })}
        </div>
        <div class="acl-field">
          <label class="acl-lbl" for="aclNote">備註</label>
          <input id="aclNote" class="acl-input"
                 placeholder="為什麼限制這個資料夾？（選填）">
        </div>
        <div class="acl-form-foot">
          <span class="acl-dim">只列到第 4 層。更深的請選它的上層 —— 子資料夾自動跟著受限，
            而限制一個葉目錄幾乎永遠不是你想做的事。</span>
          <button type="button" class="btn primary" id="aclAdd" disabled>加入限制</button>
        </div>
      </div>
    </div>`;
}

function wireAcl(el, ac) {
  const tree = buildFolderTree(ac.folders);
  const add = el.querySelector('#aclAdd');
  const lbl = el.querySelector('#aclPickLbl');
  const picker = wireFolderPicker(el, { id: 'aclPick', tree,
    onChange: f => { if (add) add.disabled = !f; } });
  if (lbl) lbl.onclick = () => el.querySelector('#aclPick-btn').focus();

  if (add) add.onclick = async () => {
    const prefix = picker.get();
    // 介面上本來就只挑得到可選的；這裡再擋一次，是因為 picker 的狀態
    // 跟這一份 ac 必須是同一份 —— 對不上就不送（後端也會擋重複，但別讓它走到那裡）。
    const n = tree.byPath.get(prefix);
    if (!prefix || !n || n.state !== 'free') return toast('先選一個可以限制的資料夾', true);
    if (!await confirmBox({
      title: '把這個資料夾設為受限', ok: '設為受限',
      body: `<code>${esc(prefix)}</code> 與它底下的子資料夾，
             之後只有<b>被授權的帳號</b>與管理員看得到。<br><br>
             設定完成的當下<b>還沒有人被授權</b> —— 記得接著按「管理授權」勾選要開放給誰。`
    })) return;
    try {
      await api('/folders/acl', { method: 'POST',
        body: JSON.stringify({ prefix, note: el.querySelector('#aclNote').value }) });
      toast('已設為受限');
      render('library');
    } catch (e) {
      // 409：跟既有規則上下重疊。detail 是物件（{message, conflicts_with, relation}），
      // api() 對物件只會給一句籠統的訊息，所以這裡自己拿 message 出來 ——
      // 管理員要知道的是「跟哪一條」，不是「有東西不能儲存」。
      // 挑選器已經擋掉「在受限資料夾底下」的情況，會走到這裡的通常是
      // 「底下已經有規則的上層資料夾」，或另一個分頁剛好先加了同一條。
      if (e.status === 409 && e.detail && e.detail.message) {
        toast(e.detail.message, true);
        render('library');          // 這份畫面已經過期了，重畫才看得到那一條規則
      } else toast(e.message, true);
    }
  };

  el.querySelectorAll('.acl-rule').forEach(card => {
    const rule = ac.rules.find(r => String(r.id) === card.dataset.rule);
    if (rule) wireAclRule(card, rule, ac);
  });
}

/* ---------------------------------------------------------------- 檔案選擇器

   取代「自己去詳情頁抄 file_id 回來貼」。那個做法的失敗方式很安靜：
   貼錯一個數字不會報錯，而是對**另一支存在的片**跑了一次好幾分鐘的實測。

   形狀照 folderPickerHTML／wireFolderPicker 那一對：一個純函式回 HTML 字串、一個函式接線。
   不要用 <select>：option 在 Chrome/Safari 套不了樣式，而且一列要放兩行
   （片名摘要 + 檔名）option 做不到。篩選也不能靠 option.hidden ——
   那個屬性只有 Firefox 認（見「受限資料夾」那一節開頭的註解）。 */

/** 一列的主行文字。純函式，測試與渲染共用。 */
/** 拆成三份而不是一串字：標題、集數、技術規格。
 *
 *  **為什麼要拆。**原本全部串成「沙丘：第二部 · 2024 · 4K · 166 分」一行等寬
 *  同色的字，掃起來是一團灰 —— 眼睛要找的是片名，卻得先讀過年份與規格。
 *  拆開之後片名可以用正常字重與 --text，規格降成小字 chip，一眼就分得出
 *  「哪一列是我要的片」與「這一列是哪個版本」。 */
function fpParts(it) {
  const pad2 = n => String(n).padStart(2, '0');
  // 沒刮到的孤兒檔沒有 title，退回檔名去掉副檔名 —— 標題空著比什麼都糟。
  const title = it.title || (it.filename || '').replace(/\.[^.]+$/, '') || `#${it.id}`;
  const ep = (it.season != null && it.episode != null)
    ? `S${pad2(it.season)}E${pad2(it.episode)}` : '';
  const meta = [];
  if (it.kind === 'movie' && it.year) meta.push(String(it.year));
  if (it.height) meta.push(it.height >= 2160 ? '4K' : it.height + 'p');
  // 片長一律用「N 分」而不是 dur()：dur() 超過一小時會變成「1 小時 55 分」，
  // 選單裡長度不一很難掃。選單是拿來比對的，數字比句子好用。
  if (it.duration) meta.push(Math.round(it.duration / 60) + ' 分');
  return { title, ep, meta };
}

/** 已選之後顯示在輸入框裡的那一行（只有這裡需要串成一串）。 */
function fpLabel(it) {
  const p = fpParts(it);
  return [p.title, p.ep, ...p.meta].filter(Boolean).join(' · ');
}

/** 檔名太長時**從中間**省略。
 *  同一支片的多個檔案差異通常在尾巴（-part2、.720p vs .1080p），
 *  尾巴被切掉等於把唯一的消歧義資訊丟了。 */
function fpShortName(name) {
  const s = String(name || '');
  return s.length <= 54 ? s : s.slice(0, 26) + '…' + s.slice(-24);
}

const FP_KIND = { home: '家庭', jav: 'JAV' };

function filePick({ id, placeholder = '選擇影片（可直接打字搜尋）…', value = null }) {
  const v = value ? fpLabel(value) : '';
  return `
  <div class="fpick" id="${esc(id)}">
    <div class="fpick-field">
      <input class="fpick-input" id="${esc(id)}-q" type="text" role="combobox"
             autocomplete="off" spellcheck="false"
             aria-expanded="false" aria-autocomplete="list" aria-haspopup="listbox"
             aria-controls="${esc(id)}-list" placeholder="${esc(placeholder)}"
             value="${esc(v)}">
      <button type="button" class="fpick-x" id="${esc(id)}-x" tabindex="-1"
              aria-label="清除已選的檔案"${value ? '' : ' hidden'}>✕</button>
      <!-- 這個箭頭不只是裝飾：它是「這是一個下拉選單，點了會有東西掉下來」
           的唯一視覺線索。沒有它，一個空的框就只是一個要你自己打字的輸入框
           —— 而那正是這整件事要取代的東西。 -->
      <button type="button" class="fpick-caret" id="${esc(id)}-caret" tabindex="-1"
              aria-label="展開清單">▾</button>
    </div>
    <div class="fpick-pop" id="${esc(id)}-pop" hidden>
      <div class="fpick-list" id="${esc(id)}-list" role="listbox" aria-label="搜尋結果"></div>
    </div>
    <div class="fpick-picked" id="${esc(id)}-picked"${value ? '' : ' hidden'}>${
      value ? esc(value.filename || '') : ''}</div>
    <div class="fpick-live" id="${esc(id)}-live" role="status" aria-live="polite"></div>
  </div>`;
}

/** 接線。回傳 { get, set, clear, destroy }。
 *  get() 回 null 或 item 物件；要 file_id 就用 .id。 */
function wireFilePick(el, { id, onPick = null, initial = null }) {
  const root = el.querySelector('#' + id);
  if (!root) return { get: () => null, set() {}, clear() {}, destroy() {} };
  const input = root.querySelector('.fpick-input');
  const pop = root.querySelector('.fpick-pop');
  const list = root.querySelector('.fpick-list');
  const xbtn = root.querySelector('.fpick-x');
  const caret = root.querySelector('.fpick-caret');
  const picked = root.querySelector('.fpick-picked');
  const live = root.querySelector('.fpick-live');

  let items = [];           // 目前清單
  let cur = -1;             // 鍵盤游標（-1 = 沒有 active 列）
  let sel = initial;        // 已選的 item
  let seq = 0;              // 請求序號
  let ctrl = null;          // AbortController
  let timer = null;         // debounce
  let lastQ = null;         // 同一個 q 不重發
  let open = false;

  const say = msg => { if (live.textContent !== msg) live.textContent = msg; };

  const setOpen = v => {
    open = v;
    pop.hidden = !v;
    root.dataset.open = v ? '1' : '0';      // 箭頭靠這個轉方向
    input.setAttribute('aria-expanded', v ? 'true' : 'false');
    if (!v) { cur = -1; input.removeAttribute('aria-activedescendant'); }
  };

  const paintMsg = (html, bad) =>
    { list.innerHTML = `<div class="fpick-msg${bad ? ' bad' : ''}">${html}</div>`; };

  const paint = (rows, more) => {
    items = rows;
    if (!rows.length) {
      const q = input.value.trim();
      // 沒打字卻什麼都沒有 = 片庫是空的，不要說「沒有符合『』的檔案」。
      paintMsg(q && !sel
        ? `沒有符合「<b>${esc(q)}</b>」的檔案。
           <div class="fpick-hint">片名、原文片名、檔名都會找。試試少打幾個字。</div>`
        : '片庫裡還沒有任何檔案。<div class="fpick-hint">掃描完成後這裡就會有東西。</div>');
      say('沒有符合的檔案');
      return;
    }
    let prev = '';
    list.innerHTML = rows.map((it, n) => {
      const p = fpParts(it);
      const dup = p.title === prev;   // 跟上一列同一部片 → 檔名是唯一的區別
      prev = p.title;
      const kind = FP_KIND[it.kind];
      return `<div class="fpick-opt${dup ? ' dup' : ''}" role="option"
                   id="${esc(id)}-o${n}" data-n="${n}" aria-selected="false">
        <div class="fpick-main">
          <span class="fpick-title">${esc(p.title)}</span>
          ${p.ep ? `<span class="fpick-ep">${esc(p.ep)}</span>` : ''}
          ${kind ? `<span class="fpick-kind">${esc(kind)}</span>` : ''}
          ${it.probe_state && it.probe_state !== 'ok'
            ? '<span class="fpick-warn">未分析</span>' : ''}
        </div>
        <div class="fpick-sub">
          <span class="fpick-file" title="${esc(it.filename || '')}">${
            esc(fpShortName(it.filename))}</span>
          ${p.meta.map(m => `<span class="fpick-tag">${esc(m)}</span>`).join('')}
          <span class="fpick-id">#${it.id}</span>
        </div>
      </div>`;
    }).join('') + (more
      ? '<div class="fpick-more">還有更多，再打幾個字縮小範圍</div>' : '');
    say(`找到 ${rows.length} 個檔案`);
  };

  const move = n => {
    const opts = [...list.querySelectorAll('.fpick-opt')];
    if (!opts.length) return;
    if (cur >= 0 && opts[cur]) {
      opts[cur].classList.remove('on');
      opts[cur].setAttribute('aria-selected', 'false');
    }
    cur = (n + opts.length) % opts.length;     // 環繞
    opts[cur].classList.add('on');
    opts[cur].setAttribute('aria-selected', 'true');
    input.setAttribute('aria-activedescendant', opts[cur].id);
    // nearest 不是 center —— center 會讓清單在每次按鍵時都跳一下
    opts[cur].scrollIntoView({ block: 'nearest' });
  };

  const run = async (q) => {
    const my = ++seq;
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    // 還沒有任何結果時才顯示「搜尋中」。已經有舊結果就讓它留著，
    // 只在頂端跑一條線 —— 清單閃一下變空再變回來，使用者一定看得到。
    if (!items.length) paintMsg('搜尋中⋯');
    pop.insertBefore(Object.assign(document.createElement('div'),
      { className: 'fpick-bar' }), pop.firstChild);
    try {
      const r = await api(`/admin/files?q=${encodeURIComponent(q)}`,
                          { signal: ctrl.signal });
      // **序號檢查不能省。**abort 只保證 fetch 的 promise 被 reject，
      // 回應已經在飛行途中、json() 已經完成的那些攔不到 ——
      // 舊結果蓋掉新結果的症狀是「清單內容跟輸入框對不上」，很難查。
      if (my !== seq) return;
      lastQ = q;
      paint(r.items || [], !!r.more);
      setOpen(true);
    } catch (e) {
      if (e.name === 'AbortError' || my !== seq) return;
      paintMsg(`搜尋失敗：${esc(e.message)}`, true);
      setOpen(true);
    } finally {
      if (my === seq) {
        const bar = pop.querySelector('.fpick-bar');
        if (bar) bar.remove();
      }
    }
  };

  const ask = (q, now) => {
    clearTimeout(timer);
    if (q === lastQ && items.length) { setOpen(true); return; }
    timer = setTimeout(() => run(q), now ? 0 : 300);
  };

  const choose = n => {
    const it = items[n];
    if (!it) return;
    sel = it;
    input.value = fpLabel(it);
    picked.textContent = it.filename || '';
    picked.hidden = false;
    xbtn.hidden = false;
    setOpen(false);
    say(`已選 ${fpLabel(it)}，按 Backspace 或 ✕ 可重新搜尋`);
    if (onPick) onPick(it);
  };

  const clear = (focus) => {
    sel = null; lastQ = null; items = [];
    input.value = '';
    picked.hidden = true; picked.textContent = '';
    xbtn.hidden = true;
    setOpen(false);
    if (onPick) onPick(null);
    if (focus) input.focus();
  };

  input.oninput = () => {
    // 已經選了又開始打字 = 他要換一個。立刻把選擇放掉，不然畫面顯示 A、
    // 實際送出的還是 B。
    if (sel) { sel = null; picked.hidden = true; xbtn.hidden = true; if (onPick) onPick(null); }
    cur = -1;
    ask(input.value.trim());
  };

  /* **點一下就要有清單掉下來。**這是「下拉選單」與「搜尋框」的差別，
     也是這個元件存在的理由 —— 一個點了沒反應的空框，跟原本那個要你自己
     去別的頁面抄 id 回來貼的輸入框，對使用者來說是同一個東西。

     所以 focus／click／箭頭三條路都走同一個 openList()：已經有結果就直接
     開，沒有就先去要一批回來（q 空白 = 最近加入的 30 筆）。 */
  const openList = () => {
    if (open) return;
    // **已經選好了又點開 = 他想換一個。**這時候清單要回到完整的那一份，
    // 不是上一次搜尋剩下的那三筆 —— 選單裡只剩自己，看起來像「沒有別的可選」。
    if (sel) { lastQ = null; items = []; ask('', true); return; }
    if (items.length) { setOpen(true); return; }
    ask(input.value.trim(), true);
  };

  input.onfocus = openList;
  input.onclick = openList;
  caret.onclick = () => { if (open) setOpen(false); else { input.focus(); openList(); } };

  input.onkeydown = e => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!open) { ask(input.value.trim(), true); return; }
      move(cur + (e.key === 'ArrowDown' ? 1 : -1));
    } else if (e.key === 'Home' && open) { e.preventDefault(); move(0); }
    else if (e.key === 'End' && open) { e.preventDefault(); move(items.length - 1); }
    else if (e.key === 'Enter') {
      if (open && cur >= 0) { e.preventDefault(); choose(cur); }
    } else if (e.key === 'Escape') {
      // 已選的東西不要因為 Escape 就消失，那是資料損失。只收浮層。
      // stopPropagation 是防著哪天這個元件被放進 confirmBox —— 那裡的
      // 全域 Escape 會把整個對話框關掉。
      if (open) { e.preventDefault(); e.stopPropagation(); setOpen(false); }
      else if (!sel && input.value) clear(true);
    } else if (e.key === 'Backspace' && sel) {
      // 已選狀態下的第一下 Backspace = 清除選擇（不是刪一個字）。
      e.preventDefault(); clear(true);
    } else if (e.key === 'Tab') {
      setOpen(false);       // Tab 是「我要走了」，不是「就選這個」
    }
  };

  // 清單一個委派就夠，不要掛 30 個 handler
  list.onclick = e => {
    const row = e.target.closest('.fpick-opt');
    if (row) choose(+row.dataset.n);
  };
  list.onmousemove = e => {
    const row = e.target.closest('.fpick-opt');
    // 鍵盤與滑鼠共用同一個 .on，不要一個 hover 一個 active —— 會同時亮兩列
    if (row && +row.dataset.n !== cur) move(+row.dataset.n);
  };
  // mousedown 時擋掉預設行為，避免 input 先失焦讓浮層收起來、點擊落空
  list.onmousedown = e => e.preventDefault();
  xbtn.onclick = () => clear(true);

  // 點外面收起來。**不要用 blur** —— blur 在 mousedown 之後、click 之前觸發，
  // 清單一收起來使用者的點擊就落空了（自製 combobox 最經典的 bug）。
  const outside = e => {
    // 後台每次 render() 都把容器 innerHTML 整個換掉，DOM 沒了監聽還在。
    // 監聽自己檢查自己還在不在，不然切幾次分頁就累積幾份。
    // （renderCache 的 warmTimer 用的是同一招。）
    if (!root.isConnected) {
      document.removeEventListener('pointerdown', outside, true);
      return;
    }
    if (!e.target.closest('#' + id)) setOpen(false);
  };
  document.addEventListener('pointerdown', outside, true);

  return {
    get: () => sel,
    set: it => { if (it) { items = [it]; choose(0); } else clear(); },
    clear: () => clear(false),
    destroy: () => document.removeEventListener('pointerdown', outside, true),
  };
}

/* ================================================================ 媒體庫 */
TABS.library = async el => {
  const [st, pr, ac, mn] = await Promise.all(
    [api('/scan/status'), api('/admin/problems'), api('/folders/acl'),
     api('/admin/manual').catch(() => ({ items: [] }))]);
  el.innerHTML = `
    <h2 class="sec">媒體庫</h2>
    <p class="secsub">掃描控制、刮不到與分析失敗的清單、FTP 目錄。</p>

    <div class="box">
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn primary" id="bScan">開始掃描</button>
        <button class="btn" id="bFull">完整重掃</button>
        <button class="btn" id="bReparse">重新分組</button>
        <button class="btn" id="bStop">停止</button>
        <button class="btn" id="bSweep">清掃孤兒</button>
      </div>
      <div id="scanBox" style="margin-top:12px"></div>
    </div>

    <h3 class="sub">手動修正過的條目（${mn.items.length}）</h3>
    <div class="box" style="font-size:12.5px;color:var(--muted)">
      這些條目的 TMDB 資料是你自己指定的。<b>自動刮削（含「完整重掃」）不會蓋掉它們</b> ——
      要連它們一起重刮，在「完整重掃」的確認框裡勾那一格。
    </div>
    ${mn.items.length ? `<div class="scrollx"><table class="t">
      <thead><tr><th>標題</th><th>類型</th><th>年份</th><th>TMDB</th><th>檔案</th><th>修改時間</th></tr></thead>
      <tbody>${mn.items.map(r => `<tr>
        <td><b>${esc(r.title)}</b></td>
        <td>${r.kind === 'tv' ? '影集' : '電影'}</td>
        <td>${r.year || '—'}</td>
        <td>${r.tmdb_id ? `<code>${r.tmdb_id}</code>` : '—'}</td>
        <td>${r.files}</td>
        <td>${when(r.updated_at)}</td></tr>`).join('')}
      </tbody></table></div>
      <div class="rowcards">${mn.items.map(r => `<div class="rowcard">
        <div class="top"><b>${esc(r.title)}</b>
          <span class="st">${r.kind === 'tv' ? '影集' : '電影'}</span></div>
        <dl><dt>年份</dt><dd>${r.year || '—'}</dd>
        <dt>TMDB</dt><dd>${r.tmdb_id || '—'}</dd>
        <dt>檔案</dt><dd>${r.files}</dd>
        <dt>修改時間</dt><dd>${when(r.updated_at)}</dd></dl></div>`).join('')}</div>`
      : '<div class="empty">還沒有手動修正過的條目。</div>'}

    <h3 class="sub">受限資料夾（${ac.rules.length}）</h3>
    ${aclBlock(ac)}

    <h3 class="sub">FTP 目錄</h3>
    <div class="box">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
        <button class="btn" id="ftpUp">上一層</button>
        <code id="ftpPath" style="color:var(--muted);overflow-wrap:anywhere">/</code>
      </div>
      <div id="ftpList"><div class="empty">按「上一層」或下面的資料夾開始瀏覽。</div></div>
    </div>

    <h3 class="sub">還沒刮到資料（${pr.unscraped_total ?? pr.unscraped.length}）</h3>
    ${pr.unscraped.length ? `<div class="scrollx"><table class="t">
      <thead><tr><th>標題</th><th>類型</th><th>年份</th><th>狀態</th><th></th></tr></thead><tbody>
      ${pr.unscraped.map(r => `<tr><td>${esc(r.title)}</td><td>${esc(r.kind)}</td>
        <td>${r.year || '—'}</td><td>${esc(r.scrape_state)}</td>
        <td><button class="btn" data-rescrape="${r.id}">重試</button>
        <button class="btn" data-skip="${r.id}">不用刮</button></td></tr>`).join('')}
      </tbody></table></div>
      <div class="rowcards">${pr.unscraped.map(r => `<div class="rowcard">
        <div class="top"><b>${esc(r.title)}</b><span class="st">${esc(r.scrape_state)}</span></div>
        <dl><dt>類型</dt><dd>${esc(r.kind)}</dd><dt>年份</dt><dd>${r.year || '—'}</dd></dl>
        <div class="acts"><button class="btn" data-rescrape="${r.id}">重試</button>
        <button class="btn" data-skip="${r.id}">不用刮</button></div>
        </div>`).join('')}</div>`
      : '<div class="empty">沒有待處理的條目。</div>'}

    <h3 class="sub">分析失敗（${pr.failed.length}）</h3>
    ${pr.failed.length ? `<div class="scrollx"><table class="t">
      <thead><tr><th>檔名</th><th>所屬</th><th>錯誤</th><th></th></tr></thead><tbody>
      ${pr.failed.map(r => `<tr><td>${esc(r.filename)}</td><td>${esc(r.title || '—')}</td>
        <td style="color:#ffaeae">${esc(r.probe_error || '')}</td>
        <td><button class="btn" data-probe="${r.id}">重試</button></td></tr>`).join('')}
      </tbody></table></div>
      <div class="rowcards">${pr.failed.map(r => `<div class="rowcard">
        <div class="top"><b>${esc(r.filename)}</b></div>
        <dl><dt>所屬</dt><dd>${esc(r.title || '—')}</dd>
        <dt>錯誤</dt><dd style="color:#ffaeae">${esc(r.probe_error || '')}</dd></dl>
        <div class="acts"><button class="btn" data-probe="${r.id}">重試分析</button></div>
        </div>`).join('')}</div>`
      : '<div class="empty">沒有失敗的檔案。</div>'}

    ${(pr.remuxOffline || []).length ? `
    <h3 class="sub">原畫質直送下線（${pr.remuxOffline.length}）</h3>
    <div class="box" style="font-size:12.5px;color:var(--muted)">
      這些檔案的 remux（原畫質直送）出過錯，已經改發單階轉碼 ——
      <b>播放沒有中斷，畫質變成轉碼的畫質</b>。重新掃描這個檔案會清掉這個狀態。
    </div>
    <div class="scrollx"><table class="t">
      <thead><tr><th>檔名</th><th>所屬</th><th>錯誤</th></tr></thead><tbody>
      ${pr.remuxOffline.map(r => `<tr><td>${esc(r.filename)}</td><td>${esc(r.title || '—')}</td>
        <td style="color:#ffaeae">${esc(r.remux_error || '')}</td></tr>`).join('')}
      </tbody></table></div>` : ''}

    ${(pr.keyframeFailed || []).length ? `
    <h3 class="sub">邊界表掃描失敗（${pr.keyframeFailed.length}）</h3>
    <div class="box" style="font-size:12.5px;color:var(--muted)">
      沒有邊界表就沒有原畫質直送那一階，但不影響轉碼播放。
    </div>
    <div class="scrollx"><table class="t">
      <thead><tr><th>檔名</th><th>所屬</th><th>錯誤</th></tr></thead><tbody>
      ${pr.keyframeFailed.map(r => `<tr><td>${esc(r.filename)}</td><td>${esc(r.title || '—')}</td>
        <td style="color:#ffaeae">${esc(r.kf_error || '')}</td></tr>`).join('')}
      </tbody></table></div>` : ''}`;

  wireAcl(el, ac);

  const paint = s => {
    $('#scanBox').innerHTML = s.running
      ? `<b>${esc(s.phase || '')}</b>　${esc(s.current || '')}
         <div style="color:var(--muted);margin-top:6px">
         目錄 ${s.dirs_seen || 0}　影片 ${s.files_found || 0}　相片 ${s.photos_found || 0}
         　已分析 ${s.probed || 0}/${s.probe_total || 0}
         　已讀相片 ${s.photos_read || 0}/${s.photo_total || 0}</div>`
      : `<span style="color:var(--muted)">目前沒有在掃描。${s.finished_at
          ? '上次結束：' + when(s.finished_at) : ''}</span>`;
  };
  paint(st);
  const run = async (q, label) => {
    try { await api('/scan' + q, { method: 'POST' }); toast(label + '已開始'); poll(); }
    catch (e) { toast(e.message, true); }
  };
  $('#bScan').onclick = () => run('', '掃描');
  $('#bFull').onclick = async () => {
    const n = mn.items.length;
    // 這一段文案是修過 bug 之後的實話。原本寫「既有的手動 TMDB 配對可能被覆蓋」——
    // 那時候是真的會被覆蓋（force 的 where 是空字串），而「可能」這個字
    // 讓它聽起來像個小風險。現在預設不會覆蓋，要覆蓋得自己勾。
    const picked = {};
    const ok = await confirmBox({
      title: '完整重掃', ok: '開始重掃',
      collect: m => { picked.remanual = !!m.querySelector('#cbRemanual')?.checked; },
      body: `會重新刮削所有條目、重新分析所有檔案。<br><br>
        ${n ? `<b>手動修正過的 ${n} 筆不會被動到。</b>真的要連它們一起重新自動比對，
               勾下面這一格 —— 你挑的那個 TMDB 配對<b>沒有留在任何地方</b>，
               蓋掉就是蓋掉了。<br>
               <label style="display:flex;gap:8px;align-items:center;margin-top:10px">
                 <input type="checkbox" id="cbRemanual" style="width:16px;height:16px">
                 <span>連手動修正過的 ${n} 筆也重新刮削</span></label>`
             : '目前沒有手動修正過的條目。'}`
    });
    if (!ok) return;
    const re = !!picked.remanual;
    run('?full=true' + (re ? '&remanual=true' : ''), re ? '完整重掃（含手動修正）' : '完整重掃');
  };
  $('#bReparse').onclick = () => run('?reparse=true', '重新分組');
  $('#bStop').onclick = async () => {
    try { await api('/scan/cancel', { method: 'POST' }); toast('已送出停止'); poll(); }
    catch (e) { toast(e.message, true); }
  };
  $('#bSweep').onclick = async () => {
    const r = await api('/maintenance/sweep?mode=report', { method: 'POST' });
    if (!r.total) return toast('沒有孤兒資料');
    const lines = Object.entries(r.found).filter(([, v]) => v)
      .map(([k, v]) => `${esc(k)} × ${v}`).join('<br>');
    if (await confirmBox({
      title: `清掉 ${r.total} 筆孤兒資料？`, ok: '清掉', danger: true,
      body: `這些資料會被永久刪除：<br><br>${lines}<br><br>連帶的海報與縮圖檔也會一起刪。`
    })) {
      const f = await api('/maintenance/sweep?mode=fix', { method: 'POST' });
      toast(`清掉 ${f.total} 筆`); render('library');
    }
  };
  el.onclick = async e => {
    const dir = e.target.closest && e.target.closest('[data-dir]');
    if (dir) return browse(dir.dataset.dir);
    const ds = e.target.dataset || {};
    if (ds.rescrape) {
      try { await api('/rescrape/' + ds.rescrape, { method: 'POST' }); toast('已重新刮削'); render('library'); }
      catch (err) { toast(err.message, true); }
      return;
    }
    if (ds.skip) {
      // 標成「不用刮」：掉出待處理清單，掃描也不會再拿它去打 API。
      try { await api('/items/' + ds.skip + '/skip', { method: 'POST' }); toast('已標記為不用刮'); render('library'); }
      catch (err) { toast(err.message, true); }
      return;
    }
    const id = ds.probe;
    if (!id) return;
    try { await api('/probe/' + id, { method: 'POST' }); toast('已重新分析'); render('library'); }
    catch (err) { toast(err.message, true); }
  };
  // FTP 目錄瀏覽器。只能看片庫設定的目錄，後端也會擋。
  let ftpPath = '/';
  const browse = async path => {
    const box = $('#ftpList');
    box.innerHTML = '<div class="empty">讀取中⋯</div>';
    try {
      const r = await api('/ftp/browse?path=' + encodeURIComponent(path));
      ftpPath = r.path;
      $('#ftpPath').textContent = r.path;
      box.innerHTML = r.entries.length ? r.entries.map(e => `
        <div class="file-row"${e.is_dir ? ` data-dir="${esc(e.path)}" style="cursor:pointer"` : ''}>
          <span>${e.is_dir ? '📁' : '📄'}</span>
          <div class="n"><b>${esc(e.name)}</b>
            <small>${e.is_dir ? '資料夾' : bytes(e.size)}　${esc(e.mtime || '')}</small></div>
        </div>`).join('') : '<div class="empty">這個資料夾是空的。</div>';
    } catch (e) {
      box.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`;
    }
  };
  $('#ftpUp').onclick = () => {
    const up = ftpPath.replace(/\/+$/, '').split('/').slice(0, -1).join('/') || '/';
    browse(up);
  };
  browse('/');

  let timer;
  const poll = async () => {
    clearTimeout(timer);
    try {
      const s = await api('/scan/status');
      if (current !== 'library') return;
      paint(s);
      if (s.running) timer = setTimeout(poll, 1200);
    } catch (e) { /* 切走或斷線就停 */ }
  };
  if (st.running) poll();
};

/* ================================================================ 使用者 */
let userFilter = '';
TABS.users = async el => {
  const d = await api('/users' + (userFilter ? '?status=' + userFilter : ''));
  // 反過來的視角：這個帳號看得到哪些受限資料夾。
  // 規則那一側在「媒體庫」分頁，但「這個人到底能看什麼」是從人這邊問的問題。
  let aclByUser = {};
  try {
    const ac = await api('/folders/acl');
    ac.rules.forEach(r => r.user_ids.forEach(
      uid => (aclByUser[uid] = aclByUser[uid] || []).push(r.prefix)));
  } catch (e) { /* 沒權限或還沒有規則都不影響這一頁 */ }
  const aclOf = u => (aclByUser[u.id] || []);
  if (!d.google_login) {
    el.innerHTML = `<h2 class="sec">使用者</h2>
      <div class="box">目前是<b>密碼登入</b>模式，沒有個別帳號。<br>
      要有帳號制請在 <code>.env</code> 設定 Google 登入
      （<code>GOOGLE_CLIENT_ID</code> 與 <code>GOOGLE_CLIENT_SECRET</code>）。</div>`;
    return;
  }
  const c = d.counts || {};
  const chips = [['', '全部'], ['pending', '待審核'], ['approved', '已核准'],
                 ['rejected', '已拒絕'], ['disabled', '已停權']];
  const acts = u => {
    const b = [];
    if (u.status !== 'approved') b.push(`<button class="btn" data-act="approved" data-id="${u.id}">核准</button>`);
    if (u.status === 'pending') b.push(`<button class="btn" data-act="rejected" data-id="${u.id}">拒絕</button>`);
    if (u.status === 'approved') b.push(`<button class="btn" data-act="disabled" data-id="${u.id}">停權</button>`);
    b.push(u.role === 'owner'
      ? `<button class="btn" data-role="viewer" data-id="${u.id}">改為唯讀</button>`
      : `<button class="btn" data-role="owner" data-id="${u.id}">升為管理員</button>`);
    b.push(`<button class="btn" data-note="${u.id}">備註</button>`);
    b.push(`<button class="btn" data-del="${u.id}">刪除</button>`);
    return b.join(' ');
  };
  el.innerHTML = `
    <h2 class="sec">使用者</h2>
    <p class="secsub">待審核 ${c.pending || 0}　已核准 ${c.approved || 0}　已停權 ${c.disabled || 0}</p>
    <div class="chips" style="margin-bottom:14px">
      ${chips.map(([v, t]) => `<button class="chip ${userFilter === v ? 'on' : ''}"
        data-filter="${v}">${t}</button>`).join('')}
    </div>
    ${d.items.length ? `<div class="scrollx"><table class="t">
      <thead><tr><th>帳號</th><th>角色</th><th>狀態</th><th>最後登入</th><th>備註</th><th></th></tr></thead>
      <tbody>${d.items.map(u => `<tr>
        <td><b>${esc(u.display_name || u.email)}</b><br><small style="color:var(--dim)">${esc(u.email)}</small></td>
        <td><span class="st ${u.role === 'owner' ? 'owner' : ''}">${u.role === 'owner' ? '管理員' : '唯讀'}</span></td>
        <td><span class="st ${esc(u.status)}">${esc(u.status)}</span></td>
        <td>${when(u.last_login_at)}<br><small style="color:var(--dim)">${esc(u.last_login_ip || '')}
          ${esc(u.last_login_country || '')}</small></td>
        <td>${esc(u.note || '')}<br>${aclOf(u).length
          ? `<small style="color:var(--accent)">受限資料夾 ${aclOf(u).length} 個</small>` : ''}</td>
        <td><div style="display:flex;gap:5px;flex-wrap:wrap">${acts(u)}</div></td></tr>`).join('')}
      </tbody></table></div>
      <div class="rowcards">${d.items.map(u => `<div class="rowcard">
        <div class="top"><b>${esc(u.display_name || u.email)}</b>
          <span class="st ${esc(u.status)}">${esc(u.status)}</span></div>
        <dl><dt>帳號</dt><dd>${esc(u.email)}</dd>
        <dt>角色</dt><dd>${u.role === 'owner' ? '管理員' : '唯讀'}</dd>
        <dt>最後登入</dt><dd>${when(u.last_login_at)}　${esc(u.last_login_ip || '')}</dd>
        <dt>備註</dt><dd>${esc(u.note || '—')}</dd>
        <dt>受限資料夾</dt><dd>${aclOf(u).length
          ? aclOf(u).map(p => `<code>${esc(p)}</code>`).join('<br>')
          : '—'}</dd></dl>
        <div class="acts">${acts(u)}</div></div>`).join('')}</div>`
      : '<div class="empty">這個篩選底下沒有帳號。</div>'}`;

  const nameOf = id => {
    const u = d.items.find(x => String(x.id) === String(id)) || {};
    return u.display_name || u.email || ('#' + id);
  };
  const patch = async (id, body) => {
    try { await api('/users/' + id, { method: 'PATCH', body: JSON.stringify(body) });
      toast('已更新'); render('users'); }
    catch (e) { toast(e.message, true); }
  };
  el.onclick = async e => {
    const t = e.target; if (!t.dataset) return;
    if (t.dataset.filter !== undefined) { userFilter = t.dataset.filter; return render('users'); }
    const id = t.dataset.id || t.dataset.del || t.dataset.note;
    if (!id) return;
    // 危險操作的文案一定要重述對象名稱，不然連按兩下等於沒問
    if (t.dataset.act === 'disabled') {
      if (await confirmBox({ title: '停權', danger: true, ok: '停權',
        body: `<b>${esc(nameOf(id))}</b> 會立刻被登出，而且再也登不進來。` }))
        patch(id, { status: 'disabled' });
    } else if (t.dataset.act) {
      patch(id, { status: t.dataset.act });
    } else if (t.dataset.role) {
      const to = t.dataset.role === 'owner' ? '管理員' : '唯讀';
      if (await confirmBox({ title: '改變角色', ok: '改成' + to,
        body: `把 <b>${esc(nameOf(id))}</b> 改成<b>${to}</b>。現有的登入階段會立刻失效。` }))
        patch(id, { role: t.dataset.role });
    } else if (t.dataset.note !== undefined && t.dataset.note) {
      const cur = (d.items.find(x => String(x.id) === String(id)) || {}).note || '';
      const v = prompt(`給「${nameOf(id)}」的備註：`, cur);
      if (v !== null) patch(id, { note: v });
    } else if (t.dataset.del) {
      if (await confirmBox({ title: '刪除帳號', danger: true, ok: '永久刪除',
        body: `<b>${esc(nameOf(id))}</b> 的帳號會被永久刪除，無法復原。<br>
               只是想擋住他的話，「停權」比較好 —— 停權留得住紀錄。` })) {
        try { await api('/users/' + id, { method: 'DELETE' }); toast('已刪除'); render('users'); }
        catch (err) { toast(err.message, true); }
      }
    }
  };
};

/* ================================================================ 播放與轉碼 */
TABS.playback = async el => {
  const d = await api('/diagnostics');
  const t = d.tools || {};
  const row = (k, v) => `<dt>${esc(k)}</dt><dd>${v}</dd>`;
  el.innerHTML = `
    <h2 class="sec">播放與轉碼</h2>
    <p class="secsub">ffmpeg 狀態、硬體加速、實測與快取。</p>

    <div class="grid-cards">
      <div class="stat ${t.ffmpeg && t.ffmpeg.ok ? '' : 'bad'}">
        <b>${t.ffmpeg && t.ffmpeg.ok ? '正常' : '不可用'}</b><small>ffmpeg</small></div>
      <div class="stat ${t.ffprobe && t.ffprobe.ok ? (t.ffprobe.fallback ? 'warn' : '') : 'bad'}">
        <b>${t.ffprobe && t.ffprobe.ok ? (t.ffprobe.fallback ? '備援' : '正常') : '不可用'}</b>
        <small>ffprobe</small></div>
      <div class="stat"><b>${esc(d.hwaccel || '—')}</b><small>硬體編碼（實測後選定）</small></div>
      <div class="stat ${d.failed_files ? 'bad' : ''}"><b>${d.failed_files || 0}</b>
        <small>分析失敗的檔案</small></div>
    </div>

    ${(d.problems || []).length ? `<div class="banner bad" style="margin-top:14px">
      ${d.problems.map(p => esc(p)).join('<br>')}</div>` : ''}

    <h3 class="sub">硬體加速探測</h3>
    <div class="box"><div class="scrollx">
      <pre style="margin:0;font-size:12px;white-space:pre-wrap">${esc(
        JSON.stringify(d.hwaccel_probe || {}, null, 2))}</pre></div></div>

    <h3 class="sub">編碼器深度診斷</h3>
    <div class="box">
      <p style="margin:0 0 10px;color:var(--muted);font-size:12.5px">
        某個硬體編碼器「不能用」的時候，這裡會給 ffmpeg 的完整訊息，
        通常一眼就看得出是驅動太舊、還是這張卡根本沒有那個單元。</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <select id="encSel" style="padding:9px 11px;border-radius:9px;background:var(--bg-2);
          border:1px solid var(--line);min-height:40px">
          ${['nvenc', 'qsv', 'amf', 'videotoolbox', 'libx264']
            .map(n => `<option>${n}</option>`).join('')}</select>
        <button class="btn" id="bEnc">診斷</button>
      </div>
      <div id="encOut" style="margin-top:12px"></div>
    </div>

    <h3 class="sub">轉碼實測</h3>
    <div class="box">
      <p style="margin:0 0 10px;color:var(--muted);font-size:12.5px">
        同一段影片跑好幾種設定並計時，用來找出是哪一個設定拖慢的。
        倍速低於 1 就代表播放會卡。</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        ${filePick({ id: 'benchPick', placeholder: '選擇要實測的影片…' })}
        <button class="btn" id="bBench" disabled>開始實測</button>
      </div>
      <div id="benchOut" style="margin-top:12px"></div>
    </div>

    <h3 class="sub">GPU</h3>
    <div class="box"><div id="gpuOut"><button class="btn" id="bGpu">查詢 GPU</button></div></div>

    <h3 class="sub">FTP 併發量測</h3>
    <div class="box">
      <p style="margin:0 0 10px;color:var(--muted);font-size:12.5px">
        會開好幾條連線並下載一小段資料來量硬上限與有效併發，可能要跑一分鐘以上。
        不會寫入任何檔案。</p>
      <button class="btn" id="bProbe">開始量測</button>
      <div id="probeOut" style="margin-top:12px"></div>
    </div>

    <h3 class="sub">快取</h3>
    <div class="box" id="cacheBox">
      <div style="color:var(--muted);font-size:12.5px">盤點中⋯</div>
    </div>`;

  renderCache();

  $('#bProbe').onclick = async () => {
    const out = $('#probeOut');
    $('#bProbe').disabled = true;
    out.innerHTML = '<span style="color:var(--muted)">量測中，請不要關掉這一頁⋯</span>';
    try {
      const r = await api('/admin/ftp-probe', { method: 'POST' });
      const sp = Object.entries(r.speed || {})
        .map(([n, v]) => `<tr><td>${esc(n)} 條</td><td>${v.mbps} MB/s</td><td>${v.rounds} 回合</td></tr>`).join('');
      out.innerHTML = `<dl class="rowcard" style="margin:0">
          ${row('硬上限', r.limit + ' 條' + (r.firstFail ? `（第 ${r.firstFail} 條開始被拒）` : ''))}
          ${row('建議併發', '<b>' + r.recommend + '</b>')}
        </dl>
        ${sp ? `<div class="scrollx" style="margin-top:10px"><table class="t" style="min-width:0">
          <thead><tr><th>併發</th><th>總吞吐</th><th></th></tr></thead><tbody>${sp}</tbody></table></div>` : ''}
        <ul style="color:var(--muted);font-size:12.5px;margin:10px 0 0;padding-left:18px">
          ${(r.notes || []).map(n => `<li>${esc(n)}</li>`).join('')}</ul>`;
    } catch (e) {
      out.innerHTML = `<span style="color:#ffaeae">${esc(e.message)}</span>`;
    }
    $('#bProbe').disabled = false;
  };
  const pre = o => `<div class="scrollx"><pre style="margin:0;font-size:12px;
    white-space:pre-wrap">${esc(typeof o === 'string' ? o : JSON.stringify(o, null, 2))}</pre></div>`;

  $('#bEnc').onclick = async () => {
    const out = $('#encOut');
    out.innerHTML = '<span style="color:var(--muted)">診斷中⋯</span>';
    try { out.innerHTML = pre(await api('/diagnostics/encoder/' + $('#encSel').value)); }
    catch (e) { out.innerHTML = `<span style="color:#ffaeae">${esc(e.message)}</span>`; }
  };
  // 「還沒選檔案」原本是按下去才 toast 說要先填 id。有了選單之後那是一個
  // 看得見的狀態，用 disabled 表達比讓人按了被罵好。
  const benchPick = wireFilePick(el, {
    id: 'benchPick',
    onPick: it => { $('#bBench').disabled = !it; },
  });
  $('#bBench').onclick = async () => {
    const f = benchPick.get();
    if (!f) return toast('先挑一個檔案', true);
    const id = f.id;
    const out = $('#benchOut');
    $('#bBench').disabled = true;
    out.innerHTML = '<span style="color:var(--muted)">實測中，這會花上一分鐘⋯</span>';
    try {
      const r = await api('/diagnostics/bench/' + encodeURIComponent(id));
      const rows = (r['結果'] || r.results || []);
      out.innerHTML = Array.isArray(rows) && rows.length
        ? `<div class="scrollx"><table class="t" style="min-width:0">
            <thead><tr><th>設定</th><th>耗時</th><th>倍速</th><th>結果</th></tr></thead><tbody>
            ${rows.map(x => `<tr><td>${esc(x['設定'])}</td><td>${x['耗時秒'] ?? '—'} 秒</td>
              <td style="color:${x['倍速'] >= 1 ? 'var(--green)' : 'var(--red)'}">${x['倍速'] ?? '—'}×</td>
              <td>${x.ok ? '正常' : esc(x['錯誤'] || '失敗')}</td></tr>`).join('')}
            </tbody></table></div>` + pre({ 檔案: r['檔案'], 片源: r['片源'] })
        : pre(r);
    } catch (e) { out.innerHTML = `<span style="color:#ffaeae">${esc(e.message)}</span>`; }
    // 跑完要不要放開，看的是「現在還有沒有選著檔案」——
    // 使用者可能在實測期間把選擇清掉了，無條件 false 會讓按鈕變成可按但沒東西。
    $('#bBench').disabled = !benchPick.get();
  };
  $('#bGpu').onclick = async () => {
    const out = $('#gpuOut');
    out.innerHTML = '<span style="color:var(--muted)">查詢中⋯</span>';
    try { out.innerHTML = pre(await api('/diagnostics/gpu')); }
    catch (e) { out.innerHTML = `<span style="color:#ffaeae">${esc(e.message)}</span>`; }
  };
};

/* ---------------------------------------------------------------- 快取

   這一塊刻意**先給看的、再給按的**：原本只有一顆「清空轉碼快取」，按下去
   會刪掉什麼、省下多少空間，按的人完全看不到。清理工具最差的設計就是
   「按一下就刪掉一些東西，而且不知道刪了什麼」（/maintenance/sweep 的
   report→fix 兩段式就是同一個理由）。

   預備（warm）的輪詢只在跑的時候開，跑完就停 —— 後台開著不動的時候不該
   每兩秒打一次伺服器。 */
let warmTimer = null;

/* 選擇器選到的檔案。**存在這裡而不是 DOM 上**，因為 renderCache() 會在
   預備跑完時自己重畫一次（pollWarm 的最後一段）—— 選好檔案、按下預備、
   跑完，然後發現選擇不見了，使用者會以為是自己按錯。 */
let warmSel = null;

async function renderCache() {
  const box = $('#cacheBox');
  if (!box) { clearInterval(warmTimer); warmTimer = null; return; }
  let s;
  try { s = await api('/cache/survey?limit=100'); }
  catch (e) { box.innerHTML = `<span style="color:#ffaeae">${esc(e.message)}</span>`; return; }

  const pct = s.limitMb ? Math.min(100, s.totalBytes / (s.limitMb * 1048576) * 100) : 0;
  const rows = (s.files || []).map(f => `
    <tr>
      <td style="white-space:nowrap">${f.file_id}</td>
      <td title="${esc(f.filename || '')}">${esc((f.filename || '（DB 裡找不到這個檔案）').slice(0, 52))}</td>
      <td style="white-space:nowrap">${bytes(f.bytes)}</td>
      <td style="white-space:nowrap">${f.segments} 段</td>
      <td>${f.profiles.map(p =>
        `<span class="pill${p.stale ? ' warn' : ''}" title="${esc(p.dir)}">v${p.version} ${esc(p.profile)}</span>`
      ).join(' ')}</td>
      <td style="white-space:nowrap">
        <button class="btn sm" data-warm="${f.file_id}">預備</button>
        <button class="btn sm" data-clearfile="${f.file_id}">清除</button>
      </td>
    </tr>`).join('');

  box.innerHTML = `
    <div class="grid-cards" style="margin-bottom:12px">
      <div class="stat"><b>${bytes(s.totalBytes)}</b><small>已用${
        s.limitMb ? `（上限 ${s.limitMb} MB，${pct.toFixed(0)}%）` : ''}</small></div>
      <div class="stat"><b>${s.fileCount}</b><small>有快取的檔案</small></div>
      <div class="stat"><b>${s.totalSegments}</b><small>分段總數</small></div>
      <div class="stat ${s.staleBytes ? 'warn' : ''}"><b>${bytes(s.staleBytes)}</b>
        <small>舊版本殘留（目前 v${s.version}）</small></div>
    </div>

    ${s.staleBytes || s.emptyDirs ? `<div class="banner" style="margin-bottom:12px">
      有 ${s.staleDirs} 個不是 v${s.version} 的資料夾（${bytes(s.staleBytes)}）${
        s.emptyDirs ? `、${s.emptyDirs} 個空資料夾` : ''}。
      這些分段<b>已經沒有人讀得到</b>（路徑帶版本，播放只會找 v${s.version}），
      刪掉不會讓任何一次播放需要重轉。
      <button class="btn sm" id="bStale" style="margin-left:8px">清掉殘留</button>
    </div>` : ''}

    <div id="warmPanel" style="margin-bottom:12px"></div>

    <!-- 這一列的選擇器是給**表格外**的檔案用的：下面的表格只列最大的 N 個，
         被截斷的那些原本完全沒有入口（預備要打 id、清除根本沒有）。 -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      ${filePick({ id: 'warmPickSel', placeholder: '選擇影片…', value: warmSel })}
      <button class="btn" id="bWarmPick"${warmSel ? '' : ' disabled'}>預備這一支⋯</button>
      <button class="btn" id="bClearOne"${warmSel ? '' : ' disabled'}>清除它的快取</button>
      <span style="flex:1"></span>
      <button class="btn danger" id="bCache">清空全部</button>
    </div>

    ${rows ? `<div class="scrollx"><table class="t">
      <thead><tr><th>id</th><th>檔名</th><th>佔用</th><th>分段</th><th>階別</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      ${s.truncated ? `<p style="color:var(--muted);font-size:12px;margin:8px 0 0">
        只列出最大的 ${s.files.length} 個（統計數字是全部算的）。</p>` : ''}`
      : '<p style="color:var(--muted);font-size:12.5px;margin:0">目前沒有任何快取分段。</p>'}`;

  $('#bCache').onclick = async () => {
    if (!await confirmBox({ title: '清空轉碼快取', ok: '清空', danger: true,
      body: `所有已轉好的分段會被刪掉（${bytes(s.totalBytes)}、${s.totalSegments} 段），
             下次播放要重轉。<b>不影響 FTP 上的片源。</b>` })) return;
    try { await api('/cache/clear', { method: 'POST' }); toast('已清空'); renderCache(); }
    catch (e) { toast(e.message, true); }
  };

  const stale = $('#bStale');
  if (stale) stale.onclick = async () => {
    try {
      const r = await api('/cache/clear-stale', { method: 'POST' });
      toast(`清掉 ${r.dirs} 個資料夾，釋出 ${bytes(r.bytes)}`);
      renderCache();
    } catch (e) { toast(e.message, true); }
  };

  // 選擇器要在 box.innerHTML 賦值**之後**才接線 —— 順序反了 querySelector
  // 回 null，而且不會噴錯（指派到 null 才噴，querySelector 本身不會）。
  // 這跟 wireAcl(el, ac) 必須在 el.innerHTML 之後被呼叫是同一個約束。
  const wp = wireFilePick(box, {
    id: 'warmPickSel',
    initial: warmSel,
    onPick: it => {
      warmSel = it;
      $('#bWarmPick').disabled = !it;
      $('#bClearOne').disabled = !it;
    },
  });

  $('#bWarmPick').onclick = () => {
    const f = wp.get();
    if (!f) return toast('先挑一個檔案', true);
    warmPick(f.id);
  };
  // 表格只列最大的 N 個，被截斷的那些原本沒有清除入口。
  // clearOne() 本來就有確認框，直接沿用，不必再問一次。
  $('#bClearOne').onclick = () => {
    const f = wp.get();
    if (!f) return toast('先挑一個檔案', true);
    clearOne(f.id, f.filename);
  };

  box.onclick = e => {
    const ds = e.target.dataset || {};
    if (ds.warm) return warmPick(parseInt(ds.warm, 10));
    if (ds.clearfile) return clearOne(parseInt(ds.clearfile, 10));
  };

  pollWarm();
}

async function clearOne(fileId, fallbackName) {
  const f = (await api('/cache/survey?limit=2000')).files.find(x => x.file_id === fileId);
  // 從選擇器挑的檔案可能根本沒有快取（表格裡找不到它）。這時候「清除」是
  // 個 no-op，與其開一個講不出要刪什麼的確認框，不如直接說清楚。
  if (!f) {
    toast(`${fallbackName || 'file_id=' + fileId} 目前沒有任何快取分段`);
    return;
  }
  if (!await confirmBox({
    title: `清除這一支的快取？`, ok: '清除', danger: true,
    body: `<b>${esc(f.filename || ('file_id=' + fileId))}</b><br><br>
           會刪掉 ${bytes(f.bytes)}、${f.segments} 段，
           下次播放要重轉。<b>不影響片源。</b>`
  })) return;
  try {
    await api('/cache/clear?file_id=' + fileId, { method: 'POST' });
    toast('已清除'); renderCache();
  } catch (e) { toast(e.message, true); }
}

/** 預備前先問清楚要哪一階、整支還是只有開頭 —— 整支片可能要跑很久。 */
async function warmPick(fileId) {
  let o;
  try { o = await api('/cache/warm/options?file_id=' + fileId); }
  catch (e) { return toast(e.message, true); }
  if (!o.ok) return toast(o.message, true);

  const opts = o.options.map((p, i) => `
    <label style="display:block;margin:6px 0">
      <input type="radio" name="wp" value="${esc(p.profile)}" ${i ? '' : 'checked'}>
      ${esc(p.label)} — 共 ${p.total} 段，已有 ${p.cached} 段</label>`).join('');

  // confirmBox 只回 true/false，`collect` 是在關掉之前被呼叫的 side effect ——
  // 選項要在那個時機抄下來，關掉之後 DOM 就沒了。
  let picked = { profile: o.options[0].profile, head: 0 };
  const ok = await confirmBox({
    title: '預備分段', ok: '開始',
    body: `<b>${esc(o.filename || '')}</b><br>
      <span style="color:var(--muted)">長度 ${Math.round(o.duration / 60)} 分鐘</span>
      <div style="margin-top:10px">${opts}</div>
      <label style="display:block;margin-top:10px">
        <input type="checkbox" id="wpHead"> 只預熱開頭 10 段（讓開播不用等，很快跑完）</label>
      <p style="color:var(--muted);font-size:12px;margin:10px 0 0">
        整支預備會佔用 CPU 一段時間，但優先權比正在播的人低，而且隨時可以取消。</p>`,
    collect: m => {
      const r = m.querySelector('input[name=wp]:checked');
      const h = m.querySelector('#wpHead');
      picked = { profile: (r && r.value) || o.options[0].profile,
                 head: h && h.checked ? 10 : 0 };
    },
  });
  if (!ok) return;
  const { profile, head } = picked;
  try {
    const r = await api(`/cache/warm?file_id=${fileId}&profile=${encodeURIComponent(profile)}&head=${head}`,
      { method: 'POST' });
    toast(r.message || '已開始');
    pollWarm();
  } catch (e) { toast(e.message, true); }
}

async function pollWarm() {
  const panel = $('#warmPanel');
  if (!panel) { clearInterval(warmTimer); warmTimer = null; return; }
  let w;
  try { w = await api('/cache/warm'); } catch { return; }

  if (!w.running && !w.phase) { panel.innerHTML = ''; }
  else {
    const pctDone = w.total ? ((w.done + w.skipped) / w.total * 100) : 0;
    const eta = w.eta > 0 ? `，預估還要 ${Math.ceil(w.eta / 60)} 分` : '';
    const cls = w.phase === 'error' ? 'bad' : '';
    panel.innerHTML = `<div class="banner ${cls}">
      <b>${w.running ? '預備中' : ({ done: '預備完成', cancelled: '已取消', error: '預備失敗' }[w.phase] || w.phase)}</b>
      ${esc((w.filename || '').slice(0, 46))}
      <div style="margin-top:6px">
        ${w.done + w.skipped} / ${w.total} 段（新轉 ${w.done}、本來就有 ${w.skipped}${
          w.failed ? `、失敗 ${w.failed}` : ''}）${w.running ? eta : ''}
      </div>
      <div style="height:6px;background:var(--bg-2);border-radius:4px;margin-top:8px;overflow:hidden">
        <div style="height:100%;width:${pctDone.toFixed(1)}%;background:var(--accent, #6ea8fe)"></div></div>
      ${w.error ? `<div style="margin-top:6px;color:#ffaeae">${esc(w.error)}</div>` : ''}
      ${(w.errors || []).length ? `<div style="margin-top:6px;font-size:12px;color:var(--muted)">
        ${w.errors.map(x => esc(x)).join('<br>')}</div>` : ''}
      ${w.running ? '<button class="btn sm" id="bWarmCancel" style="margin-top:8px">取消</button>' : ''}
    </div>`;
    const c = $('#bWarmCancel');
    if (c) c.onclick = async () => {
      try { await api('/cache/warm/cancel', { method: 'POST' }); toast('已送出取消'); }
      catch (e) { toast(e.message, true); }
    };
  }

  // 跑完就把輪詢收掉，並把盤點數字更新一次（快取變大了）。
  if (w.running && !warmTimer) warmTimer = setInterval(pollWarm, 2000);
  if (!w.running && warmTimer) {
    clearInterval(warmTimer); warmTimer = null;
    renderCache();
  }
}

/* ================================================================ 紀錄 */
let logPage = 0, logEvent = '';
TABS.logs = async el => {
  const LIMIT = 25;
  const [d, cfg] = await Promise.all([
    api(`/audit/logins?limit=${LIMIT}&offset=${logPage * LIMIT}` +
        (logEvent ? '&event=' + encodeURIComponent(logEvent) : '')),
    api('/params/audit?limit=50'),
  ]);
  const pages = Math.max(1, Math.ceil(d.total / LIMIT));
  const evs = [['', '全部'], ['success', '登入成功'], ['failed', '密碼錯誤'],
               ['register', '註冊'], ['pending', '待審核'], ['locked', '鎖定']];
  el.innerHTML = `
    <h2 class="sec">紀錄</h2>
    <p class="secsub">登入稽核與設定變更。共 ${d.total} 筆登入紀錄。</p>
    ${d.geo_available ? '' : `<div class="banner">看不到來源國家／城市：那些是
      Cloudflare 的標頭，要在 Cloudflare 後台開 <b>Managed Transforms</b> 才會送過來。</div>`}

    <div class="chips" style="margin-bottom:12px">
      ${evs.map(([v, t]) => `<button class="chip ${logEvent === v ? 'on' : ''}"
        data-ev="${v}">${t}</button>`).join('')}
    </div>

    ${d.items.length ? `<div class="scrollx"><table class="t">
      <thead><tr><th>時間</th><th>事件</th><th>身分</th><th>來源</th><th>位置</th></tr></thead>
      <tbody>${d.items.map(r => `<tr><td style="white-space:nowrap">${when(r.at)}</td>
        <td>${esc(r.event)}</td><td>${esc(r.email || r.role || '—')}</td>
        <td>${esc(r.ip || '—')}</td><td>${esc(r.where || '—')}</td></tr>`).join('')}
      </tbody></table></div>
      <div class="rowcards">${d.items.map(r => `<div class="rowcard">
        <div class="top"><b>${esc(r.event)}</b><span style="color:var(--dim);font-size:12px">${when(r.at)}</span></div>
        <dl><dt>身分</dt><dd>${esc(r.email || r.role || '—')}</dd>
        <dt>來源</dt><dd>${esc(r.ip || '—')}</dd>
        <dt>位置</dt><dd>${esc(r.where || '—')}</dd></dl></div>`).join('')}</div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:14px">
        <button class="btn" id="lPrev" ${logPage ? '' : 'disabled'}>上一頁</button>
        <span style="color:var(--muted);font-size:12.5px">第 ${logPage + 1} / ${pages} 頁</span>
        <button class="btn" id="lNext" ${logPage + 1 >= pages ? 'disabled' : ''}>下一頁</button>
      </div>`
      : '<div class="empty">這個篩選底下沒有紀錄。</div>'}

    <h3 class="sub">掃描日誌</h3>
    <div class="box" id="scanLog"><div class="empty">載入中⋯</div></div>

    <h3 class="sub">設定變更</h3>
    ${cfg.entries.length ? `<div class="scrollx"><table class="t">
      <thead><tr><th>時間</th><th>設定</th><th>從</th><th>改成</th><th>誰</th></tr></thead>
      <tbody>${cfg.entries.map(r => `<tr><td style="white-space:nowrap">${when(r.at)}</td>
        <td><code>${esc(r.k)}</code></td><td>${esc(r.old_value || '')}</td>
        <td>${esc(r.new_value || '')}</td><td>${esc(r.actor || '')}</td></tr>`).join('')}
      </tbody></table></div>
      <div class="rowcards">${cfg.entries.map(r => `<div class="rowcard">
        <div class="top"><b>${esc(r.k)}</b><span style="color:var(--dim);font-size:12px">${when(r.at)}</span></div>
        <dl><dt>從</dt><dd>${esc(r.old_value || '')}</dd>
        <dt>改成</dt><dd>${esc(r.new_value || '')}</dd>
        <dt>誰</dt><dd>${esc(r.actor || '')}</dd></dl></div>`).join('')}</div>
      <p style="color:var(--dim);font-size:12px;margin-top:10px">
        秘密欄位只記「已變更」，不記值也不記長度 —— 長度也是情報。</p>`
      : '<div class="empty">還沒有人改過設定。</div>'}`;

  api('/scan/status').then(st => {
    const box = $('#scanLog');
    if (!box) return;
    box.innerHTML = (st.log || []).length
      ? `<div class="scrollx"><pre style="margin:0;font-size:12px;white-space:pre-wrap">${
          esc(st.log.join('\n'))}</pre></div>`
      : '<div class="empty">上一次掃描沒有留下訊息。</div>';
  }).catch(e => { $('#scanLog').innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; });

  el.onclick = e => {
    const t = e.target;
    if (t.dataset && t.dataset.ev !== undefined) { logEvent = t.dataset.ev; logPage = 0; render('logs'); }
    else if (t.id === 'lPrev') { logPage = Math.max(0, logPage - 1); render('logs'); }
    else if (t.id === 'lNext') { logPage++; render('logs'); }
  };
};

/* ================================================================ 系統 */
TABS.system = async el => {
  const diag = await api('/diagnostics');
  el.innerHTML = `
    <h2 class="sec">系統</h2>
    <p class="secsub">資料庫狀態與外部相依。參數調整搬到左邊的〈系統參數〉。</p>

    <h3 class="sub">狀態</h3>
    <div class="grid-cards">
      <div class="stat ${diag.user_db && diag.user_db.ok ? '' : 'bad'}">
        <b>${esc((diag.user_db || {}).store || '—')}</b><small>使用者資料庫</small></div>
      <div class="stat ${diag.ftp && diag.ftp.ok ? '' : 'bad'}">
        <b>${diag.ftp && diag.ftp.ok ? '正常' : '連不上'}</b><small>FTP</small></div>
      <div class="stat ${diag.tmdb_enabled ? '' : 'warn'}">
        <b>${diag.tmdb_enabled ? '已設定' : '未設定'}</b><small>TMDB</small></div>
    </div>
    <div class="box" style="margin-top:12px">
      <button class="btn" id="bFtpTest">測試 FTP 連線</button>
      <button class="btn" id="bRecheck">重新檢查所有相依</button>
      <div id="sysOut" style="margin-top:10px"></div>
    </div>`;

  $('#bFtpTest').onclick = async () => {
    const out = $('#sysOut');
    out.innerHTML = '<span style="color:var(--muted)">測試中⋯</span>';
    try {
      const r = await api('/ftp/test');
      out.innerHTML = r.ok
        ? `<span style="color:var(--green)">連得上。${esc(r.welcome || '')}</span>`
        : `<span style="color:#ffaeae">連不上：${esc(r.error || '')}</span>`;
    } catch (e) { out.innerHTML = `<span style="color:#ffaeae">${esc(e.message)}</span>`; }
  };
  $('#bRecheck').onclick = async () => {
    $('#sysOut').innerHTML = '<span style="color:var(--muted)">重新檢查中⋯</span>';
    try { await api('/diagnostics/recheck', { method: 'POST' }); toast('已重新檢查'); render('system'); }
    catch (e) { toast(e.message, true); }
  };
};

/* ================================================================ 系統參數
 *
 * 這一頁的形狀是量出來的，不是設計出來的。實測 Yu 的機器：**UI 上 48 項，
 * 其中 43 項被 .env 蓋住、只有 5 項真的能在這裡改。**（Tier 0／Tier 4 的參數
 * `in_ui` 是 false，根本不會出現在這裡 —— 所以沒有「永遠唯讀」的列。）
 *
 * 所以第一軸不是分類，是**「這一項現在改不改得動」**：
 *   可以在這裡改 / 由 .env 決定
 * 分類當第二軸，給「我要找 FFMPEG_HWACCEL 在哪」用。搜尋跨全部。
 *
 * 另外：每一列都是一顆「儲存」的話，48 列就是上百顆按鈕，而且改三項要按三次、
 * 錯一項不知道另外兩項存了沒。改成整批送 PUT /api/params ——
 * 後端先全部驗證再全部寫，錯了一個字都不寫。
 */
const APPLY_TEXT = { hot: '即時生效', reload: '存檔後套用', restart: '需重啟' };
const CAT_FREE = '__free', CAT_ENV = '__env';
const CAT_NAME = { [CAT_FREE]: '可以在這裡改', [CAT_ENV]: '由 .env 決定' };

let pd = null;              // 最後一次 /params 的結果
let paramDraft = {};        // key → 使用者改到一半、還沒存的值
let paramErr = {};          // key → 後端回的驗證錯誤
let paramCat = CAT_FREE;
let paramQ = '';

const catLabel = c => CAT_NAME[c] || c;

function paramMatch(i, q) {
  if (!q) return true;
  const hay = `${i.key} ${i.label} ${i.help || ''} ${i.section}`.toLowerCase();
  // 每個詞都要中，順序不管 —— 「nvenc 品質」和「品質 nvenc」要一樣找得到
  return q.toLowerCase().split(/\s+/).filter(Boolean).every(w => hay.includes(w));
}

function catItems(cat) {
  if (cat === CAT_FREE) return pd.items.filter(i => !i.locked);
  if (cat === CAT_ENV) return pd.items.filter(i => i.locked);
  return pd.items.filter(i => i.section === cat);
}

function renderParamNav() {
  const nav = $('#paramNav');
  if (!nav) return;
  if (!pd) { nav.hidden = true; return; }
  const btn = (cat, name, n, cls = '') => `
    <button data-cat="${esc(cat)}" class="${cls}${paramCat === cat && !paramQ ? ' on' : ''}">
      <span>${esc(name)}</span><span class="n">${n}</span></button>`;
  nav.innerHTML =
    btn(CAT_FREE, CAT_NAME[CAT_FREE], pd.items.filter(i => !i.locked).length, 'strong ') +
    btn(CAT_ENV, CAT_NAME[CAT_ENV], pd.items.filter(i => i.locked).length, 'muted ') +
    '<div class="sep"></div>' +
    pd.sections.map(s => btn(s, s, pd.items.filter(i => i.section === s).length)).join('');
  nav.hidden = current !== 'params';
  nav.querySelectorAll('[data-cat]').forEach(b => b.onclick = () => {
    paramCat = b.dataset.cat;
    paramQ = '';
    if ($('#pq')) $('#pq').value = '';
    renderParamNav();
    paintParams();
  });
}

function paramBanners() {
  const out = [];
  if (pd.unknownEnvKeys.length) out.push(`<div class="banner bad">
    <b>.env 裡有程式不認得的設定</b>（等於完全沒有作用）：<br>
    ${pd.unknownEnvKeys.map(u => `<code>${esc(u.key)}</code>` +
      (u.guess ? ` → 是不是想打 <code>${esc(u.guess)}</code>？` : '')).join('<br>')}</div>`);
  const sh = pd.items.filter(i => i.shadowed);
  if (sh.length) out.push(`<div class="banner">
    <b>有 ${sh.length} 項在這裡存過的值被 .env 蓋掉了</b>，所以不會生效。
    要讓它生效就把 <code>.env</code> 裡那一行刪掉；不需要了就在該項按「清除這裡存的值」。<br>
    ${sh.map(i => `<code>${esc(i.key)}</code>`).join(' ')}</div>`);
  if (pd.needsRestart.length) out.push(`<div class="banner">
    <b>這些設定已經存好，但要重開服務才生效</b>：
    ${pd.needsRestart.map(k => `<code>${esc(k)}</code>`).join(' ')}</div>`);
  return out.join('');
}

TABS.params = async el => {
  pd = await api('/params');
  // 標頭的「需重啟」跟著更新。存完之後如果不更新，畫面會停在存之前的狀態，
  // 而使用者剛剛才被告知「有幾項要重開服務」—— 兩個訊息互相矛盾。
  $('#restartBar').hidden = !pd.needsRestart.length;
  if (paramCat !== CAT_FREE && paramCat !== CAT_ENV && !pd.sections.includes(paramCat))
    paramCat = CAT_FREE;
  const free = pd.items.filter(i => !i.locked).length;

  el.innerHTML = `
    <h2 class="sec">系統參數</h2>
    <p class="secsub">解析順序是 <b>.env / 環境變數　&gt;　這裡存的值　&gt;　程式預設值</b>，
      沒有例外。目前 <b>${pd.items.length}</b> 項裡有 <b>${pd.items.length - free}</b>
      項由 <code>.env</code> 決定、在這裡改不動。<br>
      連線資訊、綁定位址、密碼這類改錯會讓你進不來的設定，刻意<b>只能</b>在
      <code>.env</code> 改，這一頁完全不會出現。</p>
    ${paramBanners()}
    <div class="ptools">
      <input id="pq" type="search" autocomplete="off"
             placeholder="搜尋設定名稱或說明…（例如 nvenc、字幕、逾時）">
      <span class="pcount" id="pcount"></span>
    </div>
    <div id="paramList"></div>
    <div class="savebar" id="savebar" hidden>
      <span class="info" id="saveInfo"></span>
      <div class="acts">
        <button class="btn" id="bDiscard">全部還原</button>
        <button class="btn primary" id="bSaveAll">儲存</button>
      </div>
    </div>`;

  $('#pq').value = paramQ;
  $('#pq').oninput = e => { paramQ = e.target.value.trim(); renderParamNav(); paintParams(); };
  $('#bDiscard').onclick = () => { paramDraft = {}; paramErr = {}; paintParams(); };
  $('#bSaveAll').onclick = saveAllParams;
  renderParamNav();
  paintParams();
};

function paintParams() {
  const list = $('#paramList');
  if (!list || !pd) return;
  const base = paramQ ? pd.items : catItems(paramCat);
  const items = base.filter(i => paramMatch(i, paramQ));
  $('#pcount').textContent = paramQ
    ? `搜尋結果 ${items.length} 項`
    : `${catLabel(paramCat)}　${items.length} 項`;

  if (!items.length) {
    list.innerHTML = `<div class="empty"><h3>沒有符合的設定</h3>
      <p>${paramQ ? '換個關鍵字，或看左邊的分類。' : '這個分類目前沒有項目。'}</p></div>`;
    return syncSaveBar();
  }

  // 跨分類看的時候（搜尋、或那兩個狀態分類）才需要小標；看單一分類時是多餘的
  const grouped = !!paramQ || paramCat === CAT_FREE || paramCat === CAT_ENV;
  let html = '';
  if (grouped) {
    const bySec = {};
    items.forEach(i => (bySec[i.section] = bySec[i.section] || []).push(i));
    html = pd.sections.filter(s => bySec[s]).map(s =>
      `<h3 class="sub">${esc(s)}</h3><div class="box">${bySec[s].map(paramRow).join('')}</div>`
    ).join('');
  } else {
    html = `<div class="box">${items.map(paramRow).join('')}</div>`;
  }
  // 鎖住的列會出現在畫面上時，把「要怎麼改」講一次，而不是每列印一次
  if (items.some(i => i.locked)) {
    html = `<div class="box lockhint">
      <b>🔒 的項目由 <code>.env</code> 決定，在這裡改不動。</b>
      要改就改 <code>.env</code> 再重啟；想改成在後台管理，把 <code>.env</code>
      裡那一行刪掉再重啟 —— 解析順序是
      <code>.env</code> &gt; 這裡存的值 &gt; 程式預設值，沒有例外。</div>` + html;
  }
  list.innerHTML = html;

  list.oninput = e => {
    const wrap = e.target.closest('.param');
    if (!wrap || wrap.classList.contains('ro')) return;
    const key = wrap.dataset.key;
    paramDraft[key] = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    delete paramErr[key];
    wrap.classList.add('dirty');
    wrap.classList.remove('bad');
    syncSaveBar();
  };
  list.onclick = async e => {
    const b = e.target.closest('button[data-do]');
    if (!b) return;
    const wrap = b.closest('.param');
    const key = wrap.dataset.key;
    const item = pd.items.find(i => i.key === key);
    const inp = wrap.querySelector('input,select');
    if (b.dataset.do === 'default') {
      setInput(inp, item.candidates.default, item);
      paramDraft[key] = item.candidates.default;
      wrap.classList.add('dirty');
      syncSaveBar();
    }
    if (b.dataset.do === 'revert') {
      const v = item.candidates.db !== null && item.candidates.db !== undefined
        ? item.candidates.db : item.candidates.default;
      setInput(inp, v, item);
      delete paramDraft[key];
      delete paramErr[key];
      wrap.classList.remove('dirty', 'bad');
      syncSaveBar();
    }
    if (b.dataset.do === 'clear') {
      if (!await confirmBox({ title: '清除這裡存的值', ok: '清除',
        body: `<code>${esc(key)}</code> 在這裡存的值會被刪掉，之後就完全由
               <code>.env</code>（或程式預設值）決定。` })) return;
      try {
        await api('/params/' + key, { method: 'DELETE' });
        delete paramDraft[key];
        toast('已清除');
        render('params');
      } catch (err) { toast(err.message, true); }
    }
  };
  syncSaveBar();
}

function syncSaveBar() {
  const bar = $('#savebar');
  if (!bar) return;
  const keys = Object.keys(paramDraft);
  bar.hidden = !keys.length;
  if (!keys.length) return;
  const modes = keys.map(k => pd.items.find(i => i.key === k)?.applyMode);
  const restart = modes.filter(m => m === 'restart').length;
  $('#saveInfo').innerHTML = `<b>${keys.length}</b> 項未儲存` +
    (restart ? `　<span class="apply restart">其中 ${restart} 項需重啟</span>` : '');
  $('#bSaveAll').textContent = `儲存 ${keys.length} 項變更`;
}

async function saveAllParams() {
  const keys = Object.keys(paramDraft);
  if (!keys.length) return;

  // lockout 的參數目前全都是 Tier 0、根本不在這一頁，所以這一段實務上不會觸發。
  // 留著是因為某一天有人把某個 lockout 參數降成 Tier 2 時，
  // 這裡是唯一會提醒他「這會把自己鎖在外面」的地方。
  const lock = keys.filter(k => pd.items.find(i => i.key === k)?.lockout);
  if (lock.length && !await confirmBox({
    title: '這幾項可能讓你進不了後台', ok: '我知道，還是要存', danger: true,
    body: `${lock.map(k => `<code>${esc(k)}</code>`).join('、')} 改錯的話，
           你可能沒辦法再登入或連到這個後台。<br><br>
           真的進不來的時候，在伺服器上執行：<br>
           <code>python -m app.paramstore unset &lt;KEY&gt;</code><br><br>
           或直接編輯 <code>.env</code> —— 它的優先度比這裡高。`
  })) return;

  const btn = $('#bSaveAll');
  btn.disabled = true;
  try {
    const r = await api('/params', { method: 'PUT', body: JSON.stringify({ values: paramDraft }) });
    const modes = new Set((r.items || []).map(i => i.applyMode));
    const stillLocked = (r.items || []).filter(i => i.locked).map(i => i.key);
    paramDraft = {};
    paramErr = {};
    if (stillLocked.length) {
      toast(`存起來了，但 ${stillLocked.length} 項由環境變數決定，所以不會生效`, true);
    } else {
      toast(modes.has('restart') ? '已存好，有幾項要重開服務才生效'
        : modes.has('reload') ? '已儲存並套用' : '已儲存並生效');
    }
    render('params');
  } catch (e) {
    // 後端是整批驗證：錯了就一項都沒寫。所以這裡要把錯誤標回每一列，
    // 而且**不能清掉 paramDraft** —— 使用者打的東西不能被一個錯誤吃掉。
    if (e.detail && typeof e.detail === 'object') {
      paramErr = e.detail;
      toast(`有 ${Object.keys(e.detail).length} 項不合法，一項都沒有儲存`, true);
      paintParams();
    } else {
      toast(e.message, true);
    }
  } finally {
    btn.disabled = false;
  }
}

function setInput(inp, v, item) {
  if (!inp) return;
  if (item.type === 'bool') inp.checked = !!v;
  else inp.value = v === null || v === undefined ? '' : v;
}

function paramRow(i) {
  const locked = i.locked;
  const draft = paramDraft[i.key];
  const dirty = draft !== undefined;
  const err = paramErr[i.key];
  const cur = dirty ? draft : (i.secret ? '' : (i.effective.value ?? ''));

  let ctl;
  if (locked) {
    // 被 .env 蓋住的項目不給輸入框。給一個停用的框只是讓人試著打字然後發現打不進去 ——
    // 直接把生效值印出來，再說清楚要改就去改 .env。
    ctl = `<div class="roval">${i.secret ? '（已設定）' : esc(String(i.effective.value ?? '—'))}</div>`;
  } else if (i.type === 'bool') {
    ctl = `<label class="chk"><input type="checkbox" ${cur ? 'checked' : ''}>
      <span>開啟</span></label>`;
  } else if (i.choices) {
    ctl = `<select>${i.choices.map(c =>
      `<option ${String(c) === String(cur) ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select>`;
  } else if (i.secret) {
    // 唯寫欄位。API 任何情況都不回明文，所以這裡永遠是空的 ——
    // 留空 = 不改，輸入新值 = 覆蓋。
    ctl = `<input type="password" autocomplete="new-password"
      value="${esc(dirty ? draft : '')}"
      placeholder="${i.isSet ? '已設定，留空表示不修改' : '尚未設定'}">`;
  } else {
    ctl = `<input type="${i.type === 'int' || i.type === 'float' ? 'number' : 'text'}"
      ${i.type === 'float' ? 'step="0.1"' : ''}
      ${i.min !== null && i.min !== undefined ? `min="${i.min}"` : ''}
      ${i.max !== null && i.max !== undefined ? `max="${i.max}"` : ''}
      value="${esc(cur)}">`;
  }

  const notes = [];
  if (locked) {
    // 一行就好。「要怎麼改」那段說明放在分類頂端講一次 ——
    // 同一句話在 29 列上各印一次，只是把真正的資訊（值是多少）擠掉。
    notes.push(`<div class="note lock">🔒 由 <code>.env</code> 決定</div>`);
  }
  if (i.shadowed) {
    notes.push(`<div class="note lock">⚠ 這裡曾經存過
      <b>${esc(String(i.candidates.db))}</b>，但被 <code>.env</code> 蓋掉了，所以沒有作用。</div>`);
  }
  if (err) notes.push(`<div class="note err">✕ ${esc(err)}</div>`);
  else if (i.error) notes.push(`<div class="note err">✕ ${esc(i.error)}</div>`);
  if (i.secret && i.isSet && !locked) {
    notes.push(`<div class="note">已設定　${i.updatedAt ? '最後更新 ' + when(i.updatedAt) : ''}</div>`);
  }

  const acts = [];
  if (!locked && !i.secret) {
    acts.push('<button data-do="default">預設值</button>');
    if (dirty) acts.push('<button data-do="revert">還原</button>');
  }
  if (i.shadowed) acts.push('<button data-do="clear">清除這裡存的值</button>');

  return `<div class="param${locked ? ' ro' : ''}${dirty ? ' dirty' : ''}${err ? ' bad' : ''}"
               data-key="${esc(i.key)}">
    <div class="meta">
      <label>${esc(i.label)}
        <span class="apply ${i.applyMode}">${APPLY_TEXT[i.applyMode]}</span>
        ${i.sensitive ? '<span class="apply reload">敏感</span>' : ''}</label>
      <code>${esc(i.key)}</code>
      ${i.help ? `<p>${esc(i.help)}</p>` : ''}
      <p class="src">目前來源：${
        { env: '.env / 環境變數', db: '這裡設定的', default: '程式預設值' }[i.effective.source]}</p>
    </div>
    <div class="ctl">${ctl}${notes.join('')}
      ${acts.length ? `<div class="acts">${acts.join('')}</div>` : ''}</div>
  </div>`;
}

/* ================================================================ 啟動 */
$('#menuBtn').onclick = () =>
  $('#side').classList.contains('open') ? closeDrawer() : openDrawer();
$('#scrim').onclick = closeDrawer;
$$('.sidenav button').forEach(b => b.onclick = () => show(b.dataset.tab));
window.addEventListener('hashchange', () => {
  const h = location.hash.slice(1);
  if (TABS[h] && h !== current) show(h);
});
window.show = show;

(async () => {
  try {
    const me = await api('/me');
    if (!me.is_admin) {
      document.body.innerHTML = '<div class="empty" style="margin-top:20vh">' +
        '這個頁面需要管理員權限。<br><br><a class="btn" href="/">回到媒體庫</a></div>';
      return;
    }
    if (me.pending_users) { $('#pendingDot').hidden = false; }
  } catch (e) { /* /me 掛了也讓頁面顯示，各分頁自己會報錯 */ }
  try {
    const d = await api('/params');
    $('#restartBar').hidden = !d.needsRestart.length;
  } catch (e) { /* 忽略 */ }
  const h = location.hash.slice(1);
  show(TABS[h] ? h : 'overview');
})();
