from sqlalchemy import Column, String, Text, Boolean, Integer, Float, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import BaseModel


class Domain(BaseModel):
    __tablename__ = "domains"
    name          = Column(String(150), nullable=False)
    slug          = Column(String(100), nullable=False, unique=True)
    description   = Column(Text, nullable=True)
    logo_url      = Column(String(500), nullable=True)
    primary_color = Column(String(20), nullable=True)
    meta_title    = Column(String(200), nullable=True)
    meta_desc     = Column(Text, nullable=True)
    sort_order    = Column(Integer, default=0)


class DomainCategory(BaseModel):
    """Links a service category to a domain (many-to-many).
    When admin creates a category for a domain, a row is inserted here.
    """
    __tablename__ = "domain_categories"
    __table_args__ = (
        UniqueConstraint("domain_id", "category_id", name="uq_domain_category"),
    )
    domain_id   = Column(UUID(as_uuid=True), ForeignKey("domains.id"),            nullable=False)
    category_id = Column(UUID(as_uuid=True), ForeignKey("service_categories.id"), nullable=False)
    sort_order  = Column(Integer, default=0)


class DomainCity(BaseModel):
    """Links a city to a domain (many-to-many).
    Controls which cities a domain's website serves — the website only
    shows/serves cities that are linked here instead of pulling every city.
    """
    __tablename__ = "domain_cities"
    __table_args__ = (
        UniqueConstraint("domain_id", "city_id", name="uq_domain_city"),
    )
    domain_id  = Column(UUID(as_uuid=True), ForeignKey("domains.id"), nullable=False)
    city_id    = Column(UUID(as_uuid=True), ForeignKey("cities.id"),  nullable=False)
    sort_order = Column(Integer, default=0)


class DomainService(BaseModel):
    """Links a service to a domain (many-to-many).
    When admin creates a service for a domain, a row is inserted here.
    """
    __tablename__ = "domain_services"
    __table_args__ = (
        UniqueConstraint("domain_id", "service_id", name="uq_domain_service"),
    )
    domain_id  = Column(UUID(as_uuid=True), ForeignKey("domains.id"),   nullable=False)
    service_id = Column(UUID(as_uuid=True), ForeignKey("services.id"),  nullable=False)
    is_featured = Column(Boolean, default=False)


class ServiceCityPrice(BaseModel):
    """City-wise price override for a service."""
    __tablename__ = "service_city_prices"
    __table_args__ = (
        UniqueConstraint("service_id", "city_id", name="uq_service_city_price"),
    )
    service_id   = Column(UUID(as_uuid=True), ForeignKey("services.id"), nullable=False)
    city_id      = Column(UUID(as_uuid=True), ForeignKey("cities.id"),   nullable=False)
    price        = Column(Float, nullable=False)
    is_available = Column(Boolean, default=True)


class DomainSeo(BaseModel):
    """Per-domain SEO settings — one row per domain."""
    __tablename__ = "domain_seo"
    __table_args__ = (UniqueConstraint("domain_id", name="uq_domain_seo"),)
    domain_id        = Column(UUID(as_uuid=True), ForeignKey("domains.id"), nullable=False)
    meta_title       = Column(String(200), nullable=True)
    meta_description = Column(Text,        nullable=True)
    meta_keywords    = Column(Text,        nullable=True)
    og_title         = Column(String(200), nullable=True)
    og_description   = Column(Text,        nullable=True)
    og_image_url     = Column(String(500), nullable=True)
    canonical_url    = Column(String(500), nullable=True)
    robots           = Column(String(100), default="index,follow")
    schema_json      = Column(Text,        nullable=True)   # JSON-LD structured data


