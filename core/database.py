import os
import logging
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Text, Boolean, DateTime, Integer, ForeignKey, JSON, Index, func, text
from sqlalchemy.types import TypeDecorator
from sqlalchemy.ext.declarative import declarative_base, declared_attr
from sqlalchemy.orm import relationship, sessionmaker, backref

logger = logging.getLogger(__name__)

# --- foundational SQLAlchemy primitives moved to core/db_base.py (ADR-058);
# re-imported so the ORM models below + the migrations + ~46 external importers
# keep working with the SAME objects (identity-preserving).
from core.db_base import (  # noqa: E402,F401
    Base,
    TimestampMixin,
    DATABASE_URL,
    engine,
    SessionLocal,
    EncryptedText,
)


class Session(TimestampMixin, Base):
    """
    SQLAlchemy model for Session table.
    Represents a chat session with its configuration and metadata.
    """
    __tablename__ = "sessions"
    
    # Primary key
    id = Column(String, primary_key=True, index=True)
    
    # Session metadata
    name = Column(String, nullable=False)
    endpoint_url = Column(String, nullable=False)
    model = Column(String, nullable=False)
    owner = Column(String, nullable=True, index=True)  # username; null = legacy/shared
    
    # Configuration flags
    rag = Column(Boolean, default=False)
    archived = Column(Boolean, default=False)

    # Organization
    folder = Column(String, nullable=True, default=None)
    
    # Headers stored as JSON
    headers = Column(JSON, default=dict)
    
    # Timestamps are provided by TimestampMixin
    last_accessed = Column(DateTime, default=func.now(), onupdate=func.now())
    # Timestamp of the last actual MESSAGE in this session. Set explicitly
    # only when a message is persisted (NOT onupdate) — so it's a clean
    # "last conversation" signal, immune to renames / model swaps / merely
    # opening the chat (all of which bump updated_at and last_accessed).
    # The "Last active" sort uses this.
    last_message_at = Column(DateTime, nullable=True, default=None)
    
    
    # Indexes - optimized composites
    __table_args__ = (
        Index('ix_sessions_active', 'archived', 'last_accessed'),
        Index('ix_sessions_search', 'name', 'archived'),
    )
    
    # Properties
    is_important = Column(Boolean, default=False)
    message_count = Column(Integer, default=0)
    total_input_tokens = Column(Integer, default=0)
    total_output_tokens = Column(Integer, default=0)
    mode = Column(String, nullable=True)  # 'agent', 'chat', or 'research'
    crew_member_id = Column(String, nullable=True)  # links to crew_members.id

    # Relationship to chat messages
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")
    
    @property
    def is_active(self):
        """Check if session is active (not archived)"""
        return not self.archived
    
    def to_dict(self):
        """Convert session to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'name': self.name,
            'model': self.model,
            'endpoint_url': self.endpoint_url,
            'rag': self.rag,
            'archived': self.archived,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'last_accessed': self.last_accessed.isoformat() if self.last_accessed else None,
            'last_message_at': self.last_message_at.isoformat() if self.last_message_at else None,
            'message_count': self.message_count,
            'is_important': self.is_important,
            'folder': self.folder,
            'total_input_tokens': self.total_input_tokens or 0,
            'total_output_tokens': self.total_output_tokens or 0,
            'crew_member_id': self.crew_member_id,
        }

class ChatMessage(Base):
    """
    SQLAlchemy model for ChatMessage table.
    Represents individual chat messages within a session.
    """
    __tablename__ = "chat_messages"
    
    # Primary key - using String to support UUIDs
    id = Column(String, primary_key=True, index=True)
    
    # Foreign key to Session
    session_id = Column(String, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Message content
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    meta_data = Column("metadata", Text, nullable=True)  # JSON string for metrics etc.

    # Timestamp
    timestamp = Column(DateTime, default=datetime.utcnow)
    
    # Relationship to Session
    session = relationship("Session", back_populates="messages")
    
    # Indexes - optimized composite
    __table_args__ = (
        Index('ix_messages_session_time', 'session_id', 'timestamp'),  # Composite for efficient message retrieval
    )

class Document(TimestampMixin, Base):
    """Living document that the AI can create and edit in-place."""
    __tablename__ = "documents"

    id              = Column(String, primary_key=True, index=True)
    session_id      = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    title           = Column(String, nullable=False, default="Untitled")
    language        = Column(String, nullable=True)          # "python", "markdown", "text", etc.
    current_content = Column(Text, nullable=False, default="")
    version_count   = Column(Integer, default=1)
    is_active       = Column(Boolean, default=True)
    # Soft-archive: hidden from the Library's Documents list/search/Tidy until
    # restored. Distinct from is_active (which tracks "open in a session").
    archived        = Column(Boolean, default=False)
    # Owner of this document. Documents used to derive ownership from their
    # linked chat session, but a session can be deleted (session_id → NULL via
    # SET NULL), orphaning the doc and making it vanish from the owner's
    # Library + search. Owning the row directly is robust against that.
    owner           = Column(String, nullable=True, index=True)
    tidy_verdict    = Column(String, nullable=True)        # "keep", "junk", or None (not yet reviewed)
    # Provenance: if this document was created by opening an email attachment,
    # these point back to the source email so the "Sign and reply" flow can
    # thread a response on the original conversation.
    source_email_uid         = Column(String, nullable=True)
    source_email_folder      = Column(String, nullable=True)
    source_email_account_id  = Column(String, nullable=True)
    source_email_message_id  = Column(String, nullable=True, index=True)

    session  = relationship("Session", backref=backref("documents", cascade="save-update, merge"))
    versions = relationship("DocumentVersion", back_populates="document",
                           cascade="all, delete-orphan", order_by="DocumentVersion.version_number")


class DocumentVersion(Base):
    """Immutable snapshot of a document at a point in time."""
    __tablename__ = "document_versions"

    id             = Column(String, primary_key=True, index=True)
    document_id    = Column(String, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    content        = Column(Text, nullable=False)
    summary        = Column(String, nullable=True)     # Edit description
    source         = Column(String, default="ai")      # "ai" or "user"
    created_at     = Column(DateTime, default=datetime.utcnow)

    document = relationship("Document", back_populates="versions")


class GalleryAlbum(TimestampMixin, Base):
    """A photo album/folder."""
    __tablename__ = "gallery_albums"

    id          = Column(String, primary_key=True, index=True)
    name        = Column(String, nullable=False)
    description = Column(Text, default="")
    cover_id    = Column(String, nullable=True)  # GalleryImage.id for cover photo
    owner       = Column(String, nullable=True, index=True)

    images = relationship("GalleryImage", back_populates="album")


class GalleryImage(TimestampMixin, Base):
    """Stores metadata for photos and AI-generated images."""
    __tablename__ = "gallery_images"

    id         = Column(String, primary_key=True, index=True)
    filename   = Column(String, nullable=False, unique=True)
    prompt     = Column(Text, nullable=False, default="")
    model      = Column(String, nullable=True)
    size       = Column(String, nullable=True)
    quality    = Column(String, nullable=True)
    tags       = Column(String, nullable=True, default="")
    ai_tags    = Column(Text, nullable=True, default="")       # AI-generated tags (comma-separated)
    session_id = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    album_id   = Column(String, ForeignKey("gallery_albums.id", ondelete="SET NULL"), nullable=True, index=True)
    owner      = Column(String, nullable=True, index=True)
    is_active  = Column(Boolean, default=True)
    favorite   = Column(Boolean, default=False)

    # File integrity
    file_hash  = Column(String(64), nullable=True, index=True)  # SHA-256

    # EXIF / photo metadata
    taken_at       = Column(DateTime, nullable=True, index=True)  # EXIF DateTimeOriginal
    camera_make    = Column(String, nullable=True)
    camera_model   = Column(String, nullable=True)
    gps_lat        = Column(String, nullable=True)  # stored as string for precision
    gps_lng        = Column(String, nullable=True)
    width          = Column(Integer, nullable=True)
    height         = Column(Integer, nullable=True)
    file_size      = Column(Integer, nullable=True)  # bytes

    session = relationship("Session", backref=backref("gallery_images"))
    album   = relationship("GalleryAlbum", back_populates="images")

    __table_args__ = (
        Index('ix_gallery_images_tags', 'tags'),
        Index('ix_gallery_images_model', 'model'),
        Index('ix_gallery_images_active', 'is_active', 'created_at'),
    )


class EmailAccount(TimestampMixin, Base):
    """A configured IMAP/SMTP account. Supports multiple accounts per user —
    exactly one row per owner has is_default=True.

    Security note: imap_password / smtp_password are stored Fernet-encrypted
    via src/secret_storage.py. The key lives at data/.app_key (mode 0o600,
    gitignored). Anyone with read access to that file can decrypt every
    row, so the threat model is "stolen SQLite backup" rather than
    "process compromise". On first start any legacy plaintext rows are
    migrated automatically (see _migrate_encrypt_email_passwords).
    """
    __tablename__ = "email_accounts"

    id             = Column(String, primary_key=True, index=True)
    owner          = Column(String, nullable=True, index=True)
    name           = Column(String, nullable=False)  # Display name: "Work", "Personal", etc.
    is_default     = Column(Boolean, default=False, nullable=False)
    enabled        = Column(Boolean, default=True, nullable=False)

    # IMAP (receiving)
    imap_host      = Column(String, default="")
    imap_port      = Column(Integer, default=993)
    imap_user      = Column(String, default="")
    imap_password  = Column(String, default="")
    imap_starttls  = Column(Boolean, default=True)

    # SMTP (sending)
    smtp_host      = Column(String, default="")
    smtp_port      = Column(Integer, default=465)
    smtp_user      = Column(String, default="")
    smtp_password  = Column(String, default="")

    from_address   = Column(String, default="")

    __table_args__ = (
        Index('ix_email_accounts_owner_default', 'owner', 'is_default'),
    )


class ModelEndpoint(TimestampMixin, Base):
    """Admin-configured model endpoints. Models are auto-discovered via /v1/models."""
    __tablename__ = "model_endpoints"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)          # Display label, e.g. "Local vLLM", "OpenRouter"
    base_url = Column(String, nullable=False)      # Base URL, e.g. "http://localhost:8002/v1"
    api_key = Column(EncryptedText, nullable=True)  # Optional provider API key, encrypted at rest
    is_enabled = Column(Boolean, default=True)
    hidden_models = Column(Text, nullable=True)    # JSON list of model IDs that failed probing
    cached_models = Column(Text, nullable=True)    # JSON list of last-known model IDs (avoids probe on list)
    model_type = Column(String, nullable=True, default="llm")  # "llm" or "image"
    # Whether models on this endpoint accept OpenAI-style function
    # schemas + emit `tool_calls`. Auto-detected at Cookbook auto-
    # register time from `--enable-auto-tool-choice` in the serve cmd;
    # can be toggled per-endpoint in the UI. NULL = unknown, falls
    # back to the model-name keyword heuristic in agent_loop.py.
    supports_tools = Column(Boolean, nullable=True, default=None)
    # Per-user ownership. NULL = legacy/shared (visible to every user) — this
    # is the historical default. When non-null, the model picker only shows
    # the endpoint to that user (admins always see everything).
    owner = Column(String, nullable=True, index=True)

class McpServer(TimestampMixin, Base):
    """Admin-configured MCP (Model Context Protocol) tool servers."""
    __tablename__ = "mcp_servers"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    transport = Column(String, nullable=False, default="stdio")  # "stdio" or "sse"
    command = Column(String, nullable=True)      # For stdio: executable path
    args = Column(Text, nullable=True)           # JSON array of command args
    env = Column(Text, nullable=True)            # JSON object of env vars
    url = Column(String, nullable=True)          # For SSE: server URL
    is_enabled = Column(Boolean, default=True)
    oauth_config = Column(Text, nullable=True)   # JSON: provider, keys_file, token_file, scopes
    disabled_tools = Column(Text, nullable=True)  # JSON array of tool names to hide from LLM


class Comparison(TimestampMixin, Base):
    """Stores A/B model comparison results."""
    __tablename__ = "comparisons"

    id = Column(String, primary_key=True, index=True)
    session_id = Column(String, nullable=True)     # Parent session context (optional)
    owner = Column(String, nullable=True, index=True)  # username
    prompt = Column(Text, nullable=False)
    model_a = Column(String, nullable=False)
    model_b = Column(String, nullable=False)
    endpoint_a = Column(String, nullable=False)
    endpoint_b = Column(String, nullable=False)
    response_a = Column(Text, nullable=True)
    response_b = Column(Text, nullable=True)
    metrics_a = Column(Text, nullable=True)         # JSON string
    metrics_b = Column(Text, nullable=True)         # JSON string
    winner = Column(String, nullable=True)           # "a", "b", "tie", or null
    is_blind = Column(Boolean, default=True)
    blind_mapping = Column(Text, nullable=True)      # JSON: {"left": "a"/"b", "right": "a"/"b"}
    voted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_comparisons_voted_at', 'voted_at'),
    )


class Signature(TimestampMixin, Base):
    """User-saved visual signatures (image stamps).

    Reusable across PDF form filling, email composition, and document editing.
    `data_png` is a base64-encoded PNG (no `data:` prefix). The SVG vector
    column is reserved for future smooth vector storage. Both are stored
    Fernet-encrypted at rest (see EncryptedText / src.secret_storage); a
    handwritten signature is sensitive, so it must never sit plaintext in the
    DB file. Existing rows are migrated automatically on startup.
    """
    __tablename__ = "signatures"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False, default="Signature")
    data_png = Column(EncryptedText, nullable=False)   # base64 PNG, encrypted at rest
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    svg = Column(EncryptedText, nullable=True)         # vector signature, encrypted at rest


class ApiToken(TimestampMixin, Base):
    """API tokens for external integrations (n8n, Make, etc.)."""
    __tablename__ = "api_tokens"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False)
    token_hash = Column(String, nullable=False)
    token_prefix = Column(String, nullable=False)  # first 8 chars for display
    scopes = Column(String, nullable=False, default="chat")
    is_active = Column(Boolean, default=True)
    last_used_at = Column(DateTime, nullable=True)


class Webhook(TimestampMixin, Base):
    """Outgoing webhooks fired on events."""
    __tablename__ = "webhooks"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    url = Column(String, nullable=False)
    secret = Column(String, nullable=True)  # HMAC-SHA256 signing secret
    events = Column(String, nullable=False)  # comma-separated event types
    is_active = Column(Boolean, default=True)
    last_triggered_at = Column(DateTime, nullable=True)
    last_status_code = Column(Integer, nullable=True)
    last_error = Column(String, nullable=True)


class UserTool(TimestampMixin, Base):
    """User-created sandboxed mini-apps/tools."""
    __tablename__ = "user_tools"

    id            = Column(String, primary_key=True, index=True)
    name          = Column(String, nullable=False)
    description   = Column(Text, nullable=True)
    icon          = Column(String, nullable=True, default="")
    html_content  = Column(Text, nullable=False)
    scope         = Column(String, nullable=False, default="global")  # "global" or session_id
    session_id    = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    owner         = Column(String, nullable=True, index=True)      # username
    is_pinned     = Column(Boolean, default=False)
    is_active     = Column(Boolean, default=True)
    version       = Column(Integer, default=1)
    author        = Column(String, nullable=True, default="ai")

    session = relationship("Session", backref=backref("user_tools", cascade="all, delete-orphan"))

    __table_args__ = (
        Index('ix_user_tools_scope', 'scope'),
        Index('ix_user_tools_active', 'is_active'),
    )


class UserToolData(Base):
    """Key-value storage for user tool persistent data."""
    __tablename__ = "user_tool_data"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    tool_id    = Column(String, ForeignKey("user_tools.id", ondelete="CASCADE"), nullable=False)
    key        = Column(String, nullable=False)
    value      = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tool = relationship("UserTool", backref=backref("data_entries", cascade="all, delete-orphan"))

    __table_args__ = (
        Index('ix_user_tool_data_tool_key', 'tool_id', 'key', unique=True),
    )


class CrewMember(TimestampMixin, Base):
    """A custom AI persona ('crew member') with its own personality, model, tools, and memory scope."""
    __tablename__ = "crew_members"

    id            = Column(String, primary_key=True, index=True)
    owner         = Column(String, nullable=True, index=True)
    name          = Column(String, nullable=False)
    avatar        = Column(String, nullable=True)
    user_name     = Column(String, nullable=True)          # what they call the user
    personality   = Column(Text, nullable=True)             # system prompt
    model         = Column(String, nullable=True)
    endpoint_url  = Column(String, nullable=True)
    greeting      = Column(Text, nullable=True)
    enabled_tools = Column(Text, nullable=True)             # JSON array or "all"
    session_id    = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    is_active     = Column(Boolean, default=True)
    sort_order    = Column(Integer, default=0)
    is_default_assistant = Column(Boolean, default=False)   # singleton per-owner "personal assistant"
    timezone      = Column(String, nullable=True)           # IANA tz name (e.g. "America/New_York") for scheduled check-ins

    session = relationship("Session", foreign_keys=[session_id],
                           backref=backref("crew_member", uselist=False))


class ScheduledTask(TimestampMixin, Base):
    """A recurring or one-off task — LLM-powered or direct action, time or event triggered."""
    __tablename__ = "scheduled_tasks"

    id             = Column(String, primary_key=True, index=True)
    owner          = Column(String, nullable=True, index=True)
    name           = Column(String, nullable=False, default="Untitled Task")
    prompt         = Column(Text, nullable=True)              # LLM prompt (for task_type="llm")
    task_type      = Column(String, default="llm")            # "llm" | "action"
    action         = Column(String, nullable=True)            # builtin action name (for task_type="action")
    schedule       = Column(String, nullable=True)            # "once", "daily", "weekly", "monthly"
    scheduled_time = Column(String, nullable=True)            # "HH:MM" (24h, stored UTC)
    scheduled_day  = Column(Integer, nullable=True)           # day-of-week 0=Mon for weekly, day-of-month for monthly
    scheduled_date = Column(DateTime, nullable=True)          # exact datetime for "once"
    trigger_type   = Column(String, default="schedule")       # "schedule" | "event"
    trigger_event  = Column(String, nullable=True)            # e.g. "session_created", "message_sent"
    trigger_count  = Column(Integer, nullable=True)           # fire every N events
    trigger_counter = Column(Integer, default=0)              # current count toward trigger_count
    next_run       = Column(DateTime, nullable=True, index=True)
    last_run       = Column(DateTime, nullable=True)
    status         = Column(String, default="active")         # "active", "paused", "completed"
    output_target  = Column(String, default="session")        # "session" (extensible later)
    session_id     = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    model          = Column(String, nullable=True)
    endpoint_url   = Column(String, nullable=True)
    run_count      = Column(Integer, default=0)

    cron_expression = Column(String, nullable=True)           # cron string e.g. "*/5 * * * *"
    then_task_id   = Column(String, ForeignKey("scheduled_tasks.id", ondelete="SET NULL"), nullable=True)
    webhook_token  = Column(String, nullable=True, unique=True)
    crew_member_id = Column(String, nullable=True)     # optional link to crew_members.id
    # character_id historically referenced an agent_characters table that was
    # never actually created. Keep the column for schema compatibility but
    # drop the ForeignKey so SQLAlchemy table sort doesn't fail on flush.
    character_id   = Column(String, nullable=True)
    max_steps      = Column(Integer, nullable=True)       # max agent loop iterations (null=unlimited)
    email_results  = Column(Boolean, default=True)        # email results to character.email_to
    notifications_enabled = Column(Boolean, default=True) # per-task on/off for completion notifications

    session = relationship("Session", backref=backref("scheduled_tasks", cascade="save-update, merge"))
    then_task = relationship("ScheduledTask", remote_side=[id], foreign_keys=[then_task_id])

    __table_args__ = (
        Index('ix_scheduled_tasks_due', 'status', 'next_run'),
        Index('ix_scheduled_tasks_event', 'trigger_type', 'trigger_event', 'status'),
    )


class EditorDraft(TimestampMixin, Base):
    """Persisted in-progress gallery-editor session — layered project state
    that the user can close and reopen later. Stores the full layer payload
    as JSON (with base64-encoded PNG dataURLs per layer) plus a small
    thumbnail for the landing-screen list.
    """
    __tablename__ = "editor_drafts"

    id              = Column(String, primary_key=True, index=True)
    owner           = Column(String, nullable=True, index=True)
    name            = Column(String, nullable=False, default="Untitled")
    # If the draft was opened FROM a gallery photo, point back at it so we
    # can show "Resuming edit of <photo>" and so reopening that photo picks
    # up the same draft rather than starting fresh.
    source_image_id = Column(String, nullable=True, index=True)
    width           = Column(Integer, nullable=True)
    height          = Column(Integer, nullable=True)
    # Full draft body — layer pixels (base64 PNG dataURLs), offsets,
    # opacities, visibility, active id, next id, etc. Kept as TEXT/JSON so
    # we don't have to re-shape the model every time the editor adds a
    # new piece of state.
    payload         = Column(Text, nullable=False, default="")
    # Tiny preview (data URL, ~128px wide) for the landing list. Stored
    # inline so the list endpoint can return everything in one shot.
    thumbnail       = Column(Text, nullable=True)
    is_active       = Column(Boolean, default=True)

    __table_args__ = (
        Index('ix_editor_drafts_owner_updated', 'owner', 'is_active', 'updated_at'),
    )


class TaskRun(Base):
    """Record of a single execution of a ScheduledTask."""
    __tablename__ = "task_runs"

    id          = Column(String, primary_key=True, index=True)
    task_id     = Column(String, ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), nullable=False)
    started_at  = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status      = Column(String, default="running")  # "running", "success", "error"
    result      = Column(Text, nullable=True)
    error       = Column(Text, nullable=True)
    tokens_used = Column(Integer, nullable=True)
    steps       = Column(Text, nullable=True)             # JSON log of agent tool calls
    model       = Column(String, nullable=True)           # model that actually ran (resolved at execution)

    task = relationship("ScheduledTask", backref=backref("runs", cascade="all, delete-orphan",
                        order_by="TaskRun.started_at.desc()"))

    __table_args__ = (
        Index('ix_task_runs_task', 'task_id', 'started_at'),
    )


class Memory(Base):
    """
    SQLAlchemy model for Memory table.
    Represents persistent memory entries with metadata.
    """
    __tablename__ = "memories"
    
    # Primary key
    id = Column(String, primary_key=True, index=True)
    
    # Memory content
    text = Column(Text, nullable=False)
    
    # Categorization
    category = Column(String, default='fact')
    source = Column(String, default='user')

    # Owner (username)
    owner = Column(String, nullable=True, index=True)

    # Reference to session (nullable)
    session_id = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)

    # Timestamp as Unix timestamp
    timestamp = Column(Integer, default=lambda: int(datetime.utcnow().timestamp()))

    # Relationship to Session
    session = relationship("Session", backref="memories")

    # Indexes - optimized composites
    __table_args__ = (
        Index('ix_memories_lookup', 'category', 'timestamp'),  # Composite for category-based queries
        Index('ix_memories_session', 'session_id', 'timestamp'),  # Composite for session-based queries
    )

# --- the first 29 schema-migration fns moved to core/db_migrations.py (ADR-059);
# re-imported so init_db() below (run at import) keeps calling them by name.
from core.db_migrations import (  # noqa: E402,F401
    _migrate_add_last_message_at_column,
    _migrate_add_document_archived_column,
    _migrate_add_owner_column,
    _migrate_model_endpoints,
    _migrate_add_hidden_models_column,
    _migrate_add_model_endpoint_owner_column,
    _migrate_add_model_type_column,
    _migrate_add_task_run_model_column,
    _migrate_add_supports_tools_column,
    _migrate_add_cached_models_column,
    _migrate_add_notes_sort_order,
    _migrate_add_mode_column,
    _migrate_add_folder_column,
    _migrate_add_token_columns,
    _migrate_add_owner_to_table,
    _migrate_add_multiuser_owner_columns,
    _migrate_add_api_token_scopes_column,
    _migrate_assign_legacy_owner,
    _migrate_backfill_document_owner_from_session,
    _migrate_add_tidy_verdict,
    _migrate_add_doc_source_email_cols,
    _migrate_add_task_automation_columns,
    _migrate_add_oauth_config,
    _migrate_add_disabled_tools,
    _migrate_add_task_v2_columns,
    _migrate_drop_ping_notes_tasks,
    _migrate_add_notifications_enabled,
    _migrate_add_crew_member_id,
    _migrate_add_assistant_columns,
)
class Note(TimestampMixin, Base):
    """A Google Keep-style note or checklist."""
    __tablename__ = "notes"

    id         = Column(String, primary_key=True, index=True)
    owner      = Column(String, nullable=True, index=True)
    title      = Column(String, default="")
    content    = Column(Text, nullable=True)
    items      = Column(Text, nullable=True)       # JSON string of [{text, done}]
    note_type  = Column(String, default="note")     # "note" or "checklist"
    color      = Column(String, nullable=True)
    label      = Column(String, nullable=True)
    pinned     = Column(Boolean, default=False)
    archived   = Column(Boolean, default=False)
    due_date   = Column(String, nullable=True)
    source     = Column(String, default="user")     # "user" or "agent"
    session_id = Column(String, nullable=True)
    sort_order = Column(Integer, default=0)
    image_url  = Column(String, nullable=True)      # uploaded image URL (relative path)
    repeat     = Column(String, default="none")     # none, daily, weekly, monthly, yearly
    # Auto-AI fields — populated by /api/notes/{id}/classify. The classification
    # JSON shape is { kind, solvable, confidence, task_prompt, tools, items?: [...] }.
    # Content hash gates re-classification (avoid LLM spend on every save).
    ai_classification = Column(Text, nullable=True)
    ai_content_hash   = Column(String, nullable=True)
    # Chat session spawned by the note's "Agent" button (solve-this-todo).
    # The note shows a clickable tag that opens this session for review.
    agent_session_id  = Column(String, nullable=True)


class CalendarCal(TimestampMixin, Base):
    """A calendar (e.g. 'Personal', 'TimeTree')."""
    __tablename__ = "calendars"

    id    = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    name  = Column(String, nullable=False)
    color = Column(String, default="#5b8abf")
    source = Column(String, default="local")  # "local" or "timetree"

    events = relationship("CalendarEvent", back_populates="calendar", cascade="all, delete-orphan")


class CalendarEvent(TimestampMixin, Base):
    """A calendar event."""
    __tablename__ = "calendar_events"

    uid         = Column(String, primary_key=True, index=True)
    calendar_id = Column(String, ForeignKey("calendars.id"), nullable=False, index=True)
    summary     = Column(String, nullable=False, default="")
    description = Column(Text, default="")
    location    = Column(String, default="")
    dtstart     = Column(DateTime, nullable=False, index=True)
    dtend       = Column(DateTime, nullable=False)
    all_day     = Column(Boolean, default=False)
    # True when dtstart/dtend are stored as UTC instants (set on import paths
    # that preserve the source TZID). False = legacy naive-local. Drives the
    # `Z`-suffix on serialization so the frontend interprets correctly.
    is_utc      = Column(Boolean, default=False, nullable=False)
    rrule       = Column(String, default="")
    color       = Column(String, nullable=True)  # per-event color override
    status      = Column(String, default="confirmed")  # confirmed, cancelled
    importance  = Column(String, default="normal")    # low | normal | high | critical
    event_type  = Column(String, nullable=True)        # work | personal | health | travel | meal | social | admin | other
    last_pinged = Column(DateTime, nullable=True)      # last time the assistant pinged about this event

    calendar = relationship("CalendarCal", back_populates="events")


class Integration(TimestampMixin, Base):
    """An external service connection (email, RSS, webhook, etc.)."""
    __tablename__ = "integrations"

    id     = Column(String, primary_key=True, index=True)
    owner  = Column(String, nullable=True, index=True)
    name   = Column(String, nullable=False)
    type   = Column(String, nullable=False)  # "email", "rss", "webhook"
    config = Column(JSON, nullable=True)     # type-specific config
    enabled = Column(Boolean, default=True)





def _migrate_seed_email_account():
    """If email_accounts is empty and settings.json has legacy flat imap_host/smtp_host
    keys, create a single default account from them so nothing breaks for users who
    upgraded. Safe to run repeatedly — it short-circuits once any row exists."""
    try:
        with engine.connect() as conn:
            tables = [r[0] for r in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='email_accounts'"
            ))]
            if "email_accounts" not in tables:
                return
            existing = conn.execute(text("SELECT COUNT(*) FROM email_accounts")).scalar() or 0
            if existing > 0:
                return

        import json as _json
        import uuid as _uuid
        from pathlib import Path
        settings_file = Path("data/settings.json")
        if not settings_file.exists():
            return
        try:
            s = _json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            return

        imap_host = (s.get("imap_host") or "").strip()
        smtp_host = (s.get("smtp_host") or "").strip()
        if not imap_host and not smtp_host:
            return  # nothing to migrate

        now = datetime.utcnow()
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO email_accounts
                  (id, owner, name, is_default, enabled,
                   imap_host, imap_port, imap_user, imap_password, imap_starttls,
                   smtp_host, smtp_port, smtp_user, smtp_password,
                   from_address, created_at, updated_at)
                VALUES
                  (:id, :owner, :name, :is_default, :enabled,
                   :imap_host, :imap_port, :imap_user, :imap_password, :imap_starttls,
                   :smtp_host, :smtp_port, :smtp_user, :smtp_password,
                   :from_address, :created_at, :updated_at)
            """), {
                "id": _uuid.uuid4().hex,
                "owner": None,
                "name": "Default",
                "is_default": True,
                "enabled": True,
                "imap_host": imap_host,
                "imap_port": int(s.get("imap_port") or 993),
                "imap_user": s.get("imap_user") or "",
                "imap_password": s.get("imap_password") or "",
                "imap_starttls": bool(s.get("imap_starttls", True)),
                "smtp_host": smtp_host,
                "smtp_port": int(s.get("smtp_port") or 465),
                "smtp_user": s.get("smtp_user") or "",
                "smtp_password": s.get("smtp_password") or "",
                "from_address": s.get("email_from") or "",
                "created_at": now,
                "updated_at": now,
            })
            logging.getLogger(__name__).info("Seeded email_accounts 'Default' from settings.json")
    except Exception as e:
        logging.getLogger(__name__).warning(f"seed email account migration: {e}")


