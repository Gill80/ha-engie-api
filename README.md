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
git tag v0.2.0
git push origin v0.2.0
```

The [publish](.github/workflows/publish.yml) workflow builds the wheel and sdist, creates a GitHub Release with those artifacts, and uploads them to PyPI.

## Limitations

- Unofficial API — may break on app updates
- Some accounts require Okta MFA
- `estimationConsoElecMois` / `estimationConsoGazMois` (per-cadran HP/HC) can return 403; electricity then uses `ecoGestesDynamiques.prixKwh`, gas uses latest billed month `consoEuroTTC / conso` (variable, excluding subscription)
- Token refresh re-issues the client-id cookie
- Use at your own risk; respect ENGIE ToS
