# Deploy the hosted tailor endpoint (C-endpoint)

The hosted, on-demand résumé-tailoring endpoint: a container Lambda + Function URL that verifies an HMAC signed token, reads the stored JD from `seen_jobs`, tailors + renders a one-page PDF, uploads to S3, and serves a mobile page. This runbook is the manual deploy — it touches your AWS account and isn't run by CI.

> **Status:** the maintainer no longer runs the AWS path. This runbook is kept for
> reference and has not been exercised recently.

**Prereqs:**
- Docker running, with **buildx** (default in modern Docker Desktop — the build script needs it to push a Lambda-compatible single-manifest image).
- AWS credentials for an IAM user that can manage Lambda, ECR, S3, SSM and IAM (the `default` profile; region `us-east-1`).
- `resume/content.json` + `resume/evidence.json` present on disk (gitignored, but the image bakes them in).
- The `alarm_email` Terraform variable. There's no `terraform.tfvars`, so every `plan`/`apply` here passes `-var "alarm_email=you@example.com"` (use the address already subscribed to the alarm SNS topic — using a different one churns the subscription).

> The detector-side wiring (**C-wiring**) is already shipped to `main`: once this endpoint is deployed *and* the detector is redeployed (`./scripts/package.sh && terraform apply`), alerts carry a signed "Tailor resume" deep-link (ntfy action button + Discord field). Until both happen, alerts simply omit the button (env-gated, safe). You can still verify the endpoint standalone with a seeded `seen_jobs` row + a minted token (step 6).

## 1. Set the signing secret (once)

```bash
aws ssm put-parameter --name /job-aggregator/tailor_signing_secret --type SecureString \
  --value "$(openssl rand -hex 32)" --overwrite --region us-east-1
```

Keep this value handy — you need it to mint tokens (step 6). To **kill all outstanding links**, re-run this with a new value, then re-apply (step 5) so the Lambda env picks it up.

## 2. Create the ECR repo first

The Lambda can't be created until its image exists, so apply the ECR repo before building:

```bash
cd infra && terraform apply -target=aws_ecr_repository.tailor_endpoint \
  -var "alarm_email=you@example.com" && cd ..
```

## 3. Build + push the image

```bash
AWS_REGION=us-east-1 ./scripts/build_endpoint_image.sh
```

Copy the printed `image_uri = "..."`. The script builds a **single Docker v2-schema2 manifest** (`buildx --provenance=false --output …,oci-mediatypes=false`). Don't revert it to a plain `docker build` + `docker push`: modern BuildKit pushes an OCI image *index* with a provenance attestation, and Lambda rejects that with `InvalidParameterValueException: The image manifest, config or layer media type … is not supported`.

## 4. Import the signing-secret parameter (because step 1 created it out-of-band)

Step 1 set the SSM parameter via the CLI, but Terraform also manages it (`aws_ssm_parameter.tailor_signing_secret`, with `lifecycle.ignore_changes=[value]`). Without this import, the apply in step 5 fails with `ParameterAlreadyExists`. Import the existing parameter into state — its real value is preserved (only the placeholder/description reconcile):

```bash
cd infra && terraform import \
  -var "alarm_email=you@example.com" \
  aws_ssm_parameter.tailor_signing_secret /job-aggregator/tailor_signing_secret && cd ..
```

(Skip this on a re-deploy where the parameter is already in state — `terraform state list | grep tailor_signing_secret` to check.)

## 5. Apply the rest

```bash
cd infra && terraform apply \
  -var "tailor_endpoint_image_uri=<the image_uri>" \
  -var "alarm_email=you@example.com" && cd ..
```

