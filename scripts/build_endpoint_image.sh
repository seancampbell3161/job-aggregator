#!/usr/bin/env bash
# Build + push the tailor-endpoint container image to ECR.
# Usage: AWS_REGION=us-east-1 ECR_REPO=job-aggregator-tailor-endpoint ./scripts/build_endpoint_image.sh
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
REPO="${ECR_REPO:-job-aggregator-tailor-endpoint}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}"

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
# Lambda rejects a buildx OCI image *index* carrying a provenance attestation
# ("InvalidParameterValueException: image manifest ... not supported"). Build a
# single manifest: --provenance=false drops the attestation, oci-mediatypes=false
# emits Docker v2-schema2 media types Lambda accepts. push=true uploads directly.
docker buildx build --platform linux/amd64 --provenance=false \
  --output "type=image,name=${URI}:latest,oci-mediatypes=false,push=true" \
  -f Dockerfile.endpoint .
echo "pushed ${URI}:latest"
echo "image_uri = \"${URI}:latest\""   # paste into terraform.tfvars or -var