class DomainProfile(BaseModel):
    """
    Rich profile data per domain — shown on the domain website footer, about page,
    invoice headers, and social share previews.
    One row per domain (auto-created on first access).
    """
    __tablename__ = "domain_profiles"
    __table_args__ = (UniqueConstraint("domain_id", name="uq_domain_profile"),)

    domain_id = Column(UUID(as_uuid=True), ForeignKey("domains.id"), nullable=False)

    # ── Media assets ──────────────────────────────────────────
    logo_url          = Column(String(500), nullable=True)   # square logo (CDN/Cloudinary URL)
    logo_dark_url     = Column(String(500), nullable=True)   # dark-mode variant
    favicon_url       = Column(String(500), nullable=True)
    og_image_url      = Column(String(500), nullable=True)   # 1200×630 social share image
    banner_url        = Column(String(500), nullable=True)   # hero banner

    # ── Social media ─────────────────────────────────────────
    facebook_url      = Column(String(500), nullable=True)
    instagram_url     = Column(String(500), nullable=True)
    twitter_url       = Column(String(500), nullable=True)
    youtube_url       = Column(String(500), nullable=True)
    linkedin_url      = Column(String(500), nullable=True)
    whatsapp_number   = Column(String(20),  nullable=True)

    # ── Contact & office ─────────────────────────────────────
    support_phone     = Column(String(30),  nullable=True)
    support_email     = Column(String(200), nullable=True)
    office_address    = Column(Text,        nullable=True)
    office_city       = Column(String(100), nullable=True)
    office_state      = Column(String(100), nullable=True)
    office_pincode    = Column(String(10),  nullable=True)
    office_country    = Column(String(100), default="India")
    google_maps_url   = Column(String(500), nullable=True)

    # ── Invoice / business details ────────────────────────────
    business_legal_name  = Column(String(200), nullable=True)
    gstin                = Column(String(20),  nullable=True)
    pan_number           = Column(String(20),  nullable=True)
    invoice_prefix       = Column(String(20),  nullable=True)  # e.g. "PAL", "CBZ"
    bank_account_name    = Column(String(200), nullable=True)
    bank_account_number  = Column(String(50),  nullable=True)
    bank_ifsc            = Column(String(20),  nullable=True)
    bank_name            = Column(String(100), nullable=True)
    bank_branch          = Column(String(200), nullable=True)
    upi_id               = Column(String(100), nullable=True)

    # ── About / footer content ────────────────────────────────
    tagline              = Column(String(300), nullable=True)
    about_short          = Column(Text,        nullable=True)   # 2–3 line footer blurb
    copyright_text       = Column(String(300), nullable=True)   # e.g. "© 2025 Palei Solutions"


class DomainServiceOverride(BaseModel):
    """
    Per-domain overrides for a linked service — domain-specific image and SEO.
    One row per domain_service link (auto-created on first access/update).
    """
    __tablename__ = "domain_service_overrides"
    __table_args__ = (UniqueConstraint("domain_service_id", name="uq_domain_service_override"),)

    domain_service_id = Column(UUID(as_uuid=True), ForeignKey("domain_services.id", ondelete="CASCADE"), nullable=False)

    # Domain-specific images (uploaded to Cloudinary scoped to this domain)
    image_url      = Column(String(500), nullable=True)   # main service image
    thumbnail_url  = Column(String(500), nullable=True)   # card thumbnail

    # Domain-specific SEO
    meta_title       = Column(String(200), nullable=True)
    meta_description = Column(Text,        nullable=True)
    meta_keywords    = Column(Text,        nullable=True)
    og_title         = Column(String(200), nullable=True)
    og_description   = Column(Text,        nullable=True)
    og_image_url     = Column(String(500), nullable=True)
    # canonical_url, robots, schema_json are auto-generated by the domain frontend

    # Domain-specific content (stored as JSON arrays/objects)
    includes_json    = Column(Text,        nullable=True)  # JSON: ["item1", "item2", ...]
    excludes_json    = Column(Text,        nullable=True)  # JSON: ["item1", "item2", ...]
    faqs_json        = Column(Text,        nullable=True)  # JSON: [{"q":"...","a":"..."}]
