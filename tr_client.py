"""Trade Republic pour Vinancia Agent : client minimal, EN LECTURE SEULE.

Trade Republic n'a pas d'API publique : ce client parle au même service que le site
app.traderepublic.com (connexion « web » : numéro + PIN, puis validation dans l'appli Trade
Republic). Le protocole a été repris du projet libre pytr (https://github.com/pytr-org/pytr,
licence MIT, voir THIRD_PARTY.md) ; rien d'autre n'en est embarqué.

Choix de sécurité :
  * un seul hôte : api.traderepublic.com (HTTPS et WSS) ;
  * lecture seule par construction : seuls les sujets de READ_ONLY_TOPICS partent sur le websocket,
    aucun ordre, aucun virement ;
  * jamais de « device reset » (qui déconnecterait le téléphone de l'utilisateur) ;
  * `resume()` ne relance JAMAIS de connexion : chaque tentative envoie une notification au
    téléphone et Trade Republic bloque vite (429, parfois plus de 12 h) ;
  * aucune écriture sur disque ici : les cookies sortent en liste de dicts, chiffrés et rangés
    par l'agent ; les erreurs ne contiennent ni URL, ni numéro, ni identifiant de connexion.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import ssl
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

HOST = 'https://api.traderepublic.com'
WS_URL = 'wss://api.traderepublic.com'
COOKIE_DOMAIN = 'traderepublic.com'
# Valeurs que le site envoie lui-même ; Trade Republic peut refuser une version trop ancienne
# (400) : `parse_app_version` permet de relire la version courante sur la page de connexion.
APP_VERSION = '2.2646.7'
WEB_PLATFORM = 'web-pro'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/146.0.0.0 Safari/537.36')
SESSION_REFRESH_SECONDS = 290
LOGIN_WINDOW_SECONDS = 120
MAX_TIMELINE_PAGES = 60
MAX_TRANSACTIONS = 3000
MAX_POSITIONS = 300

CASH_ACCOUNT_ID = 'tr-cash'
SECURITIES_ACCOUNT_ID = 'tr-securities'

# Les seuls sujets que le client a le droit de demander.
READ_ONLY_TOPICS = frozenset({'cash', 'compactPortfolioByType', 'instrument', 'ticker', 'timelineTransactions'})

_ISIN_RE = re.compile(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]$')
_APP_VERSION_RE = re.compile(r'<meta\s+name="app-version"\s+content="([0-9][0-9.]{2,20})"')
_BOND_RE = re.compile(
    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|janv|févr|mars|avr|mai|juin|juil|août|sept|oct|nov|déc|'
    r'Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\w*\.?\s+20\d{2}',
    re.IGNORECASE,
)


class TRError(Exception):
    """Erreur présentable à l'utilisateur (aucun secret dans le texte)."""


class TRNeedsLogin(TRError):
    """La session enregistrée n'est plus valable : il faut une connexion avec validation."""


class TRBadCredentials(TRError):
    """Numéro ou PIN refusé."""


class TRRateLimited(TRError):
    """Trop de tentatives : Trade Republic bloque les connexions pour un moment."""


class TRLoginExpired(TRError):
    """La validation n'est pas arrivée à temps (ou a été refusée dans l'appli)."""


# --------------------------------------------------------------------------- règles pures


def normalize_phone(text):
    """« 06 12 34 56 78 », « 0033612345678 », « +33 6 12… » -> « +33612345678 ». Lève TRBadCredentials sinon."""
    raw = re.sub(r'[\s.\-()]', '', str(text or ''))
    if raw.startswith('00'):
        raw = '+' + raw[2:]
    elif re.fullmatch(r'0[1-9]\d{8}', raw):
        raw = '+33' + raw[1:]          # numéro français écrit sans indicatif
    if not re.fullmatch(r'\+[1-9]\d{7,14}', raw):
        raise TRBadCredentials('Numéro de téléphone illisible : écris-le avec l’indicatif, par exemple +33612345678.')
    return raw


def sanitize_error(error):
    """Texte d'erreur sans URL (elle contient l'identifiant de la connexion en cours) ni numéro."""
    text = re.sub(r'https?://\S+', '[url]', str(error))
    text = re.sub(r'\+?\d{6,}', '[n°]', text)
    return f'{type(error).__name__}: {text}'[:200]


