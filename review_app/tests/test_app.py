"""End-to-end test on a generated video: upload, cut, review, export.

  pip install -r review_app/requirements.txt pytest httpx
  pytest review_app/tests
"""

import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from review_app.app import create_app
from review_app.auth import UserStore
from review_app.config import Settings

# Speech-like bursts (seconds, in the original video) separated by silence.
BURSTS = [(1.0, 2.5), (4.0, 6.0), (8.0, 9.0)]


@pytest.fixture
def sample_video(tmp_path):
  path = tmp_path / 'game.mp4'
  gate = '+'.join(f'between(t,{s},{e})' for s, e in BURSTS)
  subprocess.run([
    'ffmpeg', '-y', '-loglevel', 'error',
    '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=25:duration=11',
    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=11',
    '-af', f"volume=volume=0:enable='not({gate})'",
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)], check=True)
  return path


@pytest.fixture
def client(tmp_path):
  settings = Settings(data_dir=tmp_path / 'data', secret_key='test', api_token='tok')
  UserStore(settings.users_path).set_user('admin', 'correct horse battery', 'admin')
  UserStore(settings.users_path).set_user('rev', 'correct horse battery', 'reviewer')
  with TestClient(create_app(settings)) as c:
    yield c


def login(client, user):
  r = client.post('/login', data={'username': user, 'password': 'correct horse battery'}, follow_redirects=False)
  assert r.status_code == 303 and r.headers['location'] == '/'


def wait_ready(client, video_id):
  for _ in range(100):
    video = client.get(f'/api/videos/{video_id}').json()
    if video['status'] != 'processing':
      return video
    time.sleep(0.1)
  raise AssertionError('processing did not finish')


def test_requires_login(client):
  assert client.get('/api/videos').status_code == 401
  assert client.get('/', follow_redirects=False).headers['location'] == '/login'
  assert client.post('/login', data={'username': 'admin', 'password': 'nope'}, follow_redirects=False) \
    .headers['location'].startswith('/login?error')


def test_upload_cut_review_export(client, sample_video):
  login(client, 'admin')
  with open(sample_video, 'rb') as f:
    r = client.post('/api/videos', files={'file': ('game.mp4', f, 'video/mp4')},
                    data={'title': 'Scrim G1', 'players': 'Enjawve, Kaylem', 'segmentation': 'silence'})
  assert r.status_code == 202, r.text
  video_id = r.json()['id']
  video = wait_ready(client, video_id)
  assert video['status'] == 'ready', video.get('error')

  clips = client.get(f'/api/videos/{video_id}/clips').json()
  assert len(clips) == len(BURSTS)
  for clip, (start, end) in zip(clips, BURSTS):
    # Clips are timestamped in the original video (0.15 s padding, aac priming tolerance).
    assert abs(clip['start'] - (start - 0.15)) < 0.12, clip
    assert abs(clip['end'] - (end + 0.15)) < 0.12, clip

  # Clip audio is served through the app, with range requests for seeking.
  r = client.get(f'/media/clips/{clips[0]["id"]}', headers={'Range': 'bytes=0-99'})
  assert r.status_code == 206 and len(r.content) == 100 and r.content[:4] == b'RIFF'
  assert client.get(f'/media/videos/{video_id}').status_code == 200

  # An external ASR job pushes model output with the API token.
  for clip, text in zip(clips, ['push mid now', 'baron is up', 'go go go']):
    r = client.put(f'/api/clips/{clip["id"]}/model-transcript', json={'text': text, 'model': 'parakeet-ft'},
                   headers={'Authorization': 'Bearer tok'})
    assert r.status_code == 200

  login(client, 'rev')
  r = client.post(f'/api/clips/{clips[0]["id"]}/review',
                  json={'action': 'approve', 'speaker': 'Enjawve', 'pleasure': 5, 'arousal': 6, 'dominance': 4})
  assert r.json()['status'] == 'approved' and r.json()['final_text'] == 'push mid now'
  r = client.post(f'/api/clips/{clips[1]["id"]}/review',
                  json={'action': 'correct', 'text': 'baron is up go', 'speaker': 'Kaylem',
                        'pleasure': 4, 'arousal': 7, 'dominance': 6, 'notes': 'shouting'})
  assert r.json()['status'] == 'corrected'
  client.post(f'/api/clips/{clips[2]["id"]}/review', json={'action': 'reject'})
  assert client.post(f'/api/clips/{clips[0]["id"]}/review', json={'action': 'approve', 'pleasure': 9}).status_code == 400
  # Reviewers can't export or delete.
  assert client.get(f'/api/videos/{video_id}/export').status_code == 403
  assert client.delete(f'/api/videos/{video_id}').status_code == 403

  login(client, 'admin')
  stats = client.get(f'/api/videos/{video_id}').json()['stats']
  assert stats['approved'] == 1 and stats['corrected'] == 1 and stats['rejected'] == 1
  assert stats['model_wer'] == pytest.approx(1 / 7, abs=1e-3)  # one missing word over 7 reference words

  lines = [json.loads(l) for l in client.get(f'/api/videos/{video_id}/export').text.splitlines()]
  assert [l['text'] for l in lines] == ['push mid now', 'baron is up go']
  assert lines[1]['speaker'] == 'Kaylem' and lines[1]['arousal'] == 7
  assert lines[0]['offset_in_video'] == clips[0]['start']

  assert client.delete(f'/api/videos/{video_id}').status_code == 200
  assert client.get(f'/api/videos/{video_id}').status_code == 404
  actions = [a['action'] for a in client.get('/api/audit').json()]
  assert 'delete' in actions and 'review:correct' in actions


