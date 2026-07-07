import uuid
from sqlalchemy import Column, String, Float, Boolean, Text, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.models.base import Base

class Wallet(Base):
    __tablename__ = "wallets"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, unique=False)
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=True, unique=True)
    balance = Column(Float, default=0.0)
    total_earned = Column(Float, default=0.0)
    total_withdrawn = Column(Float, default=0.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wallet_id = Column(UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False)
    transaction_type = Column(String(30))  # CREDIT, DEBIT, WITHDRAWAL, REFUND
    amount = Column(Float, nullable=False)
    balance_before = Column(Float, nullable=True)
    balance_after = Column(Float)
    reference_id = Column(String(200), nullable=True)  # booking/payment UUID as string
    description = Column(Text)
    status = Column(String(20), default="SUCCESS")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class WithdrawalRequest(Base):
    __tablename__ = "withdrawal_requests"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    technician_id = Column(UUID(as_uuid=True), ForeignKey("technicians.id"), nullable=False)
    wallet_id = Column(UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(String(20), default="PENDING")  # PENDING, APPROVED, REJECTED
    upi_id = Column(String(200), nullable=True)
    bank_account = Column(String(200), nullable=True)
    bank_ifsc = Column(String(20), nullable=True)
    bank_name = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)
    admin_notes = Column(Text, nullable=True)
    reviewed_by = Column(UUID(as_uuid=True), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    wallet_txn_id = Column(UUID(as_uuid=True), ForeignKey("wallet_transactions.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
