# Owner: platform
# ECR repositories for platform-owned container images, namespaced under
# percona-cd/. The mtr-ingest image (images/mtr-ingest) is built and pushed
# here and run by the mtr-ingest CronJob (resources/addons/mtr).
# EKS nodes pull via the node role; no imagePullSecret needed.

resource "aws_ecr_repository" "mtr_ingest" {
  name                 = "percona-cd/mtr-ingest"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# jenkins-endpoint-reconciler image (images/jenkins-endpoint-reconciler): the
# CronJob that reconciles EC2 Jenkins master IPs into EndpointSlices for the
# jenkins-ingress proxy (ADR 0019). Standardised onto a built ECR image.
resource "aws_ecr_repository" "jenkins_endpoint_reconciler" {
  name                 = "percona-cd/jenkins-endpoint-reconciler"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Keep storage bounded; both images are small and rebuilt rarely.
resource "aws_ecr_lifecycle_policy" "mtr_ingest" {
  repository = aws_ecr_repository.mtr_ingest.name
  policy     = local.ecr_keep_last_10
}

resource "aws_ecr_lifecycle_policy" "jenkins_endpoint_reconciler" {
  repository = aws_ecr_repository.jenkins_endpoint_reconciler.name
  policy     = local.ecr_keep_last_10
}

locals {
  ecr_keep_last_10 = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire all but the 10 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# Jenkins controller image (images/jenkins): the baked Jenkins master image
# (community plugins via jenkins-plugin-cli + the two Percona fork HPIs),
# consumed by the in-cluster Jenkins master chart. Built/pushed by the
# build-jenkins-image GHA workflow (push-to-main only) via the scoped OIDC
# role in iam-gha-jenkins-image-push.tf. Tagged <lts>-<gitsha> (immutable).
resource "aws_ecr_repository" "jenkins_percona" {
  name                 = "percona-cd/jenkins-percona"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# SAFE lifecycle: expire ONLY UNTAGGED images, and ONLY after they have aged
# (sinceImagePushed). NEVER count-based (imageCountMoreThan) and NEVER
# tagStatus=any — ECR has zero knowledge of which digests Argo / a rollback
# doc / a DR runbook still references, so a count rule could expire a tagged
# digest that is still deployed. Every DEPLOYED digest stays TAGGED
# (<lts>-<gitsha>, immutable) and is therefore never matched by this rule.
# Untagged images here are only build-time orphans (re-pushed manifest lists,
# replaced layers) safe to reap after the grace window.
resource "aws_ecr_lifecycle_policy" "jenkins_percona" {
  repository = aws_ecr_repository.jenkins_percona.name
  policy     = local.ecr_expire_untagged_after_14d
}

locals {
  ecr_expire_untagged_after_14d = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire UNTAGGED images older than 14 days; never touches tagged (deployed) digests."
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 14
      }
      action = { type = "expire" }
    }]
  })
}
