output "artifact_bucket_arn" {
  description = "Pipeline artifact bucket ARN"
  value       = aws_s3_bucket.artifacts.arn
}

output "artifact_bucket_name" {
  description = "Pipeline artifact bucket name"
  value       = aws_s3_bucket.artifacts.bucket
}

output "pipeline_name" {
  description = "CodePipeline name"
  value       = aws_codepipeline.main.name
}

output "github_connection_arn" {
  description = "CodeStar GitHub connection ARN (authorise once in the console)"
  value       = aws_codestarconnections_connection.github.arn
}