def init_db():
    """
    Initialize the database by creating all tables.
    Should be called when starting the application.
    """
    _migrate_model_endpoints()
    Base.metadata.create_all(bind=engine)
    _migrate_add_hidden_models_column()
    _migrate_add_cached_models_column()
    _migrate_add_notes_sort_order()
    _migrate_add_model_type_column()
    _migrate_add_model_endpoint_owner_column()
    _migrate_add_supports_tools_column()
    _migrate_add_task_run_model_column()
    _migrate_add_owner_column()
    _migrate_add_document_archived_column()
    _migrate_add_last_message_at_column()
    _migrate_add_folder_column()
    _migrate_add_token_columns()
    _migrate_add_mode_column()
    _migrate_add_multiuser_owner_columns()
    _migrate_add_api_token_scopes_column()
    _migrate_backfill_document_owner_from_session()
    _migrate_assign_legacy_owner()
    _migrate_add_tidy_verdict()
    _migrate_add_doc_source_email_cols()
    _migrate_add_oauth_config()
    _migrate_add_task_automation_columns()
    _migrate_add_disabled_tools()
    _migrate_add_task_v2_columns()
    _migrate_add_notifications_enabled()
    _migrate_drop_ping_notes_tasks()
    _migrate_add_crew_member_id()
    _migrate_add_assistant_columns()
    _migrate_seed_email_account()
    _migrate_add_calendar_metadata()
    _migrate_add_calendar_is_utc()
    _migrate_encrypt_email_passwords()
    _migrate_encrypt_signatures()
    _migrate_encrypt_endpoint_keys()


