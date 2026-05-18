output "vpc_id" {
  description = "Master VPC ID, consumed by the caller's peering block."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "Master VPC CIDR, drives the EKS-side peering route."
  value       = aws_vpc.this.cidr_block
}

output "eip_allocation_id" {
  description = "Master EIP allocation ID. null when create_eip=false."
  value       = var.create_eip ? aws_eip.master[0].id : null
}

output "eip_public_ip" {
  description = "Master EIP public IPv4. null when create_eip=false."
  value       = var.create_eip ? aws_eip.master[0].public_ip : null
}

output "private_route_table_ids" {
  description = "Master VPC route table IDs (single element today, CF shares one RT)."
  value       = [aws_route_table.this.id]
}

output "master_iam_role_arn" {
  description = "Master IAM role ARN."
  value       = aws_iam_role.master.arn
}

output "master_iam_role_name" {
  description = "Master IAM role name."
  value       = aws_iam_role.master.name
}

output "worker_iam_role_arn" {
  description = "Worker IAM role ARN."
  value       = aws_iam_role.worker.arn
}

output "spot_fleet_request_id" {
  description = "SpotFleet request ID maintaining the master."
  value       = aws_spot_fleet_request.master.id
}

output "termination_queue_arn" {
  description = "Termination SQS ARN. null when create_termination_queue=false."
  value       = var.create_termination_queue ? aws_sqs_queue.termination[0].arn : null
}

output "data_volume_id" {
  description = "JENKINS_HOME EBS volume ID."
  value       = aws_ebs_volume.data.id
}
