# TRANSIENT: one-shot adopt of the retained pxc CFN data volume into the module
# address. Zero-diff only because ebs_type=gp2 + ebs_size=300 + az_index=1 (the
# us-west-1b volume's AZ) all match the live volume. REMOVE this file in the
# post-cutover follow-up PR once the import has applied (the ps57 precedent,
# commit 2446959). PS-11228.
import {
  to = module.pxc.aws_ebs_volume.data
  id = "vol-03b3852ad6dd6c553"
}
