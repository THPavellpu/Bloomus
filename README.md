# BloomUs

A mobile-first Django app for couples. The foundation includes a custom user
model and botanical landing page. It supports private couple connections,
a virtual garden dashboard, and romantic messages that water a partner's
flower.

## Windows PowerShell setup

From the project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and replace `DJANGO_SECRET_KEY` with a private random value. One
way to generate one is:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(50))"
```

Then initialize the local database and start the development server:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

Open <http://127.0.0.1:8000/>. If PowerShell blocks virtual-environment
activation, these commands call its Python directly and do not require
activation.

## Couple connections and garden

After signing in, visit `/garden/` to create an invitation or enter a
partner's invitation code. Codes are shown once to the inviter, stored only
as a hash, expire after seven days, and are single-use. New codes use readable,
case-insensitive groups (for example `BU-ABCD-EFGH-JKLM-NPQR`); spaces and
hyphens may be omitted when pasting them. Previously issued codes still work.
The invitation panel offers **Copy code**, **Copy link**, and **Share invitation**
(using the device share sheet where available, or copying the link).
Share only with your partner: anyone holding an unused code or link can join.

A link opens an invitation preview. After choosing to continue, a new partner
can sign up, or an existing user can sign in, and BloomUs automatically accepts
the invitation using the same single-use, expiry, and membership rules.
Opening a link alone never connects accounts. If it expires or is used before
authentication finishes, the account stays usable and a clear error is shown.
No extra code entry is needed. Normal sign-up without an invitation is unchanged.
Use the public HTTPS site when copying links for your partner; local development
links point at your local server and aren't publicly accessible.

Creating an invitation
reserves the first membership slot; accepting it creates the partner's
membership. Database constraints limit each couple to two members, each user
to one couple, and each user to one plant.

Plants are created as each couple membership is established. They start as
baby plants and grow from successfully received partner notes: sprout at 1,
bud at 3, and bloom at 7. Growth is derived from persisted watering messages,
not elapsed age or page visits. A plant needs water every three hours.
Within that window, an unread note plays a small positive animation on each
garden visit; once notes are read, the plant is calm. After three hours without
water it shows a gentle, reversible droop, even if an older note is unread.
An unwatered plant has a three-hour grace period after creation; a plant still
waiting for a partner stays calm. Earned growth is never lost and plants never
permanently die. Animations respect reduced-motion preferences.

A red dot on **Love notes** indicates unread partner notes and remains until
they are explicitly marked read. Care uses server-managed watering timestamps.
The app does not accept client-supplied
timestamps. Use **Write a message & water** on your partner's plant to send a
private note and water it in one transaction. Notes are limited to 500
characters, duplicate submissions are ignored, and each user may send up to
10 notes per hour. Received notes appear in **Love notes** and are marked read
only when the recipient explicitly chooses to do so.

## Configuration

Settings are loaded from environment variables, with `.env` read locally by
`python-dotenv`. The default database is SQLite at `db.sqlite3`. To use
PostgreSQL, set `DB_ENGINE=postgresql` and configure `DB_NAME`, `DB_USER`,
`DB_PASSWORD`, `DB_HOST`, and `DB_PORT`. The PostgreSQL driver is included in
`requirements.txt`.

On Render, set `SECRET_KEY` to the secret you already created, `DEBUG=False`,
`DATABASE_URL` to the PostgreSQL Internal Database URL, and `ALLOWED_HOSTS`
to the web service hostname. `CSRF_TRUSTED_ORIGINS` may be set to the matching
HTTPS origin; `RENDER_EXTERNAL_HOSTNAME` automatically adds the Render
hostname to allowed hosts and its HTTPS origin to trusted origins. The
existing `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, and `DJANGO_ALLOWED_HOSTS` names
remain supported as fallbacks for local setups.

WhiteNoise serves collected static files in production. Run
`python manage.py collectstatic --noinput` during the Render build. Do not
commit `.env` or real credentials.

For a Render native Python web service whose service root contains `manage.py`
and Render's `.venv` directory, use this build command. It installs and checks
Gunicorn with the same virtual environment used to start the service:

```text
.venv/bin/python -m pip install -r requirements.txt && .venv/bin/python manage.py collectstatic --noinput && test -x .venv/bin/gunicorn && .venv/bin/gunicorn --version
```

Use this start command:

```text
.venv/bin/gunicorn config.wsgi:application
```

These are Linux commands for Render, not Windows PowerShell commands. Gunicorn
is a Linux production server; continue using `manage.py runserver` on Windows.

### Render startup troubleshooting

If the build succeeds but startup reports `gunicorn: command not found` or
`python: command not found`, the startup shell cannot resolve that executable
on its `PATH`. A successful build does not prove that the runtime has the same
environment or that its virtual environment was retained.

In **BloomUs > Settings**, check that:

- The runtime is **Python** and the root directory contains `manage.py`.
- The deployed branch and commit contain the intended project files.
- The build and start commands match the commands above.
- No manually configured `PATH` or `VIRTUAL_ENV` replaces Render's environment.
- Any `PYTHON_VERSION` override selects a Python version supported by the
  installed Django release. Local Python is 3.14.5; this repository does not
  pin Render's Python version.
- If a Blueprint manages the service, inspect its configured YAML path and
  `runtime`, `rootDir`, `buildCommand`, and `startCommand`. This repository
  contains no `render.yaml`; dashboard or externally managed Blueprint
  settings cannot be verified from the local checkout.

If Render Shell is available, run these read-only diagnostics there:

```sh
pwd
printf 'PATH=%s\n' "$PATH"
command -v python python3 gunicorn
ls -l /opt/render/project/src/.venv/bin/python /opt/render/project/src/.venv/bin/gunicorn
/opt/render/project/src/.venv/bin/gunicorn --version
```

If the explicit Gunicorn version command succeeds, the virtual-environment
start command above bypasses a PATH-resolution problem. If the file is
missing or its interpreter cannot execute, investigate the runtime, service
root, and build-artifact retention instead; changing the start command alone
will not fix that case. Do not copy the Windows `.venv` directory to Render.

Run database migrations against the Render PostgreSQL database only after
confirming the `DATABASE_URL` is the intended database.

## Tests and checks

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test
```
