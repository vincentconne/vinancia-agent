"""
Cœur de l'agent Vinancia, sans interface : configuration, secrets, Woob, synchro,
appairage, planification. Utilisé par la fenêtre (agent_gui.py), la ligne de
commande et le mode planifié (vinancia_agent.py).

Toute interaction avec l'utilisateur (code SMS, validation dans l'appli de la
banque) passe par un objet `Interaction` fourni par l'interface. Sans interaction
(mode planifié), aucune validation n'est jamais déclenchée.
"""
import contextlib
import json
import logging
import os
import platform
import re
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import keyring
import requests

from agent_payload import (  # noqa: F401
    INVESTMENT_KINDS, build_account, collect_investments, collect_transactions, history_since,
    redact, remember_secret, remember_secrets, split_credentials, type_names,
)

AGENT_VERSION = '2.0.2'
DEFAULT_API_URL = 'https://myfinances-production.up.railway.app'
WEB_URL = 'https://vinancia.com'
# Dépôt public où sont publiées les versions de l'agent (exécutables + empreintes).
RELEASES_REPO = 'vincentconne/vinancia-agent'
KEYRING_SERVICE = 'vinancia-agent'
APP_NAME = 'Vinancia Agent'
TASK_NAME = 'Vinancia Agent'
LAUNCHD_LABEL = 'com.vinancia.agent'
SYNC_EVERY_HOURS = 6

# Empreinte de la clé qui signe les modules bancaires de woob.tech. Woob fait
# confiance à la première clé reçue (les suivantes doivent être signées par elle) :
# on refuse toute autre clé que celle-ci, pour qu'un réseau piégé ne puisse pas
# faire accepter de faux modules lors de la toute première installation.
WOOB_KEYRING_SHA1 = 'b5fc136c2d500b8483696a0ee722865842a4cc45'

# Seuls les comptes du quotidien sont lus en détail : Vinancia n'importe pas les
# opérations des livrets (elles doublonneraient celles du compte courant).
HISTORY_KINDS = {'checking', 'joint'}

COMMON_BANKS = [
    ('boursorama', 'BoursoBank'),
    ('bnp', 'BNP Paribas'),
    ('societegenerale', 'Société Générale'),
    ('cragr', 'Crédit Agricole'),
    ('caissedepargne', "Caisse d'Épargne"),
    ('banquepopulaire', 'Banque Populaire'),
    ('lcl', 'LCL'),
    ('creditmutuel', 'Crédit Mutuel'),
    ('cic', 'CIC'),
    ('bp', 'La Banque Postale'),
    ('fortuneo', 'Fortuneo'),
    ('hellobank', 'Hello bank!'),
    ('bforbank', 'BforBank'),
    ('ing', 'ING'),
    ('n26', 'N26'),
    ('creditcooperatif', 'Crédit Coopératif'),
    ('axabanque', 'AXA Banque'),
    ('bred', 'BRED'),
    ('degiro', 'DEGIRO'),
    ('boursedirect', 'Bourse Direct'),
    ('binance', 'Binance'),
    ('yomoni', 'Yomoni'),
    ('nalo', 'Nalo'),
]
BANK_NAMES = dict(COMMON_BANKS)

log = logging.getLogger('vinancia_agent')


class AgentError(Exception):
    """Erreur à montrer telle quelle à l'utilisateur."""


class Cancelled(AgentError):
    def __init__(self):
        super().__init__('Opération annulée.')


class Interaction:
    """Ce que l'interface doit savoir faire pendant une connexion bancaire."""

    def ask(self, field):
        """Demande une valeur Woob (code SMS…) ; renvoie la chaîne saisie."""
        raise NotImplementedError

    def app_validation(self, message):
        """Bloque jusqu'à ce que l'utilisateur ait validé sur son téléphone."""
        raise NotImplementedError

    def redirect(self, url):
        """Fait ouvrir `url` et renvoie l'adresse finale collée par l'utilisateur."""
        raise NotImplementedError


# ─── Fichiers locaux ─────────────────────────────────────────────────────────

def is_android():
    """Vrai dans l'application Android (Chaquopy : platform.system() y répond « Linux »)."""
    return bool(os.environ.get('VINANCIA_ANDROID'))


def agent_dir():
    system = platform.system()
    if system == 'Windows':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        path = os.path.join(base, 'VinanciaAgent')
    elif system == 'Darwin':
        path = os.path.expanduser('~/Library/Application Support/VinanciaAgent')
    else:
        base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
        path = os.path.join(base, 'vinancia-agent')
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _config_path():
    return os.path.join(agent_dir(), 'config.json')


