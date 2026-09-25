# Running the report site

This guide covers going live, the admin's routine, and what to do when
something breaks.

## 1. What you need

- **A small Linux server** with Docker (1 vCPU, 2 GB RAM and 20 GB disk are
  plenty), and a domain name pointing at it, e.g. `report.whitehomes.sa`.
- **An SMTP account** for email, e.g. Google Workspace SMTP relay, Microsoft
  365 or Postmark.
- **Optional:** a Google Cloud service account, if statements should be picked
  up from Google Drive.

## 2. First deployment

```bash
git clone https://github.com/mmorad-blip/Whitehomes-Rikz-Portfolio.git /srv/rikz
cd /srv/rikz
cp .env.example .env && chmod 600 .env
docker compose build
docker compose run --rm web rikz make-keys      # copy the env lines into .env; note the two access keys
# fill in RIKZ_DOMAIN, RIKZ_PUBLIC_URL, POSTGRES_PASSWORD, SMTP_*, e-mail lists, Drive settings in .env
docker compose run --rm web rikz check-config --smtp --drive
docker compose up -d
```

Caddy gets the HTTPS certificate automatically on first request. Then:

1. Open the **admin link** (`https://<domain>/a/<admin token>/`) and enter
   the admin key.
2. Under **Admin → Upload statements**, upload all Awaed confirmations plus
   the latest Manafa portfolio export **and** account statement together.
   The first report is created.
3. Check the report, then press **Send to shareholders**.

Give the shareholders the **shareholder link** and the **shareholder access
key**, using different channels (for example, the link by email and the key
by phone or in person). The key is never emailed by the system.

## 3. Routine

- **New statements:** either upload them on the admin page, or put them in the
  shared Drive folder and wait up to 10 minutes. Each upload is either
  accepted, with a list of changes, or rejected with the reason, and nothing
  is imported from a rejected upload.
- **A Manafa export always needs the account statement downloaded the same
  day.**
- **Capital movements, the mandate rule or policy settings:** edit
  `config/capital_ledger.toml`, `config/mandate.toml` or
  `config/settings.toml` on the server. Then press **Recalculate from stored
  statements** on the admin page. Every report stores the config it was
  built with.
- **Update the SAIBOR benchmark** in `config/settings.toml` and set
  `benchmark_is_placeholder = false`.

## 4. Changing an access key

```bash
docker compose run --rm web rikz make-keys --role shareholder   # or --role admin
# put the two printed lines into .env, then:
docker compose up -d
```

A new key also gives a new link, and every existing session for that role
ends immediately.

## 5. Backups

`scripts/backup.sh` backs up the database, the statement files and the config
into `BACKUP_DIR`. Run it nightly from cron and copy the folder off the
server:

```
15 2 * * *  cd /srv/rikz && scripts/backup.sh >> /var/log/rikz-backup.log 2>&1
45 2 * * *  rclone sync /srv/rikz-backups remote:rikz-backups
```

**Restore**, after reinstalling as in section 2 and stopping `web` and `worker`:

```bash
docker compose exec -T db pg_restore -U rikz -d rikz --clean < db-<stamp>.dump
docker compose run --rm --no-deps -T --entrypoint tar worker -C /data -xz < files-<stamp>.tar.gz
tar -xzf config-<stamp>.tar.gz
docker compose run --rm web rikz verify-store
docker compose up -d
```

Practise a restore once, onto a spare machine, before relying on it.

## 6. Watching over it

- **Health:** `https://<domain>/healthz` returns `{"ok": true}`. Point an
  uptime monitor at it.
- **The admin page shows:**
  - recent uploads and their outcome;
  - Drive status (last check, waiting files, last error);
  - background jobs that are pending or failed (for example, email);
  - the view log.
- **The admin is emailed about:** new reports, rejected uploads, Drive read
  errors, emails that failed for good, and damaged stored files. The worker
  re-hashes the whole file store once a day.

## 7. What is stored, and for how long

| What | Kept |
|---|---|
| Accepted statement files (read-only, checked against their SHA-256) | Forever |
| Rejected uploads: names, hashes and reasons only, never the file content | Forever |
| Report versions (immutable snapshots) | Forever |
| View log (role, page, version, keyed hash of the address, browser; never the IP) | 400 days |
| Sign-in attempts | 30 days |

Web requests are logged with the link token replaced by `<token>`, and Caddy
keeps no access log.

## 8. Security summary

- **Two private links, each behind its own access key.** Keys are ~100 bits,
  stored only as scrypt hashes in `.env`. A wrong link gives a plain 404.
- **Guessing limits:** after 5 wrong keys, a device waits 15 minutes. After
  50 wrong keys in an hour, sign-in to that link pauses.
- **Sessions:**
  - signed cookies that are HttpOnly, Secure and SameSite=Strict;
  - scoped to their own link;
  - 7 days for shareholders and 8 hours for the admin;
  - ended by changing the key.
- **Admin forms** carry a CSRF token. Uploads are capped at 60 files, 25 MB
  per file and 100 MB per request.
- **Pages** run no scripts. They're served with a strict Content Security
  Policy, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` (so link
  tokens never leak to other sites), `Cache-Control: no-store`, HSTS and
  `noindex`.
- **Secrets** exist only in `.env` on the server, never in the repository. The
  test fixtures are redacted, and a test fails if personal data appears in
  any tracked file.
