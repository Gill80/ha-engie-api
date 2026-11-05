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
import hmac
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
    AccountBalance,
    AccountInfo,
    AccountSnapshot,
    BillingSnapshot,
    CompteContrat,
    ConsumptionPoint,
    ContractPrice,
    ContractSnapshot,
    EnergyContract,
    Invoice,
    SessionInfo,
    TariffCadran,
    PARIS,
    parse_engie_datetime,
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


_HISTORY_PATHS = {
    ("ELEC", "month"): "/dvmobile-ws/api/private/maConso/v7/histoElecMois",
    ("GAZ", "month"): "/dvmobile-ws/api/private/maConso/v7/histoGazMois",
    ("ELEC", "day"): "/dvmobile-ws/api/private/maConso/v3/histoElecJours",
    ("GAZ", "day"): "/dvmobile-ws/api/private/maConso/v3/histoGazJours",
    ("ELEC", "hour"): "/dvmobile-ws/api/private/maConso/v4/histoElecHeures",
}

_DEFAULT_HISTORY_DAYS = {"hour": 3, "day": 400, "month": 400}

# Keep diagnostics limited to API schema names and technical response metadata.
# In particular, never retain URLs, query parameters, tokens or response values.
_GAS_API_ENDPOINTS = frozenset({
    "estimationConsoGazMois", "histoGazMois", "histoGazJours",
})
_GAS_SAFE_TOP_LEVEL_FIELDS = frozenset({
    "historiquesGaz", "historiqueGaz", "historique", "consommations",
    "data", "result", "content", "prixKwh", "prixMois",
    "cadrans", "error", "status", "message",
})
_GAS_SAFE_ROW_FIELDS = frozenset({
    "conso", "consoEuroTTC", "consoEuroHT", "abonnementTTC",
    "dateDebut", "dateFin", "valeurDate", "consommation",
    "consommationKwh", "volumeM3", "m3",
})


def _range_ms(
    days: int | None = None,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    granularity: str = "day",
) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    if end_ms is None:
        end_ms = int(now.timestamp() * 1000)
    if start_ms is None:
        window = days if days is not None else _DEFAULT_HISTORY_DAYS.get(granularity, 400)
        start_ms = int((now - timedelta(days=window)).timestamp() * 1000)
    return start_ms, end_ms


def _account_id(ref_bp: str, numero_cc: str) -> str:
    return hashlib.sha256(f"{ref_bp}|{numero_cc}".encode()).hexdigest()[:16]


def _normalize_energy(energy: str) -> str:
    blob = (energy or "").strip().upper()
    if blob in {"ELEC", "ELECTRICITY", "ELECTRICITE", "ÉLEC", "ÉLECTRICITÉ"}:
        return "ELEC"
    if blob in {"GAZ", "GAS"}:
        return "GAZ"
    return blob


def _normalize_granularity(granularity: str) -> str:
    blob = (granularity or "day").strip().lower()
    if blob in {"h", "hour", "hours", "heure", "heures"}:
        return "hour"
    if blob in {"m", "month", "months", "mois"}:
        return "month"
    return "day"


def _consumption_points(
    payload: dict[str, Any], *, energy: str, granularity: str
) -> list[ConsumptionPoint]:
    points = [
        ConsumptionPoint.from_remote(row, energy=energy, granularity=granularity)
        for row in _histo_rows(payload, energy)
    ]
    points.sort(key=lambda item: item.start or item.end)
    return points


def _point_civil_date(point: ConsumptionPoint) -> Any:
    parsed = parse_engie_datetime(point.start)
    if parsed is None:
        return None
    return parsed.astimezone(PARIS).date()


def _latest_consumption(
    points: list[ConsumptionPoint], *, complete_only: bool = False
) -> ConsumptionPoint | None:
    usable = [item for item in points if item.kwh is not None]
    if complete_only:
        usable = [item for item in usable if not item.partial]
    if not usable:
        return None
    return max(usable, key=lambda item: item.end or item.start)


def _invoices_from_remote(payload: dict[str, Any]) -> list[Invoice]:
    invoices: list[Invoice] = []
    seen: set[str] = set()
    for item in payload.get("listeFactures") or []:
        parsed = Invoice.from_remote(item if isinstance(item, dict) else None)
        if parsed is None:
            continue
        invoices.append(parsed)
        if parsed.number:
            seen.add(parsed.number)
    last = Invoice.from_remote(
        payload.get("derniereFacture")
        if isinstance(payload.get("derniereFacture"), dict)
        else None
    )
    if last is not None and (not last.number or last.number not in seen):
        invoices.insert(0, last)
    return invoices