def _migrate_encrypt_endpoint_keys():
    """Encrypt any plaintext provider API keys in model_endpoints. Idempotent;
    raw SQL so the EncryptedText decorator isn't applied twice."""
    try:
        from src.secret_storage import encrypt, is_encrypted
    except Exception as e:
        logger.warning(f"secret_storage import failed; skipping endpoint-key migration: {e}")
        return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, api_key FROM model_endpoints")).fetchall()
            migrated = 0
            for rid, key in rows:
                if key and not is_encrypted(key):
                    conn.execute(text("UPDATE model_endpoints SET api_key = :k WHERE id = :id"),
                                 {"k": encrypt(key), "id": rid})
                    migrated += 1
            if migrated:
                conn.commit()
                logger.info(f"Encrypted plaintext API key on {migrated} endpoint row(s)")
    except Exception as e:
        logger.warning(f"Endpoint-key encryption migration skipped: {e}")


def _migrate_encrypt_signatures():
    """Encrypt any plaintext signature images still in the signatures table.
    Idempotent — rows already prefixed with `enc:` are skipped. Uses raw SQL
    so the EncryptedText type decorator isn't applied twice."""
    try:
        from src.secret_storage import encrypt, is_encrypted
    except Exception as e:
        logger.warning(f"secret_storage import failed; skipping signature migration: {e}")
        return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, data_png, svg FROM signatures"
            )).fetchall()
            migrated = 0
            for rid, data_png, svg in rows:
                updates = {}
                if data_png and not is_encrypted(data_png):
                    updates["data_png"] = encrypt(data_png)
                if svg and not is_encrypted(svg):
                    updates["svg"] = encrypt(svg)
                if updates:
                    sets = ", ".join(f"{k} = :{k}" for k in updates)
                    conn.execute(text(f"UPDATE signatures SET {sets} WHERE id = :id"), {**updates, "id": rid})
                    migrated += 1
            if migrated:
                conn.commit()
                logger.info(f"Encrypted plaintext signature(s) on {migrated} row(s)")
    except Exception as e:
        logger.warning(f"Signature encryption migration skipped: {e}")


