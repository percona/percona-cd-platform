packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "= 1.8.2"
    }
  }
}

# Fresh-boot smoke for an agent AMI candidate: boots the candidate, asserts the
# baked payload, and for arm64 proves x86_64 emulation end-to-end. A candidate
# is promoted (role tag flip) only after this build succeeds. skip_create_ami
# means only the boot + assertions matter, no by-product AMI exists, and
# concurrent matrix jobs cannot race on cleanup.

variable "candidate_ami" {
  type = string
}

variable "arch" {
  type = string
  validation {
    condition     = contains(["x86_64", "arm64"], var.arch)
    error_message = "The arch value must be x86_64 or arm64."
  }
}

variable "jenkins_url" {
  type    = string
  default = "https://pxb.cd.percona.com"
  validation {
    condition     = can(regex("^https://[^/]+$", var.jenkins_url))
    error_message = "The Jenkins URL must be an HTTPS origin without a trailing slash."
  }
}

variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "builder_instance_profile" {
  type    = string
  default = "jenkins-agent-builder-ssm"
}

variable "builder_sg_name" {
  type    = string
  default = "jenkins-agent-factory-builder"
}

variable "billing_tag" {
  type    = string
  default = "jenkins-agent-factory"
}

locals {
  timestamp     = regex_replace(timestamp(), "[- TZ:]", "")
  instance_type = var.arch == "arm64" ? "c7g.large" : "c6i.large"
}

source "amazon-ebs" "smoke" {
  region          = var.region
  source_ami      = var.candidate_ami
  instance_type   = local.instance_type
  ami_name        = "jenkins-agent-smoke-discard-${local.timestamp}"
  skip_create_ami = true

  communicator         = "ssh"
  ssh_username         = "ec2-user"
  ssh_interface        = "session_manager"
  iam_instance_profile = var.builder_instance_profile

  security_group_filter {
    filters = {
      "group-name" = var.builder_sg_name
    }
  }

  run_tags = {
    Name              = "jenkins-agent-smoke-${local.timestamp}"
    "iit-billing-tag" = var.billing_tag
    PerconaKeep       = "True"
    team              = "platform"
  }
  run_volume_tags = {
    "iit-billing-tag" = var.billing_tag
    PerconaKeep       = "True"
    team              = "platform"
  }
}

build {
  sources = ["source.amazon-ebs.smoke"]

  provisioner "shell" {
    env = {
      SMOKE_ARCH        = var.arch
      SMOKE_JENKINS_URL = var.jenkins_url
    }
    script = "smoke/verify.sh"
  }
}
