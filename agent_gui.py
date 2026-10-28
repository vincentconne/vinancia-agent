"""
Fenêtre de Vinancia Agent.

L'interface est une petite page web (dossier ui/, aux couleurs de Vinancia) affichée
dans la fenêtre native du système par pywebview : WebView2 sous Windows, WebKit sur
Mac. Rien n'est chargé depuis Internet ; la page ne fait qu'afficher et transmettre.

`Bridge` est l'objet que la page appelle (window.pywebview.api.<méthode>). Les
opérations longues (connexion bancaire, synchro) tournent dans un fil secondaire et
renvoient leur avancement à la page par window.agentEvent(type, données). Quand la
banque demande un code ou une validation, le fil attend la réponse de la page.
"""
import json
import os
import sys
import threading
import time
import uuid
import webbrowser

import agent_core as core
from agent_payload import last_sync_label, serialize_field

# Page et logos embarqués (dans l'exécutable : sys._MEIPASS).
UI_DIR = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))), 'ui')
LOGO_DIR = os.path.join(UI_DIR, 'logos')

# Seules adresses que la page peut faire ouvrir dans le navigateur.
OPENABLE = {
    'budget': f'{core.WEB_URL}/budget',
    'review': core.review_url(),
    'agent': f'{core.WEB_URL}/agent',
    'privacy': f'{core.WEB_URL}/privacy#synchro-bancaire',
}


def logo_path(module):
    return f'logos/{module}.png' if os.path.exists(os.path.join(LOGO_DIR, f'{module}.png')) else None


class PageInteraction(core.Interaction):
    """Les questions de la banque (code SMS, validation appli) posées dans la page."""

    def __init__(self, bridge):
        self.bridge = bridge

    def ask(self, field):
        answer = self.bridge._ask({
            'kind': 'otp',
            'label': field.label or field.description or 'Code de sécurité reçu par SMS ou e-mail',
            'masked': bool(field.masked),
        })
        if answer is None:
            raise core.Cancelled()
        return answer

    def app_validation(self, message):
        if self.bridge._ask({'kind': 'app', 'message': message}) is None:
            raise core.Cancelled()

    def redirect(self, url):
        webbrowser.open(url)
        answer = self.bridge._ask({'kind': 'redirect', 'url': url})
        if answer is None:
            raise core.Cancelled()
        return answer


