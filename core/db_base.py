"""Foundational SQLAlchemy primitives for the database layer (ADR-058, Phase 2.2).

Base (declarative_base), TimestampMixin, DATABASE_URL, engine, SessionLocal and the
EncryptedText column type, split out of core/database.py into a DEPENDENCY-FREE leaf
so the ORM models + the migrations (which use engine/Base/text) + the ~46 external
importers all share the SAME objects via re-export (identity-preserving), and so the
migrations can later move to core/db_migrations.py without a cycle. This module
imports nothing from core.database; EncryptedText's encrypt/decrypt are lazy-imported
inside its methods.
"""

import os
from datetime import datetime

from sqlalchemy import create_engine, Column, Text, DateTime
from sqlalchemy.types import TypeDecorator
from sqlalchemy.ext.declarative import declarative_base, declared_attr
from sqlalchemy.orm import sessionmaker


# Create base class for declarative models
Base = declarative_base()

class TimestampMixin:
    """Mixin that adds timestamp fields to models"""
    @declared_attr
    def created_at(cls):
        return Column(DateTime, default=datetime.utcnow, nullable=False)
    
    @declared_attr
    def updated_at(cls):
        return Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

# Get database URL from environment, default to SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")

# Create engine
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class EncryptedText(TypeDecorator):
    """Text column transparently encrypted at rest via src.secret_storage.

    Writes are Fernet-encrypted (`enc:` prefix); reads decrypt back to
    plaintext, so all consumers use the column normally. Legacy plaintext
    rows pass through unchanged until their next write (a startup migration
    encrypts them). Protects the SQLite file at rest (stolen backup / leaked
    image), not a live process that can read the key.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from src.secret_storage import encrypt
        return encrypt(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        from src.secret_storage import decrypt
        return decrypt(value)
