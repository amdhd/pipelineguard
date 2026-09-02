# PipelineGuard — AWS Deployment Guide + Cost Estimate

---

## Prerequisites

Install these before anything else:

```bash
# Terraform
brew install terraform       # macOS
# or: https://developer.hashicorp.com/terraform/install

# AWS CLI v2
brew install awscli
aws --version  # should be 2.x

# Docker (for building the app image locally to test)
brew install --cask docker

# Node.js 20 (for the sample app)
brew install node@20

# Infracost CLI (for local cost gate testing)
brew install infracost
infracost --version
```

---

## Step 0 — AWS Account Setup (One-time)

### 0a. Create an IAM User for Terraform

Never use your root account. Create a dedicated Terraform user:

```bash
# Create user
aws iam create-user --user-name terraform-pipelineguard

# Attach AdministratorAccess for initial setup
# (You can scope this down after the project is stable)
aws iam attach-user-policy \
  --user-name terraform-pipelineguard \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess

# Create access key
aws iam create-access-key --user-name terraform-pipelineguard
# SAVE the AccessKeyId and SecretAccessKey — you won't see them again
```

### 0b. Configure AWS CLI Profile

```bash
aws configure --profile pipelineguard
# Enter: AccessKeyId, SecretAccessKey, region (ap-southeast-1), output (json)

# Verify
aws sts get-caller-identity --profile pipelineguard
```

Add to your shell profile so Terraform picks it up:
```bash
export AWS_PROFILE=pipelineguard
export AWS_DEFAULT_REGION=ap-southeast-1
```

### 0c. Run the Bootstrap Script

This creates the S3 bucket backing the Terraform state. Locking is S3-native
(`use_lockfile = true`), so there is no separate lock table.
Must be done before `terraform init`.

```bash
bash scripts/bootstrap.sh
```

`scripts/bootstrap.sh` content:
```bash
#!/bin/bash
set -e

REGION="ap-southeast-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET_NAME="pipelineguard-tf-state-${ACCOUNT_ID}"

echo "Creating Terraform state bucket: $BUCKET_NAME"
aws s3api create-bucket \
  --bucket "$BUCKET_NAME" \
  --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"

# Enable versioning — critical for state file history
aws s3api put-bucket-versioning \
  --bucket "$BUCKET_NAME" \
  --versioning-configuration Status=Enabled

# Enable encryption
aws s3api put-bucket-encryption \
  --bucket "$BUCKET_NAME" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# Block all public access
aws s3api put-public-access-block \
  --bucket "$BUCKET_NAME" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"


# Write backend config files — one per layer root (each layer has its own state).
# bootstrap.sh does this; shown here for a manual setup.
cat > infra/layer1_persistent/backend.conf << EOF
bucket         = "${BUCKET_NAME}"
key            = "pipelineguard/layer1/dev/terraform.tfstate"
region         = "${REGION}"
encrypt        = true
EOF
cat > infra/layer2_ephemeral/backend.conf << EOF
bucket         = "${BUCKET_NAME}"
key            = "pipelineguard/layer2/dev/terraform.tfstate"
region         = "${REGION}"
encrypt        = true
EOF

echo "Bootstrap complete."
echo "State bucket: s3://${BUCKET_NAME}"
echo "Backend configs written for layer1_persistent + layer2_ephemeral"
```

---

## Step 1 — Get API Keys

### 1a. Anthropic API Key
```
https://console.anthropic.com/settings/keys
→ Create Key → copy it
```

### 1b. Infracost API Key
```bash
infracost auth login
# Opens browser, sign in with GitHub
# Then:
infracost configure get api_key
# Copy the key shown
```

### 1c. GitHub Personal Access Token (for PR comments)
```
https://github.com/settings/tokens/new
→ Classic token
→ Scopes: repo (full), read:org
→ Copy the token
```

