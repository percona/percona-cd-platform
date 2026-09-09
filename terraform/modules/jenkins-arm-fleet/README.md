# jenkins-arm-fleet

Diversified ARM (Graviton) EC2 spot pool consumed by the Jenkins **ec2-fleet
plugin** (`EC2FleetCloud`). It is the automatic AWS Graviton fallback for the
`docker-aarch64` workload when Hetzner ARM (CAX) capacity is unavailable
(PS-11179): a `CLOUD=auto` resolver routes to it via a controller health flag.

## Design decisions

- **Standalone module, one call per master.** Not folded into `jenkins-master`,
  so it can also attach to a master not managed by `jenkins-master` by feeding
  its existing VPC/subnet/profile/role via `data` sources. (Every master is now
  Terraform-managed, so this attach-to-external path is currently unused.)
- **ASG + `MixedInstancesPolicy`, not Spot Fleet.** AWS's recommended self-healing
  construct; the ec2-fleet plugin gets automatic AZ rebalancing only with an ASG.
  Spot Fleet is legacy.
- **`price-capacity-optimized` allocation across >= 3 same-size Graviton types**
  (m8g/m7g/m6g.2xlarge). A single type scores ~3 on the Spot Placement Score;
  the diversified pool scores ~9. The pool started on `capacity-optimized`, but
  that concentrated the whole fleet into one pool (one pool squeeze took out three
  workers in 25 minutes), so it now uses `price-capacity-optimized`, which spreads
  across pools while still honouring the capacity signal.
- **Terraform owns the pool shape; the plugin owns `DesiredCapacity` and
  `protect_from_scale_in`.** The ec2-fleet plugin sets `protect_from_scale_in=true`
  at runtime so it (not ASG scale-in) controls termination, so a full apply would
  otherwise show `protect_from_scale_in true->false` churn. Hence
  `lifecycle { ignore_changes = [desired_capacity, protect_from_scale_in] }`;
  `min_size`/`max_size` stay Terraform-managed guardrails (deliberately NOT ignored).
- **Tickets are a tag, not a name.** Resource names are `${short_name}-arm-*`;
  provenance lives in the `tickets` tag (comma-separated for multiple).
- **Cleanup-safe.** Workers carry `iit-billing-tag = short_name` so cleanup
  Lambdas do not reap them.
- **Agent parity.** The launch-template user-data is arch-aware (no hardcoded
  `x86_64`) and chowns `/mnt/jenkins` to `ec2-user`, matching the legacy
  ec2-plugin initScript behaviour that the ec2-fleet SSH launcher depends on.

## Usage

Terraform-managed master (consume the master module's outputs):

```hcl
module "ps80_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.us-west-2 }

  short_name                   = "jenkins-ps80"
  vpc_id                       = module.ps80.vpc_id
  subnet_ids                   = module.ps80.subnet_ids
  worker_instance_profile_name = module.ps80.worker_instance_profile_name
  master_role_name             = module.ps80.master_iam_role_name
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}
```

> **Note**
> `ps3` is now an in-cluster controller, not an EC2 master, so it does not consume
> this attach-to-existing module. Its Graviton pool plus the surviving VPC and
> worker IAM substrate live in the sibling `jenkins-arm-standalone` module, which
> owns that substrate outright (see `terraform/master-ps3.tf`).

Master not managed by `jenkins-master` (feed existing resource IDs via data sources):

```hcl
data "aws_vpc" "<inst>"     { tags = { Name = "jenkins-<inst>" } }
data "aws_subnets" "<inst>" { filter { name = "vpc-id"; values = [data.aws_vpc.<inst>.id] } }

module "<inst>_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.<region> }

  short_name                   = "jenkins-<inst>"
  vpc_id                       = data.aws_vpc.<inst>.id
  subnet_ids                   = data.aws_subnets.<inst>.ids
  worker_instance_profile_name = "jenkins-<inst>-worker"
  master_role_name             = "jenkins-<inst>-master"
  key_name                     = "percona-jenkins"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  max_size                     = 16
  tickets                      = "PS-11179"
}
```

Then point the master's ec2-fleet `EC2FleetCloud` at `module.<x>_arm_fleet.asg_name`
on the `docker-32gb-aarch64` label.
