# A login on the direct port (design)

> **History — a design record.** What was decided, when and why, as it stood then; parts describe code
> that has since changed. How it works today: [api.md, Who may ask](../../reference/api.md#who-may-ask),
> SECURITY.md, and `web/signin.py`. PR numbers (`#NNN`) are in this repository, `RGx01/EMT`.

*Status: built, 20 Nov 2026: #166 the design, #167 ingress told by address, #168 the checks on a real
install (§6), #169 the sign-in, #170 the card keys, and the documents after. A blocker for the first
release, closed but for one run on the soak Pi (ROADMAP). The user, 19 Nov 2026: "could we just use users
HA's login?"*

*Decided with the user on 19 Nov 2026:*
- *administrators only, on the direct port;*
- *existing dashboard cards stop working until their URL carries a key (§3.4);*
- *the build order of §7.*

*Asked: "what about docker users?" — §3.5.*

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
**direct**. "An add-on install" is the mode EMT runs in (`EMT_MODE`), never the address: standalone, every
request is direct, whatever its Docker network's addresses happen to be (§3.5). EMT reads `X-Ingress-Path` from any client today (`IngressMiddleware`, web/server.py). That is
harmless while nothing hangs on it, but a login that trusted the header would be open to anyone who sent
it. So the middleware honours the ingress headers only from that address. Ingress itself is unchanged:
Home Assistant has already signed the user in.

### 3.2 Sign in with Home Assistant's own page (recommended)

On the direct port, any page or API without a valid session goes to EMT's `/signin`. That page sends the
browser to Home Assistant's `/auth/authorize`:
- `client_id` is EMT's own origin, as the browser reached it, e.g. `http://192.168.1.10:8098/`;
- `redirect_uri` is `…/signin/done` on the same origin;
- there is a `state`, and a PKCE challenge (S256).

Home Assistant shows its login, with the user's two-factor step, and redirects back (§6: a plain-`http`
LAN origin is accepted, and a user with an authenticator app is asked for its code). EMT exchanges the
code server-side and asks Home Assistant who the user is: the WebSocket's `auth/current_user`, with the new
access token, answers `id`, `name`, `is_admin`, `is_owner`, `credentials` and `mfa_modules` (§6). It admits **administrators only**: EMT's tools
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
  the host the browser used, on port 8123. (On the Pi only the fallback was offered, and it worked; why
  the internal URL was not, unset or unread, was not established.)
- **Standalone:** see §3.5.

The token exchange goes from the container to Home Assistant: `http://homeassistant:8123` on an add-on
(§6), `HA_URL` standalone.

**A sign-in that cannot stick.** In a frame on a different address from EMT (§3.3), the sign-in itself
completes (§6), but the cookie it sets is a third-party one and is not kept, so the next page would ask
again, forever. So `/signin/done` sets the cookie and sends the browser to a check: if the cookie came back,
on to the page; if it did not, a page that says why (Home Assistant and EMT opened on different addresses)
and offers **Open EMT in a new tab**, instead of another sign-in.

### 3.3 The dashboard cards: a read-only card key

A card is an iframe inside a Home Assistant dashboard, so the session cookie is not a reliable way in:
- a dashboard opened as `homeassistant.local` showing a card at the host's IP address is **cross-site**,
  and the cookie is not sent to it (§6: Safari, a fresh cookie, not sent);
- a dashboard on `https` (Nabu Casa, a reverse proxy) cannot frame an `http` card at all, today as after.

Opened on the same address as the card, the cookie was sent (§6). But a card must not depend on how
someone happens to open their dashboard.

So the card pages take a **card key** in their URL: `/lovelace/heatmap?key=…`. Settings → Dashboard cards
shows each card's URL with the key, to paste into the card, and **New key** replaces it, which revokes
every card using the old one. A key is a random 32-byte token, kept in its own file beside the session
secret. *Built (20 Nov 2026) kept as it is, not hashed as first written: Settings shows the cards'
addresses whenever it is opened, and the secret beside it is no less sensitive.*

A key admits **GET requests only, to an allow-list**: the two card pages, their static files, and the
read APIs they call. *Built: loading each card on the demo household (20 Nov 2026) recorded
`/api/energy` and `/api/periods` only; the others first listed belong to the modules' tools, which a card
does not show (the billing card is built without corrections, the heatmap card in chart mode).* It never admits a write. The billing and heatmap modules carry tools (delete, gap fill,
corrections); on a card they must not appear. A tool's request without a session would be refused anyway,
but the card should not offer what it cannot do.

### 3.4 What changes for users

- **Ingress:** nothing.
- **The direct port in a browser:** EMT asks you to sign in with Home Assistant, once per browser per 30
  days.
- **Existing dashboard cards stop working until their URL has the key.** The update's changelog says so,
  and Settings shows the new URLs. This is the cost of closing the hole: an unkeyed card is the hole.
- **Standalone:** the same sign-in, §3.5.

### 3.5 Standalone Docker

Standalone has no Supervisor, so no ingress: the direct port is the whole UI, and everything in it signs in
as in §3.2. Three things differ.

- **Nothing is ingress.** Only an add-on install treats `172.30.32.2` as the Supervisor (§3.1). A user's
  Docker network can use any range, `172.30.32.x` included.
