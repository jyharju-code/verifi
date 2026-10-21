# TLS certificate

One Let's Encrypt certificate, named `verifi.cloud`, covers `verifi.cloud`
and `www.verifi.cloud`. nginx reads it from the `certbot-etc` volume, and
the port 80 server answers the webroot challenge for both names.
`www.verifi.cloud` only redirects to `verifi.cloud`, but the browser checks
the certificate before it follows the redirect, so www must be on it.

## The cert command

`verifi cert` issues, expands or renews the certificate by webroot and then
reloads nginx. It is idempotent: certbot does nothing until the certificate
is near expiry or a name is missing.

- From GitHub: Actions, Server ops, Run workflow, command `cert`.
- Automatically: the Server ops workflow runs `cert` every Monday, which is
  the renewal.
- On the server (Hetzner console), with the turn:
  `VERIFI_ACTOR=juhana verifi cert`

A new remote command reaches the server only after a deploy, because the
forced-command guard updates itself from main on mutating commands.

Check:

```bash
curl -sI https://www.verifi.cloud | head -3
```

The answer is `301` with `location: https://verifi.cloud/`.
