"""Module « Trade Republic » de Vinancia Agent, au format des modules bancaires Woob.

Woob n'a pas de module Trade Republic : celui-ci est embarqué dans l'appli (jamais téléchargé) et
s'appuie sur `tr_client` (lecture seule). En prenant la forme d'un module Woob, il profite de tout
le reste de l'agent sans cas particulier : trousseau, session chiffrée, fenêtre « Valide sur ton
téléphone », synchro planifiée qui ne dérange jamais l'utilisateur.

Parcours : numéro + PIN -> Trade Republic envoie une notification dans son appli -> l'utilisateur
valide -> la session (cookies) est gardée chiffrée et reprise aux synchros suivantes. Quand elle
a expiré, la synchro planifiée s'arrête (`NeedInteractive`) : UNE nouvelle connexion n'est tentée
que si l'utilisateur est devant la fenêtre.
"""

from woob.capabilities.bank import Account
from woob.capabilities.bank.base import Transaction
from woob.capabilities.bank.wealth import CapBankWealth, Investment
from woob.exceptions import (
    AppValidation, AppValidationExpired, BrowserIncorrectPassword, BrowserQuestion, BrowserUnavailable,
    BrowserUserBanned, NeedInteractive,
)
from woob.tools.backend import BackendConfig, Module
from woob.tools.value import Value, ValueBackendPassword, ValueTransient

import tr_client

__all__ = ['TradeRepublicModule']


