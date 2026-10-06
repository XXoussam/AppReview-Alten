"""Pluggable ASR backends for the 'Transcribe' button.

'none'      no model in the app; push transcripts from your own job through
            PUT /api/clips/{id}/model-transcript (bearer token) instead.
'parakeet'  loads a Hugging Face ASR pipeline in-process (needs torch +
            transformers on the VM, a GPU if you want it fast). Point
            REVIEW_ASR_MODEL at your fine-tuned checkpoint when it is ready.
'api'       sends the clips to the fine-tuned Parakeet HTTP server
            (transcribe_server.py in the FineTune repo) at REVIEW_ASR_URL.

To add your own model, subclass Transcriber and register it in build_transcriber.
transcribe() returns one text per path, or None for a clip the model could not read.
"""

import json
import logging
import mimetypes
import secrets
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


class Transcriber:
  name = 'none'

  def transcribe(self, wav_paths):
    raise NotImplementedError('No transcriber configured (REVIEW_TRANSCRIBER=none).')


class HuggingFaceTranscriber(Transcriber):
  def __init__(self, model_name):
    self.name = model_name
    self._pipe = None

  def _pipeline(self):
    if self._pipe is None:
      import torch
      from transformers import pipeline

      device = 0 if torch.cuda.is_available() else -1
      self._pipe = pipeline('automatic-speech-recognition', model=self.name, device=device)
    return self._pipe

  def transcribe(self, wav_paths):
    pipe = self._pipeline()
    texts = []
    for path in wav_paths:
      result = pipe(str(path))
      texts.append((result.get('text', '') if isinstance(result, dict) else str(result)).strip())
    return texts


class ParakeetApiTranscriber(Transcriber):
  """Client of the Parakeet server's POST /transcribe/batch.

  The server handles one request at a time on the GPU and loads a model on first use
  (~30 s), so the timeout is long. Clips are already <= 20 s, the server's chunk size.
  """

  def __init__(self, url, model, timeout=900):
    self.url = url.rstrip('/')
    self.model = model
    self.timeout = timeout
    self.name = f'parakeet-api:{model}'

  def transcribe(self, wav_paths):
    boundary = secrets.token_hex(16)
    body = bytearray()
    for path in wav_paths:
      content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
      body += (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{path.name}"\r\n'
               f'Content-Type: {content_type}\r\n\r\n').encode()
      body += path.read_bytes() + b'\r\n'
    for name, value in (('model', self.model), ('timestamps', 'false')):
      body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    body += f'--{boundary}--\r\n'.encode()

    request = urllib.request.Request(f'{self.url}/transcribe/batch', data=bytes(body), method='POST',
                                     headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
      with urllib.request.urlopen(request, timeout=self.timeout) as response:
        results = json.load(response)['results']
    except urllib.error.HTTPError as exc:
      try:
        detail = json.load(exc).get('detail', exc.reason)
      except ValueError:
        detail = exc.reason
      raise RuntimeError(f'ASR server at {self.url} answered {exc.code}: {detail}') from None
    except urllib.error.URLError as exc:
      raise RuntimeError(f'ASR server unreachable at {self.url} ({exc.reason}); '
                         'start it with Start-Asr-Api.ps1 in the FineTune repo') from None

    texts = []
    for path, result in zip(wav_paths, results):
      if 'error' in result:
        # Log the file name only: the audio and transcripts are personal data.
        logger.warning('ASR server could not transcribe %s: %s', path.name, result['error'])
        texts.append(None)
      else:
        texts.append(result.get('text', '').strip())
    return texts


def build_transcriber(settings):
  if settings.transcriber == 'none':
    return Transcriber()
  if settings.transcriber == 'parakeet':
    return HuggingFaceTranscriber(settings.asr_model)
  if settings.transcriber == 'api':
    return ParakeetApiTranscriber(settings.asr_url, settings.asr_api_model)
  raise ValueError(f'unknown REVIEW_TRANSCRIBER: {settings.transcriber}')
