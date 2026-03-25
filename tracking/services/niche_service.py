from __future__ import annotations

import logging

from common.text import extract_keywords

from tracking.adapters.base import SeedResolution
from tracking.services.llm_gemini import GeminiError, infer_keywords_ru
from tracking.services.platform_onboarding import get_recent_seed_content_texts

logger = logging.getLogger(__name__)


def build_niche_context_text(*, seed: SeedResolution, competitors: list[SeedResolution]) -> str:
    lines: list[str] = []
    lines.append("SEED CHANNEL")
    lines.append(f"Platform: {seed.platform}")
    if seed.handle:
        lines.append(f"Handle: {seed.handle}")
    if seed.url:
        lines.append(f"URL: {seed.url}")
    if seed.title:
        lines.append(f"Title: {seed.title}")
    if seed.description:
        lines.append(f"Description: {seed.description}")

    recent = get_recent_seed_content_texts(seed=seed, n=10)
    if recent:
        lines.append("Recent content:")
        for t in recent:
            lines.append(f"- {t}")

    if competitors:
        lines.append("")
        lines.append("USER-PROVIDED COMPETITORS")
        for c in competitors[:20]:
            name = c.title or c.handle or c.external_id
            lines.append(f"Channel: {name}")
            if c.handle:
                lines.append(f"Handle: {c.handle}")
            if c.url:
                lines.append(f"URL: {c.url}")
            if c.description:
                lines.append(f"Description: {c.description}")

    return "\n".join(lines).strip()


def infer_niche_keywords(
    *,
    seed: SeedResolution,
    competitors: list[SeedResolution],
    prefer_llm: bool,
) -> tuple[list[str], str]:
    """
    Returns: (keywords, source) where source in {"llm","auto"}.
    """
    context = build_niche_context_text(seed=seed, competitors=competitors)

    if prefer_llm:
        try:
            kws = infer_keywords_ru(context_text=context)
            if kws:
                return kws, "llm"
        except GeminiError as e:
            logger.warning("LLM niche inference failed: %s", e)
        except Exception as e:
            logger.exception("Unexpected LLM niche inference error: %s", e)

    kws = extract_keywords(context, max_keywords=8)
    return kws, "auto"
