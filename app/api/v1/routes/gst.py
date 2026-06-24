import re
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AnyStaff, AdminOrCCO, require_roles
from app.api.v1.schemas.gst import UpdateGSTSettingsRequest, ValidateGSTINRequest
from app.core.database import get_db
from app.models.gst import GSTSetting
from app.services.reporting import build_gst_report
from app.utils.response import success_response

router = APIRouter()
FinanceOrOps = require_roles("SUPER_ADMIN", "ADMIN", "CCO", "ACCOUNTANT")
GSTIN_REGEX = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def _normalize_gstin(gstin: str) -> str:
    return gstin.strip().upper()


def _is_valid_gstin(gstin: str) -> bool:
    return bool(GSTIN_REGEX.match(_normalize_gstin(gstin)))


async def _get_or_create_settings(db: AsyncSession) -> GSTSetting:
    settings = (
        await db.execute(select(GSTSetting).where(GSTSetting.is_active == True).order_by(GSTSetting.created_at.desc()))
    ).scalars().first()
    if settings:
        return settings
    settings = GSTSetting(
        gst_enabled=True,
        default_rate=18.0,
        allow_b2b=True,
        allow_b2c=True,
        allow_non_gst=True,
        gstin_validation_enabled=True,
        invoice_prefix="INV",
    )
    db.add(settings)
    await db.flush()
    return settings


def _serialize_settings(settings: GSTSetting):
    return {
        "id": str(settings.id),
        "gst_enabled": settings.gst_enabled,
        "default_rate": settings.default_rate,
        "allow_b2b": settings.allow_b2b,
        "allow_b2c": settings.allow_b2c,
        "allow_non_gst": settings.allow_non_gst,
        "gstin_validation_enabled": settings.gstin_validation_enabled,
        "company_gstin": settings.company_gstin,
        "company_name": settings.company_name,
        "company_address": settings.company_address,
        "hsn_code": settings.hsn_code,
        "invoice_prefix": settings.invoice_prefix,
        "state_code": settings.state_code,
        "updated_by": str(settings.updated_by) if settings.updated_by else None,
        "updated_at": settings.updated_at.isoformat() if settings.updated_at else None,
    }


@router.get("/settings", summary="Get GST settings")
async def get_gst_settings(
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    settings = await _get_or_create_settings(db)
    await db.commit()
    return success_response(data=_serialize_settings(settings))


@router.put("/settings", summary="Update GST settings [Admin/CCO]")
async def update_gst_settings(
    payload: UpdateGSTSettingsRequest,
    current_user: dict = Depends(AdminOrCCO),
    db: AsyncSession = Depends(get_db),
):
    settings = await _get_or_create_settings(db)
    if payload.gst_enabled is not None:
        settings.gst_enabled = payload.gst_enabled
    if payload.default_rate is not None:
        settings.default_rate = payload.default_rate
    if payload.allow_b2b is not None:
        settings.allow_b2b = payload.allow_b2b
    if payload.allow_b2c is not None:
        settings.allow_b2c = payload.allow_b2c
    if payload.allow_non_gst is not None:
        settings.allow_non_gst = payload.allow_non_gst
    if payload.gstin_validation_enabled is not None:
        settings.gstin_validation_enabled = payload.gstin_validation_enabled
    if payload.company_gstin is not None:
        normalized_gstin = _normalize_gstin(payload.company_gstin)
        if normalized_gstin and settings.gstin_validation_enabled and not _is_valid_gstin(normalized_gstin):
            raise HTTPException(status_code=400, detail="Company GSTIN format is invalid")
        settings.company_gstin = normalized_gstin or None
    if payload.company_name is not None:
        settings.company_name = payload.company_name
    if payload.company_address is not None:
        settings.company_address = payload.company_address
    if payload.hsn_code is not None:
        settings.hsn_code = payload.hsn_code
    if payload.invoice_prefix is not None:
        settings.invoice_prefix = payload.invoice_prefix.strip().upper() or "INV"
    if payload.state_code is not None:
        settings.state_code = payload.state_code
    settings.updated_by = UUID(current_user["user_id"])
    await db.commit()
    return success_response(data=_serialize_settings(settings), message="GST settings updated successfully")


@router.post("/validate-gstin", summary="Validate GSTIN")
async def validate_gstin(
    payload: ValidateGSTINRequest,
    current_user: dict = Depends(FinanceOrOps),
    db: AsyncSession = Depends(get_db),
):
    settings = await _get_or_create_settings(db)
    normalized_gstin = _normalize_gstin(payload.gstin)
    await db.commit()
    return success_response(
        data={
            "gstin": normalized_gstin,
            "is_valid": _is_valid_gstin(normalized_gstin),
            "validation_enabled": settings.gstin_validation_enabled,
            "state_code": normalized_gstin[:2] if len(normalized_gstin) >= 2 else None,
            "pan": normalized_gstin[2:12] if len(normalized_gstin) >= 12 else None,
        },
        message="GSTIN validation completed",
    )


@router.get("/reports", summary="GST reports")
async def gst_reports(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: dict = Depends(FinanceOrOps),
    db: AsyncSession = Depends(get_db),
):
    try:
        report = await build_gst_report(db, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return success_response(data=report)
