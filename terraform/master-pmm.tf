# Owner: pmm
# ARM Graviton spot fleet for pmm, Fleet-only (still-CloudFormation master).
# Provisions ONLY the worker ASG in Terraform; the pmm master stays CFN-managed in
# us-east-2, so no CFN-to-TF cutover is required. The ec2-fleet plugin drives the
# ASG via the CFN-managed jenkins-pmm-amzn2-master role; the jenkins-arm-fleet
# module attaches the autoscaling IAM policy by role NAME, so no Terraform
# management of that role is required.
# Folds into the later CFN->TF migration by swapping the data sources for
# module.pmm.* outputs.
#
# NOTE: pmm's CFN stack uses the older `jenkins-pmm-amzn2` naming for the VPC
# tag, the master role, and the worker instance profile. The ASG itself keeps
# the fleet-uniform `jenkins-pmm-arm-graviton` name via short_name.
data "aws_vpc" "pmm" {
  provider = aws.us-east-2
  tags     = { Name = "jenkins-pmm-amzn2" }
}

data "aws_subnets" "pmm" {
  provider = aws.us-east-2
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.pmm.id]
  }
}

module "pmm_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.us-east-2 }

  short_name                   = "jenkins-pmm"
  team                         = "pmm"
  vpc_id                       = data.aws_vpc.pmm.id
  subnet_ids                   = data.aws_subnets.pmm.ids
  worker_instance_profile_name = "jenkins-pmm-amzn2-worker"
  master_role_name             = "jenkins-pmm-amzn2-master"
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge", "m7gd.2xlarge", "m6gd.2xlarge", "r8g.2xlarge", "r7g.2xlarge", "r6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}
