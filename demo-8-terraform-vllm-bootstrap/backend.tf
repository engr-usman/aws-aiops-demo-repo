# Remote state in S3, with S3's native locking (use_lockfile) — no DynamoDB
# table required. Requires Terraform >= 1.10.
#
# NOTE: backend blocks cannot reference variables or use interpolation —
# values here must be literal. If your bucket lives in a different region
# than var.aws_region's default (us-east-1), update `region` below to match
# the BUCKET's region, not necessarily the instance's region.

terraform {
  backend "s3" {
    bucket       = "aiops-terraform-eks-cluster"
    key          = "gpu-instance/terraform.tfstate"
    region       = "eu-central-1"
    encrypt      = true
    use_lockfile = true
  }
}