def load_config():
    try:
        with open(_config_path(), encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault('api_url', DEFAULT_API_URL)
    cfg.setdefault('banks', [])
    cfg.setdefault('last_sync', {})
    cfg.setdefault('status', {})
    return cfg


def save_config(cfg):
    path = _config_path()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def bank_display_name(bank):
    return BANK_NAMES.get(bank['module'], bank['module'])


# ─── Secrets (trousseau du système) ──────────────────────────────────────────

def check_keyring():
    """Refuse de continuer sans vrai trousseau : jamais de secret en clair sur disque."""
    from keyring.backends import fail

    backend = keyring.get_keyring()
    name = type(backend).__module__.lower() + type(backend).__name__.lower()
    if isinstance(backend, fail.Keyring) or 'null' in name or 'plaintext' in name:
        raise AgentError(
            "Aucun trousseau sécurisé n'est disponible sur cet ordinateur. "
            "Sous Linux, installe gnome-keyring ou KWallet."
        )


def secret_get(key):
    return keyring.get_password(KEYRING_SERVICE, key)


def secret_set(key, value):
    keyring.set_password(KEYRING_SERVICE, key, value)


def secret_delete(key):
    with contextlib.suppress(Exception):
        keyring.delete_password(KEYRING_SERVICE, key)


def is_connected():
    return bool(secret_get('api_token'))


# ─── Sessions bancaires chiffrées ────────────────────────────────────────────

def _session_cipher():
    from cryptography.fernet import Fernet

    key = secret_get('session_key')
    if not key:
        key = Fernet.generate_key().decode('ascii')
        secret_set('session_key', key)
    return Fernet(key.encode('ascii'))


def make_storage(path):
    """
    Stockage Woob chiffré : les cookies de session bancaire (qui évitent de refaire
    la double authentification) ne sont jamais écrits en clair. La clé est dans le
    trousseau ; un fichier illisible (clé perdue) repart simplement de zéro.
    """
    import yaml
    from woob.tools.config.yamlconfig import YamlConfig
    from woob.tools.storage import StandardStorage

    cipher = _session_cipher()

    class EncryptedYamlConfig(YamlConfig):
        def load(self, default={}):
            self.values = dict(default)
            try:
                with open(self.path, 'rb') as f:
                    data = cipher.decrypt(f.read())
                self.values = yaml.load(data.decode('utf-8'), Loader=self.LOADER) or {}
            except FileNotFoundError:
                pass
            except Exception:
                log.warning('Sessions bancaires illisibles : elles sont réinitialisées.')
                self.values = dict(default)

        def save(self):
            text = yaml.dump(self.values, Dumper=self.DUMPER, default_flow_style=False)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))
            with os.fdopen(fd, 'wb') as f:
                f.write(cipher.encrypt(text.encode('utf-8')))
            os.replace(tmp, self.path)
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)

    class EncryptedStorage(StandardStorage):
        def __init__(self, storage_path):
            self.config = EncryptedYamlConfig(storage_path)
            self.config.load()

    return EncryptedStorage(path)


# ─── Woob ────────────────────────────────────────────────────────────────────

def _progress(callback=None):
    from woob.core.repositories import IProgress

    class PinnedProgress(IProgress):
        def progress(self, percent, message):
            if callback:
                callback(percent, message)

        def error(self, message):
            log.warning('woob: %s', message)

        def prompt(self, message):
            # Seule question posée par Woob : faire confiance à la clé de signature.
            return WOOB_KEYRING_SHA1 in message

    return PinnedProgress()


def use_builtin_signature_check():
    """Remplace la vérification par `gpg` de Woob par celle de `pgp_verify` (Python pur).

    Woob ne vérifie les modules que si gpg est installé (donc presque jamais chez
    un utilisateur) et la vérifie mal avec le gpg de Git pour Windows. Ici la
    signature est contrôlée à chaque installation, sur toutes les machines.
    """
    from woob.core.repositories import Keyring
    from pgp_verify import verify_detached

    def is_valid(self, data, sigdata):
        with open(self.path, 'rb') as f:
            return verify_detached(f.read(), data, sigdata)

    Keyring.find_gpg = staticmethod(lambda: 'pgp_verify')  # active le contrôle côté Woob
    Keyring.find_gpgv = staticmethod(lambda: None)
    Keyring.is_valid = is_valid


def make_woob():
    from woob.core import Woob
    from woob.core.repositories import DEFAULT_SOURCES_LIST

    use_builtin_signature_check()

    workdir = os.path.join(agent_dir(), 'woob')
    os.makedirs(workdir, mode=0o700, exist_ok=True)
    # Sans ce fichier, Woob télécharge ses dépôts dès sa création en acceptant
    # n'importe quelle clé : on le crée nous-mêmes pour garder la main (cf. _progress).
    sources = os.path.join(workdir, 'sources.list')
    if not os.path.exists(sources):
        with open(sources, 'w', encoding='utf-8') as f:
            f.write(DEFAULT_SOURCES_LIST)
    # Ancien fichier de sessions en clair (version 1) : remplacé par le stockage chiffré.
    with contextlib.suppress(FileNotFoundError):
        os.remove(os.path.join(workdir, 'sessions'))
    return Woob(workdir=workdir, datadir=workdir, storage=make_storage(os.path.join(workdir, 'sessions.enc')))


def update_modules(w, progress=None):
    w.repositories.update(progress=_progress(progress))


def ensure_module(w, module, progress=None):
    """Installe le module de la banque (téléchargé depuis woob.tech et vérifié)."""
    from woob.exceptions import ModuleInstallError
    from woob.core.repositories import RepositoryUnavailable

    try:
        minfo = w.repositories.get_module_info(module)
        if minfo is None:
            update_modules(w, progress)
            minfo = w.repositories.get_module_info(module)
        if minfo is None:
            raise AgentError(f'Banque inconnue : {module}')
        if not minfo.is_installed():
            w.repositories.install(module, progress=_progress(progress))
    except RepositoryUnavailable as e:
        raise AgentError(f'Téléchargement du module bancaire refusé ({e}). Mets à jour Vinancia Agent.') from e
    except ModuleInstallError as e:
        raise AgentError(f'Installation du module bancaire impossible : {e}') from e
    return w.modules_loader.get_or_load_module(module)


def bank_fields(loaded_module):
    """Champs à demander pour ajouter la banque : identifiant, mot de passe, puis les extras."""
    config = loaded_module.config
    # La plupart des modules ont identifiant + mot de passe, mais pas tous.
    fields = [config[key] for key in ('login', 'password') if key in config]
    for key, value in config.items():
        if key in ('login', 'password') or value.transient:
            continue
        if value.required or value.choices:
            fields.append(value)
    return fields


