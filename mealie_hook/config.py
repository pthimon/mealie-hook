"""Configuration from the environment (and an optional .env file)."""

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mealie_url: str = "http://mealie:9000/api"
    mealie_token: str = ""
    mealie_token_file: str = ""
    llm_url: str = "http://host.containers.internal:8080/v1"
    llm_model: str = "qwen"
    llm_timeout: int = 600
    data_dir: Path = Path("/data")
    # Shipped defaults; copied into DATA_DIR/rules on first start, which is the live copy.
    default_rules_dir: Path = ROOT / "rules"
    port: int = 8000
    # Wait this long after an event before sweeping, so a burst of imports is handled as one
    # sweep and a human who opens the recipe straight after importing gets a head start.
    settle_seconds: int = 90
    # Safety-net sweep, for events that were lost (service down, notifier misfire).
    sweep_minutes: int = 30
    # Only recipes created at or after this ISO timestamp are candidates. Empty means "the
    # first time the service ran", recorded in state.json, so the existing hand-curated
    # collection is never reprocessed.
    process_since: str = ""
    review_tag: str = "Needs review"
    # Home Assistant's shopping list, shown and ticked alongside Mealie's. Optional.
    ha_url: str = ""
    ha_token: str = ""
    max_attempts: int = 3
    dry_run: bool = False

    @model_validator(mode="after")
    def _token(self):
        self.mealie_url = self.mealie_url.rstrip("/")
        self.llm_url = self.llm_url.rstrip("/")
        if not self.mealie_token and self.mealie_token_file:
            self.mealie_token = Path(self.mealie_token_file).expanduser().read_text().strip()
        return self

    @property
    def rules_dir(self) -> Path:
        return self.data_dir / "rules"
