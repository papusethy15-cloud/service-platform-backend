from fastapi import APIRouter
from app.api.v1.routes import captain
from app.api.v1.routes import (
    auth, users, customers, technicians,
    services, bookings, assignments, quotations,
    invoices, payments, inventory, commissions, cash_collections,
    wallet, crm, amc, warranty, notifications,
    reports, cities, attendance, leaves, coupons,
    referrals, vendors, escalations, knowledge_base,
    tracking, gst, analytics, settings,
)
from app.api.v1.routes import appliances, refunds, sla, audit, franchises, domains
from app.api.v1.routes import chatbot

api_router = APIRouter()

api_router.include_router(auth.router,            prefix="/auth",           tags=["Auth"])
api_router.include_router(users.router,           prefix="/users",          tags=["Users"])
api_router.include_router(customers.router,       prefix="/customers",      tags=["Customers"])
api_router.include_router(technicians.router,     prefix="/technicians",    tags=["Technicians"])
api_router.include_router(services.router,        prefix="/services",       tags=["Services"])
api_router.include_router(bookings.router,        prefix="/bookings",       tags=["Bookings"])
api_router.include_router(assignments.router,     prefix="/assignments",    tags=["Assignments"])
api_router.include_router(quotations.router,      prefix="/quotations",     tags=["Quotations"])
api_router.include_router(invoices.router,        prefix="/invoices",       tags=["Invoices"])
api_router.include_router(payments.router,        prefix="/payments",       tags=["Payments"])
api_router.include_router(cash_collections.router, prefix="/cash-collections", tags=["Cash Collections"])
api_router.include_router(inventory.router,       prefix="/inventory",      tags=["Inventory"])
api_router.include_router(inventory.router,       prefix="/inventory/items",  tags=["Inventory"])   # doc-spec alias
api_router.include_router(commissions.router,     prefix="/commissions",    tags=["Commissions"])
api_router.include_router(wallet.router,          prefix="/wallet",         tags=["Wallet"])
api_router.include_router(crm.router,             prefix="/crm",            tags=["CRM"])
api_router.include_router(amc.router,             prefix="/amc",            tags=["AMC"])
api_router.include_router(warranty.router,        prefix="/warranty",       tags=["Warranty"])
api_router.include_router(notifications.router,   prefix="/notifications",  tags=["Notifications"])
api_router.include_router(reports.router,         prefix="/reports",        tags=["Reports"])
api_router.include_router(cities.router,          prefix="/cities",         tags=["Cities & Areas"])
api_router.include_router(attendance.router,      prefix="/attendance",     tags=["Attendance"])
api_router.include_router(leaves.router,          prefix="/leave",          tags=["Leave"])
api_router.include_router(coupons.router,         prefix="/coupons",        tags=["Coupons"])
api_router.include_router(referrals.router,       prefix="/referrals",      tags=["Referrals"])
api_router.include_router(vendors.router,         prefix="/vendors",        tags=["Vendors"])
api_router.include_router(escalations.router,     prefix="/escalations",    tags=["Escalations"])
api_router.include_router(knowledge_base.router,  prefix="/knowledge-base", tags=["Knowledge Base"])
api_router.include_router(tracking.router,        prefix="/tracking",       tags=["GPS Tracking"])
api_router.include_router(gst.router,             prefix="/gst",            tags=["GST"])
api_router.include_router(analytics.router,       prefix="/analytics",      tags=["Analytics"])
api_router.include_router(settings.router,        prefix="/settings",       tags=["System Settings"])
api_router.include_router(appliances.router,      prefix="/appliances",     tags=["Appliances"])
api_router.include_router(refunds.router,         prefix="/refunds",        tags=["Refunds"])
api_router.include_router(sla.router,             prefix="/sla",            tags=["SLA"])
api_router.include_router(audit.router,           prefix="/audit",          tags=["Audit Logs"])
api_router.include_router(franchises.router,      prefix="/franchises",     tags=["Franchises"])
api_router.include_router(domains.router,          prefix="/domains",         tags=["Domains"])
api_router.include_router(chatbot.router,         prefix="/chatbot",         tags=["Chatbot"])
api_router.include_router(captain.router,        prefix="/captain",        tags=["Captain App"])
