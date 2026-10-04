"""All configuration in one place.

Precedence (clig.dev): CLI flags > environment > ./warden.toml > ~/.config/warden/config.toml.
Every setting is validated at startup; a missing required key exits with the variable name.
"""

from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Role(StrEnum):
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    TOOLS = "tools"
    VERDICT = "verdict"


def _load_toml_layers() -> dict[str, Any]:
    """Merge user config then project config (project wins)."""
    merged: dict[str, Any] = {}
    for path in (
        Path.home() / ".config" / "warden" / "config.toml",
        Path.cwd() / "warden.toml",
    ):
        if path.is_file():
            with path.open("rb") as fh:
                merged.update(tomllib.load(fh))
    return merged


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Nebius Token Factory
    nebius_api_key: str = Field(default="", alias="NEBIUS_API_KEY")
    tokenfactory_base_url: str = Field(
        default="https://api.tokenfactory.nebius.com/v1", alias="TOKENFACTORY_BASE_URL"
    )
    # Sandboxes (Contree)
    contree_base_url: str = Field(default="", alias="CONTREE_BASE_URL")
    nebius_project_id: str = Field(default="", alias="NEBIUS_PROJECT_ID")
    # Research
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    # Models
    planner_model: str = Field(
        default="nvidia/Nemotron-3-Ultra-550b-a55b", alias="WARDEN_PLANNER_MODEL"
    )
    implementer_model: str = Field(
        default="nvidia/nemotron-3-super-120b-a12b", alias="WARDEN_IMPLEMENTER_MODEL"
    )
    tools_model: str = Field(default="nvidia/Nemotron-3_5-Lightning", alias="WARDEN_TOOLS_MODEL")
    # Limits
    max_forks: int = Field(default=4, alias="WARDEN_MAX_FORKS")
    max_usd_per_issue: float = Field(default=3.0, alias="WARDEN_MAX_USD_PER_ISSUE")
    max_tool_turns: int = Field(default=25, alias="WARDEN_MAX_TOOL_TURNS")
    base_image: str = Field(default="warden/fabric-base:latest", alias="WARDEN_BASE_IMAGE")
    target_repo: str = Field(
        default="moiz-lakkadkutta/Fabric-Inventory", alias="WARDEN_TARGET_REPO"
    )
    runs_dir: Path = Field(default=Path("runs"), alias="WARDEN_RUNS_DIR")

    def model_for(self, role: Role) -> str:
        return {
            Role.PLANNER: self.planner_model,
            Role.IMPLEMENTER: self.implementer_model,
            Role.TOOLS: self.tools_model,
            Role.VERDICT: self.planner_model,
        }[role]

    def routing_table(self) -> list[tuple[str, str]]:
        return [(role.value, self.model_for(role)) for role in Role]

    def missing_for(self, *keys: str) -> list[str]:
        """Return the env var names among `keys` that are empty."""
        alias_to_value = {
            "NEBIUS_API_KEY": self.nebius_api_key,
            "CONTREE_BASE_URL": self.contree_base_url,
            "NEBIUS_PROJECT_ID": self.nebius_project_id,
            "TAVILY_API_KEY": self.tavily_api_key,
        }
        return [k for k in keys if not alias_to_value.get(k)]


def load_settings() -> Settings:
    """Environment wins over TOML layers; TOML fills gaps."""
    layers = _load_toml_layers()
    for key, value in layers.items():
        env_key = (
            key.upper()
            if key.upper().startswith(
                ("WARDEN_", "NEBIUS_", "TAVILY_", "CONTREE_", "TOKENFACTORY_")
            )
            else f"WARDEN_{key.upper()}"
        )
        os.environ.setdefault(env_key, str(value))
    return Settings()
