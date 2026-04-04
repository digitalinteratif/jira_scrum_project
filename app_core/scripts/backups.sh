#!/usr/bin/env bash
#
# scripts/backups.sh - Logical PostgreSQL backup script (KAN-137)
#
# Purpose:
#   - Create point-in-time logical backups using pg_dump --format=custom (-Fc)
#   - Provide optional GPG encryption (public-key or symmetric) and optional upload to offsite storage (AWS S3 or rclone)
#   - Manage local retention/rotation and optionally verify uploaded artifact checksums
#   - Produce human-friendly trace lines to trace_KAN-137.txt (Architectural Memory)
#
# Design & Guardrails:
#   - Defensive: requires necessary CLI tools (pg_dump, pg_restore only used in docs/restore examples; aws/rclone optional)
#   - Environment-driven configuration (safe defaults). Do not embed secrets in script.
#   - Idempotent naming scheme: timestamped files.
#   - Safe file & directory permissions.
#   - Minimal assumptions about running user; prefer an ops account with appropriate DB and object-store privileges.
#
# Usage (examples):
#   # Full flow (create encrypted backup and upload to S3):
#   DATABASE_URL=postgresql://user:pass@db-host:5432/smartlink \
#     GPG_RECIPIENT="backup@ops.example.com" \
#     S3_BUCKET="s3://my-backup-bucket/smartlink" \
#     ./scripts/backups.sh --rotate 14 --upload s3
#
#   # Create an unencrypted backup and keep 7 days locally:
#   ./scripts/backups.sh --no-encrypt --rotate 7
#
#   # Dry run (shows commands that would run)
#   ./scripts/backups.sh --dry-run
#
# Exit codes:
#   0 - success (backup created; optional upload succeeded if requested)
#   1 - usage / argument error
#   2 - missing dependency
#   3 - DB dump failed
#   4 - encryption failed
#   5 - upload failed
#   6 - verification failed
#
# Notes:
#   - This is a logical backup. For large DBs or point-in-time recovery (PITR), use physical base backups + WAL shipping (pg_basebackup, WAL-E, wal-g, cloud native backups).
#   - Keep the private keys and encryption passphrases in a secure secret store (KMS, HashiCorp Vault, Secret Manager).
#
set -euo pipefail

# -----------------------------
# Configuration / Defaults
# -----------------------------
# Environment variables (can be overridden)
: "${DATABASE_URL:=${DATABASE_URL:-}}"
: "${PGHOST:=${PGHOST:-}}"
: "${PGPORT:=${PGPORT:-5432}}"
: "${PGUSER:=${PGUSER:-}}"
: "${PGDATABASE:=${PGDATABASE:-}}"
: "${PGPASSWORD:=${PGPASSWORD:-}}"

# Backup storage settings
DEFAULT_BACKUP_DIR="${PWD}/backups"
BACKUP_DIR="${BACKUP_DIR:-$DEFAULT_BACKUP_DIR}"   # local backup directory
ROTATE_DAYS="${ROTATE_DAYS:-14}"                  # default retention (days)
UMASK=${UMASK:-0077}                              # secure file perms by default (rwx------)

# Encryption settings:
# - If GPG_RECIPIENT is set => use public-key encrypt (gpg --encrypt --recipient ...)
# - If GPG_SYMM_PASS_ENV is set => use symmetric encryption with passphrase stored in named env var
GPG_RECIPIENT="${GPG_RECIPIENT:-}"
GPG_SYMM_PASS_ENV="${GPG_SYMM_PASS_ENV:-}"  # name of env var that contains passphrase (not the passphrase itself)

# Offsite upload settings:
# - UPLOAD_TARGET: 's3' or 'rclone' or empty (none)
# - For s3: set S3_BUCKET (e.g., s3://my-bucket/prefix)
# - For rclone: set RCLONE_REMOTE (e.g., "remote:bucket/prefix")
UPLOAD_TARGET="${UPLOAD_TARGET:-}"
S3_BUCKET="${S3_BUCKET:-}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"

# Checksum & verification
CHECKSUM_ALGO="sha256"   # used for local checksum file and remote verification

# Safety / dry-run
DRY_RUN=0

