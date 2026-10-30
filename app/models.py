import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def normalize_e164(value: str) -> str:
    """Accept '+34 657 84 49 47' / '0034...' and return compact E.164."""
    compact = re.sub(r"[\s().-]+", "", value or "")
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    return compact


class RegisterRequest(BaseModel):
    account: str = Field(examples=["+393331234567"], description="E.164 phone number")

    @field_validator("account", mode="before")
    @classmethod
    def _compact_account(cls, value: Any) -> Any:
        return normalize_e164(value) if isinstance(value, str) else value

    @field_validator("account")
    @classmethod
    def _valid_e164(cls, value: str) -> str:
        if not re.fullmatch(r"\+\d{7,15}", value):
            raise ValueError(
                "account must be E.164: '+' and country code, digits only "
                "(e.g. +34657844947)")
        return value
    captcha: str | None = Field(
        default=None,
        description="Token from https://signalcaptchas.org/registration/generate.html "
        "(full signalcaptcha://... link). Required when Signal asks for it.",
    )
    voice: bool = False
    reregister: bool = False


class VerifyRequest(BaseModel):
    code: str = Field(examples=["123456"], description="SMS/voice verification code")
    pin: str | None = Field(default=None, description="Registration-lock PIN, if set")


class FinishLinkRequest(BaseModel):
    device_link_uri: str = Field(description="URI returned by /accounts/link/start")
    device_name: str = "signal-gateway"


class SendMessageRequest(BaseModel):
    account: str = Field(description="Sender: phone number (+39...) or ACI UUID")
    message: str | None = None
    recipients: list[str] = Field(
        default_factory=list,
        description="Phone numbers (+39...), ACI UUIDs or u:username",
    )
    group_id: str | None = Field(default=None, description="Base64 group id (v2 groups)")
    note_to_self: bool = False
    attachments: list[str] = Field(
        default_factory=list,
        description="File paths visible to signal-cli, or data: URIs",
    )
    quote_timestamp: int | None = None
    quote_author: str | None = None
    quote_message: str | None = None

    @model_validator(mode="after")
    def _check_target(self) -> "SendMessageRequest":
        targets = bool(self.recipients) + bool(self.group_id) + bool(self.note_to_self)
        if targets != 1:
            raise ValueError("Set exactly one of: recipients, group_id, note_to_self")
        if not self.message and not self.attachments:
            raise ValueError("message or attachments is required")
        return self


class ReactionRequest(BaseModel):
    account: str
    emoji: str
    target_author: str
    target_timestamp: int
    recipient: str | None = None
    group_id: str | None = None
    remove: bool = False


class CreateGroupRequest(BaseModel):
    account: str
    name: str
    members: list[str] = Field(default_factory=list)
    description: str | None = None


class UpdateGroupRequest(BaseModel):
    account: str
    add_members: list[str] = Field(default_factory=list)
    remove_members: list[str] = Field(default_factory=list)
    name: str | None = None
    description: str | None = None


class RpcPassthroughRequest(BaseModel):
    """Escape hatch: call any signal-cli JSON-RPC method directly."""

    method: str
    params: dict[str, Any] = Field(default_factory=dict)
