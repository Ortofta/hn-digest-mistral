from datetime import timedelta

import mistralai.workflows as workflows
from mistralai.workflows.client import get_mistral_client
from pydantic import BaseModel, Field

HN_FRONT_PAGE_URL = "https://hn.algolia.com/api/v1/search"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={object_id}"
ICLOUD_SMTP_HOST = "smtp.mail.me.com"
ICLOUD_SMTP_PORT = 587
DEFAULT_CHAT_MODEL = "mistral-large-latest"
DEFAULT_TTS_MODEL = "voxtral-mini-tts-3-0"
DEFAULT_VOICE_ID = "gb_jane_neutral"


class DigestInput(BaseModel):
    recipient: str | None = Field(
        default=None,
        description="iCloud address that receives the digest. Defaults to the ICLOUD_EMAIL secret.",
    )
    voice_id: str = Field(default=DEFAULT_VOICE_ID, description="Voxtral TTS preset voice slug.")
    max_stories: int = Field(default=8, ge=1, le=15, description="Maximum number of stories in the digest.")
    chat_model: str = Field(default=DEFAULT_CHAT_MODEL, description="Model used for filtering, summarizing and scripting.")
    tts_model: str = Field(default=DEFAULT_TTS_MODEL, description="TTS model used for the podcast audio.")
    dry_run: bool = Field(default=False, description="Build the digest and audio but do not send the email.")


class HNItem(BaseModel):
    object_id: str
    title: str
    url: str
    points: int
    num_comments: int
    author: str
    created_at: str


class StoryPick(BaseModel):
    title: str
    url: str
    points: int
    num_comments: int
    summary: str


class DigestPlan(BaseModel):
    subject: str
    digest_markdown: str
    stories: list[StoryPick]


class DeliveryResult(BaseModel):
    recipient: str
    email_sent: bool
    dry_run: bool
    audio_bytes: int
    audio_format: str = "mp3"


class DigestOutput(BaseModel):
    subject: str
    digest_markdown: str
    podcast_script: str
    stories: list[StoryPick]
    delivery: DeliveryResult


DIGEST_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "description": "Email subject, e.g. 'HN Tech Digest - 2026-01-31'."},
        "digest_markdown": {"type": "string", "description": "Markdown digest: one '### headline' section per story with a 2-3 sentence summary and the source URL."},
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "points": {"type": "integer"},
                    "num_comments": {"type": "integer"},
                    "summary": {"type": "string", "description": "One or two sentences on why this matters to the tech industry."},
                },
                "required": ["title", "url", "points", "num_comments", "summary"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["subject", "digest_markdown", "stories"],
    "additionalProperties": False,
}

DIGEST_SYSTEM_PROMPT = """You are the editor of a daily tech-industry news digest built from the Hacker News front page.

Select up to {max_stories} items that are genuine tech-industry news: product launches, company and funding news,
AI models and research, semiconductors and hardware, developer tools and infrastructure, cybersecurity,
meaningful open-source releases, and regulation or policy that materially affects the tech industry.

Exclude personal blogs, essays and opinion pieces, politics, non-tech science, Ask HN / Tell HN threads,
job posts, and duplicate coverage of the same event. Prefer primary sources and notable outlets over commentary.

Return JSON matching the provided schema: a short email subject with today's date, a markdown digest with one
'### headline' section per story (2-3 sentence summary plus the source URL), and the stories array."""

SCRIPT_SYSTEM_PROMPT = """You write short podcast scripts. Write a conversational monologue of 200-260 words
(roughly 90 seconds at a natural speaking pace) presenting a tech news digest.
Start with a one-line intro, then cover each story: speak the headline, then one or two sentences of takeaway.
Do not read URLs aloud; say that full links are in the email. End with a one-line sign-off.
Plain text only: no markdown, no bullet markers, no stage directions, no phonetic spellings."""


def _message_text(content) -> str:
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content


@workflows.activity(start_to_close_timeout=timedelta(seconds=60), retry_policy_max_attempts=3)
async def fetch_front_page(hits_per_page: int = 30) -> list[HNItem]:
    import httpx

    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "hn-digest-workflow/1.0"}) as http:
        response = await http.get(HN_FRONT_PAGE_URL, params={"tags": "front_page", "hitsPerPage": hits_per_page})
        response.raise_for_status()
        data = response.json()
    items: list[HNItem] = []
    for hit in data.get("hits", []):
        object_id = str(hit.get("objectID", ""))
        items.append(
            HNItem(
                object_id=object_id,
                title=hit.get("title") or "",
                url=hit.get("url") or HN_ITEM_URL.format(object_id=object_id),
                points=hit.get("points") or 0,
                num_comments=hit.get("num_comments") or 0,
                author=hit.get("author") or "",
                created_at=hit.get("created_at") or "",
            )
        )
    return items


