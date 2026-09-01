# Invoking the aide from GitHub Actions

A GitHub runner starts with no AWS credentials, so the real work is
authentication. Use GitHub OIDC to assume an IAM role at run time — no
long-lived AWS keys stored as GitHub secrets.

## One-time AWS setup (outside this repo)

1. Add GitHub as an OIDC identity provider in the account: issuer
   `token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
2. Create an IAM role whose trust policy allows that provider, scoped to your
   repo (and ideally branch) via the token `sub` claim, e.g.
   `repo:<owner>/<repo>:ref:refs/heads/main`.
3. Grant the role just `lambda:InvokeFunction` on the aide's Interface Lambda:
   `arn:aws:lambda:<region>:<account-id>:function:sns-email-aide-interface`.

For the AWS-side IAM setup (OIDC provider + trust policy scoping), see
[Create a role for OpenID Connect federation](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html)
in the IAM User Guide.

## Workflow

See [`workflow.yml`](workflow.yml) for a complete example. Key points:

- `permissions: id-token: write` is required for the runner to mint the OIDC
  token; omitting it is the most common failure.
- Providing `role-to-assume` without an access key signals
  `aws-actions/configure-aws-credentials` to use OIDC. Prefer this over storing
  static IAM user keys as GitHub secrets.
- Once credentials are configured, invoking the aide is identical to any other
  caller (`aws lambda invoke`, or boto3 via `../aide_client.py`).