- **The Home Assistant address the browser is sent to.** `HA_URL` is how the *container* reaches Home
  Assistant, and is often a name only other containers resolve (`http://homeassistant:8123`, as the README's
  example has it). So the browser's address is:
  - `HA_PUBLIC_URL` when set;
  - otherwise, the host the browser used to reach EMT, on `HA_URL`'s port (EMT and Home Assistant on one
    machine, the usual case).

  If that address does not answer, EMT's sign-in page says which address it tried and to set
  `HA_PUBLIC_URL`, instead of sending the browser to a dead link. The token exchange stays server-side on
  `HA_URL`. The long-lived token in `HA_TOKEN` plays no part in signing in: it is EMT's, not the user's.
- **EMT in Home Assistant's sidebar.** The README has standalone users add EMT as a Webpage dashboard,
  which frames the whole UI. The session cookie reaches the frame when the dashboard and EMT share a host
  name: ports do not count, so `http://192.168.1.10:8123` framing `http://192.168.1.10:8098` works, and
  `homeassistant.local` framing the IP address does not (§6). Home Assistant's own sign-in runs inside a
  frame (§6), so a framed EMT signs in where it is. On a different address the cookie never reaches the
  frame, whoever signs in, and the sign-in's check (§3.2) says so and offers a new tab. **The README makes
  the same address a requirement**: the Webpage dashboard's URL uses the same host name, or IP address, that
  Home Assistant is opened at.

**The way out.** A setup that cannot reach Home Assistant's sign-in from the browser can set
`EMT_DIRECT_LOGIN=off`. The README says plainly that this puts it back as it is today, so a misconfigured
URL cannot lock a standalone user out of their own data. On an add-on there is no switch: ingress is always
there.

**On updating**, a standalone user meets the sign-in after `docker compose up -d --build`. The changelog
says so, with `HA_PUBLIC_URL` and the switch.

## 4. Alternatives

- **A password form checked through the Supervisor's `/auth`.** Simpler: no redirect, no URL to find.
  Against it:
  - the answer names no user and no role, so EMT cannot admit administrators only;
  - nothing documents whether it enforces two-factor;
  - the password is typed into EMT's page, so EMT must rate-limit guesses itself;
  - it does not exist standalone.

  It was the fallback if Home Assistant refused a plain-`http` `client_id` on a LAN address; it accepts
  one (§6), so it is not needed.
- **EMT's own password.** Another secret to set, forget and recover, on a port the user has to remember
  is there. No.
- **Turn the direct port off by default.** That breaks every card, and leaves standalone with no UI.
  Already the advice for anyone who does not use the cards (SECURITY.md), and still worth keeping there.
- **Trust the local network, or an IP allow-list.** A home network is not a trust boundary: guests,
  devices, anything compromised on it. No.
- **Cards through the session cookie.** It fails cross-site and in Safari (§3.3).

## 5. Tests the build carries

Each fails on the tree before it:
- a request with `X-Ingress-Path` from any address but `172.30.32.2` is treated as direct, and standalone
  one from `172.30.32.2` is too;
- standalone, the browser is sent to `HA_PUBLIC_URL` when set, else the request's host on `HA_URL`'s port;
  `EMT_DIRECT_LOGIN=off` admits every request as today;
- every route on the direct port without a session redirects (a page) or answers 401 (an API), apart
  from `/signin`, `/signin/done`, the card pages and APIs with a valid key, and static files;
- a card key admits GET on its allow-list and nothing else: no POST, no other GET, no other page;
- a write with a foreign `Origin` is refused, even with a session;
- a sign-in whose cookie does not come back ends on the page that says why, not on another sign-in;
- the sign-in callback refuses a bad `state`, a non-administrator, and a `403` from Home Assistant, and
  revokes Home Assistant's tokens on success;
- the session secret survives a restart.

Every existing route test runs as ingress or with a session, so the suite says nothing about the login
by accident.

## 6. Checked on a real install

On the owner's Pi (Home Assistant OS, the soak add-on), 19–20 Nov 2026, with a probe on a branch that was
never merged (`probe/login-checks`, deleted after): pages on the direct port that ran the sign-in and
reported what came back, revoking Home Assistant's tokens at once and logging no name or token.

| | Check | Found |
|---|---|---|
| 1 | Where requests come from | Ingress from the Supervisor (`server: first ingress request from the Supervisor`); the direct port from the browser's own address, not Docker's gateway. #167 is safe: the sidebar keeps its links. |
| 2 | `http://<LAN address>:8098/` as a `client_id` | Accepted, and the redirect back to it. |
| 3 | Two-factor | A user with an authenticator app (`totp`) was asked for its code. |
| 4 | Who signed in, and an administrator? | `auth/current_user` with the new token: `id`, `name`, `is_admin`, `is_owner`, `credentials` (`homeassistant` provider), `mfa_modules`. |
| 5 | The container to `/auth/token` | `http://homeassistant:8123` answered. Revoking the refresh token answered 200. |
| 6 | The cookie in a dashboard frame | Same address as the card: sent. Home Assistant opened by host name, card on the IP address: **not sent** (Safari, a fresh cookie). |
| 7 | Home Assistant's sign-in inside a frame | Runs, and completes. |

Not established: the browser of check 6's same-address case, and why the probe offered only "this host,
port 8123" and not Home Assistant's internal URL.

## 7. Build order

1. **The ingress check (§3.1), and §6's questions answered.** Done: #167, and §6.
2. **The session**: the secret kept in `/data`, `/signin` and its callback, the guard on every direct
   route, the `Origin` check, signing out.
3. **Card keys**: Settings → Dashboard cards, and the read-only allow-list.
4. **Documents**: SECURITY.md, the README, BETA.md, the changelog, `docs/reference/` on how EMT is
   reached, and the roadmap item closed.