def open_backend(w, bank, password, interactive, fresh=False):
    params = dict(bank.get('params') or {})
    # Champs secrets : en mémoire pendant le test d'ajout, dans le trousseau ensuite.
    params.update(bank.get('secrets') or {})
    for field in bank.get('secret_params') or []:
        value = secret_get(f"bank:{bank['name']}:{field}")
        if value:
            params[field] = value
    if bank.get('login'):
        params['login'] = bank['login']
    if password:
        params['password'] = password
    remember_secrets(params)
    backend = w.build_backend(bank['module'], params, storage=w.storage, name=bank['name'])
    if fresh:
        # Repart d'une connexion neuve : la session enregistrée (cookies) peut être refusée par la banque.
        backend.storage.set('browser_state', {})
    # request_information non vide = « un humain est là » : le module peut alors
    # déclencher un SMS ou une validation appli. Vide (mode planifié), il lève
    # NeedInteractive au lieu d'envoyer des codes à l'insu de l'utilisateur.
    if interactive and 'request_information' in backend.config:
        backend.config['request_information'].set({})
    if bank['module'] == 'cragr':
        try:
            _patch_cragr(backend)
        except Exception as error:  # noqa: BLE001 — sans le correctif, le module marche comme avant
            log.warning('correctif Crédit Agricole non appliqué : %s', type(error).__name__)
    return backend


def fetch_accounts(backend, interaction=None):
    from woob.exceptions import BrowserQuestion, BrowserRedirect, DecoupledValidation

    for _ in range(6):
        try:
            return list(backend.iter_accounts())
        except BrowserQuestion as question:
            if interaction is None:
                raise
            for field in question.fields:
                backend.config[field.id].set(interaction.ask(field))
        except DecoupledValidation as validation:
            if interaction is None:
                raise
            interaction.app_validation(str(validation) or 'Valide la connexion dans l’application de ta banque.')
            if 'resume' in backend.config:
                backend.config['resume'].set(True)
        except BrowserRedirect as redirect:
            if interaction is None:
                raise
            backend.config['auth_uri'].set(interaction.redirect(redirect.url))
    raise AgentError('Trop de questions de sécurité successives.')


def http_error_where(error):
    """« 403 sur host/chemin » (sans paramètres ni données de la requête) pour savoir quelle étape est refusée."""
    from urllib.parse import urlparse

    response = getattr(error, 'response', None)
    url = getattr(response, 'url', None) or getattr(getattr(error, 'request', None), 'url', None)
    if not url:
        return ''
    parsed = urlparse(str(url))
    path = re.sub(r'/\d{4,}', '/…', parsed.path)[:70]
    status = getattr(response, 'status_code', '')
    return f'{status} sur {parsed.netloc}{path}'.strip()


# ─── Crédit Agricole : parcours « après connexion » ──────────────────────────
#
# Après chaque connexion, le site du Crédit Agricole lit GET /bff/api/interstitials/post_authentication_journey_sequence
# (écrans à afficher avant d'aller plus loin : première connexion, mise à jour KYC…). Le module Woob ne fait pas cet
# appel ; la banque refuse alors l'accès au détail des comptes (« Interstitial block »). On fait la même lecture que le
# site, sans jamais valider un écran à la place de l'utilisateur.

CA_JOURNEY_PATH = '/bff/api/interstitials/post_authentication_journey_sequence'
CA_STEP_LABELS = {
    'NOTIFICATION-KYC': 'mettre à jour tes informations personnelles',
    'NOTIFICATION-EAI': 'lire une notification de ta banque',
    'PREMIERE-CONNEXION': 'accepter les conditions de ton espace en ligne (écran « Bienvenue dans votre espace personnel », bouton « Commencer »)',
    'PORTAIL-ENTREPRISE': 'passer par le portail entreprise',
    'PERSONNALISER-CODE-PERSONNEL': 'changer ton code personnel',
}


def ca_journey_steps(data):
    """Étapes que le Crédit Agricole demande avant d'aller plus loin (codes connus seulement, jamais de donnée perso)."""
    if not isinstance(data, dict):
        return []
    steps = [str(code) for code in (data.get('interstitials') or []) if isinstance(code, str)]
    if data.get('isKYCRequired') and 'NOTIFICATION-KYC' not in steps:
        steps.append('NOTIFICATION-KYC')
    return [code for code in steps if re.fullmatch(r'[A-Z0-9_/-]{2,60}', code)]


def ca_steps_message(steps):
    """Phrase lisible pour l'utilisateur, ou '' si rien n'est demandé."""
    labels = [CA_STEP_LABELS.get(code, f'valider l’étape « {code} »') for code in steps]
    if not labels:
        return ''
    return ('le Crédit Agricole te demande de ' + ', puis '.join(labels)
            + ' : connecte-toi sur credit-agricole.fr (le site, pas l’appli), fais-le, puis relance la synchro.')


def _patch_cragr(backend):
    """Ajoute au navigateur du module cragr la lecture du parcours après connexion, avant l'ouverture du détail."""
    browser = backend.browser
    original = getattr(browser, '_open_detail_dav', None)
    if original is None or getattr(browser, '_vna_patched', False):
        return

    def open_detail(account):
        if getattr(browser, '_vna_journey', None) is None:
            try:
                browser._vna_journey = browser.open(browser.BASEURL + CA_JOURNEY_PATH).json()
            except Exception as error:  # noqa: BLE001 — le détail est tenté quand même
                log.warning('parcours Crédit Agricole illisible : %s', type(error).__name__)
                browser._vna_journey = {}
        return original(account)

    browser._open_detail_dav = open_detail
    browser._vna_patched = True


# Encodages que libxml2 sait lire sans iconv (cas d'Android) ; les autres sont convertis en UTF-8 avant analyse.
LIBXML_NATIVE_ENCODINGS = frozenset({'utf-8', 'utf8', 'ascii', 'us-ascii', 'iso-8859-1', 'latin1', 'latin-1'})


