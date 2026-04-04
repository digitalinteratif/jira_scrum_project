# Backup & Restore Plan — Smart Link (KAN-137)

This document describes the recommended backup and restore processes for the Smart Link service (KAN-137).
It includes operational scripts (scripts/backups.sh), scheduling examples (cron / systemd timer), retention and rotation policy,
encryption and secure storage recommendations, restoration steps, and test procedures.

This plan is intentionally implementation-agnostic but provides concrete commands and examples using:
- pg_dump / pg_restore for logical backups (custom format)
- Optional encryption via GPG
- Optional offsite storage via AWS S3 or rclone
- Recommendations for large DBs and point-in-time recovery (PITR)

Architectural constraints:
- The repository contains a scripts/backups.sh script that implements routine logical backups in pg_dump custom format (-Fc).
- The script writes trace entries to trace_KAN-137.txt for Architectural Memory.
- Backups must be performed and verified periodically; restore tests must be performed against a staging environment as described below.

---

Contents:
1. Backup overview (what is backed up)
2. Backup script usage (scripts/backups.sh)
3. Retention & rotation policy
4. Encryption & secure storage recommendations
5. Offsite storage & lifecycle examples (S3)
6. Restore procedures (staging and production)
7. Restore test / verification procedures
8. Scheduling examples (cron/systemd)
9. Large DB considerations and incremental strategies (PITR)
10. Emergency restore checklist
11. Appendix: sample commands

---

1) Backup overview
-------------------
- What the logical backup contains:
  - Data and schema for the configured database (pg_dump --format=custom).
  - The script does not automatically back up global cluster objects (roles, tablespaces). Operators should run:
      pg_dumpall --globals-only > globals.sql
    and store that file alongside logical dumps (or manage roles via IaC).
- Why custom format:
  - pg_dump -Fc produces a compact, index-friendly format that can be restored with pg_restore and allows parallel restore.
- Limitations:
  - Logical backups cannot provide point-in-time recovery (PITR). For PITR, use physical base backups + WAL shipping (pg_basebackup, wal-g).

---

2) Backup script (scripts/backups.sh)
---------------------------------------
- Location: scripts/backups.sh (executable)
- Key behaviors:
  - Creates timestamped dump: <prefix>_YYYYMMDDTHHMMSSZ.dump (UTC).
  - Uses pg_dump --format=custom for portability and speed.
  - Generates SHA256 checksum file next to the dump.
  - Optional encryption:
    - Public-key gpg: set GPG_RECIPIENT.
    - Symmetric gpg: set GPG_SYMM_PASS_ENV to the name of an environment variable that stores the passphrase.
  - Optional upload:
    - S3: set UPLOAD_TARGET=s3 and S3_BUCKET (e.g., s3://my-bucket/prefix)
    - rclone: set UPLOAD_TARGET=rclone and RCLONE_REMOTE (e.g., remote:bucket/prefix)
  - Retention: deletes local backups older than configured rotate days.
  - Writes trace lines to trace_KAN-137.txt.

- Example environment/config points:
  - DATABASE_URL or PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSWORD
  - BACKUP_DIR (defaults to ./backups)
  - ROTATE_DAYS (default 14)
  - GPG_RECIPIENT or GPG_SYMM_PASS_ENV
  - UPLOAD_TARGET (s3|rclone) + S3_BUCKET or RCLONE_REMOTE

---

3) Retention & rotation policy
-------------------------------
Recommended baseline (configurable by operators):
- Local copies: retain 14 days (ROTATE_DAYS=14)
- Offsite copies (S3): retain 90 days with lifecycle rules (see S3 example).
- Cold archive: move older backups (>90 days) to cheaper archival storage (S3 Glacier / Glacier Deep Archive).
- Keep at least:
  - N recent nightly backups (14 days)
  - M weekly backups (e.g., last 12 weeks)
  - P monthly backups (e.g., last 12 months) — can be implemented by selective copying or lifecycle rules.

S3 lifecycle example (JSON) to transition to Glacier after 30 days and expire after 365 days:
{
  "Rules": [
    {
      "ID": "SmartLinkBackupLifecycle",
      "Prefix": "smartlink/",
      "Status": "Enabled",
      "Transitions": [
        { "Days": 30, "StorageClass": "GLACIER" }
      ],
      "Expiration": {
        "Days": 365
      },
      "NoncurrentVersionExpiration": { "NoncurrentDays": 365 }
    }
  ]
}

