# PS-11179: ARM Graviton spot fleet for ps57, Fleet-only.
# ps57's master is CFN-managed in eu-central-1; this provisions only the worker
# ASG in TF so no PS-11206-style master cutover is required. Capacity-optimized
# across m8g/m7g/m6g.2xlarge (eu-central-1 SPS ~9 vs ~3 single-type). The
# ec2-fleet plugin (to be installed on ps57 in a separately announced idle-window)
# drives the ASG via the CFN-managed jenkins-ps57-master role; the module attaches
# the autoscaling IAM policy by role NAME, so no TF management of that role is
# required.
data "aws_vpc" "ps57" {
  provider = aws.eu-central-1
  tags     = { Name = "jenkins-ps57" }
}

data "aws_subnets" "ps57" {
  provider = aws.eu-central-1
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.ps57.id]
  }
}

module "ps57_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.eu-central-1 }

  short_name                   = "jenkins-ps57"
  vpc_id                       = data.aws_vpc.ps57.id
  subnet_ids                   = data.aws_subnets.ps57.ids
  worker_instance_profile_name = "jenkins-ps57-worker"
  master_role_name             = "jenkins-ps57-master"
  key_name                     = "jenkins-ps57"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  tickets                      = "PS-11179"
}
