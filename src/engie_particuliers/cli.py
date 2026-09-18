#!/usr/bin/env python3
"""CLI for ENGIE Particuliers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from engie_particuliers.client import (
    EngieClient,
    factor_destination,
    factor_needs_challenge,
)
from engie_particuliers.exceptions import ApiError, EngieError, MfaRequiredError
from engie_particuliers.models import PARIS, SessionInfo, parse_engie_datetime

DEFAULT_TOKEN_FILE = ".token"


def _token_path(args: argparse.Namespace) -> Path:
    return Path(args.token_file or os.environ.get("ENGIE_TOKEN_FILE") or DEFAULT_TOKEN_FILE)


def _load_session(path: Path) -> SessionInfo | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("access_token"):
        return None
    return SessionInfo.from_dict(payload)


def _save_session(path: Path, session: SessionInfo) -> None:
    path.write_text(json.dumps(session.to_dict(), indent=2) + "\n")


def _restore(client: EngieClient, session: SessionInfo) -> SessionInfo:
    return client.restore_session(
        session.access_token,
        session.refresh_token,
        id_token=session.id_token,
        expires_in=session.expires_in,
        scope=session.scope,
    )


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing environment variable: {name}")
    return value


def _dump(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))


def _history_meta(points: list[Any]) -> dict[str, Any]:
    today = datetime.now(PARIS).date()
    complete = [
        item
        for item in points
        if getattr(item, "kwh", None) is not None and not getattr(item, "partial", False)
    ]
    last = complete[-1] if complete else None
    last_day = None
    lag_days = None
    if last is not None:
        parsed = parse_engie_datetime(getattr(last, "start", None))
        if parsed is not None:
            last_day = parsed.astimezone(PARIS).date().isoformat()
            lag_days = (today - parsed.astimezone(PARIS).date()).days
    return {
        "as_of": today.isoformat(),
        "last_complete_day": last_day,
        "lag_days": lag_days,
        "note": (
            "ENGIE daily histo is Linky D+1 (half-hourly often D+2); "
            "the current civil day is not published yet."
            if lag_days
            else None
        ),
    }


def _format_factor(index: int, factor: dict[str, Any]) -> str:
    kind = str(factor.get("factorType") or "?")
    dest = factor_destination(factor)
    fid = str(factor.get("id") or "")
    extra = f" → {dest}" if dest else ""
    return f"  {index}. {kind}{extra} ({fid})"


def _pick_factor(factors: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    if not factors:
        raise SystemExit("MFA required but Okta returned no factors")
    factor_id = (args.mfa_factor_id or "").strip()
    if factor_id:
        for item in factors:
            if str(item.get("id") or "") == factor_id:
                return item
        raise SystemExit(f"Unknown MFA factor id: {factor_id}")
    mfa_type = (args.mfa_type or "").strip().lower()
    if mfa_type:
        for item in factors:
            if str(item.get("factorType") or "").lower() == mfa_type:
                return item
        available = ", ".join(str(item.get("factorType") or "?") for item in factors)
        raise SystemExit(f"No MFA factor of type {mfa_type!r} (available: {available})")
    if len(factors) == 1 or not sys.stdin.isatty():
        return factors[0]
    print("MFA required. Choose a factor:", file=sys.stderr)
    for index, item in enumerate(factors, start=1):
        print(_format_factor(index, item), file=sys.stderr)
    raw = input("Factor [1]: ").strip() or "1"
    try:
        chosen = int(raw)
    except ValueError:
        raise SystemExit(f"Invalid factor choice: {raw}") from None
    if chosen < 1 or chosen > len(factors):
        raise SystemExit(f"Invalid factor choice: {raw}")
    return factors[chosen - 1]


def _login(client: EngieClient, args: argparse.Namespace):
    try:
        return client.login()
    except MfaRequiredError as exc:
        factor = _pick_factor(exc.factors, args)
        factor_id = str(factor.get("id") or "")
        state_token = exc.state_token
        if factor_needs_challenge(factor):
            pending = client.challenge_mfa(factor_id=factor_id, state_token=state_token)
            state_token = str(pending.get("state_token") or state_token)
            kind = str(factor.get("factorType") or "otp").upper()
            dest = factor_destination(factor)
            where = f" to {dest}" if dest else ""
            print(f"{kind} code sent{where}.", file=sys.stderr)
        code = args.mfa_code
        if not code:
            if not sys.stdin.isatty():
                raise
            code = input("MFA code: ").strip()
        if not code or not factor_id:
            raise
        return client.login(
            mfa_code=code,
            mfa_factor_id=factor_id,
            state_token=state_token,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ENGIE Particuliers CLI")
    parser.add_argument(
        "--username",
        default=os.environ.get("ENGIE_USERNAME", ""),
        help="Email (or ENGIE_USERNAME)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("ENGIE_PASSWORD", ""),
        help="Password (or ENGIE_PASSWORD)",
    )
    parser.add_argument("--mfa-code", default="", help="MFA / OTP code if Okta requires it")
    parser.add_argument("--mfa-factor-id", default="", help="Okta factor id (optional)")
    parser.add_argument(
        "--mfa-type",
        default="",
        help="MFA channel: sms or email (sends the code before prompting)",
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get("ENGIE_TOKEN_FILE", DEFAULT_TOKEN_FILE),
        help="Session JSON from `login` (default: .token)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="Authenticate and print session tokens")
    sub.add_parser("account", help="Fetch account / business-partner info")
    contracts = sub.add_parser("contracts", help="Fetch contracts for a compte contrat")
    contracts.add_argument("--ref-bp", default="", help="Business partner id (default: first from account)")
    contracts.add_argument(
        "--compte-contrat",
        default="",
        help="Compte contrat number (default: first from account)",
    )
    sub.add_parser("identifiant", help="Fetch Okta identifiant payload")
    sub.add_parser("prices", help="Current €/kWh TTC for each energy contract")
    sub.add_parser("snapshot", help="Prices, recent daily consumption, and billing")
    history = sub.add_parser("history", help="Consumption history for one contract")
    history.add_argument("--energy", default="elec", help="elec or gaz (ignored with --contract-id)")
    history.add_argument(
        "--granularity",
        default="day",
        choices=["hour", "day", "month"],
        help="histo grain (hour is electricity only)",
    )
    history.add_argument("--days", type=int, default=30, help="Lookback window")
    history.add_argument("--contract-id", default="", help="Energy contract id (default: first)")
    sub.add_parser("invoices", help="Invoice list for the primary compte contrat")
    sub.add_parser("balance", help="Outstanding balance / next invoice date")

    args = parser.parse_args(argv)
    token_path = _token_path(args)
    saved = None if args.command == "login" else _load_session(token_path)
    username = args.username or os.environ.get("ENGIE_USERNAME", "").strip()
    password = args.password or os.environ.get("ENGIE_PASSWORD", "").strip()
    if saved is None:
        username = username or _env("ENGIE_USERNAME")
        password = password or _env("ENGIE_PASSWORD")

    try:
        with EngieClient(username, password) as client:
            if saved is not None:
                try:
                    _restore(client, saved)
                except ApiError as exc:
                    if exc.status_code not in {401, 403}:
                        raise
                    print(
                        f"Saved session rejected ({exc}); logging in again.",
                        file=sys.stderr,
                    )
                    saved = None
                    username = username or _env("ENGIE_USERNAME")
                    password = password or _env("ENGIE_PASSWORD")
                    client.set_credentials(username, password)
            if saved is None:
                session = _login(client, args)
                _save_session(token_path, session)
                if args.command == "login":
                    _dump(session.to_dict())
                    return 0
            if args.command == "account":
                _dump(client.get_account().to_dict())
            elif args.command == "contracts":
                account = client.get_account()
                partner = account.primary
                if partner is None:
                    raise SystemExit("No business partner on this account")
                ref_bp = args.ref_bp or partner.ref_bp
                compte = args.compte_contrat
                if not compte:
                    if not partner.comptes_contrat:
                        raise SystemExit("No compte contrat on this account")
                    compte = partner.comptes_contrat[0].numero_cc
                _dump(client.get_contracts(ref_bp, compte))
            elif args.command == "identifiant":
                _dump(client.get_identifiant())
            elif args.command == "prices":
                prices = client.get_prices()
                _dump({"contracts": [item.to_dict() for item in prices]})
            elif args.command == "snapshot":
                _dump(client.get_snapshot().to_dict())
            elif args.command == "history":
                account = client.get_account()
                partner = account.primary
                if partner is None or not partner.comptes_contrat:
                    raise SystemExit("No compte contrat on this account")
                cc = partner.comptes_contrat[0]
                code_postal = cc.adresse.cp if cc.adresse else ""
                contract_id = (args.contract_id or "").strip()
                if contract_id:
                    points = client.get_consumption_points_for_contract(
                        contract_id,
                        granularity=args.granularity,
                        days=args.days,
                        account=account,
                    )
                else:
                    energy = "GAZ" if str(args.energy).lower().startswith("g") else "ELEC"
                    client.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)
                    points = client.get_consumption_points(
                        energy,
                        granularity=args.granularity,
                        ref_bp=partner.ref_bp,
                        compte_contrat=cc.numero_cc,
                        code_postal=code_postal,
                        days=args.days,
                    )
                payload = {
                    "count": len(points),
                    "points": [item.to_dict() for item in points],
                }
                if str(args.granularity) == "day":
                    payload.update(_history_meta(points))
                _dump(payload)
            elif args.command == "invoices":
                account = client.get_account()
                partner = account.primary
                if partner is None or not partner.comptes_contrat:
                    raise SystemExit("No compte contrat on this account")
                cc = partner.comptes_contrat[0]
                client.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)
                _dump(client.get_invoices(partner.ref_bp, cc.numero_cc))
            elif args.command == "balance":
                account = client.get_account()
                partner = account.primary
                if partner is None or not partner.comptes_contrat:
                    raise SystemExit("No compte contrat on this account")
                cc = partner.comptes_contrat[0]
                client.select_account(partner.ref_bp, cc.numero_cc, partner.id_personne)
                _dump(client.get_balance(partner.ref_bp, cc.numero_cc))
    except EngieError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