Notes:
- Retention policies must satisfy legal/regulatory requirements for your jurisdiction.
- Keep a small set of snapshots (weekly/monthly) for long-term forensic investigations.

---

4) Encryption & secure storage recommendations
-----------------------------------------------
- Always encrypt backups at rest and in transit.
- Recommended approaches:
  1. Public-key encryption (recommended for team scenarios):
     - Use GPG public keys of the ops/backups keyring (GPG_RECIPIENT).
     - Store private keys securely offline or in a KMS-protected secret manager.
  2. Envelope encryption with cloud KMS:
     - Use a per-backup data encryption key, encrypt the data with AES-GCM, and wrap the DEK with AWS KMS / GCP KMS / Azure Key Vault (recommended for S3).
  3. Symmetric encryption (passphrase):
     - Use only if public-key infrastructure not available. Store passphrase in a secrets manager (not in plain environment).
- Key rotation:
  - Rotate encryption keys periodically. Keep previous keys available to decrypt older archives until they expire.
  - Maintain a key-rotation policy: e.g., rotate annually, but ensure old keys can still be used to decrypt within retention window.
- Access control:
  - Limit who can download backups. Use IAM policies for S3 and restrict read access.
  - Audit and log all access to backup buckets/objects.
- Secrets:
  - Do not hard-code DB passwords or passphrases in scripts. Use secret stores (Vault, AWS Secrets Manager) and fetch secrets at runtime as ephemeral environment vars.

---

5) Offsite storage & lifecycle (S3 examples)
---------------------------------------------
- Upload to S3 via aws cli:
  aws s3 cp ./backups/smartlink_20240101T000000Z.dump s3://my-backup-bucket/smartlink/
  aws s3 cp ./backups/smartlink_20240101T000000Z.dump.gpg s3://my-backup-bucket/smartlink/
- Recommended S3 server-side encryption (SSE):
  - SSE-KMS preferred (AWS KMS), e.g., use --sse aws:kms or specify cmk via --sse-kms-key-id
- Lifecycle:
  - Use lifecycle rules to move backups to Glacier/Deep Archive after X days and delete after Y days.
- Bucket policies:
  - Use bucket policies to require encryption on upload and to restrict access to specific roles or IPs.

Rclone:
- rclone provides connectors to many providers; configure remote once (securely) and use:
  rclone copyto ./backups/smartlink_20240101T000000Z.dump remote:bucket/smartlink/smartlink_20240101T000000Z.dump

---

6) Restore procedures (staging & production)
---------------------------------------------
A. Basic restore to existing PostgreSQL instance (staging or temporary DB)
1) Create a clean target database (as a user with sufficient privileges):
   psql -h target-host -U postgres -c "CREATE DATABASE smartlink_staging OWNER someuser;"

2) If you have an encrypted artifact (GPG):
   # Public-key decrypt (requires private key available)
   gpg --output smartlink_20240101T000000Z.dump --decrypt smartlink_20240101T000000Z.dump.gpg

3) Restore using pg_restore:
   # recommended flags:
   pg_restore --dbname=postgresql://someuser@target-host:5432/smartlink_staging \
     --clean --if-exists --no-owner --no-acl --verbose \
     /path/to/smartlink_20240101T000000Z.dump

   Explanation:
   - --clean --if-exists: drop objects before recreating (helpful for idempotent restores to staging).
   - --no-owner --no-acl: avoid issues when restoring into different role setup.
   - For parallel restore (faster on multi-core machines & large dumps), use:
     pg_restore --jobs=4 ... --format=custom ...

4) If you also backed up globals (roles):
   psql -h target-host -U postgres -f globals.sql

B. Restore to a production database (dangerous; requires planned downtime)
- Production restores are disruptive. Prefer restoring to staging and promote only after verification.
- If you must restore to production:
  - Ensure database is stopped from accepting writes or put app into maintenance mode.
  - Take a final backup before restore if possible.
  - Restore as above, but coordinate with DBAs/operators for WAL / replication adjustments and for role/ownership mapping.

C. Restore for schema-only or selective objects:
- Use pg_restore --schema-only or --table to restore specific components.