def parse_app_version(html):
    """Version du site lue dans sa page de connexion (`<meta name="app-version" …>`), ou None."""
    match = _APP_VERSION_RE.search(html or '')
    return match.group(1) if match else None


def error_code(payload):
    """Premier `errorCode` d'une réponse d'erreur de Trade Republic, ou ''."""
    try:
        return str(payload['errors'][0]['errorCode'] or '')
    except (KeyError, IndexError, TypeError):
        return ''


def login_error(status, code):
    """Exception à lever pour un refus de connexion (statut HTTP + code Trade Republic)."""
    code = (code or '').upper()
    if status == 429 or code == 'TOO_MANY_REQUESTS':
        return TRRateLimited('Trade Republic bloque les connexions après plusieurs essais. Attends quelques heures '
                             'avant de réessayer (chaque nouvel essai prolonge le blocage).')
    if code == 'NUMBER_INVALID':
        return TRBadCredentials('Trade Republic ne reconnaît pas ce numéro de téléphone.')
    if 'PIN' in code or 'CREDENTIAL' in code or 'PASSWORD' in code or status == 401:
        return TRBadCredentials('Numéro ou code PIN refusé par Trade Republic.')
    if code in ('PROCESS_GONE', 'NOT_FOUND'):
        return TRLoginExpired('La demande de connexion a expiré : relance la synchronisation.')
    if code == 'ALREADY_PROCESSED':
        return TRLoginExpired('La demande de connexion a été refusée ou déjà utilisée : relance la synchronisation.')
    if code in ('VALIDATION_CODE_INVALID', 'VALIDATION_CODE_ALREADY_USED'):
        return TRBadCredentials('Code de l’application d’authentification refusé.')
    return TRError(f'Trade Republic a refusé la connexion ({code or status}).')


def process_deadline(process, now, fallback=LOGIN_WINDOW_SECONDS):
    """Heure limite (horodatage Unix) pour valider la connexion ; `expiresAt` peut être en ms, en s ou en ISO."""
    expires_at = (process or {}).get('expiresAt')
    try:
        if isinstance(expires_at, bool):
            raise TypeError
        if isinstance(expires_at, (int, float)):
            value = expires_at / 1000 if expires_at > 1e11 else float(expires_at)
        else:
            value = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return now + fallback
    # Jamais plus de 10 minutes d'attente, quoi que dise la réponse.
    return min(value, now + 600)


def device_info(device_id, locale='fr', tz_offset_minutes=0):
    """En-tête X-TR-Device-Info : description de l'« appareil » en JSON encodé en base64, comme le site."""
    chrome = re.search(r'Chrome/([\d.]+)', USER_AGENT)
    device = {
        'stableDeviceId': device_id,
        'browser': 'Chrome',
        'browserVersion': chrome.group(1) if chrome else '',
        'os': 'Windows',
        'osVersion': '10',
        'timezone': 'Europe/Paris',
        'timezoneOffset': tz_offset_minutes,
        'screen': '1920x1080x24',
        'preferredLanguages': [locale],
        'numberOfCores': os.cpu_count() or 1,
    }
    return base64.b64encode(json.dumps(device).encode()).decode()


def new_device_id():
    """Identifiant d'installation : 128 caractères hexadécimaux aléatoires (même forme que celui du site)."""
    return os.urandom(64).hex()


def cookies_to_list(jar):
    """Cookies Trade Republic d'un CookieJar -> liste de dicts JSON (à chiffrer par l'appelant)."""
    out = []
    for c in jar:
        if not (c.domain or '').lstrip('.').endswith(COOKIE_DOMAIN):
            continue
        out.append({'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path or '/',
                    'secure': bool(c.secure), 'expires': c.expires})
    return sorted(out, key=lambda c: (c['domain'], c['name']))


def cookies_into(jar, cookies):
    """Recharge des cookies sérialisés ; tout ce qui n'est pas du domaine Trade Republic est refusé."""
    count = 0
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        domain = str(c.get('domain') or '')
        if not domain.lstrip('.').endswith(COOKIE_DOMAIN) or not c.get('name'):
            continue
        jar.set(c['name'], c.get('value') or '', domain=domain, path=c.get('path') or '/',
                secure=bool(c.get('secure', True)), expires=c.get('expires'))
        count += 1
    return count


