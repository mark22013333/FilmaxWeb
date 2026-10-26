/* 首次設定精靈。每一步只問一件事，填完立刻驗證；最後才一次寫進 .env。
 * 密碼不進 sessionStorage，也不會從伺服器回傳到這個頁面。 */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, body) {
  const r = await fetch('/api/setup' + path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ data: body }),
  });
  let j = null;
  try { j = await r.json(); } catch (e) { /* 非 JSON */ }
  if (!r.ok) {
    const e = new Error((j && (typeof j.detail === 'string' ? j.detail : '')) || `HTTP ${r.status}`);
    e.detail = j && j.detail;
    throw e;
  }
  return j;
}
function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 2600);
}

const STEPS = ['開始', '影片來源', '資料夾', '影片資料', '誰能看', '完成'];
let S = null;                   // /state
let step = 0;
const D = {                     // 草稿。password 類欄位只活在記憶體
  source: { protocol: 'ftp', host: '', port: '', user: '', password: '',
            tls: false, passive: true, encoding: 'utf-8' },
  sourceOk: false, roots: [], browsePath: '/',
  tmdbKey: '', tmdbOk: false, tmdbSkip: false,
  mode: '', adminPassword: '', viewerPassword: '', publicBaseUrl: '',
};

function saveDraft() {
  try {
    const { source, roots, mode, publicBaseUrl, sourceOk } = D;
    sessionStorage.setItem('filmax.setup', JSON.stringify({
      step, source: { ...source, password: '' }, roots, mode, publicBaseUrl, sourceOk: false }));
  } catch (e) { /* 無痕模式等，沒有草稿也能用 */ }
}
function loadDraft() {
  try {
    const d = JSON.parse(sessionStorage.getItem('filmax.setup') || 'null');
    if (!d) return;
    Object.assign(D.source, d.source || {});
    D.roots = d.roots || []; D.mode = d.mode || ''; D.publicBaseUrl = d.publicBaseUrl || '';
    step = Math.min(d.step || 0, 4);
  } catch (e) { /* 壞掉的草稿直接忽略 */ }
}

function prefillFromEnv() {
  const e = S.env;
  if (e.FTP_HOST) {
    Object.assign(D.source, {
      protocol: e.FTP_PROTOCOL || 'ftp', host: e.FTP_HOST, port: e.FTP_PORT || '',
      user: e.FTP_USER || '', tls: /^(1|true|yes|on)$/i.test(e.FTP_TLS || ''),
      passive: !/^(0|false|no|off)$/i.test(e.FTP_PASSIVE || 'true'),
      encoding: e.FTP_ENCODING || 'utf-8',
    });
  }
  if (e.LIBRARY_ROOTS && !D.roots.length) {
    D.roots = e.LIBRARY_ROOTS.split(';').filter(Boolean).map(c => {
      const [path, kind] = c.split('|'); return { path, kind: kind || 'auto' };
    });
  }
  if (!D.mode && e.AUTH_ENABLED) D.mode = /^(1|true|yes|on)$/i.test(e.AUTH_ENABLED) ? 'public' : 'private';
  if (!D.publicBaseUrl) D.publicBaseUrl = e.PUBLIC_BASE_URL || '';
}

function renderSteps() {
  $('#steps').innerHTML = STEPS.map((n, i) =>
    `<li class="${i === step ? 'on' : i < step ? 'done click' : ''}" data-i="${i}">${i < step ? '✓ ' : ''}${esc(n)}</li>`
  ).join('');
  $('#steps').querySelectorAll('.click').forEach(li => li.onclick = () => go(+li.dataset.i));
}
function go(i) { step = i; saveDraft(); paint(); window.scrollTo(0, 0); }
function paint() { renderSteps(); VIEWS[step](); }

const nav = (back, next, nextLabel = '下一步', nextOff = false) => `
  <div class="actions">
    ${back ? '<button class="btn big" id="bBack">上一步</button>' : '<span></span>'}
    ${next ? `<button class="btn primary big" id="bNext" ${nextOff ? 'disabled' : ''}>${nextLabel}</button>` : ''}
  </div>`;
