# Security: keeping the public gateway out of the private network

The gateway is public on purpose, and it runs on homelabai, inside the home network, next to
services that trust anyone who can reach them: Ollama (11434), ComfyUI (8188), llama.cpp, the
databases, LiteLLM, Timothy, microk8s, and on the LAN Jenkins and Harbor. The gateway's user can
use Docker, which makes running a command as that user the same as being root. So the rules are:

1. **The server fetches nothing a user named without checking every hop.**
2. **Code from a coding task never touches the host, and never reaches the LAN.**

## Fetching what someone outside named: `app/net.py`

Every URL that comes from a user, a search result, a web page or a model goes through `net.get`:

- `http` or `https` only, ports 80 and 443 only, no credentials in the URL;
- every address the name resolves to must be public (loopback, RFC 1918, link-local, CGNAT,
  multicast, IPv4-mapped, 6to4, Teredo and NAT64 forms are refused);
- the connection goes to the address that was checked, with the name kept for the Host header, TLS
  and the certificate check, so a DNS answer that changes in between (rebinding) gets nowhere;
- redirects are followed one at a time, each one checked again, at most three;
- bodies are read up to a limit.

Used by: web search page reading (`app/tools/web_search.py`), picture search
(`app/tools/media.py`), and image URLs in API requests (`app/api_images.py`).

**Image URLs in API requests** (`image_url` in the OpenAI API, `source.type=url` in the Messages
API) are fetched by the gateway through `net.get` and sent on inline. Passed on as they were, the
model server would have fetched them from inside the network with no check at all.

## Coding tasks: `app/coding/sandbox.py`, `app/coding/workspace.py`

- **No way back to the host through git.** The worktree's `.git` pointer is mounted read-only in
  the container, and before every git command on the host `workspace.git` checks that it still
  names the project's own git directory. A replaced pointer would otherwise let the container
  choose git's config, and with it a program git runs (`core.fsmonitor`, hooks) as the gateway's
  user. Every host git command also overrides `core.fsmonitor` and `core.hooksPath`.
- **No network but the filtered one.** A project that needs a network for its dependencies runs on
  `gateway-sandbox`, never Docker's default bridge. Set it up once:

  ```bash
  sudo ~/model-gateway/ops/sandbox-network/setup.sh install
  ```

  It drops the sandbox's traffic to every private address and to homelabai itself, re-applies the
  rules at every boot, and labels the network `gateway.egress=filtered`. Until that label is there,
  tasks that need a network fail and say why. `status` and `remove` do what they say.
- All capabilities are dropped, memory, CPU and processes are capped, `no-new-privileges` is set.

## Signing in through SSO: `app/routers/auth_sso.py`

- **Accounts are found by the auth server's own id** (`sso_subject`, "auth server host|sub"), never
  by a name or email: those are whatever the person typed at the auth server. Matched by name, a
  display name of `admin` signed in as the built-in admin. A name already taken here - a local
  account, a manager's pre-made one - gets a suffix on the new account instead.
- **Accounts from before** were found by name. One is linked to its id once, by the person it was
  made for: only an account an SSO sign-in created, with no password, on an email the auth server
  marks verified (`email_verified`).
- **A first sign-in is a request.** The account is created switched off and listed under
  *Sign-in requests* on the Users page; only an admin can approve or reject it. Approved, it has
  **public** access: the models in `gateway.public.models` (ticked on the Users page) and no
  image generation, until someone grants more.
- **Admin by email only when verified.** `sso.admin.emails` promotes an account only if the auth
  server says the email is verified. `sso.require.verified.email=false` trusts every email, for an
  auth server that never sends the claim; `sso.claim.email_verified` names the claim if it differs.

## In the browser

- **Model-written pages never run as the gateway.** The playground shows them in a sandboxed frame
  (no access to this origin, its cookies or its API), and *Open in a tab* opens a page holding
  only that same sandboxed frame. It used to open the page itself from a blob URL, which takes the
  gateway's origin: a page planted in an answer could make keys, or users if an admin opened it.
- **Sign-in redirects stay on this site** (`sso.safe_next`): `//evil.example`, `/\evil.example`
  and full URLs fall back to the dashboard.
- **Session cookies** are `Secure` over HTTPS, `HttpOnly`, `SameSite=Lax`. Signing out bumps the
  account's `session_epoch`, which every session cookie carries, so every copy of the cookie stops
  working - not just the one in that browser.
- **Headers on every response** (`app/main.py`): `frame-ancestors 'self'` and `X-Frame-Options`
  (no other site can frame these pages), `object-src 'none'`, `base-uri 'none'`, `nosniff`, a
  referrer policy, camera/microphone/location for this site only, and HSTS over HTTPS. A CSP that
  also limits scripts needs the pages' inline scripts moved out first.
- **API docs** (`/docs`, `/openapi.json`) are for signed-in admins only.
- **Pictures in answers** load only from an exact `/chat/files/<id>`, so model output cannot make
  the browser request any other page of the gateway while signed in.

## On the server (root, by hand)

Not something the gateway can do for itself:

- Bind Ollama and ComfyUI to `127.0.0.1` unless something on the LAN needs them.
- Take the gateway's user out of the `docker` group, or run Docker rootless.
- Check what LiteLLM (4000, 8080), Timothy (3300, 8300), 9090 and microk8s (16443, 10250, 25000)
  expose: they listen on every interface, and Docker-published ports bypass UFW.
