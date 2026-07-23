This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

## Environment variables

Copy `.env.local.example` to `.env.local` and fill in both values before running the dev server:

| Variable | Required | Description |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | Yes | Base URL of the Karma Advisor API's `/api/v1` routes (e.g. `http://localhost:8000/api/v1` for local dev against `uvicorn api.main:app`). |
| `NEXT_PUBLIC_API_KEY` | Yes | One of the API server's `KARMA_API_KEYS`, sent as `X-API-Key` on every request. |

Both are `NEXT_PUBLIC_*`, so they are bundled into client-side JS and visible to
anyone who loads the app — see the auth model note below before treating this as
a secret.

**Auth model (v1): static shared key, internal-only.** Per
`karma ai/docs/frontend_contract_plan.md` section 6, v1 auth is a single shared
key shipped to the browser — it identifies no individual user and provides no
per-user isolation. This is acceptable only because v1's audience is
internal/controlled. **It is a hard launch blocker**: real per-user auth must
replace `NEXT_PUBLIC_API_KEY` before any public/storefront release.

You'll also need `KARMA_CORS_ORIGINS` set on the API server (e.g.
`http://localhost:3000` for local dev) — it defaults to empty, so the browser
will block every request until it's configured.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Fixture stub server

`npm run fixture-server` starts a dependency-free HTTP stub (`scripts/fixture-server.mjs`)
on `http://localhost:8001` that serves the committed contract fixtures from
`karma ai/api/contract/fixtures/**`, with the same CORS headers, HTTP status codes, and
`Retry-After` headers the real API would send. Use it to render error and terminal-build
states that are impractical or impossible to trigger against the live backend — e.g.
`cannot_proceed`, `failed` (both retryable and non-retryable), the 502/503/500 family, and
the floor-gated `BRIEF_FLOOR_NOT_MET` / `BRIEF_NOT_LOCKED` errors. Pick a scenario with a
query param, e.g. `http://localhost:8001/?scenario=cannot_proceed`; `GET /` with no
`scenario` lists every available name. It's a standing dev tool (not deleted after any one
phase), so it stays in sync automatically — fixtures are read fresh from disk per request.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
