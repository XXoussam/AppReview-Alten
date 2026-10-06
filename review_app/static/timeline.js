// Multi-track timeline: one lane per player under the video, every clip drawn at its
// exact position in the original video, with a playhead that follows playback.
// In "pistes séparées" mode the video is muted and each player's separated clips
// are played in sync with it, so you hear what voice separation produced.

const LANE_COLORS = ['#ff8fa3', '#4aa8ff', '#f5c542', '#7ee07e', '#c58cff', '#ff9f5a', '#5ef0d0', '#e0e0e0'];
const TICK_STEPS = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200];
const UNASSIGNED = '';

function createTimeline({video, getClips, getCurrent, onSelect, $, isBusy = () => false}) {
  const scroll = $('tl-scroll'), content = $('tl-content'), ruler = $('tl-ruler');
  const lanesEl = $('tl-lanes'), labelsEl = $('tl-labels'), playhead = $('tl-playhead');
  let pps = 20;                 // pixels per second
  let duration = 0;
  let lanes = [];               // [{name, color}]
  let muted = new Set(), solo = null;
  let audioMode = 'video';
  let focusClipId = null;       // while "Revoir segment" runs in separated mode, only this clip is heard
  const active = new Map();     // clip id -> HTMLAudioElement currently playing
  let fitted = false;

  function lanesFor(clips, players) {
    const names = [...players];
    for (const c of clips) if (c.speaker && !names.includes(c.speaker)) names.push(c.speaker);
    if (clips.some(c => !c.speaker)) names.push(UNASSIGNED);
    return names.map((name, i) => ({name, color: name === UNASSIGNED ? '#7d8aa8' : LANE_COLORS[i % LANE_COLORS.length]}));
  }

  function audible(laneName) {
    if (solo !== null) return laneName === solo;
    return !muted.has(laneName);
  }

  function renderRuler() {
    ruler.innerHTML = '';
    const step = TICK_STEPS.find(s => s * pps >= 70) || TICK_STEPS[TICK_STEPS.length - 1];
    for (let t = 0; t <= duration; t += step) {
      const tick = document.createElement('span');
      tick.className = 'tl-tick';
      tick.style.left = (t * pps) + 'px';
      tick.textContent = fmtTime(t).replace(/\.00$/, '');
      ruler.appendChild(tick);
    }
  }

  function render(players) {
    const clips = getClips();
    duration = Math.max(Number(video.duration) || 0, Number(document.getElementById('review').dataset.duration) || 0,
                        ...clips.map(c => c.end));
    lanes = lanesFor(clips, players);
    if (!fitted && duration && scroll.clientWidth) { fit(false); fitted = true; }
    content.style.width = Math.max(duration * pps, scroll.clientWidth) + 'px';
    renderRuler();

    labelsEl.innerHTML = '<div class="tl-label-spacer"></div>';
    lanesEl.innerHTML = '';
    const laneIndex = new Map();
    lanes.forEach((lane, i) => {
      laneIndex.set(lane.name, i);
      const label = document.createElement('div');
      label.className = 'tl-label' + (audible(lane.name) ? '' : ' silent');
      label.style.borderLeftColor = lane.color;
      const name = document.createElement('span');
      name.textContent = lane.name || 'Non attribué';
      name.title = name.textContent;
      const m = document.createElement('button');
      m.className = 'tl-ms' + (muted.has(lane.name) ? ' on' : '');
      m.textContent = 'M'; m.title = 'Couper cette piste (mode pistes séparées)';
      m.onclick = () => { muted.has(lane.name) ? muted.delete(lane.name) : muted.add(lane.name); render(players); };
      const s = document.createElement('button');
      s.className = 'tl-ms' + (solo === lane.name ? ' on' : '');
      s.textContent = 'S'; s.title = 'Écouter seulement cette piste (mode pistes séparées)';
      s.onclick = () => { solo = solo === lane.name ? null : lane.name; render(players); };
      label.append(name, m, s);
      labelsEl.appendChild(label);

      const laneEl = document.createElement('div');
      laneEl.className = 'tl-lane' + (audible(lane.name) ? '' : ' silent');
      lanesEl.appendChild(laneEl);
    });

    const current = getCurrent();
    clips.forEach((c, i) => {
      const laneEl = lanesEl.children[laneIndex.get(c.speaker || UNASSIGNED)];
      if (!laneEl) return;
      const block = document.createElement('div');
      block.className = `tl-clip ${c.status}` + (i === current ? ' selected' : '') + (c.source === 'separated' ? ' separated' : '')
        + (isBusy(c.id) ? ' asr-busy' : '');
      block.style.left = (c.start * pps) + 'px';
      block.style.width = Math.max(2, (c.end - c.start) * pps) + 'px';
      block.style.background = lanes[laneIndex.get(c.speaker || UNASSIGNED)].color;
      const text = c.final_text ?? c.model_text ?? c.reference_text ?? '';
      block.textContent = text;
      block.title = `#${c.idx + 1} ${c.speaker || 'Non attribué'}  ${fmtTime(c.start)} → ${fmtTime(c.end)}` + (text ? `\n${text}` : '');
      block.onclick = e => { e.stopPropagation(); onSelect(i); };
      laneEl.appendChild(block);
    });
  }

  function fit(rerender = true) {
    if (!duration) return;
    pps = Math.max(0.02, (scroll.clientWidth - 4) / duration);
    if (rerender) render(currentPlayers());
  }

  function zoom(factor, anchorX) {
    const x = anchorX ?? scroll.clientWidth / 2;
    const t = (scroll.scrollLeft + x) / pps;
    pps = Math.min(400, Math.max(0.02, pps * factor));
    render(currentPlayers());
    scroll.scrollLeft = t * pps - x;
  }

  let currentPlayers = () => [];

  // Seek by clicking the ruler or an empty spot on a lane.
  content.addEventListener('click', e => {
    if (e.target.closest('.tl-clip')) return;
    const x = e.clientX - content.getBoundingClientRect().left;
    video.currentTime = Math.max(0, Math.min(duration, x / pps));
    window.dispatchEvent(new CustomEvent('timeline-seek'));
  });
  scroll.addEventListener('wheel', e => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    zoom(e.deltaY < 0 ? 1.25 : 0.8, e.clientX - scroll.getBoundingClientRect().left);
  }, {passive: false});
  // Keep the lane labels aligned when the lanes area scrolls vertically.
  scroll.addEventListener('scroll', () => { labelsEl.scrollTop = scroll.scrollTop; });

  function stopAll() {
    for (const a of active.values()) a.pause();
    active.clear();
  }

  // Called every animation frame by the review page.
  function update(t) {
    playhead.style.left = (t * pps) + 'px';
    if (!video.paused && $('tl-follow').checked) {
      const x = t * pps - scroll.scrollLeft;
      if (x < 0 || x > scroll.clientWidth - 20) scroll.scrollLeft = t * pps - scroll.clientWidth * 0.2;
    }

    if (audioMode !== 'separated' || video.paused) { if (active.size) stopAll(); return; }
    const clips = getClips();
    const wanted = new Set();
    for (const c of clips) {
      if (c.start <= t && t < c.end && (focusClipId ? c.id === focusClipId : audible(c.speaker || UNASSIGNED))) {
        wanted.add(c.id);
        let a = active.get(c.id);
        if (!a) {
          a = new Audio(`/media/clips/${c.id}`);
          a.preload = 'auto';
          a.currentTime = t - c.start;
          a.play().catch(() => {});
          active.set(c.id, a);
        } else if (!a.paused && Math.abs(a.currentTime - (t - c.start)) > 0.15) {
          a.currentTime = t - c.start;  // resync on drift or after a seek
        }
      }
    }
    for (const [id, a] of active) if (!wanted.has(id)) { a.pause(); active.delete(id); }
  }

  $('tl-zoom-in').onclick = () => zoom(1.5);
  $('tl-zoom-out').onclick = () => zoom(1 / 1.5);
  $('tl-fit').onclick = () => fit();
  $('tl-audio').onchange = e => {
    audioMode = e.target.value;
    video.muted = audioMode === 'separated';
    stopAll();
  };
  video.addEventListener('loadedmetadata', () => render(currentPlayers()));
  video.addEventListener('seeking', stopAll);

  return {
    render(players) { currentPlayers = () => players; render(players); },
    update,
    setFocus(id) { focusClipId = id; stopAll(); },
    get audioMode() { return audioMode; },
    scrollToClip(c) {
      const x = c.start * pps;
      if (x < scroll.scrollLeft || x > scroll.scrollLeft + scroll.clientWidth - 40) {
        scroll.scrollLeft = x - scroll.clientWidth * 0.2;
      }
    },
  };
}
