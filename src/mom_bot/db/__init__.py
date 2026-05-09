"""SQLAlchemy declarative base for mom-bot.

This module establishes the shared ``Base`` that all ORM models in Epic 1+
must inherit from.  It intentionally contains no models — they will be added
as the feature epics progress.

Usage::

    from mom_bot.db import Base

    class MyModel(Base):
        __tablename__ = "my_model"
        ...
"""

from sqlalchemy.orm import DeclarativeBase

__all__ = ["Base"]


class Base(DeclarativeBase):
    """Shared declarative base for all mom-bot ORM models.

    All SQLAlchemy model classes should inherit from this ``Base`` so that
    Alembic can discover them via ``Base.metadata`` and generate accurate
    autogenerate migrations.

    This class is intentionally empty — model definitions live in their
    respective feature modules (Epic 1+).
    """
