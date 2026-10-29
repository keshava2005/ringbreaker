# is-a.dev records for keshava2005

| File | Name | Points to |
|---|---|---|
| `keshava.json` | `keshava.is-a.dev` | GitHub Pages: the public RingBreaker demo (`keshava2005.github.io/ringbreaker`) |
| `ringbreaker.keshava.json` | `ringbreaker.keshava.is-a.dev` | The AWS server (Elastic IP) |

1. Deploy to AWS first, then replace `YOUR_EIP` with `terraform output -raw public_ip`.
2. Fork https://github.com/is-a-dev/register, copy both files into `domains/`, open ONE pull request.
   Nested names need the parent (`keshava`) to be yours, so register both together.
   If the PR checks ask for a contact, add `"email"` or `"discord"` under `owner` (it becomes public).
3. After it merges: in the `ringbreaker` repo go to Settings → Pages → Custom domain, enter
   `keshava.is-a.dev`, save, then tick "Enforce HTTPS".
4. Re-run `terraform apply -var "domain=ringbreaker.keshava.is-a.dev"` so Caddy gets the certificate.

Until it merges you are not blocked: use `https://keshava2005.github.io/ringbreaker/` for the demo and
`https://<ip-with-dashes>.sslip.io` (e.g. `13-233-45-67.sslip.io`) as the AWS domain.

Check the current rules at https://docs.is-a.dev before opening the PR.
