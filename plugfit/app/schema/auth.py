from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator, ConfigDict


class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=8)


class OAuthAccountOut(BaseModel):
    provider: str
    provider_email: str
    model_config = ConfigDict(from_attributes=True)


class UserOut(BaseModel):
    id: str
    name: str
    email: EmailStr
    slug: str
    plan: str
    is_active: bool
    is_email_verified: bool
    created_at: datetime
    oauth_accounts: list[OAuthAccountOut] = []
    model_config = {"from_attributes": True}


class EmailVerificationRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=4, max_length=64)


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenOnly(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    sub: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class UserUpdate(BaseModel):
    name: str | None = Field(None, min_length=6, max_length=120)
    email: EmailStr | None
