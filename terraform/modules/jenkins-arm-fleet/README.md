# jenkins-arm-fleet

Diversified ARM (Graviton) EC2 spot pool consumed by the Jenkins **ec2-fleet
plugin** (`EC2FleetCloud`). It is the automatic AWS Graviton fallback for the
`docker-aarch64` workload when Hetzner ARM (CAX) capacity is unavailable
(PS-11179): a `CLOUD=auto` resolver routes to it via a controller health flag.

## Design decisions

- **Standalone module, one call per master.** Not folded into `jenkins-master`,
  so it also attaches to **still-CloudFormation masters** (ps57/ps80) by feeding
  their existing VPC/subnet/profile/role via `data` sources, without migrating
  the whole master to Terraform first.
- **ASG + `MixedInstancesPolicy`, not Spot Fleet.** AWS's recommended self-healing
  construct; the ec2-fleet plugin gets automatic AZ rebalancing only with an ASG.
  Spot Fleet is legacy.
- **`capacity-optimized` allocation across >= 3 same-size Graviton types**
  (m8g/m7g/m6g.2xlarge). A single type scores ~3 on the Spot Placement Score;
  the diversified pool scores ~9. capacity-optimized is the only strategy AWS
  documents as aligned with the measured SPS, and it is AWS's pick for CI.
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
module "ps3_arm_fleet" {
  source    = "./modules/jenkins-arm-fleet"
  providers = { aws = aws.eu-west-1 }

  short_name                   = "jenkins-ps3"
  vpc_id                       = module.ps3.vpc_id
  subnet_ids                   = module.ps3.subnet_ids
  worker_instance_profile_name = module.ps3.worker_instance_profile_name
  master_role_name             = module.ps3.master_iam_role_name
  key_name                     = "jenkins-ps3"
  instance_types               = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  tickets                      = "PS-11179"
}
```

Still-CloudFormation master (feed existing resource IDs via data sources):

```hcl
data "aws_vpc" "ps57"    { tags = { Name = "jenkins-ps57" } }
data "aws_subnets" "ps57" { filter { name = "vpc-id"; values = [data.aws_vpc.ps57.id] } }

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
```

Then point the master's ec2-fleet `EC2FleetCloud` at `module.<x>_arm_fleet.asg_name`
on the `docker-32gb-aarch64` label.
