'use strict';

/* ══ Tab switching (FIXED: uses .tab-panel class) ══ */
function switchTab(name) {
  document.querySelectorAll('.nav-btn[data-tab]').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p =>
    p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'dashboard') { connectWS(); refreshDash(); }
  if (name === 'history')   loadHistory(1);
  if (name === 'monitor')   loadMonitor();
  if (name === 'settings')  loadSettings();
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.nav-btn[data-tab]').forEach(btn =>
    btn.addEventListener('click', () => switchTab(btn.dataset.tab)));
  switchTab('transfer');

  const dubMix = document.getElementById('dub-mix');
  if (dubMix) dubMix.addEventListener('input', () =>
    document.getElementById('dub-mix-val').textContent = dubMix.value + '%');

  const shortsDur = document.getElementById('shorts-duration');
  if (shortsDur) shortsDur.addEventListener('input', () =>
    document.getElementById('shorts-dur-val').textContent = shortsDur.value + 'с');

  const monIn = document.getElementById('monitor-input');
  if (monIn) monIn.addEventListener('keydown', e => { if (e.key === 'Enter') addMonitorChannel(); });

  const hs = document.getElementById('history-search');
  if (hs) { let t; hs.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => loadHistory(1), 400); }); }
});

/* ══ Transfer ══ */
let currentJob = null;
let pollTimer  = null;

function onUrlInput() {
  const url = document.getElementById('yt-url').value.trim();
  if (isYT(url)) scheduleFetchPreview(url);
  else hidePreview();
}

function onDrop(e) {
  e.preventDefault();
  e.currentTarget.classList.remove('drag-over');
  const txt = e.dataTransfer.getData('text');
  if (txt) { document.getElementById('yt-url').value = txt; onUrlInput(); }
}

function pasteUrl() {
  navigator.clipboard.readText().then(t => {
    document.getElementById('yt-url').value = t; onUrlInput();
  }).catch(() => {});
}

function clearTransfer() {
  document.getElementById('yt-url').value = '';
  hidePreview();
  document.getElementById('job-card').style.display = 'none';
  document.getElementById('transfer-empty').style.display = 'block';
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  currentJob = null;
}

let previewTimer = null;
function scheduleFetchPreview(url) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => fetchPreview(url), 800);
}

function fetchPreview(url) {
  url = url || document.getElementById('yt-url').value.trim();
  if (!isYT(url)) return;
  fetch('/api/preview', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})})
    .then(r => r.json()).then(m => {
      if (m.error) return;
      document.getElementById('preview-thumb').src = m.thumbnail || '';
      document.getElementById('preview-title').textContent = m.title || '';
      document.getElementById('preview-meta').textContent =
        [m.channel, m.duration ? fmtDur(m.duration) : '', m.view_count ? fmtNum(m.view_count) + ' просмотров' : ''].filter(Boolean).join(' · ');
      document.getElementById('preview-desc').textContent = (m.description || '').slice(0, 120);
      document.getElementById('preview-card').style.display = 'block';
    }).catch(() => {});
}

function hidePreview() {
  const c = document.getElementById('preview-card');
  if (c) c.style.display = 'none';
}

function startTransfer() {
  const url = document.getElementById('yt-url').value.trim();
  if (!isYT(url)) { toast('Введите корректный YouTube URL', 'err'); return; }
  const btn = document.getElementById('start-btn');
  btn.disabled = true;
  fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})})
    .then(r => r.json()).then(data => {
      btn.disabled = false;
      if (data.error) { toast(data.error, 'err'); return; }
      currentJob = data.job_id;
      document.getElementById('transfer-empty').style.display = 'none';
      const card = document.getElementById('job-card');
      card.style.display = 'block';
      document.getElementById('job-id-display').textContent = data.job_id;
      document.getElementById('job-log').innerHTML = '';
      document.getElementById('job-result').style.display = 'none';
      pollJob(data.job_id, 'job-status-badge', 'progress-wrap', 'progress-bar', 'job-log', 'job-result');
    }).catch(e => { btn.disabled = false; toast('Ошибка: ' + e, 'err'); });
}

