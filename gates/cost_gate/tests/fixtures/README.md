# Infracost 0.10.46 compatibility fixtures

These files are unmodified JSON outputs from the official Linux amd64 release,
downloaded with its SHA-256 checksum verified. They were generated in an Alpine
3.23 container with `--network none`, using a dummy API key and pre-priced
Infracost JSON inputs for `aws_instance.dummy`:

- `diff-0`: $10 before, $10 after.
- `diff-70`: $10 before, $80 after.

The command was `infracost diff --path after.json --compare-to before.json
--format json --out-file diff.json`, with cloud upload disabled. API/telemetry
requests were blocked by the container's network policy; the local diff still
completed successfully. No Terraform plan, real pricing API, production resource
or production credential was used. These fixtures verify the parser/threshold
contract, not pricing correctness for deployed infrastructure.
