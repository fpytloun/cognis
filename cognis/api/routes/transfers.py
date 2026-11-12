"""Raw native-binary HTTP adaptation; policy belongs to the transfer service."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from cognis.core.binary_transfers import BinaryTransferService, TransferRejected
from cognis.transfers.models import TransferReceipt, UploadDescriptor, UploadRequest

router = APIRouter(prefix="/api/v1/transfers", tags=["transfers"])


def _token(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Transfer capability required")
    return token


def _service(request: Request) -> BinaryTransferService:
    service = request.app.state.binary_transfers
    if not isinstance(service, BinaryTransferService):
        raise RuntimeError("Binary transfer service is unavailable")
    return service


@router.post("", response_model=UploadDescriptor)
async def reserve_upload(request: Request, body: UploadRequest) -> UploadDescriptor:
    """Reserve a slot without receiving binary bytes in JSON."""
    try:
        return await _service(request).reserve(_token(request), body)
    except TransferRejected as exc:
        raise HTTPException(403, str(exc)) from None


@router.get("/{transfer_id}", response_model=UploadDescriptor)
async def upload_status(request: Request, transfer_id: str) -> UploadDescriptor:
    """Return receiver-observed completion after a lost upload response."""
    try:
        return await _service(request).status(_token(request), transfer_id)
    except TransferRejected as exc:
        raise HTTPException(403, str(exc)) from None


@router.put("/{transfer_id}/content", response_model=TransferReceipt)
async def receive_upload(request: Request, transfer_id: str) -> TransferReceipt:
    """Receive an opaque raw body with application/storage backpressure."""
    try:
        return await _service(request).receive(_token(request), transfer_id, request.stream())
    except TransferRejected as exc:
        raise HTTPException(403, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
