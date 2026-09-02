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
    try { msg = (await r.json()).detail || msg; } catch (e) { /* 不是 JSON 就用預設訊息 */ }
    throw new Error(msg);
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
function confirmBox({ title, body, ok = '確定', danger = false }) {
  return new Promise(resolve => {
    const m = $('#modal');
    m.innerHTML = `<div class="sheet" role="dialog" aria-modal="true">
      <h3>${esc(title)}</h3><p>${body}</p>
      <div class="row"><button class="btn" data-no>取消</button>
      <button class="btn ${danger ? 'danger' : 'primary'}" data-yes>${esc(ok)}</button></div></div>`;
    m.hidden = false;
    const done = v => { m.hidden = true; m.innerHTML = ''; document.removeEventListener('keydown', key); resolve(v); };
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
  if (c.unscraped) warn.push(`${c.unscraped} 個條目還沒刮到資料`);
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
      <div class="stat ${c.unscraped ? 'warn' : ''}"><b>${c.unscraped}</b><small>未刮削</small></div>
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

/* ================================================================ 媒體庫 */
TABS.library = async el => {
  const [st, pr] = await Promise.all([api('/scan/status'), api('/admin/problems')]);
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

    <h3 class="sub">FTP 目錄</h3>
    <div class="box">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
        <button class="btn" id="ftpUp">上一層</button>
        <code id="ftpPath" style="color:var(--muted);overflow-wrap:anywhere">/</code>
      </div>
      <div id="ftpList"><div class="empty">按「上一層」或下面的資料夾開始瀏覽。</div></div>
    </div>

    <h3 class="sub">還沒刮到資料（${pr.unscraped.length}）</h3>
    ${pr.unscraped.length ? `<div class="scrollx"><table class="t">
      <thead><tr><th>標題</th><th>類型</th><th>年份</th><th>狀態</th></tr></thead><tbody>
      ${pr.unscraped.map(r => `<tr><td>${esc(r.title)}</td><td>${esc(r.kind)}</td>
        <td>${r.year || '—'}</td><td>${esc(r.scrape_state)}</td></tr>`).join('')}
      </tbody></table></div>
      <div class="rowcards">${pr.unscraped.map(r => `<div class="rowcard">
        <div class="top"><b>${esc(r.title)}</b><span class="st">${esc(r.scrape_state)}</span></div>
        <dl><dt>類型</dt><dd>${esc(r.kind)}</dd><dt>年份</dt><dd>${r.year || '—'}</dd></dl>
        </div>`).join('')}</div>`
      : '<div class="empty">全部都刮到了。</div>'}

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
      : '<div class="empty">沒有失敗的檔案。</div>'}`;

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
    if (await confirmBox({
      title: '完整重掃', ok: '開始重掃',
      body: '會重新刮削<b>所有</b>條目、重新分析<b>所有</b>檔案。既有的手動 TMDB 配對可能被覆蓋。'
    })) run('?full=true', '完整重掃');
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
    const id = e.target.dataset && e.target.dataset.probe;
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
        <td>${esc(u.note || '')}</td>
        <td><div style="display:flex;gap:5px;flex-wrap:wrap">${acts(u)}</div></td></tr>`).join('')}
      </tbody></table></div>
      <div class="rowcards">${d.items.map(u => `<div class="rowcard">
        <div class="top"><b>${esc(u.display_name || u.email)}</b>
          <span class="st ${esc(u.status)}">${esc(u.status)}</span></div>
        <dl><dt>帳號</dt><dd>${esc(u.email)}</dd>
        <dt>角色</dt><dd>${u.role === 'owner' ? '管理員' : '唯讀'}</dd>
        <dt>最後登入</dt><dd>${when(u.last_login_at)}　${esc(u.last_login_ip || '')}</dd>
        <dt>備註</dt><dd>${esc(u.note || '—')}</dd></dl>
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
    <div class="box">
      <button class="btn" id="bCache">清空轉碼快取</button>
      <span style="color:var(--muted);margin-left:10px;font-size:12.5px">
        清掉之後第一次播放會重新轉碼，不影響片源。</span>
    </div>`;

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
  $('#bCache').onclick = async () => {
    if (!await confirmBox({ title: '清空轉碼快取', ok: '清空',
      body: '所有已轉好的分段會被刪掉，下次播放要重轉。<b>不影響 FTP 上的片源。</b>' })) return;
    try { await api('/cache/clear', { method: 'POST' }); toast('已清空'); }
    catch (e) { toast(e.message, true); }
  };
};

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
const APPLY_TEXT = { hot: '即時生效', reload: '存檔後套用', restart: '需重啟' };
let paramDraft = {};        // key → 使用者改到一半、還沒存的值

TABS.system = async el => {
  const [d, diag] = await Promise.all([api('/params'), api('/diagnostics')]);
  const bySec = {};
  d.items.forEach(i => (bySec[i.section] = bySec[i.section] || []).push(i));

  el.innerHTML = `
    <h2 class="sec">系統</h2>
    <p class="secsub">資料庫狀態、外部相依、以及所有可以在這裡調整的參數。</p>

    ${d.unknownEnvKeys.length ? `<div class="banner">
      <b>.env 裡有程式不認得的設定</b>（等於完全沒有作用）：<br>
      ${d.unknownEnvKeys.map(u => `<code>${esc(u.key)}</code>` +
        (u.guess ? ` → 是不是想打 <code>${esc(u.guess)}</code>？` : '')).join('<br>')}</div>` : ''}
    ${d.drift.length ? `<div class="banner">
      <b>有 ${d.drift.length} 項在這裡存過的設定被環境變數蓋掉了</b>，所以不會生效。
      下面那幾項會標出來，可以選擇清掉這裡存的值。</div>` : ''}
    ${d.needsRestart.length ? `<div class="banner">
      <b>這些設定已經存好，但要重開服務才生效</b>：
      ${d.needsRestart.map(k => `<code>${esc(k)}</code>`).join(' ')}</div>` : ''}

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
    </div>

    <h3 class="sub">系統參數</h3>
    <div class="box" style="font-size:12.5px;color:var(--muted)">
      解析順序是 <b>.env / 環境變數　&gt;　這裡存的值　&gt;　程式預設值</b>，
      沒有例外。被 <code>.env</code> 決定的項目在這裡會被鎖住並標示出來 ——
      這樣「存了卻沒生效」就不會是個看不見的問題。<br>
      連線資訊、綁定位址、密碼這類改錯了會讓你進不來的設定，
      刻意<b>只能</b>在 <code>.env</code> 改，這裡完全不會出現。
    </div>
    <div id="paramList"></div>`;

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

  const list = $('#paramList');
  list.innerHTML = d.sections.map(sec => `
    <h3 class="sub">${esc(sec)}</h3>
    <div class="box">${bySec[sec].map(paramRow).join('')}</div>`).join('');

  list.addEventListener('input', e => {
    const wrap = e.target.closest('.param');
    if (!wrap) return;
    const key = wrap.dataset.key;
    paramDraft[key] = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    wrap.classList.add('dirty');
  });

  list.addEventListener('click', async e => {
    const b = e.target.closest('button[data-do]');
    if (!b) return;
    const wrap = b.closest('.param');
    const key = wrap.dataset.key;
    const item = d.items.find(i => i.key === key);
    const inp = wrap.querySelector('input,select');

    if (b.dataset.do === 'save') return saveParam(key, item, inp, wrap);
    if (b.dataset.do === 'default') {
      setInput(inp, item.candidates.default, item);
      paramDraft[key] = item.candidates.default;
      wrap.classList.add('dirty');
    }
    if (b.dataset.do === 'revert') {
      const v = item.candidates.db !== null && item.candidates.db !== undefined
        ? item.candidates.db : item.candidates.default;
      setInput(inp, v, item);
      delete paramDraft[key];
      wrap.classList.remove('dirty');
    }
    if (b.dataset.do === 'clear') {
      if (!await confirmBox({ title: '清除這裡存的值', ok: '清除',
        body: `<code>${esc(key)}</code> 在這裡存的值會被刪掉，之後就完全由
               <code>.env</code>（或程式預設值）決定。` })) return;
      try { await api('/params/' + key, { method: 'DELETE' }); delete paramDraft[key];
        toast('已清除'); render('system'); }
      catch (err) { toast(err.message, true); }
    }
  });
};

function setInput(inp, v, item) {
  if (!inp) return;
  if (item.type === 'bool') inp.checked = !!v;
  else inp.value = v === null || v === undefined ? '' : v;
}

async function saveParam(key, item, inp, wrap) {
  let v = paramDraft[key];
  if (v === undefined) v = item.type === 'bool' ? inp.checked : inp.value;

  // 改到會把自己鎖在外面的東西之前，要先講清楚，而且要給救援指令。
  if (item.lockout) {
    if (!await confirmBox({
      title: '這一項可能讓你進不了後台', ok: '我知道，還是要改', danger: true,
      body: `<code>${esc(key)}</code> 改錯的話，你可能沒辦法再登入或連到這個後台。<br><br>
             真的進不來的時候，在伺服器上執行：<br>
             <code>python -m app.paramstore unset ${esc(key)}</code><br><br>
             或直接編輯 <code>.env</code> —— 它的優先度比這裡高。`
    })) return;
  }
  try {
    const r = await api('/params/' + key, { method: 'PUT', body: JSON.stringify({ value: v }) });
    delete paramDraft[key];
    wrap.classList.remove('dirty');
    if (r.locked) {
      toast('存起來了，但這一項由環境變數決定，所以不會生效', true);
    } else {
      toast({ hot: '已生效', reload: '已套用', restart: '已存好，重開服務才會生效' }[r.applyMode]);
    }
    render('system');
  } catch (e) {
    toast(e.message, true);
  }
}

function paramRow(i) {
  const locked = i.locked;
  const dis = locked ? 'disabled' : '';
  const val = i.secret ? '' : (i.effective.value ?? '');
  let ctl;
  if (i.type === 'bool') {
    ctl = `<label style="display:flex;gap:9px;align-items:center;min-height:40px">
      <input type="checkbox" style="width:18px;height:18px" ${i.effective.value ? 'checked' : ''} ${dis}>
      <span style="color:var(--muted);font-size:12.5px">開啟</span></label>`;
  } else if (i.choices) {
    ctl = `<select ${dis}>${i.choices.map(c =>
      `<option ${String(c) === String(val) ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select>`;
  } else if (i.secret) {
    // 唯寫欄位。API 任何情況都不回明文，所以這裡永遠是空的 ——
    // 留空 = 不改，輸入新值 = 覆蓋。
    ctl = `<input type="password" autocomplete="new-password" ${dis}
      placeholder="${i.isSet ? '已設定，留空表示不修改' : '尚未設定'}">`;
  } else {
    ctl = `<input type="${i.type === 'int' || i.type === 'float' ? 'number' : 'text'}"
      ${i.type === 'float' ? 'step="0.1"' : ''}
      ${i.min !== null && i.min !== undefined ? `min="${i.min}"` : ''}
      ${i.max !== null && i.max !== undefined ? `max="${i.max}"` : ''}
      value="${esc(val)}" ${dis}>`;
  }

  const notes = [];
  if (locked) {
    notes.push(`<div class="note lock">🔒 這一項由環境變數
      <code>${esc(i.key)}</code> 設定，不能在這裡修改。</div>`);
  }
  if (i.shadowed) {
    notes.push(`<div class="note lock">⚠ 這裡曾經存過
      <b>${esc(String(i.candidates.db))}</b>，但被環境變數蓋掉了，所以沒有作用。</div>`);
  }
  if (i.error) notes.push(`<div class="note err">✕ ${esc(i.error)}</div>`);
  if (i.secret && i.isSet) {
    notes.push(`<div class="note">已設定　${i.updatedAt ? '最後更新 ' + when(i.updatedAt) : ''}</div>`);
  }

  const acts = [];
  if (!locked) acts.push('<button class="btn primary" data-do="save">儲存</button>');
  if (!locked && !i.secret) {
    acts.push('<button data-do="default">重設為預設值</button>');
    acts.push('<button data-do="revert">重設為上次儲存值</button>');
  }
  if (i.shadowed) acts.push('<button data-do="clear">清除這裡存的值</button>');

  return `<div class="param" data-key="${esc(i.key)}">
    <div class="meta">
      <label>${esc(i.label)}
        <span class="apply ${i.applyMode}">${APPLY_TEXT[i.applyMode]}</span>
        ${i.sensitive ? '<span class="apply reload">敏感</span>' : ''}</label>
      <code>${esc(i.key)}</code>
      ${i.help ? `<p>${esc(i.help)}</p>` : ''}
      <p style="color:var(--dim)">目前來源：${
        { env: '環境變數', db: '這裡設定的', default: '程式預設值' }[i.effective.source]}</p>
    </div>
    <div class="ctl">${ctl}${notes.join('')}
      <div class="acts">${acts.join('')}</div></div>
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
