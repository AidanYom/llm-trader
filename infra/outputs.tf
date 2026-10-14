output "ecr_repository_url" {
  description = "Where make push sends the Lambda image."
  value       = aws_ecr_repository.trader.repository_url
}

output "function_name" {
  value = aws_lambda_function.trader.function_name
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
