'use strict';
const $ = id => document.getElementById(id);
let info = null, mode = 'video', selectedQuality = 'best';
const jobs = new Map();

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

function note(s, error = false) {
  $('notice').textContent = s;
  $('notice').className = 'notice' + (error ? ' bad' : '');
}

async function api(u, o = {}) {
  const r = await fetch(u, { headers: { 'Content-Type': 'application/json' }, ...o });
  const d = await r.json();
  if (!r.ok) throw d;
  return d;
}

function settings() {
  return {
    browser:              $('browser').value,
    use_cookies:          $('cookies').checked,
    retries:              +$('retry').value  || 8,
    fragment_retries:     +$('fretry').value || 15,
    concurrent_fragments: +$('conc').value   || 2,
    rate_limit:           $('rate').value,
    sleep_interval:       +$('sleep').value  || 0,
    max_sleep_interval:   +$('msleep').value || 0,
    embed_metadata:       $('meta').checked,
    embed_thumbnail:      $('ethumb').checked,
    embed_subtitles:      $('esubs').checked,
    subtitle_lang:        $('slang').value,
    filename_template:    $('filename').value || '%(title)s [%(id)s].%(ext)s',
    audio_quality:        $('aq').value,
  };
}

function fmt(n) {
  if (!n) return '';
  let x = Number(n);
  if (!Number.isFinite(x)) return String(n);
  const u = ['B','KB','MB','GB'];
  let i = 0;
  while (x > 1024 && i < 3) { x /= 1024; i++; }
  return x.toFixed(1) + ' ' + u[i];
}

function explain(e) {
  note(e.help || e.error || e.message || 'Operation failed', true);
  if (e.error_kind) $('diagnostics').classList.remove('hidden');
}

function renderQualities(qs) {
  const box = $('qualityButtons');
  box.innerHTML = '';
  if (!qs?.length) {
    $('qualityInfo').textContent = 'No video formats returned.';
    selectedQuality = 'best';
    return;
  }
  selectedQuality = qs[0].height;
  qs.forEach((q, i) => {
    const b = document.createElement('button');
    b.className = 'quality-btn' + (i === 0 ? ' active' : '');
    b.innerHTML = `<strong>${q.height}p</strong><span>${q.width || ''}${q.fps ? ' • ' + q.fps + 'fps' : ''}${q.mp4_compatible ? ' • MP4' : ''}</span>`;
    b.onclick = () => {
      document.querySelectorAll('.quality-btn').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      selectedQuality = q.height;
      $('qualityInfo').textContent = `${q.height}p • ${q.width || '?'}×${q.height} • ${q.fps || '?'}fps • ${q.mp4_compatible ? 'MP4 source available' : 'MKV fallback may be used'}`;
    };
    box.appendChild(b);
  });
  $('qualityInfo').textContent = `${qs.length} resolutions found. Highest: ${qs[0].height}p. MP4 sources marked.`;
}

async function analyze() {
  const u = $('url').value.trim();
  if (!u) return note('Paste a YouTube URL first.', true);
  $('analyze').disabled = true;
  note('Analyzing…');
  try {
    const b = $('cookies').checked ? $('browser').value : 'none';
    const d = await api('/api/analyze', {
      method: 'POST',
      body: JSON.stringify({ url: u, browser: b, settings: settings() }),
    });
    info = d;
    $('video').classList.remove('hidden');
    $('download').classList.remove('hidden');
    $('thumb').src = d.thumbnail || '';
    $('thumblink').href = 'https://www.youtube.com/watch?v=' + encodeURIComponent(d.id || '');
    $('vtitle').textContent = d.title || 'Untitled';
    $('videometa').textContent = [
      d.channel || d.uploader,
      d.duration_string,
      d.view_count ? Number(d.view_count).toLocaleString() + ' views' : '',
    ].filter(Boolean).join(' • ');
    $('desc').textContent = d.description || '';
    renderQualities(d.qualities);
    $('downloadPath').textContent = d.download_dir;
    note(`Ready — ${d.qualities?.length || 0} quality options found.`);
  } catch (e) {
    explain(e);
  } finally {
    $('analyze').disabled = false;
  }
}

