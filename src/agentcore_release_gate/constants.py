"""Named operational constants for the AgentCore A/B action.

Values in this module document service constraints and conservative network polling
defaults. Keeping them here makes operational tuning explicit and prevents unexplained
literals from being scattered through the orchestration code.
"""

# AWS API calls should fail quickly enough for the action's recovery path to run.
AWS_CONNECT_TIMEOUT_SECONDS = 5
AWS_READ_TIMEOUT_SECONDS = 30
AWS_MAX_ATTEMPTS = 3

# Resource operations normally settle within 15 minutes; poll without flooding AWS.
DEFAULT_AWS_WAIT_TIMEOUT_SECONDS = 15 * 60
AWS_POLL_INTERVAL_SECONDS = 10

# Evaluation results are checked more often than long observation status updates.
EVALUATION_POLL_INTERVAL_SECONDS = 30
OBSERVATION_LOG_INTERVAL_SECONDS = 30
# Fail fast if the online evaluator scores no sessions within this window.
# Sessions are considered complete only after the eval config's session-timeout
# elapses, and results then take up to ~15 minutes to appear. With a 1-minute
# session timeout that totals ~16 minutes; 1200s (20 min) gives comfortable
# headroom without waiting the full evaluation timeout on a true misconfiguration.
NO_SESSIONS_TIMEOUT_SECONDS = 20 * 60
MINIMUM_OBSERVATION_SECONDS = 60
MINIMUM_RESULT_SAMPLE_SIZE = 1
# After all evaluators have at least one result, continue polling until the total
# scored-sample count has been stable for this many seconds before evaluating gates.
# Scoring results arrive with a delay after the observation window closes, so waiting
# for a stable count ensures quality-gate decisions are made on the most complete data.
SCORING_LAG_SECONDS = 600

# A/B traffic weights are integer percentages and both variants must receive traffic.
MINIMUM_VARIANT_WEIGHT = 1
MAXIMUM_VARIANT_WEIGHT = 99
TOTAL_TRAFFIC_WEIGHT = 100
DEFAULT_CONTROL_WEIGHT = 80
DEFAULT_TREATMENT_WEIGHT = 20

# GitHub's issue-comments endpoint supports at most 100 records per page.
GITHUB_COMMENTS_PAGE_SIZE = 100
GITHUB_REQUEST_TIMEOUT_SECONDS = 30
GITHUB_API_VERSION = "2026-03-10"

# Eight random hexadecimal characters keep generated A/B test names short and unique.
AB_TEST_NAME_RANDOM_LENGTH = 8

# ECR account IDs and SHA-256 digests have service-defined fixed lengths.
AWS_ACCOUNT_ID_LENGTH = 12
SHA256_HEX_LENGTH = 64

# Custom AgentCore evaluator IDs use these service-defined component limits.
MAX_EVALUATOR_NAME_PREFIX_LENGTH = 100
EVALUATOR_ID_SUFFIX_LENGTH = 10
