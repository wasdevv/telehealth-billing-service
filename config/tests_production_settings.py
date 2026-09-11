"""Requirement 15, and the boot guard that makes it matter.

Security settings that live in a file nobody imports are settings that do not exist.
These import the module for real, with the environment a deployment would have.

Both modules are reloaded, not just production: `from .base import *` copies whatever
base computed at *its* import time, so reloading only production would test the values
the test settings happened to leave behind.
"""

import importlib
import re

import pytest


def _load_production(monkeypatch, **env):
    defaults = {
        "DJANGO_SECRET_KEY": "a-real-looking-production-secret-key-value",
        "INTERNAL_SERVICE_TOKEN": "shared-token",
        "STRIPE_SECRET_KEY": "sk_live_x",
        "STRIPE_WEBHOOK_SECRET": "whsec_x",
        "DJANGO_ALLOWED_HOSTS": "billing.example.com",
    }
    defaults.update(env)
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)

    importlib.reload(importlib.import_module("config.settings.base"))
    return importlib.reload(importlib.import_module("config.settings.production"))


@pytest.fixture(autouse=True)
def restore_base_settings():
    """Leave the module registry as it was, so later tests keep the test settings."""
    yield
    importlib.reload(importlib.import_module("config.settings.base"))


def test_transport_security_is_on(monkeypatch):
    production = _load_production(monkeypatch)

    assert production.DEBUG is False
    assert production.SECURE_SSL_REDIRECT is True
    assert production.SECURE_HSTS_SECONDS >= 31_536_000
    assert production.SECURE_HSTS_INCLUDE_SUBDOMAINS is True
    assert production.SECURE_HSTS_PRELOAD is True
    assert production.SESSION_COOKIE_SECURE is True
    assert production.SESSION_COOKIE_HTTPONLY is True
    assert production.CSRF_COOKIE_SECURE is True
    assert production.SECURE_CONTENT_TYPE_NOSNIFF is True
    assert production.X_FRAME_OPTIONS == "DENY"


def test_the_health_check_is_excluded_from_the_ssl_redirect(monkeypatch):
    """A load balancer probing over plain HTTP reads a 301 as "not healthy"."""
    production = _load_production(monkeypatch)

    # Asserting against Django's real matching rule — it strips the leading slash before
    # matching — is the point. A pattern that never matches anything would otherwise sit
    # in the settings file looking correct.
    patterns = [re.compile(pattern) for pattern in production.SECURE_REDIRECT_EXEMPT]
    assert any(pattern.search("api/v1/health/") for pattern in patterns)
    assert not any(pattern.search("api/v1/invoices/") for pattern in patterns)


def test_the_ssl_redirect_can_be_turned_off_for_a_network_without_a_tls_terminator(monkeypatch):
    """Compose and service meshes have no proxy in front; a 301 there breaks the contract."""
    production = _load_production(monkeypatch, DJANGO_SECURE_SSL_REDIRECT="false")

    assert production.SECURE_SSL_REDIRECT is False
    # Everything else stays on: this switch is about transport topology, not about
    # relaxing security generally.
    assert production.SESSION_COOKIE_SECURE is True
    assert production.SECURE_HSTS_SECONDS >= 31_536_000


def test_allowed_hosts_is_parsed_from_the_environment(monkeypatch):
    production = _load_production(monkeypatch, DJANGO_ALLOWED_HOSTS="a.example.com, b.example.com")

    assert production.ALLOWED_HOSTS == ["a.example.com", "b.example.com"]


@pytest.mark.parametrize(
    "missing",
    ["DJANGO_SECRET_KEY", "INTERNAL_SERVICE_TOKEN", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"],
)
def test_a_missing_secret_stops_the_boot(monkeypatch, missing):
    with pytest.raises(RuntimeError, match=missing):
        _load_production(monkeypatch, **{missing: ""})


def test_an_insecure_development_key_is_refused(monkeypatch):
    with pytest.raises(RuntimeError, match="DJANGO_SECRET_KEY"):
        _load_production(monkeypatch, DJANGO_SECRET_KEY="insecure-development-key-do-not-use-in-production")


def test_allowed_hosts_must_be_set(monkeypatch):
    with pytest.raises(RuntimeError, match="DJANGO_ALLOWED_HOSTS"):
        _load_production(monkeypatch, DJANGO_ALLOWED_HOSTS="")
