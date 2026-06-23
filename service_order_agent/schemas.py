from pydantic import BaseModel, Field, AliasChoices
from typing import Optional

class IncidentDetails(BaseModel):
    incident_number: str = Field(validation_alias=AliasChoices('incident_number', 'Incident Number'))
    part_number: str = Field(validation_alias=AliasChoices('part_number', 'Part Number'))
    submitter: str
    category: str
    description: str
    date: str
    part_replacement_cost: float = Field(validation_alias=AliasChoices('part_replacement_cost', 'Part Replacement Cost', 'partReplacementCost'))

class RiskAssessment(BaseModel):
    risk_factors: str = Field(description="Analysis of any risk factors associated with this incident.")
    recommendation: str = Field(description="Recommendation for approval or rejection based on risk factors.")

class ServiceOrderResult(BaseModel):
    service_order: Optional[str] = None
    outcome: str
    incident_number: str
