"""Clip review web app.

Upload a game video, cut its audio into clips whose start/end are timestamps
in the original video, transcribe them with an ASR model, and let a human
approve or correct each transcript. Run with:

  uvicorn review_app.app:create_app --factory --host 127.0.0.1 --port 8000
"""

import json
import logging
import shutil
import time
import uuid
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import importer, segmenter
from .auth import UserStore
from .config import Settings
from .db import Database, now
from .metrics import corpus_wer
from .storage import build_storage
from .transcriber import build_transcriber

logger = logging.getLogger('review_app')
HERE = Path(__file__).parent
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.webm', '.avi', '.m4v', '.ts', '.flv', '.mp3', '.wav', '.m4a', '.ogg'}
VIDEO_CONTENT_TYPES = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm', '.mkv': 'video/x-matroska',
                       '.mov': 'video/quicktime', '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4',
                       '.ogg': 'audio/ogg'}
PAD_MIN, PAD_MAX = 1, 7
BUSY = ('processing', 'transcribing', 'importing')
BROWSER_PLAYABLE = {'.mp4', '.m4v', '.webm', '.mov', '.mp3', '.wav', '.m4a', '.ogg'}


def parse_players(raw):
  names = [p.strip() for p in (raw or '').replace('\n', ',').split(',')]
  return list(dict.fromkeys(n[:64] for n in names if n))[:20]


