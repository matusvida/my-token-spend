import json
import platform
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
KEYCHAIN_SERVICE = "Claude Code-credentials"
SAMPLES_NAME = "quota_samples.jsonl"
TIMEOUT_SECONDS = 10
PER_MODEL_BUCKETS = ("seven_day_opus", "seven_day_sonnet")
EXTRA_USAGE_FIELDS = ("is_enabled", "monthly_limit", "used_credits", "currency", "decimal_places")
WEEK = timedelta(days=7)


class QuotaError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def samples_path(data_dir):
    return Path(data_dir) / SAMPLES_NAME


def default_credentials_path():
    return Path.home() / ".claude" / ".credentials.json"


def _keychain_payload():
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def read_token(path=None, now=None):
    now = now or datetime.now(timezone.utc)
    path = Path(path) if path else default_credentials_path()
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, "the credentials at %s are not readable JSON" % path
    else:
        payload = _keychain_payload()
        if payload is None:
            return None, "no credentials at %s and none in the login keychain" % path
    oauth = (payload or {}).get("claudeAiOauth")
    token = (oauth or {}).get("accessToken")
    if not token:
        return None, "the credentials carry no claudeAiOauth.accessToken"
    expires = (oauth or {}).get("expiresAt")
    if isinstance(expires, (int, float)):
        expiry = datetime.fromtimestamp(expires / 1000.0, timezone.utc)
        if expiry <= now:
            return None, (
                "the stored OAuth token expired at %s; Claude Code owns the refresh rotation"
                % expiry.isoformat()
            )
    return token, None


def fetch(token, url=USAGE_URL, timeout=TIMEOUT_SECONDS, opener=None):
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token, "anthropic-beta": OAUTH_BETA}
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise QuotaError("the usage endpoint answered %s" % status, status)
            body = response.read()
    except urllib.error.HTTPError as error:
        raise QuotaError("the usage endpoint answered %s" % error.code, error.code)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
        raise QuotaError("the usage endpoint was unreachable within %ds (%s)" % (timeout, type(error).__name__))
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise QuotaError("the usage endpoint answered 200 with a body that is not JSON")
    if not isinstance(payload, dict):
        raise QuotaError("the usage endpoint answered 200 with a body that is not an object")
    return payload


def _bucket(payload, name):
    block = payload.get(name)
    return block if isinstance(block, dict) else {}


def sample_from(payload, now, weighted_so_far):
    seven, five = _bucket(payload, "seven_day"), _bucket(payload, "five_hour")
    extra = _bucket(payload, "extra_usage")
    per_model = {}
    for name in PER_MODEL_BUCKETS:
        block = _bucket(payload, name)
        if block.get("utilization") is not None:
            per_model[name] = {"pct": block.get("utilization"), "resets_at": block.get("resets_at")}
    return {
        "ts": now.isoformat(),
        "seven_day_pct": seven.get("utilization"),
        "seven_day_resets_at": seven.get("resets_at"),
        "five_hour_pct": five.get("utilization"),
        "five_hour_resets_at": five.get("resets_at"),
        "per_model": per_model,
        "extra_usage": {field: extra.get(field) for field in EXTRA_USAGE_FIELDS} if extra else {},
        "weighted_so_far": round(float(weighted_so_far), 4),
    }


def append_sample(data_dir, sample):
    path = samples_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, sort_keys=True) + "\n")
    return path


def poll(data_dir, weighted_so_far, now=None, credentials_path=None, fetcher=None):
    now = now or datetime.now(timezone.utc)
    token, reason = read_token(credentials_path, now=now)
    if token is None:
        return {"sample": None, "skipped": reason, "status": None}
    try:
        payload = (fetcher or fetch)(token)
    except QuotaError as error:
        return {"sample": None, "skipped": str(error), "status": getattr(error, "status", None)}
    sample = sample_from(payload, now, weighted_so_far)
    if sample["seven_day_pct"] is None or not sample["seven_day_resets_at"]:
        return {"sample": None, "skipped": "the usage endpoint returned no seven_day bucket", "status": 200}
    append_sample(data_dir, sample)
    return {"sample": sample, "skipped": None, "status": 200}


def extra_usage_unused(samples, start, end):
    seen = None
    for sample in samples:
        stamp = _parse(sample.get("ts"))
        if stamp is None or not start <= stamp < end:
            continue
        extra = sample.get("extra_usage") or {}
        if not extra.get("is_enabled") or (extra.get("used_credits") or 0) != 0:
            return None
        seen = extra
    return seen


def load_samples(data_dir):
    path = samples_path(data_dir)
    if not path.exists():
        return []
    samples = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("ts"):
            samples.append(entry)
    samples.sort(key=lambda entry: entry["ts"])
    return samples


def _parse(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def reset_instants(samples):
    # The endpoint jitters resets_at by fractions of a second between calls; a real shift is minutes at least.
    seen = set()
    for sample in samples:
        moment = _parse(sample.get("seven_day_resets_at"))
        if moment is not None:
            seen.add(moment.replace(second=0, microsecond=0) + timedelta(minutes=round(moment.second / 60.0)))
    return sorted(seen)


def latest_sample(samples):
    return samples[-1] if samples else None


def sample_age_hours(sample, now=None):
    moment = _parse((sample or {}).get("ts"))
    if moment is None:
        return None
    return ((now or datetime.now(timezone.utc)) - moment).total_seconds() / 3600.0


def window_end(start, instants):
    end = start + WEEK
    for moment in instants or ():
        if start < moment < end:
            return moment
    return end


def window_instant(ts_utc, instants):
    if not instants:
        return None
    base = instants[0]
    for moment in instants:
        if moment <= ts_utc:
            base = moment
    while base > ts_utc:
        base -= WEEK
    while True:
        end = window_end(base, instants)
        if ts_utc < end:
            return base
        base = end
