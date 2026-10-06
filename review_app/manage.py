"""Admin CLI.

  python -m review_app.manage add-user alice --role admin
  python -m review_app.manage remove-user alice
  python -m review_app.manage list-users
"""

import argparse
import getpass

from .auth import ROLES, UserStore
from .config import Settings


def main():
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  sub = parser.add_subparsers(dest='command', required=True)
  add = sub.add_parser('add-user', help='create a user or reset their password')
  add.add_argument('username')
  add.add_argument('--role', choices=ROLES, default='reviewer')
  rm = sub.add_parser('remove-user')
  rm.add_argument('username')
  sub.add_parser('list-users')
  args = parser.parse_args()

  store = UserStore(Settings().users_path)
  if args.command == 'add-user':
    password = getpass.getpass('Password: ')
    if len(password) < 12:
      parser.error('use at least 12 characters')
    if password != getpass.getpass('Repeat: '):
      parser.error('passwords do not match')
    store.set_user(args.username, password, args.role)
    print(f'Saved {args.username} ({args.role})')
  elif args.command == 'remove-user':
    store.remove_user(args.username)
    print(f'Removed {args.username}')
  else:
    for name, role in store.list_users().items():
      print(f'{name}\t{role}')


if __name__ == '__main__':
  main()