def _migrate_encrypt_email_passwords():
    """Encrypt any plaintext IMAP/SMTP passwords still in the email_accounts
    table. Idempotent — rows already prefixed with `enc:` are skipped.
    Safe to run on every startup."""
    try:
        from src.secret_storage import encrypt, is_encrypted
    except Exception as e:
        logger.warning(f"secret_storage import failed; skipping password migration: {e}")
        return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, imap_password, smtp_password FROM email_accounts"
            )).fetchall()
            migrated = 0
            for row in rows:
                rid, imap_pw, smtp_pw = row
                updates = {}
                if imap_pw and not is_encrypted(imap_pw):
                    updates["imap_password"] = encrypt(imap_pw)
                if smtp_pw and not is_encrypted(smtp_pw):
                    updates["smtp_password"] = encrypt(smtp_pw)
                if updates:
                    sets = ", ".join(f"{k} = :{k}" for k in updates)
                    params = {**updates, "id": rid}
                    conn.execute(text(f"UPDATE email_accounts SET {sets} WHERE id = :id"), params)
                    migrated += 1
            if migrated:
                conn.commit()
                logger.info(f"Encrypted plaintext passwords on {migrated} email account row(s)")
    except Exception as e:
        logger.warning(f"Password migration failed (will retry next start): {e}")


