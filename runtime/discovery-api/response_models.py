from typing import Literal
from pydantic import Field
from models import Strict, Lead

ErrorCode = Literal['timed_out', 'cancelled', 'output_limit', 'prompt_limit', 'invalid_stream',
                    'invalid_backend_result', 'invalid_result_schema', 'backend_exit',
                    'turn_limit', 'backend_failed', 'procedure_unavailable', 'search_not_configured']


class IterationMetadata(Strict):
    iteration: int = Field(ge=1, le=10)
    requested_count: int = Field(ge=1, le=3)
    normal_search_page: int | None = Field(ge=1, le=10)
    accepted_count: int = Field(ge=0, le=30)
    candidate_count: int = Field(ge=0, le=30)
    rejected_count: int = Field(ge=0, le=30)
    rejected_unqualified_count: int = Field(default=0, ge=0, le=30)
    rejected_out_of_scope_count: int = Field(default=0, ge=0, le=30)
    normalized_website_count: int = Field(ge=0, le=30)
    tool_use_count: int = Field(ge=0)
    charged_turns: int = Field(ge=0, le=30)
    outcome: Literal['completed', 'partial', 'blocked', 'failed']
    error: ErrorCode | None
    rejection_counts: dict[Literal['duplicate', 'invalid', 'schema', 'unsupported_fields', 'profile_provenance', 'scope', 'role', 'quota'], int] = Field(default_factory=dict)


class ResultMetadata(Strict):
    source: Literal['linkedin', 'web', 'search']
    outcome: Literal['completed', 'partial', 'blocked']
    blocked_reason: Literal['login_required', 'challenge', 'access_limit', 'access_denied'] | None
    rejected_count: int = Field(ge=0, le=5000)
    qualified_count: int = Field(default=0, ge=0, le=100)
    rejected_unqualified_count: int = Field(default=0, ge=0, le=300)
    rejected_out_of_scope_count: int = Field(default=0, ge=0, le=300)
    candidate_count: int = Field(default=0, ge=0, le=5000)
    normalized_website_count: int = Field(default=0, ge=0, le=300)
    iterations: list[IterationMetadata] = Field(default_factory=list, max_length=10)
    iteration_count: int = Field(default=0, ge=0, le=10)
    tool_use_count: int = Field(default=0, ge=0)
    charged_turns: int = Field(default=0, ge=0, le=120)
    reserved_turns: int = Field(default=0, ge=0, le=120)
    budget_accounting: Literal['full_cli_reservation'] = 'full_cli_reservation'
    measured_model_iterations: None = None
    rejection_counts: dict[Literal['duplicate', 'invalid', 'schema', 'unsupported_fields', 'profile_provenance', 'scope', 'role', 'quota'], int] = Field(default_factory=dict)
    repaired_count: int = Field(default=0, ge=0, le=30)
    output_bytes: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    stop_reason: Literal['target_reached', 'blocked', 'search_exhausted', 'timed_out',
                         'cancelled', 'output_limit', 'prompt_limit', 'invalid_stream',
                         'invalid_backend_result', 'invalid_result_schema', 'backend_exit',
                         'turn_limit', 'backend_failed', 'procedure_unavailable', 'budget_exhausted'] | None = None
    # source=search accounting (provider + LLM spend, tiers, strategies used)
    verification_counts: dict[Literal['verified', 'plausible', 'linkedin', 'review'], int] = Field(default_factory=dict)
    min_verification: Literal['verified', 'plausible', 'linkedin'] | None = None
    expanded_keywords: list[str] = Field(default_factory=list, max_length=10)
    expanded_titles: list[str] = Field(default_factory=list, max_length=10)
    excluded_by_registry: int = Field(default=0, ge=0)   # companies blocked in the shared registry
    excluded_same_company: int = Field(default=0, ge=0)  # over max_per_company in this run
    max_per_company: int | None = None
    review_count: int = Field(default=0, ge=0, le=5000)
    cost_usd: float | None = Field(default=None, ge=0)
    provider_calls: int = Field(default=0, ge=0)
    llm_calls: int = Field(default=0, ge=0)
    queries: int = Field(default=0, ge=0)
    firms_checked: int = Field(default=0, ge=0)
    strategies: list[Literal['linkedin_search', 'firm_websites']] = Field(default_factory=list, max_length=2)
    human_review_required: Literal[True] = True
    evidence_validation: Literal['verbatim_observed_tool_output', 'verbatim_fetched_source_text']
    fit_reason_is_inference: Literal[True]
    contact_permission_established: Literal[False]


class AcceptedResult(Strict):
    leads: list[Lead] = Field(max_length=100)
    returned_count: int = Field(ge=0, le=100)
    requested_count: int = Field(ge=1, le=100)
    missing_count: int = Field(ge=0, le=100)
    metadata: ResultMetadata


class JobMetadata(Strict):
    retention_days: int = Field(ge=1)
    discovery_only: Literal[True]


class JobResponse(Strict):
    id: str
    status: Literal['queued', 'running', 'succeeded', 'partial', 'blocked', 'failed', 'timed_out']
    created_at: float
    updated_at: float
    requested_count: int = Field(ge=1, le=100)
    returned_count: int = Field(ge=0, le=100)
    missing_count: int = Field(ge=0, le=100)
    metadata: JobMetadata
    result: AcceptedResult | None
    error: str | None