Note the `tailor_endpoint_url` output (the Function URL). This apply also pre-stages the two `JOB_AGG_TAILOR_*` env vars on the **detector** Lambda (an env change only — it does *not* redeploy the detector code; that's the separate `package.sh && terraform apply`). The endpoint Lambda itself is fully functional after this step.

## 6. Verify end-to-end (no live alert needed yet)

Seed a test JD on a `seen_jobs` row:

```bash
aws dynamodb update-item --table-name seen_jobs --region us-east-1 \
  --key '{"job_id":{"S":"test:1"}}' \
  --update-expression "SET description_snapshot = :d, title = :t, company = :c" \
  --expression-attribute-values '{":d":{"S":"Senior Go/React engineer, remote US. Build product features end to end. 5+ years."},":t":{"S":"Senior SWE"},":c":{"S":"TestCo"}}'
```

Mint a 30-day token (reads the secret from SSM into an env var so it never lands in your shell history):

```bash
SEC=$(aws ssm get-parameter --name /job-aggregator/tailor_signing_secret --with-decryption \
  --query Parameter.Value --output text --region us-east-1) \
python3 -c "import os,time; from src.tailor.endpoint.auth import sign_token; print(sign_token('test:1', int(time.time())+30*86400, os.environ['SEC']))"
```

Open on your phone / browser (`job_id`'s colon is fine unencoded in a query string):

```
<tailor_endpoint_url>?job_id=test:1&t=<token>
```

Expect: the spinner, then a **Download tailored résumé (PDF)** button + cover letter + fit. Tapping again serves the cached PDF instantly; append `&regen=1` to force a fresh tailor.

## Troubleshooting

### Every request to the Function URL returns `403 Forbidden` (`AccessDeniedException`)

If the URL 403s for **all** anonymous requests but `aws lambda invoke --function-name job-aggregator-tailor-endpoint …` returns 200, the function and its code are fine — the **public Function URL is blocked at the AWS account level**. This hit this project's very first deploy.

- **Cause:** a new/unverified AWS account. Check `aws lambda get-account-settings` — `ConcurrentExecutions: 10` (default is 1000) is the tell that AWS hasn't lifted the account's initial limits, which include blocking public (`AuthType=NONE`) Function URLs. If your account is not in an organization it's not an SCP/RCP, and the resource policy (`Principal:"*"`, `lambda:InvokeFunctionUrl`, condition `lambda:FunctionUrlAuthType=NONE`) is already correct — adding more permissions won't help.
- **Confirm it (no public surface):** flip the URL to IAM auth — `aws lambda update-function-url-config --function-name job-aggregator-tailor-endpoint --auth-type AWS_IAM --region us-east-1` — and hit it with a SigV4-signed request (`curl --aws-sigv4 …`). A 200 there proves only *public* access is blocked. Set it back to `NONE` afterward (`--auth-type NONE`).
- **Fix (yours to do):** wait ~24–48h for AWS to lift new-account limits (often automatic once the account has real usage/billing), or open an AWS Support case requesting a **Lambda concurrency-limit increase** (this also gets the account reviewed and the public-URL block lifted). Once lifted, the existing `NONE` URL works with **no code change** — just re-run step 5 if the URL needs recreating.
- **Do not** weaken the design to dodge it: the phone has no AWS creds, so `NONE` + HMAC token is required.

### `ParameterAlreadyExists` on apply

You skipped step 4 — `terraform import` the signing-secret parameter, then re-run the apply.

### `InvalidParameterValueException: image manifest … not supported`

Your image is an OCI index (BuildKit provenance attestation) rather than a single manifest. Use the current `scripts/build_endpoint_image.sh` (buildx `--provenance=false --output …,oci-mediatypes=false`); rebuild + push (step 3) and re-apply (step 5).

## 7. Operate

- **Kill-switch / rotate:** re-run step 1 with a new secret → every outstanding link 401s. Re-run step 5 (`terraform apply`) to push the new secret into both Lambdas' env.
- **Update the résumé:** edit `resume/content.json`, rebuild + push the image (step 3), re-apply (step 5).
- **Cost:** the Lambda + S3 are pay-per-use (pennies at personal volume); ECR stores the image (~a few hundred MB). The S3 PDFs auto-expire after 7 days.
- **Logs:** CloudWatch `/aws/lambda/job-aggregator-tailor-endpoint`. A failed tailor logs `tailor_endpoint_run_failed` with `error`/`error_type` and the page shows a generic retry message (the endpoint never 500s on the normal paths).

## Teardown

```bash
cd infra && terraform destroy -target=aws_lambda_function_url.tailor_endpoint \
  -target=aws_lambda_function.tailor_endpoint -target=aws_s3_bucket.tailor_pdfs \
  -target=aws_ecr_repository.tailor_endpoint \
  -var "alarm_email=you@example.com" && cd ..
```

(The `seen_jobs` table and the detector Lambda are shared — don't destroy those.)
