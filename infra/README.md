# infra/

All Terraform for PipelineGuard, split into **two separate roots** (mirroring the
aether pattern) so the always-on QA core and the demo/live stack have independent
states and lifecycles. The shared `modules/` are unchanged and are never applied
from directly.

```
infra/
├── layer1_persistent/   ALWAYS-ON QA core (~$1.40/mo idle): KMS + module.qa_agent
├── layer2_ephemeral/    DEMO/LIVE stack (~$0 while down): networking + ecr +
│                        ecs + pipeline + gates. Reads layer1's KMS key via
│                        terraform_remote_state.
└── modules/
    ├── qa_agent/        Reports/code buckets, QA secret, IAM roles, AgentCore runtime
    ├── networking/      VPC, 2 public + 2 private subnets, single NAT, flow logs
    ├── ecr/             App image repository (immutable tags, lifecycle policy)
    ├── ecs/             Fargate cluster, task definition, service, ALB
    ├── pipeline/        CodePipeline, CodeBuild projects, artifact bucket
    └── gates/           Cost gate (zip Lambda) + security gate (container Lambda)
```

Each root holds its own `main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`,
`dev.tfvars` and a git-ignored `backend.conf`. Every module follows the same
layout: `main.tf`, `variables.tf`, `outputs.tf`.

| Root | Job | State key | Teardown |
|---|---|---|---|
| `layer1_persistent/` | QA core — keep the vesselAI QA workflow alive | `pipelineguard/layer1/<env>/terraform.tfstate` | only to stop the whole project |
| `layer2_ephemeral/` | Demo/live stack (NAT + ALB bill ~$50/mo while up) | `pipelineguard/layer2/<env>/terraform.tfstate` | `scripts/demo-down.sh` |

## Working here

`backend.conf` is git-ignored — generate the two layer `backend.conf` files with
[`scripts/bootstrap.sh`](../scripts/bootstrap.sh), then init each root separately.

```bash
cd layer1_persistent && terraform init -backend-config=backend.conf
terraform -chdir=infra fmt -recursive -check        # fmt walks both roots + modules
terraform -chdir=infra/layer1_persistent validate
terraform -chdir=infra/layer2_ephemeral validate
terraform -chdir=infra/layer1_persistent plan -var-file=dev.tfvars
```

Apply and teardown go through the wrapper scripts so ECR repos get emptied first
and the layer1 code vars are never stripped:

```bash
./scripts/apply-dev.sh      # layer1 (QA core) apply — pins qa_agent_code_* in dev.tfvars
./scripts/demo-up.sh        # layer2 (demo) bring-up
./scripts/demo-down.sh      # layer2 (demo) teardown — never touches layer1
./scripts/destroy-dev.sh    # BOTH layers — stops the whole project
```

See [`docs/deploy.md`](../docs/deploy.md) for the full first-time walkthrough.
