# Security gate Lambda, packaged as a container image.
#
# Why an image and not a zip+layers: Trivy (~160 MB unzipped) + Checkov (~160 MB)
# together exceed Lambda's 250 MB unzipped quota for zip-based functions. Container
# image Lambdas raise that ceiling to 10 GB. See docs/runbook.md.
#
# Build for linux/amd64 (the Lambda function's default architecture) even on Apple
# Silicon: `docker build --platform linux/amd64 ...` (handled by deploy-gates.sh).

# --- Stage 1: fetch the Trivy binary (Trivy is a static Go binary; portable) ---
FROM alpine:3.20 AS trivy
ARG TRIVY_VERSION=0.72.0
RUN apk add --no-cache curl tar \
 && curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
    | tar -xz -C /tmp trivy

# --- Stage 2: the Lambda image ---
FROM public.ecr.aws/lambda/python:3.12

# Trivy binary on PATH; trivy_runner.py reads TRIVY_BIN.
COPY --from=trivy /tmp/trivy /usr/local/bin/trivy
ENV TRIVY_BIN=/usr/local/bin/trivy

# Python deps (checkov CLI + anthropic + requests). boto3 ships in the runtime.
COPY requirements-image.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements-image.txt

# Handler code.
COPY handler.py checkov_runner.py claude_summariser.py github_commenter.py trivy_runner.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
