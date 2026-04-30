# Agent Zero RFC Integration

This document describes the recommended way to connect Influx to Agent Zero when
Agent Zero UI authentication is enabled.

## Why RFC

Normal Agent Zero webhook-like endpoints such as `/api/message_async` and
`/api/notification_create` sit behind session authentication. In practice that
means a service client is redirected to `/login` unless it carries a valid
session cookie, and likely a CSRF token as well.

Agent Zero also exposes `POST /api/rfc`. That route explicitly disables the
normal auth and CSRF checks and instead authenticates the request body using a
shared secret called the **RFC password**.

Here, `RFC` means **Remote Function Call**. It does not refer to IETF Requests
for Comments. The RFC password is simply Agent Zero's shared secret for
authorizing remote function calls.

Relevant Agent Zero source files:

- `api/message.py`
- `api/message_async.py`
- `api/rfc.py`
- `helpers/rfc.py`
- `initialize.py`
- `agent.py`

## High-Level Design

Influx sends `POST /api/rfc` to Agent Zero instead of `POST /api/message_async`.
The request asks Agent Zero to execute a small helper function that mirrors the
core logic of `message_async`:

1. resolve or create a fixed context
2. run the `user_message_ui` extension hook
3. log the message into the UI/history
4. enqueue the message into the context

The helper module is stored in this repo at:

- [integrations/agent_zero/usr/influx_rfc.py](/home/dns/projects/lithos/code/influx/integrations/agent_zero/usr/influx_rfc.py)

It is intended to be deployed into the Agent Zero container as:

- `/a0/usr/influx_rfc.py`

The RFC module path used by Influx is therefore:

- `usr.influx_rfc`

And the callable function name is:

- `enqueue_message`

## Deployment

Copy or mount the helper file into the Agent Zero container:

```text
/a0/usr/influx_rfc.py
```

Then configure Agent Zero with an RFC password. Agent Zero stores this as
`RFC_PASSWORD`.

The same secret must also be present for Influx in an environment variable of
your choosing, for example:

```bash
AGENT_ZERO_RFC_PASSWORD=...
```

## Influx Configuration

Example webhook configuration:

```toml
[[notifications.webhooks]]
name = "agent-zero-inbox-rfc"
type = "agent_zero_rfc_message"
url = "http://agent-zero:50001/api/rfc"
enabled = true
notify_on = ["manual", "scheduled"]
event_mode = "article"
min_score = 8
context = "InfluxIn"
rfc_module = "usr.influx_rfc"
rfc_function = "enqueue_message"
rfc_password_env = "AGENT_ZERO_RFC_PASSWORD"
```

## Request Shape

Influx sends JSON shaped like:

```json
{
  "rfc_input": "{\"module\": \"usr.influx_rfc\", \"function_name\": \"enqueue_message\", \"args\": [], \"kwargs\": {\"text\": \"...\", \"context\": \"InfluxIn\"}}",
  "hash": "<sha256 hex>"
}
```

The hash is computed from the serialized `rfc_input` plus the shared RFC
password, matching the current Agent Zero RFC mechanism.

## Toast Notifications

Influx's existing `agent_zero_notification_create` adapter is still a direct
HTTP call to Agent Zero's normal API and therefore remains subject to session
authentication. When UI auth is enabled, redirects such as HTTP `302 /login`
must be treated as delivery failures.

Recommendation:

- use RFC for the inbox/message path
- disable Agent Zero toast webhooks for authenticated deployments unless you
  later add a second RFC helper specifically for notifications

That keeps the integration simple and avoids depending on browser-session state.
