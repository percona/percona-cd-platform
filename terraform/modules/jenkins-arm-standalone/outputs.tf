output "vpc_id" {
  description = "VPC ID, consumed by the EKS<->ps3 peering block."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "VPC CIDR, drives the EKS-side peering route."
  value       = aws_vpc.this.cidr_block
}

output "private_route_table_ids" {
  description = "Route table IDs (single element; matches module.ps3's output so the reverse peering route for_each key is stable)."
  value       = [aws_route_table.this.id]
}

output "subnet_ids" {
  description = "Subnet IDs (one per AZ)."
  value       = [for s in aws_subnet.this : s.id]
}

output "worker_instance_profile_name" {
  description = "Worker EC2 instance profile name."
  value       = aws_iam_instance_profile.worker.name
}

output "worker_iam_role_arn" {
  description = "Worker IAM role ARN."
  value       = aws_iam_role.worker.arn
}

output "asg_name" {
  description = "ARM Graviton spot fleet ASG name (the ec2-fleet plugin EC2FleetCloud fleet field)."
  value       = aws_autoscaling_group.this.name
}

output "security_group_id" {
  description = "Worker security group ID."
  value       = aws_security_group.this.id
}

output "launch_template_id" {
  description = "Worker launch template ID."
  value       = aws_launch_template.this.id
}
