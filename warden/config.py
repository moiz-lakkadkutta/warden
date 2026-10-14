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

MAX_THEATER_RETRIES = 3  # doctor checks WARDEN_THEATER_RETRIES against it
MAX_CANDIDATES = 8  # best-of-N upper bound (doctor checks WARDEN_CANDIDATES against it)


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
    # icalendar#1928 needed 52 turns to finish; 25 and 40 both ran out mid-fix
    max_tool_turns: int = Field(default=60, alias="WARDEN_MAX_TOOL_TURNS")
    # Implementer read-loop guards: after this many tool turns in a row without write_file or
    # run_shell the loop injects a "now edit or run tests" nudge (0 = never)
    idle_nudge_after: int = Field(default=8, ge=0, alias="WARDEN_IDLE_NUDGE_AFTER")
    # Re-reading an unchanged file/range still in the model's context gets a short "you already
    # have it" reply instead of the content again
    read_cache: bool = Field(default=True, alias="WARDEN_READ_CACHE")
    base_image: str = Field(default="warden/fabric-base:latest", alias="WARDEN_BASE_IMAGE")
    workdir: str = Field(default="/src", alias="WARDEN_WORKDIR")
    gate_profile: str = Field(default="fabric", alias="WARDEN_GATE_PROFILE")
    shell_wrap: str = Field(default="with-services sh -c", alias="WARDEN_SHELL_WRAP")
    target_repo: str = Field(
        default="moiz-lakkadkutta/Fabric-Inventory", alias="WARDEN_TARGET_REPO"
    )
    runs_dir: Path = Field(default=Path("runs"), alias="WARDEN_RUNS_DIR")
    # Best-of-N: implementer runs per issue, each on its own fork of the base, verifier ranks
    candidates: int = Field(default=1, alias="WARDEN_CANDIDATES")
    # Theater retries: when the only FAIL is "no new test reproduces the issue on the unfixed
    # base", the implementer continues from the candidate with that feedback and verify reruns
    # (run 20261014-131452-cd9b: right fix, all 4 new tests already passed on base). 0 = off.
    theater_retries: int = Field(default=1, ge=0, alias="WARDEN_THEATER_RETRIES")
    # Verifier's spec review: one VERDICT call comparing the diff with the issue's stated behaviours
    spec_check: bool = Field(default=True, alias="WARDEN_SPEC_CHECK")
    # The repo's own test command (no file args). Set: it is the gate's suite and the regression
    # check's command, whatever the profile. "" = the profile's suite, no regression check.
    test_cmd: str = Field(default="", alias="WARDEN_TEST_CMD")
    # Branch fix PRs target for live issues (an external `--repo` uses its default branch).
    base_branch: str = Field(default="main", alias="WARDEN_BASE_BRANCH")
    # Markdown file for the PR body ($evidence, $disclosure, $issue, $title, $repo, $run_id,
    # $models); "" = evidence table + AI disclosure. Without $disclosure, it is appended.
    pr_template: str = Field(default="", alias="WARDEN_PR_TEMPLATE")

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
