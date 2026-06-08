# PS-11179: ARM Graviton spot fleet for pg, Fleet-only (still-CloudFormation master).
# Provisions ONLY the worker ASG in Terraform; the pg master stays CFN-managed in
# eu-central-1, so no PS-11206-style cutover is required. The ec2-fleet plugin (installed
# on pg in a separately announced idle window) drives the ASG via the CFN-managed
# jenkins-pg-master role; the jenkins-arm-fleet module attaches the autoscaling IAM
# policy by role NAME, so no Terraform management of that role is required.
# Capacity-optimized across m8g/m7g/m6g.2xlarge. Folds into the later CFN->TF migration
# by swapping the data sources for module.pg.* outputs.
data "aws_vpc" "pg" {
  provider = aws.eu-central-1
  tags     = { Name = "jenkins-pg" }
}

data "aws_subnets" "pg" {
  provider = aws.eu-central-1
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.pg.id]
  }
}

module "pg_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.eu-central-1 }

  short_name                   = "jenkins-pg"
  vpc_id                       = data.aws_vpc.pg.id
  subnet_ids                   = data.aws_subnets.pg.ids
  worker_instance_profile_name = "jenkins-pg-worker"
  master_role_name             = "jenkins-pg-master"
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}