def _migrate_add_calendar_is_utc():
    """Add is_utc column to calendar_events so imported events can preserve
    their original UTC timestamps (Z-suffix on the wire) without touching
    legacy naive-local rows."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(calendar_events)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "is_utc" not in columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN is_utc BOOLEAN DEFAULT 0 NOT NULL")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'is_utc' column to calendar_events")
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"is_utc migration failed: {e}")


def _migrate_add_calendar_metadata():
    """Add importance/event_type/last_pinged columns to calendar_events table."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(calendar_events)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "importance" not in columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN importance TEXT DEFAULT 'normal'")
        if columns and "event_type" not in columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN event_type TEXT")
        if columns and "last_pinged" not in columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN last_pinged DATETIME")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"calendar_events migration failed: {e}")

def get_db():
    """
    Dependency to get a database session.
    Used in FastAPI routes to inject database sessions.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

from contextlib import contextmanager
from typing import Generator

@contextmanager
def get_db_session() -> Generator:
    """Context manager for database sessions"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

def bulk_insert_messages(session_id: str, messages: list):
    """Efficiently insert multiple messages"""
    with get_db_session() as db:
        db.bulk_insert_mappings(
            ChatMessage,
            [
                {
                    'session_id': session_id,
                    'role': msg['role'],
                    'content': msg['content'],
                    'timestamp': datetime.utcnow()
                }
                for msg in messages
            ]
        )

