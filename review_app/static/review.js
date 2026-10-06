const root = document.getElementById('review');
const videoId = root.dataset.videoId;
const PAD_MIN = Number(root.dataset.padMin), PAD_MAX = Number(root.dataset.padMax);
const PAD_MID = Math.round((PAD_MIN + PAD_MAX) / 2);
const DIMS = ['pleasure', 'arousal', 'dominance'];
const STATUS_ICON = {pending: '○', transcribed: '◔', approved: '✔', corrected: '✎', rejected: '✕'};

const $ = id => document.getElementById(id);
const video = $('video'), transcript = $('transcript');
const fitTranscript = () => { transcript.style.height = 'auto'; transcript.style.height = transcript.scrollHeight + 2 + 'px'; };
transcript.addEventListener('input', fitTranscript);
new ResizeObserver(fitTranscript).observe(transcript);
let clips = [], current = -1, decision = null, stopAt = null;
let players = JSON.parse(root.dataset.players || '[]');
const BUSY = ['processing', 'transcribing', 'importing'];

const timeline = createTimeline({video, $, getClips: () => clips, getCurrent: () => current, onSelect: i => select(i),
                                 isBusy: id => asr.ids.has(id)});

// ---------- video: play exactly one segment of the original ----------

function playSegment() {
  const clip = clips[current];
  if (!clip) return;
  timeline.setFocus(clip.id);
  video.currentTime = clip.start;
  stopAt = clip.end;
  video.play();
}

