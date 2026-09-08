resource "aws_dynamodb_table" "seen_jobs" {
  name         = "seen_jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery { enabled = false }
}

resource "aws_dynamodb_table" "source_state" {
  name         = "source_state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "connector_name"

  attribute {
    name = "connector_name"
    type = "S"
  }

  point_in_time_recovery { enabled = false }
}

resource "aws_dynamodb_table" "discovered_slugs" {
  name         = "discovered_slugs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "connector_name"

  attribute {
    name = "connector_name"
    type = "S"
  }

  point_in_time_recovery { enabled = false }
}

resource "aws_dynamodb_table" "connector_health" {
  name         = "connector_health"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "connector_name"

  attribute {
    name = "connector_name"
    type = "S"
  }

  point_in_time_recovery { enabled = false }
}