def cleanup_old_sessions(days: int = 30):
    """Remove sessions older than specified days"""
    from datetime import timedelta
    
    with get_db_session() as db:
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        
        deleted_count = db.query(Session).filter(
            Session.archived == True,
            Session.last_accessed < cutoff_date,
            Session.is_important == False
        ).delete()
        
        return deleted_count

def get_session_stats():
    """Get database statistics"""
    with get_db_session() as db:
        stats = {
            'total_sessions': db.query(Session).count(),
            'active_sessions': db.query(Session).filter(Session.archived == False).count(),
            'archived_sessions': db.query(Session).filter(Session.archived == True).count(),
            'total_messages': db.query(ChatMessage).count(),
            'total_memories': db.query(Memory).count()
        }
        return stats

def get_detailed_stats():
    """Get comprehensive database statistics including file size"""
    stats = get_session_stats()  # Use existing function
    
    # Add database file size
    db_size_mb = 0.0
    if "sqlite" in DATABASE_URL:
        db_path = DATABASE_URL.replace("sqlite:///", "")
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)
        
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
            db_size_mb = round(db_size / (1024 * 1024), 2)
    
    stats['database_size_mb'] = db_size_mb
    return stats

def update_session_last_accessed(session_id: str):
    """Update the last_accessed timestamp for a session"""
    with get_db_session() as db:
        db_session = db.query(Session).filter(Session.id == session_id).first()
        if db_session:
            db_session.last_accessed = datetime.utcnow()
            db.commit()
            return True
    return False

