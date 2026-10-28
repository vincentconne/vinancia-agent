"""
Mise en forme du lot envoyé à Vinancia, sans dépendre de Woob (testable seul).

Les objets Woob (comptes, opérations) sont lus par simple getattr : les tests
passent de faux objets, l'agent passe les vrais.
"""
import re
import hashlib
from datetime import date, datetime, timedelta

# Fenêtre d'historique : complète à la première synchro d'une banque, puis un
# recouvrement avec la synchro précédente pour rattraper les opérations que la
# banque publie avec retard (débits différés, prélèvements comptabilisés tard).
FIRST_SYNC_DAYS = 90
OVERLAP_DAYS = 10
# Une fois la fenêtre dépassée, on s'arrête après ce nombre d'opérations plus
# anciennes d'affilée : la plupart des modules listent du plus récent au plus
# ancien, mais pas tous strictement.
STOP_AFTER_OLDER = 20
MAX_TRANSACTIONS = 3000


def account_ref(salt, module, bank_account_id):
    """Référence opaque d'un compte : ni IBAN ni numéro de compte n'est envoyé."""
    basis = f"{salt}|{module}|{bank_account_id}"
    return hashlib.sha256(basis.encode('utf-8')).hexdigest()[:32]


def type_names(account_class):
    """{valeur Account.TYPE_* : 'checking', 'livret_a', …} à partir de la classe Woob."""
    return {
        getattr(account_class, name): name[len('TYPE_'):].lower()
        for name in dir(account_class)
        if name.startswith('TYPE_') and isinstance(getattr(account_class, name), int)
    }


def history_since(last_sync_iso, today=None):
    today = today or date.today()
    if last_sync_iso:
        try:
            last = datetime.fromisoformat(last_sync_iso).date()
            return min(last, today) - timedelta(days=OVERLAP_DAYS)
        except ValueError:
            pass
    return today - timedelta(days=FIRST_SYNC_DAYS)


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _decimal(value):
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _text(value):
    # Woob utilise des valeurs spéciales (NotLoaded, NotAvailable) qui ne sont pas
    # des chaînes : on ne garde que du texte réel.
    return value.strip() if isinstance(value, str) else ''


def collect_transactions(history, since):
    """
    Opérations comptabilisées depuis `since`, prêtes à envoyer.

    Les opérations à venir (`coming`) sont exclues : elles réapparaissent plus tard,
    comptabilisées, avec un autre libellé ou identifiant, et feraient doublon.
    """
    out = []
    older_in_a_row = 0
    for tx in history:
        if len(out) >= MAX_TRANSACTIONS:
            break
        if getattr(tx, 'coming', False) is True:
            continue
        day = _as_date(getattr(tx, 'date', None)) or _as_date(getattr(tx, 'rdate', None))
        amount = _decimal(getattr(tx, 'amount', None))
        if day is None or amount is None or amount == 0:
            continue
        if day < since:
            older_in_a_row += 1
            if older_in_a_row >= STOP_AFTER_OLDER:
                break
            continue
        older_in_a_row = 0
        label = _text(getattr(tx, 'label', None))
        raw = _text(getattr(tx, 'raw', None))
        if not (label or raw):
            continue
        tx_id = getattr(tx, 'id', None)
        out.append({
            'id': tx_id if isinstance(tx_id, str) and tx_id else None,
            'date': day.isoformat(),
            'amount': amount,
            'label': label or raw,
            'raw': raw or label,
            'category': _text(getattr(tx, 'category', None)) or None,
        })
    return out


# Comptes dont on lit les positions (titres, unités de compte) en plus du solde.
INVESTMENT_KINDS = {'market', 'pea', 'life_insurance', 'capitalisation', 'per', 'perp', 'perco',
                    'madelin', 'article_83', 'pee'}


def collect_investments(investments):
    """Positions Woob (Investment) → dicts sérialisables ; les valeurs spéciales de Woob sont ignorées."""
    out = []
    for inv in investments:
        out.append({
            'code': _text(getattr(inv, 'code', None)) or None,
            'symbol': _text(getattr(inv, 'stock_symbol', None)) or None,
            'label': _text(getattr(inv, 'label', None)) or None,
            'quantity': _decimal_q(getattr(inv, 'quantity', None)),
            'unitprice': _decimal_q(getattr(inv, 'unitprice', None)),
            'unitvalue': _decimal_q(getattr(inv, 'unitvalue', None)),
            'valuation': _decimal(getattr(inv, 'valuation', None)),
            'currency': _text(getattr(inv, 'original_currency', None)) or None,
        })
    return out


