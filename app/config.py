from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
    JWT_SECRET: str = "change-me"
    JWT_EXPIRE_MINUTES: int = 720
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    ADMIN_USERNAME: str = "GOLDADMIN"
    ADMIN_PASSWORD: str = "Admin@123"
    ADMIN_TXN_PASSWORD: str = "12345678"
    SEED_DEMO_DATA: bool = True

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
