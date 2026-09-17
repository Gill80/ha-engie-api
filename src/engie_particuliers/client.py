"""HTTP client for ENGIE Particuliers.

Reverse-engineered from com.engie.particuliers v10.28.0 (prod flavor).

Login:
  1. Okta AuthN at identite-prd.engie.fr/api/v1/authn
  2. PKCE authorization-code exchange at api.dgp.engie.fr (sessionToken)
  3. Client-id cookie at particuliers.engie.fr/digital-common-ws
  4. Account payload at /dvmobile-ws/api/private/espaceClient/v2/getInfoUsers
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from engie_particuliers.exceptions import (
    ApiError,
    AuthenticationError,
    LockedOutError,
    MfaRequiredError,
    PasswordExpiredError,
)
from engie_particuliers.models import (
    AccountInfo,
    CompteContrat,
    ContractPrice,
    EnergyContract,
    SessionInfo,
    TariffCadran,
)

APP_VERSION = "10.28.0"
OKTA_ORG = "https://identite-prd.engie.fr"
API_HOST = "particuliers.engie.fr"
API_BASE = f"https://{API_HOST}"
OIDC_DISCOVERY = (
    "https://api.dgp.engie.fr/oauth/v1/authorize/.well-known/openid-configuration"
)
CLIENT_ID = "5xpLtGiEUhB5tkv8hjPASrfodOyAPOGJ"
REDIRECT_URI = "engie:/loginCallback"
SCOPES = "openid profile offline_access idb2c apihour:read apihour:write"
USER_AGENT = f"Mozilla/{APP_VERSION} (Linux; Android 14; Pixel 8 Build/AP2A)"
AUTHN_USER_AGENT = "okta-authn-python/ha-engie-api"

# Native authorize is a GET that returns 200/302 with a Location custom-scheme URI.
_CUSTOM_SCHEME = "engie:"
# SMS / email / call only send the OTP after a verify POST without passCode.
_OUT_OF_BAND_FACTORS = frozenset({"sms", "email", "call"})


def factor_needs_challenge(factor: dict[str, Any] | None) -> bool:
    if not factor:
        return False
    return str(factor.get("factorType") or "").lower() in _OUT_OF_BAND_FACTORS


def factor_destination(factor: dict[str, Any] | None) -> str:
    if not factor:
        return ""
    profile = factor.get("profile") or {}
    if not isinstance(profile, dict):
        return ""
    return str(profile.get("phoneNumber") or profile.get("email") or "")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _random_state() -> str:
    return secrets.token_urlsafe(24)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_price(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 6)


def _energy_kind(contract: EnergyContract) -> str:
    blob = f"{contract.type} {contract.type_energie}".upper()
    if "ELEC" in blob or "ÉLEC" in blob or "ELECTRIC" in blob:
        return "ELEC"
    if "GAZ" in blob:
        return "GAZ"
    return (contract.type or contract.type_energie or "UNKNOWN").upper() or "UNKNOWN"


def _histo_rows(payload: dict[str, Any], energy: str) -> list[dict[str, Any]]:
    key = "historiquesElec" if energy == "ELEC" else "historiquesGaz"
    rows = payload.get(key) or []
    return [row for row in rows if isinstance(row, dict)]


def _latest_priced_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable: list[dict[str, Any]] = []
    for row in rows:
        conso = _to_float(row.get("conso"))
        ttc = _to_float(row.get("consoEuroTTC"))
        if conso is not None and conso > 0 and ttc is not None:
            usable.append(row)
    if not usable:
        return None

    def _sort_key(row: dict[str, Any]) -> str:
        return str(row.get("dateFin") or row.get("valeurDate") or row.get("dateDebut") or "")

    return max(usable, key=_sort_key)


def _implied_kwh(row: dict[str, Any]) -> tuple[float | None, float | None]:
    conso = _to_float(row.get("conso"))
    if conso is None or conso <= 0:
        return None, None
    ttc = _to_float(row.get("consoEuroTTC"))
    ht = _to_float(row.get("consoEuroHT"))
    return (
        _round_price(ttc / conso) if ttc is not None else None,
        _round_price(ht / conso) if ht is not None else None,
    )


def _cadrans_from_estimation(payload: dict[str, Any]) -> list[TariffCadran]:
    periods = payload.get("cadrans") or []
    if not isinstance(periods, list) or not periods:
        return []
    latest = periods[-1]
    if not isinstance(latest, dict):
        return []
    cadrans: list[TariffCadran] = []
    for name, data in latest.items():
        if not isinstance(data, dict):
            continue
        price = _to_float(data.get("prixKwh"))
        if price is None:
            continue
        cadrans.append(
            TariffCadran(
                cadran=str(name),
                price_kwh_ttc=_round_price(price),
                as_of=str(data.get("prixMois") or ""),
                source="estimationConsoElecMois",
            )
        )
    return cadrans


def _releve_contract(payload: dict[str, Any], energy: str) -> dict[str, Any]:
    key = "contratElec" if energy == "ELEC" else "contratGaz"
    block = payload.get(key) or {}
    return block if isinstance(block, dict) else {}


class EngieClient:
    """Client for the ENGIE Particuliers mobile API."""

    def __init__(
        self,
        username: str = "",
        password: str = "",
        *,
        timeout: float = 30.0,
    ) -> None:
        self._username = username.strip()
        self._password = password
        self._timeout = timeout
        self._http: Optional[httpx.Client] = None
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._id_token: Optional[str] = None
        self._expires_in: int = 0
        self._scope: str = ""
        self._oidc: Optional[dict[str, Any]] = None
        self._pending_mfa: Optional[dict[str, Any]] = None

    def set_credentials(self, username: str, password: str) -> None:
        self._username = username.strip()
        self._password = password

    def __enter__(self) -> EngieClient:
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def connect(self) -> None:
        if self._http is None:
            self._http = httpx.Client(
                timeout=self._timeout,
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            )

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
        self._http = None

    @property
    def session(self) -> SessionInfo:
        self._require_logged_in()
        return SessionInfo(
            access_token=self._access_token or "",
            refresh_token=self._refresh_token or "",
            id_token=self._id_token or "",
            expires_in=self._expires_in,
            scope=self._scope,
        )

    def login(
        self,
        *,
        mfa_code: str | None = None,
        mfa_factor_id: str | None = None,
        state_token: str | None = None,
    ) -> SessionInfo:
        """Authenticate and load tokens the same way the Android app does."""
        self.connect()
        if mfa_code:
            session_token = self._verify_mfa(
                mfa_code,
                factor_id=mfa_factor_id,
                state_token=state_token,
            )
        else:
            session_token = self._authenticate()
        self._exchange_session_token(session_token)
        self._create_client_id_cookie()
        return self.session

    def restore_session(
        self,
        access_token: str,
        refresh_token: str = "",
        *,
        id_token: str = "",
        expires_in: int = 0,
        scope: str = "",
        create_cookie: bool = True,
    ) -> SessionInfo:
        """Reuse tokens from a previous login (e.g. a `.token` file)."""
        self.connect()
        self._store_tokens(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "expires_in": expires_in,
                "scope": scope or SCOPES,
            }
        )
        if create_cookie:
            self._create_client_id_cookie()
        return self.session

    def challenge_mfa(
        self,
        *,
        factor_id: str | None = None,
        state_token: str | None = None,
    ) -> dict[str, Any]:
        """Ask Okta to send the SMS / email / voice OTP (app `sendMfaCode`).

        SMS and email codes are **not** sent on password success. The app first
        POSTs `/api/v1/authn/factors/{id}/verify` with only `stateToken`.
        """
        self.connect()
        pending = self._pending_mfa or {}
        token = state_token or pending.get("state_token")
        fid = factor_id or pending.get("factor_id")
        if not token or not fid:
            raise AuthenticationError(
                "Cannot send MFA code: factor id / state token is missing"
            )
        data = self._authn(
            "POST",
            f"/api/v1/authn/factors/{fid}/verify",
            json={"stateToken": token},
        )
        status = str(data.get("status") or "")
        if status not in {"MFA_CHALLENGE", "MFA_REQUIRED"}:
            raise AuthenticationError(f"Unexpected MFA challenge status: {status or data}")
        return self._store_pending_mfa(data, factor_id=fid)

    def get_account(self) -> AccountInfo:
        """GET /dvmobile-ws/api/private/espaceClient/v2/getInfoUsers."""
        self._require_logged_in()
        payload = self._api_json(
            "GET",
            "/dvmobile-ws/api/private/espaceClient/v2/getInfoUsers",
        )
        if not isinstance(payload, dict):
            raise ApiError(200, "unexpected getInfoUsers payload", payload)
        return AccountInfo.from_remote(payload)

    def get_contracts(self, ref_bp: str, compte_contrat: str) -> dict[str, Any]:
        """GET /dvmobile-ws/api/private/espaceClient/getInfoContrats."""
        self._require_logged_in()
        payload = self._api_json(
            "GET",
            "/dvmobile-ws/api/private/espaceClient/getInfoContrats",
            params={"refBP": ref_bp, "compteContrat": compte_contrat},
        )
        if not isinstance(payload, dict):
            raise ApiError(200, "unexpected getInfoContrats payload", payload)
        return payload

    def get_identifiant(self) -> dict[str, Any]:
        """GET /auth-ws/api/private/identifiant."""
        self._require_logged_in()
        payload = self._api_json("GET", "/auth-ws/api/private/identifiant")
        if not isinstance(payload, dict):
            raise ApiError(200, "unexpected identifiant payload", payload)
        return payload

    def select_account(self, ref_bp: str, numero_cc: str, id_personne: str) -> None:
        """Set the Cel_Cc / Cel_Bp / idPersonne cookies the app attaches on maConso calls."""
        http = self._http_client()
        for name, value in (
            ("Cel_Cc", numero_cc),
            ("Cel_Bp", ref_bp),
            ("idPersonne", id_personne),
        ):
            if value:
                http.cookies.set(name, str(value), domain=API_HOST, path="/")

    def get_releve(self, ref_bp: str, compte_contrat: str) -> dict[str, Any]:
        """GET /dvmobile-ws/api/private/releve/recupererDonneesReleveV2."""
        payload = self._api_json(
            "GET",
            "/dvmobile-ws/api/private/releve/recupererDonneesReleveV2",
            params={"refBP": ref_bp, "compteContrat": compte_contrat},
        )
        return payload if isinstance(payload, dict) else {}

    def get_eco_gestes(
        self, ref_bp: str, compte_contrat: str, id_personne: str
    ) -> dict[str, Any]:
        """GET /dvmobile-ws/api/private/maConso/v2/ecoGestesDynamiques (`prixKwh`)."""
        payload = self._api_json(
            "GET",
            "/dvmobile-ws/api/private/maConso/v2/ecoGestesDynamiques",
            params={
                "refBP": ref_bp,
                "compteContrat": compte_contrat,
                "idPersonne": id_personne,
            },
        )
        return payload if isinstance(payload, dict) else {}

    def get_estimation_elec(self, ref_bp: str, compte_contrat: str) -> dict[str, Any]:
        """GET /dvmobile-ws/api/private/maConso/v2/estimationConsoElecMois."""
        payload = self._api_json(
            "GET",
            "/dvmobile-ws/api/private/maConso/v2/estimationConsoElecMois",
            params={"refBP": ref_bp, "compteContrat": compte_contrat},
        )
        return payload if isinstance(payload, dict) else {}

    def get_estimation_gaz(self, ref_bp: str, compte_contrat: str) -> dict[str, Any]:
        """GET /dvmobile-ws/api/private/maConso/v2/estimationConsoGazMois."""
        payload = self._api_json(
            "GET",
            "/dvmobile-ws/api/private/maConso/v2/estimationConsoGazMois",
            params={"refBP": ref_bp, "compteContrat": compte_contrat},
        )
        return payload if isinstance(payload, dict) else {}

    def get_monthly_history(
        self,
        energy: str,
        *,
        ref_bp: str,
        compte_contrat: str,
        communicant: bool = True,
        code_postal: str = "",
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> dict[str, Any]:
        """GET histoElecMois / histoGazMois. Do not pass `pdl` (empty payload)."""
        now = datetime.now(timezone.utc)
        if end_ms is None:
            end_ms = int(now.timestamp() * 1000)
        if start_ms is None:
            start_ms = int((now - timedelta(days=400)).timestamp() * 1000)
        kind = energy.upper()
        path = (
            "/dvmobile-ws/api/private/maConso/v7/histoElecMois"
            if kind == "ELEC"
            else "/dvmobile-ws/api/private/maConso/v7/histoGazMois"
        )
        params: dict[str, Any] = {
            "communicant": "true" if communicant else "false",
            "refBP": ref_bp,
            "compteContrat": compte_contrat,
            "startDate": start_ms,
            "endDate": end_ms,
        }
        if code_postal:
            params["codePostal"] = code_postal
        payload = self._api_json("GET", path, params=params)
        return payload if isinstance(payload, dict) else {}

    def get_prices(self, account: AccountInfo | None = None) -> list[ContractPrice]:
        """Current €/kWh TTC for every energy contract on the account.

        Prefer the app's contractual `prixKwh` (eco-gestes for electricity,
        estimation for gas). Fall back to latest billed month
        `consoEuroTTC / conso` when those endpoints are blocked (403).
        """
        if account is None:
            account = self.get_account()
        prices: list[ContractPrice] = []
        for partner in account.partners:
            for cc in partner.comptes_contrat:
                prices.extend(self._prices_for_compte(partner.ref_bp, partner.id_personne, cc))
        return prices

    def _prices_for_compte(
        self, ref_bp: str, id_personne: str, cc: CompteContrat
    ) -> list[ContractPrice]:
        self.select_account(ref_bp, cc.numero_cc, id_personne)
        code_postal = cc.adresse.cp if cc.adresse else ""
        releve = self._try_json(lambda: self.get_releve(ref_bp, cc.numero_cc))
        eco = self._try_json(
            lambda: self.get_eco_gestes(ref_bp, cc.numero_cc, id_personne)
        )
        estimation_elec = self._try_json(
            lambda: self.get_estimation_elec(ref_bp, cc.numero_cc)
        )
        estimation_gaz = self._try_json(
            lambda: self.get_estimation_gaz(ref_bp, cc.numero_cc)
        )
        kinds = {_energy_kind(contract) for contract in cc.contrats} or {"ELEC", "GAZ"}
        histo: dict[str, dict[str, Any]] = {}
        for energy in ("ELEC", "GAZ"):
            if energy not in kinds:
                continue
            meter = _releve_contract(releve, energy)
            preferred = (
                bool(meter["compteurCommuniquant"])
                if "compteurCommuniquant" in meter
                else None
            )
            histo[energy] = self._history_with_fallback(
                energy,
                ref_bp=ref_bp,
                compte_contrat=cc.numero_cc,
                code_postal=code_postal,
                preferred_communicant=preferred,
            )
        return [
            self._price_for_contract(
                contract,
                eco=eco,
                estimation_elec=estimation_elec,
                estimation_gaz=estimation_gaz,
                histo=histo,
            )
            for contract in cc.contrats
        ]

    def _price_for_contract(
        self,
        contract: EnergyContract,
        *,
        eco: dict[str, Any],
        estimation_elec: dict[str, Any],
        estimation_gaz: dict[str, Any],
        histo: dict[str, dict[str, Any]],
    ) -> ContractPrice:
        energy = _energy_kind(contract)
        row = _latest_priced_row(_histo_rows(histo.get(energy) or {}, energy))
        implied_ttc, implied_ht = _implied_kwh(row) if row else (None, None)
        abo = _to_float(row.get("abonnementTTC")) if row else None
        as_of_histo = str((row or {}).get("dateFin") or (row or {}).get("valeurDate") or "")

        cadrans: list[TariffCadran] = []
        price_ttc: float | None = None
        price_ht = implied_ht
        as_of = as_of_histo
        source = ""
        notes = ""

        if energy == "ELEC":
            cadrans = _cadrans_from_estimation(estimation_elec)
            if cadrans:
                price_ttc = cadrans[0].price_kwh_ttc
                as_of = cadrans[0].as_of or as_of
                source = "estimationConsoElecMois"
            else:
                eco_price = _to_float(eco.get("prixKwh"))
                if eco_price is not None:
                    price_ttc = _round_price(eco_price)
                    source = "ecoGestesDynamiques"
                    as_of = as_of or datetime.now(timezone.utc).date().isoformat()
                    cadrans = [
                        TariffCadran(
                            cadran="BASE",
                            price_kwh_ttc=price_ttc,
                            price_kwh_ht=price_ht,
                            as_of=as_of,
                            source=source,
                        )
                    ]
                    notes = "Contractual TTC rate used by the app eco-gestes calculator"
                elif implied_ttc is not None:
                    price_ttc = implied_ttc
                    source = "histoElecMois"
                    cadrans = [
                        TariffCadran(
                            cadran="BASE",
                            price_kwh_ttc=price_ttc,
                            price_kwh_ht=price_ht,
                            as_of=as_of,
                            source=source,
                        )
                    ]
                    notes = "Implied from latest billed month (consoEuroTTC / conso), excluding subscription"
        elif energy == "GAZ":
            gaz_price = _to_float(estimation_gaz.get("prixKwh"))
            if gaz_price is not None:
                price_ttc = _round_price(gaz_price)
                source = "estimationConsoGazMois"
                as_of = str(estimation_gaz.get("prixMois") or as_of)
                notes = "Contractual TTC rate from monthly gas estimation"
            elif implied_ttc is not None:
                price_ttc = implied_ttc
                source = "histoGazMois"
                notes = "Implied from latest billed month (consoEuroTTC / conso), excluding subscription; gas kWh is variable"
            if price_ttc is not None:
                cadrans = [
                    TariffCadran(
                        cadran="BASE",
                        price_kwh_ttc=price_ttc,
                        price_kwh_ht=price_ht,
                        as_of=as_of,
                        source=source,
                    )
                ]
        else:
            notes = f"Unknown energy type {energy!r}"

        if price_ttc is None and implied_ttc is not None:
            price_ttc = implied_ttc
            source = source or ("histoElecMois" if energy == "ELEC" else "histoGazMois")
            as_of = as_of or as_of_histo
        if price_ttc is None:
            notes = notes or "No kWh price available (estimation 403 and empty history)"

        return ContractPrice(
            contract_id=contract.id,
            libelle=contract.libelle,
            energy=energy,
            type_comptage=contract.type_comptage,
            puissance=contract.puissance,
            price_kwh_ttc=price_ttc,
            price_kwh_ht=price_ht,
            cadrans=cadrans,
            as_of=as_of,
            source=source,
            abonnement_ttc=_round_price(abo),
            notes=notes,
        )

    def _try_json(self, call: Any) -> dict[str, Any]:
        try:
            payload = call()
        except ApiError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _history_with_fallback(
        self,
        energy: str,
        *,
        ref_bp: str,
        compte_contrat: str,
        code_postal: str = "",
        preferred_communicant: bool | None = None,
    ) -> dict[str, Any]:
        if preferred_communicant is None:
            flags = (True, False)
        else:
            flags = (preferred_communicant, not preferred_communicant)
        payloads: list[dict[str, Any]] = []
        for communicant in flags:
            payload = self._try_json(
                lambda communicant=communicant: self.get_monthly_history(
                    energy,
                    ref_bp=ref_bp,
                    compte_contrat=compte_contrat,
                    communicant=communicant,
                    code_postal=code_postal,
                )
            )
            payloads.append(payload)
            if _latest_priced_row(_histo_rows(payload, energy)):
                return payload
        for payload in payloads:
            if payload:
                return payload
        return payloads[0] if payloads else {}

    def refresh_tokens(self) -> SessionInfo:
        self._require_logged_in()
        if not self._refresh_token:
            raise AuthenticationError("No refresh token on this session")
        oidc = self._provider()
        response = self._http_client().post(
            oidc["token_endpoint"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": CLIENT_ID,
                "scope": SCOPES,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if response.status_code >= 400:
            raise AuthenticationError(
                f"token refresh failed ({response.status_code}): {response.text}"
            )
        self._store_tokens(response.json())
        self._create_client_id_cookie()
        return self.session

    # ------------------------------------------------------------------
    # Okta AuthN
    # ------------------------------------------------------------------

    def _authenticate(self) -> str:
        body = {
            "username": self._username,
            "password": self._password,
            "options": {"multiOptionalFactorEnroll": True, "warnBeforePasswordExpired": True},
        }
        data = self._authn("POST", "/api/v1/authn", json=body)
        return self._session_token_from_authn(data)

    def _verify_mfa(
        self,
        code: str,
        *,
        factor_id: str | None,
        state_token: str | None,
    ) -> str:
        pending = self._pending_mfa or {}
        token = state_token or pending.get("state_token")
        fid = factor_id or pending.get("factor_id")
        if not token or not fid:
            raise AuthenticationError(
                "MFA code given but factor id / state token is missing"
            )
        data = self._authn(
            "POST",
            f"/api/v1/authn/factors/{fid}/verify",
            json={"stateToken": token, "passCode": code},
        )
        return self._session_token_from_authn(data)

    def _store_pending_mfa(
        self,
        data: dict[str, Any],
        *,
        factor_id: str | None = None,
    ) -> dict[str, Any]:
        embedded = data.get("_embedded") or {}
        factors: list[dict[str, Any]] = []
        raw_factors = embedded.get("factors") or []
        if isinstance(raw_factors, list):
            factors = [item for item in raw_factors if isinstance(item, dict)]
        single = embedded.get("factor")
        if isinstance(single, dict):
            factors = [single] + [item for item in factors if item.get("id") != single.get("id")]
        previous = self._pending_mfa or {}
        if not factors:
            factors = list(previous.get("factors") or [])
        default_id = factor_id or str(previous.get("factor_id") or "")
        if not default_id and factors:
            default_id = str(factors[0].get("id") or "")
        pending = {
            "state_token": str(data.get("stateToken") or previous.get("state_token") or ""),
            "factor_id": default_id,
            "factors": factors,
            "status": str(data.get("status") or ""),
        }
        self._pending_mfa = pending
        return pending

    def _session_token_from_authn(self, data: dict[str, Any]) -> str:
        status = str(data.get("status") or "")
        if status == "SUCCESS":
            session_token = data.get("sessionToken")
            if not session_token:
                raise AuthenticationError("AuthN SUCCESS without sessionToken")
            self._pending_mfa = None
            return str(session_token)
        if status == "MFA_REQUIRED":
            pending = self._store_pending_mfa(data)
            raise MfaRequiredError(pending["state_token"], pending["factors"])
        if status == "MFA_CHALLENGE":
            pending = self._store_pending_mfa(data)
            raise AuthenticationError(
                "MFA code missing or rejected; Okta is still waiting for the OTP"
                + (f" (factor {pending.get('factor_id')})" if pending.get("factor_id") else "")
            )
        if status == "LOCKED_OUT":
            raise LockedOutError("Account is locked out")
        if status == "PASSWORD_EXPIRED":
            raise PasswordExpiredError("Password expired")
        raise AuthenticationError(f"Unexpected AuthN status: {status or data}")

    def _authn(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = urljoin(OKTA_ORG + "/", path.lstrip("/"))
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": AUTHN_USER_AGENT,
        }
        response = self._http_client().request(method, url, headers=headers, **kwargs)
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text}
        if response.status_code >= 400:
            error = ""
            if isinstance(payload, dict):
                error = str(
                    payload.get("errorSummary")
                    or payload.get("errorCode")
                    or payload.get("errorCauses")
                    or payload
                )
            raise AuthenticationError(
                f"AuthN failed ({response.status_code}): {error or response.text}"
            )
        if not isinstance(payload, dict):
            raise AuthenticationError("AuthN returned a non-object payload")
        return payload

    # ------------------------------------------------------------------
    # OIDC sessionToken → tokens
    # ------------------------------------------------------------------

    def _provider(self) -> dict[str, Any]:
        if self._oidc is None:
            response = self._http_client().get(
                OIDC_DISCOVERY, headers={"Accept": "application/json"}
            )
            if response.status_code >= 400:
                raise AuthenticationError(
                    f"OIDC discovery failed ({response.status_code}): {response.text}"
                )
            self._oidc = response.json()
        return self._oidc

    def _exchange_session_token(self, session_token: str) -> None:
        oidc = self._provider()
        verifier, challenge = _pkce_pair()
        state = _random_state()
        nonce = _random_state()
        authorize_url = oidc["authorization_endpoint"]
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "scope": SCOPES,
            "redirect_uri": REDIRECT_URI,
            "sessionToken": session_token,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        response = self._http_client().get(authorize_url, params=params)
        location = response.headers.get("Location") or ""
        hops = 0
        while (
            response.status_code in {301, 302, 303, 307, 308}
            and location
            and not location.startswith(_CUSTOM_SCHEME)
            and hops < 8
        ):
            next_url = urljoin(str(response.url), location)
            response = self._http_client().get(next_url)
            location = response.headers.get("Location") or ""
            hops += 1
        if not location:
            raise AuthenticationError(
                f"authorize returned {response.status_code} without Location"
            )
        parsed = urlparse(location)
        query = parse_qs(parsed.query)
        if query.get("error"):
            desc = (query.get("error_description") or query.get("error") or [""])[0]
            raise AuthenticationError(f"authorize error: {desc}")
        codes = query.get("code") or []
        if not codes:
            raise AuthenticationError(f"authorize Location had no code: {location}")
        returned_state = (query.get("state") or [""])[0]
        if returned_state and returned_state != state:
            raise AuthenticationError("authorize state mismatch")

        token_response = self._http_client().post(
            oidc["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "code": codes[0],
                "code_verifier": verifier,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if token_response.status_code >= 400:
            raise AuthenticationError(
                f"token exchange failed ({token_response.status_code}): {token_response.text}"
            )
        self._store_tokens(token_response.json())

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        access = payload.get("access_token")
        if not access:
            raise AuthenticationError(f"token response missing access_token: {payload}")
        self._access_token = str(access)
        self._refresh_token = str(payload.get("refresh_token") or self._refresh_token or "")
        self._id_token = str(payload.get("id_token") or "")
        try:
            self._expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            self._expires_in = 0
        self._scope = str(payload.get("scope") or SCOPES)
        self._sync_access_token_cookie()

    def _sync_access_token_cookie(self) -> None:
        """The mobile API reads `access_token` from a cookie, not only Bearer.

        EngieCookieJar.createNonPersistentCookie(token, "access_token") on
        particuliers.engie.fr. Without it, REST calls return
        `401 Acces Token not exist`.
        """
        if not self._access_token:
            return
        self._http_client().cookies.set(
            "access_token",
            self._access_token,
            domain=API_HOST,
            path="/",
        )

    # ------------------------------------------------------------------
    # Mobile API
    # ------------------------------------------------------------------

    def _create_client_id_cookie(self) -> None:
        self._api_request(
            "POST",
            "/digital-common-ws/api-management/creation-cookie",
            demandeur="PARTICULIERS_ANDROID",
        )

    def _api_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        demandeur: str = "MOBILE_ANDROID",
    ) -> Any:
        response = self._api_request(
            method, path, params=params, json=json, demandeur=demandeur
        )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(response.status_code, "invalid JSON", response.text) from exc

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        demandeur: str = "MOBILE_ANDROID",
    ) -> httpx.Response:
        self._require_logged_in()
        self._sync_access_token_cookie()
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "*/*",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-Correlation-ID": str(uuid.uuid4()),
            "demandeur": demandeur,
        }
        url = urljoin(API_BASE + "/", path.lstrip("/"))
        response = self._http_client().request(
            method, url, headers=headers, params=params, json=json
        )
        if response.status_code >= 400:
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = response.text
            message = ""
            if isinstance(body, dict):
                message = str(
                    body.get("message") or body.get("error") or body.get("status") or ""
                )
            raise ApiError(response.status_code, message, body)
        return response

    def _http_client(self) -> httpx.Client:
        self.connect()
        assert self._http is not None
        return self._http

    def _require_logged_in(self) -> None:
        if not self._access_token:
            raise AuthenticationError("Not logged in")
