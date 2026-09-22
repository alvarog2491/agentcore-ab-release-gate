# AgentCore A/B Release Gate

AgentCore A/B Release Gate is a composite GitHub Action that evaluates an existing Amazon Bedrock AgentCore Runtime candidate against the currently approved version. It promotes the candidate only when every configured quality gate passes; otherwise it restores the existing runtime version.

The action does not build container images or generate traffic. Give it an existing Linux ARM64 ECR image and ensure that real traffic reaches the configured AgentCore Gateway during the observation period.

## What the action does

Think of the action as a safety check before releasing a new version of your AI agent:

1. It keeps the current, working version available as the **control**.
2. It creates a separate **candidate** version from the new container image you want to release.
3. For a limited time, it sends most real traffic to the current version and a smaller share to the candidate.
4. AWS evaluates how both versions perform using the checks you configured, such as helpfulness or a custom quality score.
5. If the candidate meets every quality rule and does not perform worse than the current version, the action makes it the new live version.
6. If a check fails, something is cancelled, or AWS reports an error, the current version stays live and the candidate is rolled back.

The action keeps the completed A/B test available in AWS so you can review it later. It cleans up the temporary evaluation setup it created for the test.

<div align="center">
  <img src="agentcore_gateway_traffic_evaluation.gif" width="600" alt="AgentCore A/B Release Gate demo" />
</div>

## Prerequisites

Before using the action, provide the following resources in one AWS Region:

| Resource | Requirement |
|---|---|
| ECR image | Existing Linux ARM64 image with an ECR tag or SHA-256 digest. Tags are resolved to a digest before deployment. |
| AgentCore Runtime | A ready Runtime with an existing ready control endpoint. |
| AgentCore Gateway | A ready, dedicated HTTP Gateway. |
| Online evaluation configuration | One enabled reusable configuration, passed as `evaluation-config-id`. It must contain every evaluator named in `quality-gates`. Its CloudWatch service and log-group names must identify the control endpoint so the action can create the treatment copy. |
| A/B test role | IAM role trusted by `bedrock-agentcore.amazonaws.com`. |
| GitHub deployment role | Role assumed through OIDC with the AgentCore, ECR, and IAM permissions listed below. |

The action creates the `treatment` endpoint if it does not exist. It validates an existing target before reusing it. A run will not start while the same Gateway has an A/B test in `RUNNING` or `PAUSED` state.

See the [AgentCore A/B testing prerequisites](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/ab-testing-prereqs.html) for AWS-side setup details.

## Usage

Configure AWS credentials before invoking the action. Deployments sharing a Gateway must be serialized, and the GitHub Environment should protect production access.

```yaml
permissions:
  contents: read
  id-token: write
  pull-requests: write # Needed only when github-token is supplied.

concurrency:
  group: agentcore-production
  cancel-in-progress: false

jobs:
  deploy:
    runs-on: ubuntu-latest
    timeout-minutes: 180
    environment: production
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
          aws-region: eu-central-1
          role-duration-seconds: 14400

      - uses: alvarog2491/agentcore-ab-release-gate@b45e590e6eb5369f3370412f82ee008468ec0b02 # v1
        with:
          image-uri: 123456789012.dkr.ecr.eu-central-1.amazonaws.com/agent:v2
          runtime-id: my_agent-abcdefghij
          gateway-id: my-gateway-abcdefghij
          aws-region: eu-central-1
          evaluation-config-id: online_eval-abcdefghi1
          ab-test-role-arn: arn:aws:iam::123456789012:role/AgentCoreABTestRole
          quality-gates: '{"Builtin.Helpfulness":0.75,"custom_tone-abcdefghij":3.5}'
          github-token: ${{ github.token }}
```

For a production workflow, pin the action to the full commit SHA of a reviewed release rather than a movable tag. The runner needs Linux, Python 3.13, access to download the action's Python dependencies, and AWS API access. Checking out the caller repository is not required.

## Inputs

