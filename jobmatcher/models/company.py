from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Dict, Any
import json


@dataclass
class CompanyConfig:
    """Configuration for a company to crawl."""

    name: str
    ats: str  # greenhouse | lever | ashby | custom
    identifier: str  # board token, e.g., greenhouse board name, lever company name, ashby board name
    url: Optional[str] = None  # optional override URL
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.ats = self.ats.lower().strip()
        valid = {"greenhouse", "lever", "ashby", "generic", "custom"}
        if self.ats not in valid:
            raise ValueError(f"Unsupported ATS '{self.ats}'. Valid: {valid}")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompanyConfig":
        return cls(
            name=data.get("name") or data.get("company") or data["identifier"],
            ats=data.get("ats") or data.get("source") or "greenhouse",
            identifier=data.get("identifier") or data.get("board_token") or data.get("token"),
            url=data.get("url"),
            extra={k: v for k, v in data.items() if k not in {"name", "company", "ats", "source", "identifier", "board_token", "token", "url"}},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ats": self.ats,
            "identifier": self.identifier,
            "url": self.url,
        }

    @staticmethod
    def load_list_from_json(path: str) -> list["CompanyConfig"]:
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict) and "companies" in data:
            data = data["companies"]
        return [CompanyConfig.from_dict(item) for item in data]

    @staticmethod
    def load_list_from_yaml(path: str) -> list["CompanyConfig"]:
        import yaml

        with open(path, "r") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and "companies" in data:
            data = data["companies"]
        return [CompanyConfig.from_dict(item) for item in data]