function wireNav(back, next) {
  if ($('#bBack')) $('#bBack').onclick = back;
  if ($('#bNext')) $('#bNext').onclick = next;
}
const result = (r) => !r ? '' : `<div class="result ${r.cls}">${r.html}</div>`;

/* ---------------------------------------------------------------- 0 開始 */
function viewWelcome() {
  const f = S.ffmpeg, dep = S.deps;
  const row = (ok, text, fix) => `<div class="check"><span class="i">${ok ? '✅' : '⚠️'}</span>
    <span>${text}${!ok && fix ? `<br><small style="color:var(--muted)">${fix}</small>` : ''}</span></div>`;
  $('#card').innerHTML = `
    <h2>歡迎使用 FilmaxWeb</h2>
    <p class="lead">只要 5 個步驟，約 3 分鐘。每一步都會當場幫你驗證，填錯了會直接告訴你下一步怎麼辦。
      你不需要打開 <code>.env</code>。</p>
    <div class="box">
      ${row(f.ffmpeg.ok, f.ffmpeg.ok ? `ffmpeg：${esc(f.ffmpeg.version || '已安裝')}` : '找不到 ffmpeg',
        '雙擊資料夾裡的「安裝.bat」會自動裝好。沒有它，mkv／HEVC 這類瀏覽器直接播不了的影片無法轉碼，其他功能仍可設定。')}
      ${row(f.ffprobe.ok, f.ffprobe.ok ? 'ffprobe：已安裝' : '找不到 ffprobe', '它跟 ffmpeg 同一包，裝 ffmpeg 時會一起裝。')}
      ${row(dep.paramiko, dep.paramiko ? 'SFTP 套件：已安裝' : '缺 SFTP 套件 paramiko',
        '雙擊「安裝.bat」重新安裝套件；只用 FTP 的話可以忽略。')}
    </div>
    ${S.unknownEnvKeys.length ? `<div class="result warn">.env 裡有程式不認得的設定（等於沒作用）：
      ${S.unknownEnvKeys.map(u => `<code>${esc(u.key)}</code>${u.guess ? ` → 是不是想打 <code>${esc(u.guess)}</code>？` : ''}`).join('、')}</div>` : ''}
    ${S.needsSetup ? '' : `<div class="result warn">這台已經設定過了。精靈會顯示目前 <code>.env</code> 的內容，
      改完按「完成」才會覆蓋；你不動的欄位不會被改。</div>`}
    ${nav(false, true, '開始設定')}`;
  wireNav(null, () => go(1));
}