def _decimal_q(value):
    """Quantités et cours unitaires : plus de 2 décimales (fractions de parts, cryptos)."""
    if value is None:
        return None
    try:
        return round(float(value), 8)
    except (TypeError, ValueError):
        return None


def build_account(salt, module, account, kinds, transactions, full_history=False, investments=None):
    return {
        'ref': account_ref(salt, module, getattr(account, 'id', '')),
        'bank': module,
        'label': _text(getattr(account, 'label', None)) or 'Compte bancaire',
        'kind': kinds.get(getattr(account, 'type', None), 'unknown'),
        'currency': _text(getattr(account, 'currency', None)) or 'EUR',
        'balance': _decimal(getattr(account, 'balance', None)),
        'transactions': transactions,
        # Tout l'historique disponible (première synchro, ou compte tout juste relié
        # dans Vinancia) et pas seulement les derniers jours.
        'full_history': full_history,
        # None : la banque ne détaille pas les positions de ce compte (seul le solde compte).
        'investments': investments,
    }


def split_credentials(values):
    """{id de champ: valeur} saisi dans le formulaire → (identifiant, mot de passe, autres paramètres)."""
    params = {k: v for k, v in values.items() if v and k not in ('login', 'password')}
    return values.get('login', ''), values.get('password', ''), params


def last_sync_label(iso, today=None):
    """« aujourd'hui », « hier », « il y a 3 jours », ou « jamais »."""
    if not iso:
        return 'jamais'
    try:
        day = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return 'jamais'
    days = ((today or date.today()) - day).days
    if days <= 0:
        return "aujourd'hui"
    if days == 1:
        return 'hier'
    return f'il y a {days} jours'


def serialize_field(value):
    """Champ Woob (identifiant, mot de passe, région…) → description pour le formulaire de la fenêtre."""
    field = {
        'id': value.id,
        'label': value.label or value.description or value.id,
        'type': 'password' if value.masked else 'text',
        'default': value.default if isinstance(value.default, str) else '',
        'required': bool(value.required),
    }
    if value.choices:
        field['type'] = 'select'
        field['choices'] = [{'value': k, 'label': str(v)} for k, v in value.choices.items()]
        if not field['default'] and field['choices']:
            field['default'] = field['choices'][0]['value']
    return field


# ─── Journaux sans secrets ────────────────────────────────────────────────────
# Les messages d'erreur des banques et les traces peuvent citer un identifiant, un code
# reçu par SMS ou un numéro de compte. Le journal (sync.log) est celui qu'un utilisateur
# nous envoie en cas de souci : il ne doit rien contenir de sensible.

_known_secrets = set()
_SECRET_KEY_HINTS = ('login', 'pass', 'secret', 'key', 'token', 'pin', 'otp', 'code')
_ASSIGNMENT_RE = re.compile(
    r'(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|otp|code|authorization)\b(\s*[=:]\s*)(bearer\s+)?[^\s,;&\'"]+'
)
_AGENT_TOKEN_RE = re.compile(r'vna_[A-Za-z0-9_\-]+')
_LONG_DIGITS_RE = re.compile(r'\b\d{8,}\b')
_EMAIL_RE = re.compile(r'[\w.+\-]+@([\w\-]+\.[\w.\-]+)')


def remember_secret(value):
    """Mémorise une valeur secrète (identifiant, mot de passe…) pour la masquer partout."""
    text = str(value or '')
    if len(text) >= 4:
        _known_secrets.add(text)


def remember_secrets(params):
    """Mémorise les valeurs sensibles d'un dictionnaire de paramètres de banque."""
    for key, value in (params or {}).items():
        if isinstance(value, str) and any(hint in str(key).lower() for hint in _SECRET_KEY_HINTS):
            remember_secret(value)


def redact(text):
    """Masque secrets connus, jetons, mots de passe affectés, longs numéros et e-mails."""
    out = str(text)
    for secret in sorted(_known_secrets, key=len, reverse=True):
        out = out.replace(secret, '***')
    out = _AGENT_TOKEN_RE.sub('vna_***', out)
    out = _ASSIGNMENT_RE.sub(lambda m: f'{m.group(1)}{m.group(2)}***', out)
    out = _LONG_DIGITS_RE.sub(lambda m: '*' * (len(m.group(0)) - 4) + m.group(0)[-4:], out)
    out = _EMAIL_RE.sub(lambda m: '***@' + m.group(1), out)
    return out
