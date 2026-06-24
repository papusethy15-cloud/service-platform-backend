import uuid
from sqlalchemy import Column, String, Float, Boolean, Text, ForeignKey, DateTime, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.models.base import Base

class CommissionRule(Base):
    __tablename__ = "commission_rules"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    role = Column(String(50))
    commission_type = Column(String(20))  # PERCENTAGE, FLAT
    rate = Column(Float, default=0.0)
    applies_to = Column(String(50))  # BOOKING, SERVICE, PART
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Commission(Base):
    __tablename__ = "commissions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False)
    booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"))
    rule_id = Column(UUID(as_uuid=True), ForeignKey("commission_rules.id"))
    base_amount = Column(Float)
    commission_amount = Column(Float)
    status = Column(String(20), default="PENDING")  # PENDING, APPROVED, PAID
    payout_date = Column(DateTime(timezone=True))
    notes = Column(Text)
    item_type     = Column(String(20), nullable=True)   # SERVICE | PART
    item_name     = Column(String(300), nullable=True)
    item_quantity  = Column(Integer, default=1)
    part_source    = Column(String(30), nullable=True)  # OFFICE_STOCK | MARKET_PURCHASE
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CommissionGroup(Base):
    """A named group of per-service commission rules."""
    __tablename__ = "commission_groups"
    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name        = Column(String(150), nullable=False)
    description = Column(String(500), nullable=True)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    updated_at  = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CommissionGroupRule(Base):
    """One rule per service inside a commission group.

    Priority of effective price used for commission:
      1. domain_id + service_id city price (if domain linked to a city)
      2. city_id price from service_city_prices
      3. service.base_price
    """
    __tablename__ = "commission_group_rules"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id        = Column(UUID(as_uuid=True), ForeignKey("commission_groups.id", ondelete="CASCADE"), nullable=False)
    service_id      = Column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    # Optional domain scope — if set, this rule applies only for jobs via that domain
    domain_id       = Column(UUID(as_uuid=True), ForeignKey("domains.id",   ondelete="CASCADE"), nullable=True)
    commission_type = Column(String(20), nullable=False, default="PERCENTAGE")  # PERCENTAGE | FLAT
    rate            = Column(Float, nullable=False, default=0.0)  # % or flat ₹
    created_at      = Column(DateTime(timezone=True), server_default=func.now())


class CommissionGroupAssignment(Base):
    """Links a technician to a commission group."""
    __tablename__ = "commission_group_assignments"
    __table_args__ = (__import__('sqlalchemy').UniqueConstraint("technician_id", "group_id"),)
    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id", ondelete="CASCADE"), nullable=False)
    group_id      = Column(UUID(as_uuid=True), ForeignKey("commission_groups.id", ondelete="CASCADE"), nullable=False)
    assigned_at   = Column(DateTime(timezone=True), server_default=func.now())


class CommissionGroupPartRule(Base):
    """Per-spare-part commission rule inside a commission group.

    part_source_filter: NULL = applies to both OFFICE_STOCK and MARKET_PURCHASE
                        'OFFICE_STOCK'    = only office-stock parts
                        'MARKET_PURCHASE' = only market-purchased parts
    commission_type: PERCENTAGE (of sale price) | FLAT (flat ₹ per unit)
    """
    __tablename__ = "commission_group_part_rules"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id        = Column(UUID(as_uuid=True), ForeignKey("commission_groups.id", ondelete="CASCADE"), nullable=False)
    part_name_match = Column(String(200), nullable=True)   # NULL = matches ALL parts; or keyword match
    part_source_filter = Column(String(30), nullable=True) # NULL | OFFICE_STOCK | MARKET_PURCHASE
    commission_type = Column(String(20), nullable=False, default="PERCENTAGE")  # PERCENTAGE | FLAT
    rate            = Column(Float, nullable=False, default=0.0)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