### 1d. Slack Webhook URL
```
https://api.slack.com/apps
→ Create New App → From Scratch
→ Name: PipelineGuard, pick your workspace
→ Incoming Webhooks → Activate → Add New Webhook to Workspace
→ Pick #devops channel (or create one)
→ Copy Webhook URL (starts with https://hooks.slack.com/services/...)
```

---

## Step 2 — GitHub Repository Setup

### 2a. Create the repo and push code

```bash
cd pipelineguard
git init
git add .
git commit -m "feat: initial PipelineGuard project"
git remote add origin https://github.com/YOUR_USERNAME/pipelineguard.git
git push -u origin main
```

### 2b. Create a GitHub CodeStar Connection

This links AWS CodePipeline to your GitHub repo:

```bash
# Create connection (this opens a browser for OAuth authorisation)
aws codestarconnections create-connection \
  --provider-type GitHub \
  --connection-name pipelineguard-github \
  --region ap-southeast-1

# The output gives you a ConnectionArn — save it
# Example: arn:aws:codestar-connections:ap-southeast-1:123456789:connection/abc-123

# You MUST also complete the GitHub auth in the AWS console:
# Console → Developer Tools → Connections → pipelineguard-github → Update pending connection
```

---

## Step 3 — Configure Terraform Variables

The demo stack's variables live in `infra/layer2_ephemeral/dev.tfvars` (the QA
core in layer1 has its own tfvars with the pinned `qa_agent_code_*` values):

```bash
# infra/layer2_ephemeral/dev.tfvars
cat > infra/layer2_ephemeral/dev.tfvars << 'EOF'
aws_region          = "ap-southeast-1"
environment         = "dev"
owner_tag           = "amad"
github_repo         = "YOUR_USERNAME/pipelineguard"
github_branch       = "main"
cost_gate_threshold = 50
ecs_cpu             = 256
ecs_memory          = 512
ecs_desired_count   = 1
EOF
```

Secrets never go through Terraform — not in a tfvars file, and not as `TF_VAR_*`
either. `terraform show -json` does not redact sensitive values, so any secret
reaching Terraform is written in plaintext into `plan.json`, which the pipeline
stores in S3 as an artifact.

The gate API keys go straight into Secrets Manager instead, after the apply in
Step 5 creates the (empty) secret:

```bash
export INFRACOST_API_KEY="YOUR_INFRACOST_KEY"
export ANTHROPIC_API_KEY="YOUR_ANTHROPIC_KEY"
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/YOUR/WEBHOOK"
export GITHUB_TOKEN="YOUR_GITHUB_TOKEN"   # optional; enables the PR comment
./scripts/seed-gate-secrets.sh dev ap-southeast-1
```

---

## Step 4 — Package Lambda Gates

```bash
bash scripts/deploy-gates.sh
```

`scripts/deploy-gates.sh` content:
```bash
#!/bin/bash
set -e

echo "Packaging cost gate..."
cd gates/cost_gate
pip install -r requirements.txt -t ./package --quiet
cd package && zip -r ../cost_gate.zip . -q && cd ..
zip cost_gate.zip handler.py infracost_runner.py -q
echo "Cost gate packaged: gates/cost_gate/cost_gate.zip"

echo "Packaging security gate..."
cd ../security_gate
pip install -r requirements.txt -t ./package --quiet
cd package && zip -r ../security_gate.zip . -q && cd ..
zip security_gate.zip handler.py trivy_runner.py checkov_runner.py \
  claude_summariser.py github_commenter.py -q
echo "Security gate packaged: gates/security_gate/security_gate.zip"

cd ../..
echo "All gates packaged."
```

---

## Step 5 — Deploy Infrastructure with Terraform

The infra is TWO Terraform roots with separate states. First the always-on QA
core (layer1_persistent — applied once, left up), then the demo stack
(layer2_ephemeral — the pipeline lives here).

