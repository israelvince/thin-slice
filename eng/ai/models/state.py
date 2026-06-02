from pydantic import BaseModel, Field
from typing import List, Optional, Dict

class HackathonAppState(BaseModel):
    user_request: str = Field(...)
    target_repo: str = Field(...)

    affected_files: List[str] = Field(default_factory=list)
    extracted_slice_context: str = Field(default="")

    selected_model_tier: str = Field(default="standard")
    projected_token_cost_usd: float = Field(default=0.0)
    policy_clearance: bool = Field(default=False)
    recommendation_notes: Optional[str] = Field(None)

    generated_code_blocks: Dict[str, str] = Field(default_factory=dict)
    pull_request_url: Optional[str] = Field(None)
from pydantic import BaseModel, Field
from typing import List, Optional, Dict

class HackathonAppState(BaseModel):
    user_request: str = Field(..., description="The user's change or feature request from Slack.")
    target_repo: str = Field(..., description="The sandbox environment target repository identifier.")

    affected_files: List[str] = Field(default_factory=list)
    extracted_slice_context: str = Field(default="")

    selected_model_tier: str = Field(default="standard")
    projected_token_cost_usd: float = Field(default=0.0)
    policy_clearance: bool = Field(default=False)
    recommendation_notes: Optional[str] = Field(None)

    generated_code_blocks: Dict[str, str] = Field(default_factory=dict)
    pull_request_url: Optional[str] = Field(None)
