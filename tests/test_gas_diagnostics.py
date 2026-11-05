"""Regression tests for privacy-preserving ENGIE gas API diagnostics.

All requests use httpx.MockTransport; no ENGIE account or network access.
"""

from __future__ import annotations

import json

import httpx
import pytest

from engie_particuliers.client import EngieClient, _histo_rows
from engie_particuliers.exceptions import ApiError
from engie_particuliers.models import EnergyContract


def _client(handler):
    client = EngieClient("example@invalid.test", "secret-password")
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client._access_token = "sensitive-token"
    return client


def test_http_403_is_reported_without_leaking_response_or_identifiers():
    def handler(request):
        return httpx.Response(
            403,
            json={"message": "secret-error-body", "account": "private-account"},
        )

    client = _client(handler)
    try:
        with pytest.raises(ApiError) as caught:
            client.get_estimation_gaz("private-bp", "private-cc")
        assert caught.value.status_code == 403

        report = client.gas_api_diagnostics
        result = report["estimationConsoGazMois"][0]
        assert result["http_status"] == 403
        assert result["response_type"] == "dict"
        assert "message" in result["recognized_fields"]
        serialized = json.dumps(report)
        for secret in (
            "sensitive-token",
            "secret-password",
            "private-bp",
            "private-cc",
            "private-account",
            "secret-error-body",
        ):
            assert secret not in serialized
    finally:
        client.close()


def test_monthly_history_shape_counts_and_communicant_are_recorded():
    row = {
        "conso": 42.0,
        "consoEuroTTC": 3.14,
        "dateFin": "2025-01-31",
        "meter_id": "sensitive-meter-id",
    }

    def handler(request):
        assert request.url.path.endswith("/histoGazMois")
        return httpx.Response(200, json={"historiquesGaz": [row]})

    client = _client(handler)
    try:
        payload = client.get_history(
            "GAZ",
            granularity="month",
            ref_bp="private-bp",
            compte_contrat="private-cc",
            communicant=False,
            start_ms=1,
            end_ms=2,
        )
        assert len(_histo_rows(payload, "GAZ")) == 1
        report = client.gas_api_diagnostics["histoGazMois"][0]
        assert report["http_status"] == 200
        assert report["communicant"] is False
        assert report["history_row_count"] == 1
        assert report["recognized_row_fields"] == [
            "conso", "consoEuroTTC", "dateFin"
        ]
        assert "sensitive-meter-id" not in json.dumps(report)
    finally:
        client.close()


def test_unrecognized_nested_history_is_visible_without_response_values():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": {"historiquesGaz": [{"conso": 42.0}]},
                "private": "private-value",
            },
        )

    client = _client(handler)
    try:
        payload = client.get_history(
            "GAZ", granularity="day", ref_bp="bp", compte_contrat="cc",
            start_ms=1, end_ms=2,
        )
        # Current parser only accepts a top-level historiquesGaz array.
        assert _histo_rows(payload, "GAZ") == []
        report = client.gas_api_diagnostics["histoGazJours"][0]
        assert report["http_status"] == 200
        assert report["history_field_type"] == "absent"
        assert report["recognized_fields"] == ["data"]
        assert report["data_field_type"] == "dict"
        assert "private-value" not in json.dumps(report)
    finally:
        client.close()


def test_non_json_response_is_distinguished_from_403():
    client = _client(
        lambda req: httpx.Response(200, text="<html>private-data</html>")
    )
    try:
        with pytest.raises(ApiError):
            client.get_estimation_gaz("bp", "cc")
        report = client.gas_api_diagnostics["estimationConsoGazMois"][0]
        assert report["http_status"] == 200
        assert report["response_type"] == "invalid_json"
        assert "private-data" not in json.dumps(report)
    finally:
        client.close()


def test_diagnostics_keep_at_most_six_attempts_and_return_copies():
    client = _client(
        lambda req: httpx.Response(200, json={"prixKwh": 0.125})
    )
    try:
        for _ in range(8):
            assert client.get_estimation_gaz("bp", "cc")["prixKwh"] == 0.125
        report = client.gas_api_diagnostics
        assert len(report["estimationConsoGazMois"]) == 6
        report["estimationConsoGazMois"][0]["http_status"] = 999
        assert client.gas_api_diagnostics["estimationConsoGazMois"][0]["http_status"] == 200
    finally:
        client.close()


def test_missing_price_does_not_invent_http_status():
    client = EngieClient()
    price = client._price_for_contract(
        EnergyContract(id="gas", type="GAZ", libelle="Offre Gaz Passerelle"),
        eco={},
        estimation_elec={},
        estimation_gaz={},
        histo={"GAZ": {}},
    )
    assert price.price_kwh_ttc is None
    assert "403" not in price.notes
    assert "diagnostics" in price.notes

    electricity = client._price_for_contract(
        EnergyContract(id="electric", type="ELEC", libelle="Demo electricity"),
        eco={},
        estimation_elec={},
        estimation_gaz={},
        histo={"ELEC": {}},
    )
    assert electricity.price_kwh_ttc is None
    assert "electricity" in electricity.notes
    assert "gas" not in electricity.notes


def test_contract_details_use_v2_endpoint_from_engie_android_10_31():
    """Only verifies the URL shape; the request never reaches ENGIE."""
    observed = []

    def handler(request):
        observed.append(request.url.path)
        return httpx.Response(200, json={"contrats": []})

    client = _client(handler)
    try:
        result = client.get_contracts("synthetic-bp", "synthetic-cc")
        assert result == {"contrats": []}
        assert observed == [
            "/dvmobile-ws/api/private/espaceClient/v2/getInfoContrats"
        ]
    finally:
        client.close()

