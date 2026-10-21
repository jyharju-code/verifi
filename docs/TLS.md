# TLS certificate

One Let's Encrypt certificate, named `verifi.cloud`, covers `verifi.cloud`
and `www.verifi.cloud`. nginx reads it from the `certbot-etc` volume, and
the port 80 server answers the webroot challenge for both names.

## Adding www to the certificate (one time)

`www.verifi.cloud` only redirects to `verifi.cloud`, but the browser checks
the certificate before it follows the redirect. Run this once on the server
as root (Hetzner console), with the turn. The `verifi` wrapper has no
certificate command, so this one step runs certbot through compose directly;
it touches only the certbot volumes, never a running service:

```bash
cd /root/verifi
docker compose -f deploy/docker-compose.yml --env-file .env --profile certbot \
  run --rm certbot certonly --webroot -w /var/www/certbot \
  --cert-name verifi.cloud -d verifi.cloud -d www.verifi.cloud \
  --expand --non-interactive
VERIFI_ACTOR=juhana verifi restart nginx
```

The certificate path does not change, so no nginx config edit follows.

Check:

```bash
curl -sI https://www.verifi.cloud | head -3
```

The answer is `301` with `location: https://verifi.cloud/`.

## Renewal

Renewal uses the same command family (`certbot renew`). The expansion keeps
both names, so a renewal keeps covering www as well.
