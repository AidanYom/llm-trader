variable "image_tag" {
  description = "The Lambda image's tag in ECR: the short commit hash that make deploy builds and pushes."
  type        = string
}

variable "run_mode" {
  description = "RUN_MODE for the scheduled runs: dry_run records orders without sending them, submit sends them."
  type        = string
  default     = "dry_run"

  validation {
    condition     = contains(["dry_run", "submit"], var.run_mode)
    error_message = "run_mode must be dry_run or submit."
  }
}

variable "alert_email" {
  description = "Where the alarm and budget emails go. Set in the gitignored local.auto.tfvars, never committed."
  type        = string
}

variable "log_level" {
  description = "LOG_LEVEL for the function."
  type        = string
  default     = "INFO"
}

variable "monthly_budget_usd" {
  description = "The account's AWS spend per month, actual or forecast, above which the budget emails."
  type        = number
  default     = 5
}