# Trace file (architectural memory)
TRACE_FILE="trace_KAN-137.txt"

# Timestamp for filenames
TS="$(date -u +"%Y%m%dT%H%M%SZ")"

# Helper to write trace lines non-blocking (best-effort)
_trace() {
  local msg="$1"
  # Append with timestamp
  {
    printf '%s %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$msg"
  } >>"${TRACE_FILE}" 2>/dev/null || true
}

# -----------------------------
# CLI parsing (simple)
# -----------------------------
_usage() {
  cat <<EOF
Usage: $0 [options]

Options:
  --backup-name NAME     : Optional friendly prefix for backup files (default: smartlink)
  --rotate DAYS          : Keep local backups this many days (default: ${ROTATE_DAYS})
  --upload s3|rclone     : Upload backup to offsite target after creation
  --no-encrypt           : Do not encrypt backup file (default: encrypt if GPG_RECIPIENT/GPG_SYMM_PASS_ENV set)
  --dry-run              : Print actions but do not execute them
  --help                 : Show this message
EOF
  exit 1
}

BACKUP_PREFIX="smartlink"
ENCRYPT=1

while [ "${#:-0}" -gt 0 ]; do
  case "${1:-}" in
    --backup-name) BACKUP_PREFIX="${2:-}"; shift 2;;
    --rotate) ROTATE_DAYS="${2:-}"; shift 2;;
    --upload) UPLOAD_TARGET="${2:-}"; shift 2;;
    --no-encrypt) ENCRYPT=0; shift 1;;
    --dry-run) DRY_RUN=1; shift 1;;
    --help) _usage;;
    "") break;;
    *) echo "Unknown arg: $1"; _usage;;
  esac
done

# If GPG_RECIPIENT or GPG_SYMM_PASS_ENV set, default ENCRYPT=1
if [ -n "${GPG_RECIPIENT}" ] || [ -n "${GPG_SYMM_PASS_ENV}" ]; then
  ENCRYPT=1
fi

# -----------------------------
# Dependency checks
# -----------------------------
_require_bin() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "[dry-run] require: $1"
    return 0
  fi
  if ! command -v "$1" >/dev/null 2>&1; then
    _trace "MISSING_DEP $1"
    echo "Error: required command not found: $1" >&2
    exit 2
  fi
}

# Only require optional upload tools conditionally
_require_bin pg_dump

if [ -n "${GPG_RECIPIENT}" ] || [ -n "${GPG_SYMM_PASS_ENV}" ]; then
  _require_bin gpg
fi

if [ "${UPLOAD_TARGET}" = "s3" ]; then
  _require_bin aws
elif [ "${UPLOAD_TARGET}" = "rclone" ]; then
  _require_bin rclone
fi

# -----------------------------
# Prepare environment & paths
# -----------------------------
umask "${UMASK}"

mkdir -p "${BACKUP_DIR}"

# Derive DB connection inputs in a way compatible with pg_dump.
# Prefer DATABASE_URL if set; otherwise fall back to PGHOST/PGUSER/PGDATABASE/PGPORT environment.
# pg_dump accepts --dbname argument with connection string.
PG_DSN="${DATABASE_URL:-}"
if [ -z "${PG_DSN}" ]; then
  # Build a minimal connection string (without exposing password in logs)
  if [ -n "${PGUSER}" ] && [ -n "${PGHOST}" ] && [ -n "${PGDATABASE}" ]; then
    PG_DSN="postgresql://${PGUSER}@${PGHOST}:${PGPORT}/${PGDATABASE}"
  fi
fi

BACKUP_FILENAME="${BACKUP_PREFIX}_${TS}.dump"
BACKUP_PATH="${BACKUP_DIR%/}/${BACKUP_FILENAME}"

# Temporary working copy (write in same FS as backups to avoid cross-FS rename issues)
TMP_BACKUP_PATH="${BACKUP_PATH}.tmp"

# checksum file
CHECKSUM_FILE="${BACKUP_PATH}.${CHECKSUM_ALGO}"

# encrypted path (if any)
ENCRYPTED_PATH="${BACKUP_PATH}.gpg"

