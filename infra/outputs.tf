output "lambda_function_name" {
  value = aws_lambda_function.poller.function_name
}

output "seen_jobs_table" {
  value = aws_dynamodb_table.seen_jobs.name
}
