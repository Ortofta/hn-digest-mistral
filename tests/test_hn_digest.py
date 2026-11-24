import base64
import json
import smtplib
import uuid
from types import SimpleNamespace

import httpx
from mistralai.workflows.testing import create_test_worker

import worker.workflows.hn_digest as hn

ALGOLIA_PAYLOAD = {
    "hits": [
        {
            "objectID": "1001",
            "title": "BigChip unveils next-gen AI accelerator",
            "url": "https://www.theverge.com/2026/1/31/bigchip-accelerator",
            "points": 512,
            "num_comments": 340,
            "author": "reporter1",
            "created_at": "2026-01-31T10:00:00.000Z",
        },
        {
            "objectID": "1002",
            "title": "My personal blog: thoughts on slow living",
            "url": "https://somepersonalblog.wordpress.com/2026/01/30/slow-living",
            "points": 4,
            "num_comments": 2,
            "author": "blogger",
            "created_at": "2026-01-31T09:00:00.000Z",
        },
        {
            "objectID": "1003",
            "title": "Ask HN: What is your morning routine?",
            "url": None,
            "points": 12,
            "num_comments": 58,
            "author": "asker",
            "created_at": "2026-01-31T08:00:00.000Z",
        },
    ]
}

PLAN = {
    "subject": "HN Tech Digest - 2026-01-31",
    "digest_markdown": "### BigChip unveils next-gen AI accelerator\n\nBigChip announced...\n",
    "stories": [
        {
            "title": "BigChip unveils next-gen AI accelerator",
            "url": "https://www.theverge.com/2026/1/31/bigchip-accelerator",
            "points": 512,
            "num_comments": 340,
            "summary": "New accelerator doubles inference throughput.",
        }
    ],
}

SCRIPT = "Welcome to the Hacker News tech digest. First up: BigChip unveiled its next-gen AI accelerator..."

WEEKEND_PAYLOAD = {
    "hits": [
        {
            "objectID": "2002",
            "title": "Weekend read: open-source database hits 1.0",
            "url": "https://example.com/oss-db-1-0",
            "points": 40,
            "num_comments": 12,
            "author": "ossdev",
            "created_at": "2026-11-07T15:00:00.000Z",
        },
        {
            "objectID": "2001",
            "title": "EU parliament passes AI Act implementation rules",
            "url": "https://www.reuters.com/technology/eu-ai-act-rules",
            "points": 640,
            "num_comments": 410,
            "author": "reporter2",
            "created_at": "2026-11-08T09:00:00.000Z",
        },
    ]
}


class FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeAsyncClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None):
        FakeAsyncClient.calls.append((url, params))
        if url == hn.HN_FRONT_PAGE_URL:
            assert params == {"tags": "front_page", "hitsPerPage": 30}
            return FakeHTTPResponse(ALGOLIA_PAYLOAD)
        if url == hn.HN_SEARCH_BY_DATE_URL:
            assert params["tags"] == "story"
            assert params["hitsPerPage"] == 100
            assert params["numericFilters"].startswith("created_at_i>")
            return FakeHTTPResponse(WEEKEND_PAYLOAD)
        raise AssertionError(f"unexpected url: {url}")


class FakeChat:
    def __init__(self, scripted=None):
        self.calls = []
        self.scripted = list(scripted) if scripted else None

    async def complete_async(self, **kwargs):
        self.calls.append(kwargs)
        if self.scripted:
            content = self.scripted.pop(0)
        elif kwargs.get("response_format"):
            system = kwargs["messages"][0]["content"]
            content = json.dumps({"script": SCRIPT}) if "podcast" in system else json.dumps(PLAN)
        else:
            content = SCRIPT
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


class FakeSpeech:
    def __init__(self):
        self.calls = []

    async def complete_async(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(audio_data=base64.b64encode(b"FAKE-MP3-BYTES").decode())


class FakeMistralClient:
    def __init__(self, scripted=None):
        self.chat = FakeChat(scripted)
        self.audio = SimpleNamespace(speech=FakeSpeech())


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logins = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def ehlo(self):
        return None

    def starttls(self):
        return None

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, message):
        self.sent.append(message)


