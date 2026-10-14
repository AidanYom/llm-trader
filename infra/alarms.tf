# Emails when something needs attention. AWS sends a confirmation link to alert_email on the first deploy,
# and nothing reaches it until the link is clicked.

resource "aws_sns_topic" "alerts" {
  name = "llm-trader-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# A failed invocation: a run that raised (run_daily re-raises after marking it failed), a bad event or a
# timeout. The 5-minute period emails within minutes, and the alarm resets itself, so the next error emails too.
resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "llm-trader-errors"
  alarm_description   = "The llm-trader function failed. See make logs, and the run's error in the runs table."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.trader.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# A run that logged an error without failing, such as an order Alpaca refused or a partially filled entry
# whose stop couldn't be re-placed. It matches the JSON logs' level, not their wording, so rewording a
# message can't silently disable it. "run failed" is left out: the errors alarm already covers failed runs.
resource "aws_cloudwatch_log_metric_filter" "error_logs" {
  name           = "llm-trader-error-logs"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "{ ($.level = \"ERROR\") && ($.message != \"run failed\") }"

  metric_transformation {
    name      = "ErrorLogs"
    namespace = "llm-trader"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "error_logs" {
  alarm_name          = "llm-trader-error-logs"
  alarm_description   = "An llm-trader run logged an error, such as a refused order or shares left without a stop. See make logs."
  namespace           = "llm-trader"
  metric_name         = aws_cloudwatch_log_metric_filter.error_logs.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# The whole account's spend, so any unexpected AWS cost emails, whatever caused it.
resource "aws_budgets_budget" "monthly" {
  name         = "aws-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