/* ---------------------------------------------------------------- 1 片源 */
let srcResult = null;
function viewSource() {
  const s = D.source, sftp = s.protocol === 'sftp';
  const pwSet = S.secretSet.FTP_PASSWORD && !s.password;
  $('#card').innerHTML = `
    <h2>你的影片放在哪裡？</h2>
    <p class="lead">FilmaxWeb 不搬動你的檔案，它透過網路協定去讀你放影片的那台機器。先選你那台是哪一種：</p>
    <div class="choices">
      <button class="choice ${!sftp ? 'on' : ''}" data-p="ftp"><b>NAS、路由器外接硬碟，或有開 FTP 的電腦</b>
        <span>協定 FTP（連接埠通常是 21）。Synology、QNAP、威聯通、FileZilla Server 都是這一種。</span></button>
      <button class="choice ${sftp ? 'on' : ''}" data-p="sftp"><b>一台 Windows／Linux 電腦（SSH）</b>
        <span>協定 SFTP（連接埠通常是 22）。Windows 要先在「選用功能」裡裝 OpenSSH 伺服器。</span></button>
    </div>
    <div class="field"><label for="fHost">主機位址</label>
      <input type="text" id="fHost" value="${esc(s.host)}" placeholder="例如 192.168.1.10，或同一台電腦就填 127.0.0.1" autocomplete="off">
      <div class="why">在放影片的那台機器上，開命令列輸入 <code>ipconfig</code>（Windows）或 <code>ip a</code>，找「IPv4 位址」。</div></div>
    <div class="wrow">
      <div class="field narrow"><label for="fPort">連接埠</label>
        <input type="number" id="fPort" value="${esc(s.port || (sftp ? 22 : 21))}" min="1" max="65535"></div>
      <div class="field"><label for="fUser">帳號</label>
        <input type="text" id="fUser" value="${esc(s.user)}" placeholder="${sftp ? 'Windows 帳號' : '匿名登入填 anonymous'}" autocomplete="off"></div>
    </div>
    <div class="field"><label for="fPass">密碼</label>
      <input type="password" id="fPass" value="${esc(s.password)}" autocomplete="new-password"
        placeholder="${pwSet ? '已設定，留空沿用現有的密碼' : (sftp ? 'Windows 帳號密碼（不是 PIN）' : '匿名登入就留空')}"></div>
    ${sftp ? '' : `<details class="adv"><summary>進階（連得上就不用看）</summary>
      <div class="field"><label><input type="checkbox" id="fTls" ${s.tls ? 'checked' : ''}> 使用 FTPS（加密的 FTP）</label>
        <div class="why">只有伺服器明確要求加密才勾。勾了卻連不上，就取消勾選。</div></div>
      <div class="field"><label><input type="checkbox" id="fPasv" ${s.passive ? 'checked' : ''}> 被動模式</label>
        <div class="why">幾乎都要開著。只有連得上、卻列不出目錄時才試著關。</div></div>
      <div class="field"><label for="fEnc">檔名編碼</label>
        <select id="fEnc">${['utf-8', 'big5', 'gbk', 'latin-1'].map(e =>
          `<option ${e === s.encoding ? 'selected' : ''}>${e}</option>`).join('')}</select>
        <div class="why">中文檔名在畫面上變亂碼時，換成 big5 或 gbk 再測。</div></div></details>`}
    <button class="btn big" id="bTest">測試連線</button>
    <div id="srcOut">${result(srcResult)}</div>
    ${nav(true, true, '下一步', !D.sourceOk)}`;

  document.querySelectorAll('.choice').forEach(b => b.onclick = () => {
    collectSource(); D.source.protocol = b.dataset.p;
    D.source.port = ''; D.sourceOk = false; srcResult = null; viewSource();
  });
  $('#card').oninput = () => { collectSource(); if (D.sourceOk) { D.sourceOk = false; $('#bNext').disabled = true; } };
  $('#bTest').onclick = testSource;
  wireNav(() => go(0), () => go(2));
}
function collectSource() {
  const g = id => $(id);
  if (!g('#fHost')) return;
  Object.assign(D.source, {
    host: g('#fHost').value.trim(), port: g('#fPort').value, user: g('#fUser').value.trim(),
    password: g('#fPass').value,
  });
  if (g('#fTls')) D.source.tls = g('#fTls').checked;
  if (g('#fPasv')) D.source.passive = g('#fPasv').checked;
  if (g('#fEnc')) D.source.encoding = g('#fEnc').value;
  saveDraft();
}
async function testSource() {
  collectSource();
  const out = $('#srcOut'), b = $('#bTest');
  b.disabled = true; out.innerHTML = result({ cls: '', html: '連線中⋯（最多等 10 秒）' });
  try {
    const r = await api('/test/source', D.source);
    D.sourceOk = !!r.ok;
    if (r.ok) {
      srcResult = { cls: 'ok', html: `✅ 連上了。${r.welcome ? esc(r.welcome) + '　' : ''}根目錄底下有
        <b>${(r.dirs || []).length}</b> 個資料夾${r.videos ? `、${r.videos} 部影片` : ''}。${
        r.warn ? `<span class="hint">${esc(r.warn)}</span>` : ''}` };
      D.browsePath = '/';
    } else {
      srcResult = { cls: 'bad', html: `❌ ${esc(r.message)}${r.hint ? `<span class="hint">${esc(r.hint)}</span>` : ''}` };
    }
  } catch (e) { srcResult = { cls: 'bad', html: `❌ ${esc(e.message)}` }; D.sourceOk = false; }
  out.innerHTML = result(srcResult);
  b.disabled = false;
  $('#bNext').disabled = !D.sourceOk;
}