function pollJob(jobId, statusId, wrapId, barId, logId, resultId) {
  if (pollTimer) clearTimeout(pollTimer);
  fetch('/api/job/' + jobId).then(r => r.json()).then(job => {
    if (job.error) return;
    updateJobUI(job, statusId, wrapId, barId, logId, resultId);
    if (job.status !== 'done' && job.status !== 'error')
      pollTimer = setTimeout(() => pollJob(jobId, statusId, wrapId, barId, logId, resultId), 2000);
  }).catch(() => {
    pollTimer = setTimeout(() => pollJob(jobId, statusId, wrapId, barId, logId, resultId), 4000);
  });
}

function updateJobUI(job, statusId, wrapId, barId, logId, resultId) {
  const statusEl = document.getElementById(statusId);
  if (statusEl) {
    statusEl.textContent = fmtStatus(job.status);
    statusEl.className = 'status-badge status-' + (job.status || 'queued');
  }
  const pct = job.progress || 0;
  const wrap = document.getElementById(wrapId);
  const bar  = document.getElementById(barId);
  if (wrap && bar) {
    wrap.style.display = pct > 0 ? 'block' : 'none';
    bar.style.width = pct + '%';
    bar.textContent = pct + '%';
  }
  const logEl = document.getElementById(logId);
  if (logEl && job.log) {
    logEl.innerHTML = job.log.map(e =>
      '<div class="log-line log-' + (e.level||'info') + '"><span class="log-time">' + e.t + '</span>' + esc(e.msg) + '</div>'
    ).join('');
    logEl.scrollTop = logEl.scrollHeight;
  }
  const resultEl = document.getElementById(resultId);
  if (resultEl && job.status === 'done') {
    let html = '';
    if (job.rutube_url) html += '<div class="result-link">\u2705 <a href="' + job.rutube_url + '" target="_blank">\u041e\u0442\u043a\u0440\u044b\u0442\u044c \u043d\u0430 Rutube \u2197</a></div>';
    if (html) { resultEl.innerHTML = html; resultEl.style.display = 'block'; }
  }
}

