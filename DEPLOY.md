# Deploying Samruddhi on Vercel

Vercel's serverless environment doesn't keep files between requests, so the app uses **Turso** (SQLite-compatible, hosted) when running on Vercel. Locally it still uses `database.db` (SQLite).

## 1. Create a Turso database

1. Install the Turso CLI: https://docs.turso.tech/cli/installation  
   (e.g. on macOS: `brew install tursodatabase/tap/turso`)

2. Log in:
   ```bash
   turso auth login
   ```

3. Create a database:
   ```bash
   turso db create samruddhi --region nrt
   ```
   (Use any region near you: `nrt` = Tokyo, `iad` = Virginia, etc.)

4. Get the database URL and create an auth token:
   ```bash
   turso db show samruddhi --url
   turso db tokens create samruddhi
   ```
   Save both values for the next step.

## 2. Set environment variables on Vercel

1. Open your project on [Vercel](https://vercel.com) → **Settings** → **Environment Variables**.

2. Add:

   | Name                 | Value                    |
   |----------------------|--------------------------|
   | `TURSO_DATABASE_URL` | The URL from step 1.4    |
   | `TURSO_AUTH_TOKEN`   | The token from step 1.4  |

3. Apply to **Production**, **Preview**, and **Development** if you use them.

## 3. Deploy

**From Git (recommended)**

1. Push your code to GitHub/GitLab/Bitbucket.
2. In Vercel: **Add New** → **Project** → import the repo.
3. Vercel will detect Flask (or use framework "flask").
4. Add the env vars above if you didn't already.
5. Deploy.

**From local with Vercel CLI**

```bash
npm i -g vercel
vercel login
vercel
```

Add the same env vars in the project's Vercel dashboard before or after the first deploy.

## 4. Static files (CSS)

The app expects CSS at `/static/style.css`. The repo includes `public/static/style.css`. In Vercel → Project **Settings** → **General**, set **Public Directory** to `public` if it isn't already. Then `/static/style.css` is served from `public/static/style.css`.

## 5. Local development

- **Without Turso:** run as usual; the app uses `database.db` (SQLite).
- **With Turso:** set `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in your environment (e.g. `.env` and `source` it, or export in the shell), then run the app. It will use Turso and sync with the same DB as production.

## Summary

| Where    | Database              | Config |
|----------|------------------------|--------|
| Local    | SQLite (`database.db`) | None   |
| Vercel   | Turso                  | `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` |

The app uses `db.py` to switch between SQLite and Turso based on those env vars.
