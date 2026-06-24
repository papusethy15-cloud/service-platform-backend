from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AnyStaff
from app.core.database import get_db
from app.services.reporting import (
    build_customer_report,
    build_gst_report,
    build_placeholder_report,
    build_revenue_report,
)
from app.utils.response import success_response

router = APIRouter()


def _handle_report_range_error(exc: ValueError):
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/revenue", summary="Revenue report")
async def revenue_report(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    try:
        report = await build_revenue_report(db, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        _handle_report_range_error(exc)
    return success_response(data=report)


@router.get("/gst", summary="GST report")
async def gst_report(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    try:
        report = await build_gst_report(db, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        _handle_report_range_error(exc)
    return success_response(data=report)


@router.get("/commission", summary="Commission report")
async def commission_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("commission", "Commission source tables are not implemented yet"),
        message="Commission report is waiting on the commission module",
    )


@router.get("/inventory", summary="Inventory report")
async def inventory_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("inventory", "Inventory source tables are not implemented yet"),
        message="Inventory report is waiting on the inventory module",
    )


@router.get("/amc", summary="AMC report")
async def amc_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("amc", "AMC source tables are not implemented yet"),
        message="AMC report is waiting on the AMC module",
    )


@router.get("/warranty", summary="Warranty report")
async def warranty_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("warranty", "Warranty source tables are not implemented yet"),
        message="Warranty report is waiting on the warranty module",
    )


@router.get("/customer", summary="Customer report")
async def customer_report(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: dict = Depends(AnyStaff),
    db: AsyncSession = Depends(get_db),
):
    try:
        report = await build_customer_report(db, start_date=start_date, end_date=end_date)
    except ValueError as exc:
        _handle_report_range_error(exc)
    return success_response(data=report)


@router.get("/franchise", summary="Franchise report")
async def franchise_report(current_user: dict = Depends(AnyStaff)):
    return success_response(
        data=build_placeholder_report("franchise", "Franchise source tables are not implemented yet"),
        message="Franchise report is waiting on the franchise module",
    )
