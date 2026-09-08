variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "name_suffix" {
  description = "Suffix to disambiguate per-account (e.g. AWS account ID)."
  type        = string
}
