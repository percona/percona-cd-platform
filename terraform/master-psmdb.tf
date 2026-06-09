# Owner: mongodb
# ARM Graviton spot fleet for psmdb, Fleet-only (still-CloudFormation master).
# Provisions ONLY the worker ASG in Terraform; the psmdb master stays CFN-managed in
# us-west-2, so no CFN-to-TF cutover is required. The ec2-fleet plugin (installed
# on psmdb in a separately announced idle window) drives the ASG via the CFN-managed
# jenkins-psmdb-master role; the jenkins-arm-fleet module attaches the autoscaling IAM
# policy by role NAME, so no Terraform management of that role is required.
# Capacity-optimized across m8g/m7g/m6g.2xlarge. Folds into the later CFN->TF migration
# by swapping the data sources for module.psmdb.* outputs.
#
# NOTE: psmdb's worker instance profile is jenkins-psmdb-slave (older naming), not
# jenkins-psmdb-worker like the other masters.
data "aws_vpc" "psmdb" {
  provider = aws.us-west-2
  tags     = { Name = "jenkins-psmdb" }
}

data "aws_subnets" "psmdb" {
  provider = aws.us-west-2
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.psmdb.id]
  }
}

module "psmdb_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.us-west-2 }

  short_name                   = "jenkins-psmdb"
  vpc_id                       = data.aws_vpc.psmdb.id
  subnet_ids                   = data.aws_subnets.psmdb.ids
  worker_instance_profile_name = "jenkins-psmdb-slave"
  master_role_name             = "jenkins-psmdb-master"
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}