_trace "BACKUP_START prefix=${BACKUP_PREFIX} dest=${BACKUP_DIR} ts=${TS} rotate_days=${ROTATE_DAYS} encrypt=${ENCRYPT} upload=${UPLOAD_TARGET}"

# -----------------------------
# Perform pg_dump (custom format -Fc)
# -----------------------------
_pg_dump() {
  # Use --format=custom for pg_restore compatibility and compressed storage
  # Use --verbose only in non-dry-run (we capture errors by exit code)
  local db_arg=()
  if [ -n "${PG_DSN}" ]; then
    db_arg=( --dbname "${PG_DSN}" )
  fi

  # Include globals (roles) with pg_dumpall; warn operator to handle roles separately.
  # For now, dump only the single DB content. Document in docs that pg_dumpall --globals-only should be run periodically.
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "[dry-run] pg_dump --format=custom -f \"${TMP_BACKUP_PATH}\" ${db_arg[*]}"
    return 0
  fi

  # Run pg_dump; allow environment vars PGPASSWORD to be set by operator for non-interactive runs (secure secrets store recommended)
  _trace "PG_DUMP_CMD start tmp=${TMP_BACKUP_PATH}"
  if ! pg_dump --format=custom --file="${TMP_BACKUP_PATH}" "${db_arg[@]}"; then
    _trace "PG_DUMP_FAILED tmp=${TMP_BACKUP_PATH}"
    echo "pg_dump failed" >&2
    return 3
  fi
  _trace "PG_DUMP_OK tmp=${TMP_BACKUP_PATH}"
  return 0
}

rc=$(_pg_dump || echo "RC:$?")
if [ "${rc}" != "0" ]; then
  # rc may be a number or "RC:3" string; normalize
  if [[ "${rc}" == RC:* ]]; then
    rcnum="${rc#RC:}"
  else
    rcnum="${rc}"
  fi
  exit "${rcnum}"
fi

# Move tmp into final name atomically
if [ "${DRY_RUN}" -eq 1 ]; then
  echo "[dry-run] mv \"${TMP_BACKUP_PATH}\" \"${BACKUP_PATH}\""
else
  mv "${TMP_BACKUP_PATH}" "${BACKUP_PATH}"
  chmod 600 "${BACKUP_PATH}" || true
fi

_trace "BACKUP_CREATED path=${BACKUP_PATH}"

# -----------------------------
# Compute checksum
# -----------------------------
_compute_checksum() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "[dry-run] ${CHECKSUM_ALGO}sum \"${BACKUP_PATH}\" > \"${CHECKSUM_FILE}\""
    return 0
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${BACKUP_PATH}" | awk '{print $1}' > "${CHECKSUM_FILE}"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${BACKUP_PATH}" | awk '{print $1}' > "${CHECKSUM_FILE}"
  else
    _trace "CHECKSUM_TOOL_MISSING"
    echo "Warning: no checksum tool available (sha256sum/shasum)" >&2
    return 0
  fi
  chmod 600 "${CHECKSUM_FILE}" || true
  _trace "CHECKSUM_CREATED file=${CHECKSUM_FILE}"
}

_compute_checksum

