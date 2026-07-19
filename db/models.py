from sqlalchemy import (
    Column, Integer, BigInteger, String,
    Boolean, DateTime, ForeignKey, Text,
    Index, UniqueConstraint
)
from sqlalchemy.orm import declarative_base
from datetime import datetime, timezone

Base = declarative_base()

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    """
    Глобальный профиль пользователя.
    """
    __tablename__ = "users"

    user_id   = Column(BigInteger, primary_key=True)   # Telegram ID
    username  = Column(String(255), nullable=True)
    full_name = Column(String(255))
    created_at = Column(DateTime, default=_utcnow)


class Channel(Base):
    """
    Канал, зарегистрированный в боте.
    """
    __tablename__ = "channels"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(BigInteger, unique=True, nullable=False)
    title      = Column(String(255))
    owner_id   = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    is_active  = Column(Boolean, default=True)
    timezone   = Column(String(50), default="Europe/Moscow")


class ChannelUser(Base):
    """
    Связка пользователь <-> канал.
    internal_id — уникальный 9-значный номер внутри канала.
    """
    __tablename__ = "channel_users"

    channel_id = Column(
        BigInteger,
        ForeignKey("channels.channel_id", ondelete="CASCADE"),
        primary_key=True
    )
    user_id = Column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True
    )

    internal_id = Column(String(20), nullable=False)
    role       = Column(String(50), default="user")
    joined_at  = Column(DateTime, default=_utcnow)

    last_post_time = Column(DateTime, nullable=True)
    last_edit_time = Column(DateTime, nullable=True)

    posts_today      = Column(Integer, default=0)
    posts_today_date = Column(DateTime, nullable=True)

    edits_today      = Column(Integer, default=0)
    edits_today_date = Column(DateTime, nullable=True)

    deleted_by_admin_count = Column(Integer, default=0)

    __table_args__ = (
        Index("ix_channel_user_role", "channel_id", "role"),
        UniqueConstraint("channel_id", "internal_id", name="uq_channel_internal_id"),
    )


class Post(Base):
    __tablename__ = "posts"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    post_number = Column(Integer, nullable=False)

    channel_id = Column(
        BigInteger,
        ForeignKey("channels.channel_id", ondelete="CASCADE"),
        nullable=False
    )
    user_id = Column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False
    )

    message_id     = Column(BigInteger)
    media_type     = Column(String(20), default="text")  # text / photo / video / document / animation
    file_ids       = Column(Text, nullable=True)          # file_id для медиа
    text           = Column(Text, nullable=True)          # текст или caption

    created_at    = Column(DateTime, default=_utcnow)
    last_edited_at = Column(DateTime, nullable=True)

    is_deleted    = Column(Boolean, default=False)
    deleted_by    = Column(BigInteger, nullable=True)
    delete_reason = Column(Text, nullable=True)
    deleted_at    = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("channel_id", "user_id", "post_number",
                         name="uq_channel_user_post_number"),
        Index("ix_posts_channel_created", "channel_id", "created_at"),
    )


class Ban(Base):
    __tablename__ = "bans"

    id = Column(Integer, primary_key=True, autoincrement=True)

    channel_id = Column(
        BigInteger,
        ForeignKey("channels.channel_id", ondelete="CASCADE"),
        nullable=False
    )
    user_id = Column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False
    )

    banned_by   = Column(BigInteger, nullable=True)
    ban_until   = Column(DateTime, nullable=False)
    reason      = Column(Text, nullable=True)
    created_at  = Column(DateTime, default=_utcnow)
    notified_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_bans_active", "channel_id", "user_id", "ban_until"),
    )


class Notification(Base):
    __tablename__ = "notifications"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    sender_id   = Column(BigInteger, nullable=True)   # Telegram ID of sender (None for system)
    receiver_id = Column(BigInteger, nullable=False)  # Telegram ID of receiver
    channel_id  = Column(BigInteger, nullable=True)   # Context channel
    text        = Column(Text, nullable=False)         # Content
    is_read     = Column(Boolean, default=False)       # Read status
    created_at  = Column(DateTime, default=_utcnow)
    read_at     = Column(DateTime, nullable=True)
    post_id     = Column(Integer, nullable=True)       # For report notifications
    accepted_by = Column(BigInteger, nullable=True)   # Telegram ID of admin who took the report