function tick() {
  const clip = clips[current];
  if (clip) {
    const t = video.currentTime;
    if (stopAt !== null && t >= stopAt) {
      video.pause(); video.currentTime = stopAt; stopAt = null; timeline.setFocus(null);
    }
    const len = clip.end - clip.start;
    const into = Math.min(Math.max(video.currentTime - clip.start, 0), len);
    $('seg-progress').style.width = (100 * into / len) + '%';
    $('seg-time').textContent = `${fmtTime(into)} / ${fmtTime(len)}`;
  }
  timeline.update(video.currentTime);
  $('play').textContent = video.paused ? 'Lecture' : 'Pause';
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

$('replay').onclick = playSegment;
$('play').onclick = () => {
  if (video.paused) { stopAt = null; timeline.setFocus(null); video.play(); } else video.pause();
};
window.addEventListener('timeline-seek', () => { stopAt = null; timeline.setFocus(null); });

// ---------- segments table ----------

function renderTable() {
  const onlyTodo = $('only-todo').checked;
  const tbody = document.querySelector('#segments tbody');
  tbody.innerHTML = '';
  clips.forEach((c, i) => {
    if (onlyTodo && ['approved', 'corrected', 'rejected'].includes(c.status) && i !== current) return;
    const busy = asr.ids.has(c.id);
    const tr = document.createElement('tr');
    tr.className = (i === current ? 'selected ' : '') + c.status + (busy ? ' asr-busy' : '');
    tr.dataset.clipIndex = i;

    const cbx = document.createElement('input');
    cbx.type = 'checkbox';
    cbx.checked = c._selected || false;
    cbx.onchange = (e) => { c._selected = e.target.checked; updateSelectAllCheckbox(); };
    const tdCbx = document.createElement('td');
    tdCbx.appendChild(cbx);
    tr.appendChild(tdCbx);

    for (const text of [c.idx + 1, c.speaker || '', fmtTime(c.start), fmtTime(c.end), STATUS_ICON[c.status]]) {
      const td = document.createElement('td');
      td.textContent = text;
      tr.appendChild(td);
    }
    if (busy) tr.lastChild.innerHTML = '<span class="spinner"></span>';
    tr.title = busy ? 'transcription en cours' : c.status;
    tr.onclick = (e) => { if (e.target !== cbx) select(i); };
    tbody.appendChild(tr);
  });
  const done = clips.filter(c => ['approved', 'corrected', 'rejected'].includes(c.status)).length;
  $('stats').textContent = `${done} / ${clips.length} revus`;
  updateSelectAllCheckbox();
  const sel = tbody.querySelector('tr.selected');
  if (sel) sel.scrollIntoView({block: 'nearest'});
  timeline.render(players);
}

function updateSelectAllCheckbox() {
  const n = clips.filter(c => c._selected).length;
  $('select-all').checked = n > 0 && n === clips.length;
  $('select-all').indeterminate = n > 0 && n < clips.length;
  const btn = $('transcribe');
  if (btn && !asr.started) btn.textContent = n ? `Transcrire la sélection (${n})` : 'Transcrire les clips non transcrits';
}

// ---------- transcription in progress ----------

const asr = {ids: new Set(), started: null, timer: null};

function setTranscribing(ids) {
  asr.ids = new Set(ids);
  asr.started = asr.started || Date.now();
  const btn = $('transcribe');
  const tick = () => {
    if (!btn) return;
    btn.innerHTML = '<span class="spinner"></span>';
    btn.append(`Transcription… ${Math.round((Date.now() - asr.started) / 1000)} s`);
  };
  if (btn) { btn.disabled = true; btn.classList.add('loading'); }
  clearInterval(asr.timer);
  tick();
  asr.timer = setInterval(tick, 1000);
  updateTranscriptPlaceholder();
  renderTable();
}

function doneTranscribing() {
  clearInterval(asr.timer);
  asr.ids.clear();
  asr.started = null;
  const btn = $('transcribe');
  if (btn) { btn.disabled = false; btn.classList.remove('loading'); }
  updateTranscriptPlaceholder();
}

function updateTranscriptPlaceholder() {
  const c = clips[current];
  transcript.placeholder = c && asr.ids.has(c.id) ? 'Transcription en cours…' : 'Pas encore de transcription du modèle';
}

$('select-all').onchange = e => {
  clips.forEach(c => c._selected = e.target.checked);
  renderTable();
};
$('only-todo').onchange = renderTable;

// ---------- one segment ----------

function setDecision(d) {
  decision = d;
  $('btn-ok').classList.toggle('active', d === 'ok');
  $('btn-edit').classList.toggle('active', d === 'edit');
  $('btn-reject').classList.toggle('active', d === 'reject');
  $('decision').textContent = {ok: 'Transcription correcte', edit: 'Transcription corrigée',
                               reject: 'Clip rejeté'}[d] || '';
}

function select(i, autoplay = true) {
  if (i < 0 || i >= clips.length) return;
  current = i;
  const c = clips[i];
  const hasModel = c.model_text !== null;
  transcript.value = c.final_text ?? c.model_text ?? c.reference_text ?? '';
  fitTranscript();
  transcript.readOnly = hasModel && c.status !== 'corrected';
  $('model-name').textContent = hasModel ? `(${c.model_name})` : '(pas encore transcrit)';
  $('reference').textContent = c.reference_text && hasModel ? `Sous-titre d'origine : « ${c.reference_text} »` : '';
  setDecision({approved: 'ok', corrected: 'edit', rejected: 'reject'}[c.status] || (hasModel ? null : 'edit'));

  // Speaker defaults to the previous segment's, since players often talk in runs.
  const prevSpeaker = i > 0 ? clips[i - 1].speaker : '';
  $('speaker').value = c.speaker || prevSpeaker || '';
  DIMS.forEach(d => { $(d).value = c[d] ?? PAD_MID; $(d + '-value').textContent = $(d).value; });
  $('notes').value = c.notes || '';
  $('seg-range').textContent = `⏱ ${fmtTime(c.start)} → ${fmtTime(c.end)}  (durée : ${fmtTime(c.end - c.start)})`;
  $('save-msg').textContent = c.reviewed_by ? `Revu par ${c.reviewed_by} le ${c.reviewed_at.slice(0, 16).replace('T', ' ')}` : '';
  updateTranscriptPlaceholder();
  renderTable();
  timeline.scrollToClip(c);
  if (autoplay) playSegment(); else { video.currentTime = c.start; }
}

DIMS.forEach(d => $(d).addEventListener('input', () => $(d + '-value').textContent = $(d).value));

$('btn-ok').onclick = () => {
  const c = clips[current];
  if (!c) return;
  if (c.model_text === null) { alert('Pas encore de transcription du modèle : utilisez « Corriger ».'); return; }
  transcript.value = c.model_text; fitTranscript();
  transcript.readOnly = true;
  setDecision('ok');
};
$('btn-edit').onclick = () => { transcript.readOnly = false; transcript.focus(); setDecision('edit'); };
$('btn-reject').onclick = () => setDecision('reject');

$('speaker').addEventListener('change', async e => {
  if (e.target.value !== '__add') return;
  const name = (prompt('Nom du joueur') || '').trim();
  const opts = [...e.target.options].map(o => o.value).filter(v => v && v !== '__add');
  if (name && !opts.includes(name)) {
    players = (await api('PUT', `/api/videos/${videoId}/players`, {players: [...opts, name]})).players;
    const opt = new Option(name, name);
    e.target.add(opt, e.target.options.length - 1);
  }
  e.target.value = name || '';
});

async function save() {
  const c = clips[current];
  if (!c) return false;
  let action;
  if (decision === 'reject') action = 'reject';
  else if (decision === 'ok') action = 'approve';
  else if (decision === 'edit') action = 'correct';
  else if (c.model_text !== null) action = 'approve';  // nothing touched: the model output is accepted as is
  else action = 'correct';
  const body = {action, text: transcript.value, speaker: $('speaker').value.replace('__add', ''),
                notes: $('notes').value};
  DIMS.forEach(d => body[d] = Number($(d).value));
  try {
    clips[current] = await api('POST', `/api/clips/${c.id}/review`, body);
    return true;
  } catch (err) {
    $('save-msg').textContent = 'Erreur : ' + err.message;
    return false;
  }
}

$('save-next').onclick = async () => {
  if (await save()) {
    const next = clips.findIndex((c, i) => i > current && !['approved', 'corrected', 'rejected'].includes(c.status));
    if (next >= 0) select(next);
    else if (current + 1 < clips.length) select(current + 1);
    else { renderTable(); $('save-msg').textContent = 'Dernier segment enregistré ✔'; }
  }
};
$('prev').onclick = () => select(current - 1);

document.addEventListener('keydown', e => {
  const typing = ['TEXTAREA', 'INPUT', 'SELECT'].includes(document.activeElement.tagName);
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('save-next').click(); }
  else if (typing) return;
  else if (e.key === 'r' || e.key === 'R') playSegment();
  else if (e.key === ' ') { e.preventDefault(); $('play').click(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); select(current + 1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); select(current - 1); }
});

