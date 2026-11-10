# Least-privilege instance role: the AI APIs the backend calls, plus SSM for secrets and
# Session Manager (shell access without opening port 22).

data "aws_caller_identity" "me" {}

data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mitra" {
  name_prefix        = "mitra-${var.env}-"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.mitra.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "mitra" {
  statement {
    sid = "SpeechAndLanguage"
    actions = [
      "transcribe:StartStreamTranscription",
      "transcribe:StartStreamTranscriptionWebSocket",
      "polly:SynthesizeSpeech",
      "rekognition:DetectLabels",
    ]
    resources = ["*"]
  }
  statement {
    sid = "Bedrock"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    # Foundation models and cross-region inference profiles (e.g. apac.*) in any region the
    # profile routes to.
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:::foundation-model/*", # global cross-Region inference profiles

      "arn:aws:bedrock:*:${data.aws_caller_identity.me.account_id}:inference-profile/*",
    ]
  }
  statement {
    sid       = "AppSecrets"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:PutParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter/mitra/${var.env}/*"]
  }
  dynamic "statement" {
    for_each = var.s3_bucket != "" ? [1] : []
    content {
      sid       = "OptionalBucket"
      actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
      resources = ["arn:aws:s3:::${var.s3_bucket}", "arn:aws:s3:::${var.s3_bucket}/*"]
    }
  }
}

resource "aws_iam_role_policy" "mitra" {
  role   = aws_iam_role.mitra.id
  policy = data.aws_iam_policy_document.mitra.json
}

resource "aws_iam_instance_profile" "mitra" {
  name_prefix = "mitra-${var.env}-"
  role        = aws_iam_role.mitra.name
}

# Snapshot lifecycle manager role
data "aws_iam_policy_document" "assume_dlm" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["dlm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dlm" {
  count              = var.enable_snapshots ? 1 : 0
  name_prefix        = "mitra-dlm-"
  assume_role_policy = data.aws_iam_policy_document.assume_dlm.json
}

resource "aws_iam_role_policy_attachment" "dlm" {
  count      = var.enable_snapshots ? 1 : 0
  role       = aws_iam_role.dlm[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}