def _to_utf8(content, encoding, native):
    """Contenu ré-encodé en UTF-8 si libxml2 ne connaît pas `encoding`, sinon None (rien à faire)."""
    if not encoding or encoding.lower() in native:
        return None
    try:
        return content.decode(encoding).encode('utf-8')
    except (LookupError, UnicodeDecodeError):
        return None


def ensure_dist_stub(name, version, directory):
    """
    Déclare un paquet Python « installé » sans l'être : crée `<directory>/<name>-<version>.dist-info/METADATA` et
    ajoute `directory` à `sys.path`. Woob refuse de charger un module bancaire dont le `requirements.txt` cite un
    paquet absent (`importlib.metadata.distribution`), même quand le code du module ne l'importe jamais. Utilisé sur
    Android pour `chompjs` (extension C sans version Android) : Caisse d'Épargne et Banque Populaire n'en importent
    rien, seule la liste de dépendances le réclame. Si un module en avait réellement besoin, l'import échouerait
    proprement à l'usage.
    """
    info = os.path.join(directory, f'{name}-{version}.dist-info')
    os.makedirs(info, exist_ok=True)
    meta = os.path.join(info, 'METADATA')
    if not os.path.exists(meta):
        with open(meta, 'w', encoding='utf-8') as fh:
            fh.write(f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
    if directory not in sys.path:
        sys.path.append(directory)
    return info


def patch_lxml_encodings(native=LIBXML_NATIVE_ENCODINGS):
    """Pages Woob XML / HTML d'un encodage inconnu de libxml2 (windows-1252 sur Android) : converties en UTF-8."""
    from io import BytesIO

    import lxml.etree
    from woob.browser import pages

    if getattr(pages.XMLPage.build_doc, '_vna_patched', False):
        return
    xml_build, html_build = pages.XMLPage.build_doc, pages.HTMLPage.build_doc

    def build_xml(self, content):
        data = _to_utf8(content, self.encoding, native)
        if data is None:
            return xml_build(self, content)
        data = re.sub(rb'^(\s*<\?xml[^>]*?encoding=["\'])[^"\']*', rb'\1utf-8', data, count=1)
        return lxml.etree.parse(BytesIO(data), lxml.etree.XMLParser(encoding='utf-8', resolve_entities=False))

    def build_html(self, content):
        data = _to_utf8(content, self.encoding, native)
        if data is None:
            return html_build(self, content)
        saved = self.response.encoding
        self.response.encoding = 'utf-8'
        try:
            return html_build(self, data)
        finally:
            self.response.encoding = saved

    build_xml._vna_patched = build_html._vna_patched = True
    pages.XMLPage.build_doc = build_xml
    pages.HTMLPage.build_doc = build_html


def bank_requires_step(error):
    """True si la banque répond qu'une étape obligatoire (« interstitiel » : message à lire, consentement,
    mise à jour de ses informations…) doit d'abord être faite sur son site ou son appli."""
    response = getattr(error, 'response', None)
    try:
        return 'interstitial' in (response.text or '')[:500].lower() if response is not None else False
    except Exception:
        return False


def http_error_debug(error, backend=None):
    """Éléments utiles pour comprendre un refus (403…) SANS valeur secrète : noms des en-têtes envoyés, noms et
    domaines des cookies, début de la réponse de la banque (passé par `redact`)."""
    parts = []
    request = getattr(error, 'request', None) or getattr(getattr(error, 'response', None), 'request', None)
    if request is not None:
        wanted = ('x-xsrf-token', 'origin', 'referer', 'cookie', 'content-type')
        sent = sorted(name.lower() for name in getattr(request, 'headers', {}).keys() if name.lower() in wanted)
        parts.append('en-têtes : ' + (', '.join(sent) or 'aucun'))
    try:
        cookies = sorted({f'{c.name}@{(c.domain or "").lstrip(".")}' for c in backend.browser.session.cookies})
        parts.append('cookies : ' + (', '.join(cookies)[:260] or 'aucun'))
    except Exception:  # noqa: BLE001 — simple aide au diagnostic
        pass
    body = getattr(getattr(error, 'response', None), 'text', '') or ''
    snippet = redact(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body))).strip()[:120]
    if snippet:
        parts.append(f'réponse : {snippet}')
    return ' ; '.join(parts)


def _safe_connected():
    try:
        return 'oui' if is_connected() else 'non'
    except Exception:  # noqa: BLE001 — trousseau indisponible : on le dit plutôt que de planter
        return 'inconnu (trousseau indisponible)'


def diagnostic_report(cfg, log_lines=60):
    """Rapport à copier pour le support : versions, état de chaque banque, fin du journal. Tout passe par `redact` :
    ni identifiant, ni mot de passe, ni jeton, ni numéro de compte n'y figure."""
    system = 'Android' if is_android() else platform.system()
    lines = [
        f'Vinancia Agent {AGENT_VERSION} · {system} · Python {platform.python_version()}',
        f'Relié à Vinancia : {_safe_connected()}',
        'Banques :',
    ]
    for bank in cfg.get('banks') or []:
        status = (cfg.get('status') or {}).get(bank['name'], {})
        when = str(status.get('at') or 'jamais')[:16]
        lines.append(f"- {bank['module']} : {status.get('status') or '?'} ({when}) {status.get('message') or ''}".rstrip())
    if not cfg.get('banks'):
        lines.append('- aucune')
    try:
        with open(os.path.join(agent_dir(), 'sync.log'), encoding='utf-8', errors='replace') as f:
            tail = f.read().splitlines()[-log_lines:]
        lines += ['', f'Journal (dernières lignes) :'] + tail
    except OSError:
        lines += ['', 'Journal : vide']
    return redact('\n'.join(lines))


