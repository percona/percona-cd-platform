packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "= 1.8.2"
    }
  }
}

# Parameterized Jenkins agent-image factory (strategy layer 1). One template
# serves every (os, arch, profile) combo; the first delivered images are
# jenkins-agent-al2023-{x86_64,arm64}, the arm64 profile carrying baked qemu
# user-mode emulation. Extend by adding an os/arch value plus a provisioner,
# not by copying the template.

variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "os_name" {
  type    = string
  default = "al2023"
}

variable "arch" {
  type = string
  validation {
    condition     = contains(["x86_64", "arm64"], var.arch)
    error_message = "The arch value must be x86_64 or arm64."
  }
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
  image_name    = "jenkins-agent-${var.os_name}-${var.arch}-${local.timestamp}"
  instance_type = var.arch == "arm64" ? "c7g.large" : "c6i.large"

  # Standard (not minimal) AL2023: the SSM agent ships preinstalled, which the
  # session_manager communicator and the fleet's shell access both rely on.
  base_ami_param = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-${var.arch}"

  common_tags = {
    Name              = local.image_name
    role              = "jenkins-agent-candidate"
    os                = var.os_name
    arch              = var.arch
    source            = "factory"
    "iit-billing-tag" = var.billing_tag
  }
}

data "amazon-parameterstore" "base_ami" {
  region = var.region
  name   = local.base_ami_param
}

source "amazon-ebs" "agent" {
  region        = var.region
  source_ami    = data.amazon-parameterstore.base_ami.value
  instance_type = local.instance_type
  ami_name      = local.image_name

  # SSM-only builder: no inbound SSH, the pre-created egress-only SG disables
  # Packer's temporary SG and the OIDC role carries no SG mutations.
  communicator         = "ssh"
  ssh_username         = "ec2-user"
  ssh_interface        = "session_manager"
  iam_instance_profile = var.builder_instance_profile

  security_group_filter {
    filters = {
      "group-name" = var.builder_sg_name
    }
  }

  launch_block_device_mappings {
    device_name           = "/dev/xvda"
    volume_size           = 20
    volume_type           = "gp3"
    delete_on_termination = true
  }

  run_tags      = merge(local.common_tags, { Name = "${local.image_name}-builder" })
  tags          = local.common_tags
  snapshot_tags = local.common_tags
}

build {
  sources = ["source.amazon-ebs.agent"]

  # 10-qemu-binfmt self-guards on uname and no-ops on x86_64 builders, so the
  # provisioner list stays arch-agnostic.
  provisioner "shell" {
    scripts = [
      "provisioners/00-common.sh",
      "provisioners/10-qemu-binfmt.sh",
    ]
  }

  # The workflow reads the AMI id from here, never from human-readable output.
  post-processor "manifest" {
    output = "manifest.json"
  }
}
