#!/usr/bin/env bash
#
# Deploy Booking Engine to AWS Lambda (container image).
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - Docker installed and running
#
# First run creates ECR repo, IAM role, Lambda function, and Function URL.
# Subsequent runs just build, push, and update.
#
# Usage:
#   AWS_REGION=eu-central-1 DATABASE_URL=postgresql://... ./scripts/deploy-booking.sh
#
set -euo pipefail

AWS_REGION="${AWS_REGION:-eu-central-1}"
ECR_REPO="booking-engine"
LAMBDA_FUNCTION="booking-engine-api"
ROLE_NAME="booking-engine-lambda-role"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

echo "=== Booking Engine Lambda Deployment ==="
echo "Region: ${AWS_REGION}"
echo "Account: ${ACCOUNT_ID}"
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
echo "Building Docker image..."
docker build -f booking_engine/Dockerfile -t "${ECR_REPO}:latest" --platform linux/amd64 .

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

# ── Step 5: Create or update Lambda function ──
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
        --environment "Variables={DATABASE_URL=${DATABASE_URL:-},POOL_MIN_SIZE=1,POOL_MAX_SIZE=3}" \
        --region "${AWS_REGION}"

    echo "Waiting for function to be Active..."
    aws lambda wait function-active-v2 --function-name "${LAMBDA_FUNCTION}" --region "${AWS_REGION}"

    echo "Creating Function URL (public, no auth)..."
    aws lambda create-function-url-config \
        --function-name "${LAMBDA_FUNCTION}" \
        --auth-type NONE \
        --region "${AWS_REGION}"

    # Allow public invocation
    aws lambda add-permission \
        --function-name "${LAMBDA_FUNCTION}" \
        --statement-id "FunctionURLAllowPublicAccess" \
        --action "lambda:InvokeFunctionUrl" \
        --principal "*" \
        --function-url-auth-type NONE \
        --region "${AWS_REGION}"
else
    echo ""
    echo "Updating Lambda function code..."
    aws lambda update-function-code \
        --function-name "${LAMBDA_FUNCTION}" \
        --image-uri "${ECR_URI}:latest" \
        --region "${AWS_REGION}"

    echo "Waiting for update to complete..."
    aws lambda wait function-updated-v2 --function-name "${LAMBDA_FUNCTION}" --region "${AWS_REGION}"
fi

# ── Step 6: Print result ──
echo ""
echo "=== Deployment complete ==="
FUNC_URL=$(aws lambda get-function-url-config \
    --function-name "${LAMBDA_FUNCTION}" \
    --region "${AWS_REGION}" \
    --query 'FunctionUrl' --output text 2>/dev/null || echo "N/A")
echo "Function URL: ${FUNC_URL}"
echo ""
echo "To update DATABASE_URL:"
echo "  aws lambda update-function-configuration \\"
echo "    --function-name ${LAMBDA_FUNCTION} \\"
echo "    --environment 'Variables={DATABASE_URL=postgresql://...,POOL_MIN_SIZE=1,POOL_MAX_SIZE=3}' \\"
echo "    --region ${AWS_REGION}"
