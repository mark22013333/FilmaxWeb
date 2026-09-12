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
 * 1. **從掃到的資料夾清單勾選，不讓人手打路徑。**手打就會拼錯，而拼錯的規則
 *    等於沒有保護 —— 畫面上還是會顯示「已設定」，沒有人會發現。
 * 2. **密碼登入沒有帳號身分**，所以無法被授權。這句話要印在畫面上，
 *    不然設定的人會以為「我設了但他還是看不到」是壞掉。
 * 3. 授權用 checkbox 一次送出整份名單（PUT grants），不是逐一 add/remove ——
 *    逐一送會有「加了三個、第四個失敗」的半套狀態。
 */
function aclBlock(ac) {
  // **管理員不放進勾選清單。**他們看得到受限資料夾是因為「角色」，
  // 不是因為這裡有一筆授權（acl.can_read 的第一句就是 is_admin）。
  //
  // 那為什麼不乾脆在升管理員時順手勾起來？因為降級的時候會出事：
  // 授權留著 → 他降回一般帳號之後仍然看得到，而畫面上一切正常
  // （這就是 L 章開頭寫的「安靜的外洩」）；若改成降級時自動刪，
  // 又會刪掉他升級**之前**本來就有的授權，而那救不回來。
  // 所以資料維持一種真相（角色歸角色、授權歸授權），改的是畫面：
  // 把管理員另外列出來，不要讓四個沒勾的框看起來像「四個人都看不到」。
  const admins = ac.users.filter(u => u.is_admin);
  const viewers = ac.users.filter(u => !u.is_admin);
  const adminNames = admins.map(u => esc(u.name)).join('、');
  const adminTail = admins.length
    ? ` <span style="color:var(--dim)">＋ 全部管理員（${admins.length} 人）</span>` : '';

  const rules = ac.rules.map(r => `
    <div class="rowcard acl-rule" data-rule="${r.id}">
      <div class="top"><b><code>${esc(r.prefix)}</code></b>
        <button class="btn" data-aclrm="${r.id}">移除限制</button></div>
      <dl><dt>備註</dt><dd>${esc(r.note || '—')}</dd>
        <dt>看得到的人</dt><dd>${r.user_names.length
          ? r.user_names.map(n => esc(n)).join('、') + adminTail
          : '<span style="color:#ffaeae">目前沒有人</span>' + adminTail}</dd>
        <dt>涵蓋範圍</dt><dd>${(() => {
          // 子資料夾是「自動跟著受限」的 —— 那句話寫在說明裡，但沒有人
          // 會去數到底跟了幾個。列出來才知道這條規則的實際影響有多大。
          const kids = ac.folders.filter(f => f.covered_by === r.prefix);
          if (!kids.length) return '<span style="color:var(--dim)">底下沒有子資料夾</span>';
          const show = kids.slice(0, 12);
          return `<span style="color:var(--dim)">連同底下 ${kids.length} 個子資料夾</span>
            <div class="acl-covers">${show.map(f =>
              `<code title="${esc(f.folder)}">${esc(f.folder.split('/').filter(Boolean).pop())}</code>`
            ).join('')}${kids.length > show.length
              ? `<code>…另外 ${kids.length - show.length} 個</code>` : ''}</div>`;
        })()}</dd></dl>
      <div class="acl-users">
        ${viewers.length ? viewers.map(u => `
          <label><input type="checkbox" data-aclu="${u.id}"
            ${r.user_ids.includes(u.id) ? 'checked' : ''}>
            <span>${esc(u.name)}</span></label>`).join('')
          : '<span style="color:var(--dim)">沒有可以授權的一般帳號。</span>'}
      </div>
      ${admins.length ? `<div style="font-size:11.5px;color:var(--dim);margin-top:6px">
        管理員一律看得到，不需要（也不能）在這裡勾選：${adminNames}
        ${(() => {
          // 先被授權、後來升管理員的人：那筆授權還在，而且降級後仍然有效。
          // 不講出來的話它就是一個看不見的狀態。
          const kept = admins.filter(u => r.user_ids.includes(u.id));
          return kept.length
            ? `<br>其中 ${kept.map(u => esc(u.name)).join('、')} 另外保有授權，降為一般帳號後仍看得到。`
            : '';
        })()}</div>` : ''}
      <div class="acts"><button class="btn primary" data-aclsave="${r.id}">儲存授權</button></div>
    </div>`).join('');

  // **全部列出來，不能選的也列** —— 原本只列可選的，於是「我明明有這個資料夾，
  // 為什麼清單裡找不到」變成一個沒有答案的問題。標成灰色並寫明原因（已受限／
  // 被上層涵蓋）比讓它消失有用。
  const free = ac.folders.filter(f => !f.restricted && !f.covered_by);
  const row = f => {
    const off = f.restricted || f.covered_by;
    const bits = [f.videos && `影片 ${f.videos}`, f.photos && `相片 ${f.photos}`,
                  f.documents && `文件 ${f.documents}`].filter(Boolean).join('　');
    // 只顯示最後一段，父層靠縮排線表達 —— 完整路徑在 title 裡，
    // 深層目錄的完整路徑很長，列出來會把數量擠掉。
    const leaf = f.folder === '/' ? '/' : f.folder.split('/').filter(Boolean).pop();
    const tag = f.restricted ? '<span class="ftree-tag lock">已受限</span>'
      : f.covered_by ? `<span class="ftree-tag">在 ${esc(f.covered_by)} 底下</span>` : '';
    const indent = Array.from({ length: Math.max(0, f.depth - 1) },
      () => '<span class="ftree-indent" style="width:11px"></span>').join('');
    return `<button type="button" class="ftree-row${off ? ' is-off' : ''}"
      data-folder="${esc(f.folder)}" data-path="${esc(f.folder.toLowerCase())}"
      title="${esc(f.folder)}"${off ? ' disabled' : ''}>
      ${indent}<span class="ftree-name">${esc(leaf)}</span>
      ${tag}<span class="ftree-meta">${bits}</span></button>`;
  };
  return `
    <div class="box">
      <div style="font-size:12.5px;color:var(--muted);line-height:1.7">
        <b>沒被列在這裡的資料夾，所有登入者都看得到。</b>
        列進來的只有被授權的帳號與管理員看得到，子資料夾自動跟著受限。<br>
        <b>勾選的意思是「即使不是管理員也看得到」</b> ——
        所以管理員不在勾選清單裡，而一個人從管理員降成一般帳號之後，
        沒被勾到的受限資料夾他就看不到了。要讓他降級後仍然看得到，
        趁現在先勾起來。<br>
        ${esc(ac.password_login_note)}
      </div>
      ${rules || '<div class="empty" style="margin-top:10px">目前沒有受限資料夾。</div>'}
      <div style="margin-top:14px">
        <div style="font-size:12.5px;color:var(--muted);margin-bottom:6px">
          從掃到的資料夾裡挑一個加入限制（數量已經把子資料夾累加進來）：</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <input id="aclFilter" placeholder="篩選資料夾…" style="flex:1;min-width:160px">
          <input id="aclNote" placeholder="備註（選填）" style="flex:1;min-width:140px">
        </div>
        <div class="ftree" id="aclTree">
          ${ac.folders.length ? ac.folders.map(row).join('')
            : '<div class="ftree-empty">還沒有掃到任何資料夾。</div>'}
          <div class="ftree-empty" id="aclNoHit" hidden>沒有符合的資料夾。</div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;align-items:center">
          <span id="aclPicked" style="flex:1;min-width:200px;font-size:12.5px;color:var(--dim)">
            尚未選擇（可選 ${free.length} 個）</span>
          <button class="btn" id="aclAdd" disabled>加入限制</button>
        </div>
        <div style="font-size:11.5px;color:var(--dim);margin-top:6px">
          只列到第 4 層。更深的資料夾請選它的上層 —— 子資料夾自動跟著受限，
          而限制一個葉目錄幾乎永遠不是你想做的事。
        </div>
      </div>
    </div>`;
}

