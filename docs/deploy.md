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

This creates the S3 backend for Terraform state + DynamoDB table for state locking.
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
TABLE_NAME="pipelineguard-tf-lock"

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

echo "Creating DynamoDB lock table: $TABLE_NAME"
aws dynamodb create-table \
  --table-name "$TABLE_NAME" \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION"

# Write backend config file
cat > infra/backend.conf << EOF
bucket         = "${BUCKET_NAME}"
key            = "pipelineguard/terraform.tfstate"
region         = "${REGION}"
dynamodb_table = "${TABLE_NAME}"
encrypt        = true
EOF

echo "Bootstrap complete."
echo "State bucket: s3://${BUCKET_NAME}"
echo "Lock table:   ${TABLE_NAME}"
echo "Backend config written to infra/backend.conf"
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

```bash
# infra/environments/dev.tfvars
cat > infra/environments/dev.tfvars << 'EOF'
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

Store secrets as environment variables — never in tfvars files:

```bash
export TF_VAR_slack_webhook_url="https://hooks.slack.com/services/YOUR/WEBHOOK"
export TF_VAR_infracost_api_key="YOUR_INFRACOST_KEY"
export TF_VAR_anthropic_api_key="YOUR_ANTHROPIC_KEY"
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

```bash
cd infra

# Initialise with S3 backend
terraform init -backend-config=backend.conf

# Validate syntax
terraform validate

# Preview what will be created (read this carefully)
terraform plan -var-file=environments/dev.tfvars

# Deploy (takes ~8-12 minutes first time)
terraform apply -var-file=environments/dev.tfvars
```

**What gets created (in order):**
1. VPC + subnets + IGW + NAT Gateway + route tables
2. ECR repository
3. ECS cluster + task definition + service + ALB
4. Secrets Manager secret (with your API keys)
5. Lambda gate functions
6. CodeBuild projects (one per buildspec)
7. CodePipeline with all stages
8. CloudWatch log groups + alarms
9. SNS topic for alerts

**Terraform outputs to note:**
```
alb_dns_name         = "pipelineguard-alb-dev-XXXX.ap-southeast-1.elb.amazonaws.com"
ecr_repo_url         = "123456789.dkr.ecr.ap-southeast-1.amazonaws.com/pipelineguard-app-dev"
pipeline_url         = "https://ap-southeast-1.console.aws.amazon.com/codesuite/codepipeline/pipelines/..."
cost_gate_arn        = "arn:aws:lambda:..."
security_gate_arn    = "arn:aws:lambda:..."
```

---

## Step 6 — Push First Image to ECR (Bootstrap Deploy)

The pipeline needs at least one image in ECR before ECS can run:

```bash
# Get ECR URL from Terraform output
ECR_URL=$(terraform output -raw ecr_repo_url)
AWS_REGION="ap-southeast-1"

# Login to ECR
aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin $ECR_URL

# Build and push bootstrap image
cd ../app
docker build -t $ECR_URL:bootstrap .
docker push $ECR_URL:bootstrap
docker tag $ECR_URL:bootstrap $ECR_URL:latest
docker push $ECR_URL:latest

echo "Bootstrap image pushed. ECS service will stabilise in ~2 minutes."
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
ALB_URL=$(cd infra && terraform output -raw alb_dns_name)
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
cd infra && terraform apply -var-file=environments/dev.tfvars -target=module.gates
```

Push a commit — the cost gate should block and you'll get a Slack message.

Reset after testing:
```bash
export TF_VAR_cost_gate_threshold=50
terraform apply -var-file=environments/dev.tfvars -target=module.gates
```

### Test security gate blocking

Add a deliberately vulnerable dependency to the app or an insecure Terraform resource (e.g. S3 bucket with `acl = "public-read"`) and push. Checkov should catch it, Claude should summarise it, pipeline blocks.

---

## Step 9 — Cleanup (When Done Demoing)

```bash
# Destroy all infrastructure
cd infra
terraform destroy -var-file=environments/dev.tfvars

# Delete ECR images first (destroy won't remove non-empty ECR)
ECR_URL=$(terraform output -raw ecr_repo_url)
REPO_NAME=$(echo $ECR_URL | cut -d'/' -f2)
aws ecr list-images --repository-name $REPO_NAME \
  --query 'imageIds[*]' --output json | \
  xargs -I{} aws ecr batch-delete-image --repository-name $REPO_NAME --image-ids {}

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
├── DynamoDB: pipelineguard-tf-lock (Terraform state lock)
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
> Assumes: low-traffic portfolio demo, ~5 pipeline runs/day, 1 ECS task running continuously.

### Fixed Monthly Costs (always-on)

| Resource | Spec | Monthly Cost |
|---|---|---|
| **NAT Gateway** | 1x, data ~1GB/month | **~$35.00** |
| **ALB** | 1x, ~0.5 LCU | **~$18.00** |
| **ECS Fargate** | 0.25 vCPU / 0.5 GB, 24/7 | **~$11.00** |
| **Secrets Manager** | 1 secret, 5 API calls/day | **~$0.40** |
| **CloudWatch Logs** | ~1 GB/month ingestion | **~$0.50** |
| **S3** | ~500 MB artifacts + state | **~$0.02** |
| **DynamoDB** | State lock table, on-demand | **~$0.00** |
| **ECR** | ~500 MB storage | **~$0.05** |
| **SNS** | <1000 notifications | **~$0.00** |
| **SUBTOTAL (fixed)** | | **~$65/month** |

### Variable Costs (per pipeline run)

| Resource | Per Run | 5 runs/day (monthly) |
|---|---|---|
| **CodeBuild** | ~8 min build @ general1.small | ~$0.08/run → **~$12.00** |
| **Lambda** | Cost gate ~30s, Security gate ~120s | ~$0.002/run → **~$0.30** |
| **Claude API** | ~500 tokens/scan (Haiku) | ~$0.0004/run → **~$0.06** |
| **SUBTOTAL (variable)** | | **~$12.36/month** |

### Total Estimated Monthly Cost

| Scenario | Monthly |
|---|---|
| **Just keeping infra alive (no pipeline runs)** | ~$65 |
| **Active dev (5 pipeline runs/day)** | ~$77 |
| **Portfolio demo (1 run/day)** | ~$67 |

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
| DynamoDB | 25 GB storage, 25 WCU/RCU | ✅ Fully covered |
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
