variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "lambda_memory_mb" {
  type    = number
  default = 1024
}

variable "lambda_timeout_s" {
  type    = number
  default = 120
}

variable "ats_schedule_minutes" {
  type    = number
  default = 2
}

variable "slow_schedule_minutes" {
  type    = number
  default = 15
}

variable "alarm_email" {
  description = "Email for the CloudWatch alarm SNS topic."
  type        = string
}

variable "discovery_schedule_hours" {
  type    = number
  default = 24
}

variable "digest_schedule_expression" {
  description = "EventBridge schedule for the weekly résumé-gap digest."
  type        = string
  default     = "cron(0 16 ? * MON *)" # Mondays 16:00 UTC (~09:00 PT)
}
