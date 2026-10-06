"""Accounts are kept in a small JSON file on the VM with salted PBKDF2 hashes.

Manage them with `python -m review_app.manage add-user <name> --role admin|reviewer`.
Admins can upload, delete and export; reviewers can only listen and validate.
"""

import hashlib
import hmac
import json
import secrets

ITERATIONS = 600_000
ROLES = ('admin', 'reviewer')


def hash_password(password, salt=None):
  salt = salt or secrets.token_hex(16)
  digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
  return f'pbkdf2_sha256${ITERATIONS}${salt}${digest}'


def verify_password(password, stored):
  try:
    _, iterations, salt, digest = stored.split('$')
  except ValueError:
    return False
  candidate = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(iterations)).hex()
  return hmac.compare_digest(candidate, digest)


class UserStore:
  def __init__(self, path):
    self.path = path

  def _load(self):
    if not self.path.exists():
      return {}
    return json.loads(self.path.read_text(encoding='utf-8'))

  def _save(self, users):
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self.path.write_text(json.dumps(users, indent=2), encoding='utf-8')
    self.path.chmod(0o600)

  def set_user(self, username, password, role):
    if role not in ROLES:
      raise ValueError(f'role must be one of {ROLES}')
    users = self._load()
    users[username] = {'password': hash_password(password), 'role': role}
    self._save(users)

  def remove_user(self, username):
    users = self._load()
    users.pop(username, None)
    self._save(users)

  def list_users(self):
    return {name: u['role'] for name, u in self._load().items()}

  def authenticate(self, username, password):
    user = self._load().get(username)
    if user and verify_password(password, user['password']):
      return {'username': username, 'role': user['role']}
    # Spend the same time on unknown users so usernames can't be probed by timing.
    verify_password(password, hash_password('x'))
    return None