/* ══ Dub ══ */
function startDub() {
  const url = document.getElementById('dub-url').value.trim();
  if (!isYT(url)) { toast('\u0412\u0432\u0435\u0434\u0438\u0442\u0435 YouTube URL', 'err'); return; }
  const cfg = {
    url,
    dub_source_lang:   document.getElementById('dub-src-lang').value,
    dub_target_lang:   document.getElementById('dub-tgt-lang').value,
    dub_whisper_model: document.getElementById('dub-model').value,
    dub_mix_original:  parseFloat(document.getElementById('dub-mix').value) / 100,
  };
  fetch('/api/dub/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg)})
    .then(r => r.json()).then(data => {
      if (data.error) { toast(data.error, 'err'); return; }
      document.getElementById('dub-job-card').style.display = 'block';
      document.getElementById('dub-job-id').textContent = data.job_id;
      pollJob(data.job_id, 'dub-status-badge', 'dub-progress-wrap', 'dub-progress-bar', 'dub-log', 'dub-result');
    });
}

function installDubDeps() {
  const st = document.getElementById('dub-install-status');
  st.style.display = 'block'; st.style.color = 'var(--text2)';
  st.textContent = '\u0423\u0441\u0442\u0430\u043d\u0430\u0432\u043b\u0438\u0432\u0430\u044e...';
  document.getElementById('dub-install-btn').disabled = true;
  fetch('/api/dub/install', {method:'POST'}).then(r => r.json()).then(d => {
    st.style.color = d.ok ? 'var(--green)' : 'var(--yellow)';
    st.textContent = d.ok ? '\u2713 \u0423\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u043e!' : '\u26a0 ' + JSON.stringify(d.results);
    document.getElementById('dub-install-btn').disabled = false;
  });
}

/* ══ Shorts ══ */
function startShorts() {
  const url = document.getElementById('shorts-url').value.trim();
  if (!isYT(url)) { toast('\u0412\u0432\u0435\u0434\u0438\u0442\u0435 YouTube URL', 'err'); return; }
  const cfg = {
    url,
    shorts_strategy: document.getElementById('shorts-strategy').value,
    shorts_count:    parseInt(document.getElementById('shorts-count').value) || 3,
    shorts_duration: parseInt(document.getElementById('shorts-duration').value) || 58,
  };
  fetch('/api/shorts/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg)})
    .then(r => r.json()).then(data => {
      if (data.error) { toast(data.error, 'err'); return; }
      document.getElementById('shorts-job-card').style.display = 'block';
      document.getElementById('shorts-job-id').textContent = data.job_id;
      pollJob(data.job_id, 'shorts-status-badge', 'shorts-progress-wrap', 'shorts-progress-bar', 'shorts-log', 'shorts-result');
    });
}

/* ══ Dashboard / WebSocket ══ */
let ws = null;
let wsReconnTimer = null;

function connectWS() {
  if (ws && ws.readyState < 2) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onopen = () => {
    const ind = document.getElementById('ws-indicator');
    if (ind) { ind.textContent = '\u25cf live'; ind.style.color = 'var(--green)'; }
  };
  ws.onmessage = e => { try { if (JSON.parse(e.data).type !== 'pong') refreshDash(); } catch(_) {} };
  ws.onclose = () => {
    const ind = document.getElementById('ws-indicator');
    if (ind) { ind.textContent = '\u25cf offline'; ind.style.color = 'var(--text3)'; }
    wsReconnTimer = setTimeout(connectWS, 3000);
  };
  ws.onerror = () => ws.close();
}

function refreshDash() {
  fetch('/api/dashboard').then(r => r.json()).then(data => {
    const el = document.getElementById('dash-jobs');
    const qi = document.getElementById('dash-queue-info');
    if (qi) qi.textContent = '\u041e\u0447\u0435\u0440\u0435\u0434\u044c: ' + data.stats.queued + ' \u043e\u0436\u0438\u0434\u0430\u044e\u0442, ' + data.stats.running + ' \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0445';
    const all = [...(data.active||[]), ...(data.recent||[])];
    if (!all.length) {
      el.innerHTML = '<div class="empty-state" style="padding:40px 0"><div class="empty-icon">\ud83d\udcca</div><div class="empty-title">\u041d\u0435\u0442 \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0445 \u0437\u0430\u0434\u0430\u0447</div></div>';
      return;
    }
    el.innerHTML = all.map(j =>
      '<div class="job-card" style="border-left:3px solid ' + statusColor(j.status) + ';margin-bottom:8px">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">' +
      '<code style="font-size:11px;color:var(--text3)">' + j.job_id + '</code>' +
      '<span class="status-badge status-' + j.status + '">' + fmtStatus(j.status) + '</span></div>' +
      '<div style="font-size:13px">' + esc((j.meta&&j.meta.title)||'') + '</div>' +
      (j.progress>0 ? '<div class="progress-wrap" style="display:block;margin-top:6px"><div class="progress-bar" style="width:' + j.progress + '%">' + j.progress + '%</div></div>' : '') +
      '</div>'
    ).join('');
  });
}

/* ══ Batch ══ */
function startBatch() {
  const text = document.getElementById('batch-urls').value.trim();
  if (!text) { toast('\u0414\u043e\u0431\u0430\u0432\u044c\u0442\u0435 URLs', 'err'); return; }
  const resultEl = document.getElementById('batch-result');
  resultEl.style.display = 'none';
  fetch('/api/batch', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({urls: text})})
    .then(r => r.json()).then(data => {
      resultEl.style.display = 'block';
      resultEl.innerHTML = '<div class="alert ' + (data.jobs.length?'alert-ok':'alert-err') + '">' +
        '\u0417\u0430\u043f\u0443\u0449\u0435\u043d\u043e: <b>' + data.jobs.length + '</b>, \u043e\u0448\u0438\u0431\u043e\u043a: <b>' + data.invalid.length + '</b></div>';
      if (data.jobs.length) { document.getElementById('batch-urls').value = ''; switchTab('dashboard'); }
    });
}

/* ══ History ══ */
let histPage = 1;
function loadHistory(page) {
  histPage = page || histPage;
  const search = (document.getElementById('history-search')||{}).value || '';
  fetch('/api/history?page=' + histPage + '&search=' + encodeURIComponent(search))
    .then(r => r.json()).then(data => {
      renderHistoryTable(data.history || []);
      renderPager(data.total||0, histPage, data.per_page||20);
    });
}
function searchHistory() { loadHistory(1); }

function renderHistoryTable(rows) {
  const tbody = document.getElementById('history-body');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-cell">\u0418\u0441\u0442\u043e\u0440\u0438\u044f \u043f\u0443\u0441\u0442\u0430</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(h =>
    '<tr>' +
    '<td style="font-size:11px;color:var(--text3);white-space:nowrap">' + esc(h.ts) + '</td>' +
    '<td style="font-size:12px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(h.title||h.youtube_url) + '</td>' +
    '<td style="font-size:12px">' + esc(h.mode) + '</td>' +
    '<td><span class="status-badge status-' + (h.status==='success'?'done':'error') + '">' + esc(h.status) + '</span></td>' +
    '<td>' + (h.rutube_url&&h.rutube_url.startsWith('http') ? '<a href="' + h.rutube_url + '" target="_blank" style="font-size:11px;color:var(--blue)">Rutube \u2197</a>' : '\u2014') + '</td>' +
    '</tr>'
  ).join('');
}