/* ---------------------------------------------------------------- 2 資料夾 */
let browseState = { loading: false, dirs: [], videos: 0, err: null };
async function browseTo(path) {
  D.browsePath = path; browseState = { loading: true, dirs: [], videos: 0, err: null }; paintBrowse();
  try {
    const r = await api('/browse', { source: D.source, path });
    browseState = r.ok ? { loading: false, dirs: r.dirs, videos: r.videos, err: null }
                       : { loading: false, dirs: [], videos: 0, err: `${r.message}${r.hint ? '　' + r.hint : ''}` };
  } catch (e) { browseState = { loading: false, dirs: [], videos: 0, err: e.message }; }
  paintBrowse();
}
function viewRoots() {
  $('#card').innerHTML = `
    <h2>要收錄哪些資料夾？</h2>
    <p class="lead">點進去找到放影片的資料夾，按「收錄這個資料夾」。可以選好幾個；
      <b>不選就是整個根目錄都收</b>（資料夾多又雜時會慢，也會收到不想要的東西）。</p>
    <div id="browse"></div>
    <h3 style="margin:18px 0 6px;font-size:14px">已選擇</h3>
    <div class="picked" id="picked"></div>
    <div class="result" style="margin-top:14px">
      <b>類型怎麼選？</b>　「自動判斷」靠檔名認得電影或影集，大多數情況選它就好。
      整個資料夾都是影集（例如「韓劇」）才選影集，辨識會更準。</div>
    ${nav(true, true)}`;
  wireNav(() => go(1), () => go(3));
  paintPicked(); browseTo(D.browsePath || '/');
}
function paintBrowse() {
  const el = $('#browse'); if (!el) return;
  const parts = D.browsePath.split('/').filter(Boolean);
  const crumbs = ['<button data-p="/">根目錄</button>'].concat(parts.map((p, i) =>
    `<span>/</span><button data-p="/${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</button>`)).join('');
  const dirs = browseState.loading ? '<div class="wempty">讀取中⋯</div>'
    : browseState.err ? `<div class="wempty" style="color:#ffbdbd">${esc(browseState.err)}</div>`
    : browseState.dirs.length ? browseState.dirs.map(d =>
        `<button data-d="${esc(d)}">📁 ${esc(d)}</button>`).join('')
    : '<div class="wempty">這裡沒有子資料夾。</div>';
  el.innerHTML = `<div class="wcrumbs">${crumbs}</div>
    <div class="dirs">${dirs}</div>
    <div style="margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="btn primary" id="bAddRoot">收錄這個資料夾</button>
      <small style="color:var(--muted)"><code>${esc(D.browsePath)}</code>${browseState.videos ? `　這一層有 ${browseState.videos} 部影片` : ''}</small></div>`;
  el.querySelectorAll('.wcrumbs [data-p]').forEach(b => b.onclick = () => browseTo(b.dataset.p));
  el.querySelectorAll('.dirs [data-d]').forEach(b => b.onclick = () =>
    browseTo((D.browsePath === '/' ? '' : D.browsePath) + '/' + b.dataset.d));
  $('#bAddRoot').onclick = () => {
    if (!D.roots.some(r => r.path === D.browsePath)) D.roots.push({ path: D.browsePath, kind: 'auto' });
    saveDraft(); paintPicked();
  };
}
function paintPicked() {
  const el = $('#picked'); if (!el) return;
  el.innerHTML = D.roots.length ? D.roots.map((r, i) => `
    <div class="pick"><code>${esc(r.path)}</code>
      <select data-i="${i}" aria-label="類型">${[['auto', '自動判斷'], ['movie', '電影'], ['tv', '影集']].map(([v, n]) =>
        `<option value="${v}" ${r.kind === v ? 'selected' : ''}>${n}</option>`).join('')}</select>
      <button data-x="${i}" aria-label="移除">✕</button></div>`).join('')
    : '<div class="result">還沒選。不選的話會收錄整個根目錄 <code>/</code>。</div>';
  el.querySelectorAll('select').forEach(s => s.onchange = () => { D.roots[+s.dataset.i].kind = s.value; saveDraft(); });
  el.querySelectorAll('[data-x]').forEach(b => b.onclick = () => { D.roots.splice(+b.dataset.x, 1); saveDraft(); paintPicked(); });
}

