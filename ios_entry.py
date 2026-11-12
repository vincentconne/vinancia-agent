"""Point d'entrée Python de l'application iPhone Vinancia Agent.

Même agent que sur ordinateur et sur Android (agent_core, agent_gui, agent_payload…), branché sur iOS :
  - les secrets vont dans le **Trousseau iOS** (Keychain, lié à l'appareil, non sauvegardé ailleurs) ;
  - la page de l'agent (ui/) est servie à la fenêtre web de l'appli par un petit serveur local (127.0.0.1, port
    aléatoire, jeton secret) : le pont `window.pywebview.api` de la page y devient des appels HTTP ;
  - les modules bancaires de Woob sont EMBARQUÉS dans l'appli (guideline 2.5.2 d'Apple : pas de code téléchargé après
    coup) ; plus aucune mise à jour de module à l'exécution, ce sont les mises à jour de l'appli qui les renouvellent ;
  - pas de tâche planifiée : iOS ne le permet pas sans code natif, la synchro se fait à l'ouverture de l'appli.
La logique (serveur, trousseau) est pure ou injectable : testée sous Linux (`tests/test_ios_entry.py`).
"""
import json
import mimetypes
import os
import secrets
import shutil
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Modules Woob jamais embarqués sur iPhone : ils exigent pycryptodome (extension C sans version iPhone).
# (Aucune grande banque : seuls `nef` et `erehsbc` l'utilisent.)
UNSUPPORTED_BANKS = {'nef', 'erehsbc'}

KEYCHAIN_SERVICE = 'com.vinancia.agent'
_HERE = os.path.dirname(os.path.abspath(__file__))
_state = {}
_lock = threading.Lock()


# ─── Trousseau ────────────────────────────────────────────────────────────────────────────────────

def make_keyring(store):
    """Trousseau Python (`keyring`) → un « magasin » get / put / remove (le Trousseau iOS en vrai, un faux en test)."""
    from keyring.backend import KeyringBackend

    class KeychainKeyring(KeyringBackend):
        priority = 10

        def get_password(self, service, username):
            return store.get(f'{service}:{username}')

        def set_password(self, service, username, password):
            store.put(f'{service}:{username}', password)

        def delete_password(self, service, username):
            store.remove(f'{service}:{username}')

    return KeychainKeyring()


