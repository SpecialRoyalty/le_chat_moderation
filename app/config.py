from __future__ import annotations
from functools import lru_cache
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')
    bot_token: str = Field(alias='BOT_TOKEN')
    database_url: str = Field(alias='DATABASE_URL')
    # String fields to avoid pydantic-settings JSON decoding errors on Railway.
    # Accepted values: empty, comma-separated ('123,456'), JSON-like ('[123,456]').
    admin_ids: str = Field(default='', alias='ADMIN_IDS')
    trusted_ids: str = Field(default='', alias='TRUSTED_IDS')
    main_group_id: int = Field(alias='MAIN_GROUP_ID')
    log_group_id: Optional[int] = Field(default=None, alias='LOG_GROUP_ID')
    public_bot_username: str = Field(default='', alias='PUBLIC_BOT_USERNAME')
    timezone: str = Field(default='Europe/Paris', alias='TIMEZONE')
    default_vote_goal: int = Field(default=120, alias='DEFAULT_VOTE_GOAL')
    default_time_slot: str = Field(default='22:30-00:45', alias='DEFAULT_TIME_SLOT')
    auto_schedule_enabled: bool = Field(default=True, alias='AUTO_SCHEDULE_ENABLED')

    @field_validator('database_url', mode='before')
    @classmethod
    def fix_db_url(cls, v):
        if isinstance(v, str):
            if v.startswith('postgres://'):
                v = 'postgresql://' + v[len('postgres://'):]
            if v.startswith('postgresql://'):
                v = 'postgresql+asyncpg://' + v[len('postgresql://'):]
        return v

    @staticmethod
    def _parse_ids(v) -> list[int]:
        if v is None:
            return []
        if isinstance(v, int):
            return [v]
        if isinstance(v, list):
            return [int(x) for x in v if str(x).strip()]
        if isinstance(v, str):
            raw = v.strip()
            if not raw:
                return []
            if raw.startswith('[') and raw.endswith(']'):
                raw = raw[1:-1]
            return [int(x.strip().strip('\"\'')) for x in raw.split(',') if x.strip().strip('\"\'')]
        return []

    @field_validator('log_group_id', mode='before')
    @classmethod
    def empty_int(cls, v):
        if v is None or v == '': return None
        return int(v)

    @property
    def all_admin_ids(self) -> set[int]:
        return set(self._parse_ids(self.admin_ids)) | set(self._parse_ids(self.trusted_ids))

@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore
