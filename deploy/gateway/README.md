# The gateway

One entry point in front of every tenant's API (PLAN.md D15, M8), part of the
reference deployment. An app may use its own reverse proxy instead; see
[docs/configuration.md](../../docs/configuration.md).

```
https://<host>/<tenant>/...  ──►  Traefik  ──►  api-<tenant>:8080 (flexorch_<tenant>_edge)
```

- **TLS** at the gateway, with the app's certificate: TLS 1.2 or later, HSTS.
  Plain HTTP only redirects. It never makes a certificate.
- **Routing** by path: `/<tenant>/` goes to that tenant's API with the prefix
  stripped. The API's server puts it back (`FLEXORCH_ROOT_PATH`), so
  `/<tenant>/docs` and the schema's `servers` are right. Any other path is 404.
- **No authentication here.** The API checks every token itself (D15); the
  gateway only carries the request.
- **Isolation.** The gateway is on its own network (where its ports are
  published) and on each tenant's edge network, which only that tenant's API
  is also on. It can't reach any Acquirium or the database, and it has no
  Docker socket: its routes are files, which it watches.

## Start

After `deploy/tenant/init_cluster.sh`, one of:

```bash
./init_gateway.sh --cert flex.crt --key flex.key   # HTTPS with the app's certificate
./init_gateway.sh --https                          # HTTPS; the app put tls.crt and tls.key in place
./init_gateway.sh --plain-http                     # HTTP on 127.0.0.1 only
```

- **HTTPS**: the certificate (with its chain) and key are the app's, from a
  public CA or its own. `--cert`/`--key` install them as
  `$FLEXORCH_SECRETS/gateway/tls.crt` and `tls.key`; or the app writes those
  files itself (readable by uid 1000) and runs `--https`. After a renewal, run
  it again: the gateway restarts with the new files.
- **Plain HTTP**: for a gateway only this machine uses (a demo, a local app),
  or one behind the app's own TLS proxy. It's published on 127.0.0.1 unless
  `FLEXORCH_GATEWAY_BIND` says otherwise; tokens cross it in the clear, so
  don't publish it on a network without TLS in front.

The choice is recorded (`$FLEXORCH_SECRETS/settings/gateway.env`), so
`./init_gateway.sh` with no option, after a host restart, starts it the same
way. With no certificate and no earlier choice, it refuses.

| Setting | Default | |
| --- | --- | --- |
| `FLEXORCH_GATEWAY_HTTPS_PORT` | `443` | The published HTTPS port. |
| `FLEXORCH_GATEWAY_HTTP_PORT` | `80` | The published HTTP port: it redirects with HTTPS, and serves the routes with `--plain-http`. |
| `FLEXORCH_GATEWAY_BIND` | `0.0.0.0`; `127.0.0.1` with `--plain-http` | The address the ports are published on. |
| `FLEXORCH_GATEWAY_HOST` | `localhost` | The name clients use, for the log. |
| `GATEWAY_IMAGE` | `traefik:v3.4.1` | |

`provision_tenant.sh` writes each tenant's route
(`$FLEXORCH_SECRETS/settings/gateway/tenant_<t>.yml`) and joins the gateway
to its edge network; `deprovision_tenant.sh` removes both.

Clients use the gateway's URL: `Orchestrator(url="https://<host>/<tenant>",
token=...)`. For a certificate from the app's own CA, the client trusts it with
`cafile=`.

## Files

| File | |
| --- | --- |
| `compose.gateway.yaml` | Traefik, as uid 1000 on a read-only root, its static configuration as flags |
| `common.yml` | The security headers; copied next to the routes |
| `init_gateway.sh` | The certificate (installed, never made), `tls.yml` (TLS 1.2 or later, HSTS, the redirect), start, every tenant's route and edge network |
