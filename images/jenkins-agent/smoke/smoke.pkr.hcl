packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = ">= 1.3.0"
    }
  }
}

# Fresh-boot smoke for an agent AMI candidate: boots the candidate, asserts the
# baked payload, and for arm64 proves x86_64 emulation end-to-end. A candidate
# is promoted (role tag flip) only after this build succeeds. The build
# produces a throwaway AMI name that the workflow deregisters immediately;
# only the boot + provisioner assertions matter.

variable "candidate_ami" {
  type = string
}

variable "arch" {
  type = string
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
  region        = var.region
  source_ami    = var.candidate_ami
  instance_type = local.instance_type
  ami_name      = "jenkins-agent-smoke-discard-${local.timestamp}"

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
  }

  tags = {
    role              = "jenkins-agent-smoke-discard"
    "iit-billing-tag" = var.billing_tag
  }
}

build {
  sources = ["source.amazon-ebs.smoke"]

  provisioner "shell" {
    env = {
      SMOKE_ARCH = var.arch
    }
    script = "smoke/verify.sh"
  }
}
