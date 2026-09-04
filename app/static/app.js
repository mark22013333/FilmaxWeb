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
  const pages = Math.ceil(d.total / d.page_size);
  $('#pager').innerHTML = pages <= 1 ? '' : `<div class="pager">
    <button class="btn" ${d.page <= 1 ? 'disabled' : ''} onclick="go(${d.page - 1})">上一頁</button>
    <span>${d.page} / ${pages}</span>
    <button class="btn" ${d.page >= pages ? 'disabled' : ''} onclick="go(${d.page + 1})">下一頁</button></div>`;
}
// 要等新內容渲染完才捲。先捲的話，loadLibrary 會把格線換成「載入中」，
// 頁面高度瞬間塌掉，瀏覽器把捲動位置夾到 0，接著算出來的目標也是錯的。
window.go = p => { state.page = p; return pageTo(loadLibrary, '#grid'); };

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
  const ov = $('#overlay'); ov.classList.add('show');
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
window.closeModal = () => $('#overlay').classList.remove('show');
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
  $('#overlay').classList.add('show');
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
  $('#overlay').classList.add('show');
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
const pstate = { folder: '', page: 1, sort: 'taken', items: [], total: 0, previewPx: 1920 };
const dstate = { folder: '', page: 1, sort: 'name', items: [], total: 0 };

const fmtBytes = b => !b ? '—'
  : b >= 1073741824 ? (b / 1073741824).toFixed(2) + ' GB'
  : b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' KB';

// 資料夾名稱只顯示最後一層，前面的路徑當提示就好
const shortFolder = f => (f || '').split('/').filter(Boolean).pop() || '/';

async function loadFolders() {
  let d;
  try { d = await api('/api/photos/folders'); } catch { return; }
  const bar = $('#folderBar');
  const all = `<div class="fchip ${pstate.folder ? '' : 'on'}" data-folder="">
      <span class="fc-n">全部</span></div>`;
  bar.innerHTML = all + d.items.map(f => `
    <div class="fchip ${pstate.folder === f.folder ? 'on' : ''}" data-folder="${esc(f.folder)}"
         title="${esc(f.folder)}">
      ${f.cover ? `<img loading="lazy" src="/api/image/${esc(f.cover)}" alt="">` : ''}
      <span><span class="fc-n">${esc(shortFolder(f.folder))}</span>
      <span class="fc-c"> ${f.c}</span></span>
    </div>`).join('');
  bar.querySelectorAll('[data-folder]').forEach(el => el.onclick = () => {
    pstate.folder = el.dataset.folder; pstate.page = 1; loadPhotos();
  });
}

async function loadPhotos() {
  const grid = $('#pgrid');
  grid.innerHTML = '<div class="loading">載入中…</div>';
  const p = new URLSearchParams({ page: pstate.page, page_size: 60, sort: pstate.sort });
  if (pstate.folder) p.set('folder', pstate.folder);
  if (state.q) p.set('q', state.q);
  let d;
  try { d = await api('/api/photos?' + p); }
  catch (e) { grid.innerHTML = `<div class="loading">載入失敗：${esc(e.message)}</div>`; return; }
  pstate.items = d.items; pstate.total = d.total;
  pstate.previewPx = d.preview_px || 1920;
  $('#photoCount').textContent = d.total ? `共 ${d.total} 張` : '';
  if (!d.items.length) {
    grid.innerHTML = `<div class="empty"><h3>沒有相片</h3>
      <p>把圖片放進 .env 的 LIBRARY_ROOTS 底下，再按「掃描媒體庫」即可。</p></div>`;
    $('#ppager').innerHTML = ''; return;
  }
  grid.innerHTML = d.items.map((x, i) => `
    <div class="ph" data-i="${i}" role="button" tabindex="0"
         aria-label="看大圖：${esc(x.filename)}" title="${esc(x.filename)}">
      <img loading="lazy" src="/api/photo/${x.id}/thumb.jpg" alt="${esc(x.filename)}"
           onerror="this.style.display='none'">
      <div class="ph-meta">${esc(x.filename)}</div>
    </div>`).join('');
  grid.querySelectorAll('.ph').forEach(el => {
    el.onclick = () => openPhoto(+el.dataset.i);
    el.onkeydown = e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPhoto(+el.dataset.i); }
    };
  });

  const pages = Math.ceil(d.total / d.page_size);
  $('#ppager').innerHTML = pages > 1 ? `
    <button class="btn" ${pstate.page <= 1 ? 'disabled' : ''} data-pp="-1">上一頁</button>
    <span style="padding:0 12px;color:var(--dim)">${pstate.page} / ${pages}</span>
    <button class="btn" ${pstate.page >= pages ? 'disabled' : ''} data-pp="1">下一頁</button>` : '';
  $('#ppager').querySelectorAll('[data-pp]').forEach(b => b.onclick = () => {
    pstate.page += +b.dataset.pp;
    pageTo(loadPhotos, '#pgrid');
  });
}

