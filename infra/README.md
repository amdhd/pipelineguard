# infra/

All Terraform for PipelineGuard. The root module wires five child modules together and owns the
shared CMK; nothing is applied from inside `modules/` directly.

```
infra/
├── main.tf           Module wiring (networking → ecr → ecs → pipeline → gates)
├── kms.tf            Shared CMK used by every module for at-rest / log encryption
├── variables.tf      Root input variables
├── outputs.tf        Root outputs (ALB DNS, ECR/cluster/pipeline names, gate functions)
├── versions.tf       Terraform + provider constraints, S3 backend, default tags
├── environments/     Per-environment tfvars (dev.tfvars)
└── modules/
    ├── networking/   VPC, 2 public + 2 private subnets, single NAT, flow logs
    ├── ecr/          App image repository (immutable tags, lifecycle policy)
    ├── ecs/          Fargate cluster, task definition, service, ALB
    ├── pipeline/     CodePipeline, CodeBuild projects, artifact bucket
    └── gates/        Cost gate (zip Lambda) + security gate (container Lambda)
```

Every module follows the same layout: `main.tf`, `variables.tf`, `outputs.tf`.

## Working here

`backend.conf` and `*.auto.tfvars` are git-ignored — generate them with
[`scripts/bootstrap.sh`](../scripts/bootstrap.sh) and your own secret values before the first init.

```bash
terraform -chdir=infra init -backend-config=backend.conf
terraform -chdir=infra fmt -recursive -check
terraform -chdir=infra validate
terraform -chdir=infra plan -var-file=environments/dev.tfvars
```

Apply and teardown go through the wrapper scripts so ECR repos get emptied first:

```bash
./scripts/apply-dev.sh
./scripts/destroy-dev.sh
```

See [`docs/deploy.md`](../docs/deploy.md) for the full first-time walkthrough.