# -----------------------------
# Optional encryption
# -----------------------------
_encrypt() {
  if [ "${ENCRYPT}" -eq 0 ]; then
    _trace "ENCRYPT_SKIP explicit_no_encrypt"
    return 0
  fi

  # Decide encryption mode
  if [ -n "${GPG_RECIPIENT}" ]; then
    # Public-key encryption
    if [ "${DRY_RUN}" -eq 1 ]; then
      echo "[dry-run] gpg --output \"${ENCRYPTED_PATH}\" --encrypt --recipient \"${GPG_RECIPIENT}\" \"${BACKUP_PATH}\""
      return 0
    fi
    if ! gpg --batch --yes --output "${ENCRYPTED_PATH}" --encrypt --recipient "${GPG_RECIPIENT}" "${BACKUP_PATH}"; then
      _trace "ENCRYPT_GPG_FAILED recipient=${GPG_RECIPIENT}"
      return 4
    fi
    chmod 600 "${ENCRYPTED_PATH}" || true
    _trace "ENCRYPT_GPG_OK recipient=${GPG_RECIPIENT} out=${ENCRYPTED_PATH}"
    return 0
  elif [ -n "${GPG_SYMM_PASS_ENV}" ] && [ -n "${!GPG_SYMM_PASS_ENV:-}" ]; then
    # Symmetric passphrase encryption, passphrase stored in env var named by GPG_SYMM_PASS_ENV
    local pass
    pass="${!GPG_SYMM_PASS_ENV}"
    if [ "${DRY_RUN}" -eq 1 ]; then
      echo "[dry-run] gpg --batch --yes --passphrase [REDACTED] --output \"${ENCRYPTED_PATH}\" --symmetric \"${BACKUP_PATH}\""
      return 0
    fi
    if ! gpg --batch --yes --passphrase "${pass}" --pinentry-mode loopback --output "${ENCRYPTED_PATH}" --symmetric "${BACKUP_PATH}"; then
      _trace "ENCRYPT_SYMM_FAILED env=${GPG_SYMM_PASS_ENV}"
      return 4
    fi
    chmod 600 "${ENCRYPTED_PATH}" || true
    _trace "ENCRYPT_SYMM_OK env=${GPG_SYMM_PASS_ENV} out=${ENCRYPTED_PATH}"
    return 0
  else
    _trace "ENCRYPT_SKIP no_gpg_configured"
    return 0
  fi
}

if [ "${ENCRYPT}" -eq 1 ]; then
  # If encryption requested but no config, warn and proceed unencrypted
  if [ -z "${GPG_RECIPIENT}" ] && [ -z "${GPG_SYMM_PASS_ENV}" ]; then
    _trace "ENCRYPT_REQUESTED_BUT_NO_CONFIG"
    echo "Encryption requested but neither GPG_RECIPIENT nor GPG_SYMM_PASS_ENV is configured. Proceeding unencrypted." >&2
  else
    rc_enc=$(_encrypt || echo "RC:$?")
    if [ "${rc_enc}" != "0" ]; then
      if [[ "${rc_enc}" == RC:* ]]; then
        exit "${rc_enc#RC:}"
      else
        exit "${rc_enc}"
      fi
    fi
  fi
fi

# Candidate artifact to upload: prefer encrypted file if created, else raw backup
ARTIFACT_PATH="${ENCRYPTED_PATH}"
if [ ! -f "${ARTIFACT_PATH}" ]; then
  ARTIFACT_PATH="${BACKUP_PATH}"
fi

# -----------------------------
# Optional offsite upload
# -----------------------------
_upload_s3() {
  if [ -z "${S3_BUCKET}" ]; then
    echo "S3_BUCKET not configured; cannot upload to s3" >&2
    _trace "UPLOAD_S3_MISSING_BUCKET"
    return 5
  fi
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "[dry-run] aws s3 cp \"${ARTIFACT_PATH}\" \"${S3_BUCKET%/}/$(basename "${ARTIFACT_PATH}")\" --acl bucket-owner-full-control"
    return 0
  fi

  # Use aws cli cp. It will use the environment credentials or instance role.
  if ! aws s3 cp "${ARTIFACT_PATH}" "${S3_BUCKET%/}/$(basename "${ARTIFACT_PATH}")" --acl bucket-owner-full-control; then
    _trace "UPLOAD_S3_FAILED path=${ARTIFACT_PATH} bucket=${S3_BUCKET}"
    return 5
  fi
  # Also upload the checksum file if present
  if [ -f "${CHECKSUM_FILE}" ]; then
    aws s3 cp "${CHECKSUM_FILE}" "${S3_BUCKET%/}/$(basename "${CHECKSUM_FILE}")" --acl bucket-owner-full-control || true
  fi
  _trace "UPLOAD_S3_OK path=${ARTIFACT_PATH} bucket=${S3_BUCKET}"
  return 0
}

