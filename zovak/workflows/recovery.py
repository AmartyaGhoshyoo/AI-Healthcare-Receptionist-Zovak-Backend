"""Phase 4 design contract; deliberately NOT an executing implementation.

Recovery must enumerate affected appointments, calculate safe alternatives,
obtain required organization/customer approval, make idempotent changes,
notify affected parties and reconcile failures. No bulk writes from raw LLM text.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class StaffAbsence:
    organization_id: str
    staff_id: str
    date: str
    requested_by_user_id: str


async def preview_staff_absence_recovery(request: StaffAbsence):
    raise NotImplementedError("Recovery orchestration is scheduled for Phase 4")