```bash
# Layer 1 — QA core (KMS + AgentCore runtime; applied once, stays up ~$1.40/mo)
./scripts/apply-dev.sh

# Layer 2 — demo stack. demo-up.sh handles the cold-start order (security-gate
# image first, then the rest + app image), so a fresh `cd`+init isn't needed:
cd infra/layer2_ephemeral
terraform init -backend-config=backend.conf    # first time only
cd ../..
./scripts/demo-up.sh -auto-approve
```

**What layer2 creates (in order):**
1. VPC + subnets + IGW + NAT Gateway + route tables
2. ECR repositories
3. ECS cluster + task definition + service + ALB
4. Secrets Manager secret (with your API keys)
5. Lambda gate functions
6. CodeBuild projects (one per buildspec)
7. CodePipeline with all stages
8. CloudWatch log groups + alarms
9. SNS topic for alerts

**Terraform outputs to note (layer2):**
```
alb_dns_name         = "pipelineguard-alb-dev-XXXX.ap-southeast-1.elb.amazonaws.com"
ecr_repository_url   = "123456789.dkr.ecr.ap-southeast-1.amazonaws.com/pipelineguard-app-dev"
cost_gate_function   = "pipelineguard-cost-gate-dev"
security_gate_function = "pipelineguard-security-gate-dev"
```

---

## Step 6 — Push First Image to ECR (Bootstrap Deploy)

The pipeline needs at least one image in ECR before ECS can run:

`demo-up.sh` already pushes `:latest` as its final step. To push manually (e.g.
after a rebuild):

```bash
# Get ECR URL from the layer2 output
ECR_URL=$(terraform -chdir=infra/layer2_ephemeral output -raw ecr_repository_url)
AWS_REGION="ap-southeast-1"

# Login to ECR
aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin $ECR_URL

# Build and push (linux/amd64 to match the Fargate default platform)
docker buildx build --platform linux/amd64 -t "$ECR_URL:latest" --push app/

echo "App image pushed. ECS service will stabilise in ~2 minutes."
```

---

## Step 7 — Verify Everything Works

### 7a. Check ECS service is healthy
```bash
# Should show RUNNING with 1/1 desired count
aws ecs describe-services \
  --cluster pipelineguard-dev \
  --services pipelineguard-app-dev \
  --query 'services[0].{Status:status,Running:runningCount,Desired:desiredCount}'
```

### 7b. Hit the health endpoint
```bash
ALB_URL=$(terraform -chdir=infra/layer2_ephemeral output -raw alb_dns_name)
curl http://$ALB_URL/health
# Expected: {"status":"healthy","timestamp":"...","version":"unknown","environment":"unknown"}
```

### 7c. Trigger a pipeline run
```bash
# Push a small change to trigger the pipeline
echo "# Trigger pipeline" >> README.md
git add README.md && git commit -m "test: trigger pipeline" && git push
```

### 7d. Watch the pipeline
```bash
# Get pipeline name
aws codepipeline list-pipelines --query 'pipelines[*].name'

# Watch execution
aws codepipeline get-pipeline-state --name pipelineguard-dev-pipeline
```

Or open the AWS Console → CodePipeline → pipelineguard-dev-pipeline

---

## Step 8 — Test the Gates

### Test cost gate blocking

Temporarily set the threshold to $0 to force a block:

```bash
export TF_VAR_cost_gate_threshold=0
terraform -chdir=infra/layer2_ephemeral apply -var-file=dev.tfvars -target=module.gates
```

Push a commit — the cost gate should block and you'll get a Slack message.

Reset after testing:
```bash
export TF_VAR_cost_gate_threshold=50
terraform -chdir=infra/layer2_ephemeral apply -var-file=dev.tfvars -target=module.gates
```

### Test security gate blocking

Add a deliberately vulnerable dependency to the app or an insecure Terraform resource (e.g. S3 bucket with `acl = "public-read"`) and push. Checkov should catch it, Claude should summarise it, pipeline blocks.

---

