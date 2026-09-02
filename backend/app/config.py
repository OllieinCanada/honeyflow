from typing import Optional

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings


class GraphDefaults(BaseModel):
    max_depth: int = 3
    max_children: int = 10
    decay: float = 0.8
    dev_dep_weight_multiplier: float = 0.2
    usage_freq_weight: float = 0.6
    llm_importance_weight: float = 0.4
    contributor_lines_weight: float = 0.5
    contributor_files_weight: float = 0.3
    contributor_commits_weight: float = 0.2


class CitationDefaults(BaseModel):
    max_depth: int = 2
    max_citations: int = 3


class Settings(BaseSettings):
    app_name: str = "Contribution Tracer"
    debug: bool = False
    database_url: Optional[str] = None
    attribution_admin_token: Optional[SecretStr] = None

    github_token: Optional[str] = None
    github_api_base: str = "https://api.github.com"

    privy_app_id: str = ""
    privy_app_secret: str = ""
    eth_chain: str = "sepolia"

    # 0G inference API (Next.js API route)
    inference_api_url: str = "http://localhost:3000/api/inference"

    graph: GraphDefaults = GraphDefaults()
    citation: CitationDefaults = CitationDefaults()

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
