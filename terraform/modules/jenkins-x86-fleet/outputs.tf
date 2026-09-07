output "asg_name" {
  description = "x86_64 spot fleet ASG name. Feed this to the ec2-fleet plugin EC2FleetCloud `fleet` field."
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
