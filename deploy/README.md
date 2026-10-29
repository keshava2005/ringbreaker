# Deploy RingBreaker on AWS (Windows guide)

What you get:

| URL | What | Access |
|---|---|---|
| `https://ringbreaker.<you>.is-a.dev/` | Read-only demo snapshot | Public |
| `https://ringbreaker.<you>.is-a.dev/architecture.html` | Architecture page | Public |
| `https://ringbreaker.<you>.is-a.dev/live/` | Live app: API, case actions, AI investigator | Username + password |
| `https://ringbreaker.<you>.is-a.dev/live/docs` | OpenAPI docs | Username + password |

```text
Internet ──80/443──> Elastic IP ──> EC2 t3.micro (Amazon Linux 2023, ap-south-1)
                                     ├─ Caddy: HTTPS (Let's Encrypt), security headers, basic auth on /live
                                     └─ App container: FastAPI + DuckDB + LightGBM (not exposed directly)
EC2 role ──> S3 (code bundle, private, encrypted)   SSM Parameter Store (secrets)
Admin access: SSM Session Manager only (no SSH port, no key pair)
```

Approximate cost, on-demand in ap-south-1: t3.micro $0.0112/h (about $8.2/month), public IPv4 $0.005/h
(about $3.7/month), 20 GB gp3 about $1.8/month. **About $14/month in total, paid from free-plan credits.**
S3, Parameter Store (standard), and the CloudWatch alarms used here stay within free limits.
Run `terraform destroy` when you no longer need it.

## 0. One-time setup (about 10 minutes)

1. Install the tools in PowerShell:
   ```powershell
   winget install -e --id Amazon.AWSCLI
   winget install -e --id Hashicorp.Terraform
   winget install -e --id Amazon.SessionManagerPlugin
   ```
   Close and reopen PowerShell afterwards.
2. Sign in to AWS. For a personal account, create an IAM Identity Center user or an IAM user with
   AdministratorAccess (never use the root user), then run:
   ```powershell
   aws configure sso      # or: aws configure   (access key)
   aws sts get-caller-identity
   ```
3. Store the two secrets in Parameter Store. They never go into Terraform state, this repo, or the server image.
   ```powershell
   aws ssm put-parameter --region ap-south-1 --name /ringbreaker/live_password --type SecureString --value "PICK-A-LONG-PASSWORD"
   aws ssm put-parameter --region ap-south-1 --name /ringbreaker/groq_api_key  --type SecureString --value "gsk_..."
   ```
   The Groq key is optional. Without it the investigator uses the rule-based fallback.

## 1. Deploy (about 15 minutes, mostly waiting)

From the repository root:
```powershell
python -m ringbreaker package                      # -> deploy/.build/ringbreaker-app.zip
cd deploy/terraform
terraform init
terraform apply -var "alert_email=you@example.com"
```
Type `yes`. Terraform prints `public_ip` and `test_url`.

The server now installs Docker, builds the data and model, and starts Caddy. This takes 8–12 minutes on a
t3.micro. Then `http://<public_ip>/` shows the public demo. The live app is deliberately refused on plain
HTTP: it only runs over HTTPS on your domain.

To watch progress:
```powershell
terraform output -raw shell    # copy and run it, then:
sudo tail -f /var/log/ringbreaker-bootstrap.log
```

## 2. Domain on is-a.dev

1. Fork `https://github.com/is-a-dev/register` and read its current rules at https://docs.is-a.dev.
2. Copy the two files from `deploy/is-a-dev/` into `domains/`, replace `YOUR_GITHUB_USERNAME` and `YOUR_EIP`,
   and open a pull request. Say it is a personal portfolio and an open-source project demo.
3. When it is merged (hours to a few days), tell the server its domain:
   ```powershell
   terraform apply -var "domain=ringbreaker.keshava.is-a.dev" -var "alert_email=you@example.com"
   terraform output -raw redeploy     # copy and run it
   ```
   Caddy gets the HTTPS certificate on first visit. Open `https://ringbreaker.keshava.is-a.dev/live/`
   and log in as `analyst` with your password.

## 3. Update after code changes
```powershell
python -m ringbreaker package
cd deploy/terraform; terraform apply      # uploads the new bundle to S3
terraform output -raw redeploy            # copy and run: rebuilds on the host via SSM
```

## 4. Measure (for the report)
```powershell
pip install httpx
python deploy/loadtest.py https://ringbreaker.keshava.is-a.dev --user analyst --password "..." --concurrency 20 --seconds 60
```
This writes `loadtest_results.json` with throughput, p50/p95/p99 latency and error rate per endpoint.
CPU and network graphs are in the CloudWatch console under EC2 → instance → Monitoring.

## 5. Tear down
```powershell
cd deploy/terraform; terraform destroy
aws ssm delete-parameter --region ap-south-1 --name /ringbreaker/live_password
aws ssm delete-parameter --region ap-south-1 --name /ringbreaker/groq_api_key
```
Also delete the is-a.dev files with a pull request, so the name doesn't point at an IP you no longer own.

## Security choices (and why)

| Control | Where |
|---|---|
| Only ports 80/443 open; no SSH port, no key pair | Security group; admin via SSM Session Manager |
| IMDSv2 required | Blocks SSRF-style credential theft from the metadata service |
| Least-privilege instance role | Read one S3 prefix and `/ringbreaker/*` parameters only |
| Secrets in SSM SecureString | KMS-encrypted; never in state, image or repo |
| Encrypted EBS and S3, S3 public access blocked | Data at rest |
| HTTPS with HSTS, security headers | Caddy + Let's Encrypt, auto-renewed |
| Basic auth on `/live`, refused over plain HTTP | Caddy |
| App never exposed directly; host and origin checks | Docker network + app middleware |
| One investigation at a time, 15 per hour | Protects the shared LLM key from abuse |
| `no-new-privileges`, non-root container user, memory limit | Container hardening |
| Auto-recovery alarm, CPU alarm, budget alert | CloudWatch + AWS Budgets |
