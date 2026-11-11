"""Compatibility module for the account source's normaliser.

New code imports from :mod:`dao.account.parcels`; the whole module namespace
is mirrored here, including the private one-shot warning flags the test suite
resets, so every pre-split import path keeps resolving. The shared status
vocabulary itself lives in :mod:`dao.status`, which both sources read.
"""

from .account import parcels as _account

globals().update(vars(_account))
