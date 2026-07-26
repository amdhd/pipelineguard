variable "environment" {
  description = "Deployment environment"
  type        = string
}

variable "kms_key_arn" {
  description = "CMK ARN for encrypting the VPC flow-log group"
  type        = string
}
