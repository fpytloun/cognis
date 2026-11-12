"""Small control-plane descriptors; binary bodies never belong in these models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

BINARY_TRANSFER_PROTOCOL = "http-binary-v1"
TRANSFER_CHUNK_BYTES = 1024 * 1024


class TransferGrant(BaseModel):
    """Controller-issued capability for one authorized execution attempt."""

    model_config = ConfigDict(extra="forbid")
    protocol: str = BINARY_TRANSFER_PROTOCOL
    url: str
    token: str = Field(repr=False)


class UploadRequest(BaseModel):
    """Metadata needed to reserve one immutable upload slot."""

    model_config = ConfigDict(extra="forbid")
    slot: int = Field(ge=0, lt=64)
    filename: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(
        default="application/octet-stream", min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$"
    )
    size_bytes: int = Field(ge=0)


class TransferReceipt(BaseModel):
    """Receiver-observed integrity metadata, verified again before publication."""

    model_config = ConfigDict(extra="forbid")
    transfer_id: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class UploadDescriptor(BaseModel):
    """Upload slot and its immutable raw-body endpoint."""

    transfer_id: str
    url: str
    state: str
    receipt: TransferReceipt | None = None


class DownloadDescriptor(BaseModel):
    """Authorized immutable content revision to download without RPC byte expansion."""

    model_config = ConfigDict(extra="forbid")
    url: str
    size_bytes: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    filename: str
    mime_type: str = "application/octet-stream"
