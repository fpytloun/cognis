"""FastAPI request and response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, EmailStr, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class SetupRequest(BaseModel):
    token: str
    email: EmailStr
    name: str | None = None
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    token: str | None = None
    refresh_token: str | None = None


class TokenResponse(BaseModel):
    token: str
    refresh_token: str | None = None
    expires_in: int
    user: dict[str, Any]


class ExchangeTokenResponse(BaseModel):
    token: str
    target: str
    expires_in: int


class HealthResponse(BaseModel):
    status: str
    providers: dict[str, dict[str, Any]]
    remember_queue: dict[str, Any] | None = None