def create_app(settings=None):
  logging.basicConfig(level=logging.INFO)
  settings = settings or Settings()
  settings.data_dir.mkdir(parents=True, exist_ok=True)
  settings.work_dir.mkdir(parents=True, exist_ok=True)

  db = Database(settings.db_path)
  users = UserStore(settings.users_path)
  storage = build_storage(settings)
  transcriber = build_transcriber(settings)
  # One worker: ffmpeg and the ASR model are heavy, jobs queue up in order.
  jobs = ThreadPoolExecutor(max_workers=1)
  failed_logins = defaultdict(list)

  # Jobs don't survive a restart; don't leave videos stuck as "processing".
  db.run("UPDATE videos SET status = 'failed', error = 'interrupted by a restart, delete and re-upload' "
         "WHERE status = 'processing'")
  db.run("UPDATE videos SET status = 'ready' WHERE status = 'transcribing'")
  db.run("UPDATE videos SET status = 'ready', error = 'clip import interrupted by a restart, import again' "
         "WHERE status = 'importing'")

  app = FastAPI(title='Voice comms clip review', docs_url=None, redoc_url=None, openapi_url=None)
  app.state.settings, app.state.db, app.state.storage, app.state.jobs = settings, db, storage, jobs
  templates = Jinja2Templates(directory=HERE / 'templates')
  # Appended to /static URLs so browsers fetch the new CSS/JS after an update instead of a cached copy.
  templates.env.globals['static_v'] = int(max(p.stat().st_mtime for p in (HERE / 'static').iterdir()))
  app.mount('/static', StaticFiles(directory=HERE / 'static'), name='static')

  @app.middleware('http')
  async def security(request: Request, call_next):
    # Cookie-authenticated writes must come from this site (CSRF guard on top of SameSite=strict).
    if request.method in ('POST', 'PUT', 'DELETE') and not request.headers.get('authorization'):
      origin = request.headers.get('origin')
      if origin and urlparse(origin).netloc != request.headers.get('host'):
        return PlainTextResponse('cross-site request refused', status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    # no-referrer would make browsers send `Origin: null` on form posts and trip the check above.
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = (
      "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; "
      "frame-ancestors 'none'")
    if settings.https_only:
      response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response

  # Added last so it runs first and the session is available to the middleware above.
  app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=settings.session_max_age,
                     same_site='strict', https_only=settings.https_only, session_cookie='review_session')

  # ---------- auth ----------

  def current_user(request: Request):
    header = request.headers.get('authorization', '')
    if header.startswith('Bearer ') and settings.api_token:
      if header[7:] == settings.api_token:
        return {'username': 'api-token', 'role': 'service'}
      raise HTTPException(401, 'bad token')
    user = request.session.get('user')
    if not user:
      raise HTTPException(401, 'login required')
    return user

  def admin_user(user=Depends(current_user)):
    if user['role'] != 'admin':
      raise HTTPException(403, 'admin only')
    return user

  def human_user(user=Depends(current_user)):
    if user['role'] not in ('admin', 'reviewer'):
      raise HTTPException(403, 'reviewers only')
    return user

  def page_user(request: Request):
    return request.session.get('user')

  @app.exception_handler(401)
  async def unauthorized(request: Request, exc):
    if request.url.path.startswith(('/api/', '/media/')):
      return PlainTextResponse(exc.detail, status_code=401)
    return RedirectResponse('/login', status_code=303)

  @app.get('/login', response_class=HTMLResponse)
  def login_page(request: Request, error: str = ''):
    return templates.TemplateResponse(request, 'login.html', {'error': error})

  @app.post('/login')
  def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else 'unknown'
    recent = [t for t in failed_logins[ip] if time.time() - t < 900]
    failed_logins[ip] = recent
    if len(recent) >= 10:
      return RedirectResponse('/login?error=Too+many+attempts,+try+again+later', status_code=303)
    user = users.authenticate(username, password)
    if not user:
      recent.append(time.time())
      return RedirectResponse('/login?error=Wrong+username+or+password', status_code=303)
    request.session.clear()
    request.session['user'] = user
    db.audit(username, 'login')
    return RedirectResponse('/', status_code=303)

  @app.post('/logout')
  def logout(request: Request):
    request.session.clear()
    return RedirectResponse('/login', status_code=303)

  # ---------- pages ----------

  @app.get('/', response_class=HTMLResponse)
  def index(request: Request, user=Depends(page_user)):
    if not user:
      return RedirectResponse('/login', status_code=303)
    return templates.TemplateResponse(request, 'index.html', {
      'user': user, 'videos': list_videos(), 'max_upload_mb': settings.max_upload_mb})

  @app.get('/videos/{video_id}', response_class=HTMLResponse)
  def video_page(request: Request, video_id: str, user=Depends(page_user)):
    if not user:
      return RedirectResponse('/login', status_code=303)
    video = get_video(video_id)
    return templates.TemplateResponse(request, 'video.html', {
      'user': user, 'video': video, 'transcriber': transcriber.name,
      'players': json.loads(video['players']), 'pad_scale': (PAD_MIN, PAD_MAX)})

  # ---------- videos ----------

  def get_video(video_id):
    video = db.one('SELECT * FROM videos WHERE id = ?', video_id)
    if not video:
      raise HTTPException(404, 'video not found')
    return video

  def list_videos():
    return db.all("""
      SELECT v.*, COUNT(c.id) AS clip_count,
             SUM(c.status IN ('approved', 'corrected', 'rejected')) AS reviewed_count
      FROM videos v LEFT JOIN clips c ON c.video_id = v.id
      GROUP BY v.id ORDER BY v.created_at DESC""")

  @app.get('/api/videos')
  def api_list_videos(user=Depends(current_user)):
    return list_videos()

  @app.get('/api/videos/{video_id}')
  def api_get_video(video_id: str, user=Depends(current_user)):
    video = get_video(video_id)
    video['stats'] = video_stats(video_id)
    return video

  async def save_upload(upload, dest, limit):
    written = 0
    with open(dest, 'wb') as out:
      while chunk := await upload.read(4 * 1024 * 1024):
        written += len(chunk)
        if written > limit:
          raise HTTPException(413, f'file larger than {settings.max_upload_mb} MB')
        out.write(chunk)

  @app.post('/api/videos', status_code=202)
  async def upload_video(
    file: UploadFile = File(...),
    title: str = Form(''),
    players: str = Form(''),
    segmentation: str = Form('silence'),
    captions: UploadFile | None = File(None),
    noise_db: float = Form(-35.0),
    min_silence: float = Form(0.5),
    max_len: float = Form(20.0),
    fixed_len: float = Form(10.0),
    user=Depends(admin_user),
  ):
    ext = Path(file.filename or '').suffix.lower()
    if ext not in VIDEO_EXTENSIONS:
      raise HTTPException(400, f'unsupported file type {ext or "(none)"}')
    if segmentation not in ('silence', 'fixed', 'captions', 'none'):
      raise HTTPException(400, 'segmentation must be silence, fixed, captions or none')
    caption_rows = None
    if segmentation == 'captions':
      if not captions or not captions.filename:
        raise HTTPException(400, 'captions mode needs a caption JSON file')
      try:
        caption_rows = json.loads(await captions.read())
        assert isinstance(caption_rows, list) and all('start' in c for c in caption_rows)
      except Exception:
        raise HTTPException(400, 'captions must be a JSON list of {"start", "duration", "text"}')
    if not 0.1 <= max_len <= 60 or not 0.1 <= fixed_len <= 60 or not 0.1 <= min_silence <= 10:
      raise HTTPException(400, 'clip length settings out of range')

    video_id = uuid.uuid4().hex
    job_dir = settings.work_dir / video_id
    job_dir.mkdir(parents=True)
    local_video = job_dir / f'original{ext}'
    try:
      await save_upload(file, local_video, settings.max_upload_mb * 1024 * 1024)
    except HTTPException:
      shutil.rmtree(job_dir, ignore_errors=True)
      raise

    params = {'noise_db': noise_db, 'min_silence': min_silence, 'max_len': max_len, 'fixed_len': fixed_len}
    db.run("""INSERT INTO videos (id, title, original_filename, video_key, segmentation, players, status,
                                  uploaded_by, created_at)
              VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?)""",
           video_id, title.strip() or Path(file.filename).stem, Path(file.filename).name,
           f'videos/{video_id}/original{ext}', json.dumps({'mode': segmentation, **params}),
           json.dumps(parse_players(players)), user['username'], now())
    db.audit(user['username'], 'upload', video_id)
    jobs.submit(process_video, video_id, local_video, segmentation, caption_rows, params)
    return {'id': video_id, 'status': 'processing'}

  def process_video(video_id, local_video, mode, caption_rows, params):
    video = get_video(video_id)
    job_dir = local_video.parent
    try:
      if not segmenter.has_audio(local_video):
        raise RuntimeError('the file has no audio track')
      duration = segmenter.probe_duration(local_video)
      storage.put_file(video['video_key'], local_video, VIDEO_CONTENT_TYPES.get(local_video.suffix))
      playback_key = video['video_key']
      if local_video.suffix not in BROWSER_PLAYABLE:
        playable = job_dir / 'playback.mp4'
        segmenter.make_playable(local_video, playable)
        playback_key = f'videos/{video_id}/playback.mp4'
        storage.put_file(playback_key, playable, 'video/mp4')
        playable.unlink()

      wav = job_dir / 'audio.wav'
      segmenter.extract_audio(local_video, wav)
      audio_key = f'videos/{video_id}/audio.wav'
      storage.put_file(audio_key, wav, 'audio/wav')

      if mode == 'captions':
        segments = segmenter.segments_from_captions(caption_rows, duration)
      elif mode == 'fixed':
        segments = segmenter.segments_fixed(duration, params['fixed_len'])
      elif mode == 'none':
        segments = []  # clips come later from an import of separated voices
      else:
        segments = segmenter.segments_from_silence(
          wav, duration, params['noise_db'], params['min_silence'], max_len=params['max_len'])

      rows = []
      for idx, (start, end, ref_text) in enumerate(segments):
        clip_path = job_dir / f'clip_{idx:05d}.wav'
        segmenter.cut_clip(wav, start, end, clip_path)
        clip_key = f'videos/{video_id}/clips/clip_{idx:05d}.wav'
        storage.put_file(clip_key, clip_path, 'audio/wav')
        clip_path.unlink()
        rows.append((uuid.uuid4().hex, video_id, idx, round(start, 3), round(end, 3), clip_key, ref_text, 'pending'))

      db.run_many("""INSERT INTO clips (id, video_id, idx, start, "end", clip_key, reference_text, status)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", rows)
      db.run("UPDATE videos SET status = 'ready', duration = ?, audio_key = ?, playback_key = ? WHERE id = ?",
             round(duration, 3), audio_key, playback_key, video_id)
    except Exception as exc:
      logger.exception('processing %s failed', video_id)
      db.run("UPDATE videos SET status = 'failed', error = ? WHERE id = ?", str(exc)[:1000], video_id)
    finally:
      shutil.rmtree(job_dir, ignore_errors=True)

  def renumber_clips(video_id):
    """Keep the segments table chronological once auto-cut and imported clips mix."""
    with db.connect() as conn:
      conn.execute('UPDATE clips SET idx = -idx - 1 WHERE video_id = ?', (video_id,))
      ids = [r['id'] for r in conn.execute(
        'SELECT id FROM clips WHERE video_id = ? ORDER BY start, speaker, "end"', (video_id,))]
      conn.executemany('UPDATE clips SET idx = ? WHERE id = ?', list(enumerate(ids)))

  @app.post('/api/videos/{video_id}/import', status_code=202)
  async def import_separated(video_id: str, file: UploadFile = File(...), replace: bool = Form(False),
                             user=Depends(current_user)):
    """Import per-player clips from voice separation: a ZIP with audio files and a manifest
    (player, start, end, file[, text]) whose times are positions in the original video."""
    if user['role'] not in ('admin', 'service'):
      raise HTTPException(403, 'admin or API token only')
    video = get_video(video_id)
    if video['status'] != 'ready':
      raise HTTPException(409, f'video is {video["status"]}')

    job_dir = settings.work_dir / f'import_{uuid.uuid4().hex}'
    job_dir.mkdir(parents=True)
    zip_path = job_dir / 'clips.zip'
    limit = settings.max_upload_mb * 1024 * 1024
    try:
      await save_upload(file, zip_path, limit)
      with zipfile.ZipFile(zip_path) as zf:
        importer.check_zip_size(zf, limit)
        rows = importer.read_manifest(zf)
    except zipfile.BadZipFile:
      shutil.rmtree(job_dir, ignore_errors=True)
      raise HTTPException(400, 'the file is not a ZIP')
    except importer.ImportError_ as exc:
      shutil.rmtree(job_dir, ignore_errors=True)
      raise HTTPException(400, str(exc))
    except HTTPException:
      shutil.rmtree(job_dir, ignore_errors=True)
      raise

    db.run("UPDATE videos SET status = 'importing', error = NULL WHERE id = ?", video_id)
    db.audit(user['username'], f'import:{len(rows)}' + (':replace' if replace else ''), video_id)
    jobs.submit(run_import, video_id, zip_path, rows, replace)
    return {'status': 'importing', 'clips': len(rows), 'players': sorted({r['player'] for r in rows})}

  def run_import(video_id, zip_path, rows, replace):
    job_dir = zip_path.parent
    video = get_video(video_id)
    batch = uuid.uuid4().hex[:8]
    new_keys = []
    try:
      inserted, warnings = [], 0
      with zipfile.ZipFile(zip_path) as zf:
        for n, row in enumerate(rows):
          src = job_dir / f'src_{n}{Path(row["member"]).suffix}'
          with zf.open(row['member']) as fin, open(src, 'wb') as fout:
            shutil.copyfileobj(fin, fout)
          wav = job_dir / f'sep_{n:05d}.wav'
          segmenter.convert_clip(src, wav)
          src.unlink()
          clip_len = segmenter.probe_duration(wav)
          start = row['start']
          end = row['end'] if row['end'] is not None else start + clip_len
          if abs((end - start) - clip_len) > 0.25:
            warnings += 1  # manifest times and audio length disagree; the manifest wins for placement
          key = f'videos/{video_id}/clips/sep_{batch}_{n:05d}.wav'
          storage.put_file(key, wav, 'audio/wav')
          new_keys.append(key)
          wav.unlink()
          inserted.append((uuid.uuid4().hex, video_id, -1_000_000 - n, round(start, 3), round(end, 3), key,
                           'separated', row['text'], row['player'], 'pending'))

      if replace:
        for old in db.all('SELECT clip_key FROM clips WHERE video_id = ?', video_id):
          storage.delete_prefix(old['clip_key'])
        db.run('DELETE FROM clips WHERE video_id = ?', video_id)
      db.run_many("""INSERT INTO clips (id, video_id, idx, start, "end", clip_key, source, reference_text, speaker, status)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", inserted)
      renumber_clips(video_id)

      players = json.loads(video['players'])
      players += [p for p in dict.fromkeys(r['player'] for r in rows) if p not in players]
      note = (f'{warnings} imported clip(s) have a length that differs from their start/end by more than 0.25 s'
              if warnings else None)
      db.run("UPDATE videos SET status = 'ready', players = ?, error = ? WHERE id = ?",
             json.dumps(players), note, video_id)
    except Exception as exc:
      logger.exception('import into %s failed', video_id)
      for key in new_keys:
        storage.delete_prefix(key)
      db.run("UPDATE videos SET status = 'ready', error = ? WHERE id = ?", f'import failed: {exc}'[:1000], video_id)
    finally:
      shutil.rmtree(job_dir, ignore_errors=True)

  @app.put('/api/videos/{video_id}/players')
  async def set_players(video_id: str, request: Request, user=Depends(human_user)):
    """body {"players": ["Enjawve", "Kaylem", ...]}"""
    get_video(video_id)
    body = await request.json()
    players = parse_players(','.join(body.get('players') or []))
    db.run('UPDATE videos SET players = ? WHERE id = ?', json.dumps(players), video_id)
    return {'players': players}

  @app.delete('/api/videos/{video_id}')
  def delete_video(video_id: str, user=Depends(admin_user)):
    """Erases the video, its audio, every clip and every transcript (GDPR right to erasure)."""
    video = get_video(video_id)
    if video['status'] in BUSY:
      raise HTTPException(409, 'wait for the current job to finish')
    storage.delete_prefix(f'videos/{video_id}/')
    db.run('DELETE FROM clips WHERE video_id = ?', video_id)
    db.run('DELETE FROM videos WHERE id = ?', video_id)
    db.audit(user['username'], 'delete', video_id)
    return {'deleted': video_id}

  # ---------- clips & transcription ----------

  @app.get('/api/videos/{video_id}/clips')
  def list_clips(video_id: str, user=Depends(current_user)):
    get_video(video_id)
    return db.all('SELECT * FROM clips WHERE video_id = ? ORDER BY idx', video_id)

  @app.post('/api/videos/{video_id}/transcribe', status_code=202)
  async def transcribe(video_id: str, request: Request, only_missing: bool = True, user=Depends(admin_user)):
    """Body {"clip_ids": [...]} transcribes those clips (again if they already have a transcript);
    without a body, every clip that has none yet (all clips with ?only_missing=false)."""
    video = get_video(video_id)
    if transcriber.name == 'none':
      raise HTTPException(501, 'No model configured on the server. Set REVIEW_TRANSCRIBER, or push '
                               'transcripts with PUT /api/clips/{id}/model-transcript.')
    if video['status'] != 'ready':
      raise HTTPException(409, f'video is {video["status"]}')
    clip_ids = (await request.json()).get('clip_ids') if await request.body() else None
    if clip_ids is not None and (not isinstance(clip_ids, list) or not clip_ids
                                 or not all(isinstance(i, str) for i in clip_ids)):
      raise HTTPException(400, 'clip_ids must be a non-empty list of clip ids')
    db.run("UPDATE videos SET status = 'transcribing' WHERE id = ?", video_id)
    db.audit(user['username'], 'transcribe', video_id)
    jobs.submit(run_transcription, video_id, only_missing, clip_ids)
    return {'status': 'transcribing'}

  def run_transcription(video_id, only_missing, clip_ids=None):
    job_dir = settings.work_dir / f'asr_{video_id}'
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
      if clip_ids:
        marks = ','.join('?' * len(clip_ids))
        clips = db.all(f'SELECT id, clip_key FROM clips WHERE video_id = ? AND id IN ({marks}) ORDER BY idx',
                       video_id, *clip_ids)
      else:
        where = "AND model_text IS NULL" if only_missing else ""
        clips = db.all(f'SELECT id, clip_key FROM clips WHERE video_id = ? {where} ORDER BY idx', video_id)
      for batch_start in range(0, len(clips), 32):
        batch = clips[batch_start:batch_start + 32]
        paths = []
        for clip in batch:
          path = job_dir / f'{clip["id"]}.wav'
          storage.get_to_file(clip['clip_key'], path)
          paths.append(path)
        texts = transcriber.transcribe(paths)
        for clip, text in zip(batch, texts):
          if text is not None:
            save_model_text(clip['id'], text, transcriber.name)
        for path in paths:
          path.unlink()
      db.run("UPDATE videos SET status = 'ready', error = NULL WHERE id = ?", video_id)
    except Exception as exc:
      logger.exception('transcription of %s failed', video_id)
      db.run("UPDATE videos SET status = 'ready', error = ? WHERE id = ?", f'transcription failed: {exc}'[:1000],
             video_id)
    finally:
      shutil.rmtree(job_dir, ignore_errors=True)

  def save_model_text(clip_id, text, model_name):
    # A new model output never overwrites a human decision; it only moves pending clips along.
    db.run("""UPDATE clips SET model_text = ?, model_name = ?,
                status = CASE WHEN status = 'pending' THEN 'transcribed' ELSE status END
              WHERE id = ?""", text, model_name, clip_id)

  @app.put('/api/clips/{clip_id}/model-transcript')
  async def put_model_transcript(clip_id: str, request: Request, user=Depends(current_user)):
    """For an external ASR job: body {"text": "...", "model": "parakeet-ft-v2"}."""
    if user['role'] not in ('admin', 'service'):
      raise HTTPException(403, 'admin or API token only')
    body = await request.json()
    if not isinstance(body.get('text'), str):
      raise HTTPException(400, 'text is required')
    if not db.one('SELECT id FROM clips WHERE id = ?', clip_id):
      raise HTTPException(404, 'clip not found')
    save_model_text(clip_id, body['text'].strip(), str(body.get('model') or 'external'))
    return {'ok': True}

  @app.post('/api/clips/{clip_id}/review')
  async def review_clip(clip_id: str, request: Request, user=Depends(human_user)):
    """body {"action": "approve" | "correct" | "reject" | "reset", "text": "...",
             "speaker": "...", "pleasure": 1-7, "arousal": 1-7, "dominance": 1-7, "notes": "..."}

    approve = the model transcript is right, correct = save the typed text,
    reject = no usable speech (noise, music, overlap), reset = back to unreviewed.
    """
    body = await request.json()
    clip = db.one('SELECT * FROM clips WHERE id = ?', clip_id)
    if not clip:
      raise HTTPException(404, 'clip not found')
    action = body.get('action')
    if action == 'approve':
      text = clip['model_text'] if clip['model_text'] is not None else clip['reference_text']
      if text is None:
        raise HTTPException(400, 'nothing to approve yet, type the transcript and save it as a correction')
      status = 'approved'
    elif action == 'correct':
      text = (body.get('text') or '').strip()
      if not text:
        raise HTTPException(400, 'empty transcript; use reject for clips with no usable speech')
      # Typing exactly what the model said is an approval, which keeps the WER honest.
      status = 'approved' if text == (clip['model_text'] or '').strip() else 'corrected'
    elif action == 'reject':
      text, status = None, 'rejected'
    elif action == 'reset':
      text, status = None, 'transcribed' if clip['model_text'] is not None else 'pending'
    else:
      raise HTTPException(400, 'unknown action')
    pad = {}
    for dim in ('pleasure', 'arousal', 'dominance'):
      value = body.get(dim)
      if value is not None and (not isinstance(value, int) or not PAD_MIN <= value <= PAD_MAX):
        raise HTTPException(400, f'{dim} must be an integer from {PAD_MIN} to {PAD_MAX}')
      pad[dim] = value if action not in ('reject', 'reset') else None
    speaker = (body.get('speaker') or '').strip()[:64] or None
    notes = (body.get('notes') or '').strip()[:2000] or None
    reviewer = user['username'] if action != 'reset' else None
    db.run("""UPDATE clips SET final_text = ?, status = ?, reviewed_by = ?, reviewed_at = ?, speaker = ?,
                pleasure = ?, arousal = ?, dominance = ?, notes = ? WHERE id = ?""",
           text, status, reviewer, now() if reviewer else None, speaker,
           pad['pleasure'], pad['arousal'], pad['dominance'], notes, clip_id)
    db.audit(user['username'], f'review:{action}', clip_id)
    return db.one('SELECT * FROM clips WHERE id = ?', clip_id)

  # ---------- stats & export ----------

  def video_stats(video_id):
    clips = db.all('SELECT status, model_text, final_text FROM clips WHERE video_id = ?', video_id)
    counts = {s: 0 for s in ('pending', 'transcribed', 'approved', 'corrected', 'rejected')}
    for c in clips:
      counts[c['status']] += 1
    pairs = [(c['final_text'], c['model_text']) for c in clips
             if c['status'] in ('approved', 'corrected') and c['model_text'] is not None]
    return {'total': len(clips), **counts, 'model_wer': corpus_wer(pairs), 'wer_clips': len(pairs),
            'pad_scale': [PAD_MIN, PAD_MAX]}

  @app.get('/api/videos/{video_id}/export')
  def export(video_id: str, user=Depends(admin_user)):
    """NeMo-style manifest of the human-validated clips, one JSON object per line.

    audio_filepath is the storage key; download the clips with the same layout
    (e.g. `azcopy copy` of the videos/<id>/clips folder) and the manifest works as is.
    """
    video = get_video(video_id)
    clips = db.all("""SELECT * FROM clips WHERE video_id = ? AND status IN ('approved', 'corrected')
                      ORDER BY idx""", video_id)
    lines = [json.dumps({
      'audio_filepath': c['clip_key'],
      'duration': round(c['end'] - c['start'], 3),
      'text': c['final_text'],
      'offset_in_video': c['start'],
      'end_in_video': c['end'],
      'video_id': video_id,
      'source_file': video['original_filename'],
      'speaker': c['speaker'],
      'pleasure': c['pleasure'],
      'arousal': c['arousal'],
      'dominance': c['dominance'],
      'notes': c['notes'],
      'source': c['source'],
      'status': c['status'],
      'model_text': c['model_text'],
      'reviewed_by': c['reviewed_by'],
    }, ensure_ascii=False) for c in clips]
    db.audit(user['username'], 'export', video_id)
    return PlainTextResponse('\n'.join(lines) + ('\n' if lines else ''), media_type='application/x-ndjson',
                             headers={'Content-Disposition': f'attachment; filename="{video_id}_manifest.jsonl"'})

  @app.get('/api/audit')
  def audit_log(limit: int = 200, user=Depends(admin_user)):
    return db.all('SELECT * FROM audit_log ORDER BY id DESC LIMIT ?', min(limit, 5000))

  # ---------- media (always through the app, never a public URL) ----------

  def stream(request: Request, key, content_type):
    try:
      size = storage.size(key)
    except Exception:
      raise HTTPException(404, 'media not found')
    start, end, status = 0, size - 1, 200
    range_header = request.headers.get('range')
    if range_header and range_header.startswith('bytes='):
      first, _, last = range_header[6:].split(',')[0].partition('-')
      try:
        if first:
          start, end = int(first), int(last) if last else size - 1
        else:
          start, end = max(0, size - int(last)), size - 1
      except ValueError:
        raise HTTPException(416, 'bad range')
      end = min(end, size - 1)
      if start > end:
        return PlainTextResponse('', status_code=416, headers={'Content-Range': f'bytes */{size}'})
      status = 206
    headers = {'Accept-Ranges': 'bytes', 'Content-Length': str(end - start + 1), 'Cache-Control': 'private, no-store'}
    if status == 206:
      headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    return StreamingResponse(storage.read_range(key, start, end - start + 1), status_code=status,
                             media_type=content_type, headers=headers)

  @app.get('/media/videos/{video_id}')
  def media_video(video_id: str, request: Request, user=Depends(current_user)):
    video = get_video(video_id)
    key = video['playback_key'] or video['video_key']
    return stream(request, key, VIDEO_CONTENT_TYPES.get(Path(key).suffix, 'application/octet-stream'))

  @app.get('/media/clips/{clip_id}')
  def media_clip(clip_id: str, request: Request, user=Depends(current_user)):
    clip = db.one('SELECT clip_key FROM clips WHERE id = ?', clip_id)
    if not clip:
      raise HTTPException(404, 'clip not found')
    return stream(request, clip['clip_key'], 'audio/wav')

  return app

