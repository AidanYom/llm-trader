# Terraform describes production's AWS resources (HANDOFF §16). It runs in the pinned hashicorp/terraform
# image through make (make tf, make plan, make deploy); nothing is installed on the host.

terraform {
  required_version = ">= 1.11" # S3 state locking (use_lockfile)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.66" # the exact version and its checksums are in .terraform.lock.hcl
    }
  }

  # The state records what Terraform created. Its bucket was created once by hand (README), with a random
  # suffix instead of the account ID. use_lockfile stops two applies from running at once.
  backend "s3" {
    bucket       = "llm-trader-tfstate-7fc29959"
    key          = "llm-trader/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
