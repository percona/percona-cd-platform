output "role_arn" {
  description = "ARN of the IAM role assumable by the configured GitHub Actions subjects. Plug this into `aws-actions/configure-aws-credentials` as `role-to-assume`."
  value       = aws_iam_role.this.arn
}

output "role_name" {
  description = "Name of the IAM role. Useful when the caller needs to attach additional managed policies out-of-band."
  value       = aws_iam_role.this.name
}

output "policy_arn" {
  description = "ARN of the per-workload permissions policy created and attached by this module."
  value       = aws_iam_policy.this.arn
}
