# ENGIE Particuliers Android 10.31.0 — gas API static comparison

Static analysis conducted on an XAPK supplied by the user. Package name
`com.engie.particuliers` and version `10.31.0` were read from the compiled
Android manifest. The Retrofit interface
`com.engie.particuliers.data.remote.DvServiceEngie` declares the following:

| Android method | HTTP method | Path | Query parameters | Optional header |
|---|---|---|---|---|
| `getHistoGazJours` | GET | `/dvmobile-ws/api/private/maConso/v3/histoGazJours` | `refBP`, `compteContrat`, `codePostal`, `startDate`, `endDate` | `Cache-Control` |
| `getHistoGazMois` | GET | `/dvmobile-ws/api/private/maConso/v7/histoGazMois` | `communicant`, `refBP`, `compteContrat`, `codePostal`, `startDate`, `endDate` | `Cache-Control` |
| `getEstimationConsoGazMois` | GET | `/dvmobile-ws/api/private/maConso/v2/estimationConsoGazMois` | `refBP`, `compteContrat` | — |
| `getInfoContrats` | GET | `/dvmobile-ws/api/private/espaceClient/v2/getInfoContrats` | `refBP`, `compteContrat` | `Cache-Control` |

**Findings:**

- All three gas consumption/estimation routes are **unchanged** compared with
  the forked Python client, and their Retrofit query parameter names match.
- The Retrofit history methods accept a nullable `Cache-Control` header;
  it is not possible to prove the value used by a specific runtime request
  from the method declaration alone. The Python client does not set this
  per-request header.
- A real difference was found for contract details: Android declares
  `/espaceClient/v2/getInfoContrats`, whereas the Python client previously
  used `/espaceClient/getInfoContrats`. The Python code was aligned and
  regression tested. The normal `get_snapshot` path does not currently
  invoke `get_contracts`, so this is **not yet a fix for gas data**.
- The current Home Assistant diagnostic snapshot showed a real HTTP 403 from
  `estimationConsoGazMois` and HTTP 204 from the history endpoints,
  even though gas consumption appears in the official mobile app. Those
  responses do not prove a missing consent, wrong account, or stale
  cache by themselves.

**Next controlled investigation (read-only, account-data-safe):**

1. Report whether the gas `refBP` / `compteContrat` pair passed to each
   endpoint belongs to an account listing a gas contract; export **booleans
   and counts only**, never account identifiers.
2. Report whether `codePostal` is present and the actual interval duration
   in days; do not include address or date of the user's actual readings.
3. Compare a **bounded** request with the optional `Cache-Control: no-cache`
   header and the default request, one or two requests at most; do not
   log response bodies or sensitive request headers.
4. If the endpoint still rejects/returns empty data, examine the exact app
   cookie/session bootstrap and contract selection flow.
5. Distinguish **estimated** mobile-app daily consumption from definitive
   metered readings. Do not silently combine them when importing statistics.

This investigation uses static code, not a live proxy trace of the Android app;
therefore it must not claim that these declared methods represent the precise
headers, cookie values or user-selected account of an observed runtime request.