def cookie_header(jar):
    return '; '.join(f'{c.name}={c.value}' for c in jar if (c.domain or '').lstrip('.').endswith(COOKIE_DOMAIN))


def parse_frame(frame):
    """Message du websocket « <id> <code> <json> » -> (id, code, texte). Codes : A réponse, D delta, C fin, E erreur."""
    text = frame.decode('utf-8', 'replace') if isinstance(frame, (bytes, bytearray)) else str(frame)
    sub_id, _, rest = text.partition(' ')
    return sub_id, rest[:1], rest[1:].lstrip()


def apply_delta(previous, delta):
    """Réponse « D » : différences à appliquer à la réponse précédente (format de Trade Republic)."""
    i, result = 0, []
    for diff in delta.split('\t'):
        if not diff:
            continue
        sign = diff[0]
        if sign == '+':
            result.append(urllib.parse.unquote_plus(diff[1:]))
        elif sign in '-=':
            size = int(diff[1:])
            if sign == '=':
                result.append(previous[i:i + size])
            i += size
    return ''.join(result)


def _dec(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def isin_from_icon(icon):
    """`logos/IE00B4K6B022/v2` -> ISIN ; `logos/merchant-…/v2` -> None."""
    parts = str(icon or '').split('/')
    return parts[1] if len(parts) >= 2 and _ISIN_RE.match(parts[1]) else None


def is_crypto_code(code):
    """Trade Republic identifie ses cryptos par de faux ISIN « XF000… » (ex. XF000BTC0017)."""
    return str(code or '').startswith('XF000')


def crypto_symbol(code):
    """XF000BTC0017 -> BTC ; XF000DOGE018 -> DOGE (lettres qui suivent le préfixe)."""
    match = re.match(r'XF000([A-Z]{2,6})', str(code or ''))
    return match.group(1) if match else None


_KIND_PREFIXES = (
    ('card', ('CARD_',)),
    ('interest', ('INTEREST_',)),
    ('savings_plan', ('SAVINGS_PLAN_', 'TRADING_SAVINGSPLAN_')),
    ('trade', ('ORDER_', 'TRADING_', 'TRADE_', 'IPO_', 'PRIVATE_MARKET', 'BENEFITS_', 'SAVEBACK_',
               'SPARE_CHANGE_', 'ACQUISITION_TRADE_PERK')),
    ('tax', ('SSP_TAX_', 'TAX_')),
    ('dividend', ('SSP_CORPORATE_ACTION', 'CREDIT')),
    ('transfer', ('BANK_TRANSACTION_', 'INCOMING_TRANSFER', 'OUTGOING_TRANSFER', 'PAYMENT_', 'PAYMENT-',
                  'ACCOUNT_TRANSFER_', 'JUNIOR_P2P_', 'TRANSFER_')),
)
# Mouvements espèces <-> titres à l'intérieur de Trade Republic : pas des dépenses du budget.
INTERNAL_KINDS = frozenset({'trade', 'savings_plan', 'security'})
INTERNAL_PREFIX = 'Virement interne vers titres'
_DIVIDEND_WORDS = ('dividend', 'ausschüttung', 'ausschuttung', 'coupon', 'kupon', 'zinsen', 'intérêt', 'interet')


def event_kind(event):
    """Famille d'une ligne de l'historique : card / transfer / interest / trade / savings_plan / dividend / tax /
    security / other. `security` = type inconnu mais l'icône porte un ISIN : mouvement lié à un titre."""
    etype = str(event.get('eventType') or '').upper()
    for kind, prefixes in _KIND_PREFIXES:
        if etype.startswith(prefixes):
            return kind
    icon = str(event.get('icon') or '')
    if isin_from_icon(icon):
        # Ancienne ligne sans type : un dividende reste un revenu, tout le reste (achat, vente) est un mouvement interne.
        words = f"{event.get('title') or ''} {event.get('subtitle') or ''}".lower()
        return 'dividend' if any(w in words for w in _DIVIDEND_WORDS) else 'security'
    for kind, marks in (('card', ('logos/merchant-',)),
                        ('transfer', ('logos/contacts-', 'logos/timeline_plus_circle', 'logos/timeline_minus_circle')),
                        ('interest', ('logos/timeline_interest',))):
        if icon.startswith(marks):
            return kind
    return 'other'


def event_date(event):
    """Jour d'une ligne, à l'heure de Paris à une heure près (UTC+1 : un achat de 23 h 30 UTC est du lendemain)."""
    stamp = str(event.get('timestamp') or '')
    try:
        moment = datetime.fromisoformat(stamp[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (moment + timedelta(hours=1)).date()


def normalize_transactions(items, since=None, limit=MAX_TRANSACTIONS):
    """Lignes brutes de `timelineTransactions` -> opérations du compte espèces.

    Gardées : statut EXECUTED, montant non nul en EUR, ni masquées ni supprimées. Les ordres annulés, refusés ou
    en attente et les lignes sans montant (documents, divisions d'actions) sont écartés. Un achat de titres ou un
    plan d'épargne garde son montant (le solde en dépend) mais son libellé brut commence par « Virement interne » :
    Vinancia ne le compte alors pas comme une dépense.
    """
    out, seen = [], set()
    for ev in items or []:
        if len(out) >= limit:
            break
        if not isinstance(ev, dict) or ev.get('deleted') or ev.get('hidden'):
            continue
        if str(ev.get('status') or '').upper() != 'EXECUTED':
            continue
        amount = ev.get('amount') or {}
        value = _dec(amount.get('value')) if isinstance(amount, dict) else None
        if value is None or value == 0 or (amount.get('currency') or 'EUR') != 'EUR':
            continue
        day = event_date(ev)
        if day is None or (since is not None and day < since):
            continue
        tx_id = str(ev.get('id') or '')
        if not tx_id or tx_id in seen:
            continue
        title = str(ev.get('title') or '').strip()
        subtitle = str(ev.get('subtitle') or '').strip()
        if not (title or subtitle):
            continue
        seen.add(tx_id)
        kind = event_kind(ev)
        label = title or subtitle
        raw = ' - '.join(p for p in (title, subtitle) if p)
        if kind in INTERNAL_KINDS:
            raw = f'{INTERNAL_PREFIX} - {raw}'
        elif kind == 'transfer':
            # Trade Republic n'affiche que le nom de l'autre partie : on dit que c'est un virement, pour que Vinancia
            # reconnaisse celui qui vient d'un autre compte de l'utilisateur (son propre nom) et ne le compte pas en revenu.
            label, raw = f'Virement {label}', f'Virement - {raw}'
        out.append({'id': tx_id, 'date': day, 'amount': value.quantize(Decimal('0.01')), 'label': label,
                    'raw': raw, 'kind': kind, 'isin': isin_from_icon(ev.get('icon'))})
    return out


def flatten_positions(compact):
    """Réponse `compactPortfolioByType` -> liste plate. Format : {"categories": [{"categoryType", "positions":
    [{"isin", "netSize", "averageBuyIn"}]}]} ; l'ancien format plat ({"positions": [{"instrumentId", …}]}) passe aussi."""
    out = []
    compact = compact if isinstance(compact, dict) else {}
    groups = compact.get('categories')
    if groups is None:
        groups = [{'categoryType': None, 'positions': compact.get('positions') or []}]
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        for pos in group.get('positions') or []:
            if not isinstance(pos, dict):
                continue
            code = pos.get('isin') or pos.get('instrumentId')
            size = _dec(pos.get('netSize'))
            if not code or size is None or size <= 0:
                continue
            out.append({'code': str(code), 'size': size, 'avg': _dec(pos.get('averageBuyIn')),
                        'category': group.get('categoryType') or group.get('type') or None, 'name': pos.get('name')})
    return out[:MAX_POSITIONS]


def normalize_investments(positions, instruments=None, tickers=None):
    """Positions + fiches des titres + cours -> lignes prêtes pour Woob (code, symbol, label, quantity,
    unitprice = prix de revient, unitvalue = cours, valuation). Une crypto n'a pas d'ISIN : `code` vide,
    `symbol` = « BTC-EUR » (même convention que Vinancia)."""
    instruments, tickers = instruments or {}, tickers or {}
    out = []
    for pos in positions:
        code = pos['code']
        info = instruments.get(code) or {}
        label = info.get('shortName') or info.get('name') or pos.get('name') or code
        price = _dec(((tickers.get(code) or {}).get('last') or {}).get('price'))
        if price is not None and _BOND_RE.search(str(label)):
            price = price / 100   # les obligations sont cotées en % du nominal
        valuation = None if price is None else (price * pos['size']).quantize(Decimal('0.01'))
        crypto = is_crypto_code(code)
        symbol = crypto_symbol(code) if crypto else None
        if crypto and not symbol:
            continue   # crypto qu'on ne sait pas nommer : mieux vaut l'omettre que créer un titre faux
        out.append({'code': None if crypto else code, 'symbol': f'{symbol}-EUR' if crypto else None, 'label': str(label),
                    'quantity': pos['size'], 'unitprice': pos['avg'], 'unitvalue': price, 'valuation': valuation,
                    'crypto': crypto})
    return out


def normalize_cash(cash):
    """Réponse `cash` ([{"currencyId": "EUR", "amount": 12.3}]) -> solde en euros, ou None."""
    entries = cash if isinstance(cash, list) else [cash]
    for entry in entries:
        if isinstance(entry, dict) and (entry.get('currencyId') or 'EUR') == 'EUR':
            value = _dec(entry.get('amount'))
            return None if value is None else value.quantize(Decimal('0.01'))
    return None


# --------------------------------------------------------------------------- client


def _default_session():
    import requests
    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    return session


def _default_ws(cookie, timeout):
    from websockets.sync.client import connect
    try:
        import certifi   # mêmes autorités que `requests` (sur Android, le magasin du système n'est pas toujours lisible)
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    # `__enter__` : forme demandée par websockets 17 pour obtenir la connexion ; `fetch` la ferme toujours (`close`).
    return connect(WS_URL, ssl=context, additional_headers={'Cookie': cookie},
                   user_agent_header=USER_AGENT, open_timeout=timeout, max_size=8 * 1024 * 1024).__enter__()


class TRClient:
    """Une connexion à Trade Republic. `session_factory` / `ws_factory` / `clock` / `sleep` sont remplaçables
    dans les tests (aucun test ne touche le réseau)."""

    def __init__(self, device_id, locale='fr', app_version=None, session_factory=None, ws_factory=None,
                 clock=time.time, sleep=time.sleep):
        self._session = (session_factory or _default_session)()
        self._ws_factory = ws_factory or _default_ws
        self._clock, self._sleep = clock, sleep
        self._locale = locale
        self._device_id = device_id
        self.app_version = app_version or APP_VERSION
        self._process_id = None
        self._required_action = None
        self._refreshed_at = 0.0
        self._sec_acc_no = None
        self.logged_in = False

    # -- HTTP ----------------------------------------------------------------------

    def _login_headers(self):
        return {'X-TR-Device-Info': device_info(self._device_id, self._locale),
                'X-TR-App-Version': self.app_version, 'X-Tr-Platform': WEB_PLATFORM,
                'Accept-Language': self._locale}

    def _call(self, method, path, **kwargs):
        try:
            return self._session.request(method, f'{HOST}{path}', timeout=30, **kwargs)
        except Exception as error:   # requests.RequestException : réseau, certificat, délai…
            raise TRError(f'Trade Republic injoignable ({type(error).__name__}).') from None

    @staticmethod
    def _json(response):
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def refresh_app_version(self):
        """Relit la version du site sur sa page de connexion (aucun identifiant envoyé). Sans effet si illisible."""
        try:
            page = self._session.get('https://app.traderepublic.com/login', timeout=15)
            version = parse_app_version(page.text) if page.status_code == 200 else None
        except Exception:   # noqa: BLE001 — simple confort : la version intégrée reste utilisée
            version = None
        if version:
            self.app_version = version
        return version

    # -- connexion -----------------------------------------------------------------

    def start_login(self, phone, pin):
        """Demande de connexion : UNE notification part vers le téléphone. Renvoie 'app_confirmation' (valider
        dans l'appli Trade Republic) ou 'authenticator_code' (code de l'appli d'authentification à fournir)."""
        phone = normalize_phone(phone)
        r = self._call('POST', '/api/v2/auth/web/login', json={'phoneNumber': phone, 'pin': str(pin)},
                       headers=self._login_headers())
        data = self._json(r)
        if r.status_code >= 400:
            raise login_error(r.status_code, error_code(data))
        self._process_id = data.get('processId')
        if not self._process_id:
            raise TRError('Réponse inattendue de Trade Republic à la demande de connexion.')
        self._started_at = self._clock()
        process = self._process(quiet=True)
        self._required_action = process.get('requiredAction')
        return 'authenticator_code' if self._required_action == 'AUTHENTICATOR_VERIFICATION' else 'app_confirmation'

    @property
    def login_pending(self):
        return bool(self._process_id) and not self.logged_in

    def _process(self, quiet=False):
        r = self._call('GET', f'/api/v2/auth/web/login/processes/{self._process_id}', headers=self._login_headers())
        data = self._json(r)
        if r.status_code >= 400:
            if quiet:
                return {}
            raise login_error(r.status_code, error_code(data))
        return data

    def finish_login(self, code=None, interval=2.0):
        """Attend la validation dans l'appli (au plus ~2 min), puis vérifie la session. Renvoie les cookies."""
        if not self._process_id:
            raise TRError('Connexion non démarrée.')
        try:
            if self._required_action == 'AUTHENTICATOR_VERIFICATION':
                if not code:
                    raise TRBadCredentials('Code de l’application d’authentification manquant.')
                r = self._call('POST', f'/api/v2/auth/web/login/processes/{self._process_id}/authenticator-verification',
                               json={'code': str(code).strip()}, headers=self._login_headers())
                if r.status_code >= 400:
                    raise login_error(r.status_code, error_code(self._json(r)))
            process = self._process()
            deadline = process_deadline(process, self._started_at)
            while True:
                status = process.get('status')
                if status in ('CONFIRMED', 'COMPLETED'):
                    break
                if status != 'PENDING':
                    raise TRLoginExpired('Connexion refusée dans l’application Trade Republic.')
                if self._clock() >= deadline:
                    raise TRLoginExpired('La connexion n’a pas été validée à temps dans l’application Trade Republic : '
                                         'relance la synchronisation et valide la notification dans les 2 minutes.')
                self._sleep(interval)
                process = self._process()
        finally:
            self._process_id = None   # une demande ne sert qu'une fois, réussie ou non
        self._refreshed_at = 0.0
        self._account()
        self.logged_in = True
        return self.export_cookies()

    def resume(self, cookies):
        """Reprend une session depuis des cookies enregistrés, sans validation. False = session morte.
        Ne tente jamais de reconnexion ; le PIN n'est pas nécessaire."""
        if not cookies_into(self._session.cookies, cookies):
            return False
        try:
            self._account()
        except TRNeedsLogin:
            self._session.cookies.clear()
            return False
        self.logged_in = True
        return True

    def export_cookies(self):
        """Cookies actuels (renouvelés par Trade Republic au fil des appels) : à ré-enregistrer après chaque synchro."""
        return cookies_to_list(self._session.cookies)

    def _refresh(self):
        if self._clock() - self._refreshed_at < SESSION_REFRESH_SECONDS:
            return
        r = self._call('GET', '/api/v1/auth/web/session')
        if r.status_code in (401, 403):
            raise TRNeedsLogin('Session Trade Republic expirée.')
        if r.status_code >= 400:
            raise TRError(f'Trade Republic a refusé de prolonger la session ({r.status_code}).')
        self._refreshed_at = self._clock()

    def _account(self):
        self._refresh()
        r = self._call('GET', '/api/v2/auth/account')
        if r.status_code in (401, 403):
            raise TRNeedsLogin('Session Trade Republic expirée.')
        if r.status_code >= 400:
            raise TRError(f'Trade Republic a refusé la lecture du compte ({r.status_code}).')
        data = self._json(r)
        if data.get('securitiesAccountNumber'):
            self._sec_acc_no = data['securitiesAccountNumber']
        return data

    # -- lecture -------------------------------------------------------------------

    def fetch(self, since=None, timeout=15.0):
        """Lit espèces, positions (avec nom et cours) et historique depuis `since` (date).
        Renvoie {'cash', 'investments', 'transactions', 'has_securities'}."""
        if not self.logged_in:
            raise TRNeedsLogin('Session Trade Republic absente : connexion nécessaire.')
        self._account()   # prolonge la session avant d'ouvrir le websocket
        try:
            ws = self._ws_factory(cookie_header(self._session.cookies), timeout)
        except Exception as error:
            raise TRError(f'Connexion aux données Trade Republic impossible ({type(error).__name__}).') from None
        try:
            return self._read(ws, since, timeout)
        except TRError:
            raise
        except Exception as error:   # délai dépassé, connexion coupée, JSON illisible…
            raise TRError(f'Lecture Trade Republic interrompue ({sanitize_error(error)}).') from None
        finally:
            try:
                ws.close()
            except Exception:   # noqa: BLE001
                pass

    def _read(self, ws, since, timeout):
        ws.send('connect 31 ' + json.dumps({'locale': self._locale, 'platformId': 'webtrading',
                                            'platformVersion': 'chrome - 146.0.0', 'clientId': 'app.traderepublic.com',
                                            'clientVersion': '5582'}))
        if parse_frame(ws.recv(timeout))[0] != 'connected':
            raise TRNeedsLogin('Trade Republic a refusé l’accès aux données : reconnecte-toi.')
        counter = [0]

        def ask(payload, optional=False):
            if payload.get('type') not in READ_ONLY_TOPICS:
                raise TRError(f"Sujet interdit : {payload.get('type')!r}")
            counter[0] += 1
            sub_id = str(counter[0])
            ws.send(f'sub {sub_id} {json.dumps(payload)}')
            try:
                while True:
                    got_id, code, text = parse_frame(ws.recv(timeout))
                    if got_id != sub_id:
                        continue
                    if code == 'A':
                        return json.loads(text) if text else {}
                    if code == 'E':
                        try:
                            refused = error_code(json.loads(text)) == 'AUTHENTICATION_ERROR'
                        except ValueError:
                            refused = False
                        if refused:   # vu en réel : sans session valable, le websocket se connecte puis refuse chaque sujet
                            raise TRNeedsLogin('Session Trade Republic expirée.')
                        if optional:
                            return None
                        raise TRError(f"Trade Republic n’a pas fourni « {payload['type']} ».")
                    if code == 'C':
                        return None
            except TimeoutError:
                if optional:
                    return None
                raise
            finally:
                try:
                    ws.send(f'unsub {sub_id}')
                except Exception:   # noqa: BLE001
                    pass

        cash = ask({'type': 'cash'})
        positions = []
        if self._sec_acc_no:
            positions = flatten_positions(ask({'type': 'compactPortfolioByType', 'secAccNo': self._sec_acc_no}))
        instruments, tickers = {}, {}
        for pos in positions:
            code = pos['code']
            info = ask({'type': 'instrument', 'id': code}, optional=True)
            if not isinstance(info, dict):
                continue
            instruments[code] = info
            exchanges = info.get('exchangeIds') or []
            if exchanges:
                ticker = ask({'type': 'ticker', 'id': f'{code}.{exchanges[0]}'}, optional=True)
                if isinstance(ticker, dict):
                    tickers[code] = ticker
        items, after = [], None
        for _ in range(MAX_TIMELINE_PAGES):
            page = ask({'type': 'timelineTransactions', 'after': after}) or {}
            batch = page.get('items') or []
            items.extend(batch)
            after = (page.get('cursors') or {}).get('after')
            oldest = min((d for d in map(event_date, batch) if d), default=None)
            if not after or not batch or (since is not None and oldest is not None and oldest < since):
                break
        return {'cash': normalize_cash(cash),
                'investments': normalize_investments(positions, instruments, tickers),
                'transactions': normalize_transactions(items, since),
                'has_securities': bool(self._sec_acc_no)}


def credentials_fingerprint(salt, phone, pin):
    """Empreinte d'un couple numéro + PIN refusé, pour ne jamais le renvoyer une 2e fois (des PIN faux répétés
    peuvent faire bloquer le compte). Rangée dans le stockage chiffré de l'agent."""
    import hashlib
    return hashlib.sha256(f'{salt}|{phone}|{pin}'.encode()).hexdigest()


# Après un refus « trop de tentatives », on n'envoie plus rien pendant ce délai : chaque essai prolonge le blocage.
RATE_LIMIT_PAUSE_SECONDS = 3 * 3600


def history_floor(today=None, days=100):
    """Jusqu'où remonter dans l'historique (l'agent n'envoie jamais plus de 90 jours)."""
    return (today or date.today()) - timedelta(days=days)