function wireAcl(el, ac) {
  const tree = el.querySelector('#aclTree');
  const picked = el.querySelector('#aclPicked');
  const add = el.querySelector('#aclAdd');
  let chosen = '';

  // 篩選。**原本用 option.hidden，那個屬性只有 Firefox 認** ——
  // Chrome／Safari 完全忽略，所以篩選看起來是壞的（打字沒反應）。
  // 現在是自訂清單，用 style.display 藏，三家都一樣。
  //
  // 命中一個深層目錄時，它的父層也要留著 —— 只顯示命中的那一列，
  // 縮排就沒有參照物，看起來像浮在半空。
  const flt = el.querySelector('#aclFilter');
  const applyFilter = () => {
    const q = (flt ? flt.value : '').trim().toLowerCase();
    const rows = [...tree.querySelectorAll('.ftree-row')];
    let hit = 0;
    const keep = new Set();
    if (q) {
      for (const r of rows) {
        if (!r.dataset.path.includes(q)) continue;
        keep.add(r.dataset.folder);
        // 把祖先一路加進來
        const segs = r.dataset.folder.split('/').filter(Boolean);
        for (let i = 1; i < segs.length; i++) keep.add('/' + segs.slice(0, i).join('/'));
      }
    }
    for (const r of rows) {
      const show = !q || keep.has(r.dataset.folder);
      r.style.display = show ? '' : 'none';
      if (show && r.dataset.path.includes(q)) hit++;
    }
    const none = el.querySelector('#aclNoHit');
    if (none) none.hidden = !q || hit > 0;
  };
  if (flt) flt.oninput = applyFilter;

  // 選取：自己管 on 樣式與按鈕狀態。disabled 的列（已受限／被涵蓋）點不動。
  tree.onclick = e => {
    const row = e.target.closest('.ftree-row');
    if (!row || row.disabled) return;
    const same = chosen === row.dataset.folder;
    tree.querySelectorAll('.ftree-row.on').forEach(r => r.classList.remove('on'));
    chosen = same ? '' : row.dataset.folder;
    if (!same) row.classList.add('on');
    picked.textContent = chosen ? chosen : '尚未選擇';
    picked.style.color = chosen ? 'var(--text)' : 'var(--dim)';
    add.disabled = !chosen;
  };

  if (add) add.onclick = async () => {
    const prefix = chosen;
    if (!prefix) return toast('先選一個資料夾', true);
    if (!await confirmBox({
      title: '把這個資料夾設為受限', ok: '設為受限',
      body: `<code>${esc(prefix)}</code> 與它底下的子資料夾，
             之後只有<b>被授權的帳號</b>與管理員看得到。<br><br>
             設定完成的當下<b>還沒有人被授權</b> —— 記得接著勾選要開放給誰。`
    })) return;
    try {
      await api('/folders/acl', { method: 'POST',
        body: JSON.stringify({ prefix, note: el.querySelector('#aclNote').value }) });
      toast('已設為受限');
      render('library');
    } catch (e) { toast(e.message, true); }
  };

  el.querySelectorAll('[data-aclrm]').forEach(b => b.onclick = async () => {
    const wrap = b.closest('.acl-rule');
    const prefix = wrap.querySelector('code').textContent;
    if (!await confirmBox({ title: '移除限制', ok: '移除',
      body: `<code>${esc(prefix)}</code> 之後<b>所有登入者都看得到</b>。` })) return;
    try { await api('/folders/acl/' + b.dataset.aclrm, { method: 'DELETE' });
      toast('已移除限制'); render('library'); }
    catch (e) { toast(e.message, true); }
  });

  el.querySelectorAll('[data-aclsave]').forEach(b => b.onclick = async () => {
    const wrap = b.closest('.acl-rule');
    const ruleId = +b.dataset.aclsave;
    const ids = [...wrap.querySelectorAll('[data-aclu]:checked')].map(x => +x.dataset.aclu);
    // **管理員原本就有的授權要原封帶回去。**這個端點是「整份取代」，
    // 而管理員不在勾選清單裡（見 aclBlock 的說明）—— 不帶的話，
    // 一個「先被授權、後來升管理員」的人會在別人按一次儲存時
    // 被安靜地收回授權，然後在他降級的那天才發現。
    const adminIds = new Set((ac.users || []).filter(u => u.is_admin).map(u => u.id));
    const rule = (ac.rules || []).find(r => r.id === ruleId);
    const keep = (rule ? rule.user_ids : []).filter(i => adminIds.has(i));
    const all = [...new Set([...ids, ...keep])];
    try {
      await api(`/folders/acl/${ruleId}/grants`,
        { method: 'PUT', body: JSON.stringify({ user_ids: all }) });
      toast(ids.length ? `已授權 ${ids.length} 個帳號` : '已收回全部授權');
      render('library');
    } catch (e) { toast(e.message, true); }
  });
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
        <input id="benchId" type="number" placeholder="檔案 id（詳情頁看得到）"
          style="width:190px;padding:9px 11px;border-radius:9px;background:var(--bg-2);
          border:1px solid var(--line);min-height:40px">
        <button class="btn" id="bBench">開始實測</button>
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
  $('#bBench').onclick = async () => {
    const id = $('#benchId').value.trim();
    if (!id) return toast('要先填檔案 id', true);
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
    $('#bBench').disabled = false;
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

    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <input id="warmId" type="number" placeholder="檔案 id"
        style="width:150px;padding:9px 11px;border-radius:9px;background:var(--bg-2);
        border:1px solid var(--line);min-height:40px">
      <button class="btn" id="bWarmPick">預備這一支⋯</button>
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

  $('#bWarmPick').onclick = () => {
    const id = parseInt($('#warmId').value, 10);
    if (!id) return toast('先填檔案 id', true);
    warmPick(id);
  };

  box.onclick = e => {
    const ds = e.target.dataset || {};
    if (ds.warm) return warmPick(parseInt(ds.warm, 10));
    if (ds.clearfile) return clearOne(parseInt(ds.clearfile, 10));
  };

  pollWarm();
}

async function clearOne(fileId) {
  const f = (await api('/cache/survey?limit=2000')).files.find(x => x.file_id === fileId);
  if (!await confirmBox({
    title: `清除這一支的快取？`, ok: '清除', danger: true,
    body: `<b>${esc((f && f.filename) || ('file_id=' + fileId))}</b><br><br>
           會刪掉 ${f ? bytes(f.bytes) + '、' + f.segments + ' 段' : '它的所有分段'}，
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
