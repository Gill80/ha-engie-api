"""Domain models reverse-engineered from com.engie.particuliers v10.28.0."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")


def _as_dict(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [_as_dict(item) for item in value]
    return value


@dataclass
class SessionInfo:
    access_token: str
    refresh_token: str
    id_token: str = ""
    token_type: str = "Bearer"
    expires_in: int = 0
    scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionInfo:
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            id_token=str(data.get("id_token") or ""),
            token_type=str(data.get("token_type") or "Bearer"),
            expires_in=int(data.get("expires_in") or 0),
            scope=str(data.get("scope") or ""),
        )


@dataclass
class Address:
    n_voie: str = ""
    libelle_voie: str = ""
    cp: str = ""
    ville: str = ""
    complement: str = ""

    @classmethod
    def from_remote(cls, data: dict[str, Any] | None) -> Address | None:
        if not data:
            return None
        return cls(
            n_voie=data.get("nVoie") or "",
            libelle_voie=data.get("libelleVoie") or "",
            cp=data.get("cp") or "",
            ville=data.get("ville") or "",
            complement=data.get("complementAdresse") or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnergyContract:
    id: str = ""
    id_offre: str = ""
    type: str = ""
    type_energie: str = ""
    statut: str = ""
    etat: str = ""
    libelle: str = ""
    puissance: str = ""
    type_comptage: str = ""
    date_debut: str = ""
    date_fin: str = ""

    @classmethod
    def from_remote(cls, data: dict[str, Any]) -> EnergyContract:
        return cls(
            id=data.get("id") or "",
            id_offre=data.get("idOffre") or "",
            type=data.get("type") or "",
            type_energie=data.get("typeEnergie") or "",
            statut=data.get("statut") or "",
            etat=data.get("etat") or "",
            libelle=data.get("libelle") or "",
            puissance=data.get("puissance") or "",
            type_comptage=data.get("typeComptage") or "",
            date_debut=data.get("dateDebut") or "",
            date_fin=data.get("datefin") or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompteContrat:
    numero_cc: str = ""
    statut: str = ""
    etat: str = ""
    libelle_etat: str = ""
    type_logement: str = ""
    type_tarif: str = ""
    adresse: Address | None = None
    contrats: list[EnergyContract] = field(default_factory=list)

    @classmethod
    def from_remote(cls, data: dict[str, Any]) -> CompteContrat:
        contrats = [
            EnergyContract.from_remote(item)
            for item in (data.get("listeContrat") or [])
            if isinstance(item, dict)
        ]
        return cls(
            numero_cc=data.get("numeroCC") or "",
            statut=data.get("statut") or "",
            etat=data.get("etat") or "",
            libelle_etat=data.get("libelleEtat") or "",
            type_logement=data.get("typeLogement") or "",
            type_tarif=data.get("typeTarif") or "",
            adresse=Address.from_remote(data.get("adresse") if isinstance(data.get("adresse"), dict) else None),
            contrats=contrats,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "numero_cc": self.numero_cc,
            "statut": self.statut,
            "etat": self.etat,
            "libelle_etat": self.libelle_etat,
            "type_logement": self.type_logement,
            "type_tarif": self.type_tarif,
            "adresse": _as_dict(self.adresse),
            "contrats": [_as_dict(item) for item in self.contrats],
        }


@dataclass
class BusinessPartner:
    ref_bp: str = ""
    id_personne: str = ""
    email: str = ""
    nom: str = ""
    prenom: str = ""
    civilite: str = ""
    segment_code: str = ""
    segment_libelle: str = ""
    comptes_contrat: list[CompteContrat] = field(default_factory=list)

    @classmethod
    def from_remote(cls, data: dict[str, Any]) -> BusinessPartner:
        comptes = [
            CompteContrat.from_remote(item)
            for item in (data.get("listeCC") or [])
            if isinstance(item, dict)
        ]
        return cls(
            ref_bp=data.get("refBP") or "",
            id_personne=data.get("idPersonne") or "",
            email=data.get("email") or "",
            nom=data.get("nom") or "",
            prenom=data.get("prenom") or "",
            civilite=data.get("civilite") or "",
            segment_code=data.get("segmentClientCode") or "",
            segment_libelle=data.get("segmentClientLibelle") or "",
            comptes_contrat=comptes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_bp": self.ref_bp,
            "id_personne": self.id_personne,
            "email": self.email,
            "nom": self.nom,
            "prenom": self.prenom,
            "civilite": self.civilite,
            "segment_code": self.segment_code,
            "segment_libelle": self.segment_libelle,
            "comptes_contrat": [_as_dict(item) for item in self.comptes_contrat],
        }


@dataclass
class AccountInfo:
    partners: list[BusinessPartner] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_remote(cls, data: dict[str, Any]) -> AccountInfo:
        partners = [
            BusinessPartner.from_remote(item)
            for item in (data.get("infosUsers") or [])
            if isinstance(item, dict)
        ]
        return cls(partners=partners, raw=data)

    @property
    def primary(self) -> BusinessPartner | None:
        return self.partners[0] if self.partners else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "partners": [_as_dict(item) for item in self.partners],
            "raw": self.raw,
        }


@dataclass
class TariffCadran:
    """One metering band (BASE / HP / HC) with a €/kWh rate."""

    cadran: str
    price_kwh_ttc: float | None = None
    price_kwh_ht: float | None = None
    as_of: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContractPrice:
    """Current kWh price for one energy contract."""

    contract_id: str
    libelle: str
    energy: str
    type_comptage: str = ""
    puissance: str = ""
    price_kwh_ttc: float | None = None
    price_kwh_ht: float | None = None
    cadrans: list[TariffCadran] = field(default_factory=list)
    as_of: str = ""
    source: str = ""
    unit: str = "EUR/kWh"
    abonnement_ttc: float | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "libelle": self.libelle,
            "energy": self.energy,
            "type_comptage": self.type_comptage,
            "puissance": self.puissance,
            "price_kwh_ttc": self.price_kwh_ttc,
            "price_kwh_ht": self.price_kwh_ht,
            "cadrans": [_as_dict(item) for item in self.cadrans],
            "as_of": self.as_of,
            "source": self.source,
            "unit": self.unit,
            "abonnement_ttc": self.abonnement_ttc,
            "notes": self.notes,
        }


def parse_engie_datetime(value: Any) -> datetime | None:
    """Parse ENGIE date fields (ISO, FR slash dates, or epoch ms)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000.0
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_engie_datetime(int(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo:
            return parsed
        # Date-only ISO ("2026-09-17") is a French civil day, not UTC midnight.
        if len(text) <= 10:
            return parsed.replace(tzinfo=PARIS)
        return parsed.replace(tzinfo=timezone.utc)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).replace(tzinfo=PARIS)
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=PARIS)
        except ValueError:
            continue
    return None


