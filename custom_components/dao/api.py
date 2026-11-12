"""Compatibility imports for the account source's client.

New code imports from :mod:`dao.account.client`; this module preserves the
pre-split import path, which is also what existing automations and the test
suite use. The public barcode source has its own client in
:mod:`dao.tracking.api`.
"""

from .account.client import (
    DAOApiClient,
    DAOApiError,
    DAOAuthError,
    build_authorization_url,
    new_pkce,
    resolve_lang,
)

__all__ = [
    "DAOApiClient",
    "DAOApiError",
    "DAOAuthError",
    "build_authorization_url",
    "new_pkce",
    "resolve_lang",
]