def classify_error(error):
    from woob import exceptions as e

    if isinstance(error, AgentError):
        return 'error', str(error)
    if isinstance(error, e.BrowserIncorrectPassword):
        return 'wrong_password', 'Identifiant ou mot de passe refusé par la banque.'
    if isinstance(error, (e.NeedInteractive, e.BrowserQuestion, e.DecoupledValidation, e.BrowserRedirect)):
        return 'needs_2fa', 'Ta banque redemande une validation : clique sur « Synchroniser maintenant ».'
    if isinstance(error, (e.ScrapingBlocked, e.BrowserForbidden, e.BrowserUserBanned)):
        return 'blocked', 'La banque a refusé la connexion automatique.'
    if isinstance(error, e.ActionNeeded):
        return 'error', f'Action requise sur le site de ta banque : {error}'
    if isinstance(error, e.BrowserUnavailable):
        return 'error', 'Site de la banque indisponible, nouvel essai à la prochaine synchro.'
    if isinstance(error, e.ModuleLoadError):
        return 'error', 'Module bancaire incompatible : mets à jour Vinancia Agent.'
    if isinstance(error, requests.RequestException):
        # Tout problème d'accès au site de la banque (réseau coupé, certificat, délai, redirections…) arrive ici :
        # on donne le type d'erreur plutôt que d'affirmer « pas d'Internet » à tort.
        detail = redact(str(error))[:140]
        step = getattr(error, 'vna_step', None)
        where = f' à l’étape « {step} »' if step else ''
        where_http = http_error_where(error)
        return 'error', f'Connexion à la banque impossible{where} ({where_http or type(error).__name__}) : {detail}'
    return 'error', f'{type(error).__name__}: {error}'[:200]


def _bank_password(bank):
    if not bank.get('has_password', True):
        return ''
    password = secret_get(f"bank:{bank['name']}")
    if not password:
        raise AgentError('Mot de passe absent du trousseau : retire puis rajoute cette banque.')
    return password


def test_bank(w, bank, password, interaction):
    """Première connexion à une banque qu'on s'apprête à ajouter ; renvoie ses comptes."""
    backend = open_backend(w, bank, password, interactive=True)
    try:
        return fetch_accounts(backend, interaction)
    finally:
        with contextlib.suppress(Exception):
            backend.deinit()


def next_bank_name(cfg, module):
    name, n = module, 2
    existing = {b['name'] for b in cfg['banks']}
    while name in existing:
        name, n = f'{module}{n}', n + 1
    return name


def add_bank(cfg, bank, password):
    """
    Range la banque testée ; renvoie son nom. Le mot de passe et tout autre champ
    secret (clé secrète d'API…) vont dans le trousseau, jamais dans config.json.
    """
    name = bank['name']
    if password:
        secret_set(f'bank:{name}', password)
    secrets = bank.get('secrets') or {}
    for field, value in secrets.items():
        secret_set(f'bank:{name}:{field}', value)
    cfg['banks'].append({'name': name, 'module': bank['module'], 'login': bank['login'],
                         'params': bank['params'], 'has_password': bool(password),
                         'secret_params': sorted(secrets)})
    save_config(cfg)
    return name


def remove_bank(cfg, name):
    for bank in cfg['banks']:
        if bank['name'] == name:
            for field in bank.get('secret_params') or []:
                secret_delete(f'bank:{name}:{field}')
    cfg['banks'] = [b for b in cfg['banks'] if b['name'] != name]
    cfg['last_sync'].pop(name, None)
    cfg['status'].pop(name, None)
    save_config(cfg)
    secret_delete(f'bank:{name}')


def separate_secrets(params, fields):
    """Sépare les paramètres secrets (champs masqués : clé secrète d'API…) des autres."""
    masked = {f.id for f in fields if getattr(f, 'masked', False)}
    return ({k: v for k, v in params.items() if k not in masked},
            {k: v for k, v in params.items() if k in masked})


# Aide affichée dans le formulaire pour les établissements qui ne se connectent pas
# avec un identifiant et un mot de passe.
BANK_HINTS = {
    'binance': 'Dans Binance : Profil → Gestion des API → Créer une clé, en cochant uniquement '
               '« Lecture » (aucun retrait ni trading). Colle ici la clé API et la clé secrète.',
}


def new_bank_entry(cfg, module, login, params, secrets=None):
    """
    Banque pas encore enregistrée, pour la connexion de test. Elle porte déjà son nom
    définitif : la session ouverte (et validée par SMS) pendant le test est rangée
    sous ce nom, et la première synchro la réutilise sans redemander de code.
    """
    return {'name': next_bank_name(cfg, module), 'module': module, 'login': login, 'params': params,
            'secrets': secrets or {}}


def _collect_accounts(backend, bank, salt, kinds, interaction, last_sync_iso, backfill=()):
    """Comptes, opérations et positions d'une banque déjà connectée, au format envoyé à Vinancia.

    Une étape refusée par la banque (403…) n'empêche plus le reste : le compte arrive avec son solde, et
    `_issue` (local, retiré avant l'envoi) dit ce qui manque et pourquoi.
    """
    from agent_payload import account_ref

    try:
        accounts = fetch_accounts(backend, interaction)
    except requests.RequestException as error:
        error.vna_step = 'liste des comptes'
        raise
    out = []
    for account in accounts:
        kind = kinds.get(getattr(account, 'type', None), 'unknown')
        # Vinancia redemande tout l'historique d'un compte tout juste relié.
        full = last_sync_iso is None or account_ref(salt, bank['module'], getattr(account, 'id', '')) in backfill
        since = history_since(None if full else last_sync_iso)
        issue = None
        txs = []
        if kind in HISTORY_KINDS:
            try:
                txs = collect_transactions(backend.iter_history(account), since)
            except requests.RequestException as error:
                log.warning('%s : historique refusé (%s)', bank['name'], type(error).__name__, exc_info=True)
                issue = (f'opérations refusées par la banque ({http_error_where(error) or type(error).__name__}) ; '
                         + http_error_debug(error, backend))
                if bank_requires_step(error):
                    steps = ca_journey_steps(getattr(getattr(backend, 'browser', None), '_vna_journey', None))
                    issue = ((ca_steps_message(steps) + ' ') if steps else
                             'ta banque demande une étape obligatoire avant de montrer les opérations : ouvre son site, '
                             'connecte-toi et valide le message affiché, puis relance la synchro. ') + issue
                    if steps:
                        issue += ' ; étapes demandées : ' + ', '.join(steps)
                full = False   # l'historique complet sera redemandé à la prochaine synchro
        investments = None
        if kind in INVESTMENT_KINDS and hasattr(backend, 'iter_investment'):
            try:
                investments = collect_investments(backend.iter_investment(account))
            except NotImplementedError:
                investments = None   # la banque ne détaille pas les positions : solde seul
            except requests.RequestException as error:
                log.warning('%s : positions refusées (%s)', bank['name'], type(error).__name__, exc_info=True)
                issue = f'positions refusées par la banque ({http_error_where(error) or type(error).__name__})'
        entry = build_account(salt, bank['module'], account, kinds, txs, full_history=full, investments=investments)
        if issue:
            entry['_issue'] = issue
        out.append(entry)
    return out