class TradeRepublicModule(Module, CapBankWealth):
    NAME = 'traderepublic'
    DESCRIPTION = 'Trade Republic'
    MAINTAINER = 'Vinancia'
    EMAIL = 'contact@vinancia.fr'
    LICENSE = 'MIT'
    CONFIG = BackendConfig(
        Value('login', label='Numéro de téléphone (avec l’indicatif, ex. +33612345678)'),
        ValueBackendPassword('password', label='Code PIN Trade Republic'),
        ValueTransient('request_information'),
        ValueTransient('resume'),
        ValueTransient('otp'),
    )
    STORAGE = {'cookies': [], 'device_id': '', 'blocked_until': 0, 'refused': ''}

    # Remplaçable dans les tests.
    CLIENT = tr_client.TRClient

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None
        self._data = None

    # -- session -------------------------------------------------------------------

    def _device_id(self):
        device_id = self.storage.get('device_id', default='')
        if not device_id:
            device_id = tr_client.new_device_id()
            self.storage.set('device_id', device_id)
        return device_id

    def _get_client(self):
        if self._client is None:
            self._client = self.CLIENT(self._device_id())
        return self._client

    @staticmethod
    def _explained(exception):
        # `vna_message` : le texte est le nôtre (sans secret), l'agent peut l'afficher tel quel.
        exception.vna_message = True
        return exception

    def _run(self, call):
        """Traduit les erreurs du client en exceptions que l'agent sait expliquer."""
        try:
            return call()
        except tr_client.TRNeedsLogin:
            raise
        except tr_client.TRBadCredentials as error:
            raise self._explained(BrowserIncorrectPassword(str(error))) from None
        except tr_client.TRRateLimited as error:
            raise self._explained(BrowserUserBanned(str(error))) from None
        except tr_client.TRLoginExpired as error:
            raise self._explained(AppValidationExpired(str(error))) from None
        except tr_client.TRError as error:
            raise self._explained(BrowserUnavailable(str(error))) from None

    def _session_lost(self):
        self._get_client().logged_in = False
        self.storage.set('cookies', [])
        if self.config['request_information'].get() is None:
            return NeedInteractive()
        return self._explained(BrowserUnavailable('Trade Republic a fermé la session : relance la synchronisation.'))

    def _ensure_session(self):
        client = self._get_client()
        if client.logged_in:
            return client
        interactive = self.config['request_information'].get() is not None
        if client.login_pending:
            # L'utilisateur vient de dire « c'est validé » (ou de saisir son code) : on termine CETTE demande.
            try:
                cookies = self._run(lambda: client.finish_login(self.config['otp'].get()))
            except tr_client.TRNeedsLogin:
                raise self._session_lost() from None
            self.storage.set('cookies', cookies)
            self.storage.save()
            return client
        if self._run(lambda: client.resume(self.storage.get('cookies', default=[]))):
            return client
        if not interactive:
            # Synchro planifiée : on n'envoie jamais de notification à l'insu de l'utilisateur.
            raise NeedInteractive()
        action = self._start_login(client)
        if action == 'authenticator_code':
            raise BrowserQuestion(Value('otp', label='Code à 6 chiffres de ton application d’authentification '
                                                    '(Trade Republic te demandera ensuite de valider dans son appli)'))
        raise AppValidation('Ouvre l’application Trade Republic sur ton téléphone et valide la demande de connexion '
                            '(tu as environ 2 minutes).')

    def _start_login(self, client):
        """Envoie UNE demande de connexion, sauf si l'on sait déjà qu'elle sera refusée ou qu'elle aggraverait un blocage."""
        import time

        phone, pin = self.config['login'].get(), self.config['password'].get()
        blocked_until = self.storage.get('blocked_until', default=0) or 0
        if time.time() < blocked_until:
            until = time.strftime('%H h %M', time.localtime(blocked_until))
            raise self._explained(BrowserUserBanned(
                f'Trade Republic a bloqué les connexions après plusieurs essais. Je n’envoie plus rien avant {until} '
                'pour ne pas prolonger le blocage : réessaie après.'))
        fingerprint = tr_client.credentials_fingerprint(self._device_id(), phone, pin)
        if self.storage.get('refused', default='') == fingerprint:
            raise self._explained(BrowserIncorrectPassword(
                'Trade Republic a déjà refusé ce numéro et ce code PIN : je ne les renvoie pas (plusieurs PIN faux '
                'peuvent bloquer ton compte). Retire Trade Republic de l’appli puis rajoute-le avec le bon code.'))
        client.refresh_app_version()
        try:
            action = client.start_login(phone, pin)
        except tr_client.TRRateLimited as error:
            self.storage.set('blocked_until', int(time.time()) + tr_client.RATE_LIMIT_PAUSE_SECONDS)
            self.storage.save()
            raise self._explained(BrowserUserBanned(str(error))) from None
        except tr_client.TRBadCredentials as error:
            self.storage.set('refused', fingerprint)
            self.storage.save()
            raise self._explained(BrowserIncorrectPassword(str(error))) from None
        except tr_client.TRError as error:
            raise self._explained(BrowserUnavailable(str(error))) from None
        self.storage.set('blocked_until', 0)
        return action

    def _load(self):
        if self._data is None:
            client = self._ensure_session()
            try:
                self._data = self._run(lambda: client.fetch(tr_client.history_floor()))
            except tr_client.TRNeedsLogin:
                # Session refusée en cours de lecture : même traitement qu'une session expirée.
                raise self._session_lost() from None
            self.storage.set('cookies', client.export_cookies())
        return self._data

    def deinit(self):
        # Les cookies sont renouvelés par Trade Republic au fil des appels : on garde les derniers.
        if self._client is not None and self._client.logged_in:
            self.storage.set('cookies', self._client.export_cookies())
        self.storage.save()

    # -- lecture -------------------------------------------------------------------

    def iter_accounts(self):
        data = self._load()
        cash = Account()
        cash.id = tr_client.CASH_ACCOUNT_ID
        cash.label = 'Trade Republic - Espèces'
        cash.type = Account.TYPE_CHECKING
        cash.currency = 'EUR'
        cash.balance = data['cash']
        accounts = [cash]
        if data['has_securities']:
            securities = Account()
            securities.id = tr_client.SECURITIES_ACCOUNT_ID
            securities.label = 'Trade Republic - Titres'
            securities.type = Account.TYPE_MARKET
            securities.currency = 'EUR'
            # Valeur des titres seule : les espèces sont dans l'autre compte (sinon elles compteraient deux fois).
            securities.balance = sum((inv['valuation'] for inv in data['investments'] if inv['valuation'] is not None),
                                     start=tr_client.Decimal('0'))
            accounts.append(securities)
        return iter(accounts)

    def iter_history(self, account):
        if account.id != tr_client.CASH_ACCOUNT_ID:
            return
        for row in self._load()['transactions']:
            tx = Transaction()
            tx.id = row['id']
            tx.date = row['date']
            tx.amount = row['amount']
            tx.label = row['label']
            tx.raw = row['raw']
            yield tx

    def iter_investment(self, account):
        if account.id != tr_client.SECURITIES_ACCOUNT_ID:
            return
        for row in self._load()['investments']:
            inv = Investment()
            inv.label = row['label']
            if row['code']:
                inv.code = row['code']
                inv.code_type = Investment.CODE_TYPE_ISIN
            if row['symbol']:
                inv.stock_symbol = row['symbol']
            inv.quantity = row['quantity']
            if row['unitprice'] is not None:
                inv.unitprice = row['unitprice']
            if row['unitvalue'] is not None:
                inv.unitvalue = row['unitvalue']
            if row['valuation'] is not None:
                inv.valuation = row['valuation']
            inv.original_currency = 'EUR'
            yield inv
