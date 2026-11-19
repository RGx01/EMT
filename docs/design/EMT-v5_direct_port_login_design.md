# A login on the direct port (design)

*Status: proposed, 19 Nov 2026. Not decided. A blocker for the first release (ROADMAP, Before the first
release). The user, 19 Nov 2026: "could we just use users HA's login?"*

## 1. The problem

EMT's web server is reached two ways. **Ingress** (Home Assistant's sidebar) goes through the
Supervisor, behind Home Assistant's own login. The **direct port** (8098 on the host, 8099 in the
container) has no login. Anyone who can reach it on the network sees every figure and can use every
tool, including Delete, Restore and a backup download (SECURITY.md).

The direct port is there for:

- **the dashboard cards**: a Home Assistant Webpage or iframe card showing `http://<host>:8098/lovelace/billing`
  or `/lovelace/heatmap`, loaded by the browser inside a dashboard;
- **standalone Docker users**, for whom it is the whole UI: there is no ingress without the Supervisor
  (README, Standalone Docker).

## 2. What Home Assistant offers

From Home Assistant's developer documentation (read 19 Nov 2026, contributors.home-assistant.io):

- **Ingress requests come only from the Supervisor, at `172.30.32.2`.** The ingress guide tells an app
  to "only accept connections from `172.30.32.2`". The Supervisor sets `X-Ingress-Path` and, for a
  signed-in user, `X-Remote-User-Id`, `X-Remote-User-Name` and `X-Remote-User-Display-Name`. It strips
  those headers from incoming requests, so they can be trusted only on a connection from that address.
- **A password check through the Supervisor.** With `auth_api: true` (EMT's config.yaml has had it since
  v4 1.1.0, unused), `POST http://supervisor/auth` with a username and password answers success or 401.
  The answer names no user and no admin role. Nothing documents whether it enforces multi-factor
  authentication. The Supervisor caches correct credentials for a while.
