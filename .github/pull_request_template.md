## What & why

<!-- Describe the change and the motivation. -->

## PipelineGuard gate checklist

- [ ] Terraform changes reviewed for cost impact (cost gate threshold: $50/month dev)
- [ ] No new HIGH/CRITICAL findings expected from Trivy/Checkov
- [ ] Secrets go through Secrets Manager — none added to code, env vars, or buildspecs
- [ ] New AWS resources carry the default tags (`Project`, `Environment`, `ManagedBy`, `Owner`)
- [ ] IAM changes follow least privilege (each new permission documented in a comment)

## Testing

<!-- How was this verified? Include app test output / `terraform plan` summary. -->

## Notes for reviewers

<!-- Anything the automated gates can't catch. -->