function renderPager(total, page, perPage) {
  const el = document.getElementById('history-pager');
  if (!el) return;
  const pages = Math.ceil(total / perPage);
  if (pages <= 1) { el.innerHTML = ''; return; }
  el.innerHTML = Array.from({length: pages}, (_, i) =>
    '<button class="btn btn-sm ' + (i+1===page?'btn-blue':'btn-ghost') + '" onclick="loadHistory(' + (i+1) + ')">' + (i+1) + '</button>'
  ).join('');
}

function clearHistory() {
  if (!confirm('\u041e\u0447\u0438\u0441\u0442\u0438\u0442\u044c \u0432\u0441\u044e \u0438\u0441\u0442\u043e\u0440\u0438\u044e?')) return;
  fetch('/api/history', {method:'DELETE'}).then(() => loadHistory(1));
}

/* ══ Monitor ══ */
function loadMonitor() {
  fetch('/api/monitor').then(r => r.json()).then(data => {
    const el = document.getElementById('monitor-list');
    const channels = data.channels || [];
    if (!channels.length) {
      el.innerHTML = '<div class="empty-state" style="padding:40px 0"><div class="empty-icon">\ud83d\udc41</div><div class="empty-title">\u041d\u0435\u0442 \u043e\u0442\u0441\u043b\u0435\u0436\u0438\u0432\u0430\u0435\u043c\u044b\u0445 \u043a\u0430\u043d\u0430\u043b\u043e\u0432</div></div>';
      return;
    }
    el.innerHTML = channels.map(ch =>
      '<div class="card" style="display:flex;align-items:center;gap:10px;padding:12px 16px;margin-bottom:8px">' +
      '<div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:500">' + esc(ch.name||ch.channel_id||ch.url) + '</div>' +
      '<div style="font-size:11px;color:var(--text3)">ID: ' + esc(ch.channel_id||'') + '</div></div>' +
      '<button class="btn btn-sm btn-danger" onclick="removeChannel(\'' + esc(ch.channel_id||ch.url) + '\')">\u2715 \u0423\u0434\u0430\u043b\u0438\u0442\u044c</button></div>'
    ).join('');
  });
}

function addMonitorChannel() {
  const inp = document.getElementById('monitor-input');
  const url = inp.value.trim();
  if (!url) return;
  const st = document.getElementById('monitor-status');
  st.style.display = 'block'; st.style.color = 'var(--text2)'; st.textContent = '\u0414\u043e\u0431\u0430\u0432\u043b\u044f\u044e...';
  fetch('/api/monitor', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})})
    .then(r => r.json()).then(d => {
      if (d.error) { st.style.color = 'var(--red)'; st.textContent = '\u2717 ' + d.error; return; }
      st.style.color = 'var(--green)'; st.textContent = '\u2713 \u041a\u0430\u043d\u0430\u043b \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d';
      inp.value = ''; loadMonitor();
      setTimeout(() => { st.style.display = 'none'; }, 2000);
    });
}

function removeChannel(channelId) {
  if (!confirm('\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u043a\u0430\u043d\u0430\u043b?')) return;
  fetch('/api/monitor/' + encodeURIComponent(channelId), {method:'DELETE'}).then(() => loadMonitor());
}

