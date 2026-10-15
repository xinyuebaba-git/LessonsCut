from pathlib import Path
from functools import lru_cache
from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    admin_token: SecretStr = Field(default=SecretStr(""), validation_alias="LESSONSCUT_ADMIN_TOKEN")
    database_url: str = Field(default="sqlite:///./data/lessonscut.db", validation_alias="LESSONSCUT_DATABASE_URL")
    data_dir: Path = Field(default=Path("data"), validation_alias="LESSONSCUT_DATA_DIR")
    input_root: Path = Field(default=Path("data/input"), validation_alias="LESSONSCUT_INPUT_ROOT")
    qwen_base_url: str = "http://192.168.14.12:30000/v1"
    qwen_model: str = "qwen38-27b-uncensored-aggressive"
    qwen_api_key: SecretStr = SecretStr("")
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_supervisor_model: str = "MiniMax-M3"
    minimax_api_key: SecretStr = SecretStr("")
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    dashscope_api_key: SecretStr = SecretStr("")
    qwen_image_model: str = "qwen-image-3.0"
    qwen_image_size: str = "1920x1080"
    max_calls_per_job: int = Field(default=48, ge=1, le=500, validation_alias="LESSONSCUT_MAX_CALLS_PER_JOB")
    encoder: str = Field(default="libx264", pattern="^(libx264|h264_nvenc)$", validation_alias="LESSONSCUT_ENCODER")

    def prepare(self):
        for path in [self.data_dir, self.input_root, self.data_dir / "uploads", self.data_dir / "work", self.data_dir / "output"]:
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings():
    settings = Settings()
    settings.prepare()
    return settings
