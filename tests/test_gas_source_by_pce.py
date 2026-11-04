"""Regression: ENGIE can return gas data via an electricity-listed account.

These tests use synthetic account identifiers and httpx.MockTransport only.
No live ENGIE session and no actual consumer PCE are used.
"""

from __future__ import annotations

import hashlib

import httpx

from engie_particuliers.client import EngieClient


ACTIVE_TEST_PCE = "77777777777777"
OLD_TEST_PCE = "11111111111111"


def _client(*, multiple_gas: bool = False):
    observed: list[tuple[str, str]] = []
    gas_contracts = [{"typeEnergie": "GAZ", "id": "GAS_CONTRACT", "libelle": "Gas contract"}]
    if multiple_gas:
        gas_contracts.append({"typeEnergie": "GAZ", "id": "SECOND_GAS", "libelle": "Other gas"})

    def handler(request):
        path = request.url.path
        cc = request.url.params.get("compteContrat", "")
        observed.append((path, cc))

        if path.endswith("/getInfoUsers"):
            return httpx.Response(200, json={"infosUsers": [
                {"refBP": "TEST_BP", "idPersonne": "TEST_PERSON", "listeCC": [
                    {"numeroCC": "ACTIVE_METER", "adresse": {"cp": "75000"},
                     "listeContrat": [{
                         "id": "ELECTRIC_CONTRACT", "typeEnergie": "ELEC",
                         "libelle": "Electricity contract",
                     }]},
                    {"numeroCC": "OLD_GAS_METER", "adresse": {"cp": "75000"},
                     "listeContrat": gas_contracts},
                ]}
            ]})
        if path.endswith("/recupererDonneesReleveV2"):
            return httpx.Response(200, json={
                "contratGaz": {
                    "idPDL": ACTIVE_TEST_PCE if cc == "ACTIVE_METER" else OLD_TEST_PCE,
                    "compteurCommuniquant": True,
                }
            })
        if path.endswith("/histoGazMois"):
            if cc == "ACTIVE_METER":
                return httpx.Response(200, json={"historiquesGaz": [{
                    "conso": 100, "consoEuroTTC": 12.5,
                    "consoEuroHT": 10.0,
                    "abonnementTTC": 6.0, "dateFin": "2026-10-31",
                }]})
            return httpx.Response(204)
        if path.endswith("/histoGazJours"):
            if cc == "ACTIVE_METER":
                return httpx.Response(200, json={"historiquesGaz": [{
                    "conso": 10, "consoEuroTTC": 1.25, "valeurDate": "2026-10-30"
                }]})
            return httpx.Response(204)
        if path.endswith("/ecoGestesDynamiques"):
            return httpx.Response(200, json={"prixKwh": 0.1906})
        if path.endswith("/histoElecMois") or path.endswith("/histoElecJours"):
            return httpx.Response(200, json={"historiquesElec": [{
                "conso": 20, "consoEuroTTC": 4.0, "valeurDate": "2026-10-30"
            }]})
        if path.endswith("/estimationConsoGazMois"):
            return httpx.Response(403, json={"status": "no_offer"})
        if path.endswith("/estimationConsoElecMois"):
            return httpx.Response(204)
        if path.endswith("/listeFacture") or path.endswith("/soldeApayerV2"):
            # Key safety regression: do not request bills using the
            # unrelated account's cookies after the gas source switch.
            assert request.headers.get("Cookie", "").find("Cel_Cc=" + cc) >= 0
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    client = EngieClient("synthetic@example.invalid", "NOT_A_REAL_PASSWORD")
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client._access_token = "SYNTHETIC_TOKEN"
    return client, observed


def test_pce_source_returns_gas_readings_and_preserves_contract_ids():
    client, observed = _client()
    try:
        client.set_gas_pce_sha256(
            hashlib.sha256(ACTIVE_TEST_PCE.encode("ascii")).hexdigest()
        )
        snapshot = client.get_snapshot(daily_days=14)
        assert len(snapshot.contracts) == 2
        gas = next(c for c in snapshot.contracts if c.price.energy == "GAZ")
        elec = next(c for c in snapshot.contracts if c.price.energy == "ELEC")
        assert gas.price.contract_id == "GAS_CONTRACT"
        assert elec.price.contract_id == "ELECTRIC_CONTRACT"
        assert gas.price.price_kwh_ttc == 0.125
        assert gas.price.abonnement_ttc == 6.0
        assert gas.price.source == "histoGazMois"
        assert len(gas.recent_days) == 1
        assert gas.recent_days[0].kwh == 10
        assert ("gas" in gas.price.notes.lower())
        points = client.get_consumption_points_for_contract(
            "GAS_CONTRACT", granularity="day", days=14
        )
        assert len(points) == 1
        assert points[0].kwh == 10
        assert any(path.endswith("/histoGazJours") and cc == "ACTIVE_METER"
                   for path, cc in observed)
        assert any(path.endswith("/histoGazJours") and cc == "OLD_GAS_METER"
                   for path, cc in observed)
    finally:
        client.close()


def test_without_pce_configuration_behavior_stays_unchanged():
    client, _ = _client()
    try:
        snapshot = client.get_snapshot()
        gas = next(c for c in snapshot.contracts if c.price.energy == "GAZ")
        assert gas.price.price_kwh_ttc is None
        assert not gas.recent_days
        assert gas.price.contract_id == "GAS_CONTRACT"
    finally:
        client.close()


def test_multiple_gas_contracts_disables_automatic_remapping():
    client, _ = _client(multiple_gas=True)
    try:
        client.set_gas_pce_sha256(
            hashlib.sha256(ACTIVE_TEST_PCE.encode()).hexdigest()
        )
        snapshot = client.get_snapshot()
        gas = [c for c in snapshot.contracts if c.price.energy == "GAZ"]
        assert len(gas) == 2
        assert all(c.price.price_kwh_ttc is None for c in gas)
        assert all(not c.recent_days for c in gas)
    finally:
        client.close()


def test_invalid_digest_rejected():
    client, _ = _client()
    try:
        import pytest
        with pytest.raises(ValueError):
            client.set_gas_pce_sha256("invalid")
    finally:
        client.close()
