"""Render and publish the optional pull-request deployment report."""

import json
import re
from typing import cast
from urllib.request import Request, urlopen

from agentcore_release_gate.constants import (
    GITHUB_API_VERSION,
    GITHUB_COMMENTS_PAGE_SIZE,
    GITHUB_REQUEST_TIMEOUT_SECONDS,
)
from agentcore_release_gate.evaluation import gate_failure_reason
from agentcore_release_gate.types import GitHubResponse, JsonObject

COMMENT_MARKER = "<!-- agentcore-ab-release-gate-report -->"
GITHUB_API = "https://api.github.com"
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
FAILURE_LABELS = {
    "below_minimum": "❌ Below minimum",
    "not_significant": "❌ Not significant",
    "regressed": "❌ Regressed vs control",
}


def build_report(state: JsonObject, outcome: str, run_url: str = "") -> str:
    """Build a stable Markdown summary from the recovery journal.

    Args:
        state: Persisted deployment state and evaluation results.
        outcome: GitHub Actions step outcome.
        run_url: Optional workflow-run URL included in the report.

    Returns:
        A Markdown report suitable for a pull-request comment.
    """
    finished = state.get("finished")
    if finished == "promoted" and outcome == "success":
        heading, decision = "✅ AgentCore A/B deployment promoted", "Promoted"
    elif finished == "rolled_back":
        heading, decision = "❌ AgentCore A/B deployment rejected", "Rolled back"
    else:
        heading, decision = "❌ AgentCore A/B deployment failed", "Failed"

    lines = [COMMENT_MARKER, f"## {heading}", "", f"**Decision:** {decision}"]
    if state.get("version"):
        lines.append(f"**Candidate version:** `{state['version']}`")
    if state.get("image"):
        lines.append(f"**Image:** `{state['image']}`")

    gates = state.get("quality_gates", {})
    results = state.get("variant_results", {})
    require_significance = state.get("require_significance", True)
    if gates:
        lines.extend(
            [
                "",
                "| Evaluator | Mean | Minimum | Significant | Δ vs control | p-value | Samples (control/treatment) | Result |",
                "|---|---:|---:|:---:|---:|---:|:---:|:---:|",
            ]
        )
        for evaluator, minimum in sorted(gates.items()):
            variant = results.get(evaluator)
            if variant is None:
                lines.append(f"| `{evaluator}` | — | {minimum:g} | — | — | — | — | ❌ Missing |")
                continue
            mean = variant["mean"]
            significant = variant["isSignificant"]
            change = variant["absoluteChange"]
            p_value = variant["pValue"]
            samples = f"{variant['controlSampleSize']}/{variant['treatmentSampleSize']}"
            rendered_change = f"{change:g}" if isinstance(change, (int, float)) else "—"
            rendered_p_value = f"{p_value:g}" if isinstance(p_value, (int, float)) else "—"
            reason = gate_failure_reason(
                variant, minimum, require_significance=require_significance
            )
            result = FAILURE_LABELS[reason] if reason else "✅ Pass"
            if significant:
                rendered_significant = "Yes"
            elif require_significance:
                rendered_significant = "No"
            else:
                rendered_significant = "No (not required)"
            lines.append(
                f"| `{evaluator}` | {mean:g} | {minimum:g} | {rendered_significant} | "
                f"{rendered_change} | {rendered_p_value} | {samples} | {result} |"
            )
    elif not state:
        lines.extend(
            [
                "",
                "No deployment state was recorded. Check the workflow logs for the setup error.",
            ]
        )

    if run_url:
        lines.extend(["", f"[View workflow run]({run_url})"])
    return "\n".join(lines)


def _github_request(
    token: str,
    url: str,
    method: str = "GET",
    body: JsonObject | None = None,
) -> GitHubResponse:
    """Send an authenticated request to the GitHub JSON API."""
    data = json.dumps(body).encode() if body is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "agentcore-ab-release-gate-action",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    with urlopen(request, timeout=GITHUB_REQUEST_TIMEOUT_SECONDS) as response:
        return cast(GitHubResponse, json.loads(response.read()))


def publish_report(
    token: str,
    repository: str,
    pull_request: int,
    report: str,
    api_url: str = GITHUB_API,
) -> None:
    """Create or update the action-owned pull-request comment.

    Args:
        token: GitHub API bearer token.
        repository: Repository in ``owner/name`` format.
        pull_request: Positive pull-request number.
        report: Markdown body to publish.
        api_url: Base URL for the GitHub API.

    Raises:
        ValueError: If the repository or pull-request number is invalid.
    """
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError("GitHub repository must use owner/name format")
    if pull_request <= 0:
        raise ValueError("Pull request number must be positive")

    comments_url = f"{api_url.rstrip('/')}/repos/{repository}/issues/{pull_request}/comments"
    owned_comment = None
    page = 1
    while owned_comment is None:
        response = _github_request(
            token,
            f"{comments_url}?per_page={GITHUB_COMMENTS_PAGE_SIZE}&page={page}",
        )
        if not isinstance(response, list):
            raise ValueError("GitHub comments response must be a JSON array")
        comments = response
        owned_comment = next(
            (
                comment
                for comment in comments
                if COMMENT_MARKER in comment.get("body", "")
                and comment.get("user", {}).get("type") == "Bot"
            ),
            None,
        )
        if owned_comment is not None or len(comments) < GITHUB_COMMENTS_PAGE_SIZE:
            break
        page += 1

    if owned_comment is None:
        _github_request(token, comments_url, "POST", {"body": report})
        return
    update_url = f"{api_url.rstrip('/')}/repos/{repository}/issues/comments/{owned_comment['id']}"
    _github_request(token, update_url, "PATCH", {"body": report})
