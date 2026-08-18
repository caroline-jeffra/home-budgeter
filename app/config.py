"""Configuration for the project."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from the environment or a local .env file.

    Fields have no defaults: a missing variable fails at import with a
    validation error naming the field, rather than reaching the driver as None."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str


# pydantic fills database url from env at import
settings = Settings() # type: ignore[call-arg]
