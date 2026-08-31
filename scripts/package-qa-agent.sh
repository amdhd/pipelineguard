#!/usr/bin/env bash
#
# Packages the QA agent into a Lambda-style deployment zip and uploads it to the
# code bucket. Companion to scripts/deploy-gates.sh, which does the equivalent
# job for the security gate's container image.
#
# The AgentCore runtime is created from an S3 object, so this must run BEFORE the
# apply that creates the runtime -- the same ordering constraint the gate image
# has.
#
#   AWS_PROFILE=pipelineguard ./scripts/package-qa-agent.sh [environment] [region]
#
# Prints the object key and version id; pass those to Terraform.
#
# ---------------------------------------------------------------------------
# CROSS-PLATFORM PACKAGING, AND WHY IT IS NOT OPTIONAL
#
# The zip is built here -- on a laptop, or a runner -- and executed on AWS's
# managed Linux runtime. Nothing between the two checks that they agree, so a
# mismatch surfaces as an ImportError at INVOKE time, after a browser session has
# already been paid for.
#
# Native code cannot simply be avoided: bedrock-agentcore requires pydantic
# (Rust core) and pulls in websockets (C speedups). So dependencies are installed
# FOR THE TARGET, not for this machine, using pip's platform selection -- the
# same technique Lambda packaging uses.
#
# This works because those wheels are HONESTLY TAGGED. It is exactly why
# Playwright is not a dependency here: its wheel claims py3-none-any and
# Root-Is-Purelib while bundling a `node` driver chosen for the install platform,
# so pip has nothing to select and --platform cannot correct it. A Mac-built zip
# would carry a Mach-O binary. See the note at the top of agents/qa/agent/cdp.py.
#
# The verification step below therefore does NOT reject native code. It rejects
# native code built for the WRONG target -- wrong OS, wrong architecture, or
# wrong Python minor -- which is the failure that actually bites.
# ---------------------------------------------------------------------------
set -euo pipefail

ENVIRONMENT="${1:-dev}"
REGION="${2:-ap-southeast-1}"

# Must match the `runtime` enum on aws_bedrockagentcore_agent_runtime. Vendoring
# for a different minor is the other way to earn an ImportError at invoke time:
# the .so files are tagged cpython-3XX and will not load on a different one.
PY_VERSION="${PY_VERSION:-3.12}"
PY_TAG="cp${PY_VERSION/./}"

# AgentCore Runtime CONTAINERS are documented ARM64-only, so the managed code
# runtime is assumed to match. That assumption is not confirmed by any API --
# override with TARGET_ARCH=x86_64 if a deployed runtime turns out to disagree,
# and record the answer in docs/agentcore/DISCOVERY.md when it is known.
TARGET_ARCH="${TARGET_ARCH:-aarch64}"
PLATFORM="manylinux2014_${TARGET_ARCH}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${ROOT}/agents/qa/agent"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="pipelineguard-qa-${ENVIRONMENT}-code-${ACCOUNT_ID}"

BUILD="$(mktemp -d)"
ZIP="${BUILD}.zip"
trap 'rm -rf "${BUILD}" "${ZIP}"' EXIT

echo "==> Source:   ${SRC}"
echo "==> Bucket:   ${BUCKET}"
echo "==> Target:   ${PLATFORM} / python ${PY_VERSION}"

# --- vendor dependencies FOR THE TARGET, not for this machine ---
echo "==> Installing dependencies for ${PLATFORM}..."
python3 -m pip install \
  --quiet --disable-pip-version-check \
  --target "${BUILD}" \
  --platform "${PLATFORM}" \
  --implementation cp \
  --python-version "${PY_VERSION}" \
  --only-binary=:all: \
  --requirement "${SRC}/requirements.txt"

# --- copy the agent itself ---
# Flat layout: AgentCore runs `agent.py` from the zip root, so the sibling
# modules sit beside it rather than in a package.
for f in agent.py rubric.py schema.py browser_tools.py cdp.py candidates.py; do
  cp "${SRC}/${f}" "${BUILD}/"