@workflows.activity(start_to_close_timeout=timedelta(minutes=3), retry_policy_max_attempts=3)
async def build_digest(stories: list[HNItem], max_stories: int, chat_model: str) -> DigestPlan:
    from datetime import datetime, timezone
    from urllib.parse import urlparse

    client = get_mistral_client()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    listing = "\n".join(
        f"- [{i}] {s.title} | {s.points} points, {s.num_comments} comments | author: {s.author} | host: {urlparse(s.url).netloc}"
        for i, s in enumerate(stories)
    )
    user_prompt = (
        f"Today is {today}. Here are the current Hacker News front-page items (index, title, score, comments, author, host):\n\n"
        f"{listing}\n\nSelect the tech-industry stories and return the digest as JSON."
    )
    response = await client.chat.complete_async(
        model=chat_model,
        messages=[
            {"role": "system", "content": DIGEST_SYSTEM_PROMPT.format(max_stories=max_stories)},
            {"role": "user", "content": user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "digest_plan", "schema": DIGEST_JSON_SCHEMA, "strict": True},
        },
        temperature=0.3,
    )
    plan = DigestPlan.model_validate_json(_message_text(response.choices[0].message.content))
    plan.stories = plan.stories[:max_stories]
    if not plan.subject:
        plan.subject = f"HN Tech Digest - {today}"
    return plan


@workflows.activity(start_to_close_timeout=timedelta(minutes=3), retry_policy_max_attempts=3)
async def write_podcast_script(plan: DigestPlan, chat_model: str) -> str:
    from datetime import datetime, timezone

    client = get_mistral_client()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    user_prompt = f"Today is {today}. Write the podcast script for this digest:\n\nSubject: {plan.subject}\n\n{plan.digest_markdown}"
    response = await client.chat.complete_async(
        model=chat_model,
        messages=[
            {"role": "system", "content": SCRIPT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=1024,
    )
    return _message_text(response.choices[0].message.content).strip()


def _require_env(name: str) -> str:
    import os

    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Environment variable {name} is not set. Bind the corresponding workspace secret on the deployment."
        )
    return value


@workflows.activity(start_to_close_timeout=timedelta(minutes=5), retry_policy_max_attempts=3)
async def deliver_digest(
    recipient: str,
    subject: str,
    digest_markdown: str,
    script: str,
    voice_id: str,
    tts_model: str,
    dry_run: bool,
) -> DeliveryResult:
    import base64
    import smtplib
    from email import encoders
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    client = get_mistral_client()
    speech = await client.audio.speech.complete_async(
        model=tts_model,
        input=script,
        voice_id=voice_id,
        response_format="mp3",
    )
    audio_bytes = base64.b64decode(speech.audio_data)

    if dry_run:
        return DeliveryResult(recipient=recipient, email_sent=False, dry_run=True, audio_bytes=len(audio_bytes))

    sender = _require_env("ICLOUD_EMAIL")
    app_password = _require_env("ICLOUD_APP_PASSWORD")
    recipient = recipient or sender

    message = MIMEMultipart("mixed")
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.attach(MIMEText(digest_markdown, "plain", "utf-8"))

    attachment = MIMEBase("audio", "mpeg")
    attachment.set_payload(audio_bytes)
    encoders.encode_base64(attachment)
    attachment.add_header("Content-Disposition", "attachment", filename="hn-tech-digest.mp3")
    message.attach(attachment)

    with smtplib.SMTP(ICLOUD_SMTP_HOST, ICLOUD_SMTP_PORT, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(sender, app_password)
        smtp.send_message(message)

    return DeliveryResult(recipient=recipient, email_sent=True, dry_run=False, audio_bytes=len(audio_bytes))


@workflows.workflow.define(
    name="hn-tech-digest",
    workflow_display_name="HN Tech Digest",
    workflow_description=(
        "One-shot digest of the Hacker News front page, filtered to tech-industry news. "
        "Summarizes the top stories, renders a 1-2 minute podcast-style MP3, and emails both to an iCloud inbox."
    ),
)
class HnTechDigestWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: DigestInput) -> DigestOutput:
        items = await fetch_front_page(30)
        plan = await build_digest(items, input.max_stories, input.chat_model)
        script = await write_podcast_script(plan, input.chat_model)
        delivery = await deliver_digest(
            recipient=input.recipient or "",
            subject=plan.subject,
            digest_markdown=plan.digest_markdown,
            script=script,
            voice_id=input.voice_id,
            tts_model=input.tts_model,
            dry_run=input.dry_run,
        )
        return DigestOutput(
            subject=plan.subject,
            digest_markdown=plan.digest_markdown,
            podcast_script=script,
            stories=plan.stories,
            delivery=delivery,
        )
