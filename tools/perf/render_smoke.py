#!/usr/bin/env python3
"""Render every UI page as an authenticated admin in en and zh_Hans.

Catches Jinja gettext interpolation crashes (%-format KeyError/ValueError)
and any template syntax regressions introduced by the i18n extraction.
"""
import sys

sys.path.insert(0, '/home/user/AeroFoil')

from app.app import app
from app.auth import create_or_update_user

PAGES = ['/', '/settings', '/manage', '/downloads', '/upload', '/activity',
         '/users', '/requests', '/saves', '/login']

failures = []
from app.db import db
import app.app as app_module

app_module.reload_conf()

with app.app_context():
    db.create_all()
    create_or_update_user('smoketest_admin', 'smoketest_pw_123', admin_access=True,
                          shop_access=True, backup_access=True)

client = app.test_client()
resp = client.post('/login', data={'user': 'smoketest_admin', 'password': 'smoketest_pw_123'},
                   follow_redirects=True)
print('login status:', resp.status_code)

for lang in ('en', 'zh_Hans'):
    client.set_cookie('aerofoil_lang', lang)
    for page in PAGES:
        try:
            r = client.get(page, follow_redirects=True)
            body = r.get_data(as_text=True)
            ok = r.status_code == 200
            # login-protected pages that bounced back to /login are a failure too
            if page not in ('/login',) and 'name="password"' in body and page != '/':
                ok = False
                note = 'redirected to login'
            else:
                note = ''
            status = 'OK ' if ok else 'FAIL'
            if not ok:
                failures.append((lang, page, r.status_code, note))
            print(f"{status} [{lang}] {page} -> {r.status_code} {note}")
        except Exception as e:
            failures.append((lang, page, 'EXC', repr(e)))
            print(f"FAIL [{lang}] {page} -> EXCEPTION {e!r}")

if failures:
    print('\nFAILURES:', failures)
    sys.exit(1)
print('\nall pages rendered cleanly in en and zh_Hans')
