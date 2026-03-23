import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Enum,
    ForeignKey,
    JSON,
)
from sqlalchemy.orm import relationship

from app.database import Base


class MeetingSource(str, enum.Enum):
    zoom = "zoom"
    upload = "upload"


class MeetingStatus(str, enum.Enum):
    downloading = "downloading"
    transcribing = "transcribing"
    summarizing = "summarizing"
    ready = "ready"
    error = "error"


class ChatRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    folders = relationship("Folder", back_populates="user", cascade="all, delete-orphan")
    meetings = relationship("Meeting", back_populates="user", cascade="all, delete-orphan")


class Folder(Base):
    __tablename__ = "folders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(255), nullable=False)
    icon = Column(String(10), default="📁")
    keywords = Column(String(1000), default="")
    created_at = Column(DateTime, default=utcnow)

    user = relationship("User", back_populates="folders")
    meetings = relationship("Meeting", back_populates="folder", cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", back_populates="folder", cascade="all, delete-orphan")


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    folder_id = Column(Integer, ForeignKey("folders.id"), nullable=True)
    title = Column(String(500), nullable=False)
    date = Column(DateTime, default=utcnow)
    duration_seconds = Column(Integer, default=0)
    source = Column(Enum(MeetingSource), default=MeetingSource.upload)
    zoom_meeting_id = Column(String(255), nullable=True)
    audio_path = Column(String(1000), nullable=True)
    status = Column(Enum(MeetingStatus), default=MeetingStatus.downloading)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    user = relationship("User", back_populates="meetings")
    folder = relationship("Folder", back_populates="meetings")
    transcript = relationship("Transcript", back_populates="meeting", uselist=False, cascade="all, delete-orphan")
    summary = relationship("Summary", back_populates="meeting", uselist=False, cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", back_populates="meeting", cascade="all, delete-orphan")


class Transcript(Base):
    __tablename__ = "transcripts"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, unique=True)
    full_text = Column(Text, nullable=False, default="")
    segments = Column(JSON, default=list)

    meeting = relationship("Meeting", back_populates="transcript")


class Summary(Base):
    __tablename__ = "summaries"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, unique=True)
    tldr = Column(Text, default="")
    tasks = Column(JSON, default=list)
    topics = Column(JSON, default=list)
    insights = Column(JSON, default=list)
    raw_response = Column(Text, default="")

    meeting = relationship("Meeting", back_populates="summary")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=True)
    folder_id = Column(Integer, ForeignKey("folders.id"), nullable=True)
    role = Column(Enum(ChatRole), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)

    meeting = relationship("Meeting", back_populates="chat_messages")
    folder = relationship("Folder", back_populates="chat_messages")