class Bridge:
    """
    API appelée par la page. Attention : pywebview expose les attributs publics à la
    page ; tout ce qui ne doit pas l'être commence par « _ ».
    """

    def __init__(self):
        self._window = None
        self._cfg = core.load_config()
        self._woob = None
        self._fields = {}
        self._asks = {}
        self._pairing = None
        self._interaction = PageInteraction(self)
        self._lock = threading.Lock()

    # ─── Outils internes ─────────────────────────────────────────────────────

    def _emit(self, kind, data=None):
        if self._window is not None:
            self._window.evaluate_js(f'window.agentEvent({json.dumps(kind)}, {json.dumps(data or {})})')

    def _ask(self, question):
        """Depuis un fil de travail : pose une question à la page et attend la réponse."""
        ask_id = uuid.uuid4().hex
        waiter = {'event': threading.Event(), 'value': None}
        self._asks[ask_id] = waiter
        self._emit('ask', {**question, 'id': ask_id})
        waiter['event'].wait()
        self._asks.pop(ask_id, None)
        return waiter['value']

    def _background(self, work):
        def run():
            try:
                work()
            except core.Cancelled:
                self._emit('failed', {'message': 'Opération annulée.'})
            except Exception as error:  # noqa: BLE001 — tout est montré à l'utilisateur
                self._emit('failed', {'message': core.classify_error(error)[1]})
        threading.Thread(target=run, daemon=True).start()

    def _banks_view(self):
        cfg = self._cfg = core.load_config()
        out = []
        for bank in cfg['banks']:
            status = cfg['status'].get(bank['name'], {})
            out.append({
                'key': bank['name'],
                'name': core.bank_display_name(bank),
                'logo': logo_path(bank['module']),
                'status': status.get('status') or 'ok',
                'message': status.get('message'),
                'last_sync_label': last_sync_label(cfg['last_sync'].get(bank['name'])),
            })
        return out

    def _woob_instance(self):
        if self._woob is None:
            self._woob = core.make_woob()
        return self._woob

    @staticmethod
    def _summary(result, report, cfg):
        names = {b['module']: core.bank_display_name(b) for b in cfg['banks']}
        return {
            'operations': result.get('expenses_created', 0) + result.get('revenues_created', 0),
            'accounts_pending': result.get('accounts_pending', 0),
            'suggestions': result.get('suggestions_created', 0),
            'positions': result.get('positions_created', 0),
            'report': [{'name': names.get(r['bank'], r['bank']), 'status': r['status'], 'message': r['message']}
                       for r in report],
        }

    # ─── Appelé par la page ──────────────────────────────────────────────────

    def init(self):
        threading.Thread(target=self._check_update, daemon=True).start()
        return {
            'connected': core.is_connected(),
            'user': self._cfg.get('user'),
            'version': core.AGENT_VERSION,
            'banks': self._banks_view(),
        }

    def catalog(self):
        return [{'id': module, 'name': name, 'logo': logo_path(module)} for module, name in core.COMMON_BANKS]

    def start_pairing(self):
        try:
            pairing = core.start_pairing(self._cfg)
        except Exception as error:  # noqa: BLE001
            return {'error': core.classify_error(error)[1]}
        token = object()
        self._pairing = (token, pairing)

        def wait():
            deadline = time.time() + pairing['expires_in']
            while time.time() < deadline:
                time.sleep(pairing.get('interval', 3))
                if not self._pairing or self._pairing[0] is not token:
                    return  # annulé
                try:
                    if core.finish_pairing(self._cfg, pairing['device_code']):
                        self._pairing = None
                        self._emit('paired')
                        return
                except core.AgentError as error:
                    self._pairing = None
                    self._emit('pairing_failed', {'message': str(error)})
                    return
                except Exception:  # noqa: BLE001 — coupure réseau passagère : on réessaie
                    continue
            if self._pairing and self._pairing[0] is token:
                self._pairing = None
                self._emit('pairing_failed', {'message': 'Le code a expiré : recommence la liaison.'})
        threading.Thread(target=wait, daemon=True).start()
        return {'user_code': pairing['user_code'], 'expires_in': pairing['expires_in']}

    def reopen_pairing(self):
        if self._pairing:
            webbrowser.open(core.pairing_url(self._pairing[1]['user_code']))

    def cancel_pairing(self):
        self._pairing = None

    def connect_token(self, token):
        try:
            core.connect_with_token(self._cfg, str(token or '').strip())
            return {'ok': True}
        except Exception as error:  # noqa: BLE001
            return {'error': core.classify_error(error)[1]}

    def prepare_bank(self, module):
        module = str(module or '').strip().lower()
        try:
            with self._lock:
                fields = core.bank_fields(core.ensure_module(self._woob_instance(), module))
        except Exception as error:  # noqa: BLE001
            return {'error': core.classify_error(error)[1]}
        self._fields[module] = {f.id: f for f in fields}
        return {
            'id': module,
            'name': core.BANK_NAMES.get(module, module),
            'logo': logo_path(module),
            'hint': core.BANK_HINTS.get(module),
            'fields': [serialize_field(f) for f in fields],
        }

    def validate_bank(self, module, values):
        """Erreurs de saisie par champ (vide, ou format attendu par la banque)."""
        errors = {}
        for field_id, field in self._fields.get(module, {}).items():
            value = str((values or {}).get(field_id) or '').strip()
            if not value:
                if field.required:
                    errors[field_id] = 'Champ obligatoire.'
                continue
            try:
                field.check_valid(value)
            except ValueError:
                errors[field_id] = 'Format inattendu : vérifie ta saisie.'
        return {'errors': errors}

    def connect_bank(self, module, values):
        values = {k: str(v).strip() for k, v in (values or {}).items() if k in self._fields.get(module, {})}

        def work():
            with self._lock:
                login, password, params = core.split_credentials(values)
                params, secrets = core.separate_secrets(params, self._fields.get(module, {}).values())
                bank = core.new_bank_entry(self._cfg, module, login, params, secrets)
                accounts = core.test_bank(self._woob_instance(), bank, password, self._interaction)
                core.add_bank(self._cfg, bank, password)
                self._emit('progress', {'text': f'{len(accounts)} compte(s) trouvé(s). Envoi de tes opérations à Vinancia…'})
                result, report = core.run_sync(self._cfg, interaction=self._interaction, only=[bank['name']],
                                               progress=lambda text: self._emit('progress', {'text': text}))
                try:
                    core.install_schedule()
                    self._cfg['schedule_installed'] = True
                    core.save_config(self._cfg)
                except Exception as error:  # noqa: BLE001
                    core.log.warning('planification : %s', error)
            self._emit('done', {**self._summary(result, report, self._cfg), 'kind': 'connect',
                                'accounts': len(accounts), 'banks': self._banks_view()})
        self._background(work)

    def sync_now(self):
        def work():
            with self._lock:
                result, report = core.run_sync(self._cfg, interaction=self._interaction,
                                               progress=lambda text: self._emit('progress', {'text': text}))
                if not self._cfg.get('schedule_installed'):
                    try:
                        core.install_schedule()
                        self._cfg['schedule_installed'] = True
                        core.save_config(self._cfg)
                    except Exception as error:  # noqa: BLE001
                        core.log.warning('planification : %s', error)
            summary = self._summary(result, report, self._cfg)
            if summary['accounts_pending'] or summary['suggestions']:
                webbrowser.open(core.review_url())
            self._emit('done', {**summary, 'kind': 'sync', 'banks': self._banks_view()})
        self._background(work)

    def answer(self, ask_id, value):
        waiter = self._asks.get(ask_id)
        if waiter:
            waiter['value'] = None if value is None else str(value)
            waiter['event'].set()

    def remove_bank(self, key):
        core.remove_bank(self._cfg, key)
        return {'banks': self._banks_view()}

    def unlink(self):
        core.forget_everything(self._cfg)
        self._cfg = core.load_config()
        return {'ok': True}

    def open_vinancia(self, where):
        url = OPENABLE.get(where)
        if url:
            webbrowser.open(url)

    def _check_update(self):
        latest = core.latest_release()
        if latest and core.is_newer(latest):
            self._emit('update', {'version': latest})


