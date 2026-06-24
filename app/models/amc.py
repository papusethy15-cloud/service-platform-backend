from sqlalchemy import Column, String, Text, DateTime, Float, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import BaseModel

class AMCPlan(BaseModel):
    __tablename__ = "amc_plans"
    name             = Column(String(100), nullable=False)
    plan_type        = Column(String(30), default="GOLD")
    price            = Column(Float, nullable=False)
    duration_months  = Column(Integer, default=12)
    visit_count      = Column(Integer, nullable=False)
    description      = Column(Text, nullable=True)
    appliance_types  = Column(Text, nullable=True)

class AMCSubscription(BaseModel):
    __tablename__ = "amc_subscriptions"
    customer_id       = Column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    plan_id           = Column(UUID(as_uuid=True), ForeignKey("amc_plans.id"), nullable=False)
    start_date        = Column(DateTime, nullable=False)
    end_date          = Column(DateTime, nullable=False)
    visits_remaining  = Column(Integer, default=0)
    amount_paid       = Column(Float, default=0.0)
    status            = Column(String(20), default="ACTIVE")

class AMCVisit(BaseModel):
    __tablename__ = "amc_visits"
    amc_id         = Column(UUID(as_uuid=True), ForeignKey("amc_subscriptions.id"), nullable=False)
    scheduled_date = Column(DateTime, nullable=False)
    technician_id  = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=True)
    notes          = Column(Text, nullable=True)
    status         = Column(String(20), default="SCHEDULED")
