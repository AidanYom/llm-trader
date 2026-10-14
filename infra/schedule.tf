# The daily run: 08:31 New York time, Monday to Friday. The run itself skips market holidays, recording the
# day as skipped without calling Claude.

resource "aws_iam_role" "scheduler" {
  name = "llm-trader-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name = "invoke-llm-trader"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.trader.arn
    }]
  })
}

resource "aws_scheduler_schedule" "daily" {
  name                         = "llm-trader-daily"
  description                  = "llm-trader's pre-market run"
  schedule_expression          = "cron(31 8 ? * MON-FRI *)"
  schedule_expression_timezone = "America/New_York"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.trader.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({}) # no mode, so the function's RUN_MODE applies

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
