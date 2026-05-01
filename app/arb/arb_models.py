from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ARBCaseInfo(BaseModel):
    account_number: str = ""
    property_owner: str = ""
    property_address: str = ""
    current_noticed_value: str = ""
    agent_requested_value: str = ""
    cad_proposed_value: str = ""
    property_type: str = ""
    tax_year: str = ""


class ARBPacketUpload(BaseModel):
    file_name: str
    file_base64: str


class ARBReviewRequest(BaseModel):
    cad_packet: ARBPacketUpload
    taxpayer_packet: ARBPacketUpload
    case_info: ARBCaseInfo = Field(default_factory=ARBCaseInfo)


class ARBPacketUpdateRequest(BaseModel):
    access_token: str
    cad_packet: ARBPacketUpload
    case_info: ARBCaseInfo = Field(default_factory=ARBCaseInfo)
    selected_sections: dict[str, str] = Field(default_factory=dict)
    rebuttal_argument: str = ""
    hearing_prep: list[str] = Field(default_factory=list)
    copy_ready_rebuttal: str = ""


class ARBParsedPacket(BaseModel):
    file_name: str
    packet_label: str
    text: str = ""
    pages: list[dict[str, Any]] = Field(default_factory=list)
    extraction_provider: str = "none"
    warnings: list[str] = Field(default_factory=list)


class ARBReviewSummary(BaseModel):
    cad_summary: str = ""
    taxpayer_summary: str = ""
    cad_strong_points: list[str] = Field(default_factory=list)
    cad_weak_points: list[str] = Field(default_factory=list)
    taxpayer_strong_points: list[str] = Field(default_factory=list)
    taxpayer_weak_points: list[str] = Field(default_factory=list)
    rebuttal_points: list[str] = Field(default_factory=list)
    suggested_value: str = ""
    settlement_range: str = ""
    missing_evidence: list[str] = Field(default_factory=list)
    hearing_strategy: list[str] = Field(default_factory=list)
    final_recommendation: str = ""
    analysis_status: str = "fallback"
    warnings: list[str] = Field(default_factory=list)
