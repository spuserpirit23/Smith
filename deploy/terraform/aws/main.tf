# Single-VM Mitra deployment on AWS: one Graviton EC2 instance running the docker-compose stack.
#
# Cost levers (see docs/COST.md): one on-demand Graviton VM, 40 GB gp3, Elastic IP, no NAT, no ALB,
# no Fargate, no IoT Core, no RDS. By default AWS is only the VM host: the LLM, vision and TTS run
# on the instance itself (Ollama + Piper), so there is no per-use AI bill. The instance role still
# allows the optional AWS AI providers if they are selected in config.
#
# Portability: nothing in the app depends on this file. On another cloud, create any Ubuntu/Debian
# VM, open 80/443, install Docker, and run deploy/scripts/bootstrap.sh.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "mitra", ManagedBy = "terraform", Env = var.env }
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_ssm_parameter" "ubuntu_arm64" {
  # Canonical's published AMI id for Ubuntu 24.04 LTS on arm64.
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

resource "aws_security_group" "mitra" {
  name_prefix = "mitra-${var.env}-"
  description = "Mitra: HTTPS for devices and operators"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTP (ACME challenge + redirect)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTPS: device WebSocket, MQTT-over-WSS, operator API"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  dynamic "ingress" {
    for_each = length(var.ssh_cidrs) > 0 ? [1] : []
    content {
      description = "SSH (prefer SSM Session Manager and leave ssh_cidrs empty)"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = var.ssh_cidrs
    }
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  lifecycle { create_before_destroy = true }
}

resource "aws_eip" "mitra" {
  domain = "vpc"
}

locals {
  # Without a domain, use sslip.io: 1.2.3.4 -> 1-2-3-4.sslip.io resolves to the EIP and gets real TLS.
  domain = var.domain != "" ? var.domain : "${replace(aws_eip.mitra.public_ip, ".", "-")}.sslip.io"
}

resource "aws_instance" "mitra" {
  ami                    = data.aws_ssm_parameter.ubuntu_arm64.value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.mitra.id]
  iam_instance_profile   = aws_iam_instance_profile.mitra.name
  key_name               = var.key_name != "" ? var.key_name : null

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 2          # containers can use the instance role
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.disk_gb
    encrypted             = true
    delete_on_termination = false # keep the database if the instance is replaced by accident
    tags                  = { Project = "mitra", Env = var.env, Name = "mitra-${var.env}-root" }
  }

  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    domain     = local.domain
    acme_email = var.acme_email
    region     = var.region
    repo_url   = var.repo_url
    repo_ref   = var.repo_ref
    ssm_prefix = "/mitra/${var.env}"
    extras     = var.mitra_extras
  })
  user_data_replace_on_change = false

  tags = { Name = "mitra-${var.env}" }

  lifecycle {
    ignore_changes = [ami] # don't replace the box every time Canonical publishes a new AMI
  }
}

resource "aws_eip_association" "mitra" {
  instance_id   = aws_instance.mitra.id
  allocation_id = aws_eip.mitra.id
}

# Nightly EBS snapshots, 7 kept: the cheapest whole-box backup (DB, config, certs).
resource "aws_dlm_lifecycle_policy" "snapshots" {
  count              = var.enable_snapshots ? 1 : 0
  description        = "mitra-${var.env} nightly snapshots"
  execution_role_arn = aws_iam_role.dlm[0].arn
  state              = "ENABLED"
  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Project = "mitra", Env = var.env }
    schedule {
      name = "nightly"
      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["21:30"] # 03:00 IST
      }
      retain_rule { count = 7 }
      copy_tags = true
    }
  }
}

resource "aws_budgets_budget" "monthly" {
  count        = var.budget_alert_email != "" ? 1 : 0
  name         = "mitra-${var.env}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
