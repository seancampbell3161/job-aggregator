# ---------- Hosted tailor endpoint (sub-project C-endpoint) ----------

variable "tailor_endpoint_image_uri" {
  description = "ECR image URI for the tailor-endpoint container (from build_endpoint_image.sh)"
  type        = string
  default     = ""
}

resource "aws_ecr_repository" "tailor_endpoint" {
  name                 = "${local.name}-tailor-endpoint"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_s3_bucket" "tailor_pdfs" {
  bucket_prefix = "${local.name}-tailored-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "tailor_pdfs" {
  bucket                  = aws_s3_bucket.tailor_pdfs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tailor_pdfs" {
  bucket = aws_s3_bucket.tailor_pdfs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "tailor_pdfs" {
  bucket = aws_s3_bucket.tailor_pdfs.id
  rule {
    id     = "expire-tailored-pdfs"
    status = "Enabled"
    filter { prefix = "tailored/" }
    expiration { days = 7 }
  }
}

resource "aws_ssm_parameter" "tailor_signing_secret" {
  name        = "/job-aggregator/tailor_signing_secret"
  type        = "SecureString"
  value       = "PLACEHOLDER" # set out-of-band: aws ssm put-parameter --overwrite ...
  description = "HMAC secret for tailor-endpoint deep-link tokens"
  lifecycle { ignore_changes = [value] }
}

data "aws_ssm_parameter" "tailor_signing_secret" {
  name       = aws_ssm_parameter.tailor_signing_secret.name
  depends_on = [aws_ssm_parameter.tailor_signing_secret]
}

resource "aws_iam_role" "tailor_endpoint" {
  name = "${local.name}-tailor-endpoint"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "tailor_endpoint" {
  name = "${local.name}-tailor-endpoint"
  role = aws_iam_role.tailor_endpoint.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:GetItem"], Resource = aws_dynamodb_table.seen_jobs.arn },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject", "s3:HeadObject"], Resource = "${aws_s3_bucket.tailor_pdfs.arn}/*" },
      { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = [
        aws_ssm_parameter.tailor_signing_secret.arn,
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/job-aggregator/ollama_api_key"
      ] },
      { Effect = "Allow", Action = ["kms:Decrypt"], Resource = "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm" },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" }
    ]
  })
}

resource "aws_lambda_function" "tailor_endpoint" {
  function_name = "${local.name}-tailor-endpoint"
  role          = aws_iam_role.tailor_endpoint.arn
  package_type  = "Image"
  image_uri     = var.tailor_endpoint_image_uri
  memory_size   = 1536
  timeout       = 120
  architectures = ["x86_64"]
  environment {
    variables = {
      JOB_AGG_OLLAMA_API_KEY        = data.aws_ssm_parameter.ollama_api_key.value
      JOB_AGG_TAILOR_SIGNING_SECRET = data.aws_ssm_parameter.tailor_signing_secret.value
      JOB_AGG_TAILOR_BUCKET         = aws_s3_bucket.tailor_pdfs.id
      JOB_AGG_SEEN_JOBS_TABLE       = aws_dynamodb_table.seen_jobs.name
      JOB_AGG_CONFIG_PATH           = "config.yaml"
      JOB_AGG_NTFY_TOPIC_URL        = "unused"
      JOB_AGG_DISCORD_WEBHOOK_URL   = "unused"
    }
  }
}

resource "aws_lambda_function_url" "tailor_endpoint" {
  function_name      = aws_lambda_function.tailor_endpoint.function_name
  authorization_type = "NONE" # our HMAC token is the gate; the phone has no AWS creds
}

# authorization_type=NONE still needs an explicit public-invoke grant — Terraform
# (unlike the console) does not add it automatically. Without this every call 403s.
resource "aws_lambda_permission" "tailor_endpoint_url" {
  statement_id           = "AllowPublicFunctionUrlInvoke"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.tailor_endpoint.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

output "tailor_endpoint_url" {
  value = aws_lambda_function_url.tailor_endpoint.function_url
}
