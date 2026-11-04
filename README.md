# ha-engie-api

Python client for **ENGIE Particuliers** (`com.engie.particuliers` v10.28.0), reverse-engineered from the Android APK.

There is no official public residential API — this project talks to the same Okta + DGP backends as the mobile app.

## API overview

| Item | Value |
|------|-------|
| Identity | `identite-prd.engie.fr` (Okta AuthN) |
| OAuth | `api.dgp.engie.fr/oauth/v1` (PKCE, public client) |
| REST | `particuliers.engie.fr` (TLS) |
| Auth | email/password → sessionToken → authorization code → JWT |
| Account | `GET /dvmobile-ws/api/private/espaceClient/v2/getInfoUsers` |
| Prices | `GET /dvmobile-ws/api/private/maConso/v2/ecoGestesDynamiques` (`prixKwh`) + monthly histo |
| History | `histoElecJours` / `histoGazJours` / `histoElecHeures` |
| Billing | `listeFacture` + `soldeApayerV2` |

### Login sequence

1. `POST https://identite-prd.engie.fr/api/v1/authn` with email + password
2. On `SUCCESS`, take `sessionToken`
3. PKCE `GET https://api.dgp.engie.fr/oauth/v1/authorize?...&sessionToken=...`
4. Exchange `code` at `POST https://api.dgp.engie.fr/oauth/v1/token`
5. `POST /digital-common-ws/api-management/creation-cookie` with Bearer **and** `access_token` cookie
6. `GET /dvmobile-ws/api/private/espaceClient/v2/getInfoUsers`

If AuthN returns `MFA_REQUIRED`, the CLI **sends** the SMS/email (Okta challenge) then asks for the code. Use `--mfa-type sms` or `--mfa-type email` to pick the channel. TOTP factors do not need a send step.

## Setup

```bash
cd ha-engie-api
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

### Python

```python
from engie_particuliers import EngieClient

with EngieClient("you@email.com", "your_password") as client:
    client.login()
    account = client.get_account()
    print(account.primary.ref_bp if account.primary else None)
    print(account.to_dict())
    for price in client.get_prices(account):
        print(price.libelle, price.price_kwh_ttc, price.unit)
    snapshot = client.get_snapshot(account)
    for contract in snapshot.contracts:
        last = contract.last_day
        print(contract.price.libelle, last.kwh if last else None)
```

### CLI

```bash
export ENGIE_USERNAME="you@email.com"
export ENGIE_PASSWORD="..."

engie-particuliers login          # writes .token
engie-particuliers account        # reuses .token, no MFA
engie-particuliers contracts
engie-particuliers identifiant
engie-particuliers prices         # €/kWh TTC per contract
engie-particuliers snapshot       # prices + last days + billing
engie-particuliers history --energy elec --granularity day --days 30
engie-particuliers invoices
engie-particuliers balance
```

`login` saves the session to `.token` (or `ENGIE_TOKEN_FILE`). Later commands restore it and skip password/MFA until the token expires.

## Project layout

```
ha-engie-api/
├── src/engie_particuliers/   # Python package
│   ├── client.py             # Okta + DGP + REST client
│   ├── models.py             # Account / contract dataclasses
│   └── cli.py                # Command-line interface
├── scripts/
│   └── extract_apk_urls.py   # Static APK string extraction
└── reverse/                  # RE notes & XAPK metadata
```

## Reverse engineering

OAuth and REST details were extracted from decompiled Java in:

- `com.engie.particuliers.data.remote.OktaManager`
- `com.engie.particuliers.data.remote.DvServiceEngie`
- `com.engie.particuliers.data.remote.interceptors.AuthInterceptor`
- `res/raw/config_prod.json`
- `com.engie.particuliers.presentation.debug.ProdEnvironment`

The full decompiled APK can stay in a sibling `ha/` workspace folder and is gitignored here.

```bash
./scripts/extract_apk_urls.py /path/to/com.engie.particuliers.apk
```

See `reverse/NOTES.md` for the RE summary.

## Releasing

Bump `version` in `pyproject.toml`, then push a matching tag:

```bash
git tag v0.2.1
git push origin v0.2.1
```

The [publish](.github/workflows/publish.yml) workflow builds the wheel and sdist, creates a GitHub Release with those artifacts, and uploads them to PyPI.

## Troubleshooting gas responses (Gill80 fork)

The gas endpoints may return an HTTP error, an empty response, or a valid
response whose structure is not recognized by the current parser. The
original client swallowed API errors and sometimes displayed a misleading
hard-coded `403` message. This fork records a **bounded, in-memory**
technical summary for three endpoints:

- `estimationConsoGazMois` — monthly gas price estimation
- `histoGazMois` — monthly gas consumption and costs
- `histoGazJours` — daily gas consumption

After running a snapshot, inspect `client.gas_api_diagnostics`. Each
endpoint contains at most six recent attempts with the HTTP code,
response format, recognized field names, number of history rows, and
the `communicant` boolean when applicable. A response with a nested
`data` object is currently **diagnosed**, not automatically decoded;
we should only change the parser after confirming the actual format.

The summaries contain **no request URLs, identifiers, credentials,
tokens, cookies, complete JSON responses, individual consumption
values, or invoice amounts**. Do not enable HTTP debug/trace logging,
which may reveal authentication material. Diagnostic records are lost
when the client restarts.

Tests use synthetic fixture data only and never contact ENGIE:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

**This branch alone does not change an existing Home Assistant
installation.** Deploying it requires a separate, deliberate packaging
and integration test. The Home Assistant fork can show these summaries
in its diagnostics once it is connected to this fork of the library.

## Limitations

- Daily `histoElecJours` / `histoGazJours` is Linky **D+1** (yesterday). Half-hourly `histoElecHeures` is often **D+2**. The current civil day is not in those APIs; a partial today row is added only if hourly slots exist for today.
- Some accounts require Okta MFA
- `estimationConsoElecMois` / `estimationConsoGazMois` (per-cadran HP/HC) can return 403; electricity then uses `ecoGestesDynamiques.prixKwh`, gas uses latest billed month `consoEuroTTC / conso` (variable, excluding subscription)
- Token refresh re-issues the client-id cookie
- Use at your own risk; respect ENGIE ToS
