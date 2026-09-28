locals {
  function_name = "llm-trader"
  # SSM SecureString parameters under /llm-trader/, created by hand (README): no secret value is in Terraform.
  secrets = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY", "DATABASE_URL"]
}

# ---- The function's permissions: write its logs, and read and decrypt its own secrets ----

resource "aws_iam_role" "lambda" {
  name = "llm-trader-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# SSM's own encryption key. AWS creates it with the account's first SecureString parameter.
data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm"
}

resource "aws_iam_role_policy" "lambda_secrets" {
  name = "read-llm-trader-secrets"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/llm-trader/*"
      },
      {
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = data.aws_kms_alias.ssm.target_key_arn
      },
    ]
  })
}

# ---- The function ----

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 30
}

resource "aws_lambda_function" "trader" {
  function_name = local.function_name
  description   = "llm-trader's daily pre-market run"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.trader.repository_url}:${var.image_tag}"
  architectures = ["x86_64"]
  memory_size   = 512
  # Lambda's maximum. One Claude call can take 6 minutes (120 s, retried twice), and a submit run killed by the
  # timeout stays `running` until --force. Lambda bills the time used, not this limit.
  timeout = 900

  environment {
    variables = merge(
      {
        RUN_MODE     = var.run_mode
        LOG_LEVEL    = var.log_level
        ALPACA_PAPER = "true" # going live is a reviewed change to this line (HANDOFF §19)
      },
      { for name in local.secrets : "${name}_SSM" => "/llm-trader/${name}" },
    )
  }

  # The log group first, or Lambda would create it itself, with no retention.
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy_attachment.lambda_logs]
}

# EventBridge Scheduler invokes the function asynchronously, and by default Lambda retries a failed
# asynchronous invocation twice. A failed submit run doesn't block the day's next one, so a retry could trade
# twice: no retries. A run Lambda had to queue is dropped after 30 minutes, so it never starts after the open.
resource "aws_lambda_function_event_invoke_config" "trader" {
  function_name                = aws_lambda_function.trader.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 1800
}
