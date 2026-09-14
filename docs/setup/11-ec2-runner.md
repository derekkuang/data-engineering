# 11 — EC2 runner (always-on maker + capture)

Why a box at all: the campaign needs ~113 sessions for statistical power (see
`strategies/soccer_mm/VERDICT.md`), and many fixtures kick off 03:00–05:00 UTC. Manual
supervision of that is not going to happen, so the choice is "automate or accept the
question stays open."

Why not the laptop: if it sleeps, drops wifi, or the lid closes mid-session you can leave
**resting orders on a live book with no process watching them**. `lp_live`'s cleanup only
runs if the process gets to run — a sleeping laptop means the $2/$5 caps have no enforcer.
(The stale `com.derekkuang.kxbtc-orderbook` launchd agent, which collected gap-ridden data
for months, is the cautionary example.)

Why not GitHub Actions for LIVE: its `schedule` fires 1h47m–4h39m late (measured
2026-09-08). Fine for capture, where the `--wait-minutes` loop absorbs it. Not something to
hand real money to unattended.

## Instance sizing

| | |
|---|---|
| type | **t4g.micro** (ARM, 2 vCPU burst, **1 GB**) |
| cost | ~**$6/mo** on-demand (t4g.nano at $3/mo has 0.5 GB — too tight, see below) |
| region | **us-east-1** — Kalshi is US-East (low RTT) and our S3/Athena already live there |
| OS | Amazon Linux 2023 (arm64) |

**On RAM:** the maker itself is light (~114 MB peak RSS: httpx + websockets + cryptography).
What needs headroom is *installing* `pandas`/`pyarrow`/`numpy`, which the capture needs to
land Parquet to S3. On 0.5 GB that install can OOM, so t4g.micro + swap is the pragmatic
floor. If you only ever run the maker (no capture) a nano would do.

## 1. Provision

```bash
# key pair (once)
aws ec2 create-key-pair --profile admin --key-name kalshi-runner \
  --query KeyMaterial --output text > ~/.ssh/kalshi-runner.pem
chmod 400 ~/.ssh/kalshi-runner.pem

# security group: SSH from YOUR IP only — the runner needs no inbound otherwise
MYIP=$(curl -s https://checkip.amazonaws.com)
aws ec2 create-security-group --profile admin --group-name kalshi-runner \
  --description "Kalshi maker runner" --query GroupId --output text
# then authorize SSH from $MYIP/32 on port 22 for that group id

# latest AL2023 arm64 AMI
aws ssm get-parameters --profile admin --region us-east-1 \
  --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query 'Parameters[0].Value' --output text
```

Launch `t4g.micro` with that AMI, the key pair, the security group, and **an IAM instance
profile** (next section). 8 GB gp3 root is plenty.

## 2. IAM — instance profile, not access keys

The capture writes Parquet to the raw zone. Give the instance a role rather than putting AWS
keys on disk:

- Trust: `ec2.amazonaws.com`
- Permissions: `s3:PutObject` on
  `arn:aws:s3:::derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/ws_features/*`

That is the whole AWS surface the runner needs. It does **not** need Athena or dbt — the
nightly `pipeline.yml` in GitHub Actions still does the warehouse build.

## 3. The Kalshi key — the one real risk

Kalshi API keys are **full-account; there is no read-only scope**. On the instance:

```bash
sudo install -d -m 700 -o ec2-user /opt/kalshi
# paste .env (KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY + S3_BUCKET + KALSHI_API_BASE)
sudo install -m 600 -o ec2-user /dev/stdin /opt/kalshi/.env
```

Mode `600`, owned by the run user, on a box whose only inbound rule is SSH from your IP.
Prefer a **separate CI/runner key** from your interactive trading key so it can be revoked
independently.

## 4. Setup

```bash
sudo dnf -y install git
# swap so the pandas/pyarrow install cannot OOM on 1 GB
sudo dd if=/dev/zero of=/swapfile bs=1M count=1024 && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/derekkuang/data-engineering.git /opt/kalshi/app
cd /opt/kalshi/app && ln -s /opt/kalshi/.env .env
uv sync --frozen
uv run python -m core.maker.lp_live --auth-check        # proves key + signing on the box
```

## 5. Schedule

Cron here is **precise** — that is the whole point versus GitHub's best-effort scheduler.
Drive it from the fixture calendar rather than fixed clock times:

```bash
crontab -e
# every 30 min: if a fixture is in play, capture (read-only, no money)
*/30 * * * * cd /opt/kalshi/app && /home/ec2-user/.local/bin/uv run python -m \
  strategies.soccer_mm.fixtures --now >/dev/null 2>&1 && \
  /home/ec2-user/.local/bin/uv run python -m core.capture.ws_features \
  --prefix KXEPL,KXLALIGA,KXSERIEA,KXBUNDESLIGA,KXLIGUE1,KXUCL,KXUEL,KXMLS,KXLIGAMX,KXBRASILEIRO \
  --minutes 100 --wait-minutes 20 >> /var/log/kalshi-capture.log 2>&1
```

`fixtures --now` exits non-zero when nothing is in play, so the `&&` makes this a
fixture-driven trigger rather than a clock-driven one.

**Do NOT add the live maker to cron yet.** Run it by hand over SSH for the first several
sessions:

```bash
ssh -i ~/.ssh/kalshi-runner.pem ec2-user@<ip>
cd /opt/kalshi/app && tmux new -s maker      # tmux so it survives a dropped SSH session
uv run python -m core.maker.lp_live --live --i-understand-live \
  --pilot "$SOCCER" --prefix "$SOCCER" --markets 8 --minutes 45
```

The multi-market path is days old and has already produced one real-money bug (threads
rolling onto the same market, `9e1722c`). It should earn trust supervised before it runs
while you sleep.

## 6. Safety that travels with the box

- **`data/daily_budget.json` is per-instance.** Running the maker on BOTH the laptop and the
  runner gives you two independent $5 budgets. Pick one host and stick to it.
- Caps in force: **$2/market, $5/session, $5/ET-day** (persistent, fail-closed).
- `journalctl`/cron logs: check `/var/log/kalshi-capture.log` and `data/lp_sessions.csv`.
- Kill everything: `pkill -f lp_live` — each thread flattens and cancels on the way out.

## 7. Cost

~$6/mo instance + ~$1 EBS. Inside the existing account-wide `fpledge-monthly` $10 budget —
which that budget **already shares with the fpledge project**, so raise it or expect the 80%
alert to fire.
