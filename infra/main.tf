terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  # Placeholder values below are overridden at `terraform init` via -backend-config:
  #   -backend-config="bucket=tf-state-job-aggregator-<acct>"
  #   -backend-config="key=job-aggregator/terraform.tfstate"
  #   -backend-config="region=us-east-1"
  #   -backend-config="dynamodb_table=tf-locks-job-aggregator"
  #   -backend-config="encrypt=true"
  backend "s3" {
    bucket = "REPLACE_AT_INIT"
    key    = "job-aggregator/terraform.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

locals {
  name = "job-aggregator"
}
