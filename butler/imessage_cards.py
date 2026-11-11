"""Optional bearer-link cards; possession is NOT proof of identity.

Only use for explicitly classified non-sensitive choices. Host the WSGI app on a
trusted HTTPS origin, without request-body logging. Keep this feature disabled
until the host and the authenticated Photon /send-app route are configured.
The owner must poll pending() and acknowledge each event after durable handling;
redelivery is intentional until ack().
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from urllib.parse import urlsplit


class CardStore:
    def __init__(self, directory, *, clock=time.time):
        self.clock = clock
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('card storage directory must be private and owned by the current user')
        path = directory / 'cards.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('card database must be a private, owned regular file')
        finally:
            os.close(fd)
        self.path = str(path)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cards (digest TEXT PRIMARY KEY, title TEXT NOT NULL, choices TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL, choice TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0)')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def _digest(token):
        if not isinstance(token, str) or len(token) != 43 or not all(c.isascii() and (c.isalnum() or c in '-_') for c in token):
            raise ValueError('invalid token')
        return hashlib.sha256(token.encode('ascii')).hexdigest()

    def create(self, title, choices, *, ttl, non_sensitive=False):
        if non_sensitive is not True:
            raise ValueError('bearer cards require explicit non-sensitive classification')
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise ValueError('invalid title')
        if not isinstance(choices, list) or not 2 <= len(choices) <= 10 or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in choices):
            raise ValueError('invalid choices')
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 < ttl <= 86400:
            raise ValueError('invalid expiry')
        token = secrets.token_urlsafe(32)
        with self._connect() as db:
            db.execute('INSERT INTO cards (digest,title,choices,expires) VALUES (?,?,?,?)', (self._digest(token), title, json.dumps(choices), self.clock() + ttl))
        return token

    def view(self, token):
        digest = self._digest(token)
        with self._connect() as db:
            row = db.execute('SELECT title,choices,expires,used FROM cards WHERE digest=?', (digest,)).fetchone()
        if not row or row[3] or row[2] <= self.clock():
            return None
        return {'title': row[0], 'choices': json.loads(row[1])}

    def submit(self, token, choice):
        digest = self._digest(token)
        if isinstance(choice, bool) or not isinstance(choice, int):
            raise ValueError('invalid choice')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT choices,expires,used FROM cards WHERE digest=?', (digest,)).fetchone()
            if not row or row[2] or row[1] <= self.clock():
                return False
            choices = json.loads(row[0])
            if not 0 <= choice < len(choices):
                raise ValueError('invalid choice')
            db.execute('UPDATE cards SET used=1 WHERE digest=?', (digest,))
            db.execute('INSERT INTO events (digest,choice) VALUES (?,?)', (digest, choices[choice]))
        return True

    def pending(self):
        with self._connect() as db:
            rows = db.execute('SELECT id,choice,digest FROM events WHERE acknowledged=0 ORDER BY id').fetchall()
        return [{'id': row[0], 'choice': row[1], 'card_id': row[2], 'authentication': 'bearer_only'} for row in rows]

    def ack(self, event_id):
        with self._connect() as db:
            return bool(db.execute('UPDATE events SET acknowledged=1 WHERE id=? AND acknowledged=0', (event_id,)).rowcount)


def card_url(base_url, token):
    CardStore._digest(token)
    if not isinstance(base_url, str) or any(ord(c) <= 32 or ord(c) == 127 or c == '\\' for c in base_url):
        raise ValueError('a clean HTTPS card origin is required')
    url = urlsplit(base_url)
    # Accessing port forces urllib to validate its range and syntax.
    _ = url.port
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('a clean HTTPS card origin is required')
    return base_url.rstrip('/') + '/card#' + token


def send_card(store, sidecar, chat, origin, title, choices, *, ttl, non_sensitive=False):
    """Call an explicitly configured /send-app adapter exactly once.

    A transport acknowledgement is not proof of display or recipient identity.
    No retries: a timeout can occur after delivery. The route is optional and
    must be verified in the deployed sidecar before enabling this integration.
    """
    if not isinstance(chat, str) or not chat or chat != chat.strip():
        raise ValueError('active chat required')
    # Validate origin before persisting a card.
    card_url(origin, 'A' * 43)
    token = store.create(title, choices, ttl=ttl, non_sensitive=non_sensitive)
    identifier = store._digest(token)
    try:
        response = sidecar('/send-app', {'spaceId': chat, 'url': card_url(origin, token)})
    except Exception:
        return {'card_id': identifier, 'delivery': 'unknown'}
    if isinstance(response, dict) and response.get('ok') is True:
        return {'card_id': identifier, 'delivery': 'acknowledged'}
    return {'card_id': identifier, 'delivery': 'unknown'}


_PAGE = b'''<!doctype html><meta name="referrer" content="no-referrer"><title>Card</title>
<h1 id="title">Loading</h1><div id="choices"></div><p id="status"></p>
<script>
(async()=>{const token=location.hash.slice(1);history.replaceState(null,'',location.pathname);
const post=async(path,data)=>fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),cache:'no-store',referrerPolicy:'no-referrer'});
try{const r=await post('/view',{token});if(!r.ok)throw 0;const card=await r.json();
document.getElementById('title').textContent=card.title;
card.choices.forEach((choice,i)=>{const b=document.createElement('button');b.textContent=choice;b.onclick=async()=>{b.disabled=true;try{const r=await post('/submit',{token,choice:i});document.getElementById('status').textContent=r.ok?'Response recorded':'Response unavailable or already used';if(r.ok)document.getElementById('choices').replaceChildren()}catch(e){document.getElementById('status').textContent='Outcome unknown; do not retry blindly'}};document.getElementById('choices').append(b)});
}catch(e){document.getElementById('title').textContent='Card unavailable'}})();</script>'''


def card_app(store):
    """Return WSGI app; deploy behind TLS and a no-body-logging proxy."""
    def app(environ, start_response):
        headers = [('Cache-Control', 'no-store'), ('Referrer-Policy', 'no-referrer'), ('X-Content-Type-Options', 'nosniff'), ('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")]
        status, body, content_type = '404 Not Found', b'Not found', 'text/plain'
        path, method = environ.get('PATH_INFO'), environ.get('REQUEST_METHOD')
        if method == 'GET' and path == '/card':
            status, body, content_type = '200 OK', _PAGE, 'text/html; charset=utf-8'
        elif method == 'POST' and path in ('/view', '/submit'):
            try:
                length = int(environ.get('CONTENT_LENGTH') or '0')
                if not 0 < length <= 4096 or environ.get('CONTENT_TYPE', '').split(';')[0].strip() != 'application/json':
                    raise ValueError('invalid request')
                data = json.loads(environ['wsgi.input'].read(length))
                if not isinstance(data, dict) or set(data) != ({'token'} if path == '/view' else {'token', 'choice'}):
                    raise ValueError('invalid request')
                result = store.view(data['token']) if path == '/view' else store.submit(data['token'], data['choice'])
                status = '200 OK' if result else '409 Conflict'
                body = json.dumps(result if path == '/view' and result else {'ok': bool(result)}).encode()
                content_type = 'application/json'
            except (ValueError, TypeError, json.JSONDecodeError):
                status, body = '400 Bad Request', b'Invalid request'
        headers.extend([('Content-Type', content_type), ('Content-Length', str(len(body)))])
        start_response(status, headers)
        return [body]
    return app
