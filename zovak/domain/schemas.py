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
    doctor_id: str | None = None


class BookingProposal(BaseModel):
    id: str
    tenant_id: str
    conversation_id: str
    service_id: str
    start: datetime
    end: datetime
    customer_name: str
    customer_email: str | None = None
    customer_phone: str | None = None  # from the call or WhatsApp; used to notify about delays
    doctor_id: str | None = None
    source: Literal["live", "demo"] = "live"  # demo bookings never touch Google Calendar
    expires_at: datetime
    status: Literal["pending", "confirmed", "expired", "conflict", "cancelled"] = "pending"
    # Set when the booking is confirmed, and again when it is moved. A reminder
    # is only sent for a booking that was confirmed well before its start.
    confirmed_at: datetime | None = None
    reminder_sent: bool = False
    # Patients may change the time of a confirmed booking once; after that, the clinic.
    time_changes: int = 0


class ChangeRequest(BaseModel):
    """A pending move or cancellation of a confirmed booking in the same conversation.

    Like a booking proposal, it changes nothing until the customer explicitly confirms it.
    """
    id: str
    tenant_id: str
    conversation_id: str
    booking_id: str
    kind: Literal["move", "cancel"]
    new_start: datetime | None = None
    new_end: datetime | None = None
    expires_at: datetime
    status: Literal["pending", "applied", "expired", "conflict"] = "pending"


class ChangeReply(BaseModel):
    booking_id: str
    kind: Literal["move", "cancel"]
    provider: str
    status: Literal["moved", "cancelled"]


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