async function start() {
  if (!info) return note('Analyze first.', true);
  $('start').disabled = true;
  try {
    const d = await api('/api/download', {
      method: 'POST',
      body: JSON.stringify({
        url:       info.url,
        mode,
        quality:   mode === 'audio' ? 'best' : selectedQuality,
        container: $('container').value,
        title:     info.title || info.url,
        settings:  settings(),
      }),
    });
    jobs.set(d.job_id, { id: d.job_id, status: 'queued', progress: 0, message: 'Queued', url: info.url, title: info.title });
    render();
    note('Download started.');
  } catch (e) {
    explain(e);
  } finally {
    $('start').disabled = false;
  }
}

function render() {
  const arr = [...jobs.values()];
  if (!arr.length) { $('jobs').innerHTML = 'No active downloads.'; return; }

  $('jobs').innerHTML = arr.map(j => {
    const p = Math.max(0, Math.min(100, Number(j.progress) || 0));
    const isProcessing = ['processing', 'fallback'].includes(j.status);
    const isActive     = ['downloading', 'starting', 'queued'].includes(j.status);

    let act = '';
    if (isActive || isProcessing) {
      act = `<button onclick="cancel('${j.id}')">Cancel</button>`;
      if (j.status === 'downloading') act = `<button onclick="pause('${j.id}')">Pause</button>` + act;
    } else if (j.status === 'paused') {
      act = `<button onclick="resume('${j.id}')">Resume</button><button onclick="cancel('${j.id}')">Cancel</button>`;
    } else if (j.status === 'error') {
      act = `<button onclick="retry('${j.id}')">Retry</button>`;
    }

    const file = j.filename ? `<a href="/download/${encodeURIComponent(j.filename)}"><button>Download file</button></a>` : '';

    const statusText = {
      queued:'Queued', starting:'Preparing', downloading:'Downloading',
      processing:'Processing', fallback:'Trying fallback',
      paused:'Paused', done:'Completed', error:'Failed', cancelled:'Cancelled',
    }[j.status] || j.status;

    const err = j.status === 'error' ? `
      <div class="errorbox">
        <div>${esc(j.help || j.error || 'Download failed')}</div>
        ${j.fallback_used ? '<div class="fallback-note">All automatic fallbacks were attempted.</div>' : ''}
        <details><summary>Technical log</summary><div class="logs">${esc((j.logs || []).join('\n'))}</div></details>
      </div>` : '';

    const detail = [
      j.speed && 'Speed ' + j.speed,
      j.eta   && 'ETA '   + j.eta,
      j.downloaded && j.total && j.downloaded + ' / ' + j.total,
    ].filter(Boolean).join(' • ');

    return `<div class="job ${j.status === 'error' ? 'error' : ''}">
      <div class="jobtop"><b>${esc(j.title || j.url || j.id)}</b><span class="status ${j.status}">${esc(statusText)}</span></div>
      <div class="progressline"><span>${p.toFixed(1)}%</span><span>${esc(j.message || statusText)}</span></div>
      <div class="bar ${isProcessing ? 'indeterminate' : ''}"><i style="width:${isProcessing ? 100 : p}%"></i></div>
      <small>${esc(detail)}${j.attempts ? esc(' • strategy ' + (j.attempt || 1) + '/' + j.attempts) : ''}</small>
      <div class="jobactions">${act}${file}</div>${err}
    </div>`;
  }).join('');
}

async function pause(id)  { try { await api('/api/jobs/' + id + '/pause',  { method: 'POST' }) } catch (e) { explain(e) } }
async function resume(id) { try { await api('/api/jobs/' + id + '/resume', { method: 'POST' }) } catch (e) { explain(e) } }
async function cancel(id) {
  try {
    await api('/api/jobs/' + id + '/cancel', { method: 'POST' });
    const j = jobs.get(id);
    if (j) { j.status = 'cancelled'; j.message = 'Cancelled.'; }
    render();
  } catch (e) { explain(e) }
}
async function retry(id) {
  try {
    const d = await api('/api/retry/' + id, { method: 'POST' });
    jobs.set(d.job_id, { id: d.job_id, status: 'queued', progress: 0, message: 'Queued' });
    render();
  } catch (e) { explain(e) }
}

