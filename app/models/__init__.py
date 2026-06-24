# Import all models so SQLAlchemy/Alembic can discover them
from app.models.user import User
from app.models.rbac import Role, Permission, RolePermission, UserPermission
from app.models.customer import Customer, CustomerAddress
from app.models.service import Service, ServiceCategory
from app.models.technician import Technician, TechnicianSkill, TechnicianRating, TechnicianAvailability
from app.models.booking import Booking, BookingStatus, BookingStatusLog
from app.models.assignment import AssignmentHistory, AssignmentRule
from app.models.quotation import Quotation, QuotationServiceItem, QuotationPartItem, QuotationStatusLog
from app.models.invoice import Invoice
from app.models.payment import PaymentTransaction, CashCollectionRecord
from app.models.gst import GSTSetting as GSTSettings
from app.models.tracking import TrackingLocation
from app.models.amc import AMCPlan, AMCSubscription
from app.models.crm import CRMNote as CRMLead, CRMFollowup as CRMFollowUp, CRMTask
from app.models.warranty import Warranty as WarrantyRegistration, WarrantyClaim
from app.models.escalation import Escalation
from app.models.vendor import Vendor
from app.models.referral import Referral
# New models
from app.models.inventory import InventoryCategory, InventoryItem, Warehouse, WarehouseStock, StockMovement
from app.models.notification import Notification, NotificationTemplate
from app.models.wallet import Wallet, WalletTransaction
from app.models.commission import CommissionRule, Commission
from app.models.coupon import Coupon, CouponUsage
from app.models.attendance import Attendance, LeaveRequest
from app.models.appliance import ApplianceBrand, ApplianceType, CustomerAppliance, BrandCategory
from app.models.refund import Refund
from app.models.sla import SLAPolicy, SLABreach
from app.models.audit import AuditLog
from app.models.franchise import Franchise

from app.models.city import City, Zone, Area, CitySettings
from app.models.domain import Domain, DomainCategory, DomainService, DomainCity, ServiceCityPrice
from app.models.domain import DomainSeo, DomainProfile, DomainServiceOverride
from app.models.system_setting import SystemSetting
