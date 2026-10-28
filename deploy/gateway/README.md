# The gateway

One HTTPS entry point in front of every tenant's API (PLAN.md D15, M8):

```
https://<host>/<tenant>/...  ──►  Traefik  ──►  api-<tenant>:8080 (flexorch_<tenant>_edge)
```

- **TLS** at the gateway: TLS 1.2 or later, HSTS. Plain HTTP only redirects.
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

After `deploy/tenant/init_cluster.sh`:

```bash
FLEXORCH_GATEWAY_HOST=flex.example.org ./init_gateway.sh
```

| Setting | Default | |
| --- | --- | --- |
| `FLEXORCH_GATEWAY_HOST` | `localhost` | The name in the generated certificate. |
| `FLEXORCH_GATEWAY_HTTPS_PORT` | `443` | The published HTTPS port. |
| `FLEXORCH_GATEWAY_HTTP_PORT` | `80` | The published HTTP port (redirects only). |
| `GATEWAY_IMAGE` | `traefik:v3.4.1` | |

`provision_tenant.sh` writes each tenant's route
(`$FLEXORCH_SECRETS/settings/gateway/tenant_<t>.yml`) and joins the gateway
to its edge network; `deprovision_tenant.sh` removes both. Run
`init_gateway.sh` again after a host restart or a new certificate.

## Certificates

- **Your own**: put `tls.crt` (with its chain) and `tls.key` in
  `$FLEXORCH_SECRETS/gateway/` before `init_gateway.sh`, readable by uid 1000.
- **Generated** (air-gapped sites, tests): with no certificate there,
  `init_gateway.sh` makes a CA and a certificate for `FLEXORCH_GATEWAY_HOST`.
  Clients trust `$FLEXORCH_SECRETS/gateway/ca.crt`:
  `Orchestrator(url=..., token=...)` with `SSL_CERT_FILE` pointing at it, or
  `curl --cacert`.
- **Let's Encrypt**: not set up here. Traefik can do it with a certificate
  resolver (`--certificatesResolvers.le.acme...` flags in
  `compose.gateway.yaml`, and `tls.certResolver` on the routers), for a host
  the internet can reach.

## Files

| File | |
| --- | --- |
| `compose.gateway.yaml` | Traefik, as uid 1000 on a read-only root, its static configuration as flags |
| `common.yml` | TLS (the certificate, TLS 1.2 or later) and the security headers; copied next to the routes |
| `init_gateway.sh` | Certificates, start, every tenant's route and edge network |
