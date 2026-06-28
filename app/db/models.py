from __future__ import annotations
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase): pass

class Setting(Base):
    __tablename__='settings_test'
    key: Mapped[str]=mapped_column(String(120), primary_key=True)
    value: Mapped[str]=mapped_column(Text, default='')

class User(Base):
    __tablename__='users_test'
    id: Mapped[int]=mapped_column(BigInteger, primary_key=True)
    username: Mapped[str|None]=mapped_column(String(255), nullable=True)
    full_name: Mapped[str]=mapped_column(String(512), default='')
    is_admin: Mapped[bool]=mapped_column(Boolean, default=False)
    is_trusted: Mapped[bool]=mapped_column(Boolean, default=False)
    is_banned: Mapped[bool]=mapped_column(Boolean, default=False)
    is_restricted: Mapped[bool]=mapped_column(Boolean, default=False)
    media_count: Mapped[int]=mapped_column(Integer, default=0)
    last_media_session: Mapped[int]=mapped_column(Integer, default=0)
    sessions_present: Mapped[int]=mapped_column(Integer, default=0)
    sessions_with_media: Mapped[int]=mapped_column(Integer, default=0)
    suspect_score: Mapped[int]=mapped_column(Integer, default=0)
    reward_counter: Mapped[int]=mapped_column(Integer, default=0)
    total_invites: Mapped[int]=mapped_column(Integer, default=0)
    weekly_invites: Mapped[int]=mapped_column(Integer, default=0)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow)
    last_seen: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow)

class SessionLog(Base):
    __tablename__='sessions_test'
    id: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int]=mapped_column(BigInteger, index=True)
    kind: Mapped[str]=mapped_column(String(20), default='auto')
    opened_at: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime|None]=mapped_column(DateTime, nullable=True)
    status: Mapped[str]=mapped_column(String(20), default='open')
    messages_seen: Mapped[int]=mapped_column(Integer, default=0)
    media_seen: Mapped[int]=mapped_column(Integer, default=0)
    messages_deleted: Mapped[int]=mapped_column(Integer, default=0)

class Vote(Base):
    __tablename__='votes_test'
    id: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int]=mapped_column(BigInteger, index=True)
    user_id: Mapped[int]=mapped_column(BigInteger, index=True)
    day_key: Mapped[str]=mapped_column(String(20), index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow)
    __table_args__=(UniqueConstraint('chat_id','user_id','day_key', name='uq_vote_day_test'),)

class TrackedMessage(Base):
    __tablename__='tracked_messages_test'
    id: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int]=mapped_column(BigInteger, index=True)
    message_id: Mapped[int]=mapped_column(Integer, index=True)
    user_id: Mapped[int|None]=mapped_column(BigInteger, nullable=True)
    session_id: Mapped[int]=mapped_column(Integer, default=0, index=True)
    kind: Mapped[str]=mapped_column(String(30), default='message')
    is_media: Mapped[bool]=mapped_column(Boolean, default=False)
    deleted: Mapped[bool]=mapped_column(Boolean, default=False)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow)
    __table_args__=(UniqueConstraint('chat_id','message_id', name='uq_tracked_message_test'),)

class MediaHash(Base):
    __tablename__='media_hashes_test'
    id: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int|None]=mapped_column(BigInteger, nullable=True, index=True)
    file_unique_id: Mapped[str]=mapped_column(String(255), index=True)
    file_id: Mapped[str]=mapped_column(Text, default='')
    media_type: Mapped[str]=mapped_column(String(30), default='unknown')
    banned: Mapped[bool]=mapped_column(Boolean, default=False)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow)
    __table_args__=(Index('ix_hash_banned_unique_test','file_unique_id','banned'),)

class TrustedAction(Base):
    __tablename__='trusted_actions_test'
    id: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    trusted_user_id: Mapped[int]=mapped_column(BigInteger, index=True)
    trusted_username: Mapped[str]=mapped_column(String(255), default='')
    command: Mapped[str]=mapped_column(String(50), default='')
    target_user_id: Mapped[int|None]=mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow)

class InviteLink(Base):
    __tablename__='invite_links_test'
    id: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int]=mapped_column(BigInteger, index=True)
    link: Mapped[str]=mapped_column(Text, default='')
    active: Mapped[bool]=mapped_column(Boolean, default=True)
    valid_count: Mapped[int]=mapped_column(Integer, default=0)
    suspect_count: Mapped[int]=mapped_column(Integer, default=0)
    banned_count: Mapped[int]=mapped_column(Integer, default=0)

class WordRule(Base):
    __tablename__='word_rules_test'
    id: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str]=mapped_column(String(30), index=True) # forbidden, ban, nameban
    word: Mapped[str]=mapped_column(String(255), index=True)

class ErrorLog(Base):
    __tablename__='error_logs_test'
    id: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    area: Mapped[str]=mapped_column(String(80), default='')
    message: Mapped[str]=mapped_column(Text, default='')
    created_at: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow)


class Advertisement(Base):
    __tablename__='advertisements_test'
    id: Mapped[int]=mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str]=mapped_column(String(255), default='Pub')
    text: Mapped[str]=mapped_column(Text, default='')
    image_file_id: Mapped[str|None]=mapped_column(Text, nullable=True)
    button_text: Mapped[str|None]=mapped_column(String(255), nullable=True)
    button_url: Mapped[str|None]=mapped_column(Text, nullable=True)
    active: Mapped[bool]=mapped_column(Boolean, default=True)
    created_at: Mapped[datetime]=mapped_column(DateTime, default=datetime.utcnow)
