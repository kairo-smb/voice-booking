#!/usr/bin/env bash
#
# Deploy Booking Engine to AWS Lambda + HTTP API Gateway (container image).
#
# Prerequisites:
# - AWS CLI configured (aws configure / AWS_PROFILE)
# - Docker installed and running
#
# First run creates ECR repo, IAM role, Lambda function, and HTTP API Gateway.
# Subsequent runs just build, push, and update.
#
# Usage:
#   AWS_PROFILE=kairo ./scripts/deploy-booking.sh
#   DATABASE_URL=postgresql://... ./scripts/deploy-booking.sh
#
# All env vars are optional — the script reads whatever is set and passes
# them to Lambda. Unset vars default to empty strings.
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_REGION="${AWS_REGION:-eu-central-1}"
ECR_REPO="booking-engine"
LAMBDA_FUNCTION="booking-engine-api"
API_GATEWAY_NAME="booking-engine-api"
ROLE_NAME="booking-engine-lambda-role"

# Only export AWS_PROFILE locally; in CI, credentials come from env vars set
# by configure-aws-credentials and exporting a profile overrides them.
if [[ -z "${AWS_ACCESS_KEY_ID:-}" ]]; then
  export AWS_PROFILE
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

echo "=== Booking Engine Lambda Deployment ==="
echo "Region: ${AWS_REGION}"
echo "Account: ${ACCOUNT_ID}"
echo "Profile: ${AWS_PROFILE}"
echo ""

# ── Step 1: Ensure ECR repository exists ──
if ! aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${AWS_REGION}" &>/dev/null; then
  echo "Creating ECR repository: ${ECR_REPO}"
  aws ecr create-repository \
    --repository-name "${ECR_REPO}" \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true
else
  echo "ECR repository exists: ${ECR_REPO}"
fi

# ── Step 2: Build Docker image ──
echo ""
echo "Building Docker image (linux/amd64)..."
# --provenance=false --sbom=false: avoid OCI index manifest that Lambda rejects
docker buildx build --load \
  -f booking_engine/Dockerfile \
  -t "${ECR_REPO}:latest" \
  --platform linux/amd64 \
  --provenance=false \
  --sbom=false \
  .

# ── Step 3: Push to ECR ──
echo ""
echo "Pushing to ECR..."
aws ecr get-login-password --region "${AWS_REGION}" | \
  docker login --username AWS --password-stdin "${ECR_URI}"
docker tag "${ECR_REPO}:latest" "${ECR_URI}:latest"
docker push "${ECR_URI}:latest"

# ── Step 4: Ensure IAM role exists ──
if ! aws iam get-role --role-name "${ROLE_NAME}" &>/dev/null; then
  echo ""
  echo "Creating IAM role: ${ROLE_NAME}"
  aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"
      }]
    }'
  aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
  echo "Waiting 10s for IAM propagation..."
  sleep 10
fi
ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)

# ── Step 5: Build the env-vars string once ──
ENV_VARS="Variables={"
ENV_VARS+="DATABASE_URL=${DATABASE_URL:-},"
ENV_VARS+="POOL_MIN_SIZE=${POOL_MIN_SIZE:-1},"
ENV_VARS+="POOL_MAX_SIZE=${POOL_MAX_SIZE:-3},"
ENV_VARS+="CONTROL_PLANE_SECRET=${CONTROL_PLANE_SECRET:-},"
ENV_VARS+="TELNYX_API_KEY=${TELNYX_API_KEY:-},"
ENV_VARS+="TELNYX_PUBLIC_KEY=${TELNYX_PUBLIC_KEY:-},"
ENV_VARS+="TELNYX_DEFAULT_COUNTRY=${TELNYX_DEFAULT_COUNTRY:-IT},"
ENV_VARS+="OPENAI_SIP_PROJECT_ID=${OPENAI_SIP_PROJECT_ID:-},"
ENV_VARS+="PUBLIC_BASE_URL=${PUBLIC_BASE_URL:-},"
ENV_VARS+="VOICE_KAIRO_TOKENS_PER_SECOND=${VOICE_KAIRO_TOKENS_PER_SECOND:-18},"
ENV_VARS+="VOICE_MIN_SESSION_RESERVE_TOKENS=${VOICE_MIN_SESSION_RESERVE_TOKENS:-1500}"
ENV_VARS+="}"