_upload_rclone() {
  if [ -z "${RCLONE_REMOTE}" ]; then
    echo "RCLONE_REMOTE not configured; cannot upload via rclone" >&2
    _trace "UPLOAD_RCLONE_MISSING_REMOTE"
    return 5
  fi
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "[dry-run] rclone copyto \"${ARTIFACT_PATH}\" \"${RCLONE_REMOTE%/}/$(basename "${ARTIFACT_PATH}")\""
    return 0
  fi
  if ! rclone copyto "${ARTIFACT_PATH}" "${RCLONE_REMOTE%/}/$(basename "${ARTIFACT_PATH}")" --no-traverse; then
    _trace "UPLOAD_RCLONE_FAILED path=${ARTIFACT_PATH} remote=${RCLONE_REMOTE}"
    return 5
  fi
  if [ -f "${CHECKSUM_FILE}" ]; then
    rclone copyto "${CHECKSUM_FILE}" "${RCLONE_REMOTE%/}/$(basename "${CHECKSUM_FILE}")" --no-traverse >/dev/null 2>&1 || true
  fi
  _trace "UPLOAD_RCLONE_OK path=${ARTIFACT_PATH} remote=${RCLONE_REMOTE}"
  return 0
}

if [ -n "${UPLOAD_TARGET}" ]; then
  if [ "${UPLOAD_TARGET}" = "s3" ]; then
    rc_up=$(_upload_s3 || echo "RC:$?")
  elif [ "${UPLOAD_TARGET}" = "rclone" ]; then
    rc_up=$(_upload_rclone || echo "RC:$?")
  else
    echo "Unsupported upload target: ${UPLOAD_TARGET}" >&2
    _trace "UPLOAD_UNSUPPORTED target=${UPLOAD_TARGET}"
    exit 1
  fi

  if [ "${rc_up}" != "0" ]; then
    if [[ "${rc_up}" == RC:* ]]; then
      exit "${rc_up#RC:}"
    else
      exit "${rc_up}"
    fi
  fi
fi

# -----------------------------
# Optional verification (local and remote)
# -----------------------------
verify_local_checksum() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "[dry-run] verify checksum for ${BACKUP_PATH} using ${CHECKSUM_FILE}"
    return 0
  fi
  if [ ! -f "${CHECKSUM_FILE}" ]; then
    _trace "VERIFY_LOCAL_NO_CHECKSUM file=${CHECKSUM_FILE}"
    echo "Warning: checksum file missing; skipping verification" >&2
    return 0
  fi
  local expected
  expected="$(cat "${CHECKSUM_FILE}")" || true
  local actual
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "${BACKUP_PATH}" | awk '{print $1}')" || true
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "${BACKUP_PATH}" | awk '{print $1}')" || true
  else
    echo "No checksum tool available for verification" >&2
    _trace "VERIFY_LOCAL_TOOL_MISSING"
    return 0
  fi
  if [ "${actual}" != "${expected}" ]; then
    _trace "VERIFY_LOCAL_MISMATCH expected=${expected} actual=${actual} path=${BACKUP_PATH}"
    echo "Local checksum mismatch!" >&2
    return 6
  fi
  _trace "VERIFY_LOCAL_OK path=${BACKUP_PATH}"
  return 0
}

if ! verify_local_checksum; then
  exit 6
fi

# Remote verification would be provider-specific: for S3 you can compare the uploaded object's etag/sha256 metadata if available.
# For brevity we omit an automatic remote verify in script but document verification steps in docs/backup_restore_plan.md.

# -----------------------------
# Rotation / prune local backups older than ROTATE_DAYS
# -----------------------------
_rotate() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "[dry-run] find \"${BACKUP_DIR}\" -type f -name \"${BACKUP_PREFIX}_*.dump*\" -mtime +${ROTATE_DAYS} -print -delete"
    return 0
  fi
  find "${BACKUP_DIR}" -type f -name "${BACKUP_PREFIX}_*.dump*" -mtime +"${ROTATE_DAYS}" -print -delete 2>/dev/null || true
  _trace "ROTATE_APPLIED dir=${BACKUP_DIR} keep_days=${ROTATE_DAYS}"
}

_rotate

_trace "BACKUP_COMPLETED artifact=${ARTIFACT_PATH} checksum=${CHECKSUM_FILE} rotate_days=${ROTATE_DAYS}"
echo "Backup completed: ${ARTIFACT_PATH}"
exit 0

# End of scripts/backups.sh