def test_cross_site_post_refused(client):
  login(client, 'admin')
  r = client.post('/api/videos/x/transcribe', headers={'Origin': 'https://evil.example'})
  assert r.status_code == 403


def test_mkv_gets_browser_playable_copy(client, sample_video, tmp_path):
  mkv = tmp_path / 'obs.mkv'
  subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(sample_video), '-c', 'copy', str(mkv)], check=True)
  login(client, 'admin')
  with open(mkv, 'rb') as f:
    video_id = client.post('/api/videos', files={'file': ('obs.mkv', f)}, data={'segmentation': 'fixed'}).json()['id']
  video = wait_ready(client, video_id)
  assert video['status'] == 'ready', video.get('error')
  assert video['video_key'].endswith('original.mkv') and video['playback_key'].endswith('playback.mp4')
  r = client.get(f'/media/videos/{video_id}', headers={'Range': 'bytes=0-15'})
  assert r.status_code == 206 and r.headers['content-type'] == 'video/mp4' and b'ftyp' in r.content
  assert len(client.get(f'/api/videos/{video_id}/clips').json()) == 2  # 11 s in 10 s windows -> 2 x 5.5 s


def test_captions_mode_keeps_caption_text(client, sample_video):
  login(client, 'admin')
  captions = json.dumps([{'start': 1.0, 'duration': 2, 'text': 'push mid'}, {'start': 4.0, 'duration': 2, 'text': 'baron'}])
  with open(sample_video, 'rb') as f:
    video_id = client.post('/api/videos', files={'file': ('g.mp4', f), 'captions': ('c.json', captions)},
                           data={'segmentation': 'captions'}).json()['id']
  assert wait_ready(client, video_id)['status'] == 'ready'
  clips = client.get(f'/api/videos/{video_id}/clips').json()
  assert [(c['start'], c['end'], c['reference_text']) for c in clips] == [(1.0, 4.0, 'push mid'), (4.0, 6.0, 'baron')]


def make_tone(path, seconds, freq=440):
  subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                  f'sine=frequency={freq}:duration={seconds}', str(path)], check=True)


def separated_zip(tmp_path, rows, manifest_name='manifest.csv'):
  """rows = [(player, start, end, seconds_of_audio)] -> ZIP like the voice-separation output."""
  import zipfile
  zpath = tmp_path / 'separated.zip'
  lines = ['player,start,end,file,text']
  with zipfile.ZipFile(zpath, 'w') as zf:
    for n, (player, start, end, seconds) in enumerate(rows):
      wav = tmp_path / f'{player}_{n}.wav'
      make_tone(wav, seconds, 300 + 100 * n)
      zf.write(wav, f'{player}/{n:03d}.wav')
      lines.append(f'{player},{start},{end},{player}/{n:03d}.wav,line {n}')
    zf.writestr(manifest_name, '\n'.join(lines) + '\n')
  return zpath


