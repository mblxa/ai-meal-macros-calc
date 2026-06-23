from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_token: str
    openai_api_key: str
    database_path: str = "calories.db"
    garmin_token_dir: str = "~/.garminconnect"

    model_config = {"env_file": ".env"}


settings = Settings()
