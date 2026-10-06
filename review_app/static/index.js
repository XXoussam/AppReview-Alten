document.querySelectorAll('.time[data-seconds]').forEach(el => el.textContent = fmtTime(el.dataset.seconds));

const form = document.getElementById('upload-form');
if (form) {
  const mode = document.getElementById('segmentation');
  const showOpts = () => form.querySelectorAll('.opts').forEach(o => o.hidden = o.dataset.mode !== mode.value);
  mode.addEventListener('change', showOpts);
  showOpts();

  form.addEventListener('submit', e => {
    e.preventDefault();
    const bar = document.getElementById('upload-progress'), msg = document.getElementById('upload-msg');
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/videos');
    bar.hidden = false;
    form.querySelector('button[type=submit]').disabled = true;
    xhr.upload.onprogress = ev => { if (ev.lengthComputable) bar.value = 100 * ev.loaded / ev.total; };
    xhr.onload = () => {
      if (xhr.status === 202) {
        msg.textContent = 'Envoyée. Découpage en cours…';
        setTimeout(() => location.reload(), 1000);
      } else {
        let detail = xhr.responseText; try { detail = JSON.parse(detail).detail; } catch {}
        msg.textContent = 'Erreur : ' + detail;
        form.querySelector('button[type=submit]').disabled = false;
      }
    };
    xhr.onerror = () => { msg.textContent = 'Erreur réseau pendant l\'envoi.'; };
    xhr.send(new FormData(form));
  });
}

document.querySelectorAll('[data-delete]').forEach(btn => btn.addEventListener('click', async () => {
  if (!confirm(`Supprimer définitivement « ${btn.dataset.title} », sa vidéo, ses clips et ses transcriptions ?`)) return;
  try { await api('DELETE', '/api/videos/' + btn.dataset.delete); location.reload(); }
  catch (err) { alert(err.message); }
}));

if (document.querySelector('.state.processing, .state.transcribing, .state.importing')) setTimeout(() => location.reload(), 4000);