- **Home Assistant's own sign-in (OAuth 2 / IndieAuth).** The browser is sent to Home Assistant's
  `/auth/authorize` with a `client_id` (a URL: the app's own) and a `redirect_uri` with the same host and
  port. Home Assistant runs its own login page, multi-factor included. It redirects back with a code,
  which the app exchanges at `/auth/token` for an access token. `403 access_denied` means the user may not
  log in (inactive, for example).

## 3. The proposal

### 3.1 Tell ingress from the direct port by address, never by header

A request is **ingress** when, on an add-on install, it arrives from `172.30.32.2`. Every other request is
**direct**. EMT reads `X-Ingress-Path` from any client today (`IngressMiddleware`, web/server.py). That is
harmless while nothing hangs on it, but a login that trusted the header would be open to anyone who sent
it. So the middleware honours the ingress headers only from that address. Ingress itself is unchanged:
Home Assistant has already signed the user in.

### 3.2 Sign in with Home Assistant's own page (recommended)

On the direct port, any page or API without a valid session goes to EMT's `/signin`. That page sends the
browser to Home Assistant's `/auth/authorize`:
- `client_id` is EMT's own origin, as the browser reached it, e.g. `http://192.168.1.10:8098/`;
- `redirect_uri` is `…/signin/done` on the same origin;
- there is a `state`, and a PKCE challenge (S256).

Home Assistant shows its login, with the user's two-factor step, and redirects back. EMT exchanges the
code server-side and asks Home Assistant who the user is. It admits **administrators only**: EMT's tools
delete and restore, and the add-on's sidebar panel is administrators' by default (`panel_admin`). Then it
**revokes Home Assistant's tokens** (`/auth/revoke`), because EMT needs to know who you are, not to act as
you.

EMT then sets its own session cookie:
- signed, `HttpOnly`, `SameSite=Lax`;
- kept for 30 days;
- the signing secret is kept in `/data`. Today `app.secret_key` is new at every start, which would sign
  everyone out on each restart or update.

**Signing out** clears the cookie. **Every write** (any method but GET and HEAD) on the direct port also
needs an `Origin` that matches the page's own. A `Lax` cookie still travels on a POST from another port of
the same host (Home Assistant itself, another add-on), and the `Origin` check closes that.

**Which Home Assistant address the browser is sent to:**
- **Add-on:** Home Assistant's configured internal URL, from the Supervisor's core API; failing that,
  the host the browser used, on port 8123.
- **Standalone:** a new `HA_PUBLIC_URL`, defaulting to `HA_URL`. `HA_URL` is how the container reaches
  Home Assistant, which a browser may not.

The token exchange goes from the container to Home Assistant: `homeassistant:8123` on an add-on, `HA_URL`
standalone.

### 3.3 The dashboard cards: a read-only card key

A card is an iframe inside a Home Assistant dashboard, so the session cookie is not a reliable way in:
- a dashboard opened as `homeassistant.local` showing a card at the host's IP address is **cross-site**,
  and a `Lax` cookie is not sent to it;
- Safari blocks third-party cookies in frames outright;
- a dashboard on `https` (Nabu Casa, a reverse proxy) cannot frame an `http` card at all, today as after.

So the card pages take a **card key** in their URL: `/lovelace/heatmap?key=…`. Settings → Dashboard cards
shows each card's URL with the key, to paste into the card, and **New key** replaces it, which revokes
every card using the old one. A key is a random 32-byte token, kept hashed.

A key admits **GET requests only, to an allow-list**: the two card pages, their static files, and the
read APIs they call. Today that is `/api/energy`, `/api/periods`, `/api/billing-source`,
`/api/data-health` and `/api/blocks/settlement`; the build fixes the list by loading each card and recording
what it asks for. It never admits a write. The billing and heatmap modules carry tools (delete, gap fill,
corrections); on a card they must not appear. A tool's request without a session would be refused anyway,
but the card should not offer what it cannot do.

### 3.4 What changes for users

- **Ingress:** nothing.
- **The direct port in a browser:** EMT asks you to sign in with Home Assistant, once per browser per 30
  days.
- **Existing dashboard cards stop working until their URL has the key.** The update's changelog says so,
  and Settings shows the new URLs. This is the cost of closing the hole: an unkeyed card is the hole.
- **Standalone:** the same sign-in. A setup that cannot reach Home Assistant's sign-in from the browser
  can set `EMT_DIRECT_LOGIN=off`. The README says plainly that this puts it back as it is today, so a
  misconfigured URL cannot lock a standalone user out of their own data.

## 4. Alternatives

- **A password form checked through the Supervisor's `/auth`.** Simpler: no redirect, no URL to find.
  Against it:
  - the answer names no user and no role, so EMT cannot admit administrators only;
  - nothing documents whether it enforces two-factor;
  - the password is typed into EMT's page, so EMT must rate-limit guesses itself;
  - it does not exist standalone.

  Kept as the fallback if Home Assistant refuses a plain-`http` `client_id` on a LAN address (§6).
- **EMT's own password.** Another secret to set, forget and recover, on a port the user has to remember
  is there. No.
- **Turn the direct port off by default.** That breaks every card, and leaves standalone with no UI.
  Already the advice for anyone who does not use the cards (SECURITY.md), and still worth keeping there.
- **Trust the local network, or an IP allow-list.** A home network is not a trust boundary: guests,
  devices, anything compromised on it. No.
- **Cards through the session cookie.** It fails cross-site and in Safari (§3.3).

## 5. Tests the build carries

Each fails on the tree before it:
- a request with `X-Ingress-Path` from any address but `172.30.32.2` is treated as direct;
- every route on the direct port without a session redirects (a page) or answers 401 (an API), apart
  from `/signin`, `/signin/done`, the card pages and APIs with a valid key, and static files;
- a card key admits GET on its allow-list and nothing else: no POST, no other GET, no other page;
- a write with a foreign `Origin` is refused, even with a session;
- the sign-in callback refuses a bad `state`, a non-administrator, and a `403` from Home Assistant, and
  revokes Home Assistant's tokens on success;
- the session secret survives a restart.

Every existing route test runs as ingress or with a session, so the suite says nothing about the login
by accident.

## 6. To check on a real install before building

These decide between §3.2 and the fallback, so they come first:

1. The address a direct-port request arrives from, on the Pi: the client's own, or Docker's gateway
   (`172.30.32.1`)? It must not be `172.30.32.2`. Logged once, for both paths.
2. Home Assistant accepts `http://<LAN address>:8098/` as a `client_id`, and its redirect back.
3. Two-factor is asked on that sign-in, for a user who has it on.
4. How EMT learns the user is an administrator from the access token (the WebSocket `auth/current_user`
   is the likely answer; the documentation read does not cover it).
5. The add-on container reaches `http://homeassistant:8123/auth/token`.
6. A card with its key still loads inside a dashboard opened by hostname, and in Safari.

## 7. Build order

1. **The ingress check (§3.1), and the six questions answered.** Small, and it decides the rest.
2. **The session**: the secret kept in `/data`, `/signin` and its callback, the guard on every direct
   route, the `Origin` check, signing out.
3. **Card keys**: Settings → Dashboard cards, and the read-only allow-list.
4. **Documents**: SECURITY.md, the README, BETA.md, the changelog, `docs/reference/` on how EMT is
   reached, and the roadmap item closed.