WEBVIEW2_DOWNLOAD = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'


def _webview2_missing():
    """Windows : le moteur Edge WebView2 est-il absent ? (présent sur Windows 11.)"""
    if sys.platform != 'win32':
        return False
    import winreg
    guid = r'{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE, rf'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}'),
        (winreg.HKEY_LOCAL_MACHINE, rf'SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}'),
        (winreg.HKEY_CURRENT_USER, rf'Software\Microsoft\EdgeUpdate\Clients\{guid}'),
    ):
        try:
            with winreg.OpenKey(root, path) as key:
                version, _ = winreg.QueryValueEx(key, 'pv')
                if version and version != '0.0.0.0':
                    return False
        except OSError:
            continue
    return True


def main():
    import webview

    core.setup_logging(to_file=True)
    if _webview2_missing():
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            'Vinancia Agent a besoin du composant Microsoft Edge WebView2 (gratuit, déjà présent sur '
            'Windows 11). La page de téléchargement de Microsoft va s’ouvrir : installe-le puis relance '
            'Vinancia Agent.',
            core.APP_NAME, 0x40,
        )
        webbrowser.open(WEBVIEW2_DOWNLOAD)
        return
    bridge = Bridge()
    window = webview.create_window(
        core.APP_NAME,
        url=os.path.join(UI_DIR, 'index.html'),
        js_api=bridge,
        width=480,
        height=760,
        min_size=(420, 620),
        background_color='#0b1221',
    )
    bridge._window = window
    webview.start(http_server=True, private_mode=True)


if __name__ == '__main__':
    main()