class KeychainStore:
    """Trousseau iOS (Security.framework via ctypes) : mots de passe génériques, accessibles seulement appareil
    déverrouillé, jamais copiés vers une autre machine (`ThisDeviceOnly`). Ne s'importe que sur iOS."""

    def __init__(self, service=KEYCHAIN_SERVICE):
        import ctypes
        import ctypes.util  # noqa: F401
        self.c = ctypes
        self.service = service
        self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        void = ctypes.c_void_p
        self.cf.CFStringCreateWithCString.restype = void
        self.cf.CFStringCreateWithCString.argtypes = [void, ctypes.c_char_p, ctypes.c_uint32]
        self.cf.CFDataCreate.restype = void
        self.cf.CFDataCreate.argtypes = [void, ctypes.c_char_p, ctypes.c_long]
        self.cf.CFDataGetLength.restype = ctypes.c_long
        self.cf.CFDataGetLength.argtypes = [void]
        self.cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
        self.cf.CFDataGetBytePtr.argtypes = [void]
        self.cf.CFDictionaryCreateMutable.restype = void
        self.cf.CFDictionaryCreateMutable.argtypes = [void, ctypes.c_long, void, void]
        self.cf.CFDictionarySetValue.argtypes = [void, void, void]
        self.cf.CFRelease.argtypes = [void]
        self.sec.SecItemAdd.argtypes = [void, void]
        self.sec.SecItemAdd.restype = ctypes.c_int32
        self.sec.SecItemCopyMatching.argtypes = [void, ctypes.POINTER(void)]
        self.sec.SecItemCopyMatching.restype = ctypes.c_int32
        self.sec.SecItemDelete.argtypes = [void]
        self.sec.SecItemDelete.restype = ctypes.c_int32
        self._key_cb = void.in_dll(self.cf, 'kCFTypeDictionaryKeyCallBacks')
        self._val_cb = void.in_dll(self.cf, 'kCFTypeDictionaryValueCallBacks')

    def _const(self, name, lib=None):
        return self.c.c_void_p.in_dll(lib or self.sec, name).value

    def _str(self, text):
        return self.cf.CFStringCreateWithCString(None, text.encode('utf-8'), 0x08000100)   # UTF-8

    def _query(self, account, extra=None):
        d = self.cf.CFDictionaryCreateMutable(None, 0, self.c.addressof(self._key_cb), self.c.addressof(self._val_cb))
        owned = [d]

        def put(key, value, own=False):
            self.cf.CFDictionarySetValue(d, key, value)
            if own:
                owned.append(value)

        put(self._const('kSecClass'), self._const('kSecClassGenericPassword'))
        put(self._const('kSecAttrService'), self._str(self.service), own=True)
        put(self._const('kSecAttrAccount'), self._str(account), own=True)
        for key, value in (extra or {}).items():
            put(self._const(key), value[0], own=value[1])
        return d, owned

    def _release(self, owned):
        for obj in owned:
            self.cf.CFRelease(obj)

    def get(self, account):
        true = self._const('kCFBooleanTrue', self.cf)
        d, owned = self._query(account, {'kSecReturnData': (true, False),
                                         'kSecMatchLimit': (self._const('kSecMatchLimitOne'), False)})
        out = self.c.c_void_p()
        status = self.sec.SecItemCopyMatching(d, self.c.byref(out))
        self._release(owned)
        if status != 0 or not out.value:
            return None
        try:
            size = self.cf.CFDataGetLength(out.value)
            raw = bytes(self.cf.CFDataGetBytePtr(out.value)[i] for i in range(size))
        finally:
            self.cf.CFRelease(out.value)
        return raw.decode('utf-8')

    def put(self, account, value):
        self.remove(account)
        data = self.cf.CFDataCreate(None, value.encode('utf-8'), len(value.encode('utf-8')))
        d, owned = self._query(account, {
            'kSecValueData': (data, True),
            'kSecAttrAccessible': (self._const('kSecAttrAccessibleWhenUnlockedThisDeviceOnly'), False),
        })
        status = self.sec.SecItemAdd(d, None)
        self._release(owned)
        if status != 0:
            raise RuntimeError(f'Trousseau iPhone : écriture refusée (code {status})')

    def remove(self, account):
        d, owned = self._query(account)
        self.sec.SecItemDelete(d)   # -25300 (absent) : rien à faire
        self._release(owned)


# ─── Pont page ↔ Python ───────────────────────────────────────────────────────────────────────────

class EventQueue:
    """Ce que Python demande d'exécuter à la page (`evaluate_js`) : la page vient les chercher (interrogation longue)."""

    def __init__(self, keep=500):
        self._items = deque(maxlen=keep)
        self._cond = threading.Condition()
        self._next = 1

    def push(self, script):
        with self._cond:
            self._items.append((self._next, script))
            self._next += 1
            self._cond.notify_all()

    def wait(self, after, timeout=25.0):
        with self._cond:
            if not any(i > after for i, _ in self._items):
                self._cond.wait(timeout)
            return [{'id': i, 'js': js} for i, js in self._items if i > after]


class _Window:
    """Ce que `agent_gui.Bridge` attend d'une fenêtre pywebview : `evaluate_js`."""

    def __init__(self, events):
        self._events = events

    def evaluate_js(self, script):
        self._events.push(script)


def dispatch(bridge, bridge_cls, method, args):
    """Appelle une méthode PUBLIQUE de `Bridge` (jamais `_x`, jamais un attribut non appelable)."""
    if not isinstance(method, str) or method.startswith('_') or not callable(getattr(bridge_cls, method, None)):
        raise ValueError(f'méthode inconnue : {method}')
    result = getattr(bridge, method)(*args)
    if method in ('catalog', 'known_banks'):
        result = [bank for bank in result if bank.get('id') not in UNSUPPORTED_BANKS]
    return result


