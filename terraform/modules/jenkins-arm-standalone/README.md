# jenkins-arm-standalone

Self-contained ARM (Graviton) worker substrate for a Jenkins master that has **no
EC2 master of its own**. The sibling `jenkins-arm-fleet` module *attaches* a
Graviton pool to an existing master's VPC, subnets, and worker IAM. This module
instead **owns** that substrate: the VPC, two subnets, internet gateway, route
table, S3 gateway endpoint, and the worker IAM role, policy, and instance profile,
together with the diversified Graviton ASG.

It exists for `ps3`. `ps3` moved from an EC2 spot master to an in-cluster
controller (`jenkins-ps3-k8s` on EKS), but its builds still need a
`docker-32gb-aarch64` Graviton fallback when Hetzner CAX capacity is short. The
in-cluster controller drives the ASG through the **ec2-fleet plugin** and reaches
the workers by private IP over the EKS-to-ps3 VPC peering, authenticated by the
controller's EKS Pod Identity role. When the EC2 master was retired, its VPC,
subnets, and worker IAM were re-parented into this module with `moved` blocks, a
zero-diff state re-key, so the network identity the GitOps config depends on is
preserved byte for byte.

## Design

- **Owns the substrate.** This module is the source of truth for the VPC and
  worker IAM, so it has no `master_role_name` input and no master-side autoscaling
  policy. The in-cluster controller's Pod Identity role grants the autoscaling
  actions instead.
- **Zero-diff re-parent.** `vpc_cidr` must equal the existing master VPC being
  moved in (`10.181.0.0/22` for ps3) so the `moved` blocks re-key state without
  recreating anything.
- **ASG shape versus runtime.** Terraform owns `min_size`, `max_size`, and the
  diversified `price-capacity-optimized` Graviton pool. The ec2-fleet plugin owns
  `DesiredCapacity` and `protect_from_scale_in`, both `ignore_changes`d, matching
  `jenkins-arm-fleet`.
- **Cleanup-safe.** Workers carry `iit-billing-tag = short_name` and
  `PerconaKeep`, so the account cleanup Lambdas do not reap them.

## Inputs

| Name | Description |
|------|-------------|
| `short_name` | Master short name (e.g. `jenkins-ps3`); prefixes resource names and sets `iit-billing-tag`. |
| `vpc_cidr` | VPC CIDR. Must equal the existing master VPC being moved in, so the re-parent is zero-diff. |
| `cache_bucket_name` | Worker build-cache S3 bucket. `null` disables the worker S3 IAM statement. |
| `key_name` | EC2 key pair for workers; the ec2-fleet SSH launcher uses the matching Jenkins credential. |
| `instance_types` | Graviton types for the diversified pool (three or more same-size types recommended). |
| `max_size` | ASG maximum. The ec2-fleet plugin scales `DesiredCapacity` within `[0, max_size]`. |
| `extra_ssh_cidrs` | Extra CIDRs allowed to SSH to workers, e.g. the controller's EKS VPC CIDR for the peering connection. |
| `data_volume_gb` | Size (GiB) of the `/mnt` build and docker data volume on workers. |
| `ami_id` | Override the worker AMI. `null` resolves the latest Amazon Linux 2 arm64 AMI in the region. |
| `tickets` | Tracking tickets (comma-separated), recorded in the `tickets` tag. |
| `team` | Owning product team, recorded in the `team` tag on every resource and runtime-spawned instance/volume. Default `platform`; allowed values are the Owner set enforced by `scripts/check_conventions.py`. |
| `tags` | Extra tags merged onto all resources. |

## Outputs

`vpc_id`, `vpc_cidr`, and `private_route_table_ids` feed the cross-region peering
in `master-ps3.tf`; the `private_route_table_ids` shape matches the retired
master's output so the reverse-route `for_each` key stays stable. `asg_name` is
the `EC2FleetCloud` fleet field. `subnet_ids`, `worker_instance_profile_name`,
`worker_iam_role_arn`, `security_group_id`, and `launch_template_id` are also
exported.

## Usage

```hcl
module "ps3_arm_fleet" {
  source    = "./modules/jenkins-arm-standalone"
  providers = { aws = aws.eu-west-1 }

  short_name        = "jenkins-ps3"
  vpc_cidr          = "10.181.0.0/22"
  cache_bucket_name = "ps-build-cache"
  key_name          = "percona-jenkins"
  instance_types    = ["m8g.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]
  max_size          = 16
  extra_ssh_cidrs   = [module.vpc.vpc_cidr_block]
  tickets           = "PS-11179"
}
```