| Input | Required | Default | Description |
|---|:---:|---|---|
| `image-uri` | Yes | — | Existing ARM64 ECR image tag or SHA-256 digest. |
| `runtime-id` | Yes | — | AgentCore Runtime identifier. |
| `gateway-id` | Yes | — | AgentCore Gateway identifier. |
| `aws-region` | Yes | — | AWS Region that contains the Runtime and image. |
| `duration-seconds` | No | `7200` | Observation period. The action requires at least `60` seconds; use `7200` for production. |
| `evaluation-config-id` | Yes | — | Enabled online-evaluation configuration used as a template for the temporary control and treatment configurations. |
| `ab-test-role-arn` | Yes | — | IAM role AgentCore assumes to run the A/B test. |
| `control-endpoint-name` | No | `control` | Existing stable Runtime endpoint and control Gateway target name. |
| `control-weight` | No | `80` | Percentage of Gateway traffic assigned to control during observation. |
| `treatment-weight` | No | `20` | Percentage of Gateway traffic assigned to treatment during observation. |
| `quality-gates` | Yes¹ | — | Non-empty JSON object mapping built-in, third-party, or custom evaluator IDs to numeric minimum treatment scores. |
| `require-significance` | No | `true` | When `true`, every evaluator must be statistically significant. When `false`, minimum-score and no-regression checks still apply. |
| `evaluation-timeout-seconds` | No | `1800` | Maximum time to wait for AgentCore evaluator results. |
| `scoring-lag-seconds` | No | `120` | After all required evaluators produce a result, wait until the total scored sample count has not changed for this many seconds. Use `0` to evaluate as soon as results are available. |
| `step` | No | `auto` | Deployment phase: `auto` runs observe and promote in one job; `observe` deploys and evaluates the candidate, then uploads a state artifact; `promote` downloads that artifact and promotes the candidate; `rollback` downloads the artifact and restores the control. |
| `state-artifact-name` | Conditional | Empty | Name of the state artifact produced by the `observe` step. Required when `step` is `promote` or `rollback`. |
| `github-token` | No | Empty | Token used to publish a pull-request result comment. |

`control-weight` and `treatment-weight` must be integer values from `1` through `99` and add up to `100`.

¹ Required for the `auto` and `observe` steps; not used by `promote` or `rollback`.

## Outputs

| Output | Description |
|---|---|
| `runtime-version` | The promoted AgentCore Runtime version. Available after a successful `auto` or `promote` step. |
| `image-uri` | The resolved immutable ECR digest URI used by that version. Available after a successful `auto` or `promote` step. |
| `state-artifact-name` | Name of the GitHub Actions artifact holding the deployment state. Set by the `observe` step; pass it to the `promote` and `rollback` steps via `needs.<job>.outputs.state-artifact-name`. |

## Quality gates

`quality-gates` uses evaluator IDs as keys and their minimum acceptable treatment scores as values:

```yaml
quality-gates: >-
  {"Builtin.Helpfulness":0.75,"custom_tone-abcdefghij":3.5}
```

Every evaluator named here must be included in the template online-evaluation configuration. A gate passes only if the treatment mean meets its threshold and does not have a negative absolute change relative to control. With the default `require-significance: 'true'`, the treatment result must also be statistically significant.


The action waits for each configured evaluator to return at least one scored treatment session. It rejects the candidate if a required result does not arrive before `evaluation-timeout-seconds`, if no sessions are scored within the service wait window, or if any gate fails. Choose an observation period and traffic weights that can produce enough representative sessions.

## Reading A/B test results

The A/B test compares the current version (**control**) with the new version (**treatment**). A p-value below `0.05` usually means there is enough evidence that the difference is real, rather than random variation.

| Result | What it means | Recommended next step |
|---|---|---|
| p-value is below `0.05` and the treatment score increased | The new version is performing significantly better. | Consider promoting the treatment. |
| p-value is below `0.05` and the treatment score decreased | The new version is performing significantly worse. | Keep the current version. |
| p-value is `0.05` or higher | There is not enough evidence yet to confidently say one version is better. | Continue collecting samples or send more traffic to the treatment. |

Always review **every** evaluator before making a decision. A candidate can improve one quality measure while making another one worse. The action promotes a candidate only when all configured quality gates pass.