def _collect_bank(w, bank, salt, kinds, interaction, last_sync_iso, backfill=()):
    for attempt in (1, 2):
        backend = open_backend(w, bank, _bank_password(bank), interactive=interaction is not None,
                               fresh=attempt == 2)
        try:
            return _collect_accounts(backend, bank, salt, kinds, interaction, last_sync_iso, backfill)
        except requests.RequestException as error:
            # Refus du site de la banque (403…) : souvent la session enregistrée. Un seul nouvel essai, connexion neuve.
            if attempt == 2:
                raise
            log.warning('%s : %s, nouvel essai avec une connexion neuve', bank['name'], type(error).__name__)
        finally:
            # Sauvegarde la session (cookies, appareil reconnu) : c'est ce qui évite de
            # refaire la double authentification à chaque synchro.
            with contextlib.suppress(Exception):
                backend.deinit()


def first_collect(cfg, w, bank, password, interaction):
    """
    Ajout d'une banque : se connecte UNE fois et lit tout (comptes, historique, positions) dans la même session.
    Se reconnecter juste après (session enregistrée) fait refuser la requête par certaines banques (403).
    """
    from woob.capabilities.bank import Account

    api = Api(cfg)
    salt = cfg.get('account_salt') or api.ping()['account_salt']
    cfg['account_salt'] = salt
    backend = open_backend(w, bank, password, interactive=True)
    try:
        return _collect_accounts(backend, bank, salt, type_names(Account), interaction, None)
    finally:
        with contextlib.suppress(Exception):
            backend.deinit()


@contextlib.contextmanager
def sync_lock():
    """Une seule synchro à la fois (fenêtre et tâche planifiée partagent les sessions)."""
    path = os.path.join(agent_dir(), 'sync.lock')
    try:
        if time.time() - os.path.getmtime(path) < 30 * 60:
            raise AgentError('Une synchro est déjà en cours.')
    except FileNotFoundError:
        pass
    with open(path, 'w') as f:
        f.write(str(os.getpid()))
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def run_sync(cfg, interaction=None, only=None, progress=None, prefetched=None):
    """
    Synchronise toutes les banques (ou seulement `only`) et envoie le lot à Vinancia.
    Renvoie (réponse du serveur, compte rendu par banque).
    """
    api = Api(cfg)
    salt = cfg.get('account_salt') or api.ping()['account_salt']
    cfg['account_salt'] = salt

    from woob.capabilities.bank import Account

    kinds = type_names(Account)
    with sync_lock():
        w = make_woob()
        # Les banques changent souvent leur site : les modules Woob suivent, on les
        # met à jour une fois par jour au plus.
        today = datetime.now(timezone.utc).date().isoformat()
        if cfg.get('modules_updated') != today:
            try:
                update_modules(w)
                cfg['modules_updated'] = today
            except Exception as error:
                log.warning('mise à jour des modules impossible : %s', error)

        accounts, report, synced = [], [], []
        for bank in cfg['banks']:
            if only and bank['name'] not in only:
                continue
            if progress:
                progress(f'Connexion à {bank_display_name(bank)}…')
            try:
                if prefetched and bank['name'] in prefetched:
                    accounts += prefetched[bank['name']]   # déjà lu dans la même connexion (ajout d'une banque)
                else:
                    ensure_module(w, bank['module'])
                    accounts += _collect_bank(w, bank, salt, kinds, interaction, cfg['last_sync'].get(bank['name']),
                                              backfill=set(cfg.get('backfill') or []))
                status, message = 'ok', None
                synced.append(bank['name'])
            except Cancelled:
                raise
            except Exception as error:
                status, message = classify_error(error)
                log.warning('%s : %s', bank['name'], message, exc_info=True)
            report.append({'bank': bank['module'], 'status': status, 'message': message})
            cfg['status'][bank['name']] = {'status': status, 'message': message,
                                           'at': datetime.now(timezone.utc).isoformat()}

        if progress:
            progress('Envoi à Vinancia…')
        outgoing = [{k: v for k, v in a.items() if not k.startswith('_')} for a in accounts]
        result = api.sync({'agent_version': AGENT_VERSION, 'accounts': outgoing, 'report': report})
        # Détail local (jamais renvoyé ni stocké) : ce que l'appli montre à l'utilisateur après une synchro.
        result['_local'] = [
            {'bank': a['bank'], 'label': a['label'], 'kind': a['kind'], 'balance': a['balance'],
             'operations': len(a['transactions']), 'issue': a.get('_issue')}
            for a in accounts
        ]
        cfg['backfill'] = list(result.get('full_history_refs') or [])
        for name in synced:
            cfg['last_sync'][name] = today
        save_config(cfg)
        return result, report