/* ------------------------- 文件牆（PDF） ------------------------- */
async function loadDocFolders() {
  let d;
  try { d = await api('/api/documents/folders'); } catch { return; }
  const bar = $('#docFolderBar');
  bar.innerHTML = `<div class="fchip ${dstate.folder ? '' : 'on'}" data-dfolder="">
      <span class="fc-n">全部</span></div>` + d.items.map(f => `
    <div class="fchip ${dstate.folder === f.folder ? 'on' : ''}" data-dfolder="${esc(f.folder)}"
         title="${esc(f.folder)}">
      <span><span class="fc-n">${esc(shortFolder(f.folder))}</span>
      <span class="fc-c"> ${f.c}</span></span>
    </div>`).join('');
  bar.querySelectorAll('[data-dfolder]').forEach(el => el.onclick = () => {
    dstate.folder = el.dataset.dfolder; dstate.page = 1; loadDocs();
  });
}

async function loadDocs() {
  const grid = $('#dgrid');
  grid.innerHTML = '<div class="loading">載入中…</div>';
  const p = new URLSearchParams({ page: dstate.page, page_size: 60, sort: dstate.sort });
  if (dstate.folder) p.set('folder', dstate.folder);
  if (state.q) p.set('q', state.q);
  let d;
  try { d = await api('/api/documents?' + p); }
  catch (e) { grid.innerHTML = `<div class="loading">載入失敗：${esc(e.message)}</div>`; return; }
  dstate.items = d.items; dstate.total = d.total;
  $('#docCount').textContent = d.total ? `共 ${d.total} 份` : '';
  if (!d.items.length) {
    grid.innerHTML = `<div class="empty"><h3>沒有文件</h3>
      <p>把 PDF 放進 .env 的 LIBRARY_ROOTS 底下，再按「掃描媒體庫」即可。</p></div>`;
    $('#dpager').innerHTML = ''; return;
  }
  // 用 <button> 而不是 <div>：鍵盤 Tab 與 Enter 直接就能用，不必自己補
  // role 與 keydown（相片牆那邊是後來才補上的）。
  grid.innerHTML = d.items.map(x => `
    <button class="doc" data-doc="${x.id}" title="${esc(x.folder + '/' + x.filename)}">
      <span class="di">PDF</span>
      <span class="dn"><b>${esc(x.filename)}</b><span>${esc(shortFolder(x.folder))}</span></span>
      <span class="ds">${fmtBytes(x.size)}</span>
    </button>`).join('');
  grid.querySelectorAll('[data-doc]').forEach(el => el.onclick = () => {
    location.href = '/reader?doc=' + el.dataset.doc;
  });

  const pages = Math.ceil(d.total / d.page_size);
  $('#dpager').innerHTML = pages > 1 ? `
    <button class="btn" ${dstate.page <= 1 ? 'disabled' : ''} data-dp="-1">上一頁</button>
    <span style="padding:0 12px;color:var(--dim)">${dstate.page} / ${pages}</span>
    <button class="btn" ${dstate.page >= pages ? 'disabled' : ''} data-dp="1">下一頁</button>` : '';
  $('#dpager').querySelectorAll('[data-dp]').forEach(b => b.onclick = () => {
    dstate.page += +b.dataset.dp;
    pageTo(loadDocs, '#dgrid');
  });
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
function showView(kind) {
  const photo = kind === 'photo', doc = kind === 'doc';
  const other = photo || doc;
  $('#photoView').hidden = !photo;
  $('#docView').hidden = !doc;
  for (const id of ['#grid', '#pager', '#libTitle', '#continue']) {
    const el = $(id); if (el) el.hidden = other;
  }
  $('.toolbar').hidden = other;           // 類型與排序是影片用的
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
    if (!$('#photoView').hidden) { pstate.page = 1; loadPhotos(); }
    else if (!$('#docView').hidden) { dstate.page = 1; loadDocs(); }
    else { state.page = 1; loadLibrary(); }
  }, 320);
};
$('#sort').onchange = e => { state.sort = e.target.value; state.page = 1; loadLibrary(); };

(async function init() {
  await loadMe();               // 先知道身分，才知道要不要畫出下載與管理按鈕
  await loadLibrary();
  loadGenres().catch(() => {});
  loadContinue().catch(() => {});
  const s = await api('/api/scan/status').catch(() => null);
  if (s && s.running) { $('#scanPanel').classList.add('show'); pollScan(); }
})();
