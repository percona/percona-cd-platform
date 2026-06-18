# Owner: platform
# Per-region DLM snapshot policy for the EC2 Jenkins master DATA volumes.
# A single STRINGEQUALS target tag (snapshot-policy=jenkins-master, set on every
# master data volume and on its launch-template volume tag block in
# modules/jenkins-master) lets one policy cover every master in the region,
# replacing the hand-made console policies. Daily cadence with 14 retained plus
# a cross-region copy give an explicit 24h RPO that survives a single-region
# loss. The RPO floor and the three-vehicle split (DLM here, snapscheduler for
# the in-cluster controller, AWS Backup for cluster EBS) are ratified in
# docs/adr/0037-master-ebs-snapshot-rpo.md.
# DLM-created snapshots do not inherit provider default_tags, so the schedule
# re-asserts the reaper key (docs/runbooks/cleanup-reapers.md).

resource "aws_dlm_lifecycle_policy" "data" {
  description        = "Jenkins master DATA volumes ${var.region_label}"
  execution_role_arn = var.execution_role_arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]

    target_tags = {
      "snapshot-policy" = "jenkins-master"
    }

    schedule {
      name = "daily-retain-14"

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["03:30"]
      }

      retain_rule {
        count = 14
      }

      # copy_tags carries the volume's own tags onto each snapshot; tags_to_add
      # re-asserts the reaper key because runtime-created snapshots never see
      # provider default_tags.
      copy_tags = true
      tags_to_add = {
        "PerconaKeep"     = "True"
        "snapshot-policy" = "jenkins-master"
      }

      # Region-loss durability: copy each snapshot into the DR region and
      # re-encrypt it with a key the execution role can use there.
      dynamic "cross_region_copy_rule" {
        for_each = var.copy_target_region == null ? [] : [1]
        content {
          target    = var.copy_target_region
          encrypted = true
          cmk_arn   = var.copy_target_cmk_arn
          copy_tags = true

          retain_rule {
            interval      = 14
            interval_unit = "DAYS"
          }
        }
      }
    }
  }

  tags = var.tags
}
