# Sign-in for the web page: a user pool, a public app client the browser calls
# with InitiateAuth (USER_PASSWORD_AUTH, no Hosted UI, no callback URL, so the
# demo works over plain HTTP), and one demo user.

resource "aws_cognito_user_pool" "this" {
  name                = var.name
  username_attributes = ["email"]
  deletion_protection = "INACTIVE"

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  # Nobody can self-register; the only user is the one Terraform creates.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.name}-web"
  user_pool_id = aws_cognito_user_pool.this.id

  # A browser cannot keep a client secret.
  generate_secret = false

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  prevent_user_existence_errors = "ENABLED"
  supported_identity_providers  = ["COGNITO"]

  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 1

  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_user" "demo" {
  user_pool_id = aws_cognito_user_pool.this.id
  username     = var.demo_user_email

  # `password` (not `temporary_password`) makes the user CONFIRMED at once, so
  # the first sign-in never hits a NEW_PASSWORD_REQUIRED challenge.
  password       = var.demo_user_password
  message_action = "SUPPRESS"

  attributes = {
    email          = var.demo_user_email
    email_verified = "true"
  }
}
