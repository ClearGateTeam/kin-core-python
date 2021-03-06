# -*- coding: utf-8 -*

# Copyright (C) 2018 Kin Foundation

from decimal import Decimal, getcontext
from functools import partial

from stellar_base.asset import Asset
from stellar_base.keypair import Keypair

from .channel_manager import ChannelManager
from .exceptions import (
    SdkConfigurationError,
    SdkNotConfiguredError,
    SdkHorizonError,
)
from .horizon import Horizon
from .models import AccountData, TransactionData
from .utils import validate_address, validate_secret_key

import logging
logger = logging.getLogger(__name__)

getcontext().prec = 7  # IMPORTANT: XLM decimal precision

KIN_ISSUER_PROD = 'GDVDKQFP665JAO7A2LSHNLQIUNYNAAIGJ6FYJVMG4DT3YJQQJSRBLQDG'  # TODO: real address
KIN_ASSET_PROD = Asset('KIN', KIN_ISSUER_PROD)

KIN_ISSUER_TEST = 'GCKG5WGBIJP74UDNRIRDFGENNIH5Y3KBI5IHREFAJKV4MQXLELT7EX6V'
KIN_ASSET_TEST = Asset('KIN', KIN_ISSUER_TEST)

# https://www.stellar.org/developers/guides/concepts/fees.html
BASE_RESERVE = 0.5  # in XLM
MIN_ACCOUNT_BALANCE = (2 + 1) * BASE_RESERVE  # 1 additional trustline op


