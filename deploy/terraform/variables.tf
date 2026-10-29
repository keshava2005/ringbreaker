variable "region" {
  description = "AWS region. Mumbai keeps payment data in India."
  type        = string
  default     = "ap-south-1"
}

variable "domain" {
  description = "Public hostname, e.g. ringbreaker.yourname.is-a.dev. Leave empty until the DNS record exists."
  type        = string
  default     = ""
}

variable "live_user" {
  description = "Username for the password-protected live app at /live"
  type        = string
  default     = "analyst"
}

variable "instance_type" {
  type    = string
  default = "t3.micro"
}

variable "bundle_path" {
  description = "Code bundle made by `python -m ringbreaker package` (run from the repo root)"
  type        = string
  default     = "../.build/ringbreaker-app.zip"
}

variable "param_prefix" {
  type    = string
  default = "/ringbreaker"
}

variable "alert_email" {
  description = "Email for the AWS Budgets alert. Leave empty to skip."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  type    = string
  default = "20"
}