D. Example full workflow to restore to a staging host:
   # Decrypt
   gpg --output /tmp/smartlink.dump --decrypt s3://.../smartlink.dump.gpg

   # Create DB
   psql -h staging -U postgres -c "DROP DATABASE IF EXISTS smartlink_staging; CREATE DATABASE smartlink_staging;"

   # Restore (parallel)
   pg_restore --dbname=postgresql://postgres@staging:5432/smartlink_staging --jobs=6 --no-owner --no-acl /tmp/smartlink.dump

---

7) Restore test & verification procedures (Acceptance Criteria)
----------------------------------------------------------------
Acceptance Criteria (from ticket):
- Given the backup scripts and schedule
- When a backup is taken and restored to staging
- Then the restore process succeeds and data integrity is preserved according to documented steps.

Recommended test cadence:
- Nightly automated backup verification:
  - Take a nightly backup.
  - Restore to a short-lived staging database (e.g., smartlink_test_restore_{date}).
  - Run a smoke-check script (e.g., the project's smoke_ci or targeted integration tests) against the staging DB.
  - Validate counts for key tables (users, shorturls, click events) match expected ranges / are non-empty depending on data volume.
  - Record the test in trace_KAN-137.txt (automatic); alert on failure.

- Full restore drill:
  - Quarterly: perform full restore and run the full test suite (or representative integration tests).
  - Document expected downtime and execution steps.

Restore verification checklist:
- Schema restored successfully (no missing tables/indexes).
- Row counts for critical tables match those before backup (allowing some in-flight variance).
- Primary web flows function: registration, login, create short URL, redirect.
- Materialized views (if used) are present or rebuildable by analytics refresh jobs.
- Verify auth/role mapping if restore to different environment.

Important: Always perform restores to staging, never production, unless under an emergency plan.

---

8) Scheduling examples (cron / systemd)
---------------------------------------
A. Cron (simple):
- Daily backup at 02:15 UTC and upload to S3:
  15 2 * * * DATABASE_URL="postgresql://user:pass@db-host:5432/smartlink" \
    BACKUP_DIR="/var/backups/smartlink" ROTATE_DAYS=30 \
    GPG_RECIPIENT="backup@ops.example.com" \
    UPLOAD_TARGET="s3" S3_BUCKET="s3://my-backup-bucket/smartlink" \
    /opt/app/scripts/backups.sh --backup-name smartlink --rotate 30 >> /var/log/smartlink/backups.log 2>&1

- Important: Put secrets (DB password, GPG passphrase) in a secure environment (systemd service with ProtectSystem and SecretManager) or use IAM roles and KMS.

B. systemd timer (more robust)
- Create a systemd service unit that runs the script and a corresponding timer unit to schedule it; advantages include controlled environment and logging to journal.

Example snippet (conceptual):
- /etc/systemd/system/smartlink-backup.service
  [Unit]
  Description=Smartlink DB backup
  [Service]
  Type=oneshot
  ExecStart=/opt/app/scripts/backups.sh --backup-name smartlink --rotate 30 --upload s3
  Environment=BACKUP_DIR=/var/backups/smartlink
  Environment=ROTATE_DAYS=30
  Environment=GPG_RECIPIENT=backup@ops.example.com
  Environment=S3_BUCKET=s3://my-backup-bucket/smartlink
  # For secrets, use systemd-ask-password or secret store integration

- /etc/systemd/system/smartlink-backup.timer
  [Unit]
  Description=Run smartlink backup daily
  [Timer]
  OnCalendar=*-*-* 02:15:00
  Persistent=true
  [Install]
  WantedBy=timers.target

C. CI Automation (e.g., GitHub Actions / Jenkins)
- For long-term backups, use the script on a dedicated host with sufficient disk and credentials. CI is not ideal for storing secrets.

---

9) Large DBs / incremental strategies / PITR
---------------------------------------------
- Logical dumps (pg_dump) can be CPU and IO heavy on large DBs. For very large DBs (many 10s or 100s of GB):
  - Prefer physical backups:
    - Use pg_basebackup (base backup) + WAL shipping to enable PITR.
    - Tools: wal-e, wal-g, pgBackRest, Barman.
  - Implement WAL archiving to offsite (S3). This enables point-in-time restores between base backups.
  - Minimize downtime by using streaming replication and performing restores to standby for verification.

