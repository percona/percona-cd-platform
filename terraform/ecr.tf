# ECR repositories for platform-owned container images, namespaced under
# percona-cd/. The mtr-ingest image (images/mtr-ingest) is built and pushed
# here and run by the mtr-ingest CronJob (resources/addons/mtr, PS-10541).
# EKS nodes pull via the node role; no imagePullSecret needed.

resource "aws_ecr_repository" "mtr_ingest" {
  name                 = "percona-cd/mtr-ingest"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = var.tags
}

# jenkins-endpoint-reconciler image (images/jenkins-endpoint-reconciler): the
# CronJob that reconciles EC2 Jenkins master IPs into EndpointSlices for the
# jenkins-ingress proxy (ADR 0019). Standardised onto a built ECR image.
resource "aws_ecr_repository" "jenkins_endpoint_reconciler" {
  name                 = "percona-cd/jenkins-endpoint-reconciler"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = var.tags
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
