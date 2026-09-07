# Owner: pmm
# Tag every public subnet of the legacy unmanaged PMM staging VPC, located
# through the pmm security group the AMI staging job launches with. The
# job's subnet-fallback loop selects launch subnets by this tag value, so
# managing the tags here keeps the multi-AZ pool declarative and restores
# it if a tag is ever stripped.

data "aws_security_group" "pmm_staging" {
  name = "pmm"
}

data "aws_subnets" "pmm_ami_staging" {
  filter {
    name   = "vpc-id"
    values = [data.aws_security_group.pmm_staging.vpc_id]
  }
  filter {
    name   = "map-public-ip-on-launch"
    values = ["true"]
  }
}

resource "aws_ec2_tag" "pmm_ami_staging_start" {
  for_each    = toset(data.aws_subnets.pmm_ami_staging.ids)
  resource_id = each.value
  key         = "Name"
  value       = "pmm-ami-staging-start"
}
