"""models.py - SQLAlchemy models and DB initialization."""

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    DateTime,
    MetaData,
    func,
    ForeignKey,
    Text,
    Index,
    Float,
)
from sqlalchemy.orm import declarative_base, relationship

# Naming convention to prevent DB index / constraint conflicts during schema evolution
naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}
metadata = MetaData(naming_convention=naming_convention)
Base = declarative_base(metadata=metadata)

# Runtime-initialized DB session & engine; set via init_db()
Session = None
Engine = None

def init_db(engine, session_factory):
    global Engine, Session
    Engine = engine
    Session = session_factory


class User(Base):
    """
    users table - existing user model augmented with back-populating relationship
    to ShortURL as required by KAN-111.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(512), nullable=False)
    is_active = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    # Relationship: one user -> many short URLs
    # back_populates used so ShortURL.user can reference back to User
    short_urls = relationship(
        "ShortURL",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_dict(self):
        return {"id": self.id, "email": self.email, "is_active": self.is_active}


class ShortURL(Base):
    """
    shorturls table - represents a shortened URL owned by a user.

    Columns:
      - id: primary key
      - user_id: FK -> users.id, indexed (user-scoped queries must always filter by user_id)
      - target_url: destination URL (Text to support long URLs)
      - slug: unique short path (string), unique DB constraint and indexed
      - is_custom: boolean flag indicating user-provided slug vs generated
      - created_at: timestamp when row created
      - updated_at: timestamp when row last updated
      - expire_at: nullable timestamp when short URL should stop resolving
      - is_active: boolean flag that can be used by admin scripts to mark rows inactive (default True)
      - hit_count: integer counter for hits (optional incrementing)
    """
    __tablename__ = "shorturls"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Use Text for arbitrary long URLs (SQLite ignores length; PostgreSQL supports text)
    target_url = Column(Text, nullable=False)
    # slug must be unique and indexed; length chosen to be reasonably small for storage
    slug = Column(String(255), nullable=False, unique=True, index=True)
    is_custom = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    expire_at = Column(DateTime, nullable=True)

    # New: allow admin/processes to flag links inactive without deleting them.
    # Default True preserves existing behavior.
    is_active = Column(Boolean, default=True, nullable=False)

    # Optional hit counter (can be updated eventually-consistently)
    hit_count = Column(Integer, default=0, nullable=False)

    # Relationship to owner user
    user = relationship("User", back_populates="short_urls")

    # Relationship to events (clicks)
    click_events = relationship(
        "ClickEvent",
        back_populates="shorturl",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "target_url": self.target_url,
            "slug": self.slug,
            "is_custom": self.is_custom,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expire_at": self.expire_at,
            "is_active": self.is_active,
            "hit_count": self.hit_count,
        }


class ClickEvent(Base):
    """
    clickevents table - records an anonymized "click" on a short URL.

    Columns:
      - id: primary key (BigInteger for high-throughput scenarios)
      - short_url_id: FK -> shorturls.id (which link was clicked)
      - occurred_at: timestamp when the click occurred (indexed)
      - anonymized_ip: masked client IP for privacy-preserving analytics (nullable)
      - user_agent: textual UA string (may be long)
      - referrer: referrer header (may be long)
      - country: optional country code (nullable)
    """
    __tablename__ = "clickevents"
    __table_args__ = (
        # Composite index on (short_url_id, occurred_at) - acceptance criteria
        Index("ix_clickevents_short_url_id_occurred_at", "short_url_id", "occurred_at"),
    )

    id = Column(BigInteger, primary_key=True)
    # Use database column name short_url_id to match ticket and be explicit
    short_url_id = Column("short_url_id", Integer, ForeignKey("shorturls.id", ondelete="CASCADE"), nullable=False)
    anonymized_ip = Column(String(128), nullable=True)
    user_agent = Column(Text, nullable=True)
    referrer = Column(Text, nullable=True)
    country = Column(String(8), nullable=True)
    occurred_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Back reference to ShortURL
    shorturl = relationship("ShortURL", back_populates="click_events")


# -------------------------
# New models for session token management (KAN-129)
# -------------------------
class SessionToken(Base):
    """
    session_tokens table - stores a record for issued session tokens (JWT/JTI).

    Columns:
      - id: primary key (auto)
      - jti: unique token identifier (string), used to mark tokens revoked/valid
      - user_id: FK -> users.id (owner)
      - issued_at: timestamp when token was issued
      - last_seen: timestamp last used (optional update on each request)
      - ip: anonymized IP when issued (nullable)
      - user_agent: client UA string (nullable)
      - revoked: boolean flag to mark token invalid without deleting the row
    """
    __tablename__ = "session_tokens"

    id = Column(Integer, primary_key=True)
    jti = Column(String(255), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    issued_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_seen = Column(DateTime, nullable=True)
    ip = Column(String(128), nullable=True)
    user_agent = Column(Text, nullable=True)
    revoked = Column(Boolean, default=False, nullable=False)

    # Relationship back to user (optional)
    user = relationship("User", backref="sessions")

    def to_dict(self):
        return {
            "id": self.id,
            "jti": self.jti,
            "user_id": self.user_id,
            "issued_at": self.issued_at,
            "last_seen": self.last_seen,
            "ip": self.ip,
            "user_agent": self.user_agent,
            "revoked": self.revoked,
        }


class RevokedToken(Base):
    """
    revoked_tokens table - audit of revocations for tokens. Stores explicit revocations.
    This allows quick checks and audit trail; redundancy with SessionToken.revoked is intentional.
    """
    __tablename__ = "revoked_tokens"

    id = Column(Integer, primary_key=True)
    jti = Column(String(255), nullable=False, index=True)
    revoked_at = Column(DateTime, server_default=func.now(), nullable=False)
    reason = Column(Text, nullable=True)

    def __repr__(self):
        return f"<RevokedToken jti={self.jti} revoked_at={self.revoked_at} reason={self.reason}>"


# -------------------------
# New: CustomDomain model for custom domains & verification (KAN-144)
# -------------------------
class CustomDomain(Base):
    """
    custom_domains table - represents a domain a user has registered for branded short links.

    Columns:
      - id: primary key
      - owner_id: FK -> users.id (owner). All queries MUST be filtered by owner_id to respect ID Filter rule.
      - domain: canonical lowercased host (string) - unique index
      - verification_token: short random token the user places in DNS (TXT or CNAME test)
      - is_verified: boolean flag set to True when verification succeeds
      - verification_requested_at: timestamp when user registered/initiated verification
      - verified_at: timestamp when verification completed (nullable)
      - created_at/updated_at: bookkeeping timestamps
    """
    __tablename__ = "custom_domains"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    domain = Column(String(255), nullable=False, unique=True, index=True)
    verification_token = Column(String(128), nullable=False, index=True)
    is_verified = Column(Boolean, default=False, nullable=False, index=True)
    verification_requested_at = Column(DateTime, server_default=func.now(), nullable=False)
    verified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationship back to user (optional convenience)
    owner = relationship("User", backref="custom_domains")

    def to_dict(self):
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "domain": self.domain,
            "is_verified": self.is_verified,
            "verification_requested_at": self.verification_requested_at,
            "verified_at": self.verified_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# -------------------------
# New: RateLimitCounter model for DB fallback (KAN-126)
# -------------------------
class RateLimitCounter(Base):
    """
    rate_limit_counters table - persisted token-bucket state for cross-process rate limiting fallback.

    Columns:
      - id: primary key
      - key: unique string key identifying the bucket (e.g., "user:123:/shorten" or "ip:203.0.113.1:/<slug>")
      - tokens: float - current token count (may be fractional)
      - last_refill: DateTime - when tokens last updated
      - created_at: DateTime - creation time
    """
    __tablename__ = "rate_limit_counters"

    id = Column(Integer, primary_key=True)
    key = Column(String(512), nullable=False, unique=True, index=True)
    tokens = Column(Float, nullable=False, default=0.0)
    last_refill = Column(DateTime, nullable=False, server_default=func.now())
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<RateLimitCounter key={self.key} tokens={self.tokens} last_refill={self.last_refill}>"


# -------------------------
# New: APIKey model for programmatic access (KAN-145)
# -------------------------
class APIKey(Base):
    """
    api_keys table - represents API keys that third-party developers use to call the
    programmatic endpoints. Each key may carry basic rate-limit settings.

    Fields:
      - id: primary key
      - key: the opaque API key string (unique)
      - user_id: FK -> users.id (owner) (all queries MUST be scoped by user_id when appropriate)
      - name: optional human-friendly name for the key
      - created_at: timestamp when key created
      - revoked: boolean flag to immediately disable a key
      - rate_limit_capacity: integer capacity of token bucket (e.g., 100)
      - rate_limit_window_seconds: integer window seconds for full refill (e.g., 60)
    """
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    key = Column(String(255), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    revoked = Column(Boolean, default=False, nullable=False, index=True)
    # Per-key rate limit settings (optional overrides). If null/0, caller code should fall back to app defaults.
    rate_limit_capacity = Column(Integer, nullable=True)
    rate_limit_window_seconds = Column(Integer, nullable=True)

    # Relationship back to user (optional convenience)
    owner = relationship("User", backref="api_keys")

    def to_dict(self):
        return {
            "id": self.id,
            "key": self.key,
            "user_id": self.user_id,
            "name": self.name,
            "created_at": self.created_at,
            "revoked": self.revoked,
            "rate_limit_capacity": self.rate_limit_capacity,
            "rate_limit_window_seconds": self.rate_limit_window_seconds,
        }


# -------------------------
# New: FailedLoginCounter model for lockout & brute-force tracking (KAN-127)
# -------------------------
class FailedLoginCounter(Base):
    """
    failed_login_counters table - track failed login attempts and lockout state.

    Columns:
      - id: primary key
      - key: unique string key identifying the subject of tracking (e.g., "user:123" or "ip:203.0.113.1")
      - count: integer count of recent failed attempts (resets on lockout or successful auth)
      - lockout_until: nullable DateTime indicating lockout expiry (if locked)
      - lockout_count: integer count how many times this key has been locked out (used to compute backoff)
      - last_failed_at: DateTime of most recent failed attempt
      - created_at: DateTime created
    """
    __tablename__ = "failed_login_counters"

    id = Column(Integer, primary_key=True)
    key = Column(String(512), nullable=False, unique=True, index=True)
    count = Column(Integer, nullable=False, default=0)
    lockout_until = Column(DateTime, nullable=True)
    lockout_count = Column(Integer, nullable=False, default=0)
    last_failed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<FailedLoginCounter key={self.key} count={self.count} lockout_until={self.lockout_until} lockout_count={self.lockout_count}>"


# -------------------------
# Helper utilities / examples
# -------------------------
# These helpers document and demonstrate canonical, user-scoped operations that respect the
# "ID Filter" rule. They are intentionally small and are examples; routes and services should
# call similar patterns (filter_by(..., user_id=current_user_id)).

from sqlalchemy.exc import IntegrityError

class DuplicateSlugError(Exception):
    """Raised when attempting to insert a ShortURL with a slug that already exists."""
    pass

def create_shorturl(session, user_id: int, target_url: str, slug: str, is_custom: bool = False, expire_at=None) -> ShortURL:
    """
    Create a ShortURL row in a safe, transactional manner and handle slug uniqueness conflicts.

    Usage:
      session = models.Session()
      try:
          new_short = create_shorturl(session, current_user_id, "https://...", "my-slug", is_custom=True)
      except DuplicateSlugError:
          # Handle duplicate slug: prompt user to choose a different slug, or regenerate
          pass

    Implementation notes:
      - Uses session.begin() to ensure atomicity.
      - Catches IntegrityError (slug uniqueness) and rolls back the transaction.
      - Caller is responsible for session lifetime (closing / removing).
    """
    url = ShortURL(
        user_id=user_id,
        target_url=target_url,
        slug=slug,
        is_custom=bool(is_custom),
        expire_at=expire_at,
    )
    try:
        with session.begin():
            session.add(url)
            # commit happens on successful exit of context manager
        # Refresh to populate DB-generated fields (id, timestamps)
        session.refresh(url)
        return url
    except IntegrityError as e:
        # Rollback handled by session.begin() context, but ensure explicit rollback in older SQLAlchemy versions
        try:
            session.rollback()
        except Exception:
            pass
        # Map integrity errors conservatively to DuplicateSlugError for slug uniqueness handling
        raise DuplicateSlugError("Slug conflict or other integrity error: {}".format(str(e)))


# -------------------------
# Canonical query snippets (for developers)
# -------------------------
# Always apply the "ID Filter" rule: filter by the owner (user_id) when accessing or modifying user-owned data.

# 1) Fetch a single short URL owned by the current user:
#    short = session.query(ShortURL).filter_by(id=link_id, user_id=current_user_id).first()

# 2) Update a URL's target (user-scoped):
#    short = session.query(ShortURL).filter_by(id=link_id, user_id=current_user_id).first()
#    if not short:
#        # not found or unauthorized
#    short.target_url = new_target
#    session.add(short)
#    session.commit()

# 3) Delete a short URL (user-scoped):
#    short = session.query(ShortURL).filter_by(id=link_id, user_id=current_user_id).first()
#    if short:
#        session.delete(short)
#        session.commit()

# 4) List paginated short URLs for a user:
#    q = session.query(ShortURL).filter_by(user_id=current_user_id).order_by(ShortURL.created_at.desc())
#    items = q.limit(page_size).offset(page_offset).all()

# 5) Create with duplicate-slug handling (uses helper above):
#    try:
#        new_short = create_shorturl(db_session, current_user_id, target_url, slug, is_custom=True)
#    except DuplicateSlugError:
#        # present friendly error to user; suggest different slug or allow automatic generation

# Note: Avoid using session.query(ShortURL).get(id) for user-owned resources — that bypasses ownership checks.

# End of models.py
--- END FILE ---