/* ══ Settings ══ */
function loadSettings() {
  fetch('/api/settings').then(r => r.json()).then(cfg => {
    setVal('yt-cookie-text',     cfg.yt_cookies || '');
    setVal('rutube-cookie-text', cfg.rutube_cookies || '');
    setVal('tg-token',           cfg.tg_token || cfg.telegram_bot_token || '');
    setVal('tg-chat-id',         cfg.tg_chat_id || cfg.telegram_chat_id || '');
    setCheck('tg-enabled',       cfg.tg_enabled || cfg.telegram_enabled || false);
    setVal('settings-quality',   cfg.quality || 'best');
    setVal('settings-proxy',     cfg.proxy || '');
    setCheck('settings-keep-files', cfg.keep_files !== false);
  });
}

function saveSettings() {
  const cfg = {
    yt_cookies: getVal('yt-cookie-text'), rutube_cookies: getVal('rutube-cookie-text'),
    tg_token: getVal('tg-token'), tg_chat_id: getVal('tg-chat-id'),
    tg_enabled: getCheck('tg-enabled'), quality: getVal('settings-quality'),
    proxy: getVal('settings-proxy'), keep_files: getCheck('settings-keep-files'),
  };
  fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg)})
    .then(r => r.json()).then(() => {
      const s = document.getElementById('settings-saved');
      if (s) { s.style.display = 'inline'; setTimeout(() => s.style.display = 'none', 2000); }
      toast('\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u044b', 'ok');
    });
}

function saveCookies(type) {
  const text = getVal(type + '-cookie-text');
  const key  = type === 'yt' ? 'yt_cookies' : 'rutube_cookies';
  fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({[key]: text})})
    .then(() => {
      const badge = document.getElementById(type + '-cookie-saved');
      if (badge) { badge.style.display = 'inline'; setTimeout(() => badge.style.display = 'none', 2000); }
    });
}

function testTelegram() {
  const res = document.getElementById('tg-test-result');
  res.style.display = 'block'; res.style.color = 'var(--text2)'; res.textContent = '\u041e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u044e...';
  const cfg = { tg_token: getVal('tg-token'), tg_chat_id: getVal('tg-chat-id'), tg_enabled: getCheck('tg-enabled') };
  fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg)})
    .then(() => fetch('/api/telegram/test', {method:'POST'}))
    .then(r => r.json()).then(d => {
      res.style.color = d.ok ? 'var(--green)' : 'var(--red)';
      res.textContent = d.ok ? '\u2713 \u041e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e!' : '\u2717 ' + (d.error||'\u041e\u0448\u0438\u0431\u043a\u0430');
    });
}

/* ══ Helpers ══ */
function isYT(url) { return url && (url.includes('youtube.com') || url.includes('youtu.be')); }
function fmtStatus(s) {
  return {queued:'\u0412 \u043e\u0447\u0435\u0440\u0435\u0434\u0438', running:'\u0412\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f', done:'\u0413\u043e\u0442\u043e\u0432\u043e', error:'\u041e\u0448\u0438\u0431\u043a\u0430', starting:'\u0417\u0430\u043f\u0443\u0441\u043a'}[s] || (s||'');
}
function statusColor(s) {
  return {queued:'var(--text3)', running:'var(--blue)', done:'var(--green)', error:'var(--red)'}[s] || 'var(--text3)';
}
function fmtDur(sec) { const m = Math.floor(sec/60), s = sec%60; return m + ':' + String(s).padStart(2,'0'); }
function fmtNum(n) { if (n>=1e6) return (n/1e6).toFixed(1)+'M'; if (n>=1e3) return (n/1e3).toFixed(0)+'K'; return String(n); }
function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function toast(msg, type) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg; t.className = 'toast ' + (type||'ok') + ' show';
  setTimeout(() => t.classList.remove('show'), 3000);
}
function setVal(id, v) { const el = document.getElementById(id); if (el) el.value = v; }
function getVal(id)    { const el = document.getElementById(id); return el ? el.value : ''; }
function setCheck(id, v) { const el = document.getElementById(id); if (el) el.checked = !!v; }
function getCheck(id)    { const el = document.getElementById(id); return el ? el.checked : false; }
