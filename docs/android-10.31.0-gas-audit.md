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

## Deep dive: application call path (Android DEX)

The APK is a split XAPK. Decompiled method metadata and Dalvik call
references confirm the consumption flow below; these findings come from
the shipped bytecode and do not require user credentials.

### Selected contract and request context

- `ConsoRepositoryImpl.getHistoriqueConsommation` calls
  `LocalDataSource.getCurrentUser`, then resolves
  `User.getRefBp()`, `User.getCc()` and
  `User.getSelectedCc().getAdresse().getCp()`.
- `User.getCc()` is implemented as
  `selectedCc.getNumeroCC()`, and `getRefBp()` uses the selected
  business partner. The user-selected account is therefore explicit
  in the Android request construction.
- The Android `HistoriqueConsoRequest` also stores
  `userType` (examples `GAZCO`, `GAZNC`, `DUALCO`),
  `ewePlus`, and `pasTemps` (`J`, `M`), but the final Retrofit
  gas-history interface does **not** transmit these extra fields as
  query parameters. Do **not** add them to the Python HTTP requests
  without more evidence.

### Date windows: strongest currently testable difference

- `GetHistoriqueUseCase.invoke` constructs a calendar period using
  `atStartOfDay`, `minusMonths` and `minusYears`; the separate
  `GetHistoriqueSemaineUseCase` begins with
  `LocalDateTime.now().atStartOfDay().minusDays(7)`.
- `DateUtilsKt.atStartOfDay` calls
  `LocalDate.atStartOfDay()`.
- `DateUtilsKt.toEpochMillisecond$default` uses
  `ZoneId.systemDefault()`; `toEpochMillisecond` converts the
  resulting zoned local datetime to epoch seconds × 1000.
- By contrast, the Python `_range_ms` currently computes a rolling
  interval based on `datetime.now(timezone.utc)`, preserving the
  current time of day. It can produce different `startDate` /
  `endDate` boundaries and offsets, especially around DST.
- **Hypothesis only:** the gas backend may reject an unaligned
  rolling interval with HTTP 204. Verify by comparing identical
  contract/cookie context with (A) current UTC rolling 60-day range
  and (B) exact calendar-month start/end in Europe/Paris.
  A suggested example is
  `2026-10-01 00:00 Europe/Paris` to
  `2026-11-01 00:00 Europe/Paris`, converted to milliseconds.
  Use a small number of GETs; this is **not** yet a proven fix.

### Cookies and common HTTP headers

- `EngieCookieJar.loadForRequest` dynamically constructs the
  `Cel_Cc` and `Cel_Bp` cookies from `User.selectedCc` and
  `User.selectedBp`; it also contributes `idPersonne`,
  `ClientIDCookie`, `access_token`, and
  `secureAPI*` cookies. It does so per outgoing request.
- `AuthInterceptor.addParams` sets `Authorization: Bearer`,
  `Accept: */*`, `Content-Type: application/json`,
  a device/version-based `User-Agent`,
  `X-Correlation-ID`, and `demandeur: MOBILE_ANDROID`
  for these endpoints. The Python implementation sends the same
  names; the Android device portion of the user agent may differ.
- `RemoteCourbe.toCacheControlString` uses
  `okhttp3.CacheControl.FORCE_NETWORK` for its opt-in
  no-cache mode.
- Live, redacted HA probes found HTTP 204 with **and without**
  `Cache-Control: no-cache`, and HTTP 403/204 for
  `User-Agent` versions 10.28.0 **and** 10.31.0.
  The 403 payload had a single `status` key but did not match
  `SEC_KO_EXPIRATION` or `SEC_KO_AUTHORISATION`.
- **Next check:** compare *booleans only* for cookie-to-selected-account
  alignment and whether every declared cookie actually appears in
  the outgoing request's Cookie header, without exposing values.
  The fact that cookie **names** are present in the Python cookie jar
  is insufficient to prove request-level alignment or scope.

### Consumption versus predictions

- `HistoriqueConsoRemote` contains separate
  `historiquesGaz` and `previsionGaz` lists, and its
  `toEntity` method converts them separately.
- `ConsommationGazRemote` includes `prevConso`,
  `prevVolume`, `prevEuroHT`, `prevEuroTTC`
  and a conversion to `ConsommationGaz.isPrevision`.
- The app's displayed **estimated** daily value could therefore
  originate from forecast data within a *successful history response*.
  The bytecode does **not** prove which specific response populated
  the user's screenshot; no authenticated mobile runtime traffic
  was captured.
- `RemoteCourbe.getHistoGazJours/Mois` explicitly maps
  HTTP 204 to `HistoriqueConsommation.NO_CONTENT`.
  A 204 from the Python client is consequently an actual
  no-content response, not merely a missing JSON parser field.

### Ranked follow-up

1. Compare an aligned **Europe/Paris calendar** interval against the
   Python rolling UTC interval, keeping every other field identical.
2. Compare selected-account/cookie-domain consistency using redacted
   booleans only; verify actual request cookie presence, not merely jar
   contents.
3. If 204 remains, trace the app's full authorization/consent and
   account-selection flow; compare required auxiliary calls without
   transmitting secrets.
4. Do not change contract sensor entity IDs or import forecast data
   into definitive Home Assistant energy statistics.

