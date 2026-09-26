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

## 2b. Running on Supabase (the chosen setup)

Supabase hosts the database and the statement files. Your server then only
runs the website and the worker, and keeps no data of its own.

The Supabase project already exists: **whitehomes-rikz-portfolio** (ref
`krmmgethjddzpaucowrg`, Frankfurt, eu-central-1). Its schema, tables and
private `statements` bucket were created from
`supabase/migrations/20260926000000_rikz_schema.sql`, the same objects the app
creates on start-up.

1. **Set the database password.** In the project, open *Project Settings →
   Database → Reset database password* and choose a strong one.
2. **Fill in `.env` (Option B in `.env.example`):**
   - `DATABASE_URL`: under *Connect → Session pooler* (port 5432). The
     transaction pooler (port 6543) also works.
   - `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`: under *Project Settings →
     API*. The service-role key bypasses Supabase's access rules, so keep it
     only in `.env` on the server.
3. **Run the setup:**

   ```bash
   docker compose -f docker-compose.supabase.yml build
   docker compose -f docker-compose.supabase.yml run --rm web rikz make-keys
   docker compose -f docker-compose.supabase.yml run --rm web rikz check-config --smtp --drive
   docker compose -f docker-compose.supabase.yml up -d
   ```

How the data is protected on Supabase:

- **Tables** go in their own `rikz` schema. Supabase's automatic web API
  publishes only the `public` schema, and `check-config` fails if the tables
  would land there.
- **Row-level security** is switched on for every table, with no policies. The
  anonymous and signed-in API roles can read nothing, and only the app's own
  database user sees the data.
- **Statement files** are in a private bucket (`statements`). The app refuses to
  start if that bucket is public. Files are still content-addressed, written
  once, and checked against their SHA-256 on every read.
- **Supabase Auth** isn't used. Access stays the two private links with their
  access keys.

**Backups:** use `scripts/backup-supabase.sh`. It dumps the `rikz` schema,
exports every statement file through `rikz export-store`, and saves the
config. Restore with `pg_restore` into the schema, then
`rikz import-store <folder>`.

**Moving an existing install to Supabase:**

1. Run `rikz export-store /tmp/files` on the old server.
2. `pg_dump` the old database and `pg_restore` it into the `rikz` schema.
3. Switch `.env` to Option B.
4. Run `rikz import-store /tmp/files`, then `rikz verify-store`.

## 2c. Hosting on Vercel (no server to manage)

The website runs as a Vercel function (`index.py`, `vercel.json`) in
Frankfurt (`fra1`), next to the Supabase database. Statement files are kept in
the database (`FILE_STORE=database`), so no Supabase service key is needed.

**Settings** go in Vercel → project → **Settings → Environment Variables**,
marked *Sensitive*, for Production:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Supabase → **Connect** → *Transaction pooler* string (port 6543), with your database password |

Instead of `DATABASE_URL` you can connect the Supabase database to the project under Vercel → **Storage** → the database → **Connect Project**; Vercel then sets `POSTGRES_URL` itself, and the app uses it when `DATABASE_URL` is unset (or holds the project's `https://` address by mistake).
| `FILE_STORE` | `database` |
| `RIKZ_SECRET_KEY` | a random string of at least 48 characters |
| `RIKZ_VIEW_TOKEN` | a random string of at least 24 characters (letters and digits); it becomes part of the shareholder link |
| `RIKZ_ADMIN_TOKEN` | a different random string of at least 24 characters; part of the admin link |
| `RIKZ_VIEW_KEY` | the shareholder access key (at least 16 characters) |
| `RIKZ_ADMIN_KEY` | your admin access key (at least 16 characters, different from the shareholder key) |
| `CRON_SECRET` | a random string of at least 32 characters (protects the daily job) |

A password manager's generator is the easiest way to make the random strings.
Access keys may also be given as hashes (`RIKZ_VIEW_KEY_HASH`,
`RIKZ_ADMIN_KEY_HASH` from `rikz make-keys`). A plain key is hashed in memory at
start-up and never written anywhere. Add email and Drive settings the same
way when you want them (section 2 lists them).

After saving the settings, open **Deployments** and choose **Redeploy** on the
latest deployment. Until the settings are complete, the site shows a page
naming what's missing. `https://<site>/healthz?check=db` answers
`{"ok": true, "db": true}` once the database connection works.

**Links:**
- Shareholders: `https://<site>.vercel.app/v/<RIKZ_VIEW_TOKEN>/`
- Admin: `https://<site>.vercel.app/a/<RIKZ_ADMIN_TOKEN>/`

**How Vercel differs from a server:**
- **Daily job:** Vercel Cron runs `/cron/worker` once a day at 05:00 UTC.
  It checks Drive, sends queued emails and does maintenance. Uploads through
  the admin page still create the report immediately.
- **PDF:** WeasyPrint's system libraries aren't available on Vercel, so the
  PDF link opens the print layout; your browser's Print → Save as PDF gives the
  file.
- **Upload size:** one upload can be at most 4.5 MB (Vercel's limit). A full
  set of statements is well under 1 MB.
- **Settings changes:** the capital ledger and settings files are part of the
  code. Edit them in GitHub and Vercel redeploys automatically. Then press
  **Recalculate** on the admin page.

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

`scripts/backup.sh` (bundled database) or `scripts/backup-supabase.sh` (Supabase) backs up the database, the statement files and the config
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