# ─── API Vinancia ────────────────────────────────────────────────────────────

class Api:
    def __init__(self, cfg, token=None):
        self.url = (cfg.get('api_url') or DEFAULT_API_URL).rstrip('/')
        self.token = token or secret_get('api_token')

    def _headers(self, auth=True):
        headers = {'User-Agent': f'VinanciaAgent/{AGENT_VERSION}'}
        if auth:
            if not self.token:
                raise AgentError('Agent non relié à ton compte Vinancia.')
            headers['Authorization'] = f'Bearer {self.token}'
        return headers

    def _json(self, response):
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 401:
            raise AgentError('Accès refusé par Vinancia : relie de nouveau cet ordinateur.')
        if response.status_code >= 400 and response.status_code != 404:
            raise AgentError(body.get('error') or f'Erreur Vinancia (HTTP {response.status_code}).')
        return body

    def ping(self):
        return self._json(requests.get(f'{self.url}/api/bank-agent/ping', headers=self._headers(), timeout=30))

    def known_banks(self):
        """Modules des banques déjà synchronisées pour ce compte (vide en cas de souci : simple confort)."""
        try:
            body = self._json(requests.get(f'{self.url}/api/bank-agent/ping', headers=self._headers(), timeout=8))
        except Exception:  # noqa: BLE001
            return []
        return [b['module'] for b in body.get('known_banks') or [] if isinstance(b, dict) and b.get('module')]

    def sync(self, payload):
        return self._json(requests.post(f'{self.url}/api/bank-agent/sync', json=payload,
                                        headers=self._headers(), timeout=120))

    @staticmethod
    def _device():
        if is_android():
            return {'label': os.environ.get('VINANCIA_DEVICE') or 'Mon téléphone', 'platform': 'Android'}
        return {'label': platform.node() or 'Mon ordinateur', 'platform': platform.system()}

    def pair_start(self):
        return self._json(requests.post(
            f'{self.url}/api/bank-agent/pair/start',
            json=self._device(),
            headers=self._headers(auth=False), timeout=30,
        ))

    def pair_poll(self, device_code):
        return self._json(requests.post(f'{self.url}/api/bank-agent/pair/poll',
                                        json={'device_code': device_code},
                                        headers=self._headers(auth=False), timeout=30))


def review_url():
    """Page Comptes de Vinancia où l'utilisateur relie / remplace / ajoute ses comptes à valider (ouvre la modale Synchro bancaire)."""
    return f'{WEB_URL}/accounts?synchro=valider'


def pairing_url(user_code):
    return f'{WEB_URL}/agent/autoriser?code={user_code}'


def start_pairing(cfg, open_browser=True):
    """Ouvre une demande d'appairage et la page « Autoriser » dans le navigateur."""
    check_keyring()
    pairing = Api(cfg).pair_start()
    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(pairing_url(pairing['user_code']))
    return pairing


def finish_pairing(cfg, device_code):
    """None tant que l'utilisateur n'a pas cliqué « Autoriser » ; sinon relie l'agent."""
    body = Api(cfg).pair_poll(device_code)
    status = body.get('status')
    if status == 'pending':
        return None
    if status != 'approved':
        raise AgentError('Le code a expiré : recommence la liaison.')
    _store_link(cfg, body['token'], body)
    return body


def connect_with_token(cfg, token):
    check_keyring()
    if not token.startswith('vna_'):
        raise AgentError('Ce jeton ne ressemble pas à un jeton d’agent Vinancia.')
    info = Api(cfg, token=token).ping()
    _store_link(cfg, token, info)
    return info


def _store_link(cfg, token, info):
    secret_set('api_token', token)
    cfg['account_salt'] = info['account_salt']
    cfg['user'] = info.get('user')
    save_config(cfg)


def latest_release():
    """Version publiée la plus récente, ou None (silencieux en cas d'échec)."""
    try:
        r = requests.get(f'https://api.github.com/repos/{RELEASES_REPO}/releases/latest', timeout=5)
        if r.ok:
            tag = str(r.json().get('tag_name') or '').removeprefix('agent-').removeprefix('v')
            return tag or None
    except requests.RequestException:
        pass
    return None


def is_newer(remote, local=AGENT_VERSION):
    def parts(v):
        return tuple(int(p) for p in v.split('.') if p.isdigit())
    try:
        return parts(remote) > parts(local)
    except (AttributeError, ValueError):
        return False


# ─── Installation et planification ───────────────────────────────────────────

def _no_window():
    return {'creationflags': 0x08000000} if platform.system() == 'Windows' else {}  # CREATE_NO_WINDOW


def install_location():
    """
    Copie l'application à un emplacement stable (pas le dossier Téléchargements) et
    renvoie la commande à planifier. En développement, lance simplement le script.
    """
    if not getattr(sys, 'frozen', False):
        return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vinancia_agent.py')]

    system = platform.system()
    if system == 'Windows':
        target_dir = os.path.join(os.environ.get('LOCALAPPDATA') or agent_dir(), 'Programs', 'VinanciaAgent')
        target = os.path.join(target_dir, 'VinanciaAgent.exe')
        if os.path.abspath(sys.executable).lower() != target.lower():
            os.makedirs(target_dir, exist_ok=True)
            shutil.copy2(sys.executable, target)
        _windows_shortcut(target)
        return [target]
    if system == 'Darwin':
        app = os.path.abspath(os.path.join(os.path.dirname(sys.executable), '..', '..'))
        target_app = os.path.expanduser(f'~/Applications/{APP_NAME}.app')
        if app.endswith('.app') and os.path.realpath(app) != os.path.realpath(target_app):
            os.makedirs(os.path.dirname(target_app), exist_ok=True)
            if os.path.exists(target_app):
                shutil.rmtree(target_app)
            shutil.copytree(app, target_app, symlinks=True)
            return [os.path.join(target_app, 'Contents', 'MacOS', os.path.basename(sys.executable))]
        return [sys.executable]
    return [sys.executable]