# ── Step 6: Create or update Lambda function ──
if ! aws lambda get-function --function-name "${LAMBDA_FUNCTION}" --region "${AWS_REGION}" &>/dev/null; then
  echo ""
  echo "Creating Lambda function: ${LAMBDA_FUNCTION}"
  aws lambda create-function \
    --function-name "${LAMBDA_FUNCTION}" \
    --package-type Image \
    --code "ImageUri=${ECR_URI}:latest" \
    --role "${ROLE_ARN}" \
    --timeout 30 \
    --memory-size 256 \
    --environment "${ENV_VARS}" \
    --region "${AWS_REGION}"

  echo "Waiting for function to be Active..."
  aws lambda wait function-active-v2 --function-name "${LAMBDA_FUNCTION}" --region "${AWS_REGION}"
else
  echo ""
  echo "Updating Lambda function code..."
  aws lambda update-function-code \
    --function-name "${LAMBDA_FUNCTION}" \
    --image-uri "${ECR_URI}:latest" \
    --region "${AWS_REGION}"

  echo "Waiting for update to complete..."
  aws lambda wait function-updated-v2 --function-name "${LAMBDA_FUNCTION}" --region "${AWS_REGION}"

  # Always push env vars on update so they stay in sync with CI secrets
  echo "Updating Lambda environment variables..."
  aws lambda update-function-configuration \
    --function-name "${LAMBDA_FUNCTION}" \
    --environment "${ENV_VARS}" \
    --region "${AWS_REGION}" >/dev/null
  aws lambda wait function-updated-v2 --function-name "${LAMBDA_FUNCTION}" --region "${AWS_REGION}"
fi

# ── Step 7: Ensure HTTP API Gateway exists ──
API_ID=$(aws apigatewayv2 get-apis --query "Items[?Name=='${API_GATEWAY_NAME}'].ApiId" --output text --region "${AWS_REGION}" 2>/dev/null | head -1 | tr -d '[:space:]' || echo "")

if [[ -z "${API_ID}" ]]; then
  echo ""
  echo "Creating HTTP API Gateway: ${API_GATEWAY_NAME}"
  LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${LAMBDA_FUNCTION}"
  API_ID=$(aws apigatewayv2 create-api \
    --name "${API_GATEWAY_NAME}" \
    --protocol-type HTTP \
    --target "${LAMBDA_ARN}" \
    --query 'ApiId' \
    --output text \
    --region "${AWS_REGION}")

  # Grant API Gateway permission to invoke Lambda
  aws lambda add-permission \
    --function-name "${LAMBDA_FUNCTION}" \
    --statement-id "ApiGatewayV2Invoke" \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*/*" \
    --region "${AWS_REGION}" 2>/dev/null || echo "Permission already exists"
fi

API_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"

# ── Step 8: Update PUBLIC_BASE_URL if it was not set or points to old value ──
CURRENT_PUBLIC_URL=$(aws lambda get-function-configuration \
  --function-name "${LAMBDA_FUNCTION}" \
  --query 'Environment.Variables.PUBLIC_BASE_URL' \
  --output text \
  --region "${AWS_REGION}" 2>/dev/null || echo "")

if [[ -z "${CURRENT_PUBLIC_URL}" || "${CURRENT_PUBLIC_URL}" == "N/A" || "${CURRENT_PUBLIC_URL}" != "${API_URL}" ]]; then
  if [[ -z "${PUBLIC_BASE_URL:-}" ]]; then
    echo ""
    echo "Updating PUBLIC_BASE_URL to: ${API_URL}"
    # Fetch all current env vars, merge new PUBLIC_BASE_URL, update
    CURRENT_ENV=$(aws lambda get-function-configuration \
      --function-name "${LAMBDA_FUNCTION}" \
      --query 'Environment.Variables' \
      --output json \
      --region "${AWS_REGION}")
    MERGED=$(echo "$CURRENT_ENV" | jq --arg url "$API_URL" '. + {PUBLIC_BASE_URL: $url}')
    aws lambda update-function-configuration \
      --function-name "${LAMBDA_FUNCTION}" \
      --environment "Variables=$(echo "$MERGED" | jq -c .)" \
      --region "${AWS_REGION}" >/dev/null
    aws lambda wait function-updated-v2 --function-name "${LAMBDA_FUNCTION}" --region "${AWS_REGION}"
  fi
fi

# ── Step 9: Print result ──
echo ""
echo "=== Deployment complete ==="
echo "API Gateway URL: ${API_URL}"
echo ""
echo "Set these in the webapp environment:"
echo "  VOICE_AGENT_API_URL=${API_URL}"
echo "  VOICE_AGENT_SECRET=<your CONTROL_PLANE_SECRET>"
