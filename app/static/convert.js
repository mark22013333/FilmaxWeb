/* 相片頁的「HIF 轉成 JPG」。後端與規格見 app/hifconvert.py、docs/規格/S-HIF轉JPG.md。
 *
 * 只有管理員、且 .env 開了 PHOTO_CONVERT_ENABLED 時 /api/me 才會帶 photo_convert，
 * 沒有的話這支完全不出現。真正的擋是後端的 convert_gate，這裡只是不放一顆按了會被拒絕的鈕。
 */
(() => {
  const esc = s => String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const $ = s => document.querySelector(s);
  let timer = null;

  const post = (url, body) => window.api(url, { method: 'POST', body: JSON.stringify(body || {}) });

  function modal(html) {
    $('#modal').innerHTML = `<div class="modal-body cv">${html}</div>`;
    window.showOverlay();
  }

  /* ---------- 資料夾頂端的提示列 ---------- */
  async function paintBar({ enabled, folder, onDone }) {
    const bar = $('#convBar');
    if (!bar) return;
    bar.hidden = true;
    if (!enabled || !folder) return;
    let d;
    try { d = await window.api('/api/photos/convert/info?folder=' + encodeURIComponent(folder)); }
    catch { return; }
    if (!d.enabled || !d.hif_count) return;
    bar.innerHTML = `<span>這個資料夾有 <b>${d.hif_count}</b> 張 HIF。瀏覽器看到的是自動轉出的預覽，
      原檔不變。</span><button class="btn" id="convAll">全部轉成 JPG</button>`;
    bar.hidden = false;
    $('#convAll').onclick = () => open({ folder, onDone });
  }

  /* ---------- 確認框 ---------- */
  async function open({ folder, ids, onDone }) {
    const what = ids && ids.length === 1 ? '這 1 張' : ids ? `選的 ${ids.length} 張` : '這個資料夾（這一層）的全部';
    modal(`<h2 style="margin-top:0">轉成 JPG</h2>
      <p>${what} HIF 會轉成 JPG：</p>
      <ul class="cv-list">
        <li>JPG 放在<b>同一個資料夾</b>，最高品質、全解析度，保留拍攝資訊。</li>
        <li>原檔依<b>拍攝日期</b>搬進同資料夾底下的 <code>YYYYMMDD原檔</code>，<b>不會刪除任何檔案</b>。</li>
        <li>已經有同名 JPG 的會跳過，不覆蓋。</li>
        <li>JPG 約為 HIF 的 3～4 倍大。</li>
      </ul>
      <div class="cv-probe" id="cvProbe">正在檢查來源帳號有沒有寫入權限…</div>
      <div class="cv-actions">
        <button class="btn" onclick="closeModal()">取消</button>
        <button class="btn primary" id="cvGo" disabled>開始轉換</button>
      </div>`);
    let r;
    try { r = await post('/api/photos/convert/probe', { folder }); }
    catch (e) { r = { ok: false, reason: e.message }; }
    const box = $('#cvProbe');
    if (!box) return;                                   // 檢查期間使用者關掉了
    if (!r.ok) {
      box.className = 'cv-probe bad';
      box.textContent = '不能轉換：' + r.reason;
      return;
    }
    box.className = 'cv-probe ok';
    box.textContent = '來源可以寫入。';
    const go = $('#cvGo');
    go.disabled = false;
    go.onclick = async () => {
      go.disabled = true;
      try { await post('/api/photos/convert', { folder, photo_ids: ids || null }); }
      catch (e) { box.className = 'cv-probe bad'; box.textContent = e.message; go.disabled = false; return; }
      progress(onDone);
    };
  }

  /* ---------- 進度與結果 ---------- */
  function progress(onDone) {
    clearInterval(timer);
    const tick = async () => {
      let s;
      try { s = await window.api('/api/photos/convert/status', { crossScope: true }); } catch { return; }
      const j = s.job;
      if (!j) return;
      if (s.running) {
        const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
        modal(`<h2 style="margin-top:0">轉換中…</h2>
          <div class="cv-bar"><i style="width:${pct}%"></i></div>
          <p>${j.done} / ${j.total}　${esc(j.current)}</p>
          <div class="cv-actions"><button class="btn" id="cvCancel">取消（做完目前這一張就停）</button></div>`);
        $('#cvCancel').onclick = () => post('/api/photos/convert/cancel');
        return;
      }
      clearInterval(timer);
      summary(j, onDone);
    };
    timer = setInterval(tick, 1000);
    tick();
  }

  function summary(j, onDone) {
    const c = j.counts;
    const rows = j.items.map(i => `<li><b>${esc(i.name)}</b>　${i.state === 'failed' ? '失敗' : '跳過'}：${esc(i.reason)}</li>`).join('');
    modal(`<h2 style="margin-top:0">${j.state === 'cancelled' ? '已取消' : '轉換完成'}</h2>
      <p>成功 <b>${c.archived}</b> 張，跳過 <b>${c.skipped}</b> 張，失敗 <b>${c.failed}</b> 張。</p>
      ${rows ? `<ul class="cv-list">${rows}</ul>` : ''}
      <p class="cv-note">原檔都還在，成功的已搬進各自的 <code>YYYYMMDD原檔</code> 資料夾。</p>
      <div class="cv-actions"><button class="btn primary" id="cvDone">完成</button></div>`);
    $('#cvDone').onclick = () => { window.closeModal(); onDone && onDone(); };
  }

  /* 從檢視器的側欄來：先關掉檢視器，不然對話框會被它蓋住。
     PhotoSwipe 在開場動畫還沒跑完時 close() 是空操作（HEIF 的預覽第一次要在伺服器
     轉檔，開場動畫會等它），所以要重試到它真的銷毀為止，而不是呼叫一次就當關了。 */
  async function openFromViewer({ folder, id }) {
    try {
      const v = await import('/static/photoview.js');
      for (let i = 0; i < 40 && v.isOpen(); i++) {
        v.closePhotos();
        await new Promise(r => setTimeout(r, 150));
      }
    } catch (e) { console.error('close viewer failed', e); }
    open({ folder, ids: [Number(id)], onDone: window.loadPhotos });
  }

  window.Convert = { paintBar, open, openFromViewer };
})();
