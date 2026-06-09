# Owner: cloud
# ARM Graviton spot fleet for cloud, Fleet-only (still-CloudFormation master).
# Provisions ONLY the worker ASG in Terraform; the cloud master stays CFN-managed in
# eu-west-1, so no CFN-to-TF cutover is required. The ec2-fleet plugin (installed
# on cloud in a separately announced idle window) drives the ASG via the CFN-managed
# jenkins-cloud-master role; the jenkins-arm-fleet module attaches the autoscaling IAM
# policy by role NAME, so no Terraform management of that role is required.
# Capacity-optimized across m8g/m7g/m6g.2xlarge. Folds into the later CFN->TF migration
# by swapping the data sources for module.cloud.* outputs.
data "aws_vpc" "cloud" {
  provider = aws.eu-west-1
  tags     = { Name = "jenkins-cloud" }
}

data "aws_subnets" "cloud" {
  provider = aws.eu-west-1
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.cloud.id]
  }
}

module "cloud_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.eu-west-1 }

  short_name                   = "jenkins-cloud"
  team                         = "cloud"
  vpc_id                       = data.aws_vpc.cloud.id
  subnet_ids                   = data.aws_subnets.cloud.ids
  worker_instance_profile_name = "jenkins-cloud-worker"
  master_role_name             = "jenkins-cloud-master"
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}