- Incremental logical backups:
  - pg_dump does not support incremental logical backups. Alternatives:
    - Use logical replication slots and a replica database.
    - Use scheduled partial dumps (only deltas) for very large append-only tables (requires application-level support).
  - For analytics tables like ClickEvent (high throughput), consider retention & purge strategies and separate archival pipelines (e.g., export older rows to colder storage rather than full daily backups).

- Downtime considerations:
  - For straightforward pg_restore to production, plan downtime. For minimal downtime:
    - Restore to a new host, switch application DB connection once validated. This requires DNS/load-balancer orchestration and replication planning.

---

10) Emergency restore checklist
-------------------------------
When a restore to production is required:
1. Notify stakeholders and enter incident channel.
2. Ensure you have recent backups and decryption keys.
3. Take a final snapshot of current cluster (if possible).
4. Restore to an isolated staging database and validate (smoke tests).
5. If validated, schedule downtime window and perform production restore.
6. Monitor application and DB health closely after restore.
7. Document the event in incident logs and record which backup was used (trace_KAN-137.txt).

---

11) Appendix — sample commands & examples
------------------------------------------
A. Take global roles backup (run periodically; store with logical dumps):
   pg_dumpall --globals-only > /path/to/backups/globals.sql

B. Example manual backup (custom format):
   pg_dump --format=custom --file=smartlink_20240101T000000Z.dump "postgresql://user:pass@db-host:5432/smartlink"

C. Example decrypt (GPG recipient assumed):
   gpg --output smartlink_20240101T000000Z.dump --decrypt smartlink_20240101T000000Z.dump.gpg

D. Example restore (to staging DB):
   createdb -h staging -U postgres smartlink_staging
   pg_restore --dbname=postgresql://postgres@staging:5432/smartlink_staging --jobs=4 --no-owner --no-acl --clean --if-exists /tmp/smartlink_20240101T000000Z.dump

E. Verify counts:
   psql -h staging -U postgres -d smartlink_staging -c "SELECT count(*) FROM shorturls;"
   psql -h staging -U postgres -d smartlink_staging -c "SELECT count(*) FROM users;"

F. Example AWS S3 upload with server-side encryption (SSE-KMS):
   aws s3 cp smartlink_20240101T000000Z.dump s3://my-backup-bucket/smartlink/ --sse aws:kms --sse-kms-key-id alias/smartlink-backups

---

Change log / Trace
-------------------
- All run metadata and key events should be appended into trace_KAN-137.txt by the scripting utility. Do not rely solely on ephemeral CI logs.
- Keep the trace file for audit purposes; rotate traces in the same retention cycle as other operational artifacts.

---

Operational notes
-----------------
- Periodically test restores and document outcomes.
- Store decryption private keys and KMS keys securely. Use cloud key rotation and auditing.
- Use role-based access control for any backup automation host. Avoid storing DB plaintext credentials in source.
- Keep at least two distinct offsite copies (different providers / regions) for resilience.

---

Questions & Next steps
----------------------
- Decide: Use public-key (GPG) or envelope/KMS encryption? Envelope encryption with cloud KMS is recommended for integration with S3 lifecycles and IAM.
- Decide central schedule & retention config values; implement systemd timers or Cron on a hardened backup host.
- Implement weekly restore test automation and attach it to on-call alerting for failure.

End of backup & restore plan.

------------------------

Implementation notes for the repo (what to add)
- Add scripts/backups.sh (executable).
- Add docs/backup_restore_plan.md.
- Add trace_KAN-137.txt to be created at runtime by the script (gitignore recommended to exclude heavy binary dumps and traces if you don't want them committed).
- Optionally create a systemd unit file (not included here) and CI job for nightly restore testing.

-----

If you'd like, I can:
- Create the exact git patch (diff) text to add these files.
- Create a minimal systemd service & timer units.
- Adjust the script to include S3 metadata verification (e.g., upload and verify object ETag or server-side checksum) for your specific cloud provider.
- Add automated restore-test runner that performs the staged restore and runs a small smoke test and writes trace_KAN-137-restore.txt.

Which of the above would you like me to produce next (git patch, systemd units, restore-test automation)?