def _install_fakes(monkeypatch, scripted=None):
    fake_client = FakeMistralClient(scripted)
    monkeypatch.setattr(hn, "get_mistral_client", lambda: fake_client)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.instances = []
    FakeAsyncClient.calls = []
    monkeypatch.setenv("ICLOUD_EMAIL", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx-yyyy-zzzz-wwww")
    return fake_client


async def _run_workflow(temporal_env, workflow_input):
    activities = [hn.fetch_stories, hn.build_digest, hn.write_podcast_script, hn.deliver_digest]
    async with create_test_worker(temporal_env, [hn.HnTechDigestWorkflow], activities=activities):
        handle = await temporal_env.client.start_workflow(
            "hn-tech-digest",
            workflow_input,
            id=f"test-{uuid.uuid4()}",
            task_queue="test-task-queue",
        )
        return await handle.result()


async def test_full_run_sends_email_with_audio(monkeypatch, temporal_env):
    fake_client = _install_fakes(monkeypatch)

    result = await _run_workflow(temporal_env, {"dry_run": False})

    assert result["subject"] == PLAN["subject"]
    assert result["delivery"]["email_sent"] is True
    assert result["delivery"]["recipient"] == "me@icloud.com"
    assert result["delivery"]["audio_bytes"] == len(b"FAKE-MP3-BYTES")
    assert result["podcast_script"] == SCRIPT
    assert result["stories"][0]["title"] == PLAN["stories"][0]["title"]

    digest_call = fake_client.chat.calls[0]
    listing = digest_call["messages"][1]["content"]
    assert "BigChip unveils next-gen AI accelerator" in listing
    assert "author: asker | host: news.ycombinator.com" in listing

    tts_call = fake_client.audio.speech.calls[0]
    assert tts_call["input"] == SCRIPT
    assert tts_call["response_format"] == "mp3"

    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == (hn.ICLOUD_SMTP_HOST, hn.ICLOUD_SMTP_PORT)
    assert smtp.logins == [("me@icloud.com", "xxxx-yyyy-zzzz-wwww")]
    assert len(smtp.sent) == 1
    message = smtp.sent[0]
    assert message["To"] == "me@icloud.com"
    assert message["From"] == "me@icloud.com"
    assert message["Subject"] == PLAN["subject"]
    parts = list(message.walk())
    content_types = [p.get_content_type() for p in parts]
    assert "text/plain" in content_types
    assert "audio/mpeg" in content_types
    audio_part = next(p for p in parts if p.get_content_type() == "audio/mpeg")
    assert audio_part.get_filename() == "hn-tech-digest.mp3"
    assert base64.b64decode(audio_part.get_payload()) == b"FAKE-MP3-BYTES"


async def test_dry_run_skips_email(monkeypatch, temporal_env):
    _install_fakes(monkeypatch)

    result = await _run_workflow(temporal_env, {"dry_run": True})

    assert result["delivery"]["email_sent"] is False
    assert result["delivery"]["dry_run"] is True
    assert result["delivery"]["audio_bytes"] == len(b"FAKE-MP3-BYTES")
    assert FakeSMTP.instances == []


async def test_recipient_override(monkeypatch, temporal_env):
    _install_fakes(monkeypatch)

    result = await _run_workflow(temporal_env, {"dry_run": False, "recipient": "other@me.com"})

    assert result["delivery"]["recipient"] == "other@me.com"
    assert FakeSMTP.instances[0].sent[0]["To"] == "other@me.com"


def test_effective_lookback_days():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    stockholm = ZoneInfo(hn.STOCKHOLM_TZ)
    monday = datetime(2026, 11, 9, 12, 0, tzinfo=stockholm)
    tuesday = datetime(2026, 11, 10, 12, 0, tzinfo=stockholm)
    assert monday.weekday() == 0
    assert hn._effective_lookback_days(3, now=monday) == 3
    assert hn._effective_lookback_days(3, now=tuesday) == 0
    assert hn._effective_lookback_days(0, now=monday) == 0


async def test_monday_lookback_fetches_weekend_stories(monkeypatch, temporal_env):
    fake_client = _install_fakes(monkeypatch)
    monkeypatch.setattr(hn, "_effective_lookback_days", lambda days, now=None: days)

    result = await _run_workflow(temporal_env, {"dry_run": True, "monday_lookback_days": 3})

    urls = [url for url, _ in FakeAsyncClient.calls]
    assert hn.HN_SEARCH_BY_DATE_URL in urls
    assert hn.HN_FRONT_PAGE_URL not in urls
    listing = fake_client.chat.calls[0]["messages"][1]["content"]
    assert "EU parliament passes AI Act implementation rules" in listing
    assert "Weekend read: open-source database hits 1.0" in listing
    assert result["delivery"]["dry_run"] is True


def test_extract_json():
    assert json.loads(hn._extract_json(json.dumps(PLAN)))["subject"] == PLAN["subject"]
    fenced = "```json\n" + json.dumps(PLAN) + "\n```"
    assert json.loads(hn._extract_json(fenced))["subject"] == PLAN["subject"]
    wrapped = "Here is the digest:\n" + json.dumps(PLAN) + "\nHope that helps!"
    assert json.loads(hn._extract_json(wrapped))["subject"] == PLAN["subject"]


async def test_build_digest_recovers_from_empty_content(monkeypatch, temporal_env):
    fake_client = _install_fakes(monkeypatch, scripted=["", json.dumps(PLAN), json.dumps({"script": SCRIPT})])

    result = await _run_workflow(temporal_env, {"dry_run": True})

    assert result["subject"] == PLAN["subject"]
    assert result["podcast_script"] == SCRIPT
    digest_calls = [c for c in fake_client.chat.calls if "podcast" not in c["messages"][0]["content"]]
    assert len(digest_calls) == 2
    assert digest_calls[0]["response_format"]["type"] == "json_object"
    assert digest_calls[1]["response_format"]["type"] == "json_schema"
    assert digest_calls[1]["max_tokens"] == 4096