def _latest_invoice(invoices: list[Invoice]) -> Invoice | None:
    if not invoices:
        return None

    def _key(item: Invoice) -> str:
        parsed = parse_engie_datetime(item.date or item.due_date)
        return parsed.isoformat() if parsed else (item.date or item.due_date or item.number)

    return max(invoices, key=_key)


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
        self._gas_api_diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._gas_pce_sha256: str = ""

    def set_gas_pce_sha256(self, digest: str | None) -> None:
        """Select the actual gas meter by a locally configured PCE digest.

        The plain PCE is never kept by the client or sent in diagnostics.
        Selection only takes effect when there is exactly one gas contract.
        """
        value = (digest or "").strip().lower()
        if value and (len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)):
            raise ValueError("Invalid gas PCE SHA-256 digest")
        self._gas_pce_sha256 = value

    def _gas_source_for_account(
        self, account: AccountInfo
    ) -> tuple[Any, CompteContrat] | None:
        """Resolve the meter matched by PCE, including electricity-listed accounts.

        ENGIE can expose the active gas meter under a contract account whose
        listeContrat mentions only electricity. Never guess for multi-gas
        accounts or when the PCE is absent/ambiguous.
        """
        if not self._gas_pce_sha256:
            return None
        gas_contracts = [
            contract
            for partner in account.partners
            for cc in partner.comptes_contrat
            for contract in cc.contrats
            if _energy_kind(contract) == "GAZ"
        ]
        if len(gas_contracts) != 1:
            return None
        matches: list[tuple[Any, CompteContrat]] = []
        for partner in account.partners:
            for cc in partner.comptes_contrat:
                if not partner.ref_bp or not cc.numero_cc:
                    continue
                self.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)
                releve = self._try_json(
                    lambda ref=partner.ref_bp, number=cc.numero_cc: self.get_releve(ref, number)
                )
                meter = _releve_contract(releve, "GAZ")
                meter_id = "".join(ch for ch in str(meter.get("idPDL") or "") if ch.isdigit())
                if not (10 <= len(meter_id) <= 17):
                    continue
                hashed = hashlib.sha256(meter_id.encode("ascii")).hexdigest()
                if hmac.compare_digest(hashed, self._gas_pce_sha256):
                    matches.append((partner, cc))
        return matches[0] if len(matches) == 1 else None

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
    def logged_in(self) -> bool:
        return bool(self._access_token)

    @property
    def gas_api_diagnostics(self) -> dict[str, list[dict[str, Any]]]:
        """Technical gas response summaries, safe for Home Assistant diagnostics.

        The last six attempts per endpoint are kept in memory only. No API
        response values, URLs, authentication details or account IDs are stored.
        """
        return {
            endpoint: [dict(attempt) for attempt in attempts]
            for endpoint, attempts in self._gas_api_diagnostics.items()
        }

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
        """GET /dvmobile-ws/api/private/espaceClient/v2/getInfoContrats."""
        self._require_logged_in()
        payload = self._api_json(
            "GET",
            "/dvmobile-ws/api/private/espaceClient/v2/getInfoContrats",
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

    def get_history(
        self,
        energy: str,
        *,
        granularity: str = "month",
        ref_bp: str,
        compte_contrat: str,
        communicant: bool = True,
        code_postal: str = "",
        start_ms: int | None = None,
        end_ms: int | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        """GET histoElec/histoGaz for hour, day, or month. Do not pass `pdl`."""
        kind = _normalize_energy(energy)
        grain = _normalize_granularity(granularity)
        path = _HISTORY_PATHS.get((kind, grain))
        if not path:
            raise ApiError(400, f"unsupported history {kind}/{grain}")
        start_ms, end_ms = _range_ms(
            days, start_ms=start_ms, end_ms=end_ms, granularity=grain
        )
        params: dict[str, Any] = {
            "refBP": ref_bp,
            "compteContrat": compte_contrat,
            "startDate": start_ms,
            "endDate": end_ms,
        }
        if grain == "month":
            params["communicant"] = "true" if communicant else "false"
        if code_postal and grain != "hour":
            params["codePostal"] = code_postal
        payload = self._api_json("GET", path, params=params)
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
        return self.get_history(
            energy,
            granularity="month",
            ref_bp=ref_bp,
            compte_contrat=compte_contrat,
            communicant=communicant,
            code_postal=code_postal,
            start_ms=start_ms,
            end_ms=end_ms,
        )

    def get_daily_history(
        self,
        energy: str,
        *,
        ref_bp: str,
        compte_contrat: str,
        code_postal: str = "",
        start_ms: int | None = None,
        end_ms: int | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        """GET histoElecJours / histoGazJours."""
        return self.get_history(
            energy,
            granularity="day",
            ref_bp=ref_bp,
            compte_contrat=compte_contrat,
            code_postal=code_postal,
            start_ms=start_ms,
            end_ms=end_ms,
            days=days,
        )

    def get_hourly_history(
        self,
        *,
        ref_bp: str,
        compte_contrat: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        """GET histoElecHeures (electricity only)."""
        return self.get_history(
            "ELEC",
            granularity="hour",
            ref_bp=ref_bp,
            compte_contrat=compte_contrat,
            start_ms=start_ms,
            end_ms=end_ms,
            days=days,
        )

    def get_invoices(self, ref_bp: str, compte_contrat: str) -> dict[str, Any]:
        """GET /dvmobile-ws/api/private/infolistefacture/listeFacture."""
        payload = self._api_json(
            "GET",
            "/dvmobile-ws/api/private/infolistefacture/listeFacture",
            params={"refBP": ref_bp, "compteContrat": compte_contrat},
        )
        return payload if isinstance(payload, dict) else {}

    def get_balance(self, ref_bp: str, compte_contrat: str) -> dict[str, Any]:
        """GET /dvmobile-ws/api/private/facture/soldeApayerV2."""
        payload = self._api_json(
            "GET",
            "/dvmobile-ws/api/private/facture/soldeApayerV2",
            params={"refBP": ref_bp, "compteContrat": compte_contrat},
        )
        return payload if isinstance(payload, dict) else {}

    def get_consumption_points(
        self,
        energy: str,
        *,
        granularity: str = "day",
        ref_bp: str,
        compte_contrat: str,
        code_postal: str = "",
        start_ms: int | None = None,
        end_ms: int | None = None,
        days: int | None = None,
        preferred_communicant: bool | None = None,
    ) -> list[ConsumptionPoint]:
        """Typed histo rows for Energy-dashboard import and CLI dumps."""
        kind = _normalize_energy(energy)
        grain = _normalize_granularity(granularity)
        if grain == "month":
            payload = self._history_with_fallback(
                kind,
                granularity=grain,
                ref_bp=ref_bp,
                compte_contrat=compte_contrat,
                code_postal=code_postal,
                preferred_communicant=preferred_communicant,
                start_ms=start_ms,
                end_ms=end_ms,
                days=days,
            )
        else:
            payload = self._try_json(
                lambda: self.get_history(
                    kind,
                    granularity=grain,
                    ref_bp=ref_bp,
                    compte_contrat=compte_contrat,
                    code_postal=code_postal,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    days=days,
                )
            )
        points = _consumption_points(payload, energy=kind, granularity=grain)
        if grain == "day":
            points = self._with_intraday_day(
                points,
                energy=kind,
                ref_bp=ref_bp,
                compte_contrat=compte_contrat,
            )
        return points

    def _with_intraday_day(
        self,
        daily: list[ConsumptionPoint],
        *,
        energy: str,
        ref_bp: str,
        compte_contrat: str,
    ) -> list[ConsumptionPoint]:
        """Linky daily histo is D+1. Attach a partial today row from hourly when present.

        Half-hourly is often D+2, so this usually adds nothing — kept so same-day
        slots are used the moment ENGIE publishes them.
        """
        if energy != "ELEC":
            return daily
        today = datetime.now(PARIS).date()
        covered = {_point_civil_date(item) for item in daily}
        if today in covered:
            return daily
        payload = self._try_json(
            lambda: self.get_hourly_history(
                ref_bp=ref_bp, compte_contrat=compte_contrat, days=2
            )
        )
        hours = _consumption_points(payload, energy="ELEC", granularity="hour")
        today_hours = [
            item
            for item in hours
            if item.kwh is not None and _point_civil_date(item) == today
        ]
        if not today_hours:
            return daily
        kwh = sum(float(item.kwh or 0) for item in today_hours)
        cost_ttc = sum(float(item.cost_ttc or 0) for item in today_hours)
        cost_ht = sum(float(item.cost_ht or 0) for item in today_hours)
        start = datetime(today.year, today.month, today.day, tzinfo=PARIS)
        daily.append(
            ConsumptionPoint(
                energy="ELEC",
                granularity="day",
                start=start.isoformat(),
                end=(start + timedelta(days=1)).isoformat(),
                kwh=round(kwh, 3),
                cost_ttc=round(cost_ttc, 2) if cost_ttc else None,
                cost_ht=round(cost_ht, 2) if cost_ht else None,
                partial=True,
            )
        )
        daily.sort(key=lambda item: item.start or item.end)
        return daily

    def get_consumption_points_for_contract(
        self,
        contract_id: str,
        *,
        granularity: str = "day",
        days: int | None = None,
        account: AccountInfo | None = None,
    ) -> list[ConsumptionPoint]:
        """Resolve a contract id then fetch histo for that energy."""
        if account is None:
            account = self.get_account()
        partner, cc, contract = self._find_contract(contract_id, account)
        if _energy_kind(contract) == "GAZ":
            source = self._gas_source_for_account(account)
            if source is not None:
                partner, cc = source
        self.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)
        code_postal = cc.adresse.cp if cc.adresse else ""
        return self.get_consumption_points(
            _energy_kind(contract),
            granularity=granularity,
            ref_bp=partner.ref_bp,
            compte_contrat=cc.numero_cc,
            code_postal=code_postal,
            days=days,
        )

    def get_prices(self, account: AccountInfo | None = None) -> list[ContractPrice]:
        """Current €/kWh TTC for every energy contract on the account.

        Prefer the app's contractual `prixKwh` (eco-gestes for electricity,
        estimation for gas). Fall back to latest billed month
        `consoEuroTTC / conso` when those endpoints are blocked (403).
        """
        if account is None:
            account = self.get_account()
        prices: list[ContractPrice] = []
        gas_source = self._gas_source_for_account(account)
        for partner in account.partners:
            for cc in partner.comptes_contrat:
                prices.extend(
                    self._prices_for_compte(
                        partner.ref_bp, partner.id_personne, cc,
                        gas_source=gas_source,
                    )
                )
        return prices

    def _prices_for_compte(
        self,
        ref_bp: str,
        id_personne: str,
        cc: CompteContrat,
        *,
        gas_source: tuple[Any, CompteContrat] | None = None,
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
        prices = [
            self._price_for_contract(
                contract,
                eco=eco,
                estimation_elec=estimation_elec,
                estimation_gaz=estimation_gaz,
                histo=histo,
            )
            for contract in cc.contrats
        ]
        if gas_source is not None:
            source_partner, source_cc = gas_source
            if source_cc is not cc and any(
                _energy_kind(contract) == "GAZ" for contract in cc.contrats
            ):
                self.select_account(
                    source_partner.ref_bp, source_cc.numero_cc, source_partner.id_personne
                )
                source_postal = source_cc.adresse.cp if source_cc.adresse else ""
                source_history = self._history_with_fallback(
                    "GAZ",
                    ref_bp=source_partner.ref_bp,
                    compte_contrat=source_cc.numero_cc,
                    code_postal=source_postal,
                )
                for index, contract in enumerate(cc.contrats):
                    if _energy_kind(contract) == "GAZ":
                        prices[index] = self._price_for_contract(
                            contract,
                            eco={},
                            estimation_elec={},
                            estimation_gaz={},
                            histo={"GAZ": source_history},
                        )
                        prices[index].notes = (
                            prices[index].notes + "; "
                            "gas meter matched by configured PCE"
                        ).strip("; ")
        return prices

    def get_snapshot(
        self,
        account: AccountInfo | None = None,
        *,
        daily_days: int = 14,
    ) -> AccountSnapshot:
        """Prices, recent daily consumption, invoices, and outstanding balance."""
        if account is None:
            account = self.get_account()
        gas_source = self._gas_source_for_account(account)
        contracts: list[ContractSnapshot] = []
        billing: list[BillingSnapshot] = []
        for partner in account.partners:
            for cc in partner.comptes_contrat:
                prices = self._prices_for_compte(
                    partner.ref_bp, partner.id_personne, cc,
                    gas_source=gas_source,
                )
                code_postal = cc.adresse.cp if cc.adresse else ""
                kinds = {_energy_kind(contract) for contract in cc.contrats}
                daily_by_energy: dict[str, list[ConsumptionPoint]] = {}
                for energy in ("ELEC", "GAZ"):
                    if energy not in kinds:
                        continue
                    source_partner, source_cc = partner, cc
                    if energy == "GAZ" and gas_source is not None:
                        source_partner, source_cc = gas_source
                    self.select_account(
                        source_partner.ref_bp,
                        source_cc.numero_cc,
                        source_partner.id_personne,
                    )
                    source_postal = (
                        source_cc.adresse.cp if source_cc.adresse else ""
                    )
                    daily_by_energy[energy] = self.get_consumption_points(
                        energy,
                        granularity="day",
                        ref_bp=source_partner.ref_bp,
                        compte_contrat=source_cc.numero_cc,
                        code_postal=source_postal,
                        days=daily_days,
                    )
                # Restore nominal account context for invoices and balances.
                self.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)
                invoices_payload = self._try_json(
                    lambda ref_bp=partner.ref_bp, numero=cc.numero_cc: self.get_invoices(
                        ref_bp, numero
                    )
                )
                invoices = _invoices_from_remote(invoices_payload)
                last_invoice = Invoice.from_remote(
                    invoices_payload.get("derniereFacture")
                    if isinstance(invoices_payload.get("derniereFacture"), dict)
                    else None
                ) or _latest_invoice(invoices)
                balance = AccountBalance.from_remote(
                    self._try_json(
                        lambda ref_bp=partner.ref_bp, numero=cc.numero_cc: self.get_balance(
                            ref_bp, numero
                        )
                    )
                )
                billing.append(
                    BillingSnapshot(
                        account_id=_account_id(partner.ref_bp, cc.numero_cc),
                        balance=balance,
                        last_invoice=last_invoice,
                        invoice_count=len(invoices),
                    )
                )
                for price in prices:
                    recent = daily_by_energy.get(price.energy) or []
                    contracts.append(
                        ContractSnapshot(
                            price=price,
                            last_day=_latest_consumption(recent, complete_only=True),
                            today=next(
                                (item for item in reversed(recent) if item.partial),
                                None,
                            ),
                            recent_days=recent,
                        )
                    )
        return AccountSnapshot(contracts=contracts, billing=billing)

    def _find_contract(
        self, contract_id: str, account: AccountInfo | None = None
    ) -> tuple[Any, CompteContrat, EnergyContract]:
        wanted = (contract_id or "").strip()
        if not wanted:
            raise ApiError(400, "missing contract id")
        if account is None:
            account = self.get_account()
        for partner in account.partners:
            for cc in partner.comptes_contrat:
                for contract in cc.contrats:
                    if contract.id == wanted:
                        return partner, cc, contract
        raise ApiError(404, f"unknown contract {wanted}")

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
            notes = notes or (
                "No gas kWh price returned; inspect gas API diagnostics for status and response shape"
                if energy == "GAZ"
                else "No electricity kWh price returned"
            )

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
        granularity: str = "month",
        start_ms: int | None = None,
        end_ms: int | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        if preferred_communicant is None:
            flags = (True, False)
        else:
            flags = (preferred_communicant, not preferred_communicant)
        payloads: list[dict[str, Any]] = []
        for communicant in flags:
            payload = self._try_json(
                lambda communicant=communicant: self.get_history(
                    energy,
                    granularity=granularity,
                    ref_bp=ref_bp,
                    compte_contrat=compte_contrat,
                    communicant=communicant,
                    code_postal=code_postal,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    days=days,
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

    def _record_gas_response(
        self,
        path: str,
        response: httpx.Response,
        params: dict[str, Any] | None,
    ) -> None:
        """Remember *only* technical metadata for the three gas endpoints."""
        endpoint = path.rsplit("/", 1)[-1]
        if endpoint not in _GAS_API_ENDPOINTS:
            return

        summary: dict[str, Any] = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "http_status": response.status_code,
            "content_type": (
                response.headers.get("content-type") or ""
            ).split(";", 1)[0][:64],
        }
        if params and "communicant" in params:
            summary["communicant"] = str(params["communicant"]).lower() == "true"

        if not response.content:
            summary["response_type"] = "empty"
        else:
            try:
                payload = response.json()
            except ValueError:
                summary["response_type"] = "invalid_json"
            else:
                summary["response_type"] = type(payload).__name__
                if isinstance(payload, dict):
                    summary["field_count"] = len(payload)
                    summary["recognized_fields"] = sorted(
                        key for key in payload if key in _GAS_SAFE_TOP_LEVEL_FIELDS
                    )
                    rows = payload.get("historiquesGaz")
                    summary["history_field_type"] = (
                        type(rows).__name__ if "historiquesGaz" in payload
                        else "absent"
                    )
                    if isinstance(rows, list):
                        summary["history_row_count"] = len(rows)
                        if rows and isinstance(rows[0], dict):
                            summary["recognized_row_fields"] = sorted(
                                key for key in rows[0] if key in _GAS_SAFE_ROW_FIELDS
                            )
                    if "data" in payload:
                        summary["data_field_type"] = type(payload["data"]).__name__

        attempts = self._gas_api_diagnostics.setdefault(endpoint, [])
        attempts.append(summary)
        del attempts[:-6]

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
        self._record_gas_response(path, response, params)
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