def get_session_mode(session_id: str):
    """Return a session's persisted `mode`, or None if unset/unknown.

    Best-effort: never raises (returns None on any DB error) so callers on hot
    request paths needn't guard it. Routed through get_db_session() so the
    connection is always returned to the pool."""
    try:
        with get_db_session() as db:
            return db.query(Session.mode).filter(Session.id == session_id).scalar()
    except Exception:
        logger.warning("Failed to read mode for session %s", session_id)
        return None

def set_session_mode(session_id: str, mode: str) -> bool:
    """Persist a session's `mode`. Best-effort: never raises, returns success.

    Routed through get_db_session() so a failure mid-write (e.g. a SQLite
    'database is locked' under concurrent streams) still returns the connection
    to the pool instead of leaking it — repeated leaks would exhaust it."""
    try:
        with get_db_session() as db:
            db.query(Session).filter(Session.id == session_id).update({"mode": mode})
        return True
    except Exception:
        logger.warning("Failed to persist mode %r for session %s", mode, session_id)
        return False

def get_session_by_id(session_id: str):
    """Get a session by ID"""
    with get_db_session() as db:
        return db.query(Session).filter(Session.id == session_id).first()

def archive_session(session_id: str):
    """Archive a session"""
    with get_db_session() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if session:
            session.archived = True
            db.commit()
            return True
    return False

# Initialize the database by creating all tables


init_db()
