# ⚠️ DELIBERATELY INSECURE — test fixture for the security gate (Checkov).
#
# This bucket is public-read with no encryption/versioning/logging/public-access-block,
# so Checkov flags multiple HIGH findings and the security gate BLOCKS the deploy.
# It is therefore NEVER applied (the gate stops the pipeline before deploy). Delete
# this file to remove the finding once the gate has been demonstrated.

resource "aws_s3_bucket" "insecure_demo" {
  bucket = "pipelineguard-insecure-demo-${var.environment}"
}

resource "aws_s3_bucket_acl" "insecure_demo" {
  bucket = aws_s3_bucket.insecure_demo.id
  acl    = "public-read" # Checkov CKV_AWS_20: S3 bucket allows public READ
}