/* ---------------------------------------------------------------- 3 TMDB */
let tmdbResult = null;
function viewTmdb() {
  const has = S.secretSet.TMDB_API_KEY;
  $('#card').innerHTML = `
    <h2>海報與簡介從哪來？</h2>
    <p class="lead">FilmaxWeb 用 <b>TMDB</b>（免費的電影資料庫）幫你的影片配上海報、簡介和演員。
      不設也能看片，只是畫面上沒有封面。</p>
    ${has ? '<div class="result ok">✅ 已經設定過金鑰了。不想換就直接按下一步。</div>' : ''}
    <ol class="how">
      <li>到 <a href="https://www.themoviedb.org/signup" target="_blank" rel="noopener"><u>themoviedb.org 註冊</u></a>（免費）並驗證信箱。</li>
      <li>登入後開 <a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noopener"><u>設定 → API</u></a>，申請類型選「Developer」，用途填「個人使用」。</li>
      <li>複製頁面上的 <b>API 金鑰</b>（32 個英數字，<u>不是</u>很長、eyJ 開頭的那串權杖），貼到下面。</li>
    </ol>
    <div class="field"><label for="tKey">TMDB API 金鑰</label>
      <input type="password" id="tKey" value="${esc(D.tmdbKey)}" autocomplete="off" placeholder="${has ? '已設定，留空沿用' : '貼上 32 個英數字'}"></div>
    <button class="btn big" id="bTmdb">驗證金鑰</button>
    <div id="tOut">${result(tmdbResult)}</div>
    <div class="actions"><button class="btn big" id="bBack">上一步</button>
      <span class="grow"></span>
      <button class="btn big" id="bSkip">${has || D.tmdbOk ? '' : '先跳過'}</button>
      <button class="btn primary big" id="bNext">下一步</button></div>`;
  if (has || D.tmdbOk) $('#bSkip').hidden = true;
  $('#tKey').oninput = e => { D.tmdbKey = e.target.value.trim(); D.tmdbOk = false; tmdbResult = null; $('#tOut').innerHTML = ''; };
  $('#bTmdb').onclick = async () => {
    const b = $('#bTmdb'); b.disabled = true;
    $('#tOut').innerHTML = result({ cls: '', html: '驗證中⋯' });
    try {
      const r = await api('/test/tmdb', { key: D.tmdbKey });
      D.tmdbOk = !!r.ok;
      tmdbResult = r.ok ? { cls: 'ok', html: '✅ 金鑰有效。' }
        : { cls: 'bad', html: `❌ ${esc(r.message)}${r.hint ? `<span class="hint">${esc(r.hint)}</span>` : ''}` };
    } catch (e) { tmdbResult = { cls: 'bad', html: `❌ ${esc(e.message)}` }; }
    $('#tOut').innerHTML = result(tmdbResult); b.disabled = false;
  };
  $('#bSkip').onclick = () => { D.tmdbKey = ''; D.tmdbSkip = true; go(4); };
  wireNav(() => go(2), () => {
    if (D.tmdbKey && !D.tmdbOk) { toast('這把金鑰還沒驗證通過，先按「驗證金鑰」，或清空後跳過'); return; }
    go(4);
  });
}