def civil_day_bounds(value: datetime) -> tuple[datetime, datetime]:
    """Paris midnight → next midnight for the calendar date ENGIE put on the row."""
    labeled = value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    start = datetime(labeled.year, labeled.month, labeled.day, tzinfo=PARIS)
    return start, start + timedelta(days=1)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


@dataclass
class ConsumptionPoint:
    """One histoElec/histoGaz row (hour, day, or month)."""

    energy: str
    granularity: str
    start: str = ""
    end: str = ""
    kwh: float | None = None
    cost_ttc: float | None = None
    cost_ht: float | None = None
    abonnement_ttc: float | None = None
    temperature: float | None = None
    partial: bool = False

    @classmethod
    def from_remote(
        cls, row: dict[str, Any], *, energy: str, granularity: str
    ) -> ConsumptionPoint:
        raw_start = parse_engie_datetime(row.get("dateDebut") or row.get("valeurDate"))
        raw_end = parse_engie_datetime(row.get("dateFin") or row.get("valeurDate"))
        if granularity == "day" and raw_start is not None:
            start, end = civil_day_bounds(raw_start)
        else:
            start, end = raw_start, raw_end
        return cls(
            energy=energy,
            granularity=granularity,
            start=_iso(start),
            end=_iso(end),
            kwh=_to_float(row.get("conso")),
            cost_ttc=_to_float(row.get("consoEuroTTC")),
            cost_ht=_to_float(row.get("consoEuroHT")),
            abonnement_ttc=_to_float(row.get("abonnementTTC")),
            temperature=_to_float(row.get("temperature")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Invoice:
    number: str = ""
    date: str = ""
    due_date: str = ""
    amount_ttc: float | None = None
    label: str = ""
    payment_status: str = ""

    @classmethod
    def from_remote(cls, data: dict[str, Any] | None) -> Invoice | None:
        if not isinstance(data, dict) or not data:
            return None
        number = data.get("numeroFacture")
        invoice = cls(
            number="" if number is None else str(number),
            date=str(data.get("dateFacture") or ""),
            due_date=str(data.get("dateEcheance") or ""),
            amount_ttc=_to_float(data.get("montantTTC")),
            label=str(data.get("libelle") or ""),
            payment_status=str(data.get("statutPaiement") or ""),
        )
        if not (
            invoice.number
            or invoice.date
            or invoice.due_date
            or invoice.amount_ttc is not None
        ):
            return None
        return invoice

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AccountBalance:
    solde: float | None = None
    solde_hors_ce: float | None = None
    solde_ce: float | None = None
    next_invoice_date: str = ""
    energie_coupee: bool | None = None
    recouvrement: bool | None = None
    paiement_cb: bool | None = None

    @classmethod
    def from_remote(cls, data: dict[str, Any] | None) -> AccountBalance:
        if not isinstance(data, dict):
            return cls()
        return cls(
            solde=_to_float(data.get("solde")),
            solde_hors_ce=_to_float(data.get("soldeHorsCE")),
            solde_ce=_to_float(data.get("soldeCE")),
            next_invoice_date=str(data.get("dateProchaineFacture") or ""),
            energie_coupee=_to_bool(data.get("energieCoupee")),
            recouvrement=_to_bool(data.get("recouvrement")),
            paiement_cb=_to_bool(data.get("paiementCB")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BillingSnapshot:
    """Invoices + outstanding balance for one compte contrat."""

    account_id: str
    balance: AccountBalance = field(default_factory=AccountBalance)
    last_invoice: Invoice | None = None
    invoice_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "balance": self.balance.to_dict(),
            "last_invoice": _as_dict(self.last_invoice),
            "invoice_count": self.invoice_count,
        }


@dataclass
class ContractSnapshot:
    """Current tariff plus the latest billed day for one energy contract."""

    price: ContractPrice
    last_day: ConsumptionPoint | None = None
    today: ConsumptionPoint | None = None
    recent_days: list[ConsumptionPoint] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": self.price.to_dict(),
            "last_day": _as_dict(self.last_day),
            "today": _as_dict(self.today),
            "recent_days": [_as_dict(item) for item in self.recent_days],
        }


@dataclass
class AccountSnapshot:
    """Coordinator payload: prices, recent consumption, and billing."""

    contracts: list[ContractSnapshot] = field(default_factory=list)
    billing: list[BillingSnapshot] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contracts": [_as_dict(item) for item in self.contracts],
            "billing": [_as_dict(item) for item in self.billing],
        }
