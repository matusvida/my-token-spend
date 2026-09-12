import json
import socket
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import quota

NOW = datetime(2026, 9, 12, 17, 0, tzinfo=timezone.utc)
TOKEN = "sk-ant-oat01-not-a-real-token"

RECORDED_200 = {
    "five_hour": {"utilization": 18.0, "resets_at": "2026-09-12T18:20:00.046968+00:00"},
    "seven_day": {"utilization": 3.0, "resets_at": "2026-09-19T03:00:00.046991+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": {"utilization": 2.0, "resets_at": "2026-09-19T03:00:00.046991+00:00"},
    "extra_usage": {
        "is_enabled": True,
        "monthly_limit": 30000,
        "used_credits": 0.0,
        "currency": "EUR",
        "decimal_places": 2,
        "user_disabled": False,
    },
    "limits": [{"kind": "weekly_all", "percent": 3, "severity": "normal"}],
}


def credentials(tmp_path, **overrides):
    payload = {
        "claudeAiOauth": dict(
            {
                "accessToken": TOKEN,
                "refreshToken": "sk-ant-ort01-not-a-real-token",
                "expiresAt": int((NOW + timedelta(hours=5)).timestamp() * 1000),
            },
            **overrides,
        )
    }
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_the_token_comes_from_the_credentials_file(tmp_path):
    token, reason = quota.read_token(credentials(tmp_path), now=NOW)
    assert token == TOKEN
    assert reason is None


def test_a_missing_credentials_file_is_a_skip_not_a_crash(tmp_path):
    token, reason = quota.read_token(tmp_path / "absent.json", now=NOW)
    assert token is None
    assert "no credentials" in reason


def test_unparseable_credentials_are_a_skip(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text("{ not json", encoding="utf-8")
    token, reason = quota.read_token(path, now=NOW)
    assert token is None
    assert "readable JSON" in reason


def test_credentials_without_an_access_token_are_a_skip(tmp_path):
    token, reason = quota.read_token(credentials(tmp_path, accessToken=None), now=NOW)
    assert token is None
    assert "accessToken" in reason


def test_an_expired_token_is_never_used_and_never_refreshed(tmp_path):
    expired = credentials(tmp_path, expiresAt=int((NOW - timedelta(minutes=1)).timestamp() * 1000))
    token, reason = quota.read_token(expired, now=NOW)
    assert token is None
    assert "expired" in reason
    assert "refresh" in reason.lower()


def test_no_skip_reason_ever_carries_the_token(tmp_path):
    for path in (tmp_path / "absent.json", credentials(tmp_path, expiresAt=0)):
        _, reason = quota.read_token(path, now=NOW)
        assert TOKEN not in (reason or "")


def test_the_keychain_is_read_only_when_the_file_is_missing_on_macos(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": json.dumps({"claudeAiOauth": {"accessToken": TOKEN}})})()

    monkeypatch.setattr(quota.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(quota.subprocess, "run", fake_run)
    token, reason = quota.read_token(tmp_path / "absent.json", now=NOW)
    assert token == TOKEN
    assert reason is None
    assert calls == [["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]]


def test_the_keychain_is_not_consulted_off_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(quota.platform, "system", lambda: "Windows")
    monkeypatch.setattr(quota.subprocess, "run", lambda *a, **k: pytest.fail("keychain was consulted"))
    token, _ = quota.read_token(tmp_path / "absent.json", now=NOW)
    assert token is None


def test_a_200_response_is_parsed(monkeypatch):
    seen = {}

    def opener(request, timeout=None):
        seen["headers"] = request.headers
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return FakeResponse(RECORDED_200)

    payload = quota.fetch(TOKEN, opener=opener)
    assert payload["seven_day"]["utilization"] == 3.0
    assert seen["url"] == quota.USAGE_URL
    assert seen["headers"]["Authorization"] == "Bearer " + TOKEN
    assert seen["headers"]["Anthropic-beta"] == quota.OAUTH_BETA
    assert seen["timeout"] == 10


def test_a_401_becomes_a_quota_error_without_the_token():
    def opener(request, timeout=None):
        raise urllib.error.HTTPError(quota.USAGE_URL, 401, "Unauthorized", {}, None)

    with pytest.raises(quota.QuotaError) as caught:
        quota.fetch(TOKEN, opener=opener)
    assert "401" in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_a_timeout_becomes_a_quota_error():
    def opener(request, timeout=None):
        raise socket.timeout("timed out")

    with pytest.raises(quota.QuotaError) as caught:
        quota.fetch(TOKEN, opener=opener)
    assert "10" in str(caught.value)


def test_a_network_error_becomes_a_quota_error():
    def opener(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    with pytest.raises(quota.QuotaError):
        quota.fetch(TOKEN, opener=opener)


def test_a_non_json_body_becomes_a_quota_error():
    class Garbage(FakeResponse):
        def read(self):
            return b"<html>nope</html>"

    with pytest.raises(quota.QuotaError):
        quota.fetch(TOKEN, opener=lambda request, timeout=None: Garbage({}))


def test_the_sample_carries_the_window_spend_so_far():
    sample = quota.sample_from(RECORDED_200, NOW, 1234.5)
    assert sample["ts"] == NOW.isoformat()
    assert sample["seven_day_pct"] == 3.0
    assert sample["seven_day_resets_at"] == "2026-09-19T03:00:00.046991+00:00"
    assert sample["five_hour_pct"] == 18.0
    assert sample["weighted_so_far"] == 1234.5
    assert sample["per_model"] == {"seven_day_sonnet": {"pct": 2.0, "resets_at": "2026-09-19T03:00:00.046991+00:00"}}
    assert sample["extra_usage"]["monthly_limit"] == 30000
    assert sample["extra_usage"]["currency"] == "EUR"


def test_the_sample_never_stores_the_token():
    sample = quota.sample_from(RECORDED_200, NOW, 1.0)
    assert TOKEN not in json.dumps(sample)


def test_poll_appends_a_sample(tmp_path):
    result = quota.poll(
        tmp_path,
        weighted_so_far=900.0,
        now=NOW,
        credentials_path=credentials(tmp_path),
        fetcher=lambda token: RECORDED_200,
    )
    assert result["skipped"] is None
    assert result["sample"]["seven_day_pct"] == 3.0
    stored = quota.load_samples(tmp_path)
    assert len(stored) == 1
    assert stored[0]["weighted_so_far"] == 900.0


def test_poll_appends_rather_than_replaces(tmp_path):
    for spend in (100.0, 200.0):
        quota.poll(
            tmp_path,
            weighted_so_far=spend,
            now=NOW,
            credentials_path=credentials(tmp_path),
            fetcher=lambda token: RECORDED_200,
        )
    assert [s["weighted_so_far"] for s in quota.load_samples(tmp_path)] == [100.0, 200.0]


def test_poll_writes_nothing_when_the_token_is_missing(tmp_path):
    result = quota.poll(
        tmp_path,
        weighted_so_far=1.0,
        now=NOW,
        credentials_path=tmp_path / "absent.json",
        fetcher=lambda token: pytest.fail("the endpoint was called without a token"),
    )
    assert result["sample"] is None
    assert "no credentials" in result["skipped"]
    assert not quota.samples_path(tmp_path).exists()


def test_poll_writes_nothing_when_the_endpoint_fails(tmp_path):
    def boom(token):
        raise quota.QuotaError("the usage endpoint answered 401")

    result = quota.poll(
        tmp_path, weighted_so_far=1.0, now=NOW, credentials_path=credentials(tmp_path), fetcher=boom
    )
    assert result["sample"] is None
    assert "401" in result["skipped"]
    assert not quota.samples_path(tmp_path).exists()


def test_poll_skips_a_response_without_a_seven_day_bucket(tmp_path):
    result = quota.poll(
        tmp_path,
        weighted_so_far=1.0,
        now=NOW,
        credentials_path=credentials(tmp_path),
        fetcher=lambda token: {"five_hour": {"utilization": 1.0}},
    )
    assert result["sample"] is None
    assert "seven_day" in result["skipped"]
    assert not quota.samples_path(tmp_path).exists()


def test_load_samples_of_an_absent_file_is_empty(tmp_path):
    assert quota.load_samples(tmp_path) == []


def test_a_malformed_sample_line_is_skipped(tmp_path):
    quota.samples_path(tmp_path).write_text(
        '{"ts": "2026-09-12T17:00:00+00:00", "seven_day_pct": 3.0}\nnot json\n\n', encoding="utf-8"
    )
    assert len(quota.load_samples(tmp_path)) == 1


def test_samples_come_back_in_time_order(tmp_path):
    quota.samples_path(tmp_path).write_text(
        '{"ts": "2026-09-12T17:00:00+00:00"}\n{"ts": "2026-09-11T17:00:00+00:00"}\n', encoding="utf-8"
    )
    assert [s["ts"] for s in quota.load_samples(tmp_path)] == [
        "2026-09-11T17:00:00+00:00",
        "2026-09-12T17:00:00+00:00",
    ]


def test_microsecond_jitter_is_one_reset_instant_not_many():
    samples = [
        {"ts": "2026-09-12T17:00:00+00:00", "seven_day_resets_at": "2026-09-19T03:00:00.046991+00:00"},
        {"ts": "2026-09-12T18:00:00+00:00", "seven_day_resets_at": "2026-09-19T03:00:00.050174+00:00"},
        {"ts": "2026-09-12T19:00:00+00:00", "seven_day_resets_at": "2026-09-19T03:00:29.900000+00:00"},
    ]
    assert quota.reset_instants(samples) == [datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)]


def test_a_genuine_shift_is_a_second_instant():
    samples = [
        {"ts": "2026-09-12T17:00:00+00:00", "seven_day_resets_at": "2026-09-19T03:00:00+00:00"},
        {"ts": "2026-09-20T17:00:00+00:00", "seven_day_resets_at": "2026-09-27T09:30:00+00:00"},
    ]
    assert quota.reset_instants(samples) == [
        datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 27, 9, 30, tzinfo=timezone.utc),
    ]


def test_a_sample_without_a_reset_instant_contributes_nothing():
    assert quota.reset_instants([{"ts": "2026-09-12T17:00:00+00:00"}]) == []
    assert quota.reset_instants([{"seven_day_resets_at": "nonsense"}]) == []


def test_latest_sample_is_the_newest_one(tmp_path):
    quota.samples_path(tmp_path).write_text(
        '{"ts": "2026-09-11T17:00:00+00:00", "seven_day_pct": 1.0}\n'
        '{"ts": "2026-09-12T17:00:00+00:00", "seven_day_pct": 3.0}\n',
        encoding="utf-8",
    )
    assert quota.latest_sample(quota.load_samples(tmp_path))["seven_day_pct"] == 3.0
    assert quota.latest_sample([]) is None


def test_sample_age_is_measured_in_hours():
    sample = {"ts": "2026-09-12T11:00:00+00:00"}
    assert quota.sample_age_hours(sample, NOW) == 6.0
    assert quota.sample_age_hours(None, NOW) is None
    assert quota.sample_age_hours({"ts": "nonsense"}, NOW) is None
