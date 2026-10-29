# FT-III viva prep: RingBreaker on AWS

Short answers you can say in 20–40 seconds. Every one points to something in the project.

## Cloud fundamentals

**IaaS vs PaaS vs SaaS, with examples from your project?**
IaaS: I rent virtual infrastructure and manage the OS and software. EC2, the VPC, the security group, the
Elastic IP and EBS. PaaS: AWS runs the platform and I just use it. S3, SSM Parameter Store, Session Manager,
CloudWatch. SaaS: finished software over the internet. The Groq LLM API, is-a.dev DNS and GitHub.

**Why EC2 and not Lambda (serverless)?**
The app needs pandas, DuckDB and LightGBM, which are over Lambda's 250 MB unzipped limit for a zip
package. The model build takes minutes, and an always-on API avoids cold starts. At this scale one small
instance is also cheaper than serverless plus a managed database.

**Why not Elastic Beanstalk or App Runner?**
They would hide the networking, security and storage configuration this case study is about, and cost more
for one small service.

**Region choice?**
ap-south-1 (Mumbai): RBI requires payment data to be stored in India, and it gives the lowest latency for
Indian users.

## Networking

**What is a security group?** A stateful virtual firewall on the instance. Mine allows inbound 80 and 443
only. Responses are allowed automatically because it is stateful.

**What is an Elastic IP and why does it cost money?** A static public IPv4 address that stays the same if the
instance is stopped or recovered, so the DNS record never changes. AWS charges $0.005/hour for every public
IPv4 address because IPv4 addresses are scarce.

**How does the domain work?** is-a.dev has an A record pointing `ringbreaker.<me>.is-a.dev` to my Elastic IP.

**How do you get HTTPS for free?** Caddy requests a certificate from Let's Encrypt using the HTTP-01
challenge on port 80, then renews it automatically. It also redirects HTTP to HTTPS and sends HSTS.

**Why is there no SSH?** Port 22 is a common attack target. I use SSM Session Manager instead: the SSM agent
connects outbound to AWS, so no inbound port or key pair is needed, and sessions are logged by AWS.

## Compute and deployment

**What is user data?** A script EC2 runs on first boot. Mine writes the settings file, downloads the
code bundle from S3 and runs `bootstrap.sh`.

**What does bootstrap.sh do?** Adds 2 GB swap, installs Docker and Compose, fetches code from S3, reads
secrets from Parameter Store, writes the Caddyfile with a bcrypt-hashed password, and runs
`docker compose up --build`. It is idempotent, so re-running it is how I redeploy.

**How do you redeploy?** `python -m ringbreaker package`, then `terraform apply` uploads the new bundle to S3,
then an SSM Run Command re-runs the bootstrap. No SSH at any point.

**What is Infrastructure as Code and why use Terraform?** The environment is defined in files, so it is
repeatable, reviewable and can be destroyed and rebuilt exactly. `terraform apply` builds it and
`terraform destroy` removes it.

**What happens if the instance fails?** Docker restarts crashed containers. If AWS hardware fails, a CloudWatch
alarm on `StatusCheckFailed_System` triggers EC2 auto-recovery: same instance, same IP, same disk.

## Storage and database

**Where is the data?** The transaction warehouse is a DuckDB file on encrypted EBS. Analyst decisions go to
SQLite on a host folder mounted into the container, so they survive rebuilds. Code bundles are in versioned
S3. Secrets are in Parameter Store.

**Why DuckDB and not RDS?** DuckDB is an embedded analytical database: fast SQL over 6.9 lakh rows with no
server to run or pay for. RDS would cost more than the entire stack. In the production design, cases move to
Aurora PostgreSQL for many concurrent analysts.

**What is the medallion architecture?** Bronze is raw data. Silver is validated and cleaned (11 quality
checks). Gold is the features the model and API use.

## Security

**What is IMDSv2 and why require it?** The instance metadata service hands out the instance role's temporary
credentials. IMDSv2 needs a session token from a PUT request, which blocks SSRF attacks that trick an app into
fetching the metadata URL.

**Least privilege, concretely?** The instance role can read one S3 prefix (`releases/*`) and parameters under
`/ringbreaker/*`, and decrypt only through SSM. Nothing else.

**Where are the secrets?** SSM SecureString parameters, encrypted with KMS, created with the CLI so they
never appear in Terraform state, Git or the image.

**How is the live app protected?** HTTPS only, basic auth with a bcrypt hash, host and origin checks in
the app, and the AI investigator capped at 15 runs per hour so nobody can drain the API key. The app
container has no public port.

## Results

**How did you measure performance?** With `deploy/loadtest.py`: concurrent users hitting the public page and
the authenticated API through Caddy, reporting throughput, p50/p95/p99 latency and error rate. Locally: 250
req/s, p95 75 ms, 0 errors. On AWS: [fill in from your run].

**What is p95?** 95% of requests completed faster than this value. It shows tail latency, which averages
hide.

**How would it scale?** First vertically, with a bigger instance type (one variable). Then horizontally: move
analyst state to RDS and data to S3, put N instances behind an Application Load Balancer in an Auto Scaling
group across two AZs. Finally the streaming design with Kinesis, Flink and SageMaker.

**What does it cost?** About $13.7/month: t3.micro $8.18, public IPv4 $3.65, EBS about $1.8. The rest fits
in free limits. It is covered by the AWS free-plan credits, and `terraform destroy` brings it to zero.

**Single points of failure?** One instance in one AZ. Auto-recovery reduces the impact; the fix is stage 2
(ALB + ASG across two AZs).

## About the application (if asked)

**What does RingBreaker do?** It detects money-mule networks: accounts that receive victims' money and pass
it on within minutes. A LightGBM model trained on complaint data scores accounts, graph analysis follows the
money to the cash-out layer, and cases are built from connected accounts. On the synthetic data it found all
28 planted rings, including 79 mules no victim reported. An AI investigator drafts the suspicious
transaction report.
