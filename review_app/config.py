"""Settings, read once from environment variables (or a .env-style file loaded by the shell).

Every setting has a safe default for local testing; on the VM set at least
REVIEW_SECRET_KEY and, for Azure, REVIEW_STORAGE=azure plus the AZURE_* settings.
"""

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name, default):
  value = os.environ.get(name)
  if value is None:
    return default
  return value.strip().lower() in ('1', 'true', 'yes', 'on')


@dataclass
class Settings:
  data_dir: Path = field(default_factory=lambda: Path(os.environ.get('REVIEW_DATA_DIR', 'review_data')))
  # Signs the session cookie. Must be stable across restarts in production,
  # otherwise everyone gets logged out when the service restarts.
  secret_key: str = field(default_factory=lambda: os.environ.get('REVIEW_SECRET_KEY') or secrets.token_hex(32))
  # Set to true when served over HTTPS (it should be on the VM).
  https_only: bool = field(default_factory=lambda: _bool('REVIEW_HTTPS_ONLY', False))
  session_max_age: int = field(default_factory=lambda: int(os.environ.get('REVIEW_SESSION_MAX_AGE', 8 * 3600)))
  max_upload_mb: int = field(default_factory=lambda: int(os.environ.get('REVIEW_MAX_UPLOAD_MB', 4096)))

  # 'local' keeps files under data_dir/storage, 'azure' uses a private Blob container.
  storage: str = field(default_factory=lambda: os.environ.get('REVIEW_STORAGE', 'local'))
  azure_connection_string: str = field(default_factory=lambda: os.environ.get('AZURE_STORAGE_CONNECTION_STRING', ''))
  # Alternative to the connection string: account URL + the VM's managed identity
  # (no secret stored on disk at all). Requires `pip install azure-identity`.
  azure_account_url: str = field(default_factory=lambda: os.environ.get('AZURE_STORAGE_ACCOUNT_URL', ''))
  azure_container: str = field(default_factory=lambda: os.environ.get('AZURE_STORAGE_CONTAINER', 'voicecomms-review'))

  # 'none' = no model wired yet (clips wait for transcripts pushed through the API),
  # 'parakeet' = run the Hugging Face ASR pipeline inside the app,
  # 'api' = call the fine-tuned Parakeet server (FineTune repo, transcribe_server.py).
  transcriber: str = field(default_factory=lambda: os.environ.get('REVIEW_TRANSCRIBER', 'none'))
  asr_model: str = field(default_factory=lambda: os.environ.get('REVIEW_ASR_MODEL', 'nvidia/parakeet-tdt-0.6b-v3'))
  asr_url: str = field(default_factory=lambda: os.environ.get('REVIEW_ASR_URL', 'http://127.0.0.1:8800'))
  # 'commentary' (casters, lowercase without punctuation) or 'gameaudio' (voice lines, punctuated).
  asr_api_model: str = field(default_factory=lambda: os.environ.get('REVIEW_ASR_API_MODEL', 'commentary'))
  # Bearer token for scripts that push model transcripts (e.g. a GPU job). Empty disables it.
  api_token: str = field(default_factory=lambda: os.environ.get('REVIEW_API_TOKEN', ''))

  @property
  def db_path(self):
    return self.data_dir / 'review.db'

  @property
  def users_path(self):
    return self.data_dir / 'users.json'

  @property
  def work_dir(self):
    return self.data_dir / 'work'