def wait_idle(client, video_id):
  for _ in range(100):
    video = client.get(f'/api/videos/{video_id}').json()
    if video['status'] not in ('processing', 'importing'):
      return video
    time.sleep(0.1)
  raise AssertionError('job did not finish')


def test_import_separated_voices(client, sample_video, tmp_path):
  login(client, 'admin')
  with open(sample_video, 'rb') as f:
    video_id = client.post('/api/videos', files={'file': ('g.mp4', f)},
                           data={'segmentation': 'none', 'players': 'Enjawve'}).json()['id']
  assert wait_idle(client, video_id)['status'] == 'ready'
  assert client.get(f'/api/videos/{video_id}/clips').json() == []

  # Overlapping speech from two players, times in seconds and in mm:ss.ms.
  zpath = separated_zip(tmp_path, [('Kaylem', '00:04.50', '00:06.00', 1.5), ('Enjawve', 1.0, 2.5, 1.5),
                                   ('Enjawve', '5.0', '', 1.0)])
  with open(zpath, 'rb') as f:
    r = client.post(f'/api/videos/{video_id}/import', files={'file': ('s.zip', f, 'application/zip')})
  assert r.status_code == 202, r.text
  assert r.json() == {'status': 'importing', 'clips': 3, 'players': ['Enjawve', 'Kaylem']}
  video = wait_idle(client, video_id)
  assert video['status'] == 'ready' and not video['error'], video['error']
  assert json.loads(video['players']) == ['Enjawve', 'Kaylem']

  clips = client.get(f'/api/videos/{video_id}/clips').json()
  # Chronological, placed exactly where the manifest says, overlaps kept, end defaults to start + audio length.
  assert [(c['idx'], c['speaker'], c['start'], c['end'], c['source']) for c in clips] == [
    (0, 'Enjawve', 1.0, 2.5, 'separated'), (1, 'Kaylem', 4.5, 6.0, 'separated'), (2, 'Enjawve', 5.0, 6.0, 'separated')]
  assert clips[0]['reference_text'] == 'line 1'
  r = client.get(f'/media/clips/{clips[1]["id"]}')
  assert r.status_code == 200 and r.content[:4] == b'RIFF'

  # The voice-separation job can also push with the API token; replace drops the old clips and their files.
  zpath = separated_zip(tmp_path, [('Ryuk', 7.0, 8.0, 1.0)], 'manifest.jsonl')
  zpath.unlink()
  import zipfile
  with zipfile.ZipFile(zpath, 'w') as zf:
    make_tone(tmp_path / 'r.wav', 1.0)
    zf.write(tmp_path / 'r.wav', 'out/ryuk_0.wav')
    zf.writestr('out/manifest.jsonl', json.dumps({'player': 'Ryuk', 'start': 7.0, 'end': 8.0, 'file': 'ryuk_0.wav'}))
  client.cookies.clear()
  with open(zpath, 'rb') as f:
    r = client.post(f'/api/videos/{video_id}/import', files={'file': ('s.zip', f)}, data={'replace': 'true'},
                    headers={'Authorization': 'Bearer tok'})
  assert r.status_code == 202, r.text
  login(client, 'admin')
  wait_idle(client, video_id)
  clips = client.get(f'/api/videos/{video_id}/clips').json()
  assert [(c['speaker'], c['start']) for c in clips] == [('Ryuk', 7.0)]
  assert client.get(f'/media/clips/{clips[0]["id"]}').status_code == 200


def test_import_rejects_bad_manifest(client, sample_video, tmp_path):
  import zipfile
  login(client, 'admin')
  with open(sample_video, 'rb') as f:
    video_id = client.post('/api/videos', files={'file': ('g.mp4', f)}, data={'segmentation': 'none'}).json()['id']
  wait_idle(client, video_id)
  cases = {
    'no manifest': ({'a.wav': b'x'}, 'no manifest'),
    'missing file': ({'manifest.csv': 'player,start,end,file\nA,1,2,nope.wav\n'}, 'not in the ZIP'),
    'bad time': ({'manifest.csv': 'player,start,end,file\nA,1:2:3:4,2,a.wav\n', 'a.wav': b'x'}, 'unreadable time'),
    'end before start': ({'manifest.csv': 'player,start,end,file\nA,3,2,a.wav\n', 'a.wav': b'x'}, 'after "start"'),
  }
  for name, (files, expected) in cases.items():
    zpath = tmp_path / 'bad.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
      for member, data in files.items():
        zf.writestr(member, data)
    with open(zpath, 'rb') as f:
      r = client.post(f'/api/videos/{video_id}/import', files={'file': ('bad.zip', f)})
    assert r.status_code == 400 and expected in r.json()['detail'], (name, r.text)
  r = client.post(f'/api/videos/{video_id}/import', files={'file': ('x.zip', b'not a zip')})
  assert r.status_code == 400
  login(client, 'rev')
  assert client.post(f'/api/videos/{video_id}/import', files={'file': ('x.zip', b'')}).status_code == 403


