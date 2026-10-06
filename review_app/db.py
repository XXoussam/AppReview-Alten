"""SQLite schema and small query helpers. One file on the VM disk, no server to run."""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  original_filename TEXT NOT NULL,
  video_key TEXT NOT NULL,         -- the file exactly as uploaded
  playback_key TEXT,               -- what the browser plays (same as video_key for MP4/WebM)
  audio_key TEXT,
  duration REAL,
  segmentation TEXT NOT NULL,
  players TEXT NOT NULL DEFAULT '[]',  -- JSON list of player names for the speaker picker
  status TEXT NOT NULL,          -- uploaded | processing | ready | transcribing | failed
  error TEXT,
  uploaded_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- start/end are seconds from the start of the ORIGINAL video, so a clip can
-- always be traced back to (and replayed in) the source video.
CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  start REAL NOT NULL,
  "end" REAL NOT NULL,
  clip_key TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'auto',  -- auto (cut by the app) | separated (imported per-player clip)
  reference_text TEXT,           -- e.g. text from an uploaded caption file
  model_text TEXT,               -- output of the ASR model
  model_name TEXT,
  final_text TEXT,               -- what the human validated
  speaker TEXT,                  -- player talking in this clip
  pleasure INTEGER,              -- PAD annotation, 1..7 (4 = neutral)
  arousal INTEGER,
  dominance INTEGER,
  notes TEXT,
  status TEXT NOT NULL,          -- pending | transcribed | approved | corrected | rejected
  reviewed_by TEXT,
  reviewed_at TEXT,
  UNIQUE (video_id, idx)
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  username TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT
);
"""

# Columns added after the first release; ALTERed into databases created before them.
MIGRATIONS = [
  ('clips', 'source', "TEXT NOT NULL DEFAULT 'auto'"),
]


def now():
  return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Database:
  def __init__(self, path):
    self.path = path
    self._lock = threading.Lock()
    path.parent.mkdir(parents=True, exist_ok=True)
    with self.connect() as conn:
      conn.executescript(SCHEMA)
      for table, column, definition in MIGRATIONS:
        columns = {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}
        if column not in columns:
          conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')

  @contextmanager
  def connect(self):
    # A short-lived connection per operation keeps this safe to use from the
    # request threads and the background processing thread at the same time.
    with self._lock:
      conn = sqlite3.connect(self.path)
      conn.row_factory = sqlite3.Row
      conn.execute('PRAGMA foreign_keys = ON')
      try:
        yield conn
        conn.commit()
      finally:
        conn.close()

  def one(self, sql, *params):
    with self.connect() as conn:
      row = conn.execute(sql, params).fetchone()
      return dict(row) if row else None

  def all(self, sql, *params):
    with self.connect() as conn:
      return [dict(r) for r in conn.execute(sql, params).fetchall()]

  def run(self, sql, *params):
    with self.connect() as conn:
      conn.execute(sql, params)

  def run_many(self, sql, rows):
    with self.connect() as conn:
      conn.executemany(sql, rows)

  def audit(self, username, action, target=None):
    self.run('INSERT INTO audit_log (at, username, action, target) VALUES (?, ?, ?, ?)',
             now(), username, action, target)