async function poll() {
  const DONE = new Set(['done', 'error', 'cancelled']);
  for (const id of jobs.keys()) {
    const j = jobs.get(id);
    if (j && DONE.has(j.status)) continue;
    try {
      const u = await api('/api/jobs/' + id);
      jobs.set(id, u);
      if (u.status === 'done') loadFiles();
    } catch {}
  }
  render();
  setTimeout(poll, 1500);
}

async function loadFiles() {
  try {
    const d = await api('/api/files');
    $('files').innerHTML = d.files.length
      ? d.files.map(f => `<div class="file"><span>${esc(f.relative)} <small>${fmt(f.size)}</small></span><a href="${f.url}">Download</a></div>`).join('')
      : `No completed media yet.<br><small>${esc(d.download_dir)}</small>`;
  } catch {}
}

async function openFolder() {
  try {
    const d = await api('/api/open-download-folder', { method: 'POST' });
    note(d.ok ? 'Opened downloads folder.' : d.error, !d.ok);
  } catch (e) { explain(e) }
}

async function syscheck() {
  note('Running system check…');
  try {
    const d = await api('/api/diagnostics/system');
    $('diagnostics').classList.remove('hidden');
    $('diagnostics').innerHTML = `<b>System check</b><div>${
      d.checks.map(x => `<div class="check ${x.ok ? 'ok' : 'bad'}">${x.ok ? '✓' : '✕'} ${esc(x.name)} — ${esc(x.detail)}</div>`).join('')
    }</div><small>${d.tips.map(esc).join(' • ')}</small>`;
    note(d.ok ? 'All checks passed.' : 'Issue detected — see above.', !d.ok);
  } catch (e) { explain(e) }
}

async function repairEngine() {
  note('Repairing media engine…');
  try {
    const d = await api('/api/repair-engine', { method: 'POST' });
    $('diagnostics').classList.remove('hidden');
    $('diagnostics').innerHTML = `<b>Repair log</b><pre>${esc(d.log || d.error || d.message || '')}</pre>`;
    note(d.ok ? 'Repair complete. Restart Dhurta then run System Check.' : (d.error || d.message || 'Repair failed.'), !d.ok);
  } catch (e) { explain(e) }
}

$('analyze').onclick    = analyze;
$('start').onclick      = start;
$('openfolder').onclick = openFolder;
$('openfolder2').onclick= openFolder;
$('syscheck').onclick   = syscheck;
$('repair').onclick     = repairEngine;
$('openyt').onclick     = () => api('/api/open-youtube', { method:'POST', body: JSON.stringify({ url: $('url').value.trim() || 'https://www.youtube.com' }) });
$('test').onclick = async () => {
  note('Testing connection…');
  try {
    const d = await api('/api/test', { method:'POST', body: JSON.stringify({ settings:{ browser:'none', use_cookies:false } }) });
    note(d.message);
  } catch (e) { explain(e) }
};
$('clear').onclick = async () => {
  try { await api('/api/clear-history', { method:'POST' }); } catch {}
  jobs.clear();
  render();
};

document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => {
  document.querySelectorAll('.tabs button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  mode = b.dataset.mode;
  $('container').disabled = mode === 'audio';
  document.querySelector('.qualitybox').style.opacity = mode === 'audio' ? 0.5 : 1;
});

(async () => {
  try {
    const h = await api('/api/health');
    $('health').textContent = `yt-dlp ${h.yt_dlp} • FFmpeg ${h.engine.ready ? 'OK' : 'MISSING'}`;
    $('downloadPath').textContent = h.download_dir;
  } catch {}

  try {
    const { jobs: existing } = await api('/api/jobs');
    for (const j of existing) jobs.set(j.id, j);
    render();
  } catch {}

  const q = new URLSearchParams(location.search);
  if (q.get('url')) {
    $('url').value = q.get('url');
    if (q.get('browser') && q.get('browser') !== 'none') {
      $('browser').value = q.get('browser');
      $('cookies').checked = true;
    }
    analyze();
  }

  loadFiles();
  poll();
})();
