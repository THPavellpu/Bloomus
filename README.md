# Bloom Together

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
as a hash, expire after seven days, and are single-use. Creating an invitation
reserves the first membership slot; accepting it creates the partner's
membership. Database constraints limit each couple to two members, each user
to one couple, and each user to one plant.

Plants are created as each couple membership is established. The dashboard
derives growth stages from the plant's creation time and care status from
server-managed watering timestamps. It does not accept client-supplied
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

Set `DJANGO_DEBUG=False`, provide a secure `DJANGO_SECRET_KEY`, and set
`DJANGO_ALLOWED_HOSTS` to the deployment hostnames for production. Do not
commit `.env` or real credentials.

## Tests and checks

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test
```
