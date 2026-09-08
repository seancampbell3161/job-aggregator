resource "aws_cloudwatch_event_rule" "ats" {
  name                = "${local.name}-ats"
  description         = "Fast tier: poll ATS every ${var.ats_schedule_minutes} minute(s)"
  schedule_expression = "rate(${var.ats_schedule_minutes} minute${var.ats_schedule_minutes == 1 ? "" : "s"})"
}

resource "aws_cloudwatch_event_rule" "slow" {
  name                = "${local.name}-slow"
  description         = "Slow tier: poll HN every ${var.slow_schedule_minutes} minutes"
  schedule_expression = "rate(${var.slow_schedule_minutes} minutes)"
}

resource "aws_cloudwatch_event_target" "ats" {
  rule  = aws_cloudwatch_event_rule.ats.name
  arn   = aws_lambda_function.poller.arn
  input = jsonencode({ tier = "ats" })
}

resource "aws_cloudwatch_event_target" "slow" {
  rule  = aws_cloudwatch_event_rule.slow.name
  arn   = aws_lambda_function.poller.arn
  input = jsonencode({ tier = "slow" })
}

resource "aws_lambda_permission" "ats" {
  statement_id  = "AllowEventBridgeAts"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ats.arn
}

resource "aws_lambda_permission" "slow" {
  statement_id  = "AllowEventBridgeSlow"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.slow.arn
}

resource "aws_cloudwatch_event_rule" "discovery" {
  name                = "${local.name}-discovery"
  description         = "Discovery tier: pull Hiring.cafe index every ${var.discovery_schedule_hours}h"
  schedule_expression = "rate(${var.discovery_schedule_hours} hour${var.discovery_schedule_hours == 1 ? "" : "s"})"
}

resource "aws_cloudwatch_event_target" "discovery" {
  rule  = aws_cloudwatch_event_rule.discovery.name
  arn   = aws_lambda_function.poller.arn
  input = jsonencode({ tier = "discovery" })
}

resource "aws_lambda_permission" "discovery" {
  statement_id  = "AllowEventBridgeDiscovery"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.discovery.arn
}

resource "aws_cloudwatch_event_rule" "digest" {
  name                = "${local.name}-digest"
  description         = "Weekly résumé-gap digest"
  schedule_expression = var.digest_schedule_expression
}

resource "aws_cloudwatch_event_target" "digest" {
  rule  = aws_cloudwatch_event_rule.digest.name
  arn   = aws_lambda_function.poller.arn
  input = jsonencode({ tier = "digest" })
}

resource "aws_lambda_permission" "digest" {
  statement_id  = "AllowEventBridgeDigest"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.digest.arn
}
