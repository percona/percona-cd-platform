# Owner: release
# ARM Graviton spot fleet for rel, Fleet-only (still-CloudFormation master).
# Provisions ONLY the worker ASG in Terraform; the rel master stays CFN-managed in
# eu-west-1, so no CFN-to-TF cutover is required. The ec2-fleet plugin (installed
# on rel in a separately announced idle window) drives the ASG via the CFN-managed
# jenkins-rel-master role; the jenkins-arm-fleet module attaches the autoscaling IAM
# policy by role NAME, so no Terraform management of that role is required.
# Capacity-optimized across m8g/m7g/m6g.2xlarge. Folds into the later CFN->TF migration
# by swapping the data sources for module.rel.* outputs.
data "aws_vpc" "rel" {
  provider = aws.eu-west-1
  tags     = { Name = "jenkins-rel" }
}

data "aws_subnets" "rel" {
  provider = aws.eu-west-1
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.rel.id]
  }
}

module "rel_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.eu-west-1 }

  short_name                   = "jenkins-rel"
  team                         = "release"
  vpc_id                       = data.aws_vpc.rel.id
  subnet_ids                   = data.aws_subnets.rel.ids
  worker_instance_profile_name = "jenkins-rel-worker"
  master_role_name             = "jenkins-rel-master"
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}
