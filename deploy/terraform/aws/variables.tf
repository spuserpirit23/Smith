variable "region" {
  description = "AWS region. ap-south-1 (Mumbai) is closest to Jaipur; verify Transcribe/Polly/Bedrock model availability there."
  type        = string
  default     = "ap-south-1"
}

variable "env" {
  description = "Environment name (used in names and the SSM prefix /mitra/<env>/)."
  type        = string
  default     = "proto"
}

variable "instance_type" {
  description = <<-EOT
    Graviton instance. The LLM, vision model and TTS run on its CPU, so use a non-burstable type
    (t-series burst credits drain under sustained inference). m7g.xlarge (4 vCPU, 16 GiB) holds the
    chat + vision models with headroom; c7g.xlarge (4 vCPU, 8 GiB) fits with vision off. More vCPUs
    = faster replies. See docs/LOCAL_MODELS.md.
  EOT
  type        = string
  default     = "m7g.xlarge"
}

variable "disk_gb" {
  description = "Root volume: OS + images + Postgres + local model weights (~5 GB for the defaults)."
  type        = number
  default     = 40
}

variable "domain" {
  description = "DNS name pointing at the Elastic IP. Leave empty to use <ip>.sslip.io."
  type        = string
  default     = ""
}

variable "acme_email" {
  description = "Email for Let's Encrypt registration."
  type        = string
}

variable "repo_url" {
  description = "Git URL the instance clones on first boot. Leave empty and use deploy/scripts/push_deploy.sh instead (e.g. for a private repo)."
  type        = string
  default     = ""
}

variable "repo_ref" {
  type    = string
  default = "main"
}

variable "mitra_extras" {
  description = "Backend optional extras baked into the image (local, speech, aws, wake, opus)."
  type        = string
  default     = "local,speech"
}

variable "key_name" {
  description = "Optional EC2 key pair for SSH. Session Manager works without one."
  type        = string
  default     = ""
}

variable "ssh_cidrs" {
  description = "CIDRs allowed to SSH. Empty = port 22 closed (recommended)."
  type        = list(string)
  default     = []
}

variable "s3_bucket" {
  description = "Optional existing bucket for OTA images / debug frames (storage provider s3). Empty = local disk."
  type        = string
  default     = ""
}

variable "enable_snapshots" {
  type    = bool
  default = true
}

variable "monthly_budget_usd" {
  type    = number
  default = 40
}

variable "budget_alert_email" {
  description = "Where AWS Budgets sends 80%/100% alerts. Empty disables the budget."
  type        = string
  default     = ""
}
