provider "aws" {
  region = local.region

  default_tags {
    tags = {
      Project = "llm-trader"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  region     = "us-east-1"
  account_id = data.aws_caller_identity.current.account_id
}