_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class BridgeServer:
    """Sert la page de l'agent et le pont. Écoute 127.0.0.1 seulement ; tout appel exige le jeton de lancement."""

    def __init__(self, ui_dir, bridge, bridge_cls, events, token=None, shim_path=None):
        self.ui_dir = os.path.realpath(ui_dir)
        self.bridge, self.bridge_cls, self.events = bridge, bridge_cls, events
        self.token = token or secrets.token_urlsafe(24)
        self.shim_path = shim_path or os.path.join(_HERE, 'ios_shim.js')
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):   # jamais de journal d'accès : les URL portent le jeton
                pass

            def _send(self, status, body=b'', ctype='text/plain; charset=utf-8', extra=None):
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Security-Policy', _CSP)
                self.send_header('X-Content-Type-Options', 'nosniff')
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _host_ok(self):
                return self.headers.get('Host', '').split(':')[0] in ('127.0.0.1', 'localhost')

            def _token_ok(self):
                given = self.headers.get('X-VNA-Token', '')
                return secrets.compare_digest(given, owner.token)

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, b'host')
                url = urlparse(self.path)
                if url.path == '/__vna/events':
                    if not self._token_ok():
                        return self._send(403, b'token')
                    after = int((parse_qs(url.query).get('after') or ['0'])[0] or 0)
                    return self._send(200, json.dumps(owner.events.wait(after)).encode(), 'application/json')
                if url.path == '/__vna/shim.js':
                    with open(owner.shim_path, 'rb') as f:
                        return self._send(200, f.read(), 'application/javascript; charset=utf-8')
                if url.path in ('/', '/index.html'):
                    given = (parse_qs(url.query).get('t') or [''])[0]
                    if not secrets.compare_digest(given, owner.token):
                        return self._send(403, b'token')
                    with open(os.path.join(owner.ui_dir, 'index.html'), encoding='utf-8') as f:
                        html = f.read().replace('<script src="app.js">', '<script src="/__vna/shim.js"></script>\n  <script src="app.js">', 1)
                    return self._send(200, html.encode(), 'text/html; charset=utf-8')
                target = os.path.realpath(os.path.join(owner.ui_dir, url.path.lstrip('/')))
                if not target.startswith(owner.ui_dir + os.sep) or not os.path.isfile(target):
                    return self._send(404, b'introuvable')
                with open(target, 'rb') as f:
                    return self._send(200, f.read(), mimetypes.guess_type(target)[0] or 'application/octet-stream')

            def do_POST(self):
                if not self._host_ok() or not self._token_ok():
                    return self._send(403, b'token')
                url = urlparse(self.path)
                if not url.path.startswith('/__vna/call/'):
                    return self._send(404, b'introuvable')
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if length > 1_000_000:
                        return self._send(413, b'trop gros')
                    args = json.loads(self.rfile.read(length) or b'[]')
                    result = dispatch(owner.bridge, owner.bridge_cls, url.path[len('/__vna/call/'):], args)
                    body = {'ok': True, 'result': result}
                except Exception as exc:  # noqa: BLE001 — la page affiche le message
                    body = {'ok': False, 'error': str(exc)[:300]}
                self._send(200, json.dumps(body).encode(), 'application/json')

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name='vna-bridge')

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/?t={self.token}'

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ─── Modules bancaires embarqués ──────────────────────────────────────────────────────────────────

def _secure_permissions(woob_dir):
    """Woob refuse de démarrer si son fichier `backends` ou son dossier est lisible par d'autres utilisateurs : les copies
    faites depuis le lot de l'appli héritent de droits plus larges (0644), on les ramène à 0600 / 0700."""
    for name in ('backends', 'sessions.enc'):
        path = os.path.join(woob_dir, name)
        if os.path.isfile(path):
            os.chmod(path, 0o600)
    os.chmod(woob_dir, 0o700)


