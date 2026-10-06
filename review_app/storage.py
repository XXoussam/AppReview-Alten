"""Where videos, extracted audio and clips live.

Both backends expose the same small interface and are only ever reached
through the app: nothing is served from a public URL. With Azure the
container must stay private (public access level "Private"); the app
authenticates with a connection string or, better, the VM's managed identity
and streams bytes to logged-in users itself.
"""

import shutil
from pathlib import Path


class Storage:
  def put_file(self, key, local_path, content_type=None):
    raise NotImplementedError

  def get_to_file(self, key, local_path):
    raise NotImplementedError

  def size(self, key):
    raise NotImplementedError

  def read_range(self, key, start, length):
    """Yield the bytes [start, start+length) in chunks."""
    raise NotImplementedError

  def delete_prefix(self, prefix):
    raise NotImplementedError


class LocalStorage(Storage):
  def __init__(self, root):
    self.root = Path(root).resolve()
    self.root.mkdir(parents=True, exist_ok=True)

  def _path(self, key):
    path = (self.root / key).resolve()
    if self.root not in path.parents:
      raise ValueError(f'invalid storage key: {key}')
    return path

  def put_file(self, key, local_path, content_type=None):
    dest = self._path(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(local_path, dest)

  def get_to_file(self, key, local_path):
    shutil.copyfile(self._path(key), local_path)

  def size(self, key):
    return self._path(key).stat().st_size

  def read_range(self, key, start, length, chunk_size=1024 * 1024):
    with open(self._path(key), 'rb') as f:
      f.seek(start)
      remaining = length
      while remaining > 0:
        chunk = f.read(min(chunk_size, remaining))
        if not chunk:
          break
        remaining -= len(chunk)
        yield chunk

  def delete_prefix(self, prefix):
    target = self._path(prefix)
    if target.is_dir():
      shutil.rmtree(target)
    elif target.exists():
      target.unlink()


class AzureBlobStorage(Storage):
  def __init__(self, container, connection_string='', account_url=''):
    from azure.storage.blob import BlobServiceClient

    if connection_string:
      service = BlobServiceClient.from_connection_string(connection_string)
    elif account_url:
      from azure.identity import DefaultAzureCredential
      service = BlobServiceClient(account_url, credential=DefaultAzureCredential())
    else:
      raise ValueError('Set AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL')

    self.container = service.get_container_client(container)
    if not self.container.exists():
      # Created without a public access level, i.e. private.
      self.container.create_container()

  def put_file(self, key, local_path, content_type=None):
    from azure.storage.blob import ContentSettings

    settings = ContentSettings(content_type=content_type) if content_type else None
    with open(local_path, 'rb') as f:
      self.container.upload_blob(key, f, overwrite=True, content_settings=settings, max_concurrency=4)

  def get_to_file(self, key, local_path):
    with open(local_path, 'wb') as f:
      self.container.download_blob(key, max_concurrency=4).readinto(f)

  def size(self, key):
    return self.container.get_blob_client(key).get_blob_properties().size

  def read_range(self, key, start, length):
    yield from self.container.download_blob(key, offset=start, length=length).chunks()

  def delete_prefix(self, prefix):
    for blob in self.container.list_blobs(name_starts_with=prefix):
      self.container.delete_blob(blob.name)


def build_storage(settings):
  if settings.storage == 'azure':
    return AzureBlobStorage(settings.azure_container, settings.azure_connection_string, settings.azure_account_url)
  if settings.storage == 'local':
    return LocalStorage(settings.data_dir / 'storage')
  raise ValueError(f'unknown REVIEW_STORAGE: {settings.storage}')
