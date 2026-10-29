# RingBreaker on AWS (ap-south-1, Mumbai)
#
#   Internet ──80/443──> Elastic IP ──> EC2 t3.micro (default VPC, public subnet)
#                                        ├─ Caddy  (HTTPS via Let's Encrypt, basic auth on /live)
#                                        └─ App    (FastAPI + DuckDB + LightGBM, private to the host)
#   EC2 role ──read──> S3 (code bundle) and SSM Parameter Store (secrets)
#   No SSH: admin access is AWS Systems Manager Session Manager only.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "ringbreaker", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "me" {}

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# ---------------------------------------------------------------------------- network
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

resource "aws_security_group" "web" {
  name        = "ringbreaker-web"
  description = "HTTP and HTTPS only. No SSH: use SSM Session Manager."
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description      = "HTTP (redirects to HTTPS, ACME challenge)"
    from_port        = 80
    to_port          = 80
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
  ingress {
    description      = "HTTPS"
    from_port        = 443
    to_port          = 443
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
  ingress {
    description      = "HTTP/3 (QUIC)"
    from_port        = 443
    to_port          = 443
    protocol         = "udp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
  egress {
    description      = "Outbound: package installs, S3, SSM, Groq API, Lets Encrypt"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
}

# ---------------------------------------------------------------------------- storage: code bundle
resource "aws_s3_bucket" "artifacts" {
  bucket        = "ringbreaker-${data.aws_caller_identity.me.account_id}-${var.region}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-old-bundles"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 14 }
  }
}

resource "aws_s3_object" "bundle" {
  bucket = aws_s3_bucket.artifacts.id
  key    = "releases/ringbreaker-app.zip"
  source = var.bundle_path
  etag   = filemd5(var.bundle_path)
}

# ---------------------------------------------------------------------------- configuration (non-secret)
# Secrets (groq_api_key, live_password) are created with the AWS CLI so they never enter Terraform state.
resource "aws_ssm_parameter" "domain" {
  name  = "${var.param_prefix}/domain"
  type  = "String"
  value = var.domain == "" ? "none" : var.domain
}

resource "aws_ssm_parameter" "live_user" {
  name  = "${var.param_prefix}/live_user"
  type  = "String"
  value = var.live_user
}

# ---------------------------------------------------------------------------- identity: least privilege
data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "host" {
  name               = "ringbreaker-host"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "host" {
  statement {
    sid       = "ReadCodeBundle"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/releases/*"]
  }
  statement {
    sid       = "ReadOwnParameters"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter${var.param_prefix}/*"]
  }
  statement {
    sid       = "DecryptSecureStrings"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "host" {
  name   = "ringbreaker-host"
  role   = aws_iam_role.host.id
  policy = data.aws_iam_policy_document.host.json
}

resource "aws_iam_instance_profile" "host" {
  name = "ringbreaker-host"
  role = aws_iam_role.host.name
}

# ---------------------------------------------------------------------------- compute
resource "aws_instance" "host" {
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.instance_type
  subnet_id                   = data.aws_subnets.public.ids[0]
  vpc_security_group_ids      = [aws_security_group.web.id]
  iam_instance_profile        = aws_iam_instance_profile.host.name
  associate_public_ip_address = true
  monitoring                  = false

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region       = var.region
    bucket       = aws_s3_bucket.artifacts.id
    key          = aws_s3_object.bundle.key
    param_prefix = var.param_prefix
  })
  user_data_replace_on_change = false

  tags = { Name = "ringbreaker" }

  lifecycle {
    ignore_changes = [ami] # don't replace the host every time Amazon publishes a new AMI
  }
  depends_on = [aws_ssm_parameter.domain, aws_ssm_parameter.live_user]
}

resource "aws_eip" "host" {
  domain   = "vpc"
  instance = aws_instance.host.id
}

# ---------------------------------------------------------------------------- reliability + cost guardrails
resource "aws_cloudwatch_metric_alarm" "auto_recover" {
  alarm_name          = "ringbreaker-auto-recover"
  alarm_description   = "Recover the instance onto healthy hardware if the system status check fails"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_System"
  dimensions          = { InstanceId = aws_instance.host.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = ["arn:aws:automate:${var.region}:ec2:recover"]
}

resource "aws_cloudwatch_metric_alarm" "cpu_high" {
  alarm_name          = "ringbreaker-cpu-high"
  alarm_description   = "Sustained high CPU: burst credits may run out (consider t3.small)"
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  dimensions          = { InstanceId = aws_instance.host.id }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
}

resource "aws_budgets_budget" "monthly" {
  count        = var.alert_email == "" ? 0 : 1
  name         = "ringbreaker-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}
