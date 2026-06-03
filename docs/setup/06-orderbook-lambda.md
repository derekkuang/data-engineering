# 06 — KXBTC15M order-book collector on AWS Lambda

Serverless graduation of the local launchd collector (`ingestion/kalshi_orderbook.py`),
built to gather decision-minute order books 24/7 without Mac-sleep gaps, so we can
test whether the **W+9–W+13 profit cluster** (`ml/decision_minute_profit.py`) is a
real edge or the latency-bound artifact.

## Architecture

```
EventBridge cron (12 fires/hr)  ->  Lambda (handler.py, stdlib-only)  ->  S3
  W+1 / W+12 / W+14 of each           snapshot live book + BTC spot       raw/orderbook_snapshots/
  15-min window                       3x ~20s apart (repricing curve)     dt=YYYY-MM-DD/<ticker>_wk<k>.jsonl
```

- **Function:** `lambda/orderbook_collector/handler.py` — dependency-free (Python stdlib
  + boto3 from the runtime); read-only public Kalshi market data, no auth/orders.
- **Schedule:** minutes `1,12,14,16,27,29,31,42,44,46,57,59` = W+1 (control), W+12
  (candidate), W+14 (near-expiry) for all four windows/hour.
- **IAM:** least privilege — `s3:PutObject` only under `raw/orderbook_snapshots/*`,
  plus its own CloudWatch log group.
- **Cost:** ~12 invocations/hr × ~45s × 128 MB ≈ a few cents/month (effectively free).

## Deploy (needs ADMIN credentials)

Creating the IAM role / Lambda / EventBridge rule is beyond the least-privilege
`crypto-de-pipeline` user (S3-only), so run the deploy with an admin profile:

```bash
AWS_PROFILE=admin scripts/deploy_orderbook_lambda.sh
```

The script zips the handler, uploads it to `s3://$S3_BUCKET/lambda-artifacts/...`,
and runs `cloudformation deploy` (stack `kxbtc-orderbook-collector`). Re-run anytime
to ship a new handler — it updates the stack in place.

## Verify

```bash
# one-off invoke (writes a snapshot now)
aws lambda invoke --region us-east-1 --function-name kxbtc-orderbook-collector /tmp/out.json
cat /tmp/out.json   # {"ok": true, "captured": 3, "wk": 12, ...}

# snapshots landing in S3
aws s3 ls s3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/orderbook_snapshots/ --recursive | tail

# logs
aws logs tail /aws/lambda/kxbtc-orderbook-collector --since 1h
```

## After it's confirmed working

- **Retire the local launchd collector** to drop the Mac dependency (it writes to a
  *local* JSONL, the Lambda writes to *S3*, so they don't collide — but no need to run
  both): `launchctl bootout gui/501 ~/Library/LaunchAgents/com.derekkuang.{kxbtc-orderbook,stay-awake}.plist`
- **Point the reconciliation at S3.** `ml/live_exec_reconcile.py` / `ml/live_paper_pnl.py`
  currently read the local JSONL; add an S3 reader (or `aws s3 sync`) so they consume the
  Lambda output, then extend them to select the W+12 snapshot (not just W+1) to test the cluster.

## Teardown

```bash
AWS_PROFILE=admin aws cloudformation delete-stack --region us-east-1 --stack-name kxbtc-orderbook-collector
```
