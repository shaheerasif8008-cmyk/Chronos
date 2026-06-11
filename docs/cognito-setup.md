# Amazon Cognito setup for Chronos

Chronos supports **dev OTP** (local), **Amazon Cognito** (hosted UI), or **both**.

## 1. Create a User Pool (AWS Console)

1. Open **Amazon Cognito** → **User pools** → **Create user pool**.
2. **Sign-in experience**: Email (recommended) or email + username.
3. **Security**: defaults are fine; enable MFA later for production.
4. **Sign-up**: disable self-registration unless you want open signup.
5. Create the pool and note:
   - **User pool ID** (e.g. `us-east-1_AbCdEfGhI`)
   - **Region** (e.g. `us-east-1`)

## 2. App client

1. In the pool → **App integration** → **App clients** → **Create app client**.
2. Name: `chronos-web`
3. **Confidential client** if you will store `COGNITO_APP_CLIENT_SECRET` on the API only.
4. **Allowed callback URLs**:
   - `http://localhost:3000/login/callback`
   - Your production URL, e.g. `https://app.cognisiatech.com/login/callback`
5. **Allowed sign-out URLs**: `http://localhost:3000/login`
6. **OAuth 2.0 grant types**: Authorization code grant
7. **OpenID Connect scopes**: `openid`, `email`, `profile`
8. Note the **Client ID** and **Client secret** (if confidential).

## 3. Cognito domain (hosted UI)

1. **App integration** → **Domain** → create a prefix, e.g. `chronos-dev`.
2. Hosted UI base: `https://chronos-dev.auth.us-east-1.amazoncognito.com`

## 4. Create a test user

1. **Users** → **Create user**
2. Email: same as `ADMIN_EMAIL` in `.env` (e.g. `admin@example.com`)
3. Set a temporary password and complete first-login password change in hosted UI.

## 5. Configure Chronos `.env`

```bash
AUTH_PROVIDER=cognito
# AUTH_PROVIDER=both          # Cognito + dev OTP for local debugging

COGNITO_REGION=us-east-1
COGNITO_USER_POOL_ID=us-east-1_XXXXXXXXX
COGNITO_APP_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
COGNITO_APP_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
COGNITO_DOMAIN=chronos-dev
COGNITO_CALLBACK_URL=http://localhost:3000/login/callback

# Optional: auto-create members row on first Cognito login (default: false)
COGNITO_AUTO_PROVISION_MEMBERS=false
```

Restart the API after changing env vars.

## 6. Sign in

1. Open `http://localhost:3000/login`
2. Click **Sign in with Cognito**
3. Complete hosted UI login
4. You are redirected to `/login/callback` and then `/chat`

## AWS deployment (Terraform / ECS)

The ECS task reads the same env vars, but on AWS they come from Terraform —
**not** a local `.env`. Set these in `infra/` (e.g. `terraform.tfvars`):

```hcl
auth_provider             = "cognito"          # or "both" to keep dev-OTP fallback
cognito_user_pool_id      = "us-east-1_XXXXXXXXX"
cognito_app_client_id     = "xxxxxxxxxxxxxxxxxxxxxxxxxx"
cognito_app_client_secret = "xxxxxxxxxxxxxxxxxxxxxxxx"   # omit if the client has no secret
cognito_domain            = "chronos-prod"     # hosted-UI prefix
```

Terraform derives the rest automatically:
- `COGNITO_REGION` ← `aws_region`
- `COGNITO_CALLBACK_URL` ← `https://<domain_name>/login/callback` (or the web ALB
  DNS name when `domain_name` is empty). Add this exact URL to the Cognito app
  client's **Allowed callback URLs**.

Apply, then force a new deployment so the API picks up the task definition:

```bash
terraform apply
aws ecs update-service --cluster chronos-prod-cluster --service chronos-prod-api --force-new-deployment
```

> **Why login fell back to OTP:** the task definition previously hardcoded
> `AUTH_PROVIDER=sendgrid_otp`, which is not a valid provider — `cognito_enabled()`
> only activates for `cognito`/`both`, so the Cognito button never rendered. It is
> now driven by the `auth_provider` variable (default `cognito`).

## Member access

By default, Cognito users must already exist in the `members` table (run `python seed.py` for the admin email).

Set `COGNITO_AUTO_PROVISION_MEMBERS=true` to create a `user` member on first login.

## API endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /auth/config` | Public auth settings + Cognito login URL |
| `POST /auth/cognito/callback` | Exchange OAuth `code` for Chronos JWT |
| `POST /auth/cognito/verify` | Verify Cognito `id_token` (Amplify / mobile) |

## AWS CLI quick reference

```bash
aws cognito-idp create-user-pool --pool-name chronos-dev
aws cognito-idp create-user-pool-domain --user-pool-id <POOL_ID> --domain chronos-dev
```

See [AWS Cognito docs](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-app-integration.html) for production hardening (WAF, custom domain, SAML).