def _windows_shortcut(target):
    """Raccourci dans le menu Démarrer, pour rouvrir l'appli facilement."""
    menu = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Start Menu', 'Programs')
    if not os.path.isdir(menu):
        return
    link = os.path.join(menu, f'{APP_NAME}.lnk')
    script = (
        '$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:VNA_LINK);'
        '$s.TargetPath=$env:VNA_TARGET;$s.Save()'
    )
    env = dict(os.environ, VNA_LINK=link, VNA_TARGET=target)
    with contextlib.suppress(Exception):
        subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                       env=env, check=False, timeout=30, **_no_window())


def install_schedule():
    """Programme la synchro automatique toutes les 6 h (et au démarrage de session)."""
    command = install_location()
    system = platform.system()
    if system == 'Windows':
        _install_windows_task(command)
    elif system == 'Darwin':
        _install_launch_agent(command)
    else:
        _install_crontab(command)


def _install_windows_task(command):
    exe = command[0]
    args = ' '.join(f'"{a}"' for a in command[1:] + ['sync', '--auto'])
    start = datetime.now().replace(microsecond=0).isoformat()
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Synchro bancaire Vinancia</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><Delay>PT5M</Delay></LogonTrigger>
    <TimeTrigger>
      <Repetition><Interval>PT{SYNC_EVERY_HOURS}H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>{escape(args)}</Arguments></Exec></Actions>
</Task>
"""
    fd, path = tempfile.mkstemp(suffix='.xml')
    os.close(fd)
    try:
        with open(path, 'w', encoding='utf-16') as f:
            f.write(xml)
        result = subprocess.run(['schtasks', '/Create', '/F', '/TN', TASK_NAME, '/XML', path],
                                capture_output=True, text=True, timeout=60, **_no_window())
        if result.returncode != 0:
            raise AgentError(f'Programmation de la synchro impossible : {result.stderr.strip() or result.stdout.strip()}')
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def _launch_agent_path():
    return os.path.expanduser(f'~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist')


def _install_launch_agent(command):
    path = _launch_agent_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    logfile = os.path.join(agent_dir(), 'sync.log')
    with open(path, 'wb') as f:
        plistlib.dump({
            'Label': LAUNCHD_LABEL,
            'ProgramArguments': command + ['sync', '--auto'],
            'StartInterval': SYNC_EVERY_HOURS * 3600,
            'RunAtLoad': True,
            'ProcessType': 'Background',
            'StandardOutPath': logfile,
            'StandardErrorPath': logfile,
        }, f)
    domain = f'gui/{os.getuid()}'
    subprocess.run(['launchctl', 'bootout', domain, path], capture_output=True, check=False)
    result = subprocess.run(['launchctl', 'bootstrap', domain, path], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        subprocess.run(['launchctl', 'load', '-w', path], capture_output=True, check=False)


def _crontab_line(command):
    quoted = ' '.join(f'"{c}"' for c in command)
    return f'17 */{SYNC_EVERY_HOURS} * * * {quoted} sync --auto  # vinancia-agent'


def _install_crontab(command):
    current = subprocess.run(['crontab', '-l'], capture_output=True, text=True, check=False).stdout
    lines = [line for line in current.splitlines() if '# vinancia-agent' not in line]
    lines.append(_crontab_line(command))
    result = subprocess.run(['crontab', '-'], input='\n'.join(lines) + '\n', text=True,
                            capture_output=True, check=False)
    if result.returncode != 0:
        raise AgentError('Programmation de la synchro impossible (crontab).')


def remove_schedule():
    system = platform.system()
    if system == 'Windows':
        subprocess.run(['schtasks', '/Delete', '/F', '/TN', TASK_NAME], capture_output=True, check=False, **_no_window())
    elif system == 'Darwin':
        path = _launch_agent_path()
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', path], capture_output=True, check=False)
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
    else:
        current = subprocess.run(['crontab', '-l'], capture_output=True, text=True, check=False).stdout
        lines = [line for line in current.splitlines() if '# vinancia-agent' not in line]
        subprocess.run(['crontab', '-'], input='\n'.join(lines) + '\n', text=True, capture_output=True, check=False)


def forget_everything(cfg):
    """Délie l'ordinateur : supprime jeton, mots de passe, sessions et planification."""
    remove_schedule()
    for bank in cfg['banks']:
        secret_delete(f"bank:{bank['name']}")
        for field in bank.get('secret_params') or []:
            secret_delete(f"bank:{bank['name']}:{field}")
    for key in ('api_token', 'session_key'):
        secret_delete(key)
    shutil.rmtree(os.path.join(agent_dir(), 'woob'), ignore_errors=True)
    with contextlib.suppress(FileNotFoundError):
        os.remove(_config_path())


class _RedactingFormatter(logging.Formatter):
    """Passe chaque ligne (traces comprises) par `redact` avant de l'écrire."""

    def format(self, record):
        return redact(super().format(record))


def setup_logging(to_file):
    handlers = []
    if to_file:
        path = os.path.join(agent_dir(), 'sync.log')
        with contextlib.suppress(OSError):
            if os.path.getsize(path) > 1_000_000:
                os.replace(path, path + '.old')
        handlers.append(logging.FileHandler(path, encoding='utf-8'))
    else:
        handlers.append(logging.StreamHandler())
    # WARNING au minimum : les journaux de Woob en DEBUG contiendraient des pages bancaires.
    formatter = _RedactingFormatter('%(asctime)s %(levelname)s %(message)s')
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.WARNING, handlers=handlers)