// ---------- admin actions ----------

const transcribeBtn = $('transcribe');
if (transcribeBtn) transcribeBtn.onclick = async () => {
  // Checked clips are (re)transcribed; with none checked, every clip without a transcript.
  const selected = clips.filter(c => c._selected).map(c => c.id);
  clips.forEach(c => c._selected = false);
  setTranscribing(selected.length ? selected : untranscribedIds());
  try {
    await api('POST', `/api/videos/${videoId}/transcribe`, selected.length ? {clip_ids: selected} : undefined);
    poll();
  } catch (err) {
    doneTranscribing();
    renderTable();
    alert(err.message);
  }
};

const untranscribedIds = () => clips.filter(c => c.model_text === null).map(c => c.id);

// ---------- loading ----------

async function load(keepSelection) {
  clips = await api('GET', `/api/videos/${videoId}/clips`);
  if (keepSelection && current >= 0) { renderTable(); return; }
  const first = clips.findIndex(c => !['approved', 'corrected', 'rejected'].includes(c.status));
  if (clips.length) select(first >= 0 ? first : 0, false); else renderTable();
}

async function poll() {
  const v = await api('GET', `/api/videos/${videoId}`);
  $('video-state').textContent = v.status;
  $('video-state').className = 'state ' + v.status;
  $('video-error').textContent = v.error || '';
  if (BUSY.includes(v.status)) { setTimeout(poll, v.status === 'transcribing' ? 1500 : 3000); return; }
  if (asr.started) doneTranscribing();
  const importMsg = $('import-msg');
  if (importMsg && importMsg.textContent.endsWith('…')) importMsg.textContent = v.error || 'Import terminé ✔';
  if (JSON.stringify(JSON.parse(v.players)) !== JSON.stringify(players)) location.reload();  // new player lanes
  await load(clips.length > 0);
  if (current >= 0) select(current, false);
}

const importForm = $('import-form');
if (importForm) importForm.addEventListener('submit', async e => {
  e.preventDefault();
  const msg = $('import-msg');
  const data = new FormData(importForm);
  if (data.get('replace') && !confirm('Remplacer tous les clips existants de cette vidéo, revues comprises ?')) return;
  msg.textContent = 'Envoi…';
  try {
    const res = await fetch(`/api/videos/${videoId}/import`, {method: 'POST', body: data, credentials: 'same-origin'});
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || res.statusText);
    msg.textContent = `${body.clips} clips de ${body.players.length} joueur(s) en cours d'import…`;
    poll();
  } catch (err) { msg.textContent = 'Erreur : ' + err.message; }
});

if (root.dataset.status === 'transcribing') {
  // Page opened mid-transcription: show the clips and the progress straight away.
  load(false).then(() => { setTranscribing(untranscribedIds()); poll(); });
} else if (BUSY.includes(root.dataset.status)) poll();
else load(false);
