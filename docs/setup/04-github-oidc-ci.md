# 04 — GitHub Actions CI via OIDC (no stored AWS keys)

CI runs `dbt build` against Athena on every push/PR (`.github/workflows/ci.yml`).
For that, GitHub Actions needs AWS credentials. We use **OIDC federation** instead
of storing a long-lived access key in GitHub secrets:

- GitHub Actions presents a short-lived signed token proving *"I am a run from
  `repo:derekkuang/data-engineering`"*.
- AWS trusts GitHub as an OpenID Connect identity provider and, if the token's
  `sub` claim matches the role's trust policy, returns **temporary** STS creds.
- No access key is ever created, so none can leak — the right pattern for a
  **public** repo.

This is a one-time setup. The commands below need **IAM admin** credentials (root
or an admin IAM user) — the least-privilege `crypto-de-pipeline` user can't create
roles. Run them with an admin profile: `--profile admin` (or via the console).

All region/account values match the rest of the project: `us-east-1`, account
`546712138633`, bucket `derekkuang-crypto-de-raw-546712138633-us-east-1-an`.

---

## Step 1 — Create the GitHub OIDC identity provider (once per AWS account)

Check whether it already exists:

```bash
aws iam list-open-id-connect-providers
```

If you don't see `token.actions.githubusercontent.com`, create it. (AWS validates
the GitHub endpoint against a trusted CA now, so the thumbprint value is no longer
used for verification — but the CLI still requires the argument.)

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

---

## Step 2 — Create the CI role with the GitHub trust policy

The trust policy (`docs/setup/iam/github-oidc-trust-policy.json`) only lets runs
from **this repo** assume the role. To tighten later, replace the `sub` wildcard
`repo:derekkuang/data-engineering:*` with e.g.
`repo:derekkuang/data-engineering:ref:refs/heads/main` (plus a `pull_request`
entry) once you're confident.

```bash
aws iam create-role \
  --role-name crypto-de-ci \
  --assume-role-policy-document file://docs/setup/iam/github-oidc-trust-policy.json \
  --description "GitHub Actions CI: runs dbt build against Athena via OIDC"
```

This produces the role ARN `arn:aws:iam::546712138633:role/crypto-de-ci`, which is
already wired into `.github/workflows/ci.yml`.

---

## Step 3 — Give the role exactly what dbt needs

dbt-athena needs: Athena query lifecycle + Glue read on `crypto_raw` + Glue write
on `crypto_staging`/`crypto_marts` + S3 object CRUD on the bucket. The first three
are **already covered by the two managed policies** you built for local dbt — reuse
them. S3 object CRUD lived on the *user's* inline policy, which a role can't
inherit, so we add it as a small managed policy here.

Find your existing managed-policy ARNs:

```bash
aws iam list-policies --scope Local \
  --query "Policies[].{Name:PolicyName,Arn:Arn}" --output table
```

Attach the Athena-query and dbt-glue-write policies (substitute the ARNs from
above):

```bash
aws iam attach-role-policy --role-name crypto-de-ci \
  --policy-arn arn:aws:iam::546712138633:policy/<your-athena-query-policy>

aws iam attach-role-policy --role-name crypto-de-ci \
  --policy-arn arn:aws:iam::546712138633:policy/<your-dbt-glue-write-policy>
```

Create and attach the CI S3 access policy:

```bash
aws iam create-policy \
  --policy-name crypto-de-ci-s3 \
  --policy-document file://docs/setup/iam/ci-s3-access-policy.json

aws iam attach-role-policy --role-name crypto-de-ci \
  --policy-arn arn:aws:iam::546712138633:policy/crypto-de-ci-s3
```

---

## Step 4 — Push and verify

Push any commit. The `dbt CI` workflow should:

1. Exchange the OIDC token for `crypto-de-ci` creds (the "Configure AWS
   credentials via OIDC" step).
2. `uv sync` → `dbt deps` → `dbt build` against Athena.
3. Go green when every model materializes and every test passes (including the
   point-in-time-correctness singular test).

If the AWS step fails with `Not authorized to perform sts:AssumeRoleWithWebIdentity`,
the `sub` in the trust policy doesn't match the run — re-check the repo
owner/name. If `dbt build` fails on AccessDenied, a policy from Step 3 is missing.

---

## Notes / known tradeoffs

- **CI writes to the live warehouse.** `dbt build` re-runs the views (free) and a
  `MERGE` into the incremental marts (0 rows when there's no new data, so it's
  effectively a re-validation). A cleaner setup would point CI at an isolated
  `crypto_*_ci` Glue database via a dedicated dbt target — captured as future
  work, consistent with the dev/staging/prod separation noted in the README.
- The `concurrency` block in the workflow serializes runs per ref so two
  Iceberg `MERGE`s never hit the same mart at once.
