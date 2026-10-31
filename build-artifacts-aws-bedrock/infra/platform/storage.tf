# Thread metadata in DynamoDB, published files in S3.

resource "aws_dynamodb_table" "threads" {
  name         = "${var.name}-threads"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id"
  range_key    = "thread_id"

  attribute {
    name = "user_id"
    type = "S"
  }

  attribute {
    name = "thread_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_s3_bucket" "files" {
  bucket_prefix = "${var.name}-files-"

  # Demo project: `terraform destroy` empties the bucket first.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "files" {
  bucket = aws_s3_bucket.files.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "files" {
  bucket = aws_s3_bucket.files.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "files" {
  bucket = aws_s3_bucket.files.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Every object lives under users/<user>/threads/<thread>/..., so one rule
# expires them all after files_ttl_days (the same number both services show
# as the file's expiry).
resource "aws_s3_bucket_lifecycle_configuration" "files" {
  bucket = aws_s3_bucket.files.id

  rule {
    id     = "expire-files"
    status = "Enabled"

    filter {
      prefix = "users/"
    }

    expiration {
      days = var.files_ttl_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "files_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.files.arn,
      "${aws_s3_bucket.files.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "files" {
  bucket = aws_s3_bucket.files.id
  policy = data.aws_iam_policy_document.files_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.files]
}