/* ---------------------------------------------------------------- 4 誰能看 */
function genPassword() {
  const a = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const v = crypto.getRandomValues(new Uint32Array(14));
  return Array.from(v, n => a[n % a.length]).join('');
}
function viewAccess() {
  const pub = D.mode === 'public';
  $('#card').innerHTML = `
    <h2>誰會來看？</h2>
    <p class="lead">這決定要不要設密碼。選錯了也沒關係，之後隨時可以改。</p>
    <div class="choices">
      <button class="choice ${D.mode === 'private' ? 'on' : ''}" data-m="private"><b>只有家裡／公司內網的人</b>
        <span>不用密碼。程式會自動擋掉來自外部網路的連線，所以就算誤接了外網也不會公開。</span></button>
      <button class="choice ${pub ? 'on' : ''}" data-m="public"><b>我要在外面也能看（或要分享給別人）</b>
        <span>要設密碼。可以另外設一組「唯讀密碼」給朋友：他們只能看片，不能下載原檔或動設定。</span></button>
    </div>
    ${pub ? `
    <div class="field"><label for="aPass">管理員密碼</label>
      <div class="wrow"><input type="password" id="aPass" value="${esc(D.adminPassword)}" autocomplete="new-password" placeholder="至少 8 個字元">
        <button class="btn narrow" id="bGen" type="button">幫我產生</button></div>
      <div class="why">這組有完整權限，請自己記好。<span id="genShow"></span></div></div>
    <div class="field"><label for="vPass">唯讀密碼（可不填）</label>
      <input type="password" id="vPass" value="${esc(D.viewerPassword)}" autocomplete="new-password" placeholder="要分享給別人看時再設">
      <div class="why">要跟管理員密碼不一樣。</div></div>
    <div class="field"><label for="aUrl">對外網址（可不填）</label>
      <input type="text" id="aUrl" value="${esc(D.publicBaseUrl)}" placeholder="例如 https://video.example.com">
      <div class="why">只有你用 Cloudflare Tunnel 或網域對外時才有。填了 https 開頭的，程式會自動把登入 cookie 設成「只走加密連線」。
        還沒有對外網址？<code>cloudflare</code> 資料夾裡有一鍵設定的腳本，之後再來做也行。</div></div>
    <div class="result warn">Google 帳號登入、MSSQL 資料庫這些進階功能不在精靈裡 ——
      等基本設定完成後，到「後台 → 系統參數」或 README 的「登入與權限」處理。</div>` : ''}
    ${nav(true, true)}`;
  document.querySelectorAll('.choice').forEach(b => b.onclick = () => { collectAccess(); D.mode = b.dataset.m; saveDraft(); viewAccess(); });
  if ($('#bGen')) $('#bGen').onclick = () => {
    const p = genPassword(); $('#aPass').type = 'text'; $('#aPass').value = p; D.adminPassword = p;
    $('#genShow').innerHTML = ' 已填入上面，<b>請現在就抄下來</b>。';
  };
  $('#card').oninput = collectAccess;
  wireNav(() => go(3), () => {
    collectAccess();
    if (!D.mode) { toast('請先選一個'); return; }
    if (D.mode === 'public') {
      if (D.adminPassword.length < 8) { toast('管理員密碼至少 8 個字元'); return; }
      if (D.viewerPassword && D.viewerPassword === D.adminPassword) { toast('兩組密碼一定要不一樣'); return; }
    }
    go(5);
  });
}
function collectAccess() {
  if ($('#aPass')) D.adminPassword = $('#aPass').value;
  if ($('#vPass')) D.viewerPassword = $('#vPass').value;
  if ($('#aUrl')) D.publicBaseUrl = $('#aUrl').value.trim().replace(/\/+$/, '');
}

