resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 7
}

# Read the SSM SecureString values live at apply time. The aws_ssm_parameter
# resources have lifecycle.ignore_changes=[value] so their in-state values are
# the placeholders from initial apply — referencing those would bake the
# placeholder into Lambda forever. Data sources always read live.
data "aws_ssm_parameter" "ntfy_topic_url" {
  name       = aws_ssm_parameter.ntfy_topic_url.name
  depends_on = [aws_ssm_parameter.ntfy_topic_url]
}

data "aws_ssm_parameter" "discord_webhook_url" {
  name       = aws_ssm_parameter.discord_webhook_url.name
  depends_on = [aws_ssm_parameter.discord_webhook_url]
}

data "aws_ssm_parameter" "anthropic_api_key" {
  name       = aws_ssm_parameter.anthropic_api_key.name
  depends_on = [aws_ssm_parameter.anthropic_api_key]
}

data "aws_ssm_parameter" "google_api_key" {
  name       = aws_ssm_parameter.google_api_key.name
  depends_on = [aws_ssm_parameter.google_api_key]
}

data "aws_ssm_parameter" "ollama_api_key" {
  name       = aws_ssm_parameter.ollama_api_key.name
  depends_on = [aws_ssm_parameter.ollama_api_key]
}

resource "aws_lambda_function" "poller" {
  function_name    = local.name
  role             = aws_iam_role.lambda_exec.arn
  runtime          = "python3.12"
  handler          = "src.handler.handler"
  filename         = "${path.module}/../build/lambda.zip"
  source_code_hash = filebase64sha256("${path.module}/../build/lambda.zip")

  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_s
  architectures = ["x86_64"]

  environment {
    variables = {
      JOB_AGG_NTFY_TOPIC_URL         = data.aws_ssm_parameter.ntfy_topic_url.value
      JOB_AGG_DISCORD_WEBHOOK_URL    = data.aws_ssm_parameter.discord_webhook_url.value
      JOB_AGG_ANTHROPIC_API_KEY      = data.aws_ssm_parameter.anthropic_api_key.value
      JOB_AGG_GOOGLE_API_KEY         = data.aws_ssm_parameter.google_api_key.value
      JOB_AGG_OLLAMA_API_KEY         = data.aws_ssm_parameter.ollama_api_key.value
      JOB_AGG_SEEN_JOBS_TABLE        = aws_dynamodb_table.seen_jobs.name
      JOB_AGG_SOURCE_STATE_TABLE     = aws_dynamodb_table.source_state.name
      JOB_AGG_DISCOVERED_SLUGS_TABLE = aws_dynamodb_table.discovered_slugs.name
      JOB_AGG_CONNECTOR_HEALTH_TABLE = aws_dynamodb_table.connector_health.name
      JOB_AGG_CONFIG_PATH            = "config.yaml"
      JOB_AGG_BACKEND                = "dynamodb"
      # C-wiring: lets the detector build the signed "Tailor resume" deep-link in alerts.
      JOB_AGG_TAILOR_ENDPOINT_URL   = aws_lambda_function_url.tailor_endpoint.function_url
      JOB_AGG_TAILOR_SIGNING_SECRET = data.aws_ssm_parameter.tailor_signing_secret.value
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}