done

# --- verify every native object targets the runtime, not this laptop ---
echo "==> Verifying native objects target ${TARGET_ARCH} / ${PY_TAG}..."
BAD=""
while IFS= read -r so; do
  [ -z "${so}" ] && continue
  desc="$(file -b "${so}")"
  case "${desc}" in
    *ELF*) ;;
    *) BAD="${BAD}\n  WRONG OS   ${so#${BUILD}/} -- ${desc}" ; continue ;;
  esac
  case "${desc}" in
    *aarch64*|*ARM\ aarch64*) found_arch="aarch64" ;;
    *x86-64*)                 found_arch="x86_64" ;;
    *)                        found_arch="unknown" ;;
  esac
  [ "${found_arch}" = "${TARGET_ARCH}" ] || \
    BAD="${BAD}\n  WRONG ARCH ${so#${BUILD}/} -- ${found_arch}, expected ${TARGET_ARCH}"
  case "${so}" in
    *cpython-*)
      case "${so}" in
        *cpython-${PY_VERSION/./}*) ;;
        *) BAD="${BAD}\n  WRONG PY   ${so#${BUILD}/} -- expected cpython-${PY_VERSION/./}" ;;
      esac
      ;;
  esac
done < <(find "${BUILD}" \( -name '*.so' -o -name '*.so.*' -o -name '*.dylib' -o -name '*.pyd' -o -name '*.dll' \) -print)

# Extensionless native executables -- how Playwright's `node` driver would slip
# past a suffix-only check.
while IFS= read -r exe; do
  [ -z "${exe}" ] && continue
  case "$(file -b "${exe}")" in
    Mach-O*|PE32*) BAD="${BAD}\n  NON-LINUX  ${exe#${BUILD}/} -- $(file -b "${exe}" | cut -c1-50)" ;;
  esac
done < <(find "${BUILD}" -type f -perm -u+x ! -name '*.py' -print)

if [ -n "${BAD}" ]; then
  echo "" >&2
  echo "ERROR: the build tree contains objects that will not run on the runtime." >&2
  echo "This fails at INVOKE time with an ImportError, not here, so packaging" >&2
  echo "refuses instead." >&2
  # shellcheck disable=SC2059
  printf "${BAD}\n" >&2
  echo "" >&2
  echo "If a dependency ships no manylinux wheel, it cannot be vendored this" >&2
  echo "way. See the note at the top of agents/qa/agent/cdp.py." >&2
  exit 1
fi
echo "    all native objects target ${TARGET_ARCH}/${PY_TAG}."

# --- zip ---
( cd "${BUILD}" && zip -qr "${ZIP}" . -x '*.pyc' -x '*__pycache__*' )
echo "==> Built $(du -h "${ZIP}" | cut -f1) zip"

# --- upload ---
KEY="agent/qa-agent-${ENVIRONMENT}.zip"
VERSION_ID="$(aws s3api put-object \
  --bucket "${BUCKET}" \
  --key "${KEY}" \
  --body "${ZIP}" \
  --region "${REGION}" \
  --query VersionId --output text)"

echo ""
echo "==> Uploaded s3://${BUCKET}/${KEY}"
echo "==> Version:  ${VERSION_ID}"
echo ""
echo "Record it in infra/environments/dev.tfvars, in the same commit as the"
echo "code change, so the deployed version is reviewable in git:"
echo ""
echo "  qa_agent_code_key        = \"${KEY}\""
echo "  qa_agent_code_version_id = \"${VERSION_ID}\""
echo ""
echo "Then apply normally:  ./scripts/apply-dev.sh"
echo ""
echo "Do NOT pass these as -var flags instead. A value that lives only in your"
echo "shell history is a value the next plain apply silently reverts — and an"
echo "ABSENT key destroys the runtime outright. See the note in dev.tfvars."
