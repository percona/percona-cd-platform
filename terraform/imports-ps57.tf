# One-shot adopt of the retained CFN JENKINS_HOME volume into the TF master
# module (PS-11206). vol-07070c2c983c2cc5f is 100 GiB gp2 in eu-central-1b and
# has carried ps57's JENKINS_HOME since 2019. Its CFN DeletionPolicy was flipped
# Snapshot -> Retain before delete-stack, so the volume survives the CFN teardown
# and this import adopts it in place (az/size/type match module config -> no
# replacement). Remove this block once the cutover apply has imported it.
import {
  to = module.ps57.aws_ebs_volume.data
  id = "vol-07070c2c983c2cc5f"
}
