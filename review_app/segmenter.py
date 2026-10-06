"""Audio extraction and clip cutting with ffmpeg.

All clip boundaries are expressed in seconds from the start of the original
video. The extracted audio is padded at the front if needed
(aresample first_pts=0) so that second 0 of the WAV is second 0 of the video,
which keeps every clip's start/end directly usable to seek the video player.

Clips are 16 kHz mono WAV, the input format Parakeet / NeMo expect, and are
cut from that WAV with sample-accurate seeking (unlike `-c copy` on mp3 in
Esports-Event-to-Commentary-Generation-LoL/data/scripts/segment_audio.py, which snaps to frame boundaries).
"""

import json
import math
import re
import subprocess

SAMPLE_RATE = 16000


def run(cmd):
  result = subprocess.run(cmd, capture_output=True, text=True)
  if result.returncode != 0:
    raise RuntimeError(f"{cmd[0]} failed: {result.stderr.strip()[-2000:]}")
  return result


def probe_duration(path):
  out = run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)]).stdout
  return float(json.loads(out)['format']['duration'])


def has_audio(path):
  out = run(['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index',
             '-of', 'json', str(path)]).stdout
  return bool(json.loads(out).get('streams'))


def make_playable(video_path, out_path):
  """MP4 copy for the browser player when the upload is MKV/AVI/TS/FLV (e.g. OBS recordings).

  Tries a lossless remux first and only re-encodes if the codecs don't fit in MP4.
  Timestamps are kept, so clip times stay valid in the playable copy.
  """
  try:
    run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(video_path), '-map', '0:v:0?', '-map', '0:a:0?',
         '-c', 'copy', '-movflags', '+faststart', str(out_path)])
  except RuntimeError:
    run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(video_path), '-map', '0:v:0?', '-map', '0:a:0?',
         '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26', '-pix_fmt', 'yuv420p',
         '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(out_path)])


def extract_audio(video_path, wav_path):
  run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(video_path), '-vn',
       '-af', 'aresample=async=1:first_pts=0', '-ac', '1', '-ar', str(SAMPLE_RATE),
       '-c:a', 'pcm_s16le', str(wav_path)])


def convert_clip(src_path, out_path):
  """Any audio file -> 16 kHz mono WAV, the format every clip is stored in."""
  run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(src_path), '-vn', '-ac', '1', '-ar', str(SAMPLE_RATE),
       '-c:a', 'pcm_s16le', str(out_path)])


def cut_clip(wav_path, start, end, out_path):
  run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(wav_path), '-ss', f'{start:.3f}', '-to', f'{end:.3f}',
       '-c:a', 'pcm_s16le', str(out_path)])


def detect_silences(wav_path, noise_db=-35.0, min_silence=0.5):
  """Return [(silence_start, silence_end), ...] using ffmpeg's silencedetect."""
  result = run(['ffmpeg', '-hide_banner', '-nostats', '-i', str(wav_path),
                '-af', f'silencedetect=noise={noise_db}dB:d={min_silence}', '-f', 'null', '-'])
  silences, start = [], None
  for line in result.stderr.splitlines():
    m = re.search(r'silence_start: (-?[\d.]+)', line)
    if m:
      start = max(0.0, float(m.group(1)))
      continue
    m = re.search(r'silence_end: ([\d.]+)', line)
    if m and start is not None:
      silences.append((start, float(m.group(1))))
      start = None
  if start is not None:
    silences.append((start, float('inf')))
  return silences


def split_long(segments, max_len):
  out = []
  for start, end in segments:
    n = max(1, math.ceil((end - start) / max_len))
    step = (end - start) / n
    out.extend((start + i * step, start + (i + 1) * step) for i in range(n))
  return out


def segments_from_silence(wav_path, duration, noise_db=-35.0, min_silence=0.5,
                          min_len=0.25, max_len=20.0, pad=0.15):
  """Speech regions between silences: one clip per utterance, which suits voice comms."""
  speech, cursor = [], 0.0
  for s_start, s_end in detect_silences(wav_path, noise_db, min_silence):
    if s_start > cursor:
      speech.append((cursor, s_start))
    cursor = min(s_end, duration)
  if cursor < duration:
    speech.append((cursor, duration))

  padded = [(max(0.0, s - pad), min(duration, e + pad)) for s, e in speech if e - s >= min_len]
  # Padding can make neighbours overlap; merge those so clips never overlap.
  merged = []
  for s, e in padded:
    if merged and s <= merged[-1][1]:
      merged[-1] = (merged[-1][0], max(merged[-1][1], e))
    else:
      merged.append((s, e))
  return [(s, e, None) for s, e in split_long(merged, max_len)]


def segments_fixed(duration, length=10.0):
  return [(s, e, None) for s, e in split_long([(0.0, duration)], length)]


def segments_from_captions(captions, duration):
  """Same rule as segment_audio.py in Esports-Event-to-Commentary-Generation-LoL/data/scripts: a caption ends where the next one starts."""
  captions = sorted(captions, key=lambda c: c['start'])
  out = []
  for i, cap in enumerate(captions):
    start = float(cap['start'])
    end = float(captions[i + 1]['start']) if i + 1 < len(captions) else start + float(cap.get('duration', 0))
    end = min(end, duration)
    text = (cap.get('text') or '').strip()
    if text and end > start:
      out.append((start, end, text))
  return out