/* ---------------------------------------------------------------- 5 完成 */
function buildForm() {
  const form = {
    source: { ...D.source, passive: D.source.passive },
    roots: D.roots,
    access: D.mode === 'public'
      ? { mode: 'public', adminPassword: D.adminPassword, viewerPassword: D.viewerPassword, publicBaseUrl: D.publicBaseUrl }
      : { mode: 'private' },
  };
  if (D.tmdbKey) form.tmdbKey = D.tmdbKey;
  return form;
}
function viewFinish() {
  const s = D.source, row = (k, v) => `<tr><td>${k}</td><td>${v}</td></tr>`;
  $('#card').innerHTML = `
    <h2>確認並套用</h2>
    <p class="lead">按下「套用」會把這些寫進 <code>.env</code>（舊檔會先備份成 <code>.env.bak</code>），然後重新啟動服務。</p>
    <table class="sum">
      ${row('影片來源', `${esc(s.protocol.toUpperCase())}　${esc(s.user || '匿名')}@${esc(s.host)}:${esc(s.port || (s.protocol === 'sftp' ? 22 : 21))}`)}
      ${row('收錄資料夾', D.roots.length ? D.roots.map(r => `<code>${esc(r.path)}</code>`).join(' ') : '<code>/</code>（整個根目錄）')}
      ${row('海報與簡介', D.tmdbKey ? '已設定 TMDB' : (S.secretSet.TMDB_API_KEY ? '沿用現有金鑰' : '未設定（之後可在後台補）'))}
      ${row('誰能看', D.mode === 'public' ? `需要密碼登入${D.viewerPassword ? '（另有唯讀密碼）' : ''}${D.publicBaseUrl ? `<br><code>${esc(D.publicBaseUrl)}</code>` : ''}` : '只限內網，不用密碼')}
    </table>
    ${D.mode === 'public' ? `<div class="result warn">⚠ 套用後<b>每個人都要輸入密碼</b>，包括你自己。
      管理員密碼忘了的話，直接打開 <code>.env</code> 改 <code>AUTH_PASSWORD</code> 就能救回來。</div>` : ''}
    <div id="fOut"></div>
    ${nav(true, true, '套用並重新啟動')}`;
  wireNav(() => go(4), apply);
}
async function apply() {
  const b = $('#bNext'), out = $('#fOut'); b.disabled = true;
  try {
    const r = await api('/save', buildForm());
    try { sessionStorage.removeItem('filmax.setup'); } catch (e) { /* */ }
    if (!r.changed.length) {
      out.innerHTML = result({ cls: 'ok', html: '✅ 內容跟 <code>.env</code> 現在寫的一樣，沒有需要改的。' }); return;
    }
    if (S.restartSupported) {
      out.innerHTML = result({ cls: 'ok', html: '✅ 已寫入。正在重新啟動服務⋯（會彈出一個新的黑色視窗，不要關它）' });
      await api('/restart', {});
      await waitBack(r.authWillBeEnabled);
    } else {
      out.innerHTML = result({ cls: 'ok', html: `✅ 已寫入 <code>.env</code>。請手動重新啟動服務，然後
        <a href="/"><u>回到首頁</u></a>。` });
    }
  } catch (e) {
    b.disabled = false;
    const d = e.detail;
    out.innerHTML = result({ cls: 'bad', html: '❌ ' + (d && typeof d === 'object'
      ? '有欄位不合法，什麼都沒寫：<br>' + Object.entries(d).map(([k, v]) => `${esc(k)}：${esc(v)}`).join('<br>')
      : esc(e.message)) });
  }
}
async function waitBack(login) {
  const out = $('#fOut');
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 1500));
    try {
      const r = await fetch('/healthz', { cache: 'no-store' });
      if (r.ok) {
        out.innerHTML = result({ cls: 'ok', html: `✅ 服務已重新啟動。${login ? '請用剛設的密碼登入。' : ''}
          <a href="${login ? '/login' : '/admin'}"><u>${login ? '前往登入' : '前往後台，開始第一次掃描'}</u></a>` });
        setTimeout(() => { location.href = login ? '/login' : '/admin'; }, 2500);
        return;
      }
    } catch (e) { /* 還沒起來 */ }
  }
  out.innerHTML = result({ cls: 'warn', html: '等了很久服務還沒回來。看一下新開的黑色視窗有沒有錯誤訊息，或手動雙擊「啟動.bat」。' });
}

const VIEWS = [viewWelcome, viewSource, viewRoots, viewTmdb, viewAccess, viewFinish];

(async () => {
  try {
    S = await api('/state');
  } catch (e) {
    $('#card').innerHTML = `<h2>無法開啟設定精靈</h2><div class="result bad">${esc(e.message)}</div>
      <p><a class="btn" href="/">回到首頁</a></p>`;
    return;
  }
  loadDraft(); prefillFromEnv();
  if (step > 1 && !D.sourceOk) { D.sourceOk = false; }   // 草稿不帶測試結果，回來要重測
  paint();
})();