class SDK(object):
    """
    The :class:`~kin.SDK` class is the primary interface to the KIN Python SDK based on Stellar Blockchain.
    It maintains a connection context with a Horizon node and hides all the specifics of dealing with Stellar REST API.
    """

    def __init__(self, secret_key='', horizon_endpoint_uri='', network='PUBLIC', channel_secret_keys=None):
        """Create a new instance of the KIN SDK for Stellar.

        If secret key is not provided, the SDK can still be used in "anonymous" mode with only the following
        functions available:
            - get_account_native_balance
            - get_account_kin_balance
            - check_account_exists
            - check_account_activated
            - get_account_data
            - get_transaction_data
            - monitor_accounts_kin_payments
            - monitor_accounts_transactions

        :param str secret_key: (optional) a key to initialize the sdk wallet account with. If not provided, the wallet
            not not be initialized and methods needing the wallet will raise exception.

        :param str horizon_endpoint_uri: (optional) a Horizon endpoint. If not provided, a default endpoint will be
            used, either a testnet or pubnet, depending on the `network` parameter.

        :param str network: (optional) either PUBLIC or TESTNET, will set the Horizon endpoint in the absence of
            `horizon_endpoint_uri`. Defaults to PUBLIC if not specified.

        :param list of str channel_secret_keys: (optional) a list of channels to sign transactions with. More channels means
            less blocking on transactions and better response time.

        :return: An instance of the SDK.
        :rtype: :class:`~kin.SDK`

        :raises: :class:`~kin.SdkConfigurationError` if some of the configuration parameters are invalid.
        """

        channel_secret_keys = channel_secret_keys or []
        self.network = network or 'PUBLIC'
        self.kin_asset = KIN_ASSET_PROD if self.network == 'PUBLIC' else KIN_ASSET_TEST

        # set connection pool size for channels, monitoring connection + extra
        pool_size = max(1, len(channel_secret_keys)) + 2

        if horizon_endpoint_uri:
            self.horizon = Horizon(horizon_uri=horizon_endpoint_uri, pool_size=pool_size)
        else:
            from stellar_base.horizon import HORIZON_LIVE, HORIZON_TEST
            if self.network == 'TESTNET':
                self.horizon = Horizon(horizon_uri=HORIZON_TEST, pool_size=pool_size)
            else:
                self.horizon = Horizon(horizon_uri=HORIZON_LIVE, pool_size=pool_size)

        # init sdk account base_keypair if a secret_key is supplied
        self.base_keypair = None
        if secret_key:
            try:
                validate_secret_key(secret_key)
            except ValueError:
                raise SdkConfigurationError('invalid secret key: {}'.format(secret_key))
            self.base_keypair = Keypair.from_seed(secret_key)

            # check channel keys
            if channel_secret_keys:
                for channel_key in channel_secret_keys:
                    try:
                        validate_secret_key(channel_key)
                    except ValueError:
                        raise SdkConfigurationError('invalid channel key: {}'.format(channel_key))
            else:
                channel_secret_keys = [secret_key]

            # init channel manager
            self.channel_manager = ChannelManager(secret_key, channel_secret_keys, self.network, self.horizon)

        logger.info('Kin SDK inited on network {}, horizon endpoint {}'.format(self.network, self.horizon.horizon_uri))

    def get_status(self):
        """Get system status."""
        status = {
            'network': self.network,
            'address': None,
            'kin_asset': {
                'code': self.kin_asset.code,
                'issuer': self.kin_asset.issuer
            },
            'horizon': {
                'uri': self.horizon.horizon_uri,
                'online': False,
                'error': None,
            },
            'channels': None,
        }
        if self.base_keypair:
            status['address'] = self.get_address()
            status['channels'] = {
                'all': self.channel_manager.num_channels,
                'free': self.channel_manager.channel_builders.qsize()
            }

        # now check Horizon connection
        try:
            self.horizon.query('')
            status['horizon']['online'] = True
        except Exception as e:
            status['horizon']['error'] = str(e)

        return status

    def get_address(self):
        """Get the address of the SDK wallet account.
        The wallet is configured by a secret key supplied during SDK initialization.

        :return: public address of the wallet.
        :rtype: str

        :raises: :class:`~kin.SdkConfigurationError`: if the SDK wallet is not configured.
        """
        if not self.base_keypair:
            raise SdkNotConfiguredError('address not configured')
        return self.base_keypair.address().decode()

    def get_native_balance(self):
        """Get native (lumen) balance of the SDK wallet.
        The wallet is configured by a secret key supplied during SDK initialization.

        :return: : the balance in lumens.
        :rtype: Decimal

        :raises: :class:`~kin.SdkConfigurationError`: if the SDK wallet is not configured.
        """
        return self.get_account_native_balance(self.get_address())

    def get_kin_balance(self):
        """Get KIN balance of the SDK wallet.
        The wallet is configured by a secret key supplied during SDK initialization.

        :return: : the balance in KIN.
        :rtype: Decimal

        :raises: :class:`~kin.SdkConfigurationError`: if the SDK wallet is not configured.
        """
        return self.get_account_kin_balance(self.get_address())

    def get_account_native_balance(self, address):
        """Get native (lumen) balance of the account identified by the provided address.

        :param: str address: the address of the account to query.

        :return: the lumen balance of the account.
        :rtype: Decimal

        :raises: ValueError: if the supplied address has a wrong format.
        """
        return self._get_account_asset_balance(address, Asset.native())

    def get_account_kin_balance(self, address):
        """Get KIN balance of the account identified by the provided address.

        :param: str address: the address of the account to query.

        :return: : the balance in KIN of the account.
        :rtype: Decimal

        :raises: ValueError: if the supplied address has a wrong format.
        """
        return self._get_account_asset_balance(address, self.kin_asset)

    def create_account(self, address, starting_balance=MIN_ACCOUNT_BALANCE, memo_text=None):
        """Create an account identified by the provided address.

        :param str address: the address of the account to create.

        :param number starting_balance: (optional) the starting balance of the account. If not provided, a default
            MIN_ACCOUNT_BALANCE will be used.

        :param str memo_text: (optional) a text to put into transaction memo.

        :return: transaction hash
        :rtype: str

        :raises: :class:`~kin.SdkConfigurationError` if the SDK wallet is not configured.
        :raises: ValueError: if the supplied address has a wrong format.
        """
        if not self.base_keypair:
            raise SdkNotConfiguredError('address not configured')
        validate_address(address)

        return self.channel_manager.send_transaction(lambda builder:
                                                     partial(builder.append_create_account_op, address,
                                                             starting_balance),
                                                     memo_text=memo_text)

    def check_account_exists(self, address):
        """Check whether the account identified by the provided address exists.

        :param str address: the account address to query.

        :return: True if the account exists.
        :rtype: boolean

        :raises: ValueError: if the supplied address has a wrong format.
        """
        try:
            self.get_account_data(address)
            return True
        except SdkHorizonError as se:
            if se.status == 404:
                return False
            raise

    def check_account_activated(self, address):
        """Check if the account is activated (has a trustline to KIN asset).

        :param str address: the account address to query.

        :return: True if the account is activated.
        :rtype: boolean

        :raises: ValueError: if the supplied address has a wrong format.
        """
        return self._check_asset_trusted(address, self.kin_asset)

    def send_native(self, address, amount, memo_text=None):
        """Send native currency (lumens) to the account identified by the provided address.

        :param str address: the account to send lumens to.

        :param number amount: the number of lumens to send.

        :param str memo_text: (optional) a text to put into transaction memo.

        :return: transaction hash
        :rtype: str

        :raises: :class:`~kin.SdkConfigurationError` if the SDK wallet is not configured.
        :raises: ValueError: if the provided address has a wrong format.
        :raises: ValueError: if the amount is not positive.
        """
        return self._send_asset(Asset.native(), address, amount, memo_text)

    def send_kin(self, address, amount, memo_text=None):
        """Send KIN to the account identified by the provided address.

        :param str address: the account to send KIN to.

        :param number amount: the amount of KIN to send.

        :param str memo_text: (optional) a text to put into transaction memo.

        :return: transaction hash
        :rtype: str

        :raises: :class:`~kin.SdkConfigurationError` if the SDK wallet is not configured.
        :raises: ValueError: if the provided address has a wrong format.
        :raises: ValueError: if the amount is not positive.
        """
        return self._send_asset(self.kin_asset, address, amount, memo_text)

    def get_account_data(self, address):
        """Gets account data.

        :param str address: the account to query.

        :return: account data
        :rtype: :class:`~kin.AccountData`

        :raises: ValueError: if the provided address has a wrong format.
        """
        validate_address(address)
        acc = self.horizon.account(address)
        return AccountData(acc, strict=False)

    def get_transaction_data(self, tx_hash):
        """Gets transaction data.

        :param str tx_hash: transaction hash.

        :return: transaction data
        :rtype: :class:`~kin.TransactionData`
        """
        # get transaction data
        tx = self.horizon.transaction(tx_hash)

        # get transaction operations
        tx_ops = self.horizon.transaction_operations(tx['hash'], params={'limit': 100})

        tx['operations'] = tx_ops['_embedded']['records']

        return TransactionData(tx, strict=False)

    def monitor_kin_payments(self, callback_fn):
        """Monitor KIN payment transactions related to the SDK wallet account.
        NOTE: the function starts a background thread.

        :param callback_fn: the function to call on each received payment as `callback_fn(address, tx_data)`.
        :type: callable[[str, :class:`~kin.TransactionData`], None]

        :raises: :class:`~kin.SdkConfigurationError` if the SDK wallet is not configured.
        """
        self.monitor_accounts_kin_payments([self.get_address()], callback_fn)

    def monitor_accounts_kin_payments(self, addresses, callback_fn):
        """Monitor KIN payment transactions related to the accounts identified by provided addresses.
        NOTE: the function starts a background thread.

        :param list of str addresses: the addresses of the accounts to query.

        :param callback_fn: the function to call on each received payment as `callback_fn(address, tx_data)`.
        :type: callable[[str, :class:`~kin.TransactionData`], None]

        :raises: ValueError: when no addresses are given.
        :raises: ValueError: if one of the provided addresses has a wrong format.
        """
        self._monitor_accounts_transactions(self.kin_asset, addresses, callback_fn, only_payments=True)

    # noinspection PyTypeChecker
    def monitor_accounts_transactions(self, addresses, callback_fn):
        """Monitor transactions related to the account identified by a provided addresses (all transaction types).
        NOTE: the function starts a background thread.

        :param list of str addresses: the addresses of the accounts to query.

        :param callback_fn: the function to call on each received transaction as `callback_fn(address, tx_data)`.
        :type: callable[[str, :class:`~kin.TransactionData`], None]

        :raises: ValueError: when no addresses are given.
        :raises: ValueError: if one of the provided addresses has a wrong format.
        """
        self._monitor_accounts_transactions(None, addresses, callback_fn)

    # Helpers

    def _get_account_asset_balance(self, address, asset):
        """Get asset balance of the account identified by the provided address.

        :param: str address: the address of the account to query.

        :param asset: the asset to get balance for.
        :type: :class:`stellar_base.asset.Asset`

        :return: : the balance in asset units of the account.
        :rtype: Decimal

        :raises: ValueError: if the supplied address has a wrong format.
        :raises: ValueError: when account is not activated (no trustline).
        """
        if not asset.is_native():
            try:
                validate_address(asset.issuer)
            except ValueError:
                raise ValueError('asset issuer invalid')

        acc_data = self.get_account_data(address)
        for balance in acc_data.balances:
            if (balance.asset_type == 'native' and asset.code == 'XLM') \
                    or (balance.asset_code == asset.code and balance.asset_issuer == asset.issuer):
                return balance.balance

        raise ValueError('account not activated for the asset')

    def _trust_asset(self, asset, limit=None, memo_text=None):
        """Establish a trustline from the SDK wallet to the asset issuer.

        :param asset: the asset to establish a trustline to.
        :type: :class:`stellar_base.asset.Asset`

        :param number limit: trustline limit.

        :param str memo_text: (optional) a text to put into transaction memo.

        :return: transaction hash
        :rtype: str

        :raises: :class:`~kin.SdkConfigurationError` if the SDK wallet is not configured.
        :raises: ValueError: if the issuer address has a wrong format.
        """
        if not self.base_keypair:
            raise SdkNotConfiguredError('address not configured')

        if not asset.is_native():
            try:
                validate_address(asset.issuer)
            except ValueError:
                raise ValueError('asset issuer invalid')

        return self.channel_manager.send_transaction(lambda builder:
                                                     partial(builder.append_trust_op, asset.issuer, asset.code,
                                                             limit=limit),
                                                     memo_text=memo_text)

    def _check_asset_trusted(self, address, asset):
        """Check if the account has a trustline to the provided asset.

        :param str address: the account address to query.

        :param asset: the asset to check
        :type: :class:`stellar_base.asset.Asset`

        :return: True if the account has a trustline to the asset.
        :rtype: boolean

        :raises: ValueError: if the supplied address has a wrong format.
        :raises: ValueError: if the asset issuer address has a wrong format.
        """
        if not asset.is_native():
            try:
                validate_address(asset.issuer)
            except ValueError:
                raise ValueError('asset issuer invalid')

        acc_data = self.get_account_data(address)
        for balance in acc_data.balances:
            if balance.asset_code == asset.code and balance.asset_issuer == asset.issuer:
                return True
        return False

    def _send_asset(self, asset, address, amount, memo_text=None):
        """Send asset to the account identified by the provided address.

        :param str address: the account to send asset to.

        :param asset: asset to send
        :type: :class:`stellar_base.asset.Asset`

        :param number amount: the asset amount to send.

        :param str memo_text: (optional) a text to put into transaction memo.

        :return: transaction hash
        :rtype: str

        :raises: :class:`~kin.SdkConfigurationError` if the SDK wallet is not configured.
        :raises: ValueError: if the provided address has a wrong format.
        :raises: ValueError: if the asset issuer address has a wrong format.
        :raises: ValueError: if the amount is not positive.
        """
        if not self.base_keypair:
            raise SdkNotConfiguredError('address not configured')
        validate_address(address)

        if amount <= 0:
            raise ValueError('amount must be positive')

        if not asset.is_native():
            try:
                validate_address(asset.issuer)
            except ValueError:
                raise ValueError('asset issuer invalid')

        return self.channel_manager.send_transaction(lambda builder:
                                                     partial(builder.append_payment_op, address, amount,
                                                             asset_type=asset.code, asset_issuer=asset.issuer),
                                                     memo_text=memo_text)

    def _monitor_accounts_transactions(self, asset, addresses, callback_fn, only_payments=False):
        """Monitor transactions related to the accounts identified by provided addresses. If asset is given, only
        the transactions for this asset will be returned.
        NOTE: the functions starts a background thread.

        :param: asset: (optional) the asset to query.
        :type: :class:`stellar_base.asset.Asset`

        :param: str addresses: the addresses of the accounts to query.

        :param callback_fn: the function to call on each received transaction as `callback_fn(address, tx_data)`.
        :type: callable[[str, :class:`~kin.TransactionData`], None]

        :param boolean only_payments: whether to return payment transactions only.

        :raises: ValueError: if asset issuer is invalid.
        :raises: ValueError: when no addresses are given.
        :raises: ValueError: if one of the provided addresses has a wrong format.
        """
        if asset and not asset.is_native():
            try:
                validate_address(asset.issuer)
            except ValueError:
                raise ValueError('asset issuer invalid')

        if not addresses:
            raise ValueError('no addresses to monitor')

        for address in addresses:
            validate_address(address)

        # determine the last_id to start from
        last_id = None
        if len(addresses) == 1:
            reply = self.horizon.account_transactions(addresses[0], params={'order': 'desc', 'limit': 2})
        else:
            reply = self.horizon.transactions(params={'order': 'desc', 'limit': 2})
        if len(reply['_embedded']['records']) == 2:
            tt = reply['_embedded']['records'][1]
            last_id = TransactionData(tt, strict=False).paging_token

        # start monitoring transactions from last_id. TODO: use cursor=now instead
        # make the SSE request synchronous (will raise errors in the current thread)
        if len(addresses) == 1:
            events = self.horizon.account_transactions(addresses[0], sse=True, params={'last_id': last_id})
        else:
            events = self.horizon.transactions(sse=True, params={'last_id': last_id})

        # asynchronous event processor
        def event_processor():
            import json
            for event in events:
                if event.event != 'message':
                    continue
                try:
                    tx = json.loads(event.data)

                    # get transaction operations
                    tx_ops = self.horizon.transaction_operations(tx['hash'], params={'limit': 100})
                    tx['operations'] = tx_ops['_embedded']['records']

                    # deserialize
                    tx_data = TransactionData(tx, strict=False)

                    # iterate over transaction operations and see if there's a match
                    for op_data in tx_data.operations:
                        if only_payments and op_data.type != 'payment':
                            continue
                        if asset:
                            if op_data.asset_type == 'native' and not asset.is_native():
                                continue
                            if op_data.asset_code != asset.code or op_data.asset_issuer != asset.issuer:
                                continue
                        if len(addresses) == 1:
                            callback_fn(addresses[0], tx_data)
                            break
                        elif op_data.from_address in addresses:
                            callback_fn(op_data.from_address, tx_data)
                            break
                        elif op_data.to_address in addresses:
                            callback_fn(op_data.to_address, tx_data)
                            break

                except Exception as e:
                    logger.exception(e)
                    continue

        # start monitoring thread
        import threading
        t = threading.Thread(target=event_processor)
        t.daemon = True
        t.start()