@pytest.fixture
def fake_asr_server():
  """Stands in for the Parakeet server's POST /transcribe/batch; fails on the second file."""
  import re
  import threading
  from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

  received = {}

  class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
      body = self.rfile.read(int(self.headers['Content-Length']))
      names = re.findall(rb'name="files"; filename="([^"]+)"', body)
      received['path'] = self.path
      received['model'] = re.search(rb'name="model"\r\n\r\n(\w+)', body).group(1).decode()
      received['wav_headers'] = body.count(b'RIFF')
      results = [{'filename': n.decode(), 'error': 'could not decode audio'} if i == 1
                 else {'filename': n.decode(), 'text': f' text {i} '} for i, n in enumerate(names)]
      payload = json.dumps({'model': received['model'], 'device': 'cuda', 'results': results}).encode()
      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.send_header('Content-Length', str(len(payload)))
      self.end_headers()
      self.wfile.write(payload)

    def log_message(self, *args):
      pass

  server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  yield f'http://127.0.0.1:{server.server_port}', received
  server.shutdown()


def test_transcribe_with_parakeet_api(tmp_path, sample_video, fake_asr_server):
  url, received = fake_asr_server
  settings = Settings(data_dir=tmp_path / 'data', secret_key='test', transcriber='api', asr_url=url,
                      asr_api_model='gameaudio')
  UserStore(settings.users_path).set_user('admin', 'correct horse battery', 'admin')
  with TestClient(create_app(settings)) as client:
    login(client, 'admin')
    with open(sample_video, 'rb') as f:
      video_id = client.post('/api/videos', files={'file': ('game.mp4', f, 'video/mp4')},
                             data={'title': 'G1', 'segmentation': 'silence'}).json()['id']
    assert wait_ready(client, video_id)['status'] == 'ready'
    def transcribe(**kwargs):
      assert client.post(f'/api/videos/{video_id}/transcribe', **kwargs).status_code == 202
      for _ in range(100):
        video = client.get(f'/api/videos/{video_id}').json()
        if video['status'] != 'transcribing':
          break
        time.sleep(0.1)
      assert video['status'] == 'ready' and video['error'] is None, video
      return client.get(f'/api/videos/{video_id}/clips').json()

    clips = transcribe()
    assert received == {'path': '/transcribe/batch', 'model': 'gameaudio', 'wav_headers': len(BURSTS)}
    # The clip the server could not decode stays untranscribed instead of failing the whole video.
    assert [c['model_text'] for c in clips] == ['text 0', None, 'text 2']
    assert [c['status'] for c in clips] == ['transcribed', 'pending', 'transcribed']
    assert clips[0]['model_name'] == 'parakeet-api:gameaudio'

    # A selection sends only the checked clips, in one request.
    clips = transcribe(json={'clip_ids': [clips[1]['id']]})
    assert received['wav_headers'] == 1
    assert [c['model_text'] for c in clips] == ['text 0', 'text 0', 'text 2']
    assert client.post(f'/api/videos/{video_id}/transcribe', json={'clip_ids': []}).status_code == 400


def test_parakeet_api_offline_gives_clear_error(tmp_path):
  from review_app.transcriber import ParakeetApiTranscriber

  wav = tmp_path / 'a.wav'
  wav.write_bytes(b'RIFF')
  with pytest.raises(RuntimeError, match='unreachable.*Start-Asr-Api'):
    ParakeetApiTranscriber('http://127.0.0.1:9', 'commentary', timeout=5).transcribe([wav])
