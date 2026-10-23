from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field


class ServiceInfo(BaseModel):
    id: str
    name: str
    duration_minutes: int = Field(gt=0)


class Slot(BaseModel):
    start: datetime
    end: datetime


class BookingProposal(BaseModel):
    id: str
    tenant_id: str
    conversation_id: str
    service_id: str
    start: datetime
    end: datetime
    customer_name: str
    customer_email: str | None = None
    expires_at: datetime
    status: Literal["pending", "confirmed", "expired", "conflict"] = "pending"


class ChatRequest(BaseModel):
    tenant_id: str = "demo-business"  # DEVELOPMENT ONLY: NOT authenticated
    conversation_id: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=5000)


class ChatReply(BaseModel):
    response: str
    conversation_id: str


class ConfirmRequest(BaseModel):
    tenant_id: str
    conversation_id: str
    proposal_id: str


class ConfirmReply(BaseModel):
    booking_id: str
    provider: str
    status: Literal["confirmed"]