def install_bundled_modules(bundle_dir, woob_dir):
    """Copie les modules Woob embarqués (déjà téléchargés et vérifiés à la construction de l'appli) dans le dossier de
    travail de l'agent. Idempotent : ne réécrase pas ce qui existe déjà (les sessions chiffrées restent intactes)."""
    if not os.path.isdir(bundle_dir):
        return False
    os.makedirs(woob_dir, mode=0o700, exist_ok=True)
    marker = os.path.join(woob_dir, '.bundle-version')
    version = ''
    try:
        with open(os.path.join(bundle_dir, 'VERSION'), encoding='utf-8') as f:
            version = f.read().strip()
    except OSError:
        pass
    try:
        with open(marker, encoding='utf-8') as f:
            if f.read().strip() == version and version:
                return False
    except OSError:
        pass
    for name in os.listdir(bundle_dir):
        if name in ('VERSION', 'backends'):    # `backends` : config des banques de l'utilisateur, jamais celle du lot
            continue
        src, dst = os.path.join(bundle_dir, name), os.path.join(woob_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    _secure_permissions(woob_dir)
    with open(marker, 'w', encoding='utf-8') as f:
        f.write(version)
    return True


# ─── Mise en route ────────────────────────────────────────────────────────────────────────────────

def setup(files_dir, ui_dir, bundle_dir=None, open_url=None, device='Mon iPhone', store=None):
    """Prépare l'environnement (idempotent) et démarre le serveur local. Renvoie le `BridgeServer`."""
    with _lock:
        if _state.get('server'):
            return _state['server']
        os.environ['VINANCIA_IOS'] = '1'
        os.environ['VINANCIA_DEVICE'] = device or 'Mon iPhone'
        os.environ['HOME'] = files_dir
        os.environ['XDG_CONFIG_HOME'] = os.path.join(files_dir, 'config')
        os.makedirs(os.environ['XDG_CONFIG_HOME'], mode=0o700, exist_ok=True)

        import keyring
        keyring.set_keyring(make_keyring(store or KeychainStore()))

        import webbrowser

        def _open(url, *args, **kwargs):
            if isinstance(url, str) and url.startswith('https://') and open_url:
                open_url(url)
                return True
            return False

        webbrowser.open = _open

        import agent_core as core
        core.install_schedule = lambda: None      # pas de cron / launchd sur iPhone : synchro à l'ouverture
        core.remove_schedule = lambda: None
        core.latest_release = lambda: None        # les mises à jour passent par l'App Store / TestFlight
        core.setup_logging(to_file=True)
        # chompjs (extension C) n'est que listé dans le requirements.txt des modules Caisse d'Épargne / Banque Populaire.
        core.ensure_dist_stub('chompjs', '1.4.1', os.path.join(files_dir, 'stubs'))
        if bundle_dir:
            install_bundled_modules(bundle_dir, os.path.join(core.agent_dir(), 'woob'))

        def no_download(w, progress=None):
            raise core.AgentError("Ce module bancaire n'est pas inclus dans cette version de l'application.")

        core.update_modules = no_download         # jamais de code téléchargé après installation (règle 2.5.2 d'Apple)

        import agent_gui
        known = {n for n in os.listdir(os.path.join(ui_dir, 'logos')) if n.endswith('.png')} \
            if os.path.isdir(os.path.join(ui_dir, 'logos')) else set()
        agent_gui.logo_path = lambda module: f'logos/{module}.png' if f'{module}.png' in known else None

        events = EventQueue()
        bridge = agent_gui.Bridge()
        bridge._window = _Window(events)
        server = BridgeServer(ui_dir, bridge, agent_gui.Bridge, events).start()
        _state['server'] = server
        return server


SYNC_EVERY_SECONDS = 6 * 3600


def due_for_sync(last_epoch, now, every=SYNC_EVERY_SECONDS):
    """Vrai si la dernière synchro à l'ouverture date de plus de `every` secondes (ou n'a jamais eu lieu)."""
    return not last_epoch or now - float(last_epoch) >= every


def sync_on_open(now=None):
    """À l'ouverture : synchro silencieuse si la dernière date de plus de 6 h (jamais de SMS : `interaction=None`).
    iOS ne permet pas de tâche planifiée sans code natif : c'est le compromis honnête."""
    import time
    import agent_core as core
    cfg = core.load_config()
    if not core.is_connected() or not cfg.get('banks'):
        return None
    now = now or time.time()
    if not due_for_sync(cfg.get('ios_last_auto_sync'), now):
        return None
    cfg['ios_last_auto_sync'] = now
    core.save_config(cfg)
    _, report = core.run_sync(cfg, interaction=None)
    return report
