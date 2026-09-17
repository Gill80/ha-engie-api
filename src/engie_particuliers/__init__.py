"""Python client for ENGIE Particuliers (com.engie.particuliers v10.28.0)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engie_particuliers.exceptions import (
    ApiError,
    AuthenticationError,
    EngieError,
    LockedOutError,
    MfaRequiredError,
    PasswordExpiredError,
)
from engie_particuliers.models import (
    AccountInfo,
    Address,
    BusinessPartner,
    CompteContrat,
    ContractPrice,
    EnergyContract,
    SessionInfo,
    TariffCadran,
)

if TYPE_CHECKING:
    from engie_particuliers.client import EngieClient

__all__ = [
    "EngieClient",
    "EngieError",
    "AuthenticationError",
    "MfaRequiredError",
    "LockedOutError",
    "PasswordExpiredError",
    "ApiError",
    "SessionInfo",
    "AccountInfo",
    "BusinessPartner",
    "CompteContrat",
    "EnergyContract",
    "Address",
    "ContractPrice",
    "TariffCadran",
]


def __getattr__(name: str):
    if name == "EngieClient":
        from engie_particuliers.client import EngieClient as _Client

        return _Client
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
