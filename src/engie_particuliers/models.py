"""Domain models reverse-engineered from com.engie.particuliers v10.28.0."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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
