output "function_arn" {
  description = "ARN of the scheduled Lambda function."
  value       = aws_lambda_function.this.arn
}

output "function_name" {
  description = "Name of the scheduled Lambda function (= name_prefix + name). Handy for `aws lambda invoke` during a DRY_RUN bake-in."
  value       = aws_lambda_function.this.function_name
}

output "role_arn" {
  description = "ARN of the function execution role."
  value       = aws_iam_role.this.arn
}

output "role_name" {
  description = "Name of the function execution role."
  value       = aws_iam_role.this.name
}

output "log_group_name" {
  description = "CloudWatch log group the function writes to."
  value       = aws_cloudwatch_log_group.this.name
}

output "event_rule_arn" {
  description = "ARN of the EventBridge rule that triggers the function on schedule."
  value       = aws_cloudwatch_event_rule.this.arn
}