For the AWS definitions and result fields, see [Understanding A/B test results](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/ab-testing-target-based.html#target-based-get-results).

## Manual approval

To require a human decision before promotion, split the deployment into three jobs and protect the `promote` job with a [GitHub Environment](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments). The `observe` job runs the A/B test and uploads a state artifact; the `promote` job waits for environment approval before promoting; the `rollback` job runs automatically if the environment gate is denied or the promote job fails.

Configure the required reviewers for your environment in **Settings → Environments** before using this pattern.

```yaml
permissions:
  contents: read
  id-token: write
  pull-requests: write   # needed only when github-token is supplied

concurrency:
  group: agentcore-production
  cancel-in-progress: false

jobs:
  observe:
    runs-on: ubuntu-latest
    timeout-minutes: 180
    outputs:
      state-artifact-name: ${{ steps.gate.outputs.state-artifact-name }}
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
          aws-region: eu-central-1
          role-duration-seconds: 14400

      - uses: alvarog2491/agentcore-ab-release-gate@b45e590e6eb5369f3370412f82ee008468ec0b02 # v1
        id: gate
        with:
          step: observe
          image-uri: 123456789012.dkr.ecr.eu-central-1.amazonaws.com/agent:v2
          runtime-id: my_agent-abcdefghij
          gateway-id: my-gateway-abcdefghij
          aws-region: eu-central-1
          evaluation-config-id: online_eval-abcdefghi1
          ab-test-role-arn: arn:aws:iam::123456789012:role/AgentCoreABTestRole
          quality-gates: '{"Builtin.Helpfulness":0.75}'
          github-token: ${{ github.token }}

  promote:
    needs: observe
    runs-on: ubuntu-latest
    timeout-minutes: 30
    environment: production          # ← approval gate; configure reviewers in Settings → Environments
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
          aws-region: eu-central-1
          role-duration-seconds: 1800

      - uses: alvarog2491/agentcore-ab-release-gate@b45e590e6eb5369f3370412f82ee008468ec0b02 # v1
        with:
          step: promote
          state-artifact-name: ${{ needs.observe.outputs.state-artifact-name }}
          runtime-id: my_agent-abcdefghij
          gateway-id: my-gateway-abcdefghij
          aws-region: eu-central-1
          github-token: ${{ github.token }}

  rollback:
    needs: [observe, promote]
    runs-on: ubuntu-latest
    timeout-minutes: 30
    if: always() && needs.observe.result == 'success' && needs.promote.result != 'success'
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
          aws-region: eu-central-1
          role-duration-seconds: 1800

      - uses: alvarog2491/agentcore-ab-release-gate@b45e590e6eb5369f3370412f82ee008468ec0b02 # v1
        with:
          step: rollback
          state-artifact-name: ${{ needs.observe.outputs.state-artifact-name }}
          runtime-id: my_agent-abcdefghij
          gateway-id: my-gateway-abcdefghij
          aws-region: eu-central-1
```

Review evaluator means, significance, sample counts, and quality-gate results in the `observe` job logs before approving the deployment.

## Pull-request report

The action is designed to create or update one result comment when `github-token` is provided and the caller runs on a pull-request event. The comment includes the decision, candidate runtime version, resolved image digest, evaluator scores, significance, sample counts, and workflow-run link. Reporting is skipped without pull-request context or a token.

Grant `pull-requests: write` to the calling job when enabling this feature.

## AWS permissions

Scope the GitHub deployment role to the specific Runtime, Gateway, evaluation configurations, ECR repository, and roles it needs. It needs permissions for:

- Runtime: `GetAgentRuntime`, `UpdateAgentRuntime`, `GetAgentRuntimeEndpoint`, `CreateAgentRuntimeEndpoint`, and `UpdateAgentRuntimeEndpoint`.
- Gateway: `GetGateway`, `ListGatewayTargets`, `GetGatewayTarget`, and `CreateGatewayTarget`.
- A/B testing: `CreateABTest`, `GetABTest`, `UpdateABTest`, and `ListABTests`.
- Online evaluation configuration: `GetOnlineEvaluationConfig`, `CreateOnlineEvaluationConfig`, and `DeleteOnlineEvaluationConfig`.
- ECR: `DescribeImages`.
- IAM: `PassRole` for the Runtime execution role and A/B test role.

The AgentCore Runtime execution role also needs the observability, ECR-pull, and application-specific permissions required by its container. For online evaluation telemetry, grant the Runtime role:

- `xray:PutTraceSegments`
- `xray:PutTelemetryRecords`
- `xray:GetSamplingRules`
- `xray:GetSamplingTargets`

AWS credentials must remain valid through observation, evaluation, approval, promotion or rollback, and cleanup. Set both the workflow's `role-duration-seconds` and the IAM role's `MaxSessionDuration` accordingly.

## Promotion, rollback, and recovery

On success, the action stops the A/B test, points both the control and treatment Runtime endpoints to the candidate version, and deletes the temporary evaluation configurations.

On failure, cancellation, a failed quality gate, or an approval failure, it stops the A/B test, restores the control endpoint if promotion had started, returns treatment to the baseline version, and deletes the temporary configurations. A local JSON recovery journal makes cleanup safe to retry.

Runner loss, expired credentials, or an AWS service interruption can prevent automatic cleanup. In that case, stop the recorded A/B test and restore the recorded baseline manually:

```bash
aws bedrock-agentcore update-ab-test \
  --ab-test-id "$AB_TEST_ID" \
  --execution-status STOPPED

aws bedrock-agentcore-control update-agent-runtime-endpoint \
  --agent-runtime-id "$RUNTIME_ID" \
  --endpoint-name "$CONTROL_ENDPOINT_NAME" \
  --agent-runtime-version "$BASELINE_VERSION"

aws bedrock-agentcore-control update-agent-runtime-endpoint \
  --agent-runtime-id "$RUNTIME_ID" \
  --endpoint-name treatment \
  --agent-runtime-version "$BASELINE_VERSION"
```