## Step 9 — Cleanup (When Done Demoing)

```bash
# Between demos: destroy ONLY the demo layer (layer2). Empties ECR repos first
# (destroy won't remove non-empty ECR). NEVER touches the layer1 QA core.
./scripts/demo-down.sh -auto-approve

# To stop the WHOLE project (QA core included — the AgentCore runtime the vesselAI
# QA workflow invokes goes away until recreated):
./scripts/destroy-dev.sh -auto-approve

# Delete S3 state bucket (do this last, manually)
# aws s3 rb s3://pipelineguard-tf-state-ACCOUNT_ID --force

# Delete IAM user
aws iam detach-user-policy \
  --user-name terraform-pipelineguard \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess
aws iam delete-access-key --user-name terraform-pipelineguard --access-key-id YOUR_KEY_ID
aws iam delete-user --user-name terraform-pipelineguard
```

---

## Infrastructure Map

```
AWS Account (ap-southeast-1)
│
├── VPC: 10.0.0.0/16
│   ├── Public Subnet A (10.0.0.0/24) — AZ: ap-southeast-1a
│   ├── Public Subnet B (10.0.1.0/24) — AZ: ap-southeast-1b
│   ├── Private Subnet A (10.0.10.0/24) — ECS tasks run here
│   ├── Private Subnet B (10.0.11.0/24) — ECS tasks run here
│   ├── Internet Gateway
│   └── NAT Gateway (in Public Subnet A)
│
├── ECR: pipelineguard-app-dev
│   └── Lifecycle policy: keep last 10 images
│
├── ECS Fargate
│   ├── Cluster: pipelineguard-dev
│   ├── Service: pipelineguard-app-dev (1 task)
│   └── Task: 0.25 vCPU / 512 MB, runs in Private Subnets
│
├── ALB: pipelineguard-alb-dev
│   ├── Listener: HTTP:80
│   └── Target Group → ECS tasks on port 3000
│
├── Lambda
│   ├── pipelineguard-cost-gate-dev (Python 3.12, 256 MB, 300s timeout)
│   └── pipelineguard-security-gate-dev (Python 3.12, 512 MB, 600s timeout)
│
├── CodePipeline: pipelineguard-dev-pipeline
│   ├── Source: GitHub (via CodeStar Connection)
│   ├── CodeBuild: test + build (parallel)
│   ├── CodeBuild: terraform plan
│   ├── CodeBuild: invoke cost gate
│   ├── CodeBuild: invoke security gate
│   └── CodeBuild: terraform apply + ECS deploy
│
├── S3
│   ├── pipelineguard-artifacts-dev (pipeline artifacts, versioned)
│   └── pipelineguard-tf-state-ACCOUNT (Terraform state, versioned)
│
├── Secrets Manager: pipelineguard/gates/dev
│   └── {INFRACOST_API_KEY, ANTHROPIC_API_KEY, SLACK_WEBHOOK_URL}
│
│
├── CloudWatch
│   ├── Log Groups: /ecs/pipelineguard-dev, /aws/lambda/pipelineguard-*
│   └── Alarms: gate failures, ECS CPU
│
└── SNS: pipelineguard-alerts-dev → Slack webhook
```

---

## Cost Estimate (ap-southeast-1 / Singapore)

> All prices in USD. Based on AWS public pricing as of mid-2026.
> The stack is split so the always-on footprint is tiny: layer1 (QA core) stays
> up, layer2 (demo, where the NAT + ALB + Fargate live) exists only while demoing.

### Layer 1 — QA core (always-on, ~$1.40/mo)

| Resource | Spec | Monthly Cost |
|---|---|---|
| **KMS key** | 1x customer-managed | **~$1.00** |
| **Secrets Manager** | QA secret | **~$0.40** |
| **SUBTOTAL (layer1)** | | **~$1.40/month** |

The AgentCore QA runtime is PUBLIC-mode and bills per session, never while idle.

