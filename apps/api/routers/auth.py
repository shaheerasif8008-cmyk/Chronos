import random

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, update

from core import audit
from core.auth import create_access_token
from core.config import settings
from core.db import engine, reflect_table

router = APIRouter(prefix="/auth", tags=["auth"])
_otp_store: dict[str, str] = {}


class OtpRequest(BaseModel):
    email: EmailStr


class OtpVerify(BaseModel):
    email: EmailStr
    code: str


@router.post("/request-otp")
async def request_otp(req: OtpRequest) -> dict[str, str]:
    code = f"{random.randint(0, 999999):06d}"
    _otp_store[req.email.lower()] = code
    await audit.log("otp_requested", req.email.lower(), "auth.request_otp")
    print(f"Chronos dev OTP for {req.email}: {code}", flush=True)
    return {"status": "otp_sent_dev_console"}


@router.post("/verify-otp")
async def verify_otp(req: OtpVerify, response: Response) -> dict[str, str]:
    email = req.email.lower()
    if _otp_store.get(email) != req.code:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (await conn.execute(select(members).where(members.c.email == email))).mappings().first()
        if row is None:
            raise HTTPException(status_code=403, detail="Email is not seeded as a Chronos member")
        token = create_access_token(row["id"])
        await conn.execute(
            update(members).where(members.c.id == row["id"]).values(region=settings.region)
        )
    await audit.log("otp_verified", row["id"], "auth.verify_otp")
    response.set_cookie("chronos_session", token, httponly=True, samesite="lax")
    return {"access_token": token, "token_type": "bearer", "member_id": row["id"]}
