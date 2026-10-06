"""Import of per-player clips produced by voice separation.

Expected input: one ZIP holding the audio clips and a manifest at its root,
`manifest.csv`, `manifest.jsonl` or `manifest.json`, with one row per clip:

  player  name of the player speaking (one timeline lane per player)
  start   where the clip starts in the ORIGINAL video: seconds (83.42) or [hh:]mm:ss(.ms) (01:23.42)
  end     where it ends, same format (optional: the clip's own duration is used if missing)
  file    path of the audio file inside the ZIP (any format ffmpeg reads)
  text    optional reference transcript

See review_app/README.md, "Importing separated voices".
"""

import csv
import io
import json
import re
import zipfile
from pathlib import PurePosixPath

MANIFEST_NAMES = ('manifest.csv', 'manifest.jsonl', 'manifest.json')
MAX_FILES = 20000


class ImportError_(ValueError):
  pass


def parse_time(value):
  if value is None or str(value).strip() == '':
    return None
  if isinstance(value, (int, float)):
    return float(value)
  text = str(value).strip().replace(',', '.')
  if re.fullmatch(r'\d+(\.\d+)?', text):
    return float(text)
  m = re.fullmatch(r'(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)', text)
  if not m:
    raise ImportError_(f'unreadable time "{value}" (use seconds or [hh:]mm:ss.ms)')
  hours, minutes, seconds = m.groups()
  return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def _rows(name, raw):
  text = raw.decode('utf-8-sig')
  if name.endswith('.csv'):
    sample = text[:2048]
    dialect = csv.Sniffer().sniff(sample, delimiters=',;\t') if sample.strip() else csv.excel
    return list(csv.DictReader(io.StringIO(text), dialect=dialect))
  if name.endswith('.jsonl'):
    return [json.loads(line) for line in text.splitlines() if line.strip()]
  data = json.loads(text)
  return data['clips'] if isinstance(data, dict) else data


def read_manifest(zf):
  """Return [{'player', 'start', 'end' (or None), 'member', 'text'}] sorted by start."""
  names = {PurePosixPath(n).as_posix(): n for n in zf.namelist() if not n.endswith('/')}
  if len(names) > MAX_FILES:
    raise ImportError_(f'too many files in the ZIP (max {MAX_FILES})')

  # The manifest may sit at the root or inside a single top-level folder.
  manifest = next((n for n in names if PurePosixPath(n).name.lower() in MANIFEST_NAMES
                   and len(PurePosixPath(n).parts) <= 2), None)
  if not manifest:
    raise ImportError_('no manifest.csv / manifest.jsonl / manifest.json in the ZIP')
  base = PurePosixPath(manifest).parent

  try:
    rows = _rows(manifest.lower(), zf.read(names[manifest]))
  except (ValueError, KeyError, csv.Error) as exc:
    raise ImportError_(f'cannot read {manifest}: {exc}')

  clips = []
  for line, row in enumerate(rows, start=2 if manifest.lower().endswith('.csv') else 1):
    row = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    where = f'{PurePosixPath(manifest).name} row {line}'
    player = str(row.get('player') or row.get('speaker') or '').strip()
    file_ = str(row.get('file') or row.get('audio_filepath') or row.get('clip') or '').strip()
    if not player or not file_:
      raise ImportError_(f'{where}: "player" and "file" are required')
    start = parse_time(row.get('start'))
    if start is None or start < 0:
      raise ImportError_(f'{where}: "start" is required and must be >= 0')
    end = parse_time(row.get('end'))
    if end is not None and end <= start:
      raise ImportError_(f'{where}: "end" must be after "start"')
    member = (base / file_).as_posix() if str(base) != '.' else PurePosixPath(file_).as_posix()
    if member not in names:
      raise ImportError_(f'{where}: file "{file_}" is not in the ZIP')
    text = str(row.get('text') or '').strip() or None
    clips.append({'player': player[:64], 'start': start, 'end': end, 'member': names[member], 'text': text})

  if not clips:
    raise ImportError_('the manifest lists no clips')
  return sorted(clips, key=lambda c: (c['start'], c['player']))


def check_zip_size(zf, limit_bytes):
  total = sum(info.file_size for info in zf.infolist())
  if total > limit_bytes:
    raise ImportError_(f'the ZIP unpacks to {total // 2**20} MB, over the {limit_bytes // 2**20} MB limit')