### Layer 2 — demo stack (only while up; ~$2.10/day)

| Resource | Spec | Daily Cost |
|---|---|---|
| **NAT Gateway** | 1x | ~$1.08/day |
| **ALB** | 1x | ~$0.54/day |
| **ECS Fargate** | 0.25 vCPU / 0.5 GB | ~$0.37/day |
| **Secrets · Logs · S3 · ECR · SNS** | | ~$0.11/day |
| **SUBTOTAL (layer2)** | | **~$2.10/day · $0 while down** |

### Variable Costs (per pipeline run, demo layer only)

| Resource | Per Run |
|---|---|
| **CodeBuild** | ~8 min build @ general1.small → ~$0.08/run |
| **Lambda** | Cost gate ~30s, Security gate ~120s → ~$0.002/run |

### Total Estimated Monthly Cost

| Scenario | Monthly |
|---|---|
| **QA core only, no demos (default)** | **~$1.40** |
| **One demo day + a handful of QA runs** | **~$5–8** |
| **Demoing every day** | ~$65+ |

### 💡 Cost Reduction Tips for Portfolio

**Biggest cost: NAT Gateway ($35/month)**
For a portfolio project, you can eliminate this by:
- Moving ECS tasks to public subnets with `assign_public_ip = true` (less secure but fine for demo)
- This drops the fixed cost to **~$30/month**

**Second biggest: ALB ($18/month)**
- Could replace with API Gateway HTTP API + direct Fargate invocation for the demo endpoint
- Saves ~$15/month
- But ALB is more realistic for a portfolio demo of a "real" setup

**ECS Fargate ($11/month)**
- Can scale to 0 when not demoing using scheduled scaling
- Off 16 hrs/day = saves ~$7/month

**Realistic portfolio cost with cost-saving tweaks:**

| Config | Monthly |
|---|---|
| Full setup as designed | ~$77 |
| Remove NAT (public subnets) | ~$42 |
| Remove NAT + scale ECS to 0 nights/weekends | ~$28 |
| **Destroy infra when not actively using** | **~$0** |

> **Recommendation:** Run `terraform apply` when you want to demo. Run `terraform destroy` when done. Keep the code and README as the portfolio artifact — the live demo is a bonus.

### AWS Free Tier Coverage (first 12 months)

| Service | Free Tier | Coverage |
|---|---|---|
| Lambda | 1M requests, 400K GB-seconds | ✅ Fully covered |
| S3 | 5 GB storage, 20K GET requests | ✅ Fully covered |
| CloudWatch | 10 custom metrics, 5 GB logs | ✅ Mostly covered |
| ECR | 500 MB/month | ✅ Mostly covered |
| CodeBuild | 100 build-minutes/month | ⚠️ Partial (5 runs/day exceeds this) |
| ECS Fargate | No free tier | ❌ Not covered |
| ALB | No free tier | ❌ Not covered |
| NAT Gateway | No free tier | ❌ Not covered |

---

## Common Issues + Fixes

| Issue | Fix |
|---|---|
| `terraform init` fails on backend | Run `bootstrap.sh` first, check S3 bucket exists |
| CodeStar connection pending | Go to AWS Console → Developer Tools → Connections → click Update pending |
| ECS task failing health check | Check CloudWatch logs `/ecs/pipelineguard-dev`, verify Docker image built correctly |
| Lambda gate timeout | Increase `timeout` in Terraform for the relevant gate (Trivy scan can be slow) |
| Cost gate always blocks at $0 | Check `cost_gate_threshold` variable, ensure it's set to 50 not 0 |
| `terraform destroy` hangs on ECR | Delete images manually first with the ECR cleanup command in Step 9 |
| NAT Gateway not deleted | NAT Gateway takes ~1 min to delete. Wait and retry. |
| GitHub PR comment not posting | Check GitHub token has `repo` scope, verify `github_repo` variable format is `owner/repo` |
