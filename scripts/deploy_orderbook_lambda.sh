#!/bin/bash
# Build + deploy the KXBTC15M order-book collector Lambda (CloudFormation).
#
# The handler is dependency-free (stdlib + boto3-from-runtime), so the package is
# just handler.py — no pip install, no native wheels. Steps: zip -> upload to S3 ->
# `cloudformation deploy`.
#
# REQUIRES ADMIN AWS CREDENTIALS: creating the IAM role / Lambda / EventBridge rule
# is beyond the least-privilege `crypto-de-pipeline` user (S3-only). Run this with an
# admin profile, e.g.:  AWS_PROFILE=admin scripts/deploy_orderbook_lambda.sh
#
# Idempotent: re-running rebuilds the zip (new key) and updates the stack in place.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
BUCKET="${S3_BUCKET:-derekkuang-crypto-de-raw-546712138633-us-east-1-an}"
STACK="kxbtc-orderbook-collector"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/lambda/orderbook_collector"
BUILD="$ROOT/build"

mkdir -p "$BUILD"
ZIP="$BUILD/orderbook_collector.zip"
KEY="lambda-artifacts/orderbook-collector/$(date -u +%Y%m%dT%H%M%SZ).zip"

echo "==> packaging $ZIP (handler only, no deps)"
rm -f "$ZIP"
( cd "$SRC" && zip -q "$ZIP" handler.py )

echo "==> uploading to s3://$BUCKET/$KEY"
aws s3 cp "$ZIP" "s3://$BUCKET/$KEY" --region "$REGION"

echo "==> cloudformation deploy ($STACK)"
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$SRC/template.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "DataBucket=$BUCKET" "CodeS3Key=$KEY"

echo "==> done. Outputs:"
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs" --output table

echo
echo "Smoke-test the deployed function:"
echo "  aws lambda invoke --region $REGION --function-name kxbtc-orderbook-collector /tmp/out.json && cat /tmp/out.json"
