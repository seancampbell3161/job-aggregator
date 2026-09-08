data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${local.name}-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda_perms" {
  statement {
    sid = "DDB"
    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Scan",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
    ]
    resources = [
      aws_dynamodb_table.seen_jobs.arn,
      aws_dynamodb_table.source_state.arn,
      aws_dynamodb_table.discovered_slugs.arn,
      aws_dynamodb_table.connector_health.arn,
    ]
  }

  statement {
    sid     = "SSM"
    actions = ["ssm:GetParameter"]
    resources = [
      aws_ssm_parameter.ntfy_topic_url.arn,
      aws_ssm_parameter.discord_webhook_url.arn,
      aws_ssm_parameter.anthropic_api_key.arn,
      aws_ssm_parameter.google_api_key.arn,
      aws_ssm_parameter.ollama_api_key.arn,
    ]
  }

  statement {
    sid       = "KMSDecryptForSSM"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
  }

  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["JobAggregator"]
    }
  }

  statement {
    sid = "Logs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }

  statement {
    sid       = "LogsCreateGroup"
    actions   = ["logs:CreateLogGroup"]
    resources = [aws_cloudwatch_log_group.lambda.arn]
  }
}

resource "aws_iam_role_policy" "lambda_perms" {
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_perms.json
}
