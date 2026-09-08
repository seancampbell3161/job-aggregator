# SecureString parameter SHELLS — values are populated out-of-band:
#   aws ssm put-parameter --name /job-aggregator/ntfy_topic_url \
#     --type SecureString --value <url> --overwrite
#
# Terraform creates the parameter with a placeholder; subsequent updates of the
# value should NOT cause TF drift, hence the lifecycle ignore_changes on `value`.

resource "aws_ssm_parameter" "ntfy_topic_url" {
  name        = "/job-aggregator/ntfy_topic_url"
  type        = "SecureString"
  value       = "PLACEHOLDER_SET_VIA_AWS_CLI"
  description = "ntfy.sh topic URL for push notifications"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "discord_webhook_url" {
  name        = "/job-aggregator/discord_webhook_url"
  type        = "SecureString"
  value       = "PLACEHOLDER_SET_VIA_AWS_CLI"
  description = "Discord webhook URL"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "anthropic_api_key" {
  name  = "/job-aggregator/anthropic_api_key"
  type  = "SecureString"
  value = "REPLACE_ME_AT_DEPLOY"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "google_api_key" {
  name        = "/job-aggregator/google_api_key"
  type        = "SecureString"
  value       = "REPLACE_ME_AT_DEPLOY"
  description = "Google Gen AI API key (only used when relevance.provider=gemini)"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "ollama_api_key" {
  name        = "/job-aggregator/ollama_api_key"
  type        = "SecureString"
  value       = "REPLACE_ME_AT_DEPLOY"
  description = "Ollama Cloud API key (only used when relevance.provider=ollama)"

  lifecycle {
    ignore_changes = [value]
  }
}
