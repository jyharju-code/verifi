"""The logging config must never capture secrets, bodies, the query string, or
the claim text. These checks lock that down so a future edit cannot regress it.
"""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NGINX_LOG = (ROOT / "deploy/nginx/conf.d/00-logging.conf").read_text()
NGINX_SSL = (ROOT / "deploy/nginx/conf.d/verifi-ssl.conf").read_text()
AUDIT_SCHEMA = (ROOT / "core/db/request_audit.sql").read_text()
AUDIT_MIGRATION = (ROOT / "core/db/migrations/2026-08-10-request-audit.sql").read_text()


class NginxLogFormatSafety(unittest.TestCase):
    def test_never_logs_secrets_body_or_query(self):
        forbidden = [
            "$request_body",
            "$http_authorization",
            "$http_cookie",
            "$http_payment_signature",
            "$http_payment_response",
            "$sent_http_payment_response",
            "$http_x_payment",
            "$args",
            "$query_string",
            "$request_uri",  # carries the query string; we log $uri instead
        ]
        for token in forbidden:
            self.assertNotIn(token, NGINX_LOG, f"{token} must never appear in the access log")
        # $request holds "METHOD uri?query PROTO", so it leaks the query too.
        self.assertNotIn("$request ", NGINX_LOG)
        self.assertNotIn('"$request"', NGINX_LOG)

    def test_logs_the_required_fields(self):
        for field in [
            "$time_iso8601", "$request_id", "$realip_remote_addr", "$remote_addr",
            "$http_x_forwarded_for", "$host", "$request_method", "$uri", "$status",
            "$request_length", "$bytes_sent", "$request_time", "$upstream_response_time",
            "$upstream_addr", "$upstream_status", "$ssl_protocol", "$http_user_agent",
            "$safe_referer",
        ]:
            self.assertIn(field, NGINX_LOG, f"{field} is required in the access log")
        self.assertIn("escape=json", NGINX_LOG)

    def test_trust_boundary_is_restricted_to_local_and_tailscale(self):
        # X-Forwarded-For may be believed only from our own proxies.
        self.assertIn("real_ip_header X-Forwarded-For", NGINX_LOG)
        self.assertIn("100.64.0.0/10", NGINX_LOG)  # Tailscale CGNAT
        self.assertIn("set_real_ip_from 127.0.0.1", NGINX_LOG)
        # The admin location, which can carry ?token=, must not be logged.
        self.assertIn("access_log off", NGINX_SSL)
        self.assertIn("error_log /dev/null crit", NGINX_SSL)

    def test_referer_query_is_not_logged(self):
        self.assertIn("map $http_referer $safe_referer", NGINX_LOG)
        log_format = NGINX_LOG.split("log_format json_access", 1)[1]
        self.assertNotIn("$http_referer", log_format)

    def test_request_id_is_returned_and_forwarded(self):
        self.assertIn("add_header X-Request-ID $request_id", NGINX_SSL)
        self.assertIn("proxy_set_header X-Request-ID $request_id", NGINX_SSL)
        # The public edge forces the audit source, so mcp cannot be forged.
        self.assertIn("proxy_set_header X-Verifi-Source rest", NGINX_SSL)


class AuditSchemaSafety(unittest.TestCase):
    @staticmethod
    def _code_only(sql: str) -> str:
        # Drop comment lines so we test the actual column definitions, not the
        # prose that explains what is deliberately excluded.
        return "\n".join(
            line for line in sql.splitlines() if not line.lstrip().startswith("--")
        ).lower()

    def test_stores_claim_digest_never_claim_text(self):
        for sql in (AUDIT_SCHEMA, AUDIT_MIGRATION):
            self.assertIn("claim_len", sql)
            self.assertIn("claim_sha256", sql)
            self.assertIn("wallet_ownership_proven", sql)
            code = self._code_only(sql)
            # No column holds the claim or intent text itself.
            self.assertNotIn("intent", code)
            self.assertNotIn("claim text", code)
            self.assertNotIn(" claim ", code)

    def test_callback_is_host_only(self):
        self.assertIn("callback_host", AUDIT_SCHEMA)
        self.assertNotIn("callback_url", AUDIT_SCHEMA)


if __name__ == "__main__":
    unittest.main()
